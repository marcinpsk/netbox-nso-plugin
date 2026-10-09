# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Project routing protocol intent without ownership qualification or writes."""

from collections import defaultdict

from .comparison_values import MISSING, ProjectedEntry, _normalized, observed_value

ATTRIBUTES = {
    "isis": (
        "net",
        "is_type",
        "metric_style",
        "overload_bit",
        "area_auth_type",
        "domain_auth_type",
        "area_auth_key_present",
        "domain_auth_key_present",
        "spf_initial_wait",
        "spf_max_wait",
        "lsp_initial_wait",
        "lsp_max_wait",
        "lsp_lifetime",
        "lsp_refresh_interval",
        "lsp_mtu",
        "overload_on_startup",
        "overload_timeout",
        "te_enabled",
        "suppress_attached_bit",
        "ignore_attached_bit",
        "fast_reroute",
        "microloop_avoidance",
        "distance",
        "maximum_paths",
        "reference_bandwidth",
        "process_tag",
        "circuit_type",
        "network_type",
        "metric",
        "passive",
        "bound_port",
        "hello_auth_type",
        "hello_auth_key_present",
        "bfd_enabled",
        "frr_enabled",
        "frr_protection",
        "csnp_interval",
        "retransmit_interval",
        "lsp_interval",
        "mesh_group",
        "value",
        "default_metric",
        "wide_metrics_only",
        "preference",
        "labeled_preference",
        "disabled",
        "auth_type",
        "auth_key_present",
        "hello_interval",
        "hello_multiplier",
        "priority",
        "enabled",
        "srv6_enabled",
        "prefix_sid_range",
        "srgb_start",
        "srgb_range",
        "srlb_start",
        "srlb_range",
        "maximum_sid_depth",
        "tunnel_table_pref",
        "node_sid_index",
        "node_sid_label",
        "node_sid_v6_index",
        "node_sid_v6_label",
        "prefix",
        "algorithm",
        "is_anycast",
        "is_micro_segment",
        "flavor",
        "block_length",
        "node_length",
        "function_length",
        "argument_length",
        "isis_level",
        "sid_index",
        "sid_label",
        "n_flag",
        "no_php",
        "explicit_null",
        "readvertise",
        "area_auth_present",
        "domain_auth_present",
        "hello_auth_present",
        "auth_present",
        "segment_routing_reported",
        "segment_routing_configured",
    ),
    "isis_flex_algo": (
        "metric_type",
        "priority",
        "admin_group_exclude",
        "admin_group_include_any",
        "admin_group_include_all",
    ),
    "ospf": (
        "router_id",
        "enabled",
        "area_type",
        "area_id",
        "passive",
        "priority",
        "cost",
        "network_type",
        "auth_type",
        "auth_key_present",
        "auth_present",
        "bfd_enabled",
    ),
    "redistribution": ("route_map", "metric", "metric_type"),
}

_CREDENTIALS = {
    "area_auth_key_present",
    "domain_auth_key_present",
    "hello_auth_key_present",
    "auth_key_present",
    "area_auth_present",
    "domain_auth_present",
    "hello_auth_present",
    "auth_present",
}
_CREDENTIAL_REASON = "credential values are not comparable"


def _native_values(row, names):
    return {name: getattr(row, name, MISSING) for name in names if not name.endswith("_key")}


def _wire_values(model, item, names):
    from .isis_reconciler import _absent_value

    instance = model()
    fields = {field.name for field in model._meta.get_fields()}
    values = {}
    for name in names:
        if name.endswith("_key"):
            continue
        value = observed_value(item, name)
        if name == "prefix" and value is not MISSING and value is not None:
            value = str(instance._meta.get_field(name).to_python(value))
        values[name] = _absent_value(instance, name) if value is None and name in fields else value
    return _normalized(item, values)


def _wire_entry(identity, model, item, names):
    from django.core.exceptions import ValidationError

    try:
        values = _wire_values(model, item, names)
        reason = ""
    except (TypeError, ValueError, ValidationError):
        values = {}
        reason = "observed routing values cannot be projected"
    return ProjectedEntry(identity, values, unavailable=_credentials(item), reason=reason)


def _credentials(item):
    return {name: _CREDENTIAL_REASON for name in _CREDENTIALS if observed_value(item, name) is not MISSING}


def _isis_fields(kind):
    from .template_content import _ISIS_IFACE_SCALAR_ATTRS, _ISIS_INSTANCE_SCALAR_ATTRS, _ISIS_INSTANCE_SCALAR_COLS

    if kind == "process":
        return tuple(
            name for name in (*_ISIS_INSTANCE_SCALAR_COLS, *_ISIS_INSTANCE_SCALAR_ATTRS) if not name.endswith("_key")
        )
    return ("process_tag", "bound_port", *_ISIS_IFACE_SCALAR_ATTRS)


def _isis_children(kind):
    from netbox_routing.models import (
        ISISInterfaceLevel,
        ISISLevel,
        ISISPrefixSID,
        ISISSegmentRouting,
        ISISSetting,
        ISISSRv6Locator,
    )

    from .template_content import (
        _ISIS_IFACE_LEVEL_COLS,
        _ISIS_LEVEL_COLS,
        _ISIS_PREFIX_SID_COLS,
        _ISIS_SRV6_LOCATOR_COLS,
        _SR_INSTANCE_COLS,
    )

    if kind == "process":
        return (
            ("setting", ISISSetting, "key", ("value",)),
            ("level", ISISLevel, "level", _ISIS_LEVEL_COLS),
            (
                "segment_routing",
                ISISSegmentRouting,
                None,
                (*_SR_INSTANCE_COLS, "node_sid_index", "node_sid_label", "node_sid_v6_index", "node_sid_v6_label"),
            ),
            ("srv6_locator", ISISSRv6Locator, "name", _ISIS_SRV6_LOCATOR_COLS),
        )
    return (
        ("setting", ISISSetting, "key", ("value",)),
        ("level", ISISInterfaceLevel, "level", _ISIS_IFACE_LEVEL_COLS),
        ("prefix_sid", ISISPrefixSID, "algorithm", _ISIS_PREFIX_SID_COLS),
    )


def _isis_native(management):
    from netbox_routing.models import ISISInstance, ISISInterface

    from .models import NSOISISInstanceState, NSOISISInterfaceState

    entries = []
    for kind, model, queryset, overlay_model, overlay_key in (
        (
            "process",
            ISISInstance,
            ISISInstance.objects.filter(device_id=management.device_id).select_related("vrf"),
            NSOISISInstanceState,
            "isis_instance_id",
        ),
        (
            "interface",
            ISISInterface,
            ISISInterface.objects.filter(interface__device_id=management.device_id).select_related(
                "interface", "instance__vrf"
            ),
            NSOISISInterfaceState,
            "isis_interface_id",
        ),
    ):
        parents = list(queryset.order_by("pk"))
        overlays = {getattr(row, overlay_key): row for row in overlay_model.objects.filter(management=management)}
        for row in parents:
            identity = (
                ("process", row.process_tag)
                if kind == "process"
                else ("interface", row.interface.name, row.address_family)
            )
            values = _native_values(row, _isis_fields(kind))
            overlay = overlays.get(row.pk)
            objects = (row, row.vrf, overlay) if kind == "process" else (row, row.interface, overlay)
            if kind == "interface":
                values.update(process_tag=row.instance.process_tag, bound_port=MISSING)
            entries.append(ProjectedEntry(identity, values, objects))
        entries.extend(_isis_native_children(kind, model, parents))
    return entries


def _isis_native_children(kind, parent_model, parents):
    from django.contrib.contenttypes.models import ContentType

    entries = []
    indexed = {row.pk: row for row in parents}
    for tag, model, key, names in _isis_children(kind):
        if tag == "setting":
            parent_field = "assigned_object_id"
            queryset = model.objects.filter(
                assigned_object_type=ContentType.objects.get_for_model(parent_model), assigned_object_id__in=indexed
            )
        else:
            parent_field = "instance_id" if kind == "process" else "interface_id"
            queryset = model.objects.filter(**{f"{parent_field}__in": indexed})
        for row in queryset.order_by("pk"):
            parent = indexed[getattr(row, parent_field)]
            prefix = (
                ("process", parent.process_tag)
                if kind == "process"
                else ("interface", parent.interface.name, parent.address_family)
            )
            identity = (*prefix, tag, getattr(row, key)) if key else (*prefix, tag)
            values = _native_values(row, names)
            if "prefix" in values and values["prefix"] is not MISSING:
                values["prefix"] = str(values["prefix"])
            entries.append(ProjectedEntry(identity, values, (row,)))
    return entries


def _flex_native(management):
    from netbox_routing.models import ISISFlexAlgo, ISISInstance

    from .models import NSOISISInstanceState

    overlays = {row.isis_instance_id: row for row in NSOISISInstanceState.objects.filter(management=management)}
    parents = [
        ProjectedEntry(
            ("process", row.process_tag),
            {},
            (row, row.vrf, overlays.get(row.pk)),
            coverage_only=True,
        )
        for row in ISISInstance.objects.filter(device_id=management.device_id).select_related("vrf").order_by("pk")
    ]
    return parents + [
        ProjectedEntry(
            ("process", row.instance.process_tag, "flex_algo", row.algo_id),
            _native_values(row, ATTRIBUTES["isis_flex_algo"]),
            (row, row.instance, row.instance.vrf),
        )
        for row in ISISFlexAlgo.objects.filter(instance__device_id=management.device_id)
        .select_related("instance__vrf")
        .order_by("pk")
    ]


def _ospf_native(management):
    from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

    from .models import NSOOSPFInstanceState, NSOOSPFInterfaceState
    from .ospf_reconciler import _ospf_instance_object_content, _ospf_interface_content
    from .template_content import _canonical_area_id

    overlays = {row.ospf_instance_id: row for row in NSOOSPFInstanceState.objects.filter(management=management)}
    iface_overlays = {row.interface_id: row for row in NSOOSPFInterfaceState.objects.filter(management=management)}
    interfaces = list(
        OSPFInterface.objects.filter(interface__device_id=management.device_id)
        .select_related("interface", "instance__vrf", "area")
        .order_by("pk")
    )
    areas = defaultdict(dict)
    for interface in interfaces:
        areas[interface.instance_id][interface.area_id] = interface.area
    instances = list(OSPFInstance.objects.filter(device_id=management.device_id).select_related("vrf").order_by("pk"))
    overlay_area_ids = {
        area["area-id"]
        for overlay in overlays.values()
        if isinstance(overlay.areas, list)
        for area in overlay.areas
        if isinstance(area, dict) and "area-id" in area
    }
    from .ospf_reconciler import _area_candidates

    candidates = {candidate for area_id in overlay_area_ids for candidate in _area_candidates(area_id)}
    overlay_areas = defaultdict(list)
    for area in OSPFArea.objects.filter(area_id__in=candidates):
        overlay_areas[_canonical_area_id(area.area_id)].append(area)
    entries = []
    for row in instances:
        content = _ospf_instance_object_content(row)
        overlay = overlays.get(row.pk)
        identity = ("instance", str(row.process_id), row.vrf.name if row.vrf else "")
        entries.append(
            ProjectedEntry(
                identity,
                {"router_id": content["router_id"], "enabled": overlay.enabled if overlay else MISSING},
                (row, row.vrf, overlay),
            )
        )
        for area in areas[row.pk].values():
            entries.append(
                ProjectedEntry(
                    (*identity, "area", _canonical_area_id(area.area_id)),
                    {"area_type": area.area_type},
                    (area, row, row.vrf),
                )
            )
        if overlay is not None and not isinstance(overlay.areas, list):
            entries.append(
                ProjectedEntry(
                    (*identity, "area", "invalid"),
                    {},
                    (row, row.vrf, overlay),
                    reason="invalid native OSPF area association",
                )
            )
        elif overlay is not None:
            native_area_ids = {_canonical_area_id(area.area_id) for area in areas[row.pk].values()}
            for item in overlay.areas:
                if not isinstance(item, dict) or "area-id" not in item:
                    entries.append(
                        ProjectedEntry(
                            (*identity, "area", "invalid"),
                            {},
                            (row, row.vrf, overlay),
                            reason="invalid native OSPF area association",
                        )
                    )
                    continue
                area_id = _canonical_area_id(item["area-id"])
                if area_id in native_area_ids:
                    continue
                matches = overlay_areas[area_id]
                if matches:
                    entries.extend(
                        ProjectedEntry(
                            (*identity, "area", area_id), {"area_type": area.area_type}, (area, row, row.vrf, overlay)
                        )
                        for area in matches
                    )
                else:
                    entries.append(
                        ProjectedEntry(
                            (*identity, "area", area_id),
                            {"area_type": item.get("area-type", MISSING)},
                            (row, row.vrf, overlay),
                        )
                    )
    for row in interfaces:
        content = _ospf_interface_content(
            row,
            {name: None for name in ("passive", "priority", "cost", "network_type", "authentication", "area")},
            object_values=True,
        )
        values = {name: content[name] for name in ("passive", "priority", "cost", "network_type")}
        values.update(area_id=content["area"], auth_type=content["authentication"], bfd_enabled=row.bfd)
        entries.append(
            ProjectedEntry(
                ("interface", row.interface.name, str(row.instance.process_id)),
                values,
                (row, row.interface, row.instance, row.instance.vrf, row.area, iface_overlays.get(row.interface_id)),
                parent_identities=(
                    ("instance", str(row.instance.process_id), row.instance.vrf.name if row.instance.vrf else ""),
                    (
                        "instance",
                        str(row.instance.process_id),
                        row.instance.vrf.name if row.instance.vrf else "",
                        "area",
                        content["area"],
                    ),
                ),
            )
        )
    return entries


def _redist_destinations(management):
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import BGPAddressFamily, ISISInstance, OSPFInstance

    from .models import NSOISISInstanceState, NSOOSPFInstanceState
    from .ownership_planner import _destination_reference

    overlays = {
        "isis": {row.isis_instance_id: row for row in NSOISISInstanceState.objects.filter(management=management)},
        "ospf": {row.ospf_instance_id: row for row in NSOOSPFInstanceState.objects.filter(management=management)},
    }

    destinations = {}
    for model, queryset in (
        (ISISInstance, ISISInstance.objects.filter(device_id=management.device_id).select_related("vrf")),
        (OSPFInstance, OSPFInstance.objects.filter(device_id=management.device_id).select_related("vrf")),
        (
            BGPAddressFamily,
            BGPAddressFamily.objects.filter(
                scope__router__assigned_object_type=ContentType.objects.get_for_model(Device),
                scope__router__assigned_object_id=management.device_id,
            ).select_related("scope__router__asn", "scope__vrf"),
        ),
    ):
        content_type = ContentType.objects.get_for_model(model).pk
        for row in queryset:
            protocol, reference = _destination_reference(row)
            objects = (row,)
            vrf = None
            if protocol == "bgp":
                objects += (row.scope, row.scope.router, row.scope.router.asn, row.scope.vrf)
            elif protocol == "ospf":
                vrf = row.vrf.name if row.vrf else ""
                objects += (row.vrf, overlays[protocol].get(row.pk))
            elif protocol == "isis":
                objects += (row.vrf, overlays[protocol].get(row.pk))
            destinations[(content_type, row.pk)] = (protocol, reference, vrf, objects)
    return destinations


def _redist_reference(protocol, reference):
    from .routing_policy_projection import parse_comparison_asn

    reference = reference or ""
    if protocol == "bgp" and reference:
        parts = reference.split("/")
        parts[0] = str(parse_comparison_asn(parts[0]))
        return "/".join(parts)
    return reference.strip()


def _redist_identity(item):
    from .routing_policy_projection import parse_comparison_asn

    protocol = item["dest_protocol"]
    source_protocol = item["source_protocol"]
    source_ref = item.get("source_ref", "")
    source_ref = (
        str(parse_comparison_asn(source_ref))
        if source_protocol == "bgp"
        else _redist_reference(source_protocol, source_ref)
    )
    return (
        protocol,
        _redist_reference(protocol, item["dest_ref"]),
        _redist_destination_vrf_key(protocol, item.get("dest_vrf")),
        source_protocol,
        source_ref,
    )


def _redist_destination_vrf_key(protocol, vrf):
    """Return the destination VRF as the native destination key stores it."""
    if protocol == "ospf":
        return vrf or ""
    if vrf:
        raise ValueError("only an OSPF destination carries a VRF")
    return None


def _redist_native(management):
    from django.db.models import Q
    from netbox_routing.models import Redistribution

    from .redistribution_reconciler import _redist_object_content

    destinations = _redist_destinations(management)
    predicate = Q(pk__in=[])
    for content_type, pk in destinations:
        predicate |= Q(destination_type_id=content_type, destination_id=pk)
    entries = [
        ProjectedEntry((protocol, _redist_reference(protocol, reference), vrf), {}, objects, coverage_only=True)
        for protocol, reference, vrf, objects in destinations.values()
    ]
    for row in Redistribution.objects.filter(predicate).select_related("route_map").order_by("pk"):
        protocol, reference, vrf, objects = destinations[(row.destination_type_id, row.destination_id)]
        item = {
            "dest_protocol": protocol,
            "dest_ref": reference,
            "dest_vrf": vrf,
            "source_protocol": row.source_protocol,
            "source_ref": row.source_ref,
        }
        values = _redist_object_content(row)
        values["route_map"] = row.route_map.name if row.route_map else ""
        try:
            identity = _redist_identity(item)
            reason = ""
        except (TypeError, ValueError):
            identity = ("invalid", row.pk)
            reason = "invalid native redistribution identity"
        entries.append(ProjectedEntry(identity, values, (row, row.route_map, *objects), reason=reason))
    return _redist_source_dependencies(entries, management)


def _redist_source_dependencies(entries, management):
    from ipam.models import ASN
    from netbox_routing.models import ISISInstance, OSPFInstance

    references = defaultdict(set)
    for entry in entries:
        if len(entry.identity) == 5:
            references[entry.identity[3]].add(entry.identity[4])
    sources = defaultdict(list)
    for row in ASN.objects.filter(asn__in=references["bgp"]):
        sources[("bgp", str(row.asn))].append(row)
    for row in ISISInstance.objects.filter(
        device_id=management.device_id, process_tag__in=references["isis"]
    ).select_related("vrf"):
        sources[("isis", row.process_tag)].extend((row, row.vrf))
    for row in OSPFInstance.objects.filter(
        device_id=management.device_id, process_id__in=references["ospf"]
    ).select_related("vrf"):
        sources[("ospf", str(row.process_id))].extend((row, row.vrf))
    for entry in entries:
        if len(entry.identity) == 5:
            entry.objects += tuple(sources[entry.identity[3:]])
    return entries


def native_entries(scope, management, ned_id=""):
    return {
        "isis": _isis_native,
        "isis_flex_algo": _flex_native,
        "ospf": _ospf_native,
        "redistribution": _redist_native,
    }[scope](management)


def _isis_device(scope, document, *, defaults=None):
    from netbox_routing.models import ISISFlexAlgo, ISISInstance, ISISInterface

    entries = []
    for kind, collection, model in (("process", "processes", ISISInstance), ("interface", "interfaces", ISISInterface)):
        if scope == "isis_flex_algo" and kind == "interface":
            continue
        for item in document.get(collection) or []:
            prefix = (
                ("process", item.get("process_tag", ""))
                if kind == "process"
                else ("interface", item["interface_name"], item["af"])
            )
            if scope == "isis_flex_algo":
                entries.append(ProjectedEntry(prefix, {}, coverage_only=True))
                for child in item.get("flex_algo") or []:
                    entries.append(
                        _wire_entry((*prefix, "flex_algo", child["algo_id"]), ISISFlexAlgo, child, ATTRIBUTES[scope])
                    )
                continue
            parent = _wire_entry(prefix, model, item, _isis_fields(kind))
            _apply_isis_defaults(parent, item, (defaults or {}).get(kind, {}))
            entries.append(parent)
            for tag, child_model, key, names in _isis_children(kind):
                children = [item[tag]] if key is None and item.get(tag) is not None else item.get(tag) or []
                for child in children:
                    identity = (*prefix, tag, child[key]) if key else (*prefix, tag)
                    entry = _wire_entry(identity, child_model, child, names)
                    if tag == "srv6_locator":
                        _apply_isis_defaults(entry, child, (defaults or {}).get(tag, {}), null_is_omitted=True)
                    entries.append(entry)
    return entries


def _apply_isis_defaults(entry, item, defaults, *, null_is_omitted=False):
    for name, value in defaults.items():
        observed = observed_value(item, name)
        if observed is MISSING or (null_is_omitted and observed is None):
            entry.values[name] = value


def _isis_omitted_defaults(management):
    from .template_content import (
        _ISIS_PROCESS_FLAG_DEFAULTS,
        _device_uses_timos_ned,
        _isis_instance_omitted_defaults,
        _isis_srv6_locator_omitted_defaults,
    )

    return {
        "process": {**_ISIS_PROCESS_FLAG_DEFAULTS, **_isis_instance_omitted_defaults(management.device)},
        "srv6_locator": _isis_srv6_locator_omitted_defaults(management.device),
        "interface": {name: None for name in ("csnp_interval", "retransmit_interval", "lsp_interval")}
        if _device_uses_timos_ned(management.device)
        else {},
    }


def _ospf_device(document):
    from .ospf_reconciler import _ospf_interface_fields
    from .template_content import _canonical_area_id, _clean_router_id

    entries = []
    for item in document.get("instances") or []:
        identity = ("instance", str(item["process_id"]), item.get("vrf", ""))
        values = _normalized(
            item, {"router_id": str(_clean_router_id(item.get("router_id"))), "enabled": item.get("enabled")}
        )
        entries.append(ProjectedEntry(identity, values))
        for area in item.get("area") or []:
            entries.append(
                ProjectedEntry(
                    (*identity, "area", _canonical_area_id(area["area_id"])),
                    _normalized(area, {"area_type": area.get("area_type")}),
                )
            )
    for item in document.get("interfaces") or []:
        fields = _ospf_interface_fields(item, None, None)
        values = {name: fields[name] for name in ("passive", "priority", "cost", "network_type")}
        values["auth_type"] = fields["authentication"]
        values.update(
            _normalized(
                item,
                {
                    "area_id": _canonical_area_id(item["area_id"]) if item.get("area_id") is not None else None,
                    "bfd_enabled": item.get("bfd_enabled"),
                },
            )
        )
        process = item.get("process_id")
        entries.append(
            ProjectedEntry(
                ("interface", item["interface_name"], str(process) if process is not None else None),
                values,
                unavailable=_credentials(item),
            )
        )
    return entries


def _redist_device(document):
    from .redistribution_reconciler import _redist_device_content

    entries = []
    for index, item in enumerate(document.get("entries") or []):
        try:
            identity = _redist_identity(item)
            reason = ""
        except (TypeError, ValueError):
            identity = ("invalid", index)
            reason = "invalid observed redistribution identity"
        values = _redist_device_content(item, None)
        values["route_map"] = item.get("route_map") or ""
        entries.append(ProjectedEntry(identity, _normalized(item, values), reason=reason))
    return entries


def _redist_inventory_reason(component):
    from .routing_policy_projection import parse_comparison_asn

    protocol = component.get("protocol")
    if protocol not in {"bgp", "isis", "ospf"}:
        return "redistribution inventory protocol is unavailable"
    if protocol == "bgp":
        try:
            for parent in component.get("inventory") or []:
                parse_comparison_asn(parent["asn"])
        except ValueError:
            return "redistribution inventory has an invalid AS number"
    return ""


def _redist_coverage_entries(document):
    entries = []
    for component in document.get("components") or []:
        if _redist_inventory_reason(component):
            continue
        protocol = component["protocol"]
        for parent in component.get("inventory") or []:
            if protocol in {"isis", "ospf"}:
                reference = parent.get("process_tag", "") if protocol == "isis" else str(parent["process_id"])
                vrf = parent.get("vrf", "") if protocol == "ospf" else None
                entries.append(ProjectedEntry((protocol, reference, vrf), {}, coverage_only=True))
                continue
            asn = _redist_reference("bgp", parent["asn"])
            entries.append(ProjectedEntry(("bgp",), {"router_asn": asn}, coverage_only=True))
            for scope in parent.get("scope") or []:
                vrf = scope.get("vrf", "")
                entries.append(ProjectedEntry(("bgp",), {"router_asn": asn, "vrf": vrf}, coverage_only=True))
                for family in scope.get("address_family") or []:
                    reference = f"{asn}/{vrf}" + (f"/{family['afi']}" if family.get("afi") else "")
                    entries.append(ProjectedEntry(("bgp", reference, None), {}, coverage_only=True))
    return entries


def _device_interfaces(management):
    from dcim.models import Interface

    interfaces = defaultdict(list)
    for row in Interface.objects.filter(device_id=management.device_id):
        interfaces[row.name].append(row)
    return interfaces


def _redist_device_dependencies(management, snapshot, entries):
    from netbox_routing.models import RouteMap

    dependencies = defaultdict(list)
    for protocol, reference, vrf, objects in _redist_destinations(management).values():
        dependencies[(protocol, _redist_reference(protocol, reference), vrf)].extend(objects)
    names = {entry.values.get("route_map") for entry in entries} - {None, "", MISSING}
    policies = defaultdict(list)
    for row in RouteMap.objects.filter(name__in=names):
        policies[row.name].append(row)
    roots, scopes, vrfs = _redist_coverage_dependencies(management, entries)
    for entry in entries:
        entry.objects = tuple(dependencies[entry.identity[:3]] + policies[entry.values.get("route_map")])
        entry.objects += tuple(vrfs[_redist_destination_vrf(entry.identity)])
        asn = entry.values.get("router_asn")
        if asn is not None:
            entry.objects += tuple(roots[asn])
            if "vrf" in entry.values:
                vrf = entry.values["vrf"]
                entry.objects += tuple(scopes[(asn, vrf)] + vrfs[vrf])
    return _redist_source_dependencies(entries, management)


def _redist_destination_vrf(identity):
    if len(identity) >= 3:
        if identity[0] == "ospf":
            return identity[2] or ""
        if identity[0] == "bgp":
            parts = identity[1].split("/")
            return parts[1] if len(parts) > 1 else ""
    return ""


def _redist_coverage_dependencies(management, entries):
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import VRF
    from netbox_routing.models import BGPRouter, BGPScope

    roots = defaultdict(list)
    scopes = defaultdict(list)
    vrfs = defaultdict(list)
    if any("router_asn" in entry.values for entry in entries):
        assigned = {
            "assigned_object_type": ContentType.objects.get_for_model(Device),
            "assigned_object_id": management.device_id,
        }
        for row in BGPRouter.objects.filter(**assigned).select_related("asn"):
            if row.asn is not None:
                roots[str(row.asn.asn)].extend((row, row.asn))
        scope_filter = {f"router__{name}": value for name, value in assigned.items()}
        for row in BGPScope.objects.filter(**scope_filter).select_related("router__asn", "vrf"):
            if row.router.asn is not None:
                scopes[(str(row.router.asn.asn), row.vrf.name if row.vrf else "")].extend(
                    (row, row.router, row.router.asn, row.vrf)
                )
    names = {entry.values["vrf"] for entry in entries if entry.values.get("vrf")}
    names.update(_redist_destination_vrf(entry.identity) for entry in entries)
    names.discard("")
    for row in VRF.objects.filter(name__in=names):
        vrfs[row.name].append(row)
    return roots, scopes, vrfs


def _isis_device_dependencies(management, snapshot, entries):
    from netbox_routing.models import ISISInstance

    interfaces = _device_interfaces(management)
    processes = defaultdict(list)
    for row in ISISInstance.objects.filter(device_id=management.device_id).select_related("vrf"):
        processes[row.process_tag].extend((row, row.vrf))
    reported_interfaces = defaultdict(list)
    for item in snapshot.document.get("interfaces") or []:
        reported_interfaces[(item["interface_name"], item["af"])].extend(
            interfaces[item["interface_name"]]
            + interfaces[observed_value(item, "bound_port")]
            + processes[item.get("process_tag", "")]
        )
    for entry in entries:
        if entry.identity[0] == "process" and len(entry.identity) == 2:
            entry.objects = tuple(processes[entry.identity[1]])
        elif entry.identity[0] == "interface" and len(entry.identity) == 3:
            entry.objects = tuple(reported_interfaces[entry.identity[1:3]])
    return entries


def _ospf_device_dependencies(management, snapshot, entries, *, native_items=()):
    from ipam.models import VRF
    from netbox_routing.models import OSPFArea, OSPFInstance

    from .ospf_reconciler import _area_candidates
    from .template_content import _canonical_area_id

    interfaces = _device_interfaces(management)
    instances = defaultdict(list)
    by_process = defaultdict(set)
    interface_parents = defaultdict(set)
    for entry in entries:
        if entry.identity[0] == "instance" and len(entry.identity) == 3:
            by_process[entry.identity[1]].add(entry.identity)
    for entry in native_items:
        if entry.identity[0] == "interface":
            interface_parents[entry.identity].update(
                parent for parent in entry.parent_identities or () if len(parent) == 3
            )
    for row in OSPFInstance.objects.filter(device_id=management.device_id).select_related("vrf"):
        objects = [row, row.vrf]
        instances[(str(row.process_id), row.vrf.name if row.vrf else "")].extend(objects)
        by_process[str(row.process_id)].add(("instance", str(row.process_id), row.vrf.name if row.vrf else ""))
    vrfs = defaultdict(list)
    names = {entry.identity[2] for entry in entries if entry.identity[0] == "instance" and entry.identity[2]}
    for row in VRF.objects.filter(name__in=names):
        vrfs[row.name].append(row)
    area_ids = {entry.identity[-1] for entry in entries if "area" in entry.identity}
    area_ids.update(entry.values["area_id"] for entry in entries if entry.values.get("area_id") not in {None, MISSING})
    area_candidates = {candidate for area_id in area_ids for candidate in _area_candidates(area_id)}
    areas = defaultdict(list)
    for row in OSPFArea.objects.filter(area_id__in=area_candidates):
        areas[_canonical_area_id(row.area_id)].append(row)
    for entry in entries:
        if entry.identity[0] == "instance":
            entry.objects = tuple(instances[entry.identity[1:3]] + vrfs[entry.identity[2]])
            if len(entry.identity) > 3:
                entry.objects += tuple(areas[entry.identity[-1]])
        else:
            parents = interface_parents[entry.identity] or by_process[entry.identity[2]]
            area = entry.values.get("area_id", MISSING)
            entry.parent_identities = tuple(parents) + tuple((*parent, "area", area) for parent in parents)
            entry.objects = tuple(
                interfaces[entry.identity[1]]
                + [obj for parent in parents for obj in instances[parent[1:3]]]
                + areas[area]
            )
    return entries


def device_entries(scope, management, snapshot, ned_id="", *, native_items=()):
    if scope in {"isis", "isis_flex_algo"}:
        entries = _isis_device(
            scope, snapshot.document, defaults=_isis_omitted_defaults(management) if scope == "isis" else None
        )
    elif scope == "ospf":
        entries = _ospf_device(snapshot.document)
    else:
        entries = [*_redist_device(snapshot.document), *_redist_coverage_entries(snapshot.document)]
    dependencies = {
        "isis": _isis_device_dependencies,
        "isis_flex_algo": _isis_device_dependencies,
        "ospf": _ospf_device_dependencies,
        "redistribution": _redist_device_dependencies,
    }
    options = {"native_items": native_items} if scope == "ospf" else {}
    return dependencies[scope](management, snapshot, entries, **options)


def component(scope, item):
    identity = item.identity
    if scope == "redistribution":
        return identity[0]
    if scope == "isis_flex_algo":
        return "processes"
    return {"process": "processes", "instance": "instances", "interface": "interfaces"}[identity[0]]


def _collection_gap(item, name):
    if (
        name == "segment_routing"
        and observed_value(item, "segment_routing_reported") is True
        and observed_value(item, "segment_routing_configured") is False
    ):
        return ""
    value = observed_value(item, name)
    if value is MISSING:
        return f"{name} collection was not reported"
    if value is None:
        return f"{name} collection is not comparable"
    return ""


def nested_gaps(scope, snapshot):
    document = snapshot.document
    gaps = {}
    if scope in {"isis", "isis_flex_algo"}:
        for kind, collection in (("process", "processes"), ("interface", "interfaces")):
            for item in document.get(collection) or []:
                prefix = (
                    ("process", item.get("process_tag", ""))
                    if kind == "process"
                    else ("interface", item["interface_name"], item["af"])
                )
                if scope == "isis_flex_algo":
                    names = ("flex_algo",) if kind == "process" else ()
                else:
                    names = (
                        ("setting", "level", "segment_routing", "srv6_locator")
                        if kind == "process"
                        else ("setting", "level", "prefix_sid")
                    )
                for name in names:
                    reason = _collection_gap(item, name)
                    if reason:
                        gaps[((*prefix, name), "")] = reason
    elif scope == "ospf":
        for item in document.get("instances") or []:
            reason = _collection_gap(item, "area")
            if reason:
                gaps[(("instance", str(item["process_id"]), item.get("vrf", ""), "area"), "")] = reason
    elif scope == "redistribution":
        covered = {item["protocol"] for item in snapshot.coverage.get("components", [])}
        gaps.update(_redist_nested_gaps(document, covered))
    return gaps


def _redist_nested_gaps(document, covered):
    gaps = {}
    for item in document.get("components") or []:
        protocol = item.get("protocol")
        if protocol not in covered or _redist_inventory_reason(item):
            continue
        for destination in item.get("inventory") or []:
            if protocol != "bgp":
                reference = destination.get("process_tag", "") if protocol == "isis" else str(destination["process_id"])
                vrf = destination.get("vrf", "") if protocol == "ospf" else None
                reason = _collection_gap(destination, "redistribute")
                if reason:
                    gaps[((protocol, reference, vrf), "redistribute")] = reason
                continue
            asn = _redist_reference("bgp", destination["asn"])
            reason = _collection_gap(destination, "scope")
            if reason:
                gaps[(("bgp",), "scope")] = reason
            for scope in destination.get("scope") or []:
                reason = _collection_gap(scope, "address_family")
                if reason:
                    gaps[(("bgp",), "address_family")] = reason
                for family in scope.get("address_family") or []:
                    reference = f"{asn}/{scope.get('vrf', '')}" + (f"/{family['afi']}" if family.get("afi") else "")
                    reason = _collection_gap(family, "redistribute")
                    if reason:
                        gaps[(("bgp", reference, None), "redistribute")] = reason
    return gaps


def component_gaps(snapshot):
    covered = {item["protocol"] for item in snapshot.coverage.get("components", [])}
    inventories = {item.get("protocol"): item for item in snapshot.document.get("components", [])}
    gaps = {}
    for protocol in ("bgp", "isis", "ospf"):
        if protocol not in covered:
            gaps[protocol] = "redistribution component is not covered"
        elif protocol not in inventories:
            gaps[protocol] = "redistribution component was not reported"
        else:
            reason = _collection_gap(inventories[protocol], "inventory") or _redist_inventory_reason(
                inventories[protocol]
            )
            if reason:
                gaps[protocol] = reason
    return gaps
