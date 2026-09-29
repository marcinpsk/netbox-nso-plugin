# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Identity checks shared by subinterface ownership and delivery."""

from .models import NSOSwitchportState


def subinterface_parent_identity_errors(management, parent):
    """Return errors when a parent does not belong to the managed device."""
    if parent is None or parent.device_id != management.device_id:
        return ["Parent must belong to this managed device."]
    return []


def subinterface_parent_errors(management, parent):
    """Return errors that make a parent ineligible for a new subinterface."""
    identity_errors = subinterface_parent_identity_errors(management, parent)
    if identity_errors:
        return identity_errors
    if parent.name.lower().startswith(("lo", "irb")):
        return ["Loopback and IRB interfaces cannot be subinterface parents."]
    if parent.parent_id is not None:
        return ["A subinterface cannot be a subinterface parent."]
    if parent.name.lower().startswith("vlan"):
        return ["An SVI cannot be a subinterface parent."]
    if parent.mode or NSOSwitchportState.objects.filter(management=management, interface=parent).exists():
        return ["A switchport cannot be a subinterface parent."]
    return []


def subinterface_errors(row):
    """Return field errors for a subinterface overlay or proposed overlay."""
    errors = {}
    interface = row.interface
    parent = row.parent_interface
    device_id = row.management.device_id
    tag = row.dot1q_vlan
    if interface.device_id != device_id:
        errors["interface"] = ["Interface must belong to this managed device."]
    parent_errors = subinterface_parent_identity_errors(row.management, parent)
    if parent_errors:
        errors["parent_interface"] = parent_errors
    elif interface.parent_id != parent.pk:
        errors["parent_interface"] = ["Overlay parent must match the native interface parent."]
    if parent is not None:
        prefix = f"{parent.name}."
        unit = interface.name[len(prefix) :] if interface.name.startswith(prefix) else ""
        if not unit or not unit.isascii() or not unit.isdecimal():
            errors["interface"] = ["Interface name must be <parent name>.<numeric unit>."]
    if tag is None:
        errors["dot1q_vlan"] = ["A dot1q VLAN tag is required."]
    elif not 1 <= tag <= 4094:
        errors["dot1q_vlan"] = ["Must be between 1 and 4094."]
    if (
        parent is not None
        and tag is not None
        and (
            type(row)
            .objects.filter(management=row.management, parent_interface=parent, dot1q_vlan=tag)
            .exclude(pk=row.pk)
            .exists()
        )
    ):
        errors.setdefault("dot1q_vlan", []).append(
            f"dot1q VLAN {tag} is already used by another subinterface on {parent.name}."
        )
    return errors
