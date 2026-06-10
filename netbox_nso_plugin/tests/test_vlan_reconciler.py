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

    def test_moved_vlan_stays_synced_not_duplicated(self):
        """A VLAN re-scoped to a broader group is followed via the overlay FK, not duplicated."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        per_device = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.get(group=per_device, vid=10)

        # Operator re-scopes the VLAN into a shared, site-wide group.
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")
        vlan.group = site
        vlan.save()

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})

        # No duplicate created back in the per-device group; the moved VLAN is reused.
        self.assertEqual(VLAN.objects.filter(vid=10).count(), 1)
        self.assertFalse(VLAN.objects.filter(group=per_device, vid=10).exists())
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=10)
        self.assertEqual(state.vlan_id, vlan.pk)
        self.assertEqual(state.vlan.group_id, site.pk)

    def test_moved_vlan_still_drifts_when_device_drops_it(self):
        """Stale detection follows the overlay FK even after a re-scope out of the per-device group."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}, {"vlan_id": 20, "name": "DATA"}]}
        )
        vlan10 = NSOVLANState.objects.get(management=self.management, vlan__vid=10).vlan
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")
        vlan10.group = site
        vlan10.save()

        # Device now reports only VLAN 20 → the moved VLAN 10 must still surface as drift.
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 20, "name": "DATA"}]})
        state10 = NSOVLANState.objects.get(management=self.management, vlan__vid=10)
        self.assertEqual(state10.status, "changed")

    def test_switchport_anchors_to_moved_vlan(self):
        """A trunk's tagged VLAN resolves to the re-scoped VLAN, not a per-device duplicate."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        vlan10 = NSOVLANState.objects.get(management=self.management, vlan__vid=10).vlan
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")
        vlan10.group = site
        vlan10.save()

        reconcile_switchport(
            self.device,
            {"interfaces": [{"interface_name": "GigabitEthernet0/1", "mode": "trunk", "tagged_vlans": [10]}]},
        )
        self.interface.refresh_from_db()
        self.assertEqual(VLAN.objects.filter(vid=10).count(), 1)
        self.assertEqual(list(self.interface.tagged_vlans.values_list("pk", flat=True)), [vlan10.pk])

    def test_switchport_seeded_when_pristine(self):
        """A pristine NetBox interface is SEEDED from the device (read mirror) → imported, no drift.

        This is the fix for false drift on freshly-imported switchports: an interface with
        no L2 config gets the device's mode/native/tagged written onto it (like VLAN seeds
        its name), instead of being flagged as 'changed'.
        """
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
        self.assertEqual(rows[0].status, "imported")  # seeded, not false drift
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mode, "access")  # device L2 materialised onto NetBox
        self.assertEqual(self.interface.untagged_vlan.vid, 10)

    def test_switchport_imported_when_netbox_matches_nso(self):
        # Non-pristine interface already matching the device → imported (no drift), no clobber.
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

    def test_switchport_changed_when_netbox_has_divergent_value(self):
        """A non-pristine interface whose L2 differs from the device → changed, NOT clobbered."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}, {"vlan_id": 20, "name": "DATA"}]}
        )
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        # NetBox already carries access vlan 20; the device says access vlan 10 → divergence.
        self.interface.mode = "access"
        self.interface.untagged_vlan = VLAN.objects.get(group=group, vid=20)
        self.interface.save()

        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {"interface_name": "GigabitEthernet0/1", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
                ]
            },
        )
        self.assertEqual(rows[0].status, "changed")
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan.vid, 20)  # operator value NOT clobbered

    def test_switchport_native_vlan_1_normalized(self):
        """IOS implicit default native VLAN 1 is treated as 'no native' (no false drift)."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1",
                        "mode": "trunk-all",
                        "untagged_vlan": 1,
                        "tagged_vlans": [],
                    }
                ]
            },
        )
        self.assertEqual(rows[0].status, "imported")  # pristine → seeded, in sync
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mode, "tagged-all")
        self.assertIsNone(self.interface.untagged_vlan)  # native VLAN 1 normalised away


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

    def test_force_repushes_owned_vlan_with_live_name(self):
        """Apply force-pushes owned VLANs so a post-accept rename (no signal) still ships.

        Renaming the ipam.VLAN fires no plugin signal, so the row stays 'in_sync' and a
        normal push dedups. The single Apply calls the push with force=True, which
        bypasses the dedup and ships the LIVE NetBox name.
        """
        from unittest.mock import patch

        from netbox_nso_plugin.signals import _push_vlan_intent_for_device, reset_intent_push_state

        self._state(vid=2213, name="LIVE_RENAMED", status="in_sync", device_name="OLD")
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as mock_put:
            _push_vlan_intent_for_device(self.device.pk, 77)  # first push
            _push_vlan_intent_for_device(self.device.pk, 77)  # unchanged → deduped
            self.assertEqual(mock_put.call_count, 1)
            _push_vlan_intent_for_device(self.device.pk, 77, force=True)  # Apply path
            self.assertEqual(mock_put.call_count, 2)
            self.assertEqual(mock_put.call_args[0][1], [{"vlan_id": 2213, "name": "LIVE_RENAMED"}])

    def test_prepare_apply_pushes_vlan_intent(self):
        """_prepare_apply ships owned VLAN intent (not just LACP/switchport)."""
        from unittest.mock import patch

        from netbox_nso_plugin.signals import reset_intent_push_state
        from netbox_nso_plugin.views import _prepare_apply

        mgmt = NSODeviceManagement.objects.get(pk=self.management.pk)
        mgmt.adapter_device_id = 77
        mgmt.save(update_fields=["adapter_device_id"])
        self._state(vid=2213, name="LIVE_RENAMED", status="in_sync", device_name="OLD")
        reset_intent_push_state()
        # LACP/switchport pushes hit the (blocked) adapter network and are swallowed by
        # _prepare_apply's per-push try/except; only the VLAN push is asserted here.
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as mock_vlan:
            _prepare_apply(mgmt)
        mock_vlan.assert_called_once()
        self.assertEqual(mock_vlan.call_args[0][1], [{"vlan_id": 2213, "name": "LIVE_RENAMED"}])

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
