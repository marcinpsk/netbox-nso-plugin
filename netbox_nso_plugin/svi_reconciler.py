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

from .intent_state import mirror_reconciler, reconcile_transaction

logger = logging.getLogger(__name__)


def svi_reconcile_plan(device, payload: dict):
    """Declare one SVI refresh and whether it changes rendered membership."""
    from dcim.models import Interface
    from ipam.models import VLAN

    from .apply_state import vlan_ids_for_dependency_lock
    from .intent_state import MutationFootprint, ReconcileMutationPlan, SourceRow
    from .models import NSODeviceManagement, NSOSVIState

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return ReconcileMutationPlan(MutationFootprint())
    raw_items = payload.get("interfaces", []) if isinstance(payload, dict) else []
    items = raw_items if isinstance(raw_items, list) else []
    vids = vlan_ids_for_dependency_lock(items)

    interfaces = tuple(Interface.objects.filter(device=device).order_by("pk"))
    states = tuple(NSOSVIState.objects.filter(management=management).select_related("interface").order_by("pk"))
    vlan_ids = {state.vlan_id for state in states if state.vlan_id is not None}
    vlan_ids.update(VLAN.objects.filter(group__slug=f"nso-{device.pk}", vid__in=vids).values_list("pk", flat=True))
    reported = {item.get("interface_name") for item in items if isinstance(item, dict) and item.get("interface_name")}
    changes_content = any(state.status == "in_sync" and state.interface.name not in reported for state in states)
    return ReconcileMutationPlan(
        MutationFootprint.for_keys(
            {(device.pk, "svi")},
            shared_keys=(("vlan", str(vlan_id)) for vlan_id in vlan_ids),
            source_rows=(
                SourceRow("dcim.device", device.pk),
                SourceRow("dcim.interface", None),
                *(SourceRow("dcim.interface", interface.pk) for interface in interfaces),
                *(SourceRow("ipam.vlan", vlan_id) for vlan_id in vlan_ids),
            ),
            overlay_rows=(
                SourceRow("netbox_nso_plugin.nsosvistate", None),
                *(SourceRow(state._meta.label_lower, state.pk) for state in states),
            ),
        ),
        changes_content=changes_content,
    )


def svi_reconcile_footprint(device, payload: dict):
    """Return the immutable footprint for callers that only need lock discovery."""
    return svi_reconcile_plan(device, payload).footprint


@mirror_reconciler
def reconcile_svi(device, payload: dict) -> list:
    """Create/update virtual SVI/IRB interfaces + NSOSVIState from the adapter payload."""
    with reconcile_transaction(svi_reconcile_plan(device, payload)):
        return _reconcile_svi(device, payload)


def _reconcile_svi(device, payload: dict) -> list:
    """Apply an SVI mirror after its complete footprint is locked."""
    from dcim.models import Interface
    from django.db import IntegrityError, transaction
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
        iface = Interface.objects.filter(device=device, name=name).first()
        if iface is None:
            iface = Interface(device=device, name=name, type="virtual")
            try:
                with transaction.atomic():
                    iface.save(force_insert=True)
            except IntegrityError:
                iface = Interface.objects.filter(device=device, name=name).first()
                if iface is None:
                    raise
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
