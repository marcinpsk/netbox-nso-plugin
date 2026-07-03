# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile Nokia L2 services into native vpn.L2VPN + L2VPNTermination.

Read → model (no device write). Each epipe/vpls service becomes a per-device-scoped
``vpn.L2VPN`` (epipe→VPWS, vpls→VPLS); each SAP a ``vpn.L2VPNTermination`` on its port
interface; and ``NSOL2SapState`` carries the status/drift, the operator-accept marker,
and the per-SAP dot1q encap (which has no home on the native termination — the tag is
interface-local encap, not an ipam.VLAN; see SPIKE-FINDINGS-nokia-l2.md).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# SR OS service-type → NetBox vpn.L2VPNTypeChoices code.
_L2VPN_TYPE = {"epipe": "vpws", "vpls": "vpls"}


def _upsert_l2vpn(L2VPN, device, service_name: str, service_type: str, service_id):
    """get_or_create the per-device-scoped L2VPN and keep type/identifier in sync."""
    l2vpn_type = _L2VPN_TYPE.get(service_type, "vpls")
    slug = f"nso-{device.pk}-{service_name}"
    l2vpn, _ = L2VPN.objects.get_or_create(
        slug=slug,
        defaults={"name": f"{device.name}: {service_name}", "type": l2vpn_type, "identifier": service_id},
    )
    fields = []
    if l2vpn.type != l2vpn_type:
        l2vpn.type = l2vpn_type
        fields.append("type")
    if service_id is not None and l2vpn.identifier != service_id:
        l2vpn.identifier = service_id
        fields.append("identifier")
    if fields:
        l2vpn.save(update_fields=fields)
    return l2vpn


def _reconcile_sap(NSOL2SapState, L2VPNTermination, mgmt, l2vpn, svc, sap, iface_map, iface_ct, now):
    """Upsert one SAP's NSOL2SapState + its L2VPNTermination; set status."""
    from . import status_machine as sm

    state, _ = NSOL2SapState.objects.get_or_create(
        management=mgmt,
        service_name=svc["service_name"],
        sap_id=sap["sap_id"],
        defaults={"service_type": svc.get("service_type", ""), "port": sap.get("port", "")},
    )
    state.service_type = svc.get("service_type", "")
    state.service_id = svc.get("service_id")
    state.port = sap.get("port", "")
    state.outer_tag = sap.get("outer_tag")
    state.inner_tag = sap.get("inner_tag")
    state.l2vpn = l2vpn
    state.last_sync_at = now

    iface = iface_map.get(state.port)
    conflict = False
    if iface is None:
        # Port not present in NetBox — can't terminate; adoption ambiguity.
        conflict = True
        state.termination = None
    else:
        term = L2VPNTermination.objects.filter(assigned_object_type=iface_ct, assigned_object_id=iface.pk).first()
        if term is not None and term.l2vpn_id != l2vpn.pk:
            # The port already terminates on a different L2VPN (NetBox enforces one).
            conflict = True
            state.termination = None
        else:
            if term is None:
                term = L2VPNTermination(l2vpn=l2vpn, assigned_object=iface)
                term.save()
            state.termination = term
    # FK overlay: 'matches'=termination materialized (not device confirmation) →
    # settles_owned=False. Unowned: conflict→conflict, else imported. Owned preserved.
    state.status = sm.on_reconcile(state.status, matches=not conflict, conflict=conflict, settles_owned=False)
    state.save()


def _retire_stale_l2_saps(NSOL2SapState, mgmt, seen: set, now) -> None:
    """Mark SAP rows the payload no longer reports as 'changed' (drift).

    Native L2VPN/termination objects are left intact — clobber-safe; the operator reviews
    and P2b's write path handles removal.
    """
    from . import status_machine as sm

    for state in NSOL2SapState.objects.filter(management=mgmt):
        if (state.service_name, state.sap_id) in seen:
            continue
        new_status = sm.on_reconcile(state.status, present=False)
        if new_status != state.status:
            state.status = new_status
            state.last_sync_at = now
            state.save(update_fields=["status", "last_sync_at"])


def reconcile_l2_services(device, payload: dict) -> list:
    """Reconcile the adapter L2-service payload into L2VPN/termination + NSOL2SapState.

    Returns all current NSOL2SapState rows for the device. No-op (``[]``) when the device
    has no NSO management or the ``vpn`` app is unavailable.
    """
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    from .models import NSOL2SapState

    try:
        from vpn.models import L2VPN, L2VPNTermination
    except Exception:
        return []

    mgmt = getattr(device, "nso_management", None)
    if mgmt is None:
        return []

    now = timezone.now()
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    iface_ct = ContentType.objects.get_for_model(Interface)
    seen: set = set()

    for svc in payload.get("services", []) or []:
        service_name = svc.get("service_name")
        if not service_name:
            continue
        l2vpn = _upsert_l2vpn(L2VPN, device, service_name, svc.get("service_type", ""), svc.get("service_id"))
        for sap in svc.get("saps", []) or []:
            if not sap.get("sap_id"):
                continue
            seen.add((service_name, sap["sap_id"]))
            _reconcile_sap(NSOL2SapState, L2VPNTermination, mgmt, l2vpn, svc, sap, iface_map, iface_ct, now)

    _retire_stale_l2_saps(NSOL2SapState, mgmt, seen, now)
    return list(NSOL2SapState.objects.filter(management=mgmt).select_related("l2vpn", "termination"))
