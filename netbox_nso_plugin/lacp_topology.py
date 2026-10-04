# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Native NetBox LAG topology and complete LACP ownership episodes."""

from __future__ import annotations

import copy

from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.db.models import F, Q


def is_bundle(interface, device_id=None):
    """Return whether an interface is a native LAG for the selected device."""
    return interface.type == "lag" and (device_id is None or interface.device_id == device_id)


def bundle_interfaces(device_id):
    """Return native LAG interfaces for one device."""
    return Interface.objects.filter(device_id=device_id, type="lag").order_by("pk")


def bundle_state_filter():
    """Select bundle overlays with a qualifying native anchor."""
    return Q(interface__type="lag", interface__device_id=F("management__device_id"))


def member_interfaces(device_id):
    """Return members with a LAG on the same device."""
    return Interface.objects.filter(device_id=device_id, lag__type="lag", lag__device_id=F("device_id")).order_by("pk")


def bundle_of(interface):
    """Resolve a member's qualifying native LAG, or fail closed."""
    bundle = interface.lag
    if bundle is None or not is_bundle(bundle, interface.device_id):
        return None
    return bundle


def members_of(bundle):
    """Return the qualifying native members of a bundle."""
    return member_interfaces(bundle.device_id).filter(lag_id=bundle.pk)


def member_states(bundle_state):
    """Return tracked native members without acquiring NetBox-only interfaces."""
    from .models import NSOLACPMemberState

    return (
        NSOLACPMemberState.objects.filter(
            management_id=bundle_state.management_id,
            interface__device_id=bundle_state.interface.device_id,
            interface__lag_id=bundle_state.interface_id,
            interface__lag__type="lag",
            interface__lag__device_id=F("interface__device_id"),
        )
        .filter(interface__device_id=F("management__device_id"))
        .select_related("interface", "interface__lag")
        .order_by("pk")
    )


def interface_overlay_querysets(interface_id, *, lag_ids=()):
    """Find overlays of an interface and both sides of a native move."""
    from .models import NSOLACPBundleState, NSOLACPMemberState

    native_parent = Interface.objects.filter(pk=interface_id).values("lag_id")
    parents = {interface_id, *lag_ids}
    return (
        NSOLACPBundleState.objects.filter(
            Q(interface_id__in=parents) | Q(interface_id__in=native_parent)
        ).select_related("management"),
        NSOLACPMemberState.objects.filter(
            Q(interface_id=interface_id) | Q(interface__lag_id=interface_id)
        ).select_related("management"),
    )


def native_validation_message(interface, **changes):
    """Return NetBox's reason for refusing one projected native interface."""
    candidate = copy.copy(interface)
    for field, value in changes.items():
        setattr(candidate, field, value)
    try:
        candidate.full_clean()
    except ValidationError as exc:
        return "; ".join(exc.messages)
    return ""


def bundle_acquisition_blockers(bundle_state):
    """Require the complete reported membership before any bundle acquisition."""
    from .models import NSOLACPMemberState

    bundle = bundle_state.interface
    blockers = []
    if bundle_state.vpc_sensitive:
        blockers.append(f"LACP bundle {bundle.name} is vPC-protected and cannot be onboarded.")
    if not is_bundle(bundle, bundle_state.management.device_id):
        reason = native_validation_message(bundle, type="lag")
        blockers.append(f"NetBox does not model {bundle.name} as a LAG" + (f": {reason}" if reason else "."))
    reported = set(bundle_state.observed_members)
    interfaces = {row.name: row for row in Interface.objects.filter(device_id=bundle.device_id).select_related("lag")}
    overlays = {
        row.interface_id: row for row in NSOLACPMemberState.objects.filter(management_id=bundle_state.management_id)
    }
    for name in sorted(reported):
        interface = interfaces.get(name)
        if interface is None:
            blockers.append(f"Reported member {name} is missing from NetBox.")
            continue
        if bundle_of(interface) is None or interface.lag_id != bundle.pk:
            reason = native_validation_message(interface, lag=bundle)
            blockers.append(
                f"NetBox does not model {name} as a member of {bundle.name}" + (f": {reason}" if reason else ".")
            )
        if interface.pk not in overlays:
            blockers.append(f"Reported member {name} has no LACP member observation.")
    for row in member_states(bundle_state):
        if row.interface.name not in reported:
            blockers.append(f"Native member {row.interface.name} is not reported by the device in {bundle.name}.")
    return tuple(blockers)


def acquisition_signature(bundle_state):
    """Freeze native membership, observed names, overlay identities, and vPC protection."""
    from .models import NSOLACPMemberState

    native = tuple(members_of(bundle_state.interface))
    reported = set(bundle_state.observed_members)
    interfaces = (
        Interface.objects.filter(device_id=bundle_state.interface.device_id)
        .filter(Q(pk__in=[row.pk for row in native]) | Q(name__in=reported))
        .order_by("pk")
    )
    overlays = NSOLACPMemberState.objects.filter(
        management_id=bundle_state.management_id, interface_id__in=interfaces.values("pk")
    ).order_by("pk")
    return (
        bundle_state.pk,
        bundle_state.interface_id,
        bundle_state.interface.type,
        tuple(bundle_state.observed_members),
        bundle_state.vpc_sensitive,
        tuple((row.pk, row.name, row.lag_id) for row in interfaces),
        tuple((row.pk, row.interface_id) for row in overlays),
    )


def acquisition_validator(bundle_state):
    """Recheck the original complete selection under the renderer writer locks."""
    from .models import NSOLACPBundleState
    from .ownership_planner import OwnershipNotQualified
    from .renderer_writer import IntentPlanStaleError

    blockers = bundle_acquisition_blockers(bundle_state)
    if blockers:
        raise OwnershipNotQualified(" ".join(blockers), device_id=bundle_state.interface.device_id)
    expected = acquisition_signature(bundle_state)

    def validate():
        current = NSOLACPBundleState.objects.select_related("interface").filter(pk=bundle_state.pk).first()
        if current is None or acquisition_signature(current) != expected:
            raise IntentPlanStaleError("The LACP bundle membership or observation changed after selection.")
        blockers = bundle_acquisition_blockers(current)
        if blockers:
            raise OwnershipNotQualified(" ".join(blockers), device_id=bundle_state.interface.device_id)

    return validate


def owned_manifest_interfaces(management):
    """Protect native rows for the full owned episode, including missing overlays."""
    from .models import NSOOwnershipManifest

    return set(
        NSOOwnershipManifest.objects.filter(
            device_id=management.device_id, scope="lacp", ownership_state="owned"
        ).values_list("native_id", flat=True)
    )


def topology_snapshot(management):
    """Capture decision-time native rows, overlay rows, and ownership episodes."""
    from .models import NSOLACPBundleState, NSOLACPMemberState, NSOOwnershipManifest
    from .renderer_writer import _field_values

    groups = (
        Interface.objects.filter(device_id=management.device_id),
        NSOLACPBundleState.objects.filter(management=management),
        NSOLACPMemberState.objects.filter(management=management),
        NSOOwnershipManifest.objects.filter(device_id=management.device_id, scope="lacp"),
    )
    return tuple(tuple((row.pk, _field_values(row, None)) for row in rows.order_by("pk")) for rows in groups)


def topology_validator(management, expected):
    """Refuse changed ownership or topology before executing a passive plan."""
    from .renderer_writer import IntentPlanStaleError

    def validate():
        if topology_snapshot(management) != expected:
            raise IntentPlanStaleError("LACP ownership or native topology changed after planning.")

    return validate


def bundle_episode_retirement(management, bundle_interface):
    """Freeze complete bundle retirement before an audit or replacement insertion."""
    from .models import NSOLACPBundleState, NSOLACPMemberState, NSOOwnershipManifest
    from .renderer_writer import planned_save
    from .status_machine import OWNED_STATES

    interface_ids = {bundle_interface.pk, *members_of(bundle_interface).values_list("pk", flat=True)}
    manifests = NSOOwnershipManifest.objects.filter(
        device_id=management.device_id, scope="lacp", ownership_state="owned", native_id__in=interface_ids
    ).order_by("pk")
    saves = []
    for current in manifests:
        candidate = copy.copy(current)
        candidate.ownership_state = "retired"
        saves.append(planned_save(candidate, update_fields=("ownership_state",), expected_before=current))
    for model in (NSOLACPBundleState, NSOLACPMemberState):
        for current in model.objects.filter(
            management=management, interface_id__in=interface_ids, status__in=OWNED_STATES
        ).order_by("pk"):
            candidate = copy.copy(current)
            candidate.status = "imported"
            candidate.accepted_at = None
            saves.append(planned_save(candidate, update_fields=("status", "accepted_at"), expected_before=current))
    return tuple(saves)


def execute_frozen_operations(writer, model_labels):
    """Execute only the native and overlay operations frozen by the active plan."""
    from django.apps import apps

    from .renderer_writer import thaw_field_value

    for write in writer.plan.write_set:
        if write.model_label not in model_labels or write.cascade:
            continue
        model = apps.get_model(write.model_label)
        current = model.objects.filter(pk=write.pk).first() if write.pk is not None else None
        if write.operation == "delete":
            writer.delete(current)
            continue
        candidate = copy.copy(current) if current is not None else model(pk=write.pk)
        for name, value in write.values:
            setattr(candidate, name, thaw_field_value(model._meta.get_field(name), value))
        if not (writer.consume_existing_creation(candidate) or writer.consume_applied_save(candidate)):
            writer.save(candidate, update_fields=write.update_fields, force_insert=write.force_insert)
