# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Settle Apply rows only from evidence addressed to their durable attempt."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from django.utils import timezone

logger = logging.getLogger(__name__)

GENERATION_STATUSES = frozenset({"pending", "running", "settled", "failed", "outcome_unknown", "abandoned"})
_ATTEMPT_FIELDS = frozenset({"apply_attempt_id", "admission_state", "http_status", "response", "generations"})
_GENERATION_FIELDS = frozenset(
    {
        "generation_id",
        "seq",
        "status",
        "sections",
        "source_push_seq",
        "carrier_job_id",
        "carrier_job_status",
        "carrier_job_result",
        "carrier_job_error",
        "updated_at",
    }
)


class EvidenceInvariantError(RuntimeError):
    """The adapter evidence cannot truthfully judge local Apply rows."""


def _generation_disposition(status: str) -> str:
    """Classify every status in the adapter's exhaustive OpenAPI enum."""
    match status:
        case "pending" | "running":
            return "waiting"
        case "settled":
            return "settled"
        case "failed" | "outcome_unknown":
            return "blocked"
        case "abandoned":
            return "abandoned"
        case _:
            raise EvidenceInvariantError(f"unknown generation status {status!r}")


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
    if not isinstance(raw, dict) or set(raw) != _GENERATION_FIELDS:
        raise EvidenceInvariantError("deployment evidence generation has an invalid shape")
    if not _positive_int(raw["generation_id"]) or not _positive_int(raw["seq"]):
        raise EvidenceInvariantError("deployment evidence generation has an invalid identity")
    _generation_disposition(raw["status"])
    if (
        not isinstance(raw["sections"], list)
        or any(type(section) is not str for section in raw["sections"])
        or not isinstance(raw["source_push_seq"], dict)
        or any(type(stream) is not str for stream in raw["source_push_seq"])
        or any(value is not None and not _positive_int(value) for value in raw["source_push_seq"].values())
    ):
        raise EvidenceInvariantError("deployment evidence generation has invalid stream provenance")
    _parse_time(raw["updated_at"])
    return raw


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


def _validate_attempt(raw, local) -> dict:
    if not isinstance(raw, dict) or set(raw) != _ATTEMPT_FIELDS:
        raise EvidenceInvariantError("deployment evidence attempt has an invalid shape")
    try:
        attempt_id = UUID(str(raw["apply_attempt_id"]))
    except (TypeError, ValueError, AttributeError):
        raise EvidenceInvariantError("deployment evidence has an invalid attempt UUID") from None
    if attempt_id != local.pk:
        raise EvidenceInvariantError("deployment evidence names the wrong local attempt")
    if raw["admission_state"] not in {"admitted", "rejected"}:
        raise EvidenceInvariantError("deployment evidence has an invalid admission state")
    if not isinstance(raw["http_status"], int) or not isinstance(raw["response"], dict):
        raise EvidenceInvariantError("deployment evidence has an invalid stored response")
    response = raw["response"]
    selected = response.get("selected")
    if raw["admission_state"] == "admitted":
        if not isinstance(selected, dict) or selected != local.selected:
            raise EvidenceInvariantError("deployment evidence changed the Apply selection")
        skipped = response.get("skipped")
        if response.get("outcome") not in {"promoted", "no_op"} or not isinstance(skipped, dict):
            raise EvidenceInvariantError("deployment evidence has an invalid admitted response")
    raw_generations = raw["generations"]
    if not isinstance(raw_generations, list):
        raise EvidenceInvariantError("deployment evidence generations are not a list")
    generations = [_validate_generation(generation) for generation in raw_generations]
    if len({generation["generation_id"] for generation in generations}) != len(generations):
        raise EvidenceInvariantError("deployment evidence repeats a generation identity")
    if _response_generation_ids(response) != {generation["generation_id"] for generation in generations}:
        raise EvidenceInvariantError("deployment evidence is incomplete for its stored response")
    if local.response is not None and (local.http_status != raw["http_status"] or local.response != response):
        raise EvidenceInvariantError("local and adapter Apply responses disagree")
    return {**raw, "id": attempt_id, "generations": generations}


def _attempt_disposition(attempt: dict, stream: str):
    response = attempt["response"]
    if attempt["admission_state"] == "rejected" or response.get("outcome") == "no_op":
        return "not_promoted", None
    if stream in response.get("skipped", {}):
        return "not_promoted", None
    selected_seq = response["selected"].get(stream)
    generations = [
        generation for generation in attempt["generations"] if generation["source_push_seq"].get(stream) == selected_seq
    ]
    if selected_seq is None or not generations:
        raise EvidenceInvariantError(f"attempt has no generation for selected stream {stream!r}")
    dispositions = {_generation_disposition(generation["status"]) for generation in generations}
    if "blocked" in dispositions:
        disposition = "blocked"
    elif "waiting" in dispositions:
        disposition = "waiting"
    elif "abandoned" in dispositions:
        disposition = "abandoned"
    elif dispositions == {"settled"}:
        disposition = "settled"
    else:
        raise EvidenceInvariantError("attempt generations have an invalid disposition combination")
    representative = next(
        generation for generation in generations if _generation_disposition(generation["status"]) == disposition
    )
    return disposition, representative


def _carrier_error(generation, scope: str) -> str:
    error = generation.get("carrier_job_error")
    detail = error.get("detail") if isinstance(error, dict) else None
    items = detail.get("items") if isinstance(detail, dict) else None
    messages = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("type") == scope and item.get("error"):
            message = str(item["error"]).strip()
            if message and message not in messages:
                messages.append(message)
    return "; ".join(messages)


def _failure_message(disposition, attempt_id, generation, scope):
    generation_id = generation["generation_id"]
    if disposition == "blocked":
        return (
            f"Apply attempt {attempt_id} is blocked at generation {generation_id} with status "
            f"{generation['status']}. Fix the cause, then retry generation {generation_id}. Abandon it only if "
            "the intended state is already present or no longer required."
        )
    if disposition == "abandoned":
        return (
            f"Generation {generation_id} for Apply attempt {attempt_id} was abandoned. The adapter did not prove "
            "that this intent was delivered. Run Apply again if the intent is still required."
        )
    carrier = _carrier_error(generation, scope)
    if carrier:
        return carrier
    return (
        f"Generation {generation_id} settled, but later device reads did not show this value. "
        "Check device and NED support, then run Apply again."
    )


def _load_attempts(management, deployment_evidence, rows_by_scope, required_attempt_ids=()):
    from .models import NSOApplyAttempt

    raw_attempts = deployment_evidence["attempts"]
    if not isinstance(raw_attempts, list) or not isinstance(deployment_evidence["unknown_apply_attempt_ids"], list):
        raise EvidenceInvariantError("deployment evidence attempt collections are invalid")
    attempt_ids = {row.apply_attempt_id for rows in rows_by_scope.values() for row in rows}
    attempt_ids.update(required_attempt_ids)
    local_attempts = NSOApplyAttempt.objects.in_bulk(attempt_ids)
    validated = {}
    for raw in raw_attempts:
        try:
            raw_id = UUID(str(raw.get("apply_attempt_id")))
        except (TypeError, ValueError, AttributeError):
            raise EvidenceInvariantError("deployment evidence has an invalid attempt UUID") from None
        local = local_attempts.get(raw_id)
        if local is None:
            continue
        if raw_id in validated:
            raise EvidenceInvariantError("deployment evidence repeats an Apply attempt")
        if local.adapter_device_id != management.adapter_device_id:
            continue
        validated[raw_id] = _validate_attempt(raw, local)

    unknown_ids = set()
    for value in deployment_evidence["unknown_apply_attempt_ids"]:
        try:
            unknown_ids.add(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise EvidenceInvariantError("deployment evidence has an invalid unknown attempt UUID") from None
    if unknown_ids & set(validated):
        raise EvidenceInvariantError("an Apply attempt is both known and unknown")
    return local_attempts, validated, unknown_ids


def _settlement_decisions(rows_by_scope, validated, unknown_ids, *, static_route_feed_drained):
    from . import delivery
    from .reconcile import _stuck_deploying_grace

    decisions = []
    now = timezone.now()
    grace = _stuck_deploying_grace()
    registry = delivery.delivery_keys()
    for scope, rows in rows_by_scope.items():
        stream = registry[scope].section
        for row in rows:
            if row.apply_attempt_id in unknown_ids:
                continue
            attempt = validated.get(row.apply_attempt_id)
            if attempt is None:
                continue
            disposition, generation = _attempt_disposition(attempt, stream)
            if disposition == "waiting":
                continue
            if disposition == "not_promoted":
                decisions.append((scope, row, "accepted", "", True))
                continue
            if disposition == "settled":
                if scope == "static_route" and not static_route_feed_drained:
                    continue
                if now - _parse_time(generation["updated_at"]) < grace:
                    continue
            decisions.append(
                (
                    scope,
                    row,
                    "apply_failed",
                    _failure_message(disposition, row.apply_attempt_id, generation, scope),
                    False,
                )
            )
    return decisions


def settle_apply_attempts(
    management,
    deployment_evidence,
    *,
    static_route_feed_drained: bool,
    required_attempt_ids=(),
) -> None:
    """Apply one validated evidence snapshot through UUID-fenced compare-and-set writes."""
    from django.db import transaction

    from .adapter_client import DEPLOYMENT_EVIDENCE_FIELDS
    from .apply_state import deploying_models
    from .intent_state import mirror_refresh
    from .models import NSOApplyAttempt
    from .signals import suppress_intent_push

    if not isinstance(deployment_evidence, dict) or set(deployment_evidence) != DEPLOYMENT_EVIDENCE_FIELDS:
        raise EvidenceInvariantError("deployment evidence has an invalid top-level shape")
    if deployment_evidence["device_id"] != management.adapter_device_id:
        raise EvidenceInvariantError("deployment evidence names another adapter device")
    rows_by_scope = {
        scope: list(model.objects.filter(management=management, status="deploying"))
        for scope, model in deploying_models().items()
    }
    local_attempts, validated, unknown_ids = _load_attempts(
        management,
        deployment_evidence,
        rows_by_scope,
        required_attempt_ids,
    )
    decisions = _settlement_decisions(
        rows_by_scope,
        validated,
        unknown_ids,
        static_route_feed_drained=static_route_feed_drained,
    )
    now = timezone.now()

    for scope, row, status, error, clear_attempt in decisions:
        model = deploying_models()[scope]
        fields = {
            "status": status,
            "last_apply_error": error,
            "last_updated": now,
        }
        if clear_attempt:
            fields["apply_attempt_id"] = None
        with transaction.atomic():
            current = (
                model.objects.select_for_update(of=("self",))
                .filter(
                    pk=row.pk,
                    status="deploying",
                    apply_attempt_id=row.apply_attempt_id,
                )
                .first()
            )
            if current is None:
                continue
            for field_name, value in fields.items():
                setattr(current, field_name, value)
            with suppress_intent_push(), mirror_refresh(current, fields) as locked:
                if locked is not None:
                    for field_name, value in fields.items():
                        setattr(locked, field_name, value)
                    locked.save(update_fields=fields)

    for attempt_id, evidence in validated.items():
        local = local_attempts[attempt_id]
        if local.response is None:
            NSOApplyAttempt.objects.filter(pk=attempt_id, response__isnull=True).update(
                http_status=evidence["http_status"],
                response=evidence["response"],
            )


def deploying_attempt_ids(management) -> tuple[UUID, ...]:
    """Return the distinct durable attempts still referenced by deploying rows."""
    from .apply_state import deploying_models
    from .models import NSOApplyAttempt

    referenced_ids = {
        attempt_id
        for model in deploying_models().values()
        for attempt_id in model.objects.filter(
            management=management,
            status="deploying",
            apply_attempt_id__isnull=False,
        ).values_list("apply_attempt_id", flat=True)
    }
    attempt_ids = NSOApplyAttempt.objects.filter(
        management=management,
        pk__in=referenced_ids,
    ).values_list("pk", flat=True)
    return tuple(sorted(attempt_ids, key=str))


def route_policy_deploying_attempt_ids(management) -> tuple[UUID, ...]:
    """Return attempts referenced by route-policy rows in the locked reconcile footprint."""
    from .models import NSOApplyAttempt, NSORoutePolicyState

    referenced_ids = NSORoutePolicyState.objects.filter(
        management=management,
        status="deploying",
        apply_attempt_id__isnull=False,
    ).values_list("apply_attempt_id", flat=True)
    attempt_ids = NSOApplyAttempt.objects.filter(
        management=management,
        pk__in=referenced_ids,
    ).values_list("pk", flat=True)
    return tuple(sorted(attempt_ids, key=str))


def _record_replay_answer(attempt, result=None, error=None) -> None:
    from .models import NSOApplyAttempt

    if isinstance(result, dict):
        status = 200 if result.get("outcome") == "no_op" else 202
        response = result
    elif error is not None and type(error.status_code) is int and isinstance(error.response, dict):
        status = error.status_code
        response = error.response
    else:
        return
    NSOApplyAttempt.objects.filter(pk=attempt.pk, response__isnull=True).update(
        http_status=status,
        response=response,
    )


def load_deployment_evidence(management, *, attempt_ids=None):
    """Fetch attempt evidence and recover unknown UUIDs by replaying their exact request."""
    from . import adapter_client as client
    from .models import NSOApplyAttempt

    if attempt_ids is None:
        attempt_ids = deploying_attempt_ids(management)
    if not attempt_ids:
        return None
    evidence = client.get_deployment_evidence(management.adapter_device_id, attempt_ids)
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
    replayed = False
    for attempt in NSOApplyAttempt.objects.filter(
        pk__in=unknown,
        management=management,
        adapter_device_id=management.adapter_device_id,
    ):
        try:
            result = client.trigger_apply(management.adapter_device_id, attempt.pk, attempt.selected)
        except client.AdapterError as exc:
            if exc.status_code == 409 and exc.code == "conflict":
                job_id = exc.detail.get("job_id") if isinstance(exc.detail, dict) else None
                logger.info(
                    "Apply replay for attempt %s is waiting for adapter job %s",
                    attempt.pk,
                    job_id,
                )
            else:
                _record_replay_answer(attempt, error=exc)
        else:
            _record_replay_answer(attempt, result=result)
        replayed = True
    if replayed:
        evidence = client.get_deployment_evidence(management.adapter_device_id, attempt_ids)
    return evidence


def settle_device_apply_attempts(management, *, static_route_feed_drained: bool):
    """Fetch one evidence snapshot and settle this device's currently referenced attempts."""
    evidence = load_deployment_evidence(management)
    if evidence is not None:
        settle_apply_attempts(
            management,
            evidence,
            static_route_feed_drained=static_route_feed_drained,
        )
    return evidence


def latest_route_policy_carrier(deployment_evidence, *, attempt_ids=None):
    """Return the newest exact carrier snapshot that reports route-policy outcomes."""
    candidates = []
    allowed_attempt_ids = None if attempt_ids is None else set(attempt_ids)
    if not isinstance(deployment_evidence, dict):
        return None
    attempts = deployment_evidence.get("attempts", [])
    if not isinstance(attempts, list):
        return None
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        try:
            attempt_id = UUID(str(attempt.get("apply_attempt_id")))
        except (TypeError, ValueError, AttributeError):
            continue
        if allowed_attempt_ids is not None and attempt_id not in allowed_attempt_ids:
            continue
        generations = attempt.get("generations", [])
        if not isinstance(generations, list):
            continue
        for generation in generations:
            if not isinstance(generation, dict) or not _positive_int(generation.get("seq")):
                continue
            result = generation.get("carrier_job_result")
            if not isinstance(result, dict) or not isinstance(result.get("route_policy_count_by_outcome"), dict):
                continue
            job_id = generation.get("carrier_job_id")
            if not _positive_int(job_id):
                continue
            candidates.append(
                (
                    generation.get("seq", 0),
                    {
                        "id": job_id,
                        "apply_attempt_id": str(attempt_id),
                        "status": generation.get("carrier_job_status"),
                        "result": result,
                        "error": generation.get("carrier_job_error"),
                        "updated_at": generation.get("updated_at"),
                    },
                )
            )
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None
