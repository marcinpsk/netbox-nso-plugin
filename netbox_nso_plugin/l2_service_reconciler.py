# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile Nokia L2 services into native VPN rows and exact SAP overlays."""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)

# SR OS service-type to NetBox vpn.L2VPNTypeChoices code.
_L2VPN_TYPE = {"epipe": "vpws", "vpls": "vpls"}


def _validated_l2_services(payload) -> list:
    """Validate one adapter L2 document before planning any changes."""
    from .adapter_client import AdapterError

    if not isinstance(payload, dict):
        raise AdapterError("L2 service payload must be an object.", code="invalid_response")
    services = payload.get("services", [])
    if not isinstance(services, list):
        raise AdapterError("L2 services must be a list.", code="invalid_response")
    for service in services:
        if not isinstance(service, dict):
            raise AdapterError("L2 service entries must be objects.", code="invalid_response")
        service_name = service.get("service_name")
        if not isinstance(service_name, str) or not service_name:
            raise AdapterError("L2 service names must be non-empty strings.", code="invalid_response")
        service_type = service.get("service_type")
        if not isinstance(service_type, str) or service_type not in _L2VPN_TYPE:
            value = repr(service_type) if "service_type" in service else "<missing>"
            raise AdapterError(
                f"L2 service {service_name!r} has unsupported service_type {value}.",
                code="invalid_response",
            )
        saps = service.get("saps", [])
        if not isinstance(saps, list):
            raise AdapterError(f"L2 service {service_name!r} SAPs must be a list.", code="invalid_response")
        for sap in saps:
            if not isinstance(sap, dict):
                raise AdapterError("L2 SAP entries must be objects.", code="invalid_response")
            for field_name in ("sap_id", "port"):
                value = sap.get(field_name)
                if not isinstance(value, str) or not value:
                    raise AdapterError(
                        f"L2 SAP {field_name} values must be non-empty strings.",
                        code="invalid_response",
                    )
    return services


def l2_service_reconcile_plan(device, payload: dict):
    """Freeze every native VPN, termination, and L2 SAP overlay write."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, deletes, _operations = _l2_service_reconcile_operations(device, payload, planned_at)
    return RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)


def _same_l2vpn(left, right) -> bool:
    """Return whether two persisted or planned L2VPN objects have one identity."""
    if left is None or right is None:
        return False
    if left.pk is not None and right.pk is not None:
        return left.pk == right.pk
    return left.slug == right.slug


def _l2_service_reconcile_operations(device, payload, planned_at):  # noqa: C901
    """Build the deterministic L2-service write sequence for preflight and apply."""
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from vpn.models import L2VPN, L2VPNTermination

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOL2SapState
    from .renderer_writer import planned_save

    services = _validated_l2_services(payload)
    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], []

    interface_type = ContentType.objects.get_for_model(Interface)
    interfaces = {row.name: row for row in Interface.objects.filter(device=device).order_by("pk")}
    l2vpns = {row.slug: row for row in L2VPN.objects.filter(slug__startswith=f"nso-{device.pk}-").order_by("pk")}
    terminations = {
        row.assigned_object_id: row
        for row in L2VPNTermination.objects.filter(
            assigned_object_type=interface_type,
            assigned_object_id__in=[interface.pk for interface in interfaces.values()],
        )
        .select_related("l2vpn")
        .order_by("pk")
    }
    states = {
        (row.service_name, row.sap_id): row
        for row in NSOL2SapState.objects.filter(management=management)
        .select_related("l2vpn", "termination")
        .order_by("pk")
    }
    saves = []
    operations = []
    reported = set()
    reported_services = set()

    def save(instance, *, update_fields=None, force_insert=False, natural_key=()):
        saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            )
        )
        operations.append((instance, update_fields, force_insert))

    for service in services:
        service_name = service["service_name"]
        if service_name in reported_services:
            continue
        reported_services.add(service_name)
        service_type = service["service_type"]
        l2vpn_type = _L2VPN_TYPE[service_type]
        slug = f"nso-{device.pk}-{service_name}"
        current_l2vpn = l2vpns.get(slug)
        if current_l2vpn is None:
            l2vpn = L2VPN(
                slug=slug,
                name=f"{device.name}: {service_name}",
                type=l2vpn_type,
                identifier=service.get("service_id"),
            )
            save(l2vpn, force_insert=True, natural_key=("slug",))
            l2vpns[slug] = l2vpn
        else:
            l2vpn = current_l2vpn
            fields = []
            candidate = copy.copy(current_l2vpn)
            if candidate.type != l2vpn_type:
                candidate.type = l2vpn_type
                fields.append("type")
            service_id = service.get("service_id")
            if service_id is not None and candidate.identifier != service_id:
                candidate.identifier = service_id
                fields.append("identifier")
            if fields:
                l2vpn = candidate
                save(l2vpn, update_fields=fields)
                l2vpns[slug] = l2vpn

        for sap in service.get("saps", []):
            key = (service_name, sap["sap_id"])
            if key in reported:
                continue
            reported.add(key)
            current_state = states.get(key)
            state = (
                copy.copy(current_state)
                if current_state is not None
                else NSOL2SapState(management=management, service_name=service_name, sap_id=sap["sap_id"])
            )
            observed_service_type = service_type
            observed_port = sap.get("port", "")
            observed_outer_tag = sap.get("outer_tag")
            observed_inner_tag = sap.get("inner_tag")
            owned = sm.is_owned(state.status)
            if not owned:
                state.service_type = observed_service_type
                state.port = observed_port
                state.outer_tag = observed_outer_tag
                state.inner_tag = observed_inner_tag
            state.service_id = service.get("service_id")
            state.l2vpn = l2vpn
            state.last_sync_at = planned_at

            interface = interfaces.get(observed_port)
            conflict = interface is None
            termination = None if interface is None else terminations.get(interface.pk)
            if termination is not None and not _same_l2vpn(termination.l2vpn, l2vpn):
                conflict = True
                termination = None
            elif termination is None and interface is not None:
                termination = L2VPNTermination(l2vpn=l2vpn, assigned_object=interface)
                save(
                    termination,
                    force_insert=True,
                    natural_key=("assigned_object_type", "assigned_object_id"),
                )
                terminations[interface.pk] = termination
            state.termination = termination
            matches = not conflict and (
                state.service_type,
                state.port,
                state.outer_tag,
                state.inner_tag,
            ) == (
                observed_service_type,
                observed_port,
                observed_outer_tag,
                observed_inner_tag,
            )
            state.status = sm.on_reconcile(
                state.status,
                matches=matches,
                conflict=conflict,
                settles_owned=False,
                settles_deploying=False,
            )
            created = current_state is None
            save(
                state,
                force_insert=created,
                natural_key=("management", "service_name", "sap_id"),
            )
            states[key] = state

    for key, current in states.items():
        if key in reported or current.pk is None:
            continue
        new_status = sm.on_reconcile(current.status, present=False)
        if new_status == current.status:
            continue
        state = copy.copy(current)
        state.status = new_status
        state.last_sync_at = planned_at
        save(state, update_fields=("status", "last_sync_at"))

    return saves, [], operations


def reconcile_l2_services(device, payload: dict) -> list:
    """Apply one frozen L2-service reconciliation through the renderer writer."""
    from .renderer_writer import active_renderer_writer, renderer_writes_replanning_once
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    mutation = (
        contextlib.nullcontext((active, active.plan))
        if active is not None
        else renderer_writes_replanning_once(lambda: l2_service_reconcile_plan(device, payload))
    )
    with mutation as (writer, plan), suppress_intent_push():
        _saves, _deletes, operations = _l2_service_reconcile_operations(device, payload, plan.planned_at)
        for instance, update_fields, force_insert in operations:
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)

    from .models import NSODeviceManagement, NSOL2SapState

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return []
    return list(NSOL2SapState.objects.filter(management=management).select_related("l2vpn", "termination"))
