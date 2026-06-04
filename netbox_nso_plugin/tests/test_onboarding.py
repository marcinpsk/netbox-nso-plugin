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

    def test_candidate_without_mapping_still_listed(self):
        """A device with no platform-NED mapping is still a candidate (ned_id="")
        — the mapping is only the default suggestion; the operator picks the NED."""
        _device("nomap", platform=self.ios, ip="10.1.1.2/24")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["ned_id"], "")

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


class TestOnboardCandidate(TestCase):
    """Tests for onboarding.onboard_candidate (the write action)."""

    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSOInstance

        cls.instance = NSOInstance.objects.create(name="onbX", adapter_instance_id="onbX")
        cls.ios = Platform.objects.create(name="IOS-X", slug="ios-x-onb")

    def _mapped_device(self, name, ip="10.5.5.5/24"):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        d = _device(name, platform=self.ios, ip=ip)
        NSOPlatformNedMapping.objects.get_or_create(platform=self.ios, defaults={"ned_id": "cisco-ios-cli-6.114"})
        return d

    def test_no_ned_and_no_mapping_errors(self):
        """No explicit NED and no platform mapping → asks the operator to pick a NED."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = _device("noplat", ip="10.5.5.1/24")
        res = onboard_candidate(d, self.instance)
        self.assertFalse(res["ok"])
        self.assertIn("No NED selected", res["error"])

    def test_no_mapping_but_explicit_ned_onboards(self):
        """No mapping is fine when an explicit ned_id is given (override / no mapping)."""
        from netbox_nso_plugin.models import NSODeviceManagement, NSOPlatformNedMapping
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = _device("nomap2", platform=self.ios, ip="10.5.5.2/24")
        NSOPlatformNedMapping.objects.filter(platform=self.ios).delete()
        with patch(
            "netbox_nso_plugin.adapter_client.provision_device",
            return_value={"ok": True, "steps": [], "device_id": 7},
        ) as prov:
            res = onboard_candidate(d, self.instance, ned_id="cisco-iosxr-cli-7.55:cisco-iosxr-cli-7.55")
        self.assertTrue(res["ok"])
        self.assertTrue(NSODeviceManagement.objects.filter(device=d).exists())
        self.assertEqual(prov.call_args.kwargs["ned_id"], "cisco-iosxr-cli-7.55:cisco-iosxr-cli-7.55")

    def test_explicit_ned_overrides_mapping(self):
        """An explicit ned_id wins over the platform mapping default."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("override-rtr")
        with patch(
            "netbox_nso_plugin.adapter_client.provision_device",
            return_value={"ok": True, "steps": [], "device_id": 8},
        ) as prov:
            res = onboard_candidate(d, self.instance, ned_id="test-ned:test-ned")
        self.assertTrue(res["ok"])
        self.assertEqual(prov.call_args.kwargs["ned_id"], "test-ned:test-ned")

    def test_no_primary_ip_errors(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = _device("noip2", platform=self.ios)
        NSOPlatformNedMapping.objects.get_or_create(platform=self.ios, defaults={"ned_id": "x"})
        res = onboard_candidate(d, self.instance)
        self.assertFalse(res["ok"])
        self.assertIn("primary IP", res["error"])

    def test_success_creates_management(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("good-rtr")
        with patch(
            "netbox_nso_plugin.adapter_client.provision_device",
            return_value={"ok": True, "steps": [{"step": "create", "status": "ok"}], "device_id": 1},
        ) as prov:
            res = onboard_candidate(d, self.instance)
        self.assertTrue(res["ok"])
        self.assertTrue(NSODeviceManagement.objects.filter(device=d).exists())
        prov.assert_called_once()
        # ned_id + authgroup passed through
        _, kw = prov.call_args
        self.assertEqual(kw["ned_id"], "cisco-ios-cli-6.114")
        self.assertEqual(kw["authgroup"], "network")

    def test_provision_failure_no_management(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("bad-rtr")
        with patch(
            "netbox_nso_plugin.adapter_client.provision_device",
            return_value={"ok": False, "steps": [{"step": "fetch_host_keys", "status": "failed"}]},
        ):
            res = onboard_candidate(d, self.instance)
        self.assertFalse(res["ok"])
        self.assertFalse(NSODeviceManagement.objects.filter(device=d).exists())

    def test_already_managed_errors(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("dup-rtr")
        NSODeviceManagement.objects.create(device=d, nso_instance=self.instance, nso_device_name=d.name)
        res = onboard_candidate(d, self.instance)
        self.assertFalse(res["ok"])
        self.assertIn("already managed", res["error"].lower())


class TestManageExisting(TestCase):
    """Tests for onboarding.manage_existing (quick-manage of an external device)."""

    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSOInstance

        cls.instance = NSOInstance.objects.create(name="mgX", adapter_instance_id="mgX")

    def test_creates_management_no_provision(self):
        """Creating a management row for an already-in-NSO device does not provision."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import manage_existing

        d = _device("ext-rtr", ip="10.7.7.7/24")
        with patch("netbox_nso_plugin.adapter_client.provision_device") as prov:
            res = manage_existing(d, self.instance, "ext-rtr-nso")
        self.assertTrue(res["ok"])
        prov.assert_not_called()
        mgmt = NSODeviceManagement.objects.get(device=d)
        self.assertEqual(mgmt.nso_device_name, "ext-rtr-nso")

    def test_already_managed_errors(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import manage_existing

        d = _device("ext-dup", ip="10.7.7.8/24")
        NSODeviceManagement.objects.create(device=d, nso_instance=self.instance, nso_device_name="x")
        res = manage_existing(d, self.instance, "x")
        self.assertFalse(res["ok"])
        self.assertIn("already managed", res["error"].lower())

    def test_missing_name_errors(self):
        from netbox_nso_plugin.onboarding import manage_existing

        d = _device("ext-noname", ip="10.7.7.9/24")
        res = manage_existing(d, self.instance, "")
        self.assertFalse(res["ok"])


class TestNormalizeNsoName(TestCase):
    def test_keeps_valid_names(self):
        from netbox_nso_plugin.onboarding import normalize_nso_device_name

        assert normalize_nso_device_name("core-rtr-01") == "core-rtr-01"
        assert normalize_nso_device_name("rtr01.lab.example.net") == "rtr01.lab.example.net"

    def test_replaces_invalid_runs(self):
        from netbox_nso_plugin.onboarding import normalize_nso_device_name

        assert normalize_nso_device_name("core rtr 01") == "core-rtr-01"
        assert normalize_nso_device_name("a/b c:d") == "a-b-c-d"

    def test_trims_separators_and_empty_fallback(self):
        from netbox_nso_plugin.onboarding import normalize_nso_device_name

        assert normalize_nso_device_name("  .rtr.  ") == "rtr"
        assert normalize_nso_device_name("  ///  ") == "device"


class TestOnboardNameNormalization(TestCase):
    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSOInstance, NSOPlatformNedMapping

        cls.instance = NSOInstance.objects.create(name="onbN", adapter_instance_id="onbN")
        cls.plat = Platform.objects.create(name="IOS-N", slug="ios-n-onb")
        NSOPlatformNedMapping.objects.create(platform=cls.plat, ned_id="cisco-ios-cli-6.114")

    def test_onboard_normalizes_device_name(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = _device("edge rtr 5", platform=self.plat, ip="10.6.6.6/24")
        with patch(
            "netbox_nso_plugin.adapter_client.provision_device",
            return_value={"ok": True, "steps": [], "device_id": 1},
        ) as prov:
            res = onboard_candidate(d, self.instance)
        assert res["ok"]
        _, kw = prov.call_args
        assert kw["device_name"] == "edge-rtr-5"  # spaces normalized
        mgmt = NSODeviceManagement.objects.get(device=d)
        assert mgmt.nso_device_name == "edge-rtr-5"

    def test_onboard_name_collision_blocked(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        other = _device("taken", platform=self.plat, ip="10.6.6.1/24")
        NSODeviceManagement.objects.create(device=other, nso_instance=self.instance, nso_device_name="edge-1")
        d = _device("edge 1", platform=self.plat, ip="10.6.6.2/24")  # normalizes to edge-1
        res = onboard_candidate(d, self.instance)
        assert res["ok"] is False
        assert "already used" in res["error"]
