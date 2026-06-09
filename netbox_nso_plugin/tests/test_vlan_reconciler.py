# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M34: VLAN + switchport reconcile into NetBox (per-device VLANGroup + native L2)."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase
from ipam.models import VLAN, VLANGroup

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOVLANState,
)


def _make_device(tag="m34"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"VMfg{tag}", slug=f"vmfg{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"VDev{tag}", slug=f"vdev{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"VRole{tag}", slug=f"vrole{tag}")
    site, _ = Site.objects.get_or_create(name=f"VSite{tag}", slug=f"vsite{tag}")
    return Device.objects.create(name=f"vlan-router-{tag}", device_type=dt, role=role, site=site)


class TestVlanReconciler(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="nso-dev", adapter_instance_id="nso-dev")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="vlan-router-m34"
        )
        cls.interface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")

    def test_vlan_reconciler_creates_group_scoped_state(self):
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        rows = reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}, {"vlan_id": 20, "name": "DATA"}]}
        )
        self.assertEqual(len(rows), 2)
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        self.assertTrue(VLAN.objects.filter(group=group, vid=10).exists())
        self.assertTrue(
            NSOVLANState.objects.filter(management=self.management, vlan__group=group, vlan__vid=10).exists()
        )

    def test_operator_rename_is_drift_not_clobbered(self):
        """Renaming a VLAN in NetBox must surface as drift, not be reverted to the device name."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2213, "name": "OLD_NAME"}]})
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.get(group=group, vid=2213)

        # Operator renames the VLAN in NetBox.
        vlan.name = "NEW_NAME"
        vlan.save()

        # Next reconcile (e.g. opening the NSO tab) must NOT revert the rename.
        rows = reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2213, "name": "OLD_NAME"}]})
        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "NEW_NAME")  # not clobbered back to OLD_NAME
        self.assertEqual(rows[0].status, "changed")  # drift surfaced
        self.assertEqual(rows[0].device_name, "OLD_NAME")  # device value mirrored for display

    def test_switchport_in_sync_when_netbox_matches_nso(self):
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        vlan10 = VLAN.objects.get(group=group, vid=10)
        self.interface.mode = "access"
        self.interface.untagged_vlan = vlan10
        self.interface.save()

        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {"interface_name": "GigabitEthernet0/1", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
                ]
            },
        )
        self.assertEqual(rows[0].status, "in_sync")

    def test_switchport_changed_when_netbox_differs(self):
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {"interface_name": "GigabitEthernet0/1", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
                ]
            },
        )
        self.assertEqual(rows[0].status, "changed")
