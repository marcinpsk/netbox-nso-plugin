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
import re

logger = logging.getLogger(__name__)


def _hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


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


# RFC 1997 + common well-known community keywords → numeric form (so they dedup
# with the numeric members and fit the numeric-only Community model).
_WELL_KNOWN_COMMUNITIES = {
    "no-export": "65535:65281",
    "no-advertise": "65535:65282",
    "no-export-subconfed": "65535:65283",
    "local-as": "65535:65283",
    "internet": "0:0",
}

# Extended/typed community prefixes → ExtendedCommunityTypeChoices value.
_EXT_COMMUNITY_TYPES = {
    "target": "route-target",
    "route-target": "route-target",
    "rt": "route-target",
    "origin": "route-origin",
    "route-origin": "route-origin",
    "soo": "route-origin",
    "color": "color",
    "bandwidth": "bandwidth",
    "encapsulation": "encapsulation",
}

# Reverse of _EXT_COMMUNITY_TYPES for the WRITE path: ExtendedCommunityTypeChoices value
# → the canonical device member prefix. The forward map is many-to-one (target/rt/
# route-target all normalise to "route-target"), so the original keyword is lost; we emit
# the canonical device keyword ("target:6830:100" etc.), which the NEDs accept and which
# round-trips against what the read path captured.
_EXT_TYPE_TO_DEVICE_PREFIX = {
    "route-target": "target",
    "route-origin": "origin",
    "color": "color",
    "bandwidth": "bandwidth",
    "encapsulation": "encapsulation",
}

# One colon-separated part of a community value: digits, dots and regex/wildcard
# metacharacters. Mirrors the relaxed netbox_routing Community/ExtendedCommunity
# validators so anything we classify as standard/extended also validates there.
# A value is up to 3 such parts (4+ is not a valid community).
_COMMUNITY_PART = r"[\d.*^$()\[\]|+?\\_-]+"
_STD_OR_REGEX_RE = re.compile(rf"^{_COMMUNITY_PART}(?::{_COMMUNITY_PART}){{0,2}}$")


def _classify_community(value: str):
    """Classify a community-list member value.

    Returns one of:
      ("standard", value)                — fits netbox_routing.Community (exact OR regex)
      ("extended", ext_type, ext_value)  — fits netbox_routing.ExtendedCommunity
      ("skip", reason)                   — unparseable / unsupported (e.g. large:); dropped

    Regex / wildcard members (Cisco expanded community-lists, Nokia/Junos inline —
    6830:*, 6830:.*, 6830:1113.) are accepted: they are match-only but must round-trip,
    and the netbox_routing validators now permit the metacharacters.
    """
    v = (value or "").strip()
    if not v:
        return ("skip", "empty")
    if v.lower() in _WELL_KNOWN_COMMUNITIES:
        return ("standard", _WELL_KNOWN_COMMUNITIES[v.lower()])
    # Extended/typed prefix (target:/origin:/...) — the remainder may be exact or regex.
    # Checked before the standard branch because typed prefixes carry letters and never
    # match _STD_OR_REGEX_RE.
    if ":" in v:
        prefix, _, rest = v.partition(":")
        etype = _EXT_COMMUNITY_TYPES.get(prefix.lower())
        if etype and _STD_OR_REGEX_RE.match(rest):
            return ("extended", etype, rest)
    # Standard exact (6830:100) or inline regex/wildcard (6830:*, 6830:1113.).
    if _STD_OR_REGEX_RE.match(v):
        return ("standard", v)
    return ("skip", f"unsupported community member {v!r}")


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

    Standard members go to Community; extended/typed members are routed into a parallel
    ExtendedCommunityList of the same name. Regex/wildcard members are logged and dropped
    (they don't fit either model).
    """
    from netbox_routing.models import Community, CommunityListEntry

    try:
        from netbox_routing.models import (
            ExtendedCommunity,
            ExtendedCommunityList,
            ExtendedCommunityListEntry,
        )

        have_ext = True
    except ImportError:
        have_ext = False

    CommunityListEntry.objects.filter(community_list=cl_obj).delete()
    ext_parent = None
    std_rows = []
    for e in entries:
        kind = _classify_community(e.get("community", ""))
        action = _norm_action(e.get("action"))
        if kind[0] == "standard":
            comm, _ = Community.objects.get_or_create(community=kind[1])
            std_rows.append(CommunityListEntry(community_list=cl_obj, action=action, community=comm))
        elif kind[0] == "extended" and have_ext:
            if ext_parent is None:
                ext_parent, _ = ExtendedCommunityList.objects.get_or_create(name=cl_obj.name)
                ExtendedCommunityListEntry.objects.filter(extended_community_list=ext_parent).delete()
            ec, _ = ExtendedCommunity.objects.get_or_create(type=kind[1], value=kind[2])
            ExtendedCommunityListEntry.objects.create(
                extended_community_list=ext_parent, action=action, extended_community=ec
            )
        else:
            reason = kind[1] if kind[0] == "skip" else "no ExtendedCommunity model"
            logger.info("route-policy: skipping community member in %s (%s)", cl_obj.name, reason)
    if std_rows:
        CommunityListEntry.objects.bulk_create(std_rows)


def _fill_route_map_entries(rm_obj, entries: list, pl_by_name, cl_by_name, ap_by_name, ext_cl_by_name) -> None:
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
            # A community-list whose members are extended/typed lives in a parallel
            # ExtendedCommunityList of the same name — link that too (its CommunityList
            # shell may be empty).
            ext = ext_cl_by_name.get(nm)
            if ext is not None:
                rme.match_extended_community_list.add(ext)
            if obj is None and ext is None:
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
        pl_obj, created = PrefixList.objects.get_or_create(name=name)
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
        cl_obj, created = CommunityList.objects.get_or_create(name=name)
        name_map[name] = cl_obj
        state, should_fill = _upsert_state(mgmt, "community_list", name, cl_obj, ct, _hash(entries), now)
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
        ap_obj, created = ASPath.objects.get_or_create(name=name)
        name_map[name] = ap_obj
        state, should_fill = _upsert_state(mgmt, "as_path", name, ap_obj, ct, _hash(entries), now)
        if _needs_fill(ASPathEntry, created, should_fill, aspath=ap_obj):
            _fill_as_path_entries(ap_obj, entries)
        seen_keys.add(("as_path", name))


def _reconcile_route_maps(
    mgmt, device, rm_list, RouteMap, ContentType, now, seen_keys, pl_map, cl_map, ap_map, ext_cl_map
):
    from netbox_routing.models import RouteMapEntry

    ct = ContentType.objects.get_for_model(RouteMap)
    for rm_data in rm_list:
        name = rm_data.get("name", "")
        if not name:
            continue
        entries = rm_data.get("entries", []) or []
        rm_obj, created = RouteMap.objects.get_or_create(name=name)
        state, should_fill = _upsert_state(mgmt, "route_map", name, rm_obj, ct, _hash(entries), now)
        if _needs_fill(RouteMapEntry, created, should_fill, route_map=rm_obj):
            _fill_route_map_entries(rm_obj, entries, pl_map, cl_map, ap_map, ext_cl_map)
        seen_keys.add(("route_map", name))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reconcile_route_policy(device, payload: dict) -> list:
    """Reconcile route-policy data (objects + entries) from the adapter into NetBox.

    Returns a list of NSORoutePolicyState instances for this device.
    """
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
    # Extended community-lists parallel the community-lists by name (created by the
    # community-list reconcile above for target:/typed members); map them for route-map
    # match linking. Empty if the fork lacks the ExtendedCommunityList model.
    ext_cl_map: dict[str, object] = {}
    try:
        from netbox_routing.models import ExtendedCommunityList

        ext_cl_map = {ecl.name: ecl for ecl in ExtendedCommunityList.objects.filter(name__in=cl_map.keys())}
    except ImportError:
        pass
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
        ext_cl_map,
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
