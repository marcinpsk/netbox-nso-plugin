# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/route-policy.

Pins the JSON shape the plugin CONSUMES in
``route_policy_reconciler.reconcile_route_policy`` (the four policy-object families +
their entries) against the documented adapter contract. Only the prefix-list entry has
optional keys (``ge``/``le``); there is no top-level ``refresh_source``.

Canonical contract: ``nso-adapter/docs/api-contract.md`` § "GET .../route-policy".
Mirror (producer side): ``nso-adapter/tests/api/test_contract_route_policy.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSORoutePolicyState
from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

REQUIRED_TOP_KEYS = {"device_id", "last_refreshed_at", "prefix_lists", "community_lists", "as_paths", "route_maps"}
REQUIRED_PL_KEYS = {"name", "family", "entries"}
REQUIRED_PL_ENTRY_KEYS = {"sequence", "action", "prefix"}
OPTIONAL_PL_ENTRY_KEYS = {"ge", "le"}
REQUIRED_CL_KEYS = {"name", "entries"}
REQUIRED_CL_ENTRY_KEYS = {"sequence", "action", "community"}
REQUIRED_AP_KEYS = {"name", "entries"}
REQUIRED_AP_ENTRY_KEYS = {"sequence", "action", "pattern"}
REQUIRED_RM_KEYS = {"name", "entries"}
REQUIRED_RM_ENTRY_KEYS = {
    "sequence",
    "action",
    "match_prefix_lists",
    "match_community_lists",
    "match_as_paths",
    "match",
    "set",
}

CONTRACT_PAYLOAD = {
    "device_id": 7940,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "prefix_lists": [
        {
            "name": "PL-1",
            "family": 4,
            "entries": [
                {"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8", "ge": 16, "le": 24},
                {"sequence": 20, "action": "deny", "prefix": "0.0.0.0/0"},
            ],
        }
    ],
    "community_lists": [{"name": "CL-1", "entries": [{"sequence": 10, "action": "permit", "community": "65000:100"}]}],
    "as_paths": [{"name": "AP-1", "entries": [{"sequence": 10, "action": "permit", "pattern": "^65000_"}]}],
    "route_maps": [
        {
            "name": "RM-1",
            "entries": [
                {
                    "sequence": 10,
                    "action": "permit",
                    "match_prefix_lists": ["PL-1"],
                    "match_community_lists": ["CL-1"],
                    "match_as_paths": ["AP-1"],
                    "match": '{"prefix": "PL-1"}',
                    "set": '{"local_preference": 200}',
                }
            ],
        }
    ],
}


class TestRoutePolicyContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RpCt", slug="rpct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RpCtDev", slug="rpctdev")
        role = DeviceRole.objects.create(name="RpCtRole", slug="rpctrole")
        site = Site.objects.create(name="RpCtSite", slug="rpctsite")
        cls.device = Device.objects.create(name="rp-ct-rtr", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="rp-ct-inst", adapter_instance_id="rp-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="rp-ct", adapter_device_id=7940
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), REQUIRED_TOP_KEYS)

        pl = CONTRACT_PAYLOAD["prefix_lists"][0]
        self.assertEqual(set(pl.keys()), REQUIRED_PL_KEYS)
        pl_entries = {e["sequence"]: e for e in pl["entries"]}
        self.assertEqual(set(pl_entries[10].keys()), REQUIRED_PL_ENTRY_KEYS | OPTIONAL_PL_ENTRY_KEYS)
        self.assertEqual(set(pl_entries[20].keys()), REQUIRED_PL_ENTRY_KEYS)

        cl = CONTRACT_PAYLOAD["community_lists"][0]
        self.assertEqual(set(cl.keys()), REQUIRED_CL_KEYS)
        self.assertEqual(set(cl["entries"][0].keys()), REQUIRED_CL_ENTRY_KEYS)

        ap = CONTRACT_PAYLOAD["as_paths"][0]
        self.assertEqual(set(ap.keys()), REQUIRED_AP_KEYS)
        self.assertEqual(set(ap["entries"][0].keys()), REQUIRED_AP_ENTRY_KEYS)

        rm = CONTRACT_PAYLOAD["route_maps"][0]
        self.assertEqual(set(rm.keys()), REQUIRED_RM_KEYS)
        self.assertEqual(set(rm["entries"][0].keys()), REQUIRED_RM_ENTRY_KEYS)

    def test_consumer_reads_contract_payload(self):
        """reconcile_route_policy ingests the documented shape into overlays + netbox_routing."""
        try:
            from netbox_routing.models import PrefixList, PrefixListEntry, RouteMap, RouteMapEntry
        except ImportError:
            self.skipTest("netbox_routing not installed")

        result = reconcile_route_policy(self.device, CONTRACT_PAYLOAD)

        # One overlay row per family.
        families = set(NSORoutePolicyState.objects.filter(management=self.mgmt).values_list("family", flat=True))
        self.assertEqual(families, {"prefix_list", "community_list", "as_path", "route_map"})
        self.assertEqual(len(result), 4)

        # Prefix-list entries materialised (both sequences).
        pl = PrefixList.objects.get(name="PL-1")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 2)

        # Route-map entry + its match_prefix_list link resolved by name.
        rm = RouteMap.objects.get(name="RM-1")
        rm_entry = RouteMapEntry.objects.get(route_map=rm)
        self.assertIn("PL-1", list(rm_entry.match_prefix_list.values_list("name", flat=True)))
