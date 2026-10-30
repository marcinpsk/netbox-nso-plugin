# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Settle Apply rows only from evidence addressed to their durable attempt."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import UUID

from django.utils import timezone

from .refusal_contract import GENERATION_FIELDS, ContractError
from .settlement_schemas import APPLY_SKIP_REASONS, canonical_uuid

logger = logging.getLogger(__name__)

LOST_RESPONSE_REPLAY_WINDOW = timedelta(days=1)

GENERATION_DISPOSITIONS = {
    "pending": "waiting",
    "running": "waiting",
    "settled": "settled",
    "failed": "blocked",
    "outcome_unknown": "blocked",
    "abandoned": "abandoned",
}
_GENERATION_FIELDS = GENERATION_FIELDS
_CARRIER_EVIDENCE_FIELDS = (
    "carrier_job_id",
    "carrier_job_status",
    "carrier_job_result",
    "carrier_job_error",
    "row_outcomes",
    "refusals",
)


class EvidenceInvariantError(RuntimeError):
    """The adapter evidence cannot truthfully judge local Apply rows."""


@contextmanager
def identify_evidence_stall(job, incarnation, *, sequences=None):
    """Keep the blocked feed identity when local evidence validation fails."""
    from .adapter_client import InvalidJobEvidence

    try:
        yield
    except InvalidJobEvidence as exc:
        _complete_evidence_identity(exc, job, incarnation, sequences or {})
        raise
    except (EvidenceInvariantError, ContractError) as exc:
        error = exc if isinstance(exc, EvidenceInvariantError) else EvidenceInvariantError("invalid_terminal_batch")
        if not hasattr(error, "invalid_job") and (_positive_int(job.get("id")) or job.get("device_id") is not None):
            error.invalid_job = InvalidJobEvidence(job, "invalid settlement evidence")
        if hasattr(error, "invalid_job"):
            _complete_evidence_identity(error.invalid_job, job, incarnation, sequences or {})
        if error is exc:
            raise
        raise error from exc


def _complete_evidence_identity(error, job, incarnation, sequences):
    """Complete missing fences without replacing the innermost carrier."""
    if error.device_id is None:
        error.device_id = job.get("device_id")
    if error.store_incarnation is None:
        error.store_incarnation = incarnation
    if error.settle_seq is None and _positive_int(error.job_id):
        sequence = sequences.get(error.job_id)
        if sequence is None and error.job_id == job.get("id"):
            sequence = job.get("settle_seq")
        if _positive_int(sequence):
            error.settle_seq = sequence


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _carrier_job(adapter_device_id, carrier_id, sequences):
    """Keep the carrier identity until its settlement order is known."""
    job = {"device_id": adapter_device_id}
    if _positive_int(carrier_id):
        job["id"] = carrier_id
    sequence = sequences.get(carrier_id) if _positive_int(carrier_id) else None
    if _positive_int(sequence):
        job["settle_seq"] = sequence
    return job


@contextmanager
def identify_generation_stall(generation, *, settle_seq=None):
    """Keep the generation's carrier when validation precedes the feed join."""
    carrier_id = generation.get("carrier_job_id") if isinstance(generation, dict) else None
    job = _carrier_job(None, carrier_id, {})
    if _positive_int(settle_seq):
        job["settle_seq"] = settle_seq
    with identify_evidence_stall(job, None):
        yield


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise EvidenceInvariantError("generation updated_at is not an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise EvidenceInvariantError("generation updated_at is not timezone-aware")
    return parsed


def _validate_generation(raw) -> dict:
    from .adapter_client import raise_item_error
    from .refusal_contract import validate_generation

    with identify_generation_stall(raw):
        raise_item_error(raw)
        return validate_generation(raw)


def _response_generation_ids(response) -> set[int]:
    if not isinstance(response, dict):
        raise EvidenceInvariantError("stored Apply response is not an object")
    generations = response.get("generations")
    if generations is None:
        return set()
    if not isinstance(generations, list):
        raise EvidenceInvariantError("stored Apply response generations are not a list")
    ids = set()
    for generation in generations:
        if not isinstance(generation, dict) or not _positive_int(generation.get("generation_id")):
            raise EvidenceInvariantError("stored Apply response has an invalid generation identity")
        ids.add(generation["generation_id"])
    if len(ids) != len(generations):
        raise EvidenceInvariantError("stored Apply response repeats a generation identity")
    return ids


def _validate_attempt(raw, local, store_incarnation=None, *, settlement_sequences=None, carrier_id=None) -> dict:
    from .adapter_client import raise_item_error

    raise_item_error(raw)
    try:
        attempt_id = UUID(str(raw["apply_attempt_id"]))
    except (TypeError, ValueError, AttributeError):
        raise EvidenceInvariantError("deployment evidence has an invalid attempt UUID") from None
    if attempt_id != local.pk:
        raise EvidenceInvariantError("deployment evidence names the wrong local attempt")
    if raw["admission_state"] not in {"admitted", "rejected"}:
        raise EvidenceInvariantError("deployment evidence has an invalid admission state")
    if not 100 <= raw["http_status"] <= 599:
        raise EvidenceInvariantError("deployment evidence has an invalid stored response")
    response = raw["response"]
    selected = response.get("selected")
    if raw["admission_state"] == "admitted":
        if response.get("device_id") != local.adapter_device_id:
            raise EvidenceInvariantError("admission_mapping_changed")
        if not isinstance(selected, dict) or selected != local.selected:
            raise EvidenceInvariantError("deployment evidence changed the Apply selection")
        skipped = response.get("skipped")
        if (
            not isinstance(response.get("outcome"), str)
            or response["outcome"] not in {"promoted", "no_op"}
            or not isinstance(skipped, dict)
        ):
            raise EvidenceInvariantError("deployment evidence has an invalid admitted response")
    identities = [generation["generation_id"] for generation in raw["generations"]]
    if len(set(identities)) != len(identities):
        raise EvidenceInvariantError("deployment evidence repeats a generation identity")
    if _response_generation_ids(response) != set(identities):
        raise EvidenceInvariantError("deployment evidence is incomplete for its stored response")
    generations = _validated_attempt_generations(
        [row for row in raw["generations"] if carrier_id is None or row["carrier_job_id"] == carrier_id],
        local,
        store_incarnation,
        settlement_sequences or {},
    )
    if local.response is not None and (local.http_status != raw["http_status"] or local.response != response):
        raise EvidenceInvariantError("local and adapter Apply responses disagree")
    return {**raw, "id": attempt_id, "generations": generations}


def _attempt_generations(raw):
    """Read retained child identities without treating a failed attempt as a projection."""
    from .adapter_client import EvidenceItem

    return raw.evidence_generations if isinstance(raw, EvidenceItem) else raw["generations"]


def _admission_response(raw):
    """Require a complete attempt before reading its admission response."""
    from .adapter_client import raise_item_error

    raise_item_error(raw)
    return raw["response"]


def _validated_attempt_generations(raw_generations, local, store_incarnation, settlement_sequences):
    generations = []
    for generation in raw_generations:
        carrier_id = generation.get("carrier_job_id") if isinstance(generation, dict) else None
        job = _carrier_job(local.adapter_device_id, carrier_id, settlement_sequences)
        with identify_evidence_stall(job, store_incarnation):
            try:
                generations.append(_validate_generation(generation))
            except EvidenceInvariantError:
                from .outcome_settlement import _record_invariant

                _record_invariant(local.management, generation, store_incarnation, local, "invalid_terminal_batch")
                raise
    return generations


def _load_attempts(
    management,
    deployment_evidence,
    required_attempt_ids=(),
    *,
    store_incarnation=None,
    settlement_sequences,
    carrier_id=None,
):
    from .models import NSOApplyAttempt

    raw_attempts = deployment_evidence["attempts"]
    raw_ids = []
    for raw in raw_attempts:
        try:
            raw_id = UUID(str(raw.get("apply_attempt_id")))
        except (TypeError, ValueError, AttributeError):
            raise EvidenceInvariantError("deployment evidence has an invalid attempt UUID") from None
        raw_ids.append(raw_id)

    unknown_ids = set()
    for value in deployment_evidence["unknown_apply_attempt_ids"]:
        try:
            unknown_ids.add(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise EvidenceInvariantError("deployment evidence has an invalid unknown attempt UUID") from None

    attempt_ids = set(required_attempt_ids)
    attempt_ids.update(raw_ids)
    attempt_ids.update(unknown_ids)
    local_attempts = NSOApplyAttempt.objects.filter(
        management=management,
        pk__in=attempt_ids,
        trigger_kind="manual",
        adapter_apply_attempt_id__isnull=False,
    ).in_bulk()
    validated = {}
    for raw, raw_id in zip(raw_attempts, raw_ids, strict=True):
        if (
            carrier_id is not None
            and getattr(deployment_evidence, "carrier_id", None) != carrier_id
            and not any(row["carrier_job_id"] == carrier_id for row in _attempt_generations(raw))
        ):
            continue
        local = local_attempts.get(raw_id)
        if local is None:
            continue
        if raw_id in validated:
            raise EvidenceInvariantError("deployment evidence repeats an Apply attempt")
        if local.adapter_device_id != management.adapter_device_id:
            continue
        validated[raw_id] = _validate_attempt(
            raw, local, store_incarnation, settlement_sequences=settlement_sequences, carrier_id=carrier_id
        )

    if unknown_ids & set(validated):
        raise EvidenceInvariantError("an Apply attempt is both known and unknown")
    return local_attempts, validated, unknown_ids


_APPLY_GENERATION_FIELDS = frozenset(
    {"generation_id", "seq", "job_id", "mode", "source_push_seq", "stream_revisions", "digest"}
)


def apply_stream_partition(result, selected):
    """Validate the selector echo and return its selected and skipped stream sets."""
    if not isinstance(result, dict):
        return None, None, "Adapter returned an invalid Apply response."
    expected_selected = dict(selected)
    returned_selected = result.get("selected")
    skipped = result.get("skipped")
    if not isinstance(returned_selected, dict) or returned_selected != expected_selected:
        return None, None, "Adapter returned an invalid Apply selector."
    if (
        not isinstance(skipped, dict)
        or not set(skipped).issubset(expected_selected)
        or any(type(reason) is not str or reason not in APPLY_SKIP_REASONS for reason in skipped.values())
    ):
        return None, None, "Adapter returned invalid Apply skip results."
    return expected_selected, skipped, None


def promoted_stream_coverage(generations, expected_selected, skipped):
    """Return the promoted stream union when every generation proves its selector."""
    from .delivery import direct_streams

    switching_streams = direct_streams()
    promoted_streams = set()
    generation_ids = set()
    previous_seq = None
    for link in generations:
        if not isinstance(link, dict) or not _APPLY_GENERATION_FIELDS.issubset(link):
            return None, "Adapter returned an invalid Apply generation."
        generation_id = link.get("generation_id")
        seq = link.get("seq")
        job_id = link.get("job_id")
        mode = link.get("mode")
        sources = link.get("source_push_seq")
        revisions = link.get("stream_revisions")
        digest = link.get("digest")
        valid_shape = (
            type(generation_id) is int
            and generation_id > 0
            and generation_id not in generation_ids
            and type(seq) is int
            and seq > 0
            and (previous_seq is None or seq > previous_seq)
            and (job_id is None or (type(job_id) is int and job_id > 0))
            and isinstance(mode, str)
            and mode in {"networked", "detach"}
            and isinstance(sources, dict)
            and isinstance(revisions, dict)
            and bool(revisions)
            and isinstance(digest, str)
        )
        if not valid_shape:
            return None, "Adapter returned an invalid Apply generation."
        streams = set(revisions)
        if streams != set(sources) or not streams.issubset(expected_selected):
            return None, "Adapter returned an invalid Apply generation."
        if any(type(revision) is not int for revision in revisions.values()) or any(
            (
                revisions[stream] != expected_selected[stream] or sources[stream] is not None
                if stream in switching_streams
                else type(sources[stream]) is not int or sources[stream] != expected_selected[stream]
            )
            for stream in streams
        ):
            return None, "Adapter returned an invalid Apply generation provenance."
        promoted_streams.update(revisions)
        generation_ids.add(generation_id)
        previous_seq = seq
    if promoted_streams != set(expected_selected) - set(skipped):
        return None, "Adapter returned incomplete Apply generation coverage."
    return promoted_streams, None


_ADMISSION_GENERATION_METADATA = ("generation_id", "seq", "digest", "mode", "stream_revisions", "source_push_seq")


def reconcile_admission_links(responses, generations, *, generation_ids=None):
    """Match immutable admission links with the complete generation listing."""
    by_id = {row["generation_id"]: row for row in generations}
    for response in responses:
        if not isinstance(response, dict):
            raise EvidenceInvariantError("admission_generation_mismatch")
        _response_generation_ids(response)
        for link in response.get("generations") or ():
            if generation_ids is not None and link["generation_id"] not in generation_ids:
                continue
            generation = by_id.get(link["generation_id"])
            with identify_generation_stall(generation):
                from .adapter_client import raise_item_error

                raise_item_error(generation)
                if generation is None:
                    raise EvidenceInvariantError("admission_generation_mismatch")
                for key in _ADMISSION_GENERATION_METADATA:
                    if key not in link or type(link[key]) is not type(generation[key]) or link[key] != generation[key]:
                        raise EvidenceInvariantError(f"admission_generation_mismatch: {key}")


def _reconcile_admissions(
    management,
    attempt_ids,
    snapshots,
    answers,
    generations,
    incarnation,
    settlement_sequences,
    *,
    carrier_id=None,
    carrier_attempt_ids=(),
):
    from django.db.models import F

    from .models import NSOApplyAttempt

    held_ids = (
        set(attempt_ids)
        if carrier_id is None
        else set(carrier_attempt_ids)
        | {
            UUID(raw["apply_attempt_id"])
            for snapshot in snapshots
            for raw in snapshot["attempts"]
            if any(row["carrier_job_id"] == carrier_id for row in _attempt_generations(raw))
        }
    )
    for snapshot in snapshots:
        head = snapshot["head"]
        if carrier_id is not None and head is not None and head["carrier_job_id"] != carrier_id:
            head = None
        if head is not None:
            head_carrier_id = head.get("carrier_job_id") if isinstance(head, dict) else None
            job = _carrier_job(management.adapter_device_id, head_carrier_id, settlement_sequences)
            with identify_evidence_stall(job, incarnation):
                from .adapter_client import raise_item_error

                raise_item_error(head)
                if head["apply_attempt_id"] is not None:
                    try:
                        held_ids.add(UUID(str(head["apply_attempt_id"])))
                    except (ValueError, TypeError, AttributeError):
                        raise EvidenceInvariantError("admission_generation_mismatch") from None
    responses = [
        _admission_response(raw)
        for snapshot in snapshots
        for raw in snapshot["attempts"]
        if carrier_id is None or UUID(raw["apply_attempt_id"]) in held_ids
    ]
    responses.extend(
        result
        for attempt, result, _error in answers
        if result is not None and (carrier_id is None or attempt.pk in held_ids)
    )
    if not all(isinstance(response, dict) for response in responses):
        raise EvidenceInvariantError("deployment evidence has an invalid stored response")
    responses.extend(
        error.rejection_response
        for attempt, _result, error in answers
        if error is not None and error.definitely_not_enqueued and (carrier_id is None or attempt.pk in held_ids)
    )
    responses.extend(
        NSOApplyAttempt.objects.filter(
            management=management,
            pk__in=held_ids,
            adapter_device_id=management.adapter_device_id,
            trigger_kind="manual",
            adapter_apply_attempt_id=F("pk"),
            response__isnull=False,
        ).values_list("response", flat=True)
    )
    selected = (
        None
        if carrier_id is None
        else {row["generation_id"] for row in generations if row["carrier_job_id"] == carrier_id}
        | {
            row["generation_id"]
            for snapshot in snapshots
            for attempt in snapshot["attempts"]
            for row in _attempt_generations(attempt)
            if row["carrier_job_id"] == carrier_id
        }
        | {
            link["generation_id"]
            for response in responses
            for link in response.get("generations") or ()
            if link.get("job_id") == carrier_id
        }
    )
    reconcile_admission_links(responses, generations, generation_ids=selected)


def _admission_keep_streams(attempt, response, *, rejected=False):
    """Validate admission before choosing which snapshots can be restored."""
    if rejected:
        return ()
    selected, skipped, error = apply_stream_partition(response, attempt.selected)
    if error:
        raise EvidenceInvariantError(error)
    if response.get("outcome") == "no_op":
        if set(skipped) != set(selected) or response.get("generations") != []:
            raise EvidenceInvariantError("Apply no-op does not cover its exact selection")
        return ()
    generations = response.get("generations")
    if response.get("outcome") != "promoted" or not isinstance(generations, list) or not generations:
        raise EvidenceInvariantError("Apply admission has no valid generation chain")
    promoted_streams, error = promoted_stream_coverage(generations, selected, skipped)
    if error:
        raise EvidenceInvariantError(error)
    return tuple(sorted(promoted_streams)) if skipped else None


def _retained_admission_restorations(local_attempts, validated):
    """Recover unfinished restoration from durable admission facts after remote pruning."""
    restorations = []
    for attempt_id, local in local_attempts.items():
        if attempt_id in validated or local.response is None:
            continue
        keep = _admission_keep_streams(local, local.response, rejected=local.http_status not in (200, 202))
        if keep is not None:
            restorations.append((attempt_id, keep))
    return restorations


def settle_apply_attempts(
    management,
    deployment_evidence,
    *,
    required_attempt_ids=(),
    store_incarnation=None,
    before_settle=None,
    after_settle=None,
) -> None:
    """Commit each complete carrier independently in settlement order."""
    if deployment_evidence["device_id"] != management.adapter_device_id:
        raise EvidenceInvariantError("deployment_mapping_changed")
    incarnation = store_incarnation or getattr(deployment_evidence, "store_incarnation", None)
    if not canonical_uuid(incarnation):
        raise EvidenceInvariantError("missing_store_incarnation")
    sequences = getattr(deployment_evidence, "settlement_sequences", {})
    carrier_id = getattr(deployment_evidence, "carrier_id", None)
    carriers = (
        {carrier_id}
        if carrier_id is not None
        else {
            generation["carrier_job_id"]
            for attempt in deployment_evidence["attempts"]
            for generation in _attempt_generations(attempt)
        }
        | {generation["carrier_job_id"] for generation in getattr(deployment_evidence, "automatic_generations", ())}
    )
    carriers.discard(None)
    passes = [(deployment_evidence, carrier) for carrier in sorted(carriers, key=lambda value: sequences.get(value, 0))]
    if carrier_id is None:
        # Carrier passes skip admissions that no carrier names, so an unscoped read ends with them.
        passes.append((_carrierless_view(deployment_evidence) if carriers else deployment_evidence, None))
    for evidence, carrier in passes:
        _settle_apply_carrier(
            management,
            evidence,
            carrier,
            required_attempt_ids=required_attempt_ids,
            store_incarnation=incarnation,
            before_settle=before_settle,
            after_settle=after_settle,
        )


def _carrierless_view(evidence):
    """Return bound evidence that holds only the admissions that no carrier names."""
    view = _BoundEvidence(evidence)
    view.__dict__.update(vars(evidence))
    view["head"] = None
    view["attempts"] = [attempt for attempt in evidence["attempts"] if not _attempt_generations(attempt)]
    view.automatic_generations = []
    view.manual_attempt_by_generation = {}
    return view


def _settle_apply_carrier(
    management,
    deployment_evidence,
    carrier_id,
    *,
    required_attempt_ids=(),
    store_incarnation=None,
    before_settle=None,
    after_settle=None,
):
    """Keep all generations, certificates and cursor effects in one owner transaction."""
    from .models import NSOApplyAttempt
    from .outcome_settlement import settle_terminal_generations

    incarnation = store_incarnation
    sequences = getattr(deployment_evidence, "settlement_sequences", {})
    local_attempts, validated, _unknown = _load_attempts(
        management,
        deployment_evidence,
        required_attempt_ids,
        store_incarnation=incarnation,
        settlement_sequences=sequences,
        carrier_id=carrier_id,
    )
    generations = []
    generation_modes = dict(getattr(deployment_evidence, "generation_modes", {}))
    restorations = list(getattr(deployment_evidence, "replay_restorations", ()))
    seen = set()
    for attempt_id, evidence in validated.items():
        local = local_attempts[attempt_id]
        keep = _admission_keep_streams(local, evidence["response"], rejected=evidence["admission_state"] == "rejected")
        for link in evidence["response"].get("generations") or ():
            generation_modes.setdefault(link["generation_id"], link["mode"])
        if keep is not None:
            restorations.append((attempt_id, keep))
        for generation in evidence["generations"]:
            identity = generation["generation_id"], generation["carrier_job_id"]
            generations.append((generation, local))
            seen.add(identity)
    for generation in getattr(deployment_evidence, "automatic_generations", ()):
        if carrier_id is not None and generation["carrier_job_id"] != carrier_id:
            continue
        identity = generation["generation_id"], generation["carrier_job_id"]
        if identity not in seen:
            local = getattr(deployment_evidence, "manual_attempt_by_generation", {}).get(identity)
            local = _manual_carrier_attempt(generation, validated, local_attempts, local)
            generations.append((generation, local))
            seen.add(identity)
    if carrier_id is None:
        restorations.extend(_retained_admission_restorations(local_attempts, validated))
    receipt_recoveries = getattr(deployment_evidence, "receipt_recoveries", {})

    def finish():
        for attempt_id, evidence in validated.items():
            local = local_attempts[attempt_id]
            if local.response is None:
                NSOApplyAttempt.objects.filter(pk=attempt_id, response__isnull=True).update(
                    http_status=evidence["http_status"], response=evidence["response"]
                )
        if after_settle is not None:
            after_settle()

    settle_terminal_generations(
        management,
        [item[0] for item in sorted(generations, key=lambda item: sequences.get(item[0]["carrier_job_id"], 0))],
        incarnation,
        attempts={
            (generation["generation_id"], generation["carrier_job_id"]): local for generation, local in generations
        },
        settle_sequences=sequences,
        modes=generation_modes,
        restorations=list(dict.fromkeys((attempt_id, tuple(keep)) for attempt_id, keep in restorations)),
        receipt_recoveries=receipt_recoveries,
        before_settle=before_settle,
        after_settle=finish,
    )


def _manual_carrier_attempt(generation, validated, local_attempts, head_attempt):
    """Join retained carrier evidence to its immutable manual admission link."""
    candidates = {
        attempt_id
        for attempt_id, evidence in validated.items()
        for link in evidence["response"].get("generations") or ()
        if link["generation_id"] == generation["generation_id"] and link["job_id"] == generation["carrier_job_id"]
    }
    if head_attempt is not None:
        candidates.add(head_attempt.pk)
    if len(candidates) > 1:
        raise EvidenceInvariantError("ambiguous_manual_carrier")
    if candidates:
        return local_attempts.get(next(iter(candidates)), head_attempt)
    return None


def deploying_attempt_ids(management) -> tuple[UUID, ...]:
    """Return manual attempts with unresolved snapshots across the closed registry."""
    from .durable_capture import mapping_epoch
    from .models import NSOApplyAttempt

    attempt_ids = (
        NSOApplyAttempt.objects.filter(
            management=management,
            adapter_device_id=management.adapter_device_id,
            source_epoch=management.adapter_source_epoch,
            trigger_kind="manual",
            adapter_apply_attempt_id__isnull=False,
            overlay_snapshots__mapping_epoch=mapping_epoch(management),
            overlay_snapshots__settlement_disposition="unresolved",
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    return tuple(sorted(attempt_ids, key=str))


def deployment_evidence_attempt_ids(management) -> tuple[UUID, ...]:
    """Return referenced attempts plus local attempts whose response was lost."""
    from .models import NSOApplyAttempt

    attempt_ids = set(deploying_attempt_ids(management))
    attempt_ids.update(
        NSOApplyAttempt.objects.filter(
            management=management,
            adapter_device_id=management.adapter_device_id,
            trigger_kind="manual",
            adapter_apply_attempt_id__isnull=False,
            lifecycle="open",
            response__isnull=True,
            created_at__gte=timezone.now() - LOST_RESPONSE_REPLAY_WINDOW,
        ).values_list("pk", flat=True)
    )
    return tuple(sorted(attempt_ids, key=str))


def _record_replay_answer(attempt, result=None, error=None):
    """Persist verified admission facts and return any later restoration decision."""
    from .models import NSOApplyAttempt

    if isinstance(result, dict):
        if result.get("outcome") not in ("promoted", "no_op"):
            return None
        status = 200 if result.get("outcome") == "no_op" else 202
        response = result
    elif error is not None and error.definitely_not_enqueued:
        status = error.status_code
        response = error.rejection_response
    else:
        return None
    keep_streams = _admission_keep_streams(attempt, response, rejected=error is not None)
    updated = NSOApplyAttempt.objects.filter(pk=attempt.pk, response__isnull=True).update(
        http_status=status,
        response=response,
    )
    if updated != 1:
        stored = NSOApplyAttempt.objects.get(pk=attempt.pk)
        if stored.http_status != status or stored.response != response:
            raise EvidenceInvariantError("An Apply replay changed its stored admission answer")
    if keep_streams is not None:
        return attempt.pk, keep_streams
    return None


def _reject_unrequested_attempts(evidence, attempt_ids) -> None:
    """Refuse an evidence snapshot whose attempt records are malformed or unrequested."""
    requested = set(attempt_ids)
    for raw in evidence["attempts"]:
        try:
            raw_id = UUID(raw["apply_attempt_id"])
        except ValueError:
            raise EvidenceInvariantError("invalid_apply_attempt_uuid") from None
        if raw_id not in requested:
            raise EvidenceInvariantError("unrequested_apply_attempt")


def _record_replay_answers(answers):
    restorations = []
    for attempt, result, error in answers:
        restoration = _record_replay_answer(attempt, result=result, error=error)
        if restoration is not None:
            restorations.append(restoration)
    return restorations


def _validated_unknown_attempt_ids(evidence, attempt_ids) -> set[UUID]:
    """Parse unknown attempt IDs and refuse IDs outside the request."""
    try:
        unknown = {UUID(value) for value in evidence["unknown_apply_attempt_ids"]}
    except ValueError:
        raise EvidenceInvariantError("invalid_unknown_attempt_uuid") from None
    if unknown - set(attempt_ids):
        raise EvidenceInvariantError("unrequested_unknown_apply_attempt")
    return unknown


def _replay_apply_answer(management, attempt):
    """Validate a replay answer before reconciliation can use its admission facts."""
    from . import adapter_client as client
    from .settlement_schemas import validate_settlement_response

    try:
        result = client.trigger_apply(management.adapter_device_id, attempt.pk, attempt.selected)
    except client.AdapterError as exc:
        if exc.code == "invalid_response" and exc.status_code is None:
            raise EvidenceInvariantError("invalid_apply_replay_response") from exc
        if exc.status_code != 409 or exc.code != "conflict":
            return attempt, None, exc
        job_id = exc.detail.get("job_id") if isinstance(exc.detail, dict) else None
        logger.info(
            "Apply replay for adapter device %s attempt %s is waiting for adapter job %s",
            management.adapter_device_id,
            attempt.pk,
            job_id,
        )
        return None
    validate_settlement_response("ActionApplyOut", result, management.adapter_device_id)
    if result["device_id"] != management.adapter_device_id:
        raise EvidenceInvariantError("deployment_mapping_changed")
    _admission_keep_streams(attempt, result)
    return attempt, result, None


def _replay_unknown_attempts(management, unknown, incarnation):
    """Recover eligible manual admission answers without restoring their owners."""
    from .models import NSOApplyAttempt

    replayed = False
    replay_answers = []
    for attempt in NSOApplyAttempt.objects.filter(
        pk__in=unknown,
        management=management,
        adapter_device_id=management.adapter_device_id,
        trigger_kind="manual",
        adapter_apply_attempt_id__isnull=False,
    ):
        if attempt.response is not None and attempt.http_status not in (200, 202):
            continue
        if (
            attempt.overlay_snapshots.filter(selection__binding_state="invalidated").exists()
            or attempt.overlay_snapshots.filter(selection__store_incarnation__isnull=False)
            .exclude(selection__store_incarnation=incarnation)
            .exists()
        ):
            continue
        answer = _replay_apply_answer(management, attempt)
        if answer is not None:
            replay_answers.append(answer)
        replayed = True
    return replay_answers, replayed


def load_deployment_evidence(management, *, attempt_ids=None, defer_carriers=False):
    """Fetch attempt evidence and recover unknown UUIDs by replaying their exact request."""
    from . import adapter_client as client
    from .models import NSOApplyAttempt
    from .settlement import SETTLE_FEED_PAGE

    if attempt_ids is None:
        attempt_ids = deployment_evidence_attempt_ids(management)
    else:
        attempt_ids = tuple(attempt_ids)
        eligible = set(
            NSOApplyAttempt.objects.filter(
                management=management,
                adapter_device_id=management.adapter_device_id,
                trigger_kind="manual",
                adapter_apply_attempt_id__isnull=False,
                pk__in=attempt_ids,
            ).values_list("pk", flat=True)
        )
        attempt_ids = tuple(attempt_id for attempt_id in attempt_ids if attempt_id in eligible)
    from .durable_capture import mapping_epoch

    epoch = mapping_epoch(management)
    cursor = (
        management.settle_cursor_seq or 0
        if management.settle_cursor_device_id == management.adapter_device_id and management.settle_cursor_incarnation
        else 0
    )
    feed, incarnation = client.get_settlement_feed(
        management.adapter_device_id, after_settle_seq=cursor, limit=SETTLE_FEED_PAGE
    )
    if cursor and incarnation != management.settle_cursor_incarnation:
        cursor = 0
        feed, incarnation = client.get_settlement_feed(
            management.adapter_device_id, after_settle_seq=cursor, limit=SETTLE_FEED_PAGE
        )
    sequences = {}
    with identify_evidence_stall({"device_id": management.adapter_device_id}, incarnation, sequences=sequences):
        seen_jobs = set()
        last_sequence = (
            max((job["settle_seq"] for job in feed if _positive_int(job["settle_seq"])), default=cursor)
            if defer_carriers
            else _validate_feed_page(feed, management.adapter_device_id, cursor, seen_jobs, incarnation)
        )
        page = feed
        while len(page) == SETTLE_FEED_PAGE and _positive_int(page[-1]["settle_seq"]):
            page, next_incarnation = client.get_settlement_feed(
                management.adapter_device_id, after_settle_seq=page[-1]["settle_seq"], limit=SETTLE_FEED_PAGE
            )
            if next_incarnation != incarnation:
                raise EvidenceInvariantError("evidence_epoch_changed")
            last_sequence = (
                max((job["settle_seq"] for job in page if _positive_int(job["settle_seq"])), default=last_sequence)
                if defer_carriers
                else _validate_feed_page(page, management.adapter_device_id, last_sequence, seen_jobs, incarnation)
            )
            feed.extend(page)
            if len(feed) > 10000:
                raise EvidenceInvariantError("settlement_feed_not_bounded")
        sequences.update(_settlement_sequences(feed))
        evidence = client.get_deployment_evidence(management.adapter_device_id, attempt_ids)
        snapshots = [evidence]
        if not defer_carriers:
            client.raise_evidence_errors(evidence)
            _reject_unrequested_attempts(evidence, attempt_ids)
        unknown = _validated_unknown_attempt_ids(evidence, attempt_ids)
        replay_answers, replayed = _replay_unknown_attempts(management, unknown, incarnation)
        if replayed:
            evidence = client.get_deployment_evidence(management.adapter_device_id, attempt_ids)
            if not defer_carriers:
                client.raise_evidence_errors(evidence)
                _reject_unrequested_attempts(evidence, attempt_ids)
            _validated_unknown_attempt_ids(evidence, attempt_ids)
            snapshots.append(evidence)
        generations = client.list_device_generations(management.adapter_device_id)
        _last_page, final_incarnation = client.get_settlement_feed(
            management.adapter_device_id, after_settle_seq=last_sequence, limit=1
        )
        if not defer_carriers:
            _validate_feed_page(_last_page, management.adapter_device_id, last_sequence, set(), final_incarnation)
        management.refresh_from_db()
        if incarnation != final_incarnation or mapping_epoch(management) != epoch:
            raise EvidenceInvariantError("evidence_epoch_changed")
        read = _BoundEvidence(evidence)
        read.store_incarnation = incarnation
        read.feed_jobs = feed
        read.generation_listing = generations
        read.read_attempt_ids = attempt_ids
        read.read_snapshots = snapshots
        read.replay_answers = replay_answers
        read.mapping_epoch = epoch
        if defer_carriers:
            return read
        bound = prepare_carrier_evidence(management, read)
        from .intent_state import MutationFootprint, mirror_transaction

        with mirror_transaction(MutationFootprint(device_ids=(management.device_id,))):
            management.refresh_from_db()
            validate_carrier_under_lock(management, bound)
        return bound


def prepare_carrier_evidence(management, read, carrier_id=None):
    """Validate only the carrier being committed; retain sibling failures for their turn."""
    from .adapter_client import raise_evidence_errors
    from .durable_capture import mapping_epoch

    if mapping_epoch(management) != read.mapping_epoch:
        raise EvidenceInvariantError("evidence_epoch_changed")
    jobs = [job for job in read.feed_jobs if carrier_id is None or job["id"] == carrier_id]
    relevant = [row for row in read.generation_listing if carrier_id is None or row["carrier_job_id"] == carrier_id]
    raise_evidence_errors(jobs)
    raise_evidence_errors(relevant)
    evidence = dict(read)
    if carrier_id is not None:
        if evidence["head"] is not None and evidence["head"]["carrier_job_id"] != carrier_id:
            evidence["head"] = None
        evidence["attempts"] = _carrier_attempts(management, read, carrier_id)
    for raw in evidence["attempts"]:
        from .adapter_client import InvalidJobEvidence, raise_item_error

        try:
            raise_item_error(raw)
        except InvalidJobEvidence as exc:
            if exc.job_id is None and _positive_int(carrier_id):
                exc.job_id = carrier_id
            raise
    scoped = {
        **evidence,
        "attempts": [
            {
                **raw,
                "generations": [
                    row
                    for row in raw["generations"]
                    if carrier_id is None or _generation_names_carrier(row, read, carrier_id)
                ],
            }
            for raw in evidence["attempts"]
        ],
    }
    raise_evidence_errors(scoped)
    _reject_unrequested_attempts(evidence, read.read_attempt_ids)
    sequences = _settlement_sequences(read.feed_jobs)
    _reconcile_admissions(
        management,
        read.read_attempt_ids,
        read.read_snapshots,
        read.replay_answers,
        read.generation_listing,
        read.store_incarnation,
        sequences,
        carrier_id=carrier_id,
        carrier_attempt_ids={UUID(raw["apply_attempt_id"]) for raw in evidence["attempts"]},
    )
    bound = _bound_evidence(
        evidence,
        jobs,
        read.generation_listing,
        read.store_incarnation,
        management=management,
        carrier_id=carrier_id,
        settlement_sequences=sequences,
    )
    bound.settlement_sequences.update(sequences)
    _restore_consumed_sequences(bound, management, read.store_incarnation)
    _require_ordered_terminal_evidence(bound, management, read.store_incarnation)
    pending, document = _load_pending_receipts(management, scoped, relevant)
    bound.receipt_recoveries = _prepare_receipt_recoveries(management, bound, pending, document, read.store_incarnation)
    bound.read = read
    held_ids = {UUID(raw["apply_attempt_id"]) for raw in evidence["attempts"]}
    bound.replay_answers = [answer for answer in read.replay_answers if carrier_id is None or answer[0].pk in held_ids]
    bound.replay_restorations = []
    for attempt, result, error in bound.replay_answers:
        response = result if result is not None else error.rejection_response if error.definitely_not_enqueued else None
        if response is not None:
            keep = _admission_keep_streams(attempt, response, rejected=error is not None)
            if keep is not None:
                bound.replay_restorations.append((attempt.pk, keep))
    return bound


def _carrier_attempts(management, read, carrier_id):
    """Keep the attempts that name *carrier_id*, with one local attempt read."""
    from django.db.models import F

    from .models import NSOApplyAttempt

    local = NSOApplyAttempt.objects.filter(
        management=management,
        adapter_device_id=management.adapter_device_id,
        pk__in=[raw["apply_attempt_id"] for raw in read["attempts"]],
        trigger_kind="manual",
        adapter_apply_attempt_id=F("pk"),
    )
    attempts = {str(attempt.pk): attempt for attempt in local}
    return [raw for raw in read["attempts"] if _attempt_names_carrier(attempts, raw, read, carrier_id)]


def _attempt_names_carrier(local_attempts, raw, read, carrier_id):
    """Keep attempts attached to their independently retained carrier admission."""
    if any(_generation_names_carrier(row, read, carrier_id) for row in _attempt_generations(raw)):
        return True
    head = read["head"]
    if (
        head is not None
        and head.get("apply_attempt_id") == raw["apply_attempt_id"]
        and head["carrier_job_id"] == carrier_id
    ):
        return True
    local = local_attempts.get(raw["apply_attempt_id"])
    if local is None or local.response is None:
        return False
    generation_ids = _response_generation_ids(local.response)
    if any(link["job_id"] == carrier_id for link in local.response.get("generations") or ()):
        return True
    if any(
        row["generation_id"] in generation_ids
        for job in read.feed_jobs
        if job["id"] == carrier_id
        for row in job.get("row_outcomes") or ()
    ):
        return True
    return any(
        row["generation_id"] in generation_ids and row["carrier_job_id"] == carrier_id
        for row in read.generation_listing
    )


def _generation_names_carrier(row, read, carrier_id):
    """Retain failed generation projections through the listing's independent carrier."""
    from .adapter_client import EvidenceItem

    return (
        row["carrier_job_id"] == carrier_id
        or isinstance(row, EvidenceItem)
        and any(
            generation["generation_id"] == row["generation_id"] and generation["carrier_job_id"] == carrier_id
            for generation in read.generation_listing
        )
    )


def validate_carrier_under_lock(management, evidence):
    """Recheck immutable certificates and mapping under the carrier's owner locks."""
    from .durable_capture import mapping_epoch
    from .models import NSOAcceptedSelection

    read = evidence.read
    if mapping_epoch(management) != read.mapping_epoch:
        raise EvidenceInvariantError("evidence_epoch_changed")
    _require_terminal_metadata(evidence, read.generation_listing, management, read.store_incarnation)
    _drop_earlier_epoch_history(evidence, management, read.store_incarnation)
    _restore_consumed_sequences(evidence, management, read.store_incarnation)
    _require_ordered_terminal_evidence(evidence, management, read.store_incarnation)
    NSOAcceptedSelection.objects.filter(management=management, binding_state="bound").exclude(
        store_incarnation=read.store_incarnation
    ).update(binding_state="invalidated")
    _record_replay_answers(evidence.replay_answers)


def _require_ordered_terminal_evidence(evidence, management, incarnation):
    """Keep unsequenced remote terminal evidence from authorizing local effects."""
    generations = list(evidence.automatic_generations)
    generations.extend(
        generation
        for attempt in evidence["attempts"]
        for generation in attempt["generations"]
        if evidence.carrier_id is None or generation["carrier_job_id"] == evidence.carrier_id
    )
    for generation in generations:
        if terminal_carrier_requires_certificate(generation) and not _positive_int(
            evidence.settlement_sequences.get(generation["carrier_job_id"])
        ):
            with identify_generation_stall(generation):
                _missing_generation_evidence(management, incarnation, generation)


def _restore_consumed_sequences(bound, management, incarnation):
    """Recover omitted carrier order from exact durable execution certificates."""
    from .outcome_settlement import immutable_execution_projection

    generations = list(bound.automatic_generations)
    generations.extend(
        generation
        for attempt in bound["attempts"]
        for generation in attempt["generations"]
        if (getattr(bound, "carrier_id", None) is None or generation["carrier_job_id"] == bound.carrier_id)
        and generation["row_outcomes"] is not None
    )
    by_identity = {}
    for generation in generations:
        identity = generation["generation_id"], generation["carrier_job_id"]
        by_identity.setdefault(identity, []).append(generation)
    certificates = _consumed_executions(management, incarnation).filter(
        carrier_job_id__in={identity[1] for identity in by_identity},
    )
    for execution in certificates:
        job = _carrier_job(management.adapter_device_id, execution.carrier_job_id, bound.settlement_sequences)
        with identify_evidence_stall(job, incarnation):
            identity = execution.generation_id, execution.carrier_job_id
            candidates = by_identity.get(identity)
            if candidates is None:
                continue
            projection = _execution_projection(execution)
            if any(
                immutable_execution_projection(_validate_generation(candidate))
                != immutable_execution_projection(projection)
                for candidate in candidates
            ):
                raise EvidenceInvariantError("consumed_execution_changed")
            sequence = execution.settle_seq
            known = bound.settlement_sequences.get(execution.carrier_job_id)
            if sequence is None:
                if known is None:
                    _missing_generation_evidence(management, incarnation, projection)
                continue
            if not _positive_int(sequence) or known is not None and known != sequence:
                raise EvidenceInvariantError("invalid_consumed_settlement_sequence")
            bound.settlement_sequences[execution.carrier_job_id] = sequence


def _prepare_receipt_recoveries(management, bound, selections, document, incarnation):
    from .outcome_settlement import _record_invariant
    from .selection_receipts import prepare_recoveries

    if not selections:
        return {}
    try:
        return prepare_recoveries(management, selections, document, incarnation)
    except EvidenceInvariantError:
        generations = list(bound.automatic_generations)
        generations.extend(generation for attempt in bound["attempts"] for generation in attempt["generations"])
        for generation in generations:
            _record_invariant(management, generation, incarnation, None, "invalid_selection_receipt")
        raise


def _load_pending_receipts(management, evidence, generations):
    from .adapter_client import AdapterError, get_intent_receipts
    from .selection_receipts import pending_selections

    selections = pending_selections(management, evidence, generations)
    document = None
    if selections:
        try:
            document = get_intent_receipts(device_id=management.adapter_device_id)
        except AdapterError as exc:
            if exc.code != "invalid_response" or exc.status_code is not None:
                raise
            raise EvidenceInvariantError("invalid_selection_receipt") from exc
    return selections, document


def settle_device_apply_attempts(management):
    """Fetch one evidence snapshot and settle this device's currently referenced attempts."""
    evidence = load_deployment_evidence(management)
    if evidence is not None:
        with identify_evidence_stall({"device_id": management.adapter_device_id}, evidence.store_incarnation):
            settle_apply_attempts(
                management,
                evidence,
            )
    return evidence


def route_policy_carriers(deployment_evidence, *, attempt_ids=None):
    """Return every exact validated carrier in settlement order, never ordered by job IDs."""
    from .adapter_client import raise_item_error

    candidates = []
    allowed = None if attempt_ids is None else set(attempt_ids)
    if not isinstance(deployment_evidence, dict):
        return []
    for attempt in deployment_evidence["attempts"]:
        if allowed is not None and UUID(str(attempt["apply_attempt_id"])) not in allowed:
            continue
        raise_item_error(attempt)
        for generation in attempt["generations"]:
            _validate_generation(generation)
            if generation["row_outcomes"] is not None and any(
                row["stream"] == "route_policy" for row in generation["row_outcomes"]
            ):
                candidates.append(generation)
    if allowed is None:
        for generation in getattr(deployment_evidence, "automatic_generations", ()):
            _validate_generation(generation)
            if generation["row_outcomes"] is not None and any(
                row["stream"] == "route_policy" for row in generation["row_outcomes"]
            ):
                candidates.append(generation)
    candidates = list({(row["generation_id"], row["carrier_job_id"]): row for row in candidates}.values())
    sequences = getattr(deployment_evidence, "settlement_sequences", {})
    if len(candidates) > 1 and any(row["carrier_job_id"] not in sequences for row in candidates):
        return []
    return [
        {
            "id": generation["carrier_job_id"],
            "store_incarnation": getattr(deployment_evidence, "store_incarnation", None),
            "generation_id": generation["generation_id"],
            "status": generation["carrier_job_status"],
            "result": generation["carrier_job_result"],
            "error": generation["carrier_job_error"],
            "updated_at": generation["updated_at"],
        }
        for generation in sorted(candidates, key=lambda row: sequences.get(row["carrier_job_id"], 0))
    ]


class _BoundEvidence(dict):
    """The validated HTTP body with its separately fetched store epoch."""


def _bound_evidence(
    evidence, jobs, generations, incarnation, *, management, carrier_id=None, settlement_sequences=None
):
    """Join jobs to authoritative generation sequences, including automatic heads."""
    bound = _BoundEvidence(evidence)
    bound.store_incarnation = incarnation
    bound.carrier_id = carrier_id
    bound.settlement_sequences = _settlement_sequences(jobs) if settlement_sequences is None else settlement_sequences
    bound.feed_carrier_ids = {job["id"] for job in jobs}
    bound.automatic_generations = []
    bound.manual_attempt_by_generation = {}
    by_id = {row["generation_id"]: row for row in generations}
    head = evidence["head"]
    if head is not None and head["row_outcomes"] is not None:
        job = _carrier_job(management.adapter_device_id, head["carrier_job_id"], bound.settlement_sequences)
        with identify_evidence_stall(job, incarnation):
            _append_head_evidence(bound, head, by_id, management, incarnation)
    for job in jobs:
        with identify_evidence_stall(job, incarnation):
            outcomes = job["row_outcomes"]
            if outcomes is None:
                continue
            carrier = {
                "carrier_job_id": job["id"],
                "carrier_job_status": job["status"],
                "carrier_job_result": job["result"],
                "carrier_job_error": job["error"],
                "row_outcomes": job["row_outcomes"],
                "refusals": job["refusals"],
                "updated_at": job["updated_at"],
            }
            identifiers = _carrier_generation_ids(outcomes) | {
                generation["generation_id"] for generation in generations if generation["carrier_job_id"] == job["id"]
            }
            for generation_id in identifiers:
                bound.automatic_generations.append(
                    _authoritative_carrier_generation(
                        by_id.get(generation_id), generation_id, carrier, management, incarnation, bound=bound
                    )
                )
    _require_terminal_metadata(bound, generations, management, incarnation)
    _bind_generation_modes(bound, generations)
    _drop_earlier_epoch_history(bound, management, incarnation)
    return bound


def _bind_generation_modes(bound, generations):
    """Read modes only for the carrier and generations whose evidence was required."""
    from .adapter_client import raise_item_error

    identifiers = {row["generation_id"] for row in bound.automatic_generations}
    bound.generation_modes = {}
    for row in generations:
        if bound.carrier_id is None or row["carrier_job_id"] == bound.carrier_id or row["generation_id"] in identifiers:
            raise_item_error(row)
            if "mode" in row:
                bound.generation_modes[row["generation_id"]] = row["mode"]


def _carrier_generation_ids(outcomes):
    return {identity["generation_id"] for outcome in outcomes for identity in (outcome, outcome["carrier"])}


def _append_head_evidence(bound, head, by_id, management, incarnation):
    from .models import NSOApplyAttempt

    local = None
    if head["apply_attempt_id"] is not None:
        local = NSOApplyAttempt.objects.filter(
            pk=head["apply_attempt_id"],
            management=management,
            adapter_device_id=management.adapter_device_id,
            trigger_kind="manual",
            adapter_apply_attempt_id=head["apply_attempt_id"],
        ).first()
    manual_generations = set()
    if local is not None:
        manual_generations = (
            {head["generation_id"]} if local.response is None else _response_generation_ids(local.response)
        )
    _match_listing_metadata(by_id.get(head["generation_id"]), head)
    if head["carrier_job_id"] == by_id[head["generation_id"]]["carrier_job_id"]:
        _match_current_carrier(by_id[head["generation_id"]], head)
    identifiers = _carrier_generation_ids(head["row_outcomes"]) | {head["generation_id"]}
    for generation_id in identifiers:
        bound.automatic_generations.append(
            _authoritative_carrier_generation(
                by_id.get(generation_id), generation_id, head, management, incarnation, bound=bound
            )
        )
        if local is not None and generation_id in manual_generations:
            bound.manual_attempt_by_generation[(generation_id, head["carrier_job_id"])] = local


_IMMUTABLE_GENERATION_METADATA = ("generation_id", "seq", "stream_revisions", "source_push_seq")


def _match_listing_metadata(generation, evidence):
    from .adapter_client import raise_item_error

    raise_item_error(generation)
    raise_item_error(evidence)
    if generation is None or any(evidence.get(key) != generation[key] for key in _IMMUTABLE_GENERATION_METADATA):
        raise EvidenceInvariantError("generation_evidence_mismatch")


def _match_current_carrier(generation, evidence):
    _match_listing_metadata(generation, evidence)
    if evidence.get("carrier_job_status") != generation["carrier_job_status"]:
        raise EvidenceInvariantError("generation_evidence_mismatch")


def _missing_generation_evidence(management, incarnation, generation):
    from .outcome_settlement import _record_invariant

    if generation.get("carrier_job_id") is None:
        logger.warning(
            "missing_generation_evidence: terminal generation %s has no retained carrier", generation["generation_id"]
        )
    _record_invariant(management, generation, incarnation, None, "missing_generation_evidence")
    raise EvidenceInvariantError("missing_generation_evidence")


def _drop_earlier_epoch_history(bound, management, incarnation):
    """Drop generations consumed only under an earlier mapping epoch: the cursor already passed them."""
    from .durable_capture import mapping_epoch
    from .outcome_settlement import immutable_execution_projection

    carrier_ids = {generation["carrier_job_id"] for generation in bound.automatic_generations}
    epoch = mapping_epoch(management)
    current, earlier = set(), {}
    for execution in _cursor_epoch_executions(management, incarnation).filter(carrier_job_id__in=carrier_ids):
        identity = execution.generation_id, execution.carrier_job_id
        if execution.mapping_epoch == epoch:
            current.add(identity)
        else:
            earlier[identity] = execution
    kept = []
    for generation in bound.automatic_generations:
        job = _carrier_job(management.adapter_device_id, generation["carrier_job_id"], bound.settlement_sequences)
        with identify_evidence_stall(job, incarnation):
            identity = generation["generation_id"], generation["carrier_job_id"]
            execution = earlier.get(identity)
            if execution is None or identity in current:
                kept.append(generation)
            elif immutable_execution_projection(_validate_generation(generation)) != immutable_execution_projection(
                _execution_projection(execution)
            ):
                raise EvidenceInvariantError("consumed_execution_changed")
    bound.automatic_generations = kept


def _historical_certificate(management, incarnation, generation_id, carrier_id):
    candidates = list(
        _cursor_epoch_executions(management, incarnation).filter(
            generation_id=generation_id,
            carrier_job_id=carrier_id,
        )[:2]
    )
    if len(candidates) != 1 or candidates[0].invariant_code or candidates[0].consumed_at is None:
        return None
    return _execution_projection(candidates[0])


def _ordered_feed_carrier(generation, carrier_id, bound):
    """Prove that a retained feed carrier precedes the listing's current retry."""
    if generation is None or carrier_id not in bound.feed_carrier_ids:
        return False
    previous = bound.settlement_sequences.get(carrier_id)
    current = bound.settlement_sequences.get(generation["carrier_job_id"])
    return _positive_int(previous) and _positive_int(current) and previous < current


def _authoritative_carrier_generation(generation, generation_id, carrier, management, incarnation, *, bound):
    from .adapter_client import raise_item_error

    raise_item_error(generation)
    raise_item_error(carrier)
    if (
        generation is not None
        and generation["carrier_job_id"] is not None
        and generation["carrier_job_id"] == carrier["carrier_job_id"]
    ):
        projection = _generation_from_carrier(generation, carrier)
        _match_current_carrier(generation, projection)
        return projection
    cached = _historical_certificate(management, incarnation, generation_id, carrier["carrier_job_id"])
    if cached is not None and generation is not None:
        _match_listing_metadata(generation, cached)
        if any(cached[key] != carrier[key] for key in _CARRIER_EVIDENCE_FIELDS):
            raise EvidenceInvariantError("consumed_execution_changed")
        return cached
    if _ordered_feed_carrier(generation, carrier["carrier_job_id"], bound):
        # The retained result proves outcomes, but the retry owns the listed generation state.
        projection = {
            **_generation_from_carrier(generation, carrier),
            "status": "outcome_unknown",
            "updated_at": carrier["updated_at"],
        }
        return _validate_generation(projection)
    return _missing_generation_evidence(
        management,
        incarnation,
        {
            "generation_id": generation_id,
            "carrier_job_id": carrier["carrier_job_id"],
        },
    )


def _require_terminal_metadata(bound, generations, management, incarnation):
    """Certify current carriers separately from exact historical certificates."""
    from .outcome_settlement import immutable_execution_projection

    by_id = {generation["generation_id"]: generation for generation in generations}
    present = set()
    evidence = list(bound.automatic_generations)
    evidence.extend(
        generation
        for attempt in bound["attempts"]
        for generation in attempt["generations"]
        if (getattr(bound, "carrier_id", None) is None or generation["carrier_job_id"] == bound.carrier_id)
        and generation["row_outcomes"] is not None
    )
    for row in evidence:
        job = _carrier_job(management.adapter_device_id, row["carrier_job_id"], bound.settlement_sequences)
        with identify_evidence_stall(job, incarnation):
            generation = by_id.get(row["generation_id"])
            _match_listing_metadata(generation, row)
            if generation["carrier_job_id"] is not None and row["carrier_job_id"] == generation["carrier_job_id"]:
                _match_current_carrier(generation, row)
                present.add((generation["generation_id"], generation["carrier_job_id"]))
            else:
                cached = _historical_certificate(management, incarnation, row["generation_id"], row["carrier_job_id"])
                if cached is None:
                    if not _ordered_feed_carrier(generation, row["carrier_job_id"], bound):
                        _missing_generation_evidence(management, incarnation, row)
                    _validate_generation(row)
                elif immutable_execution_projection(cached) != immutable_execution_projection(row):
                    raise EvidenceInvariantError("consumed_execution_changed")
    _require_matching_carrier_projections(evidence, bound, management, incarnation)
    pending = [
        generation
        for generation in generations
        if (getattr(bound, "carrier_id", None) is None or generation["carrier_job_id"] == bound.carrier_id)
        and terminal_carrier_requires_certificate(generation)
        and (generation["generation_id"], generation["carrier_job_id"]) not in present
    ]
    certificates = generation_certificate_candidates(management, incarnation, pending)
    for generation in pending:
        job = _carrier_job(management.adapter_device_id, generation["carrier_job_id"], bound.settlement_sequences)
        with identify_evidence_stall(job, incarnation):
            identity = generation["generation_id"], generation["carrier_job_id"]
            if generation["carrier_job_id"] is None:
                _missing_generation_evidence(management, incarnation, generation)
            projection = certified_projection(generation, certificates.get(identity, ()))
            if projection is None:
                _missing_generation_evidence(management, incarnation, generation)
            bound.automatic_generations.append(projection)


def _require_matching_carrier_projections(evidence, bound, management, incarnation):
    """Reject conflicting projections before deduplication can select an owner outcome."""
    from .outcome_settlement import immutable_execution_projection

    projections = {}
    carriers = {}
    for row in evidence:
        job = _carrier_job(management.adapter_device_id, row["carrier_job_id"], bound.settlement_sequences)
        with identify_evidence_stall(job, incarnation):
            _validate_generation(row)
            identity = row["generation_id"], row["carrier_job_id"]
            projection = immutable_execution_projection(row)
            carrier = {key: row[key] for key in _CARRIER_EVIDENCE_FIELDS}
            if (
                identity in projections
                and projections[identity] != projection
                or row["carrier_job_id"] in carriers
                and carriers[row["carrier_job_id"]] != carrier
            ):
                _record_carrier_projection_conflict(evidence, bound, management, incarnation, row["carrier_job_id"])
                raise EvidenceInvariantError("invalid_terminal_batch")
            projections[identity] = projection
            carriers[row["carrier_job_id"]] = carrier


def _record_carrier_projection_conflict(evidence, bound, management, incarnation, carrier_id):
    """Retain the rejected carrier's complete generation diagnostics."""
    from .models import NSOApplyAttempt
    from .outcome_settlement import _record_invariant

    attempts = {
        row["generation_id"]: UUID(raw["apply_attempt_id"])
        for raw in bound["attempts"]
        for row in raw["generations"]
        if row["carrier_job_id"] == carrier_id
    }
    local = NSOApplyAttempt.objects.filter(management=management, pk__in=set(attempts.values())).in_bulk()
    generations = {row["generation_id"]: row for row in evidence if row["carrier_job_id"] == carrier_id}
    for generation_id, generation in generations.items():
        _record_invariant(
            management, generation, incarnation, local.get(attempts.get(generation_id)), "invalid_terminal_batch"
        )


def terminal_carrier_requires_certificate(generation):
    """Require terminal evidence because a nullable job FK cannot prove that no carrier ran."""
    from .adapter_client import raise_item_error

    raise_item_error(generation)
    return generation["status"] not in {"pending", "running"}


def generation_certificate_candidates(management, incarnation, generations):
    """Load the consumed certificates of every listed current carrier in one query."""
    carriers = [generation for generation in generations if generation["carrier_job_id"] is not None]
    candidates = {}
    if not carriers:
        return candidates
    for cached in generation_certificates(management, incarnation, *carriers).filter(
        carrier_job_id__in={generation["carrier_job_id"] for generation in carriers}
    ):
        candidates.setdefault((cached.generation_id, cached.carrier_job_id), []).append(cached)
    return candidates


def certified_projection(generation, candidates):
    """Require one ordered consumed certificate for the listing's current carrier."""
    if generation["carrier_job_id"] is None or len(candidates) != 1:
        return None
    cached = candidates[0]
    if cached.invariant_code or cached.consumed_at is None or not _positive_int(cached.settle_seq):
        return None
    projection = _execution_projection(cached)
    _match_current_carrier(generation, projection)
    return projection


def generation_certificates(management, incarnation, *generations):
    """Keep all consumed carriers that recovery can request for the listed generations, in any mapping epoch."""
    return _cursor_epoch_executions(management, incarnation).filter(
        generation_id__in=[generation["generation_id"] for generation in generations]
    )


def _cursor_epoch_executions(management, incarnation):
    """Return consumed certificates under the settlement cursor's epoch: store incarnation and adapter device."""
    from .models import NSOApplyExecution

    return NSOApplyExecution.objects.filter(
        management=management,
        adapter_device_id=management.adapter_device_id,
        store_incarnation=incarnation,
        consumption_state__in=("consumed", "superseded"),
    )


def _consumed_executions(management, incarnation):
    from .durable_capture import mapping_epoch

    return _cursor_epoch_executions(management, incarnation).filter(mapping_epoch=mapping_epoch(management))


def _execution_projection(execution):
    projection = _validate_generation(execution.terminal_projection)
    if (projection["generation_id"], projection["carrier_job_id"], projection["seq"]) != (
        execution.generation_id,
        execution.carrier_job_id,
        execution.generation_seq,
    ):
        raise EvidenceInvariantError("consumed_execution_changed")
    return projection


def _generation_from_carrier(generation, carrier):
    from .adapter_client import raise_item_error
    from .refusal_registry import document_section_for_stream

    raise_item_error(generation)
    raise_item_error(carrier)
    return {
        "generation_id": generation["generation_id"],
        "seq": generation["seq"],
        "status": generation["status"],
        "sections": sorted({document_section_for_stream(stream) for stream in generation["stream_revisions"]}),
        "stream_revisions": generation["stream_revisions"],
        "source_push_seq": generation["source_push_seq"],
        **{key: carrier[key] for key in _CARRIER_EVIDENCE_FIELDS},
        "updated_at": generation["updated_at"],
    }


def _settlement_sequences(jobs):
    """Keep the first binding until the ordered walk rejects a repeated carrier."""
    sequences = {}
    for job in jobs:
        sequences.setdefault(job["id"], job["settle_seq"])
    return sequences


def carrierless_evidence_read(read):
    """Select admissions that have no carrier after ordered settlement has drained."""
    scoped = _BoundEvidence(read)
    scoped.__dict__.update(vars(read))
    scoped["head"] = None
    scoped["attempts"] = [attempt for attempt in read["attempts"] if not _attempt_generations(attempt)]
    scoped.feed_jobs = []
    scoped.generation_listing = []
    scoped.read_attempt_ids = tuple(
        {UUID(attempt["apply_attempt_id"]) for attempt in scoped["attempts"]}
        | {UUID(value) for value in scoped["unknown_apply_attempt_ids"]}
    )
    scoped.read_snapshots = [
        {
            **snapshot,
            "head": None,
            "attempts": [attempt for attempt in snapshot["attempts"] if not _attempt_generations(attempt)],
        }
        for snapshot in read.read_snapshots
    ]
    scoped.replay_answers = [answer for answer in read.replay_answers if answer[0].pk in scoped.read_attempt_ids]
    return scoped


def _validate_feed_page(jobs, adapter_device_id, after, seen, incarnation):
    """Require ordered terminal carriers before any local settlement mutation."""
    for job in jobs:
        with identify_evidence_stall(job, incarnation):
            from .adapter_client import raise_item_error

            raise_item_error(job)
            sequence = job["settle_seq"]
            if (
                type(sequence) is not int
                or sequence <= after
                or job["device_id"] != adapter_device_id
                or job["id"] in seen
                or job["status"] not in {"succeeded", "failed"}
            ):
                raise EvidenceInvariantError("invalid_settlement_order")
            seen.add(job["id"])
            after = sequence
    return after
