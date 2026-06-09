# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M36: reconcile dot1q L3 subinterfaces from NSO into NetBox.

Materialises the virtual ``dcim.Interface`` (type=virtual), links it to its
physical parent via ``Interface.parent`` (looked up by name — never created
here; the parent comes from normal device sync / the interface export), records
the interface-local dot1q encapsulation tag on the overlay (NOT an ``ipam.VLAN``),
and tracks ``NSOSubinterfaceState``. IP addresses are NOT handled here — they
ride the M12 interface-IP path on the same interface, so this reconcile MUST run
before ``_reconcile_interface_ips`` (which only attaches IPs to interfaces that
already exist).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def reconcile_subinterface(device, payload: dict) -> list:
    """Create/update virtual subinterfaces + NSOSubinterfaceState from the payload."""
    from dcim.models import Interface
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
            iface = Interface.objects.create(device=device, name=name, type="virtual")
            iface_map[name] = iface

        # Resolve the physical parent from the map; never create it (device sync owns it).
        parent = iface_map.get(item.get("parent_interface"))
        if parent and iface.parent_id != parent.id:
            iface.parent = parent
            iface.save(update_fields=["parent"])

        state, _ = NSOSubinterfaceState.objects.get_or_create(management=management, interface=iface)
        state.parent_interface = parent
        state.dot1q_vlan = item.get("dot1q_vlan")
        state.vrf = item.get("vrf") or ""
        # 'parent present' is structural materialization, not device confirmation, so
        # it must not settle an owned row (settles_owned=False): unowned → imported
        # (ok) / changed (no parent); owned preserved, settling only via Apply.
        state.status = sm.on_reconcile(state.status, matches=parent is not None, settles_owned=False)
        state.last_sync_at = now
        state.save()
        rows.append(state)

    # Prune overlay rows the device no longer reports (keep the dcim.Interface).
    reported = {item.get("interface_name") for item in payload.get("interfaces", [])}
    NSOSubinterfaceState.objects.filter(management=management).exclude(interface__name__in=reported).delete()
    return rows
