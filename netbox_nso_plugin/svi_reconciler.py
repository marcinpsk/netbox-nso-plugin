# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile L3 VLAN interfaces (SVIs / IRBs) from NSO into NetBox.

Materialises the virtual ``dcim.Interface`` (type=virtual), links it to its VLAN
(via per-device VLAN group), and tracks ``NSOSVIState``. IP addresses are
NOT handled here — they ride the interface-IP path on the same interface, so
this reconcile MUST run before ``_reconcile_interface_ips`` (which only attaches
IPs to interfaces that already exist).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def lock_svi_reconcile_dependencies(device, payload: dict) -> None:
    """Lock native VLANs before the device lock and SVI overlay writes."""
    from .models import NSOSVIState
    from .vlan_reconciler import _lock_reconcile_vlan_dependencies

    def collect_overlay_vlan_ids(management, _vids):
        return NSOSVIState.objects.filter(management=management, vlan__isnull=False).values_list(
            "vlan_id",
            flat=True,
        )

    _lock_reconcile_vlan_dependencies(
        device,
        payload,
        payload_key="interfaces",
        vid_fields=("vlan_id",),
        collect_overlay_vlan_ids=collect_overlay_vlan_ids,
    )


def reconcile_svi(device, payload: dict) -> list:
    """Create/update virtual SVI/IRB interfaces + NSOSVIState from the adapter payload."""
    from dcim.models import Interface
    from django.utils import timezone
    from ipam.models import VLAN

    from . import status_machine as sm
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
        device_type = item.get("type") or "svi"
        device_vrf = item.get("vrf") or ""
        state, _ = NSOSVIState.objects.get_or_create(management=management, interface=iface)
        if sm.is_owned(state.status):
            # Owned values are NetBox intent. Compare the device read to them without
            # replacing them; otherwise a refresh between inline edit and Apply silently
            # restores the old device VRF and the pending change is lost.
            desired_vid = state.vlan.vid if state.vlan else None
            matches = desired_vid == vid and state.svi_type == device_type and state.vrf == device_vrf
            state.status = sm.on_reconcile(state.status, matches=matches)
        else:
            # Unowned rows are device mirrors and continue tracking every reported value.
            state.vlan = vlan
            state.svi_type = device_type
            state.vrf = device_vrf
            state.status = sm.on_reconcile(state.status, matches=None)
        state.last_sync_at = now
        state.save()
        rows.append(state)

    # SVI states the device no longer reports: NEVER hard-delete an owned row (operator
    # intent or an in-flight Apply marker). An unowned
    # SVI overlay is a pure device mirror with no separate native config object (the virtual
    # interface is kept regardless), so a stale unowned row is a vestigial husk → drop it to
    # avoid orphan churn; owned rows surface as drift (``changed``) instead of data-loss.
    reported = {item.get("interface_name") for item in payload.get("interfaces", [])}
    for stale in NSOSVIState.objects.filter(management=management).exclude(interface__name__in=reported):
        sm.finalise_stale_overlay(stale, vestigial=True, now=now)
    return rows
