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
from .settlement_evidence import request_fence, write_diagnostic

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
    """Commit complete carriers in order and retain progress before a stalled item."""
    from .apply_settlement import (
        EvidenceInvariantError,
    )
    from .models import NSODeviceManagement

    pk = getattr(mgmt, "pk", mgmt)
    management = NSODeviceManagement.objects.get(pk=pk)
    # Only a pass that saw the resume request before its reads may clear it.
    resume = management.settle_resume_pending
    fence = request_fence(management)
    acknowledged = _acknowledgement_count(pk)
    epoch = fence["mapping_epoch"]
    read = None
    consumed = 0
    epoch_reset = False
    try:
        read = _read_device_evidence(management)
        with transaction.atomic():
            row = NSODeviceManagement.objects.select_for_update().get(pk=pk)
            fence = request_fence(row)
            if row.adapter_device_id is None:
                return ConsumeResult(None, 0, False, False, row.settle_cursor_seq)
            jobs, cursor, incarnation, epoch_reset = _read_locked_page(row)
        read = _align_carrier_read(read, row, jobs, incarnation)
        historical = _historical_carriers(read, row) if read is not None else []
        work = [(carrier, None) for carrier in historical] + [(job["id"], job) for job in jobs]
        if not work:
            work = [(None, None)]
        stalled = False
        expected_fence = fence
        seen = set()
        for carrier_id, job in work:
            management = NSODeviceManagement.objects.get(pk=pk)
            fence = request_fence(management)
            if fence != expected_fence:
                raise _SettlementReadChanged
            cursor, stalled = _commit_carrier(
                management,
                read,
                carrier_id,
                job,
                cursor,
                incarnation,
                fence,
                persist_cursor=job is not None or not jobs,
                seen=seen,
            )
            expected_fence = request_fence(management)
            if stalled:
                break
            if job is not None:
                consumed += 1
        drained = not stalled and len(jobs) < SETTLE_FEED_PAGE
        result = ConsumeResult(row.adapter_device_id, consumed, stalled, epoch_reset, cursor, drained)
        if read is not None and drained:
            management.refresh_from_db()
            fence = request_fence(management)
            result = _complete_evidence_result(result, read, management, fence, expected_fence, epoch)
        if drained and resume:
            _clear_resume_request(pk, expected_fence)
        return result
    except _SettlementReadChanged:
        management.refresh_from_db()
        return ConsumeResult(management.adapter_device_id, consumed, True, epoch_reset, management.settle_cursor_seq)
    except adapter_client.InvalidJobEvidence as exc:
        stall = _invalid_evidence_stall(management, fence, acknowledged, exc, "invalid_evidence")
    except EvidenceInvariantError as exc:
        logger.warning("Settlement feed remains unresolved for management %s: %s", pk, exc)
        stall = _invariant_evidence_stall(management, fence, acknowledged, exc)
    _publish_stall_issue(pk)
    return replace(stall, consumed=consumed, epoch_reset=epoch_reset)


def _clear_resume_request(pk, fence):
    """Clear the resume request after a drained pass, unless the cursor moved after the walk."""
    from .intent_state import mirror_refresh
    from .models import NSODeviceManagement
    from .settlement_evidence import matches_request_fence
    from .signals import suppress_intent_push

    with transaction.atomic():
        row = NSODeviceManagement.objects.select_for_update().get(pk=pk)
        if not row.settle_resume_pending or not matches_request_fence(row, fence):
            return
        with suppress_intent_push(), mirror_refresh(row, ("settle_resume_pending",)) as locked:
            locked.settle_resume_pending = False
            locked.save(update_fields=("settle_resume_pending",))


def request_settlement_pass(device_id):
    """Enqueue the normal settlement pass; the durable resume request and the sweep cover a lost enqueue."""
    from .reconcile import enqueue_device_reconcile

    try:
        enqueue_device_reconcile(device_id)
    except Exception:  # noqa: BLE001 - the resume request is committed, and the sweep is the retry clock
        logger.exception("Could not enqueue the settlement pass for device %s", device_id)


def _publish_stall_issue(pk):
    """Publish the identified stall from pure reads; the diagnostic stays when this fails."""
    from .settlement_issue import publish_stall_issue

    try:
        publish_stall_issue(pk)
    except Exception:  # noqa: BLE001 - advisory; the next pass publishes from the kept diagnostic
        logger.exception("Settlement issue publication failed for management %s", pk)


def _align_carrier_read(read, row, jobs, incarnation):
    """Bind jobs that became terminal during the evidence read before writing owners."""
    from .apply_settlement import _Rows
    from .durable_capture import mapping_epoch

    if read is None or mapping_epoch(row) != read.mapping_epoch or incarnation != read.store_incarnation:
        return None
    known = {job["id"] for job in read.feed_jobs}
    read.feed_jobs = _Rows([*read.feed_jobs, *(job for job in jobs if job["id"] not in known)])
    return read


def _complete_evidence_result(result, read, management, fence, expected_fence, epoch):
    """Resolve carrierless admissions only after the ordered walk has drained."""
    from .apply_settlement import (
        _settlement_sequences,
        carrierless_evidence_read,
        identify_evidence_stall,
        prepare_carrier_evidence,
    )
    from .durable_capture import mapping_epoch

    if fence != expected_fence:
        raise _SettlementReadChanged
    admissions = carrierless_evidence_read(read)
    if admissions["attempts"] or admissions["unknown_apply_attempt_ids"]:
        _commit_carrier(
            management,
            admissions,
            None,
            None,
            result.cursor,
            read.store_incarnation,
            fence,
            persist_cursor=False,
            seen=set(),
        )
    with identify_evidence_stall(
        {"device_id": management.adapter_device_id},
        read.store_incarnation,
        sequences=_settlement_sequences(read.feed_jobs),
    ):
        evidence = prepare_carrier_evidence(management, read)
    if mapping_epoch(management) == epoch:
        return replace(result, deployment_evidence=evidence, mapping_epoch=epoch)
    return result


def _read_device_evidence(management):
    """Retain the fail-closed cursor walk when the store changes or the backlog is unbounded."""
    from .apply_settlement import EvidenceInvariantError, load_deployment_evidence

    if management.adapter_device_id is None:
        return None
    fence = request_fence(management)
    try:
        return load_deployment_evidence(management, defer_carriers=True)
    except EvidenceInvariantError as exc:
        logger.warning("Settlement evidence remains unresolved for management %s", management.pk)
        if str(exc) not in {"settlement_feed_not_bounded", "evidence_epoch_changed"}:
            raise
        if str(exc) == "evidence_epoch_changed" and request_fence(management) != fence:
            raise _SettlementReadChanged from exc
        return None


class _SettlementReadChanged(Exception):
    """A concurrent consumer changed the cursor after this page was fetched."""


def _commit_carrier(management, read, carrier_id, job, cursor, incarnation, fence, *, persist_cursor, seen):
    """Commit one complete carrier after its receipt claims and owner footprint are locked."""
    from .apply_settlement import (
        _rows,
        _validate_feed_page,
        identify_evidence_stall,
        prepare_carrier_evidence,
        settle_apply_attempts,
        validate_carrier_under_lock,
    )
    from .outcome_settlement import settle_terminal_generations
    from .settlement_evidence import matches_request_fence
    from .settlement_resolution import acknowledged_jobs

    identity = job or {"id": carrier_id, "device_id": management.adapter_device_id}
    stalled = False
    sequences = _rows(read.feed_jobs).sequences if read is not None else {}
    released = job is not None and job["id"] in acknowledged_jobs(management, incarnation)
    with identify_evidence_stall(identity, incarnation, sequences=sequences):
        if released:
            _validate_released_job(management, job, cursor, seen, incarnation)
        elif job is not None:
            _validate_feed_page([job], management.adapter_device_id, cursor, seen, incarnation)
        evidence = prepare_carrier_evidence(management, read, carrier_id) if read is not None and not released else None

        def before_settle():
            management.refresh_from_db()
            if not matches_request_fence(management, fence):
                raise _SettlementReadChanged
            if not released:
                _validate_locked_carrier(read, job)
            if evidence is not None:
                validate_carrier_under_lock(management, evidence)

        def finish():
            nonlocal cursor, stalled
            management.refresh_from_db()
            if job is not None:
                if released or _settle_job(management, job, incarnation):
                    cursor = job["settle_seq"]
                    _clear_stall(management)
                else:
                    _record_stall(management, job["settle_seq"])
                    stalled = True
            if persist_cursor:
                _persist(management, cursor, management.adapter_device_id, incarnation)
                management.refresh_from_db()
                write_diagnostic(management, fence, cursor_written=True)

        if evidence is not None:
            settle_apply_attempts(management, evidence, before_settle=before_settle, after_settle=finish)
        else:
            settle_terminal_generations(management, [], incarnation, before_settle=before_settle, after_settle=finish)
    return cursor, stalled


def _validate_released_job(management, job, cursor, seen, incarnation):
    """Walk past an acknowledged job by its trusted identity; its evidence stays unreadable."""
    from .apply_settlement import EvidenceInvariantError
    from .models import NSOSettlementIssue

    sequence = job["settle_seq"]
    if (
        type(sequence) is not int
        or sequence <= cursor
        or job["device_id"] != management.adapter_device_id
        or job["id"] in seen
        or not NSOSettlementIssue.objects.filter(
            management=management,
            adapter_device_id=management.adapter_device_id,
            store_incarnation=incarnation,
            job_id=job["id"],
            settle_seq=sequence,
            state="acknowledged",
        ).exists()
    ):
        raise EvidenceInvariantError("invalid_settlement_order")
    seen.add(job["id"])


def _validate_locked_carrier(read, job):
    """Require the locked feed to retain the carrier facts used for owner settlement."""
    from .apply_settlement import EvidenceInvariantError, _rows

    if read is None or job is None:
        return
    retained = next(iter(_rows(read.feed_jobs).matching(id=[job["id"]])), None)
    adapter_client.raise_item_error(retained)
    adapter_client.raise_item_error(job)
    if retained is not None and any(
        retained[key] != job[key]
        for key in ("id", "device_id", "type", "status", "settle_seq", "result", "error", "row_outcomes", "refusals")
    ):
        raise EvidenceInvariantError("consumed_execution_changed")


def _historical_carriers(read, management):
    """Require durable positive order before treating absent feed work as history."""
    from .apply_settlement import _attempt_generations, _cursor_epoch_executions

    feed_ids = {job["id"] for job in read.feed_jobs}
    rows = list(read.generation_listing)
    rows.extend(row for attempt in read["attempts"] for row in _attempt_generations(attempt))
    if read["head"] is not None:
        rows.append(read["head"])
    candidates = {row["carrier_job_id"] for row in rows if row["carrier_job_id"] not in feed_ids}
    if (
        management.settle_cursor_incarnation != read.store_incarnation
        or management.settle_cursor_device_id != management.adapter_device_id
    ):
        return []
    return list(
        dict.fromkeys(
            _cursor_epoch_executions(management, read.store_incarnation)
            .filter(
                carrier_job_id__in=candidates,
                invariant_code="",
                consumed_at__isnull=False,
                settle_seq__gt=0,
                settle_seq__lte=management.settle_cursor_seq or 0,
            )
            .order_by("settle_seq")
            .values_list("carrier_job_id", flat=True)
        )
    )


def _invariant_evidence_stall(management, fence, acknowledged, error):
    """Persist the invariant through its request fence."""
    from .settlement_evidence import invariant_code

    invalid_job = getattr(error, "invalid_job", adapter_client.InvalidJobEvidence({}, "invalid settlement evidence"))
    return _invalid_evidence_stall(
        management, fence, acknowledged, invalid_job, "evidence_invariant", invariant_code(error)
    )


def _acknowledgement_count(pk):
    from .models import NSOSettlementIssue

    return NSOSettlementIssue.objects.filter(management_id=pk, state="acknowledged").count()


def _invalid_evidence_stall(management, fence, acknowledged, error, stall_class, code=None):
    """Retain invalid evidence without advancing its settlement cursor.

    A pass that read its evidence before an acknowledgement committed writes nothing: a historical
    acknowledgement keeps the request fence, and the acknowledgement already requested a new pass.
    """
    from .models import NSODeviceManagement

    with transaction.atomic():
        row = NSODeviceManagement.objects.select_for_update().get(pk=management.pk)
        if _acknowledgement_count(row.pk) == acknowledged:
            write_diagnostic(row, fence, error, stall_class=stall_class, code=code)
        return ConsumeResult(row.adapter_device_id, 0, True, False, row.settle_cursor_seq, False)


def settle_static_routes(mgmt) -> ConsumeResult:
    """Consume the shared logical outcomes and the static-route success proof."""
    return consume_static_route_settlements(mgmt)


def sweep_static_route_settlements() -> tuple[int, int]:
    """Poll devices with pending owned work. Return ``(polled, failed)``.

    This independent clock can recover when the adapter cannot deliver callbacks.
    Query candidates after link repair so each poll uses the current mapping.
    Include every registered delivery scope and every sent selection that cadence or retry can still execute.
    Include every device with a resume request from an acknowledgement.
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
    candidate_pks.update(
        NSODeviceManagement.objects.filter(adapter_device_id__isnull=False, settle_resume_pending=True).values_list(
            "pk", flat=True
        )
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


def _read_locked_page(row):
    """Read one page from the current locked cursor without settling its carriers."""
    device_id = row.adapter_device_id
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

    return jobs, cursor, incarnation, epoch_reset


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
    from .apply_settlement import _cursor_epoch_executions
    from .outcome_settlement import _bind_consumed_sequences

    # A certificate from an earlier mapping epoch is still this cursor's history.
    executions = list(_cursor_epoch_executions(row, incarnation).filter(carrier_job_id=job["id"]))
    matching = [
        execution
        for execution in executions
        if all(
            execution.terminal_projection[projection_key] == job[job_key]
            for projection_key, job_key in (
                ("carrier_job_result", "result"),
                ("carrier_job_status", "status"),
                ("carrier_job_error", "error"),
                ("row_outcomes", "row_outcomes"),
                ("refusals", "refusals"),
            )
        )
    ]
    if executions and not matching:
        from .apply_settlement import EvidenceInvariantError

        raise EvidenceInvariantError("consumed_execution_changed")
    if job["type"] not in {"apply", "removal"}:
        return True
    result = job["result"]
    if not isinstance(result, dict) or "row_outcomes" not in result or not matching:
        return False
    _bind_consumed_sequences(matching, job["settle_seq"])
    return True
