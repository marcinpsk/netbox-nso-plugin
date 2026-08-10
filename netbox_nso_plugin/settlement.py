# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S — the settlement consumer: cursor epoch, feed walk, stall bound, verdicts.

This module walks one device's ordered settlement feed, decides for each terminal job
whether that job's per-route results can be correlated at all, applies the per-route
verdict to the overlay, advances a durable cursor over the jobs it decided, and bounds the
ones it cannot decide so a single unresolvable result never blocks a device forever.

It is the **only** writer of a static-route overlay's apply status. ``"static_route"`` left
``reconcile._APPLY_DEPLOYING_SCOPES`` and the static reconciler passes
``settles_deploying=False``, because neither of those channels can say *which* generation
the device is reflecting: both settled a row green for content the device may never have
received. Two clocks drive this one implementation — the adapter's post-apply notification
through ``run_device_reconcile``'s Step 4, and the plugin's own five-minute maintenance
tick (:func:`sweep_static_route_settlements`), which survives a dead callback channel.

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

# The bound is attempts only: on the fifth failed resolution of one feed entry the walk
# abandons it with a loud log. Elapsed time is recorded and logged but is deliberately
# not a second settlement policy.
SETTLE_STALL_MAX_ATTEMPTS = 5

# One feed page per pass. The cursor is durable, so a device with a backlog drains over
# consecutive passes instead of holding the management row locked for an unbounded walk.
SETTLE_FEED_PAGE = 100

# Used only when the adapter reported a per-route failure with no message of its own.
_GENERIC_RESULT_ERROR = "The apply reported a failure for this route (see adapter apply job #{job_id})."


@dataclass(frozen=True)
class ConsumeResult:
    """What one pass over one device's feed did."""

    adapter_device_id: int | None
    #: jobs whose results were correlated (or had nothing to correlate) and were walked past
    consumed: int
    #: the walk stopped on an unresolved head; the cursor did not move past it
    stalled: bool
    #: the stall bound was reached, so this pass abandoned an entry it could never resolve
    advanced_past_stall: bool
    #: the epoch changed, so the cursor and the stall triple were reset
    epoch_reset: bool
    cursor: int | None
    #: the walk reached the END of the feed: nothing is stalled and no further page is owed.
    #: The ONLY state in which the timeout backstop may judge a row — see
    #: :func:`settle_static_routes`.
    drained: bool = False


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


def settle_static_routes(mgmt, *, escalate: bool = True, apply_active: bool | None = None) -> ConsumeResult:
    """Walk the feed, then let the timeout backstop judge — but only a **drained** feed.

    The one implementation both clocks call, so the post-apply carrier and the five-minute
    maintenance tick cannot drift on either half. Two properties live here rather than at
    the call sites, because getting them wrong is silent in both directions:

    * **The backstop runs only on a drained, unstalled feed.** A walk that stopped on an
      unresolved head, or that filled its page and owes another, has not proven anything
      about a row still ``deploying`` — its result may be the very sequence the walk did not
      reach. Escalating there fails a healthy row on the first read-back outage and bypasses
      the durable stall bound that exists to make one unresolvable result survivable.
    * **The escalation belongs to BOTH clocks.** A dead callback channel is exactly the case
      the tick exists for; a tick that consumed but never escalated would advance past an
      unresolvable result on attempt five and then leave the row ``deploying`` forever,
      because every later page is empty and only the carrier judged. That is the same
      shared-failure-domain trap one level down.

    The remaining precondition — no apply in flight — is the backstop's own, so that both
    clocks get it without either restating it. A caller that already read the job state hands
    it over as *apply_active* rather than paying for a second jobs fetch; the maintenance
    tick, which has read nothing, leaves it None and the backstop looks it up itself.
    """
    from .reconcile import _escalate_stuck_static_routes

    outcome = consume_static_route_settlements(mgmt)
    if escalate and outcome.drained and outcome.adapter_device_id is not None:
        _escalate_stuck_static_routes(mgmt, adapter_device_id=outcome.adapter_device_id, apply_active=apply_active)
    return outcome


def sweep_static_route_settlements() -> tuple[int, int]:
    """Consume settlements for every device that is still owed one. Returns ``(polled, failed)``.

    The plugin's own clock, and the reason a dead adapter-to-NetBox callback channel cannot
    strand a device: this pass runs plugin-to-adapter, the direction that survives an
    invalid callback token. It is the **last** pass of the maintenance tick, after the link
    repair, and it issues its **own** candidate query rather than reusing the row objects
    that tick materialized — the repair writes the database while leaving the caller's
    object holding ``None`` or a dead adapter id in two of its three branches, so a reused
    list would skip a repaired device or poll it on an id that no longer exists.

    Bounded and isolated: a device with no owned static-route overlay still in flight is
    never polled, and one device's settlement error aborts neither the rest of the sweep
    nor anything that ran before it.
    """
    from . import status_machine as sm
    from .models import NSODeviceManagement

    candidates = list(
        NSODeviceManagement.objects.filter(
            adapter_device_id__isnull=False,
            static_route_states__status__in=(sm.ACCEPTED, sm.DEPLOYING),
        )
        .distinct()
        .values_list("pk", flat=True)
    )
    polled = 0
    failed = 0
    for pk in candidates:
        try:
            settle_static_routes(pk)
        except Exception:  # noqa: BLE001 — one device's adapter must not abort the fleet sweep
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
    advanced_past_stall = False
    readback = _Readback(device_id)
    for job in jobs:
        seq = job.get("settle_seq")
        if seq is None:
            # The ascending page's predicate is NULL-false by construction, so an
            # unsequenced row in it means the feed contract broke. Never guess a position:
            # bound it on the cursor it sits behind — the position it blocks — and abandon it
            # there, the way an unresolvable sequence is abandoned. Raising instead would roll
            # this transaction back, taking the cursor, the verdicts already written and the
            # stall record with it, so every later pass would meet the same row unbounded.
            logger.error(
                "job %s appeared in the ascending settlement feed for adapter device %s with no "
                "settle_seq: the feed contract is broken",
                job.get("id"),
                device_id,
            )
            if not _record_stall(row, cursor):
                stalled = True
                break
            logger.error(
                "the settlement feed for adapter device %s served an unsequenced job %s times, "
                "first seen %s — skipping that entry; its result is abandoned",
                device_id,
                row.settle_stall_attempts,
                row.settle_stall_first_seen_at,
            )
            _clear_stall(row)
            advanced_past_stall = True
            continue
        if _settle_job(row, device_id, job, readback):
            cursor = max(cursor, seq)
            _clear_stall(row)
            consumed += 1
            continue

        if _record_stall(row, seq):
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
    # A full page means the adapter may be holding more. Only a SHORT page proves the walk
    # reached the end, which is what the timeout backstop needs before it may judge.
    drained = not stalled and len(jobs) < SETTLE_FEED_PAGE
    return ConsumeResult(device_id, consumed, stalled, advanced_past_stall, epoch_reset, cursor, drained)


def _record_stall(row, seq) -> bool:
    """Count one failed resolution of *seq* on *row*. True once the durable bound is reached.

    *seq* keys the triple. For a job the feed served with no sequence there is none, so the
    key is the cursor that entry blocks: a stall key equal to the cursor is exactly that
    case, since a real undecided sequence is always ahead of it.
    """
    if row.settle_stall_seq != seq:
        row.settle_stall_seq = seq
        row.settle_stall_attempts = 1
        row.settle_stall_first_seen_at = timezone.now()
    else:
        row.settle_stall_attempts += 1
    return row.settle_stall_attempts >= SETTLE_STALL_MAX_ATTEMPTS


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


class _Readback:
    """One device's intent read-back, fetched at most once per pass — the failure too.

    ``get_static_route_intent`` is keyed by device alone, so every job on a page that needs
    it gets the same answer. Fetching per job would issue up to ``SETTLE_FEED_PAGE`` identical
    HTTP calls while this pass holds the management row's ``SELECT … FOR UPDATE``, blocking
    the push recorder, the link repair and reconcile for the sum of them.
    """

    def __init__(self, device_id: int):
        self._device_id = device_id
        self._fetched = False
        self._echoed = None
        self._error = None

    def fetch(self):
        """Return ``(echoed, error)`` — exactly one of the two is set."""
        if not self._fetched:
            self._fetched = True
            try:
                self._echoed = adapter_client.get_static_route_intent(self._device_id)
            except adapter_client.AdapterError as exc:
                self._error = exc
        return self._echoed, self._error


def _settle_job(row, device_id: int, job: dict, readback: _Readback) -> bool:
    """Apply this job's per-route verdicts. False means "undecided yet" — a stall.

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
        # Absence is not "all failed": a removal job carries no route ids at all, and an
        # apply that touched no static route says nothing about this device's routes.
        return True

    # Re-read (the verdicts are written through ``.update()``), but only the routes this
    # job's results name — the rest of the device's overlay is not evidence about them.
    named = {
        entry["route_id"] for entry in results if isinstance(entry, dict) and isinstance(entry.get("route_id"), int)
    }
    states = {
        s.static_route_id: s for s in NSOStaticRouteState.objects.filter(management=row, static_route_id__in=named)
    }
    correlated: list[tuple[dict, object]] = []
    pending = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        route_id = entry.get("route_id")
        if route_id is None:
            # This row is uncorrelated, and only this row: a null id is not a device-wide
            # fence signal, and falling back to the (vrf, prefix, next-hop) triple would
            # settle whatever route happens to match it.
            continue
        state = states.get(route_id)
        if state is None:
            # The overlay is gone (the device left the route's membership), so nothing
            # waits on this result.
            continue
        generation = entry.get("generation")
        if generation is None or generation == UNALLOCATED or generation != state.intent_generation:
            # Superseded, or never ours: the overlay has moved on to a newer generation, or
            # this result names intent that was never put on the wire under one.
            continue
        correlated.append((entry, state))
        if state.expected_generation != generation:
            pending.append(state)

    if pending:
        echoed, error = readback.fetch()
        if error is not None:
            logger.warning(
                "settlement read-back failed for adapter device %s (job %s): %s — sequence %s undecided",
                device_id,
                job.get("id"),
                error,
                job.get("settle_seq"),
            )
            return False
        _record_readback_expectations(row, pending, echoed)
        recovered = {s.pk: s for s in NSOStaticRouteState.objects.filter(pk__in=[s.pk for s in pending])}
        correlated = [(entry, recovered.get(state.pk, state)) for entry, state in correlated]

    for entry, state in correlated:
        _apply_verdict(device_id, job, entry, state)
    return True


def _apply_verdict(device_id: int, job: dict, entry: dict, state) -> None:
    """Turn one correlated per-route result into this overlay's status, or say why not."""
    generation = entry.get("generation")
    if state.expected_generation != generation:
        # The read-back could not recover an expectation, so no later poll can decide this
        # result either. Advancing is correct; doing it silently is not.
        logger.warning(
            "the adapter's intent read-back names no expectation for route %s on device %s, "
            "so sequence %s can never correlate — advancing past it",
            state.static_route_id,
            device_id,
            job.get("settle_seq"),
        )
        return

    fingerprint = entry.get("fingerprint") or ""
    if fingerprint != state.expected_fingerprint:
        logger.warning(
            "settlement fingerprint mismatch for route %s on device %s at generation %s: "
            "the apply reported %r, this device expected %r — not settled",
            state.static_route_id,
            device_id,
            generation,
            fingerprint,
            state.expected_fingerprint,
        )
        _advise(
            state,
            f"The apply reported fingerprint {fingerprint!r} for generation {generation}, but this "
            f"device's intent expects {state.expected_fingerprint!r}. The result describes content "
            f"this row is not waiting for, so it did not settle.",
        )
        return

    outcome = entry.get("outcome")
    if outcome == "in_sync":
        _settle(state, ok=True)
    elif outcome == "apply_failed":
        _settle(state, ok=False, error=_result_error(entry) or _GENERIC_RESULT_ERROR.format(job_id=job.get("id")))
    else:
        # `unproven` (and anything the adapter adds later) is evidence the apply could not
        # prove the value landed. It is not a failure and it is emphatically not a settle;
        # the scope-level counter may still say in_sync and must not be believed here.
        reason = _result_error(entry)
        _advise(
            state,
            f"The adapter reported {outcome!r} for generation {generation} on apply job "
            f"#{job.get('id')}, so this row did not settle." + (f" {reason}" if reason else ""),
        )


def _write_verdict(state, **fields) -> bool:
    """Write *fields* under a compare-and-set on everything the verdict was computed from.

    The consumer locks the **management** row, not the overlay, and it makes an HTTP
    read-back call while holding a copy of it — so an operator Accept or content edit can
    allocate a new generation and reset the status in that window. A plain save would then
    put an old result's verdict on new intent: a green badge for content the device has not
    been asked for yet. The CAS is the same shape ``_record_static_route_expectations``
    already uses on this table, and ``.update()`` also keeps the write from re-firing the
    row's intent push, which a settlement must never do.

    Returns whether the row still matched. Zero rows means a newer writer won, and the next
    pass recomputes from a fresh read.
    """
    from .models import NSOStaticRouteState

    matched = NSOStaticRouteState.objects.filter(
        pk=state.pk,
        status=state.status,
        intent_generation=state.intent_generation,
        expected_generation=state.expected_generation,
        expected_fingerprint=state.expected_fingerprint,
    ).update(**fields)
    if not matched:
        logger.debug(
            "settlement verdict for overlay %s skipped: the row moved under the read (generation %s)",
            state.pk,
            state.intent_generation,
        )
    return bool(matched)


def _settle(state, *, ok: bool, error: str = "") -> None:
    """Write the settled status, or leave the row alone when the result concerns no in-flight one."""
    from . import status_machine as sm

    new_status = sm.on_apply_result(state.status, ok=ok, settle_accepted=True)
    if new_status == state.status:
        # Already settled (or never owned): re-applying the same verdict would only rewrite
        # an error the row has since recovered from.
        return
    _write_verdict(
        state,
        status=new_status,
        last_apply_at=timezone.now(),
        last_apply_error=error,
        last_result_advisory="",
    )


def _advise(state, reason: str) -> None:
    """Record why a correlated result did not settle. Non-settling, so the status is untouched."""
    if state.last_result_advisory == reason:
        return
    _write_verdict(state, last_result_advisory=reason)


def _result_error(entry: dict) -> str:
    """Render the row-level ``{code, message, detail}`` the adapter computed as one line."""
    err = entry.get("error")
    if isinstance(err, dict):
        code = str(err.get("code") or "").strip()
        message = str(err.get("message") or err.get("detail") or "").strip()
        return f"{code}: {message}" if code and message else (message or code)
    return str(err or "").strip()


def _record_readback_expectations(row, pending, echoed) -> None:
    """Store the recovered expectations through the one writer the push path uses."""
    from .signals import _record_static_route_expectations

    generations = {state.static_route_id: state.intent_generation for state in pending}
    _record_static_route_expectations(row.device_id, generations, (echoed or {}).get("routes") or [])
