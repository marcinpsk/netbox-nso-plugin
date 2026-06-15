# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pure transform: route-map entry match/set blobs → structured model fields (M17 P1).

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
