# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/redistribution.

Pins the JSON shape the plugin CONSUMES in
``redistribution_reconciler.reconcile_redistribution`` against the documented adapter
contract. Optional keys (route_map, metric, metric_type) are omitted when unset.

Canonical contract: ``nso-adapter/docs/api-contract.md`` § "GET .../redistribution".
Mirror (producer side): ``nso-adapter/tests/api/test_contract_redistribution.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSORedistributionState
from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

REQUIRED_TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "entries"}
REQUIRED_ENTRY_KEYS = {"dest_protocol", "dest_ref", "source_protocol", "source_ref"}
OPTIONAL_ENTRY_KEYS = {"route_map", "metric", "metric_type"}

CONTRACT_PAYLOAD = {
    "device_id": 7920,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "entries": [
        {
            "dest_protocol": "ospf",
            "dest_ref": "1",
            "source_protocol": "bgp",
            "source_ref": "65100",
            "route_map": "RM-REDIST",
            "metric": 100,
            "metric_type": "type-1",
        },
        {"dest_protocol": "isis", "dest_ref": "", "source_protocol": "connected", "source_ref": ""},
    ],
}


class TestRedistributionContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RdCt", slug="rdct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RdCtDev", slug="rdctdev")
        role = DeviceRole.objects.create(name="RdCtRole", slug="rdctrole")
        site = Site.objects.create(name="RdCtSite", slug="rdctsite")
        cls.device = Device.objects.create(name="rd-ct-rtr", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="rd-ct-inst", adapter_instance_id="rd-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="rd-ct", adapter_device_id=7920
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), REQUIRED_TOP_KEYS)
        by_key = {(e["dest_protocol"], e["source_protocol"]): e for e in CONTRACT_PAYLOAD["entries"]}
        self.assertEqual(set(by_key[("ospf", "bgp")].keys()), REQUIRED_ENTRY_KEYS | OPTIONAL_ENTRY_KEYS)
        self.assertEqual(set(by_key[("isis", "connected")].keys()), REQUIRED_ENTRY_KEYS)

    def test_consumer_reads_contract_payload(self):
        """reconcile_redistribution ingests the documented shape into NSORedistributionState."""
        result = reconcile_redistribution(self.device, CONTRACT_PAYLOAD)

        self.assertEqual(NSORedistributionState.objects.filter(management=self.mgmt).count(), 2)
        self.assertEqual(len(result), 2)
        maximal = NSORedistributionState.objects.get(management=self.mgmt, dest_protocol="ospf", source_protocol="bgp")
        self.assertEqual(maximal.route_map, "RM-REDIST")
        self.assertEqual(maximal.metric, 100)
        self.assertEqual(maximal.metric_type, "type-1")
        # Minimal entry: optionals default cleanly (no KeyError).
        minimal = NSORedistributionState.objects.get(
            management=self.mgmt, dest_protocol="isis", source_protocol="connected"
        )
        self.assertEqual(minimal.route_map, "")
        self.assertIsNone(minimal.metric)
