# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Bounded retention of terminal Apply history with no remaining authority."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import ProtectedError, Q, RestrictedError
from django.utils import timezone

from . import adapter_client, refusal_contract
from .deployment import guarded
from .durable_capture import mapping_epoch
from .models import NSOAcceptedSelection, NSOApplyAttempt, NSODeviceManagement, NSOIntentOutboxState
from .refusal_registry import OVERLAYS

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
DEFAULT_BATCH_SIZE = 100
#: Adapter error codes that make every device unreadable, so the batch stops instead of waiting per device.
ADAPTER_DOWN_CODES = frozenset({"nso_unreachable", "nso_timeout"})


@dataclass(frozen=True)
class PruneResult:
    examined: int
    eligible: int
    deleted: int
    next_cursor: str | None
    adapter_unavailable: bool = False


class _AdapterUnavailable(Exception):
    """The adapter cannot answer for any device."""


def _valid_execution_evidence(execution, attempt):
    from .apply_settlement import _parse_time

    try:
        generation = refusal_contract.validate_generation(execution.terminal_projection)
        return (
            generation["generation_id"] == execution.generation_id
            and generation["seq"] == execution.generation_seq
            and generation["carrier_job_id"] == execution.carrier_job_id
            and execution.terminal_at == _parse_time(generation["updated_at"])
            and execution.commit_acknowledged == generation["carrier_job_result"]["commit_acknowledged"]
            and execution.source_selectors
            == {"source_push_seq": generation["source_push_seq"], "stream_revisions": generation["stream_revisions"]}
            and attempt.selected
            == {
                stream: source if source is not None else generation["stream_revisions"][stream]
                for stream, source in generation["source_push_seq"].items()
            }
        )
    except (refusal_contract.ContractError, TypeError, KeyError):
        return False


def _referenced(attempt):
    if attempt.referenced_prior_snapshots.exclude(attempt=attempt).exists():
        return True
    if attempt.referenced_expected_snapshots.exclude(attempt=attempt).exists():
        return True
    if attempt.active_settlement_fences.exists() or attempt.deployment_verifications.exists():
        return True
    return any(spec.model.objects.filter(apply_attempt_id=attempt.pk).exists() for spec in OVERLAYS.values())


def _eligible(attempt, management):
    if attempt.trigger_kind == "manual" and (attempt.response is None or attempt.http_status is None):
        return False
    if _referenced(attempt):
        return False
    executions = list(attempt.executions.all())
    if not executions:
        return False
    for execution in executions:
        if (
            execution.consumption_state not in {"consumed", "superseded"}
            or execution.invariant_code
            or execution.consumed_at is None
            or execution.settle_seq is None
            or execution.settlement_fences.exists()
            or execution.mapping_epoch != mapping_epoch(management)
            or execution.adapter_device_id != management.adapter_device_id
            or management.settle_cursor_device_id != execution.adapter_device_id
            or management.settle_cursor_incarnation != execution.store_incarnation
            or (management.settle_cursor_seq or 0) <= execution.settle_seq
        ):
            return False
        if not _valid_execution_evidence(execution, attempt):
            return False
    if attempt.trigger_kind == "manual":
        from .apply_settlement import EvidenceInvariantError, _validate_attempt

        try:
            _validate_attempt(
                {
                    "apply_attempt_id": str(attempt.pk),
                    "admission_state": "admitted",
                    "http_status": attempt.http_status,
                    "response": attempt.response,
                    "generations": [execution.terminal_projection for execution in executions],
                },
                attempt,
            )
        except EvidenceInvariantError:
            return False
    snapshots = list(attempt.overlay_snapshots.select_related("selection"))
    for snapshot in snapshots:
        if snapshot.settlement_disposition not in {"in_sync", "not_applied", "refused", "superseded"}:
            return False
        if snapshot.selection_id is not None:
            selection = snapshot.selection
            if (
                selection.binding_state != "invalidated"
                or selection.publication_state != "sent"
                or selection.acknowledged_at is None
            ):
                return False
    return not NSOIntentOutboxState.objects.filter(device_id=management.device_id, push_seq__isnull=False).exists()


@dataclass(frozen=True)
class _RemoteGenerations:
    adapter_device_id: int
    store_incarnation: str
    generations: list


def _remote_generations(management_id):
    """Read one stable generation listing per device, without a row lock; None retains its history."""
    device_id = NSODeviceManagement.objects.filter(pk=management_id).values_list("adapter_device_id", flat=True).first()
    if device_id is None:
        return None
    try:
        _page, incarnation = adapter_client.get_settlement_feed(device_id, after_settle_seq=0, limit=1)
        generations = adapter_client.list_device_generations(device_id)
        _page, final_incarnation = adapter_client.get_settlement_feed(device_id, after_settle_seq=0, limit=1)
    except adapter_client.AdapterError as exc:
        if exc.code in ADAPTER_DOWN_CODES:
            raise _AdapterUnavailable from exc
        logger.warning(
            "Apply ledger retention keeps history for management %s: the adapter is unavailable", management_id
        )
        return None
    if incarnation != final_incarnation:
        return None
    return _RemoteGenerations(device_id, incarnation, generations)


def _historical_certificate_required(attempt, management, remote):
    from .apply_settlement import generation_certificates

    if attempt.executions.exclude(store_incarnation=remote.store_incarnation).exists():
        return True
    return (
        generation_certificates(management, remote.store_incarnation, *remote.generations)
        .filter(attempt=attempt)
        .exists()
    )


@guarded("Apply ledger retention")
def prune_apply_ledger(*, execute=False, batch_size=DEFAULT_BATCH_SIZE, cursor=None):
    """Examine at most one batch and retain all unresolved or referenced records."""
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ValueError("batch size must be between 1 and 10000")
    cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)
    query = NSOApplyAttempt.objects.filter(lifecycle="terminal", completed_at__lt=cutoff)
    if cursor is not None:
        stamp, attempt_id = cursor.split("|", 1)
        stamp, attempt_id = datetime.fromisoformat(stamp), UUID(attempt_id)
        if timezone.is_naive(stamp):
            raise ValueError("The retention cursor must have a timezone")
        query = query.filter(Q(completed_at__gt=stamp) | Q(completed_at=stamp, pk__gt=attempt_id))
    candidates = list(
        query.order_by("completed_at", "pk").values_list(
            "pk",
            "management_id",
            "completed_at",
        )[:batch_size]
    )
    eligible = deleted = 0
    remote_by_management = {}
    examined = len(candidates)
    for index, (attempt_id, management_id, _completed_at) in enumerate(candidates):
        if management_id not in remote_by_management:
            try:
                remote_by_management[management_id] = _remote_generations(management_id)
            except _AdapterUnavailable:
                # The shared deployment lock is held: waiting out the timeout per device would block quiesce().
                logger.warning("Apply ledger retention stops: the adapter is unavailable")
                examined = index
                break
        remote = remote_by_management[management_id]
        if remote is None:
            continue
        with transaction.atomic():
            management = NSODeviceManagement.objects.select_for_update().get(pk=management_id)
            if management.adapter_device_id != remote.adapter_device_id:
                continue
            attempt = (
                NSOApplyAttempt.objects.select_for_update()
                .filter(
                    pk=attempt_id,
                    lifecycle="terminal",
                    completed_at__lt=cutoff,
                )
                .first()
            )
            if attempt is None or not _eligible(attempt, management):
                continue
            if _historical_certificate_required(attempt, management, remote):
                continue
            eligible += 1
            if not execute:
                continue
            selection_ids = list(attempt.overlay_snapshots.values_list("selection_id", flat=True))
            try:
                with transaction.atomic():
                    attempt.delete()
            except (ProtectedError, RestrictedError):
                continue
            deleted += 1
            NSOAcceptedSelection.objects.filter(
                pk__in=selection_ids,
                binding_state="invalidated",
                overlay_snapshots__isnull=True,
            ).delete()
    if examined < len(candidates):
        last = candidates[examined - 1] if examined else None
        next_cursor = f"{last[2].isoformat()}|{last[0]}" if last is not None else cursor
        return PruneResult(examined, eligible, deleted, next_cursor, adapter_unavailable=True)
    next_cursor = f"{candidates[-1][2].isoformat()}|{candidates[-1][0]}" if len(candidates) == batch_size else None
    return PruneResult(len(candidates), eligible, deleted, next_cursor)
