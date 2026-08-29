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
    from dcim.models import Interface
    from django.utils import timezone

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOInterfaceMtuState
    from .renderer_writer import RendererMutationPlan, planned_delete, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return RendererMutationPlan.build()
    interfaces = {row.name: row for row in Interface.objects.filter(device=device)}
    states = {
        row.interface_id: row
        for row in NSOInterfaceMtuState.objects.filter(management=management).select_related("interface").order_by("pk")
    }
    planned_at = timezone.now()
    saves = []
    deletes = []
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
            candidate.status = sm.on_reconcile(candidate.status, matches=device_values == expected_values)
        else:
            candidate.l2_mtu, candidate.ip_mtu, candidate.mpls_mtu = device_values
            candidate.status = sm.on_reconcile(candidate.status, matches=None)
        candidate.last_sync_at = planned_at
        saves.append(
            planned_save(
                candidate,
                force_insert=current is None,
                natural_key=("management", "interface"),
            )
        )

    for stale in states.values():
        if stale.interface.name in matched_names:
            continue
        if sm.is_owned(stale.status):
            candidate = copy.copy(stale)
            candidate.status = sm.on_reconcile(stale.status, present=False)
            candidate.last_sync_at = planned_at
            saves.append(planned_save(candidate, update_fields=("status", "last_sync_at")))
        else:
            deletes.append(planned_delete(stale))
    return RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)


def reconcile_interface_mtu(device, payload: dict) -> list:
    """Apply one frozen MTU reconciliation through the renderer writer."""
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    plan = active.plan if active is not None else interface_mtu_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        return _reconcile_interface_mtu(device, payload, writer, plan.planned_at)


def _reconcile_interface_mtu(device, payload: dict, writer, planned_at) -> list:
    """Apply the MTU payload after its exact write set is frozen."""
    from dcim.models import Interface

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOInterfaceMtuState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    # One query for the device's interfaces; match each payload entry by name.
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}

    now = planned_at
    rows: list = []
    matched_names: set[str] = set()
    for item in payload.get("interfaces", []):
        name = item.get("interface_name")
        if not name or name in matched_names:
            continue
        iface = iface_map.get(name)
        if iface is None:
            # MTU is an attribute on an existing interface — skip if the interface
            # isn't in NetBox yet (device/interface sync owns interface creation).
            continue
        matched_names.add(name)

        state = NSOInterfaceMtuState.objects.filter(management=management, interface=iface).first()
        created = state is None
        if created:
            state = NSOInterfaceMtuState(management=management, interface=iface)
        dev_mtu, dev_ip, dev_mpls = item.get("mtu"), item.get("ip_mtu"), item.get("mpls_mtu")
        state.bound_port = item.get("bound_port") or ""

        if sm.is_owned(state.status):
            # Owned MTU values are operator intent and must not be replaced with device values.
            # Only correlated Apply evidence can settle a deploying row.
            matches = dev_mtu == state.l2_mtu and dev_ip == state.ip_mtu and dev_mpls == state.mpls_mtu
            state.status = sm.on_reconcile(state.status, matches=matches, settles_deploying=False)
        else:
            # Unowned mirror: track the device values for display.
            state.l2_mtu, state.ip_mtu, state.mpls_mtu = dev_mtu, dev_ip, dev_mpls
            state.status = sm.on_reconcile(state.status, matches=None)
        state.last_sync_at = now
        writer.save(state, force_insert=created)
        rows.append(state)

    # Rows the device no longer reports an MTU on: owned rows are operator intent
    # (the device may simply not have caught up yet) → transition via present=False
    # (keeps accepted/deploying, else → changed); unowned vestigial rows are pruned.
    for stale in NSOInterfaceMtuState.objects.filter(management=management).exclude(interface__name__in=matched_names):
        if sm.is_owned(stale.status):
            stale.status = sm.on_reconcile(stale.status, present=False)
            stale.last_sync_at = now
            writer.save(stale, update_fields=["status", "last_sync_at"])
        else:
            writer.delete(stale)
    return rows
