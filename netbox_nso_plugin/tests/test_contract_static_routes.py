# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/static-routes.

Routes omit optional keys when unset. Consumed by
``template_content._reconcile_static_routes``.

Canonical contract: ``nso-adapter/docs/api-contract.md`` § "Static Routing".
Mirror (producer side): ``nso-adapter/tests/api/test_contract_static_routes.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOStaticRouteState
from netbox_nso_plugin.template_content import _reconcile_static_routes, static_route_reconcile_plan

TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "routes"}
ROUTE_REQUIRED_KEYS = {"vrf", "prefix", "next_hop"}
ROUTE_OPTIONAL_KEYS = {"interface_next_hop", "metric", "permanent", "tag", "name"}

CONTRACT_PAYLOAD = {
    "device_id": 7970,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "routes": [
        {
            "vrf": "",
            "prefix": "10.0.0.0/8",
            "next_hop": "192.0.2.1",
            "interface_next_hop": "GE0/0",
            "metric": 10,
            "permanent": True,
            "tag": 99,
            "name": "RT-1",
        },
        {"vrf": "", "prefix": "0.0.0.0/0", "next_hop": "192.0.2.254"},
    ],
}


class TestStaticRoutesContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SrCt", slug="srct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SrCtDev", slug="srctdev")
        role = DeviceRole.objects.create(name="SrCtRole", slug="srctrole")
        site = Site.objects.create(name="SrCtSite", slug="srctsite")
        cls.device = Device.objects.create(name="sr-ct-rtr", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="sr-ct-inst", adapter_instance_id="sr-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="sr-ct", adapter_device_id=7970
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), TOP_KEYS)
        routes = {r["prefix"]: r for r in CONTRACT_PAYLOAD["routes"]}
        self.assertEqual(set(routes["10.0.0.0/8"].keys()), ROUTE_REQUIRED_KEYS | ROUTE_OPTIONAL_KEYS)
        self.assertEqual(set(routes["0.0.0.0/0"].keys()), ROUTE_REQUIRED_KEYS)

    def test_consumer_reads_contract_payload(self):
        """_reconcile_static_routes ingests the documented shape without KeyError.

        Overlay materialisation is gated by netbox_routing + the AdapterConnection
        ``static_route`` auto-create toggle (out of scope here), so the contract only
        asserts the documented keys are consumed cleanly and any rows created carry the
        documented values.
        """
        result = _reconcile_static_routes(self.device, CONTRACT_PAYLOAD)
        self.assertIsInstance(result, list)
        for row in NSOStaticRouteState.objects.filter(management=self.mgmt):
            self.assertIn(row.nso_prefix, {"10.0.0.0/8", "0.0.0.0/0"})

    def test_reconcile_preflight_is_an_exact_renderer_plan(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan

        plan = static_route_reconcile_plan(self.device, CONTRACT_PAYLOAD)

        self.assertIsInstance(plan, RendererMutationPlan)
