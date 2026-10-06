# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Derive read-only NetBox differences from the last published device observations."""

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_interface
from typing import Any

from .ownership_planner import _ip_bindings, converted_scope_rules, device_interfaces
from .summary import _netbox_value_for, matches_device_value
from .template_content import interface_ip_vrf_candidates_by_name, resolve_interface_ip_interface

KINDS = ("netbox_only", "device_only", "mismatch", "ambiguous", "unavailable")
NOT_SUPPORTED = "not supported yet"


class _MissingValue:
    def __str__(self):
        return "missing"


MISSING = _MissingValue()


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


def _interface_identity(item):
    return item["name"]


def _interface_blockers(native, device):
    return "multiple interfaces have the same name" if len(native) > 1 or len(device) > 1 else ""


def _interface_rows(spec, management, snapshot):
    attributes = [
        projection
        for projection in spec.attributes
        if projection.name in management.managed_attributes and projection.name in snapshot.coverage["attributes"]
    ]
    native = defaultdict(list)
    device = defaultdict(list)
    rows = []
    for interface in device_interfaces(management):
        if not interface.name:
            rows.append(
                Difference(spec.scope, "ambiguous", f"interface {interface.pk}", reason="missing interface name")
            )
        else:
            native[interface.name].append(interface)
    for item in snapshot.document["interfaces"]:
        device[spec.identity(item)].append(item)
    for name in sorted(native.keys() | device.keys()):
        native_items, device_items = native[name], device[name]
        reason = spec.blockers(native_items, device_items)
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
    interface = resolve_interface_ip_interface(interfaces, entry["interface"], entry["bound_port"])
    return {
        **address,
        "interface": interface.name if interface is not None else entry["interface"],
        "host": str(ip_interface(address["address"]).ip),
    }


def _ip_device_index(spec, snapshot, interfaces):
    from ipam.models import VRF

    device = defaultdict(list)
    names = {address["vrf"] for entry in snapshot.document["interfaces"] for address in entry["addresses"]}
    vrfs = interface_ip_vrf_candidates_by_name(VRF, sorted(name for name in names if name))
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
                rows.append(
                    Difference(
                        spec.scope, "ambiguous", spec.identity(item), device_value=address, reason="non-unique VRF name"
                    )
                )
                blocked.update((item["interface"], item["host"], vrf.pk) for vrf in vrfs[name])
                continue
            vrf_id = vrfs[name][0].pk if vrfs[name] else (name if name else None)
            device[(item["interface"], item["host"], vrf_id)].append(item)
    return device, rows, blocked


def _ip_candidates(hosts):
    from django.db.models import Q
    from ipam.models import IPAddress

    query = Q(pk__in=[])
    for host in hosts:
        query |= Q(address__net_host=host)
    candidates = defaultdict(list)
    for native in IPAddress.objects.filter(query).select_related("vrf").order_by("pk"):
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


def _ip_rows(spec, management, snapshot):
    interfaces = {interface.pk: interface for interface in device_interfaces(management)}
    device, rows, blocked = _ip_device_index(
        spec, snapshot, {interface.name: interface for interface in interfaces.values()}
    )
    native = defaultdict(list)
    for _scope, ip, _model, _key in _ip_bindings(management):
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
    candidates = _ip_candidates({key[1] for key in device})
    device_counts = Counter()
    for key, items in device.items():
        device_counts[(key[1], key[2])] += len(items)
    for key in sorted(native.keys() | device.keys(), key=repr):
        if key not in blocked:
            host_vrf = (key[1], key[2])
            rows.extend(_ip_group_rows(spec, native[key], device[key], candidates[host_vrf], device_counts[host_vrf]))
    return rows


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


def observation_snapshots(management):
    """Read each observation once so rows and displayed metadata use the same revision."""
    from .models import NSOFamilyObservation

    return {
        snapshot.read_state.family: snapshot
        for snapshot in NSOFamilyObservation.objects.filter(read_state__management=management).select_related(
            "read_state"
        )
    }


def differences(management, *, snapshots=None):
    """Return ordered differences using only snapshots and current NetBox rows."""
    if snapshots is None:
        snapshots = observation_snapshots(management)
    rows = []
    for scope in converted_scope_rules():
        spec = SCOPE_SPECS.get(scope)
        if spec is None:
            rows.append(Difference(scope, "unavailable", reason=NOT_SUPPORTED))
            continue
        snapshot = snapshots.get(spec.family)
        if snapshot is None:
            rows.append(Difference(scope, "unavailable", reason="no successful read yet"))
            continue
        rows.extend(spec.rows(spec, management, snapshot))
        rows.extend(
            Difference(scope, "ambiguous", f"device entry {item['index']}", reason=item["reason"])
            for item in snapshot.document["unprojectable"]
        )
    return sorted(rows, key=lambda row: (row.scope, str(row.identity), row.attribute, row.kind, row.reason))
