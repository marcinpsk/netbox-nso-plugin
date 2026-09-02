# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile OSPF through one exact native and overlay mutation plan."""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)


def ospf_reconcile_plan(device, payload):
    """Freeze every native and overlay OSPF write before reconciliation."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    try:
        saves, deletes, _operations, _dropped = _ospf_reconcile_operations(device, payload, planned_at)
    except ImportError:
        return RendererMutationPlan.build(planned_at=planned_at)
    return RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)


def _area_candidates(area_id) -> set[str]:
    """Return all equivalent persisted forms of one OSPF area identifier."""
    from .template_content import _canonical_area_id

    canonical = _canonical_area_id(area_id)
    candidates = {str(area_id), canonical}
    try:
        octets = [int(value) for value in canonical.split(".")]
        candidates.add(str((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]))
    except (IndexError, TypeError, ValueError):
        pass
    return candidates


def _ospf_instance_device_content(entry, vrf) -> dict:
    from . import merge_util
    from .template_content import _clean_router_id

    return {
        "router_id": str(_clean_router_id(entry.get("router_id"))),
        "vrf": merge_util.pk(vrf),
    }


def _ospf_instance_object_content(instance) -> dict:
    return {
        "router_id": str(instance.router_id),
        "vrf": instance.vrf_id,
    }


def _ospf_interface_fields(entry, instance, area) -> dict:
    from .template_content import _OSPF_AUTH_MAP, _OSPF_NETWORK_TYPES

    cost = entry.get("cost")
    cost = cost if isinstance(cost, int) and 1 <= cost <= 65535 else None
    network_type = entry.get("network_type")
    network_type = network_type if network_type in _OSPF_NETWORK_TYPES else None
    return {
        "instance": instance,
        "area": area,
        "passive": bool(entry.get("passive", False)),
        "priority": entry.get("priority"),
        "cost": cost,
        "network_type": network_type,
        "authentication": _OSPF_AUTH_MAP.get(entry.get("auth_type") or ""),
    }


def _ospf_interface_content(native, fields, *, object_values: bool) -> dict:
    from . import merge_util
    from .template_content import _canonical_area_id

    content = {}
    for name, value in fields.items():
        if name == "instance":
            content[name] = native.instance_id if object_values else merge_util.pk(value)
        elif name == "area":
            area = native.area if object_values else value
            content[name] = _canonical_area_id(area.area_id) if area is not None else None
        elif name == "passive":
            content[name] = bool(getattr(native, name) if object_values else value)
        else:
            content[name] = getattr(native, name) if object_values else value
    return content


def _ospf_reconcile_operations(device, payload, planned_at):  # noqa: C901, PLR0915
    """Build the deterministic OSPF writes used by preflight and apply."""
    from dcim.models import Interface
    from ipam.models import VRF
    from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

    from . import merge_util
    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOOSPFInstanceState, NSOOSPFInterfaceState
    from .renderer_writer import planned_delete, planned_save
    from .template_content import _adapter_setting, _canonical_area_id, _clean_router_id

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], [], []

    saves = []
    deletes = []
    operations = []

    def save(instance, *, update_fields=None, force_insert=False, natural_key=()):
        saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            )
        )
        operations.append(("save", instance, update_fields, force_insert))

    def delete(instance):
        deletes.append(planned_delete(instance))
        operations.append(("delete", instance, None, False))

    instance_entries = {}
    for raw_entry in payload.get("instances") or []:
        if not isinstance(raw_entry, dict) or raw_entry.get("process_id") is None:
            continue
        entry = dict(raw_entry)
        process_id = str(entry["process_id"])
        entry["process_id"] = process_id
        instance_entries[process_id] = entry

    interface_entries = []
    for raw_entry in payload.get("interfaces") or []:
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        raw_process_id = entry.get("process_id")
        entry["process_id"] = str(raw_process_id) if raw_process_id is not None else None
        interface_entries.append(entry)

    areas = list(OSPFArea.objects.all().order_by("pk"))
    area_by_canonical = {}
    for area in areas:
        for candidate in _area_candidates(area.area_id):
            area_by_canonical.setdefault(_canonical_area_id(candidate), area)
    requested_area_ids = {
        _canonical_area_id(entry.get("area_id") or "0.0.0.0")
        for entry in interface_entries
        if entry.get("interface_name")
    }
    for area_id in sorted(requested_area_ids):
        if area_id in area_by_canonical:
            continue
        area = OSPFArea(area_id=area_id, area_type="standard")
        area_by_canonical[area_id] = area
        save(area, force_insert=True, natural_key=("area_id",))

    vrf_by_name = {row.name: row for row in VRF.objects.all().order_by("pk")}
    native_instances = {
        str(row.process_id): row
        for row in OSPFInstance.objects.filter(device=device).select_related("vrf").order_by("pk")
    }
    instance_states = {
        row.process_id: row
        for row in NSOOSPFInstanceState.objects.filter(management=management)
        .select_related("ospf_instance")
        .order_by("pk")
    }
    seen_process_ids = set()

    for process_id, entry in instance_entries.items():
        seen_process_ids.add(process_id)
        current_state = instance_states.get(process_id)
        state = (
            copy.copy(current_state)
            if current_state is not None
            else NSOOSPFInstanceState(management=management, process_id=process_id)
        )
        owned = sm.is_owned(state.status)
        state.router_id = _clean_router_id(entry.get("router_id"))
        state.vrf = entry.get("vrf") or ""
        state.areas = entry.get("areas") or []
        if not owned:
            state.enabled = entry.get("enabled")
        state.last_sync_at = planned_at

        native = None
        matches = True
        conflict = False
        new_base = state.device_base_hash
        router_id = _clean_router_id(entry.get("router_id"))
        if router_id:
            vrf_name = entry.get("vrf") or ""
            vrf = vrf_by_name.get(vrf_name) if vrf_name else None
            if vrf_name and vrf is None and _adapter_setting("vrf_auto_create"):
                vrf = VRF(name=vrf_name)
                vrf_by_name[vrf_name] = vrf
                save(vrf, force_insert=True, natural_key=("name",))

            current_native = native_instances.get(process_id)
            native = (
                copy.copy(current_native)
                if current_native is not None
                else OSPFInstance(
                    device=device,
                    process_id=process_id,
                    name=process_id,
                    router_id=router_id,
                    vrf=vrf,
                )
            )
            created_native = current_native is None
            device_hash = merge_util.content_hash(_ospf_instance_device_content(entry, vrf))
            object_hash = merge_util.content_hash(_ospf_instance_object_content(native))
            action = merge_util.three_way(
                created=created_native,
                base=state.device_base_hash,
                obj_hash=object_hash,
                dev_hash=device_hash,
            )
            if created_native:
                save(native, force_insert=True, natural_key=("device", "process_id"))
            elif action == "mirror":
                native.router_id = router_id
                native.vrf = vrf
                save(native, update_fields=("router_id", "vrf"))
            if action in {"seed", "mirror", "insync"}:
                matches = True
                new_base = device_hash
            elif action == "freeze":
                matches = False
            else:
                matches = False
                conflict = True
            native_instances[process_id] = native

        state.ospf_instance = native or state.ospf_instance
        state.device_base_hash = new_base
        state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
        created_state = current_state is None
        fields = (
            None
            if created_state
            else (
                "router_id",
                "vrf",
                "areas",
                "enabled",
                "ospf_instance",
                "status",
                "last_sync_at",
                "device_base_hash",
            )
        )
        save(
            state,
            update_fields=fields,
            force_insert=created_state,
            natural_key=("management", "process_id"),
        )

    for process_id, current_state in instance_states.items():
        if process_id in seen_process_ids:
            continue
        if not sm.is_owned(current_state.status) and current_state.ospf_instance_id is None:
            delete(current_state)
            continue
        new_status = sm.on_reconcile(current_state.status, present=False)
        if new_status == current_state.status:
            continue
        state = copy.copy(current_state)
        state.status = new_status
        state.last_sync_at = planned_at
        save(state, update_fields=("status", "last_sync_at"))

    interfaces = {row.name: row for row in Interface.objects.filter(device=device).order_by("pk")}
    native_interfaces = {
        row.interface_id: row
        for row in OSPFInterface.objects.filter(interface__device=device)
        .select_related("instance", "area")
        .order_by("pk")
    }
    interface_states = {
        row.interface_id: row
        for row in NSOOSPFInterfaceState.objects.filter(management=management)
        .select_related("interface")
        .order_by("pk")
    }
    seen_interface_ids = set()
    dropped = []

    for entry in interface_entries:
        interface_name = entry.get("interface_name") or ""
        if not interface_name:
            continue
        interface = interfaces.get(interface_name)
        if interface is None:
            dropped.append(interface_name)
            continue
        seen_interface_ids.add(interface.pk)
        current_state = interface_states.get(interface.pk)
        state = (
            copy.copy(current_state)
            if current_state is not None
            else NSOOSPFInterfaceState(management=management, interface=interface)
        )
        owned = sm.is_owned(state.status)
        if not owned:
            state.process_id = entry["process_id"]
            state.area_id = entry.get("area_id") or ""
            state.passive = bool(entry.get("passive", False))
            state.priority = entry.get("priority")
            state.cost = entry.get("cost")
            state.network_type = entry.get("network_type") or ""
            state.auth_type = entry.get("auth_type") or ""
            state.auth_present = bool(entry.get("auth_present", False))
        state.last_sync_at = planned_at

        matches = True
        conflict = False
        new_base = state.device_base_hash
        instance = native_instances.get(entry["process_id"])
        if instance is not None:
            area_id = _canonical_area_id(entry.get("area_id") or "0.0.0.0")
            area = area_by_canonical[area_id]
            fields = _ospf_interface_fields(entry, instance, area)
            current_native = native_interfaces.get(interface.pk)
            native = (
                copy.copy(current_native)
                if current_native is not None
                else OSPFInterface(interface=interface, **fields)
            )
            created_native = current_native is None
            device_hash = merge_util.content_hash(_ospf_interface_content(native, fields, object_values=False))
            object_hash = merge_util.content_hash(_ospf_interface_content(native, fields, object_values=True))
            action = merge_util.three_way(
                created=created_native,
                base=state.device_base_hash,
                obj_hash=object_hash,
                dev_hash=device_hash,
            )
            if created_native:
                save(native, force_insert=True, natural_key=("interface",))
            elif action == "mirror":
                for name, value in fields.items():
                    setattr(native, name, value)
                save(native, update_fields=tuple(fields))
            elif current_native.passive is None:
                native.passive = fields["passive"]
                save(native, update_fields=("passive",))
            if action in {"seed", "mirror", "insync"}:
                matches = True
                new_base = device_hash
            elif action == "freeze":
                matches = False
            else:
                matches = False
                conflict = True
            native_interfaces[interface.pk] = native

        state.device_base_hash = new_base
        state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
        created_state = current_state is None
        fields = (
            None
            if created_state
            else (
                "process_id",
                "area_id",
                "passive",
                "priority",
                "cost",
                "network_type",
                "auth_type",
                "auth_present",
                "status",
                "last_sync_at",
                "device_base_hash",
            )
        )
        save(
            state,
            update_fields=fields,
            force_insert=created_state,
            natural_key=("management", "interface"),
        )

    for interface_id, current_state in interface_states.items():
        if interface_id in seen_interface_ids:
            continue
        native_present = interface_id in native_interfaces
        if not sm.is_owned(current_state.status) and not native_present:
            delete(current_state)
            continue
        new_status = sm.on_reconcile(current_state.status, present=False)
        if new_status == current_state.status:
            continue
        state = copy.copy(current_state)
        state.status = new_status
        state.last_sync_at = planned_at
        save(state, update_fields=("status", "last_sync_at"))

    return saves, deletes, operations, dropped


def reconcile_ospf(device, payload):
    """Apply one frozen OSPF reconciliation through the renderer writer."""
    try:
        from netbox_routing.models import OSPFInstance  # noqa: F401
    except ImportError:
        logger.warning("netbox_routing not installed; skipping OSPF reconcile")
        return {"instances": [], "interfaces": []}

    from .models import NSODeviceManagement, NSOOSPFInstanceState, NSOOSPFInterfaceState
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    if not NSODeviceManagement.objects.filter(device=device).exists():
        return {"instances": [], "interfaces": []}

    active = active_renderer_writer()
    plan = active.plan if active is not None else ospf_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        _saves, _deletes, operations, dropped = _ospf_reconcile_operations(device, payload, plan.planned_at)
        for operation, instance, update_fields, force_insert in operations:
            if operation == "delete":
                writer.delete(instance)
            else:
                writer.save(instance, update_fields=update_fields, force_insert=force_insert)

    if dropped:
        logger.warning(
            "OSPF reconcile for %s: %d interface(s) not found in NetBox, dropped: %s",
            device,
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )

    management = NSODeviceManagement.objects.get(device=device)
    return {
        "instances": list(NSOOSPFInstanceState.objects.filter(management=management).select_related("ospf_instance")),
        "interfaces": list(NSOOSPFInterfaceState.objects.filter(management=management).select_related("interface")),
    }
