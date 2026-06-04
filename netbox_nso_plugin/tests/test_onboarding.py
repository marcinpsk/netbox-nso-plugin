# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the onboarding dashboard computation (3 tiles + identity matching)."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from django.test import TestCase
from ipam.models import IPAddress


def _device(name, *, status="active", platform=None, ip=None):
    mfg, _ = Manufacturer.objects.get_or_create(name="OnbMfg", slug="onbmfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model="OnbDev", slug="onbdev")
    role, _ = DeviceRole.objects.get_or_create(name="OnbRole", slug="onbrole")
    site, _ = Site.objects.get_or_create(name="OnbSite", slug="onbsite")
    d = Device.objects.create(name=name, device_type=dt, role=role, site=site, status=status, platform=platform)
    if ip:
        iface = Interface.objects.create(device=d, name="mgmt0", type="virtual")
        addr = IPAddress.objects.create(address=ip, assigned_object=iface)
        d.primary_ip4 = addr
        d.save()
    return d


class TestOnboardingDashboard(TestCase):
    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSOInstance

        cls.instance = NSOInstance.objects.create(name="onb", adapter_instance_id="onb")
        cls.ios = Platform.objects.create(name="IOS", slug="ios-onb")

    def _build(self, nso_devices):
        from netbox_nso_plugin.onboarding import build_onboarding_dashboard

        with patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=nso_devices):
            return build_onboarding_dashboard(self.instance)

    def _nso(self, name, *, ned="cisco-ios-cli-6.114", address=None, nb_id=None, admin="unlocked"):
        return {
            "name": name,
            "ned_id": ned,
            "platform": "ios",
            "address": address,
            "admin_state": admin,
            "onboarded": nb_id is not None,
            "onboarded_netbox_device_id": nb_id,
        }

    def test_match_by_plugin_link(self):
        d = _device("rtr-link")
        data = self._build([self._nso("nso-rtr", nb_id=d.id)])
        self.assertEqual(len(data["onboarded"]), 1)
        self.assertEqual(data["onboarded"][0]["matched_by"], "link")
        self.assertEqual(data["onboarded"][0]["netbox_device"], d)

    def test_match_by_name(self):
        _device("rtr-name")
        data = self._build([self._nso("rtr-name")])
        self.assertEqual(data["onboarded"][0]["matched_by"], "name")

    def test_match_by_primary_ip(self):
        d = _device("rtr-ip", ip="10.9.9.9/32")  # noqa: F841
        data = self._build([self._nso("different-nso-name", address="10.9.9.9")])
        self.assertEqual(len(data["onboarded"]), 1)
        self.assertEqual(data["onboarded"][0]["matched_by"], "ip")

    def test_unmatched_is_orphan(self):
        data = self._build([self._nso("ghost", address="203.0.113.1")])
        self.assertEqual(len(data["orphans"]), 1)
        self.assertEqual(data["orphans"][0]["nso_name"], "ghost")

    def test_candidate_active_ip_mapped(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        _device("cand", platform=self.ios, ip="10.1.1.1/24")
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="cisco-ios-cli-6.114")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 1)
        c = data["candidates"][0]
        self.assertEqual(c["device"].name, "cand")
        self.assertEqual(c["ned_id"], "cisco-ios-cli-6.114")
        self.assertEqual(c["primary_ip"], "10.1.1.1")

    def test_not_candidate_when_no_mapping(self):
        _device("nomap", platform=self.ios, ip="10.1.1.2/24")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 0)

    def test_not_candidate_when_inactive(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        _device("staged", status="staged", platform=self.ios, ip="10.1.1.3/24")
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="x")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 0)

    def test_not_candidate_when_no_primary_ip(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        _device("noip", platform=self.ios)
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="x")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 0)

    def test_onboarded_excluded_from_candidates(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        d = _device("both", platform=self.ios, ip="10.1.1.4/24")
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="x")
        data = self._build([self._nso("both", nb_id=d.id)])
        self.assertEqual(len(data["onboarded"]), 1)
        self.assertEqual(len(data["candidates"]), 0)

    def test_adapter_error_sets_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.onboarding import build_onboarding_dashboard

        with patch(
            "netbox_nso_plugin.adapter_client.list_instance_devices",
            side_effect=AdapterError("boom", code="nso_unreachable"),
        ):
            data = build_onboarding_dashboard(self.instance)
        self.assertIsNotNone(data["error"])
        self.assertEqual(data["onboarded"], [])
