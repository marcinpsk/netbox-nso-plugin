# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 2b: reconcile per-interface MTU from NSO into NetBox (read path).

Read-only mirror: each payload entry maps to an existing ``dcim.Interface`` by
name (never created here — the interface comes from normal device sync). The
native ``l2_mtu`` is mirrored on ``NSOInterfaceMtuState`` for display but NOT yet
written to ``dcim.Interface.mtu`` (that is the accept/write slice). Only
interfaces that actually set an MTU appear (the export reads NO_DEFAULTS).
"""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)


def interface_mtu_reconcile_plan(device, payload: dict):
    """Freeze every MTU overlay save/delete before the first lock or write."""
    from django.utils import timezone

    plan, _operations, _rows = _interface_mtu_plan_and_operations(device, payload, timezone.now())
    return plan


def _interface_mtu_plan_and_operations(device, payload, planned_at):
    """Build one exact MTU plan and its matching operation sequence."""
    from .renderer_writer import RendererMutationPlan

    saves, deletes, operations, rows = _interface_mtu_reconcile_operations(device, payload, planned_at)
    plan = RendererMutationPlan.build(
        saves=saves,
        deletes=deletes,
        planned_at=planned_at,
        settles_deploying=False,
    )
    return plan, operations, rows


def _interface_mtu_reconcile_operations(device, payload, planned_at):
    """Build deterministic MTU writes for preflight and direct apply."""
    from dcim.models import Interface

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOInterfaceMtuState
    from .renderer_writer import planned_delete, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], [], []
    interfaces = {row.name: row for row in Interface.objects.filter(device=device)}
    states = {
        row.interface_id: row
        for row in NSOInterfaceMtuState.objects.filter(management=management).select_related("interface").order_by("pk")
    }
    saves = []
    deletes = []
    operations = []
    rows = []
    matched_names = set()

    for item in payload.get("interfaces", []):
        name = item.get("interface_name")
        if name in matched_names:
            continue
        interface = interfaces.get(name)
        if not name or interface is None:
            continue
        matched_names.add(name)
        current = states.get(interface.pk)
        candidate = (
            copy.copy(current)
            if current is not None
            else NSOInterfaceMtuState(management=management, interface=interface)
        )
        device_values = (item.get("mtu"), item.get("ip_mtu"), item.get("mpls_mtu"))
        candidate.bound_port = item.get("bound_port") or ""
        if sm.is_owned(candidate.status):
            expected_values = (candidate.l2_mtu, candidate.ip_mtu, candidate.mpls_mtu)
            candidate.status = sm.on_reconcile(
                candidate.status,
                matches=device_values == expected_values,
                settles_deploying=False,
            )
        else:
            candidate.l2_mtu, candidate.ip_mtu, candidate.mpls_mtu = device_values
            candidate.status = sm.on_reconcile(candidate.status, matches=None, settles_deploying=False)
        candidate.last_sync_at = planned_at
        created = current is None
        saves.append(planned_save(candidate, force_insert=created, natural_key=("management", "interface")))
        operations.append(("save", candidate, None, created))
        rows.append(candidate)

    for stale in states.values():
        if stale.interface.name in matched_names:
            continue
        if sm.is_owned(stale.status):
            candidate = copy.copy(stale)
            candidate.status = sm.on_reconcile(stale.status, present=False, settles_deploying=False)
            candidate.last_sync_at = planned_at
            fields = ("status", "last_sync_at")
            saves.append(planned_save(candidate, update_fields=fields))
            operations.append(("save", candidate, fields, False))
        else:
            deletes.append(planned_delete(stale))
            operations.append(("delete", stale, None, False))
    return saves, deletes, operations, rows


def reconcile_interface_mtu(device, payload: dict) -> list:
    """Apply one frozen MTU reconciliation through the renderer writer."""
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    if active is None:
        from django.utils import timezone

        plan, operations, rows = _interface_mtu_plan_and_operations(device, payload, timezone.now())
    else:
        plan = active.plan
        operations, rows = _frozen_interface_mtu_operations(plan)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        _execute_interface_mtu_operations(writer, operations)
    return rows


def _frozen_interface_mtu_operations(plan):
    """Materialize only MTU operations from the active immutable write set."""
    from .models import NSOInterfaceMtuState

    operations = []
    rows = []
    model_label = NSOInterfaceMtuState._meta.label_lower
    for write in plan.write_set:
        if write.model_label != model_label or write.cascade:
            continue
        current = NSOInterfaceMtuState.objects.filter(pk=write.pk).first() if write.pk is not None else None
        instance = copy.copy(current) if current is not None else NSOInterfaceMtuState(pk=write.pk)
        for field_name, value in write.values:
            if NSOInterfaceMtuState._meta.get_field(field_name).get_internal_type() == "JSONField":
                continue
            setattr(instance, field_name, value)
        if write.operation == "save":
            operations.append(("save", instance, write.update_fields, write.force_insert))
            if write.update_fields is None:
                rows.append(instance)
        elif write.operation == "delete":
            operations.append(("delete", instance, None, False))
    return operations, rows


def _execute_interface_mtu_operations(writer, operations):
    """Replay the operations paired with one frozen MTU plan."""
    for operation, instance, update_fields, force_insert in operations:
        if operation == "delete":
            writer.delete(instance)
        else:
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)
