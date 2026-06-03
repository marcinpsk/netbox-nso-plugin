# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Route-policy reconciler for M17 A4.

Reads the adapter's GET /api/v1/devices/{id}/route-policy response and
reconciles it into netbox-routing policy objects (PrefixList, CommunityList,
ASPath, RouteMap) plus NSORoutePolicyState overlay rows.

Decision: global dedup by name — same-named object across N devices = ONE
NetBox object. On-device divergence sets status=conflict, never silently
overwrites existing content.
"""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_FAMILY_MODELS = {
    "prefix_list": "netbox_routing.PrefixList",
    "community_list": "netbox_routing.CommunityList",
    "as_path": "netbox_routing.ASPath",
    "route_map": "netbox_routing.RouteMap",
}


def _hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _get_routing_models():
    """Import and return netbox-routing models, or None if not installed."""
    try:
        from netbox_routing.models import (
            ASPath,
            CommunityList,
            PrefixList,
            RouteMap,
        )

        return PrefixList, CommunityList, ASPath, RouteMap
    except ImportError:
        logger.warning("netbox_routing not installed; skipping route-policy reconcile")
        return None


# ---------------------------------------------------------------------------
# Per-family handlers
# ---------------------------------------------------------------------------


def _reconcile_prefix_lists(mgmt, device, pl_list: list, PrefixList, ContentType, now, seen_keys: set):
    from .models import NSORoutePolicyState

    ct = ContentType.objects.get_for_model(PrefixList)
    for pl_data in pl_list:
        name = pl_data.get("name", "")
        if not name:
            continue
        entries_hash = _hash(pl_data.get("entries", []))
        # Global dedup by name.
        pl_obj, created = PrefixList.objects.get_or_create(name=name)

        state, _ = NSORoutePolicyState.objects.get_or_create(
            management=mgmt,
            family="prefix_list",
            object_name=name,
            defaults={
                "content_type": ct,
                "object_id": pl_obj.pk,
                "content_hash": entries_hash,
                "status": "imported",
                "last_sync_at": now,
            },
        )
        if not _:
            # Row existed — check divergence.
            if state.content_hash and state.content_hash != entries_hash:
                state.status = "conflict"
            else:
                state.status = "in_sync"  # GFK always set; hash matches → in_sync
                state.content_hash = entries_hash
            state.last_sync_at = now
            state.content_type = ct
            state.object_id = pl_obj.pk
            state.save(update_fields=["status", "content_hash", "last_sync_at", "content_type", "object_id"])
        seen_keys.add(("prefix_list", name))


def _reconcile_community_lists(mgmt, device, cl_list: list, CommunityList, ContentType, now, seen_keys: set):
    from .models import NSORoutePolicyState

    ct = ContentType.objects.get_for_model(CommunityList)
    for cl_data in cl_list:
        name = cl_data.get("name", "")
        if not name:
            continue
        entries_hash = _hash(cl_data.get("entries", []))
        cl_obj, created = CommunityList.objects.get_or_create(name=name)

        state, new_row = NSORoutePolicyState.objects.get_or_create(
            management=mgmt,
            family="community_list",
            object_name=name,
            defaults={
                "content_type": ct,
                "object_id": cl_obj.pk,
                "content_hash": entries_hash,
                "status": "imported",
                "last_sync_at": now,
            },
        )
        if not new_row:
            if state.content_hash and state.content_hash != entries_hash:
                state.status = "conflict"
            else:
                state.status = "in_sync"  # GFK always set; hash matches → in_sync
                state.content_hash = entries_hash
            state.last_sync_at = now
            state.content_type = ct
            state.object_id = cl_obj.pk
            state.save(update_fields=["status", "content_hash", "last_sync_at", "content_type", "object_id"])
        seen_keys.add(("community_list", name))


def _reconcile_as_paths(mgmt, device, ap_list: list, ASPath, ContentType, now, seen_keys: set):
    from .models import NSORoutePolicyState

    ct = ContentType.objects.get_for_model(ASPath)
    for ap_data in ap_list:
        name = ap_data.get("name", "")
        if not name:
            continue
        entries_hash = _hash(ap_data.get("entries", []))
        ap_obj, created = ASPath.objects.get_or_create(name=name)

        state, new_row = NSORoutePolicyState.objects.get_or_create(
            management=mgmt,
            family="as_path",
            object_name=name,
            defaults={
                "content_type": ct,
                "object_id": ap_obj.pk,
                "content_hash": entries_hash,
                "status": "imported",
                "last_sync_at": now,
            },
        )
        if not new_row:
            if state.content_hash and state.content_hash != entries_hash:
                state.status = "conflict"
            else:
                state.status = "in_sync"  # GFK always set; hash matches → in_sync
                state.content_hash = entries_hash
            state.last_sync_at = now
            state.content_type = ct
            state.object_id = ap_obj.pk
            state.save(update_fields=["status", "content_hash", "last_sync_at", "content_type", "object_id"])
        seen_keys.add(("as_path", name))


def _reconcile_route_maps(mgmt, device, rm_list: list, RouteMap, ContentType, now, seen_keys: set):
    from .models import NSORoutePolicyState

    ct = ContentType.objects.get_for_model(RouteMap)
    for rm_data in rm_list:
        name = rm_data.get("name", "")
        if not name:
            continue
        entries_hash = _hash(rm_data.get("entries", []))
        rm_obj, created = RouteMap.objects.get_or_create(name=name)

        state, new_row = NSORoutePolicyState.objects.get_or_create(
            management=mgmt,
            family="route_map",
            object_name=name,
            defaults={
                "content_type": ct,
                "object_id": rm_obj.pk,
                "content_hash": entries_hash,
                "status": "imported",
                "last_sync_at": now,
            },
        )
        if not new_row:
            if state.content_hash and state.content_hash != entries_hash:
                state.status = "conflict"
            else:
                state.status = "in_sync"  # GFK always set; hash matches → in_sync
                state.content_hash = entries_hash
            state.last_sync_at = now
            state.content_type = ct
            state.object_id = rm_obj.pk
            state.save(update_fields=["status", "content_hash", "last_sync_at", "content_type", "object_id"])
        seen_keys.add(("route_map", name))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reconcile_route_policy(device, payload: dict) -> list:
    """Reconcile route-policy data from the adapter into NetBox objects.

    For each entry in the payload:
    - Ensures the netbox-routing object exists (global dedup by name).
    - Creates/updates NSORoutePolicyState overlay rows.
    - Sets status=conflict when the on-device hash differs from stored hash.
    - Marks previously-seen objects no longer reported as status='changed'.

    Returns a list of NSORoutePolicyState instances for this device.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    from .models import NSODeviceManagement, NSORoutePolicyState

    result = _get_routing_models()
    if result is None:
        return []
    PrefixList, CommunityList, ASPath, RouteMap = result

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    now = timezone.now()
    seen_keys: set[tuple] = set()

    _reconcile_prefix_lists(mgmt, device, payload.get("prefix_lists", []), PrefixList, ContentType, now, seen_keys)
    _reconcile_community_lists(
        mgmt, device, payload.get("community_lists", []), CommunityList, ContentType, now, seen_keys
    )
    _reconcile_as_paths(mgmt, device, payload.get("as_paths", []), ASPath, ContentType, now, seen_keys)
    _reconcile_route_maps(mgmt, device, payload.get("route_maps", []), RouteMap, ContentType, now, seen_keys)

    # Mark stale rows as 'changed'.
    all_states = NSORoutePolicyState.objects.filter(management=mgmt)
    for state in all_states:
        if (state.family, state.object_name) not in seen_keys:
            if state.status not in ("changed", "accepted"):
                state.status = "changed"
                state.save(update_fields=["status"])

    return list(NSORoutePolicyState.objects.filter(management=mgmt).order_by("family", "object_name"))
