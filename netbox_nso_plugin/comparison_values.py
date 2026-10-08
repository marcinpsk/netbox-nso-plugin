# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Preserve typed values and dependencies for read-only comparison projections."""

from collections import defaultdict
from dataclasses import dataclass, field


class _MissingValue:
    def __str__(self):
        return "missing"


MISSING = _MissingValue()


@dataclass
class ProjectedEntry:
    identity: object
    values: dict
    objects: tuple = ()
    unavailable: dict = field(default_factory=dict)
    reason: str = ""
    coverage_only: bool = False
    # Preserve structural identities that the comparison identity cannot express.
    parent_identities: tuple | None = None


def observed_value(item, name):
    if "present" in item and name not in item["present"]:
        return MISSING
    return item.get(name, MISSING)


def _values(item, names):
    return {name: observed_value(item, name) for name in names}


def _normalized(item, values):
    return {name: value if observed_value(item, name) is not MISSING else MISSING for name, value in values.items()}


def _routing_dependency_key(scope, entry):
    identity = entry.identity
    if scope == "redistribution" and identity == ("bgp",) and "router_asn" in entry.values:
        key = ("bgp", "router", str(entry.values["router_asn"]))
        return (*key, "scope", entry.values["vrf"]) if "vrf" in entry.values else key
    return identity


def _isis_parent_identities(entry):
    identity = entry.identity
    if identity[0] == "process":
        return (identity[:2],) if len(identity) > 2 else ()
    if len(identity) > 3:
        return (identity[:3],)
    process = entry.values.get("process_tag", MISSING)
    return (("process", process),) if process not in (MISSING, None) else ()


def _ospf_parent_identities(entry):
    identity = entry.identity
    if identity[0] == "instance":
        return (identity[:3],) if len(identity) > 3 else ()
    return ()


def _bgp_parent_identities(entry, indexed):
    identity = entry.identity
    if len(identity) == 2:
        return ()
    parents = [identity[:-2]]
    if identity[-2] in {"peer_address_family", "peer_group_address_family"}:
        parents.append((*identity[:4], "address_family", identity[-1]))
    peer_identity = identity[:-2] if identity[-2] == "peer_address_family" else identity
    if len(peer_identity) == 6 and peer_identity[-2] == "peer":
        peers = indexed.get(peer_identity, ()) if peer_identity != identity else (entry,)
        for peer in peers:
            group = peer.values.get("peer_group", MISSING)
            if group not in (MISSING, None, ""):
                group_identity = (*identity[:4], "peer_group", group)
                parents.append(group_identity)
    return tuple(parents)


def _redistribution_parent_identities(entry):
    identity = entry.identity
    if identity == ("bgp",):
        if "router_asn" in entry.values and "vrf" in entry.values:
            return (("bgp", "router", str(entry.values["router_asn"])),)
        return ()
    parents = [identity[:3]] if len(identity) == 5 else []
    if identity[0] == "bgp" and len(identity) >= 3:
        parts = identity[1].split("/")
        parents.append(("bgp", "router", parts[0]))
        if len(parts) > 1:
            parents.append(("bgp", "router", parts[0], "scope", parts[1]))
    return tuple(parents)


def _routing_parent_identities(scope, entry, indexed):
    if entry.parent_identities is not None:
        return entry.parent_identities
    if scope == "lacp":
        bundle = entry.values.get("bundle", MISSING)
        return (("bundle", bundle),) if entry.identity[0] == "member" and bundle is not MISSING else ()
    if scope == "l2_sap":
        return (entry.identity[:1],) if len(entry.identity) > 1 else ()
    if scope in {"isis", "isis_flex_algo"}:
        return _isis_parent_identities(entry)
    if scope == "ospf":
        return _ospf_parent_identities(entry)
    if scope == "bgp":
        return _bgp_parent_identities(entry, indexed)
    if scope == "redistribution":
        return _redistribution_parent_identities(entry)
    return (entry.identity[:2],) if len(entry.identity) > 2 else ()


def inherit_parent_dependencies(scope, native_entries, observed_entries):
    """Close both comparison graphs over every structural parent's dependencies.

    Union duplicate entries and parent links before walking ancestors. A dependency
    found on either side restricts descendants on both sides, even if no child
    counterpart exists. Keep redistribution router and scope coverage keys distinct.
    """
    indexed = defaultdict(list)
    entries = (*native_entries, *observed_entries)
    for entry in entries:
        indexed[_routing_dependency_key(scope, entry)].append(entry)
    parents = {
        key: {parent for entry in items for parent in _routing_parent_identities(scope, entry, indexed)}
        for key, items in indexed.items()
    }
    resolved = {}

    def dependencies(key):
        if key in resolved:
            return resolved[key]
        objects, visited, pending = {}, set(), [key]
        while pending:
            ancestor = pending.pop()
            if ancestor in visited:
                continue
            visited.add(ancestor)
            for item in indexed.get(ancestor, ()):
                objects.update((id(obj), obj) for obj in item.objects if obj is not None)
            pending.extend(parents.get(ancestor, ()))
        resolved[key] = tuple(objects.values())
        return resolved[key]

    # Resolve the original graph before mutating entries.
    for key in indexed:
        dependencies(key)
    for key, items in indexed.items():
        for item in items:
            item.objects = resolved[key]
    return observed_entries
