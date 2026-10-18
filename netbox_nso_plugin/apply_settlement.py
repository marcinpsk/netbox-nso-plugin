# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Settle Apply rows only from evidence addressed to their durable attempt."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from django.utils import timezone

from .refusal_contract import GENERATION_FIELDS

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
_ATTEMPT_FIELDS = frozenset({"apply_attempt_id", "admission_state", "http_status", "response", "generations"})
_GENERATION_FIELDS = GENERATION_FIELDS


class EvidenceInvariantError(RuntimeError):
    """The adapter evidence cannot truthfully judge local Apply rows."""


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise EvidenceInvariantError("generation updated_at is not an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise EvidenceInvariantError("generation updated_at is not timezone-aware")
    return parsed


def _validate_generation(raw) -> dict:
    from .refusal_contract import ContractError, validate_generation

    try:
        return validate_generation(raw)
    except ContractError as exc:
        raise EvidenceInvariantError("invalid_terminal_batch") from exc


def _response_generation_ids(response) -> set[int]:
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


def _validate_attempt(raw, local, store_incarnation=None) -> dict:
    if not isinstance(raw, dict) or set(raw) != _ATTEMPT_FIELDS:
        raise EvidenceInvariantError("deployment evidence attempt has an invalid shape")
    try:
        attempt_id = UUID(str(raw["apply_attempt_id"]))
    except (TypeError, ValueError, AttributeError):
        raise EvidenceInvariantError("deployment evidence has an invalid attempt UUID") from None
    if attempt_id != local.pk:
        raise EvidenceInvariantError("deployment evidence names the wrong local attempt")
    if type(raw["admission_state"]) is not str or raw["admission_state"] not in {"admitted", "rejected"}:
        raise EvidenceInvariantError("deployment evidence has an invalid admission state")
    if (
        type(raw["http_status"]) is not int
        or not 100 <= raw["http_status"] <= 599
        or not isinstance(raw["response"], dict)
    ):
        raise EvidenceInvariantError("deployment evidence has an invalid stored response")
    response = raw["response"]
    selected = response.get("selected")
    if raw["admission_state"] == "admitted":
        if response.get("device_id") != local.adapter_device_id:
            raise EvidenceInvariantError("admission_mapping_changed")
        if not isinstance(selected, dict) or selected != local.selected:
            raise EvidenceInvariantError("deployment evidence changed the Apply selection")
        skipped = response.get("skipped")
        if response.get("outcome") not in {"promoted", "no_op"} or not isinstance(skipped, dict):
            raise EvidenceInvariantError("deployment evidence has an invalid admitted response")
    generations = _validated_attempt_generations(raw["generations"], local, store_incarnation)
    if len({generation["generation_id"] for generation in generations}) != len(generations):
        raise EvidenceInvariantError("deployment evidence repeats a generation identity")
    if _response_generation_ids(response) != {generation["generation_id"] for generation in generations}:
        raise EvidenceInvariantError("deployment evidence is incomplete for its stored response")
    if local.response is not None and (local.http_status != raw["http_status"] or local.response != response):
        raise EvidenceInvariantError("local and adapter Apply responses disagree")
    return {**raw, "id": attempt_id, "generations": generations}


def _validated_attempt_generations(raw_generations, local, store_incarnation):
    if not isinstance(raw_generations, list):
        raise EvidenceInvariantError("deployment evidence generations are not a list")
    generations = []
    for generation in raw_generations:
        try:
            generations.append(_validate_generation(generation))
        except EvidenceInvariantError:
            from .outcome_settlement import _record_invariant

            _record_invariant(local.management, generation, store_incarnation, local, "invalid_terminal_batch")
            raise
    return generations


def _load_attempts(management, deployment_evidence, required_attempt_ids=(), *, store_incarnation=None):
    from .models import NSOApplyAttempt

    raw_attempts = deployment_evidence["attempts"]
    if not isinstance(raw_attempts, list) or not isinstance(deployment_evidence["unknown_apply_attempt_ids"], list):
        raise EvidenceInvariantError("deployment evidence attempt collections are invalid")
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
        local = local_attempts.get(raw_id)
        if local is None:
            continue
        if raw_id in validated:
            raise EvidenceInvariantError("deployment evidence repeats an Apply attempt")
        if local.adapter_device_id != management.adapter_device_id:
            continue
        validated[raw_id] = _validate_attempt(raw, local, store_incarnation)

    if unknown_ids & set(validated):
        raise EvidenceInvariantError("an Apply attempt is both known and unknown")
    return local_attempts, validated, unknown_ids


_APPLY_GENERATION_FIELDS = frozenset(
    {"generation_id", "seq", "job_id", "mode", "source_push_seq", "stream_revisions", "digest"}
)
_APPLY_SKIP_REASONS = frozenset(
    {"superseded", "already_applied", "already_authorized", "no_receipt", "backfill_only", "revision_mismatch"}
)


def apply_stream_partition(result, selected):
    """Validate the selector echo and return its selected and skipped stream sets."""
    expected_selected = dict(selected)
    returned_selected = result.get("selected")
    skipped = result.get("skipped")
    if not isinstance(returned_selected, dict) or returned_selected != expected_selected:
        return None, None, "Adapter returned an invalid Apply selector."
    if (
        not isinstance(skipped, dict)
        or not set(skipped).issubset(expected_selected)
        or any(type(reason) is not str or reason not in _APPLY_SKIP_REASONS for reason in skipped.values())
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
) -> None:
    """Consume complete logical batches; counts never choose owner status."""
    from .adapter_client import DEPLOYMENT_EVIDENCE_FIELDS
    from .models import NSOApplyAttempt
    from .outcome_settlement import settle_terminal_generations

    if not isinstance(deployment_evidence, dict) or set(deployment_evidence) != DEPLOYMENT_EVIDENCE_FIELDS:
        raise EvidenceInvariantError("invalid_deployment_envelope")
    if deployment_evidence["device_id"] != management.adapter_device_id:
        raise EvidenceInvariantError("deployment_mapping_changed")
    incarnation = store_incarnation or getattr(deployment_evidence, "store_incarnation", None)
    if not incarnation:
        raise EvidenceInvariantError("missing_store_incarnation")
    local_attempts, validated, _unknown = _load_attempts(
        management, deployment_evidence, required_attempt_ids, store_incarnation=incarnation
    )
    generations = []
    generation_modes = dict(getattr(deployment_evidence, "generation_modes", {}))
    restorations = list(getattr(deployment_evidence, "replay_restorations", ()))
    seen = set()
    for attempt_id, evidence in validated.items():
        local = local_attempts[attempt_id]
        keep = _admission_keep_streams(local, evidence["response"], rejected=evidence["admission_state"] == "rejected")
        for link in evidence["response"].get("generations", []):
            generation_modes[link["generation_id"]] = link["mode"]
        if keep is not None:
            restorations.append((attempt_id, keep))
        for generation in evidence["generations"]:
            identity = generation["generation_id"], generation["carrier_job_id"]
            generations.append((generation, local))
            seen.add(identity)
    for generation in getattr(deployment_evidence, "automatic_generations", ()):
        identity = generation["generation_id"], generation["carrier_job_id"]
        if identity not in seen:
            local = getattr(deployment_evidence, "manual_attempt_by_generation", {}).get(identity)
            generations.append((generation, local))
            seen.add(identity)
    restorations.extend(_retained_admission_restorations(local_attempts, validated))
    # Every envelope and terminal batch is checked before the first local status write.
    from .outcome_settlement import validate_terminal_generation

    authorities = {}
    receipt_recoveries = getattr(deployment_evidence, "receipt_recoveries", {})
    for generation, _local in generations:
        authorities.setdefault(generation["carrier_job_id"], {})[generation["generation_id"]] = generation
    for generation, local in generations:
        validate_terminal_generation(
            management,
            generation,
            incarnation,
            attempt=local,
            authorities=authorities[generation["carrier_job_id"]],
            receipt_recoveries=receipt_recoveries,
        )
    sequences = getattr(deployment_evidence, "settlement_sequences", {})
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
    )
    for attempt_id, evidence in validated.items():
        local = local_attempts[attempt_id]
        if local.response is None:
            NSOApplyAttempt.objects.filter(pk=attempt_id, response__isnull=True).update(
                http_status=evidence["http_status"], response=evidence["response"]
            )


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
    from . import adapter_client as client

    raw_attempts = evidence.get("attempts")
    if not isinstance(raw_attempts, list):
        raise client.AdapterError(
            "Adapter returned an invalid Apply attempt collection.",
            code="invalid_response",
        )
    requested = set(attempt_ids)
    for raw in raw_attempts:
        if not isinstance(raw, dict) or "apply_attempt_id" not in raw:
            raise client.AdapterError(
                "Adapter returned an invalid Apply attempt record.",
                code="invalid_response",
            )
        try:
            raw_id = UUID(str(raw["apply_attempt_id"]))
        except (AttributeError, TypeError, ValueError) as exc:
            raise client.AdapterError(
                "Adapter returned an invalid Apply attempt UUID.",
                code="invalid_response",
            ) from exc
        if raw_id not in requested:
            raise client.AdapterError(
                "Adapter returned an unrequested Apply attempt.",
                code="invalid_response",
            )


def _record_replay_answers(answers):
    restorations = []
    for attempt, result, error in answers:
        restoration = _record_replay_answer(attempt, result=result, error=error)
        if restoration is not None:
            restorations.append(restoration)
    return restorations


def _validated_unknown_attempt_ids(evidence, attempt_ids) -> set[UUID]:
    """Parse unknown attempt IDs and refuse IDs outside the request."""
    from . import adapter_client as client

    raw_unknown = evidence.get("unknown_apply_attempt_ids", [])
    if not isinstance(raw_unknown, list):
        raise client.AdapterError(
            "Adapter returned an invalid unknown attempt collection.",
            code="invalid_response",
        )
    try:
        unknown = {UUID(str(value)) for value in raw_unknown}
    except (AttributeError, TypeError, ValueError) as exc:
        raise client.AdapterError(
            "Adapter returned an invalid unknown attempt UUID.",
            code="invalid_response",
        ) from exc
    if unknown - set(attempt_ids):
        # An adapter answer we did not ask for; the caller's adapter boundary handles it.
        raise client.AdapterError(
            "Adapter returned an unrequested unknown Apply attempt.",
            code="invalid_response",
        )
    return unknown


def load_deployment_evidence(management, *, attempt_ids=None):
    """Fetch attempt evidence and recover unknown UUIDs by replaying their exact request."""
    from . import adapter_client as client
    from .models import NSOApplyAttempt

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
    from .models import NSOAcceptedSelection

    feed, incarnation = client.get_settlement_feed(management.adapter_device_id, after_settle_seq=0, limit=100)
    seen_jobs = set()
    last_sequence = _validate_feed_page(feed, management.adapter_device_id, 0, seen_jobs)
    page = feed
    while len(page) == 100:
        page, next_incarnation = client.get_settlement_feed(
            management.adapter_device_id, after_settle_seq=page[-1]["settle_seq"], limit=100
        )
        if next_incarnation != incarnation:
            raise EvidenceInvariantError("evidence_epoch_changed")
        last_sequence = _validate_feed_page(page, management.adapter_device_id, last_sequence, seen_jobs)
        feed.extend(page)
        if len(feed) > 10000:
            raise EvidenceInvariantError("settlement_feed_not_bounded")
    evidence = client.get_deployment_evidence(management.adapter_device_id, attempt_ids)
    _reject_unrequested_attempts(evidence, attempt_ids)
    unknown = _validated_unknown_attempt_ids(evidence, attempt_ids)
    replayed = False
    replay_answers = []
    for attempt in NSOApplyAttempt.objects.filter(
        pk__in=unknown,
        management=management,
        adapter_device_id=management.adapter_device_id,
        trigger_kind="manual",
        adapter_apply_attempt_id__isnull=False,
    ):
        # A retained overlay reference or explicit selection cannot reopen a refusal.
        if attempt.response is not None and attempt.http_status not in (200, 202):
            continue
        if (
            attempt.overlay_snapshots.filter(selection__binding_state="invalidated").exists()
            or attempt.overlay_snapshots.filter(selection__store_incarnation__isnull=False)
            .exclude(selection__store_incarnation=incarnation)
            .exists()
        ):
            continue
        try:
            result = client.trigger_apply(management.adapter_device_id, attempt.pk, attempt.selected)
        except client.AdapterError as exc:
            if exc.status_code == 409 and exc.code == "conflict":
                job_id = exc.detail.get("job_id") if isinstance(exc.detail, dict) else None
                logger.info(
                    "Apply replay for adapter device %s attempt %s is waiting for adapter job %s",
                    management.adapter_device_id,
                    attempt.pk,
                    job_id,
                )
            else:
                replay_answers.append((attempt, None, exc))
        else:
            replay_answers.append((attempt, result, None))
        replayed = True
    if replayed:
        evidence = client.get_deployment_evidence(management.adapter_device_id, attempt_ids)
        _reject_unrequested_attempts(evidence, attempt_ids)
        _validated_unknown_attempt_ids(evidence, attempt_ids)
    generations = client.list_device_generations(management.adapter_device_id)
    unacknowledged, receipt_document = _load_pending_receipts(management, evidence, generations)
    _last_page, final_incarnation = client.get_settlement_feed(
        management.adapter_device_id, after_settle_seq=0, limit=1
    )
    _validate_feed_page(_last_page, management.adapter_device_id, 0, set())
    management.refresh_from_db()
    if incarnation != final_incarnation or mapping_epoch(management) != epoch:
        raise EvidenceInvariantError("evidence_epoch_changed")
    bound = _bound_evidence(evidence, feed, generations, incarnation, management=management)
    bound.receipt_recoveries = _prepare_receipt_recoveries(
        management, bound, unacknowledged, receipt_document, incarnation
    )
    from .intent_state import MutationFootprint, mirror_transaction

    with mirror_transaction(MutationFootprint(device_ids=(management.device_id,))):
        management.refresh_from_db()
        if mapping_epoch(management) != epoch:
            raise EvidenceInvariantError("evidence_epoch_changed")
        NSOAcceptedSelection.objects.filter(management=management, binding_state="bound").exclude(
            store_incarnation=incarnation
        ).update(binding_state="invalidated")
        bound.replay_restorations = _record_replay_answers(replay_answers)
    return bound


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
    from .adapter_client import get_intent_receipts
    from .selection_receipts import pending_selections

    selections = pending_selections(management, evidence, generations)
    document = get_intent_receipts(device_id=management.adapter_device_id) if selections else None
    return selections, document


def settle_device_apply_attempts(management):
    """Fetch one evidence snapshot and settle this device's currently referenced attempts."""
    evidence = load_deployment_evidence(management)
    if evidence is not None:
        settle_apply_attempts(
            management,
            evidence,
        )
    return evidence


def latest_route_policy_carrier(deployment_evidence, *, attempt_ids=None):
    """Return one exact validated carrier without inferring order from job IDs."""
    candidates = []
    allowed = None if attempt_ids is None else set(attempt_ids)
    if not isinstance(deployment_evidence, dict):
        return None
    for attempt in deployment_evidence["attempts"]:
        if allowed is not None and UUID(str(attempt["apply_attempt_id"])) not in allowed:
            continue
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
    if not candidates:
        return None
    sequences = getattr(deployment_evidence, "settlement_sequences", {})
    if len(candidates) > 1 and any(row["carrier_job_id"] not in sequences for row in candidates):
        return None
    generation = max(candidates, key=lambda row: sequences.get(row["carrier_job_id"], 0))
    return {
        "id": generation["carrier_job_id"],
        "store_incarnation": getattr(deployment_evidence, "store_incarnation", None),
        "generation_id": generation["generation_id"],
        "status": generation["carrier_job_status"],
        "result": generation["carrier_job_result"],
        "error": generation["carrier_job_error"],
        "updated_at": generation["updated_at"],
    }


class _BoundEvidence(dict):
    """The validated HTTP body with its separately fetched store epoch."""


def _bound_evidence(evidence, jobs, generations, incarnation, *, management):
    """Join jobs to authoritative generation sequences, including automatic heads."""
    bound = _BoundEvidence(evidence)
    bound.store_incarnation = incarnation
    bound.settlement_sequences = {job["id"]: job["settle_seq"] for job in jobs}
    bound.automatic_generations = []
    bound.generation_modes = {row["generation_id"]: row["mode"] for row in generations}
    bound.manual_attempt_by_generation = {}
    by_id = {row["generation_id"]: row for row in generations}
    head = evidence["head"]
    if head is not None and head["row_outcomes"] is not None:
        _append_head_evidence(bound, head, by_id, management, incarnation)
    for job in jobs:
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
            generation["generation_id"] for generation in generations if generation["job_id"] == job["id"]
        }
        for generation_id in identifiers:
            bound.automatic_generations.append(
                _authoritative_carrier_generation(
                    by_id.get(generation_id), generation_id, carrier, management, incarnation
                )
            )
    _require_terminal_metadata(bound, generations, management, incarnation)
    return bound


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
    identifiers = _carrier_generation_ids(head["row_outcomes"]) | {head["generation_id"]}
    for generation_id in identifiers:
        bound.automatic_generations.append(
            _authoritative_carrier_generation(by_id.get(generation_id), generation_id, head, management, incarnation)
        )
        if local is not None and generation_id in manual_generations:
            bound.manual_attempt_by_generation[(generation_id, head["carrier_job_id"])] = local


def _authoritative_carrier_generation(generation, generation_id, carrier, management, incarnation):
    from .durable_capture import mapping_epoch
    from .models import NSOApplyExecution
    from .outcome_settlement import _record_invariant

    if generation is not None and generation["job_id"] == carrier["carrier_job_id"]:
        return _generation_from_carrier(generation, carrier)
    cached = NSOApplyExecution.objects.filter(
        management=management,
        mapping_epoch=mapping_epoch(management),
        store_incarnation=incarnation,
        generation_id=generation_id,
        carrier_job_id=carrier["carrier_job_id"],
        terminal_projection__isnull=False,
    ).first()
    if cached is not None:
        return cached.terminal_projection
    _record_invariant(
        management,
        {"generation_id": generation_id, "carrier_job_id": carrier["carrier_job_id"]},
        incarnation,
        None,
        "missing_generation_evidence",
    )
    raise EvidenceInvariantError("missing_generation_evidence")


def _require_terminal_metadata(bound, generations, management, incarnation):
    """Block historical carrier gaps before a later execution can consume its cursor."""
    from .durable_capture import mapping_epoch
    from .models import NSOApplyExecution
    from .outcome_settlement import _record_invariant

    present = {
        (generation["generation_id"], generation["carrier_job_id"]) for generation in bound.automatic_generations
    }
    present.update(
        (generation["generation_id"], generation["carrier_job_id"])
        for attempt in bound["attempts"]
        for generation in attempt["generations"]
        if generation["row_outcomes"] is not None
    )
    for generation in generations:
        identity = generation["generation_id"], generation["job_id"]
        if generation["status"] in {"pending", "running"} or identity in present:
            continue
        cached = NSOApplyExecution.objects.filter(
            management=management,
            mapping_epoch=mapping_epoch(management),
            store_incarnation=incarnation,
            generation_id=generation["generation_id"],
            carrier_job_id=generation["job_id"],
            consumption_state__in=("consumed", "superseded"),
            terminal_projection__isnull=False,
        ).first()
        if cached is not None:
            projection = cached.terminal_projection
            if all(projection[key] == generation[key] for key in ("seq", "stream_revisions", "source_push_seq")):
                bound.automatic_generations.append(projection)
                continue
        _record_invariant(
            management,
            {"generation_id": generation["generation_id"], "carrier_job_id": generation["job_id"]},
            incarnation,
            None,
            "missing_generation_evidence",
        )
        raise EvidenceInvariantError("missing_generation_evidence")


def _generation_from_carrier(generation, carrier):
    from .refusal_registry import document_section_for_stream

    return {
        "generation_id": generation["generation_id"],
        "seq": generation["seq"],
        "status": generation["status"],
        "sections": sorted({document_section_for_stream(stream) for stream in generation["stream_revisions"]}),
        "stream_revisions": generation["stream_revisions"],
        "source_push_seq": generation["source_push_seq"],
        **{
            key: carrier[key]
            for key in (
                "carrier_job_id",
                "carrier_job_status",
                "carrier_job_result",
                "carrier_job_error",
                "row_outcomes",
                "refusals",
            )
        },
        "updated_at": generation["updated_at"],
    }


def _validate_feed_page(jobs, adapter_device_id, after, seen):
    """Require ordered terminal carriers before any local settlement mutation."""
    for job in jobs:
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
