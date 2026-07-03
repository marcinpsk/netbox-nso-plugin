# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/interface-ips.

Addresses grouped per interface; fixed key set at every level. Consumed by
``template_content._reconcile_interface_ips``.

Canonical contract: ``nso-adapter/docs/api-contract.md`` (interface-ips §).
Mirror (producer side): ``nso-adapter/tests/api/test_contract_interface_ips.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceIPState
from netbox_nso_plugin.template_content import _reconcile_interface_ips

TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "interfaces"}
IFACE_KEYS = {"interface", "bound_port", "addresses"}
ADDR_KEYS = {"address", "prefix_length", "family", "secondary", "vrf"}

CONTRACT_PAYLOAD = {
    "device_id": 7980,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "interfaces": [
        {
            "interface": "GE0/0",
            "bound_port": "GE0/0",
            "addresses": [
                {"address": "10.0.0.1/24", "prefix_length": 24, "family": "ipv4", "secondary": False, "vrf": ""}
            ],
        }
    ],
}


class TestInterfaceIpsContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="IpCt", slug="ipct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IpCtDev", slug="ipctdev")
        role = DeviceRole.objects.create(name="IpCtRole", slug="ipctrole")
        site = Site.objects.create(name="IpCtSite", slug="ipctsite")
        cls.device = Device.objects.create(name="ip-ct-rtr", device_type=dt, role=role, site=site)
        Interface.objects.create(device=cls.device, name="GE0/0", type="1000base-t")
        inst = NSOInstance.objects.create(name="ip-ct-inst", adapter_instance_id="ip-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="ip-ct", adapter_device_id=7980
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), TOP_KEYS)
        iface = CONTRACT_PAYLOAD["interfaces"][0]
        self.assertEqual(set(iface.keys()), IFACE_KEYS)
        self.assertEqual(set(iface["addresses"][0].keys()), ADDR_KEYS)

    def test_consumer_reads_contract_payload(self):
        """_reconcile_interface_ips ingests the documented shape into NSOInterfaceIPState."""
        _reconcile_interface_ips(self.device, CONTRACT_PAYLOAD)
        state = NSOInterfaceIPState.objects.get(interface__name="GE0/0", address="10.0.0.1/24")
        self.assertEqual(state.family, "ipv4")
        self.assertFalse(state.secondary)
