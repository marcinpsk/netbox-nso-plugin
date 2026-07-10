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

    def _get_jobs(self, jobs):
        """Hit device_nso_jobs with the adapter's job list canned at the transport."""
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_session_cls,
        ):
            mock_session_cls.return_value = make_session(response=make_response(200, json_data=jobs))
            url = reverse("plugins:netbox_nso_plugin:device_nso_jobs", args=[self.device.pk])
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)


class TestDeviceJobsBlockedRemovals(BlockedRemovalTestBase):
    """device_nso_jobs surfaces blocked removals — latest removal job per scope decides."""

    def test_blocked_removal_surfaced_with_detail(self):
        """A failed removal with removal_blocked_collateral yields a blocked_removals entry."""
        data = self._get_jobs([_blocked_job()])
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
