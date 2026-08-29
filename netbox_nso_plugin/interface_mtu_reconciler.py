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
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ReconcileExecution:
    """The operations and result rows captured by one frozen MTU plan."""

    operations: tuple
    rows: tuple


def _validated_interface_items(payload: dict) -> tuple[dict, ...]:
    """Validate one adapter MTU document before planning stale-row changes."""
    if not isinstance(payload, dict):
        raise ValueError("interface MTU payload must be an object")
    items = payload.get("interfaces", [])
    if not isinstance(items, list):
        raise ValueError("interface MTU interfaces must be a list")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("interface MTU payload entry must be an object")
        name = item.get("interface_name")
        if not isinstance(name, str) or not name:
            raise ValueError("interface MTU payload entry interface_name must be a non-empty string")
        if name in seen:
            raise ValueError(f"duplicate interface_name in interface MTU payload: {name}")
        seen.add(name)
    return tuple(items)


def interface_mtu_reconcile_plan(device, payload: dict):
    """Freeze every MTU overlay save/delete before the first lock or write."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, deletes, operations, rows = _interface_mtu_reconcile_operations(device, payload, planned_at)
    return RendererMutationPlan.build(
        saves=saves,
        deletes=deletes,
        planned_at=planned_at,
        execution=_ReconcileExecution(tuple(operations), tuple(rows)),
    )


def _interface_mtu_reconcile_operations(device, payload, planned_at):
    """Build the deterministic MTU writes shared by preflight and apply."""
    from dcim.models import Interface

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOInterfaceMtuState
    from .renderer_writer import planned_delete, planned_save

    items = _validated_interface_items(payload)
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

    for item in items:
        name = item.get("interface_name")
        interface = interfaces.get(name)
        if interface is None:
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

    _validated_interface_items(payload)
    active = active_renderer_writer()
    plan = active.plan if active is not None else interface_mtu_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        return _reconcile_interface_mtu(payload, writer, plan)


def _reconcile_interface_mtu(payload: dict, writer, plan) -> list:
    """Apply the MTU payload after its exact write set is frozen."""
    _validated_interface_items(payload)
    execution = plan.execution
    if not isinstance(execution, _ReconcileExecution):
        raise ValueError("interface MTU reconciliation requires its frozen execution steps")
    for operation, instance, update_fields, force_insert in execution.operations:
        if operation == "save":
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)
        else:
            writer.delete(instance)
    return list(execution.rows)
