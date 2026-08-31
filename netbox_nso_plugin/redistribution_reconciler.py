# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Redistribution reconciler — moved from template_content.py (Track B).

Also creates/links the netbox-routing Redistribution object: it resolves the
destination scope (OSPFInstance / ISISInstance / BGPAddressFamily) the routes are
redistributed into, then get_or_creates the Redistribution and links the
NSORedistributionState overlay row to it.
"""

import logging

from .intent_state import mirror_reconciler

logger = logging.getLogger(__name__)

_DESTINATION_PROTOCOLS = ("bgp", "isis", "ospf")


def redistribution_reconcile_plan(device, payload: dict):
    """Declare every native and overlay row one redistribution refresh can write."""
    import copy
    from collections import Counter

    from django.db.models import Count

    from . import status_machine as sm
    from .intent_state import MutationFootprint, ReconcileMutationPlan, SourceRow, canonical_fragment
    from .models import NSODeviceManagement, NSORedistributionState

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return ReconcileMutationPlan(MutationFootprint())
    states = tuple(
        NSORedistributionState.objects.filter(management=management).select_related("redistribution").order_by("pk")
    )
    reported = {
        (
            entry.get("dest_protocol") or "",
            entry.get("dest_ref") or "",
            entry.get("source_protocol") or "",
            entry.get("source_ref") or "",
        ): entry
        for entry in payload.get("entries", []) or []
        if isinstance(entry, dict) and entry.get("dest_protocol") and entry.get("source_protocol")
    }
    protocols = {
        protocol
        for protocol in (
            *(state.dest_protocol for state in states),
            *(key[0] for key in reported),
        )
        if protocol in _DESTINATION_PROTOCOLS
    }
    redistribution_ids = {state.redistribution_id for state in states if state.redistribution_id is not None}
    reference_counts = dict(
        NSORedistributionState.objects.filter(redistribution_id__in=redistribution_ids)
        .values("redistribution_id")
        .annotate(count=Count("pk"))
        .values_list("redistribution_id", "count")
    )
    stale_unowned_counts = Counter(
        state.redistribution_id
        for state in states
        if state.redistribution_id is not None
        and not sm.is_owned(state.status)
        and (state.dest_protocol, state.dest_ref, state.source_protocol, state.source_ref) not in reported
    )
    changes_content = False
    for state in states:
        key = (state.dest_protocol, state.dest_ref, state.source_protocol, state.source_ref)
        if key not in reported:
            candidate = copy.copy(state)
            candidate.status = sm.on_reconcile(state.status, present=False)
            if canonical_fragment(state) != canonical_fragment(candidate):
                changes_content = True
                break
            if state.redistribution_id is not None and stale_unowned_counts[
                state.redistribution_id
            ] == reference_counts.get(state.redistribution_id, 0):
                changes_content = True
                break
        elif sm.is_owned(state.status) and state.redistribution_id is None:
            if _resolve_redist_destination(device, state.dest_protocol, state.dest_ref) is not None:
                changes_content = True
                break
    footprint = MutationFootprint.for_keys(
        {(device.pk, protocol) for protocol in protocols},
        source_rows=(
            SourceRow("netbox_routing.redistribution", None),
            *(SourceRow("netbox_routing.redistribution", pk) for pk in redistribution_ids),
        ),
        overlay_rows=(
            SourceRow("netbox_nso_plugin.nsoredistributionstate", None),
            *(SourceRow(state._meta.label_lower, state.pk) for state in states),
        ),
    )
    return ReconcileMutationPlan(footprint, changes_content=changes_content)


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


def _redist_metric_type(entry: dict) -> str:
    """Return only configured intent; absence must stay absent through Apply."""
    if "metric_type" in entry:
        return entry.get("metric_type") or ""
    return ""


def _redist_device_content(entry: dict, route_map, device) -> dict:
    """Canonical device-desired Redistribution content (route_map as pk)."""
    from . import merge_util

    return {
        "route_map": merge_util.pk(route_map),
        "metric": entry.get("metric"),
        "metric_type": _redist_metric_type(entry),
    }


def _redist_object_content(redist) -> dict:
    """Canonical content read back from the netbox-routing Redistribution object."""
    return {
        "route_map": redist.route_map_id,
        "metric": redist.metric,
        "metric_type": redist.metric_type or "",
    }


def _redist_overlay_matches_device(state, entry: dict) -> bool:
    """Compare owned overlay intent with the device row without requiring an FK."""
    return (
        state.route_map == (entry.get("route_map") or "")
        and state.metric == entry.get("metric")
        and (state.metric_type or "") == _redist_metric_type(entry)
    )


def _write_redist(redist, route_map, entry: dict, device) -> None:
    """Write the device-desired values onto the Redistribution (seed / auto-mirror)."""
    redist.route_map = route_map
    redist.metric = entry.get("metric")
    fields = ["route_map", "metric"]
    redist.metric_type = _redist_metric_type(entry)
    fields.append("metric_type")
    redist.save(update_fields=fields)


def _create_or_link_redistribution(state, device, entry: dict) -> tuple[bool | None, bool]:
    """3-way reconcile the netbox_routing.Redistribution for this state row.

    Returns ``(matches, conflict)``. Device-side changes auto-mirror when the object is
    untouched; operator edits survive and surface as 'changed'; both-moved → conflict.
    """
    from . import merge_util

    try:
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import Redistribution, RouteMap
    except ImportError:
        return None, False

    try:
        dest = _resolve_redist_destination(device, state.dest_protocol, state.dest_ref)
        if dest is None:
            return None, False  # destination scope not modelled (yet) — comparison is incomplete

        route_map = (
            RouteMap.objects.filter(name=entry.get("route_map") or "").first() if entry.get("route_map") else None
        )
        dct = ContentType.objects.get_for_model(dest.__class__)
        redist, created = Redistribution.objects.get_or_create(
            destination_type=dct,
            destination_id=dest.pk,
            source_protocol=state.source_protocol,
            source_ref=state.source_ref,
            defaults={
                "route_map": route_map,
                "metric": entry.get("metric"),
                "metric_type": _redist_metric_type(entry),
            },
        )
        state.redistribution = redist
        state.save(update_fields=["redistribution"])

        dev_hash = merge_util.content_hash(_redist_device_content(entry, route_map, device))
        obj_hash = merge_util.content_hash(_redist_object_content(redist))
        from . import status_machine as sm

        if sm.is_owned(state.status):
            # metric-type is provenance-explicit: an omitted device leaf must not
            # settle a deliberately owned explicit value equal to its NED default.
            return obj_hash == dev_hash, False
        action = merge_util.three_way(
            created=created, base=state.device_base_hash, obj_hash=obj_hash, dev_hash=dev_hash
        )
        if action in ("seed", "mirror"):
            if action == "mirror":
                _write_redist(redist, route_map, entry, device)
            state.device_base_hash = dev_hash
            return True, False
        if action == "insync":
            state.device_base_hash = dev_hash
            return True, False
        if action == "freeze":
            return False, False
        return False, True  # conflict
    except Exception as exc:
        logger.debug("_create_or_link_redistribution: error for state %s: %s", state.pk, exc)
        return None, False


@mirror_reconciler
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
        if not sm.is_owned(state.status):
            state.route_map = entry.get("route_map") or ""
            state.metric = entry.get("metric")
            state.metric_type = _redist_metric_type(entry)
        state.device_present = True  # device reports it this pass (flips back if it had vanished)
        state.last_sync_at = now
        state.save()
        # 3-way merge: device change auto-mirrors when untouched; operator edit →
        # changed (and survives); both moved → conflict. The helper mutates this same
        # state object (redistribution FK + device_base_hash); the final save persists.
        matches, conflict = _create_or_link_redistribution(state, device, entry)
        if sm.is_owned(state.status):
            if matches is None or not _redist_overlay_matches_device(state, entry):
                matches = False
        state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
        state.save()
        seen_keys.add(key)

    # Stale rows: the device stopped reporting them.
    #   - OWNED (accepted/deploying/in_sync/apply_failed): keep the row + its object and flag
    #     drift (device_present=False) for the operator to resolve — operator intent is never
    #     auto-removed.
    #   - UNOWNED: the device no longer has it and nobody claimed it, so track the removal —
    #     drop the overlay and its (leaf, unshared) Redistribution object once no other overlay
    #     references it.
    for stale in NSORedistributionState.objects.filter(management=mgmt):
        k = (stale.dest_protocol, stale.dest_ref, stale.source_protocol, stale.source_ref)
        if k in seen_keys:
            continue
        if sm.is_owned(stale.status):
            fields = []
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                fields.append("status")
            if stale.device_present:
                stale.device_present = False
                fields.append("device_present")
            if fields:
                stale.save(update_fields=fields)
        else:
            rd = stale.redistribution
            stale.delete()
            if rd is not None and not rd.nso_redistribution_states.exists():
                rd.delete()

    return list(NSORedistributionState.objects.filter(management=mgmt))
