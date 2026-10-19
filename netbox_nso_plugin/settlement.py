# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Consume ordered executions through exact logical outcomes and durable local evidence.

The shared settlement runtime owns lifecycle writes. This module advances the device
cursor only after that runtime records the complete execution. Static-route success
also requires the route generation and fingerprint echoed by the adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from django.db import transaction
from django.utils import timezone

from . import adapter_client

logger = logging.getLogger(__name__)

# One feed page per pass. The cursor is durable, so a device with a backlog drains over
# consecutive passes instead of holding the management row locked for an unbounded walk.
SETTLE_FEED_PAGE = 100


@dataclass(frozen=True)
class ConsumeResult:
    """What one pass over one device's feed did."""

    adapter_device_id: int | None
    #: jobs whose results were correlated (or had nothing to correlate) and were walked past
    consumed: int
    #: the walk stopped on an unresolved head; the cursor did not move past it
    stalled: bool
    #: the epoch changed, so the cursor and the stall triple were reset
    epoch_reset: bool
    cursor: int | None
    #: the walk reached the END of the feed: nothing is stalled and no further page is owed.
    drained: bool = False
    #: Already-consumed evidence is reusable only for this exact current mapping.
    deployment_evidence: dict | None = None
    mapping_epoch: str | None = None


def consume_static_route_settlements(mgmt) -> ConsumeResult:
    """Settle exact row evidence before locking the durable feed cursor."""
    from .apply_settlement import EvidenceInvariantError, settle_device_apply_attempts
    from .durable_capture import mapping_epoch
    from .models import NSODeviceManagement

    pk = getattr(mgmt, "pk", mgmt)
    management = NSODeviceManagement.objects.get(pk=pk)
    epoch = mapping_epoch(management)
    evidence = None
    if management.adapter_device_id is not None:
        try:
            evidence = settle_device_apply_attempts(management)
        except EvidenceInvariantError as exc:
            logger.warning("Settlement evidence remains unresolved for management %s", pk)
            # A backlog too long to bind still drains page by page; _settle_job stays fail-closed.
            if str(exc) != "settlement_feed_not_bounded":
                return ConsumeResult(management.adapter_device_id, 0, True, False, management.settle_cursor_seq, False)
    with transaction.atomic():
        row = NSODeviceManagement.objects.select_for_update().get(pk=pk)
        result = _consume_locked(row)
        row.refresh_from_db()
        if (
            evidence is not None
            and mapping_epoch(row) == epoch
            and result.adapter_device_id == evidence["device_id"]
            and row.settle_cursor_incarnation == evidence.store_incarnation
        ):
            return replace(result, deployment_evidence=evidence, mapping_epoch=epoch)
        return result


def settle_static_routes(mgmt) -> ConsumeResult:
    """Consume the shared logical outcomes and the static-route success proof."""
    return consume_static_route_settlements(mgmt)


def sweep_static_route_settlements() -> tuple[int, int]:
    """Poll devices with pending owned work. Return ``(polled, failed)``.

    This independent clock can recover when the adapter cannot deliver callbacks.
    Query candidates after link repair so each poll uses the current mapping.
    Include every registered delivery scope and every sent selection that cadence or retry can still execute.
    Keep each device's failure separate so one outage does not stop the sweep.
    """
    from . import status_machine as sm
    from .apply_state import deploying_models
    from .models import NSODeviceManagement

    candidate_pks = set(
        NSODeviceManagement.objects.filter(
            adapter_device_id__isnull=False,
            static_route_states__status__in=(sm.ACCEPTED, sm.DEPLOYING),
        ).values_list("pk", flat=True)
    )
    for models in deploying_models().values():
        for model in models:
            candidate_pks.update(
                model.objects.filter(
                    status=sm.DEPLOYING,
                    management__adapter_device_id__isnull=False,
                ).values_list("management_id", flat=True)
            )
    candidate_pks.update(
        NSODeviceManagement.objects.filter(
            adapter_device_id__isnull=False,
            accepted_selections__publication_state="sent",
            accepted_selections__binding_state__in=("unbound", "bound"),
        ).values_list("pk", flat=True)
    )
    candidates = sorted(candidate_pks)
    polled = 0
    failed = 0
    for pk in candidates:
        try:
            settle_static_routes(pk)
        except Exception:  # noqa: BLE001 one device's failure must not stop the fleet sweep
            logger.exception("static-route settlement sweep failed for management row %s", pk)
            failed += 1
            continue
        polled += 1
    return polled, failed


def _consume_locked(row) -> ConsumeResult:
    """Consume for the already-locked management row *row*."""
    device_id = row.adapter_device_id
    if device_id is None:
        # Unlinked: there is no adapter device to poll and no epoch to record. The link
        # repair owns getting this row an id back.
        return ConsumeResult(None, 0, False, False, row.settle_cursor_seq)

    cursor = row.settle_cursor_seq or 0
    jobs, incarnation = adapter_client.get_settlement_feed(device_id, after_settle_seq=cursor, limit=SETTLE_FEED_PAGE)

    epoch_reset = incarnation != row.settle_cursor_incarnation or device_id != row.settle_cursor_device_id
    if epoch_reset:
        logger.info(
            "settlement epoch changed for management %s: (%s, %s) → (%s, %s); cursor %s reset",
            row.pk,
            row.settle_cursor_incarnation or "-",
            row.settle_cursor_device_id,
            incarnation,
            device_id,
            cursor,
        )
        _clear_stall(row)
        if cursor:
            # The page in hand was requested from a cursor that belongs to a store or a
            # device it is not about, so ask again from the start. Only the header of the
            # page actually consumed is recorded, so a store that changes again mid-pass
            # is recorded correctly. A cursor already at 0 asked for that page already.
            cursor = 0
            jobs, incarnation = adapter_client.get_settlement_feed(
                device_id, after_settle_seq=cursor, limit=SETTLE_FEED_PAGE
            )

    consumed = 0
    stalled = False
    for job in jobs:
        seq = job.get("settle_seq")
        if type(seq) is not int or seq <= cursor:
            from .apply_settlement import EvidenceInvariantError

            raise EvidenceInvariantError("invalid_settlement_order")
        if _settle_job(row, job, incarnation):
            cursor = max(cursor, seq)
            _clear_stall(row)
            consumed += 1
            continue

        _record_stall(row, seq)

        # Head-of-line: nothing behind an unresolved settlement may be consumed, or the
        # cursor would advance past a result still owed to an overlay.
        stalled = True
        break

    _persist(row, cursor, device_id, incarnation)
    # A full page can leave more executions pending. A short page proves the walk drained.
    drained = not stalled and len(jobs) < SETTLE_FEED_PAGE
    return ConsumeResult(device_id, consumed, stalled, epoch_reset, cursor, drained)


def _record_stall(row, seq) -> None:
    """Record the unresolved feed head without discarding its execution evidence."""
    if row.settle_stall_seq != seq:
        row.settle_stall_seq = seq
        row.settle_stall_attempts = 1
        row.settle_stall_first_seen_at = timezone.now()
    else:
        row.settle_stall_attempts += 1


def _clear_stall(row) -> None:
    """Drop the stall triple: it describes exactly one sequence and nothing else."""
    row.settle_stall_seq = None
    row.settle_stall_attempts = 0
    row.settle_stall_first_seen_at = None


def _persist(row, cursor: int, device_id: int, incarnation: str) -> None:
    """Write the cursor, the epoch and the stall triple in the consuming transaction.

    The cursor advance is bookkeeping, not an intent change.
    """
    from django.db import transaction

    from .intent_state import mirror_refresh
    from .signals import suppress_intent_push

    values = {
        "settle_cursor_seq": cursor,
        "settle_cursor_incarnation": incarnation,
        "settle_cursor_device_id": device_id,
        "settle_stall_seq": row.settle_stall_seq,
        "settle_stall_attempts": row.settle_stall_attempts,
        "settle_stall_first_seen_at": row.settle_stall_first_seen_at,
    }
    with transaction.atomic(), suppress_intent_push(), mirror_refresh(row, values) as locked:
        for field_name, value in values.items():
            setattr(locked, field_name, value)
        locked.save(update_fields=values)


def static_route_success_proven(snapshot, generation) -> bool:
    """Require the exact route generation and fingerprint echoed before this execution."""
    fences = snapshot.static_route_fences
    result = generation["carrier_job_result"]
    if not isinstance(fences, dict) or not isinstance(result, dict):
        return False
    expected = fences["expected_generation"]
    fingerprint = fences["expected_fingerprint"]
    if type(expected) is not int or expected <= 0 or expected != fences["intent_generation"] or not fingerprint:
        return False
    entries = result.get("static_route_results")
    if not isinstance(entries, list):
        return False
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("route_id") == fences["route_id"]]
    return (
        len(matches) == 1
        and type(matches[0].get("generation")) is int
        and matches[0]["generation"] == expected
        and matches[0].get("fingerprint") == fingerprint
        and matches[0].get("outcome") == "in_sync"
    )


def _settle_job(row, job, incarnation) -> bool:
    """Advance only after the exact remote execution has a durable local disposition."""
    from .durable_capture import mapping_epoch
    from .models import NSOApplyExecution
    from .outcome_settlement import _bind_consumed_sequences

    if job.get("type") not in {"apply", "removal"}:
        return True
    result = job.get("result")
    if not isinstance(result, dict) or "row_outcomes" not in result:
        return False
    executions = NSOApplyExecution.objects.filter(
        management=row,
        mapping_epoch=mapping_epoch(row),
        store_incarnation=incarnation,
        carrier_job_id=job["id"],
        consumption_state__in=("consumed", "superseded"),
    )
    matching = [
        execution
        for execution in executions
        if all(
            execution.terminal_projection[projection_key] == job[job_key]
            for projection_key, job_key in (
                ("carrier_job_result", "result"),
                ("carrier_job_status", "status"),
                ("carrier_job_error", "error"),
            )
        )
    ]
    if not matching:
        return False
    _bind_consumed_sequences(matching, job.get("settle_seq"))
    return True
