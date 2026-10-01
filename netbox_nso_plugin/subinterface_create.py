# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Create an owned subinterface through the renderer mutation protocol."""

from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import NSOSubinterfaceState
from .ownership_grants import OwnershipGrant
from .renderer_writer import RendererMutationPlan, planned_save, renderer_writes
from .signals import _schedule_intent_push, suppress_intent_push
from .subinterface_identity import subinterface_errors, subinterface_parent_errors


def create_subinterface(management, parent, unit, dot1q_vlan, vrf):
    """Create the native child and accepted overlay as one planned mutation."""
    parent_errors = subinterface_parent_errors(management, parent)
    if parent_errors:
        raise ValidationError({"parent": parent_errors})
    unit_text = str(unit)
    if not unit_text.isascii() or not unit_text.isdecimal():
        raise ValidationError({"unit": "Unit must be a non-negative integer."})
    name = f"{parent.name}.{unit_text}"
    if Interface.objects.filter(device_id=management.device_id, name=name).exists():
        raise ValidationError({"unit": "This unit already exists on the parent."})
    interface = Interface(device=management.device, name=name, type="virtual", parent=parent)
    interface._site = management.device.site
    interface._location = management.device.location
    interface._rack = management.device.rack
    now = timezone.now()
    state = NSOSubinterfaceState(
        management=management,
        interface=interface,
        parent_interface=parent,
        dot1q_vlan=dot1q_vlan,
        vrf=vrf,
        status="accepted",
        accepted_at=now,
    )
    errors = subinterface_errors(state)
    if errors:
        raise ValidationError(errors)

    def validate_after_acquire():
        current_parent = Interface.objects.filter(pk=parent.pk).first()
        current_errors = subinterface_parent_errors(management, current_parent)
        if current_errors:
            raise ValidationError({"parent": current_errors})
        if current_parent.name != parent.name:
            raise ValidationError({"parent": "Parent changed. Refresh the page and try again."})
        if NSOSubinterfaceState.objects.filter(
            management=management, parent_interface=current_parent, dot1q_vlan=dot1q_vlan
        ).exists():
            raise ValidationError({"dot1q_vlan": "This tag is already used on the parent."})

    plan = RendererMutationPlan.build(
        grant=OwnershipGrant("create"),
        saves=(
            planned_save(interface, force_insert=True, natural_key=("device", "name")),
            planned_save(
                state,
                force_insert=True,
                natural_key=("management", "interface"),
                references=(("interface", interface),),
            ),
        ),
        read_dependencies=(parent,),
        validate_after_acquire=validate_after_acquire,
        planned_at=now,
    )
    with renderer_writes(plan) as writer:
        with suppress_intent_push():
            writer.save(interface, force_insert=True)
            writer.save(state, force_insert=True)
        _schedule_intent_push((management.device_id, "subinterface"))
    return state
