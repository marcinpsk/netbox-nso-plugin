# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile LACP/LAG configuration from the adapter into overlay state models.

Read → model (no device write). Each bundle reported by the adapter's
``GET /api/v1/devices/{id}/lag-config`` becomes an ``NSOLACPBundleState`` on the
LAG ``dcim.Interface``; each member an ``NSOLACPMemberState`` on its physical
interface, linked back to the LAG via ``lag_bundle``.

Follows the established overlay-reconcile convention (see
``_reconcile_isis_interfaces`` / ``reconcile_l2_services``): keyed by
(management, interface); refresh the NSO-reported fields on every read; set
status ``imported`` unless the operator has advanced it into a write-path state
(accepted/deploying/in_sync — never clobbered); rows the payload no longer
reports are marked ``changed`` (drift). Never raises — interfaces absent from
NetBox are logged and skipped.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def reconcile_lag_config(device, payload: dict) -> list:
    """Upsert NSOLACPBundleState + NSOLACPMemberState from the adapter lag-config payload.

    ``payload`` is the JSON body of GET /api/v1/devices/{id}/lag-config. Returns
    all current NSOLACPBundleState rows for the device (``[]`` if the device has
    no NSO management).
    """
    from dcim.models import Interface
    from django.utils import timezone

    from . import status_machine as sm
    from .models import (
        NSODeviceManagement,
        NSOLACPBundleState,
        NSOLACPMemberState,
    )

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    bundles = payload.get("bundles", []) or []
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    now = timezone.now()
    seen_bundles: set[int] = set()
    seen_members: set[int] = set()
    dropped: list[str] = []

    for bundle_data in bundles:
        bundle_name = bundle_data.get("name") or ""
        if not bundle_name:
            continue
        lag_iface = iface_map.get(bundle_name)
        if lag_iface is None:
            dropped.append(bundle_name)
            continue

        state, _ = NSOLACPBundleState.objects.get_or_create(
            management=mgmt,
            interface=lag_iface,
            defaults={"status": "unknown"},
        )
        state.lag_id = bundle_data.get("lag_id")
        state.min_links = bundle_data.get("min_links")
        state.system_priority = bundle_data.get("system_priority")
        state.system_id = bundle_data.get("system_id") or ""
        state.timer = bundle_data.get("timer") or ""
        state.admin_key = bundle_data.get("admin_key")
        state.last_sync_at = now
        state.status = sm.on_reconcile(state.status, matches=None)  # mirror overlay
        state.save()
        seen_bundles.add(lag_iface.pk)

        for member_data in bundle_data.get("members", []) or []:
            iface_name = member_data.get("interface_name") or ""
            if not iface_name:
                continue
            member_iface = iface_map.get(iface_name)
            if member_iface is None:
                dropped.append(iface_name)
                continue

            m_state, _ = NSOLACPMemberState.objects.get_or_create(
                management=mgmt,
                interface=member_iface,
                defaults={"status": "unknown"},
            )
            m_state.lag_bundle = lag_iface
            m_state.mode = member_data.get("mode") or ""
            m_state.port_priority = member_data.get("port_priority")
            m_state.last_sync_at = now
            m_state.status = sm.on_reconcile(m_state.status, matches=None)  # mirror overlay
            m_state.save()
            seen_members.add(member_iface.pk)

    # Rows the payload no longer reports → prune vestigial husks, else drift (clobber-safe;
    # native interfaces untouched). A stale bundle is vestigial when its LAG interface has
    # no members left; a stale member when its interface is no longer assigned to any LAG.
    for stale in NSOLACPBundleState.objects.filter(management=mgmt):
        if stale.interface_id not in seen_bundles:
            vestigial = not Interface.objects.filter(lag_id=stale.interface_id).exists()
            sm.finalise_stale_overlay(stale, vestigial=vestigial, now=now)
    for stale in NSOLACPMemberState.objects.filter(management=mgmt).select_related("interface"):
        if stale.interface_id not in seen_members:
            sm.finalise_stale_overlay(stale, vestigial=stale.interface.lag_id is None, now=now)

    if dropped:
        logger.warning(
            "LACP reconcile for %s: %d interface(s) not found in NetBox, dropped: %s",
            device,
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )

    return list(NSOLACPBundleState.objects.filter(management=mgmt).select_related("interface"))
