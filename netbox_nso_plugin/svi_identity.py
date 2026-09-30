# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Identity checks shared by SVI ownership and delivery."""

from ipam.models import VLAN

from .status_machine import OWNED_STATES


def attached_svi_vlans(management):
    """Return VLANs attached to this managed device by VLAN state."""
    return VLAN.objects.filter(nso_vlan_states__management=management)


def svi_identity_index(management, model):
    """Return attached VLAN and owned VID indexes for one management."""
    attached_vlan_pks = set()
    attached_vids = {}
    for pk, vid in attached_svi_vlans(management).order_by().values_list("pk", "vid").distinct():
        attached_vlan_pks.add(pk)
        attached_vids.setdefault(vid, set()).add(pk)
    owned_vids = {}
    for vid, pk in (
        model.objects.filter(management_id=management.pk, status__in=OWNED_STATES)
        .order_by()
        .values_list("vlan__vid", "pk")
    ):
        owned_vids.setdefault(vid, set()).add(pk)
    return attached_vlan_pks, attached_vids, owned_vids


def svi_errors(row, index=None):
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
        if index is None:
            index = svi_identity_index(row.management, type(row))
        attached_vlan_pks, attached_vids, owned_vids = index
        if not 1 <= vlan.vid <= 4094:
            errors["vlan"] = ["VLAN VID must be between 1 and 4094."]
        if vlan.pk not in attached_vlan_pks:
            errors.setdefault("vlan", []).append("VLAN must be attached to this managed device.")
        if (row.pk is None or row.status not in OWNED_STATES) and len(attached_vids.get(vlan.vid, set())) > 1:
            errors.setdefault("vlan", []).append("VLAN VID is ambiguous on this managed device.")
        if owned_vids.get(vlan.vid, set()) - {row.pk}:
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
