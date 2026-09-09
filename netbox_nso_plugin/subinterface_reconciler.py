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
        if not name or name in reported:
            continue
        reported.add(name)
        current_interface = interfaces.get(name)
        stored_parent_id = current_interface.parent_id if current_interface is not None else None
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
        elif not owned and stored_parent_id != interface.parent_id:
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
        if active is not None:
            return _reconcile_frozen_subinterface(writer, payload)
        return _reconcile_subinterface(device, payload, writer, plan.planned_at)


def _reconcile_subinterface(device, payload: dict, writer, planned_at) -> list:
    """Apply a subinterface mirror after its complete footprint is locked."""
    _saves, _deletes, operations, rows = _subinterface_reconcile_operations(device, payload, planned_at)
    for row in rows:
        writer.consume_existing_creation(row.interface)
    for operation, instance, update_fields, force_insert in operations:
        if operation == "delete":
            writer.delete(instance)
        else:
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)
    return rows


def _frozen_subinterface_operations(plan):
    """Materialize native and overlay operations from the frozen write set."""
    from dcim.models import Interface

    from .models import NSOSubinterfaceState
    from .renderer_writer import RendererCreationRef

    models = {model._meta.label_lower: model for model in (Interface, NSOSubinterfaceState)}
    creations = {}
    for write in plan.write_set:
        if write.model_label not in models or write.cascade:
            continue
        model = models[write.model_label]
        values = {
            name: creations[value].pk if isinstance(value, RendererCreationRef) else value
            for name, value in write.values
        }
        current = model.objects.filter(pk=write.pk).first() if write.pk is not None else None
        if write.force_insert:
            natural_key = {
                name: creations[value].pk if isinstance(value, RendererCreationRef) else value
                for name, value in write.natural_key
            }
            current = model.objects.filter(**natural_key).first()
        instance = copy.copy(current) if current is not None else model(pk=write.pk)
        if not write.force_insert or current is None:
            for field_name, value in values.items():
                if model._meta.get_field(field_name).get_internal_type() == "JSONField":
                    value = dict(value)
                setattr(instance, field_name, value)
        if write.force_insert:
            reference = RendererCreationRef(model_label=write.model_label, natural_key=write.natural_key)
            creations[reference] = instance
        yield write.operation, instance, write.update_fields, write.force_insert


def _reconcile_frozen_subinterface(writer, payload):
    """Consume completed writes and execute the remaining frozen operations."""
    from .models import NSOSubinterfaceState

    raw_items = payload.get("interfaces", []) if isinstance(payload, dict) else []
    items = raw_items if isinstance(raw_items, list) else []
    reported = {item.get("interface_name") for item in items if isinstance(item, dict)}
    rows = []
    for operation, instance, update_fields, force_insert in _frozen_subinterface_operations(writer.plan):
        if operation == "delete":
            writer.delete(instance)
            continue
        if not (writer.consume_existing_creation(instance) or writer.consume_applied_save(instance)):
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)
        if isinstance(instance, NSOSubinterfaceState) and instance.interface.name in reported:
            rows.append(instance)
    return rows
