# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/lag-config.

Bundles and members omit optional keys when unset. Consumed by
``lacp_reconciler.reconcile_lag_config``.

Canonical contract: ``nso-adapter/docs/api-contract.md`` (LACP/LAG §).
Mirror (producer side): ``nso-adapter/tests/api/test_contract_lag.py`` — the ``*_KEYS``
sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOLACPBundleState, NSOLACPMemberState

TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "bundles"}
BUNDLE_REQUIRED_KEYS = {"name", "lag_id", "members"}
BUNDLE_OPTIONAL_KEYS = {"min_links", "system_priority", "system_id", "timer", "admin_key"}
MEMBER_REQUIRED_KEYS = {"interface_name"}
MEMBER_OPTIONAL_KEYS = {"mode", "port_priority"}

CONTRACT_PAYLOAD = {
    "device_id": 7990,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "bundles": [
        {
            "name": "Bundle-Ether1",
            "lag_id": 1,
            "min_links": 2,
            "system_priority": 100,
            "system_id": "00:11:22:33:44:55",
            "timer": "fast",
            "admin_key": 10,
            "members": [
                {"interface_name": "GE0/1", "mode": "active", "port_priority": 32},
                {"interface_name": "GE0/2"},
            ],
        },
        {"name": "Bundle-Ether2", "lag_id": 2, "members": []},
    ],
}


class TestLagContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LagCt", slug="lagct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LagCtDev", slug="lagctdev")
        role = DeviceRole.objects.create(name="LagCtRole", slug="lagctrole")
        site = Site.objects.create(name="LagCtSite", slug="lagctsite")
        cls.device = Device.objects.create(name="lag-ct-rtr", device_type=dt, role=role, site=site)
        Interface.objects.create(device=cls.device, name="Bundle-Ether1", type="lag")
        Interface.objects.create(device=cls.device, name="Bundle-Ether2", type="lag")
        Interface.objects.create(device=cls.device, name="GE0/1", type="1000base-t")
        Interface.objects.create(device=cls.device, name="GE0/2", type="1000base-t")
        inst = NSOInstance.objects.create(name="lag-ct-inst", adapter_instance_id="lag-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="lag-ct", adapter_device_id=7990
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), TOP_KEYS)
        bundles = {b["lag_id"]: b for b in CONTRACT_PAYLOAD["bundles"]}
        self.assertEqual(set(bundles[1].keys()), BUNDLE_REQUIRED_KEYS | BUNDLE_OPTIONAL_KEYS)
        self.assertEqual(set(bundles[2].keys()), BUNDLE_REQUIRED_KEYS)
        members = {m["interface_name"]: m for m in bundles[1]["members"]}
        self.assertEqual(set(members["GE0/1"].keys()), MEMBER_REQUIRED_KEYS | MEMBER_OPTIONAL_KEYS)
        self.assertEqual(set(members["GE0/2"].keys()), MEMBER_REQUIRED_KEYS)

    def test_consumer_reads_contract_payload(self):
        """reconcile_lag_config ingests the documented shape into the LACP overlays."""
        reconcile_lag_config(self.device, CONTRACT_PAYLOAD)
        self.assertTrue(
            NSOLACPBundleState.objects.filter(management=self.mgmt, interface__name="Bundle-Ether1").exists()
        )
        self.assertTrue(NSOLACPMemberState.objects.filter(management=self.mgmt, interface__name="GE0/1").exists())
