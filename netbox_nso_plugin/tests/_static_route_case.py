# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared fixtures for the #1396 R3 static-route suites (P2 transition, P6 push errors).

Defined once, here, so the two suites cannot drift on what a device, a brownfield route or
an owned overlay looks like. Same role as :mod:`._settlement_case` for the Appendix S suites.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import transaction
from django.utils import timezone

PUT = "netbox_nso_plugin.adapter_client.put_static_route_intent"


def _make_device(tag: str, index: int = 1):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"Sr{tag}Mfg", slug=f"sr{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Sr{tag}Dev", slug=f"sr{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Sr{tag}Role", slug=f"sr{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Sr{tag}Site", slug=f"sr{tag}site")
    with transaction.atomic():
        return Device.objects.create(name=f"sr-{tag}-rtr-{index}", device_type=dt, role=role, site=site)


def _make_mgmt(device, tag: str, adapter_device_id: int):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    with transaction.atomic():
        inst, _ = NSOInstance.objects.get_or_create(
            name=f"sr-{tag}-inst", defaults={"adapter_instance_id": f"sr-{tag}-inst"}
        )
        return NSODeviceManagement.objects.create(
            device=device,
            nso_instance=inst,
            nso_device_name=f"nso-sr-{tag}-{device.pk}",
            adapter_device_id=adapter_device_id,
        )


@contextlib.contextmanager
def _fixtures():
    """Build fixtures with the adapter patched out, then clear the coalescer.

    Creating an overlay fires its own push, which inside a ``TestCase``'s ambient atomic
    block lands in the thread-local pending map that only an ``on_commit`` drain clears.
    That drain is registered outside the assertion's ``captureOnCommitCallbacks()``, so it
    never runs there — and the still-populated map then suppresses the registration the
    transition's own push needs (the pre-existing rollback leak; Appendix O owns the fix).

    The reset runs in a ``finally``: a fixture body that raises would otherwise leak the
    thread-local pending map into every later test on this worker.
    """
    from netbox_nso_plugin.signals import reset_intent_push_state

    try:
        with patch(PUT):
            yield
    finally:
        reset_intent_push_state()


def _route(prefix, next_hop, *, vrf=None, metric=1, devices=()):
    """Create a route already assigned to *devices*, without owning it (brownfield shape)."""
    from netbox_routing.models import StaticRoute

    with transaction.atomic():
        sr = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, vrf=vrf, metric=metric)
    if devices:
        _assign_without_push(sr, *devices)
    return sr


def _assign_without_push(route, *devices):
    """Assign a brownfield route through its exact suppressed content footprint."""
    from netbox_nso_plugin.intent_state import _static_route_devices_footprint, intent_transaction
    from netbox_nso_plugin.signals import suppress_intent_push

    footprint = _static_route_devices_footprint(
        route,
        "pre_add",
        {device.pk for device in devices},
        False,
    )
    with suppress_intent_push(), intent_transaction(footprint):
        route.devices.add(*devices)


def _unassign_without_push(route, *devices):
    """Remove a route assignment through its exact suppressed content footprint."""
    from netbox_nso_plugin.intent_state import _static_route_devices_footprint, intent_transaction
    from netbox_nso_plugin.signals import suppress_intent_push

    footprint = _static_route_devices_footprint(
        route,
        "pre_remove",
        {device.pk for device in devices},
        False,
    )
    with suppress_intent_push(), intent_transaction(footprint):
        route.devices.remove(*devices)


def _carried_last_acked(mgmt, route_id):
    """Read the acknowledged triple carried by this fixture's pending deletion."""
    from netbox_nso_plugin import outbox
    from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentOutboxState

    state = NSOIntentOutboxState.objects.filter(device_id=mgmt.device_id, scope="static_route").first()
    transitions = [
        record
        for row in NSOIntentOutboxEntry.objects.filter(
            device_id=mgmt.device_id,
            scope="static_route",
            consumed_by_push_seq__isnull=True,
        ).order_by("id")
        for record in row.transitions
    ]
    return outbox.carried_triple(
        route_id,
        transitions=transitions,
        queued=state.queued_deletions if state else (),
        claim_deletions=state.claim_deletions if state else (),
        lineage_carry=state.lineage_carry if state else None,
    )


def _acquire_static_route(route, device) -> None:
    """Create or own one static-route overlay for a test fixture."""
    import copy

    from netbox_nso_plugin import outbox
    from netbox_nso_plugin.models import NSODeviceManagement, NSOStaticRouteState
    from netbox_nso_plugin.renderer_writer import (
        RendererMutationPlan,
        planned_save,
        renderer_mirror_writes,
        renderer_writes,
    )
    from netbox_nso_plugin.signals import (
        _OWNED_PUSH_STATUSES,
        _arm_static_route_generation,
        _schedule_intent_push,
    )

    mgmt = NSODeviceManagement.objects.get(device=device)
    state = NSOStaticRouteState.objects.filter(management=mgmt, static_route=route).first()
    created = state is None
    candidate = (
        NSOStaticRouteState(
            management=mgmt,
            static_route=route,
            status="accepted",
            accepted_at=timezone.now(),
        )
        if created
        else copy.copy(state)
    )
    if created:
        candidate.last_acked_triple = _carried_last_acked(mgmt, route.pk)
    was_owned = not created and candidate.status in _OWNED_PUSH_STATUSES
    if not created and not was_owned:
        candidate.status = "accepted"
        candidate.accepted_at = timezone.now()
    if not was_owned:
        _arm_static_route_generation(candidate)
    candidate.nso_vrf = route.vrf.name if route.vrf else ""
    candidate.nso_prefix = str(route.prefix or "")
    candidate.nso_next_hop = str(route.next_hop or "")
    candidate.last_sync_at = timezone.now()
    plan = RendererMutationPlan.build(
        saves=(
            planned_save(
                candidate,
                force_insert=created,
                natural_key=("management", "static_route") if created else (),
            ),
        ),
        planned_at=candidate.accepted_at,
    )
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer:
        writer.save(candidate, force_insert=created)
        if not was_owned:
            _schedule_intent_push(
                (mgmt.device_id, "static_route"),
                transitions=[outbox.revoke_transition(route.pk)],
            )


def _assign_and_accept(route, *devices):
    """Assign and own routes through exact assignment and acquisition plans."""
    from netbox_nso_plugin.renderer_writer import (
        RendererMutationPlan,
        planned_m2m_add,
        renderer_mirror_writes,
        renderer_writes,
    )
    from netbox_nso_plugin.signals import suppress_intent_push

    assignment = RendererMutationPlan.build(m2m_writes=(planned_m2m_add(route, "devices", devices),))
    mutation = renderer_writes(assignment) if assignment.changes_content else renderer_mirror_writes(assignment)
    with mutation as writer, suppress_intent_push():
        writer.m2m_add(route, "devices", devices)
    for device in devices:
        _acquire_static_route(route, device)


def _edit_owned_route(route, **values):
    """Apply one operator content edit through the production exact writer."""
    import copy

    from netbox_nso_plugin.models import NSOStaticRouteState
    from netbox_nso_plugin.renderer_writer import (
        RendererMutationPlan,
        planned_save,
        renderer_mirror_writes,
        renderer_writes,
    )
    from netbox_nso_plugin.views import _save_owned_static_route_edit

    state = NSOStaticRouteState.objects.filter(static_route=route).select_related("static_route").order_by("pk").first()
    if state is None:
        current = type(route).objects.get(pk=route.pk)
        candidate = copy.copy(current)
        for field_name, value in values.items():
            setattr(candidate, field_name, value)
        fields = tuple(values)
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=fields),))
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
        with mutation as writer:
            writer.save(candidate, update_fields=fields)
        route.refresh_from_db()
        return
    native = state.static_route
    old_values = {field_name: getattr(native, field_name) for field_name in values}
    for field_name, value in values.items():
        setattr(native, field_name, value)
    _save_owned_static_route_edit(state, old_values)
    route.refresh_from_db()


def _touch_owned_route(route):
    """Make one real wire-visible edit through the production exact writer."""
    current = type(route).objects.get(pk=route.pk)
    _edit_owned_route(route, metric=(current.metric or 0) + 1)


def _delete_owned_route(route):
    """Delete one native route and its exact Collector closure."""
    from netbox_nso_plugin import signals
    from netbox_nso_plugin.models import NSOStaticRouteState
    from netbox_nso_plugin.renderer_writer import (
        RendererMutationPlan,
        planned_delete,
        renderer_mirror_writes,
        renderer_writes,
    )

    current = type(route).objects.get(pk=route.pk)
    transitions = tuple(
        (
            state.management.device_id,
            signals._static_route_delete_transition(state, current.pk),
        )
        for state in NSOStaticRouteState.objects.filter(static_route=current)
        .select_related("management")
        .order_by("management__device_id", "pk")
    )
    plan = RendererMutationPlan.build(deletes=(planned_delete(current),))
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer:
        with signals.suppress_intent_push():
            writer.delete(current)
        with signals._delete_origin_dispatch():
            for device_id, transition in transitions:
                signals._schedule_intent_push(
                    (device_id, "static_route"),
                    transitions=(transition,),
                )


def _accept_with_permit(route, device):
    """Own one route overlay through its exact content permit."""
    from netbox_nso_plugin.renderer_writer import (
        IntentPlanStaleError,
        RendererMutationPlan,
        planned_m2m_add,
        renderer_mirror_writes,
        renderer_writes,
    )
    from netbox_nso_plugin.signals import suppress_intent_push

    for attempt in range(2):
        try:
            if not route.devices.filter(pk=device.pk).exists():
                assignment = RendererMutationPlan.build(
                    m2m_writes=(planned_m2m_add(route, "devices", (device,)),),
                )
                mutation = (
                    renderer_writes(assignment) if assignment.changes_content else renderer_mirror_writes(assignment)
                )
                with mutation as writer, suppress_intent_push():
                    writer.m2m_add(route, "devices", (device,))
            _acquire_static_route(route, device)
            return
        except IntentPlanStaleError:
            if attempt:
                raise


def _unassign_and_retire(route, device):
    """Remove one owned route membership through one exact retirement plan."""
    from netbox_nso_plugin import signals
    from netbox_nso_plugin.models import NSOStaticRouteState
    from netbox_nso_plugin.renderer_writer import (
        RendererMutationPlan,
        planned_delete,
        planned_m2m_set,
        renderer_writes,
    )

    current = type(route).objects.get(pk=route.pk)
    remaining = tuple(current.devices.exclude(pk=device.pk).order_by("pk"))
    state = NSOStaticRouteState.objects.filter(management__device=device, static_route=current).first()
    deletes = () if state is None else (planned_delete(state),)
    plan = RendererMutationPlan.build(
        deletes=deletes,
        m2m_writes=(planned_m2m_set(current, "devices", remaining),),
    )
    if not plan.changes_content:
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes

        mutation = renderer_mirror_writes(plan)
    else:
        mutation = renderer_writes(plan)
    with mutation as writer:
        transition = None if state is None else signals._static_route_delete_transition(state, current.pk)
        with signals.suppress_intent_push():
            if state is not None:
                writer.delete(state)
            writer.m2m_set(current, "devices", remaining)
        if transition is not None:
            with signals._delete_origin_dispatch():
                signals._schedule_intent_push(
                    (state.management.device_id, "static_route"),
                    transitions=(transition,),
                )


def _own(sr, mgmt, *, status="in_sync", mirror_vrf=None):
    """Create the overlay the reconciler would have, already carrying a generation."""
    from netbox_nso_plugin.intent_generation import allocate_intent_generation
    from netbox_nso_plugin.models import NSOApplyAttempt, NSOStaticRouteState

    with transaction.atomic():
        attempt_id = NSOApplyAttempt.objects.create(management=mgmt).pk if status == "deploying" else None
        return NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=sr,
            status=status,
            nso_vrf=mirror_vrf if mirror_vrf is not None else (sr.vrf.name if sr.vrf else ""),
            nso_prefix=str(sr.prefix or ""),
            nso_next_hop=str(sr.next_hop or ""),
            accepted_at=timezone.now(),
            apply_attempt_id=attempt_id,
            intent_generation=allocate_intent_generation(),
            generation_started_at=timezone.now(),
        )
