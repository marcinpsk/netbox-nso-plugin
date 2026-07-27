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
from contextvars import ContextVar

from . import shared_object_ownership as ownership
from .route_policy_structure import canonical_route_map, prefix_list_entry_unit

logger = logging.getLogger(__name__)

# name → tuple of prefix-list units, memoized for one reconcile pass (cleared at its start).
# Lets canonical_route_map expand prefix-list refs to content without re-querying per call.
_PL_UNIT_CACHE: ContextVar[dict[str, tuple] | None] = ContextVar("route_policy_prefix_units", default=None)


def _resolve_prefix_list_units(name: str) -> tuple:
    """Resolve a prefix-list NAME to its content as ``(action, prefix, ge, le)`` units.

    Reads the GLOBAL materialized version (one NetBox object per name; falls back to any
    device's capture) so both devices' route-maps expand a shared list to the SAME units —
    the comparison stays about route-map content, not which box reported the list. A name with
    no captured prefix-list yet resolves to empty (the term simply has nothing to expand).
    """
    key = name.lower()
    cache = _PL_UNIT_CACHE.get()
    if cache is None:
        cache = {}
        _PL_UNIT_CACHE.set(cache)
    if key in cache:
        return cache[key]
    from .models import NSORoutePolicyState

    row = (
        NSORoutePolicyState.objects.filter(family="prefix_list", object_name__iexact=name, is_materialized=True).first()
        or NSORoutePolicyState.objects.filter(family="prefix_list", object_name__iexact=name).first()
    )
    units = tuple(prefix_list_entry_unit(e) for e in ((row.captured or {}).get("entries") or [])) if row else ()
    cache[key] = units
    return units


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


def _set_prefix_list_family(pl_obj, captured) -> None:
    """Mirror the owner capture's address family onto the materialized PrefixList.

    family is derived by the device reader (any v6 prefix → family 6); without this the
    netbox_routing.PrefixList kept the model default (4), so a v6 list (MARTIANS_V6,
    LGI_PREFIXES_V6, ...) displayed as IPv4 even after the reader was fixed. The dedup hash
    is entries-only, so a family-only change is a pure display correction (never drift).
    """
    family = captured.get("family")
    if family in (4, 6) and pl_obj.family != family:
        pl_obj.family = family
        pl_obj.save(update_fields=["family"])


def _fill_prefix_list(pl_obj, captured) -> None:
    """Materialize a PrefixList from a device capture: address family + entries."""
    _set_prefix_list_family(pl_obj, captured)
    _fill_prefix_list_entries(pl_obj, _entries(captured))


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


def _extract_prefix_list(pl_obj) -> dict:
    """Reverse of _fill_prefix_list: CURRENT object content in device-capture shape (#93).

    Key-compatible with the capture entries the fill consumes (prefix/action/ge/le);
    sequences are positional artifacts and are renumbered by the comparator.
    """
    entries = []
    for e in pl_obj.prefix_list_entries.all().order_by("sequence"):
        cp = e.assigned_prefix
        if cp is None:
            continue
        entry = {"sequence": e.sequence, "action": (e.action or "permit").lower(), "prefix": str(cp.prefix)}
        if getattr(e, "ge", None) is not None:
            entry["ge"] = e.ge
        if getattr(e, "le", None) is not None:
            entry["le"] = e.le
        entries.append(entry)
    return {"entries": entries}


def _extract_community_list(cl_obj) -> dict:
    """Reverse of _cl_fill: members verbatim + invert_match, capture-shaped (#93)."""
    entries = []
    seq = 0
    for e in cl_obj.communitylistentries.all():
        if not e.community_id:
            continue
        seq += 1
        entries.append(
            {"sequence": seq, "action": (e.action or "permit").lower(), "community": str(e.community.community)}
        )
    return {"entries": entries, "invert_match": bool(cl_obj.invert_match)}


def _extract_as_path(ap_obj) -> dict:
    """Reverse of _fill_as_path_entries (#93). The fill's key is ``pattern``."""
    return {
        "entries": [
            {"sequence": e.sequence, "action": (e.action or "permit").lower(), "pattern": e.pattern or ""}
            for e in ap_obj.aspath_entries.all().order_by("sequence")
        ]
    }


def _extract_route_map(rm_obj) -> dict:
    """Reverse of _rm_fill, capture-shaped (#100).

    match/set blobs are stored VERBATIM by the fill, so returning them verbatim yields an
    identical canonical_route_map projection (which drops sequences and sorts the name
    refs — M2M order is irrelevant); flow_control is re-lifted into set-json (the fill
    popped it into the model field); the synthetic default-action entry was kept by the
    fill and rides along like any entry.
    """
    entries = []
    for e in rm_obj.route_map_entries.all().order_by("sequence"):
        set_data = dict(e.set or {})
        if e.flow_control is not None and "flow_control" not in set_data:
            set_data["flow_control"] = e.flow_control
        entries.append(
            {
                "seq": e.sequence,
                "action": (e.action or "permit").lower(),
                "match": dict(e.match or {}),
                "set": set_data,
                "match_prefix_lists": sorted(e.match_prefix_list.values_list("name", flat=True)),
                "match_community_lists": sorted(e.match_community_list.values_list("name", flat=True)),
                "match_as_paths": sorted(e.match_aspath.values_list("name", flat=True)),
            }
        )
    return {"entries": entries}


def _register_specs() -> None:
    Spec = ownership.SharedObjectSpec
    ownership.register(
        "prefix_list",
        Spec(fill=_fill_prefix_list, hash_captured=lambda c: _hash(_entries(c)), extract=_extract_prefix_list),
    )
    ownership.register("community_list", Spec(fill=_cl_fill, hash_captured=_cl_hash, extract=_extract_community_list))
    ownership.register(
        "as_path",
        Spec(
            fill=lambda o, c: _fill_as_path_entries(o, _entries(c)),
            hash_captured=lambda c: _hash(_entries(c)),
            extract=_extract_as_path,
        ),
    )
    # Route-maps dedup on a VENDOR-NEUTRAL SEMANTIC digest (not the raw entries): the same
    # logical policy spelled in Junos vs Nokia encoding (term/terminal labels, family
    # spelling, scalar-vs-leaf-list, fall-through verb, as-path-group placement) converges
    # instead of showing false cross-vendor conflict. Prefix matches expand to their CONTENT
    # (via _resolve_prefix_list_units) so a Junos inline route-filter set converges with the
    # equivalent named-list refs. Genuine differences keep a distinct digest. See
    # route_policy_structure.canonical_route_map.
    ownership.register(
        "route_map",
        Spec(
            fill=_rm_fill,
            hash_captured=lambda c: _hash(canonical_route_map(c, _resolve_prefix_list_units)),
            extract=_extract_route_map,
        ),
    )


_register_specs()


# ---------------------------------------------------------------------------
# Overlay upsert (shared by every family)
# ---------------------------------------------------------------------------

_FILL_STATUSES = ("imported", "in_sync")  # safe to (re)fill entries in these states


def _lock_policy_groups(groups: set[tuple[str, str]]) -> None:
    """Take deterministic transaction locks for shared route-policy namespaces."""
    from django.db import connection

    if connection.vendor != "postgresql":
        return
    normalized = {(family, object_name.casefold()) for family, object_name in groups}
    with connection.cursor() as cursor:
        for family, object_name in sorted(normalized):
            digest = hashlib.sha256(f"nso-route-policy:{family}:{object_name}".encode()).digest()[:8]
            lock_id = int.from_bytes(digest, byteorder="big", signed=True)
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


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


def _owner_can_refresh(state) -> bool:
    """Return True if a divergence on *state* should auto-refresh NetBox, not conflict.

    NetBox mirrors exactly ONE version per (family, name): the materialized owner's. When
    that owner's OWN device changes, NetBox should track it — the row is just a device mirror
    until an operator accepts it, so following the source it already tracks is correct, not a
    clobber. This holds whether or not other devices share the name: only the owner ever
    writes the shared object (non-owners never materialize), so there is no last-writer churn,
    and a non-owner that diverges from the (now updated) version still surfaces as ``conflict``
    on its next reconcile. Two invariants keep it safe:

    - UNOWNED only — an operator-owned row (accepted/deploying/in_sync/apply_failed) is intent
      and is never auto-clobbered;
    - OWNER only — a non-owner divergence is a genuine cross-device conflict, left untouched.

    (Without this an unowned owner whose device changed froze in ``conflict`` forever, since
    ``should_fill`` then stays False and the owner could never recover.)
    """
    from . import status_machine as sm

    return state.is_materialized and not sm.is_owned(state.status)


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
    EXCEPT for the unowned materialized owner (the one version NetBox mirrors), whose own
    device changes are tracked in place (see :func:`_owner_can_refresh`). The device's own
    ``captured`` is always refreshed so every version stays visible.
    """
    from .models import NSORoutePolicyState

    entries_hash = ownership.hash_captured(family, captured)
    state = NSORoutePolicyState.objects.filter(
        management=mgmt,
        family=family,
        object_name__iexact=name,
    ).first()
    new_row = state is None
    if state is None:
        state = NSORoutePolicyState.objects.create(
            management=mgmt,
            family=family,
            object_name=name,
            content_type=ct,
            object_id=obj.pk,
            content_hash=entries_hash,
            captured=captured,
            status="imported",
            last_sync_at=now,
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

    # #93 — device-caught-up settle for OWNED rows: the operator's intent IS the current
    # NetBox object; when THIS device's capture equals it, the device has caught up —
    # genuine confirmation, so it may settle accepted/deploying/apply_failed → in_sync.
    # (The materialized-content 'matches' below can never see this: settles_owned=False
    # kept staged intent pending forever — example-comm sat 26 days already satisfied.)
    # route_map has no extractor yet (push shape ≠ capture shape) → None → no settle.
    if sm.is_owned(state.status) and ownership.device_caught_up(
        family,
        captured,
        obj,
        exclude_members=(list(state.unsupported_members or []) or None) if family == "community_list" else None,
    ):
        state.status = sm.on_reconcile(state.status, matches=True, settles_owned=True)

    # FK/content overlay: 'matches' = materialized (content recorded & unchanged), not
    # device confirmation, so it must not settle an owned row (settles_owned=False).
    diverged = _row_diverged(state, entries_hash, family, name)
    # The materialized owner is the one version NetBox mirrors for this name: when its own
    # device changes and the row is unowned, track it (full-replace) instead of freezing as
    # conflict. Non-owners that diverge are still genuine cross-device conflicts.
    if diverged and _owner_can_refresh(state):
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
# MASTER vs LOCAL classification (see docs/master-vs-local-route-policy.md)
# ---------------------------------------------------------------------------


def _group_mode(family: str, object_name: str) -> str:
    """Classification for a route-policy object group: 'master' (default) or 'local'.

    Absence of a NSORoutePolicyObjectClass row == implicit MASTER (auto-dedup, the default).
    """
    from .models import NSORoutePolicyObjectClass

    # Case-insensitive to match the object dedup (name__iexact): otherwise a peer device
    # reporting a different case (ACCEPT-ALL vs accept-all — the same shared object) misses the
    # operator's stored classification and silently reverts to implicit MASTER.
    row = NSORoutePolicyObjectClass.objects.filter(family=family, object_name__iexact=object_name).first()
    return row.mode if row else "master"


def _family_model(family: str):
    """Return the netbox-routing model class for a route-policy family (None if unknown)."""
    from netbox_routing.models import ASPath, CommunityList, PrefixList, RouteMap

    return {"prefix_list": PrefixList, "community_list": CommunityList, "as_path": ASPath, "route_map": RouteMap}.get(
        family
    )


def _promote_group_to_master(family, object_name, now) -> None:
    """Re-materialize a group as MASTER from the stored per-device captures.

    Fills the shared netbox-routing object from the version with the most entries, links every
    device row to it, and recomputes each sibling's conflict status against it. Owned rows
    (accepted/deploying/in_sync/apply_failed) are left alone.
    """
    from django.contrib.contenttypes.models import ContentType

    from . import status_machine as sm
    from .models import NSORoutePolicyState

    model = _family_model(family)
    if model is None:
        return
    rows = [
        r
        for r in NSORoutePolicyState.objects.filter(family=family, object_name__iexact=object_name).select_related(
            "management"
        )
        if r.captured
    ]
    if not rows:
        return
    spec = ownership.get_spec(family)
    ct = ContentType.objects.get_for_model(model)
    owner = max(rows, key=lambda r: len((r.captured or {}).get("entries") or []))
    obj, _ = _get_or_create_named(model, object_name)
    spec.fill(obj, owner.captured)
    owner_hash = ownership.hash_captured(family, owner.captured)
    for r in rows:
        r.content_type = ct
        r.object_id = obj.pk
        r_hash = ownership.hash_captured(family, r.captured)
        r.content_hash = r_hash
        if r.pk == owner.pk:
            r.is_materialized = True
            if not sm.is_owned(r.status):
                r.status = sm.IMPORTED
        else:
            r.is_materialized = False
            if not sm.is_owned(r.status):
                r.status = sm.CONFLICT if r_hash != owner_hash else sm.IMPORTED
        r.save()


def set_classification(family: str, object_name: str, mode: str):
    """Operator action: classify a route-policy object group MASTER or LOCAL (re-processed now).

    Re-processes the existing per-device captures so the change takes effect immediately (no
    device read). LOCAL → de-materialize every device row + clear cross-device conflicts
    (captured-only). MASTER → re-materialize an owner from the group's captures + re-compare.
    """
    from django.utils import timezone

    from .models import NSORoutePolicyObjectClass, NSORoutePolicyState
    from .signals import suppress_intent_push

    if mode not in ("master", "local"):
        raise ValueError(f"invalid mode {mode!r}")
    obj = NSORoutePolicyObjectClass.objects.filter(family=family, object_name__iexact=object_name).first()
    if obj is None:
        obj = NSORoutePolicyObjectClass.objects.create(
            family=family,
            object_name=object_name,
            mode=mode,
            source="operator",
        )
    else:
        obj.mode = mode
        obj.source = "operator"
        obj.save(update_fields=["mode", "source"])
    now = timezone.now()
    with suppress_intent_push():
        if mode == "local":
            rows = NSORoutePolicyState.objects.filter(family=family, object_name__iexact=object_name).select_related(
                "management"
            )
            for r in rows:
                _upsert_local_state(r.management, family, object_name, r.captured or {}, now)
        else:
            _promote_group_to_master(family, object_name, now)
    return obj


def resettle_false_conflicts(groups: set[tuple[str, str]] | None = None) -> int:
    """Clear stale 'conflict' rows whose content_hash now equals their materialized owner's.

    A non-owner row goes 'conflict' when it diverges from the owner; if the owner is later
    re-materialized to matching content (or the row re-converges) but that device is not
    re-read, the status stays conflict. This recompute settles any such row whose hash now
    matches the canonical owner — without a device round-trip. Returns the count cleared.
    """
    from django.db import transaction

    from . import status_machine as sm
    from .models import NSORoutePolicyState

    cleared = 0
    if groups is not None:
        if not groups:
            return 0
    else:
        groups = set(
            NSORoutePolicyState.objects.filter(status=sm.CONFLICT, is_materialized=False).values_list(
                "family", "object_name"
            )
        )
    for family, object_name in sorted(groups):
        with transaction.atomic():
            _lock_policy_groups({(family, object_name)})
            states = NSORoutePolicyState.objects.select_for_update().filter(
                family=family,
                object_name__iexact=object_name,
                status=sm.CONFLICT,
                is_materialized=False,
            )
            canon = ownership.canonical_hash(NSORoutePolicyState, family, object_name)
            for state in states:
                if canon is not None and canon == state.content_hash:
                    state.status = sm.on_reconcile(state.status, matches=True, conflict=False, settles_owned=False)
                    state.save(update_fields=["status"])
                    cleared += 1
    return cleared


def _upsert_local_state(mgmt, family, name, captured, now):
    """Upsert a per-device (LOCAL) overlay row: captured-only, never a cross-device conflict.

    The object legitimately differs per device, so there is no shared canonical to drift
    against — each device records its own version (shown in the NSO tab). Also de-materializes
    a row left over from a prior MASTER classification.
    """
    from . import status_machine as sm
    from .models import NSORoutePolicyState

    entries_hash = ownership.hash_captured(family, captured)
    state = NSORoutePolicyState.objects.filter(
        management=mgmt,
        family=family,
        object_name__iexact=name,
    ).first()
    new_row = state is None
    if state is None:
        state = NSORoutePolicyState.objects.create(
            management=mgmt,
            family=family,
            object_name=name,
            content_hash=entries_hash,
            captured=captured,
            status="imported",
            last_sync_at=now,
        )
    if not new_row:
        changed = state.content_hash != entries_hash
        state.status = sm.on_reconcile(state.status, matches=not changed, conflict=False, settles_owned=False)
        state.content_hash = entries_hash
    # LOCAL is never materialized; drop any object link left from a prior MASTER classification.
    state.captured = captured
    state.last_sync_at = now
    state.is_materialized = False
    state.content_type = None
    state.object_id = None
    state.device_present = True
    state.save()
    return state


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
        if _group_mode("prefix_list", name) == "local":
            _upsert_local_state(mgmt, "prefix_list", name, pl_data, now)
            seen_keys.add(("prefix_list", name.casefold()))
            continue
        entries = pl_data.get("entries", []) or []
        pl_obj, created = _get_or_create_named(PrefixList, name)
        name_map[name] = pl_obj
        state, should_fill = _upsert_state(mgmt, "prefix_list", name, pl_obj, ct, pl_data, now)
        # family is device-sourced and entry-independent (not in the hash), so refresh it on any
        # non-conflicting read — _needs_fill skips an already-populated list, leaving a stale v4
        # on a v6 list. (A conflicting read leaves should_fill False → an owned/diverged row is
        # untouched; the owner-content-changed path sets it via _fill_prefix_list.) Mirrors the
        # community_list invert_match refresh below.
        if should_fill:
            _set_prefix_list_family(pl_obj, pl_data)
        if _needs_fill(PrefixListEntry, created, should_fill, prefix_list=pl_obj):
            _fill_prefix_list_entries(pl_obj, entries)
            ownership.mark_materialized(state)
        seen_keys.add(("prefix_list", name.casefold()))


def _reconcile_community_lists(mgmt, device, cl_list, CommunityList, ContentType, now, seen_keys, name_map):
    from netbox_routing.models import CommunityListEntry

    ct = ContentType.objects.get_for_model(CommunityList)
    for cl_data in cl_list:
        name = cl_data.get("name", "")
        if not name:
            continue
        if _group_mode("community_list", name) == "local":
            _upsert_local_state(mgmt, "community_list", name, cl_data, now)
            seen_keys.add(("community_list", name.casefold()))
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
        seen_keys.add(("community_list", name.casefold()))


def _reconcile_as_paths(mgmt, device, ap_list, ASPath, ContentType, now, seen_keys, name_map):
    from netbox_routing.models import ASPathEntry

    ct = ContentType.objects.get_for_model(ASPath)
    for ap_data in ap_list:
        name = ap_data.get("name", "")
        if not name:
            continue
        if _group_mode("as_path", name) == "local":
            _upsert_local_state(mgmt, "as_path", name, ap_data, now)
            seen_keys.add(("as_path", name.casefold()))
            continue
        entries = ap_data.get("entries", []) or []
        ap_obj, created = _get_or_create_named(ASPath, name)
        name_map[name] = ap_obj
        state, should_fill = _upsert_state(mgmt, "as_path", name, ap_obj, ct, ap_data, now)
        if _needs_fill(ASPathEntry, created, should_fill, aspath=ap_obj):
            _fill_as_path_entries(ap_obj, entries)
            ownership.mark_materialized(state)
        seen_keys.add(("as_path", name.casefold()))


def _reconcile_route_maps(mgmt, device, rm_list, RouteMap, ContentType, now, seen_keys, pl_map, cl_map, ap_map):
    from netbox_routing.models import RouteMapEntry

    ct = ContentType.objects.get_for_model(RouteMap)
    for rm_data in rm_list:
        name = rm_data.get("name", "")
        if not name:
            continue
        if _group_mode("route_map", name) == "local":
            _upsert_local_state(mgmt, "route_map", name, rm_data, now)
            seen_keys.add(("route_map", name.casefold()))
            continue
        entries = rm_data.get("entries", []) or []
        rm_obj, created = _get_or_create_named(RouteMap, name)
        state, should_fill = _upsert_state(mgmt, "route_map", name, rm_obj, ct, rm_data, now)
        if _needs_fill(RouteMapEntry, created, should_fill, route_map=rm_obj):
            _fill_route_map_entries(rm_obj, entries, pl_map, cl_map, ap_map)
            ownership.mark_materialized(state)
        seen_keys.add(("route_map", name.casefold()))


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
    from django.db import transaction

    from .signals import suppress_intent_push

    with suppress_intent_push(), transaction.atomic():
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

    family_payload_keys = {
        "prefix_list": "prefix_lists",
        "community_list": "community_lists",
        "as_path": "as_paths",
        "route_map": "route_maps",
    }
    touched_groups = {
        (family, row.get("name", ""))
        for family, key in family_payload_keys.items()
        for row in payload.get(key) or []
        if row.get("name")
    }
    touched_groups.update(NSORoutePolicyState.objects.filter(management=mgmt).values_list("family", "object_name"))
    _lock_policy_groups(touched_groups)

    now = timezone.now()
    _PL_UNIT_CACHE.set({})  # context-local: concurrent device reconciles cannot cross-contaminate
    seen_keys: set[tuple] = set()
    pl_map: dict[str, object] = {}
    cl_map: dict[str, object] = {}
    ap_map: dict[str, object] = {}

    _reconcile_prefix_lists(
        mgmt,
        device,
        sorted(payload.get("prefix_lists") or [], key=lambda row: (row.get("name") or "").casefold()),
        PrefixList,
        ContentType,
        now,
        seen_keys,
        pl_map,
    )
    _reconcile_community_lists(
        mgmt,
        device,
        sorted(payload.get("community_lists") or [], key=lambda row: (row.get("name") or "").casefold()),
        CommunityList,
        ContentType,
        now,
        seen_keys,
        cl_map,
    )
    _reconcile_as_paths(
        mgmt,
        device,
        sorted(payload.get("as_paths") or [], key=lambda row: (row.get("name") or "").casefold()),
        ASPath,
        ContentType,
        now,
        seen_keys,
        ap_map,
    )
    # Route-maps last: their match M2Ms reference the objects created above.
    _reconcile_route_maps(
        mgmt,
        device,
        sorted(payload.get("route_maps") or [], key=lambda row: (row.get("name") or "").casefold()),
        RouteMap,
        ContentType,
        now,
        seen_keys,
        pl_map,
        cl_map,
        ap_map,
    )

    # Stale rows: the device stopped reporting these objects.
    #   - OWNED (anywhere in the shared group): keep + flag drift (device_present=False) for the
    #     operator — intent is never auto-removed.
    #   - UNOWNED: track the removal — re-point to a device that still reports it, or delete the
    #     shared object once no device has it and nothing else references it (see
    #     :func:`_track_unowned_removal`).
    from . import status_machine as sm

    touched_groups = set(seen_keys)
    for state in list(NSORoutePolicyState.objects.filter(management=mgmt)):
        if (state.family, state.object_name.casefold()) in seen_keys:
            continue
        touched_groups.add((state.family, state.object_name))
        if sm.is_owned(state.status):
            _flag_removed(state)
        else:
            _track_unowned_removal(state)

    # Self-heal: a sibling left 'conflict' before its owner re-materialized to matching content
    # settles here once the hashes agree (no device round-trip needed).
    resettle_false_conflicts(touched_groups)

    return list(NSORoutePolicyState.objects.filter(management=mgmt).order_by("family", "object_name"))


def _flag_removed(state) -> None:
    """Keep a stale row but record that the device removed it: device_present=False + drift.

    The shared object and the row are preserved (operator intent, or a still-referenced object);
    the row advances to ``changed`` via on_reconcile and the diff then shows "removed on device".
    """
    from . import status_machine as sm

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


def _object_referenced(obj, family) -> bool:
    """Return whether another netbox-routing object still references *obj*.

    Deleting a referenced object would break that reference, so removal keeps it instead.
    Conservative — an unrecognised family is treated as referenced (kept).
    """
    if family in ("prefix_list", "as_path"):
        return obj.route_map_entries.exists()
    if family == "community_list":
        return obj.route_map_entries.exists() or obj.set_by_route_map_entries.exists()
    if family == "route_map":
        return obj.called_by_entries.exists() or obj.applied_by_entries.exists() or obj.redistribution_entries.exists()
    return True


def _track_unowned_removal(state) -> None:
    """Track an unowned shared object the device removed (no operator ever claimed it).

    Re-point ownership to a device that still reports it (the object lives on, drift clears);
    if NO device reports it anymore, delete the object + its overlay rows — unless another
    netbox-routing object still references it, in which case keep it and flag drift (deleting
    would break that reference). The removing device's own overlay row is dropped when the
    object survives elsewhere, or with the object when it is removed entirely.
    """
    from . import status_machine as sm

    group = list(ownership.group_rows(state))
    if any(sm.is_owned(s.status) for s in group):
        _flag_removed(state)  # operator intent in the group → never auto-remove
        return
    siblings = [s for s in group if s.pk != state.pk]
    live = [s for s in siblings if s.device_present and s.captured]
    obj = state.assigned_object
    family = state.family
    if live:
        # Some device still reports it → drop this device's row; re-point if we were the owner.
        was_owner = state.is_materialized
        state.delete()
        if was_owner:
            ownership.rematerialize(live[0])
        return
    # No device reports this object anymore.
    if obj is not None and _object_referenced(obj, family):
        for s in group:
            _flag_removed(s)  # still referenced elsewhere → keep the object, flag the rows
        return
    for s in siblings:
        s.delete()
    state.delete()
    if obj is not None:
        obj.delete()
