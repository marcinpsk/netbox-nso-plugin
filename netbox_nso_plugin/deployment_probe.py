# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Durable authority for one bounded BGP settlement probe while quiesced."""

import contextlib
import copy
from dataclasses import dataclass
from uuid import UUID

from django.db import connection, transaction
from django.utils import timezone

from . import adapter_client, deployment, refusal_contract
from .apply_settlement import EvidenceInvariantError
from .durable_capture import mapping_epoch
from .models import NSOApplyAttempt, NSOApplyExecution, NSOIntentDeploymentControl
from .refusal_registry import OVERLAYS


class ProbeBlocked(RuntimeError):
    """The selected probe has no exact durable authority."""


class ProbePending(ProbeBlocked):
    """The named admitted request has not supplied terminal execution bindings."""


def _require(condition, message):
    if not condition:
        raise ProbeBlocked(message)


@contextlib.contextmanager
def locked_gate():
    """Serialize maintenance, probe execution, revocation, and resume."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [deployment._LOCK_KEY])
        _require(cursor.fetchone()[0], "A deployment operation is still running")
        control = NSOIntentDeploymentControl.objects.select_for_update().filter(pk=1).first()
        _require(control is not None and control.quiesced_at is not None, "The deployment gate must stay quiesced")
        _require(
            control.paired_activation,
            "This action requires a paired-activation gate; run nso_intent_deployment_gate --prepare --paired-activation",
        )
        yield control


@contextlib.contextmanager
def maintenance_gate(*, verification_attempt=None):
    """Hold the gate across bounded actions that require autocommit delivery."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [deployment._LOCK_KEY])
        _require(cursor.fetchone()[0], "A deployment operation is still running")
    try:
        with locked_gate() as control:
            allowed = {None} if verification_attempt is None else {None, UUID(str(verification_attempt))}
            _require(control.verification_attempt_id in allowed, "Revoke the probe permit before maintenance")
        yield
    except Exception:
        if verification_attempt is not None:
            with locked_gate() as control:
                if control.verification_attempt_id == UUID(str(verification_attempt)):
                    control.verification_attempt = None
                    control.save(update_fields=("verification_attempt",))
        raise
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [deployment._LOCK_KEY])


@dataclass(frozen=True)
class Probe:
    attempt: NSOApplyAttempt
    execution: NSOApplyExecution
    owners: frozenset


def require_current_capture(attempt, quiesced_at):
    """Bind capture and its later admission to the current verification interval."""
    _require(
        attempt.created_at >= quiesced_at,
        "Historical probe capture cannot verify the current deployment gate",
    )


def authority(attempt_id, device_id=None, *, completed=False):
    """Derive and validate the exact authority from retained ledger records."""
    attempt = NSOApplyAttempt.objects.select_related("management__device").get(pk=attempt_id)
    management = attempt.management
    _require(device_id is None or management.device_id == device_id, "Probe device does not match")
    _require(attempt.adapter_device_id == management.adapter_device_id, "Probe adapter device changed")
    _require(attempt.source_epoch == management.adapter_source_epoch, "Probe source epoch changed")
    _require(attempt.trigger_kind == "manual" and attempt.adapter_apply_attempt_id == attempt.pk, "Probe is not manual")
    _require(isinstance(attempt.selected, dict) and set(attempt.selected) == {"bgp"}, "Probe must select only BGP")
    executions = list(attempt.executions.all())
    if not executions and attempt.lifecycle == "open":
        raise ProbePending("Probe terminal bindings are pending; keep the deployment quiesced")
    _require(len(executions) == 1, "Probe requires one bound execution")
    execution = executions[0]
    _require(
        execution.management_id == management.pk
        and execution.adapter_device_id == management.adapter_device_id
        and execution.mapping_epoch == mapping_epoch(management)
        and bool(execution.store_incarnation)
        and execution.generation_id is not None
        and execution.generation_seq is not None
        and execution.carrier_job_id is not None
        and execution.settle_seq is not None
        and not execution.invariant_code,
        "Probe execution identity is incomplete",
    )
    _require(execution.terminal_projection is not None, "Probe has no captured terminal evidence")
    generation = execution.terminal_projection
    refusal_contract.validate_generation(generation)
    from .apply_settlement import _parse_time

    with locked_gate() as control:
        require_current_capture(attempt, control.quiesced_at)
        _require(
            execution.created_at >= control.quiesced_at
            and execution.terminal_at == _parse_time(generation["updated_at"])
            and execution.terminal_at >= attempt.created_at,
            "Historical terminal execution cannot verify the current deployment gate",
        )
    _require(
        generation["generation_id"] == execution.generation_id
        and generation["seq"] == execution.generation_seq
        and generation["carrier_job_id"] == execution.carrier_job_id
        and generation["source_push_seq"] == attempt.selected
        and set(generation["stream_revisions"]) == {"bgp"}
        and generation["sections"] == ["bgp"],
        "Probe generation does not match its execution",
    )
    from .apply_settlement import _response_generation_ids
    from .outcome_settlement import _join

    _require(
        _response_generation_ids(attempt.response) == {execution.generation_id},
        "Probe execution differs from manual admission",
    )

    joined = _join(management, generation, execution.store_incarnation, attempt)
    _require(joined is not None, "Probe execution is not terminal")
    selections, _expected, _bindings, snapshots, grouped = joined
    owners = frozenset(grouped)
    _require(owners and set(snapshots) == owners, "Probe snapshot membership changed")
    _require(
        all(label in OVERLAYS and "bgp" in OVERLAYS[label].read_families for label, _pk in owners),
        "Probe contains an unselected read family",
    )
    _require(
        all(
            selection.binding_state == "bound"
            and selection.store_incarnation == execution.store_incarnation
            and selection.acknowledged_at is not None
            for selection in selections.values()
        ),
        "Probe selection is not bound",
    )
    if completed:
        _require(
            execution.consumption_state == "consumed" and execution.consumed_at is not None,
            "Probe has not settled through production settlement",
        )
        _require(
            all(
                snapshot.settlement_disposition in {"refused", "not_applied", "in_sync"}
                for snapshot in snapshots.values()
            ),
            "Probe has unresolved contributor dispositions",
        )
    else:
        _require(execution.consumption_state == "unconsumed", "Probe is stale or already consumed")
    return Probe(attempt, execution, owners)


def _terminal_evidence(attempt, *, settle_seq=None):
    """Fetch one terminal execution without replaying Apply or consuming unrelated work."""
    management = attempt.management
    _page, incarnation = adapter_client.get_settlement_feed(
        management.adapter_device_id, after_settle_seq=(settle_seq or 1) - 1, limit=100
    )
    evidence = adapter_client.get_deployment_evidence(management.adapter_device_id, [attempt.pk])
    _require(evidence["device_id"] == management.adapter_device_id, "Probe evidence device changed")
    rows = evidence["attempts"]
    _require(len(rows) == 1 and rows[0]["apply_attempt_id"] == str(attempt.pk), "Probe admission is missing")
    row = rows[0]
    _require(
        row["admission_state"] == "admitted"
        and row["response"] == attempt.response
        and row["http_status"] == attempt.http_status,
        "Probe admission changed",
    )
    from .apply_settlement import reconcile_admission_links

    listing = adapter_client.list_device_generations(management.adapter_device_id)
    reconcile_admission_links((row["response"], attempt.response), listing)
    generations = row["generations"]
    _require(len(generations) == 1, "Probe requires one generation")
    generation = generations[0]
    refusal_contract.validate_generation(generation)
    from .apply_settlement import _response_generation_ids

    _require(
        _response_generation_ids(attempt.response) == {generation["generation_id"]},
        "Probe generation differs from its admitted execution",
    )
    _require(generation["source_push_seq"] == attempt.selected, "Probe frozen selection changed")
    link = attempt.response["generations"][0]
    _require(
        len(attempt.response["generations"]) == 1
        and link["mode"] == "networked"
        and link["seq"] == generation["seq"]
        and link["source_push_seq"] == generation["source_push_seq"]
        and link["stream_revisions"] == generation["stream_revisions"],
        "Probe carrier differs from the admitted generation",
    )
    _require(generation["status"] in {"settled", "failed"}, "Probe execution is not terminal")
    after = (settle_seq or 1) - 1
    jobs = []
    for _page_number in range(100):
        sequences = [job["settle_seq"] for job in _page]
        _require(
            all(type(value) is int and value > after for value in sequences) and sequences == sorted(set(sequences)),
            "Probe settlement feed is malformed",
        )
        jobs = [job for job in _page if job["id"] == generation["carrier_job_id"]]
        if jobs or len(_page) < 100:
            break
        after = sequences[-1]
        _page, next_incarnation = adapter_client.get_settlement_feed(
            management.adapter_device_id,
            after_settle_seq=after,
            limit=100,
        )
        _require(incarnation == next_incarnation, "Probe store changed during evidence fetch")
    _last, final_incarnation = adapter_client.get_settlement_feed(
        management.adapter_device_id,
        after_settle_seq=after,
        limit=1,
    )
    _require(incarnation == final_incarnation, "Probe store changed during evidence fetch")
    _require(len(jobs) == 1 and type(jobs[0]["settle_seq"]) is int, "Probe carrier sequence is unavailable")
    job = jobs[0]
    _require(
        job["device_id"] == management.adapter_device_id
        and job["type"] == "apply"
        and job["status"] == generation["carrier_job_status"]
        and job["result"] == generation["carrier_job_result"]
        and job["row_outcomes"] == generation["row_outcomes"]
        and job["refusals"] == generation["refusals"],
        "Probe carrier evidence changed",
    )
    return generation, incarnation, job["settle_seq"]


def _register_execution(attempt_id):
    from .apply_settlement import _parse_time
    from .outcome_settlement import _bind, _join

    attempt = NSOApplyAttempt.objects.select_for_update().select_related("management").get(pk=attempt_id)
    _require(
        attempt.trigger_kind == "manual" and set(attempt.selected) == {"bgp"}, "Probe must be a BGP manual attempt"
    )
    generation, incarnation, sequence = _terminal_evidence(attempt)
    _require(generation["source_push_seq"] == attempt.selected, "Probe selection changed")
    refusal_contract.validate_generation(generation)
    joined = _join(attempt.management, generation, incarnation, attempt)
    _require(joined is not None, "Probe lacks terminal ownership")
    selections, expected, bindings, _snapshots, _grouped = joined
    _bind(selections, expected, bindings, incarnation, generation["stream_revisions"])
    identity = dict(
        management=attempt.management,
        mapping_epoch=mapping_epoch(attempt.management),
        store_incarnation=incarnation,
        generation_id=generation["generation_id"],
        carrier_job_id=generation["carrier_job_id"],
    )
    execution, created = NSOApplyExecution.objects.get_or_create(
        **identity,
        defaults={
            "attempt": attempt,
            "adapter_device_id": attempt.adapter_device_id,
            "generation_seq": generation["seq"],
            "settle_seq": sequence,
            "source_selectors": {
                "source_push_seq": generation["source_push_seq"],
                "stream_revisions": generation["stream_revisions"],
            },
            "terminal_projection": copy.deepcopy(generation),
            "terminal_at": _parse_time(generation["updated_at"]),
        },
    )
    _require(created or execution.attempt_id == attempt.pk, "Probe execution belongs to another attempt")
    authority(attempt.pk)
    return attempt


def register_probe(attempt_id):
    """Bind the named execution or revoke its permit without releasing the gate."""
    error = None
    with locked_gate() as control:
        _require(control.verification_attempt_id in {None, UUID(str(attempt_id))}, "Another probe owns the permit")
        try:
            with transaction.atomic():
                attempt = _register_execution(attempt_id)
        except Exception as exc:
            error = exc
            control.verification_attempt = None
        else:
            control.verification_attempt = attempt
        control.save(update_fields=("verification_attempt",))
    if error is not None:
        raise error


def enqueue_probe(device_id):
    """Validate under the gate, then publish the job after its transaction commits."""
    import django_rq

    error = None
    with locked_gate() as control:
        _require(control.verification_attempt_id is not None, "No verification probe is permitted")
        _require(
            NSOApplyAttempt.objects.filter(
                pk=control.verification_attempt_id, management__device_id=device_id
            ).exists(),
            "Probe device does not match",
        )
        try:
            probe = authority(control.verification_attempt_id, device_id)
        except ProbePending as exc:
            error = exc
        except Exception as exc:
            control.verification_attempt = None
            control.save(update_fields=("verification_attempt",))
            error = exc
    if error is not None:
        raise ProbeBlocked("Probe authority could not be validated") from error
    try:
        return django_rq.get_queue("default").enqueue(run_probe, str(probe.attempt.pk), device_id)
    except Exception:
        with locked_gate() as control:
            if control.verification_attempt_id == probe.attempt.pk:
                control.verification_attempt = None
                control.save(update_fields=("verification_attempt",))
        raise


def run_probe(attempt_id, device_id):
    """Recheck the permit under locks, then consume only its captured BGP footprint."""
    from .lifecycle_fence import bounded_owners
    from .outcome_settlement import immutable_execution_projection, settle_terminal_generation
    from .signals import suppress_intent_push

    try:
        with locked_gate() as control:
            _require(str(control.verification_attempt_id) == str(attempt_id), "Probe permit is stale")
            error = None
            try:
                with transaction.atomic():
                    probe = authority(attempt_id, device_id)
                with (
                    transaction.atomic(),
                    deployment.gate_bypass(),
                    suppress_intent_push(),
                    bounded_owners(probe.owners),
                ):
                    generation, incarnation, sequence = _terminal_evidence(
                        probe.attempt, settle_seq=probe.execution.settle_seq
                    )
                    _require(
                        incarnation == probe.execution.store_incarnation
                        and sequence == probe.execution.settle_seq
                        and immutable_execution_projection(generation)
                        == immutable_execution_projection(probe.execution.terminal_projection),
                        "Probe terminal evidence changed",
                    )
                    execution = settle_terminal_generation(
                        probe.attempt.management,
                        generation,
                        incarnation,
                        attempt=probe.attempt,
                        settle_seq=sequence,
                    )
                    _require(
                        execution.pk == probe.execution.pk and execution.consumption_state == "consumed",
                        "Probe did not settle its selected execution",
                    )
                    read_bgp(probe.attempt.management, owners=probe.owners)
                    _page, final_incarnation = adapter_client.get_settlement_feed(
                        probe.attempt.adapter_device_id,
                        after_settle_seq=sequence,
                        limit=1,
                    )
                    _require(final_incarnation == incarnation, "Probe store changed during the bounded read")
            except Exception as exc:
                error = exc
            finally:
                control.verification_attempt = None
                control.save(update_fields=("verification_attempt",))
        if error is not None:
            raise error
        return {"device_id": device_id, "attempt_id": str(attempt_id), "settled": True}
    except (ProbeBlocked, EvidenceInvariantError, adapter_client.AdapterError, refusal_contract.ContractError) as exc:
        return {"device_id": device_id, "attempt_id": str(attempt_id), "blocked": str(exc)}


def read_bgp(management, *, owners=None):
    """Use the production family gate and BGP planner for one bounded read."""
    from .bgp_reconciler import _bgp_complete_observation, _reconcile_bgp_config, bgp_reconcile_plan
    from .reconcile import _gated

    payload = adapter_client.get_bgp_config(management.adapter_device_id)
    _require(_bgp_complete_observation(payload), "BGP observation is incomplete")
    if owners is not None:
        state = payload.get("read_state") or {}
        from .apply_settlement import _parse_time

        _require(
            state.get("incarnation") == management.adapter_incarnation
            and _parse_time(state.get("incarnation_born")) == management.adapter_incarnation_born
            and state.get("source_epoch") == management.adapter_source_epoch
            and not management.reset_pending_incarnation
            and not management.source_rekey_pending,
            "Probe BGP read identity changed",
        )
    original_epoch = mapping_epoch(management)
    ctx = {}
    result = _gated(
        ctx,
        management,
        "bgp",
        payload,
        lambda: _reconcile_bgp_config(management.device, payload, allowed_owners=owners),
        epoch=management.adapter_device_id,
        publish_applied=owners is None,
        pre_body=lambda: bgp_reconcile_plan(management.device, payload, allowed_owners=owners),
    )
    _require(result.disposition == "ran", "BGP observation was not admitted")
    if owners is not None:
        management.refresh_from_db()
        _require(mapping_epoch(management) == original_epoch, "Probe mapping changed during the bounded read")
    return payload


def _validate_capture(attempt, management, incarnation):
    from .durable_capture import _matches, _snapshot_transaction, _validate_snapshot_membership

    with locked_gate() as control:
        require_current_capture(attempt, control.quiesced_at)
    _require(
        attempt.management_id == management.pk
        and attempt.adapter_device_id == management.adapter_device_id
        and attempt.source_epoch == management.adapter_source_epoch
        and attempt.trigger_kind == "manual"
        and attempt.lifecycle == "open"
        and set(attempt.selected) == {"bgp"},
        "The named probe does not match this current BGP admission",
    )
    with _snapshot_transaction(attempt.pk) as (locked, current, snapshots, rows, revisions):
        _require(
            snapshots
            and all(
                item.selection_id is not None and item.settlement_disposition == "unresolved" for item in snapshots
            ),
            "Probe snapshots are missing or already disposed",
        )
        for item in snapshots:
            selection = item.selection
            _require(
                selection.mapping_epoch == mapping_epoch(current)
                and selection.store_incarnation == incarnation
                and selection.stream == "bgp"
                and selection.source_push_seq == locked.selected["bgp"]
                and selection.plugin_revision == locked.scope_revisions["bgp"]
                and selection.publication_state == "sent"
                and selection.acknowledged_at is not None
                and selection.binding_state != "invalidated",
                "Probe selection belongs to another store or mapping",
            )
            _validate_snapshot_membership(selection)
            prefix = "prior" if locked.adapter_apply_attempt_id is None else "expected"
            _require(
                _matches(item, rows.get((item.model_label, item.overlay_pk)), current, revisions, prefix),
                "Probe owner no longer matches its captured authority",
            )


def _finish_rejected(attempt, *, status, response):
    from .durable_capture import restore_attempt
    from .views import PreparedApply, _record_apply_response

    with transaction.atomic():
        _record_apply_response(PreparedApply(attempt.pk), http_status=status, response=response)
        restore_attempt(attempt.pk)
        NSOApplyAttempt.objects.filter(pk=attempt.pk).update(lifecycle="terminal", completed_at=timezone.now())


def start_probe(attempt_id, device_id):
    """Capture a prepared BGP selection and register its permit before remote admission."""
    from . import delivery, drain
    from .apply_settlement import apply_stream_partition, promoted_stream_coverage
    from .apply_state import promote_current_intent
    from .durable_capture import _validate_snapshot_membership, _wire, promote_snapshots
    from .models import NSOAcceptedSelection, NSODeviceManagement, NSOIntentOutboxState
    from .signals import suppress_intent_push
    from .views import PreparedApply, _record_apply_response

    attempt_id = UUID(str(attempt_id))
    with maintenance_gate(verification_attempt=attempt_id), deployment.gate_bypass(), suppress_intent_push():
        management = NSODeviceManagement.objects.get(device_id=device_id)
        _require(
            type(management.adapter_source_epoch) is int
            and management.adapter_source_epoch > 0
            and bool(management.adapter_incarnation)
            and not management.reset_pending_incarnation
            and not management.source_rekey_pending,
            "Import the current source identity before starting a probe",
        )
        _page, incarnation = adapter_client.get_settlement_feed(
            management.adapter_device_id, after_settle_seq=0, limit=1
        )
        _require(
            incarnation == management.adapter_incarnation, "Import the current adapter store identity before Apply"
        )
        attempt = NSOApplyAttempt.objects.filter(pk=attempt_id).first()
        if attempt is None:
            selection = (
                NSOAcceptedSelection.objects.filter(
                    management=management,
                    mapping_epoch=mapping_epoch(management),
                    stream="bgp",
                    publication_state="sent",
                    acknowledged_at__isnull=False,
                )
                .exclude(binding_state="invalidated")
                .order_by("-pk")
                .first()
            )
            _require(
                selection is not None and selection.store_incarnation == incarnation,
                "Prepare the current BGP selection in this adapter store before starting a probe",
            )
            _validate_snapshot_membership(selection)
            rendered = delivery.render("bgp", device_id, management.adapter_device_id)
            digest, _wire_body = _wire(rendered, marking_mode=delivery.delivery_keys()["bgp"].marking_mode)
            _require(digest == selection.request_digest, "Prepared BGP content changed before the probe")
            state = NSOIntentOutboxState.objects.get(device_id=device_id, scope="bgp")
            pushed = drain.SuccessfulPush(
                selection.source_push_seq, state.last_success_identity, selection.plugin_revision
            )
            attempt = promote_current_intent(
                management,
                {"bgp": delivery.delivery_keys()["bgp"]},
                {"bgp": pushed},
                direct_selected={},
                direct_source_revisions={},
                apply_attempt_id=attempt_id,
                static_route_stored=False,
            )
        _validate_capture(attempt, management, incarnation)
        if attempt.adapter_apply_attempt_id is None:
            _require(attempt.response is None and attempt.http_status is None, "Unsent probe has an admission response")
            promote_snapshots(attempt)
        _require(attempt.adapter_apply_attempt_id == attempt.pk, "Probe promotion identity changed")
        with locked_gate() as control:
            control.verification_attempt = attempt
            control.save(update_fields=("verification_attempt",))
        try:
            if attempt.response is None:
                response = adapter_client.trigger_apply(management.adapter_device_id, attempt.pk, attempt.selected)
                _require(
                    isinstance(response, dict) and response.get("device_id") == management.adapter_device_id,
                    "Probe admission device changed",
                )
                expected, skipped, error = apply_stream_partition(response, attempt.selected)
                _require(error is None, error)
                if response.get("outcome") == "no_op":
                    _require(
                        response.get("generations") == [] and set(skipped) == set(expected),
                        "Probe no-op evidence is incomplete",
                    )
                    _finish_rejected(attempt, status=200, response=response)
                    raise ProbeBlocked("Probe did not execute; select a fresh probe UUID")
                _require(
                    isinstance(response, dict)
                    and response.get("device_id") == management.adapter_device_id
                    and response.get("outcome") == "promoted"
                    and response.get("selected") == attempt.selected
                    and isinstance(response.get("generations"), list)
                    and len(response["generations"]) == 1,
                    "Probe admission did not promote its exact BGP selection",
                )
                streams, error = promoted_stream_coverage(response["generations"], expected, skipped)
                _require(
                    error is None
                    and streams == {"bgp"}
                    and response["generations"][0]["mode"] == "networked"
                    and response.get("job_id") == response["generations"][0]["job_id"],
                    "Probe promoted generation coverage changed",
                )
                _record_apply_response(PreparedApply(attempt.pk), http_status=202, response=response)
        except Exception as exc:
            if isinstance(exc, adapter_client.AdapterError) and exc.definitely_not_enqueued:
                _finish_rejected(attempt, status=exc.status_code, response=exc.rejection_response)
            raise
    return attempt_id
