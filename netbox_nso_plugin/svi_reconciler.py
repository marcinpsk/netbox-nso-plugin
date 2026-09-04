# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile L3 VLAN interfaces from NSO into NetBox."""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)


def svi_reconcile_plan(device, payload: dict):
    """Freeze every native interface and SVI overlay write."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, deletes, _operations, _rows = _svi_reconcile_operations(device, payload, planned_at)
    return RendererMutationPlan.build(
        saves=saves,
        deletes=deletes,
        planned_at=planned_at,
        settles_deploying=False,
    )


def svi_reconcile_footprint(device, payload: dict):
    """Return the mechanically derived SVI reconcile lock footprint."""
    return svi_reconcile_plan(device, payload).lock_footprint


def _svi_reconcile_operations(device, payload, planned_at):
    """Build the deterministic SVI writes shared by preflight and apply."""
    from dcim.models import Interface
    from ipam.models import VLAN

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOSVIState
    from .renderer_writer import planned_delete, planned_save
    from .vlan_reconciler import _device_vlan_group

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], [], []
    raw_items = payload.get("interfaces", []) if isinstance(payload, dict) else []
    items = raw_items if isinstance(raw_items, list) else []
    group = _device_vlan_group(device, create=False)
    interfaces = {row.name: row for row in Interface.objects.filter(device=device).order_by("pk")}
    states = {
        row.interface.name: row
        for row in NSOSVIState.objects.filter(management=management).select_related("interface", "vlan").order_by("pk")
    }
    vlans = {row.vid: row for row in VLAN.objects.filter(group=group).order_by("pk")} if group is not None else {}
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
        interface = interfaces.get(name)
        if interface is None:
            interface = Interface(device=device, name=name, type="virtual")
            interface._site = device.site
            interface._location = device.location
            interface._rack = device.rack
            saves.append(planned_save(interface, force_insert=True, natural_key=("device", "name")))
            operations.append(("save", interface, None, True))

        current = states.get(name)
        state = (
            copy.copy(current)
            if current is not None
            else NSOSVIState(management=management, interface=interface, status="unknown")
        )
        vid = item.get("vlan_id")
        vlan = vlans.get(vid) if vid else None
        device_type = item.get("type") or "svi"
        device_vrf = item.get("vrf") or ""
        if sm.is_owned(state.status):
            desired_vid = state.vlan.vid if state.vlan else None
            matches = desired_vid == vid and state.svi_type == device_type and state.vrf == device_vrf
            state.status = sm.on_reconcile(state.status, matches=matches, settles_deploying=False)
        else:
            state.vlan = vlan
            state.svi_type = device_type
            state.vrf = device_vrf
            state.status = sm.on_reconcile(state.status, matches=None)
        state.last_sync_at = planned_at
        created = current is None
        if created:
            update_fields = None
        elif sm.is_owned(current.status):
            update_fields = ("status", "last_sync_at")
        else:
            update_fields = ("vlan", "svi_type", "vrf", "status", "last_sync_at")
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


def reconcile_svi(device, payload: dict) -> list:
    """Apply one frozen SVI reconciliation through the renderer writer."""
    from .renderer_writer import active_renderer_writer, renderer_writes_replanning_once
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    if active is not None:
        with contextlib.nullcontext(active) as writer, suppress_intent_push():
            return _reconcile_svi(device, payload, writer, active.plan.planned_at)

    def plan_fn():
        return svi_reconcile_plan(device, payload)

    with renderer_writes_replanning_once(plan_fn) as (writer, plan), suppress_intent_push():
        return _reconcile_svi(device, payload, writer, plan.planned_at)


def _reconcile_svi(device, payload: dict, writer, planned_at) -> list:
    """Execute the SVI operations after their exact write set is frozen."""
    _saves, _deletes, operations, rows = _svi_reconcile_operations(device, payload, planned_at)
    raced_states = set()
    for row in rows:
        writer.consume_existing_creation(row.interface)
        if writer.consume_existing_creation(row):
            raced_states.add(id(row))
    for operation, instance, update_fields, force_insert in operations:
        if operation == "delete":
            writer.delete(instance)
        elif id(instance) in raced_states:
            continue
        else:
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)
    return rows
