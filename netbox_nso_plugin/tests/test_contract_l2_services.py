# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/l2-services.

Nokia epipe/vpls services + SAPs; fixed key set per level; NO top-level
last_refreshed_at/refresh_source. Consumed by
``l2_service_reconciler.reconcile_l2_services``.

Canonical contract: ``nso-adapter/docs/api-contract.md`` (M37 L2 services §).
Mirror (producer side): ``nso-adapter/tests/api/test_contract_l2_services.py`` — the
``*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.l2_service_reconciler import reconcile_l2_services
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOL2SapState

TOP_KEYS = {"device_id", "services"}
SERVICE_KEYS = {"service_name", "service_type", "service_id", "saps"}
SAP_KEYS = {"sap_id", "port", "outer_tag", "inner_tag"}

CONTRACT_PAYLOAD = {
    "device_id": 7995,
    "services": [
        {
            "service_name": "EPIPE-1",
            "service_type": "epipe",
            "service_id": 100,
            "saps": [{"sap_id": "1/1/1:200", "port": "1/1/1", "outer_tag": 200, "inner_tag": None}],
        }
    ],
}


class TestL2ServicesContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="L2sCt", slug="l2sct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="L2sCtDev", slug="l2sctdev")
        role = DeviceRole.objects.create(name="L2sCtRole", slug="l2sctrole")
        site = Site.objects.create(name="L2sCtSite", slug="l2sctsite")
        cls.device = Device.objects.create(name="l2s-ct-rtr", device_type=dt, role=role, site=site)
        Interface.objects.create(device=cls.device, name="1/1/1", type="other")
        inst = NSOInstance.objects.create(name="l2s-ct-inst", adapter_instance_id="l2s-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="l2s-ct", adapter_device_id=7995
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), TOP_KEYS)
        svc = CONTRACT_PAYLOAD["services"][0]
        self.assertEqual(set(svc.keys()), SERVICE_KEYS)
        self.assertEqual(set(svc["saps"][0].keys()), SAP_KEYS)

    def test_consumer_reads_contract_payload(self):
        """reconcile_l2_services ingests the documented shape into NSOL2SapState."""
        reconcile_l2_services(self.device, CONTRACT_PAYLOAD)
        state = NSOL2SapState.objects.get(management=self.mgmt, service_name="EPIPE-1", sap_id="1/1/1:200")
        self.assertEqual(state.service_type, "epipe")
