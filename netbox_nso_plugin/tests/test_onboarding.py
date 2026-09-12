# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the onboarding dashboard computation (3 tiles + identity matching)."""

from types import SimpleNamespace
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from django.test import SimpleTestCase, TestCase
from ipam.models import IPAddress
from netaddr import IPNetwork


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
        self.assertEqual(c["mgmt_ip"], "10.1.1.1")
        self.assertFalse(c["oob_only"])

    def test_candidate_requires_platform_mapping(self):
        """A device whose platform has NO NED mapping is NOT a candidate.

        The mapping filters to platforms we plausibly have a NED for (excludes
        servers etc.); the operator can still override the NED in the picker, and
        the mapped value is the pre-selected default.
        """
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        _device("nomap", platform=self.ios, ip="10.1.1.2/24")
        # No mapping yet → not listed.
        self.assertEqual(len(self._build([])["candidates"]), 0)
        # Add the mapping → now listed with the mapped NED as the default.
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="cisco-ios-cli-6.114")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["ned_id"], "cisco-ios-cli-6.114")

    def test_not_candidate_when_inactive(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        _device("staged", status="staged", platform=self.ios, ip="10.1.1.3/24")
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="x")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 0)

    def test_not_candidate_when_no_mgmt_ip(self):
        """No primary AND no OOB → not a candidate (NSO needs some address to reach it)."""
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        _device("noip", platform=self.ios)
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="x")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 0)

    def test_candidate_oob_only(self):
        """A freshly-deployed box with only an OOB IP (no primary yet) IS onboardable —
        it onboards over OOB, the same fallback the failover loop uses."""
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        d = _device("oobonly", platform=self.ios)
        iface = Interface.objects.create(device=d, name="oob0", type="virtual")
        d.oob_ip = IPAddress.objects.create(address="192.0.2.50/24", assigned_object=iface)
        d.save()
        NSOPlatformNedMapping.objects.create(platform=self.ios, ned_id="cisco-ios-cli-6.114")
        data = self._build([])
        self.assertEqual(len(data["candidates"]), 1)
        c = data["candidates"][0]
        self.assertEqual(c["mgmt_ip"], "192.0.2.50")
        self.assertTrue(c["oob_only"])

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

    # Onboarding is now async: provision_device ENQUEUES a job and returns {job_id, ...};
    # onboard_candidate creates the management row in 'provisioning' immediately (its
    # adapter-push signal gated) and the dashboard polls the job to completion.
    _QUEUED = {"job_id": "55", "nso_device_name": "x", "status": "queued"}

    def test_no_mapping_but_explicit_ned_onboards(self):
        """No mapping is fine when an explicit ned_id is given (override / no mapping)."""
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentRevision, NSOPlatformNedMapping
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = _device("nomap2", platform=self.ios, ip="10.5.5.2/24")
        NSOPlatformNedMapping.objects.filter(platform=self.ios).delete()
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED) as prov:
            res = onboard_candidate(d, self.instance, ned_id="cisco-iosxr-cli-7.55:cisco-iosxr-cli-7.55")
        self.assertTrue(res["ok"])
        self.assertTrue(res["provisioning"])
        self.assertTrue(NSODeviceManagement.objects.filter(device=d, onboard_status="provisioning").exists())
        self.assertEqual(prov.call_args.kwargs["ned_id"], "cisco-iosxr-cli-7.55:cisco-iosxr-cli-7.55")
        # Onboarding learns the platform→NED mapping (so future same-platform
        # devices become candidates); the chosen NED is recorded.
        self.assertTrue(res.get("mapping_created"))
        m = NSOPlatformNedMapping.objects.get(platform=self.ios)
        self.assertEqual(m.ned_id, "cisco-iosxr-cli-7.55:cisco-iosxr-cli-7.55")
        revision = NSOIntentRevision.objects.get(device=d, scope="interface")
        # The management create is verified first. The subsequent legacy platform-mapping
        # create advances the scope again until that input is converted.
        self.assertIsNotNone(revision.verified_revision)
        self.assertTrue(revision.verified_fingerprint)

    def test_tracking_row_create_failure_reports_job_for_recovery(self):
        """If the tracking-row create fails AFTER the adapter provision job is enqueued, surface
        the ghost-onboard (error names the job id) instead of raising — NSO is already building
        the node, so the operator needs the job id to recover (a bare create would 'ghost' it)."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("ghost-rtr")
        with (
            patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED) as prov,
            patch("netbox_nso_plugin.management_lifecycle.save_management", side_effect=Exception("db down")),
        ):
            res = onboard_candidate(d, self.instance)  # must NOT raise
        prov.assert_called_once()  # the provision job WAS enqueued
        self.assertFalse(res["ok"])
        self.assertEqual(res["job_id"], "55")
        self.assertIn("55", res["error"] or "")  # job id surfaced for recovery
        self.assertFalse(NSODeviceManagement.objects.filter(device=d).exists())

    def test_onboard_does_not_override_existing_mapping(self):
        """If a platform mapping already exists, onboarding leaves it (no-op)."""
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("keepmap-rtr")
        existing = NSOPlatformNedMapping.objects.get(platform=self.ios).ned_id
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED):
            res = onboard_candidate(d, self.instance, ned_id="cisco-iosxr-cli-7.55:cisco-iosxr-cli-7.55")
        self.assertTrue(res["ok"])
        self.assertFalse(res.get("mapping_created"))
        self.assertEqual(NSOPlatformNedMapping.objects.get(platform=self.ios).ned_id, existing)

    def test_explicit_ned_overrides_mapping(self):
        """An explicit ned_id wins over the platform mapping default."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("override-rtr")
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED) as prov:
            res = onboard_candidate(d, self.instance, ned_id="test-ned:test-ned")
        self.assertTrue(res["ok"])
        self.assertEqual(prov.call_args.kwargs["ned_id"], "test-ned:test-ned")

    def test_no_mgmt_ip_errors(self):
        """No primary AND no OOB → cannot onboard (no address to reach the device)."""
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = _device("noip2", platform=self.ios)
        NSOPlatformNedMapping.objects.get_or_create(platform=self.ios, defaults={"ned_id": "x"})
        res = onboard_candidate(d, self.instance)
        self.assertFalse(res["ok"])
        self.assertIn("OOB", res["error"])

    def test_onboard_oob_only_uses_oob_as_address(self):
        """An OOB-only box onboards over its OOB address (address=OOB) — the same fallback
        the failover loop uses — instead of being blocked for lack of a primary."""
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = _device("oob-only-rtr", platform=self.ios)
        NSOPlatformNedMapping.objects.get_or_create(platform=self.ios, defaults={"ned_id": "cisco-ios-cli-6.114"})
        iface = Interface.objects.create(device=d, name="oob0", type="virtual")
        d.oob_ip = IPAddress.objects.create(address="192.0.2.7/24", assigned_object=iface)
        d.save()
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED) as prov:
            res = onboard_candidate(d, self.instance)
        self.assertTrue(res["ok"])
        self.assertEqual(prov.call_args.kwargs["address"], "192.0.2.7")  # onboarded over OOB

    def test_onboard_prefers_primary_address_when_present(self):
        """With both, the primary is the provision address and OOB rides as the fallback."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("both-rtr", ip="10.5.5.30/24")
        iface = Interface.objects.filter(device=d).first()
        d.oob_ip = IPAddress.objects.create(address="192.0.2.8/24", assigned_object=iface)
        d.save()
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED) as prov:
            res = onboard_candidate(d, self.instance)
        self.assertTrue(res["ok"])
        self.assertEqual(prov.call_args.kwargs["address"], "10.5.5.30")  # primary
        self.assertEqual(prov.call_args.kwargs["oob_ip"], "192.0.2.8")  # OOB fallback

    def test_onboard_passes_oob_ip(self):
        """A device with an OOB IP forwards it to provision_device (the failover fallback,
        so a fresh box only reachable on OOB is still onboardable)."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("oob-rtr", ip="10.5.5.20/24")
        iface = Interface.objects.filter(device=d).first()
        oob = IPAddress.objects.create(address="192.0.2.9/24", assigned_object=iface)
        d.oob_ip = oob
        d.save()
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED) as prov:
            res = onboard_candidate(d, self.instance)
        self.assertTrue(res["ok"])
        self.assertEqual(prov.call_args.kwargs["oob_ip"], "192.0.2.9")  # /24 stripped → host only

    def test_onboard_oob_ip_none_when_unset(self):
        """A device with no OOB IP forwards oob_ip=None (no fallback) — never blocks onboard."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("nooob-rtr", ip="10.5.5.21/24")
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value=self._QUEUED) as prov:
            res = onboard_candidate(d, self.instance)
        self.assertTrue(res["ok"])
        self.assertIsNone(prov.call_args.kwargs["oob_ip"])

    def test_success_creates_provisioning_row_and_gates_signal(self):
        """Enqueue → row created in 'provisioning' with the job id; the adapter-push signal
        is GATED (no onboard/set_scope/sync_notify) until the job completes."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("good-rtr")
        with (
            patch(
                "netbox_nso_plugin.adapter_client.provision_device",
                return_value={"job_id": "77", "nso_device_name": "good-rtr", "status": "queued"},
            ) as prov,
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope") as set_scope,
        ):
            res = onboard_candidate(d, self.instance)
        self.assertTrue(res["ok"] and res["provisioning"])
        self.assertEqual(res["job_id"], "77")
        mgmt = NSODeviceManagement.objects.get(device=d)
        self.assertEqual(mgmt.onboard_status, "provisioning")
        self.assertEqual(mgmt.onboard_job_id, "77")
        self.assertIsNone(mgmt.adapter_device_id)  # not mapped yet — signal was gated
        onboard.assert_not_called()
        set_scope.assert_not_called()
        prov.assert_called_once()
        _, kw = prov.call_args
        self.assertEqual(kw["ned_id"], "cisco-ios-cli-6.114")
        self.assertEqual(kw["authgroup"], "network")

    def test_no_job_id_no_management(self):
        """If the adapter returns no job id (enqueue failed) → error and no row created."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.onboarding import onboard_candidate

        d = self._mapped_device("bad-rtr")
        with patch("netbox_nso_plugin.adapter_client.provision_device", return_value={"status": "error"}):
            res = onboard_candidate(d, self.instance)
        self.assertFalse(res["ok"])
        self.assertIn("job id", res["error"])
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
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentRevision
        from netbox_nso_plugin.onboarding import manage_existing

        d = _device("ext-rtr", ip="10.7.7.7/24")
        with patch("netbox_nso_plugin.adapter_client.provision_device") as prov:
            res = manage_existing(d, self.instance, "ext-rtr-nso")
        self.assertTrue(res["ok"])
        prov.assert_not_called()
        mgmt = NSODeviceManagement.objects.get(device=d)
        self.assertEqual(mgmt.nso_device_name, "ext-rtr-nso")
        revision = NSOIntentRevision.objects.get(device=d, scope="interface")
        self.assertEqual(revision.verified_revision, revision.revision)
        self.assertTrue(revision.verified_fingerprint)

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


class TestIpHost(SimpleTestCase):
    """Pure-logic tests for onboarding._ip_host (no DB) — both NetBox IPAddress shapes."""

    def test_none_returns_none(self):
        from netbox_nso_plugin.onboarding import _ip_host

        self.assertIsNone(_ip_host(None))

    def test_db_loaded_ipnetwork_returns_host(self):
        """DB-loaded IPAddress: ``.address`` is a netaddr IPNetwork → host via ``.ip``."""
        from netbox_nso_plugin.onboarding import _ip_host

        self.assertEqual(_ip_host(SimpleNamespace(address=IPNetwork("10.0.0.1/32"))), "10.0.0.1")

    def test_raw_string_address_returns_host(self):
        """In-memory/unsaved IPAddress: ``.address`` is the raw 'x/yy' string → split host."""
        from netbox_nso_plugin.onboarding import _ip_host

        self.assertEqual(_ip_host(SimpleNamespace(address="172.16.0.5/24")), "172.16.0.5")

    def test_ipv6_host(self):
        from netbox_nso_plugin.onboarding import _ip_host

        self.assertEqual(_ip_host(SimpleNamespace(address=IPNetwork("2001:db8::1/64"))), "2001:db8::1")


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
            return_value={"job_id": "5", "nso_device_name": "edge-rtr-5", "status": "queued"},
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


class TestDeviceMgmtAddresses(TestCase):
    """device_mgmt_addresses — the single resolver shared by onboarding + the failover
    scope push (signals), so the provision address and the failover-probed addresses can
    never diverge."""

    def test_resolves_primary_and_oob_host_strings(self):
        from netbox_nso_plugin.onboarding import device_mgmt_addresses

        d = _device("res-rtr", ip="10.9.9.9/24")
        iface = Interface.objects.filter(device=d).first()
        d.oob_ip = IPAddress.objects.create(address="192.0.2.99/24", assigned_object=iface)
        d.save()
        self.assertEqual(device_mgmt_addresses(d), ("10.9.9.9", "192.0.2.99"))

    def test_returns_none_when_absent(self):
        from netbox_nso_plugin.onboarding import device_mgmt_addresses

        d = _device("bare-rtr")
        self.assertEqual(device_mgmt_addresses(d), (None, None))

    def test_oob_only_resolves_primary_none(self):
        from netbox_nso_plugin.onboarding import device_mgmt_addresses

        d = _device("oob-res-rtr")
        iface = Interface.objects.create(device=d, name="oob0", type="virtual")
        d.oob_ip = IPAddress.objects.create(address="192.0.2.77/24", assigned_object=iface)
        d.save()
        self.assertEqual(device_mgmt_addresses(d), (None, "192.0.2.77"))
