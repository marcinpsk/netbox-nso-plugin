# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile per-interface BFD from the adapter into netbox_routing.

BFD is an interface property; the timer profile is usually ONE shared template
across the network, so we dedupe BFDProfile by its timer-set (a deterministic
``bfd-<tx>-<rx>-x<mult>`` name reused everywhere) and link each BFDInterface to
it. micro-BFD (RFC 7130, per-LAG-member) vs normal is recorded on BFDInterface.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _get_or_create_bfd_profile(entry: dict, BFDProfile, cache: dict):
    """Return a shared BFDProfile for this entry's timer-set, or None.

    BFDProfile timers are required + range-validated (tx/rx 60-60000, mult 0-255);
    when the device didn't expose all three (or they're out of range) we can't
    build a valid profile, so the BFDInterface is left without one.
    """
    tx, rx, mult = entry.get("min_tx"), entry.get("min_rx"), entry.get("multiplier")
    if tx is None or rx is None or mult is None:
        return None
    if not (60 <= tx <= 60000 and 60 <= rx <= 60000 and 0 <= mult <= 255):
        return None
    name = f"bfd-{tx}-{rx}-x{mult}"
    if name in cache:
        return cache[name]
    obj, _ = BFDProfile.objects.get_or_create(
        name=name,
        defaults={"min_tx_int": tx, "min_rx_int": rx, "multiplier": mult},
    )
    cache[name] = obj
    return obj


def _upsert_bfd_state(mgmt, iface, entry: dict, now) -> None:
    """Mirror one interface's device-observed BFD timers + lifecycle status into the overlay.

    Fresh import lands 'imported'; owned statuses are preserved, except 'deploying'
    (Apply in flight) → 'in_sync' once the device re-reports BFD here (apply landed).
    """
    from . import status_machine as sm
    from .models import NSOBFDInterfaceState

    state, _ = NSOBFDInterfaceState.objects.get_or_create(management=mgmt, interface=iface)
    state.min_tx = entry.get("min_tx")
    state.min_rx = entry.get("min_rx")
    state.multiplier = entry.get("multiplier")
    state.micro_bfd = bool(entry.get("micro_bfd", False))
    # Mirror overlay: imported on import; owned preserved; deploying→in_sync on apply.
    state.status = sm.on_reconcile(state.status, matches=None)
    state.last_sync_at = now
    state.save()


def reconcile_bfd(device, interfaces: list) -> list:
    """Create/update BFDInterface rows for *device* from the adapter BFD payload.

    Maps each entry to a NetBox dcim.Interface (by name, then Nokia bound-port),
    dedupes the shared BFDProfile, and prunes BFDInterface rows whose interface is
    no longer reported. Returns the BFDInterface instances for this device.
    """
    try:
        from netbox_routing.models import BFDInterface, BFDProfile
    except ImportError:
        logger.warning("netbox_routing not installed; skipping BFD reconcile")
        return []

    from dcim.models import Interface
    from django.utils import timezone

    from .models import NSOBFDInterfaceState, NSODeviceManagement

    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    profiles: dict = {}
    seen_iface_ids: set = set()
    mgmt = NSODeviceManagement.objects.filter(device=device).first()
    now = timezone.now()
    seen_state_iface_ids: set = set()

    for entry in interfaces or []:
        name = entry.get("interface_name") or ""
        if not name:
            continue
        iface = iface_map.get(name)
        if iface is None:
            bound = entry.get("bound_port")
            if bound:
                iface = iface_map.get(bound)
        if iface is None:
            continue  # interface not modelled in NetBox

        profile = _get_or_create_bfd_profile(entry, BFDProfile, profiles)
        desired = {
            "bfd_profile": profile,
            "micro_bfd": bool(entry.get("micro_bfd", False)),
            "enabled": bool(entry.get("enabled", True)),
        }
        obj, created = BFDInterface.objects.get_or_create(interface=iface, defaults=desired)
        if not created:
            changed = False
            if obj.bfd_profile_id != (profile.pk if profile else None):
                obj.bfd_profile = profile
                changed = True
            for field in ("micro_bfd", "enabled"):
                if getattr(obj, field) != desired[field]:
                    setattr(obj, field, desired[field])
                    changed = True
            if changed:
                obj.save()
        seen_iface_ids.add(iface.pk)

        # Write-path overlay: mirror the device-observed timers + status so an
        # operator can accept/own and push BFD back (preserve owned statuses).
        if mgmt is not None:
            _upsert_bfd_state(mgmt, iface, entry, now)
            seen_state_iface_ids.add(iface.pk)

    # Prune BFDInterface rows for this device's interfaces no longer reporting BFD.
    BFDInterface.objects.filter(interface__device=device).exclude(interface_id__in=seen_iface_ids).delete()
    if mgmt is not None:
        NSOBFDInterfaceState.objects.filter(management=mgmt).exclude(interface_id__in=seen_state_iface_ids).delete()

    return list(BFDInterface.objects.filter(interface__device=device).select_related("interface", "bfd_profile"))
