# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Route-policy reconciler for M17 A4.

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


def _fill_route_map_entries(rm_obj, entries: list, pl_by_name, cl_by_name, ap_by_name) -> None:
    from netbox_routing.models import RouteMapEntry

    RouteMapEntry.objects.filter(route_map=rm_obj).delete()
    # Positional sequence — unique per route-map and smallint-safe (the device sequence
    # can exceed the field's range; see _fill_prefix_list_entries).
    for i, e in enumerate(entries, start=1):
        # flow_control (IOS route-map `continue`) rides inside set-json (no dedicated
        # adapter leaf) — lift it into the model field and keep it out of the set blob.
        set_data = _load_json(e.get("set"))
        flow_control = set_data.pop("flow_control", None)
        rme = RouteMapEntry.objects.create(
            route_map=rm_obj,
            sequence=i,
            action=_norm_action(e.get("action")),
            flow_control=flow_control,
            match=_load_json(e.get("match")),
            set=set_data,
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


# ---------------------------------------------------------------------------
# Overlay upsert (shared by every family)
# ---------------------------------------------------------------------------

_FILL_STATUSES = ("imported", "in_sync")  # safe to (re)fill entries in these states


def _upsert_state(mgmt, family, name, obj, ct, entries_hash, now):
    """Create/update the NSORoutePolicyState overlay row. Returns (state, should_fill).

    should_fill is True when this is a fresh import (or the row matches) — i.e. it is
    safe to fill entries. A divergent hash on an already-imported object sets
    status=conflict and should_fill stays False (no silent clobber).
    """
    from .models import NSORoutePolicyState

    state, new_row = NSORoutePolicyState.objects.get_or_create(
        management=mgmt,
        family=family,
        object_name=name,
        defaults={
            "content_type": ct,
            "object_id": obj.pk,
            "content_hash": entries_hash,
            "status": "imported",
            "last_sync_at": now,
        },
    )
    if new_row:
        return state, True

    from . import status_machine as sm

    # FK/content overlay: 'matches' = materialized (content_hash recorded & unchanged),
    # not device confirmation, so it must not settle an owned row (settles_owned=False).
    # Divergence is an adoption conflict for an unowned row.
    diverged = bool(state.content_hash) and state.content_hash != entries_hash
    state.status = sm.on_reconcile(state.status, matches=not diverged, conflict=diverged, settles_owned=False)
    should_fill = state.status != sm.CONFLICT
    if should_fill:
        state.content_hash = entries_hash
    state.last_sync_at = now
    state.content_type = ct
    state.object_id = obj.pk
    state.save(update_fields=["status", "content_hash", "last_sync_at", "content_type", "object_id"])
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
        state, should_fill = _upsert_state(mgmt, "prefix_list", name, pl_obj, ct, _hash(entries), now)
        if _needs_fill(PrefixListEntry, created, should_fill, prefix_list=pl_obj):
            _fill_prefix_list_entries(pl_obj, entries)
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
        # Backward-compatible hash: keep the plain-entries hash for non-inverted lists
        # (the common case) so they don't all false-drift on the first poll after this
        # field shipped; an invert_match flip still changes the hash → drift detected.
        hash_input = {"invert_match": True, "entries": entries} if invert_match else entries
        state, should_fill = _upsert_state(mgmt, "community_list", name, cl_obj, ct, _hash(hash_input), now)
        # invert_match is device-sourced config — refresh it on any non-conflicting read
        # (a conflicting read leaves should_fill False, so an owned/diverged row is untouched).
        if should_fill and cl_obj.invert_match != invert_match:
            cl_obj.invert_match = invert_match
            cl_obj.save(update_fields=["invert_match"])
        if _needs_fill(CommunityListEntry, created, should_fill, community_list=cl_obj):
            _fill_community_list_entries(cl_obj, entries)
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
        state, should_fill = _upsert_state(mgmt, "as_path", name, ap_obj, ct, _hash(entries), now)
        if _needs_fill(ASPathEntry, created, should_fill, aspath=ap_obj):
            _fill_as_path_entries(ap_obj, entries)
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
        state, should_fill = _upsert_state(mgmt, "route_map", name, rm_obj, ct, _hash(entries), now)
        if _needs_fill(RouteMapEntry, created, should_fill, route_map=rm_obj):
            _fill_route_map_entries(rm_obj, entries, pl_map, cl_map, ap_map)
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

    # Mark stale rows as drift (accepted/deploying intent is preserved by on_reconcile).
    from . import status_machine as sm

    for state in NSORoutePolicyState.objects.filter(management=mgmt):
        if (state.family, state.object_name) in seen_keys:
            continue
        new_status = sm.on_reconcile(state.status, present=False)
        if new_status != state.status:
            state.status = new_status
            state.save(update_fields=["status"])

    return list(NSORoutePolicyState.objects.filter(management=mgmt).order_by("family", "object_name"))
