# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and execute Sync from NSO: the device wins, only NetBox changes, only unowned rows."""

import copy
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from ipaddress import ip_interface
from typing import Any

from . import status_machine as sm
from .device_differences import (
    NOT_VISIBLE,
    SCOPE_SPECS,
    _visible_pks,
    differences,
    identity_label,
    observation_snapshots,
)
from .ownership_planner import _ip_bindings, converted_scope_rules, device_interfaces

SYNC_SCOPES = ("ip", "interface")
NOT_SUPPORTED = "sync not supported yet"
OWNED = "owned: Release first"
UNPROVEN_ABSENCE = "the device observation has entries that Sync cannot compare, so absence is not proven"
PROTECTED = "NetBox protects it with dependent objects"
HIDDEN_TARGET = "(hidden)"
HIDDEN_SOURCE = "the address is on an interface that is not visible to you"
SYNCABLE_KINDS = ("mismatch", "device_only", "netbox_only")
_TOKEN = re.compile(r"^([a-z_]+):[0-9a-f]{16}$")
_PRIMARY_FIELDS = (("primary_ip4", "primary IPv4"), ("primary_ip6", "primary IPv6"), ("oob_ip", "OOB IP"))
_DELETE_DEPENDENTS = {
    "ipam.ipaddress": "IP addresses",
    "dcim.cabletermination": "a cable",
    "dcim.interface": "dependent interfaces (LAG members, child or bridge interfaces)",
}


class SyncSelectionError(ValueError):
    """The posted selection is malformed."""


class SyncReadFailed(Exception):
    """The fresh scoped read did not publish every selected family."""


class SyncStale(Exception):
    """The plan changed after the preview, so nothing was written."""


class SyncRowFailed(Exception):
    """One planned row failed at execution, so the whole plan rolled back."""

    def __init__(self, label, reason):
        super().__init__(f"{label}: {reason}")
        self.label = label
        self.reason = reason


def row_token(row):
    """Return the stable selection token of one difference row."""
    payload = json.dumps([row.scope, row.kind, row.identity, row.attribute], default=str)
    return f"{row.scope}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


@dataclass(frozen=True)
class SyncSelection:
    """The rows and whole scopes that the operator selected, plus primary-IP choices."""

    rows: frozenset = frozenset()
    scopes: frozenset = frozenset()
    primary: tuple = ()

    @classmethod
    def from_post(cls, data):
        known = set(converted_scope_rules())
        rows = frozenset(data.getlist("row"))
        scopes = frozenset(data.getlist("scope"))
        primary = {}
        for name, value in data.items():
            if name.startswith("primary-") and value:
                primary[name.removeprefix("primary-")] = value
        for token in rows | set(primary):
            match = _TOKEN.match(token)
            if match is None or match.group(1) not in known:
                raise SyncSelectionError(f"Unknown row token {token!r}.")
        if scopes - known:
            raise SyncSelectionError(f"Unknown scope {sorted(scopes - known)[0]!r}.")
        if any(value != "clear" and not value.isdigit() for value in primary.values()):
            raise SyncSelectionError("A primary-IP choice must be 'clear' or an IP address id.")
        return cls(rows, scopes, tuple(sorted(primary.items())))

    @property
    def read_scopes(self):
        return frozenset(token.split(":", 1)[0] for token in self.rows) | self.scopes

    def selects(self, row, token):
        return row.scope in self.scopes or token in self.rows

    def fields(self):
        """Return the (name, value) form fields that repeat this selection."""
        return [
            *(("scope", scope) for scope in sorted(self.scopes)),
            *(("row", token) for token in sorted(self.rows)),
            *((f"primary-{token}", value) for token, value in self.primary),
        ]


@dataclass(frozen=True)
class SyncStep:
    scope: str
    action: str
    target: str
    detail: str
    facts: Any = ()


@dataclass(frozen=True)
class SyncNote:
    scope: str
    target: str
    reason: str


@dataclass
class _Operation:
    kind: str
    instance: Any
    label: str
    update_fields: tuple | None = None
    create: bool = False
    permission: str | None = None
    natural_key: tuple = ()
    expected_before: Any = None


@dataclass
class SyncPlan:
    """One previewed plan: its steps, blockers, notes, digest, and exact operations."""

    steps: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    primary_choices: list = field(default_factory=list)
    operations: list = field(default_factory=list)
    scopes: tuple = ()
    digest: str = ""


def _jsonable(value):
    if hasattr(value, "_meta") and hasattr(value, "pk"):
        return f"{value._meta.label_lower}#{value.pk}"
    return str(value)


def _projection(instance, names):
    return {name: _jsonable(getattr(instance, name)) for name in ("pk", *names)}


def _prepared(address):
    from ipam.models import IPAddress

    return IPAddress._meta.get_field("address").get_prep_value(address)


def _host(address):
    return str(ip_interface(str(address)).ip)


def _value_text(value):
    return json.dumps(value) if isinstance(value, str) else str(value)


def _candidate(instance):
    """Copy a native row and record its change-log snapshot before any field changes."""
    candidate = copy.copy(instance)
    candidate.snapshot()
    return candidate


def validation_failure(candidate):
    """Return why NetBox refuses *candidate*, naming only its own values and failing fields."""
    from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

    try:
        candidate.clean_fields()
    except ValidationError as exc:
        return "; ".join(exc.messages)
    try:
        candidate.full_clean()
    except ValidationError as exc:
        # Model, uniqueness and constraint checks can quote other objects, so name only the fields.
        fields = sorted(name for name in exc.message_dict if name != NON_FIELD_ERRORS) or ["the row"]
        return f"NetBox validation failed on: {', '.join(fields)}"
    return ""


class _Planner:
    def __init__(self, management, user, selection):
        from dcim.models import Device

        self.management = management
        self.user = user
        self.selection = selection
        self.plan = SyncPlan()
        self.device = Device.objects.restrict(user, "view").get(pk=management.device_id)
        self.device_after = None
        self.unproven = set()
        self.device_fields = {}
        self.device_facts = []
        self.ip_interfaces = set()
        self.overlay_targets = []
        self.saves = []
        self.creates = []
        self.deletes = []
        self.interface_saves = []
        self.interface_deletes = []
        self.status_saves = []

    def block(self, scope, target, reason):
        # Sync needs only change permission, so a hidden row never shows its name.
        self.plan.blockers.append(SyncNote(scope, HIDDEN_TARGET if reason == NOT_VISIBLE else target, reason))

    def note(self, scope, target, reason):
        self.plan.notes.append(SyncNote(scope, target, reason))

    def permitted(self, instance, action):
        return type(instance).objects.restrict(self.user, action).filter(pk=instance.pk).exists()

    def step(self, scope, action, target, detail, facts):
        self.plan.steps.append(SyncStep(scope, action, target, detail, facts))

    def device_candidate(self):
        if self.device_after is None:
            self.device_after = _candidate(self.device)
        return self.device_after


def _ip_overlay_facts(keys):
    from .models import NSOInterfaceIPState

    rows = []
    for interface_id, address, vrf in keys:
        rows.extend(
            row
            for row in NSOInterfaceIPState.objects.filter(interface_id=interface_id, vrf=vrf or "").order_by("pk")
            if _prepared(row.address) == _prepared(address)
        )
    return rows


def _ip_ownership(native, target):
    """Return (owned, facts) for the native IP binding and the target overlay key."""
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType

    from .models import NSOOwnershipManifest

    keys = [target] if target is not None else []
    interface_type = ContentType.objects.get_for_model(Interface)
    if native is not None and native.assigned_object_type_id == interface_type.pk and native.assigned_object_id:
        keys.append((native.assigned_object_id, str(native.address), native.vrf.name if native.vrf else ""))
    overlays = _ip_overlay_facts(keys)
    manifests = (
        list(
            NSOOwnershipManifest.objects.filter(
                native_model_label="ipam.ipaddress", native_id=native.pk, ownership_state="owned"
            ).values_list("pk", flat=True)
        )
        if native is not None
        else []
    )
    owned = bool(manifests) or any(sm.is_owned(row.status) for row in overlays)
    return owned, {"overlays": sorted((row.pk, row.status) for row in overlays), "manifests": sorted(manifests)}


class _IPContext:
    def __init__(self, planner):
        from dcim.models import Interface
        from ipam.models import VRF, IPAddress

        self.interfaces = {interface.pk: interface for interface in device_interfaces(planner.management)}
        self.by_name = {interface.name: interface for interface in self.interfaces.values()}
        self.visible_interfaces = _visible_pks(Interface, planner.user, self.interfaces.values())
        bindings = [ip for _scope, ip, _model, _key in _ip_bindings(planner.management)]
        visible = _visible_pks(IPAddress, planner.user, bindings)
        vrfs = [ip.vrf for ip in bindings if ip.vrf_id is not None]
        visible_vrfs = _visible_pks(VRF, planner.user, vrfs)
        self.natives = defaultdict(list)
        self.device_ips = []
        for ip in bindings:
            if ip.pk not in visible or (ip.vrf_id is not None and ip.vrf_id not in visible_vrfs):
                continue
            self.device_ips.append(ip)
            interface = self.interfaces[ip.assigned_object_id]
            self.natives[(interface.name, _host(ip.address), ip.vrf.name if ip.vrf else None)].append(ip)

    def native(self, identity):
        interface, host, _prefix_length, vrf = identity
        matches = self.natives[(interface, host, vrf)]
        return matches[0] if len(matches) == 1 else None


_IP_MOVE_FIELDS = ("address", "assigned_object_type", "assigned_object_id")


def _ip_label(ip):
    return f"{ip.address} (IP #{ip.pk})"


def _plan_ip_change(planner, label, before, after, *, action, detail, target):
    owned, facts = _ip_ownership(before, target)
    if owned:
        planner.block("ip", label, OWNED)
        return
    if not planner.permitted(before, "change"):
        planner.block("ip", label, "permission denied: you cannot change this IP address")
        return
    if error := validation_failure(after):
        planner.block("ip", label, error)
        return
    names = ("address", "vrf_id", "assigned_object_type_id", "assigned_object_id", "status")
    planner.step(
        "ip",
        action,
        _ip_label(before),
        detail,
        {"before": _projection(before, names), "after": _projection(after, names), **facts},
    )
    fields = ("address",) if before.assigned_object_id == after.assigned_object_id else _IP_MOVE_FIELDS
    planner.saves.append(
        _Operation("save", after, _ip_label(before), update_fields=fields, permission="change", expected_before=before)
    )
    planner.overlay_targets.append(target)
    planner.ip_interfaces.update({target[0], before.assigned_object_id})


def _plan_ip_device_only(planner, context, row, label, moved_from):
    from dcim.models import Interface
    from ipam.models import VRF, IPAddress

    from .template_content import interface_ip_vrf_candidates_by_name

    item = row.device_value
    interface = context.by_name.get(item["interface"])
    if interface is None:
        planner.block("ip", label, f"interface {item['interface']} is not in NetBox")
        return
    if interface.pk not in context.visible_interfaces:
        planner.block("ip", label, NOT_VISIBLE)
        return
    if type(item["prefix_length"]) is not int:
        planner.block("ip", label, "the device reports no prefix length")
        return
    vrf = None
    if item["vrf"]:
        vrfs = interface_ip_vrf_candidates_by_name(VRF, [item["vrf"]])[item["vrf"]]
        if len(vrfs) != 1:
            planner.block("ip", label, f"VRF {item['vrf']} is not one NetBox VRF")
            return
        if not _visible_pks(VRF, planner.user, vrfs):
            planner.block("ip", label, NOT_VISIBLE)
            return
        vrf = vrfs[0]
    address = str(ip_interface(f"{item['host']}/{item['prefix_length']}"))
    target = (interface.pk, address, vrf.name if vrf else "")
    candidate = row.association_candidate
    if candidate is None:
        native = IPAddress(address=address, vrf=vrf, status="active")
        native.assigned_object = interface
        owned, facts = _ip_ownership(None, target)
        if owned:
            planner.block("ip", label, OWNED)
        elif not planner.user.has_perm("ipam.add_ipaddress"):
            planner.block("ip", label, "permission denied: you cannot add IP addresses")
        elif error := validation_failure(native):
            planner.block("ip", label, error)
        else:
            planner.step(
                "ip",
                "create",
                address,
                f"on {interface.name}",
                {"target": list(target), "vrf_id": vrf.pk if vrf else None, **facts},
            )
            planner.creates.append(
                _Operation("save", native, address, create=True, permission="add", natural_key=("address", "vrf"))
            )
            planner.overlay_targets.append(target)
            planner.ip_interfaces.add(interface.pk)
        return
    assigned = candidate.assigned_object
    if assigned is not None and not (
        isinstance(assigned, Interface) and assigned.device_id == planner.management.device_id
    ):
        planner.block("ip", label, "the address belongs to another object; Sync never changes it for one device")
        return
    if assigned is not None and assigned.pk not in context.visible_interfaces:
        planner.block("ip", label, HIDDEN_SOURCE)
        return
    after = _candidate(candidate)
    after.address = address
    after.assigned_object = interface
    parts = [part for part, changed in (("modify", str(candidate.address) != address), ("move", True)) if changed]
    source = assigned.name if assigned is not None else "no interface"
    detail = f"{candidate.address} on {source} -> {address} on {interface.name}"
    moved_from[candidate.pk] = label
    _plan_ip_change(planner, label, candidate, after, action=" and ".join(parts), detail=detail, target=target)


def _delete_closure(planner, scope, label, instance, allowed):
    """Return the exact delete closure, or block a delete that NetBox protects or that changes other rows."""
    from django.apps import apps
    from django.db.models import ProtectedError, RestrictedError

    from .intent_state import OVERLAY_MODEL_RANKS, IntentMutationProtocolError
    from .renderer_writer import RendererMutationPlan, planned_delete

    try:
        plan = RendererMutationPlan.build(deletes=(planned_delete(instance),))
    except (IntentMutationProtocolError, ProtectedError, RestrictedError):
        planner.block(scope, label, PROTECTED)
        return None
    root = (instance._meta.label_lower, instance.pk)
    dependents = set()
    owned = False
    for write in plan.write_set:
        if (write.model_label, write.pk) == root and not write.cascade:
            continue
        if write.model_label in OVERLAY_MODEL_RANKS and write.operation == "delete":
            owned = owned or sm.is_owned(dict(write.before_values).get("status"))
        elif write.model_label != "extras.taggeditem" and not allowed(write):
            model = apps.get_model(write.model_label)
            dependents.add(_DELETE_DEPENDENTS.get(write.model_label, str(model._meta.verbose_name_plural)))
    if owned:
        planner.block(scope, label, OWNED)
        return None
    if dependents:
        planner.block(scope, label, f"it has {', '.join(sorted(dependents))}")
        return None
    return [
        [write.operation, write.model_label, write.pk, write.before_values, write.values] for write in plan.write_set
    ]


def _plan_ip_delete(planner, context, token, row, label, deleting):
    native = context.native(row.identity)
    if "ip" in planner.unproven:
        planner.block("ip", label, UNPROVEN_ABSENCE)
        return
    if native is None:
        planner.block("ip", label, "NetBox has no single matching address")
        return
    owned, facts = _ip_ownership(native, None)
    if owned:
        planner.block("ip", label, OWNED)
        return
    if not planner.permitted(native, "delete"):
        planner.block("ip", label, "permission denied: you cannot delete this IP address")
        return

    def own_device_primary(write):
        return (write.operation, write.model_label, write.pk) == ("set_update", "dcim.device", planner.device.pk)

    closure = _delete_closure(planner, "ip", label, native, own_device_primary)
    if closure is None:
        return
    fields = [(name, text) for name, text in _PRIMARY_FIELDS if getattr(planner.device, f"{name}_id") == native.pk]
    if fields:
        choice = dict(planner.selection.primary).get(token)
        replacements = [
            ip
            for ip in context.device_ips
            if ip.pk not in deleting and ip.pk != native.pk and ip.family == native.family
        ]
        names = ", ".join(text for _name, text in fields)
        if choice is None:
            planner.block("ip", label, f"it is the device {names}: choose a replacement or Clear")
            planner.plan.primary_choices.append((token, label, names, replacements))
            return
        replacement = None if choice == "clear" else next((ip for ip in replacements if str(ip.pk) == choice), None)
        if choice != "clear" and replacement is None:
            planner.block("ip", label, "the replacement address is not available")
            planner.plan.primary_choices.append((token, label, names, replacements))
            return
        if not planner.permitted(planner.device, "change"):
            planner.block("ip", label, "permission denied: you cannot change this device")
            return
        device = planner.device_candidate()
        previous = {name: getattr(device, name) for name, _text in fields}
        for name in previous:
            setattr(device, name, replacement)
        if error := validation_failure(device):
            for name, value in previous.items():
                setattr(device, name, value)
            planner.block("ip", label, error)
            return
        planner.device_fields.update(dict.fromkeys(previous, replacement))
        planner.device_facts.append([token, choice])
    before = _projection(native, ("address", "vrf_id"))
    planner.step("ip", "delete", _ip_label(native), "", {"before": before, "closure": closure, **facts})
    planner.deletes.append(_Operation("delete", native, _ip_label(native), permission="delete"))
    planner.ip_interfaces.add(native.assigned_object_id)


def _plan_ip(planner, rows):
    context = _IPContext(planner)
    moved_from = {}
    deleting = set()
    for _token, row in rows:
        if row.kind == "netbox_only" and (native := context.native(row.identity)) is not None:
            deleting.add(native.pk)
    for token, row in rows:
        label = identity_label(row)
        if row.kind not in SYNCABLE_KINDS:
            planner.block("ip", label, row.reason or row.kind)
        elif row.kind == "device_only":
            _plan_ip_device_only(planner, context, row, label, moved_from)
        elif row.kind == "mismatch":
            native = context.native(row.identity)
            if native is None:
                planner.block("ip", label, "NetBox has no single matching address")
                continue
            if type(row.device_value) is not int:
                planner.block("ip", label, "the device reports no prefix length")
                continue
            address = str(ip_interface(f"{row.identity[1]}/{row.device_value}"))
            after = _candidate(native)
            after.address = address
            target = (native.assigned_object_id, address, native.vrf.name if native.vrf else "")
            detail = f"{native.address} -> {address}"
            _plan_ip_change(planner, label, native, after, action="modify", detail=detail, target=target)
    for token, row in rows:
        if row.kind != "netbox_only":
            continue
        label = identity_label(row)
        native = context.native(row.identity)
        if native is not None and native.pk in moved_from:
            planner.note("ip", label, f"the move of {moved_from[native.pk]} keeps this address")
            continue
        _plan_ip_delete(planner, context, token, row, label, deleting - set(moved_from))


def _interface_ownership(interface, attributes):
    from .models import NSOInterfaceState, NSOOwnershipManifest

    overlays = list(NSOInterfaceState.objects.filter(interface=interface, attribute__in=attributes).order_by("pk"))
    manifests = list(
        NSOOwnershipManifest.objects.filter(
            native_model_label="dcim.interface",
            native_id=interface.pk,
            scope="interface",
            ownership_state="owned",
            state_key__attribute__in=list(attributes),
        ).values_list("pk", flat=True)
    )
    owned = bool(manifests) or any(sm.is_owned(row.status) for row in overlays)
    return owned, overlays, {"overlays": [(row.pk, row.status) for row in overlays], "manifests": sorted(manifests)}


def _device_attribute_value(attribute, value):
    if attribute == "enabled":
        return value if type(value) is bool else None
    return "" if value is None else value.strip() if isinstance(value, str) else None


def _plan_interface_delete(planner, interface, label):
    from .models import NSOOwnershipManifest

    if "interface" in planner.unproven:
        planner.block("interface", label, UNPROVEN_ABSENCE)
        return
    if NSOOwnershipManifest.objects.filter(
        native_model_label="dcim.interface", native_id=interface.pk, ownership_state="owned"
    ).exists():
        planner.block("interface", label, OWNED)
        return
    if not planner.permitted(interface, "delete"):
        planner.block("interface", label, "permission denied: you cannot delete this interface")
        return
    if interface.pk in planner.ip_interfaces:
        planner.block("interface", label, "an IP change in this sync uses it")
        return
    closure = _delete_closure(planner, "interface", label, interface, lambda _write: False)
    if closure is None:
        return
    before = _projection(interface, ("name", "description", "enabled", "type"))
    planner.step("interface", "delete", interface.name, "", {"before": before, "closure": closure})
    planner.interface_deletes.append(_Operation("delete", interface, interface.name, permission="delete"))


def _plan_interface(planner, rows):
    from dcim.models import Interface

    interfaces = list(device_interfaces(planner.management))
    visible = _visible_pks(Interface, planner.user, interfaces)
    by_name = {interface.name: interface for interface in interfaces if interface.pk in visible}
    changes = defaultdict(dict)
    for _token, row in rows:
        label = identity_label(row)
        interface = by_name.get(row.identity)
        if row.kind not in SYNCABLE_KINDS:
            planner.block("interface", label, row.reason or row.kind)
        elif row.kind == "device_only":
            planner.block("interface", label, "NetBox needs an interface type, which the device does not report")
        elif interface is None:
            planner.block("interface", label, NOT_VISIBLE)
        elif row.kind == "netbox_only":
            _plan_interface_delete(planner, interface, label)
        elif (value := _device_attribute_value(row.attribute, row.device_value)) is None:
            planner.block("interface", label, f"the device reports no {row.attribute} value")
        else:
            changes[interface.pk][row.attribute] = value
    for pk, values in sorted(changes.items()):
        interface = next(row for row in interfaces if row.pk == pk)
        attributes = tuple(sorted(values))
        owned, overlays, facts = _interface_ownership(interface, attributes)
        if owned:
            planner.block("interface", interface.name, OWNED)
            continue
        if not planner.permitted(interface, "change"):
            planner.block("interface", interface.name, "permission denied: you cannot change this interface")
            continue
        after = _candidate(interface)
        for attribute, value in values.items():
            setattr(after, attribute, value)
        if error := validation_failure(after):
            planner.block("interface", interface.name, error)
            continue
        detail = "; ".join(
            f"{name}: {_value_text(getattr(interface, name))} -> {_value_text(values[name])}" for name in attributes
        )
        planner.step(
            "interface",
            "modify",
            interface.name,
            detail,
            {"before": _projection(interface, attributes), "after": _projection(after, attributes), **facts},
        )
        planner.interface_saves.append(
            _Operation(
                "save", after, interface.name, update_fields=attributes, permission="change", expected_before=interface
            )
        )
        for overlay in overlays:
            _plan_overlay(planner, "interface", f"{interface.name} {overlay.attribute}", overlay)


def _plan_overlay(planner, scope, target, overlay):
    if overlay.status == sm.IMPORTED:
        return
    sm.advance(overlay.status, sm.RECONCILE, to=sm.IMPORTED)
    candidate = copy.copy(overlay)
    candidate.status = sm.IMPORTED
    planner.step(
        scope, "status", target, f"{overlay.status} -> {sm.IMPORTED}", {"overlay": [overlay.pk, overlay.status]}
    )
    planner.status_saves.append(
        _Operation("save", candidate, target, update_fields=("status",), expected_before=overlay)
    )


def _plan_device(planner):
    if not planner.device_fields:
        return []
    names = tuple(sorted(planner.device_fields))
    after = planner.device_after
    detail = "; ".join(
        f"{name}: {getattr(planner.device, name) or 'none'} -> {getattr(after, name) or 'cleared'}" for name in names
    )
    facts = {
        "before": _projection(planner.device, names),
        "after": _projection(after, names),
        "choices": planner.device_facts,
    }
    planner.step("ip", "modify", planner.device.name, detail, facts)
    return [
        _Operation(
            "save", after, planner.device.name, update_fields=names, permission="change", expected_before=planner.device
        )
    ]


_PLANNERS = {"ip": _plan_ip, "interface": _plan_interface}


def _canonical_row(token, row):
    return [
        token,
        row.kind,
        row.identity,
        row.attribute,
        row.netbox_value,
        row.device_value,
        row.reason,
        row.association_candidate,
    ]


def build_plan(management, user, selection):
    """Return the exact plan for *selection* from the published snapshots and NetBox now."""
    snapshots = observation_snapshots(management)
    planner = _Planner(management, user, selection)
    plan = planner.plan
    for scope in sorted(selection.read_scopes - set(SYNC_SCOPES)):
        planner.block(scope, scope, NOT_SUPPORTED)
    selected = defaultdict(list)
    seen = set()
    for row in differences(management, user=user, snapshots=snapshots):
        token = row_token(row)
        if row.scope in SYNC_SCOPES and selection.selects(row, token):
            selected[row.scope].append((token, row))
            seen.add(token)
    plan.scopes = tuple(scope for scope in SYNC_SCOPES if scope in selection.read_scopes)
    planner.unproven = {
        scope
        for scope in plan.scopes
        if (snapshot := snapshots.get(SCOPE_SPECS[scope].family)) is not None and snapshot.document["unprojectable"]
    }
    for scope in plan.scopes:
        gone = sum(token.startswith(f"{scope}:") for token in selection.rows - seen)
        if gone:
            planner.note(scope, scope, f"{gone} selected row(s) no longer differ after the fresh read")
        _PLANNERS[scope](planner, selected[scope])
    device_saves = _plan_device(planner)
    overlays = {}
    for target in planner.overlay_targets:
        for overlay in _ip_overlay_facts([target]):
            if not sm.is_owned(overlay.status):
                overlays[overlay.pk] = overlay
    for overlay in overlays.values():
        _plan_overlay(planner, "ip", f"{overlay.interface.name} {overlay.address}", overlay)
    plan.operations = [
        *device_saves,
        *planner.saves,
        *planner.creates,
        *planner.deletes,
        *planner.interface_saves,
        *planner.interface_deletes,
        *planner.status_saves,
    ]
    families = sorted(SCOPE_SPECS[scope].family for scope in plan.scopes)
    payload = {
        "device": management.device_id,
        "selection": selection.fields(),
        "snapshots": [
            [family, snapshot.revision, snapshot.source_epoch, snapshot.digest]
            for family in families
            if (snapshot := snapshots.get(family)) is not None
        ],
        "rows": [_canonical_row(token, row) for scope in plan.scopes for token, row in selected[scope]],
        "steps": [[step.scope, step.action, step.target, step.detail, step.facts] for step in plan.steps],
        "blockers": [[note.scope, note.target, note.reason] for note in plan.blockers],
        "notes": [[note.scope, note.target, note.reason] for note in plan.notes],
    }
    canonical = json.dumps(payload, sort_keys=True, default=_jsonable)
    plan.digest = hashlib.sha256(canonical.encode()).hexdigest()
    return plan


def fresh_read(management, scopes):
    """Read the selected families through the gated reconcile path and fail closed."""
    from .adapter_client import AdapterError, public_error_message
    from .read_gate import RAN
    from .reconcile import reconcile_category

    families = sorted(SCOPE_SPECS[scope].family for scope in scopes)
    category = "interface_ips" if set(scopes) == {"ip"} else "interfaces"
    try:
        context = reconcile_category(management.device, management, category)
    except AdapterError as exc:
        raise SyncReadFailed(f"The fresh read failed: {public_error_message(exc)}") from None
    gate = context.get("_gate", {})
    failed = [f"{family} ({gate.get(family, 'not read')})" for family in families if gate.get(family) != RAN]
    if failed:
        raise SyncReadFailed(f"The fresh read did not publish {', '.join(failed)}. Nothing was planned.")


def preview_sync(management, user, selection):
    """Read the selected sync scopes again, then return the exact plan and its digest."""
    scopes = [scope for scope in SYNC_SCOPES if scope in selection.read_scopes]
    if scopes:
        fresh_read(management, scopes)
    return build_plan(management, user, selection)


def confirm_sync(management, user, selection, digest):
    """Execute exactly the previewed plan, or raise SyncStale with zero writes."""
    from django.db.models import ProtectedError, RestrictedError

    from .intent_state import RendererTargetsChanged, reconcile_family_footprint
    from .renderer_writer import (
        IntentPlanStaleError,
        RendererMutationPlan,
        planned_delete,
        planned_save,
        renderer_mirror_writes,
        renderer_writes,
    )
    from .signals import suppress_intent_push

    plan = build_plan(management, user, selection)
    if plan.digest != digest:
        raise SyncStale
    if not plan.operations:
        return plan

    def recheck():
        if build_plan(management, user, selection).digest != digest:
            raise SyncStale

    saves = [op for op in plan.operations if op.kind == "save"]
    try:
        renderer_plan = RendererMutationPlan.build(
            saves=[
                planned_save(
                    op.instance,
                    update_fields=op.update_fields,
                    force_insert=op.create,
                    natural_key=op.natural_key,
                    expected_before=op.expected_before,
                )
                for op in saves
            ],
            deletes=[planned_delete(op.instance) for op in plan.operations if op.kind == "delete"],
            additional_footprints=(reconcile_family_footprint(management.device_id, plan.scopes),),
            validate_after_acquire=recheck,
        )
    except (ProtectedError, RestrictedError):
        raise SyncStale from None
    mutation = renderer_writes if renderer_plan.changes_content else renderer_mirror_writes
    try:
        with suppress_intent_push(), mutation(renderer_plan) as writer:
            for op in plan.operations:
                if op.kind == "delete":
                    try:
                        writer.delete(op.instance)
                    except (ProtectedError, RestrictedError):
                        raise SyncRowFailed(op.label, PROTECTED) from None
                    continue
                if op.permission is not None and (failure := validation_failure(op.instance)):
                    raise SyncRowFailed(op.label, failure)
                writer.save(op.instance, update_fields=op.update_fields, force_insert=op.create)
                if (
                    op.permission in ("add", "change")
                    and not type(op.instance).objects.restrict(user, op.permission).filter(pk=op.instance.pk).exists()
                ):
                    raise SyncRowFailed(op.label, f"permission denied: you cannot {op.permission} this object")
    except (IntentPlanStaleError, RendererTargetsChanged):
        raise SyncStale from None
    return plan
