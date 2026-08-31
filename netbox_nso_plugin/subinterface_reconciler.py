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

import logging

from .intent_state import mirror_reconciler, reconcile_transaction

logger = logging.getLogger(__name__)


def subinterface_reconcile_plan(device, payload: dict):
    """Declare one subinterface refresh and whether it changes rendered membership."""
    from dcim.models import Interface

    from .intent_state import MutationFootprint, ReconcileMutationPlan, SourceRow
    from .models import NSODeviceManagement, NSOSubinterfaceState

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return ReconcileMutationPlan(MutationFootprint())
    raw_items = payload.get("interfaces", []) if isinstance(payload, dict) else []
    items = raw_items if isinstance(raw_items, list) else []
    interfaces = tuple(Interface.objects.filter(device=device).order_by("pk"))
    states = tuple(
        NSOSubinterfaceState.objects.filter(management=management).select_related("interface").order_by("pk")
    )
    reported = {item.get("interface_name") for item in items if isinstance(item, dict) and item.get("interface_name")}
    changes_content = any(state.status == "in_sync" and state.interface.name not in reported for state in states)
    return ReconcileMutationPlan(
        MutationFootprint.for_keys(
            {(device.pk, "subinterface")},
            source_rows=(
                SourceRow("dcim.device", device.pk),
                SourceRow("dcim.interface", None),
                *(SourceRow("dcim.interface", interface.pk) for interface in interfaces),
            ),
            overlay_rows=(
                SourceRow("netbox_nso_plugin.nsosubinterfacestate", None),
                *(SourceRow(state._meta.label_lower, state.pk) for state in states),
            ),
        ),
        changes_content=changes_content,
    )


def subinterface_reconcile_footprint(device, payload: dict):
    """Return the immutable footprint for callers that only need lock discovery."""
    return subinterface_reconcile_plan(device, payload).footprint


@mirror_reconciler
def reconcile_subinterface(device, payload: dict) -> list:
    """Create/update virtual subinterfaces + NSOSubinterfaceState from the payload."""
    with reconcile_transaction(subinterface_reconcile_plan(device, payload)):
        return _reconcile_subinterface(device, payload)


def _reconcile_subinterface(device, payload: dict) -> list:
    """Apply a subinterface mirror after its complete footprint is locked."""
    from dcim.models import Interface
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOSubinterfaceState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    # One query for the device's interfaces; resolve both the subinterface and its
    # parent from this map. Devices can carry thousands of subinterfaces (dev27 has
    # ~2160), so a per-row parent lookup would be thousands of extra queries.
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}

    now = timezone.now()
    rows: list = []
    for item in payload.get("interfaces", []):
        name = item.get("interface_name")
        if not name:
            continue
        iface = iface_map.get(name)
        if iface is None:
            iface = Interface(device=device, name=name, type="virtual")
            try:
                with transaction.atomic():
                    iface.save(force_insert=True)
            except IntegrityError:
                iface = Interface.objects.filter(device=device, name=name).first()
                if iface is None:
                    raise
            iface_map[name] = iface

        # Resolve the physical parent from the map; never create it (device sync owns it).
        device_parent_name = item.get("parent_interface") or ""
        parent = iface_map.get(device_parent_name)
        device_dot1q = item.get("dot1q_vlan")
        device_vrf = item.get("vrf") or ""
        state, _ = NSOSubinterfaceState.objects.get_or_create(management=management, interface=iface)
        if sm.is_owned(state.status):
            # Owned values are NetBox intent. A refresh compares the device read but
            # never restores the old dot1q/VRF/parent before Apply can push them.
            desired_parent_name = state.parent_interface.name if state.parent_interface else ""
            matches = (
                desired_parent_name == device_parent_name
                and state.dot1q_vlan == device_dot1q
                and state.vrf == device_vrf
            )
            state.status = sm.on_reconcile(state.status, matches=matches)
        else:
            if parent and iface.parent_id != parent.id:
                iface.parent = parent
                iface.save(update_fields=["parent"])
            state.parent_interface = parent
            state.dot1q_vlan = device_dot1q
            state.vrf = device_vrf
            # Parent presence is structural materialization, not device confirmation.
            state.status = sm.on_reconcile(state.status, matches=parent is not None, settles_owned=False)
        state.last_sync_at = now
        state.save()
        rows.append(state)

    # Overlay rows the device no longer reports (keep the dcim.Interface): NEVER hard-delete an
    # owned row (operator intent or an in-flight Apply marker). An unowned subinterface overlay is a pure device mirror with no
    # separate native config object, so a stale unowned row is a vestigial husk → drop it; owned
    # rows surface as drift (``changed``) instead of data-loss.
    reported = {item.get("interface_name") for item in payload.get("interfaces", [])}
    for stale in NSOSubinterfaceState.objects.filter(management=management).exclude(interface__name__in=reported):
        sm.finalise_stale_overlay(stale, vestigial=True, now=now)
    return rows
