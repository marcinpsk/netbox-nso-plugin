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

#: Non-canonical states still declared by the older M12 interface / interface-IP
#: overlays, tracked here so the parity guard tolerates them *explicitly* (a brand
#: new stray state still fails) while flagging them for migration:
#:   ``drifted``  — pre-unification synonym for ``changed`` (``NSOInterfaceState``,
#:                  ``NSOInterfaceIPState``); ``views.py`` filters it alongside
#:                  ``changed`` to this day. Should fold into ``changed``.
#:   ``reserved`` — IP-reservation status specific to ``NSOInterfaceIPState``;
#:                  arguably a legitimate IP-domain state, kept separate for now.
LEGACY_STATES: frozenset[str] = frozenset({"drifted", "reserved"})

#: Maps each overlay that diverges from the canonical vocabulary to the legacy
#: states it declares. The parity test pins this so closing a gap is a visible diff.
LEGACY_VOCAB_BY_MODEL: dict[str, frozenset[str]] = {
    "NSOInterfaceState": frozenset({"drifted"}),
    "NSOInterfaceIPState": frozenset({"drifted", "reserved"}),
}

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


#: The intended machine. Edges with ``implemented=False`` are the tracked gaps.
TRANSITIONS: tuple[Transition, ...] = (
    # -- automatic: device reconcile -----------------------------------------
    Transition(RECONCILE, UNKNOWN, IMPORTED, True, "first observation"),
    Transition(RECONCILE, IMPORTED, IMPORTED, True, "still matches device"),
    Transition(RECONCILE, CHANGED, IMPORTED, True, "drift resolved"),
    Transition(RECONCILE, IN_SYNC, IN_SYNC, True, "owned, device still matches"),
    Transition(RECONCILE, ACCEPTED, ACCEPTED, True, "owned, not clobbered (no-clobber guard)"),
    Transition(RECONCILE, ACCEPTED, IN_SYNC, True, "value-aware (VLAN): device now matches NetBox"),
    Transition(RECONCILE, DEPLOYING, IN_SYNC, True, "apply landed: device re-reports applied config"),
    # -- automatic: drift -----------------------------------------------------
    Transition(DRIFT, IMPORTED, CHANGED, True, "device diverged / row dropped from payload"),
    Transition(DRIFT, IN_SYNC, CHANGED, True, "drift after sync"),
    Transition(DRIFT, ACCEPTED, CHANGED, True, "owned value no longer on device"),
    # -- automatic: adoption ambiguity ---------------------------------------
    Transition(CONFLICT_DETECTED, UNKNOWN, CONFLICT, True, "native object exists, not created by us"),
    Transition(CONFLICT_DETECTED, IMPORTED, CONFLICT, True, "native object diverged from ours"),
    # -- operator: accept / revert -------------------------------------------
    Transition(ACCEPT, IMPORTED, ACCEPTED, True, "operator takes ownership"),
    Transition(ACCEPT, CHANGED, ACCEPTED, True, "accept the drifted value"),
    Transition(ACCEPT, CONFLICT, ACCEPTED, True, "resolve adoption ambiguity"),
    Transition(ACCEPT, APPLY_FAILED, ACCEPTED, True, "retry after a failed apply"),
    Transition(REVERT, ACCEPTED, IMPORTED, True, "edit back to device value clears pending (c160039)"),
    # -- operator: apply ------------------------------------------------------
    Transition(APPLY, ACCEPTED, DEPLOYING, True, "_prepare_apply marks owned accepted→deploying"),
    Transition(APPLY_OK, DEPLOYING, IN_SYNC, True, "settled by the next reconcile (deploying→in_sync)"),
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
