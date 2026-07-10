# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Universal per-device 'captured version' + materialized-owner machinery.

Several config families are deduplicated **by name** into a single NetBox object —
route-policy route-maps / community-lists / prefix-lists / as-paths today, ACLs next —
yet every device has its OWN content for that name.  Storing only one device's content
on the shared object leaves the rest perpetually divergent, and naively re-importing the
last device to poll causes cross-device churn.

This module is the family-agnostic core that resolves that tension:

* every device keeps its own capture on its overlay row (``captured`` field);
* exactly ONE device per (family, object_name) group is the *materialized owner*
  (``is_materialized``) — its capture is what populates the shared NetBox object;
* the first device to import an object owns it (no churn: only the owner ever writes);
* an operator can **re-point** ownership to a different device's version via
  :func:`rematerialize`, which refills the shared object and flips the flags.

A family plugs in by registering a :class:`SharedObjectSpec` (how to materialize a
capture into the NetBox object + how to hash a capture).  Nothing here knows about
route-policy specifically, so ACL reuses it by registering its own families and a state
model that mixes in ``models.SharedObjectStateMixin``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SharedObjectSpec:
    """How one family of shared named objects materializes into its NetBox object.

    ``fill(target_obj, captured)`` rebuilds the NetBox object's content from a device
    capture (full replace).  ``hash_captured(captured)`` returns a stable digest of a
    capture so two devices' versions can be compared without re-serialising the object.
    Both operate on the raw per-object payload dict the device reported.
    """

    fill: Callable[[object, dict], None]
    hash_captured: Callable[[dict], str]
    label: str = ""
    # Reverse of fill: the CURRENT NetBox object content in device-capture shape, for the
    # device-caught-up settle (#93). None = the family cannot compare (settle skipped).
    extract: Callable[[object], dict] | None = None


_REGISTRY: dict[str, SharedObjectSpec] = {}


def register(family: str, spec: SharedObjectSpec) -> None:
    """Register a family's materialization spec (idempotent; last registration wins)."""
    _REGISTRY[family] = spec


def get_spec(family: str) -> SharedObjectSpec | None:
    return _REGISTRY.get(family)


def hash_captured(family: str, captured: dict) -> str:
    """Digest a device capture using the family's spec (empty string if unknown family)."""
    spec = _REGISTRY.get(family)
    return spec.hash_captured(captured) if spec is not None else ""


def _renumbered(captured: dict) -> dict:
    """Positionally renumber ``entries`` sequences (1..n) for the caught-up comparison.

    Sequence numbers are ARTIFACTS on both sides — readers synthesize 10/20/…, the
    materializer renumbers 1..n (smallint-safe), and some devices (IOS community-lists)
    have no sequences at all — so position is the only truth the comparison may use.
    The sequence key is FORCED onto every dict entry so a capture without sequences
    still lands in the same key-set as an extracted object.
    """
    entries = captured.get("entries")
    if not isinstance(entries, list):
        return captured
    out = dict(captured)
    out["entries"] = [({**e, "sequence": i} if isinstance(e, dict) else e) for i, e in enumerate(entries, start=1)]
    return out


def device_caught_up(family: str, captured: dict, obj, exclude_members: list | None = None) -> bool | None:
    """Whether the device capture equals the CURRENT NetBox object content (the intent).

    True/False when *family* registered an ``extract``; None when it cannot compare
    (no extractor, no object, or an extraction error — never settle on a guess).
    Equality here is GENUINE device confirmation — this device now renders what the
    operator's object holds — so ``on_reconcile`` may settle owned rows
    (``settles_owned=True``), unlike the materialized-content 'matches' which only
    says the recorded import is unchanged. Sequence-insensitive via :func:`_renumbered`.
    """
    spec = _REGISTRY.get(family)
    if spec is None or spec.extract is None or obj is None:
        return None
    try:
        extracted = spec.extract(obj)
        if exclude_members:
            # #101 — compare the REPRESENTABLE intent: members the NED cannot hold
            # (recorded on the row by the push transparency) are absent from the device
            # BY DESIGN, not by drift. Filter ONLY the object side: if the device
            # actually holds a (stale-)excluded member, the sides still differ and the
            # row correctly does not settle.
            drop = set(exclude_members)
            extracted = dict(extracted)
            extracted["entries"] = [
                e for e in (extracted.get("entries") or []) if not (isinstance(e, dict) and e.get("community") in drop)
            ]
        return spec.hash_captured(_renumbered(extracted)) == spec.hash_captured(_renumbered(captured))
    except Exception:
        logger.warning("device_caught_up: extract/hash failed for %s", family, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Group helpers — a "group" is every overlay row sharing (family, object_name).
# ---------------------------------------------------------------------------


def group_rows(state):
    """All overlay rows (across every device) for this row's (family, object_name)."""
    return type(state).objects.filter(family=state.family, object_name=state.object_name)


def materialized_row(state_model, family: str, object_name: str):
    """Return the row whose capture currently populates the shared object (or None)."""
    return state_model.objects.filter(family=family, object_name=object_name, is_materialized=True).first()


def canonical_hash(state_model, family: str, object_name: str) -> str | None:
    """content_hash of the group's materialized owner, or None if none is marked yet.

    Reconcilers compare a freshly-read device capture against this to decide whether a
    NON-owner device's version diverges from what's actually in NetBox (an honest
    cross-device conflict) rather than only tracking each device against its own history.
    """
    owner = materialized_row(state_model, family, object_name)
    if owner is None:
        return None
    return owner.content_hash or None


def mark_materialized(state) -> None:
    """Make ``state`` the sole materialized owner of its group (flag only, no refill).

    Called when a device first fills an empty shared object.  Clears the flag on any
    sibling so the 'exactly one owner' invariant holds even across races.
    """
    from django.db import transaction

    with transaction.atomic():
        # Lock the whole group so two devices first-filling the same shared object serialize:
        # without this both could pass the "no materialized sibling" check and both flag
        # themselves owner, leaving two materialized owners (this runs on every reconcile of
        # every shared route-policy object, so the race is realistic). Take the lock, then clear
        # any sibling and flag self.
        list(group_rows(state).select_for_update().values_list("pk", flat=True))
        group_rows(state).filter(is_materialized=True).exclude(pk=state.pk).update(is_materialized=False)
        if not state.is_materialized:
            state.is_materialized = True
            state.save(update_fields=["is_materialized"])


def versions(state_model, family: str, object_name: str) -> list:
    """Every device's version of a shared object, owner first (for the versions UI)."""
    rows = state_model.objects.filter(family=family, object_name=object_name).select_related(
        "management", "management__device"
    )
    return sorted(rows, key=lambda r: (not r.is_materialized, str(getattr(r.management, "device", ""))))


def version_items(state_model, family: str, object_name: str) -> list[dict]:
    """Annotated per-device versions for the versions UI (owner first).

    Each item is ``{row, device, has_capture, entry_count, is_owner, comparable,
    matches_owner}`` — enough to render "which device NetBox mirrors, which match it, which
    diverge" without re-serialising the NetBox object.  ``comparable`` is False until both
    this row and the owner have a fresh ``captured`` (e.g. rows backfilled before the next
    reconcile): without it we can't honestly claim match-or-diverge.  Returned as plain data
    so the view stays thin and this logic is unit testable without rendering the full page
    (and reusable by the future ACL versions view).
    """
    rows = versions(state_model, family, object_name)
    owner = next((r for r in rows if r.is_materialized), None)
    # ``hash_captured`` returns "" when the family's spec is not registered (the import-order
    # trap: a web worker can render this page before the reconciler module — which registers
    # the specs — has loaded). An empty digest is NOT a real content hash, so treat it as
    # "no basis to compare": every captured row would otherwise hash to "" and falsely read
    # as "matches", making divergent content look in-sync.
    owner_hash = (hash_captured(family, owner.captured) or None) if owner and owner.captured else None
    items = []
    for r in rows:
        r_hash = (hash_captured(family, r.captured) or None) if r.captured else None
        comparable = owner_hash is not None and r_hash is not None
        items.append(
            {
                "row": r,
                "device": getattr(r.management, "device", None),
                "has_capture": bool(r.captured),
                "entry_count": len((r.captured or {}).get("entries") or []),
                "is_owner": r.is_materialized,
                "comparable": comparable,
                "matches_owner": comparable and r_hash == owner_hash,
            }
        )
    return items


# ---------------------------------------------------------------------------
# Operator re-point: make a chosen device's version the materialized one.
# ---------------------------------------------------------------------------


def rematerialize(state) -> None:
    """Re-point a shared object's content to ``state``'s captured version.

    Refills the shared NetBox object from this device's capture, marks this row the
    materialized owner, and recomputes every sibling's status against the new content
    (matching device → imported, divergent → conflict; owned rows are left alone).

    Writes only into NetBox (device→NetBox); it does NOT push to any device — that is a
    separate, explicit operator Accept.  Runs under ``suppress_intent_push`` so the
    NetBox-object saves don't fire the operator-edit push handlers.
    """
    from django.db import transaction

    from . import status_machine as sm
    from .signals import suppress_intent_push

    spec = get_spec(state.family)
    if spec is None:
        raise ValueError(f"no materialization spec registered for family {state.family!r}")
    target = state.assigned_object
    if target is None:
        raise ValueError("overlay row is not linked to a NetBox object")
    captured = state.captured or {}
    if not captured:
        raise ValueError("no captured content to materialize for this device")

    new_hash = spec.hash_captured(captured)
    with transaction.atomic():
        with suppress_intent_push():
            spec.fill(target, captured)
        for row in group_rows(state):
            if row.pk == state.pk:
                row.is_materialized = True
                row.content_hash = new_hash
                if not sm.is_owned(row.status):
                    row.status = sm.IMPORTED
                row.save(update_fields=["is_materialized", "content_hash", "status"])
                continue
            _resettle_sibling(row, spec, new_hash)


def _resettle_sibling(row, spec: SharedObjectSpec, owner_hash: str) -> None:
    """Clear a non-owner row's flag and recompute its conflict status against the owner."""
    from django.utils import timezone

    from . import status_machine as sm

    row.is_materialized = False
    if not sm.is_owned(row.status) and row.captured:
        diverged = spec.hash_captured(row.captured) != owner_hash
        row.status = sm.CONFLICT if diverged else sm.IMPORTED
    # Bump last_sync_at alongside the status change so the UI's "last seen" isn't stale next to
    # a freshly-recomputed status.
    row.last_sync_at = timezone.now()
    row.save(update_fields=["is_materialized", "status", "last_sync_at"])
