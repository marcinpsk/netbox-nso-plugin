# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/ospf.

Pins the JSON shape the plugin CONSUMES in ``template_content._reconcile_ospf``
against the documented adapter contract. Optional keys are omitted when unset.

Canonical contract: ``nso-adapter/docs/api-contract.md`` § "GET .../ospf".
Mirror (producer side): ``nso-adapter/tests/api/test_contract_ospf.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOOSPFInstanceState, NSOOSPFInterfaceState
from netbox_nso_plugin.template_content import _reconcile_ospf

REQUIRED_TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "instances", "interfaces"}
REQUIRED_INSTANCE_KEYS = {"process_id", "vrf", "areas"}
OPTIONAL_INSTANCE_KEYS = {"router_id"}
REQUIRED_IFACE_KEYS = {"interface_name", "passive", "auth_present"}
OPTIONAL_IFACE_KEYS = {"process_id", "area_id", "priority", "cost", "network_type", "auth_type"}

CONTRACT_PAYLOAD = {
    "device_id": 7930,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "instances": [
        {"process_id": "1", "vrf": "", "areas": ["0.0.0.0"], "router_id": "10.0.0.1"},
        {"process_id": "2", "vrf": "", "areas": []},
    ],
    "interfaces": [
        {
            "interface_name": "GE0/0",
            "passive": True,
            "auth_present": True,
            "process_id": "1",
            "area_id": "0.0.0.0",
            "priority": 10,
            "cost": 100,
            "network_type": "point-to-point",
            "auth_type": "md5",
        },
        {"interface_name": "GE0/1", "passive": False, "auth_present": False},
    ],
}


class TestOspfContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="OsCt", slug="osct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="OsCtDev", slug="osctdev")
        role = DeviceRole.objects.create(name="OsCtRole", slug="osctrole")
        site = Site.objects.create(name="OsCtSite", slug="osctsite")
        cls.device = Device.objects.create(name="os-ct-rtr", device_type=dt, role=role, site=site)
        # Interfaces must exist for the OSPF interface reconcile to link them.
        for name in ("GE0/0", "GE0/1"):
            Interface.objects.create(device=cls.device, name=name, type="1000base-t")
        inst = NSOInstance.objects.create(name="os-ct-inst", adapter_instance_id="os-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="os-ct", adapter_device_id=7930
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), REQUIRED_TOP_KEYS)
        insts = {i["process_id"]: i for i in CONTRACT_PAYLOAD["instances"]}
        self.assertEqual(set(insts["1"].keys()), REQUIRED_INSTANCE_KEYS | OPTIONAL_INSTANCE_KEYS)
        self.assertEqual(set(insts["2"].keys()), REQUIRED_INSTANCE_KEYS)
        ifaces = {i["interface_name"]: i for i in CONTRACT_PAYLOAD["interfaces"]}
        self.assertEqual(set(ifaces["GE0/0"].keys()), REQUIRED_IFACE_KEYS | OPTIONAL_IFACE_KEYS)
        self.assertEqual(set(ifaces["GE0/1"].keys()), REQUIRED_IFACE_KEYS)

    def test_consumer_reads_contract_payload(self):
        """_reconcile_ospf ingests the documented shape into the OSPF overlays."""
        import copy

        # _reconcile_ospf normalises (mutates) interface entries in place, so feed a
        # copy to keep the shared CONTRACT_PAYLOAD fixture pristine for the keys test.
        result = _reconcile_ospf(self.device, copy.deepcopy(CONTRACT_PAYLOAD))

        self.assertEqual(NSOOSPFInstanceState.objects.filter(management=self.mgmt).count(), 2)
        self.assertEqual(NSOOSPFInterfaceState.objects.filter(management=self.mgmt).count(), 2)
        self.assertEqual(len(result["instances"]), 2)
        self.assertEqual(len(result["interfaces"]), 2)

        inst = NSOOSPFInstanceState.objects.get(management=self.mgmt, process_id="1")
        self.assertEqual(inst.router_id, "10.0.0.1")
        self.assertEqual(inst.areas, ["0.0.0.0"])

        iface = NSOOSPFInterfaceState.objects.get(management=self.mgmt, interface__name="GE0/0")
        self.assertEqual(iface.area_id, "0.0.0.0")
        self.assertEqual(iface.cost, 100)
        self.assertTrue(iface.passive)
        self.assertTrue(iface.auth_present)
