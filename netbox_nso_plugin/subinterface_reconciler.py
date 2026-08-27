# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile dot1q L3 subinterfaces from NSO into NetBox.

Materialises the virtual ``dcim.Interface`` (type=virtual), links it to its
physical parent via ``Interface.parent`` (looked up by name — never created
here; the parent comes from normal device sync / the interface export), records
the interface-local dot1q encapsulation tag on the overlay (NOT an ``ipam.VLAN``),
and tracks ``NSOSubinterfaceState``. IP addresses are NOT handled here — they
ride the interface-IP path on the same interface, so this reconcile MUST run
before ``_reconcile_interface_ips`` (which only attaches IPs to interfaces that
already exist).
"""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)


def subinterface_reconcile_plan(device, payload: dict):
    """Freeze every native interface and subinterface overlay write."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, deletes, _operations, _rows = _subinterface_reconcile_operations(device, payload, planned_at)
    return RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)


def _subinterface_reconcile_operations(device, payload, planned_at):
    """Build deterministic subinterface writes for preflight and apply."""
    from dcim.models import Interface

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOSubinterfaceState
    from .renderer_writer import planned_delete, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], [], []
    raw_items = payload.get("interfaces", []) if isinstance(payload, dict) else []
    items = raw_items if isinstance(raw_items, list) else []
    interfaces = {row.name: row for row in Interface.objects.filter(device=device).order_by("pk")}
    states = {
        row.interface.name: row
        for row in NSOSubinterfaceState.objects.filter(management=management)
        .select_related("interface", "parent_interface")
        .order_by("pk")
    }
    saves = []
    deletes = []
    operations = []
    rows = []
    reported = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("interface_name")
        if not name:
            continue
        reported.add(name)
        current_interface = interfaces.get(name)
        interface = current_interface or Interface(device=device, name=name, type="virtual")
        if current_interface is None:
            interface._site = device.site
            interface._location = device.location
            interface._rack = device.rack
            interfaces[name] = interface

        current = states.get(name)
        state = (
            copy.copy(current)
            if current is not None
            else NSOSubinterfaceState(management=management, interface=interface)
        )
        parent = interfaces.get(item.get("parent_interface") or "")
        device_dot1q = item.get("dot1q_vlan")
        device_vrf = item.get("vrf") or ""
        owned = sm.is_owned(state.status)
        if owned:
            desired_parent_name = state.parent_interface.name if state.parent_interface else ""
            matches = (
                desired_parent_name == (item.get("parent_interface") or "")
                and state.dot1q_vlan == device_dot1q
                and state.vrf == device_vrf
            )
            state.status = sm.on_reconcile(state.status, matches=matches, settles_deploying=False)
        else:
            interface.parent = parent
            state.parent_interface = parent
            state.dot1q_vlan = device_dot1q
            state.vrf = device_vrf
            state.status = sm.on_reconcile(state.status, matches=parent is not None, settles_owned=False)
        state.last_sync_at = planned_at

        if current_interface is None:
            saves.append(planned_save(interface, force_insert=True, natural_key=("device", "name")))
            operations.append(("save", interface, None, True))
        elif not owned and current_interface.parent_id != interface.parent_id:
            saves.append(planned_save(interface, update_fields=("parent",)))
            operations.append(("save", interface, ("parent",), False))
        created = current is None
        update_fields = (
            None
            if created
            else (
                ("status", "last_sync_at")
                if owned
                else ("parent_interface", "dot1q_vlan", "vrf", "status", "last_sync_at")
            )
        )
        saves.append(
            planned_save(
                state,
                update_fields=update_fields,
                force_insert=created,
                natural_key=("management", "interface"),
            )
        )
        operations.append(("save", state, update_fields, created))
        rows.append(state)

    for stale in states.values():
        if stale.interface.name in reported:
            continue
        if not sm.is_owned(stale.status):
            deletes.append(planned_delete(stale))
            operations.append(("delete", stale, None, False))
            continue
        new_status = sm.on_reconcile(stale.status, present=False)
        if new_status == stale.status:
            continue
        candidate = copy.copy(stale)
        candidate.status = new_status
        candidate.last_sync_at = planned_at
        fields = ("status", "last_sync_at")
        saves.append(planned_save(candidate, update_fields=fields))
        operations.append(("save", candidate, fields, False))

    return saves, deletes, operations, rows


def subinterface_reconcile_footprint(device, payload: dict):
    """Return the immutable footprint for callers that only need lock discovery."""
    return subinterface_reconcile_plan(device, payload).lock_footprint


def reconcile_subinterface(device, payload: dict) -> list:
    """Apply one frozen subinterface reconciliation through the renderer writer."""
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    plan = active.plan if active is not None else subinterface_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        return _reconcile_subinterface(device, payload, writer, plan.planned_at)


def _reconcile_subinterface(device, payload: dict, writer, planned_at) -> list:
    """Apply a subinterface mirror after its complete footprint is locked."""
    _saves, _deletes, operations, rows = _subinterface_reconcile_operations(device, payload, planned_at)
    for operation, instance, update_fields, force_insert in operations:
        if operation == "delete":
            writer.delete(instance)
        else:
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)
    return rows
