# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O — the durable push outbox: what an operator transaction records.

The in-memory coalescer loses a scheduled push on rollback and on failure, and the
authority to DELETE an object is the one thing that may never be lost. So a scheduled push
becomes a row: ``NSOIntentOutboxEntry`` is appended by the operator's own transaction, so
the database decides what survives a rollback, a savepoint rollback and a crash.

This module owns the write path and the authority algebra over what it recorded. The claim
protocol, compaction and the drain read these records; none of them is here.
"""

from __future__ import annotations

import dataclasses
import threading

#: Plugin-global, created by Appendix O's migration. Names a logical operation, never an
#: attempt, and never wraps: a re-issued value would let the adapter admit a replay as new.
PUSH_SEQ_SEQUENCE = "nso_intent_push_seq"
PUSH_SEQ_ADVANCE_BATCH = 10_000

OP_DELETE = "delete"
OP_REVOKE = "revoke"


def allocate_push_seq() -> int:
    """Return the next logical-operation id (strictly increasing, never reused)."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT nextval('{PUSH_SEQ_SEQUENCE}')")  # noqa: S608 — a module constant, not input
        return int(cursor.fetchone()[0])


def advance_push_seq(watermark: int) -> int:
    """Move the sequence toward *watermark*, and return the highest value it proved issued.

    A restored database brings the sequence back rewound with it, so the far side can already
    hold operations above it. The move is made by ``nextval`` and nothing else: each call is
    atomic and strictly forward, so a concurrent allocation can only carry the sequence
    further and can never be undone by this one. ``setval`` cannot promise that. It reads
    ``last_value`` and writes without holding the sequence across the two, so a ``nextval``
    landing between them is erased and its value is handed out a SECOND time, which the far
    side refuses as a reused sequence for the life of the key, past every retry.

    Each call burns at most ``PUSH_SEQ_ADVANCE_BATCH`` values. The caller can release its
    own locks between calls instead of holding them through an attacker-sized watermark
    gap. The values walked past are never reissued, and the sequence is BIGINT NO CYCLE, so
    a rare restore may overshoot for free. A watermark the sequence has already passed takes
    nothing at all. Sequences are not transactional, so this survives a rollback of the
    caller's transaction, which is the safe direction.
    """
    from django.db import connection

    watermark = int(watermark)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT last_value, is_called FROM {PUSH_SEQ_SEQUENCE}")  # noqa: S608 (a constant)
        last_value, is_called = cursor.fetchone()
        # A sequence not yet called has handed out nothing: last_value is the NEXT value.
        issued = int(last_value) if is_called else int(last_value) - 1
        if issued >= watermark:
            return issued
        step = min(watermark - issued, PUSH_SEQ_ADVANCE_BATCH)
        cursor.execute(
            f"SELECT max(nextval('{PUSH_SEQ_SEQUENCE}')) FROM generate_series(1, %s)",  # noqa: S608
            [step],
        )
        return int(cursor.fetchone()[0])


# ── Transition records: the provenance an entry carries ───────────────────────


def triple_of(vrf: str, prefix: str, next_hop: str) -> dict:
    """Build the identity the adapter matches a route by."""
    return {"vrf": vrf or "", "prefix": prefix or "", "next_hop": next_hop or ""}


def canonical_lineage(last_acked: dict | None, current: dict | None) -> list[dict]:
    """``[last_acked, current]``, deduplicated in that order, most-authoritative first.

    The adapter holds either the last triple it acknowledged or the one an unresolved claim
    may have delivered, so the lineage is at most two long. Order is significant: a content
    edit whose push never landed leaves the adapter on the older triple, and an id carrying
    only the current one would match nothing and be classified moot.
    """
    lineage = [t for t in (last_acked, current) if t]
    if len(lineage) == 2 and lineage[0] == lineage[1]:
        return lineage[:1]
    return lineage


def delete_transition(route_id: int, *, last_acked: dict | None, current: dict | None) -> dict:
    """Record a deletion while the overlay mirror is still alive — the last moment it exists."""
    return {
        "op": OP_DELETE,
        "route_id": route_id,
        "triples": canonical_lineage(last_acked, current),
        # Declared, never inferred from the lineage's shape: a verified ``[C, C]``
        # deduplicates to exactly what an unverified ``[C]`` produces.
        "unverified": last_acked is None,
    }


def revoke_transition(route_id: int, *, carried_triple: dict | None = None) -> dict:
    """Record a re-ownership: it withdraws any deletion authority pending for that pk."""
    record = {"op": OP_REVOKE, "route_id": route_id}
    if carried_triple:
        record["carried_triple"] = carried_triple
    return record


@dataclasses.dataclass
class FoldedAuthority:
    """The authority a set of transitions amounts to, in the two homes of §4.3(b)."""

    queued: dict = dataclasses.field(default_factory=dict)
    revoked: set = dataclasses.field(default_factory=set)
    lineage_carry: dict = dataclasses.field(default_factory=dict)


def fold_transitions(transitions, *, claim_deletions=(), queued=(), revoked=(), lineage_carry=None) -> FoldedAuthority:
    """Apply the authority algebra to *transitions*, in entry-id order.

    ``claim_deletions`` are the ids an in-flight claim already carries: a deletion of one of
    those is not new authority but the WITHDRAWAL of a revocation, and a re-ownership of one
    of them is a revocation the pre-send check must see. Same-route transitions serialize on
    the route's own locks, so entry-id order is commit order for a route and the fold needs
    no transaction identity.

    ``queued``, ``revoked`` and ``lineage_carry`` seed the fold with what the state row
    already holds, because the algebra is over both homes: a re-ownership must discard a
    deletion queued by an earlier fold exactly as it discards one from this batch, and a
    carried triple is cleared by an acknowledged success alone.
    """
    held = set(claim_deletions)
    folded = FoldedAuthority(
        queued={int(record["route_id"]): record for record in queued},
        revoked={int(route_id) for route_id in revoked},
        lineage_carry={int(route_id): triple for route_id, triple in (lineage_carry or {}).items()},
    )
    for record in transitions:
        route_id = record.get("route_id")
        if route_id is None:
            continue
        if record.get("op") == OP_DELETE:
            folded.revoked.discard(route_id)
            if route_id not in held:
                folded.queued[route_id] = record
        elif record.get("op") == OP_REVOKE:
            discarded = folded.queued.pop(route_id, None)
            carried = record.get("carried_triple") or _last_acked_of(discarded)
            if carried:
                folded.lineage_carry[route_id] = carried
            if route_id in held:
                folded.revoked.add(route_id)
    return folded


def carried_triple(route_id, *, transitions=(), queued=(), claim_deletions=(), lineage_carry=None) -> dict | None:
    """Return the acknowledged triple a re-ownership of *route_id* inherits (§4.3(b)).

    Removal deletes the overlay and re-ownership creates a fresh one, so the pending
    deletion record is the only history there is. Reading it here is what makes a
    delete/re-own/re-delete cycle carry ``[A, C]`` instead of ``[C]``, which is the
    difference between the adapter matching the row it holds and calling the id moot while
    silently detaching it.

    A pure read: it applies the algebra to a synthetic revocation and takes the carry that
    transition would produce, so the answer is the fold's answer rather than a second one.
    Both authority homes are read, because the record may sit in either.
    """
    folded = fold_transitions(
        [*transitions, revoke_transition(route_id)],
        queued=[*queued, *claim_deletions],
        lineage_carry=lineage_carry,
    )
    return folded.lineage_carry.get(route_id)


def _last_acked_of(record) -> dict | None:
    """Read the acknowledged triple a deletion record carries, or ``None`` when unverified."""
    if not record or record.get("unverified"):
        return None
    triples = record.get("triples") or []
    return triples[0] if triples else None


def _carried_by(record) -> dict | None:
    """Read the acknowledged triple *record* hands to a revocation folded on top of it."""
    if record is None:
        return None
    if record.get("op") == OP_REVOKE:
        return record.get("carried_triple")
    return _last_acked_of(record)


def reduce_transitions(transitions) -> list:
    """Reduce *transitions* to at most one per route, applying the algebra rather than dropping it.

    A route's contribution is determined by its LAST transition plus one fold-time constant
    (whether an active claim holds it), so everything earlier for that route is dead —
    everything except the lineage a revocation carries. Reducing ``delete R(record A),
    revoke R`` to a bare revoke would discard A, and the ``[A, C]`` lineage a later deletion
    of a re-owned pk needs could never form, so the surviving revoke carries A forward.

    Transitions for different routes commute, so their relative order is not preserved and
    does not need to be. A record naming no route is kept verbatim: a rewrite may reduce, it
    may not lose.
    """
    survivors: dict = {}
    unkeyed: list = []
    for record in transitions:
        route_id = record.get("route_id")
        if route_id is None:
            unkeyed.append(record)
            continue
        if record.get("op") == OP_REVOKE:
            carried = record.get("carried_triple") or _carried_by(survivors.get(route_id))
            record = {**record, "carried_triple": carried} if carried else record
        survivors[route_id] = record
    return [*survivors.values(), *unkeyed]


# ── The write path ────────────────────────────────────────────────────────────
#
# A device being torn down must not be given new entries. The rows would be inserted after
# Django's collector took its snapshot of what to cascade, and PostgreSQL defers the foreign
# key to COMMIT, so the teardown would fail there; more to the point, an overlay cascade is
# NetBox-side bookkeeping and carries no operator intent at all.

_teardown = threading.local()


def _teardown_marks() -> dict:
    marks = getattr(_teardown, "marks", None)
    if marks is None:
        marks = {}
        _teardown.marks = marks
    return marks


def mark_device_teardown(device_id, txid: int) -> None:
    """Count one in-progress deletion of *device_id* (or of its management row)."""
    marks = _teardown_marks()
    held_txid, count = marks.get(device_id, (txid, 0))
    marks[device_id] = (txid, count + 1) if held_txid == txid else (txid, 1)


def clear_device_teardown(device_id, txid: int) -> None:
    """Release one mark. A mark left behind by a failed teardown expires with its txid."""
    marks = _teardown_marks()
    held_txid, count = marks.get(device_id, (None, 0))
    if held_txid != txid or count <= 1:
        marks.pop(device_id, None)
    else:
        marks[device_id] = (held_txid, count - 1)


def _device_is_tearing_down(device_id, txid: int) -> bool:
    marks = getattr(_teardown, "marks", None)
    if not marks or device_id not in marks:
        return False
    return marks[device_id][0] == txid


def current_txid() -> int:
    """Read the current transaction's id: the entry's ``batch_id`` and the mark's epoch."""
    from django.db import connection, transaction

    outer = connection.atomic_blocks[0] if connection.atomic_blocks else None
    hooks = connection.run_on_commit
    cached = getattr(connection, "_nso_intent_txid", None)
    # Django 6.1 rollback paths replace this private hook list. Its identity rejects
    # transaction IDs cached by a transaction or savepoint that rolled back.
    if outer is not None and cached is not None and cached[0] is outer and cached[1] is hooks:
        return cached[2]

    with connection.cursor() as cursor:
        cursor.execute("SELECT txid_current()")
        txid = int(cursor.fetchone()[0])
    if outer is not None:
        connection._nso_intent_txid = (outer, hooks, txid)

        def clear_cache():
            cached = getattr(connection, "_nso_intent_txid", None)
            if cached is not None and cached[0] is outer and cached[1] is hooks:
                del connection._nso_intent_txid

        transaction.on_commit(clear_cache)
    return txid


def _refuse_outside_a_transaction() -> None:
    """Refuse an append that would commit on its own (O1.2).

    The entry survives a rollback, a savepoint rollback and a crash because it is the
    operator transaction's OWN row. A caller in autocommit gets two transactions instead of
    one, so its write commits and a failure between the two leaves owned intent with no
    durable record and nothing for the drain to find. That loss is silent, so this is a
    refusal rather than a warning: the writer opens the transaction, or it writes nothing.
    """
    from django.db import connection

    if not connection.in_atomic_block:
        raise RuntimeError("an intent outbox entry must be appended inside the writer's own transaction")


def enqueue(device_id, scope: str, *, transitions=(), delete_origin: bool = False) -> None:
    """Append this transaction's contribution to ``(device_id, scope)``.

    Writes nothing for a reconcile or render write (those mirror the adapter and are not
    operator intent) and nothing for a device whose teardown is in progress. Takes no lock:
    two transactions appending to two keys, in either order, never meet.
    """
    from .models import NSOIntentOutboxEntry
    from .signals import _is_intent_push_suppressed, _is_render_request

    if _is_intent_push_suppressed() or _is_render_request():
        return
    _refuse_outside_a_transaction()
    txid = current_txid()
    if _device_is_tearing_down(device_id, txid):
        return
    NSOIntentOutboxEntry.objects.create(
        device_id=device_id,
        scope=scope,
        batch_id=txid,
        transitions=list(transitions),
        mark_and=delete_origin,
        mark_any=delete_origin,
    )
