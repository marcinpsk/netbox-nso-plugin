# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Execute production management-row lifecycle mutations through the exact writer."""

from __future__ import annotations

import contextlib
import contextvars

from .renderer_writer import (
    RendererMutationPlan,
    planned_delete,
    planned_save,
    renderer_mirror_writes,
    renderer_writes,
)

ONBOARD_EVIDENCE_FIELDS = ("onboard_status", "onboard_steps", "onboard_error")

_MANAGEMENT_CRUD_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "nso_management_crud_active",
    default=False,
)


def management_crud_is_active() -> bool:
    """Return whether a production management CRUD surface entered its writer seam."""
    return _MANAGEMENT_CRUD_ACTIVE.get()


@contextlib.contextmanager
def management_crud_writes():
    """Route management model saves and deletes through the exact writer."""
    token = _MANAGEMENT_CRUD_ACTIVE.set(True)
    try:
        yield
    finally:
        _MANAGEMENT_CRUD_ACTIVE.reset(token)


def save_management(instance, *, update_fields=None, force_insert=False):
    """Save one management row with an exact precomputed mutation plan."""
    _refresh_full_save_records(instance, update_fields)
    update_fields = _prepare_source_fence(instance, update_fields)
    natural_key = ("device",) if instance.pk is None or instance._state.adding else ()
    plan = RendererMutationPlan.build(
        saves=(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            ),
        )
    )
    context = renderer_writes if plan.changes_content else renderer_mirror_writes
    with context(plan) as writer:
        writer.save(instance, update_fields=update_fields, force_insert=force_insert)
    return instance


def _refresh_full_save_records(instance, update_fields):
    """Carry every monotone record into a full save before its plan is frozen."""
    if update_fields is not None or instance.pk is None or instance._state.adding:
        return
    protected = type(instance)._STALE_SAVE_PROTECTED_FIELDS
    current = type(instance).objects.filter(pk=instance.pk).values(*protected).first()
    if current is None:
        return
    for field_name in protected:
        setattr(instance, field_name, current[field_name])


def _prepare_source_fence(instance, update_fields):
    """Put a source-rekey fence in the frozen write that changes its source tuple."""
    if instance.pk is None or instance._state.adding:
        return update_fields
    selected = None if update_fields is None else set(update_fields)
    source_fields = {"nso_instance", "nso_instance_id", "nso_device_name"}
    if selected is not None and not selected & source_fields:
        return update_fields
    previous = (
        type(instance)
        .objects.filter(pk=instance.pk)
        .values_list("nso_instance_id", "nso_device_name", "source_rekey_pending")
        .first()
    )
    if previous is None:
        return update_fields
    if selected is None and previous[2]:
        instance.source_rekey_pending = True
    if previous[:2] == (instance.nso_instance_id, instance.nso_device_name):
        return update_fields
    instance.source_rekey_pending = True
    if selected is None:
        return update_fields
    selected.add("source_rekey_pending")
    return tuple(sorted(selected))


def delete_management(instance):
    """Delete one management row and its exact Collector closure through the writer."""
    plan = RendererMutationPlan.build(deletes=(planned_delete(instance),))
    context = renderer_writes if plan.changes_content else renderer_mirror_writes
    with context(plan) as writer:
        return writer.delete(instance)


def _control_footprint(device_id):
    """Freeze the management row and current address owners for one control POST.

    Exactly what the payload is read from, and nothing else: the device intent lock and the
    management row (L4/L5) order the pushes for this device, and the device plus its address
    rows (L6) keep the five values one snapshot. The 18 delivery families are not read here
    and must never be frozen across the adapter round trip.
    """
    from dcim.models import Device

    from .intent_state import MutationFootprint, SourceRow

    identity = (
        Device.objects.filter(pk=device_id).values_list("pk", "primary_ip4_id", "primary_ip6_id", "oob_ip_id").first()
    )
    if identity is None:
        return None
    address_ids = {value for value in identity[1:] if value is not None}
    return MutationFootprint(
        device_ids=(device_id,),
        source_rows=(
            SourceRow("dcim.device", device_id),
            *(SourceRow("ipam.ipaddress", address_id) for address_id in sorted(address_ids)),
        ),
    )


def _update_management_control(device_id: int, *, compare_adapter: bool) -> bool:
    """Write adapter control fields while their owners stay locked."""
    from . import adapter_client
    from .intent_state import mirror_transaction
    from .models import NSODeviceManagement
    from .onboarding import device_mgmt_addresses

    footprint = _control_footprint(int(device_id))
    if footprint is None:
        return False
    with mirror_transaction(footprint):
        management = (
            NSODeviceManagement.objects.select_related(
                "device__primary_ip4",
                "device__primary_ip6",
                "device__oob_ip",
            )
            .filter(device_id=device_id)
            .first()
        )
        if management is None or management.adapter_device_id is None:
            return False
        locked_address_ids = {row.pk for row in footprint.source_rows if row.model_label == "ipam.ipaddress"}
        current_address_ids = {
            address_id
            for address_id in (
                management.device.primary_ip4_id,
                management.device.primary_ip6_id,
                management.device.oob_ip_id,
            )
            if address_id is not None
        }
        if not current_address_ids.issubset(locked_address_ids):
            return False
        primary_ip, oob_ip = device_mgmt_addresses(management.device)
        desired = adapter_client.AdapterControlState(
            managed_attributes=tuple(management.managed_attributes),
            auto_apply=management.auto_apply,
            sync_before_apply=management.sync_before_apply,
            primary_ip=primary_ip,
            oob_ip=oob_ip,
        )
        if compare_adapter and adapter_client.get_control_state(management.adapter_device_id) == desired:
            return False
        adapter_client.set_control_state(management.adapter_device_id, desired)
        return True


def reconcile_management_control(device_id: int) -> bool:
    """Update divergent adapter control fields while their owners stay locked."""
    return _update_management_control(device_id, compare_adapter=True)


def push_management_control(device_id: int) -> bool:
    """Push a control change while its authoritative owners stay locked."""
    return _update_management_control(device_id, compare_adapter=False)
