# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile redistribution into exact native and overlay renderer writes."""

import contextlib
import copy
import logging
from dataclasses import replace

logger = logging.getLogger(__name__)


def redistribution_reconcile_plan(device, payload):
    """Freeze every native and overlay redistribution write before reconciliation."""
    from django.utils import timezone

    from .intent_state import MutationFootprint, route_policy_footprint
    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    try:
        saves, deletes, _operations, dependencies = _redistribution_reconcile_operations(device, payload, planned_at)
    except ImportError:
        return RendererMutationPlan.build(planned_at=planned_at)
    plan = RendererMutationPlan.build(
        saves=saves,
        deletes=deletes,
        read_dependencies=dependencies,
        planned_at=planned_at,
    )
    route_map_groups = {
        ("route_map", entry.get("route_map")) for entry in payload.get("entries") or [] if entry.get("route_map")
    }
    policy_footprint = route_policy_footprint(route_map_groups)
    policy_dependencies = MutationFootprint.for_keys(
        (),
        shared_keys=policy_footprint.shared_keys,
        source_rows=policy_footprint.source_rows,
        overlay_rows=policy_footprint.overlay_rows,
    )
    lock_footprint = MutationFootprint.merge(plan.lock_footprint, policy_dependencies)
    lock_footprint = replace(
        lock_footprint,
        device_ids=tuple(sorted({*lock_footprint.device_ids, *policy_footprint.device_ids})),
    )
    return replace(plan, lock_footprint=lock_footprint)


def redistribution_reconcile_footprint(device, payload=None):
    """Return the exact preflight footprint for compatibility callers."""
    return redistribution_reconcile_plan(device, payload or {"entries": []}).lock_footprint


def _resolve_redist_destination(device, dest_protocol: str, dest_ref: str):
    """Resolve the native destination object for one redistribution entry."""
    try:
        from netbox_routing.models import BGPAddressFamily, BGPRouter, BGPScope, ISISInstance, OSPFInstance
    except ImportError:
        return None

    if dest_protocol == "ospf":
        try:
            process_id = int(dest_ref)
        except (TypeError, ValueError):
            return None
        return OSPFInstance.objects.filter(device=device, process_id=process_id).first()
    if dest_protocol == "isis":
        return ISISInstance.objects.filter(device=device, process_tag=dest_ref or "").first()
    if dest_protocol != "bgp":
        return None

    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import ASN, VRF

    parts = (dest_ref or "").split("/")
    if len(parts) != 3:
        return None
    asn_value, vrf_name, address_family = parts
    try:
        asn = ASN.objects.filter(asn=int(asn_value)).first()
    except (TypeError, ValueError):
        return None
    if asn is None or not address_family:
        return None
    device_type = ContentType.objects.get_for_model(Device)
    router = BGPRouter.objects.filter(
        assigned_object_type=device_type,
        assigned_object_id=device.pk,
        asn=asn,
    ).first()
    if router is None:
        return None
    vrf = VRF.objects.filter(name=vrf_name).first() if vrf_name else None
    scope = BGPScope.objects.filter(router=router, vrf=vrf).first()
    if scope is None:
        return None
    return BGPAddressFamily.objects.filter(scope=scope, address_family=address_family).first()


def _redist_metric_type(entry: dict) -> str:
    """Return only configured intent. Keep an omitted value absent."""
    return (entry.get("metric_type") or "") if "metric_type" in entry else ""


def _redist_device_content(entry: dict, route_map) -> dict:
    from . import merge_util

    return {
        "route_map": merge_util.pk(route_map),
        "metric": entry.get("metric"),
        "metric_type": _redist_metric_type(entry),
    }


def _redist_object_content(redistribution) -> dict:
    return {
        "route_map": redistribution.route_map_id,
        "metric": redistribution.metric,
        "metric_type": redistribution.metric_type or "",
    }


def _redist_overlay_matches_device(state, entry: dict) -> bool:
    return (
        state.route_map == (entry.get("route_map") or "")
        and state.metric == entry.get("metric")
        and (state.metric_type or "") == _redist_metric_type(entry)
    )


def _redistribution_reconcile_operations(device, payload, planned_at):  # noqa: C901
    """Build the deterministic redistribution writes used by preflight and apply."""
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import Redistribution, RouteMap

    from . import merge_util
    from . import status_machine as sm
    from .models import NSODeviceManagement, NSORedistributionState
    from .renderer_writer import planned_delete, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], [], []
    states = {
        (row.dest_protocol, row.dest_ref, row.source_protocol, row.source_ref): row
        for row in NSORedistributionState.objects.filter(management=management)
        .select_related("redistribution")
        .order_by("pk")
    }
    saves = []
    deletes = []
    operations = []
    dependencies = {}
    seen = set()

    def save(instance, *, update_fields=None, force_insert=False, natural_key=()):
        saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            )
        )
        operations.append(("save", instance, update_fields, force_insert))

    def delete(instance):
        deletes.append(planned_delete(instance))
        operations.append(("delete", instance, None, False))

    for entry in payload.get("entries") or []:
        destination_protocol = entry.get("dest_protocol") or ""
        destination_ref = entry.get("dest_ref") or ""
        source_protocol = entry.get("source_protocol") or ""
        source_ref = entry.get("source_ref") or ""
        if not destination_protocol or not source_protocol:
            continue
        key = (destination_protocol, destination_ref, source_protocol, source_ref)
        seen.add(key)
        current = states.get(key)
        state = (
            copy.copy(current)
            if current is not None
            else NSORedistributionState(
                management=management,
                dest_protocol=destination_protocol,
                dest_ref=destination_ref,
                source_protocol=source_protocol,
                source_ref=source_ref,
            )
        )
        owned = sm.is_owned(state.status)
        if not owned:
            state.route_map = entry.get("route_map") or ""
            state.metric = entry.get("metric")
            state.metric_type = _redist_metric_type(entry)
        state.device_present = True
        state.last_sync_at = planned_at
        matches = None
        conflict = False

        destination = _resolve_redist_destination(device, destination_protocol, destination_ref)
        if destination is not None:
            dependencies[(destination._meta.label_lower, destination.pk)] = destination
            route_map = (
                RouteMap.objects.filter(name=entry.get("route_map") or "").first() if entry.get("route_map") else None
            )
            if route_map is not None:
                dependencies[(route_map._meta.label_lower, route_map.pk)] = route_map
            destination_type = ContentType.objects.get_for_model(type(destination))
            current_native = Redistribution.objects.filter(
                destination_type=destination_type,
                destination_id=destination.pk,
                source_protocol=source_protocol,
                source_ref=source_ref,
            ).first()
            native = (
                copy.copy(current_native)
                if current_native is not None
                else Redistribution(
                    destination_type=destination_type,
                    destination_id=destination.pk,
                    source_protocol=source_protocol,
                    source_ref=source_ref,
                    route_map=route_map,
                    metric=entry.get("metric"),
                    metric_type=_redist_metric_type(entry),
                )
            )
            created_native = current_native is None
            device_hash = merge_util.content_hash(_redist_device_content(entry, route_map))
            object_hash = merge_util.content_hash(_redist_object_content(native))
            if owned:
                matches = object_hash == device_hash
            else:
                action = merge_util.three_way(
                    created=created_native,
                    base=state.device_base_hash,
                    obj_hash=object_hash,
                    dev_hash=device_hash,
                )
                if action == "mirror":
                    native.route_map = route_map
                    native.metric = entry.get("metric")
                    native.metric_type = _redist_metric_type(entry)
                    save(native, update_fields=("route_map", "metric", "metric_type"))
                elif created_native:
                    save(
                        native,
                        force_insert=True,
                        natural_key=(
                            "destination_type",
                            "destination_id",
                            "source_protocol",
                            "source_ref",
                        ),
                    )
                if action in {"seed", "mirror", "insync"}:
                    state.device_base_hash = device_hash
                    matches = True
                elif action == "freeze":
                    matches = False
                else:
                    matches = False
                    conflict = True
            state.redistribution = native

        if owned and (matches is None or not _redist_overlay_matches_device(state, entry)):
            matches = False
        state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
        created_state = current is None
        update_fields = (
            None
            if created_state
            else (
                "route_map",
                "metric",
                "metric_type",
                "redistribution",
                "status",
                "device_present",
                "last_sync_at",
                "device_base_hash",
            )
        )
        save(
            state,
            update_fields=update_fields,
            force_insert=created_state,
            natural_key=(
                "management",
                "dest_protocol",
                "dest_ref",
                "source_protocol",
                "source_ref",
            ),
        )

    deleting_state_pks = {
        state.pk for key, state in states.items() if key not in seen and not sm.is_owned(state.status)
    }
    deleting_natives = {}
    for key, stale in states.items():
        if key in seen:
            continue
        if sm.is_owned(stale.status):
            candidate = copy.copy(stale)
            candidate.status = sm.on_reconcile(candidate.status, present=False)
            candidate.device_present = False
            fields = tuple(
                name for name in ("status", "device_present") if getattr(candidate, name) != getattr(stale, name)
            )
            if fields:
                save(candidate, update_fields=fields)
            continue
        native = stale.redistribution
        delete(stale)
        if native is not None and not native.nso_redistribution_states.exclude(pk__in=deleting_state_pks).exists():
            deleting_natives[native.pk] = native

    for native in deleting_natives.values():
        delete(native)

    return saves, deletes, operations, tuple(dependencies.values())


def reconcile_redistribution(device, payload: dict) -> list:
    """Apply one frozen redistribution reconciliation through the renderer writer."""
    try:
        from netbox_routing.models import Redistribution  # noqa: F401
    except ImportError:
        logger.warning("netbox_routing not installed; skipping redistribution reconcile")
        return []

    from .models import NSODeviceManagement, NSORedistributionState
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return []
    active = active_renderer_writer()
    plan = active.plan if active is not None else redistribution_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        _saves, _deletes, operations, _dependencies = _redistribution_reconcile_operations(
            device,
            payload,
            plan.planned_at,
        )
        for operation, instance, update_fields, force_insert in operations:
            if operation == "delete":
                writer.delete(instance)
            else:
                writer.save(instance, update_fields=update_fields, force_insert=force_insert)
    return list(NSORedistributionState.objects.filter(management=management))
