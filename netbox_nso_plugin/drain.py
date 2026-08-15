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

Two of the eighteen delivery keys hold no receipt and so may never be replayed at all. They
take the claim-less path of :func:`_deliver_direct` (the Rev 15 split, §7.1).
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
from .outbox import (
    OP_REVOKE,
    advance_push_seq,
    allocate_push_seq,
    fold_transitions,
    issued_push_seq,
    reduce_transitions,
    triple_of,
)

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
#: Largest sequence gap one restore may burn automatically. A larger far-side watermark
#: needs operator investigation instead of an unbounded walk through a NO CYCLE sequence.
MAX_RESTORE_GAP = 10_000_000
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
REFUSED = "refused"
PARKED = "parked"
ABANDONED = "abandoned"
SUCCEEDED = "succeeded"
_PARKED_SEND = object()
_ABANDONED_SEND = object()
FAILED = "failed"
SUPERSEDED = "superseded"
UNACKNOWLEDGED = "unacknowledged"
REJECTED = "rejected"

WITHHELD = "withheld"

#: Why a durable ``degraded_deletions`` record was written. Never cleared by a push outcome:
#: only the explicit operator acknowledgement clears either of them.
LEGACY_MARK_DOWNGRADED = "legacy_mark_downgraded"
PRE_FENCE_DETACH = "pre_fence_detach"

#: What each reason means to an operator, who reads a banner rather than a constant.
DEGRADED_REASONS = {
    LEGACY_MARK_DOWNGRADED: "sent unmarked: an unmarked contributor folded in with the deletion",
    PRE_FENCE_DETACH: "detached before the fence opened: the adapter dropped the row it could not remove",
}

#: Adapter rejections PROVEN to have had no effect: the request unwound through the claim
#: guard's rollback before its single commit (O-A5). Only these may abandon a sent claim.
PROVEN_NO_EFFECT = ("fence_shut", "store_only_deletion")

#: The adapter's refusal of the BODY, at the boundary: every raise site precedes the first
#: statement of the request's own transaction, so it is proven no-effect by the same rule.
BOUNDARY_REJECTION = "validation_error"


class ClaimBusy(Exception):
    """A forced call found an active claim and would not queue behind it."""


class ProtocolViolation(Exception):
    """A response that is not a partition of the claim it answers (§4.4)."""

    code = "nso_ack_not_a_partition"


class ClaimConflict(Exception):
    """The world changed under a claim: its exact-primary-key write hit a different count."""


class AuthorityPending(Exception):
    """A store-only claim was asked to carry deletion authority it can never deliver (§4.3(d))."""

    code = "nso_store_only_authority_pending"


class DirectApplyFailed(Exception):
    """A direct-apply endpoint answered HTTP 200 with an error envelope (§7.1)."""

    code = "nso_direct_apply_failed"


@dataclasses.dataclass
class Claim:
    """One logical operation: the request, its authority, and the id it is admitted under."""

    device_id: int
    scope: str
    adapter_device_id: int
    push_seq: int
    payload: object
    #: Plugin-side, mode-bearing: the acknowledged baseline an unchanged claim drops against.
    identity: str
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


def request_identity(payload, *, mode, deletions, mark, epoch) -> str:
    """Return the identity of one request: its body, its mode, its authority, its legacy flag.

    Two claims with the same identity are the same operation, which is what lets an unchanged
    save be dropped against the acknowledged baseline instead of re-sent. It is a PLUGIN-side
    fact and never leaves the plugin: the mode and the mark ride as query flags, so a body
    delivered store-only and the same body delivered normally are one wire digest and two
    identities, and dropping the second against the first would lose the delivery.

    *epoch* is the adapter mapping the body is delivered under (:func:`mapping_epoch`), and it
    is part of the identity for the same reason: a repaired link points the same body at a
    device that never received it, so an identity blind to the mapping would drop the delivery
    that mapping needs. Binding it here covers every writer of ``adapter_device_id`` at once.
    """
    material = {
        "payload": payload,
        "mode": mode,
        "deletions": sorted(int(record["route_id"]) for record in deletions),
        # The flag as the wire carries it: no contributors and unmarked contributors are one
        # request, so they must be one identity or an unchanged save re-sends forever.
        "mark": bool(mark),
        "epoch": list(epoch),
    }
    return _sha256(material)


def mapping_epoch(mgmt) -> tuple:
    """Return the adapter a body is delivered to: its device row, and the store holding it.

    Appendix S's own epoch (``models.py``: the settlement cursor), for the same two events: a
    remapped device id and a rebuilt store both leave the far side holding nothing the
    acknowledged baseline can speak for.

    The OBSERVED incarnation is read ahead of the adopted one, because ``adapter_incarnation``
    is written only where a gated read publication adopts the new pair. A store rebuilt under
    the same numeric device id changes nothing else the row carries, so an epoch taken from the
    adopted field alone stays the dead store's for the whole adoption window: an unchanged save
    draining there matches the baseline, is retired with no request, and nothing re-enqueues
    it. The observed marker is the same fact recorded earlier, and it is the only one recorded
    at all for a pair the gate refuses to adopt, which an equal-born rebuild leaves forever.
    """
    return (mgmt.adapter_device_id, mgmt.reset_pending_incarnation or mgmt.adapter_incarnation or "")


def wire_digest(body) -> str:
    """Return the digest of the exact JSON body sent, which is what the receipt names (§4.4).

    The adapter computes its ``request_digest`` from the raw body it received, so this is
    the only value a receipt's digest can be compared against. It is DERIVED rather than
    stored: the claim already persists the payload it sent, and the envelope around it
    belongs to the scope's endpoint, so storing a digest of the two would be storing a
    value the row already determines.
    """
    if not isinstance(body, bytes):
        raise TypeError("wire_digest requires the serialized request bytes")
    return hashlib.sha256(body).hexdigest()


def _sha256(material) -> str:
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
    if state.fence_withheld_since is not None and mode != delivery.MODE_BACKFILL_ONLY:
        # While the fence is withheld the ONLY send permitted for the key is the
        # backfill-only claim that opens it: any ordinary or store-only push omits the
        # withheld route and would destroy its before-image (O-A4).
        state.save()
        logger.info("%s/%s is fence-withheld; only the backfill claim may send", device_id, scope)
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
        replay = _takeover(state, mgmt, now)
        if replay is not None:
            return replay
        return _form(state, mgmt, now, mode, force)
    return _form(state, mgmt, now, mode, force)


def _takeover(state, mgmt, now) -> Claim | None:
    """Replay the unacknowledged operation: same sequence, same body, fresh lease."""
    flags = state.claim_flags or {}
    current_identity = request_identity(
        state.claim_payload,
        mode=flags.get("mode", delivery.MODE_NORMAL),
        deletions=state.claim_deletions or [],
        mark=state.claim_mark,
        epoch=mapping_epoch(mgmt),
    )
    if current_identity != state.claim_identity:
        logger.info(
            "abandoning push_seq %s for %s/%s after its mapping epoch changed",
            state.push_seq,
            state.device_id,
            state.scope,
        )
        _abandon_locked(state)
        return None
    state.claimed_at = now
    state.save()
    logger.info("taking over push_seq %s for %s/%s", state.push_seq, state.device_id, state.scope)
    return Claim(
        device_id=state.device_id,
        scope=state.scope,
        adapter_device_id=mgmt.adapter_device_id,
        push_seq=state.push_seq,
        payload=state.claim_payload,
        identity=state.claim_identity,
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
    if mode == delivery.MODE_BACKFILL_ONLY:
        return _form_backfill(state, mgmt, now)
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
    if mode == delivery.MODE_STORE_ONLY:
        # A deletion mark on a key whose pending rows recorded no provenance at all is
        # authority the FOLD cannot see: the request flag is the whole of it. Where a row
        # DID record its transitions the fold decides, so a revocation withdraws it as usual.
        untracked = mark_any and not any(row.transitions for row in rows)
        return _form_store_only(state, mgmt, now, deletions, untracked, force)

    rendered = delivery.render(scope, device_id, mgmt.adapter_device_id)
    identity = request_identity(rendered.payload, mode=mode, deletions=deletions, mark=mark, epoch=mapping_epoch(mgmt))

    state.revoked_ids = sorted(folded.revoked)
    state.lineage_carry = folded.lineage_carry
    if not force and not deletions and identity == state.last_success_identity:
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
    state.claim_identity = identity
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
        identity=identity,
        deletions=deletions,
        mark=mark,
        mark_any=mark_any,
        mode=mode,
        rendered=rendered,
    )


def _form_store_only(state, mgmt, now, deletions, untracked_mark, force) -> Claim:
    """Take a claim that carries the owned snapshot and consumes nothing (§4.3(d)).

    Store-only carries no ids and clears none, so the fold above is a READ: it decides the
    refusal and nothing else. Consuming the key's entries would let the outcome retire them
    (O1.36) on a request that delivered none of what they stand for, so the ordinary
    delivery would be gone with nothing on the wire that ever carried it. They stay
    unconsumed for the ordinary claim, which the tick supplies.

    The refusal is the same rule at its authority end: the adapter writes no tombstone for a
    store-only request (O-A3), so a deletion carried here would be acknowledged with the
    device untouched. Rolling the whole claim back leaves that authority where it was.

    Authority is read at BOTH ends, because a ``query_flag`` scope carries it in the request
    flag rather than in ids: a marked deletion folds to no ids at all, so a refusal reading
    only the folded ids would let the resync replace the adapter's stored rows while the
    removal job is suppressed, and the ordinary claim behind it would then render a body
    that no longer names the object and retract nothing.
    """
    device_id, scope = state.device_id, state.scope
    if deletions or untracked_mark:
        raise AuthorityPending(f"{device_id}/{scope} holds deletion authority a store-only request cannot carry")
    rendered = delivery.render(scope, device_id, mgmt.adapter_device_id)
    push_seq = allocate_push_seq()
    state.push_seq = push_seq
    state.claimed_at = now
    state.claim_payload = rendered.payload
    state.claim_identity = request_identity(
        rendered.payload, mode=delivery.MODE_STORE_ONLY, deletions=[], mark=None, epoch=mapping_epoch(mgmt)
    )
    state.claim_deletions = []
    state.claim_mark = None
    state.claim_flags = {"mode": delivery.MODE_STORE_ONLY, "mark_any": False, "force": bool(force)}
    state.save()
    return Claim(
        device_id=device_id,
        scope=scope,
        adapter_device_id=mgmt.adapter_device_id,
        push_seq=push_seq,
        payload=rendered.payload,
        identity=state.claim_identity,
        deletions=[],
        mark=None,
        mark_any=False,
        mode=delivery.MODE_STORE_ONLY,
        rendered=rendered,
    )


def _form_backfill(state, mgmt, now) -> Claim:
    """Take a claim that carries the owned snapshot and nothing else (§4.4).

    An explicit branch, never an implication of the mode: a backfill claim that ran the
    generic steps would fold the key's entries, move ``queued_deletions`` into
    ``claim_deletions`` and then take the adapter's 422 for carrying authority the mode
    forbids. It consumes no entry, so the real work is still owed after it succeeds.
    """
    rendered = delivery.render(state.scope, state.device_id, mgmt.adapter_device_id)
    push_seq = allocate_push_seq()
    state.push_seq = push_seq
    state.claimed_at = now
    state.claim_payload = rendered.payload
    state.claim_identity = request_identity(
        rendered.payload, mode=delivery.MODE_BACKFILL_ONLY, deletions=[], mark=None, epoch=mapping_epoch(mgmt)
    )
    state.claim_deletions = []
    state.claim_mark = None
    state.claim_flags = {"mode": delivery.MODE_BACKFILL_ONLY, "mark_any": False, "force": True}
    state.save()
    return Claim(
        device_id=state.device_id,
        scope=state.scope,
        adapter_device_id=mgmt.adapter_device_id,
        push_seq=push_seq,
        payload=rendered.payload,
        identity=state.claim_identity,
        deletions=[],
        mark=None,
        mark_any=False,
        mode=delivery.MODE_BACKFILL_ONLY,
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
                route_id = record.get("route_id")
                if record.get("op") == OP_REVOKE and route_id is not None and int(route_id) in held:
                    return True
    return False


def send_claim(claim: Claim, *, deadline=None):
    """Make the claim's HTTP call and return the adapter's answer.

    Returns a private control sentinel when the device is no longer managed or the pre-send
    scan found a revocation, in both cases without sending. Parking is not a
    third abandon cause: the operation keeps its rows, its authority and its sequence, and
    resumes when the device is managed again.
    """
    _refuse_in_transaction("send")
    from . import signals

    if not signals._device_is_managed(claim.device_id):
        logger.info("device %s is no longer NSO-managed, parking push_seq %s", claim.device_id, claim.push_seq)
        return _PARKED_SEND
    if revocation_hit(claim):
        logger.info("a revocation withdrew authority from push_seq %s, abandoning it", claim.push_seq)
        abandon(claim)
        return _ABANDONED_SEND
    return delivery.send(
        claim.rendered,
        claim.payload,
        mode=claim.mode,
        mark=bool(claim.mark),
        push_seq=claim.push_seq,
        deadline=SEND_DEADLINE.total_seconds() if deadline is None else deadline,
    )


def _send_clock() -> float:
    """Return the monotonic clock used for a shared send deadline."""
    return time.monotonic()


def _remaining_send_deadline(deadline_at) -> float:
    """Return this send's share of one drain deadline."""
    if deadline_at is None:
        return SEND_DEADLINE.total_seconds()
    remaining = deadline_at - _send_clock()
    if remaining <= 0:
        raise delivery.SendDeadlineExceeded("the shared send deadline expired before transport")
    return remaining


# ── The outcome ───────────────────────────────────────────────────────────────


#: The three lists that must partition a claim's requested set exactly (§4.4).
_ACK_LISTS = ("deleted_executed_ids", "deleted_degraded_ids", "deleted_moot_ids")


def _named_ids(response) -> list:
    """Every value *response* names in any acknowledgement list, verbatim."""
    if not isinstance(response, dict):
        return []
    return [value for name in _ACK_LISTS for value in (response.get(name) or [])]


def _route_ids(values) -> tuple[set[int] | None, object]:
    """Return the route ids *values* names, or ``(None, the offender)`` when one is not an id."""
    ids: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | str):
            return None, value
        try:
            ids.add(int(value))
        except ValueError:
            return None, value
    return ids, None


def _malformed_list(response) -> str:
    """Why *response*'s acknowledgement lists are not id lists at all, or "" (§4.4).

    Checked at the boundary, before any rule reads them: an id coerced downstream raised
    out of the outcome, which left the claim active and made the stored response poisonous
    for every replay of it.
    """
    if not isinstance(response, dict):
        return ""
    for name in _ACK_LISTS:
        values = response.get(name)
        if values is None:
            continue
        if not isinstance(values, list):
            return f"{name} is not a list"
        _ids, offender = _route_ids(values)
        if offender is not None:
            return f"{name} names {offender!r}, which is not a route id"
    return ""


def acknowledgement(claim: Claim, response) -> Acknowledgement:
    """Whether *response* acknowledges the claim's authority exactly (§4.4).

    A ``query_flag`` scope carries its authority in the request flag rather than in ids, so
    a successful marked full-replace IS the deletion and acknowledges everything it claimed.
    A ``per_object`` scope must answer with three lists that partition the requested set:
    unique within each, pairwise disjoint, no id the claim did not request, exact coverage.

    A claim carrying NO authority is the same rule at its boundary: a store-only or
    backfill-only request, and every request of a key with nothing queued, must be answered
    with no ids at all. A response naming one is naming an id the claim never requested.

    An id that is not an id at all is refused here too, before any rule reads the lists: it
    is a protocol violation like every other, never an exception out of the outcome.
    """
    malformed = _malformed_list(response)
    if malformed:
        return Acknowledgement(False, reason=malformed)
    requested = {int(record["route_id"]) for record in claim.deletions}
    if not requested:
        named = _named_ids(response)
        if named:
            ids = sorted(int(value) for value in named)
            return Acknowledgement(False, reason=f"the acknowledgement names unrequested ids {ids}")
        return Acknowledgement(True)
    if delivery.delivery_keys()[claim.scope].marking_mode != delivery.MARKING_PER_OBJECT:
        return Acknowledgement(True, frozenset(requested))
    if not isinstance(response, dict):
        return Acknowledgement(False, reason="the response carries no acknowledgement lists")
    seen: set[int] = set()
    for name in _ACK_LISTS:
        values = response.get(name)
        if not isinstance(values, list):
            return Acknowledgement(False, reason=f"{name} is missing")
        ids, _offender = _route_ids(values)
        if len(ids) != len(values):
            return Acknowledgement(False, reason=f"{name} repeats an id")
        if ids & seen:
            return Acknowledgement(False, reason="the acknowledgement lists overlap")
        seen |= ids
    if seen != requested:
        return Acknowledgement(False, reason="the acknowledgement does not cover the claim exactly")
    return Acknowledgement(True, frozenset(seen))


def _report_protocol_violation(claim: Claim, reason: str) -> None:
    """Surface a response that is not a partition of the claim, where an operator sees it.

    The state row already RECORDS it as ``ack_not_exact``. This is the reporting half: the
    per-scope rejection entry is what the device tab renders, and a response the plugin
    cannot validate has to read as a refusal rather than as silence.
    """
    from . import signals

    signals._record_push_outcome(
        claim.device_id,
        claim.scope,
        (signals.read_push_attempt(claim.device_id, claim.scope) or 0),
        ProtocolViolation(f"push_seq {claim.push_seq}: {reason}"),
    )


def _report_refusal(device_id, scope, exc) -> None:
    """Surface a push this key may not conclude, where an operator reads its other refusals.

    Two callers, one rule: the store-only claim that may not carry authority (§4.3(d)) and
    the direct-apply answer that reports a failed device write under HTTP 200 (§7.1). Neither
    is a silent drop: the caller reads a non-success, and the per-scope rejection entry is
    what the device tab renders.
    """
    from . import signals

    logger.warning("the %s/%s push was refused: %s", device_id, scope, exc)
    signals._record_push_outcome(device_id, scope, (signals.read_push_attempt(device_id, scope) or 0), exc)


def _degradations(state, claim: Claim, response, now) -> list[dict]:
    """Return the §4.3(c) records this response owes, most attributable first.

    Two sources, one shape. The adapter's ``deleted_degraded_ids`` names the ids it dropped
    to a detach under the ratified class. ``removed_uncorrelated`` names the triples of the
    NULL-``route_id`` rows the request removed that no requested id claimed, and those drive
    the request-wide conservative rule: a deletion still PENDING for this key whose lineage
    contains one of them, or whose lineage is ``unverified`` and matched nothing, is
    attributed to that removal rather than left to be mooted silently later.

    The triples of the rows actually removed are part of the record: a route id alone tells
    an operator nothing about what left the service.
    """
    if not isinstance(response, dict):
        return []
    removed = [triple for triple in (response.get("removed_uncorrelated") or []) if isinstance(triple, dict)]
    records = []
    degraded_ids = sorted({int(value) for value in (response.get("deleted_degraded_ids") or [])})
    if degraded_ids:
        records.append(_degradation(degraded_ids, removed or _lineages(claim.deletions, degraded_ids), now, claim))
    if removed:
        pending = [record for record in _pending_deletions(state, claim) if _uncorrelated_hit(record, removed)]
        if pending:
            records.append(_degradation(sorted(int(r["route_id"]) for r in pending), removed, now, claim))
    return records


def _pending_deletions(state, claim: Claim) -> list[dict]:
    """Every deletion this key still owes, read from BOTH homes and from its unconsumed entries.

    A backfill claim skips the fold and the ``queued_deletions`` move entirely (§4.4), so a
    deletion committing after the fence shut is in an entry and in neither home. Reading the
    state row alone left that one unattributed while the same pass pruned its NULL-``route_id``
    row, and the next ordinary claim then folded it after the row was gone: the adapter moots
    it and the detach is silent.

    The fold is the authority algebra's own answer, not a second one, so a revocation
    committed behind the deletion withdraws it here exactly as it would in a claim. Ids an
    in-flight claim already carries are excluded: the response classifies those.
    """
    transitions = [
        record
        for row in _unconsumed(state.device_id, state.scope).only("transitions").order_by("id")
        for record in row.transitions
    ]
    folded = fold_transitions(
        transitions,
        claim_deletions=[int(record["route_id"]) for record in claim.deletions],
        queued=state.queued_deletions,
        revoked=state.revoked_ids,
    )
    return list(folded.queued.values())


def _degradation(route_ids, triples, now, claim: Claim) -> dict:
    return {
        "route_ids": route_ids,
        "triples": triples,
        "at": now.isoformat(),
        "reason": PRE_FENCE_DETACH,
        "device": claim.device_id,
    }


def _lineages(deletions, route_ids) -> list[dict]:
    """Return the triples the claim carried for *route_ids*, when the response reported none."""
    wanted = set(route_ids)
    return [
        triple for record in deletions if int(record["route_id"]) in wanted for triple in (record.get("triples") or [])
    ]


def _uncorrelated_hit(record, removed) -> bool:
    """Whether a pending deletion is attributable to one of the rows this request removed.

    An exact lineage match is `authority:399`'s ratified class. So is an ``unverified``
    record that matches nothing, because the pass removed a row no id claimed and the
    conservative direction is to attribute the detach rather than to keep it silent.
    """
    triples = record.get("triples") or []
    if any(triple in removed for triple in triples):
        return True
    return bool(record.get("unverified"))


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
            _report_protocol_violation(claim, ack.reason)
            return UNACKNOWLEDGED

        if claim.mark is False and claim.mark_any:
            # The fold downgraded a marked contributor, which is today's cross-transaction
            # AND. It is recorded because the success path pops the transient error entry.
            state.degraded_deletions = [
                *state.degraded_deletions,
                {
                    "route_ids": sorted(ack.acknowledged),
                    # Each deletion holds a LINEAGE, and every reader of the record wants the
                    # triples themselves: a list of lists renders as no route at all.
                    "triples": _lineages(claim.deletions, ack.acknowledged),
                    "at": now.isoformat(),
                    "reason": LEGACY_MARK_DOWNGRADED,
                    "device": claim.device_id,
                },
            ]
        state.degraded_deletions = [*state.degraded_deletions, *_degradations(state, claim, response, now)]
        if claim.mode == delivery.MODE_BACKFILL_ONLY and state.fence_withheld_since is not None:
            # The one thing that lifts the withholding: the fence is open, so the deletion
            # the key is holding can be re-claimed at a NEW sequence and executed.
            logger.info("the fence opened for %s/%s; ordinary sends resume", claim.device_id, claim.scope)
            state.fence_withheld_since = None
        retired = NSOIntentOutboxEntry.objects.filter(
            device_id=claim.device_id, scope=claim.scope, consumed_by_push_seq=claim.push_seq
        )
        _retire(retired, list(retired.values_list("pk", flat=True)))
        state.revoked_ids = [int(route_id) for route_id in state.revoked_ids if int(route_id) not in ack.acknowledged]
        state.lineage_carry = {
            route_id: triple
            for route_id, triple in (state.lineage_carry or {}).items()
            if int(route_id) not in ack.acknowledged
        }
        _clear_claim(state)
        state.attempts = 0
        state.last_success_identity = claim.identity
        state.last_success_at = now
        state.save()
        _stamp_last_acked(claim)
    return SUCCEEDED


def _stamp_last_acked(claim: Claim) -> None:
    """Record the triple the adapter accepted, which is the only one a deletion can match.

    Stamped from the acknowledged claim's OWN payload, on a normal or store-only success,
    a receipt replay included, and on an exact acknowledgement that named degraded ids: the
    body was still what the adapter accepted. A BACKFILL-ONLY success never stamps — it
    adopts ids and generations and accepts no content, so stamping it would record an
    acknowledgement the adapter never gave.

    Not filtered on the overlay's current generation: a late acknowledgement whose overlay
    has since advanced still acknowledged the intermediate triple, and that is exactly the
    one the lineage exists to remember.
    """
    if claim.scope != "static_route" or claim.mode == delivery.MODE_BACKFILL_ONLY:
        return
    from .models import NSOStaticRouteState

    triples_by_route = {}
    for route in claim.payload or []:
        route_id = route.get("route_id")
        if route_id is None:
            continue
        triples_by_route[route_id] = triple_of(
            route.get("vrf") or "",
            route.get("prefix") or "",
            route.get("next_hop") or "",
        )
    rows = list(
        NSOStaticRouteState.objects.filter(
            management__device_id=claim.device_id,
            static_route_id__in=triples_by_route,
        ).only("static_route_id", "last_acked_triple")
    )
    for row in rows:
        row.last_acked_triple = triples_by_route[row.static_route_id]
    if rows:
        NSOStaticRouteState.objects.bulk_update(rows, ["last_acked_triple"])


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
    with transaction.atomic():
        state = _lock_state(claim.device_id, claim.scope)
        if state.push_seq != claim.push_seq:
            return SUPERSEDED
        _abandon_locked(state)
    return ABANDONED


def _abandon_locked(state) -> None:
    """Rehome one locked claim without opening another transaction."""
    from .models import NSOIntentOutboxEntry

    NSOIntentOutboxEntry.objects.filter(
        device_id=state.device_id, scope=state.scope, consumed_by_push_seq=state.push_seq
    ).update(consumed_by_push_seq=None)
    queued = {int(record["route_id"]): record for record in state.queued_deletions}
    revoked = {int(route_id) for route_id in state.revoked_ids or []}
    for record in state.claim_deletions or []:
        route_id = int(record["route_id"])
        if route_id not in revoked:
            queued.setdefault(route_id, record)
    state.queued_deletions = list(queued.values())
    _clear_claim(state)
    state.save()


# ── The direct-apply keys, which are out of protocol (Rev 15 split, §7.1) ─────

#: The entries were consumed and the body is rendered: this attempt owes a send.
_SENDING = "sending"


def _deliver_direct(device_id, scope, *, mode, force, deadline_at) -> tuple[str, object]:
    """Deliver an out-of-protocol key: coalesced like every other, and claim-less (O-P12c).

    ``lacp`` and ``switchport`` write to NSO synchronously inside the request, so no receipt
    can be atomic with their effect and no sequence may name their operation: a takeover
    would replay the body into a SECOND device write. They take no sequence, no lease and no
    takeover, and nothing here retires authority on their behalf.

    What survives is the coalescing: one fold, one send per burst. The entries are consumed
    on the attempt, so the retry semantics are today's rather than the claim's, and a failure
    lands in the push journal exactly as today's direct call leaves it. §7.1's card owns
    joining these two to the protocol.
    """
    outcome, prepared = _repeatable_read(lambda: _take_direct_entries(device_id, scope, force))
    if prepared is None:
        return outcome, None
    rendered, mark = prepared
    try:
        answer = delivery.send(
            rendered,
            rendered.payload,
            mode=mode,
            mark=bool(mark),
            deadline=_remaining_send_deadline(deadline_at),
        )
    except Exception as exc:  # noqa: BLE001 (the send records it; nothing replays a device write)
        logger.warning("the %s/%s apply failed: %s", device_id, scope, exc)
        return FAILED, None
    refusal = _apply_envelope_error(answer)
    if refusal:
        _report_refusal(device_id, scope, DirectApplyFailed(refusal))
        return FAILED, None
    return SUCCEEDED, answer


def _take_direct_entries(device_id, scope, force) -> tuple[str, tuple | None]:
    """Consume the key's unconsumed entries and render its body, under the state-row lock.

    The lock is what makes two workers' bursts one send, and the entries are retired here
    rather than in an outcome: an operation nothing can replay has no outcome transaction to
    retire them in, and a row left behind would be re-sent to the device by the next pass.
    """
    from .models import NSODeviceManagement, NSOIntentOutboxEntry

    state = _lock_state(device_id, scope)
    state.last_drain_attempted_at = _db_now()
    state.save()
    mgmt = (
        NSODeviceManagement.objects.select_for_update(of=("self",))
        .order_by()
        .filter(device_id=device_id, adapter_device_id__isnull=False)
        .first()
    )
    if mgmt is None:
        return PARKED, None  # unmanaged or unlinked: the entries keep, as the claim path parks
    rows = list(_unconsumed(device_id, scope).select_for_update().order_by("id"))
    if not rows and not force:
        return NOTHING, None
    rendered = delivery.render(scope, device_id, mgmt.adapter_device_id)
    entry_ids = [row.pk for row in rows]
    _retire(NSOIntentOutboxEntry.objects.filter(id__in=entry_ids), entry_ids)
    return _SENDING, (rendered, all(row.mark_and for row in rows) if rows else None)


def _apply_envelope_error(answer) -> str:
    """Why a direct-apply answer reports a failed device write, or "" when it does not.

    Both endpoints answer a failed apply with HTTP 200 and a ``{"status": "error"}`` envelope
    (§7.1), so the transport cannot tell it from a success. The status codes and the rest of
    the taxonomy belong to the split card; what belongs here is that such an answer is
    recorded as the failure it is.
    """
    if isinstance(answer, dict) and answer.get("status") == "error":
        return str(answer.get("message") or answer.get("detail") or "the adapter reported a failed apply")
    return ""


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


def _restamp_attempt(device_id, scope) -> None:
    """Stamp a key whose drain RAISED, in a transaction of its own (O1.33).

    The claim stamps itself before anything can refuse, but that write rolls back with the
    transaction it belongs to, so a key raising inside the claim (a render that blows up, a
    fold that hits a non-retryable error) leaves no stamp at all and stays at the head of
    every bounded pass, starving the keys behind it. The compact-retry path already
    re-stamps for the same reason.
    """
    try:
        _stamp_attempt(device_id, scope)
    except Exception:  # noqa: BLE001 (a key that cannot even be stamped must not abort the pass)
        logger.exception("intent outbox attempt stamp failed for %s/%s", device_id, scope)


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


def drain_key(
    device_id,
    scope,
    *,
    mode=delivery.MODE_NORMAL,
    force=False,
    chain=DRAIN_CHAIN_MAX,
    reform=1,
    deadline=None,
) -> str:
    """Claim, send and settle one key, then chain a bounded number of further drains."""
    outcome, _answer = _drain_once(
        device_id, scope, mode=mode, force=force, chain=chain, reform=reform, deadline=deadline
    )
    return outcome


def push_now(device_id, scope, *, mode=delivery.MODE_NORMAL, force=False, deadline=None):
    """Drain one key synchronously and return the adapter's answer, or ``None``.

    The forced-push sites use this: a forced call is its own logical operation, never
    coalesced with a queued claim and never dropped as digest-equal, and several of them
    read the answer (the Apply's promotion gate, the fleet resync's stored count). ``None``
    means the operation was not acknowledged, which is what those callers act on.
    """
    outcome, answer = _drain_once(device_id, scope, mode=mode, force=force, deadline=deadline)
    return answer if outcome == SUCCEEDED else None


def _drain_once(
    device_id,
    scope,
    *,
    mode,
    force,
    chain=DRAIN_CHAIN_MAX,
    reform=1,
    deadline=None,
    _deadline_at=None,
) -> tuple[str, object]:
    """Run one claim/send/outcome cycle, returning ``(outcome, the adapter's answer)``."""
    _refuse_in_transaction("drain")
    if deadline is not None and _deadline_at is None:
        _deadline_at = _send_clock() + deadline
    if not delivery.delivery_keys()[scope].in_protocol:
        return _deliver_direct(device_id, scope, mode=mode, force=force, deadline_at=_deadline_at)
    # The mode THIS attempt may send in. The caller's own mode is what a re-form or a chain
    # resumes with, so a fence that opens mid-chain goes back to delivering, not backfilling.
    send_mode = _withheld_mode(device_id, scope, mode)
    try:
        claimed, timed_out = _claim_or_compact(device_id, scope, mode=send_mode, force=force)
    except AuthorityPending as refusal:
        _record_claim_refusal(device_id, scope, refusal)
        _report_refusal(device_id, scope, refusal)
        return REFUSED, None
    if timed_out:
        return FAILED, None
    if claimed is None:
        return NOTHING, None
    try:
        answer = send_claim(claimed, deadline=_remaining_send_deadline(_deadline_at))
    except Exception as exc:  # noqa: BLE001 (the operation is replayed, so the failure is data)
        logger.warning("push_seq %s failed for %s/%s: %s", claimed.push_seq, device_id, scope, exc)
        if _proven_no_effect(exc):
            return _withhold(claimed, exc), None
        if _rejected_at_boundary(exc):
            return _dissolve(claimed, exc), None
        return record_failure(claimed, exc), None
    if answer is _ABANDONED_SEND and reform > 0:
        # A fixed revocation resolves in ONE re-form: the re-form folds the revoking entry
        # with everything else, so the authority no longer names that route. A revocation
        # committing during the re-form is the accepted OQ-O-7 residual, not a loop.
        return _drain_once(
            device_id,
            scope,
            mode=mode,
            force=force,
            chain=chain,
            reform=reform - 1,
            deadline=deadline,
            _deadline_at=_deadline_at,
        )
    if answer is _PARKED_SEND:
        return PARKED, None
    if answer is _ABANDONED_SEND:
        return ABANDONED, None
    outcome = settle(claimed, answer)
    if outcome == SUCCEEDED:
        continued = _after_success(
            claimed,
            mode=mode,
            force=force,
            chain=chain,
            deadline=deadline,
            deadline_at=_deadline_at,
        )
        if continued is not None:
            return continued
    return outcome, answer


def _after_success(claimed, *, mode, force, chain, deadline, deadline_at):
    """Resolve any operation still owed after a successful preparatory pass."""
    device_id, scope = claimed.device_id, claimed.scope
    if _answered_other_work(claimed, mode, force):
        if chain <= 0:
            logger.info(
                "%s/%s settled a preparatory operation after the chain budget expired",
                device_id,
                scope,
            )
            return NOTHING, None
        # What settled was somebody else's operation. This call's own mode and force are
        # still owed, so only the next claim's result can answer this call.
        return _drain_once(
            device_id,
            scope,
            mode=mode,
            force=force,
            chain=chain - 1,
            deadline=deadline,
            _deadline_at=deadline_at,
        )
    if chain > 0 and mode == delivery.MODE_NORMAL and _pending(device_id, scope):
        # This chain is a latency optimization. The tick guarantees any remaining tail.
        _drain_once(
            device_id,
            scope,
            mode=mode,
            force=False,
            chain=chain - 1,
            deadline=deadline,
            _deadline_at=deadline_at,
        )
    return None


def _answered_other_work(claim: Claim, mode, force) -> bool:
    """Whether the settled claim was not the operation this call asked for (§4.2).

    Request mode is per call, so two passes settle something else and both are preparatory.
    A TAKEOVER replays the unacknowledged operation with the body, the authority and the mode
    it was admitted under, so it can never answer a call that asked for a different mode or
    forced an operation of its own. A fence-withheld key SUBSTITUTES the backfill-only claim
    that opens the fence (§4.3(c)), which by definition delivers nothing: reporting its
    success as the caller's would promote static routes to ``deploying`` on a pass that
    carried no intent at all.
    """
    return claim.mode != mode or (claim.replayed and force)


def _withheld_mode(device_id, scope, mode) -> str:
    """Return the mode this key may actually send in, which a shut fence overrides (§4.3(c)).

    A fence-withheld key owes exactly one send: the backfill-only pass that opens the fence.
    Scheduling it is what bounds the withheld state, so the drain runs it in place of the
    ordinary claim rather than leaving the key stalled until an operator intervenes.
    """
    from .models import NSOIntentOutboxState

    if mode == delivery.MODE_BACKFILL_ONLY:
        return mode
    withheld = NSOIntentOutboxState.objects.filter(
        device_id=device_id, scope=scope, fence_withheld_since__isnull=False
    ).exists()
    if not withheld:
        return mode
    logger.info("%s/%s is fence-withheld; draining the backfill-only claim instead", device_id, scope)
    return delivery.MODE_BACKFILL_ONLY


def _proven_no_effect(exc) -> bool:
    """Whether the adapter rejected the request BEFORE any effect, which lets it be abandoned.

    Post-send abandonment is otherwise forbidden: only the receipt can say whether the far
    side applied the operation. These codes are the exception the adapter proves, by
    unwinding through its claim guard's rollback ahead of its single commit.
    """
    detail = getattr(exc, "detail", None)
    detail = detail if isinstance(detail, dict) else {}
    return bool({getattr(exc, "code", None), detail.get("code"), detail.get("reason")} & set(PROVEN_NO_EFFECT))


def _rejected_at_boundary(exc) -> bool:
    """Whether the adapter refused the BODY, which is deterministic and had no effect.

    The refusal is raised before the request's first statement and repeats for the same
    bytes, so replaying it is not a retry: it is the same rejection, forever.
    """
    return getattr(exc, "code", None) == BOUNDARY_REJECTION


def _dissolve(claim: Claim, exc) -> str:
    """Abandon a refused body so the next claim folds the operator's correction.

    §4.2's proven-no-effect abandon, without the withholding the fence adds: a shut fence is
    a condition of the DEVICE that one backfill lifts, while an invalid body is a condition
    of the request that only an operator edit changes. Replaying it would take that edit over
    at the burned body on every later drain, so the correction could never reach the wire.
    """
    abandon(claim)
    with transaction.atomic():
        state = _lock_state(claim.device_id, claim.scope)
        state.last_error_code = str(getattr(exc, "code", "") or type(exc).__name__)[:64]
        state.last_error_at = _db_now()
        state.save()
    logger.warning("%s/%s refused push_seq %s at the boundary: %s", claim.device_id, claim.scope, claim.push_seq, exc)
    return REJECTED


def _record_claim_refusal(device_id, scope, exc) -> None:
    """Record a refusal whose claim transaction rolled back."""
    with transaction.atomic():
        state = _lock_state(device_id, scope)
        now = _db_now()
        state.attempts += 1
        state.last_drain_attempted_at = now
        state.last_error_code = str(getattr(exc, "code", "") or type(exc).__name__)[:64]
        state.last_error_at = now
        state.save()


def _withhold(claim: Claim, exc) -> str:
    """Abandon a proven-no-effect rejection and withhold the key's ordinary sends.

    The sequence is burned and the authority returns WHOLE to ``queued_deletions``, so the
    deletion survives and the ids can still be cross-checked against what a later pass
    reports removing. Only the backfill-only claim's acknowledged success lifts this.
    """
    abandon(claim)
    with transaction.atomic():
        state = _lock_state(claim.device_id, claim.scope)
        now = _db_now()
        if state.fence_withheld_since is None:
            state.fence_withheld_since = now
        state.last_error_code = str(getattr(exc, "code", "") or type(exc).__name__)[:64]
        state.last_error_at = now
        state.save()
    logger.warning("%s/%s is fence-withheld: %s", claim.device_id, claim.scope, exc)
    return WITHHELD


def degraded_deletions(device_id) -> list[dict]:
    """Return one rendered record per degradation recorded for *device_id*, newest first.

    The operator surface of §4.3(c). The record is durable and no push outcome clears it, so
    the device tab renders it as its own banner until the acknowledgement command clears it.
    A pure read of the state rows: it takes no lock and never touches the adapter, so it
    renders on a tab whose adapter is down exactly as on one whose adapter is up.
    """
    from django.utils.dateparse import parse_datetime

    from .models import NSOIntentOutboxState

    labels = delivery.delivery_keys()
    rendered = []
    rows = NSOIntentOutboxState.objects.filter(device_id=device_id).exclude(degraded_deletions=[])
    for state in rows:
        entry = labels.get(state.scope)
        for record in state.degraded_deletions:
            at = record.get("at") or ""
            rendered.append(
                {
                    "scope": state.scope,
                    "label": entry.label if entry else state.scope,
                    "route_ids": record.get("route_ids") or [],
                    "triples": [triple for triple in (record.get("triples") or []) if isinstance(triple, dict)],
                    "reason": DEGRADED_REASONS.get(record.get("reason"), record.get("reason") or "unknown"),
                    "at": parse_datetime(at) if at else None,
                    "sort_key": at,
                }
            )
    rendered.sort(key=lambda row: row["sort_key"], reverse=True)
    return rendered


def acknowledge_degraded_deletions(device_id=None, scope=None) -> list[tuple[int, str, list]]:
    """Clear the durable degradation records, and return exactly the ones cleared, per key.

    The ONLY thing that clears them. A push outcome never does: the transient per-scope
    error entry is popped by the very next success (`signals.py`), so a degradation
    recorded there would be erased before an operator could read it.

    Reading and clearing under the key's own state-row lock is what makes the returned set
    the whole set: every write of the field takes that lock, so a degradation recorded
    while the acknowledgement runs waits behind it and survives, uncleared and unreported.
    Reporting the return value is therefore reporting exactly what was cleared, which a
    caller that listed the records itself and then cleared blindly could not promise.
    """
    from .models import NSOIntentOutboxState

    acknowledged = []
    with transaction.atomic():
        rows = NSOIntentOutboxState.objects.select_for_update(of=("self",)).order_by().exclude(degraded_deletions=[])
        if device_id is not None:
            rows = rows.filter(device_id=device_id)
        if scope is not None:
            rows = rows.filter(scope=scope)
        for state in rows:
            acknowledged.append((state.device_id, state.scope, list(state.degraded_deletions)))
            state.degraded_deletions = []
            state.save(update_fields=["degraded_deletions"])
    return acknowledged


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
    from django.db.models import F, OuterRef, Q, Subquery

    from .models import NSOIntentOutboxEntry, NSOIntentOutboxState

    limit = DRAIN_BATCH if limit is None else limit
    fresh = _db_now() - LEASE
    state_for_entry = NSOIntentOutboxState.objects.filter(
        device_id=OuterRef("device_id"),
        scope=OuterRef("scope"),
    ).order_by()
    entry_keys = list(
        NSOIntentOutboxEntry.objects.filter(
            consumed_by_push_seq__isnull=True,
            device__nso_management__adapter_device_id__isnull=False,
        )
        .order_by()
        .values("device_id", "scope")
        .annotate(
            claimed_at=Subquery(state_for_entry.values("claimed_at")[:1]),
            attempted_at=Subquery(state_for_entry.values("last_drain_attempted_at")[:1]),
        )
        .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lte=fresh))
        .order_by(F("attempted_at").asc(nulls_first=True), "device_id", "scope")
        .distinct()[:limit]
    )
    state_keys = list(
        NSOIntentOutboxState.objects.filter(device__nso_management__adapter_device_id__isnull=False)
        .filter(Q(push_seq__isnull=False) | ~Q(queued_deletions=[]))
        .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lte=fresh))
        .order_by(F("last_drain_attempted_at").asc(nulls_first=True), "device_id", "scope")
        .values("device_id", "scope", attempted_at=F("last_drain_attempted_at"))[:limit]
    )

    by_key = {}
    for row in [*entry_keys, *state_keys]:
        key = (row["device_id"], row["scope"])
        by_key[key] = row["attempted_at"]
    ordered = [(attempted is not None, attempted or datetime.datetime.min, key) for key, attempted in by_key.items()]
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))
    return [key for _stamped, _at, key in ordered[:limit]]


def compact_intent_outbox(limit=None) -> None:
    """Compact the tick's bounded candidate set without contacting the adapter."""
    for device_id, scope in compaction_candidates(limit):
        try:
            compact(device_id, scope)
        except Exception:  # noqa: BLE001 (one key's compaction must not abort the fleet pass)
            logger.exception("intent outbox compaction failed for %s/%s", device_id, scope)


def drain_intent_outbox(limit=None) -> tuple[int, int]:
    """Appendix O's tick pass. Returns ``(drained, failed)``.

    Bounded, candidate-filtered and starvation-free: a key is attempted only when it owes a
    send, the stamp on every attempt rotates a replayably failing key to the back, and one
    key's failure aborts neither the pass nor the tick.

    Compaction runs first, over its OWN candidates: a key whose claim is stuck on a
    replayable failure is not drainable and would never be compacted otherwise, which is
    exactly the case where a burst accumulates.
    """
    compact_intent_outbox(limit)

    drained = failed = 0
    for device_id, scope in drain_candidates(limit):
        try:
            outcome = drain_key(device_id, scope)
        except Exception:  # noqa: BLE001 (one key's adapter must not abort the fleet pass)
            logger.exception("intent outbox drain failed for %s/%s", device_id, scope)
            _restamp_attempt(device_id, scope)
            failed += 1
            continue
        if outcome == SUCCEEDED:
            drained += 1
        elif outcome in (FAILED, UNACKNOWLEDGED):
            failed += 1
    return drained, failed


# ── The restore ───────────────────────────────────────────────────────────────

RESTORE_SETTLED = "settled"
RESTORE_REBASED = "rebased"
RESTORE_FAILED_CLOSED = "failed_closed"
RESTORE_REPLAY = "replay"


def _sent_wire_digest(state) -> str:
    """Digest the body the unresolved claim put on the wire, from the row that recorded it.

    The claim persists the payload it sent, and the envelope around it (``{"routes": …}``)
    belongs to the scope's endpoint, so the two together reproduce the exact body the adapter
    digested into its receipt. The render is for the endpoint's own call, never for its
    payload: this hashes what was sent, not what the device would render now.
    """
    rendered = delivery.render(state.scope, state.device_id, 0)
    return wire_digest(delivery.wire_body(rendered, state.claim_payload))


def resolve_restored_claim(device_id, scope, receipt) -> str:
    """Resolve one restored claim against the adapter's receipt for its key (§4.6).

    *receipt* is what ``GET /api/v1/intent-receipts`` returned for this key, or ``None``.
    Four cases, and the first is why this exists rather than being folded into the outcome
    path: a same-sequence, same-digest receipt is settled only if its stored response is a
    real PARTITION of the claim, validated by the SAME check the outcome path runs. A union
    test would settle a response in which one id is both executed and degraded, and that
    contradiction then drives both the degradation record and this decision.
    """
    _refuse_in_transaction("restore")
    from .models import NSOIntentOutboxEntry

    accepted = None
    if receipt is not None:
        if not isinstance(receipt, dict):
            return RESTORE_FAILED_CLOSED
        accepted = receipt.get("accepted_push_seq")
        if isinstance(accepted, bool) or not isinstance(accepted, int) or accepted < 1:
            return RESTORE_FAILED_CLOSED

    while True:
        with transaction.atomic():
            state = _lock_state(device_id, scope)
            if state.push_seq is None:
                return RESTORE_REPLAY
            if accepted is None or accepted < state.push_seq:
                return RESTORE_REPLAY  # the far side never saw it; the ordinary replay carries it
            if accepted > state.push_seq:
                issued = issued_push_seq()
                if accepted - issued > MAX_RESTORE_GAP:
                    logger.error(
                        "%s/%s receipt push_seq %s is %s values ahead of the local sequence",
                        device_id,
                        scope,
                        accepted,
                        accepted - issued,
                    )
                    return RESTORE_FAILED_CLOSED
                # The sequence moves first. A rebase under a rewound sequence would hand the
                # next claim a stale id that the adapter refuses forever. End this transaction
                # after each bounded step so another operation can lock the key between steps.
                if advance_push_seq(accepted) < accepted:
                    continue

                # Preserve the authority and return the rows to unconsumed so a later claim
                # refolds them. Revoked ids stay revoked because re-ownership outlives a restore.
                revoked = {int(route_id) for route_id in state.revoked_ids or []}
                queued = {int(record["route_id"]): record for record in state.queued_deletions}
                for record in state.claim_deletions or []:
                    if int(record["route_id"]) not in revoked:
                        queued.setdefault(int(record["route_id"]), record)
                NSOIntentOutboxEntry.objects.filter(
                    device_id=device_id, scope=scope, consumed_by_push_seq=state.push_seq
                ).update(consumed_by_push_seq=None)
                state.queued_deletions = list(queued.values())
                _clear_claim(state)
                state.save()
                return RESTORE_REBASED
            if receipt.get("request_digest") != _sent_wire_digest(state):
                logger.error(
                    "%s/%s holds push_seq %s at a digest the receipt does not name",
                    device_id,
                    scope,
                    state.push_seq,
                )
                return RESTORE_FAILED_CLOSED
            break

    # Rebuilt from the row alone: nothing is sent, so the claim needs no adapter id.
    restored = Claim(
        device_id=device_id,
        scope=scope,
        adapter_device_id=0,
        push_seq=state.push_seq,
        payload=state.claim_payload,
        identity=state.claim_identity,
        deletions=list(state.claim_deletions or []),
        mark=state.claim_mark,
        mark_any=bool((state.claim_flags or {}).get("mark_any")),
        mode=(state.claim_flags or {}).get("mode", delivery.MODE_NORMAL),
        rendered=None,
        replayed=True,
    )
    if settle(restored, receipt.get("stored_response")) != SUCCEEDED:
        return RESTORE_FAILED_CLOSED
    return RESTORE_SETTLED


def clear_acknowledged_lineage() -> int:
    """Forget every acknowledged triple, which a restored database has no right to claim.

    A plugin-only restore can believe it holds ``{A, A}`` while the adapter is at C, so the
    lineage theorem's scope (tracked-era, non-restored rows) stops holding for every overlay
    at once. NULL is not a gap here: it IS the wire's ``unverified`` flag, and saying so is
    what keeps a later deletion attributable instead of silently moot.
    """
    from .models import NSOStaticRouteState

    return NSOStaticRouteState.objects.exclude(last_acked_triple=None).update(last_acked_triple=None)


def _clear_claim(state) -> None:
    """Drop the claim fields. The sequence is burned; it is never reissued."""
    state.claim_deletions = []
    state.push_seq = None
    state.claimed_at = None
    state.claim_payload = None
    state.claim_identity = ""
    state.claim_flags = {}
    state.claim_mark = None


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
    for entry_device, scope in (
        rows.filter(consumed_by_push_seq__isnull=True).order_by().values_list("device_id", "scope").distinct()
    ):
        blockers.append(f"{entry_device}/{scope}: an unconsumed entry")
    for entry_device, scope in (
        rows.filter(consumed_by_push_seq__isnull=False).order_by().values_list("device_id", "scope").distinct()
    ):
        blockers.append(f"{entry_device}/{scope}: a row carrying a push_seq")
    for state in states:
        key = f"{state.device_id}/{state.scope}"
        if state.push_seq is not None:
            # A claim that consumed no row leaves nothing else behind, and a forced or
            # store-only one never consumes any: the sequence alone says the far side may
            # still hold an operation this key has not resolved.
            blockers.append(f"{key}: an unacknowledged operation")
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
