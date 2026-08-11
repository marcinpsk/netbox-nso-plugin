# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O: the claim protocol and the drain pass over the durable outbox.

The outbox records what an operator transaction did; this is what turns those records into
one request and settles it. A claim is one short REPEATABLE READ transaction: it locks the
key's state row, folds every unconsumed entry, renders the body under the same snapshot,
allocates the logical operation id and commits. The send then runs with no transaction open
and no row held, and a second short transaction records the outcome under a compare-and-set
on that id.

The snapshot bracket is the point of the isolation level: under READ COMMITTED the fold and
the render see different worlds, so a deletion committing between them produces a body that
omits a route while carrying no authority for it, and the adapter detaches it. One snapshot
makes the concurrently committed deletion invisible to both, so the entry stays unconsumed
and the next claim ships it with the body that omits it.

A failed attempt is REPLAYED, never reallocated: the sequence names the operation, not the
attempt, so the far side can admit a retry against its receipt and answer a lost response
with the stored one. Only two things return a consumed row to unconsumed, and both are
here: :func:`abandon` and the restore rebase Appendix O's later chunk owns.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import time

from django.db import IntegrityError, OperationalError, connection, transaction

from . import delivery
from .outbox import OP_REVOKE, allocate_push_seq, fold_transitions, reduce_transitions, triple_of

logger = logging.getLogger(__name__)

#: How long a claim's lease runs before a scavenger may take it over. Longer than any
#: sender's total deadline, so a live sender is never robbed of its own operation.
LEASE = datetime.timedelta(minutes=10)
#: Total wall clock one send may take. The client's ``(connect, read)`` tuple is not a
#: deadline: it measures the gap between bytes, so a dripping response resets it forever.
SEND_DEADLINE = datetime.timedelta(minutes=2)
#: Keys one drain pass may claim. It bounds KEYS, never a fold: a truncated fold would ship
#: a body without the authority for what the body already omits.
DRAIN_BATCH = 20
#: Synchronous redrains after a success, for latency only. The tick guarantees the rest.
DRAIN_CHAIN_MAX = 4
#: How long a forced call waits for an active claim before failing fast.
FORCE_WAIT = datetime.timedelta(seconds=5)
_FORCE_POLL = 0.2
_RETRIES = 3
# serialization_failure, deadlock_detected, unique_violation: all mean "retry the whole
# transaction", because a retry resumes on a snapshot the fold agrees with.
_RETRYABLE_SQLSTATES = {"40001", "40P01", "23505"}
# query_canceled: the fold outgrew the statement budget, which compaction is what fixes.
_STATEMENT_TIMEOUT = "57014"

NOTHING = "nothing"
PARKED = "parked"
ABANDONED = "abandoned"
SUCCEEDED = "succeeded"
FAILED = "failed"
SUPERSEDED = "superseded"
UNACKNOWLEDGED = "unacknowledged"

LEGACY_MARK_DOWNGRADED = "legacy_mark_downgraded"


class ClaimBusy(Exception):
    """A forced call found an active claim and would not queue behind it."""


class ClaimConflict(Exception):
    """The world changed under a claim: its exact-primary-key write hit a different count."""


@dataclasses.dataclass
class Claim:
    """One logical operation: the request, its authority, and the id it is admitted under."""

    device_id: int
    scope: str
    adapter_device_id: int
    push_seq: int
    payload: object
    digest: str
    deletions: list
    mark: bool | None
    mark_any: bool
    mode: str
    rendered: object
    replayed: bool = False


@dataclasses.dataclass
class Acknowledgement:
    """Whether a response acknowledges a claim's authority exactly, and which ids it named."""

    exact: bool
    acknowledged: frozenset = frozenset()
    reason: str = ""


# ── Plumbing ──────────────────────────────────────────────────────────────────


def _refuse_in_transaction(what: str) -> None:
    """Refuse a drain-side operation nested in a caller's transaction.

    The claim runs at REPEATABLE READ, which PostgreSQL only accepts before any statement of
    a transaction, and the send must hold no lock at all. Both are impossible inside a
    caller's block, so this refuses rather than silently degrading to the caller's isolation.
    """
    if connection.in_atomic_block:
        raise RuntimeError(f"the intent outbox {what} must run outside an open transaction")


def _db_now():
    """Read the database clock, the only clock both sides of the lease agree on."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT now()")
        return cursor.fetchone()[0]


def _retryable(exc) -> bool:
    return getattr(exc.__cause__, "sqlstate", None) in _RETRYABLE_SQLSTATES


def _repeatable_read(work):
    """Run *work* in one REPEATABLE READ transaction, retrying the WHOLE transaction.

    A serialization failure can surface at the state-row lock or at an entry lock taken
    after waiting behind compaction, so retrying anything narrower would resume on a
    snapshot the fold no longer agrees with. Exhaustion leaves every row untouched and the
    tick supplies the next attempt.
    """
    for attempt in range(1, _RETRIES + 1):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                return work()
        except (OperationalError, IntegrityError) as exc:
            if attempt == _RETRIES or not _retryable(exc):
                raise
            logger.info("intent outbox claim retried after %s (attempt %s)", type(exc).__name__, attempt)
    raise AssertionError("unreachable: the retry loop either returns or raises")


def _lock_state(device_id, scope):
    """Take the key's state row FOR UPDATE, creating it on demand.

    This row is the mutual-exclusion point of every drain-side operation (claim, outcome,
    abandon and compaction alike), so "never touches a row carrying a push_seq" is a lock
    rather than a predicate two readers can both satisfy.

    ``of=("self",)`` and the cleared ordering are load-bearing: ``Meta.ordering`` names the
    ``device`` FK, which orders by the RELATED model's ordering and so joins ``dcim_device``,
    and an unqualified ``select_for_update`` locks every joined table. That would take FOR
    UPDATE on the device row and block every operator transaction inserting anything that
    references it, the outbox entry included.
    """
    from .models import NSOIntentOutboxState

    rows = NSOIntentOutboxState.objects.select_for_update(of=("self",)).order_by()
    state = rows.filter(device_id=device_id, scope=scope).first()
    if state is None:
        NSOIntentOutboxState.objects.create(device_id=device_id, scope=scope)
        state = rows.get(device_id=device_id, scope=scope)
    return state


def _unconsumed(device_id, scope):
    from .models import NSOIntentOutboxEntry

    return NSOIntentOutboxEntry.objects.filter(device_id=device_id, scope=scope, consumed_by_push_seq__isnull=True)


def _work_pending(state) -> bool:
    """Whether the key owes a send: an unconsumed entry, queued authority, or an open claim."""
    return (
        state.push_seq is not None or bool(state.queued_deletions) or _unconsumed(state.device_id, state.scope).exists()
    )


def request_digest(payload, *, mode, deletions, mark) -> str:
    """Return the identity of one request: its body, its mode, its authority, its legacy flag.

    Two claims with the same digest are the same operation, which is what lets an unchanged
    save be dropped against the acknowledged baseline instead of re-sent.
    """
    material = {
        "payload": payload,
        "mode": mode,
        "deletions": sorted(int(record["route_id"]) for record in deletions),
        # The flag as the wire carries it: no contributors and unmarked contributors are one
        # request, so they must be one digest or an unchanged save re-sends forever.
        "mark": bool(mark),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


# ── The claim ─────────────────────────────────────────────────────────────────


def claim(device_id, scope, *, mode=delivery.MODE_NORMAL, force=False) -> Claim | None:
    """Form or replay the key's logical operation, and return it, or ``None`` if none is owed."""
    _refuse_in_transaction("claim")
    return _repeatable_read(lambda: _claim_locked(device_id, scope, mode, force))


def _claim_locked(device_id, scope, mode, force) -> Claim | None:
    from .models import NSODeviceManagement

    state = _lock_state(device_id, scope)
    now = _db_now()
    # Stamped for being TRIED, before anything can refuse: a key whose claim fails on every
    # tick must rotate to the back, or it occupies every pass and starves the rest.
    state.last_drain_attempted_at = now

    if state.claimed_at is not None and state.claimed_at > now - LEASE:
        state.save()
        if force:
            raise ClaimBusy(f"{device_id}/{scope} is claimed at push_seq {state.push_seq}")
        return None
    if not (force or state.push_seq is not None or _work_pending(state)):
        state.save()
        return None

    mgmt = (
        NSODeviceManagement.objects.select_for_update(of=("self",))
        .order_by()
        .filter(device_id=device_id, adapter_device_id__isnull=False)
        .first()
    )
    if mgmt is None:
        # Unmanaged or unlinked: the work keeps, and the link repair owns getting an id back.
        state.save()
        return None

    if state.push_seq is not None:
        return _takeover(state, mgmt, now)
    return _form(state, mgmt, now, mode, force)


def _takeover(state, mgmt, now) -> Claim:
    """Replay the unacknowledged operation: same sequence, same body, fresh lease."""
    state.claimed_at = now
    state.save()
    flags = state.claim_flags or {}
    logger.info("taking over push_seq %s for %s/%s", state.push_seq, state.device_id, state.scope)
    return Claim(
        device_id=state.device_id,
        scope=state.scope,
        adapter_device_id=mgmt.adapter_device_id,
        push_seq=state.push_seq,
        payload=state.claim_payload,
        digest=state.claim_digest,
        deletions=list(state.claim_deletions or []),
        mark=state.claim_mark,
        mark_any=bool(flags.get("mark_any")),
        mode=flags.get("mode", delivery.MODE_NORMAL),
        rendered=delivery.render(state.scope, state.device_id, mgmt.adapter_device_id),
        replayed=True,
    )


def _form(state, mgmt, now, mode, force) -> Claim | None:
    """Fold every unconsumed entry, render under the same snapshot, and take the operation."""
    from .models import NSOIntentOutboxEntry

    device_id, scope = state.device_id, state.scope
    rows = list(_unconsumed(device_id, scope).select_for_update().order_by("id"))
    entry_ids = [row.pk for row in rows]
    mark = all(row.mark_and for row in rows) if rows else None
    mark_any = any(row.mark_any for row in rows)
    folded = fold_transitions(
        [record for row in rows for record in row.transitions],
        claim_deletions=[int(record["route_id"]) for record in state.claim_deletions or []],
        queued=state.queued_deletions,
        revoked=state.revoked_ids,
        lineage_carry=state.lineage_carry,
    )
    deletions = list(folded.queued.values())

    rendered = delivery.render(scope, device_id, mgmt.adapter_device_id)
    digest = request_digest(rendered.payload, mode=mode, deletions=deletions, mark=mark)

    state.revoked_ids = sorted(folded.revoked)
    state.lineage_carry = folded.lineage_carry
    if not force and not deletions and digest == state.last_success_digest:
        # Nothing changed and nothing is owed. The rows are retired here because this path
        # has no outcome transaction, and rows nobody retires would hold the deployment gate
        # shut forever while no-op history grew.
        state.queued_deletions = []
        state.save()
        _retire(NSOIntentOutboxEntry.objects.filter(id__in=entry_ids), entry_ids)
        logger.debug("dropping the unchanged %s/%s claim and retiring %s rows", device_id, scope, len(entry_ids))
        return None

    push_seq = allocate_push_seq()
    _consume(entry_ids, push_seq)
    state.push_seq = push_seq
    state.claimed_at = now
    state.claim_payload = rendered.payload
    state.claim_digest = digest
    state.claim_deletions = deletions
    state.claim_mark = mark
    state.claim_flags = {"mode": mode, "mark_any": mark_any, "force": bool(force)}
    state.queued_deletions = []
    state.save()
    return Claim(
        device_id=device_id,
        scope=scope,
        adapter_device_id=mgmt.adapter_device_id,
        push_seq=push_seq,
        payload=rendered.payload,
        digest=digest,
        deletions=deletions,
        mark=mark,
        mark_any=mark_any,
        mode=mode,
        rendered=rendered,
    )


def _consume(entry_ids, push_seq) -> None:
    """Mark exactly the folded rows, and verify the count against the selection."""
    from .models import NSOIntentOutboxEntry

    if not entry_ids:
        return
    marked = NSOIntentOutboxEntry.objects.filter(id__in=entry_ids, consumed_by_push_seq__isnull=True).update(
        consumed_by_push_seq=push_seq
    )
    if marked != len(entry_ids):
        raise ClaimConflict(f"consumed {marked} of {len(entry_ids)} folded entries")


def _retire(queryset, expected_ids) -> None:
    """Delete exactly the named rows, aborting rather than proceeding on a changed world."""
    if not expected_ids:
        return
    deleted, _ = queryset.delete()
    if deleted != len(expected_ids):
        raise ClaimConflict(f"retired {deleted} of {len(expected_ids)} rows")


# ── Compaction ────────────────────────────────────────────────────────────────


def compact(device_id, scope) -> int:
    """Collapse the key's compactable unconsumed entries into one. Returns rows removed.

    It runs whatever the claim is doing. A claim stuck on a replayable failure keeps the key
    undrainable until its lease runs out, and a burst arriving behind it would otherwise
    accumulate one row per operator transaction with nothing to merge them.
    """
    _refuse_in_transaction("compaction")
    return _repeatable_read(lambda: _compact_locked(device_id, scope))


def _compact_locked(device_id, scope) -> int:
    from .models import NSOIntentOutboxEntry

    state = _lock_state(device_id, scope)
    held = {int(record["route_id"]) for record in state.claim_deletions or []}
    rows = list(_unconsumed(device_id, scope).select_for_update().order_by("id"))
    inputs = [row for row in rows if _compactable(row, held)]
    if len(inputs) < 2:
        return 0

    # The HIGHEST-id input, updated in place. A minted row would take an id above anything
    # that committed while this ran, so the later fold would apply that entry's transition
    # before the compacted one and reverse the real order.
    survivor, retired = inputs[-1], [row.pk for row in inputs[:-1]]
    updated = NSOIntentOutboxEntry.objects.filter(pk=survivor.pk, consumed_by_push_seq__isnull=True).update(
        transitions=reduce_transitions([record for row in inputs for record in row.transitions]),
        mark_and=all(row.mark_and for row in inputs),
        mark_any=any(row.mark_any for row in inputs),
    )
    if updated != 1:
        raise ClaimConflict(f"compaction rewrote {updated} rows for entry {survivor.pk}")
    _retire(NSOIntentOutboxEntry.objects.filter(pk__in=retired), retired)
    logger.debug("compacted %s rows into entry %s for %s/%s", len(inputs), survivor.pk, device_id, scope)
    return len(retired)


def _compactable(row, held) -> bool:
    """Whether a row may be merged: it names no route an active claim already holds.

    The exactness proof needs the fold-time constant to hold from compaction until the later
    fold, and that interval spans the claim's success or abandon. Excluding the routes it
    holds makes the interval real; those rows compact on a later pass.
    """
    return not any(record.get("route_id") in held for record in row.transitions)


def compaction_candidates(limit=None) -> list[tuple[int, str]]:
    """Return the keys carrying more than one unconsumed entry, which are the only merge-able ones."""
    from django.db.models import Count

    from .models import NSOIntentOutboxEntry

    limit = DRAIN_BATCH if limit is None else limit
    grouped = (
        NSOIntentOutboxEntry.objects.filter(consumed_by_push_seq__isnull=True)
        .values_list("device_id", "scope")
        .annotate(rows=Count("id"))
        .filter(rows__gt=1)
        .order_by("device_id", "scope")
    )
    return [(device_id, scope) for device_id, scope, _rows in grouped[:limit]]


# ── The send ──────────────────────────────────────────────────────────────────


def revocation_hit(claim: Claim) -> bool:
    """Whether a committed re-ownership of an in-flight deletion is visible before the send.

    Best effort by operator decision (OQ-O-7): it promises to catch a revocation committed
    before this scan, not one committed while the socket is being written. The scan reads
    the unconsumed transitions as well as the state row, because a revocation that committed
    before the check lives in an entry no claim has folded yet.
    """
    if not claim.deletions:
        return False
    held = {int(record["route_id"]) for record in claim.deletions}
    with transaction.atomic():
        state = _lock_state(claim.device_id, claim.scope)
        if held & {int(route_id) for route_id in state.revoked_ids or []}:
            return True
        for row in _unconsumed(claim.device_id, claim.scope).only("transitions"):
            for record in row.transitions:
                if record.get("op") == OP_REVOKE and int(record.get("route_id", -1)) in held:
                    return True
    return False


def send_claim(claim: Claim):
    """Make the claim's HTTP call and return the adapter's answer.

    Returns :data:`PARKED` when the device is no longer managed and :data:`ABANDONED` when
    the pre-send scan found a revocation, in both cases without sending. Parking is not a
    third abandon cause: the operation keeps its rows, its authority and its sequence, and
    resumes when the device is managed again.
    """
    _refuse_in_transaction("send")
    from . import signals

    if not signals._device_is_managed(claim.device_id):
        logger.info("device %s is no longer NSO-managed, parking push_seq %s", claim.device_id, claim.push_seq)
        return PARKED
    if revocation_hit(claim):
        logger.info("a revocation withdrew authority from push_seq %s, abandoning it", claim.push_seq)
        abandon(claim)
        return ABANDONED
    return delivery.send(
        claim.rendered,
        claim.payload,
        mode=claim.mode,
        mark=bool(claim.mark),
        push_seq=claim.push_seq,
        deadline=SEND_DEADLINE.total_seconds(),
    )


# ── The outcome ───────────────────────────────────────────────────────────────


def acknowledgement(claim: Claim, response) -> Acknowledgement:
    """Whether *response* acknowledges the claim's authority exactly (§4.4).

    A ``query_flag`` scope carries its authority in the request flag rather than in ids, so
    a successful marked full-replace IS the deletion and acknowledges everything it claimed.
    A ``per_object`` scope must answer with three lists that partition the requested set:
    unique within each, pairwise disjoint, no id the claim did not request, exact coverage.
    """
    requested = {int(record["route_id"]) for record in claim.deletions}
    if not requested:
        return Acknowledgement(True)
    if delivery.delivery_keys()[claim.scope].marking_mode != delivery.MARKING_PER_OBJECT:
        return Acknowledgement(True, frozenset(requested))
    if not isinstance(response, dict):
        return Acknowledgement(False, reason="the response carries no acknowledgement lists")
    seen: set[int] = set()
    for name in ("deleted_executed_ids", "deleted_degraded_ids", "deleted_moot_ids"):
        values = response.get(name)
        if not isinstance(values, list):
            return Acknowledgement(False, reason=f"{name} is missing")
        ids = {int(value) for value in values}
        if len(ids) != len(values):
            return Acknowledgement(False, reason=f"{name} repeats an id")
        if ids & seen:
            return Acknowledgement(False, reason="the acknowledgement lists overlap")
        seen |= ids
    if seen != requested:
        return Acknowledgement(False, reason="the acknowledgement does not cover the claim exactly")
    return Acknowledgement(True, frozenset(seen))


def settle(claim: Claim, response) -> str:
    """Record the outcome of an acknowledged send, under a compare-and-set on the sequence."""
    _refuse_in_transaction("outcome")
    from .models import NSOIntentOutboxEntry

    with transaction.atomic():
        state = _lock_state(claim.device_id, claim.scope)
        if state.push_seq != claim.push_seq:
            logger.info(
                "discarding the push_seq %s outcome for %s/%s: the key is now at %s",
                claim.push_seq,
                claim.device_id,
                claim.scope,
                state.push_seq,
            )
            return SUPERSEDED
        now = _db_now()
        ack = acknowledgement(claim, response)
        if not ack.exact:
            logger.warning("push_seq %s was not acknowledged exactly: %s", claim.push_seq, ack.reason)
            state.claimed_at = None
            state.attempts += 1
            state.last_error_code = "ack_not_exact"
            state.last_error_at = now
            state.save()
            return UNACKNOWLEDGED

        if claim.mark is False and claim.mark_any:
            # The fold downgraded a marked contributor, which is today's cross-transaction
            # AND. It is recorded because the success path pops the transient error entry.
            state.degraded_deletions = [
                *state.degraded_deletions,
                {
                    "route_ids": sorted(ack.acknowledged),
                    "triples": [record.get("triples") for record in claim.deletions],
                    "at": now.isoformat(),
                    "reason": LEGACY_MARK_DOWNGRADED,
                    "device": claim.device_id,
                },
            ]
        retired = NSOIntentOutboxEntry.objects.filter(
            device_id=claim.device_id, scope=claim.scope, consumed_by_push_seq=claim.push_seq
        )
        _retire(retired, list(retired.values_list("pk", flat=True)))
        state.claim_deletions = []
        state.revoked_ids = [int(route_id) for route_id in state.revoked_ids if int(route_id) not in ack.acknowledged]
        state.lineage_carry = {
            route_id: triple
            for route_id, triple in (state.lineage_carry or {}).items()
            if int(route_id) not in ack.acknowledged
        }
        state.push_seq = None
        state.claimed_at = None
        state.claim_payload = None
        state.claim_digest = ""
        state.claim_flags = {}
        state.claim_mark = None
        state.attempts = 0
        state.last_success_digest = claim.digest
        state.last_success_at = now
        state.save()
        _stamp_last_acked(claim)
    return SUCCEEDED


def _stamp_last_acked(claim: Claim) -> None:
    """Record the triple the adapter accepted, which is the only one a deletion can match.

    Not filtered on the overlay's current generation: a late acknowledgement whose overlay
    has since advanced still acknowledged the intermediate triple, and that is exactly the
    one the lineage exists to remember.
    """
    if claim.scope != "static_route":
        return
    from .models import NSOStaticRouteState

    for route in claim.payload or []:
        route_id = route.get("route_id")
        if route_id is None:
            continue
        NSOStaticRouteState.objects.filter(management__device_id=claim.device_id, static_route_id=route_id).update(
            last_acked_triple=triple_of(route.get("vrf") or "", route.get("prefix") or "", route.get("next_hop") or "")
        )


def record_failure(claim: Claim, exc: Exception) -> str:
    """Release the lease and count the attempt. The authority is untouched: it is replayed."""
    _refuse_in_transaction("outcome")
    with transaction.atomic():
        state = _lock_state(claim.device_id, claim.scope)
        if state.push_seq != claim.push_seq:
            return SUPERSEDED
        state.claimed_at = None
        state.attempts += 1
        state.last_error_code = str(getattr(exc, "code", "") or type(exc).__name__)[:64]
        state.last_error_at = _db_now()
        state.save()
    return FAILED


def abandon(claim: Claim) -> str:
    """Return the claim's rows to unconsumed and its authority to ``queued_deletions``.

    Legal only before the send or on a proven-no-effect response: after the send nothing but
    the receipt can say whether the far side applied the operation. The sequence is burned,
    never reissued.
    """
    _refuse_in_transaction("abandon")
    from .models import NSOIntentOutboxEntry

    with transaction.atomic():
        state = _lock_state(claim.device_id, claim.scope)
        if state.push_seq != claim.push_seq:
            return SUPERSEDED
        NSOIntentOutboxEntry.objects.filter(
            device_id=claim.device_id, scope=claim.scope, consumed_by_push_seq=claim.push_seq
        ).update(consumed_by_push_seq=None)
        queued = {int(record["route_id"]): record for record in state.queued_deletions}
        for record in state.claim_deletions or []:
            queued.setdefault(int(record["route_id"]), record)
        state.queued_deletions = list(queued.values())
        state.claim_deletions = []
        state.push_seq = None
        state.claimed_at = None
        state.claim_payload = None
        state.claim_digest = ""
        state.claim_flags = {}
        state.claim_mark = None
        state.save()
    return ABANDONED


# ── The drain ─────────────────────────────────────────────────────────────────


def _timed_out(exc) -> bool:
    """Whether the database cancelled the statement, which is the fold outgrowing its budget."""
    return getattr(exc.__cause__, "sqlstate", None) == _STATEMENT_TIMEOUT


def _stamp_attempt(device_id, scope) -> None:
    """Record that the key was tried, so a key that cannot be claimed still rotates."""
    with transaction.atomic():
        state = _lock_state(device_id, scope)
        state.last_drain_attempted_at = _db_now()
        state.save()


def _claim_or_compact(device_id, scope, *, mode, force):
    """Take the claim, compacting once if the fold outgrew the statement budget.

    Returns ``(claim_or_None, timed_out)``. A key that times out even after compaction keeps
    its work and is left STAMPED, so it rotates to the back of the next pass rather than
    spinning at the head of every one.
    """
    try:
        return _claim_or_wait(device_id, scope, mode=mode, force=force), False
    except OperationalError as exc:
        if not _timed_out(exc):
            raise
    logger.warning("the %s/%s fold timed out; compacting and retrying it once", device_id, scope)
    compact(device_id, scope)
    try:
        return _claim_or_wait(device_id, scope, mode=mode, force=force), False
    except OperationalError as retried:
        if not _timed_out(retried):
            raise
    _stamp_attempt(device_id, scope)
    return None, True


def drain_key(device_id, scope, *, mode=delivery.MODE_NORMAL, force=False, chain=DRAIN_CHAIN_MAX, reform=1) -> str:
    """Claim, send and settle one key, then chain a bounded number of further drains."""
    _refuse_in_transaction("drain")
    claimed, timed_out = _claim_or_compact(device_id, scope, mode=mode, force=force)
    if timed_out:
        return FAILED
    if claimed is None:
        return NOTHING
    try:
        answer = send_claim(claimed)
    except Exception as exc:  # noqa: BLE001 (the operation is replayed, so the failure is data)
        logger.warning("push_seq %s failed for %s/%s: %s", claimed.push_seq, device_id, scope, exc)
        return record_failure(claimed, exc)
    if answer == ABANDONED and reform > 0:
        # A fixed revocation resolves in ONE re-form: the re-form folds the revoking entry
        # with everything else, so the authority no longer names that route. A revocation
        # committing during the re-form is the accepted OQ-O-7 residual, not a loop.
        return drain_key(device_id, scope, mode=mode, force=force, chain=chain, reform=reform - 1)
    if answer in (PARKED, ABANDONED):
        return answer
    outcome = settle(claimed, answer)
    if outcome == SUCCEEDED and chain > 0 and _pending(device_id, scope):
        # Level-triggered, and terminating: each successful pass retires at least one row or
        # clears authority. The cap is for latency; the tick guarantees the rest.
        drain_key(device_id, scope, mode=mode, chain=chain - 1)
    return outcome


def _claim_or_wait(device_id, scope, *, mode, force) -> Claim | None:
    """Take the claim. A forced call waits a bounded time for an active one, then fails fast."""
    if not force:
        return claim(device_id, scope, mode=mode)
    deadline = time.monotonic() + FORCE_WAIT.total_seconds()
    while True:
        try:
            return claim(device_id, scope, mode=mode, force=True)
        except ClaimBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_FORCE_POLL)


def _pending(device_id, scope) -> bool:
    from .models import NSOIntentOutboxState

    state = NSOIntentOutboxState.objects.filter(device_id=device_id, scope=scope).first()
    if state is None:
        return _unconsumed(device_id, scope).exists()
    return _work_pending(state)


def drain_candidates(limit=None) -> list[tuple[int, str]]:
    """Return the keys this pass may claim, least recently attempted first.

    Re-queried, never taken from a caller's row objects: the tick's link repair writes the
    database while leaving those objects holding ``None`` or a dead adapter id, so a reused
    list would skip a device repaired this tick or push to an id that no longer exists.
    """
    from .models import NSODeviceManagement, NSOIntentOutboxEntry, NSOIntentOutboxState

    limit = DRAIN_BATCH if limit is None else limit
    managed = set(
        NSODeviceManagement.objects.filter(adapter_device_id__isnull=False).values_list("device_id", flat=True)
    )
    if not managed:
        return []
    keys = {
        (device_id, scope)
        for device_id, scope in NSOIntentOutboxEntry.objects.filter(consumed_by_push_seq__isnull=True)
        .values_list("device_id", "scope")
        .distinct()
        if device_id in managed
    }
    states = {}
    for state in NSOIntentOutboxState.objects.filter(device_id__in=managed):
        states[(state.device_id, state.scope)] = state
        if state.push_seq is not None or state.queued_deletions:
            keys.add((state.device_id, state.scope))

    fresh = _db_now() - LEASE
    ordered = []
    for key in keys:
        state = states.get(key)
        if state is not None and state.claimed_at is not None and state.claimed_at > fresh:
            continue  # an active claim owns the key; its own lease is what releases it
        attempted = state.last_drain_attempted_at if state is not None else None
        ordered.append((attempted is not None, attempted or datetime.datetime.min, key))
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))
    return [key for _stamped, _at, key in ordered[:limit]]


def drain_intent_outbox(limit=None) -> tuple[int, int]:
    """Appendix O's tick pass. Returns ``(drained, failed)``.

    Bounded, candidate-filtered and starvation-free: a key is attempted only when it owes a
    send, the stamp on every attempt rotates a replayably failing key to the back, and one
    key's failure aborts neither the pass nor the tick.

    Compaction runs first, over its OWN candidates: a key whose claim is stuck on a
    replayable failure is not drainable and would never be compacted otherwise, which is
    exactly the case where a burst accumulates.
    """
    for device_id, scope in compaction_candidates(limit):
        try:
            compact(device_id, scope)
        except Exception:  # noqa: BLE001 (one key's compaction must not abort the fleet pass)
            logger.exception("intent outbox compaction failed for %s/%s", device_id, scope)

    drained = failed = 0
    for device_id, scope in drain_candidates(limit):
        try:
            outcome = drain_key(device_id, scope)
        except Exception:  # noqa: BLE001 (one key's adapter must not abort the fleet pass)
            logger.exception("intent outbox drain failed for %s/%s", device_id, scope)
            failed += 1
            continue
        if outcome == SUCCEEDED:
            drained += 1
        elif outcome in (FAILED, UNACKNOWLEDGED):
            failed += 1
    return drained, failed


def gate_blockers(device_id=None) -> list[str]:
    """Return §4.6's per-key deployment preconditions, as the reasons a gate would refuse.

    The command that enforces them is O3's; the predicate lives here because it reads the
    outbox's own invariants and is what a claim's own paths have to leave true.
    """
    from .models import NSOIntentOutboxEntry, NSOIntentOutboxState

    rows = NSOIntentOutboxEntry.objects.all()
    states = NSOIntentOutboxState.objects.all()
    if device_id is not None:
        rows = rows.filter(device_id=device_id)
        states = states.filter(device_id=device_id)

    blockers = []
    for entry_device, scope in rows.filter(consumed_by_push_seq__isnull=True).values_list("device_id", "scope"):
        blockers.append(f"{entry_device}/{scope}: an unconsumed entry")
    for entry_device, scope in rows.filter(consumed_by_push_seq__isnull=False).values_list("device_id", "scope"):
        blockers.append(f"{entry_device}/{scope}: a row carrying a push_seq")
    for state in states:
        key = f"{state.device_id}/{state.scope}"
        if state.queued_deletions:
            blockers.append(f"{key}: queued deletions")
        if state.revoked_ids:
            blockers.append(f"{key}: revoked ids")
        if state.claimed_at is not None:
            blockers.append(f"{key}: a live claim")
        if state.claim_deletions:
            blockers.append(f"{key}: in-flight deletion authority")
        if state.fence_withheld_since is not None:
            blockers.append(f"{key}: the fence is withheld")
    return sorted(set(blockers))
