# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Route-policy reconciler for A4.

Reads the adapter's GET /api/v1/devices/{id}/route-policy response and
reconciles it into netbox-routing policy objects (PrefixList, CommunityList,
ASPath, RouteMap) — including their ENTRIES — plus NSORoutePolicyState overlay
rows.

Decision: global dedup by name — same-named object across N devices = ONE
NetBox object. On-device divergence sets status=conflict, never silently
overwrites existing content. Entries are filled on first import / for empty
shells; once an object has entries, a later content divergence is flagged
``conflict`` and the entries are left untouched (no silent clobber).
"""

from __future__ import annotations

import hashlib
import json
import logging

from . import shared_object_ownership as ownership

logger = logging.getLogger(__name__)


def _hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _get_or_create_named(model, name, **defaults):
    """Get-or-create a netbox_routing policy object by CASE-INSENSITIVE name.

    netbox_routing enforces uniqueness on ``Lower(name)``, so a plain
    ``get_or_create(name=...)`` raises IntegrityError when an object with the same
    name in a different case already exists (device ``ACCEPT-ALL`` vs an existing
    ``accept-all``). That exception aborted the ENTIRE route-policy reconcile, leaving
    every row stuck in ``error``. The global dedup is case-insensitive, so match
    existing objects with ``name__iexact``; create only when truly absent, re-fetching
    on a race (the create is savepointed so an IntegrityError doesn't poison the txn).
    """
    from django.db import IntegrityError, transaction

    obj = model.objects.filter(name__iexact=name).first()
    if obj is not None:
        return obj, False
    try:
        with transaction.atomic():
            return model.objects.create(name=name, **defaults), True
    except IntegrityError:
        existing = model.objects.filter(name__iexact=name).first()
        if existing is None:
            raise
        return existing, False


def _norm_action(action: str | None) -> str:
    """Map an adapter action to netbox_routing ActionChoices (permit/deny)."""
    a = (action or "").strip().lower()
    if a in ("permit", "accept"):
        return "permit"
    if a in ("deny", "reject"):
        return "deny"
    return "permit"


def _load_json(value) -> dict:
    """Parse a match/set JSON string into a dict (the adapter sends them as strings)."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Entry fill helpers — each rebuilds a parent's entries (full replace).
# Called only when the parent is new or has no entries (see _reconcile_*).
# ---------------------------------------------------------------------------


def _fill_prefix_list_entries(pl_obj, entries: list) -> None:
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import CustomPrefix, PrefixListEntry

    ct = ContentType.objects.get_for_model(CustomPrefix)
    PrefixListEntry.objects.filter(prefix_list=pl_obj).delete()
    # Resolve every CustomPrefix once (prefixes repeat across lists).
    cp_by_prefix: dict[str, object] = {}
    rows = []
    # Sequence is assigned positionally: the model field is a (signed) smallint and the
    # adapter's step-10 sequence overflows it on large lists (and Junos prefix-lists have
    # no real sequence). Positional 1-based numbering preserves order and always fits.
    seq = 0
    for e in entries:
        prefix = (e.get("prefix") or "").strip()
        if not prefix:
            continue
        cp = cp_by_prefix.get(prefix)
        if cp is None:
            try:
                cp, _ = CustomPrefix.objects.get_or_create(prefix=prefix)
            except Exception as exc:
                logger.warning("route-policy: bad prefix %r in %s: %s", prefix, pl_obj.name, exc)
                continue
            cp_by_prefix[prefix] = cp
        seq += 1
        rows.append(
            PrefixListEntry(
                prefix_list=pl_obj,
                assigned_prefix_type=ct,
                assigned_prefix_id=cp.pk,
                sequence=seq,
                action=_norm_action(e.get("action")),
                ge=e.get("ge"),
                le=e.get("le"),
            )
        )
    if rows:
        PrefixListEntry.objects.bulk_create(rows)


def _fill_as_path_entries(ap_obj, entries: list) -> None:
    from netbox_routing.models import ASPathEntry

    ASPathEntry.objects.filter(aspath=ap_obj).delete()
    # Positional sequence (smallint-safe; see _fill_prefix_list_entries).
    rows = [
        ASPathEntry(
            aspath=ap_obj,
            sequence=i,
            action=_norm_action(e.get("action")),
            pattern=(e.get("pattern") or "")[:1000],
        )
        for i, e in enumerate(entries, start=1)
    ]
    if rows:
        ASPathEntry.objects.bulk_create(rows)


def _fill_community_list_entries(cl_obj, entries: list) -> None:
    """Fill a CommunityList's members.

    The universal Community model stores every member string VERBATIM, exactly as the
    device reports it — numeric (1111:100), well-known keywords (no-export), typed extended
    (target:1111:100, color:0:128), RFC 8092 large (large:GA:L1:L2), and match-only
    regex/wildcards (1111:*, 1111:1113.). The kind is derived by parsing the text on the
    netbox_routing side; the plugin no longer routes members to parallel typed lists.
    Empty members are skipped.
    """
    from netbox_routing.models import Community, CommunityListEntry

    CommunityListEntry.objects.filter(community_list=cl_obj).delete()
    rows = []
    for e in entries:
        value = (e.get("community") or "").strip()
        if not value:
            logger.info("route-policy: skipping empty community member in %s", cl_obj.name)
            continue
        action = _norm_action(e.get("action"))
        comm, _ = Community.objects.get_or_create(community=value)
        rows.append(CommunityListEntry(community_list=cl_obj, action=action, community=comm))
    if rows:
        CommunityListEntry.objects.bulk_create(rows)


# Community literals (vs community-LIST names) when resolving a set-community by-ref:
# anything with a ':' or a well-known keyword is an inline literal, the rest is a list name.
_WELLKNOWN_COMMUNITIES = frozenset(
    {
        "no-export",
        "no-advertise",
        "no-export-subconfed",
        "local-as",
        "internet",
        "gshut",
        "accept-own",
        "none",
    }
)


def _looks_like_community_literal(name: str) -> bool:
    n = name.strip().lower()
    return ":" in n or n in _WELLKNOWN_COMMUNITIES


def _resolve_call_policy(RouteMap, name: str | None):
    """Resolve a Junos from-policy / IOS-XR apply policy name to an existing RouteMap (by-ref)."""
    if not name:
        return None
    return RouteMap.objects.filter(name__iexact=name).first()


def _materialise_set_communities(rme, structured, cl_by_name) -> list:
    """Create RouteMapEntrySetCommunity rows from the structured set-actions (R3).

    Each action targets a community-LIST by reference (resolved against the materialised
    CommunityList objects), or an inline community literal (IOS-style). Anything that
    resolves to neither — a dangling by-ref to a list the device never defined — is returned
    so the caller can preserve it in vendor_ext rather than drop it (no silent loss).
    """
    from netbox_routing.models import Community, CommunityList, RouteMapEntrySetCommunity

    unresolved: list = []
    for sc in structured.set_communities:
        cl = cl_by_name.get(sc.name) or CommunityList.objects.filter(name__iexact=sc.name).first()
        if cl is not None:
            RouteMapEntrySetCommunity.objects.create(route_map_entry=rme, operation=sc.operation, community_list=cl)
        elif _looks_like_community_literal(sc.name):
            row = RouteMapEntrySetCommunity.objects.create(route_map_entry=rme, operation=sc.operation)
            comm, _ = Community.objects.get_or_create(community=sc.name)
            row.communities.add(comm)
        else:
            unresolved.append({"operation": sc.operation, "name": sc.name})
    return unresolved


def _fill_route_map_entries(rm_obj, entries: list, pl_by_name, cl_by_name, ap_by_name) -> None:
    from netbox_routing.models import RouteMap, RouteMapEntry

    from .route_policy_structure import structure_entry

    RouteMapEntry.objects.filter(route_map=rm_obj).delete()
    # Positional sequence — unique per route-map and smallint-safe (the device sequence
    # can exceed the field's range; see _fill_prefix_list_entries).
    default_action = None
    for i, e in enumerate(entries, start=1):
        match_blob = _load_json(e.get("match"))
        set_blob = _load_json(e.get("set"))
        # Derive the structured projection: match_afi, set-community ops, call-policy,
        # vendor_ext. The full match/set blobs are kept AS-IS (authoritative for the write-side
        # round-trip until the reader/contract speak structured in P3) — the structured fields
        # are an additive, queryable/display view, not a replacement. See route_policy_structure.
        structured = structure_entry(match_blob, set_blob)
        # flow_control (IOS route-map `continue`) rides inside set-json (no dedicated adapter
        # leaf) — lift it into the model field; the push re-adds it so the round-trip is symmetric.
        set_data = dict(set_blob)
        flow_control = set_data.pop("flow_control", None)

        # default-action projection (R5): mirror the device's flagged default-action entry onto
        # RouteMap.default_action. The synthetic entry itself is KEPT so the write-side blob
        # round-trip stays byte-symmetric (the reader still synthesises it pre-contract-v2);
        # P2 hides it in favour of the field, P3 retires the synthesis.
        if structured.is_default_action:
            default_action = _norm_action(e.get("action"))

        vendor_ext = dict(structured.vendor_ext)
        rme = RouteMapEntry.objects.create(
            route_map=rm_obj,
            sequence=i,
            action=_norm_action(e.get("action")),
            flow_control=flow_control,
            match=match_blob,
            set=set_data,
            match_afi=structured.match_afi or None,
            call_policy=_resolve_call_policy(RouteMap, structured.call_policy),
        )
        for nm in e.get("match_prefix_lists") or []:
            obj = pl_by_name.get(nm)
            if obj:
                rme.match_prefix_list.add(obj)
        for nm in e.get("match_community_lists") or []:
            obj = cl_by_name.get(nm)
            if obj:
                rme.match_community_list.add(obj)
                # Devices match community-LISTS, never individual communities, so also
                # link the list's member Communities into match_community — surfacing
                # which concrete communities the route-map matches (user request).
                member_ids = obj.communitylistentries.values_list("community_id", flat=True)
                if member_ids:
                    rme.match_community.add(*[cid for cid in member_ids if cid])
            else:
                logger.debug("route-policy: route-map %s refs community-list %r not resolvable", rm_obj.name, nm)
        for nm in e.get("match_as_paths") or []:
            obj = ap_by_name.get(nm)
            if obj:
                rme.match_aspath.add(obj)
        unresolved = _materialise_set_communities(rme, structured, cl_by_name)
        if unresolved:
            vendor_ext.setdefault("unmapped", {})["set_community"] = unresolved
        # Persist vendor_ext last (it may have grown an "unmapped" note above); null when empty.
        rme.vendor_ext = vendor_ext or None
        rme.save(update_fields=["vendor_ext"])

    # default_action is device-sourced config on the shared RouteMap (full-replace each fill).
    if rm_obj.default_action != default_action:
        rm_obj.default_action = default_action
        rm_obj.save(update_fields=["default_action"])


# ---------------------------------------------------------------------------
# Family materialization specs — register route-policy into the universal
# shared_object_ownership core (route-maps/community-lists/prefix-lists/as-paths).
# Each spec knows how to rebuild its NetBox object from a device capture and how
# to hash a capture; ACL plugs in the same way later.
# ---------------------------------------------------------------------------


def _entries(captured: dict) -> list:
    return captured.get("entries") or []


def _cl_hash(captured: dict) -> str:
    """Hash a community-list capture (invert_match-aware; see _reconcile_community_lists)."""
    entries = _entries(captured)
    if bool(captured.get("invert_match", False)):
        return _hash({"invert_match": True, "entries": entries})
    return _hash(entries)


def _cl_fill(obj, captured: dict) -> None:
    invert = bool(captured.get("invert_match", False))
    if obj.invert_match != invert:
        obj.invert_match = invert
        obj.save(update_fields=["invert_match"])
    _fill_community_list_entries(obj, _entries(captured))


def _resolve_name_maps(entries: list):
    """Resolve a route-map capture's referenced object names to existing NetBox objects.

    Used when re-materializing a route-map from a device capture: the referenced
    prefix-lists / community-lists / as-paths already exist (global dedup), so look them
    up case-insensitively rather than relying on the reconcile-time in-memory maps.
    """
    from netbox_routing.models import ASPath, CommunityList, PrefixList

    pl_names: set[str] = set()
    cl_names: set[str] = set()
    ap_names: set[str] = set()
    for e in entries:
        pl_names.update(e.get("match_prefix_lists") or [])
        cl_names.update(e.get("match_community_lists") or [])
        ap_names.update(e.get("match_as_paths") or [])

    def _lookup(model, names):
        out = {}
        for nm in names:
            obj = model.objects.filter(name__iexact=nm).first()
            if obj is not None:
                out[nm] = obj
        return out

    return _lookup(PrefixList, pl_names), _lookup(CommunityList, cl_names), _lookup(ASPath, ap_names)


def _rm_fill(obj, captured: dict) -> None:
    entries = _entries(captured)
    pl_map, cl_map, ap_map = _resolve_name_maps(entries)
    _fill_route_map_entries(obj, entries, pl_map, cl_map, ap_map)


def _register_specs() -> None:
    Spec = ownership.SharedObjectSpec
    ownership.register(
        "prefix_list",
        Spec(fill=lambda o, c: _fill_prefix_list_entries(o, _entries(c)), hash_captured=lambda c: _hash(_entries(c))),
    )
    ownership.register("community_list", Spec(fill=_cl_fill, hash_captured=_cl_hash))
    ownership.register(
        "as_path",
        Spec(fill=lambda o, c: _fill_as_path_entries(o, _entries(c)), hash_captured=lambda c: _hash(_entries(c))),
    )
    ownership.register("route_map", Spec(fill=_rm_fill, hash_captured=lambda c: _hash(_entries(c))))


_register_specs()


# ---------------------------------------------------------------------------
# Overlay upsert (shared by every family)
# ---------------------------------------------------------------------------

_FILL_STATUSES = ("imported", "in_sync")  # safe to (re)fill entries in these states


def _row_diverged(state, entries_hash, family, name) -> bool:
    """Whether this reconcile's capture diverges from what's materialized in NetBox.

    The materialized owner compares against its OWN recorded content (a device-side
    change to the canonical version is drift, never an auto-clobber).  A non-owner row
    compares against the canonical (owner's) hash — an honest cross-device divergence —
    so a second device reporting different content for the same name shows ``conflict``
    instead of a misleading ``imported``.  With no owner yet, fall back to self-history.
    """
    from .models import NSORoutePolicyState

    if state.is_materialized:
        return bool(state.content_hash) and state.content_hash != entries_hash
    canon = ownership.canonical_hash(NSORoutePolicyState, family, name)
    if canon is None:
        return bool(state.content_hash) and state.content_hash != entries_hash
    return canon != entries_hash


def _sole_owner_can_refresh(state, family, name) -> bool:
    """Return True if a divergence on *state* should auto-refresh NetBox, not conflict.

    A materialized owner that is the ONLY device reporting this name is the sole authority
    for it: a divergence in its OWN capture is just the device's config moving, not a
    cross-device adoption ambiguity (there is no other version to be ambiguous with). In
    that case NetBox should track the change (full-replace from the new capture) instead of
    freezing the row in ``conflict`` forever — once conflicted, ``should_fill`` stays False
    and the owner could never recover. Restricted to UNOWNED rows: an operator-owned row
    (accepted/deploying/in_sync/apply_failed) is intent and is never auto-clobbered.
    """
    from . import status_machine as sm
    from .models import NSORoutePolicyState

    if not state.is_materialized or sm.is_owned(state.status):
        return False
    others = NSORoutePolicyState.objects.filter(family=family, object_name=name).exclude(pk=state.pk)
    return not others.exists()


def _refresh_owner(state, family, obj, ct, captured, entries_hash, now) -> None:
    """Re-materialize a sole owner's NetBox object from its current capture (full replace).

    The reconcile already runs under ``suppress_intent_push`` so the object saves don't fire
    the operator-edit push handlers; this just refreshes the mirror to match the device.
    """
    from . import status_machine as sm

    spec = ownership.get_spec(family)
    if spec is not None:
        spec.fill(obj, captured)
    state.status = sm.IMPORTED
    state.content_hash = entries_hash
    state.captured = captured
    state.last_sync_at = now
    state.content_type = ct
    state.object_id = obj.pk
    state.is_materialized = True
    state.device_present = True
    state.save(
        update_fields=[
            "status",
            "content_hash",
            "captured",
            "last_sync_at",
            "content_type",
            "object_id",
            "is_materialized",
            "device_present",
        ]
    )


def _upsert_state(mgmt, family, name, obj, ct, captured, now):
    """Create/update the NSORoutePolicyState overlay row. Returns (state, should_fill).

    should_fill is True when it is safe to fill the shared object's entries from THIS
    device — a fresh import, or a non-owner whose capture matches the canonical version.
    A divergence sets status=conflict and should_fill stays False (no silent clobber) —
    EXCEPT for a sole-device materialized owner, the only authority for its name, whose own
    device edits are tracked in place (see :func:`_sole_owner_can_refresh`). The device's
    own ``captured`` is always refreshed so every version stays visible.
    """
    from .models import NSORoutePolicyState

    entries_hash = ownership.hash_captured(family, captured)
    state, new_row = NSORoutePolicyState.objects.get_or_create(
        management=mgmt,
        family=family,
        object_name=name,
        defaults={
            "content_type": ct,
            "object_id": obj.pk,
            "content_hash": entries_hash,
            "captured": captured,
            "status": "imported",
            "last_sync_at": now,
        },
    )
    if new_row:
        from . import status_machine as sm

        # A brand-new row for an object another device already materialized: imported if
        # it matches that canonical version, conflict if it diverges (and don't refill).
        canon = ownership.canonical_hash(NSORoutePolicyState, family, name)
        if canon is not None and canon != entries_hash:
            state.status = sm.CONFLICT
            state.save(update_fields=["status"])
            return state, False
        return state, True

    from . import status_machine as sm

    # FK/content overlay: 'matches' = materialized (content recorded & unchanged), not
    # device confirmation, so it must not settle an owned row (settles_owned=False).
    diverged = _row_diverged(state, entries_hash, family, name)
    # A sole-device materialized owner is the only authority for this name: track its own
    # device's edits (full-replace) instead of freezing it in a (non-existent) conflict.
    if diverged and _sole_owner_can_refresh(state, family, name):
        _refresh_owner(state, family, obj, ct, captured, entries_hash, now)
        return state, False
    state.status = sm.on_reconcile(state.status, matches=not diverged, conflict=diverged, settles_owned=False)
    should_fill = state.status != sm.CONFLICT
    if should_fill:
        state.content_hash = entries_hash
    state.captured = captured  # always refresh this device's own version (display)
    state.last_sync_at = now
    state.content_type = ct
    state.object_id = obj.pk
    state.device_present = True  # the device reported it this pass (flips back if it had vanished)
    state.save(
        update_fields=[
            "status",
            "content_hash",
            "captured",
            "last_sync_at",
            "content_type",
            "object_id",
            "device_present",
        ]
    )
    return state, should_fill


def _needs_fill(EntryModel, created: bool, should_fill: bool, **filt) -> bool:
    """Decide whether to (re)fill a parent's entries.

    True when the parent is brand new or an empty shell — but never when the overlay
    flagged a conflict (should_fill is False then).
    """
    if not should_fill:
        return False
    if created:
        return True
    return not EntryModel.objects.filter(**filt).exists()


# ---------------------------------------------------------------------------
# Per-family handlers
# ---------------------------------------------------------------------------


def _reconcile_prefix_lists(mgmt, device, pl_list, PrefixList, ContentType, now, seen_keys, name_map):
    from netbox_routing.models import PrefixListEntry

    ct = ContentType.objects.get_for_model(PrefixList)
    for pl_data in pl_list:
        name = pl_data.get("name", "")
        if not name:
            continue
        entries = pl_data.get("entries", []) or []
        pl_obj, created = _get_or_create_named(PrefixList, name)
        name_map[name] = pl_obj
        state, should_fill = _upsert_state(mgmt, "prefix_list", name, pl_obj, ct, pl_data, now)
        if _needs_fill(PrefixListEntry, created, should_fill, prefix_list=pl_obj):
            _fill_prefix_list_entries(pl_obj, entries)
            ownership.mark_materialized(state)
        seen_keys.add(("prefix_list", name))


def _reconcile_community_lists(mgmt, device, cl_list, CommunityList, ContentType, now, seen_keys, name_map):
    from netbox_routing.models import CommunityListEntry

    ct = ContentType.objects.get_for_model(CommunityList)
    for cl_data in cl_list:
        name = cl_data.get("name", "")
        if not name:
            continue
        entries = cl_data.get("entries", []) or []
        invert_match = bool(cl_data.get("invert_match", False))
        cl_obj, created = _get_or_create_named(CommunityList, name, invert_match=invert_match)
        name_map[name] = cl_obj
        # Hash is invert_match-aware via the registered spec (_cl_hash); a non-inverted
        # list keeps the plain-entries hash so it doesn't false-drift, an invert flip drifts.
        state, should_fill = _upsert_state(mgmt, "community_list", name, cl_obj, ct, cl_data, now)
        # invert_match is device-sourced config — refresh it on any non-conflicting read
        # (a conflicting read leaves should_fill False, so an owned/diverged row is untouched).
        if should_fill and cl_obj.invert_match != invert_match:
            cl_obj.invert_match = invert_match
            cl_obj.save(update_fields=["invert_match"])
        if _needs_fill(CommunityListEntry, created, should_fill, community_list=cl_obj):
            _fill_community_list_entries(cl_obj, entries)
            ownership.mark_materialized(state)
        seen_keys.add(("community_list", name))


def _reconcile_as_paths(mgmt, device, ap_list, ASPath, ContentType, now, seen_keys, name_map):
    from netbox_routing.models import ASPathEntry

    ct = ContentType.objects.get_for_model(ASPath)
    for ap_data in ap_list:
        name = ap_data.get("name", "")
        if not name:
            continue
        entries = ap_data.get("entries", []) or []
        ap_obj, created = _get_or_create_named(ASPath, name)
        name_map[name] = ap_obj
        state, should_fill = _upsert_state(mgmt, "as_path", name, ap_obj, ct, ap_data, now)
        if _needs_fill(ASPathEntry, created, should_fill, aspath=ap_obj):
            _fill_as_path_entries(ap_obj, entries)
            ownership.mark_materialized(state)
        seen_keys.add(("as_path", name))


def _reconcile_route_maps(mgmt, device, rm_list, RouteMap, ContentType, now, seen_keys, pl_map, cl_map, ap_map):
    from netbox_routing.models import RouteMapEntry

    ct = ContentType.objects.get_for_model(RouteMap)
    for rm_data in rm_list:
        name = rm_data.get("name", "")
        if not name:
            continue
        entries = rm_data.get("entries", []) or []
        rm_obj, created = _get_or_create_named(RouteMap, name)
        state, should_fill = _upsert_state(mgmt, "route_map", name, rm_obj, ct, rm_data, now)
        if _needs_fill(RouteMapEntry, created, should_fill, route_map=rm_obj):
            _fill_route_map_entries(rm_obj, entries, pl_map, cl_map, ap_map)
            ownership.mark_materialized(state)
        seen_keys.add(("route_map", name))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reconcile_route_policy(device, payload: dict) -> list:
    """Reconcile route-policy data (objects + entries) from the adapter into NetBox.

    Runs under ``suppress_intent_push()``: this reconcile MATERIALIZES netbox-routing fork
    objects (CommunityList/RouteMap/... + their entries) from device state, and those saves
    would otherwise fire the operator-edit push handlers (own + push). Suppression keeps the
    import side-effect-free; it is reentrant, so this is safe whether or not the caller
    (reconcile_device) already suppresses. Returns NSORoutePolicyState instances for the device.
    """
    from .signals import suppress_intent_push

    with suppress_intent_push():
        return _reconcile_route_policy(device, payload)


def _reconcile_route_policy(device, payload: dict) -> list:
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    try:
        from netbox_routing.models import ASPath, CommunityList, PrefixList, RouteMap
    except ImportError:
        logger.warning("netbox_routing not installed; skipping route-policy reconcile")
        return []

    from .models import NSODeviceManagement, NSORoutePolicyState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    now = timezone.now()
    seen_keys: set[tuple] = set()
    pl_map: dict[str, object] = {}
    cl_map: dict[str, object] = {}
    ap_map: dict[str, object] = {}

    _reconcile_prefix_lists(
        mgmt, device, payload.get("prefix_lists", []), PrefixList, ContentType, now, seen_keys, pl_map
    )
    _reconcile_community_lists(
        mgmt, device, payload.get("community_lists", []), CommunityList, ContentType, now, seen_keys, cl_map
    )
    _reconcile_as_paths(mgmt, device, payload.get("as_paths", []), ASPath, ContentType, now, seen_keys, ap_map)
    # Route-maps last: their match M2Ms reference the objects created above.
    _reconcile_route_maps(
        mgmt,
        device,
        payload.get("route_maps", []),
        RouteMap,
        ContentType,
        now,
        seen_keys,
        pl_map,
        cl_map,
        ap_map,
    )

    # Mark stale rows as drift: the device stopped reporting them. Record device_present=False
    # (so the drift delta shows a real removal, not the stale capture falsely matching the
    # object) and advance the status (accepted/deploying intent preserved by on_reconcile).
    from . import status_machine as sm

    for state in NSORoutePolicyState.objects.filter(management=mgmt):
        if (state.family, state.object_name) in seen_keys:
            continue
        fields = []
        new_status = sm.on_reconcile(state.status, present=False)
        if new_status != state.status:
            state.status = new_status
            fields.append("status")
        if state.device_present:
            state.device_present = False
            fields.append("device_present")
        if fields:
            state.save(update_fields=fields)

    return list(NSORoutePolicyState.objects.filter(management=mgmt).order_by("family", "object_name"))
