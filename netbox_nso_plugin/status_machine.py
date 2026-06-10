# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Canonical status state machine for the ``NSO*State`` write-path overlays.

This module is the **single source of truth** for the overlay lifecycle. Today the
reconcilers (``vlan_reconciler``, ``svi_reconciler``, ``subinterface_reconciler``,
``bfd_reconciler``, ``lacp_reconciler`` …), the accept/apply views and the signals
each set ``state.status = "..."`` by hand. This module does NOT yet drive them — it
*documents* the intended machine and lets ``tests/test_status_machine.py`` assert
two invariants:

  1. every overlay's ``status`` choices stay within :data:`STATES`, and
  2. every declared state is *reachable* through real transitions.

Invariant (2) is what surfaces the known gaps: ``apply_failed`` and ``error`` are in
every overlay's choices (``apply_failed`` even renders a red badge and gates Accept
eligibility) but **no code path sets them** — so the edges that would reach them are
marked ``implemented=False`` below. That makes "a failed apply leaves the row stuck
in ``deploying`` forever" a tracked, testable fact instead of folklore.

When the centralize step lands (route reconcilers through :func:`advance`) and the
adapter exposes per-intent apply errors, flip the affected edges to
``implemented=True`` and the reachability guard turns green on its own.

State semantics
---------------
Read / drift side (set automatically by reconcilers):
  ``unknown``  initial default; never observed from the device yet.
  ``imported`` observed from the device, NOT owned by the operator (read-only mirror).
  ``changed``  was imported/owned, then the device value diverged or the payload
               stopped reporting the row (drift).
  ``conflict`` a matching native NetBox object exists that we did not create
               (adoption ambiguity) — operator must accept to take ownership.

Write side (operator-driven):
  ``accepted``     operator owns the row; intent pushed to the adapter; pending Apply.
  ``deploying``    Apply in flight (marker set by the Apply action / ``_prepare_apply``).
  ``in_sync``      applied and the device re-reports the matching value.
  ``apply_failed`` the apply errored; retryable via Accept.  (GAP: not yet reachable.)
  ``error``        unexpected failure during reconcile.       (GAP: not yet reachable.)
"""

from __future__ import annotations

from typing import NamedTuple

# --- States -----------------------------------------------------------------

UNKNOWN = "unknown"
IMPORTED = "imported"
CHANGED = "changed"
CONFLICT = "conflict"
ACCEPTED = "accepted"
DEPLOYING = "deploying"
IN_SYNC = "in_sync"
APPLY_FAILED = "apply_failed"
ERROR = "error"

#: The canonical vocabulary. Every ``NSO*State.status`` choices list must be a
#: subset of this (enforced by the parity test).
STATES: frozenset[str] = frozenset(
    {UNKNOWN, IMPORTED, CHANGED, CONFLICT, ACCEPTED, DEPLOYING, IN_SYNC, APPLY_FAILED, ERROR}
)

INITIAL = UNKNOWN

#: Historically the M12 interface / interface-IP overlays declared non-canonical
#: states (``drifted`` = synonym for ``changed``; ``reserved`` = a dead overlay choice,
#: the real reservation lives on ipam.IPAddress). Both are now folded: the adapter's
#: ``drifted`` is normalised to ``changed`` at ingest, the dead choices were dropped.
#: Empty = fully folded; a re-introduced legacy state fails the parity test.
LEGACY_STATES: frozenset[str] = frozenset()

#: Maps each overlay that diverges from the canonical vocabulary to the legacy
#: states it declares. Empty now (folded); the parity test pins it so a regression
#: re-introducing a legacy state is a visible diff.
LEGACY_VOCAB_BY_MODEL: dict[str, frozenset[str]] = {}

#: Overlays whose status the plugin does NOT compute via on_reconcile. Only
#: ``NSOInterfaceState`` remains: interface-attribute status is *adapter-driven* (the
#: plugin copies the string the adapter computes; ``drifted`` is normalised to
#: ``changed`` at ingest so the vocabulary is aligned). Fully routing it through this
#: machine requires the adapter (nso-adapter) to adopt it — a cross-repo change.
NOT_YET_UNIFIED: frozenset[str] = frozenset({"NSOInterfaceState"})

#: Overlays that deliberately omit the ``changed`` (value-diff drift) state: the
#: EAV / secret-style mirrors (SNMP, logging) signal divergence via ``conflict``
#: instead of a value diff. Pinned so a *new* overlay forgetting ``changed`` fails.
OVERLAYS_WITHOUT_DRIFT_STATE: frozenset[str] = frozenset(
    {
        "NSOSnmpCommunityState",
        "NSOSnmpHostState",
        "NSOSnmpSystemInfoState",
        "NSOSnmpV3UserState",
        "NSOLoggingHostState",
    }
)

#: Owned (operator-claimed) statuses a reconcile must never clobber back to
#: ``imported``. Mirrors ``models._VLAN_WRITE_PATH_STATUSES`` — kept here as the
#: canonical definition the reconcilers should converge on.
OWNED_STATES: frozenset[str] = frozenset({ACCEPTED, DEPLOYING, IN_SYNC})

# --- Events -----------------------------------------------------------------

RECONCILE = "reconcile"  # device read refreshed the row (automatic)
DRIFT = "drift"  # device diverged / payload dropped the row (automatic)
CONFLICT_DETECTED = "conflict_detected"  # native object exists, not ours (automatic)
ACCEPT = "accept"  # operator takes ownership
REVERT = "revert"  # operator edits back to device value / un-accepts
APPLY = "apply"  # operator triggers Apply → mark deploying
APPLY_OK = "apply_ok"  # apply landed (today: realized by the next reconcile)
APPLY_ERR = "apply_err"  # apply worker reported a failure
RECONCILE_ERROR = "reconcile_error"  # unexpected exception during reconcile

EVENTS: frozenset[str] = frozenset(
    {RECONCILE, DRIFT, CONFLICT_DETECTED, ACCEPT, REVERT, APPLY, APPLY_OK, APPLY_ERR, RECONCILE_ERROR}
)


class Transition(NamedTuple):
    """One edge of the machine.

    ``implemented`` is False for edges the codebase *should* have but doesn't yet —
    those are the holes the reachability guard reports. ``note`` records where the
    transition is realized (or why it's missing).
    """

    event: str
    src: str
    dst: str
    implemented: bool
    note: str


#: The one machine that governs EVERY overlay. There is no separate "read" vs
#: "write" path: a read-only overlay simply never fires accept/apply, so it lives
#: between ``imported`` and ``changed``; an ownable overlay additionally walks
#: ``accepted → deploying → in_sync``. ``in_sync`` means exactly one thing —
#: *owned, applied, and confirmed on the device* — so it never reverts to
#: ``imported`` (the older overlays that used ``in_sync`` to mean "materialized /
#: matches device" were mislabeled; that is now ``imported``).
#: Edges with ``implemented=False`` are the tracked gaps.
TRANSITIONS: tuple[Transition, ...] = (
    # -- automatic: device observed the row (present) ------------------------
    Transition(RECONCILE, UNKNOWN, IMPORTED, True, "first import, matches device"),
    Transition(RECONCILE, UNKNOWN, CHANGED, True, "first import but already diverged"),
    Transition(RECONCILE, IMPORTED, IMPORTED, True, "still matches device"),
    Transition(RECONCILE, IMPORTED, CHANGED, True, "device diverged from NetBox (drift)"),
    Transition(RECONCILE, CHANGED, IMPORTED, True, "drift resolved"),
    Transition(RECONCILE, CHANGED, CHANGED, True, "still drifted"),
    Transition(RECONCILE, CONFLICT, IMPORTED, True, "adoption ambiguity resolved"),
    Transition(RECONCILE, CONFLICT, CHANGED, True, "still conflicting / diverged"),
    Transition(RECONCILE, DEPLOYING, IN_SYNC, True, "apply landed: device re-reports applied config"),
    Transition(RECONCILE, ACCEPTED, IN_SYNC, True, "owned & device now matches NetBox"),
    Transition(RECONCILE, ACCEPTED, ACCEPTED, True, "owned, apply not yet reflected (still pending)"),
    Transition(RECONCILE, IN_SYNC, IN_SYNC, True, "owned, device still matches"),
    Transition(RECONCILE, IN_SYNC, ACCEPTED, True, "owned, device drifted → re-pend for apply"),
    # -- automatic: row dropped from the payload -----------------------------
    Transition(DRIFT, UNKNOWN, CHANGED, True, "dropped before first sync"),
    Transition(DRIFT, IMPORTED, CHANGED, True, "no longer reported by device"),
    Transition(DRIFT, CHANGED, CHANGED, True, "still gone"),
    Transition(DRIFT, CONFLICT, CHANGED, True, "no longer reported"),
    Transition(DRIFT, ACCEPTED, CHANGED, True, "owned value no longer on device"),
    Transition(DRIFT, DEPLOYING, CHANGED, True, "vanished mid-apply"),
    Transition(DRIFT, IN_SYNC, CHANGED, True, "synced value disappeared"),
    # -- automatic: adoption ambiguity (native object exists, not ours) ------
    Transition(CONFLICT_DETECTED, UNKNOWN, CONFLICT, True, "native object exists, not created by us"),
    Transition(CONFLICT_DETECTED, IMPORTED, CONFLICT, True, "native object diverged from ours"),
    Transition(CONFLICT_DETECTED, CHANGED, CONFLICT, True, "drift is an adoption conflict"),
    Transition(CONFLICT_DETECTED, CONFLICT, CONFLICT, True, "still conflicting"),
    # -- operator: accept / revert -------------------------------------------
    Transition(ACCEPT, IMPORTED, ACCEPTED, True, "operator takes ownership"),
    Transition(ACCEPT, CHANGED, ACCEPTED, True, "accept the drifted value"),
    Transition(ACCEPT, CONFLICT, ACCEPTED, True, "resolve adoption ambiguity"),
    Transition(ACCEPT, APPLY_FAILED, ACCEPTED, True, "retry after a failed apply"),
    Transition(REVERT, ACCEPTED, IMPORTED, True, "edit back to device value clears pending (c160039)"),
    # -- operator: apply ------------------------------------------------------
    Transition(APPLY, ACCEPTED, DEPLOYING, True, "_prepare_apply marks owned accepted→deploying"),
    Transition(APPLY_OK, DEPLOYING, IN_SYNC, True, "apply worker reported success"),
    # -- GAPS: declared states with no entry path ----------------------------
    Transition(
        APPLY_ERR,
        DEPLOYING,
        APPLY_FAILED,
        False,
        "GAP: adapter does not expose per-intent apply error; the plugin never sets "
        "apply_failed, so a failed apply leaves the row stuck in 'deploying'.",
    ),
    Transition(
        RECONCILE_ERROR,
        IMPORTED,
        ERROR,
        False,
        "GAP: exceptions during reconcile are logged but never set status=error.",
    ),
)


def transitions(*, implemented_only: bool = False) -> tuple[Transition, ...]:
    """Return the transition edges, optionally only the implemented ones."""
    if implemented_only:
        return tuple(t for t in TRANSITIONS if t.implemented)
    return TRANSITIONS


def reachable_states(*, implemented_only: bool = False, start: str = INITIAL) -> frozenset[str]:
    """States reachable from ``start`` by following transition edges (BFS)."""
    edges = transitions(implemented_only=implemented_only)
    reached = {start}
    frontier = [start]
    while frontier:
        node = frontier.pop()
        for t in edges:
            if t.src == node and t.dst not in reached:
                reached.add(t.dst)
                frontier.append(t.dst)
    return frozenset(reached)


def unreachable_states(*, implemented_only: bool = False) -> frozenset[str]:
    """Return declared states with no entry path (gaps when ``implemented_only=True``)."""
    return STATES - reachable_states(implemented_only=implemented_only)


def allowed(event: str, src: str, *, implemented_only: bool = False) -> frozenset[str]:
    """Destination states permitted for ``(event, src)``. Empty = illegal transition.

    This is the guard the centralize step will use: a reconcile/accept/apply that
    isn't in this set is a bug, not a silent string overwrite.
    """
    return frozenset(t.dst for t in transitions(implemented_only=implemented_only) if t.event == event and t.src == src)


# --- Engine -----------------------------------------------------------------
#
# ``advance`` is the runtime form of the spec: the single chokepoint the
# reconcilers / views / signals will route through (step 2, the centralize work).
# It operates over the *intended* machine (all edges, implemented or not) so that
# wiring a gap edge later — e.g. ``deploying -> apply_failed`` — needs only its
# caller plus flipping ``implemented=True``; ``advance`` already permits it.
#
# Today every call site does a raw ``state.status = "..."`` with no check, so an
# illegal jump (a reconcile clobbering an owned row, an apply from an un-accepted
# row) corrupts state silently. Routing through ``advance`` turns each of those
# into a raised error at the source.


class IllegalTransition(ValueError):
    """Raised when ``(event, current[, to])`` is not an edge of the machine."""

    def __init__(self, current: str, event: str, to: str | None = None):
        self.current, self.event, self.to = current, event, to
        if to is None:
            super().__init__(f"no {event!r} transition from {current!r}")
        else:
            super().__init__(f"{event!r} cannot move {current!r} -> {to!r}")


class AmbiguousTransition(ValueError):
    """Raised when ``(event, current)`` has several targets and ``to`` was omitted.

    These are the guarded/value-aware edges (e.g. a reconcile of an ``accepted``
    row may settle to ``in_sync`` or stay ``accepted`` depending on whether the
    device matches). The caller owns that decision and must pass ``to=``.
    """

    def __init__(self, current: str, event: str, options: frozenset[str]):
        self.current, self.event, self.options = current, event, options
        super().__init__(f"{event!r} from {current!r} is ambiguous; pass to= one of {sorted(options)}")


def can(event: str, src: str, to: str | None = None) -> bool:
    """Return True if ``(event, src[, to])`` is a legal transition (no exceptions)."""
    dests = allowed(event, src)
    return bool(dests) if to is None else to in dests


def advance(current: str, event: str, *, to: str | None = None) -> str:
    """Return the next status for ``current`` under ``event``, or raise.

    - Deterministic edge (one legal target): ``to`` is optional and inferred.
    - Guarded edge (several legal targets): ``to`` is required; the caller decides
      based on its own context (does the device match? is the parent present?), and
      ``advance`` validates the choice is legal.
    - No legal target, or a ``to`` outside the legal set: raises :class:`IllegalTransition`.

    Operates over the intended machine, so unimplemented gap edges (``apply_err``,
    ``reconcile_error``) are already accepted — wiring them later is caller-only.
    """
    if current not in STATES:
        raise IllegalTransition(current, event, to)
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}")
    dests = allowed(event, current)
    if not dests:
        raise IllegalTransition(current, event, to)
    if to is None:
        if len(dests) == 1:
            return next(iter(dests))
        raise AmbiguousTransition(current, event, dests)
    if to not in dests:
        raise IllegalTransition(current, event, to)
    return to


def is_owned(status: str) -> bool:
    """Return True if the operator has claimed the row (accepted/deploying/in_sync).

    Single definition of "owned" — replaces the per-reconciler
    ``_VLAN_WRITE_PATH_STATUSES`` copies. A reconcile must never clobber an owned
    row back to an unowned state.
    """
    return status in OWNED_STATES


def on_reconcile(
    current: str,
    *,
    present: bool = True,
    matches: bool | None = None,
    conflict: bool = False,
    settles_owned: bool = True,
) -> str:
    """Apply the single reconcile transition shared by EVERY overlay (no read/write split).

    Call this from a reconciler each time it observes (or fails to observe) a row;
    it returns the next status, validated through :func:`advance`.

    - ``present``  — the device still reports this row. ``False`` → ``changed`` (drift),
      except ``accepted``/``deploying`` (intent not yet confirmed on device) are kept.
    - ``matches``  — whether the row's tracked value is satisfied. For a true value
      overlay (VLAN name, switchport L2) this is "device value == NetBox value"; for an
      FK/content overlay it is "materialized in NetBox". ``None`` for pure mirrors with
      no value at all (resting unowned state is simply ``imported``).
    - ``conflict`` — a native NetBox object exists that we did not create (adoption
      ambiguity); only meaningful for an unowned row.
    - ``settles_owned`` — whether ``matches`` reflects genuine device confirmation and
      may therefore settle an owned row (``accepted → in_sync``). Pass ``False`` for
      FK/content overlays, where ``matches`` means "materialized at import" (NOT applied
      to the device): an owned row must then settle only via Apply (``deploying →
      in_sync``), never by reconcile.

    Ownership is preserved: an owned row is never pulled back to ``imported``.
    """
    if not present:
        # accepted/deploying are operator intent not yet confirmed on the device, so
        # the device legitimately not reporting the row is expected — don't flag drift.
        # in_sync (was confirmed) and unowned rows that vanish are real drift.
        if current in (ACCEPTED, DEPLOYING) or current == CHANGED:
            return current
        return advance(current, DRIFT, to=CHANGED)
    if is_owned(current):
        if current == DEPLOYING:
            return advance(current, RECONCILE, to=IN_SYNC)
        if matches is None or not settles_owned:
            return current  # owned + no device-confirmed value → preserve accepted/in_sync
        return advance(current, RECONCILE, to=IN_SYNC if matches else ACCEPTED)
    if conflict:
        return advance(current, CONFLICT_DETECTED, to=CONFLICT)
    if matches is None:
        return advance(current, RECONCILE, to=IMPORTED)
    return advance(current, RECONCILE, to=IMPORTED if matches else CHANGED)
