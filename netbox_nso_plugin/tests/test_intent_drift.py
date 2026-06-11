# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for intent_drift: detect + re-sync orphaned adapter intent (split-brain)."""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin import intent_drift
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceIPState


class TestIntentDrift(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="DriftMfg", slug="driftmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="DriftDev", slug="driftdev")
        role = DeviceRole.objects.create(name="DriftRole", slug="driftrole")
        site = Site.objects.create(name="DriftSite", slug="driftsite")
        cls.device = Device.objects.create(name="drift-rtr", device_type=dt, role=role, site=site)
        cls.iface = Interface.objects.create(device=cls.device, name="Gi0/0", type="1000base-t")
        inst = NSOInstance.objects.create(name="DriftNSO", adapter_instance_id="nso-drift")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="drift-rtr", adapter_device_id=88
        )

    _SUMMARY = {"scopes": {"interface_ip_intent": {"count": 2, "applied": 0, "failed": 0}}}

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_orphaned_when_adapter_has_intent_and_netbox_owns_none(self, mock_sum):
        mock_sum.return_value = self._SUMMARY
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        keys = {d["key"] for d in drift}
        self.assertIn("interface_ip", keys)
        entry = next(d for d in drift if d["key"] == "interface_ip")
        self.assertEqual(entry["count"], 2)

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_not_orphaned_when_netbox_owns_the_scope(self, mock_sum):
        mock_sum.return_value = self._SUMMARY
        NSOInterfaceIPState.objects.create(
            interface=self.iface, address="10.0.0.1/32", vrf="", family="ipv4", status="accepted"
        )
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        self.assertNotIn("interface_ip", {d["key"] for d in drift})

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_empty_summary_no_drift(self, mock_sum):
        mock_sum.return_value = {"scopes": {}}
        self.assertEqual(intent_drift.compute_intent_drift(self.device, self.mgmt), [])

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary", side_effect=RuntimeError("adapter down"))
    def test_adapter_error_returns_empty(self, _mock):
        # Must never break the tab render.
        self.assertEqual(intent_drift.compute_intent_drift(self.device, self.mgmt), [])

    @patch("netbox_nso_plugin.signals._push_ip_intent_for_device")
    def test_resync_calls_push_for_scope(self, mock_push):
        done = intent_drift.resync_intent(self.device, self.mgmt, ["interface_ip"])
        self.assertEqual(done, ["interface_ip"])
        mock_push.assert_called_once_with(self.mgmt.device_id, 88)

    def test_resync_no_adapter_id_noop(self):
        self.mgmt.adapter_device_id = None
        self.assertEqual(intent_drift.resync_intent(self.device, self.mgmt, ["interface_ip"]), [])
