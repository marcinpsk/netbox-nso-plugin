# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 2b: plugin interface-MTU reconciler — read-only mirror overlay."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceMtuState


def _make_device(tag="mtu"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"MtuMfg{tag}", slug=f"mtumfg{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"MtuDev{tag}", slug=f"mtudev{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"MtuRole{tag}", slug=f"mturole{tag}")
    site, _ = Site.objects.get_or_create(name=f"MtuSite{tag}", slug=f"mtusite{tag}")
    return Device.objects.create(name=f"rtr-{tag}", device_type=dt, role=role, site=site)


class TestInterfaceMtuReconciler(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="nso-mtu", adapter_instance_id="nso-mtu")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="rtr-mtu"
        )
        cls.po1 = Interface.objects.create(device=cls.device, name="Port-channel1", type="lag")
        cls.lag99 = Interface.objects.create(device=cls.device, name="LAG99:99", type="virtual")

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        orphan = _make_device("orphan")
        assert reconcile_interface_mtu(orphan, {"interfaces": [{"interface_name": "X", "mtu": 9000}]}) == []

    def test_mirrors_l2_and_ip_mtu(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        rows = reconcile_interface_mtu(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "Port-channel1",
                        "mtu": 9216,
                        "ip_mtu": None,
                        "mpls_mtu": None,
                        "bound_port": "",
                    },
                    {
                        "interface_name": "LAG99:99",
                        "mtu": None,
                        "ip_mtu": 9170,
                        "mpls_mtu": None,
                        "bound_port": "lag-99",
                    },
                ]
            },
        )
        self.assertEqual(len(rows), 2)
        po = NSOInterfaceMtuState.objects.get(interface=self.po1)
        self.assertEqual(po.l2_mtu, 9216)
        self.assertIsNone(po.ip_mtu)
        self.assertEqual(po.status, "imported")
        lag = NSOInterfaceMtuState.objects.get(interface=self.lag99)
        self.assertEqual(lag.ip_mtu, 9170)
        self.assertEqual(lag.bound_port, "lag-99")

    def test_interface_absent_in_netbox_is_skipped(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        rows = reconcile_interface_mtu(
            self.device,
            {"interfaces": [{"interface_name": "TenGig9/9/9", "mtu": 9216}]},
        )
        self.assertEqual(rows, [])
        self.assertEqual(NSOInterfaceMtuState.objects.count(), 0)

    def test_stale_state_pruned(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "Port-channel1", "mtu": 9216}]})
        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "LAG99:99", "ip_mtu": 9170}]})
        names = set(
            NSOInterfaceMtuState.objects.filter(management=self.management).values_list("interface__name", flat=True)
        )
        self.assertEqual(names, {"LAG99:99"})

    def test_value_update_on_resync(self):
        from netbox_nso_plugin.interface_mtu_reconciler import reconcile_interface_mtu

        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "Port-channel1", "mtu": 9216}]})
        reconcile_interface_mtu(self.device, {"interfaces": [{"interface_name": "Port-channel1", "mtu": 1500}]})
        self.assertEqual(NSOInterfaceMtuState.objects.get(interface=self.po1).l2_mtu, 1500)
