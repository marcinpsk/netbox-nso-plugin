# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M36: plugin dot1q subinterface reconciler — virtual interface + parent link + overlay."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase
from ipam.models import VLAN

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOSubinterfaceState

from .mixins import IntentPushResetMixin


def _make_device(tag="m36"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"UMfg{tag}", slug=f"umfg{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"UDev{tag}", slug=f"udev{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"URole{tag}", slug=f"urole{tag}")
    site, _ = Site.objects.get_or_create(name=f"USite{tag}", slug=f"usite{tag}")
    return Device.objects.create(name=f"rtr-{tag}", device_type=dt, role=role, site=site)


class TestSubinterfaceReconciler(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="nso-dev", adapter_instance_id="nso-dev")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="rtr-m36"
        )
        cls.parent = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        orphan = _make_device("orphan")
        assert (
            reconcile_subinterface(orphan, {"interfaces": [{"interface_name": "Gi0/1.100", "dot1q_vlan": 100}]}) == []
        )

    def test_creates_subinterface_with_parent_and_records_dot1q(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        rows = reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.100",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 100,
                        "type": "subinterface",
                        "vrf": "TENANT_A",
                    }
                ]
            },
        )
        self.assertEqual(len(rows), 1)
        sub = Interface.objects.get(device=self.device, name="GigabitEthernet0/1.100")
        self.assertEqual(sub.type, "virtual")
        self.assertEqual(sub.parent_id, self.parent.id)
        self.assertEqual(rows[0].dot1q_vlan, 100)
        self.assertEqual(rows[0].vrf, "TENANT_A")
        self.assertEqual(rows[0].status, "imported")
        # A dot1q tag must NOT create a device VLAN object.
        self.assertEqual(VLAN.objects.count(), 0)

    def test_missing_parent_creates_subif_without_parent_flagged_changed(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        rows = reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet9/9.300",
                        "parent_interface": "GigabitEthernet9/9",
                        "dot1q_vlan": 300,
                        "type": "subinterface",
                    }
                ]
            },
        )
        sub = Interface.objects.get(device=self.device, name="GigabitEthernet9/9.300")
        self.assertIsNone(sub.parent_id)
        self.assertEqual(rows[0].status, "changed")  # missing parent flagged for review

    def test_existing_interface_is_reused_not_duplicated(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        Interface.objects.create(device=self.device, name="GigabitEthernet0/1.200", type="virtual")
        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.200",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 200,
                        "type": "subinterface",
                    }
                ]
            },
        )
        self.assertEqual(Interface.objects.filter(device=self.device, name="GigabitEthernet0/1.200").count(), 1)

    def test_stale_state_pruned(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.300",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 300,
                    }
                ]
            },
        )
        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.301",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 301,
                    }
                ]
            },
        )
        names = set(
            NSOSubinterfaceState.objects.filter(management=self.management).values_list("interface__name", flat=True)
        )
        self.assertEqual(names, {"GigabitEthernet0/1.301"})


class TestSubinterfaceWritePath(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("wp")
        cls.instance = NSOInstance.objects.create(name="nso-wp", adapter_instance_id="nso-wp")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="rtr-wp", adapter_device_id=42
        )
        cls.parent = Interface.objects.create(device=cls.device, name="ge-0/0/0", type="1000base-t")

    def _state(self, name="ge-0/0/0.100", dot1q=100, status="imported"):
        iface = Interface.objects.create(device=self.device, name=name, type="virtual", parent=self.parent)
        return NSOSubinterfaceState.objects.create(
            management=self.management,
            interface=iface,
            parent_interface=self.parent,
            dot1q_vlan=dot1q,
            vrf="MTI",
            status=status,
        )

    def test_reconcile_preserves_owned_status(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "ge-0/0/0.100",
                        "parent_interface": "ge-0/0/0",
                        "dot1q_vlan": 100,
                        "type": "subinterface",
                    }
                ]
            },
        )
        self.assertEqual(NSOSubinterfaceState.objects.get(interface__name="ge-0/0/0.100").status, "accepted")

    def test_push_builds_owned_snapshot(self):
        from unittest.mock import patch

        from netbox_nso_plugin.signals import _push_subinterface_intent_for_device, reset_intent_push_state

        self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        self._state(name="ge-0/0/0.200", dot1q=200, status="imported")  # not owned → excluded
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent") as mock_put:
            _push_subinterface_intent_for_device(self.device.pk, 42)
        mock_put.assert_called_once()
        ifaces = mock_put.call_args[0][1]
        assert [i["interface_name"] for i in ifaces] == ["ge-0/0/0.100"]
        assert ifaces[0]["dot1q_vlan"] == 100
        assert ifaces[0]["parent_interface"] == "ge-0/0/0"
        assert ifaces[0]["vrf"] == "MTI"

    def test_accept_marks_owned(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        state = self._state(name="ge-0/0/0.300", dot1q=300, status="conflict")
        User = get_user_model()
        admin = User.objects.create_superuser(username="subif-admin", password="pw", email="s@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent"):
            resp = self.client.post(f"/plugins/nso/subinterface/state/{state.pk}/accept/")
        assert resp.status_code == 302
        state.refresh_from_db()
        assert state.status == "accepted" and state.accepted_at is not None
