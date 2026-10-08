# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Project BGP and policy graphs without ownership qualification filters."""

import json
from collections import defaultdict
from ipaddress import ip_address, ip_network

from .comparison_values import MISSING, ProjectedEntry, _normalized, observed_value

_BGP_PEER_FIELDS = ("remote_as", "local_as", "enabled", "ttl", "source", "description", "peer_group", "bfd_enabled")
_BGP_AF_FIELDS = ("enabled", "routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out")
_POLICY_COMPONENTS = {
    "prefix_list": "prefix_lists",
    "community_list": "community_lists",
    "as_path": "as_paths",
    "route_map": "route_maps",
}
ATTRIBUTES = {
    "bgp": ("router_id", *_BGP_PEER_FIELDS, *_BGP_AF_FIELDS[1:], "password"),
    "route_policy": (
        "family",
        "invert_match",
        "action",
        "prefix",
        "ge",
        "le",
        "community",
        "pattern",
        "match_prefix_lists",
        "match_community_lists",
        "match_as_paths",
        "match_json",
        "set_json",
    ),
}


def parse_comparison_asn(value):
    """Return the uint32 identity of an RFC 5396 asplain or asdot value."""
    from .bgp_reconciler import _parse_asn

    if type(value) not in (str, int):
        raise ValueError("invalid AS number")
    parts = str(value).split(".")
    if len(parts) not in (1, 2) or not all(
        part.isascii() and part.isdecimal() and (part == "0" or not part.startswith("0")) for part in parts
    ):
        raise ValueError("invalid AS number")
    if value in (0, "0", "0.0"):
        return 0
    return _parse_asn(str(value))


def _asn(value):
    return None if value is None else parse_comparison_asn(value)


def _policy_models():
    from netbox_routing.models import ASPath, CommunityList, PrefixList, RouteMap

    return {"prefix_lists": PrefixList, "community_lists": CommunityList, "as_paths": ASPath, "route_maps": RouteMap}


def _reference_objects(document):
    """Load exact policy references once for the complete observed graph."""
    from .route_policy_structure import _as_list, _as_path_groups, structure_entry

    names = defaultdict(set)
    for component_name in _POLICY_COMPONENTS.values():
        for parent in document.get(component_name) or []:
            names[component_name].add(parent["name"])
            for item in parent.get("entry") or []:
                for field_name, target in (
                    ("match_prefix_lists", "prefix_lists"),
                    ("match_community_lists", "community_lists"),
                    ("match_as_paths", "as_paths"),
                ):
                    names[target].update(item.get(field_name) or [])
                try:
                    names["prefix_lists"].update(_prefix_reference_names(item))
                    structured = structure_entry(_json_blob(item.get("match_json")), _json_blob(item.get("set_json")))
                except ValueError:
                    continue
                names["community_lists"].update(_set_community_reference_names(item))
                names["as_paths"].update(_as_path_groups(structured.vendor_ext))
                if structured.call_policy:
                    names["route_maps"].add(structured.call_policy)
                names["route_maps"].update(
                    str(name) for name in _as_list(_json_blob(item.get("set_json")).get("apply"))
                )
    for router in document.get("routers") or []:
        for scope in router.get("scope") or []:
            for field_name, af_name in (("peer", "peer_address_family"), ("peer_group", "peer_group_address_family")):
                for peer in scope.get(field_name) or []:
                    for item in peer.get(af_name) or []:
                        for name in _BGP_AF_FIELDS[1:]:
                            value = item.get(name)
                            if value:
                                names["route_maps" if name.startswith("routemap") else "prefix_lists"].add(value)
    return _load_reference_objects(names)


def _load_reference_objects(names):
    from django.db.models import Prefetch
    from netbox_routing.models import Community, PrefixListEntry

    references = {}
    for component_name, model in _policy_models().items():
        if not names[component_name]:
            continue
        rows = model.objects.filter(name__in=names[component_name]).order_by("pk")
        if component_name == "prefix_lists":
            rows = rows.prefetch_related(
                Prefetch(
                    "prefix_list_entries",
                    queryset=PrefixListEntry.objects.order_by("sequence").prefetch_related("assigned_prefix"),
                )
            )
        references[component_name] = {row.name: row for row in rows}
    if names["community_lists"]:
        references["communities"] = {
            str(row.community): row for row in Community.objects.filter(community__in=names["community_lists"])
        }
    units, objects = {}, {}
    for name, root in references.get("prefix_lists", {}).items():
        values, related = [], [root]
        for row in root.prefix_list_entries.all():
            prefix = row.assigned_prefix
            related.extend((row, prefix))
            if prefix is None:
                values = None
                break
            from .route_policy_structure import prefix_list_entry_unit

            values.append(
                prefix_list_entry_unit({"action": row.action, "prefix": str(prefix.prefix), "ge": row.ge, "le": row.le})
            )
        units[name] = tuple(values) if values is not None else None
        objects[name] = tuple(related)
    references["prefix_units"], references["prefix_objects"] = units, objects
    return references


def _json_blob(value):
    """Refuse invalid policy JSON rather than treating it as an empty match."""
    if value in (None, ""):
        return {}
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("policy match and set values must be JSON objects")
    return parsed


def _prefix_reference_names(item):
    match_data = _json_blob(item.get("match_json"))
    names = list(item.get("match_prefix_lists") or [])
    filters = match_data.get("_junos_prefix_list_filter") or []
    if not isinstance(filters, list):
        raise ValueError("invalid prefix-list filter collection")
    for entry in filters:
        if not isinstance(entry, dict) or not isinstance(entry.get("list"), str) or not entry["list"]:
            raise ValueError("invalid prefix-list filter")
        names.append(entry["list"])
    filters = match_data.get("_junos_route_filter") or []
    if not isinstance(filters, list):
        raise ValueError("invalid route-filter collection")
    for entry in filters:
        if not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str):
            raise ValueError("invalid route filter")
        ip_network(entry["prefix"], strict=False)
        if entry.get("match") is not None and not isinstance(entry["match"], str):
            raise ValueError("invalid route-filter match")
    return tuple(names)


def _policy_entry_values(component_name, item, *, prefix_resolver=None):
    from .route_policy_reconciler import _norm_action
    from .route_policy_structure import canonical_route_map, prefix_list_entry_unit

    action = item.get("action")
    if action not in ("permit", "deny", "accept", "reject"):
        raise ValueError("invalid route-policy action")
    values = {"action": _norm_action(action)}
    if component_name == "prefix_lists":
        prefix = str(ip_network(item["prefix"], strict=False))
        _action, prefix, ge, le = prefix_list_entry_unit({**item, "prefix": prefix})
        values.update(prefix=prefix, ge=ge, le=le)
    elif component_name == "community_lists":
        values["community"] = item["community"].strip()
    elif component_name == "as_paths":
        values["pattern"] = item["pattern"]
    else:
        _prefix_reference_names(item)
        captured = {
            "action": values["action"],
            "match": _json_blob(item.get("match_json")),
            "set": _json_blob(item.get("set_json")),
            **{
                name: item.get(name) or [] for name in ("match_prefix_lists", "match_community_lists", "match_as_paths")
            },
        }
        canonical = canonical_route_map({"entries": [captured]}, prefix_resolver)
        entry = canonical["entries"][0] if canonical["entries"] else canonical["default"]
        match_only = canonical_route_map(
            {
                "entries": [
                    {
                        "action": "permit",
                        "match": captured["match"],
                        "match_prefix_lists": captured["match_prefix_lists"],
                    }
                ]
            },
            prefix_resolver,
        )
        set_only = canonical_route_map({"entries": [{"action": "permit", "set": captured["set"]}]})
        values.update(
            match_prefix_lists=entry.get("prefix_match", sorted(captured["match_prefix_lists"])),
            match_community_lists=sorted(captured["match_community_lists"]),
            match_as_paths=entry.get("as_paths", []),
            match_json=match_only,
            set_json=set_only,
        )
    return values


def _observed_prefix_units(document):
    units = {}
    for parent in document.get("prefix_lists") or []:
        if observed_value(parent, "entry") in (MISSING, None):
            units[parent["name"]] = None
            continue
        try:
            normalized = [_policy_entry_values("prefix_lists", item) for item in parent["entry"]]
            units[parent["name"]] = tuple(
                (item["action"], item["prefix"], item["ge"], item["le"]) for item in normalized
            )
        except (TypeError, ValueError):
            units[parent["name"]] = None
    return units


def _policy_comparison_values(component_name, item, units):
    if component_name != "route_maps":
        return _policy_entry_values(component_name, item), {}
    unavailable = any(units.get(name) is None for name in _prefix_reference_names(item))
    if unavailable:
        values = _policy_entry_values(component_name, item)
        values.pop("match_prefix_lists")
        values.pop("match_json")
        return values, {name: "prefix-list content is unavailable" for name in ("match_prefix_lists", "match_json")}
    return _policy_entry_values(component_name, item, prefix_resolver=units.__getitem__), {}


def _policy_references(item, references):
    from .route_policy_structure import _as_list, _as_path_groups, structure_entry

    objects = []
    for name, target in (
        ("match_prefix_lists", "prefix_lists"),
        ("match_community_lists", "community_lists"),
        ("match_as_paths", "as_paths"),
    ):
        objects.extend(references.get(target, {}).get(value) for value in item.get(name) or [])
    objects.extend(
        obj for name in _prefix_reference_names(item) for obj in references.get("prefix_objects", {}).get(name, ())
    )
    structured = structure_entry(_json_blob(item.get("match_json")), _json_blob(item.get("set_json")))
    community_names = _set_community_reference_names(item)
    objects.extend(references.get("community_lists", {}).get(name) for name in community_names)
    objects.extend(references.get("communities", {}).get(name) for name in community_names)
    objects.extend(references.get("as_paths", {}).get(name) for name in _as_path_groups(structured.vendor_ext))
    objects.append(references.get("route_maps", {}).get(structured.call_policy))
    objects.extend(
        references.get("route_maps", {}).get(str(name))
        for name in _as_list(_json_blob(item.get("set_json")).get("apply"))
    )
    return tuple(objects)


def _set_community_reference_names(item):
    from .route_policy_structure import _as_list, structure_entry

    set_blob = _json_blob(item.get("set_json"))
    structured = structure_entry(_json_blob(item.get("match_json")), set_blob)
    return tuple(action.name for action in structured.set_communities) + tuple(
        str(name) for name in _as_list(set_blob.get("community_delete")) if str(name)
    )


def _native_policy(management):
    from django.db.models import Prefetch
    from netbox_routing.models import (
        ASPathEntry,
        CommunityListEntry,
        PrefixListEntry,
        RouteMapEntry,
        RouteMapEntrySetCommunity,
    )

    from .models import NSORoutePolicyState

    states = list(
        NSORoutePolicyState.objects.filter(management=management).select_related("content_type").order_by("pk")
    )
    rows = {}
    entry_queries = {
        "prefix_lists": (
            "prefix_list_entries",
            PrefixListEntry.objects.order_by("sequence").prefetch_related("assigned_prefix"),
        ),
        "community_lists": (
            "communitylistentries",
            CommunityListEntry.objects.select_related("community").order_by("pk"),
        ),
        "as_paths": ("aspath_entries", ASPathEntry.objects.order_by("sequence")),
        "route_maps": (
            "route_map_entries",
            RouteMapEntry.objects.select_related("call_policy", "apply_policy")
            .order_by("sequence")
            .prefetch_related(
                "match_prefix_list",
                "match_community_list",
                "match_aspath",
                Prefetch(
                    "set_communities",
                    queryset=RouteMapEntrySetCommunity.objects.select_related("community_list").prefetch_related(
                        "communities"
                    ),
                ),
            ),
        ),
    }
    for component_name, model in _policy_models().items():
        state_family = next(family for family, component in _POLICY_COMPONENTS.items() if component == component_name)
        ids = {
            state.object_id
            for state in states
            if state.family == state_family
            and state.content_type is not None
            and state.content_type.model_class() is model
        }
        relation, query = entry_queries[component_name]
        rows[component_name] = {
            row.pk: row for row in model.objects.filter(pk__in=ids).prefetch_related(Prefetch(relation, queryset=query))
        }
    result, pending, reference_document = [], [], {}
    for state in states:
        component_name = _POLICY_COMPONENTS.get(state.family)
        if component_name is None:
            result.append(
                ProjectedEntry(("unknown", state.object_name), {}, (state,), reason="invalid native policy family")
            )
            continue
        expected_model = _policy_models()[component_name]
        root = (
            rows[component_name].get(state.object_id)
            if (state.content_type is not None and state.content_type.model_class() is expected_model)
            else None
        )
        identity = (component_name, root.name if root is not None else state.object_name)
        objects = (state, root)
        if root is None:
            result.append(ProjectedEntry(identity, {}, objects, reason="native policy object is unavailable"))
            continue
        values = {}
        if component_name == "prefix_lists":
            values["family"] = int(root.family)
        elif component_name == "community_lists":
            values["invert_match"] = bool(root.invert_match)
        result.append(ProjectedEntry(identity, values, objects))
        relation = entry_queries[component_name][0]
        for index, row in enumerate(getattr(root, relation).all(), start=1):
            entry_identity = (*identity, "entry", index)
            try:
                projected, item = _native_policy_entry(component_name, row, entry_identity)
            except (TypeError, ValueError):
                projected = ProjectedEntry(entry_identity, {}, (row,), reason="invalid native policy content")
                item = {}
            result.append(projected)
            pending.append((projected, item))
            reference_document.setdefault(component_name, []).append({"name": root.name, "entry": [item]})
    edited_policies = {projected.identity[:2] for projected, item in pending if item.get("structured_edited")}
    for projected, item in pending:
        if (
            projected.identity[:2] in edited_policies
            and projected.identity[-1] == 1
            and isinstance(item.get("match_json"), dict)
        ):
            item["match_json"].pop("_rpl_raw", None)
    references = _reference_objects(reference_document)
    for projected, item in pending:
        if not projected.reason:
            projected.values, projected.unavailable = _policy_comparison_values(
                projected.identity[0], item, references["prefix_units"]
            )
            projected.objects += _policy_references(item, references)
    return tuple(result)


def _native_policy_entry(component_name, row, identity):
    entry_objects = [row]
    item = {"action": row.action}
    reason = ""
    if component_name == "prefix_lists":
        prefix = row.assigned_prefix
        entry_objects.append(prefix)
        if prefix is None:
            reason = "native prefix is unavailable"
        item.update(prefix=str(prefix.prefix) if prefix else "", ge=row.ge, le=row.le)
    elif component_name == "community_lists":
        entry_objects.append(row.community)
        if row.community is None:
            reason = "native community is unavailable"
        item["community"] = str(row.community.community) if row.community else ""
    elif component_name == "as_paths":
        item["pattern"] = row.pattern or ""
    else:
        match_data, set_data, edited = _native_structured_blobs(row, entry_objects)
        item.update(match_json=match_data, set_json=set_data, structured_edited=edited)
        for name, relation in (
            ("match_prefix_lists", "match_prefix_list"),
            ("match_community_lists", "match_community_list"),
            ("match_as_paths", "match_aspath"),
        ):
            related = tuple(getattr(row, relation).all())
            entry_objects.extend(related)
            item[name] = sorted(obj.name for obj in related)
    try:
        values = {} if reason else _policy_entry_values(component_name, item)
    except ValueError:
        values, reason = {}, "invalid native policy content"
    return ProjectedEntry(identity, values, tuple(entry_objects), reason=reason), item


def _native_structured_blobs(row, objects):
    from .signals import _project_structured_entry

    if row.vendor_ext is not None and (
        not isinstance(row.vendor_ext, dict) or not all(isinstance(value, dict) for value in row.vendor_ext.values())
    ):
        raise ValueError("invalid native vendor structure")
    if row.match_afi is not None and not isinstance(row.match_afi, list):
        raise ValueError("invalid native address families")
    match_data, set_data = dict(_json_blob(row.match)), dict(_json_blob(row.set))
    if row.flow_control is not None and "flow_control" not in set_data:
        set_data["flow_control"] = row.flow_control
    objects.extend((row.call_policy, row.apply_policy))
    for action in row.set_communities.all():
        if action.operation not in {"add", "set", "delete"}:
            raise ValueError("invalid native set-community operation")
        objects.extend((action, action.community_list, *action.communities.all()))
    edited = _project_structured_entry(row, match_data, set_data)
    return match_data, set_data, edited


def _native_bgp(management):
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Prefetch, Q
    from netbox_routing.models import (
        BGPAddressFamily,
        BGPPeer,
        BGPPeerAddressFamily,
        BGPPeerTemplate,
        BGPRouter,
        BGPScope,
    )

    from .models import NSOBGPPeerState, NSOBGPPeerTemplateState

    routers = list(
        BGPRouter.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=management.device_id,
        )
        .select_related("asn")
        .prefetch_related(Prefetch("peer_templates", queryset=BGPPeerTemplate.objects.select_related("remote_as")))
        .order_by("pk")
    )
    scopes = list(BGPScope.objects.filter(router__in=routers).select_related("vrf").order_by("pk"))
    peers = list(
        BGPPeer.objects.filter(scope__in=scopes)
        .select_related(
            "peer__vrf",
            "remote_as",
            "local_as",
            "peer_group__remote_as",
            "source__vrf",
            "update_source",
        )
        .order_by("pk")
    )
    peer_states = defaultdict(list)
    for state in NSOBGPPeerState.objects.filter(management=management).order_by("pk"):
        peer_states[state.bgp_peer_id].append(state)
    template_states = list(
        NSOBGPPeerTemplateState.objects.filter(management=management).select_related("template__remote_as")
    )
    templates = {row.peer_group_id: row.peer_group for row in peers if row.peer_group_id is not None}
    template_routers = defaultdict(set)
    for router in routers:
        for template in router.peer_templates.all():
            templates[template.pk] = template
            template_routers[template.pk].add(router.pk)
    templates.update({state.template_id: state.template for state in template_states if state.template_id is not None})
    peer_type = ContentType.objects.get_for_model(BGPPeer)
    template_type = ContentType.objects.get_for_model(BGPPeerTemplate)
    afs = defaultdict(list)
    for row in (
        BGPPeerAddressFamily.objects.filter(
            Q(assigned_object_type=peer_type, assigned_object_id__in=[peer.pk for peer in peers])
            | Q(assigned_object_type=template_type, assigned_object_id__in=templates)
        )
        .select_related("address_family", "routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out")
        .order_by("pk")
    ):
        afs[(row.assigned_object_type_id, row.assigned_object_id)].append(row)
    result, router_entries, scope_entries = [], {}, {}
    for router in routers:
        reason = "native BGP source AS is unavailable" if router.asn is None else ""
        identity = ("router", int(router.asn.asn) if router.asn else None)
        projected = ProjectedEntry(
            identity,
            {"router_id": str(router.router_id) if router.router_id else None},
            (router, router.asn),
            reason=reason,
        )
        result.append(projected)
        router_entries[router.pk] = projected
    for scope in scopes:
        parent = router_entries[scope.router_id]
        identity = (*parent.identity, "scope", scope.vrf.name if scope.vrf else "")
        projected = ProjectedEntry(identity, {}, (scope, scope.vrf), reason=parent.reason)
        result.append(projected)
        scope_entries[scope.pk] = projected
    for row in BGPAddressFamily.objects.filter(scope__in=scopes).order_by("pk"):
        parent = scope_entries[row.scope_id]
        result.append(
            ProjectedEntry(
                (*parent.identity, "address_family", row.address_family),
                {},
                (row,),
                reason=parent.reason,
            )
        )
    group_scopes = defaultdict(set)
    for peer in peers:
        parent = scope_entries[peer.scope_id]
        if peer.peer_group_id is not None:
            group_scopes[peer.peer_group_id].add(peer.scope_id)
        address = str(peer.peer.address.ip) if peer.peer else None
        values = {name: getattr(peer, name) for name in ("enabled", "ttl", "bfd_enabled")}
        values.update(
            remote_as=int(peer.remote_as.asn) if peer.remote_as else None,
            local_as=int(peer.local_as.asn) if peer.local_as else None,
            peer_group=peer.peer_group.name if peer.peer_group else None,
            source=peer.update_source.name
            if peer.update_source
            else (str(peer.source.address.ip) if peer.source else None),
        )
        reason = parent.reason or ("native BGP remote AS is unavailable" if peer.remote_as is None else "")
        if peer.peer is None:
            reason = reason or "native BGP peer address is unavailable"
        objects = (
            peer,
            peer.peer,
            peer.peer.vrf if peer.peer else None,
            peer.remote_as,
            peer.local_as,
            peer.peer_group,
            peer.source,
            peer.source.vrf if peer.source else None,
            peer.update_source,
            *peer_states[peer.pk],
        )
        projected = ProjectedEntry(
            (*parent.identity, "peer", address),
            values,
            objects,
            {"password": "credentials are presence-only", "description": "native BGP description is not modeled"},
            reason,
        )
        result.append(projected)
        result.extend(_native_peer_af(projected, afs[(peer_type.pk, peer.pk)], "peer_address_family"))
    for template_id, template in templates.items():
        attached_scopes = group_scopes[template_id]
        placed_routers = {scope.router_id for scope in scopes if scope.pk in attached_scopes}
        unplaced_routers = template_routers[template_id] - placed_routers
        if not attached_scopes and not unplaced_routers:
            unplaced_routers = {None}
        for router_id in unplaced_routers:
            states = tuple(state for state in template_states if state.template_id == template_id)
            parent = router_entries.get(router_id)
            identity = parent.identity if parent else ("router", None)
            objects = (template, template.remote_as, *states)
            result.append(
                ProjectedEntry(
                    (*identity, "peer_group", template.name),
                    {},
                    objects,
                    reason="native BGP peer-group scope is unavailable",
                )
            )
        for scope_id in attached_scopes:
            parent = scope_entries[scope_id]
            states = tuple(state for state in template_states if state.template_id == template_id)
            projected = ProjectedEntry(
                (*parent.identity, "peer_group", template.name),
                {"remote_as": int(template.remote_as.asn) if template.remote_as else None},
                (template, template.remote_as, *states),
                reason=parent.reason,
            )
            result.append(projected)
            result.extend(_native_peer_af(projected, afs[(template_type.pk, template_id)], "peer_group_address_family"))
    return tuple(result)


def _native_peer_af(parent, rows, collection):
    from .bgp_reconciler import _af_rows_content

    result = []
    for row in rows:
        content = _af_rows_content([row])[0]
        values = {"enabled": content["enabled"]}
        values.update({name: content[name][-1] if content[name] is not None else None for name in _BGP_AF_FIELDS[1:]})
        result.append(
            ProjectedEntry(
                (*parent.identity, collection, content["af"]),
                values,
                (
                    row,
                    row.address_family,
                    row.routemap_in,
                    row.routemap_out,
                    row.prefixlist_in,
                    row.prefixlist_out,
                ),
                reason=parent.reason,
            )
        )
    return result


def native_entries(scope, management, *, ned_id=""):
    return _native_bgp(management) if scope == "bgp" else _native_policy(management)


def _device_policy(document, references):
    result = []
    units = _observed_prefix_units(document)
    for component_name in _POLICY_COMPONENTS.values():
        for parent in document.get(component_name) or []:
            identity = (component_name, parent["name"])
            parent_objects = (references.get(component_name, {}).get(parent["name"]),)
            fields = (
                ("family",)
                if component_name == "prefix_lists"
                else (("invert_match",) if component_name == "community_lists" else ())
            )
            result.append(
                ProjectedEntry(identity, {name: observed_value(parent, name) for name in fields}, parent_objects)
            )
            sequences = set()
            for index, item in enumerate(parent.get("entry") or [], start=1):
                reason = "multiple policy entries have the same sequence" if item["sequence"] in sequences else ""
                sequences.add(item["sequence"])
                entry_identity = (*identity, "entry", index)
                try:
                    values, unavailable = _policy_comparison_values(component_name, item, units)
                    related = _policy_references(item, references)
                except ValueError:
                    values, unavailable, related, reason = {}, {}, (), "invalid device policy content"
                result.append(
                    ProjectedEntry(
                        entry_identity,
                        values,
                        related,
                        unavailable=unavailable,
                        reason=reason,
                    )
                )
    return tuple(result)


def _bgp_peer_dependencies(identity, vrf, values, item, objects, dependencies, native_values):
    source_objects = dependencies.get(("source", vrf, values.get("source")), ())
    if native_values.get(identity, {}).get("source", MISSING) == values.get("source"):
        source_objects = ()
    return (
        *(dependencies.get(("ip", vrf, identity[-1]), ()) if identity not in objects else ()),
        *source_objects,
        *dependencies.get(("asn", values.get("remote_as")), ()),
        *dependencies.get(("asn", values.get("local_as")), ()),
        *dependencies.get(("peer_group", item.get("peer_group")), ()),
    )


def _device_bgp(document, objects, references, dependencies, *, native_values=None):
    native_values = native_values or {}
    result = []

    def project(identity, item, values, related=(), unavailable=None, reason=""):
        projected = ProjectedEntry(
            identity,
            _normalized(item, values),
            related,
            unavailable or {},
            reason,
        )
        result.append(projected)
        return projected

    def peer_afs(peer, parent, collection):
        for item in peer.get(collection) or []:
            # Keep the reconciler's AF omission defaults.
            values = {name: item.get(name) for name in _BGP_AF_FIELDS}
            values["enabled"] = bool(item.get("enabled", True))
            related = tuple(
                references.get("route_maps" if name.startswith("routemap") else "prefix_lists", {}).get(item.get(name))
                for name in _BGP_AF_FIELDS[1:]
            )
            identity = (*parent.identity, collection, item["afi"])
            result.append(
                ProjectedEntry(
                    identity,
                    values,
                    related,
                    reason=parent.reason,
                )
            )

    for router in document.get("routers") or []:
        router_identity = ("router", _bgp_asn_identity(router["asn"]))
        try:
            values = {"router_id": str(ip_address(router["router_id"])) if router.get("router_id") else None}
            reason = "" if isinstance(router_identity[1], int) else "invalid device BGP source AS"
        except ValueError:
            values, reason = {}, "invalid device BGP router ID"
        parent = project(
            router_identity, router, values, dependencies.get(("asn", router_identity[1]), ()), reason=reason
        )
        for scope in router.get("scope") or []:
            vrf = scope.get("vrf", "")
            scoped = project(
                (*router_identity, "scope", vrf),
                scope,
                {},
                dependencies.get(("vrf", vrf), ()),
                reason=parent.reason,
            )
            for item in scope.get("address_family") or []:
                project((*scoped.identity, "address_family", item["afi"]), item, {})
            for item in scope.get("peer") or []:
                identity = (*scoped.identity, "peer", _bgp_address_identity(item["peer_address"]))
                values = {name: item.get(name) for name in _BGP_PEER_FIELDS}
                reason = scoped.reason
                try:
                    values.update(remote_as=_asn(item.get("remote_as")), local_as=_asn(item.get("local_as")))
                    source = item.get("source")
                    if source:
                        from .bgp_reconciler import _canonical_source_ip

                        values["source"] = _canonical_source_ip(source) or source
                except ValueError:
                    reason = reason or "invalid device BGP peer content"
                try:
                    ip_address(identity[-1])
                except ValueError:
                    reason = reason or "invalid device BGP peer address"
                related = _bgp_peer_dependencies(identity, vrf, values, item, objects, dependencies, native_values)
                peer = project(
                    identity,
                    item,
                    values,
                    related,
                    {"password": "credentials are presence-only"},
                    reason,
                )
                peer_afs(item, peer, "peer_address_family")
            for item in scope.get("peer_group") or []:
                reason = scoped.reason
                try:
                    remote_as = _asn(item.get("remote_as"))
                except ValueError:
                    remote_as, reason = None, "invalid device BGP group AS"
                source = observed_value(item, "source")
                unavailable = {"source": "native BGP group source is not modeled"} if source is not MISSING else {}
                related = (
                    *dependencies.get(("asn", remote_as), ()),
                    *dependencies.get(("source", vrf, source), ()),
                    *dependencies.get(("peer_group", item["name"]), ()),
                )
                group = project(
                    (*scoped.identity, "peer_group", item["name"]),
                    item,
                    {"remote_as": remote_as, "source": item.get("source")},
                    related,
                    unavailable,
                    reason,
                )
                peer_afs(item, group, "peer_group_address_family")
    return tuple(result)


def _bgp_asn_identity(value):
    try:
        return parse_comparison_asn(value)
    except ValueError:
        return str(value)


def _bgp_address_identity(value):
    try:
        return str(ip_address(value))
    except ValueError:
        return value


def _bgp_reference_names(document):
    from .bgp_reconciler import _canonical_source_ip

    asns, addresses, interfaces, vrfs, groups = set(), set(), set(), set(), set()
    for router in document.get("routers") or []:
        asns.add(_bgp_asn_identity(router["asn"]))
        for scope in router.get("scope") or []:
            vrf = scope.get("vrf", "")
            if vrf:
                vrfs.add(vrf)
            for group in scope.get("peer_group") or []:
                groups.add(group["name"])
            for collection in ("peer", "peer_group"):
                for item in scope.get(collection) or []:
                    for name in ("remote_as", "local_as"):
                        if item.get(name) is not None:
                            asns.add(_bgp_asn_identity(item[name]))
                    if item.get("peer_group"):
                        groups.add(item["peer_group"])
                    if "peer_address" in item:
                        addresses.add((vrf, _bgp_address_identity(item["peer_address"])))
                    source = item.get("source")
                    if source:
                        try:
                            address = _canonical_source_ip(source)
                        except ValueError:
                            continue
                        (addresses if address else interfaces).add((vrf, address or source))
    return asns, addresses, interfaces, vrfs, groups


def _bgp_dependencies(document, management):
    """Resolve observed references even when the peer has no native binding."""
    from dcim.models import Interface
    from django.db.models import Q
    from ipam.models import ASN, VRF, IPAddress
    from netbox_routing.models import BGPPeerTemplate

    asns, addresses, interfaces, vrfs, groups = _bgp_reference_names(document)
    dependencies = defaultdict(tuple)
    for row in VRF.objects.filter(name__in=vrfs):
        dependencies[("vrf", row.name)] += (row,)
    for row in BGPPeerTemplate.objects.filter(name__in=groups).select_related("remote_as"):
        dependencies[("peer_group", row.name)] += (row, row.remote_as)
    for row in ASN.objects.filter(asn__in=[value for value in asns if isinstance(value, int)]):
        dependencies[("asn", int(row.asn))] += (row,)
    predicate = Q(pk__in=[])
    for vrf, address in addresses:
        try:
            ip_address(address)
        except ValueError:
            continue
        predicate |= (
            Q(address__net_host=address, vrf__name=vrf) if vrf else Q(address__net_host=address, vrf__isnull=True)
        )
    for row in IPAddress.objects.filter(predicate).select_related("vrf"):
        objects = (row, row.vrf)
        vrf = row.vrf.name if row.vrf else ""
        dependencies[("ip", vrf, str(row.address.ip))] += objects
        dependencies[("source", vrf, str(row.address.ip))] += objects
    interface_scopes = defaultdict(set)
    for vrf, name in interfaces:
        interface_scopes[name].add(vrf)
    for row in Interface.objects.filter(device_id=management.device_id, name__in=interface_scopes):
        for vrf in interface_scopes[row.name]:
            dependencies[("source", vrf, row.name)] += (row,)
    return dependencies


def device_entries(scope, management, snapshot, *, ned_id="", native_items=()):
    references = _reference_objects(snapshot.document)
    if scope == "bgp":
        objects = defaultdict(tuple)
        for item in native_items:
            objects[item.identity] += item.objects
        return _device_bgp(
            snapshot.document,
            objects,
            references,
            _bgp_dependencies(snapshot.document, management),
            native_values={item.identity: item.values for item in native_items},
        )
    return _device_policy(snapshot.document, references)


def component(scope, item):
    return "routers" if scope == "bgp" else item.identity[0]


def nested_gaps(scope, snapshot):
    gaps = {}

    def missing(parent, identity, collection):
        if observed_value(parent, collection) in (MISSING, None):
            gaps[((*identity, collection), collection)] = f"{collection} coverage is unavailable"

    if scope == "route_policy":
        for component_name in _POLICY_COMPONENTS.values():
            for parent in snapshot.document.get(component_name) or []:
                missing(parent, (component_name, parent["name"]), "entry")
        return gaps
    for router in snapshot.document.get("routers") or []:
        identity = ("router", _bgp_asn_identity(router["asn"]))
        missing(router, identity, "scope")
        for parent in router.get("scope") or []:
            scoped = (*identity, "scope", parent.get("vrf", ""))
            for collection in ("address_family", "peer", "peer_group"):
                missing(parent, scoped, collection)
            for peer in parent.get("peer") or []:
                missing(peer, (*scoped, "peer", _bgp_address_identity(peer["peer_address"])), "peer_address_family")
            for group in parent.get("peer_group") or []:
                missing(group, (*scoped, "peer_group", group["name"]), "peer_group_address_family")
    return gaps
