# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M34: reconcile the adapter VLAN-database + switchport payloads into NetBox.

VLANs are reconciled into a per-device ``ipam.VLANGroup`` (slug ``nso-{device.pk}``)
so imported vids are scoped per device (NetBox enforces UNIQUE(group, vid) — two
switches can both have VLAN 10 without colliding). Switchports compare the NSO-
observed mode/untagged/tagged against the LIVE NetBox interface to compute drift;
the overlay carries status, native L2 fields stay the source of truth.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# NSO emits access/trunk/trunk-all; NetBox interface modes are access/tagged/tagged-all.
_NSO_TO_NETBOX_MODE = {"access": "access", "trunk": "tagged", "trunk-all": "tagged-all"}


def _device_vlan_group(device):
    """Per-device VLAN group so imported vids are scoped to the device."""
    from ipam.models import VLANGroup

    group, _ = VLANGroup.objects.get_or_create(slug=f"nso-{device.pk}", defaults={"name": f"NSO {device.name}"})
    return group


def reconcile_vlan_database(device, payload: dict) -> list:
    """Upsert ipam.VLAN (per-device group) + NSOVLANState from the adapter payload."""
    from django.utils import timezone
    from ipam.models import VLAN

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOVLANState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    group = _device_vlan_group(device)
    now = timezone.now()
    rows: list = []
    seen_vids: set[int] = set()
    for item in payload.get("vlans", []) or []:
        vid = int(item["vlan_id"])
        seen_vids.add(vid)
        name = item.get("name") or ""
        # Seed the name on first import only. NEVER clobber it afterwards: the
        # NetBox VLAN name is operator-editable, and overwriting it back to the
        # device value would silently revert (and hide) an operator rename.
        vlan, _ = VLAN.objects.get_or_create(group=group, vid=vid, defaults={"name": name})
        state, _ = NSOVLANState.objects.get_or_create(management=management, vlan=vlan)
        state.last_sync_at = now
        state.device_name = name  # mirror the device value for drift display
        # Value overlay: the editable value is the VLAN name. A device with no name
        # has nothing to drift against, so treat that as a match. The unified machine
        # then settles owned→in_sync (or re-pends to accepted) and rests unowned at
        # imported (or changed on a real rename divergence).
        matches = (not name) or vlan.name == name
        state.status = sm.on_reconcile(state.status, matches=matches)
        state.save()
        rows.append(state)

    # rows the payload no longer reports → drift
    for stale in NSOVLANState.objects.filter(management=management, vlan__group=group):
        if stale.vlan.vid not in seen_vids:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])
    return rows


def reconcile_switchport(device, payload: dict) -> list:
    """Upsert NSOSwitchportState; status = in_sync iff the live NetBox interface matches NSO."""
    from django.utils import timezone
    from ipam.models import VLAN

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOSwitchportState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    group = _device_vlan_group(device)
    now = timezone.now()
    rows: list = []
    seen: set[int] = set()
    for item in payload.get("interfaces", []) or []:
        try:
            interface = device.interfaces.get(name=item["interface_name"])
        except Exception:
            continue

        nso_mode = _NSO_TO_NETBOX_MODE.get(item.get("mode") or "", "")
        nso_untagged = item.get("untagged_vlan")
        nso_tagged = sorted(item.get("tagged_vlans") or [])

        state, _ = NSOSwitchportState.objects.get_or_create(management=management, interface=interface)
        state.mode = nso_mode
        state.untagged_vlan = (
            VLAN.objects.filter(group=group, vid=nso_untagged).first() if nso_untagged is not None else None
        )
        state.last_sync_at = now
        state.save()
        state.tagged_vlans.set(VLAN.objects.filter(group=group, vid__in=nso_tagged))

        nb_untagged = interface.untagged_vlan.vid if interface.untagged_vlan else None
        nb_tagged = sorted(interface.tagged_vlans.values_list("vid", flat=True))
        # Value overlay: the editable value is the live NetBox L2 config (mode +
        # untagged + tagged) compared against the device-observed config.
        matches = (interface.mode or "") == nso_mode and nb_untagged == nso_untagged and nb_tagged == nso_tagged
        state.status = sm.on_reconcile(state.status, matches=matches)
        state.save()
        rows.append(state)
        seen.add(interface.pk)

    for stale in NSOSwitchportState.objects.filter(management=management):
        if stale.interface_id not in seen:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])
    return rows
