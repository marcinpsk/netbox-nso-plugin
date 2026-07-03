# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pure transform: route-map entry match/set blobs → structured model fields.

The network-state-export reader captures every vendor construct losslessly into the
``match`` / ``set`` JSON blobs of a route-map entry: vendor-neutral keys (``family``,
``community``, ``protocol``, …) plus namespaced ``_rpl_*`` / ``_junos_*`` / ``_timos_*``
keys for anything without a first-class home. This module lifts those blobs into the
netbox-routing structured fields added in P1 — without losing anything:

* ``match_afi``        ← ``family`` / ``_junos_family``      (normalised AFI tokens)
* ``set_communities``  ← ``community`` (+ op) / ``community_add|remove|replace``
* ``call_policy``      ← ``_junos_from_policy``               (Junos match subroutine)
* ``vendor_ext``       ← every ``_rpl_*`` / ``_junos_*`` / ``_timos_*`` key (namespaced)
* ``default_action``   ← the ``_timos_default_action`` synthetic trailing entry
* residual ``match`` / ``set`` blobs keep the still-unmodelled vendor-neutral knobs.

Everything here is a pure function over plain dicts (no Django, no DB) so it can be
unit-tested directly and reused by the write-side later. Name → object resolution
(CommunityList by-ref vs Community inline) is left to the reconciler, which has the DB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Vendor family spellings → the canonical RoutePolicyAFIChoices set. Anything not here is
# preserved under vendor_ext (never dropped) so the AFI column stays clean + comparable.
_AFI_MAP: dict[str, str] = {
    "ipv4": "ipv4",
    "ipv4-unicast": "ipv4",
    "inet": "ipv4",
    "ipv6": "ipv6",
    "ipv6-unicast": "ipv6",
    "inet6": "ipv6",
    "vpn-ipv4": "vpn-ipv4",
    "vpn-ipv4-unicast": "vpn-ipv4",
    "inet-vpn": "vpn-ipv4",
    "vpn-ipv6": "vpn-ipv6",
    "vpn-ipv6-unicast": "vpn-ipv6",
    "inet6-vpn": "vpn-ipv6",
    "l2-vpn": "l2vpn",
    "l2vpn": "l2vpn",
    "evpn": "l2vpn",
}

# _<prefix>_ key → vendor_ext namespace.
_NS_BY_PREFIX: dict[str, str] = {
    "_rpl_": "xr",
    "_junos_": "junos",
    "_timos_": "timos",
}

# set-blob keys consumed into the set_communities relation (so they leave the residual blob).
_COMMUNITY_SET_KEYS = (
    "community",
    "community_additive",
    "community_add",
    "community_remove",
    "community_replace",
    "_junos_community_op",
)


@dataclass
class SetCommunity:
    """One structured set-community action (operation + a single target name).

    ``name`` is a community-LIST name (by-reference) for most vendors, or an inline
    community literal for IOS literal sets — the reconciler decides which when it resolves
    the name against the DB.
    """

    operation: str  # add | set | delete  (CommunitySetActionChoices)
    name: str


@dataclass
class StructuredEntry:
    """Structured view of a single route-map entry derived from its match/set blobs."""

    match_afi: list[str] = field(default_factory=list)
    unmapped_afi: list[str] = field(default_factory=list)
    set_communities: list[SetCommunity] = field(default_factory=list)
    call_policy: str | None = None
    vendor_ext: dict = field(default_factory=dict)
    residual_match: dict = field(default_factory=dict)
    residual_set: dict = field(default_factory=dict)
    is_default_action: bool = False


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def normalize_afi(values) -> tuple[list[str], list[str]]:
    """Split raw vendor family tokens into (canonical AFI choices, unmapped tokens).

    De-duplicates while preserving first-seen order; unmapped tokens are returned so the
    caller can stash them in vendor_ext rather than drop them.
    """
    mapped: list[str] = []
    unmapped: list[str] = []
    for raw in values:
        token = str(raw).strip().lower()
        if not token:
            continue
        canon = _AFI_MAP.get(token)
        if canon is None:
            if token not in unmapped:
                unmapped.append(token)
        elif canon not in mapped:
            mapped.append(canon)
    return mapped, unmapped


def _collect_vendor_ext(*blobs: dict) -> dict:
    """Gather every ``_rpl_*`` / ``_junos_*`` / ``_timos_*`` key into a namespaced dict.

    ``{"_junos_priority": "high"}`` → ``{"junos": {"priority": "high"}}``. This is the
    documented successor to the ad-hoc underscore keys; we keep the FULL set (the lossless
    record) even when a key also drives a structured field.
    """
    out: dict = {}
    for blob in blobs:
        for key, value in blob.items():
            for prefix, ns in _NS_BY_PREFIX.items():
                if key.startswith(prefix):
                    out.setdefault(ns, {})[key[len(prefix) :]] = value
                    break
    return out


def _is_vendor_key(key: str) -> bool:
    return any(key.startswith(p) for p in _NS_BY_PREFIX)


def _extract_set_communities(set_blob: dict) -> list[SetCommunity]:
    """Derive set-community actions from a set blob (R3).

    * Nokia/SR OS by-ref ops: ``community_add|remove|replace`` → add | delete | set.
    * Junos inline list ``community`` + per-name ops ``_junos_community_op``.
    * IOS-XR ``community`` + ``community_additive`` flag (additive → add, else set/replace).
    """
    out: list[SetCommunity] = []

    for raw_key, op in (("community_add", "add"), ("community_remove", "delete"), ("community_replace", "set")):
        for nm in _as_list(set_blob.get(raw_key)):
            if str(nm):
                out.append(SetCommunity(operation=op, name=str(nm)))

    community = set_blob.get("community")
    if community is not None:
        names = [str(n) for n in _as_list(community) if str(n)]
        junos_ops = _as_list(set_blob.get("_junos_community_op"))
        additive = bool(set_blob.get("community_additive"))
        if junos_ops:
            # Junos: explicit verb per community-name (add|set|delete), positional.
            for idx, nm in enumerate(names):
                raw_op = str(junos_ops[idx]) if idx < len(junos_ops) else ""
                op = raw_op if raw_op in ("add", "set", "delete") else "set"
                out.append(SetCommunity(operation=op, name=nm))
        else:
            op = "add" if additive else "set"
            out.extend(SetCommunity(operation=op, name=nm) for nm in names)

    return out


def structure_entry(match_blob: dict | None, set_blob: dict | None) -> StructuredEntry:
    """Lift one route-map entry's match/set blobs into a StructuredEntry (lossless)."""
    match_blob = match_blob or {}
    set_blob = set_blob or {}

    afi_raw = _as_list(match_blob.get("family")) + _as_list(match_blob.get("_junos_family"))
    match_afi, unmapped_afi = normalize_afi(afi_raw)

    vendor_ext = _collect_vendor_ext(match_blob, set_blob)
    if unmapped_afi:
        vendor_ext.setdefault("unmapped", {})["family"] = unmapped_afi

    from_policy = _as_list(match_blob.get("_junos_from_policy"))
    call_policy = str(from_policy[0]) if from_policy else None

    set_communities = _extract_set_communities(set_blob)

    # Residual blobs: drop vendor (_*_) keys (now in vendor_ext), the AFI ``family`` key,
    # and the community-set keys (now in set_communities). What remains is the still-
    # unmodelled vendor-neutral knobs (local_preference, metric, origin, protocol, …).
    residual_match = {k: v for k, v in match_blob.items() if not _is_vendor_key(k) and k != "family"}
    residual_set = {k: v for k, v in set_blob.items() if not _is_vendor_key(k) and k not in _COMMUNITY_SET_KEYS}

    return StructuredEntry(
        match_afi=match_afi,
        unmapped_afi=unmapped_afi,
        set_communities=set_communities,
        call_policy=call_policy,
        vendor_ext=vendor_ext,
        residual_match=residual_match,
        residual_set=residual_set,
        is_default_action=match_blob.get("_timos_default_action") is True,
    )


def _loads(value) -> dict:
    """Parse a match/set value (the adapter ships them as JSON strings) into a dict."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        import json

        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


# Vendor-ext sub-keys that are COSMETIC (a label, or already consumed into a vendor-neutral
# canonical field below) — dropped from the canonical semantic digest so the SAME logical
# route-map converges across vendors. Anything NOT listed stays in the residual ``vendor``
# blob, so genuinely vendor-specific content (e.g. Junos ``route_filter``) preserves drift.
_COSMETIC_VENDOR_EXT: dict[str, set[str]] = {
    # family→afi, term/flat→label, terminal→flow, default→default_action,
    # as_path_group→as_paths, from_policy→call_policy, community_op→set_communities.
    "junos": {"family", "term", "flat", "terminal", "default", "as_path_group", "from_policy", "community_op"},
    # description→label, action_type/no_action→flow, default_action→default_action,
    # as_path_group→as_paths.
    "timos": {"description", "action_type", "no_action", "default_action", "as_path_group"},
}


def _flow(action: str | None, vendor_ext: dict) -> str:
    """Vendor-neutral flow verb for a route-map entry: ``accept`` | ``reject`` | ``continue``.

    Junos ``_junos_terminal`` (accept|reject|none) and Nokia ``_timos_action_type``
    (accept|reject|next-policy|next-entry) both encode whether the entry is terminal or
    falls through — the SAME semantics spelled differently. Normalise both; absent a marker,
    derive from the permit/deny action (a permit clause accepts, a deny rejects).
    """
    junos = vendor_ext.get("junos", {})
    timos = vendor_ext.get("timos", {})
    term = str(junos.get("terminal") or "").lower()
    if term in ("accept", "reject"):
        return term
    if term == "none":
        return "continue"
    action_type = str(timos.get("action_type") or "").lower()
    if action_type in ("accept", "reject"):
        return action_type
    if action_type in ("next-policy", "next-entry"):
        return "continue"
    return "reject" if (action or "").strip().lower() == "deny" else "accept"


def _as_path_groups(vendor_ext: dict) -> list[str]:
    """Junos/Nokia ``as_path_group`` markers — the same match, placed differently per vendor."""
    out: list[str] = []
    for ns in ("junos", "timos"):
        out += [str(x) for x in _as_list(vendor_ext.get(ns, {}).get("as_path_group"))]
    return out


def _residual_vendor(vendor_ext: dict) -> dict:
    """vendor_ext with the cosmetic/consumed sub-keys stripped (what genuinely still differs)."""
    out: dict = {}
    for ns, blob in vendor_ext.items():
        drop = _COSMETIC_VENDOR_EXT.get(ns, set())
        kept = {k: v for k, v in blob.items() if k not in drop}
        if kept:
            out[ns] = kept
    return out


def _canon_value(value):
    """Scalar/list-insensitive, type-insensitive knob value: a sorted list of strings.

    ``protocol: "bgp"`` and ``protocol: ["bgp"]`` (one vendor reads a scalar, the other a
    leaf-list) — and ``250`` vs ``"250"`` — must hash equal. Both sides run through this, so
    the only thing equated is shape/spelling, never two genuinely different value sets.
    """
    return sorted(str(x) for x in _as_list(value))


def _canon_knobs(blob: dict) -> dict:
    return {k: _canon_value(v) for k, v in blob.items()}


def _is_default_entry(match_blob: dict) -> bool:
    """Report whether an entry is the synthetic policy-level default (Nokia or Junos)."""
    return match_blob.get("_timos_default_action") is True or match_blob.get("_junos_default") is True


def _prefix_len(prefix) -> int:
    s = str(prefix)
    if "/" in s:
        return int(s.split("/", 1)[1])
    return 128 if ":" in s else 32


def _max_len(prefix) -> int:
    return 128 if ":" in str(prefix) else 32


def prefix_list_entry_unit(entry: dict) -> tuple:
    """One prefix-list entry → a normalized match unit ``(action, prefix, ge, le)``.

    A bare prefix (no ge/le) is an EXACT match (ge == le == prefix length), so it lines up
    with a Junos ``route-filter ... exact`` unit. Used by the reconciler's prefix-list
    resolver to express a named list's content in the same shape as inline route-filters.
    """
    pfx = str(entry.get("prefix"))
    plen = _prefix_len(pfx)
    ge = entry.get("ge")
    le = entry.get("le")
    ge = int(ge) if ge is not None else plen
    le = int(le) if le is not None else plen
    action = (entry.get("action") or "permit").strip().lower() or "permit"
    return (action, pfx, ge, le)


def _route_filter_unit(rf: dict) -> tuple | None:
    """One Junos inline ``route-filter`` → a match unit ``(permit, prefix, ge, le)``.

    Maps the Junos match-type to a length range: ``exact`` → [len, len]; ``orlonger`` →
    [len, max]; ``longer`` → [len+1, max]; ``upto /Z`` → [len, Z]; ``prefix-length-range
    /X-/Y`` → [X, Y]. An unrecognised type round-trips its raw spelling so it stays distinct
    (never silently equated with a clean range).
    """
    pfx = rf.get("prefix")
    if not pfx:
        return None
    plen = _prefix_len(pfx)
    match = (rf.get("match") or "").strip().lower()
    arg = str(rf.get("arg") or "")
    nums = [int(x) for x in re.findall(r"\d+", arg)]
    if match == "exact":
        lo, hi = plen, plen
    elif match == "orlonger":
        lo, hi = plen, _max_len(pfx)
    elif match == "longer":
        lo, hi = plen + 1, _max_len(pfx)
    elif match == "upto" and nums:
        lo, hi = plen, nums[0]
    elif match == "prefix-length-range" and len(nums) >= 2:
        lo, hi = nums[0], nums[1]
    else:
        # Unrecognised type: keep the raw spelling so it stays distinct, but as a 4-tuple with a
        # sentinel length (-1, never a real prefix length) so a set mixing recognised + raw units
        # still sorts (a 3-tuple here made sorted() compare str vs int → TypeError, aborting the
        # whole route-map reconcile for the device).
        return ("permit", str(pfx), -1, f"raw:{match}:{arg}")
    return ("permit", str(pfx), lo, hi)


def _prefix_match_units(entry: dict, resolver) -> list:
    """Build a route-map entry's prefix match as a sorted set of ``(action, prefix, ge, le)`` units.

    Unions every source of a prefix match into ONE comparable set, regardless of how the
    vendor spells it: named ``match_prefix_lists`` and Junos ``prefix-list-filter`` refs are
    resolved to their list content via ``resolver(name)``; Junos inline ``route-filter``
    blocks are converted directly. So a Junos term that inlines what a Nokia term references
    by name converges — and a term that matches a genuinely different prefix set does not.
    """
    units: set[tuple] = set()
    match_blob = _loads(entry.get("match"))
    names = list(entry.get("match_prefix_lists") or [])
    for plf in match_blob.get("_junos_prefix_list_filter") or []:
        if isinstance(plf, dict) and plf.get("list"):
            names.append(plf["list"])
    for nm in names:
        for unit in resolver(nm) or ():
            units.add(tuple(unit))
    for rf in match_blob.get("_junos_route_filter") or []:
        unit = _route_filter_unit(rf)
        if unit is not None:
            units.add(unit)
    return sorted(units)


def _canonical_entry(e: dict, prefix_resolver=None) -> dict:
    """One route-map entry as a vendor-neutral semantic dict (sequence + labels dropped).

    With a ``prefix_resolver`` (a callable ``name → iterable of prefix-list units``) the
    entry's prefix match is expanded to a content tuple-set (``prefix_match``) so inline
    route-filters and named lists compare apples-to-apples; the now-consumed Junos
    ``prefix-list-filter`` / ``route-filter`` markers drop out of the residual vendor blob.
    Without a resolver it keeps the by-NAME ``prefix_lists`` projection (pure, DB-free).
    """
    s = structure_entry(_loads(e.get("match")), _loads(e.get("set")))
    afi = sorted(set(s.match_afi)) + sorted(set(s.unmapped_afi))
    as_paths = sorted(set(e.get("match_as_paths") or []) | set(_as_path_groups(s.vendor_ext)))
    entry = {
        "action": (e.get("action") or "").strip().lower() or None,
        "flow": _flow(e.get("action"), s.vendor_ext),
        "afi": afi,
        "prefix_lists": sorted(e.get("match_prefix_lists") or []),
        "community_lists": sorted(e.get("match_community_lists") or []),
        "as_paths": as_paths,
        "match_knobs": _canon_knobs(s.residual_match),
        "set_communities": sorted([c.operation, c.name] for c in s.set_communities),
        "set_knobs": _canon_knobs(s.residual_set),
        "call_policy": s.call_policy,
        "vendor": _residual_vendor(s.vendor_ext),
    }
    if prefix_resolver is not None:
        entry["prefix_match"] = _prefix_match_units(e, prefix_resolver)
        del entry["prefix_lists"]
        junos = entry["vendor"].get("junos")
        if junos:
            junos.pop("prefix_list_filter", None)
            junos.pop("route_filter", None)
            if not junos:
                entry["vendor"].pop("junos", None)
    return entry


def canonical_route_map(captured: dict | None, prefix_resolver=None) -> dict:
    """Vendor-neutral SEMANTIC projection of a device's captured route-map, for the dedup hash.

    Two devices' versions of the same logical route-map hash EQUAL when they differ only in
    cosmetic vendor encoding — term/entry labels, family spelling (``inet`` vs ``ipv4``),
    scalar-vs-leaf-list (``protocol "bgp"`` vs ``["bgp"]``), the fall-through verb
    (``_junos_terminal: none`` vs ``_timos_action_type: next-policy``), or where an
    ``as-path-group`` match lands. Genuinely different content (different prefix-lists,
    extra terms, Junos inline ``route_filter`` blocks) keeps a distinct digest → honest
    cross-vendor drift the operator resolves (push one version, or mark the group LOCAL).

    Entries are compared POSITIONALLY (the sequence number is dropped — Junos numbers terms
    10/20, Nokia 1/2/1000). The synthetic policy-level default entry (Nokia ``default-action``
    / Junos policy ``then``) is folded into ``default`` rather than counted as an entry.

    With ``prefix_resolver`` (``name → prefix-list units``), each entry's prefix match is
    expanded to its CONTENT (see :func:`_prefix_match_units`), so a Junos term that inlines a
    route-filter set converges with the Nokia term that references the equivalent named lists.
    """
    captured = captured or {}
    default = None
    entries: list[dict] = []
    for e in captured.get("entries") or []:
        if _is_default_entry(_loads(e.get("match"))):
            ce = _canonical_entry(e, prefix_resolver)
            default = {
                "action": ce["action"],
                "flow": ce["flow"],
                "set_communities": ce["set_communities"],
                "set_knobs": ce["set_knobs"],
            }
            continue
        entries.append(_canonical_entry(e, prefix_resolver))
    return {"default": default, "entries": entries}


def summarize_route_map(captured: dict | None) -> dict:
    """Build a display summary of a device's CAPTURED route-map (for the versions UI).

    Returns ``{"default_action": <permit|deny|None>, "entries": [ {…}, … ]}``. Each entry is
    the structured projection of one device entry — action, match AFI / referenced lists /
    residual match knobs, set-community ops, call-policy, residual set knobs, vendor_ext — so
    operators can compare two devices' versions of the same route-map without reading raw JSON.
    The synthetic ``_timos_default_action`` entry is folded into ``default_action`` (not shown
    as an entry). Pure + display-only; mirrors what the reconciler materialises.
    """
    captured = captured or {}
    default_action = None
    entries: list[dict] = []
    for e in captured.get("entries") or []:
        s = structure_entry(_loads(e.get("match")), _loads(e.get("set")))
        action = (e.get("action") or "").strip().lower() or None
        if s.is_default_action:
            default_action = action
            continue
        entries.append(
            {
                "sequence": e.get("sequence"),
                "action": action,
                "match_afi": s.match_afi,
                "match_prefix_lists": e.get("match_prefix_lists") or [],
                "match_community_lists": e.get("match_community_lists") or [],
                "match_as_paths": e.get("match_as_paths") or [],
                "match_knobs": s.residual_match,
                "set_communities": [{"operation": c.operation, "name": c.name} for c in s.set_communities],
                "call_policy": s.call_policy,
                "set_knobs": s.residual_set,
                "vendor_ext": s.vendor_ext,
            }
        )
    return {"default_action": default_action, "entries": entries}
