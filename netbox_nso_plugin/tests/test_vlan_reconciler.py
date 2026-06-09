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
from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

from .mixins import IntentPushResetMixin


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

    def test_switchport_imported_when_netbox_matches_nso(self):
        # Unified machine: an unowned row that matches the device rests at 'imported'
        # (in_sync is reserved for owned+applied), not 'in_sync' as the old code set.
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
        self.assertEqual(rows[0].status, "imported")

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


class TestVlanWritePath(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("vwp")
        cls.instance = NSOInstance.objects.create(name="nso-vwp", adapter_instance_id="nso-vwp")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="vlan-router-vwp", adapter_device_id=77
        )

    def _state(self, vid=2213, name="OLD", status="imported", device_name="OLD"):
        group = _device_vlan_group(self.device)
        vlan = VLAN.objects.create(group=group, vid=vid, name=name)
        return NSOVLANState.objects.create(
            management=self.management, vlan=vlan, device_name=device_name, status=status
        )

    def test_push_builds_owned_snapshot_with_live_name(self):
        from unittest.mock import patch

        from netbox_nso_plugin.signals import _push_vlan_intent_for_device, reset_intent_push_state

        owned = self._state(vid=2213, name="RENAMED", status="accepted", device_name="OLD")
        self._state(vid=10, name="MGMT", status="imported")  # not owned → excluded
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as mock_put:
            _push_vlan_intent_for_device(self.device.pk, 77)
        mock_put.assert_called_once()
        vlans = mock_put.call_args[0][1]
        assert vlans == [{"vlan_id": 2213, "name": "RENAMED"}]  # live NetBox name, owned only
        del owned

    def test_accept_marks_owned(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        state = self._state(vid=2213, name="RENAMED", status="conflict")
        User = get_user_model()
        admin = User.objects.create_superuser(username="vlan-admin", password="pw", email="v@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent"):
            resp = self.client.post(f"/plugins/nso/vlan/state/{state.pk}/accept/")
        assert resp.status_code == 302
        state.refresh_from_db()
        assert state.status == "accepted" and state.accepted_at is not None

    def test_owned_settles_in_sync_when_device_matches(self):
        """An accepted VLAN whose device name now matches NetBox → in_sync (apply landed)."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        self._state(vid=2213, name="FW-01", status="accepted")
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2213, "name": "FW-01"}]})
        assert NSOVLANState.objects.get(vlan__vid=2213).status == "in_sync"

    def test_owned_stays_accepted_when_device_differs(self):
        """An accepted VLAN whose device name still differs stays pending (accepted)."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        self._state(vid=2300, name="NEW", status="accepted")
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2300, "name": "OLD"}]})
        assert NSOVLANState.objects.get(vlan__vid=2300).status == "accepted"
