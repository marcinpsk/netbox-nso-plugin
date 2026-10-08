# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Project scope bindings with their reconciler normalizers."""

from .comparison_values import (
    MISSING,
    ProjectedEntry,
    _normalized,
    _values,
    inherit_parent_dependencies,
    observed_value,
)
from .ownership_planner import _NATIVE_BINDING_BUILDERS, _native_binding, converted_scope_rules, device_interfaces


def routing_projection(scope):
    from . import routing_policy_projection, routing_protocol_projection

    return routing_policy_projection if scope in routing_policy_projection.ATTRIBUTES else routing_protocol_projection


def _interface_comparison_bindings(management, scope):
    from dcim.models import Interface
    from django.db.models import Q

    if scope == "lacp":
        from .lacp_topology import bundle_interfaces, member_interfaces

        return (
            *(
                _native_binding(scope, row, "netbox_nso_plugin.nsolacpbundlestate")
                for row in bundle_interfaces(management.device_id)
            ),
            *(
                _native_binding(scope, row, "netbox_nso_plugin.nsolacpmemberstate")
                for row in member_interfaces(management.device_id).select_related("lag")
            ),
        )
    interfaces = Interface.objects.filter(device_id=management.device_id)
    if scope == "switchport":
        interfaces = (
            interfaces.filter(Q(mode__gt="") | Q(untagged_vlan_id__isnull=False) | Q(tagged_vlans__isnull=False))
            .select_related("untagged_vlan")
            .prefetch_related("tagged_vlans")
        )
        label = "netbox_nso_plugin.nsoswitchportstate"
    else:
        interfaces = interfaces.filter(Q(mtu__isnull=False) | Q(nso_mtu_states__management_id=management.pk))
        label = "netbox_nso_plugin.nsointerfacemtustate"
    return tuple(_native_binding(scope, row, label) for row in interfaces.distinct().order_by("pk"))


def _comparison_bindings(management, scope):
    """Return the scope bindings before ownership qualification filters."""
    from django.apps import apps

    if scope in {"lacp", "switchport", "interface_mtu"}:
        return _interface_comparison_bindings(management, scope)
    if scope == "static_route":
        from netbox_routing.models import StaticRoute

        return tuple(
            _native_binding(scope, row, "netbox_nso_plugin.nsostaticroutestate")
            for row in StaticRoute.objects.filter(devices__id=management.device_id)
            .select_related("vrf")
            .distinct()
            .order_by("pk")
        )
    builder = _NATIVE_BINDING_BUILDERS.get(scope)
    if builder is not None:
        return builder(management)
    if scope == "bfd":
        from netbox_routing.models import BFDInterface

        return tuple(
            _native_binding(scope, row, "netbox_nso_plugin.nsobfdinterfacestate")
            for row in BFDInterface.objects.filter(interface__device_id=management.device_id)
            .select_related("interface", "bfd_profile")
            .order_by("pk")
        )
    rule = converted_scope_rules()[scope]
    bindings = []
    for label in rule.overlay_model_labels:
        model = apps.get_model(label)
        rows = model.objects.filter(management=management)
        if scope in {"svi", "subinterface"}:
            rows = rows.filter(interface__device_id=management.device_id)
        relations = {
            "svi": ("interface", "vlan"),
            "subinterface": ("interface__parent", "parent_interface"),
            "l2_sap": ("l2vpn", "termination"),
        }.get(scope, ())
        if relations:
            rows = rows.select_related(*relations)
        bindings.extend(_native_binding(scope, row, label) for row in rows.order_by("pk"))
    return tuple(bindings)


def _overlay_values(overlay, names):
    return {name: getattr(overlay, name) if overlay is not None else MISSING for name in names}


def _lacp_native(management, interface, *, overlays):
    from .lacp_topology import bundle_of, is_bundle

    if is_bundle(interface, management.device_id):
        overlay = overlays.get(("NSOLACPBundleState", interface.pk))
        names = ("lag_id", "min_links", "system_priority", "system_id", "timer", "admin_key", "vpc_sensitive")
        return ProjectedEntry(("bundle", interface.name), _overlay_values(overlay, names), (interface, overlay))
    bundle = bundle_of(interface)
    overlay = overlays.get(("NSOLACPMemberState", interface.pk))
    values = {"bundle": bundle.name if bundle else MISSING, **_overlay_values(overlay, ("mode", "port_priority"))}
    return ProjectedEntry(
        ("member", interface.name),
        values,
        (interface, bundle, overlay),
        reason="native bundle is unavailable" if bundle is None else "",
    )


def _vlan_native(management, vlan):
    from .vlan_reconciler import _validated_vlan_items

    item = _validated_vlan_items({"vlans": [{"vlan_id": vlan.vid, "name": vlan.name}]})[0]
    return ProjectedEntry(item["vlan_id"], {"name": item["name"]}, (vlan,))


def _switchport_native(management, interface):
    tagged = tuple(interface.tagged_vlans.all())
    untagged = interface.untagged_vlan
    return ProjectedEntry(
        interface.name,
        {
            "mode": interface.mode,
            "untagged_vlan": untagged.vid if untagged else None,
            "tagged_vlans": sorted(vlan.vid for vlan in tagged),
        },
        (interface, untagged, *tagged),
    )


def _mtu_native(management, interface, *, overlays):
    from .interface_mtu_reconciler import _validated_interface_items

    overlay = overlays.get(("NSOInterfaceMtuState", interface.pk))
    values = _overlay_values(overlay, ("ip_mtu", "mpls_mtu", "bound_port"))
    values["mtu"] = interface.mtu
    wire = {
        "interface_name": interface.name,
        **{name: None if value is MISSING else value for name, value in values.items()},
    }
    _validated_interface_items({"interfaces": [wire]})
    return ProjectedEntry(interface.name, values, (interface, overlay))


def _svi_native(management, row):
    from .svi_reconciler import svi_values

    svi_type, vrf = svi_values({"type": row.svi_type, "vrf": row.vrf})
    return ProjectedEntry(
        row.interface.name,
        {"vlan_id": row.vlan.vid if row.vlan else MISSING, "type": svi_type, "vrf": vrf},
        (row, row.interface, row.vlan),
        reason="native VLAN is unavailable" if row.vlan is None else "",
    )


def _subinterface_native(management, row):
    from .subinterface_reconciler import subinterface_values

    parent = row.interface.parent
    overlay_parent = row.parent_interface
    reason = "native parent interface is unavailable" if parent is None else ""
    if row.interface.parent_id != row.parent_interface_id:
        reason = "native and overlay parent interfaces are inconsistent"
    parent_name, tag, vrf = subinterface_values(
        {"parent_interface": parent.name if parent else None, "dot1q_vlan": row.dot1q_vlan, "vrf": row.vrf}
    )
    return ProjectedEntry(
        row.interface.name,
        {"parent_interface": parent_name, "dot1q_vlan": tag, "type": "subinterface", "vrf": vrf},
        (row, row.interface, parent, overlay_parent),
        reason=reason,
    )


def _bfd_native(management, row):
    from .bfd_reconciler import _profile_values

    profile = row.bfd_profile
    values = {
        "enabled": row.enabled,
        "micro_bfd": row.micro_bfd,
        "min_tx": profile.min_tx_int if profile else None,
        "min_rx": profile.min_rx_int if profile else None,
        "multiplier": profile.multiplier if profile else None,
    }
    reason = "invalid native BFD profile" if profile is not None and _profile_values(values) is None else ""
    return ProjectedEntry(row.interface.name, values, (row, row.interface, profile), reason=reason)


def _l2_native(management, row):
    from .adapter_client import AdapterError
    from .l2_service_reconciler import _validated_l2_services, l2_sap_values

    objects = (row, row.l2vpn, row.termination)
    try:
        _validated_l2_services(
            {
                "services": [
                    {
                        "service_name": row.service_name,
                        "service_type": row.service_type,
                        "service_id": row.service_id,
                        "saps": [
                            {
                                "sap_id": row.sap_id,
                                "port": row.port,
                                "outer_tag": row.outer_tag,
                                "inner_tag": row.inner_tag,
                            }
                        ],
                    }
                ]
            }
        )

        service_type, port, outer_tag, inner_tag = l2_sap_values(
            {"service_type": row.service_type},
            {"port": row.port, "outer_tag": row.outer_tag, "inner_tag": row.inner_tag},
        )
    except (AdapterError, ValueError) as exc:
        return ProjectedEntry((row.service_name, row.sap_id), {}, objects, reason=str(exc))
    return ProjectedEntry(
        (row.service_name, row.sap_id),
        {
            "service_type": service_type,
            "service_id": row.service_id,
            "port": port,
            "outer_tag": outer_tag,
            "inner_tag": inner_tag,
        },
        objects,
    )


def _logging_native(management, row, *, ned_id):
    from .template_content import _canonical_logging_field

    if row._meta.model_name == "nsologginghoststate":
        names = ("port", "severity", "facility", "transport", "vrf", "source")
        values = {name: _canonical_logging_field(ned_id, name, getattr(row, name)) for name in names}
        return ProjectedEntry(("host", row.address), values, (row,))
    values = {
        name: _canonical_logging_field(ned_id, "severity", getattr(row, name))
        for name in ("console_severity", "monitor_severity", "module_severity")
    }
    return ProjectedEntry(("local_levels",), values, (row,))


def _snmp_native(management, row, *, compare_supported):
    from .snmp_versions import canonical_snmp_version
    from .vault_refs import is_secret_fingerprint

    kind = row._meta.model_name
    if kind == "nsosnmpcommunitystate":
        if not is_secret_fingerprint(row.community_hash):
            return ProjectedEntry(("community", ""), {}, (row,), reason="invalid community fingerprint")
        unavailable = {}
        if not is_secret_fingerprint(row.vault_secret_hash) or not compare_supported:
            unavailable["secret"] = "comparable secret fingerprint is unavailable"
        return ProjectedEntry(
            ("community", row.community_hash),
            {"access": row.access, "acl": row.acl, "secret": row.vault_secret_hash},
            (row,),
            unavailable,
        )
    if kind == "nsosnmpv3userstate":
        return ProjectedEntry(
            ("user", row.username),
            {"auth_secret": MISSING, "priv_secret": MISSING},
            (row,),
            {
                name: "secret fingerprint is unavailable; the device reports presence only"
                for name in ("auth_secret", "priv_secret")
            },
        )
    if kind == "nsosnmphoststate":
        return ProjectedEntry(
            ("host", row.address),
            {
                "version": canonical_snmp_version(row.version),
                "notify_type": row.notify_type,
                "port": row.port,
                "user": row.username,
            },
            (row,),
        )
    return ProjectedEntry(("system",), {"location": row.location, "contact": row.contact}, (row,))


def _route_native(management, row):
    from .template_content import static_route_interface_next_hop, static_route_name, static_route_permanent

    values = {
        "vrf": row.vrf.name if row.vrf else "",
        "prefix": str(row.prefix),
        "next_hop": str(row.next_hop) if row.next_hop is not None else None,
        "interface_next_hop": static_route_interface_next_hop(row.interface_next_hop),
        "metric": row.metric,
        "permanent": static_route_permanent(row.permanent),
        "tag": row.tag,
        "name": static_route_name(row.name),
        "next_hop_vrf": MISSING,
    }
    return ProjectedEntry(
        _route_identity(values), values, (row, row.vrf), {"next_hop_vrf": "NetBox has no next-hop VRF field"}
    )


_NATIVE_PROJECTORS = {
    "lacp": _lacp_native,
    "vlan": _vlan_native,
    "switchport": _switchport_native,
    "interface_mtu": _mtu_native,
    "svi": _svi_native,
    "subinterface": _subinterface_native,
    "bfd": _bfd_native,
    "l2_sap": _l2_native,
    "logging": _logging_native,
    "snmp": _snmp_native,
    "static_route": _route_native,
}


def native_entries(scope, management, *, ned_id=""):
    if scope in {"bgp", "isis", "isis_flex_algo", "ospf", "redistribution", "route_policy"}:
        return routing_projection(scope).native_entries(scope, management, ned_id=ned_id)
    from .adapter_client import AdapterError

    bindings = _comparison_bindings(management, scope)
    options = {}
    if scope in {"lacp", "interface_mtu"}:
        from django.apps import apps

        labels = ("NSOLACPBundleState", "NSOLACPMemberState") if scope == "lacp" else ("NSOInterfaceMtuState",)
        interface_ids = [row.pk for _scope, row, _model, _key in bindings]
        options["overlays"] = {
            (label, overlay.interface_id): overlay
            for label in labels
            for overlay in apps.get_model("netbox_nso_plugin", label).objects.filter(
                management=management,
                interface_id__in=interface_ids,
            )
        }
    elif scope == "logging":
        options["ned_id"] = ned_id
    elif scope == "snmp":
        from .template_content import _snmp_value_compare_supported

        options["compare_supported"] = _snmp_value_compare_supported(management.device)
    entries = []
    if scope == "l2_sap":
        from vpn.models import L2VPN

        prefix = f"nso-{management.device_id}-"
        entries.extend(
            ProjectedEntry((row.slug[len(prefix) :],), {}, (row,), coverage_only=True)
            for row in L2VPN.objects.filter(slug__startswith=prefix).order_by("pk")
        )
        entries.extend(
            ProjectedEntry((row.service_name,), {}, (row.l2vpn,), coverage_only=True)
            for _scope, row, _model, _key in bindings
            if row.l2vpn is not None
        )
    for _scope, row, _model, _key in bindings:
        try:
            entries.append(_NATIVE_PROJECTORS[scope](management, row, **options))
        except (AdapterError, ValueError) as exc:
            entries.append(ProjectedEntry(f"native {row.pk}", {}, (row,), reason=str(exc)))
    return entries


def _route_identity(values):
    from .template_content import static_route_identity

    return static_route_identity(
        values.get("vrf"), values.get("prefix"), values.get("next_hop"), values.get("interface_next_hop")
    )


def _interface_device(scope, item, management, interfaces):
    from .bfd_reconciler import _profile_values, bfd_flag_values
    from .interface_mtu_reconciler import _validated_interface_items
    from .subinterface_reconciler import subinterface_values
    from .svi_reconciler import svi_values
    from .template_content import resolve_interface_ip_interface
    from .vlan_reconciler import _validated_switchport_items, _validated_vlan_id, switchport_values

    name = item["interface_name"]
    related = [interfaces.get(name)]
    reason = ""
    if scope == "switchport":
        _validated_switchport_items(
            {
                "interfaces": [
                    {
                        "interface_name": name,
                        "mode": item.get("mode") or "",
                        "untagged_vlan": item.get("untagged_vlan"),
                        "tagged_vlans": item.get("tagged_vlans") or [],
                    }
                ]
            }
        )
        mode, untagged, tagged = switchport_values(item)
        normalized = {"mode": mode, "untagged_vlan": untagged, "tagged_vlans": tagged}
        values = _normalized(item, normalized)
        for field_name in ("mode", "tagged_vlans"):
            if observed_value(item, field_name) is None:
                values[field_name] = None
    elif scope == "interface_mtu":
        _validated_interface_items(
            {
                "interfaces": [
                    {
                        "interface_name": name,
                        **{field: item.get(field) for field in ("mtu", "ip_mtu", "mpls_mtu", "bound_port")},
                    }
                ]
            }
        )
        values = _values(item, ("mtu", "ip_mtu", "mpls_mtu", "bound_port"))
        if values["bound_port"] is None:
            values["bound_port"] = ""
    elif scope == "svi":
        _validated_vlan_id(item["vlan_id"], "SVI VLAN ID")
        svi_type, vrf = svi_values(item)
        values = {"vlan_id": item["vlan_id"], **_normalized(item, {"type": svi_type, "vrf": vrf})}
    elif scope == "subinterface":
        _validated_vlan_id(item["dot1q_vlan"], "subinterface VLAN tag")
        parent, tag, vrf = subinterface_values(item)
        values = _normalized(item, {"parent_interface": parent, "dot1q_vlan": tag, "vrf": vrf})
        values["type"] = observed_value(item, "type")
        related.append(interfaces.get(item.get("parent_interface")))
    else:
        interface = resolve_interface_ip_interface(interfaces, name, item.get("bound_port"))
        if interface is not None:
            name = interface.name
            related.append(interface)
        values = {**_values(item, ("min_tx", "min_rx", "multiplier")), **_normalized(item, bfd_flag_values(item))}
        if all(values[key] is not MISSING and values[key] is not None for key in ("min_tx", "min_rx", "multiplier")):
            reason = "invalid BFD profile" if _profile_values(values) is None else ""
    return ProjectedEntry(name, values, tuple(related), reason=reason)


def _lag_device(document, interfaces):
    from .lacp_reconciler import lacp_bundle_values, lacp_member_values

    entries = []
    for bundle in document["bundles"] or []:
        name = bundle["name"]
        entries.append(
            ProjectedEntry(
                ("bundle", name),
                _normalized(bundle, lacp_bundle_values(bundle)),
                (interfaces.get(name),),
            )
        )
        if observed_value(bundle, "member") is MISSING or bundle["member"] is None:
            continue
        for member in bundle["member"]:
            entries.append(
                ProjectedEntry(
                    ("member", member["interface_name"]),
                    {"bundle": name, **_normalized(member, lacp_member_values(member))},
                    (interfaces.get(member["interface_name"]), interfaces.get(name)),
                )
            )
    return entries


def _l2_device(document, interfaces):
    from .adapter_client import AdapterError
    from .l2_service_reconciler import _validated_l2_services, l2_sap_values

    entries = []
    for service in document["services"] or []:
        if service.get("saps") is None or "saps" not in service.get("present", []):
            entries.append(ProjectedEntry((service["service_name"],), {}, reason="service SAP coverage is unavailable"))
            continue
        entries.append(ProjectedEntry((service["service_name"],), {}, coverage_only=True))
        for sap in service["saps"]:
            try:
                _validated_l2_services({"services": [{**service, "saps": [sap]}]})
            except AdapterError as exc:
                entries.append(
                    ProjectedEntry(
                        (service["service_name"], sap["sap_id"]),
                        {},
                        (interfaces.get(sap.get("port")),),
                        reason=str(exc),
                    )
                )
                continue
            service_type, port, outer_tag, inner_tag = l2_sap_values(service, sap)
            values = {
                **_normalized(service, {"service_type": service_type, "service_id": service.get("service_id")}),
                **_normalized(sap, {"port": port, "outer_tag": outer_tag, "inner_tag": inner_tag}),
            }
            entries.append(ProjectedEntry((service["service_name"], sap["sap_id"]), values, (interfaces.get(port),)))
    return entries


def _snmp_device(document):
    from .snmp_versions import canonical_snmp_version
    from .template_content import snmp_community_values, snmp_host_values, snmp_system_values
    from .vault_refs import is_secret_fingerprint

    entries = []
    for item in document["communities"] or []:
        if not is_secret_fingerprint(item["name"]):
            entries.append(ProjectedEntry(("community", ""), {}, reason="invalid community fingerprint"))
            continue
        entries.append(
            ProjectedEntry(
                ("community", item["name"]),
                {**_normalized(item, snmp_community_values(item)), "secret": item["name"]},
            )
        )
    for item in document["users"] or []:
        entries.append(
            ProjectedEntry(
                ("user", item["username"]),
                {"auth_secret": MISSING, "priv_secret": MISSING},
                unavailable={
                    name: "secret fingerprint is unavailable; the device reports presence only"
                    for name in ("auth_secret", "priv_secret")
                },
            )
        )
    for item in document["hosts"] or []:
        normalized = snmp_host_values({**item, "username": item.get("user")})
        normalized["user"] = normalized.pop("username")
        normalized.pop("community_hash")
        values = _normalized(item, normalized)
        if values["version"] is not MISSING and values["version"] is not None:
            values["version"] = canonical_snmp_version(values["version"])
        entries.append(ProjectedEntry(("host", item["address"]), values))
    if document["system"].get("present"):
        system = document["system"]
        entries.append(ProjectedEntry(("system",), _normalized(system, snmp_system_values(system))))
    return entries


def _logging_device(document, ned_id):
    from .template_content import _canonical_logging_field, logging_host_values, logging_level_values

    entries = []
    for item in document["hosts"] or []:
        values = _normalized(item, logging_host_values(item))
        for name, value in values.items():
            if value is not MISSING:
                values[name] = _canonical_logging_field(ned_id, name, value)
        entries.append(ProjectedEntry(("host", item["address"]), values))
    if document["local_levels"] is not None:
        item = document["local_levels"]
        values = _normalized(item, logging_level_values(item))
        for name, value in values.items():
            if value is not MISSING:
                values[name] = _canonical_logging_field(ned_id, "severity", value)
        entries.append(ProjectedEntry(("local_levels",), values))
    return entries


def _route_device(item, management):
    from ipam.models import VRF
    from netbox_routing.models import StaticRoute

    from .template_content import (
        _static_route_metric,
        interface_ip_vrf_candidates_by_name,
        static_route_interface_next_hop,
        static_route_name,
        static_route_permanent,
    )

    values = {
        **_values(item, ("vrf", "prefix", "next_hop", "next_hop_vrf", "metric", "permanent", "tag")),
        **_normalized(
            item,
            {
                "interface_next_hop": static_route_interface_next_hop(item.get("interface_next_hop")),
                "name": static_route_name(item.get("name")),
            },
        ),
    }
    if values["permanent"] is not MISSING:
        values["permanent"] = static_route_permanent(values["permanent"])
    for name in ("prefix", "next_hop"):
        value = values[name]
        if value is not MISSING and value is not None:
            parsed = StaticRoute._meta.get_field(name).to_python(value)
            values[name] = str(parsed) if parsed is not None else None
    metric = _static_route_metric(item, management.device)
    reason = (
        "metric cannot be represented by NetBox" if item.get("metric") is not None and metric != item["metric"] else ""
    )
    if values["metric"] is not MISSING:
        values["metric"] = metric
    name = item.get("vrf") or ""
    vrfs = interface_ip_vrf_candidates_by_name(VRF, [name]).get(name, []) if name else []
    if len(vrfs) > 1:
        reason = "non-unique VRF name"
    identity_values = {key: None if value is MISSING else value for key, value in values.items()}
    identity_values["vrf"] = name
    if not identity_values["next_hop"] and not identity_values["interface_next_hop"]:
        reason = "route next hop is unavailable"
    return ProjectedEntry(_route_identity(identity_values), values, tuple(vrfs), reason=reason)


def device_entries(scope, management, snapshot, *, ned_id="", native_items=()):
    if scope in {"bgp", "isis", "isis_flex_algo", "ospf", "redistribution", "route_policy"}:
        entries = routing_projection(scope).device_entries(
            scope, management, snapshot, ned_id=ned_id, native_items=native_items
        )
        return inherit_parent_dependencies(scope, native_items, entries)
    from django.core.exceptions import ValidationError

    from .adapter_client import AdapterError
    from .vlan_reconciler import _validated_vlan_items

    document = snapshot.document
    interfaces = {row.name: row for row in device_interfaces(management)}
    if scope == "lacp":
        return inherit_parent_dependencies(scope, native_items, _lag_device(document, interfaces))
    if scope == "l2_sap":
        return inherit_parent_dependencies(scope, native_items, _l2_device(document, interfaces))
    if scope == "snmp":
        return _snmp_device(document)
    if scope == "logging":
        return _logging_device(document, ned_id)
    entries = []
    collection = "vlans" if scope == "vlan" else ("routes" if scope == "static_route" else "interfaces")
    for item in document[collection] or []:
        try:
            if scope == "vlan":
                normalized = _validated_vlan_items({"vlans": [item]})[0]
                entries.append(ProjectedEntry(item["vlan_id"], _normalized(item, {"name": normalized["name"]})))
            elif scope == "static_route":
                entries.append(_route_device(item, management))
            else:
                entries.append(_interface_device(scope, item, management, interfaces))
        except (AdapterError, ValueError, ValidationError) as exc:
            entries.append(
                ProjectedEntry(item.get("interface_name", item.get("vlan_id", "device entry")), {}, reason=str(exc))
            )
    return entries
