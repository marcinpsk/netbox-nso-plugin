# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Identity checks shared by SVI ownership and delivery."""

from ipam.models import VLAN

from .status_machine import OWNED_STATES


def attached_svi_vlans(management):
    """Return VLANs attached to this managed device by VLAN state."""
    return VLAN.objects.filter(nso_vlan_states__management=management)


def svi_errors(row):
    """Return field errors for an SVI overlay or proposed overlay."""
    errors = {}
    interface = row.interface
    vlan = row.vlan
    device_id = row.management.device_id
    if interface.device_id != device_id:
        errors["interface"] = ["Interface must belong to this managed device."]
    if vlan is None:
        errors["vlan"] = ["A device VLAN is required."]
    else:
        if not 1 <= vlan.vid <= 4094:
            errors["vlan"] = ["VLAN VID must be between 1 and 4094."]
        if not attached_svi_vlans(row.management).filter(pk=vlan.pk).exists():
            errors.setdefault("vlan", []).append("VLAN must be attached to this managed device.")
        if (row.pk is None or row.status not in OWNED_STATES) and attached_svi_vlans(row.management).filter(
            vid=vlan.vid
        ).values("pk").distinct().count() > 1:
            errors.setdefault("vlan", []).append("VLAN VID is ambiguous on this managed device.")
        if (
            type(row)
            .objects.filter(management_id=row.management_id, vlan__vid=vlan.vid, status__in=OWNED_STATES)
            .exclude(pk=row.pk)
            .exists()
        ):
            errors.setdefault("vlan", []).append("VLAN VID is already bound to an owned SVI on this device.")
    name = interface.name
    if row.svi_type == "irb":
        unit = name[4:] if name.startswith("irb.") else ""
        if not unit or not unit.isascii() or not unit.isdecimal():
            errors["interface"] = ["Junos SVI name must be irb.<numeric unit>."]
    elif row.svi_type == "svi":
        if vlan is not None and name != f"Vlan{vlan.vid}":
            errors["interface"] = ["Cisco SVI name must match Vlan<VLAN VID>."]
        elif vlan is None and not name.startswith("Vlan"):
            errors["interface"] = ["Cisco SVI name must be Vlan<VLAN VID>."]
    else:
        errors["svi_type"] = ["SVI type must be svi or irb."]
    return errors
