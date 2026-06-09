# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Redistribution reconciler — moved from template_content.py (Track B, M20).

Also creates/links the netbox-routing Redistribution object: it resolves the
destination scope (OSPFInstance / ISISInstance / BGPAddressFamily) the routes are
redistributed into, then get_or_creates the Redistribution and links the
NSORedistributionState overlay row to it.
"""

import logging

logger = logging.getLogger(__name__)


def _resolve_redist_destination(device, dest_protocol: str, dest_ref: str):
    """Resolve the netbox_routing destination object for a redistribution entry.

    Destination is the scope the routes are redistributed INTO:
    - ospf → OSPFInstance(device, process_id)
    - isis → ISISInstance(device, process_tag)
    - bgp  → BGPAddressFamily, addressed by the adapter's ``<asn>/<vrf>/<afi>`` ref

    Returns the object or None (the protocol reconciles, which run first, create it).
    """
    try:
        from netbox_routing.models import BGPAddressFamily, BGPRouter, BGPScope, ISISInstance, OSPFInstance
    except ImportError:
        return None

    if dest_protocol == "ospf":
        try:
            pid = int(dest_ref)
        except (TypeError, ValueError):
            return None
        return OSPFInstance.objects.filter(device=device, process_id=pid).first()

    if dest_protocol == "isis":
        return ISISInstance.objects.filter(device=device, process_tag=dest_ref or "").first()

    if dest_protocol == "bgp":
        from dcim.models import Device as DcimDevice
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, VRF

        parts = (dest_ref or "").split("/")
        if len(parts) != 3:
            return None
        asn_s, vrf_s, afi = parts
        try:
            asn_obj = ASN.objects.filter(asn=int(asn_s)).first()
        except (TypeError, ValueError):
            return None
        if asn_obj is None or not afi:
            return None
        ct = ContentType.objects.get_for_model(DcimDevice)
        router = BGPRouter.objects.filter(assigned_object_type=ct, assigned_object_id=device.pk, asn=asn_obj).first()
        if router is None:
            return None
        vrf_obj = VRF.objects.filter(name=vrf_s).first() if vrf_s else None
        scope = BGPScope.objects.filter(router=router, vrf=vrf_obj).first()
        if scope is None:
            return None
        return BGPAddressFamily.objects.filter(scope=scope, address_family=afi).first()

    return None


def _create_or_link_redistribution(state, device, entry: dict):
    """Create (or link to) the netbox_routing.Redistribution for this state row.

    Resolves the destination scope + route-map, then get_or_creates the
    Redistribution keyed by (destination, source_protocol, source_ref) and links it.
    No-ops if netbox_routing is absent or the destination scope doesn't exist yet.
    """
    if state.redistribution_id is not None:
        return  # already linked

    try:
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import Redistribution, RouteMap
    except ImportError:
        return

    try:
        dest = _resolve_redist_destination(device, state.dest_protocol, state.dest_ref)
        if dest is None:
            return  # destination scope not modelled (yet) — leave status=imported

        route_map = None
        rm_name = entry.get("route_map") or ""
        if rm_name:
            route_map = RouteMap.objects.filter(name=rm_name).first()

        dct = ContentType.objects.get_for_model(dest.__class__)
        redist, _ = Redistribution.objects.get_or_create(
            destination_type=dct,
            destination_id=dest.pk,
            source_protocol=state.source_protocol,
            source_ref=state.source_ref,
            defaults={
                "route_map": route_map,
                "metric": entry.get("metric"),
                "metric_type": entry.get("metric_type") or "",
            },
        )
        state.redistribution = redist
        state.save(update_fields=["redistribution"])
    except Exception as exc:
        logger.debug("_create_or_link_redistribution: error for state %s: %s", state.pk, exc)


def reconcile_redistribution(device, payload: dict) -> list:
    """Reconcile redistribution data from the adapter into NSORedistributionState rows.

    ``payload`` is the response body from GET /api/v1/devices/{id}/redistribution.

    For each entry:
    - Find or create NSORedistributionState keyed by (management, dest_protocol, dest_ref,
      source_protocol, source_ref).
    - Update fields; set status='imported' if not in write-path status.
    - Attempt to link the FK to a netbox-routing Redistribution object.

    Stale rows: set status='changed'.
    Returns list of NSORedistributionState objects.
    """
    from django.utils import timezone

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSORedistributionState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    now = timezone.now()

    seen_keys: set[tuple] = set()
    for entry in payload.get("entries") or []:
        dest_proto = entry.get("dest_protocol") or ""
        dest_ref = entry.get("dest_ref") or ""
        src_proto = entry.get("source_protocol") or ""
        src_ref = entry.get("source_ref") or ""
        if not dest_proto or not src_proto:
            continue
        key = (dest_proto, dest_ref, src_proto, src_ref)
        state, _ = NSORedistributionState.objects.get_or_create(
            management=mgmt,
            dest_protocol=dest_proto,
            dest_ref=dest_ref,
            source_protocol=src_proto,
            source_ref=src_ref,
            defaults={"status": "unknown"},
        )
        state.route_map = entry.get("route_map") or ""
        state.metric = entry.get("metric")
        state.metric_type = entry.get("metric_type") or ""
        state.last_sync_at = now
        if not sm.is_owned(state.status) and state.status != "conflict":
            state.save()
            _create_or_link_redistribution(state, device, entry)
            state.refresh_from_db(fields=["redistribution"])
            # Mirror overlay: an unowned row rests at imported. Linking the
            # netbox-routing Redistribution is best-effort (an unmodelled destination
            # is benign, not drift), so it does not change the status.
            state.status = sm.on_reconcile(state.status, matches=None)
        state.save()
        seen_keys.add(key)

    # Mark stale rows (accepted/deploying intent preserved by on_reconcile).
    for stale in NSORedistributionState.objects.filter(management=mgmt):
        k = (stale.dest_protocol, stale.dest_ref, stale.source_protocol, stale.source_ref)
        if k not in seen_keys:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])

    return list(NSORedistributionState.objects.filter(management=mgmt))
