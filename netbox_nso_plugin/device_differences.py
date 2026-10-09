# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Derive read-only NetBox differences from the last published device observations."""

import contextlib
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_interface
from typing import Any

from .comparison_values import MISSING
from .difference_projection import ROUTING_SCOPES, routing_projection
from .ownership_planner import _ip_bindings, converted_scope_rules, device_interfaces
from .summary import _netbox_value_for, matches_device_value

KINDS = ("netbox_only", "device_only", "mismatch", "ambiguous", "unavailable")
NOT_VISIBLE = "NetBox object is not visible to you"


@dataclass(frozen=True)
class Difference:
    scope: str
    kind: str
    identity: Any = ""
    attribute: str = ""
    netbox_value: Any = MISSING
    device_value: Any = MISSING
    reason: str = ""
    association_candidate: Any = None


@dataclass(frozen=True)
class AttributeProjection:
    name: str
    netbox_value: Callable
    device_value: Callable
    matches: Callable


@dataclass(frozen=True)
class ScopeSpec:
    scope: str
    family: str
    identity: Callable
    attributes: tuple[AttributeProjection, ...]
    blockers: Callable
    rows: Callable
    components: tuple[str, ...] = ()


def _interface_identity(item):
    return item["name"]


def _interface_blockers(native, device):
    return "multiple interfaces have the same name" if len(native) > 1 or len(device) > 1 else ""


def _visible_pks(model, user, objects):
    return set(
        model.objects.restrict(user, "view").filter(pk__in=[obj.pk for obj in objects]).values_list("pk", flat=True)
    )


def _interface_rows(spec, management, snapshot, user):
    from dcim.models import Interface

    attributes = [
        projection
        for projection in spec.attributes
        if projection.name in management.managed_attributes and projection.name in snapshot.coverage["attributes"]
    ]
    native = defaultdict(list)
    device = defaultdict(list)
    hidden = set()
    rows = []
    interfaces = list(device_interfaces(management))
    visible = _visible_pks(Interface, user, interfaces)
    for interface in interfaces:
        if interface.pk not in visible:
            hidden.add(interface.name)
        elif not interface.name:
            rows.append(
                Difference(spec.scope, "ambiguous", f"interface {interface.pk}", reason="missing interface name")
            )
        else:
            native[interface.name].append(interface)
    for item in snapshot.document["interfaces"]:
        device[spec.identity(item)].append(item)
    for name in sorted(native.keys() | device.keys()):
        native_items, device_items = native[name], device[name]
        reason = NOT_VISIBLE if name in hidden else spec.blockers(native_items, device_items)
        if reason:
            rows.append(Difference(spec.scope, "ambiguous", name, reason=reason))
        elif not native_items:
            rows.append(Difference(spec.scope, "device_only", name, device_value=device_items[0]))
        elif not device_items:
            values = {projection.name: projection.netbox_value(native_items[0]) for projection in attributes}
            rows.append(Difference(spec.scope, "netbox_only", name, netbox_value=values))
        else:
            for projection in attributes:
                netbox_value = projection.netbox_value(native_items[0])
                device_value = projection.device_value(device_items[0])
                if device_value is MISSING or not projection.matches(netbox_value, device_value):
                    rows.append(Difference(spec.scope, "mismatch", name, projection.name, netbox_value, device_value))
    return rows


def _ip_identity(item):
    return item["interface"], item["host"], item["prefix_length"], item["vrf"]


def identity_label(row):
    """Return the operator-facing text for a row identity."""
    if row.scope != "ip" or not isinstance(row.identity, tuple):
        return str(row.identity)
    interface, host, prefix_length, vrf = row.identity
    address = host if prefix_length is None else f"{host}/{prefix_length}"
    return f"{interface} {address}" + (f" (VRF {vrf})" if vrf else "")


def _ip_blockers(native, device, candidates, device_count):
    if len(native) > 1 or len(device) > 1 or len(candidates) > 1 or device_count > 1:
        return "multiple addresses have the same host and VRF"
    return ""


def _native_ip_projection(native, interfaces):
    address = ip_interface(str(native.address))
    return {
        "interface": interfaces[native.assigned_object_id].name,
        "host": str(address.ip),
        "prefix_length": address.network.prefixlen,
        "vrf": native.vrf.name if native.vrf else None,
    }


def _device_ip_projection(entry, address, interfaces):
    from .template_content import resolve_interface_ip_interface

    interface = resolve_interface_ip_interface(interfaces, entry["interface"], entry["bound_port"])
    return {
        **address,
        "interface": interface.name if interface is not None else entry["interface"],
        "host": str(ip_interface(address["address"]).ip),
    }


def _without_vrf(observed):
    """Return the observed address without the name of a VRF the user cannot view."""
    return {**observed, "vrf": None}


def _ip_device_index(spec, snapshot, interfaces, user):
    from ipam.models import VRF

    from .template_content import interface_ip_vrf_candidates_by_name

    device = defaultdict(list)
    names = {address["vrf"] for entry in snapshot.document["interfaces"] for address in entry["addresses"]}
    # Match names over every VRF so keys stay true; rows that depend on a hidden VRF are redacted.
    vrfs = interface_ip_vrf_candidates_by_name(VRF, sorted(name for name in names if name))
    every_vrf = [vrf for candidates in vrfs.values() for vrf in candidates]
    hidden_vrfs = {vrf.pk for vrf in every_vrf} - _visible_pks(VRF, user, every_vrf)
    vrfs[None] = vrfs[""] = []
    rows = []
    blocked = set()
    for entry in snapshot.document["interfaces"]:
        for address in entry["addresses"]:
            try:
                item = _device_ip_projection(entry, address, interfaces)
            except ValueError:
                rows.append(
                    Difference(
                        spec.scope, "ambiguous", entry["interface"], device_value=address, reason="invalid address"
                    )
                )
                continue
            name = address["vrf"]
            if len(vrfs[name]) > 1:
                if any(vrf.pk in hidden_vrfs for vrf in vrfs[name]):
                    rows.append(
                        Difference(
                            spec.scope,
                            "ambiguous",
                            spec.identity(_without_vrf(item)),
                            device_value=_without_vrf(address),
                            reason=NOT_VISIBLE,
                        )
                    )
                else:
                    rows.append(
                        Difference(
                            spec.scope,
                            "ambiguous",
                            spec.identity(item),
                            device_value=address,
                            reason="non-unique VRF name",
                        )
                    )
                blocked.update((item["interface"], item["host"], vrf.pk) for vrf in vrfs[name])
                continue
            vrf_id = vrfs[name][0].pk if vrfs[name] else (name if name else None)
            device[(item["interface"], item["host"], vrf_id)].append(item)
    return device, rows, blocked, hidden_vrfs


def _ip_candidates(hosts, user):
    from django.db.models import Q
    from ipam.models import IPAddress

    query = Q(pk__in=[])
    for host in hosts:
        query |= Q(address__net_host=host)
    candidates = defaultdict(list)
    for native in IPAddress.objects.restrict(user, "view").filter(query).select_related("vrf").order_by("pk"):
        candidates[(str(ip_interface(str(native.address)).ip), native.vrf_id)].append(native)
    return candidates


def _ip_group_rows(spec, native, device, candidates, device_count):
    rows = []
    reason = spec.blockers(native, device, candidates, device_count)
    identity = spec.identity(device[0] if device else native[0])
    if reason:
        return [Difference(spec.scope, "ambiguous", identity, reason=reason)]
    if not device:
        return [Difference(spec.scope, "netbox_only", identity, netbox_value=native[0])]
    if not native:
        candidate = candidates[0] if candidates else None
        return [
            Difference(spec.scope, "device_only", identity, device_value=device[0], association_candidate=candidate)
        ]
    for projection in spec.attributes:
        netbox_value = projection.netbox_value(native[0])
        device_value = projection.device_value(device[0])
        if not projection.matches(netbox_value, device_value):
            rows.append(Difference(spec.scope, "mismatch", identity, projection.name, netbox_value, device_value))
    return rows


def _ip_rows(spec, management, snapshot, user):
    from dcim.models import Interface
    from ipam.models import VRF, IPAddress

    interfaces = {interface.pk: interface for interface in device_interfaces(management)}
    visible_interfaces = _visible_pks(Interface, user, interfaces.values())
    device, rows, blocked, hidden_vrfs = _ip_device_index(
        spec, snapshot, {interface.name: interface for interface in interfaces.values()}, user
    )
    native = defaultdict(list)
    hidden = set()
    bindings = [ip for _scope, ip, _model, _key in _ip_bindings(management)]
    visible = _visible_pks(IPAddress, user, bindings)
    native_vrfs = [ip.vrf for ip in bindings if ip.vrf_id is not None]
    hidden_vrfs |= {vrf.pk for vrf in native_vrfs} - _visible_pks(VRF, user, native_vrfs)
    for ip in bindings:
        if ip.pk not in visible or ip.vrf_id in hidden_vrfs or ip.assigned_object_id not in visible_interfaces:
            with contextlib.suppress(KeyError, ValueError):
                item = _native_ip_projection(ip, interfaces)
                hidden.add((item["interface"], item["host"], ip.vrf_id))
            continue
        interface = interfaces.get(ip.assigned_object_id)
        if interface is None or not interface.name:
            rows.append(Difference(spec.scope, "ambiguous", str(ip.address), reason="native interface is unavailable"))
            continue
        try:
            item = _native_ip_projection(ip, interfaces)
        except ValueError:
            rows.append(Difference(spec.scope, "ambiguous", str(ip.address), reason="invalid native address"))
            continue
        native[(item["interface"], item["host"], ip.vrf_id)].append(item)
    candidates = _ip_candidates({key[1] for key in device}, user)
    device_counts = Counter()
    for key, items in device.items():
        device_counts[(key[1], key[2])] += len(items)
    for key in sorted(native.keys() | device.keys(), key=repr):
        if (key in hidden or key[2] in hidden_vrfs) and not native[key]:
            item = (device[key] or native[key])[0]
            item = _without_vrf(item) if key[2] in hidden_vrfs else item
            rows.append(Difference(spec.scope, "ambiguous", spec.identity(item), reason=NOT_VISIBLE))
        elif key not in blocked:
            host_vrf = (key[1], key[2])
            rows.extend(_ip_group_rows(spec, native[key], device[key], candidates[host_vrf], device_counts[host_vrf]))
    return rows


def _projected_identity(item):
    return item.identity


def _scope_blockers(native, device):
    if len(native) > 1 or len(device) > 1:
        return "multiple entries have the same identity"
    return next((item.reason for item in (*native, *device) if item.reason), "")


def _entry_visibility(entries, user):
    grouped = defaultdict(dict)
    for item in entries:
        for obj in item.objects:
            if obj is not None:
                grouped[type(obj)][obj.pk] = obj
    visible = {model: _visible_pks(model, user, objects.values()) for model, objects in grouped.items()}
    return {id(item): all(obj is None or obj.pk in visible[type(obj)] for obj in item.objects) for item in entries}


def _entry_component(spec, item):
    if spec.scope in ROUTING_SCOPES:
        return routing_projection(spec.scope).component(spec.scope, item)
    if len(spec.components) == 1:
        return spec.components[0]
    return {
        "community": "communities",
        "user": "users",
        "host": "hosts",
        "system": "system",
        "local_levels": "local_levels",
    }[item.identity[0]]


def _scope_component_gaps(spec, snapshot):
    if spec.scope == "redistribution":
        from .routing_protocol_projection import component_gaps

        return component_gaps(snapshot)
    gaps = {}
    for name in spec.components:
        if name != "system" and name not in snapshot.document["present"]:
            gaps[name] = "device component was not observed"
        elif snapshot.document[name] is None:
            gaps[name] = "device component is null"
    return gaps


def _coverage_reason(scope, attribute, coverage):
    aliases = {"secret": "name", "bundle": "member", "port_priority": "member.port_priority"}
    covered = "member.mode" if scope == "lacp" and attribute == "mode" else aliases.get(attribute, attribute)
    if attribute in coverage.get("not_comparable", []) or covered in coverage.get("not_comparable", []):
        return "attribute is not comparable in device coverage"
    if attribute not in coverage["attributes"] and covered not in coverage["attributes"]:
        return "attribute is not covered by the device observation"
    return ""


def _projected_matches(spec, projection, native, device, ned_id):
    if spec.scope == "isis":
        from .template_content import _ISIS_PROCESS_FLAG_DEFAULTS, _isis_process_flag_matches

        if projection.name in _ISIS_PROCESS_FLAG_DEFAULTS:
            return _isis_process_flag_matches(device, native)
    if projection.name == "port" and spec.scope in {"logging", "snmp"}:
        from .template_content import logging_host_field_matches, snmp_host_field_matches

        matches = logging_host_field_matches if spec.scope == "logging" else snmp_host_field_matches
        return matches(ned_id, "port", native, device, omitted=device is MISSING)
    return projection.matches(native, device)


def _projected_group_rows(spec, native, device, snapshot, ned_id):
    identity = spec.identity((device or native)[0])
    reason = spec.blockers(native, device)
    if reason:
        return [Difference(spec.scope, "ambiguous", identity, reason=reason)]
    if not native or not device:
        item = (device or native)[0]
        row = (
            Difference(spec.scope, "device_only", identity, device_value=item.values)
            if device
            else Difference(spec.scope, "netbox_only", identity, netbox_value=item.values)
        )
        return [
            row,
            *(
                Difference(spec.scope, "unavailable", identity, name, reason=reason)
                for name, reason in item.unavailable.items()
            ),
        ]
    rows = []
    for projection in spec.attributes:
        name = projection.name
        if not any(name in item.values or name in item.unavailable for item in (native[0], device[0])):
            continue
        reason = native[0].unavailable.get(name) or device[0].unavailable.get(name)
        reason = reason or _coverage_reason(spec.scope, name, snapshot.coverage)
        netbox_value, device_value = projection.netbox_value(native[0]), projection.device_value(device[0])
        if not reason and netbox_value is MISSING:
            reason = "NetBox has no value for this attribute"
        if reason:
            rows.append(Difference(spec.scope, "unavailable", identity, name, reason=reason))
        elif not _projected_matches(spec, projection, netbox_value, device_value, ned_id):
            rows.append(Difference(spec.scope, "mismatch", identity, name, netbox_value, device_value))
    return rows


def _nested_component_gaps(spec, snapshot):
    if spec.scope in ROUTING_SCOPES:
        return routing_projection(spec.scope).nested_gaps(spec.scope, snapshot)
    if spec.scope == "l2_sap":
        return {
            (item["service_name"], "saps"): "service SAP coverage is unavailable"
            for item in snapshot.document["services"] or []
            if item.get("saps") is None or "saps" not in item.get("present", [])
        }
    if spec.scope == "lacp":
        return {
            (item["name"], "member"): "LAG member coverage is unavailable"
            for item in snapshot.document["bundles"] or []
            if item.get("member") is None or "member" not in item.get("present", [])
        }
    return {}


def _nested_coverage_rows(spec, gaps, entries, visible):
    if spec.scope in ROUTING_SCOPES:
        return _routing_coverage_rows(spec, gaps, entries, visible)
    hidden = set()
    for item in entries:
        if visible[id(item)]:
            continue
        if spec.scope == "l2_sap":
            hidden.add(item.identity[0])
        elif spec.scope == "lacp":
            hidden.add(item.identity[1] if item.identity[0] == "bundle" else item.values.get("bundle"))
    return [
        Difference(spec.scope, "ambiguous", reason=NOT_VISIBLE)
        if name in hidden
        else Difference(spec.scope, "unavailable", name, attribute, reason=reason)
        for (name, attribute), reason in sorted(gaps.items())
    ]


def _routing_coverage_rows(spec, gaps, entries, visible):
    rows = []
    for (prefix, attribute), reason in sorted(gaps.items(), key=repr):
        related = [
            item
            for item in entries
            if isinstance(item.identity, tuple)
            and (item.identity[: len(prefix)] == prefix or prefix[: len(item.identity)] == item.identity)
        ]
        if any(not visible[id(item)] for item in related):
            rows.append(Difference(spec.scope, "ambiguous", reason=NOT_VISIBLE))
        else:
            rows.append(Difference(spec.scope, "unavailable", prefix, attribute or str(prefix[-1]), reason=reason))
    return rows


def _nested_entry_blocked(spec, item, gaps):
    if spec.scope in ROUTING_SCOPES:
        return any(
            isinstance(item.identity, tuple) and item.identity[: len(prefix)] == prefix for prefix, _attribute in gaps
        )
    if spec.scope == "l2_sap":
        return (item.identity[0], "saps") in gaps
    if spec.scope == "lacp" and item.identity[0] == "member":
        return (item.values.get("bundle"), "member") in gaps
    return False


def _scope_rows(spec, management, snapshot, user):
    from .difference_projection import device_entries, native_entries

    native = defaultdict(list)
    device = defaultdict(list)
    rows = []
    gaps = _scope_component_gaps(spec, snapshot)
    rows.extend(Difference(spec.scope, "unavailable", attribute=name, reason=reason) for name, reason in gaps.items())
    if len(gaps) == len(spec.components):
        return rows
    ned_id = ""
    if spec.scope in {"logging", "snmp"}:
        from .template_content import _device_ned_id

        ned_id = _device_ned_id(management.device)
    nested_gaps = _nested_component_gaps(spec, snapshot)
    try:
        native_items = native_entries(spec.scope, management, ned_id=ned_id)
    except ModuleNotFoundError as exc:
        if exc.name not in {"netbox_routing", "netbox_routing.models"}:
            raise
        return [Difference(spec.scope, "unavailable", reason="NetBox routing models are unavailable")]
    device_items = device_entries(spec.scope, management, snapshot, ned_id=ned_id, native_items=native_items)
    visible = _entry_visibility((*native_items, *device_items), user)
    rows.extend(_nested_coverage_rows(spec, nested_gaps, (*native_items, *device_items), visible))
    hidden = set()
    for items, index in ((native_items, native), (device_items, device)):
        for item in items:
            if item.coverage_only:
                continue
            if _entry_component(spec, item) in gaps or _nested_entry_blocked(spec, item, nested_gaps):
                continue
            key = spec.identity(item)
            if not visible[id(item)]:
                hidden.add(key)
            elif key in (None, ""):
                rows.append(Difference(spec.scope, "ambiguous", reason="missing native identity"))
            else:
                index[key].append(item)
    for key in sorted(native.keys() | device.keys() | hidden, key=repr):
        if key in hidden:
            if device[key] or native[key] or any(item.identity == key for item in device_items):
                rows.append(Difference(spec.scope, "ambiguous", reason=NOT_VISIBLE))
        else:
            rows.extend(_projected_group_rows(spec, native[key], device[key], snapshot, ned_id))
    return rows


def _same_value(native, device):
    return type(native) is type(device) and native == device


def _projected_attributes(*names):
    return tuple(
        AttributeProjection(
            name,
            lambda item, name=name: item.values.get(name, MISSING),
            lambda item, name=name: item.values.get(name, MISSING),
            _same_value,
        )
        for name in names
    )


def _description_projection():
    return AttributeProjection(
        "description",
        lambda interface: _netbox_value_for("description", interface),
        lambda item: item.get("description", MISSING),
        lambda native, device: matches_device_value("description", native, device),
    )


SCOPE_SPECS = {
    "interface": ScopeSpec(
        "interface",
        "interface_attributes",
        _interface_identity,
        (
            _description_projection(),
            AttributeProjection(
                "enabled",
                lambda interface: _netbox_value_for("enabled", interface),
                lambda item: item.get("enabled", MISSING),
                lambda native, device: matches_device_value("enabled", native, device),
            ),
        ),
        _interface_blockers,
        _interface_rows,
    ),
    "ip": ScopeSpec(
        "ip",
        "interface_ip",
        _ip_identity,
        (
            AttributeProjection(
                "prefix_length",
                lambda item: item["prefix_length"],
                lambda item: item["prefix_length"],
                lambda native, device: native == device,
            ),
        ),
        _ip_blockers,
        _ip_rows,
    ),
}


SCOPE_SPECS.update(
    {
        "lacp": ScopeSpec(
            "lacp",
            "lag_config",
            _projected_identity,
            _projected_attributes(
                "lag_id",
                "min_links",
                "system_priority",
                "system_id",
                "timer",
                "admin_key",
                "vpc_sensitive",
                "bundle",
                "mode",
                "port_priority",
            ),
            _scope_blockers,
            _scope_rows,
            ("bundles",),
        ),
        "vlan": ScopeSpec(
            "vlan", "vlan", _projected_identity, _projected_attributes("name"), _scope_blockers, _scope_rows, ("vlans",)
        ),
        "switchport": ScopeSpec(
            "switchport",
            "switchport",
            _projected_identity,
            _projected_attributes("mode", "untagged_vlan", "tagged_vlans"),
            _scope_blockers,
            _scope_rows,
            ("interfaces",),
        ),
        "interface_mtu": ScopeSpec(
            "interface_mtu",
            "interface_mtu",
            _projected_identity,
            _projected_attributes("mtu", "ip_mtu", "mpls_mtu", "bound_port"),
            _scope_blockers,
            _scope_rows,
            ("interfaces",),
        ),
        "svi": ScopeSpec(
            "svi",
            "svi",
            _projected_identity,
            _projected_attributes("vlan_id", "type", "vrf"),
            _scope_blockers,
            _scope_rows,
            ("interfaces",),
        ),
        "subinterface": ScopeSpec(
            "subinterface",
            "subinterface",
            _projected_identity,
            _projected_attributes("parent_interface", "dot1q_vlan", "type", "vrf"),
            _scope_blockers,
            _scope_rows,
            ("interfaces",),
        ),
        "bfd": ScopeSpec(
            "bfd",
            "bfd",
            _projected_identity,
            _projected_attributes("min_tx", "min_rx", "multiplier", "enabled", "micro_bfd"),
            _scope_blockers,
            _scope_rows,
            ("interfaces",),
        ),
        "l2_sap": ScopeSpec(
            "l2_sap",
            "l2_service",
            _projected_identity,
            _projected_attributes("service_type", "service_id", "port", "outer_tag", "inner_tag"),
            _scope_blockers,
            _scope_rows,
            ("services",),
        ),
        "logging": ScopeSpec(
            "logging",
            "logging",
            _projected_identity,
            _projected_attributes(
                "port",
                "severity",
                "facility",
                "transport",
                "vrf",
                "source",
                "console_severity",
                "monitor_severity",
                "module_severity",
            ),
            _scope_blockers,
            _scope_rows,
            ("hosts", "local_levels"),
        ),
        "snmp": ScopeSpec(
            "snmp",
            "snmp",
            _projected_identity,
            _projected_attributes(
                "access",
                "acl",
                "secret",
                "auth_secret",
                "priv_secret",
                "version",
                "notify_type",
                "port",
                "user",
                "location",
                "contact",
            ),
            _scope_blockers,
            _scope_rows,
            ("communities", "users", "hosts", "system"),
        ),
        "static_route": ScopeSpec(
            "static_route",
            "static_route",
            _projected_identity,
            _projected_attributes("interface_next_hop", "next_hop_vrf", "metric", "permanent", "tag", "name"),
            _scope_blockers,
            _scope_rows,
            ("routes",),
        ),
    }
)


def _routing_scope_specs():
    return {
        scope: ScopeSpec(
            scope,
            family,
            _projected_identity,
            _projected_attributes(*routing_projection(scope).ATTRIBUTES[scope]),
            _scope_blockers,
            _scope_rows,
            components,
        )
        for scope, family, components in (
            ("bgp", "bgp", ("routers",)),
            ("isis", "isis", ("processes", "interfaces")),
            ("isis_flex_algo", "isis", ("processes",)),
            ("ospf", "ospf", ("instances", "interfaces")),
            ("redistribution", "redistribution", ("bgp", "isis", "ospf")),
            ("route_policy", "route_policy", ("prefix_lists", "community_lists", "as_paths", "route_maps")),
        )
    }


SCOPE_SPECS.update(_routing_scope_specs())


def observation_snapshots(management):
    """Read each observation once so rows and displayed metadata use the same revision."""
    from .models import NSOFamilyObservation

    return {
        snapshot.read_state.family: snapshot
        for snapshot in NSOFamilyObservation.objects.filter(read_state__management=management).select_related(
            "read_state"
        )
    }


def differences(management, *, user, snapshots=None):
    """Return ordered differences using only snapshots and the NetBox rows *user* can view."""
    if snapshots is None:
        snapshots = observation_snapshots(management)
    rows = []
    for scope in converted_scope_rules():
        spec = SCOPE_SPECS[scope]
        snapshot = snapshots.get(spec.family)
        if snapshot is None:
            rows.append(Difference(scope, "unavailable", reason="no successful read yet"))
            continue
        rows.extend(spec.rows(spec, management, snapshot, user))
        rows.extend(
            Difference(scope, "ambiguous", f"device entry {item['index']}", reason=item["reason"])
            for item in snapshot.document["unprojectable"]
        )
    return sorted(rows, key=lambda row: (row.scope, str(row.identity), row.attribute, row.kind, row.reason))
