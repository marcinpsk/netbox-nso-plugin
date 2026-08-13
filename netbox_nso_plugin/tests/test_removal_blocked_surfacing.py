# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Blocked-removal surfacing (#89): jobs endpoint + force-removal action + tab banner.

A removal job the adapter blocked with ``removal_blocked_collateral`` (the ra1 lo0
incident guard) means the intent retraction is NOT enforced on the device. These
tests cover the plugin surface: the device-jobs JSON gains ``blocked_removals``
(latest removal job per scope decides), the NSO tab carries the banner template +
force action wiring, and the force-removal view proxies the adapter override.

Adapter traffic is faked at the transport boundary (real ``requests.Response``
bodies via ``_adapter_http``); everything above it — URL routing, view logic,
permission gating, JSON shapes — runs for real.
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

from ._adapter_http import make_response, make_session

User = get_user_model()
TEST_PASSWORD = "testpass789"  # noqa: S105

_ADAPTER_CFG = {
    "url": "http://adapter",
    "token": "tok",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


def _blocked_job(job_id=50, scope="isis", status="failed", updated="2026-07-10T06:00:00Z"):
    """A removal job the collateral guard blocked (shape from adapter run_removal)."""
    return {
        "id": job_id,
        "type": "removal",
        "device_id": 10,
        "status": status,
        "result": None,
        "context": {"scope": scope},
        "error": {
            "code": "removal_blocked_collateral",
            "message": "PUT-replace would retract rows not in intent",
            "detail": {
                "scope": scope,
                "orphans": {"interface-config": [["lo0", "ipv4"], ["lag31", "ipv4"]], "process-config": [["legacy"]]},
                "preview": "<edit-config>… would delete lo0 …</edit-config>",
                "hint": "Re-accept them into intent to keep them, or force-removal to flush.",
            },
        },
        "created_at": "2026-07-10T05:59:00Z",
        "updated_at": updated,
        "started_at": "2026-07-10T05:59:30Z",
        "heartbeat_at": None,
    }


def _removal_job(job_id, scope, status, updated="2026-07-10T07:00:00Z", context=True):
    """A generic removal job in *status*; scope attributed via context and/or result."""
    return {
        "id": job_id,
        "type": "removal",
        "device_id": 10,
        "status": status,
        "result": {"scope": scope} if status == "succeeded" else None,
        "context": {"scope": scope} if context else None,
        "error": None,
        "created_at": updated,
        "updated_at": updated,
        "started_at": None,
        "heartbeat_at": None,
    }


def _device_generations():
    # Copied from ../nso-adapter/docs/api-contract.md, GET devices/{id}/generations,
    # and ../nso-adapter/tests/api/test_device_generations.py, complete emit-null shape.
    return [
        {
            "generation_id": 81,
            "seq": 4,
            "status": "settled",
            "job_id": 501,
            "mode": "detach",
            "settlement_cohort": 73,
            "digest": "a" * 64,
            "stream_revisions": {"vlan": 11},
            "source_push_seq": {"vlan": 501},
            "created_at": "2026-08-12T09:15:00Z",
            "updated_at": "2026-08-12T09:30:00Z",
        },
        {
            "generation_id": 82,
            "seq": 5,
            "status": "pending",
            "job_id": None,
            "mode": "networked",
            "settlement_cohort": 73,
            "digest": "b" * 64,
            "stream_revisions": {"vlan": 12},
            "source_push_seq": {"vlan": 502},
            "created_at": "2026-08-12T09:30:00Z",
            "updated_at": "2026-08-12T09:30:00Z",
        },
        {
            "generation_id": 90,
            "seq": 9,
            "status": "running",
            "job_id": 510,
            "mode": "networked",
            "settlement_cohort": None,
            "digest": "c" * 64,
            "stream_revisions": {"logging": 3},
            "source_push_seq": {"logging": None},
            "created_at": "2026-08-13T10:45:00Z",
            "updated_at": "2026-08-13T11:00:00Z",
        },
    ]


class BlockedRemovalTestBase(TestCase):
    """Superuser + onboarded managed device."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="blockedremovaladmin", password=TEST_PASSWORD, email="br@test.example"
        )
        manufacturer = Manufacturer.objects.create(name="BRMan", slug="brman")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="BRType", slug="brtype")
        role = DeviceRole.objects.create(name="BRRole", slug="brrole")
        site = Site.objects.create(name="BRSite", slug="brsite")
        cls.device = Device.objects.create(name="br-router-01", device_type=device_type, role=role, site=site)
        nso_instance = NSOInstance.objects.create(name="br-nso", adapter_instance_id="br-nso-id")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=nso_instance, nso_device_name="br-router-01", adapter_device_id=10
        )

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def _get_jobs(self, jobs, *, generations=None, generation_ids=()):
        """Hit device_nso_jobs with the adapter's job list canned at the transport."""
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            session = make_session()

            def request(_method, url, **_kwargs):
                if url.endswith("/generations"):
                    return make_response(200, json_data=generations)
                return make_response(200, json_data=jobs)

            session.request.side_effect = request
            mock_session_cls.return_value = session
            url = reverse("plugins:netbox_nso_plugin:device_nso_jobs", args=[self.device.pk])
            response = self.client.get(url, {"generation_id": generation_ids})
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)


class TestDeviceJobsBlockedRemovals(BlockedRemovalTestBase):
    """device_nso_jobs surfaces blocked removals — latest removal job per scope decides."""

    def test_blocked_removal_surfaced_with_detail(self):
        """A failed removal with removal_blocked_collateral yields a blocked_removals entry."""
        job = _blocked_job()
        data = self._get_jobs([job])
        self.assertEqual(data["jobs"], [job])
        self.assertEqual(len(data["blocked_removals"]), 1)
        entry = data["blocked_removals"][0]
        self.assertEqual(entry["scope"], "isis")
        self.assertEqual(entry["job_id"], 50)
        self.assertEqual(
            entry["orphans"],
            {"interface-config": [["lo0", "ipv4"], ["lag31", "ipv4"]], "process-config": [["legacy"]]},
        )
        self.assertIn("would delete lo0", entry["preview"])
        self.assertEqual(entry["blocked_at"], "2026-07-10T06:00:00Z")

    def test_requested_apply_generations_are_wired_on_the_first_poll(self):
        generations = _device_generations()
        # Job fields come from JobOut in ../nso-adapter/tests/api/openapi_snapshot.json.
        jobs = [
            _removal_job(510, "logging", "running"),
            _removal_job(501, "vlan", "succeeded"),
        ]

        data = self._get_jobs(jobs, generations=generations, generation_ids=(81, 82))

        self.assertEqual(data["generations"], generations[:2])
        self.assertEqual(data["jobs"], [jobs[1]])

    def test_ordinary_failed_removal_not_blocked(self):
        """A removal that failed for another reason does not raise the banner."""
        job = _removal_job(51, "isis", "failed")
        job["error"] = {"code": "nso_error", "message": "boom"}
        data = self._get_jobs([job])
        self.assertEqual(data["blocked_removals"], [])

    def test_newer_succeeded_removal_clears_block(self):
        """A later succeeded removal for the SAME scope masks the stale block (list is newest-first)."""
        data = self._get_jobs([_removal_job(60, "isis", "succeeded"), _blocked_job(job_id=50)])
        self.assertEqual(data["blocked_removals"], [])

    def test_newer_queued_force_removal_masks_block(self):
        """A queued force re-run (scope only in context) already masks the banner."""
        queued = _removal_job(61, "isis", "queued")
        data = self._get_jobs([queued, _blocked_job(job_id=50)])
        self.assertEqual(data["blocked_removals"], [])

    def test_other_scope_success_keeps_block(self):
        """A newer succeeded removal for ANOTHER scope leaves the isis block standing."""
        data = self._get_jobs([_removal_job(62, "bgp", "succeeded"), _blocked_job(job_id=50)])
        self.assertEqual([e["scope"] for e in data["blocked_removals"]], ["isis"])

    def test_legacy_job_without_context_attributed_via_error_detail(self):
        """Pre-context adapter jobs still surface: scope falls back to error.detail."""
        job = _blocked_job(job_id=49)
        job["context"] = None
        data = self._get_jobs([job])
        self.assertEqual([e["scope"] for e in data["blocked_removals"]], ["isis"])

    def test_legacy_blocked_job_shape_still_surfaces(self):
        """Blocked jobs from the isis-only guard era (orphan_interfaces/orphan_processes)
        still raise the banner — their lists compose into the generic orphans dict."""
        job = _blocked_job(job_id=48)
        job["error"]["detail"] = {
            "scope": "isis",
            "orphan_interfaces": [["lo0", "ipv4"]],
            "orphan_processes": ["OLD"],
            "preview": "p",
        }
        data = self._get_jobs([job])
        self.assertEqual(
            data["blocked_removals"][0]["orphans"],
            {"interface-config": [["lo0", "ipv4"]], "process-config": ["OLD"]},
        )

    def test_non_removal_jobs_ignored(self):
        """Sync/apply jobs never contribute to blocked_removals or mask a block."""
        sync = {
            "id": 70,
            "type": "sync",
            "device_id": 10,
            "status": "succeeded",
            "result": None,
            "context": None,
            "error": None,
            "created_at": "2026-07-10T08:00:00Z",
            "updated_at": "2026-07-10T08:00:00Z",
            "started_at": None,
            "heartbeat_at": None,
        }
        data = self._get_jobs([sync, _blocked_job(job_id=50)])
        self.assertEqual([e["scope"] for e in data["blocked_removals"]], ["isis"])


class TestForceRemovalView(BlockedRemovalTestBase):
    """POST force-removal proxies the adapter override and gates on permission."""

    def _url(self):
        return reverse("plugins:netbox_nso_plugin:nsodevicemanagement_force_removal", args=[self.mgmt.pk])

    def test_ajax_post_triggers_adapter_force_removal(self):
        """AJAX POST hits the adapter force-removal endpoint with the scope and returns the job id."""
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            session = make_session(response=make_response(202, json_data={"job_id": 77}))
            mock_session_cls.return_value = session
            response = self.client.post(self._url(), {"scope": "isis"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["job_id"], 77)
        method, api_url = session.request.call_args[0][:2]
        self.assertEqual(method, "POST")
        self.assertIn("/api/v1/devices/10/actions/force-removal", api_url)
        self.assertEqual(session.request.call_args.kwargs.get("json"), {"scope": "isis"})

    def test_plain_post_redirects_to_tab(self):
        """A non-AJAX POST queues the job and redirects back to the device."""
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            mock_session_cls.return_value = make_session(response=make_response(202, json_data={"job_id": 78}))
            response = self.client.post(self._url(), {"scope": "isis"})
        self.assertEqual(response.status_code, 302)

    def test_missing_scope_rejected(self):
        """POST without a scope is a 400 (AJAX) — nothing reaches the adapter."""
        response = self.client.post(self._url(), {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)

    def test_not_onboarded_rejected(self):
        """POST for a device without adapter_device_id is a 409 (AJAX)."""
        self.mgmt.adapter_device_id = None
        self.mgmt.save(update_fields=["adapter_device_id"])
        try:
            response = self.client.post(self._url(), {"scope": "isis"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertEqual(response.status_code, 409)
        finally:
            self.mgmt.adapter_device_id = 10
            self.mgmt.save(update_fields=["adapter_device_id"])

    def test_adapter_error_reported(self):
        """An adapter 4xx/5xx surfaces as 502 JSON, not a success."""
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            mock_session_cls.return_value = make_session(
                response=make_response(400, json_data={"error": {"code": "bad_request", "message": "Unknown scope"}})
            )
            response = self.client.post(self._url(), {"scope": "nope"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 502)

    def test_requires_action_permission(self):
        """An authenticated user without change_nsodevicemanagement gets 403."""
        plain = User.objects.create_user(username="brplainuser", password=TEST_PASSWORD)
        self.client.force_login(plain)
        response = self.client.post(self._url(), {"scope": "isis"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 403)


def _residue_job(job_id=60, scope="svi", residue=None, updated="2026-07-11T20:00:00Z"):
    """A SUCCEEDED removal whose post-retract check found device residue (#104-A)."""
    return {
        "id": job_id,
        "type": "removal",
        "device_id": 10,
        "status": "succeeded",
        "result": {
            "scope": scope,
            "residue_check": "found",
            "residue": residue or {"interface": [["Vlan987"]]},
        },
        "context": {"scope": scope, "removed": {"interface": [["Vlan987"]]}},
        "error": None,
        "created_at": "2026-07-11T19:59:00Z",
        "updated_at": updated,
        "started_at": "2026-07-11T19:59:30Z",
        "heartbeat_at": None,
    }


class TestDeviceJobsResidueRemovals(BlockedRemovalTestBase):
    """#104-A: a succeeded removal whose result carries residue is surfaced.

    FASTMAP can keep a retracted entry that picked up foreign leaves (the sw03
    Vlan987 husk) while the removal job reports success — the adapter now records
    the survivors in job.result.residue and the tab must attribute the reappeared
    unowned rows to the retraction instead of presenting them as new device config.
    """

    def test_residue_removal_surfaced_with_detail(self):
        data = self._get_jobs([_residue_job()])
        self.assertEqual(len(data["residue_removals"]), 1)
        entry = data["residue_removals"][0]
        self.assertEqual(entry["scope"], "svi")
        self.assertEqual(entry["job_id"], 60)
        self.assertEqual(entry["residue"], {"interface": [["Vlan987"]]})
        self.assertEqual(entry["detected_at"], "2026-07-11T20:00:00Z")

    def test_clean_removal_yields_no_entry(self):
        job = _residue_job()
        job["result"] = {"scope": "svi", "residue_check": "clean"}
        data = self._get_jobs([job])
        self.assertEqual(data["residue_removals"], [])

    def test_newer_clean_removal_masks_stale_residue(self):
        newer = _residue_job(job_id=61)
        newer["result"] = {"scope": "svi", "residue_check": "clean"}
        data = self._get_jobs([newer, _residue_job(job_id=60)])  # most-recent-first
        self.assertEqual(data["residue_removals"], [])

    def test_other_scope_clean_removal_keeps_residue(self):
        clean_static = _residue_job(job_id=62, scope="static_route")
        clean_static["result"] = {"scope": "static_route", "residue_check": "clean"}
        data = self._get_jobs([clean_static, _residue_job(job_id=60)])
        self.assertEqual(len(data["residue_removals"]), 1)
        self.assertEqual(data["residue_removals"][0]["scope"], "svi")

    def test_blocked_removal_not_double_reported_as_residue(self):
        data = self._get_jobs([_blocked_job()])
        self.assertEqual(data["residue_removals"], [])

    def test_tab_contains_residue_template(self):
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            mock_session_cls.return_value = make_session(response=make_response(200, json_data={"id": 10}))
            url = reverse("dcim:device_nso", args=[self.device.pk])
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="nso-residue-removals"', html)
        self.assertIn('id="nso-residue-removal-tpl"', html)


class TestCategoryGridResidueBadges(BlockedRemovalTestBase):
    """#104 phase-2: grid rows whose key survives a retraction get a per-row badge.

    The tab banner already lists the surviving keys; the badge attributes the
    re-imported husk IN PLACE so an operator scanning a category grid doesn't
    read it as new device config. Newest-first masking is shared with the banner
    (_residue_removals), so a later clean removal clears the badges too.
    """

    def _get_category(self, key, jobs):
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            mock_session_cls.return_value = make_session(response=make_response(200, json_data=jobs))
            url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": key})
            return self.client.get(url)

    def test_vlan_grid_badges_surviving_key_only(self):
        """Paged-category path: the surviving vid is badged, its sibling is not; the
        badge cites the removal job. Residue keys arrive as JSON ints — the match
        must str-normalize both sides."""
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        group = _device_vlan_group(self.device)
        v987 = VLAN.objects.create(group=group, vid=987, name="husk")
        v100 = VLAN.objects.create(group=group, vid=100, name="fine")
        NSOVLANState.objects.create(management=self.mgmt, vlan=v987, status="imported")
        NSOVLANState.objects.create(management=self.mgmt, vlan=v100, status="imported")

        jobs = [_residue_job(job_id=71, scope="vlan", residue={"vlan": [[987]]})]
        resp = self._get_category("vlan", jobs)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(html.count("removal residue"), 1)
        self.assertIn("adapter job #71", html)

    def test_isis_grid_badges_interface_and_process_rows(self):
        """Reconcile-on-expand path: the compound (interface-name, af) key and the
        process tag both match their residue lists (export names interface/process,
        trigger says interface-config/process-config)."""
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOISISInstanceState, NSOISISInterfaceState

        iface = Interface.objects.create(device=self.device, name="ge-0/0/0", type="other")
        iface2 = Interface.objects.create(device=self.device, name="ge-0/0/1", type="other")
        r1 = NSOISISInterfaceState.objects.create(
            management=self.mgmt, interface=iface, af="ipv4", process_tag="CORE", status="imported"
        )
        r2 = NSOISISInterfaceState.objects.create(
            management=self.mgmt, interface=iface2, af="ipv4", process_tag="CORE", status="imported"
        )
        proc = NSOISISInstanceState.objects.create(management=self.mgmt, process_tag="CORE", status="imported")

        jobs = [
            _residue_job(
                job_id=72,
                scope="isis",
                residue={"interface-config": [["ge-0/0/0", "ipv4"]], "process-config": [["CORE"]]},
            )
        ]
        with patch(
            "netbox_nso_plugin.reconcile.reconcile_category",
            return_value={"isis_interfaces": [r1, r2], "isis_processes": [proc]},
        ):
            resp = self._get_category("isis", jobs)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        # IS-IS is a client-side grid now: the residue flag rides in the embedded payload
        # and nso-grid.js draws the badge. Assert the flag lands on exactly the surviving
        # rows — stronger than counting badge strings, which could not tell WHICH row was
        # badged, and so would have passed had the residue been pinned to the wrong one.
        payload = json.loads(re.search(r'<script id="nso-isis-data"[^>]*>(.*?)</script>', html, re.S).group(1))
        by_iface = {(r["iface"]["name"], r["af"]): r for r in payload["interfaces"]["rows"]}
        self.assertTrue(by_iface[("ge-0/0/0", "ipv4")]["residue"])  # survived the retraction
        self.assertFalse(by_iface[("ge-0/0/1", "ipv4")]["residue"])  # sibling did not
        self.assertEqual(by_iface[("ge-0/0/0", "ipv4")]["residue_job"], 72)

        procs = {r["process_tag"]: r for r in payload["instances"]["rows"]}
        self.assertTrue(procs["CORE"]["residue"])
        self.assertEqual(procs["CORE"]["residue_job"], 72)

    def test_interface_ips_grid_badges_surviving_address_values(self):
        """#104 phase-3: interface receipt residue is value-grain. The surviving
        (interface, address, vrf) triples are badged, siblings are not. The trigger
        reports the NetBox text form while the re-imported row carries the device
        form, so IPv6 case/zero-compression must normalize before matching."""
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceIPState

        iface = Interface.objects.create(device=self.device, name="Gi0/3", type="other")
        survivor = NSOInterfaceIPState.objects.create(interface=iface, address="10.0.0.2/24", vrf="", status="imported")
        survivor_v6 = NSOInterfaceIPState.objects.create(
            interface=iface, address="2001:db8::1/64", vrf="CUST", family="ipv6", status="imported"
        )
        sibling = NSOInterfaceIPState.objects.create(interface=iface, address="10.0.0.1/24", vrf="", status="imported")

        jobs = [
            _residue_job(
                job_id=73,
                scope="interface_config",
                residue={"address": [["Gi0/3", "10.0.0.2/24", ""], ["Gi0/3", "2001:DB8:0:0::1/64", "CUST"]]},
            )
        ]
        with patch(
            "netbox_nso_plugin.reconcile.reconcile_category",
            return_value={"interface_ips": [survivor, survivor_v6, sibling]},
        ):
            resp = self._get_category("interface_ips", jobs)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(html.count("removal residue"), 2)
        self.assertIn("adapter job #73", html)

    def test_grid_renders_unbadged_when_jobs_unavailable(self):
        """Badges are best-effort decoration: adapter trouble must not break the grid."""
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=200, name="ok")
        NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, status="imported")

        with patch("netbox_nso_plugin.adapter_client.list_jobs", side_effect=Exception("adapter down")):
            url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "vlan"})
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("removal residue", resp.content.decode())


class TestTabBannerWiring(BlockedRemovalTestBase):
    """The NSO tab ships the banner template + force form for the jobs-poll JS to clone."""

    def test_tab_contains_blocked_removal_template(self):
        """Tab HTML carries the <template>, the container and the force-removal action URL."""
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            mock_session_cls.return_value = make_session(response=make_response(200, json_data={"id": 10}))
            url = reverse("dcim:device_nso", args=[self.device.pk])
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="nso-blocked-removals"', html)
        self.assertIn('id="nso-blocked-removal-tpl"', html)
        force_url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_force_removal", args=[self.mgmt.pk])
        self.assertIn(force_url, html)


class TestFreeFormErrorDetail(TestCase):
    """``error`` is an object by contract; what is INSIDE it is free-form JSON.

    The client boundary pins ``job.error`` to ``dict | None``, and nothing more: ``detail``
    and its members are an unschema'd JSON column in the adapter's store. So the readers
    that walk into it still check what they find, and the helpers are called directly here
    because that walk is the whole unit under test.
    """

    def _job(self, error):
        job = _removal_job(80, "isis", "failed", context=False)
        job["error"] = error
        return job

    def test_the_scope_falls_back_to_the_error_detail(self):
        """The attribution path the blocked-job legacy shape actually depends on.

        Context and result are both absent here, so ``error.detail`` is the only place a
        scope can come from; a job without one is skipped before any error is read.
        """
        from netbox_nso_plugin.views import _removal_job_scope

        self.assertEqual(_removal_job_scope(self._job({"detail": {"scope": "isis"}})), "isis")

    def test_a_non_object_detail_does_not_break_the_attribution(self):
        from netbox_nso_plugin.views import _removal_job_scope

        for detail in ("boom", 3, ["scope"]):
            with self.subTest(detail=detail):
                self.assertIsNone(_removal_job_scope(self._job({"detail": detail})))

    def test_a_non_object_orphans_map_still_raises_the_banner(self):
        """The block is the operator-critical fact: a junk orphans map must not hide it."""
        from netbox_nso_plugin.views import _blocked_removals

        job = _removal_job(81, "isis", "failed")
        job["error"] = {
            "code": "removal_blocked_collateral",
            "message": "blocked",
            "detail": {"scope": "isis", "orphans": "boom", "preview": "p"},
        }

        entries = _blocked_removals([job])

        self.assertEqual([e["scope"] for e in entries], ["isis"])
        self.assertEqual(entries[0]["orphans"], {})

    def test_a_non_object_detail_on_a_blocked_job_still_reports_the_block(self):
        """Scope comes from the context here, so the block is known even with no usable detail."""
        from netbox_nso_plugin.views import _blocked_removals

        job = _removal_job(82, "isis", "failed")
        job["error"] = {"code": "removal_blocked_collateral", "message": "blocked", "detail": "boom"}

        entries = _blocked_removals([job])

        self.assertEqual([e["scope"] for e in entries], ["isis"])
        self.assertEqual(entries[0]["orphans"], {})
        self.assertEqual(entries[0]["preview"], "")


class TestFreeFormResidue(TestCase):
    """``result`` is an object by contract; the residue map inside it is free-form JSON.

    The residue badge is best-effort decoration on the category grid, so a junk report must
    cost the badge and nothing else. Before the guard it raised out of the grid view, which
    the call sites do not wrap.
    """

    def _annotate(self, residue):
        """Run the real annotator against a job carrying *residue*, return its VLAN row."""
        from netbox_nso_plugin.views import _annotate_residue_rows

        job = _removal_job(90, "vlan", "succeeded")
        job["result"] = {"scope": "vlan", "residue": residue}
        mgmt = NSODeviceManagement(adapter_device_id=10)
        row = SimpleNamespace(vlan=SimpleNamespace(vid="b"))
        ctx = {"vlan_states": [row]}
        with patch("netbox_nso_plugin.adapter_client.list_jobs", return_value=[job]):
            _annotate_residue_rows(ctx, "vlan", mgmt)
        return row

    def test_a_non_object_residue_map_costs_only_the_badge(self):
        for residue in ("boom", 3, ["vlan"]):
            with self.subTest(residue=residue):
                self.assertFalse(hasattr(self._annotate(residue), "residue_survivor"))

    def test_a_non_list_key_list_costs_only_that_label(self):
        self.assertFalse(hasattr(self._annotate({"vlan": "boom"}), "residue_survivor"))
