# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile LACP/LAG configuration from the adapter into overlay state models.

Read → model (no device write). Each bundle reported by the adapter's
``GET /api/v1/devices/{id}/lag-config`` becomes an ``NSOLACPBundleState`` on the
LAG ``dcim.Interface``; each member an ``NSOLACPMemberState`` on its physical
interface, linked back to the LAG via ``lag_bundle``.

Follows the established overlay-reconcile convention (see
``_reconcile_isis_interfaces`` / ``reconcile_l2_services``): keyed by
(management, interface); refresh the NSO-reported fields on every read; set
status ``imported`` unless the operator has advanced it into a write-path state
(accepted/deploying/in_sync — never clobbered); rows the payload no longer
reports are marked ``changed`` (drift). Never raises — interfaces absent from
NetBox are logged and skipped.
"""

from __future__ import annotations

import contextlib
import copy
import logging

logger = logging.getLogger(__name__)


def _member_bearing_lag_ids(interfaces):
    """Return every LAG referenced by the device's loaded interfaces."""
    return {row.lag_id for row in interfaces if row.lag_id is not None}


def lacp_reconcile_plan(device, payload: dict):
    """Freeze every LACP overlay save/delete before the first lock or write."""
    from dcim.models import Interface
    from django.utils import timezone

    from . import status_machine as sm
    from .models import NSODeviceManagement, NSOLACPBundleState, NSOLACPMemberState
    from .renderer_writer import RendererMutationPlan, planned_delete, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return RendererMutationPlan.build()
    interfaces = {row.name: row for row in Interface.objects.filter(device=device)}
    member_bearing_lag_ids = _member_bearing_lag_ids(interfaces.values())
    bundle_states = {
        row.interface_id: row for row in NSOLACPBundleState.objects.filter(management=management).order_by("pk")
    }
    member_states = {
        row.interface_id: row
        for row in NSOLACPMemberState.objects.filter(management=management).select_related("interface").order_by("pk")
    }
    planned_at = timezone.now()
    saves = []
    deletes = []
    seen_bundles = set()
    seen_members = set()

    for bundle_data in payload.get("bundles", []) or []:
        lag_interface = interfaces.get(bundle_data.get("name") or "")
        if lag_interface is None:
            continue
        current = bundle_states.get(lag_interface.pk)
        candidate = (
            copy.copy(current)
            if current is not None
            else NSOLACPBundleState(management=management, interface=lag_interface, status="unknown")
        )
        candidate.lag_id = bundle_data.get("lag_id")
        candidate.min_links = bundle_data.get("min_links")
        candidate.system_priority = bundle_data.get("system_priority")
        candidate.system_id = bundle_data.get("system_id") or ""
        candidate.timer = bundle_data.get("timer") or ""
        candidate.admin_key = bundle_data.get("admin_key")
        candidate.vpc_sensitive = bool(bundle_data.get("vpc_sensitive"))
        candidate.last_sync_at = planned_at
        candidate.status = sm.on_reconcile(candidate.status, matches=None)
        saves.append(
            planned_save(
                candidate,
                force_insert=current is None,
                natural_key=("management", "interface"),
            )
        )
        seen_bundles.add(lag_interface.pk)

        for member_data in bundle_data.get("members", []) or []:
            member_interface = interfaces.get(member_data.get("interface_name") or "")
            if member_interface is None:
                continue
            if member_interface.pk in seen_members:
                continue
            current_member = member_states.get(member_interface.pk)
            member = (
                copy.copy(current_member)
                if current_member is not None
                else NSOLACPMemberState(management=management, interface=member_interface, status="unknown")
            )
            member.lag_bundle = lag_interface
            member.mode = member_data.get("mode") or ""
            member.port_priority = member_data.get("port_priority")
            member.last_sync_at = planned_at
            member.status = sm.on_reconcile(member.status, matches=None)
            saves.append(
                planned_save(
                    member,
                    force_insert=current_member is None,
                    natural_key=("management", "interface"),
                )
            )
            seen_members.add(member_interface.pk)

    for stale in bundle_states.values():
        if stale.interface_id in seen_bundles:
            continue
        vestigial = stale.interface_id not in member_bearing_lag_ids
        if not sm.is_owned(stale.status) and vestigial:
            deletes.append(planned_delete(stale))
            continue
        new_status = sm.on_reconcile(stale.status, present=False)
        if new_status != stale.status:
            candidate = copy.copy(stale)
            candidate.status = new_status
            candidate.last_sync_at = planned_at
            saves.append(planned_save(candidate, update_fields=("status", "last_sync_at")))
    for stale in member_states.values():
        if stale.interface_id in seen_members:
            continue
        if not sm.is_owned(stale.status) and stale.interface.lag_id is None:
            deletes.append(planned_delete(stale))
            continue
        new_status = sm.on_reconcile(stale.status, present=False)
        if new_status != stale.status:
            candidate = copy.copy(stale)
            candidate.status = new_status
            candidate.last_sync_at = planned_at
            saves.append(planned_save(candidate, update_fields=("status", "last_sync_at")))

    return RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)


def reconcile_lag_config(device, payload: dict) -> list:
    """Apply one frozen LACP reconciliation through the renderer writer.

    ``payload`` is the JSON body of GET /api/v1/devices/{id}/lag-config. Returns
    all current NSOLACPBundleState rows for the device (``[]`` if the device has
    no NSO management).
    """
    from .renderer_writer import (
        active_renderer_writer,
        renderer_mirror_writes,
        renderer_writes,
    )
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    plan = active.plan if active is not None else lacp_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        return _reconcile_lag_config(device, payload, writer, plan.planned_at)


def _reconcile_lag_config(device, payload: dict, writer, planned_at) -> list:
    """Apply the LACP payload after its exact write set is frozen."""
    from dcim.models import Interface

    from . import status_machine as sm
    from .models import (
        NSODeviceManagement,
        NSOLACPBundleState,
        NSOLACPMemberState,
    )

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    bundles = payload.get("bundles", []) or []
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    now = planned_at
    seen_bundles: set[int] = set()
    seen_members: set[int] = set()
    dropped: list[str] = []

    for bundle_data in bundles:
        bundle_name = bundle_data.get("name") or ""
        if not bundle_name:
            continue
        lag_iface = iface_map.get(bundle_name)
        if lag_iface is None:
            dropped.append(bundle_name)
            continue

        state = NSOLACPBundleState.objects.filter(management=mgmt, interface=lag_iface).first()
        created = state is None
        if created:
            state = NSOLACPBundleState(management=mgmt, interface=lag_iface, status="unknown")
        state.lag_id = bundle_data.get("lag_id")
        state.min_links = bundle_data.get("min_links")
        state.system_priority = bundle_data.get("system_priority")
        state.system_id = bundle_data.get("system_id") or ""
        state.timer = bundle_data.get("timer") or ""
        state.admin_key = bundle_data.get("admin_key")
        # NX-P2: a vPC-protected bundle is reported (visible) but refused zero-write by the
        # writer, so the Accept view gates on this and the intent push excludes it.
        state.vpc_sensitive = bool(bundle_data.get("vpc_sensitive"))
        state.last_sync_at = now
        state.status = sm.on_reconcile(state.status, matches=None)  # mirror overlay
        writer.save(state, force_insert=created)
        seen_bundles.add(lag_iface.pk)

        for member_data in bundle_data.get("members", []) or []:
            iface_name = member_data.get("interface_name") or ""
            if not iface_name:
                continue
            member_iface = iface_map.get(iface_name)
            if member_iface is None:
                dropped.append(iface_name)
                continue
            if member_iface.pk in seen_members:
                continue

            m_state = NSOLACPMemberState.objects.filter(management=mgmt, interface=member_iface).first()
            member_created = m_state is None
            if member_created:
                m_state = NSOLACPMemberState(management=mgmt, interface=member_iface, status="unknown")
            m_state.lag_bundle = lag_iface
            m_state.mode = member_data.get("mode") or ""
            m_state.port_priority = member_data.get("port_priority")
            m_state.last_sync_at = now
            m_state.status = sm.on_reconcile(m_state.status, matches=None)  # mirror overlay
            writer.save(m_state, force_insert=member_created)
            seen_members.add(member_iface.pk)

    # Rows the payload no longer reports → prune vestigial husks, else drift (clobber-safe;
    # native interfaces untouched). A stale bundle is vestigial when its LAG interface has
    # no members left; a stale member when its interface is no longer assigned to any LAG.
    _finalise_stale_lacp(
        mgmt,
        seen_bundles,
        seen_members,
        _member_bearing_lag_ids(iface_map.values()),
        writer,
        now,
    )

    if dropped:
        logger.warning(
            "LACP reconcile for %s: %d interface(s) not found in NetBox, dropped: %s",
            device,
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )

    return list(NSOLACPBundleState.objects.filter(management=mgmt).select_related("interface"))


def _finalise_stale_lacp(management, seen_bundles, seen_members, member_bearing_lag_ids, writer, now) -> None:
    """Apply the planned stale-row outcomes for one LACP snapshot."""
    from . import status_machine as sm
    from .models import NSOLACPBundleState, NSOLACPMemberState

    groups = (
        (
            NSOLACPBundleState.objects.filter(management=management),
            seen_bundles,
            lambda row: row.interface_id not in member_bearing_lag_ids,
        ),
        (
            NSOLACPMemberState.objects.filter(management=management).select_related("interface"),
            seen_members,
            lambda row: row.interface.lag_id is None,
        ),
    )
    for rows, seen, is_vestigial in groups:
        for stale in rows:
            if stale.interface_id in seen:
                continue
            if not sm.is_owned(stale.status) and is_vestigial(stale):
                writer.delete(stale)
                continue
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.last_sync_at = now
                writer.save(stale, update_fields=("status", "last_sync_at"))
