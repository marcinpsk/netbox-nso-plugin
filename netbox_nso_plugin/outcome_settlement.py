# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Join terminal logical outcomes to immutable selections before local writes."""

import copy
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from . import delivery, refusal_contract
from . import durable_capture as capture
from .apply_settlement import (
    EvidenceInvariantError,
    _carrier_job,
    _parse_time,
    _validate_generation,
    identify_evidence_stall,
    identify_generation_stall,
)
from .models import NSOAcceptedSelection, NSOApplyAttempt, NSOApplyExecution
from .refusal_registry import OVERLAYS
from .settlement_resolution import (
    RESOLVED_STATES,
    acknowledged_since,
    close_completed_attempt,
    cover_unknown_contributions,
    unknown_bookkeeping,
)
from .settlement_schemas import canonical_uuid


@dataclass(frozen=True)
class OwnerOutcome:
    snapshot: object
    outcomes: tuple

    @property
    def disposition(self):
        dispositions = {item["disposition"] for item in self.outcomes}
        if "refused" in dispositions:
            return "refused"
        if "unavailable" in dispositions or len(dispositions) != 1:
            return "unavailable"
        return next(iter(dispositions))


def _require(condition, code):
    if not condition:
        raise EvidenceInvariantError(code)


def _bind_consumed_sequences(executions, sequence):
    """Bind verified feed order under the caller's management transaction."""
    _require(
        type(sequence) is int
        and sequence > 0
        and all(execution.settle_seq in (None, sequence) for execution in executions),
        "invalid_consumed_settlement_sequence",
    )
    missing = [execution.pk for execution in executions if execution.settle_seq is None]
    if missing:
        NSOApplyExecution.objects.filter(pk__in=missing, settle_seq__isnull=True).update(settle_seq=sequence)
    for execution in executions:
        execution.settle_seq = sequence


def _reuse_consumed_execution(execution, generation, settle_seq):
    _require(
        immutable_execution_projection(execution.terminal_projection) == immutable_execution_projection(generation),
        "consumed_execution_changed",
    )
    if settle_seq is not None:
        _bind_consumed_sequences([execution], settle_seq)
    return execution


def _ref(ref):
    return ref["subject"], ref["key"]


def _logical(outcome):
    return outcome["stream"], *_ref(outcome["row"])


def _remote_binding(outcome):
    """Keep immutable accepted content separate from its execution identity."""
    return {
        "frozen_revision": outcome["frozen_revision"],
        "acceptance_identity": outcome["acceptance_identity"],
        "carrier": {
            key: value for key, value in outcome["carrier"].items() if key not in {"generation_id", "settlement_cohort"}
        },
    }


def _selection(management, epoch, stream, source, revision, incarnation, receipt_recoveries=None):
    selectors = {"source_push_seq": source} if source is not None else {"selection_revision": revision}
    rows = list(
        NSOAcceptedSelection.objects.filter(management=management, mapping_epoch=epoch, stream=stream, **selectors)
    )
    eligible = [
        row for row in rows if row.binding_state != "invalidated" and row.store_incarnation in (None, incarnation)
    ]
    _require(len(eligible) == 1, "selection_not_unique")
    selection = eligible[0]
    if selection.acknowledged_at is None:
        from .selection_receipts import verify_recovery

        recovery = (receipt_recoveries or {}).get(selection.pk)
        _require(recovery is not None, "selection_unacknowledged")
        verify_recovery(management, selection, recovery, incarnation)
    else:
        _require(selection.publication_state == "sent", "selection_unacknowledged")
    _require(selection.store_incarnation in (None, incarnation), "selection_store_changed")
    _require(selection.binding_state != "bound" or selection.frozen_revision == revision, "selection_revision_changed")
    snapshots = capture._validate_snapshot_membership(selection)
    _require(all(snapshot.expected_owner_revision is not None for snapshot in snapshots), "owner_authority_missing")
    return selection


def _join(management, generation, incarnation, attempt, authorities=None, receipt_recoveries=None):
    """Return complete frozen ownership and proposed bindings without mutation."""
    epoch = capture.mapping_epoch(management)
    authorities = authorities or {generation["generation_id"]: generation}
    selected = {}
    expected = {}
    bindings = {}
    for stream, revision in generation["stream_revisions"].items():
        source = generation["source_push_seq"][stream]
        selection = _selection(management, epoch, stream, source, revision, incarnation, receipt_recoveries)
        selected[selection.pk] = selection
        for owner in selection.contributor_manifest:
            owner_key = owner["model_label"], owner["overlay_pk"]
            _require(owner_key[0] in OVERLAYS, "unknown_frozen_owner")
            for contributor in owner["contributors"]:
                _require(contributor["stream"] == stream, "selection_contributor_stream_changed")
                key = (stream, *_ref(contributor["logical_ref"]))
                _require(key not in expected, "ambiguous_frozen_owner")
                expected[key] = (owner_key, selection, contributor)
    outcomes = generation["row_outcomes"]
    _require(isinstance(outcomes, list), "missing_terminal_outcomes")
    actual = {}
    for outcome in outcomes:
        if outcome["generation_id"] != generation["generation_id"]:
            continue
        key = _logical(outcome)
        _require(key not in actual and key in expected, "unknown_or_duplicate_outcome")
        owner_key, selection, contributor = expected[key]
        _require(outcome["generation_id"] == generation["generation_id"], "outcome_generation_changed")
        _require(
            outcome["frozen_revision"] == generation["stream_revisions"][outcome["stream"]], "outcome_revision_changed"
        )
        _require(outcome["source_push_seq"] == selection.source_push_seq, "outcome_selector_changed")
        carrier = outcome["carrier"]
        _require(_ref(carrier["row"]) == _ref(contributor["carrier_ref"]), "outcome_carrier_changed")
        authority = authorities.get(carrier["generation_id"])
        _require(authority is not None, "carrier_generation_changed")
        _require(carrier["stream"] in authority["stream_revisions"], "carrier_stream_changed")
        _require(carrier["settlement_cohort"] == outcome["settlement_cohort"], "carrier_cohort_changed")
        _require(
            carrier["frozen_revision"] == authority["stream_revisions"][carrier["stream"]], "carrier_revision_changed"
        )
        _require(
            carrier["source_push_seq"] == authority["source_push_seq"][carrier["stream"]], "carrier_selector_changed"
        )
        binding = _remote_binding(outcome)
        if selection.binding_state == "bound":
            _require(contributor.get("remote_binding") == binding, "acceptance_binding_changed")
        bindings[key] = binding
        actual[key] = outcome
    _require(set(actual) == set(expected), "incomplete_frozen_outcomes")
    attempted = {refusal_contract.logical_key(outcome) for outcome in generation["carrier_job_result"]["row_outcomes"]}
    for cause in generation["refusals"]:
        for logical in cause["logical_rows"]:
            if (
                refusal_contract.logical_key(logical) not in attempted
                or logical["generation_id"] != generation["generation_id"]
            ):
                continue
            key = _logical(logical)
            _require(key in expected, "cause_unknown_logical_owner")
            contributor = expected[key][2]
            _require(
                any(
                    ref["family"] == cause["family"] and _ref(ref) == _ref(cause["row"])
                    for ref in contributor["emitted_refs"]
                ),
                "cause_not_in_frozen_contribution",
            )
    if attempt is not None:
        _require(
            attempt.management_id == management.pk and attempt.adapter_device_id == management.adapter_device_id,
            "attempt_mapping_changed",
        )
        _require(attempt.source_epoch == management.adapter_source_epoch, "attempt_source_changed")
        _require(
            attempt.trigger_kind == "manual" and attempt.adapter_apply_attempt_id == attempt.pk, "attempt_not_promoted"
        )
        snapshots = {(item.model_label, item.overlay_pk): item for item in attempt.overlay_snapshots.all()}
    else:
        snapshots = {}
    grouped = {}
    for key, outcome in actual.items():
        owner_key, selection, contributor = expected[key]
        if attempt is not None:
            snapshot = snapshots.get(owner_key)
            _require(snapshot is not None and snapshot.mapping_epoch == epoch, "missing_owner_snapshot")
            _require(snapshot.expected_owner_revision is not None, "owner_authority_missing")
            frozen = [
                item
                for item in snapshot.contributors["rows"]
                if item["stream"] == key[0] and _ref(item["logical_ref"]) == key[1:]
            ]
            _require(
                len(frozen) == 1 and frozen[0]["source_selector"] == contributor["source_selector"],
                "snapshot_selector_changed",
            )
            scopes = {scope for scope, entry in delivery.delivery_keys().items() if entry.section == key[0]}
            _require(
                all(snapshot.scope_revisions.get(scope) == selection.plugin_revision for scope in scopes),
                "snapshot_delivery_revision_changed",
            )
            _require(
                snapshot.content_digest
                == next(
                    owner["content_digest"]
                    for owner in selection.contributor_manifest
                    if (owner["model_label"], owner["overlay_pk"]) == owner_key
                ),
                "snapshot_content_changed",
            )
        grouped.setdefault(owner_key, []).append(outcome)
    return selected, expected, bindings, snapshots, grouped


def _bind(selections, expected, bindings, incarnation, revisions):
    for selection in selections.values():
        if selection.binding_state == "bound":
            continue
        manifest = copy.deepcopy(selection.contributor_manifest)
        for owner in manifest:
            for contributor in owner["contributors"]:
                key = (contributor["stream"], *_ref(contributor["logical_ref"]))
                contributor["remote_binding"] = bindings[key]
                contributor["remote_acceptance_identity"] = bindings[key]["acceptance_identity"]
            selection.overlay_snapshots.filter(
                attempt__trigger_kind="push", model_label=owner["model_label"], overlay_pk=owner["overlay_pk"]
            ).update(contributors={"rows": owner["contributors"]})
        contributor_revisions = {
            binding["frozen_revision"] for key, binding in bindings.items() if expected[key][1].pk == selection.pk
        }
        _require(
            not contributor_revisions or contributor_revisions == {revisions[selection.stream]},
            "selection_multiple_revisions",
        )
        selection.contributor_manifest = manifest
        selection.store_incarnation = incarnation
        selection.frozen_revision = revisions[selection.stream]
        selection.binding_state = "bound"
        selection.save(update_fields=("contributor_manifest", "store_incarnation", "frozen_revision", "binding_state"))


def _earlier_execution(fence, generation, incarnation, settle_seq):
    prior = fence.last_execution
    if prior is None:
        return False
    if prior.store_incarnation != incarnation:
        raise EvidenceInvariantError("execution_order_unproven")
    if settle_seq is not None and prior.settle_seq is not None:
        return settle_seq <= prior.settle_seq
    # A different execution needs ordered feed evidence before it can replace a certificate.
    raise EvidenceInvariantError("execution_order_unproven")


def _batch_code(exc):
    """Keep the closed code of an invariant; a contract error or free text has no specific cause."""
    from .settlement_evidence import invariant_code

    code = invariant_code(exc) if isinstance(exc, EvidenceInvariantError) else "unclassified"
    return "invalid_terminal_batch" if code == "unclassified" else code


def _record_invariant(management, generation, incarnation, attempt, code):
    """Persist safe diagnostics after the rejected batch transaction rolled back."""
    if not isinstance(generation, dict) or not canonical_uuid(incarnation):
        return
    generation_id, carrier_id = generation.get("generation_id"), generation.get("carrier_job_id")
    if type(generation_id) is not int or type(carrier_id) is not int or generation_id <= 0 or carrier_id <= 0:
        return
    from .models import NSODeviceManagement
    from .settlement_evidence import matches_request_fence, request_fence

    fence = request_fence(management)
    with transaction.atomic():
        management = NSODeviceManagement.objects.select_for_update().get(pk=management.pk)
        if not matches_request_fence(management, fence):
            return
        identity = dict(
            management=management,
            mapping_epoch=capture.mapping_epoch(management),
            store_incarnation=incarnation,
            generation_id=generation_id,
            carrier_job_id=carrier_id,
        )
        existing = NSOApplyExecution.objects.filter(**identity).first()
        if existing is not None:
            if existing.consumption_state == "unconsumed":
                existing.invariant_code = code
                existing.save(update_fields=("invariant_code",))
            return
        if attempt is None:
            attempt = NSOApplyAttempt.objects.create(
                management=management,
                adapter_device_id=management.adapter_device_id,
                source_epoch=management.adapter_source_epoch,
                trigger_kind="cadence",
            )
        NSOApplyExecution.objects.create(
            **identity, attempt=attempt, adapter_device_id=management.adapter_device_id, invariant_code=code
        )


def validate_terminal_generation(
    management, generation, store_incarnation, *, attempt=None, authorities=None, receipt_recoveries=None
):
    """Prove complete ownership without binding selections or writing overlays."""
    with identify_generation_stall(generation):
        try:
            return _validated_join(management, generation, store_incarnation, attempt, authorities, receipt_recoveries)
        except (refusal_contract.ContractError, EvidenceInvariantError) as exc:
            code = _batch_code(exc)
            _record_invariant(management, generation, store_incarnation, attempt, code)
            error = EvidenceInvariantError(code)
            if hasattr(exc, "invalid_job"):
                error.invalid_job = exc.invalid_job
            raise error from exc


def _validated_join(management, generation, store_incarnation, attempt, authorities, receipt_recoveries):
    _validate_generation(generation)
    _require(canonical_uuid(store_incarnation), "missing_store_incarnation")
    if generation["status"] in {"pending", "running"} or generation["row_outcomes"] is None:
        return None
    return _join(management, generation, store_incarnation, attempt, authorities, receipt_recoveries)


def _attempt_for(attempts, generation):
    if not isinstance(generation, dict):
        return None
    identity = generation.get("generation_id"), generation.get("carrier_job_id")
    return attempts.get(identity) if all(type(value) is int for value in identity) else None


def settle_terminal_generation(
    management, generation, store_incarnation, *, attempt=None, settle_seq=None, mode="networked"
):
    """Validate a complete terminal batch, then commit owner CAS decisions together."""
    with identify_generation_stall(generation, settle_seq=settle_seq):
        try:
            _validate_generation(generation)
        except EvidenceInvariantError:
            _record_invariant(management, generation, store_incarnation, attempt, "invalid_terminal_batch")
            raise
    return settle_terminal_generations(
        management,
        [generation],
        store_incarnation,
        attempts={(generation["generation_id"], generation["carrier_job_id"]): attempt},
        settle_sequences={generation["carrier_job_id"]: settle_seq},
        modes={generation["generation_id"]: mode},
    )[0]


def _validated_carrier_authorities(adapter_device_id, generations, incarnation, sequences):
    authorities, groups = {}, {}
    for generation in generations:
        carrier_id = generation.get("carrier_job_id") if isinstance(generation, dict) else None
        job = _carrier_job(adapter_device_id, carrier_id, sequences)
        with identify_evidence_stall(job, incarnation):
            _validate_generation(generation)
            groups.setdefault(carrier_id, []).append(generation)
            authorities.setdefault(carrier_id, {})[generation["generation_id"]] = generation
    for carrier_id, group in groups.items():
        job = _carrier_job(adapter_device_id, carrier_id, sequences)
        with identify_evidence_stall(job, incarnation):
            refusal_contract.validate_carrier_generations(group)
    return authorities


def settle_terminal_generations(
    management,
    generations,
    store_incarnation,
    *,
    attempts=None,
    settle_sequences=None,
    modes=None,
    restorations=(),
    receipt_recoveries=None,
    before_settle=None,
    after_settle=None,
):
    """Commit all generation authorities in each carrier as one owner transaction."""
    from .intent_state import MutationFootprint, SourceRow, mirror_transaction
    from .lifecycle_fence import authoritative_bookkeeping
    from .selection_receipts import acknowledge_recoveries, lock_recovery_claims, recovery_footprint

    attempts, settle_sequences, modes = attempts or {}, settle_sequences or {}, modes or {}
    receipt_recoveries = receipt_recoveries or {}
    generations = list(generations)
    try:
        authorities = _validated_carrier_authorities(
            management.adapter_device_id, generations, store_incarnation, settle_sequences
        )
        grouped = set()
        keys = set()
        for generation in generations:
            identity = generation["generation_id"], generation["carrier_job_id"]
            # The batch handler below records the invariant for every generation.
            job = _carrier_job(management.adapter_device_id, generation["carrier_job_id"], settle_sequences)
            with identify_evidence_stall(job, store_incarnation):
                joined = _validated_join(
                    management,
                    generation,
                    store_incarnation,
                    attempts.get(identity),
                    authorities[generation["carrier_job_id"]],
                    receipt_recoveries,
                )
            if joined is not None:
                grouped.update(joined[-1])
        for label, _pk in grouped:
            keys.update((management.device_id, scope) for scope in OVERLAYS[label].delivery_keys)
        footprint = MutationFootprint.merge(
            MutationFootprint.for_keys(keys, overlay_rows=(SourceRow(label, pk) for label, pk in grouped)),
            MutationFootprint(device_ids=(management.device_id,)),
            *(capture.attempt_footprint(attempt_id) for attempt_id, _keep in restorations),
            recovery_footprint(receipt_recoveries),
        )
        with (
            lock_recovery_claims(receipt_recoveries) as states,
            authoritative_bookkeeping(),
            mirror_transaction(footprint),
        ):
            if before_settle is not None:
                before_settle()
            for generation in generations:
                _validated_join(
                    management,
                    generation,
                    store_incarnation,
                    attempts.get((generation["generation_id"], generation["carrier_job_id"])),
                    authorities[generation["carrier_job_id"]],
                    receipt_recoveries,
                )
            acknowledge_recoveries(management, receipt_recoveries, store_incarnation, states)
            for attempt_id, keep in restorations:
                capture.restore_attempt(attempt_id, keep_streams=keep)
            executions = []
            for generation in generations:
                job = _carrier_job(management.adapter_device_id, generation["carrier_job_id"], settle_sequences)
                with identify_evidence_stall(job, store_incarnation):
                    executions.append(
                        _settle(
                            management,
                            generation,
                            store_incarnation,
                            attempt=attempts.get((generation["generation_id"], generation["carrier_job_id"])),
                            settle_seq=settle_sequences.get(generation["carrier_job_id"]),
                            mode=modes.get(generation["generation_id"], "networked"),
                            authorities=authorities[generation["carrier_job_id"]],
                        )
                    )
            if after_settle is not None:
                after_settle()
            return executions
    except (refusal_contract.ContractError, EvidenceInvariantError) as exc:
        code = _batch_code(exc)
        for generation in generations:
            _record_invariant(
                management,
                generation,
                store_incarnation,
                _attempt_for(attempts, generation),
                code,
            )
        error = EvidenceInvariantError(code)
        if hasattr(exc, "invalid_job"):
            error.invalid_job = exc.invalid_job
        raise error from exc


def _settle(management, generation, incarnation, *, attempt, settle_seq, mode, authorities=None):
    from .intent_state import MutationFootprint, SourceRow, mirror_transaction
    from .lifecycle_fence import authoritative_bookkeeping, fence_for
    from .models import NSODeviceManagement

    _validate_generation(generation)
    _require(canonical_uuid(incarnation), "missing_store_incarnation")
    _require(settle_seq is None or type(settle_seq) is int and settle_seq > 0, "invalid_settlement_sequence")
    if generation["status"] in {"pending", "running"} or generation["row_outcomes"] is None:
        return None
    original_epoch = capture.mapping_epoch(management)
    selections, expected, bindings, snapshots, grouped = _join(
        management, generation, incarnation, attempt, authorities
    )
    keys = {(management.device_id, scope) for label, _pk in grouped for scope in OVERLAYS[label].delivery_keys}
    footprint = MutationFootprint.merge(
        MutationFootprint.for_keys(keys, overlay_rows=(SourceRow(label, pk) for label, pk in grouped)),
        MutationFootprint(device_ids=(management.device_id,)),
    )
    with authoritative_bookkeeping(), mirror_transaction(footprint):
        management = NSODeviceManagement.objects.select_for_update().get(pk=management.pk)
        _require(capture.mapping_epoch(management) == original_epoch, "mapping_changed_during_evidence")
        identity = dict(
            management=management,
            mapping_epoch=original_epoch,
            store_incarnation=incarnation,
            generation_id=generation["generation_id"],
            carrier_job_id=generation["carrier_job_id"],
        )
        execution = NSOApplyExecution.objects.select_for_update().filter(**identity).first()
        if execution is not None and execution.consumption_state in {"consumed", "superseded"}:
            return _reuse_consumed_execution(execution, generation, settle_seq)
        if execution is not None:
            recorded_attempt = execution.attempt
            if attempt is not None and attempt.pk != recorded_attempt.pk:
                # A diagnostic placeholder does not establish manual execution ownership.
                _require(
                    execution.consumption_state == "unconsumed"
                    and execution.invariant_code
                    and execution.terminal_projection is None
                    and recorded_attempt.trigger_kind == "cadence"
                    and attempt.trigger_kind == "manual"
                    and not execution.overlay_snapshots.exists()
                    and not recorded_attempt.overlay_snapshots.exists(),
                    "execution_attempt_changed",
                )
            else:
                attempt = recorded_attempt
        if (
            attempt is not None
            and attempt.trigger_kind == "manual"
            and NSOApplyExecution.objects.filter(
                management=management,
                mapping_epoch=original_epoch,
                store_incarnation=incarnation,
                generation_id=generation["generation_id"],
                attempt=attempt,
                consumption_state__in=RESOLVED_STATES,
            )
            .exclude(carrier_job_id=generation["carrier_job_id"])
            .exists()
        ):
            # A new adapter retry needs the bookkeeping that exists before this execution.
            attempt = None
        selections, expected, bindings, snapshots, grouped = _join(
            management,
            generation,
            incarnation,
            attempt if attempt is not None and attempt.trigger_kind == "manual" else None,
            authorities,
        )
        if attempt is None:
            attempt = NSOApplyAttempt.objects.create(
                management=management,
                adapter_device_id=management.adapter_device_id,
                source_epoch=management.adapter_source_epoch,
                trigger_kind="explicit_retry"
                if NSOApplyExecution.objects.filter(
                    management=management,
                    store_incarnation=incarnation,
                    generation_id=generation["generation_id"],
                    consumption_state__in=RESOLVED_STATES,
                ).exists()
                else "cadence",
                selected={row.stream: row.source_push_seq or row.selection_revision for row in selections.values()},
                scope_revisions={
                    scope: row.plugin_revision
                    for row in selections.values()
                    for scope, entry in delivery.delivery_keys().items()
                    if entry.section == row.stream
                },
            )
        automatic = attempt.trigger_kind != "manual"
        rows = {
            (label, pk): OVERLAYS[label]
            .model.objects.select_for_update(of=("self",))
            .filter(pk=pk, **{OVERLAYS[label].device_lookup: management.device_id})
            .first()
            for label, pk in grouped
        }
        _validate_owner_deletions(management, rows, snapshots, expected)
        for row in rows.values():
            if row is not None:
                _earlier_execution(fence_for(row), generation, incarnation, settle_seq)
        stale_owners = (
            _capture_automatic_snapshots(attempt, generation, incarnation, settle_seq, rows, expected, snapshots)
            if automatic
            else set()
        )
        _bind(selections, expected, bindings, incarnation, generation["stream_revisions"])
        if execution is None:
            execution = NSOApplyExecution.objects.create(
                **identity, attempt=attempt, adapter_device_id=management.adapter_device_id
            )
        else:
            execution.attempt = attempt
        execution.generation_seq = generation["seq"]
        execution.settlement_cohort = next(iter(grouped.values()))[0]["settlement_cohort"] if grouped else None
        execution.mode = mode
        execution.source_selectors = {
            "source_push_seq": generation["source_push_seq"],
            "stream_revisions": generation["stream_revisions"],
        }
        execution.settle_seq = settle_seq
        execution.terminal_projection = copy.deepcopy(generation)
        execution.terminal_at = _parse_time(generation["updated_at"])
        execution.commit_acknowledged = generation["carrier_job_result"].get("commit_acknowledged")
        execution.invariant_code = ""
        execution.save()
        dispositions = []
        for owner_key, outcomes in grouped.items():
            snapshot = snapshots.get(owner_key)
            if snapshot is None:
                _require(owner_key in stale_owners, "missing_owner_snapshot")
                dispositions.append("superseded")
                continue
            row = rows[owner_key]
            execution.overlay_snapshots.add(snapshot)
            if snapshot.settlement_disposition != "unresolved":
                dispositions.append(snapshot.settlement_disposition)
                continue
            if row is None:
                disposition = "superseded"
                snapshot.settlement_disposition = disposition
                snapshot.save(update_fields=("settlement_disposition",))
            else:
                disposition = _settle_owner(snapshot, row, outcomes, management, execution, attempt, settle_seq)
            dispositions.append(disposition)
        execution.consumption_state = (
            "superseded" if dispositions and set(dispositions) == {"superseded"} else "consumed"
        )
        execution.consumed_at = timezone.now()
        execution.save(update_fields=("consumption_state", "consumed_at"))
        close_completed_attempt(attempt, timezone.now())
        return execution


def _settle_owner(snapshot, row, outcomes, management, execution, attempt, settle_seq):
    from .intent_state import mirror_refresh
    from .lifecycle_fence import advance_fence, fence_for
    from .signals import suppress_intent_push

    generation = execution.terminal_projection
    fence = fence_for(row) if row is not None else None
    matches = capture._matches(snapshot, row, management, {}, "expected", check_revisions=False)
    acknowledged = None if matches else acknowledged_since(snapshot, row, fence, management)
    if not (matches or acknowledged) or _earlier_execution(fence, generation, execution.store_incarnation, settle_seq):
        disposition = "superseded"
    else:
        disposition = OwnerOutcome(snapshot, tuple(outcomes)).disposition
        if disposition == "in_sync" and snapshot.model_label == "netbox_nso_plugin.nsostaticroutestate":
            from .settlement import static_route_success_proven

            if not static_route_success_proven(snapshot, generation):
                disposition = "unavailable"
        # After an acknowledgement, the state before this execution is the acknowledged one.
        source = row if acknowledged else snapshot
        prefix = "" if acknowledged else "prior_"
        values = {name: copy.deepcopy(getattr(source, prefix + name)) for name in capture._BOOKKEEPING}
        if disposition == "refused":
            causes = [cause for outcome in outcomes for cause in outcome["refusals"]]
            error = refusal_error(causes, json_error=OVERLAYS[snapshot.model_label].error_type == "JSONField")
            values.update(status="apply_failed", last_apply_error=error, apply_attempt_id=attempt.pk)
        elif disposition == "in_sync":
            values.update(
                status="in_sync",
                last_apply_at=execution.terminal_at,
                last_apply_error={} if OVERLAYS[snapshot.model_label].error_type == "JSONField" else "",
                apply_attempt_id=attempt.pk,
            )
        unknown = cover_unknown_contributions(row, fence, execution, outcomes)
        if unknown and disposition != "refused":
            # Every final write keeps acknowledged unknown contributions from success or prior state.
            disposition = "unavailable"
            values = unknown_bookkeeping(row, [owner.issue.job_id for owner, _keys in unknown], attempt)
        with suppress_intent_push(), mirror_refresh(row, values) as locked:
            _require(locked is not None, "owner_disappeared_under_lock")
            for name, value in values.items():
                setattr(locked, name, value)
            locked.save(update_fields=values)
            row = locked
        advance_fence(
            row,
            current_attempt=attempt,
            last_execution=execution,
            last_settle_seq=settle_seq,
            observation_watermark=snapshot.observation_watermark,
        )
    snapshot.settlement_disposition = disposition
    snapshot.save(update_fields=("settlement_disposition",))
    return disposition


def _automatic_owner_eligible(row, original, selection, fence):
    from .selection_manifest import current_content_digest

    if row.accepted_at != original.accepted_at or current_content_digest(row) != original.content_digest:
        return False
    if original.expected_owner_revision is None or fence.owner_revision != original.expected_owner_revision:
        return False
    if (
        row.status == "deploying"
        and row.apply_attempt_id is not None
        and fence.current_attempt_id == row.apply_attempt_id
    ):
        from .models import NSOApplyOverlaySnapshot

        if NSOApplyOverlaySnapshot.objects.filter(
            attempt_id=row.apply_attempt_id,
            attempt__trigger_kind="manual",
            attempt__adapter_apply_attempt_id=row.apply_attempt_id,
            mapping_epoch=fence.mapping_epoch,
            model_label=original.model_label,
            overlay_pk=original.overlay_pk,
            expected_status="deploying",
            expected_apply_attempt_id=row.apply_attempt_id,
            settlement_disposition="unresolved",
        ).exists():
            return False
    return True


def _capture_automatic_snapshots(attempt, generation, incarnation, settle_seq, rows, expected, snapshots):
    from .lifecycle_fence import fence_for

    stale_owners = set()
    for owner_key in rows:
        row = rows[owner_key]
        if row is None:
            if owner_key not in snapshots:
                original = _selection_owner_snapshot(expected, owner_key)
                values = {
                    field.attname: copy.deepcopy(getattr(original, field.attname))
                    for field in original._meta.concrete_fields
                    if not field.primary_key and field.name != "attempt"
                }
                values["settlement_disposition"] = "unresolved"
                snapshots[owner_key] = type(original).objects.create(attempt=attempt, **values)
            continue
        if _earlier_execution(fence_for(row), generation, incarnation, settle_seq):
            stale_owners.add(owner_key)
            continue
        if owner_key in snapshots:
            continue
        selection = next(value[1] for value in expected.values() if value[0] == owner_key)
        owner = next(
            item for item in selection.contributor_manifest if (item["model_label"], item["overlay_pk"]) == owner_key
        )
        original = _selection_owner_snapshot(expected, owner_key)
        fence = fence_for(row)
        if not _automatic_owner_eligible(row, original, selection, fence):
            stale_owners.add(owner_key)
            continue
        snapshot = capture._snapshot(attempt, selection, owner, row, attempt.scope_revisions)
        snapshot.accepted_at = original.accepted_at
        snapshot.save(update_fields=("accepted_at",))
        snapshots[owner_key] = snapshot
    return stale_owners


def _selection_owner_snapshot(expected, owner_key):
    selection = next(value[1] for value in expected.values() if value[0] == owner_key)
    return selection.overlay_snapshots.get(
        attempt__trigger_kind="push", model_label=owner_key[0], overlay_pk=owner_key[1]
    )


def _validate_owner_deletions(management, rows, snapshots, expected):
    from .lifecycle_fence import owner_deletion_proven

    for owner_key, row in rows.items():
        if row is None:
            original = snapshots.get(owner_key) or _selection_owner_snapshot(expected, owner_key)
            _require(owner_deletion_proven(management, original), "owner_missing_without_deletion_evidence")


def refusal_error(causes, *, json_error=False):
    """Format validated catalog causes without interpolating device diagnostics."""
    from .refusal_display import catalog_causes, format_causes

    if json_error:
        return {"refusals": catalog_causes(causes)}
    return format_causes(causes)


def immutable_execution_projection(generation):
    """Exclude mutable generation state from a durable carrier certificate."""
    return {key: value for key, value in generation.items() if key not in {"status", "updated_at"}}
