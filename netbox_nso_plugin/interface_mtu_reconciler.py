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

import logging

logger = logging.getLogger(__name__)


def reconcile_interface_mtu(device, payload: dict) -> list:
    """Create/update NSOInterfaceMtuState rows from the payload."""
    from dcim.models import Interface
    from django.utils import timezone

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOInterfaceMtuState

    try:
        management = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    # One query for the device's interfaces; match each payload entry by name.
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}

    now = timezone.now()
    rows: list = []
    matched_names: set[str] = set()
    for item in payload.get("interfaces", []):
        name = item.get("interface_name")
        if not name:
            continue
        iface = iface_map.get(name)
        if iface is None:
            # MTU is an attribute on an existing interface — skip if the interface
            # isn't in NetBox yet (device/interface sync owns interface creation).
            continue
        matched_names.add(name)

        state, _ = NSOInterfaceMtuState.objects.get_or_create(management=management, interface=iface)
        dev_mtu, dev_ip, dev_mpls = item.get("mtu"), item.get("ip_mtu"), item.get("mpls_mtu")
        state.bound_port = item.get("bound_port") or ""

        if sm.is_owned(state.status):
            # Owned: the operator's MTU values are the intent — never clobber them with
            # the device read. Settle by device-vs-intent (deploying → in_sync once the
            # device reflects the pushed values; accepted holds until then).
            matches = dev_mtu == state.l2_mtu and dev_ip == state.ip_mtu and dev_mpls == state.mpls_mtu
            state.status = sm.on_reconcile(state.status, matches=matches)
        else:
            # Unowned mirror: track the device values for display.
            state.l2_mtu, state.ip_mtu, state.mpls_mtu = dev_mtu, dev_ip, dev_mpls
            state.status = sm.on_reconcile(state.status, matches=None)
        state.last_sync_at = now
        state.save()
        rows.append(state)

    # Rows the device no longer reports an MTU on: owned rows are operator intent
    # (the device may simply not have caught up yet) → transition via present=False
    # (keeps accepted/deploying, else → changed); unowned vestigial rows are pruned.
    for stale in NSOInterfaceMtuState.objects.filter(management=management).exclude(interface__name__in=matched_names):
        if sm.is_owned(stale.status):
            stale.status = sm.on_reconcile(stale.status, present=False)
            stale.last_sync_at = now
            stale.save(update_fields=["status", "last_sync_at"])
        else:
            stale.delete()
    return rows
