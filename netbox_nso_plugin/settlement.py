# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S — the durable settlement consumer: cursor epoch, feed walk, stall bound.

This module owns the *transport* of settlement, not its verdicts. It walks one device's
ordered settlement feed, decides for each terminal job whether that job's per-route
results can be correlated at all, advances a durable cursor over the ones that can, and
bounds the ones that cannot so a single unresolvable result never blocks a device forever.
Turning a correlated result into an overlay status is the next layer's job.

Three properties are load-bearing and each cost a review round to establish:

* **The epoch is ``(store incarnation, adapter device id)``, and neither half is read from
  a cache.** The adapter's settlement counter is scoped to its ``Device`` primary key and
  restarts at 1 for a fresh one, so a cursor carried across a device remap skips every
  settlement below its old value, permanently and silently. The incarnation half comes
  from the ``X-Store-Incarnation`` header of the page being consumed, because
  ``adapter_incarnation`` on the management row is only written when a read-state
  publication is adopted: a rebuilt store that recreates the device under the same numeric
  id leaves both cached halves matching while the counter has restarted.
* **The epoch is read off the row this module locks, never off an object it was handed.**
  The link repair writes the database and leaves its caller's row object holding ``None``
  or a dead id in two of its three branches, so binding to the locked row is what keeps
  every caller honest at once. ``SELECT … FOR UPDATE`` re-reads by definition, so the
  reload is not an added query — it is the lock query.
* **The stall bound is durable.** An in-memory counter restarts with the worker, and the
  device it was protecting head-of-line blocks forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from . import adapter_client

logger = logging.getLogger(__name__)

# The bound is attempts only: on the fifth failed resolution of one sequence the cursor
# advances past it with a loud log. Elapsed time is recorded and logged but is deliberately
# not a second settlement policy.
SETTLE_STALL_MAX_ATTEMPTS = 5

# One feed page per pass. The cursor is durable, so a device with a backlog drains over
# consecutive passes instead of holding the management row locked for an unbounded walk.
SETTLE_FEED_PAGE = 100


class SettlementFeedError(Exception):
    """The adapter served a settlement page that breaks the feed contract."""


@dataclass(frozen=True)
class ConsumeResult:
    """What one pass over one device's feed did."""

    adapter_device_id: int | None
    #: jobs whose results were correlated (or had nothing to correlate) and were walked past
    consumed: int
    #: the walk stopped on an unresolved head; the cursor did not move past it
    stalled: bool
    #: the stall bound was reached, so the cursor advanced past a sequence that never resolved
    advanced_past_stall: bool
    #: the epoch changed, so the cursor and the stall triple were reset and the page re-requested
    epoch_reset: bool
    cursor: int | None


def consume_static_route_settlements(mgmt) -> ConsumeResult:
    """Walk one device's settlement feed once, advancing its durable cursor.

    *mgmt* may be an ``NSODeviceManagement`` instance or its pk: only the pk is used. The
    row is re-read under ``select_for_update`` and every decision is made against that
    locked copy, because a caller's in-memory row can be stale in a way nothing signals.
    """
    from .models import NSODeviceManagement

    pk = getattr(mgmt, "pk", mgmt)
    with transaction.atomic():
        row = NSODeviceManagement.objects.select_for_update().get(pk=pk)
        return _consume_locked(row)


def _consume_locked(row) -> ConsumeResult:
    """Consume for the already-locked management row *row*."""
    device_id = row.adapter_device_id
    if device_id is None:
        # Unlinked: there is no adapter device to poll and no epoch to record. The link
        # repair owns getting this row an id back.
        return ConsumeResult(None, 0, False, False, False, row.settle_cursor_seq)

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
        cursor = 0
        _clear_stall(row)
        # Re-request from the start rather than applying a cursor that belongs to a store
        # or a device this page is not about. The header of the page actually consumed is
        # the one recorded, so a store that changes again mid-pass is recorded correctly.
        jobs, incarnation = adapter_client.get_settlement_feed(
            device_id, after_settle_seq=cursor, limit=SETTLE_FEED_PAGE
        )

    consumed = 0
    stalled = False
    advanced_past_stall = False
    for job in jobs:
        seq = job.get("settle_seq")
        if seq is None:
            # The ascending page's predicate is NULL-false by construction, so an
            # unsequenced row in it means the feed contract broke. Never guess a position.
            raise SettlementFeedError(
                f"job {job.get('id')} appeared in the ascending settlement feed with no settle_seq"
            )
        if _resolve_job(row, device_id, job):
            cursor = max(cursor, seq)
            _clear_stall(row)
            consumed += 1
            continue

        if row.settle_stall_seq != seq:
            row.settle_stall_seq = seq
            row.settle_stall_attempts = 1
            row.settle_stall_first_seen_at = timezone.now()
        else:
            row.settle_stall_attempts += 1

        if row.settle_stall_attempts >= SETTLE_STALL_MAX_ATTEMPTS:
            logger.error(
                "settlement stalled %s times on sequence %s for device %s (adapter device %s), "
                "first seen %s — advancing the cursor past it; that result is abandoned",
                row.settle_stall_attempts,
                seq,
                row.device_id,
                device_id,
                row.settle_stall_first_seen_at,
            )
            cursor = max(cursor, seq)
            _clear_stall(row)
            advanced_past_stall = True
            continue

        # Head-of-line: nothing behind an unresolved settlement may be consumed, or the
        # cursor would advance past a result still owed to an overlay.
        stalled = True
        break

    _persist(row, cursor, device_id, incarnation)
    return ConsumeResult(device_id, consumed, stalled, advanced_past_stall, epoch_reset, cursor)


def _clear_stall(row) -> None:
    """Drop the stall triple: it describes exactly one sequence and nothing else."""
    row.settle_stall_seq = None
    row.settle_stall_attempts = 0
    row.settle_stall_first_seen_at = None


def _persist(row, cursor: int, device_id: int, incarnation: str) -> None:
    """Write the cursor, the epoch and the stall triple in the consuming transaction.

    Through ``.update()``: a ``save()`` re-fires the management row's push handlers, and
    the cursor advance is bookkeeping, not an intent change.
    """
    from .models import NSODeviceManagement

    NSODeviceManagement.objects.filter(pk=row.pk).update(
        settle_cursor_seq=cursor,
        settle_cursor_incarnation=incarnation,
        settle_cursor_device_id=device_id,
        settle_stall_seq=row.settle_stall_seq,
        settle_stall_attempts=row.settle_stall_attempts,
        settle_stall_first_seen_at=row.settle_stall_first_seen_at,
    )


def _resolve_job(row, device_id: int, job: dict) -> bool:
    """Can this job's per-route results be correlated? False means "not yet" — a stall.

    An overlay that recorded no expectation cannot judge a result: the adapter commits its
    intent write before answering the PUT, so a response lost in flight leaves a committed
    generation the pusher never recorded. The read-back GET re-serves what that PUT would
    have echoed, and it is the only recovery — so failing to obtain it is undecided, not a
    verdict, and undecided is what the stall bound exists to bound.
    """
    from .intent_generation import UNALLOCATED
    from .models import NSOStaticRouteState

    results = (job.get("result") or {}).get("static_route_results")
    if not results:
        # Nothing correlatable rides this job (a removal job carries no route ids at all).
        return True

    states = {s.static_route_id: s for s in NSOStaticRouteState.objects.filter(management=row)}
    pending = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        state = states.get(entry.get("route_id"))
        if state is None:
            # The overlay is gone (the device left the route's membership), so nothing
            # waits on this result.
            continue
        if state.expected_generation is not None:
            continue
        if state.intent_generation == UNALLOCATED:
            # Never put on the wire, so no result can ever name it. The fleet backfill is
            # the repair; blocking the device on it would be a stall with no exit.
            continue
        pending.append(state)

    if not pending:
        return True

    try:
        echoed = adapter_client.get_static_route_intent(device_id)
    except adapter_client.AdapterError as exc:
        logger.warning(
            "settlement read-back failed for adapter device %s (job %s): %s — sequence %s undecided",
            device_id,
            job.get("id"),
            exc,
            job.get("settle_seq"),
        )
        return False

    _record_readback_expectations(row, pending, echoed)
    recovered = set(
        NSOStaticRouteState.objects.filter(
            pk__in=[state.pk for state in pending], expected_generation__isnull=False
        ).values_list("static_route_id", flat=True)
    )
    unrecoverable = sorted({state.static_route_id for state in pending} - recovered)
    if unrecoverable:
        # The store no longer holds what this result is about, so no later poll can decide
        # it either. Advancing is correct; doing it silently is not.
        logger.warning(
            "the adapter's intent read-back names no expectation for route(s) %s on device %s, "
            "so sequence %s can never correlate — advancing past it",
            unrecoverable,
            device_id,
            job.get("settle_seq"),
        )
    return True


def _record_readback_expectations(row, pending, echoed) -> None:
    """Store the recovered expectations through the one writer the push path uses."""
    from .signals import _record_static_route_expectations

    generations = {state.static_route_id: state.intent_generation for state in pending}
    _record_static_route_expectations(row.device_id, generations, (echoed or {}).get("routes") or [])
