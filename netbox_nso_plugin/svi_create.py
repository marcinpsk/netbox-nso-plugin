# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Create an owned SVI through the renderer mutation protocol."""

from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.utils import timezone
from ipam.models import VLAN

from . import adapter_client
from .models import NSOSVIState, NSOVLANState
from .renderer_writer import RendererMutationPlan, planned_save, renderer_writes
from .signals import _schedule_intent_push, suppress_intent_push
from .svi_identity import attached_svi_vlans, svi_errors


def svi_type_for_device(management):
    """Return the SVI naming family from the adapter device's NED."""
    if management.adapter_device_id is None:
        raise ValidationError("This device has no adapter mapping. Connect it before creating an SVI.")
    try:
        ned_id = adapter_client.get_device_ned(management.adapter_device_id)
    except adapter_client.AdapterError as exc:
        raise ValidationError("Cannot read the device NED from the adapter. Try again when it is available.") from exc
    if not isinstance(ned_id, str) or not ned_id:
        raise ValidationError("The adapter does not report a device NED. SVI creation is unavailable.")
    if ned_id.startswith("juniper-junos-"):
        return "irb"
    if ned_id.startswith(("cisco-ios-cli", "cisco-iosxe-cli", "cisco-nx-cli")):
        return "svi"
    raise ValidationError("The adapter device NED does not support SVI creation.")


def create_svi(management, vlan, unit, vrf, *, svi_type):
    """Create the native interface and accepted overlay as one planned mutation."""
    if not attached_svi_vlans(management).filter(pk=vlan.pk).exists() or not 1 <= vlan.vid <= 4094:
        raise ValidationError({"vlan": "Choose a VLAN on this managed device with VID 1 through 4094."})
    if svi_type == "irb":
        unit_text = str(unit)
        if not unit_text.isascii() or not unit_text.isdecimal():
            raise ValidationError({"unit": "Junos IRB unit must be a non-negative integer."})
        name = f"irb.{unit_text}"
    else:
        if unit not in (None, ""):
            raise ValidationError({"unit": "Cisco SVI names come from the selected VLAN."})
        name = f"Vlan{vlan.vid}"
    if Interface.objects.filter(device_id=management.device_id, name=name).exists():
        raise ValidationError({"unit" if svi_type == "irb" else "vlan": "This interface already exists."})
    if NSOSVIState.objects.filter(management=management, interface__name=name).exists():
        raise ValidationError({"vlan": "This SVI overlay already exists."})
    interface = Interface(device=management.device, name=name, type="virtual")
    interface._site = management.device.site
    interface._location = management.device.location
    interface._rack = management.device.rack
    now = timezone.now()
    state = NSOSVIState(
        management=management,
        interface=interface,
        vlan=vlan,
        svi_type=svi_type,
        vrf=vrf,
        status="accepted",
        accepted_at=now,
    )
    errors = svi_errors(state)
    if errors:
        raise ValidationError(errors)
    attachment = NSOVLANState.objects.filter(management=management, vlan=vlan).first()
    if attachment is None:
        raise ValidationError({"vlan": "VLAN attachment changed. Refresh the page and try again."})

    def validate_after_acquire():
        current_vlan = VLAN.objects.filter(pk=vlan.pk).first()
        if current_vlan is None or not attached_svi_vlans(management).filter(pk=vlan.pk).exists():
            raise ValidationError({"vlan": "VLAN changed. Refresh the page and try again."})
        if current_vlan.vid != vlan.vid or not 1 <= current_vlan.vid <= 4094:
            raise ValidationError({"vlan": "VLAN VID changed. Refresh the page and try again."})
        state.vlan = current_vlan
        errors = svi_errors(state)
        if errors:
            raise ValidationError(errors)
        if Interface.objects.filter(device_id=management.device_id, name=name).exists():
            raise ValidationError({"unit" if svi_type == "irb" else "vlan": "This interface already exists."})
        if NSOSVIState.objects.filter(management=management, interface__name=name).exists():
            raise ValidationError({"vlan": "This SVI overlay already exists."})

    plan = RendererMutationPlan.build(
        saves=(
            planned_save(interface, force_insert=True, natural_key=("device", "name")),
            planned_save(
                state,
                force_insert=True,
                natural_key=("management", "interface"),
                references=(("interface", interface),),
            ),
        ),
        read_dependencies=(vlan, management.device, attachment),
        validate_after_acquire=validate_after_acquire,
        planned_at=now,
    )
    with renderer_writes(plan) as writer:
        with suppress_intent_push():
            writer.save(interface, force_insert=True)
            writer.save(state, force_insert=True)
        _schedule_intent_push((management.device_id, "svi"))
    return state
