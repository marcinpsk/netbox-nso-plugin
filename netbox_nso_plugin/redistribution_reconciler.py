# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Redistribution reconciler — moved from template_content.py (Track B, M20).

Also contains _try_link_redistribution which attempts to link an
NSORedistributionState row to the corresponding netbox-routing Redistribution
object after it has been saved.
"""

import logging

logger = logging.getLogger(__name__)


def _try_link_redistribution(state):
    """Attempt to link state.redistribution to a netbox_routing.Redistribution object.

    Matches on (dest_protocol, dest_ref, source_protocol, source_ref) where the
    Redistribution's destination object type (OSPFInstance/ISISInstance) determines
    dest_protocol, and destination object's process_id/process_tag maps to dest_ref.

    No-ops silently if netbox_routing is not installed or no match found.
    """
    if state.redistribution_id is not None:
        return  # already linked

    try:
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution
    except ImportError:
        return

    try:
        ospf_ct = ContentType.objects.get_for_model(OSPFInstance)  # noqa: F841 — for potential future typing

        # Try to find a match in netbox-routing Redistribution
        qs = Redistribution.objects.filter(
            source_protocol=state.source_protocol,
            source_ref=state.source_ref,
        )

        for redist in qs:
            dest_obj = redist.destination
            if dest_obj is None:
                continue
            # Determine what the dest_ref should be for this redistribution
            if hasattr(dest_obj, "process_id"):
                candidate_ref = str(dest_obj.process_id)
                candidate_proto = "ospf"
            elif hasattr(dest_obj, "process_tag"):
                candidate_ref = dest_obj.process_tag or ""
                candidate_proto = "isis"
            else:
                continue

            if candidate_proto == state.dest_protocol and candidate_ref == state.dest_ref:
                state.redistribution = redist
                state.save(update_fields=["redistribution"])
                return
    except Exception as exc:
        logger.debug("_try_link_redistribution: error linking state %s: %s", state.pk, exc)


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

    from .models import _REDISTRIBUTION_WRITE_PATH_STATUSES, NSODeviceManagement, NSORedistributionState

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
        if state.status not in _REDISTRIBUTION_WRITE_PATH_STATUSES and state.status != "conflict":
            state.save()
            _try_link_redistribution(state)
            state.refresh_from_db(fields=["redistribution"])
            state.status = "in_sync" if state.redistribution_id is not None else "imported"
        state.save()
        seen_keys.add(key)

    # Mark stale rows
    for stale in NSORedistributionState.objects.filter(management=mgmt):
        k = (stale.dest_protocol, stale.dest_ref, stale.source_protocol, stale.source_ref)
        if k not in seen_keys:
            stale.status = "changed"
            stale.save(update_fields=["status"])

    return list(NSORedistributionState.objects.filter(management=mgmt))
