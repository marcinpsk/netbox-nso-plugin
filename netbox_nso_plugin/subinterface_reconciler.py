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

    from .models import NSODeviceManagement, NSOSubinterfaceState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    now = timezone.now()
    rows: list = []
    for item in payload.get("interfaces", []):
        name = item.get("interface_name")
        if not name:
            continue
        iface, _ = Interface.objects.get_or_create(device=device, name=name, defaults={"type": "virtual"})

        # Look up the physical parent by name; never create it (device sync owns it).
        parent_name = item.get("parent_interface")
        parent = Interface.objects.filter(device=device, name=parent_name).first() if parent_name else None
        if parent and iface.parent_id != parent.id:
            iface.parent = parent
            iface.save(update_fields=["parent"])

        state, _ = NSOSubinterfaceState.objects.get_or_create(management=management, interface=iface)
        state.parent_interface = parent
        state.dot1q_vlan = item.get("dot1q_vlan")
        state.vrf = item.get("vrf") or ""
        # Never clobber operator-owned statuses (the write-path lifecycle). A fresh
        # import lands as 'imported'; a missing parent is flagged 'changed' for review.
        if state.status not in ("accepted", "deploying", "in_sync"):
            state.status = "imported" if parent is not None else "changed"
        state.last_sync_at = now
        state.save()
        rows.append(state)

    # Prune overlay rows the device no longer reports (keep the dcim.Interface).
    reported = {item.get("interface_name") for item in payload.get("interfaces", [])}
    NSOSubinterfaceState.objects.filter(management=management).exclude(interface__name__in=reported).delete()
    return rows
