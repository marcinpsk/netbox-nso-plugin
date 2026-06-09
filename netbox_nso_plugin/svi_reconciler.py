# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M35: reconcile L3 VLAN interfaces (SVIs / IRBs) from NSO into NetBox.

Materialises the virtual ``dcim.Interface`` (type=virtual), links it to its VLAN
(via M34's per-device VLAN group), and tracks ``NSOSVIState``. IP addresses are
NOT handled here — they ride the M12 interface-IP path on the same interface, so
this reconcile MUST run before ``_reconcile_interface_ips`` (which only attaches
IPs to interfaces that already exist).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def reconcile_svi(device, payload: dict) -> list:
    """Create/update virtual SVI/IRB interfaces + NSOSVIState from the adapter payload."""
    from dcim.models import Interface
    from django.utils import timezone
    from ipam.models import VLAN

    from .models import NSODeviceManagement, NSOSVIState
    from .vlan_reconciler import _device_vlan_group

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    group = _device_vlan_group(device)
    now = timezone.now()
    rows: list = []
    for item in payload.get("interfaces", []):
        name = item.get("interface_name")
        if not name:
            continue
        iface, _ = Interface.objects.get_or_create(device=device, name=name, defaults={"type": "virtual"})
        vid = item.get("vlan_id")
        vlan = VLAN.objects.filter(group=group, vid=vid).first() if vid else None
        state, _ = NSOSVIState.objects.get_or_create(management=management, interface=iface)
        state.vlan = vlan
        state.svi_type = item.get("type") or "svi"
        state.status = "in_sync"
        state.last_sync_at = now
        state.save()
        rows.append(state)

    # Prune SVI states the device no longer reports (and their virtual interfaces
    # if we own them and they carry no IPs/other use is out of scope — keep the
    # interface, just drop the overlay row to avoid orphan churn).
    reported = {item.get("interface_name") for item in payload.get("interfaces", [])}
    NSOSVIState.objects.filter(management=management).exclude(interface__name__in=reported).delete()
    return rows
