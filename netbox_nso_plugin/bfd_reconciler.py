# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile per-interface BFD from the adapter into netbox_routing."""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)


def _profile_values(entry):
    """Return a deterministic BFD profile identity and values, if valid."""
    tx, rx, mult = entry.get("min_tx"), entry.get("min_rx"), entry.get("multiplier")
    if tx is None or rx is None or mult is None:
        return None
    if not (60 <= tx <= 60000 and 60 <= rx <= 60000 and 0 <= mult <= 255):
        return None
    return f"bfd-{tx}-{rx}-x{mult}", tx, rx, mult


def bfd_reconcile_plan(device, interfaces: list):
    """Freeze every native BFD and overlay write before reconciliation."""
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    try:
        saves, deletes, _operations = _bfd_reconcile_operations(device, interfaces, planned_at)
    except ImportError:
        return RendererMutationPlan.build(planned_at=planned_at)
    return RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)


def _bfd_reconcile_operations(device, interfaces, planned_at):  # noqa: C901
    """Build the deterministic BFD write sequence used by preflight and apply."""
    from dcim.models import Interface
    from netbox_routing.models import BFDInterface, BFDProfile

    from . import status_machine as sm
    from .models import NSOBFDInterfaceState, NSODeviceManagement
    from .renderer_writer import planned_delete, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    interface_by_name = {row.name: row for row in Interface.objects.filter(device=device).order_by("pk")}
    state_by_interface = (
        {
            row.interface_id: row
            for row in NSOBFDInterfaceState.objects.filter(management=management)
            .select_related("interface")
            .order_by("pk")
        }
        if management is not None
        else {}
    )
    native_by_interface = {
        row.interface_id: row
        for row in BFDInterface.objects.filter(interface__device=device).select_related("bfd_profile").order_by("pk")
    }
    profiles = {row.name: row for row in BFDProfile.objects.all().order_by("pk")}

    entries_by_interface = {}
    for entry in interfaces or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("interface_name") or ""
        if not name:
            continue
        interface = interface_by_name.get(name)
        if interface is None and entry.get("bound_port"):
            interface = interface_by_name.get(entry["bound_port"])
        if interface is not None:
            entries_by_interface[interface.pk] = (interface, entry)

    saves = []
    deletes = []
    operations = []
    seen_interface_ids = set()

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

    def profile_for(entry):
        values = _profile_values(entry)
        if values is None:
            return None
        name, tx, rx, mult = values
        profile = profiles.get(name)
        if profile is not None:
            return profile
        profile = BFDProfile(name=name, min_tx_int=tx, min_rx_int=rx, multiplier=mult)
        profiles[name] = profile
        save(profile, force_insert=True, natural_key=("name",))
        return profile

    for interface_id, (interface, entry) in entries_by_interface.items():
        seen_interface_ids.add(interface_id)
        current_state = state_by_interface.get(interface_id)
        owned = current_state is not None and sm.is_owned(current_state.status)
        desired_entry = (
            {
                "min_tx": current_state.min_tx,
                "min_rx": current_state.min_rx,
                "multiplier": current_state.multiplier,
            }
            if owned
            else entry
        )
        profile = profile_for(desired_entry)

        current_native = native_by_interface.get(interface_id)
        if current_native is None:
            native = BFDInterface(
                interface=interface,
                bfd_profile=profile,
                micro_bfd=current_state.micro_bfd if owned else bool(entry.get("micro_bfd", False)),
                enabled=True if owned else bool(entry.get("enabled", True)),
            )
            save(native, force_insert=True, natural_key=("interface",))
            native_by_interface[interface_id] = native
        elif not owned:
            desired_micro = bool(entry.get("micro_bfd", False))
            desired_enabled = bool(entry.get("enabled", True))
            if (
                (profile is not None and profile.pk is None)
                or current_native.bfd_profile_id != (profile.pk if profile is not None else None)
                or current_native.micro_bfd != desired_micro
                or current_native.enabled != desired_enabled
            ):
                native = copy.copy(current_native)
                native.bfd_profile = profile
                native.micro_bfd = desired_micro
                native.enabled = desired_enabled
                fields = ("bfd_profile", "micro_bfd", "enabled")
                save(native, update_fields=fields)
                native_by_interface[interface_id] = native

        if management is None:
            continue
        state = (
            copy.copy(current_state)
            if current_state is not None
            else NSOBFDInterfaceState(management=management, interface=interface)
        )
        if owned:
            matches = all(entry.get(field) == getattr(state, field) for field in ("min_tx", "min_rx", "multiplier"))
            matches = matches and state.micro_bfd == bool(entry.get("micro_bfd", False))
            # A matching read is not apply evidence: only a correlated apply result settles deploying.
            state.status = sm.on_reconcile(state.status, matches=matches, settles_deploying=False)
        else:
            state.min_tx = entry.get("min_tx")
            state.min_rx = entry.get("min_rx")
            state.multiplier = entry.get("multiplier")
            state.micro_bfd = bool(entry.get("micro_bfd", False))
            state.status = sm.on_reconcile(state.status)
        state.last_sync_at = planned_at
        created = current_state is None
        fields = (
            None
            if created
            else (
                ("status", "last_sync_at")
                if owned
                else ("min_tx", "min_rx", "multiplier", "micro_bfd", "status", "last_sync_at")
            )
        )
        save(
            state,
            update_fields=fields,
            force_insert=created,
            natural_key=("management", "interface"),
        )

    owned_interface_ids = {
        interface_id for interface_id, state in state_by_interface.items() if sm.is_owned(state.status)
    }
    deleted_native_ids = set()
    for interface_id, native in native_by_interface.items():
        if interface_id in seen_interface_ids or interface_id in owned_interface_ids or native.pk is None:
            continue
        delete(native)
        deleted_native_ids.add(interface_id)

    if management is not None:
        for interface_id, current_state in state_by_interface.items():
            if interface_id in seen_interface_ids:
                continue
            native_present = interface_id in native_by_interface and interface_id not in deleted_native_ids
            if not sm.is_owned(current_state.status) and not native_present:
                delete(current_state)
                continue
            new_status = sm.on_reconcile(current_state.status, present=False)
            if new_status == current_state.status:
                continue
            state = copy.copy(current_state)
            state.status = new_status
            state.last_sync_at = planned_at
            fields = ("status", "last_sync_at")
            save(state, update_fields=fields)

    return saves, deletes, operations


def reconcile_bfd(device, interfaces: list) -> list:
    """Apply one frozen BFD reconciliation through the renderer writer."""
    try:
        from netbox_routing.models import BFDInterface  # noqa: F401
    except ImportError:
        logger.warning("netbox_routing not installed; skipping BFD reconcile")
        return []

    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    plan = active.plan if active is not None else bfd_reconcile_plan(device, interfaces)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        _saves, _deletes, operations = _bfd_reconcile_operations(device, interfaces, plan.planned_at)
        for operation, instance, update_fields, force_insert in operations:
            if operation == "delete":
                writer.delete(instance)
            else:
                writer.save(instance, update_fields=update_fields, force_insert=force_insert)

    from netbox_routing.models import BFDInterface

    return list(BFDInterface.objects.filter(interface__device=device).select_related("interface", "bfd_profile"))
