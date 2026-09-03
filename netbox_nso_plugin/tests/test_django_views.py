# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django-stack tests for views: list, detail, CRUD, action views.

These tests require the full NetBox/Django stack (run in devcontainer).
Adapter calls are mocked so no live adapter is needed.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import requests
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.models import (
    AdapterConnection,
    NSODerivedIntentTemplate,
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
)

from ._adapter_http import make_response, make_session
from ._outbox_case import (
    ReceiptAdapter,
    content_bulk_update,
    make_managed,
    mirror_update,
    without_commit_drain,
)
from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin, _CascadeFlushMixin

User = get_user_model()
TEST_PASSWORD = "testpass789"  # noqa: S105


def _make_fixtures():
    """Create a reusable set of DB objects for view tests."""
    manufacturer = Manufacturer.objects.create(name="ViewMfgNSO", slug="viewmfgnso")
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ViewDevNSO", slug="viewdevnso")
    role = DeviceRole.objects.create(name="ViewRoleNSO", slug="viewrolenso")
    site = Site.objects.create(name="ViewSiteNSO", slug="viewsitenso")
    device = Device.objects.create(name="view-router-01", device_type=device_type, role=role, site=site)
    nso_instance = NSOInstance.objects.create(name="view-nso", adapter_instance_id="view-nso-id")
    mgmt = NSODeviceManagement.objects.create(
        device=device, nso_instance=nso_instance, nso_device_name="view-router-01"
    )
    interface = Interface.objects.create(device=device, name="Loopback0", type="virtual")
    iface_state = NSOInterfaceState.objects.create(
        interface=interface, attribute="description", status="changed", nso_value="test desc"
    )
    return {
        "device": device,
        "nso_instance": nso_instance,
        "mgmt": mgmt,
        "interface": interface,
        "iface_state": iface_state,
    }


class ViewTestBase(IntentPushDeliveryMixin, TestCase):
    """Base class: creates superuser and logs in, creates fixtures.

    The view cases assert that an edit reached the adapter, and a ``TestCase`` cannot drain:
    its transaction never commits and the drain refuses to run inside one. The mixin delivers
    what the transaction scheduled instead, through the same choke point the claim uses.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="viewtestnsoadmin",
            password=TEST_PASSWORD,
            email="nsoadmin@test.example",
        )
        fixtures = _make_fixtures()
        cls.device = fixtures["device"]
        cls.nso_instance = fixtures["nso_instance"]
        cls.mgmt = fixtures["mgmt"]
        cls.interface = fixtures["interface"]
        cls.iface_state = fixtures["iface_state"]

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)


# ── Onboarding dashboard (NSO Devices) ────────────────────────────────────────────


class TestOnboardingDashboardView(ViewTestBase):
    """Tests for the NSO Devices dashboard render + quick-manage action."""

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[{"ned_id": "cisco-ios-cli-6.114"}])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices")
    def test_dashboard_renders_all_tabs(self, mock_list, _mock_neds):
        """Dashboard renders 200 with managed + external rows — guards the {% url %}
        wiring for the per-row edit/delete and quick-manage buttons."""
        # The managed device (self.mgmt.device) appears in NSO as a managed device,
        # plus an 'external' device matched by name only.
        ext = Device.objects.create(
            name="ext-router-01", device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        mock_list.return_value = [
            {
                "name": self.mgmt.nso_device_name,
                "ned_id": "cisco-ios-cli-6.114",
                "admin_state": "unlocked",
                "onboarded_netbox_device_id": self.device.id,
            },
            {"name": "ext-router-01", "ned_id": "juniper-junos-nc-4.19", "admin_state": "unlocked"},
        ]
        url = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # external device (matched, not plugin-managed) is offered a quick-manage button
        self.assertContains(response, "Manage")
        self.assertEqual(ext.name, "ext-router-01")

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    def test_dashboard_shows_partial_status_with_degraded_surfaces(self, _list, _neds):
        """The managed-row status column renders a warning 'partial' badge whose tooltip
        names the stale surfaces, from the cached mgmt fields (no fresh adapter poll)."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.last_sync_status = "partial"
        mgmt.degraded_surfaces = ["bgp", "ospf"]
        mgmt.save(update_fields=["last_sync_status", "degraded_surfaces"])

        url = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("text-bg-warning", html)
        self.assertIn("partial", html)
        self.assertIn("bgp, ospf", html)

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_devices")
    def test_dashboard_refresh_costs_no_query_per_managed_row(self, mock_devices, _list, _neds):
        """The pre-render mirror refresh classifies every row through its ``NSOInstance``.

        Without that join on the dashboard queryset each row costs one more NSOInstance
        query, so the page grows with the fleet. Counting only that table isolates the join
        from whatever else a longer row list renders.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.models import NSODeviceManagement

        mirror_update(self.mgmt, adapter_device_id=901)
        self.addCleanup(mirror_update, self.mgmt, adapter_device_id=None)

        def _snapshot():
            # Every row matched and already current, so the refresh mirrors nothing and
            # writes nothing: what is counted is the classification, not an update.
            return [
                {
                    "id": m.adapter_device_id,
                    "nso_instance": self.nso_instance.adapter_instance_id,
                    "nso_device_name": m.nso_device_name,
                    "netbox_device_id": m.device_id,
                    "last_sync_at": None,
                    "last_sync_status": "",
                    "degraded_surfaces": None,
                }
                for m in NSODeviceManagement.objects.filter(nso_instance=self.nso_instance)
            ]

        mock_devices.side_effect = lambda: _snapshot()
        url = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")
        params = {"instance": self.nso_instance.adapter_instance_id}
        table = NSODeviceManagement._meta.get_field("nso_instance").related_model._meta.db_table

        def _instance_queries():
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(url, params).status_code, 200)
            return [q["sql"] for q in captured.captured_queries if table in q["sql"]]

        _instance_queries()  # warm the per-request caches the count must not measure
        one_row = _instance_queries()

        extra = []
        for n in (2, 3):
            dev = Device.objects.create(
                name=f"dash-count-{n}",
                device_type=self.device.device_type,
                role=self.device.role,
                site=self.device.site,
            )
            extra.append(
                NSODeviceManagement.objects.create(
                    device=dev,
                    nso_instance=self.nso_instance,
                    nso_device_name=f"dash-count-{n}",
                    adapter_device_id=900 + n,
                )
            )
        three_rows = _instance_queries()

        self.assertEqual(
            len(three_rows),
            len(one_row),
            f"the dashboard reads NSOInstance once per managed row: {three_rows}",
        )
        for mgmt in extra:
            mgmt.device.delete()

    def test_quick_manage_creates_management(self):
        """POST to quick_manage creates the management record for an external device.

        The NSODeviceManagement post_save signal fires and attempts an adapter call,
        which it swallows on error — so no adapter mock is needed here.
        """
        ext = Device.objects.create(
            name="ext-router-02", device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        url = reverse("plugins:netbox_nso_plugin:quick_manage")
        response = self.client.post(
            url,
            {"device": ext.pk, "instance": self.nso_instance.adapter_instance_id, "nso_name": "ext-router-02"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(NSODeviceManagement.objects.filter(device=ext).exists())

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    def test_provisioning_row_poll_uses_server_rendered_csrf(self, _list, _neds):
        """A provisioning row renders the poll script with a SERVER-rendered CSRF token.

        NetBox sets CSRF_COOKIE_HTTPONLY=True, so the csrftoken cookie is NOT readable from
        document.cookie — the dashboard poll must embed the token server-side or every
        onboard-status POST 403s and the row never advances (caught in live Playwright).
        """
        dev = Device.objects.create(
            name="prov-dash", device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        NSODeviceManagement.objects.create(
            device=dev,
            nso_instance=self.nso_instance,
            nso_device_name="prov-dash",
            onboard_status="provisioning",
            onboard_job_id="123",
        )
        url = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")
        response = self.client.get(url, {"instance": self.nso_instance.adapter_instance_id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-onboard-pk=")  # the row is pollable
        self.assertContains(response, 'var csrfToken = "')  # token rendered server-side
        # The HttpOnly cookie read must NOT be how the poll obtains the token.
        self.assertNotContains(response, "document.cookie.match(/csrftoken")


class TestOnboardStatusView(ViewTestBase):
    """Async-onboarding status-advance endpoint (polled by the dashboard while a row provisions).

    Drives the real view through its URL: it polls the adapter job (mocked at the HTTP-boundary
    client) and advances the NSODeviceManagement row. The success path proves the gated
    adapter-push signal *re-fires* once the row flips to ready.
    """

    def _provisioning_mgmt(self, name, job_id="99"):
        dev = Device.objects.create(
            name=name, device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        # Created in 'provisioning' → the post_save signal is gated (no adapter call here).
        return NSODeviceManagement.objects.create(
            device=dev,
            nso_instance=self.nso_instance,
            nso_device_name=name,
            onboard_status="provisioning",
            onboard_job_id=job_id,
        )

    def _post_status(self, mgmt):
        return self.client.post(reverse("plugins:netbox_nso_plugin:onboard_status", args=[mgmt.pk]))

    @patch("netbox_nso_plugin.adapter_client.get_job", return_value={"status": "running"})
    def test_running_job_stays_provisioning(self, _job):
        mgmt = self._provisioning_mgmt("prov-running")
        resp = self._post_status(mgmt)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "provisioning")
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provisioning")

    @patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None)
    @patch("netbox_nso_plugin.adapter_client.set_scope")
    @patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 321})
    @patch(
        "netbox_nso_plugin.adapter_client.get_job",
        return_value={
            "status": "succeeded",
            "result": {"ok": True, "steps": [{"step": "create", "status": "ok"}], "device_id": None},
        },
    )
    def test_succeeded_job_flips_ready_and_fires_signal(self, _job, onboard, _scope, _notify):
        mgmt = self._provisioning_mgmt("prov-ok")
        # The mapping push is deferred to transaction.on_commit; TestCase never commits.
        with self.captureOnCommitCallbacks(execute=True):
            resp = self._post_status(mgmt)
        self.assertEqual(resp.json()["status"], "ready")
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "")
        # Flipping to ready re-fires the (now un-gated) adapter mapping signal.
        onboard.assert_called_once()
        self.assertEqual(mgmt.adapter_device_id, 321)

    @patch(
        "netbox_nso_plugin.adapter_client.get_job",
        return_value={
            "status": "succeeded",
            "result": {
                "ok": False,
                "steps": [{"step": "fetch_host_keys", "status": "failed", "detail": "timeout"}],
            },
        },
    )
    def test_succeeded_but_failed_step_marks_provision_failed(self, _job):
        mgmt = self._provisioning_mgmt("prov-stepfail")
        resp = self._post_status(mgmt)
        self.assertEqual(resp.json()["status"], "provision_failed")
        self.assertEqual(resp.json()["error"], "Provisioning failed. See the server log.")
        self.assertNotIn("timeout", resp.content.decode())
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provision_failed")
        self.assertEqual(mgmt.onboard_error, "Provisioning failed. See the server log.")

    @patch(
        "netbox_nso_plugin.adapter_client.get_job",
        return_value={"status": "failed", "error": {"message": "Provision exceeded 600s timeout"}},
    )
    def test_failed_job_marks_provision_failed(self, _job):
        mgmt = self._provisioning_mgmt("prov-jobfail")
        resp = self._post_status(mgmt)
        self.assertEqual(resp.json()["status"], "provision_failed")
        self.assertEqual(resp.json()["error"], "Provisioning failed. See the server log.")
        self.assertNotIn("600s", resp.content.decode())
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provision_failed")
        self.assertEqual(mgmt.onboard_error, "Provisioning failed. See the server log.")

    def test_already_terminal_is_idempotent(self):
        """A row that already reached a terminal state just reports it (no adapter poll)."""
        mgmt = self._provisioning_mgmt("prov-terminal", job_id="1")
        mgmt.onboard_status = "provision_failed"
        mgmt.onboard_error = "earlier failure"
        mgmt.save(update_fields=["onboard_status", "onboard_error"])
        resp = self._post_status(mgmt)  # no get_job patch — terminal short-circuits
        self.assertEqual(resp.json()["status"], "provision_failed")
        self.assertEqual(resp.json()["error"], "earlier failure")

    def test_missing_job_id_marks_failed(self):
        """A provisioning row with no job id can never advance → provision_failed."""
        mgmt = self._provisioning_mgmt("prov-nojob", job_id="")
        resp = self._post_status(mgmt)  # no get_job patch — never reached
        self.assertEqual(resp.json()["status"], "provision_failed")
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provision_failed")

    @patch("netbox_nso_plugin.adapter_client.get_job", side_effect=AdapterError("adapter down"))
    def test_transient_adapter_error_keeps_provisioning(self, _job):
        """A transient adapter error while polling keeps the row provisioning (client retries)."""
        mgmt = self._provisioning_mgmt("prov-blip")
        resp = self._post_status(mgmt)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "provisioning")
        self.assertIn("poll_error", resp.json())
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provisioning")


class TestFailoverSettingsDeploymentWarning(ViewTestBase):
    """The failover settings page warns when failover is off at the adapter deployment level.

    Without it, enabling failover here is a silent no-op (the adapter gates the whole feature
    on its static enable_failover; the runtime toggle has no effect until that is on).
    """

    URL_NAME = "plugins:netbox_nso_plugin:nsofailoversettings"
    _WARN = "disabled at the adapter deployment level"

    @patch(
        "netbox_nso_plugin.adapter_client.get_failover_config",
        return_value={"deployment_enabled": False, "enabled": True},
    )
    def test_warns_when_deployment_failover_disabled(self, _cfg):
        response = self.client.get(reverse(self.URL_NAME))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._WARN)

    @patch(
        "netbox_nso_plugin.adapter_client.get_failover_config",
        return_value={"deployment_enabled": True, "enabled": True},
    )
    def test_no_warning_when_deployment_failover_enabled(self, _cfg):
        response = self.client.get(reverse(self.URL_NAME))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self._WARN)

    @patch("netbox_nso_plugin.adapter_client.get_failover_config", side_effect=AdapterError("adapter down"))
    def test_adapter_unreachable_does_not_block_or_warn(self, _cfg):
        response = self.client.get(reverse(self.URL_NAME))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self._WARN)


# ── List views ──────────────────────────────────────────────────────────────────


class TestNSOInstanceListView(ViewTestBase):
    """Tests for NSOInstanceListView."""

    def test_list_200(self):
        """List view returns 200 OK."""
        url = reverse("plugins:netbox_nso_plugin:nsoinstance_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_list_unauthenticated_redirects(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        url = reverse("plugins:netbox_nso_plugin:nsoinstance_list")
        response = self.client.get(url)
        self.assertIn(response.status_code, [302, 403])


class TestNSODeviceManagementListView(ViewTestBase):
    """Tests for NSODeviceManagementListView."""

    def test_list_200(self):
        """List view returns 200 OK."""
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_list_polls_and_refreshes_last_sync(self, mock_session_cls, mock_cfg):
        """List view refreshes cached last_sync_* via one bulk list_devices poll."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 16
        mgmt.last_sync_at = None
        mgmt.last_sync_status = ""
        mgmt.save(update_fields=["adapter_device_id", "last_sync_at", "last_sync_status"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }

        calls = []

        def make_resp(method, url, **kwargs):
            calls.append(url)
            return make_response(
                200,
                json_data=[
                    {
                        "id": 16,
                        "nso_instance": "view-nso-id",
                        "nso_device_name": "view-router-01",
                        "netbox_device_id": None,
                        "last_sync_at": "2025-06-01T10:00:00Z",
                        "last_sync_status": "succeeded",
                    }
                ],
            )

        session = make_session()
        session.request = make_resp
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Poll hit the bulk device list (not /interfaces or /state — list is lightweight).
        self.assertTrue(any(u.endswith("/api/v1/devices") for u in calls), calls)
        self.assertFalse(any("/state" in u or "/interfaces" in u for u in calls), calls)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "succeeded")
        self.assertIsNotNone(mgmt.last_sync_at)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_successful_poll_keeps_adapter_link_error(self, mock_session_cls, mock_cfg):
        """A poll finding the device synced 'succeeded' must NOT retire an adapter_link_error.

        The two live on different clocks: the error is a failed plugin→adapter scope push, the
        status is the adapter→NSO device sync, stamped whenever the adapter last synced. A
        'succeeded' that predates the failure would clear the banner (and its "Retry adapter
        link" button) for a scope that never landed. Only the successful push or the retry
        action clears it."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 16
        mgmt.save(update_fields=["adapter_device_id"])
        # Stage the stale error exactly as production writes it — .update() fires no signals,
        # so the save above can't prematurely clear it.
        mirror_update(mgmt, adapter_link_error="Internal Server Error", last_sync_status="")

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }

        def make_resp(method, url, **kwargs):
            return make_response(
                200,
                json_data=[
                    {
                        "id": 16,
                        "nso_instance": "view-nso-id",
                        "nso_device_name": "view-router-01",
                        "netbox_device_id": None,
                        "last_sync_at": "2025-06-01T10:00:00Z",
                        "last_sync_status": "succeeded",
                    }
                ],
            )

        session = make_session()
        session.request = make_resp
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "succeeded")  # the poll did run
        self.assertEqual(mgmt.adapter_link_error, "Internal Server Error")

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_failed_poll_keeps_adapter_link_error(self, mock_session_cls, mock_cfg):
        """A poll whose device-level sync is NOT 'succeeded' must keep the link error visible."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 16
        mgmt.save(update_fields=["adapter_device_id"])
        mirror_update(mgmt, adapter_link_error="Internal Server Error", last_sync_status="")

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }

        def make_resp(method, url, **kwargs):
            return make_response(
                200,
                json_data=[
                    {
                        "id": 16,
                        "nso_instance": "view-nso-id",
                        "nso_device_name": "view-router-01",
                        "netbox_device_id": None,
                        "last_sync_at": "2025-06-01T10:00:00Z",
                        "last_sync_status": "failed",
                    }
                ],
            )

        session = make_session()
        session.request = make_resp
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_link_error, "Internal Server Error")

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_list_renders_partial_badge_and_caches_degraded_surfaces(self, mock_session_cls, mock_cfg):
        """A 'partial' device renders a warning badge in the table whose tooltip lists
        the stale surfaces, and caches degraded_surfaces on the row."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 17
        mgmt.last_sync_status = ""
        mgmt.degraded_surfaces = None
        mgmt.save()

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }

        def make_resp(method, url, **kwargs):
            return make_response(
                200,
                json_data=[
                    {
                        "id": 17,
                        "nso_instance": "view-nso-id",
                        "nso_device_name": "view-router-01",
                        "netbox_device_id": None,
                        "last_sync_at": "2025-06-01T10:00:00Z",
                        "last_sync_status": "partial",
                        "degraded_surfaces": ["bgp", "ospf"],
                    }
                ],
            )

        session = make_session()
        session.request = make_resp
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("text-bg-warning", html)
        self.assertIn("partial", html)
        self.assertIn("bgp, ospf", html)  # tooltip lists the stale surfaces

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.degraded_surfaces, ["bgp", "ospf"])

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_list_survives_adapter_error(self, mock_session_cls, mock_cfg):
        """An unreachable adapter must not break the list — error is swallowed per row."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 16
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        mock_session_cls.return_value = make_session(response=make_response(502, json_data={}))

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])


class TestNSOInterfaceStateListView(ViewTestBase):
    """Tests for NSOInterfaceStateListView."""

    def test_list_200(self):
        """List view returns 200 OK."""
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class TestInterfacesPageNoAttrsHint(ViewTestBase):
    """The interfaces page must explain an empty tab caused by no attrs in scope.

    manage_interfaces (master) on but neither manage_description nor manage_enabled
    (leaves) → empty adapter scope → a sync can never produce rows. The empty state
    must say that, not the misleading "wait for the next sync".
    """

    def _render(self, device):
        from django.test import RequestFactory

        from netbox_nso_plugin.views import NSOCategoryView

        req = RequestFactory().get("/x?key=interfaces")
        req.user = self.superuser
        return NSOCategoryView()._render_interfaces_page(req, device).content.decode()

    def test_hint_shown_when_master_on_no_leaves(self):
        """Master on, no leaf attrs, no rows → actionable hint, not the sync message."""
        self.mgmt.manage_interfaces = True
        self.mgmt.manage_description = False
        self.mgmt.manage_enabled = False
        self.mgmt.save()
        NSOInterfaceState.objects.filter(interface__device=self.device).delete()

        html = self._render(self.device)
        self.assertIn("no interface attributes are selected", html)
        self.assertNotIn("wait for the next sync", html)

    def test_no_hint_when_a_leaf_is_selected(self):
        """With an attribute in scope, the no-attrs hint must not appear."""
        self.mgmt.manage_interfaces = True
        self.mgmt.manage_description = True
        self.mgmt.manage_enabled = False
        self.mgmt.save()
        NSOInterfaceState.objects.filter(interface__device=self.device).delete()

        html = self._render(self.device)
        self.assertNotIn("no interface attributes are selected", html)


# ── Detail views ─────────────────────────────────────────────────────────────────


class TestNSOInstanceDetailView(ViewTestBase):
    """Tests for NSOInstanceView detail view."""

    def test_detail_200(self):
        """Detail view returns 200 OK."""
        url = reverse("plugins:netbox_nso_plugin:nsoinstance", args=[self.nso_instance.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class TestNSOPlatformNedMappingDetailView(ViewTestBase):
    """Tests for NSOPlatformNedMappingView detail view (regression: detail template existed)."""

    def test_detail_200(self):
        """Detail view returns 200 — guards against a missing detail template,
        which previously crashed the post-create redirect (TemplateDoesNotExist)."""
        from dcim.models import Platform

        from netbox_nso_plugin.models import NSOPlatformNedMapping

        platform = Platform.objects.create(name="NedMapPlatform", slug="nedmapplatform")
        mapping = NSOPlatformNedMapping.objects.create(
            platform=platform, ned_id="cisco-ios-cli-6.114:cisco-ios-cli-6.114"
        )
        url = reverse("plugins:netbox_nso_plugin:nsoplatformnedmapping", args=[mapping.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class TestNSODeviceManagementDetailView(ViewTestBase):
    """Tests for NSODeviceManagementView — adapter_device_id is None (no adapter calls)."""

    def test_detail_200_no_adapter_id(self):
        """Detail view returns 200 when adapter_device_id is None."""
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement", args=[self.mgmt.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_detail_200_with_adapter_id(self, mock_session_cls):
        """Detail view returns 200 when adapter_device_id is set and adapter returns data."""
        # Set adapter_device_id temporarily on a fresh mgmt record
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 99
        mgmt.save(update_fields=["adapter_device_id"])

        session = make_session(response=make_response(200, json_data={"interfaces": [], "compliant": True}))
        mock_session_cls.return_value = session

        with patch("netbox_nso_plugin.adapter_client._resolve_config") as mock_cfg:
            mock_cfg.return_value = {
                "url": "http://adapter",
                "token": "tok",
                "verify_tls": True,
                "ca_cert_path": None,
                "timeout": 30,
            }
            url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement", args=[mgmt.pk])
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        # Reset
        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_detail_adapter_error_shows_page(self, mock_session_cls):
        """Detail view returns 200 even when adapter raises AdapterError."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 99
        mgmt.save(update_fields=["adapter_device_id"])

        session = make_session(
            response=make_response(
                502, json_data={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
            )
        )
        mock_session_cls.return_value = session

        with patch("netbox_nso_plugin.adapter_client._resolve_config") as mock_cfg:
            mock_cfg.return_value = {
                "url": "http://adapter",
                "token": "tok",
                "verify_tls": True,
                "ca_cert_path": None,
                "timeout": 30,
            }
            url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement", args=[mgmt.pk])
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])


class TestNSOInterfaceStateDetailView(ViewTestBase):
    """Tests for NSOInterfaceStateView."""

    def test_detail_200(self):
        """Detail view returns 200 OK."""
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate", args=[self.iface_state.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


# ── Edit/delete views ────────────────────────────────────────────────────────────


class TestNSOInstanceEditView(ViewTestBase):
    """Tests for NSOInstanceEditView (GET)."""

    def test_edit_get_200(self):
        """Edit view returns 200 on GET."""
        url = reverse("plugins:netbox_nso_plugin:nsoinstance_edit", args=[self.nso_instance.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_add_get_200(self):
        """Add view returns 200 on GET."""
        url = reverse("plugins:netbox_nso_plugin:nsoinstance_add")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class TestNSOInstanceDeleteView(ViewTestBase):
    """Tests for NSOInstanceDeleteView (GET).

    Uses a dedicated NSOInstance with no related NSODeviceManagement so that
    NetBox's ObjectDeleteView does not redirect on ProtectedError.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.deletable_instance = NSOInstance.objects.create(
            name="view-nso-deletable",
            adapter_instance_id="view-nso-deletable-id",
        )

    def test_delete_get_200(self):
        """Delete confirmation view returns 200 on GET."""
        url = reverse("plugins:netbox_nso_plugin:nsoinstance_delete", args=[self.deletable_instance.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class TestNSODeviceManagementEditView(ViewTestBase):
    """Tests for NSODeviceManagementEditView."""

    def test_add_get_200(self):
        """Add view returns 200 on GET."""
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_add")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_add_has_visible_return_to_nso_devices_dashboard(self):
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_add")
        dashboard = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{dashboard}"')
        self.assertContains(response, "Back to NSO Devices")
        self.assertEqual(response.context["return_url"], dashboard)

    def test_edit_preserves_explicit_device_tab_return(self):
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_edit", args=[self.mgmt.pk])
        device_tab = reverse("dcim:device_nso", args=[self.device.pk])

        response = self.client.get(url, {"return_url": device_tab})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{device_tab}"')
        self.assertEqual(response.context["return_url"], device_tab)

    def test_bulk_delete_has_visible_dashboard_return(self):
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_bulk_delete")
        dashboard = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")

        response = self.client.post(url, {"pk": self.mgmt.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Back to NSO Devices")
        self.assertEqual(response.context["return_url"], dashboard)

    def test_form_auto_fills_nso_device_name_from_device(self):
        """NSODeviceManagementForm pre-populates nso_device_name from a device pk in initial."""
        from netbox_nso_plugin.forms import NSODeviceManagementForm

        form = NSODeviceManagementForm(initial={"device": self.device.pk})
        self.assertEqual(form.initial.get("nso_device_name"), self.device.name)

    def test_form_auto_selects_default_instance(self):
        """A new NSODeviceManagementForm pre-selects the default NSO instance."""
        from netbox_nso_plugin.forms import NSODeviceManagementForm

        # self.nso_instance (from fixtures) is the first instance -> default.
        form = NSODeviceManagementForm(initial={"device": self.device.pk})
        self.assertEqual(form.initial.get("nso_instance"), self.nso_instance.pk)

    def test_form_does_not_override_chosen_instance(self):
        """An explicitly provided nso_instance is not overridden by the default."""
        from netbox_nso_plugin.forms import NSODeviceManagementForm

        other = NSOInstance.objects.create(name="other-nso", adapter_instance_id="other-id")
        form = NSODeviceManagementForm(initial={"device": self.device.pk, "nso_instance": other.pk})
        self.assertEqual(form.initial.get("nso_instance"), other.pk)

    def test_form_auto_fill_invalid_pk_is_ignored(self):
        """NSODeviceManagementForm silently ignores a non-existent device pk."""
        from netbox_nso_plugin.forms import NSODeviceManagementForm

        form = NSODeviceManagementForm(initial={"device": 99999})
        # Should not raise; nso_device_name stays unset
        self.assertNotIn("nso_device_name", form.initial)


class TestAdapterConnectionEditView(ViewTestBase):
    """Tests for AdapterConnectionEditView singleton."""

    def test_edit_get_200_no_existing(self):
        """Edit view returns 200 when no AdapterConnection exists yet."""
        AdapterConnection.objects.all().delete()
        url = reverse("plugins:netbox_nso_plugin:adapterconnection")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_edit_get_200_existing(self):
        """Edit view returns 200 when AdapterConnection exists."""
        AdapterConnection.objects.create(url="http://adapter:8000")
        url = reverse("plugins:netbox_nso_plugin:adapterconnection")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_edit_get_shows_derived_intent_section(self):
        """Edit view includes derived intent templates section in response."""
        url = reverse("plugins:netbox_nso_plugin:adapterconnection")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Derived Intent Templates", response.content)

    def test_derived_intent_templates_are_managed_in_the_ui(self):
        """The adapter page exposes database-backed template management, not deployment config."""
        NSODerivedIntentTemplate.objects.create(
            sentinel="[auto]",
            template="[auto] to {peer_host}:{peer_iface}",
        )

        response = self.client.get(reverse("plugins:netbox_nso_plugin:adapterconnection"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "[auto] to {peer_host}:{peer_iface}")
        self.assertContains(response, reverse("plugins:netbox_nso_plugin:nsoderivedintenttemplate_list"))
        self.assertNotContains(response, "Add <code>DERIVED_INTENT_TEMPLATES</code>")


class TestNSODerivedIntentTemplateViews(ViewTestBase):
    """Database-backed derived-intent templates are fully manageable through NetBox."""

    def test_list_and_add_pages_are_available(self):
        for url_name in ("nsoderivedintenttemplate_list", "nsoderivedintenttemplate_add"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(f"plugins:netbox_nso_plugin:{url_name}"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Derived Intent")

    def test_create_valid_template(self):
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsoderivedintenttemplate_add"),
            {
                "sentinel": "[auto]",
                "template": "[auto] to {peer_host}:{peer_iface}",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(NSODerivedIntentTemplate.objects.filter(sentinel="[auto]", enabled=True).exists())

    def test_detail_edit_and_delete_pages_are_available(self):
        template = NSODerivedIntentTemplate.objects.create(
            sentinel="[auto]",
            template="[auto] to {peer_host}:{peer_iface}",
        )
        for url_name in (
            "nsoderivedintenttemplate",
            "nsoderivedintenttemplate_edit",
            "nsoderivedintenttemplate_delete",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(f"plugins:netbox_nso_plugin:{url_name}", args=[template.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "[auto]")

    def test_unknown_placeholder_is_rejected_in_the_form(self):
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsoderivedintenttemplate_add"),
            {
                "sentinel": "[auto]",
                "template": "[auto] to {unknown_peer}",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unknown placeholder")
        self.assertFalse(NSODerivedIntentTemplate.objects.exists())

    def test_overlapping_enabled_sentinel_is_rejected(self):
        NSODerivedIntentTemplate.objects.create(sentinel="[auto]", template="[auto] {peer_host}")

        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsoderivedintenttemplate_add"),
            {
                "sentinel": "[auto]-edge",
                "template": "[auto]-edge {peer_host}",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ambiguous match order")
        self.assertEqual(NSODerivedIntentTemplate.objects.count(), 1)


# ── NSO Device Names AJAX view ───────────────────────────────────────────────────


class TestNSODeviceNamesView(ViewTestBase):
    """Tests for NSODeviceNamesView AJAX endpoint."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_returns_json(self, mock_session_cls, mock_cfg):
        """GET returns JSON list of NSO devices."""
        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(200, json_data=[{"name": "router-01", "onboarded": False}]))
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:ajax_nso_device_names", args=[self.nso_instance.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("devices", data)

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_adapter_error_returns_502(self, mock_session_cls, mock_cfg):
        """GET returns 502 JSON when adapter raises AdapterError."""
        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(
            response=make_response(
                502, json_data={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
            )
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:ajax_nso_device_names", args=[self.nso_instance.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 502)


# ── NSO Job Status view ──────────────────────────────────────────────────────────


class TestNSOJobStatusView(ViewTestBase):
    """Tests for NSOJobStatusView."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_job_returns_json(self, mock_session_cls, mock_cfg):
        """GET returns JSON job status from adapter."""
        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(200, json_data={"job_id": 42, "status": "completed"}))
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsojob_status", args=[42])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["job_id"], 42)

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_job_adapter_error(self, mock_session_cls, mock_cfg):
        """GET returns 502 on adapter error."""
        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(
            response=make_response(404, json_data={"error": {"code": "not_found", "message": "job gone", "detail": {}}})
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsojob_status", args=[99])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 502)


# ── Device action views ──────────────────────────────────────────────────────────


class TestNSODeviceActionView(ViewTestBase):
    """Tests for NSODeviceActionView POST."""

    def test_post_unknown_action_redirects(self):
        """POST with unknown action redirects with error message."""
        # Set adapter_device_id so action path is exercised
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "unknown-action"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    def test_post_no_adapter_id_redirects(self):
        """POST when device is not onboarded redirects with warning."""
        self.assertIsNone(self.mgmt.adapter_device_id)
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.mgmt.pk, "sync"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_sync_success_redirect(self, mock_session_cls, mock_cfg):
        """POST sync with adapter success redirects."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(202, json_data={"job_id": 5}))
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_sync_ajax_success(self, mock_session_cls, mock_cfg):
        """AJAX POST sync returns JSON on success."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(202, json_data={"job_id": 7}))
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "ok")

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_sync_conflict_ajax(self, mock_session_cls, mock_cfg):
        """AJAX POST reports that the conflicting sync job is queued."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(
            response=make_response(
                409,
                json_data={
                    "error": {
                        "code": "conflict",
                        "message": "A job of the requested type is already queued for this device",
                        "detail": {"job_id": 3},
                    }
                },
            )
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "status": "conflict",
                "message": "A job is already queued for this device. (Job ID: 3)",
                "job_id": 3,
                "job_type": None,
            },
        )

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_sync_from_nso_ajax_success(self, mock_session_cls, mock_cfg):
        """S5a C: the fifth action — comprehensive CDB-only read — rides the generic
        dispatch: AJAX POST hits /actions/sync-from-nso and returns the job id."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(202, json_data={"job_id": 77}))
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync-from-nso"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["job_id"], 77)
        called_path = session.request.call_args.args[1] if session.request.call_args.args else ""
        self.assertIn("/actions/sync-from-nso", str(called_path) + str(session.request.call_args))

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_sync_conflict_reports_queued_incumbent_type(self, mock_session_cls, mock_cfg):
        """A sync conflict names its queued incumbent without calling it running."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session()
        session.request.side_effect = [
            make_response(
                409,
                json_data={
                    "error": {
                        "code": "conflict",
                        "message": "A job of the requested type is already queued for this device",
                        "detail": {"job_id": 3},
                    }
                },
            ),
            make_response(200, json_data={"id": 3, "type": "sync", "status": "queued"}),
        ]
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 409)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "conflict")
        self.assertEqual(data["message"], "Another job is already queued: sync. (Job ID: 3)")
        self.assertEqual(data["job_id"], 3)
        self.assertEqual(data["job_type"], "sync")

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_conflict_incumbent_fetch_failure_falls_back(self, mock_session_cls, mock_cfg):
        """S5a C: the incumbent-type lookup is best-effort — a failed get_job still returns
        an honest generic conflict (job_type null), never a 500."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session()
        session.request.side_effect = [
            make_response(
                409,
                json_data={
                    "error": {
                        "code": "conflict",
                        "message": "A job of the requested type is already queued for this device",
                        "detail": {"job_id": 3},
                    }
                },
            ),
            make_response(500, content=b"boom"),
        ]
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 409)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "conflict")
        self.assertEqual(data["message"], "A job is already queued for this device. (Job ID: 3)")
        self.assertIsNone(data["job_type"])

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_conflict_with_non_object_detail_still_returns_conflict(self, mock_session_cls, mock_cfg):
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])
        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        mock_session_cls.return_value = make_session(
            response=make_response(
                409,
                json_data={
                    "error": {
                        "code": "conflict",
                        "message": "A job of the requested type is already queued for this device",
                        "detail": ["busy"],
                    }
                },
            )
        )

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "status": "conflict",
                "message": "A job is already queued for this device.",
                "job_id": None,
                "job_type": None,
            },
        )

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_conflict_does_not_reflect_an_invalid_job_id(self, mock_session_cls, mock_cfg):
        supplied = "Traceback: private job path"
        mirror_update(self.mgmt, adapter_device_id=10)
        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        mock_session_cls.return_value = make_session(
            response=make_response(
                409,
                json_data={
                    "error": {
                        "code": "conflict",
                        "message": "busy",
                        "detail": {"job_id": supplied},
                    }
                },
            )
        )

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["job_id"], None)
        self.assertNotIn(supplied, response.content.decode())

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_conflict_does_not_reflect_an_invalid_job_type(self, mock_session_cls, mock_cfg):
        supplied = "private_adapter_job"
        mirror_update(self.mgmt, adapter_device_id=10)
        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session()
        session.request.side_effect = [
            make_response(
                409,
                json_data={"error": {"code": "conflict", "message": "busy", "detail": {"job_id": 3}}},
            ),
            make_response(200, json_data={"id": 3, "type": supplied, "status": "queued"}),
        ]
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 409)
        self.assertIsNone(response.json()["job_type"])
        self.assertNotIn(supplied, response.content.decode())

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_sync_success_no_job_id(self, mock_session_cls, mock_cfg):
        """Non-AJAX POST sync with no job_id in response shows generic success message."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        # Adapter returns 202 but no job_id field
        session = make_session(response=make_response(202, json_data={}))
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_adapter_error_non_ajax(self, mock_session_cls, mock_cfg):
        """Non-AJAX non-conflict AdapterError shows error message and redirects."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(
            response=make_response(
                503, json_data={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
            )
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        # No HTTP_X_REQUESTED_WITH → non-AJAX path
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_sync_conflict_non_ajax_uses_queued_wording(self, mock_session_cls, mock_cfg):
        """A non-AJAX sync conflict redirects with truthful queued wording."""
        from django.contrib.messages import get_messages

        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session()
        session.request.side_effect = [
            make_response(
                409,
                json_data={
                    "error": {
                        "code": "conflict",
                        "message": "A job of the requested type is already queued for this device",
                        "detail": {"job_id": 3},
                    }
                },
            ),
            make_response(200, json_data={"id": 3, "type": "sync", "status": "queued"}),
        ]
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Another job is already queued: sync. (Job ID: 3)"],
        )

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_adapter_error_ajax(self, mock_session_cls, mock_cfg):
        """AJAX POST returns 502 error JSON on adapter failure."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 10
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(
            response=make_response(
                503, json_data={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
            )
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 502)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])


# ── Refresh compliance view ──────────────────────────────────────────────────────


class TestNSORefreshStateView(ViewTestBase):
    """Tests for NSORefreshStateView."""

    def test_post_no_adapter_id_redirects(self):
        """POST when not onboarded redirects with warning."""
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_refresh", args=[self.mgmt.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_success_redirects(self, mock_session_cls, mock_cfg):
        """POST with successful adapter response redirects."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 11
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(200, json_data={"interfaces": [], "compliant": True}))
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_refresh", args=[mgmt.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client.get_interfaces_doc")
    @patch("netbox_nso_plugin.adapter_client.get_state")
    def test_unavailable_interfaces_doc_keeps_last_known_snapshot(self, mock_state, mock_doc):
        """codex B5-F5: an interfaces-doc declaring unavailable (e.g. not_ready after a
        store reset) legitimately serves an EMPTY list — the cached snapshot must keep
        the last-known interfaces instead of being wiped by the non-authoritative read."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 11
        mgmt.state_snapshot = {
            "compliance": {"compliant": True},
            "interfaces": [{"name": "ge-0/0/0"}],
            "refreshed_at": "2026-07-20T00:00:00Z",
        }
        mgmt.save(update_fields=["adapter_device_id", "state_snapshot"])

        mock_state.return_value = {"compliant": True}
        mock_doc.return_value = {
            "interfaces": [],
            "read_state": {
                "outcome": "unavailable",
                "reason": "not_ready",
                "succeeded": None,
                "result": None,
                "attempt_id": None,
                "incarnation": "11111111-aaaa-4aaa-8aaa-111111111111",
                "incarnation_born": "2026-07-01T00:00:10Z",
            },
        }
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_refresh", args=[mgmt.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.state_snapshot["interfaces"], [{"name": "ge-0/0/0"}])

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client.get_interfaces_doc")
    @patch("netbox_nso_plugin.adapter_client.get_state")
    def test_degraded_authoritative_tuple_keeps_last_known_snapshot(self, mock_state, mock_doc):
        """codex B5-R2-4: authoritativeness is the FULL gate tuple, not the outcome
        name — outcome=present with succeeded=false/result=error must not wipe the
        cached interfaces; explicit read_state:null (malformed S4) must not either."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 11
        mgmt.state_snapshot = {"compliance": {}, "interfaces": [{"name": "xe-0/0/1"}], "refreshed_at": "x"}
        mgmt.save(update_fields=["adapter_device_id", "state_snapshot"])
        mock_state.return_value = {"compliant": True}
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_refresh", args=[mgmt.pk])

        mock_doc.return_value = {
            "interfaces": [],
            "read_state": {
                "outcome": "present",
                "succeeded": False,
                "result": "error",
                "attempt_id": 9,
                "incarnation": "11111111-aaaa-4aaa-8aaa-111111111111",
                "incarnation_born": "2026-07-01T00:00:10Z",
            },
        }
        self.client.post(url)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.state_snapshot["interfaces"], [{"name": "xe-0/0/1"}])

        mock_doc.return_value = {"interfaces": [], "read_state": None}  # explicit null
        self.client.post(url)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.state_snapshot["interfaces"], [{"name": "xe-0/0/1"}])

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_post_adapter_error_redirects(self, mock_session_cls, mock_cfg):
        """POST with adapter error still redirects."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 11
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(
            response=make_response(
                503, json_data={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
            )
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_refresh", args=[mgmt.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])


# ── Accept / Bulk Accept views ────────────────────────────────────────────────────


class TestNSOAcceptAttributeView(ViewTestBase):
    """Tests for NSOAcceptAttributeView."""

    def test_accept_changed_value_becomes_pending_apply(self):
        """Accepting a DIFFERING value (changed) → accepted (real intent to push)."""
        content_bulk_update(self.iface_state, status="changed")
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", args=[self.iface_state.pk])
        with patch("netbox_nso_plugin.signals.push_intent_on_accept"):
            response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.iface_state.refresh_from_db()
        self.assertEqual(self.iface_state.status, "accepted")

    def test_accept_matching_value_becomes_in_sync(self):
        """Accepting a value that already matches the device (imported) → in_sync,
        NOT pending apply — there is nothing to push (the ae2.0 fix)."""
        content_bulk_update(self.iface_state, status="imported")
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", args=[self.iface_state.pk])
        with patch("netbox_nso_plugin.signals.push_intent_on_accept"):
            response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.iface_state.refresh_from_db()
        self.assertEqual(self.iface_state.status, "in_sync")

    def test_accept_ajax_returns_json_no_redirect(self):
        """An XHR accept returns JSON (200) so the tab can refresh without collapsing."""
        content_bulk_update(self.iface_state, status="changed")
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", args=[self.iface_state.pk])
        with patch("netbox_nso_plugin.signals.push_intent_on_accept"):
            response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["status"], "ok")
        self.iface_state.refresh_from_db()
        self.assertEqual(self.iface_state.status, "accepted")


class TestNSOAcceptDeviceView(ViewTestBase):
    """Tests for NSOAcceptDeviceView (adopt the device value into NetBox)."""

    def test_accept_device_copies_value_and_sets_in_sync(self):
        """Accept-device writes the device (nso) value onto the interface → in_sync."""
        self.interface.description = "old-netbox"
        self.interface.save(update_fields=["description"])
        content_bulk_update(self.iface_state, status="changed", nso_value="DEVICE-NEW")

        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept_device", args=[self.iface_state.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        self.interface.refresh_from_db()
        self.iface_state.refresh_from_db()
        self.assertEqual(self.interface.description, "DEVICE-NEW")  # device value adopted
        self.assertEqual(self.iface_state.status, "in_sync")


class TestNSOInterfaceEditFieldView(ViewTestBase):
    """Tests for NSOInterfaceEditFieldView (inline edit of description/enabled from the tab)."""

    def _make_managed(self):
        """Put the fixture device under management for description+enabled with an adapter id."""
        self.mgmt.adapter_device_id = 42
        self.mgmt.manage_description = True
        self.mgmt.manage_enabled = True
        self.mgmt.save(update_fields=["adapter_device_id", "manage_description", "manage_enabled"])

    def test_edit_description_promotes_and_pushes(self):
        """Inline-editing description writes the interface and fires Decision-G (owns + pushes)."""
        self._make_managed()
        content_bulk_update(self.iface_state, status="imported", accepted_at=None)
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_edit_field", args=[self.iface_state.pk])

        with patch("netbox_nso_plugin.adapter_client.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, {"value": "operator-set"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["status"], "ok")
        self.interface.refresh_from_db()
        self.iface_state.refresh_from_db()
        self.assertEqual(self.interface.description, "operator-set")
        self.assertEqual(self.iface_state.status, "accepted")  # Decision-G: NetBox now owns it
        self.assertIsNotNone(self.iface_state.accepted_at)
        mock_put.assert_called()

    def test_toggle_enabled_flips_and_owns(self):
        """Inline toggle of enabled flips the interface and owns the 'enabled' attribute."""
        self._make_managed()
        self.interface.enabled = True
        self.interface.save(update_fields=["enabled"])
        en_state = NSOInterfaceState.objects.create(
            interface=self.interface, attribute="enabled", status="imported", nso_value="True"
        )
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_edit_field", args=[en_state.pk])

        with patch("netbox_nso_plugin.adapter_client.put_intent"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, {"value": "false"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.interface.refresh_from_db()
        en_state.refresh_from_db()
        self.assertFalse(self.interface.enabled)
        self.assertEqual(en_state.status, "accepted")

    def test_toggle_back_to_device_value_is_in_sync_not_pending(self):
        """Flipping enabled off then back on lands on the device value → in_sync, not pending."""
        self._make_managed()
        self.interface.enabled = True
        self.interface.save(update_fields=["enabled"])
        en_state = NSOInterfaceState.objects.create(
            interface=self.interface, attribute="enabled", status="in_sync", nso_value="True"
        )
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_edit_field", args=[en_state.pk])

        with patch("netbox_nso_plugin.adapter_client.put_intent"):
            # Flip to False — now differs from the device → pending apply (accepted).
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {"value": "false"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            en_state.refresh_from_db()
            self.assertEqual(en_state.status, "accepted")
            # Flip back to True — matches the device again → in_sync (nothing to apply).
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {"value": "true"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        en_state.refresh_from_db()
        self.assertEqual(en_state.status, "in_sync")
        self.assertIsNotNone(en_state.accepted_at)  # still owned by NetBox

    def test_unknown_attribute_rejected(self):
        """A state whose attribute is not description/enabled cannot be inline-edited."""
        odd = NSOInterfaceState.objects.create(
            interface=self.interface, attribute="mtu", status="imported", nso_value="1500"
        )
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_edit_field", args=[odd.pk])
        response = self.client.post(url, {"value": "9000"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)


class TestNSOApplyPreviewView(ViewTestBase):
    """Tests for NSOApplyPreviewView (what Apply would push)."""

    def test_preview_lists_pending_changes(self):
        import json

        from django.utils import timezone

        self.interface.description = "intended"
        self.interface.save(update_fields=["description"])
        # Ownership is status-based: an 'accepted' status is owned, so the preview lists it.
        # (accepted_at is stamped too by the real accept-flow, but is no longer what the
        # preview keys off.)
        content_bulk_update(
            self.iface_state,
            status="accepted",
            nso_value="on-device",
            accepted_at=timezone.now(),
        )

        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        self.assertFalse(data["auto_apply"])
        self.assertEqual(data["total"], 1)
        chg = data["changes"][0]
        self.assertEqual(chg["attribute"], "description")
        self.assertEqual(chg["device"], "on-device")
        self.assertEqual(chg["netbox"], "intended")

    def test_preview_empty_when_nothing_pending(self):
        import json

        # in_sync AND values match (device == NetBox's empty description) → genuinely nothing
        # to push. (The fixture's nso_value is "test desc"; clear it so the row is truly in sync
        # rather than an owned value-difference, which would correctly be pending.)
        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        self.assertEqual(data["total"], 0)

    def test_preview_excludes_imported_attribute_with_stale_accepted_at(self):
        """An attribute that is 'imported' (un-owned) must NOT be listed as pending, even if it
        carries a stale accepted_at from a past acceptance. Apply only pushes OWNED intent, so an
        imported row never reaches the adapter mirror and the dry-run shows no change — listing it
        made the modal claim an interface change Apply would never push (observed on a derived
        ae2.0 description that drifted back to imported)."""
        import json

        from django.utils import timezone

        self.interface.description = "derived-desc"  # NetBox value differs from the (empty) device
        self.interface.save(update_fields=["description"])
        content_bulk_update(
            self.iface_state,
            attribute="description",
            status="imported",
            nso_value="",
            accepted_at=timezone.now(),
        )

        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        self.assertEqual(data["changes"], [])  # imported (un-owned) → not pushed → not previewed
        self.assertEqual(data["total"], 0)

    def test_preview_forwards_outformat_to_adapter_and_echoes_it(self):
        """?outformat=cli threads to the adapter apply-diff (NSO's NED-uniform +/- tree
        diff for the preview's diff-u panel) and is echoed so the JS picks the renderer."""
        import json
        from unittest.mock import patch

        self.mgmt.adapter_device_id = 77
        self.mgmt.save()
        with patch(
            "netbox_nso_plugin.adapter_client.get_apply_diff",
            return_value={"outformat": "cli", "diffs": {"isis": "+ isis bfd"}},
        ) as gad:
            url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
            data = json.loads(self.client.get(url + "?outformat=cli").content)
        gad.assert_called_once_with(77, outformat="cli")
        self.assertEqual(data["outformat"], "cli")
        self.assertEqual(data["device_diff"], {"isis": "+ isis bfd"})

    def test_preview_invalid_outformat_falls_back_to_native(self):
        import json
        from unittest.mock import patch

        self.mgmt.adapter_device_id = 78
        self.mgmt.save()
        with patch("netbox_nso_plugin.adapter_client.get_apply_diff", return_value={"diffs": {}}) as gad:
            url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
            data = json.loads(self.client.get(url + "?outformat=bogus").content)
        gad.assert_called_once_with(78, outformat="native")
        self.assertEqual(data["outformat"], "native")

    def test_preview_reports_whether_the_dry_run_actually_ran(self):
        """An EMPTY diff and an UNAVAILABLE diff are both {} on the wire but mean opposite
        things. The panel reads meaning INTO emptiness — a staged row whose scope has no
        diff gets a "no device change" badge, and total==0 + empty diff lets Apply skip the
        confirm modal. When the dry-run THREW, both read the failure as reassurance. So the
        preview must say which case it is.
        """
        import json
        from unittest.mock import patch

        from netbox_nso_plugin.adapter_client import AdapterError

        self.mgmt.adapter_device_id = 79
        self.mgmt.save()
        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])

        with patch(
            "netbox_nso_plugin.adapter_client.get_apply_diff",
            side_effect=AdapterError("Adapter unreachable: nope", code="nso_unreachable"),
        ):
            failed = json.loads(self.client.get(url).content)
        self.assertEqual(failed["device_diff"], {})
        self.assertFalse(failed["diff_available"], "a dry-run that threw must not read as an empty diff")
        self.assertFalse(failed["nothing_pending"], "and it must never let Apply skip the confirm modal")

        with patch("netbox_nso_plugin.adapter_client.get_apply_diff", return_value={"diffs": {}}):
            clean = json.loads(self.client.get(url).content)
        self.assertEqual(clean["device_diff"], {})
        self.assertTrue(clean["diff_available"], "a dry-run that ran and found nothing is a real answer")

    def test_preview_isis_interface_detail_includes_bfd(self):
        """#77 transparency: a tri-state bfd intent MUST appear in 'properties pushed' —
        the dry-run diff showed bfd-enabled while the intent list stayed silent (operator
        caught it on the first live preview)."""
        import json

        from netbox_nso_plugin.models import NSOISISInterfaceState

        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        NSOISISInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            af="ipv4",
            process_tag="CORE",
            bfd_enabled=True,
            status="accepted",
        )
        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        rc = next(r for r in data["routing_changes"] if r["category"] == "IS-IS interface")
        self.assertIn("bfd on", rc["detail"])

    def test_preview_rows_carry_scope_key(self):
        """Each intent row must name its apply-diff SCOPE so the modal can badge rows
        whose scope produced no device delta ('no device change') — the operator saw
        example-comm listed as intent with no diff and rightly asked why (a row staged
        weeks ago can be already-satisfied on the device)."""
        import json

        from netbox_nso_plugin.models import NSOISISInterfaceState

        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        NSOISISInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            af="ipv4",
            process_tag="CORE",
            status="accepted",
        )
        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        rc = next(r for r in data["routing_changes"] if r["category"] == "IS-IS interface")
        self.assertEqual(rc["scope"], "isis")

    def test_preview_rows_carry_staleness(self):
        """A row staged long ago and never applied must SAY so (the example-comm case:
        accepted June 14, no apply ever ran, panels silently disagreed for a month).
        Rows expose staged_days + never_applied so the modal can badge them."""
        import json
        from datetime import timedelta

        from django.utils import timezone

        from netbox_nso_plugin.models import NSOISISInterfaceState

        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        NSOISISInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            af="ipv4",
            process_tag="CORE",
            status="accepted",
            accepted_at=timezone.now() - timedelta(days=26),
        )
        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        rc = next(r for r in data["routing_changes"] if r["category"] == "IS-IS interface")
        self.assertEqual(rc["staged_days"], 26)
        self.assertTrue(rc["never_applied"])

    def test_preview_ospf_interface_lists_pushed_properties(self):
        """An accepted OSPF interface overlay shows its pushed properties (area/cost/network-type)."""
        import json

        from netbox_nso_plugin.models import NSOOSPFInterfaceState

        # in_sync + matching value (empty == empty) → no interface change in the preview.
        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        NSOOSPFInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            process_id="1",
            area_id="0.0.0.0",
            cost=120,
            network_type="point-to-point",
            status="accepted",
        )

        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        rc = next(r for r in data["routing_changes"] if r["category"] == "OSPF interface")
        # Overlay holds the values directly (no netbox-routing object in this fixture).
        self.assertIn("area 0.0.0.0", rc["detail"])
        self.assertIn("cost 120", rc["detail"])
        self.assertIn("point-to-point", rc["detail"])

    def test_preview_itemises_non_interface_changes(self):
        """A pending overlay (e.g. VLAN) is listed with category+item+detail, not just counted."""
        import json

        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        # in_sync + matching value (empty == empty) → no interface change; only the VLAN is pending.
        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=2213, name="FW_uplink_cpms-01")
        NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, device_name="OLD", status="accepted")

        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["routing_changes"]), 1)
        rc = data["routing_changes"][0]
        self.assertEqual(rc["category"], "VLAN")
        self.assertEqual(rc["item"], "VLAN 2213")
        self.assertEqual(rc["detail"], "name FW_uplink_cpms-01")

    def test_preview_counts_deploying_interface_change(self):
        """A 'deploying' interface attr (apply pushed, awaiting device confirmation) must be
        counted. The tab badges deploying as 'pending apply', so the preview total must agree —
        otherwise openApply() sees total=0, silently skips the confirm modal and fires Apply."""
        import json

        from django.utils import timezone

        self.interface.description = "intended"
        self.interface.save(update_fields=["description"])
        content_bulk_update(
            self.iface_state,
            status="deploying",
            nso_value="on-device",
            accepted_at=timezone.now(),
        )

        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["changes"][0]["attribute"], "description")

    def test_preview_counts_deploying_routing_overlay(self):
        """A 'deploying' routing overlay (e.g. a route-policy row stuck awaiting device
        confirmation) must be counted by the preview, matching the tab's pending-apply badge —
        otherwise total=0 and the Apply modal is skipped (observed on rg03 route-policy rows)."""
        import json

        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=2299, name="stuck")
        NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="OLD",
            status="deploying",
            apply_attempt_id=uuid4(),
        )

        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["routing_changes"]), 1)
        self.assertEqual(data["routing_changes"][0]["status"], "deploying")

    def test_preview_adoption_only_apply_is_not_nothing_pending(self):
        """#107: accepting an imported (matching) row lands it in_sync — zero itemised
        preview rows — yet the first Apply still commits the FASTMAP service adoption,
        which the NSO dry-run shows. The preview must say nothing_pending=False so the
        confirm modal opens instead of auto-proceeding (the #11 redistribution case:
        routing_changes=[] total=0 while device_diff.bgp held the real adoption)."""
        import json
        from unittest.mock import patch

        from netbox_nso_plugin.models import NSORedistributionState

        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="bgp",
            source_protocol="connected",
            route_map="RM-CONN",
            status="in_sync",  # accepted-as-matching (brownfield adoption), never yet applied
        )
        self.mgmt.adapter_device_id = 79
        self.mgmt.save()
        with patch(
            "netbox_nso_plugin.adapter_client.get_apply_diff",
            return_value={"outformat": "cli", "diffs": {"bgp": "+ redistribute connected route-map RM-CONN"}},
        ):
            url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
            data = json.loads(self.client.get(url + "?outformat=cli").content)
        self.assertEqual(data["total"], 0)  # nothing itemised — the row reads as settled
        self.assertFalse(data["nothing_pending"])  # but the transaction is real → confirm

    def test_preview_nothing_pending_in_steady_state(self):
        """No pending rows AND an empty dry-run diff → genuinely nothing to commit, the
        Apply button may proceed without the confirm modal (unchanged fast path)."""
        import json
        from unittest.mock import patch

        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        self.mgmt.adapter_device_id = 80
        self.mgmt.save()
        with patch("netbox_nso_plugin.adapter_client.get_apply_diff", return_value={"diffs": {}}):
            url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
            data = json.loads(self.client.get(url).content)
        self.assertEqual(data["total"], 0)
        self.assertTrue(data["nothing_pending"])

    def test_preview_pending_rows_never_nothing_pending(self):
        """Itemised pending rows force the confirm modal even when the dry-run diff is
        unavailable (adapter down → device_diff={} is best-effort, not proof of no-op)."""
        import json

        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        content_bulk_update(self.iface_state, status="in_sync", nso_value="")
        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=2300, name="pending")
        NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, device_name="OLD", status="accepted")

        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        data = json.loads(self.client.get(url).content)
        self.assertEqual(data["total"], 1)
        self.assertFalse(data["nothing_pending"])


class TestRoutingStateAcceptView(ViewTestBase):
    """Per-row routing accepts (RoutingStateAcceptMixin families)."""

    def test_accept_stamps_accepted_at(self):
        """#107 adjacent: the mixin must stamp accepted_at like every other accept view —
        without it staged_days stays null for ALL routing families and the apply-preview's
        'staged Nd' staleness badge can never fire for them."""
        from netbox_nso_plugin.models import NSORedistributionState

        state = NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="bgp",
            source_protocol="static",
            status="changed",
        )
        url = reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", kwargs={"pk": state.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")  # differing value → pending apply
        self.assertIsNotNone(state.accepted_at)

    def test_accept_keeps_original_accepted_at_on_reaccept(self):
        """Re-accepting must not refresh accepted_at — staged_days measures how long the
        intent has been waiting since FIRST acceptance (mirrors the interface-edit signal)."""
        from datetime import timedelta

        from django.utils import timezone

        from netbox_nso_plugin.models import NSORedistributionState

        original = timezone.now() - timedelta(days=12)
        state = NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="bgp",
            source_protocol="static",
            status="changed",
            accepted_at=original,
        )
        url = reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", kwargs={"pk": state.pk})
        self.client.post(url)
        state.refresh_from_db()
        self.assertEqual(state.accepted_at, original)


class TestNSOBulkAcceptView(ViewTestBase):
    """Tests for NSOBulkAcceptView."""

    @patch("netbox_nso_plugin.signals._schedule_intent_push")
    def test_post_bulk_accept_redirects(self, mock_schedule):
        """POST bulk accept accepts all changed states and redirects."""
        # Ensure state is 'changed'
        content_bulk_update(self.iface_state, status="changed")
        mirror_update(self.mgmt, adapter_device_id=24)
        self.addCleanup(mirror_update, self.mgmt, adapter_device_id=None)

        url = reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=[self.device.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        mock_schedule.assert_called_once_with((self.device.pk, "interface"))

    def test_post_bulk_accept_nothing_to_accept(self):
        """POST bulk accept when no changed states redirects with info."""
        content_bulk_update(self.iface_state, status="in_sync")

        url = reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=[self.device.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

    @patch("netbox_nso_plugin.signals._schedule_intent_push")
    def test_post_bulk_accept_keeps_the_first_ownership_time(self, _mock_schedule):
        from django.utils import timezone

        original = timezone.now() - timedelta(days=12)
        content_bulk_update(self.iface_state, status="changed", accepted_at=original)

        response = self.client.post(reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=[self.device.pk]))

        self.assertEqual(response.status_code, 302)
        self.iface_state.refresh_from_db()
        self.assertEqual(self.iface_state.status, "accepted")
        self.assertEqual(self.iface_state.accepted_at, original)


# ── Device NSO Tab ────────────────────────────────────────────────────────────────


class TestDeviceNSOTabView(ViewTestBase):
    """Tests for DeviceNSOTabView (device NSO tab)."""

    def test_job_activity_uses_active_and_polled_status_wording(self):
        """The tab does not describe every queued or active job as running."""
        response = self.client.get(reverse("dcim:device_nso", kwargs={"pk": self.device.pk}))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("</strong> active", body)
        self.assertIn("label + ' ' + job.status + '…", body)
        self.assertIn("label + ' active across ' + expected", body)
        self.assertIn("label + ' active across ' + data.generations.length", body)
        self.assertIn("label + ' queued… (Job #'", body)
        self.assertNotIn("already running", body)

    def test_device_nso_tab_no_mgmt(self):
        """Device without NSODeviceManagement returns 200 with empty context."""
        # Create a second device without mgmt
        device2 = Device.objects.create(
            name="view-router-02",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        url = reverse("dcim:device_nso", kwargs={"pk": device2.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])  # may redirect if no tab
        device2.delete()

    def test_degraded_deletion_read_failure_does_not_break_the_tab(self):
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})

        with patch("netbox_nso_plugin.drain.degraded_deletions", side_effect=ValueError("bad timestamp")):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["degraded_deletions"], [])

    def test_oob_probe_timeout_is_not_rendered_as_unreachable(self):
        """The adapter can still connect after its short health window; preserve that as a
        timeout with the target/detail instead of claiming the active OOB is unreachable."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        mgmt.save(update_fields=["adapter_device_id"])
        stack, mocks = self._patch_all_getters()
        mocks["get_device"].return_value = {
            "id": 15,
            "last_sync_at": None,
            "last_sync_status": "succeeded",
            "failover": {
                "active_address": "oob",
                "oob_ip": "192.0.2.5",
                "primary_ip": "10.0.0.1",
                "last_probe_result": "timeout",
                "last_probe_target": "oob",
                "last_probe_detail": "cold connect exceeded probe window",
                "last_probe_at": "2026-07-18T09:23:48Z",
                "oob_healthy": False,
                "oob_health_result": "timeout",
                "oob_health_detail": "cold connect exceeded probe window",
                "oob_health_checked_at": "2026-07-18T09:23:48Z",
                "last_switch_at": "2026-07-15T13:00:46Z",
                "manual_override": False,
            },
        }

        with stack:
            response = self.client.get(reverse("dcim:device_nso", kwargs={"pk": self.device.pk}))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Timed out", body)
        self.assertIn("OOB target", body)
        self.assertIn("cold connect exceeded probe window", body)
        self.assertNotIn(">Unreachable<", body)

    #: The two timestamp shapes the adapter is allowed to emit on the failover block.
    _FAILOVER_SHAPES = (
        ("2026-07-18T09:23:48Z", datetime(2026, 7, 18, 9, 23, 48, tzinfo=UTC)),
        ("2026-07-18T09:23:48.123456Z", datetime(2026, 7, 18, 9, 23, 48, 123456, tzinfo=UTC)),
    )
    _FAILOVER_TS_KEYS = ("last_probe_at", "last_switch_at", "oob_health_checked_at")

    def _render_tab_with_failover_ts(self, wire):
        """Render the real NSO tab with *wire* on every failover timestamp; return its context."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        mgmt.save(update_fields=["adapter_device_id"])
        stack, mocks = self._patch_all_getters()
        mocks["get_device"].return_value = {
            "id": 15,
            "last_sync_at": None,
            "last_sync_status": "succeeded",
            "failover": {
                "active_address": "primary",
                "oob_ip": "192.0.2.5",
                "primary_ip": "10.0.0.1",
                "oob_healthy": True,
                "manual_override": False,
                **dict.fromkeys(self._FAILOVER_TS_KEYS, wire),
            },
        }
        with stack:
            return self.client.get(reverse("dcim:device_nso", kwargs={"pk": self.device.pk}))

    def test_failover_timestamps_parse_as_aware_utc(self):
        """The tab parses the failover block's timestamps for the template's ``|date`` filter.

        A host-local tzinfo carries the right instant only while the host runs UTC, so assert
        the zone, not just equality.
        """
        for wire, expected in self._FAILOVER_SHAPES:
            with self.subTest(wire=wire):
                response = self._render_tab_with_failover_ts(wire)
                self.assertEqual(response.status_code, 200)
                failover = response.context["failover"]
                for key in self._FAILOVER_TS_KEYS:
                    self.assertEqual(failover[key], expected, key)
                    self.assertEqual(failover[key].microsecond, expected.microsecond, key)
                    self.assertEqual(str(failover[key].tzinfo), "UTC", f"{key} must carry UTC, not a host-local zone")

    def test_unparseable_failover_timestamp_does_not_break_the_tab(self):
        """The tab's ``try`` only catches AdapterError — a raising parser 500s the whole page."""
        response = self._render_tab_with_failover_ts("2026-07-18T09:23:48+00:00Z")
        self.assertEqual(response.status_code, 200)
        failover = response.context["failover"]
        for key in self._FAILOVER_TS_KEYS:
            self.assertIsNone(failover[key], key)

    def test_non_string_failover_timestamp_does_not_break_the_tab(self):
        """The key guard is truthiness only, so a number or an object reaches the parser."""
        for wire in (1717236000, {"at": "2026-07-18T09:23:48Z"}):
            with self.subTest(wire=wire):
                response = self._render_tab_with_failover_ts(wire)
                self.assertEqual(response.status_code, 200)
                failover = response.context["failover"]
                for key in self._FAILOVER_TS_KEYS:
                    self.assertIsNone(failover[key], key)

    def _patch_all_getters(self):
        """Patch every adapter getter the tab view may call; return the patch context
        and a dict of the mocks keyed by attribute name."""
        from contextlib import ExitStack

        names = [
            "get_device",
            "get_interfaces",
            "get_state",
            "get_snmp_config",
            "get_static_routes",
            "get_isis_interfaces",
            "get_route_policy",
            "get_ospf",
            "get_redistribution",
            "get_bgp_config",
        ]
        stack = ExitStack()
        mocks = {}
        for name in names:
            m = stack.enter_context(patch(f"netbox_nso_plugin.adapter_client.{name}"))
            m.return_value = {} if name in ("get_device", "get_state", "get_snmp_config") else []
            mocks[name] = m
        mocks["get_device"].return_value = {"id": 15, "last_sync_at": None, "last_sync_status": ""}
        mocks["get_isis_interfaces"].return_value = {"interfaces": [], "processes": []}
        mocks["get_bgp_config"].return_value = {"routers": []}
        # Patch the reconcilers/upsert too: this test verifies the view's *gating*
        # decision (which adapter getter runs for a given scope), not reconciler
        # internals, so stub them out to keep the test isolated and DB-free.
        for target, ret in (
            ("netbox_nso_plugin.template_content._upsert_interface_states", {}),
            ("netbox_nso_plugin.template_content._reconcile_snmp_config", {}),
            ("netbox_nso_plugin.template_content._reconcile_static_routes", []),
            ("netbox_nso_plugin.template_content._reconcile_isis_interfaces", []),
            ("netbox_nso_plugin.template_content._reconcile_isis_process", []),
            ("netbox_nso_plugin.template_content._reconcile_ospf", {"instances": [], "interfaces": []}),
            ("netbox_nso_plugin.route_policy_reconciler.reconcile_route_policy", []),
            ("netbox_nso_plugin.redistribution_reconciler.reconcile_redistribution", []),
            ("netbox_nso_plugin.bgp_reconciler._reconcile_bgp_config", []),
        ):
            stack.enter_context(patch(target, return_value=ret))
        return stack, mocks

    def _render_tab_with_scopes(self, **scopes):
        """Set the given scope flags on the fixture mgmt, render the tab, return the mocks."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        for field in (
            "manage_interfaces",
            "manage_routing",
            "manage_static",
            "manage_isis",
            "manage_ospf",
            "manage_bgp",
            "manage_route_policy",
            "manage_redistribution",
            "manage_snmp",
        ):
            setattr(mgmt, field, scopes.get(field, False))
        mgmt.save()

        stack, mocks = self._patch_all_getters()
        with stack:
            url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
            self.client.get(url)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        return mocks

    def test_tab_all_scopes_off_skips_all_data_fetches(self):
        """With every scope disabled, no scoped adapter getter is called (only get_device)."""
        mocks = self._render_tab_with_scopes()
        mocks["get_device"].assert_called_once()
        for name in (
            "get_interfaces",
            "get_state",
            "get_snmp_config",
            "get_static_routes",
            "get_isis_interfaces",
            "get_route_policy",
            "get_ospf",
            "get_redistribution",
            "get_bgp_config",
        ):
            mocks[name].assert_not_called()

    def test_tab_renders_partial_split_brain_banner(self):
        """Partial drift (adapter holds more rows than NetBox owns) renders the banner
        with both counts + the partial badge + the re-sync form."""
        from netbox_nso_plugin.models import NSOInterfaceIPState

        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        mgmt.save(update_fields=["adapter_device_id"])
        for i in (1, 2):
            NSOInterfaceIPState.objects.create(
                interface=self.interface, address=f"10.9.9.{i}/32", vrf="", family="ipv4", status="in_sync"
            )

        stack, _mocks = self._patch_all_getters()
        with stack:
            with patch(
                "netbox_nso_plugin.adapter_client.get_intent_summary",
                return_value={"scopes": {"interface_ip_intent": {"count": 3, "applied": 0, "failed": 0}}},
            ):
                url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
                response = self.client.get(url)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Adapter holds intent NetBox no longer owns", html)
        self.assertIn("NetBox owns 2", html)
        self.assertIn(">partial<", html)
        self.assertIn("Re-sync adapter intent", html)

    def test_tab_renders_partial_last_sync_and_caches_degraded_surfaces(self):
        """A 'partial' last-sync from the adapter renders a warning badge naming the
        stale routing surfaces, and caches degraded_surfaces on the mgmt row so the
        list/dashboard can show it without a fresh poll.

        (This 'partial' is the device-level last_sync_status — distinct from the
        intent split-brain 'partial' badge covered above.)
        """
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        mgmt.last_sync_status = ""
        mgmt.degraded_surfaces = None
        mgmt.save()

        stack, mocks = self._patch_all_getters()
        # The identity fields are load-bearing: the tab mirrors this payload onto the row, and
        # that mirror now fails closed unless the payload really is this device's.
        mocks["get_device"].return_value = {
            "id": 15,
            "nso_instance": self.nso_instance.adapter_instance_id,
            "nso_device_name": mgmt.nso_device_name,
            "netbox_device_id": self.device.pk,
            "last_sync_at": "2025-06-01T10:00:00Z",
            "last_sync_status": "partial",
            "degraded_surfaces": ["bgp", "ospf"],
        }
        with stack:
            url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
            response = self.client.get(url)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # the sync-status row shows the partial badge and names the stale surfaces
        self.assertIn("Stale (NSO read failed)", html)
        self.assertIn(">bgp</span>", html)
        self.assertIn(">ospf</span>", html)
        # ...and the surfaces are cached so the list/dashboard need no fresh poll
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "partial")
        self.assertEqual(mgmt.degraded_surfaces, ["bgp", "ospf"])

    def test_tab_renders_honest_freshness_labels(self):
        """C2/C3: the overlay-only button reads 'Refresh overlays' (not the misleading 'Refresh
        from NSO'), the genuine device-reread 'Sync Now' remains, and the device-sync banner is
        labelled 'Last device sync' so it is not mistaken for the tab-overlay freshness."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        mgmt.save(update_fields=["adapter_device_id"])

        stack, mocks = self._patch_all_getters()
        mocks["get_device"].return_value = {
            "id": 15,
            "last_sync_at": "2025-06-01T10:00:00Z",
            "last_sync_status": "succeeded",
        }
        with stack:
            url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
            response = self.client.get(url)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Refresh overlays", html)
        self.assertNotIn("Refresh from NSO", html)
        self.assertIn("Last device sync", html)
        self.assertIn("Sync Now", html)  # the genuine device-reread action is unchanged
        # S5a C: the middle tier of the three-button ladder — comprehensive CDB-only read.
        self.assertIn("Sync from NSO", html)
        self.assertIn("sync-from-nso", html)

    def test_tab_render_is_counts_only_no_scoped_fetches(self):
        """The tab RENDER fetches no per-scope adapter data — only get_device for the
        banner. Counts come from persisted NSO*State; rows (and their adapter fetches)
        are deferred to the lazy category endpoint."""
        mocks = self._render_tab_with_scopes(manage_interfaces=True, manage_routing=True, manage_bgp=True)
        mocks["get_device"].assert_called_once()
        for name in (
            "get_interfaces",
            "get_state",
            "get_snmp_config",
            "get_static_routes",
            "get_isis_interfaces",
            "get_route_policy",
            "get_ospf",
            "get_redistribution",
            "get_bgp_config",
        ):
            mocks[name].assert_not_called()

    def _load_category(self, key, **scopes):
        """Set scopes, GET the lazy category endpoint for *key*, return the getter mocks."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        for field in (
            "manage_interfaces",
            "manage_routing",
            "manage_static",
            "manage_isis",
            "manage_ospf",
            "manage_bgp",
            "manage_route_policy",
            "manage_redistribution",
            "manage_snmp",
        ):
            setattr(mgmt, field, scopes.get(field, False))
        mgmt.save()

        stack, mocks = self._patch_all_getters()
        with stack:
            url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": key})
            self.client.get(url)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        return mocks

    def test_lazy_category_interfaces_is_read_only_no_adapter_fetch(self):
        """The interfaces category loads paginated from persisted state — NO adapter call."""
        mocks = self._load_category("interfaces", manage_interfaces=True)
        mocks["get_interfaces"].assert_not_called()
        mocks["get_state"].assert_not_called()

    def test_interfaces_page_paginates_filters_and_states(self):
        """The per-attribute interfaces view paginates, name-filters, and state-filters."""
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState

        # 120 interfaces, each with a description state. Classification is value-aware:
        # all are in sync (NetBox description == device value) except #0, whose values
        # differ and which nobody owns → drift.
        for n in range(120):
            iface = Interface.objects.create(
                device=self.device,
                name=f"et-0/0/{n}",
                type="other",
                description="nb-0" if n == 0 else f"v-{n}",
            )
            NSOInterfaceState.objects.create(
                interface=iface,
                attribute="description",
                status="changed" if n == 0 else "imported",
                nso_value="dev-0" if n == 0 else f"v-{n}",
            )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "interfaces"}
        )

        r1 = self.client.get(url)
        self.assertEqual(r1.status_code, 200)
        body1 = r1.content.decode()
        self.assertIn("nso-if-filter", body1)  # filter box
        self.assertIn("nso-if-state", body1)  # state chips
        self.assertIn("et-0/0/0", body1)
        self.assertIn("dev-0", body1)  # device (NSO) value column
        self.assertIn("nb-0", body1)  # NetBox value column
        self.assertNotIn("et-0/0/60", body1)  # 50/page → page 1 only
        # No leaked Django comments (illegal multi-line {# #} renders as text).
        self.assertNotIn("{#", body1)
        self.assertNotIn("#}", body1)

        # page 2 shows later interfaces
        self.assertIn("et-0/0/60", self.client.get(url, {"page": 2}).content.decode())

        # name filter narrows
        bodyf = self.client.get(url, {"q": "et-0/0/119"}).content.decode()
        self.assertIn("et-0/0/119", bodyf)
        self.assertNotIn("et-0/0/0<", bodyf)

        # state=drift shows only the one drifted attribute
        bodyd = self.client.get(url, {"state": "drift"}).content.decode()
        self.assertIn("et-0/0/0", bodyd)
        self.assertNotIn("et-0/0/1<", bodyd)

        Interface.objects.filter(device=self.device, name__startswith="et-0/0/").delete()

    def test_merged_interface_category_pivots_overlays_into_one_row(self):
        """The consolidated 'interface' card renders one row per interface with every
        per-interface scalar overlay (enabled/description/MTU/IP/switchport) as a column,
        plus the column-select chips. adapter_device_id=None → renders from persisted
        state with no adapter fetch."""
        from dcim.models import Interface

        from netbox_nso_plugin.models import (
            NSOInterfaceIPState,
            NSOInterfaceMtuState,
            NSOInterfaceState,
            NSOSwitchportState,
        )

        iface = Interface.objects.create(device=self.device, name="Gi0/1", type="other", description="nb")
        NSOInterfaceState.objects.create(interface=iface, attribute="enabled", status="imported", nso_value="true")
        NSOInterfaceState.objects.create(
            interface=iface, attribute="description", status="changed", nso_value="uplink to core"
        )
        NSOInterfaceMtuState.objects.create(
            management=self.mgmt, interface=iface, l2_mtu=9216, ip_mtu=9000, status="imported"
        )
        NSOInterfaceIPState.objects.create(interface=iface, address="10.0.0.1/31", family="ipv4", status="imported")
        NSOSwitchportState.objects.create(management=self.mgmt, interface=iface, mode="access", status="imported")

        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "interface"}
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("nso-grid-cols", body)  # column-select chips (shared nso-grid.js contract)
        self.assertIn('id="nso-ifg-data"', body)  # embedded grid payload (json_script)
        self.assertIn("NSOGridInterface", body)  # fragment mounts the static column module
        self.assertIn("Gi0/1", body)
        self.assertIn("uplink to core", body)  # description device value
        self.assertIn("9216", body)  # L2 MTU
        self.assertIn("10.0.0.1/31", body)  # IP address
        # No leaked Django comments (illegal multi-line {# #} would render as text).
        self.assertNotIn("{#", body)
        self.assertNotIn("#}", body)

        iface.delete()

    def test_merged_interface_category_json_serves_grid_rows(self):
        """?format=json returns the full (unpaginated) per-interface matrix for the
        client-side grid: per-cell value + status + kind + accept/edit URLs, a row-level
        rollup state (worst cell wins), and the quick-filter counts."""
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import (
            NSOInterfaceIPState,
            NSOInterfaceMtuState,
            NSOInterfaceState,
            NSOSwitchportState,
        )

        iface = Interface.objects.create(device=self.device, name="Gi0/1", type="other", description="nb")
        NSOInterfaceState.objects.create(interface=iface, attribute="enabled", status="imported", nso_value="true")
        st_desc = NSOInterfaceState.objects.create(
            interface=iface, attribute="description", status="changed", nso_value="uplink to core"
        )
        NSOInterfaceMtuState.objects.create(
            management=self.mgmt, interface=iface, l2_mtu=9216, ip_mtu=9000, status="imported"
        )
        NSOInterfaceIPState.objects.create(interface=iface, address="10.0.0.1/31", family="ipv4", status="imported")
        IPAddress.objects.create(address="198.18.30.0/31")
        NSOInterfaceIPState.objects.create(
            interface=iface,
            address="198.18.30.0/31",
            family="ipv4",
            status="changed",
        )
        NSOSwitchportState.objects.create(management=self.mgmt, interface=iface, mode="access", status="imported")

        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "interface"}
        )
        r = self.client.get(url, {"format": "json"})
        self.assertEqual(r.status_code, 200)
        data = r.json()

        # Counts cover every interface (the shared fixture creates others) and always
        # agree with the row list itself; our drifting row must be counted.
        self.assertEqual(data["counts"]["all"], len(data["rows"]))
        self.assertGreaterEqual(data["counts"]["drift"], 1)
        row = next(r for r in data["rows"] if r["iface"]["name"] == "Gi0/1")
        self.assertTrue(row["iface"]["url"])

        # enabled: NetBox True vs device "true" — value-aware in sync (not owned, no accept hidden).
        self.assertEqual(row["enabled"]["kind"], "in_sync")
        # description: NetBox "nb" vs device "uplink to core", status=changed, unowned → drift,
        # with both values shipped: the cell displays + the editor prefills the NetBox value
        # (the intent an edit writes); the device mirror renders only as the "device:" note.
        self.assertEqual(row["description"]["kind"], "drift")
        self.assertEqual(row["description"]["value"], "uplink to core")
        self.assertEqual(row["description"]["netbox_value"], "nb")
        self.assertIn(f"/{st_desc.pk}/", row["description"]["accept_url"])
        self.assertIn(f"/{st_desc.pk}/", row["description"]["edit_url"])

        self.assertEqual(row["mtu"]["l2"], 9216)
        self.assertEqual(row["mtu"]["ip"], 9000)
        self.assertEqual(row["ips"][0]["address"], "10.0.0.1/31")
        self.assertTrue(row["ips"][0]["device_present"])
        self.assertEqual(
            row["ips"][0]["netbox"],
            {"present": False, "address": None, "vrf": "", "assignment": "absent"},
        )
        stale_ip = next(ip for ip in row["ips"] if ip["address"] == "198.18.30.0/31")
        self.assertFalse(stale_ip["device_present"])
        self.assertEqual(
            stale_ip["netbox"],
            {
                "present": True,
                "address": "198.18.30.0/31",
                "vrf": "",
                "assignment": "unassigned",
            },
        )
        self.assertEqual(row["switchport"]["mode"], "access")
        self.assertEqual(
            row["switchport"]["netbox"],
            {"mode": "", "untagged": None, "tagged": []},
        )
        # Row rollup: the drifting description outranks the in-sync cells.
        self.assertEqual(row["state"], "drift")

        iface.delete()

    def test_merged_interface_grid_links_native_ip_and_surfaces_cable_peer(self):
        """A cabled interface exposes its cable/peer in the compact row payload, and
        an observed address links to its native IPAddress while offering pair editing.

        The peer state lives on the far-end device: the device-local overlay queryset
        alone is therefore insufficient to build the optional two-ended editor.
        """
        from dcim.models import Cable, CableTermination, Device, Interface
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState

        local = Interface.objects.create(device=self.device, name="Gi0/10", type="other")
        peer_device = Device.objects.create(
            name="view-router-02",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        peer = Interface.objects.create(device=peer_device, name="Gi0/20", type="other")
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=local)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=peer)

        native = IPAddress.objects.create(address="198.18.10.0/31", assigned_object=local)
        IPAddress.objects.create(address="198.18.10.1/31", assigned_object=peer)
        local_state = NSOInterfaceIPState.objects.create(
            interface=local,
            address="198.18.10.0/31",
            family="ipv4",
            status="imported",
        )
        peer_state = NSOInterfaceIPState.objects.create(
            interface=peer,
            address="198.18.10.1/31",
            family="ipv4",
            status="imported",
        )

        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "interface"}
        )
        data = self.client.get(url, {"format": "json"}).json()
        row = next(item for item in data["rows"] if item["iface"]["name"] == local.name)

        self.assertEqual(row["link"]["cable"]["url"], cable.get_absolute_url())
        self.assertEqual(row["link"]["peer"]["name"], peer.name)
        self.assertEqual(row["link"]["peer"]["device"], peer_device.name)
        ip = row["ips"][0]
        self.assertEqual(ip["url"], native.get_absolute_url())
        self.assertTrue(ip["device_present"])
        self.assertEqual(
            ip["netbox"],
            {
                "present": True,
                "address": "198.18.10.0/31",
                "vrf": "",
                "assignment": local.name,
            },
        )
        self.assertIn(f"/{local_state.pk}/", ip["edit_url"])
        self.assertEqual(ip["peer"]["pk"], peer_state.pk)
        self.assertEqual(ip["peer"]["address"], "198.18.10.1/31")

        cable.delete()
        peer_device.delete()
        local.delete()

    def test_quick_filter_counts_agree_with_the_row_state_they_filter_on(self):
        """The grid collapses each interface's cells to ONE worst-first row state, and every
        quick-filter pill matches on exactly that value. The chip counts were still computed
        by set-membership over the raw cell kinds, so an interface that is BOTH drifted and
        apply_failed collapses to apply_failed — the Drift filter (state == 'drift') hides it,
        yet the Drift chip counted it. The chip promised a row its own filter would not show.
        """
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState, NSOInterfaceState

        iface = Interface.objects.create(device=self.device, name="Gi0/9", type="other", description="nb")
        # cell 1 → apply_failed. Only the value-aware attribute cells can produce that kind
        # (interface_row_state); display_state folds apply_failed into "pending".
        NSOInterfaceState.objects.create(
            interface=iface, attribute="description", status="apply_failed", nso_value="device-side"
        )
        # cell 2 → drift (unowned differ). apply_failed outranks drift in _KIND_SEVERITY,
        # so the ROW collapses to apply_failed while the raw kind set still holds "drift".
        NSOInterfaceMtuState.objects.create(management=self.mgmt, interface=iface, l2_mtu=9216, status="changed")

        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "interface"}
        )
        data = self.client.get(url, {"format": "json"}).json()

        row = next(r for r in data["rows"] if r["iface"]["name"] == "Gi0/9")
        self.assertEqual(row["state"], "apply_failed", "the worst cell wins the row state")

        # What each pill would actually show, by the grid's own filter predicates.
        shown_drift = [r for r in data["rows"] if r["state"] == "drift"]
        shown_pending = [r for r in data["rows"] if r["state"] in ("pending", "apply_failed")]
        self.assertEqual(data["counts"]["drift"], len(shown_drift))
        self.assertEqual(data["counts"]["pending"], len(shown_pending))
        self.assertNotIn(row, shown_drift)

        iface.delete()

    def test_paged_category_reads_persisted_paginated_and_searchable(self):
        """Single-table categories render paginated from last-synced state with NO
        adapter call on plain expand; ?page navigates, ?q filters server-side.
        (vlan is the representative — route_policy, the previous one, is a grid now.)"""
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        for n in range(60):
            vlan = VLAN.objects.create(vid=100 + n, name=("MATCHME" if n == 0 else f"VL{n:02d}"))
            NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, status="imported")
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "vlan"})

        # Plain expand reads persisted state — no adapter round-trip — and paginates.
        with patch("netbox_nso_plugin.adapter_client.get_vlan_database") as getter:
            r = self.client.get(url)
        getter.assert_not_called()
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("nso-cat-pager", body)
        self.assertIn("Page 1 of 2", body)
        self.assertIn("VL01", body)
        self.assertNotIn("VL55", body)  # 50/page → page 2
        self.assertNotIn("{#", body)
        self.assertNotIn("#}", body)

        # ?page navigates; ?q filters server-side.
        self.assertIn("VL55", self.client.get(url, {"page": 2}).content.decode())
        bodyq = self.client.get(url, {"q": "MATCHME"}).content.decode()
        self.assertIn("MATCHME", bodyq)
        self.assertNotIn("VL01", bodyq)

        NSOVLANState.objects.filter(management=self.mgmt).delete()

    def test_vlan_category_renders_compact_inline_name_editor(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOVLANState

        group = VLANGroup.objects.create(name="Compact VLANs", slug="compact-vlans")
        vlan = VLAN.objects.create(group=group, vid=120, name="CUSTOMER-A")
        state = NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="DEVICE-A",
            status="changed",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "vlan"},
        )

        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-pe-fields="name:text:Name"')
        edit_url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit",
            kwargs={"key": "vlan_name", "pk": state.pk},
        )
        self.assertContains(response, edit_url)
        self.assertContains(response, "VID 120")
        self.assertContains(response, "Compact VLANs")
        self.assertNotContains(response, "Name (NetBox)")
        self.assertNotContains(response, "<th>Device</th>", html=True)

    def test_svi_category_renders_compact_inline_vrf_editor(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOSVIState

        group = VLANGroup.objects.create(name="Compact SVI VLANs", slug="compact-svi-vlans")
        vlan = VLAN.objects.create(group=group, vid=220, name="CUSTOMER-A")
        interface = Interface.objects.create(device=self.device, name="Vlan220", type="virtual")
        state = NSOSVIState.objects.create(
            management=self.mgmt,
            interface=interface,
            vlan=vlan,
            svi_type="svi",
            vrf="CUSTOMER",
            status="changed",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "svi"},
        )

        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-pe-fields="vrf:text:VRF"')
        edit_url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit",
            kwargs={"key": "svi", "pk": state.pk},
        )
        self.assertContains(response, edit_url)
        self.assertContains(response, "Vlan220")
        self.assertContains(response, "VID 220")
        self.assertNotContains(response, "<th>Type</th>", html=True)
        self.assertNotContains(response, "<th>VLAN</th>", html=True)

    def test_subinterface_category_renders_compact_inline_l3_editor(self):
        from netbox_nso_plugin.models import NSOSubinterfaceState

        parent = Interface.objects.create(device=self.device, name="GigabitEthernet0/1", type="1000base-t")
        interface = Interface.objects.create(
            device=self.device,
            name="GigabitEthernet0/1.220",
            type="virtual",
            parent=parent,
        )
        state = NSOSubinterfaceState.objects.create(
            management=self.mgmt,
            interface=interface,
            parent_interface=parent,
            dot1q_vlan=220,
            vrf="CUSTOMER",
            status="changed",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "subinterface"},
        )

        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-pe-fields="dot1q_vlan:number:dot1q VLAN,vrf:text:VRF"')
        edit_url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit",
            kwargs={"key": "subinterface", "pk": state.pk},
        )
        self.assertContains(response, edit_url)
        self.assertContains(response, "GigabitEthernet0/1.220")
        self.assertContains(response, "dot1q 220")
        self.assertNotContains(response, "<th>Parent</th>", html=True)
        self.assertNotContains(response, "<th>dot1q VLAN</th>", html=True)

    def test_interfaces_page_classification_is_value_aware(self):
        """Display follows NetBox-vs-device values + status-based ownership.

        - An OWNED status (accepted) whose NetBox value differs from the device → pending.
        - A value that matches reads "in sync" even if the status is a DIFFER status.
        - The device-27 ae2.0 case: an UN-owned status ('imported'/'unknown') with a value
          that differs and a STALE accepted_at → drift (not pending), because Apply pushes
          by status and would never push it.
        """
        from dcim.models import Interface
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState

        # Owned (accepted) + NetBox has a description the device lacks → pending.
        owned_pending = Interface.objects.create(
            device=self.device, name="ae2.0", type="virtual", description="Core Link"
        )
        NSOInterfaceState.objects.create(
            interface=owned_pending, attribute="description", status="accepted", nso_value=""
        )
        # Status says "changed" (DIFFER) but the values actually match → in sync.
        matched = Interface.objects.create(device=self.device, name="ae3.0", type="virtual", description="same")
        NSOInterfaceState.objects.create(interface=matched, attribute="description", status="changed", nso_value="same")
        # Un-owned status with a STALE accepted_at + differing value → drift, NOT pending.
        stale = Interface.objects.create(device=self.device, name="ae4.0", type="virtual", description="Core Link")
        NSOInterfaceState.objects.create(
            interface=stale, attribute="description", status="imported", nso_value="", accepted_at=timezone.now()
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "interfaces"}
        )

        # ae2.0 (owned, differs) surfaces under pending; ae3.0 (matches) and ae4.0 (un-owned) do not.
        pending = self.client.get(url, {"state": "pending"}).content.decode()
        self.assertIn("ae2.0", pending)
        self.assertNotIn("ae3.0", pending)
        self.assertNotIn("ae4.0", pending)

        # ae3.0 (values match) surfaces under in_sync; ae2.0/ae4.0 (differ) do not.
        in_sync = self.client.get(url, {"state": "in_sync"}).content.decode()
        self.assertIn("ae3.0", in_sync)
        self.assertNotIn("ae2.0", in_sync)
        self.assertNotIn("ae4.0", in_sync)

        # ae4.0 (un-owned, differs, stale accepted_at) surfaces under drift; ae2.0 does not.
        drift = self.client.get(url, {"state": "drift"}).content.decode()
        self.assertIn("ae4.0", drift)
        self.assertNotIn("ae2.0", drift)

        Interface.objects.filter(device=self.device, name__in=["ae2.0", "ae3.0", "ae4.0"]).delete()

    def test_interfaces_page_apply_failed_renders_distinctly(self):
        """A failed apply shows 'apply failed' + a Retry button, not plain 'pending apply'."""
        from dcim.models import Interface
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState

        iface = Interface.objects.create(device=self.device, name="ae9.0", type="virtual", description="Wants This")
        NSOInterfaceState.objects.create(
            interface=iface,
            attribute="description",
            status="apply_failed",
            nso_value="",
            accepted_at=timezone.now(),
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "interfaces"}
        )
        body = self.client.get(url, {"q": "ae9.0"}).content.decode()
        self.assertIn("apply failed", body)
        self.assertIn("Retry apply", body)
        # And it lives under the pending filter (needs operator action), not in_sync.
        # Key off row-only strings: the query term itself echoes into the filter box.
        self.assertIn("Retry apply", self.client.get(url, {"q": "ae9.0", "state": "pending"}).content.decode())
        in_sync = self.client.get(url, {"q": "ae9.0", "state": "in_sync"}).content.decode()
        self.assertNotIn("apply failed", in_sync)
        self.assertNotIn("Retry apply", in_sync)

        Interface.objects.filter(device=self.device, name="ae9.0").delete()

    def test_tab_routing_master_off_skips_protocols(self):
        """A protocol flag without the routing master does not trigger its fetch."""
        mocks = self._render_tab_with_scopes(manage_isis=True, manage_bgp=True)
        mocks["get_isis_interfaces"].assert_not_called()
        mocks["get_bgp_config"].assert_not_called()

    def test_lazy_category_bgp_fetches_bgp_only(self):
        """Expanding the BGP category fetches BGP but no other routing protocol."""
        mocks = self._load_category("bgp", manage_routing=True, manage_bgp=True)
        mocks["get_bgp_config"].assert_called_once()
        for name in (
            "get_interfaces",
            "get_static_routes",
            "get_isis_interfaces",
            "get_route_policy",
            "get_ospf",
            "get_redistribution",
        ):
            mocks[name].assert_not_called()

    def test_accept_bgp_peer_template_marks_owned(self):
        """POST to the peer-group template accept URL takes ownership (status + accepted_at)."""
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        state = NSOBGPPeerTemplateState.objects.create(
            management=self.mgmt, template_name="RR-CLIENTS", remote_as_str="65000", status="changed"
        )
        url = reverse("plugins:netbox_nso_plugin:routing_accept_bgp_peer_template", kwargs={"pk": state.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")  # differing value → pending (no apply path)
        self.assertIsNotNone(state.accepted_at)

    def test_bgp_count_folds_in_peer_templates(self):
        """The BGP headline count includes peer-group templates (with a 'templates' sub-count)."""
        from netbox_nso_plugin.models import NSOBGPPeerState, NSOBGPPeerTemplateState
        from netbox_nso_plugin.summary import _category_counts

        NSOBGPPeerState.objects.create(
            management=self.mgmt, asn_str="65000", peer_address_str="10.0.0.2", status="imported"
        )
        NSOBGPPeerTemplateState.objects.create(management=self.mgmt, template_name="RR", status="changed")
        counts = _category_counts("bgp", self.device, self.mgmt)
        self.assertEqual(counts["total"], 2)  # 1 peer + 1 template
        self.assertEqual(counts["templates"], 1)
        self.assertEqual(counts["drift"], 1)  # the un-owned 'changed' template

    def test_category_counts_endpoint_returns_live_counts(self):
        """The category-counts JSON endpoint feeds the post-Sync header-badge refresh."""
        import json

        from django.utils import timezone

        self.mgmt.manage_interfaces = True
        self.mgmt.manage_description = True
        self.mgmt.save(update_fields=["manage_interfaces", "manage_description"])
        # A pending (owned) interface attr → counts.interfaces.pending == 1.
        iface = Interface.objects.create(device=self.device, name="et-9/9/9", type="other", description="nb")
        NSOInterfaceState.objects.create(
            interface=iface,
            attribute="description",
            status="accepted",
            nso_value="dev",
            accepted_at=timezone.now(),
        )
        url = reverse("plugins:netbox_nso_plugin:device_nso_category_counts", kwargs={"device_pk": self.device.pk})
        data = json.loads(self.client.get(url).content)
        # The per-interface scalar overlays render as one merged "interface" card.
        self.assertIn("interface", data["categories"])
        self.assertEqual(data["categories"]["interface"]["pending"], 1)

    def test_refresh_from_nso_enqueues_reconcile(self):
        """The device-level 'Refresh from NSO' button enqueues a background reconcile."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        mgmt.save(update_fields=["adapter_device_id"])
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_reconcile", kwargs={"pk": mgmt.pk})
        with patch("netbox_nso_plugin.reconcile.enqueue_device_reconcile") as m:
            resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        m.assert_called_once_with(mgmt.device_id)
        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_device_nso_tab_with_mgmt(self, mock_session_cls, mock_cfg):
        """Device with NSODeviceManagement and adapter_device_id shows NSO tab."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 15
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session()

        def make_resp(method, url, **kwargs):
            """Return the proper real response shape for each adapter endpoint."""
            if "/interfaces" in url:
                return make_response(200, json_data=[])  # GET /devices/{id}/interfaces → list
            if "/state" in url:
                return make_response(
                    200,
                    json_data={
                        "device_id": 15,
                        "managed_interfaces": 0,
                        "by_status": {},
                        "last_checked_at": None,
                    },
                )
            return make_response(200, json_data={"id": 15, "last_sync_at": None, "last_sync_status": ""})

        session.request.side_effect = make_resp
        mock_session_cls.return_value = session

        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])
        if response.status_code == 200:
            body = response.content.decode()
            # No leaked Django comments (illegal multi-line {# #} renders as text).
            self.assertNotIn("{#", body)
            self.assertNotIn("#}", body)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_device_nso_tab_last_sync_at_updates_db(self, mock_session_cls, mock_cfg):
        """DeviceNSOTabView saves last_sync_at and last_sync_status when adapter returns them."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 16
        mgmt.last_sync_at = None
        mgmt.last_sync_status = ""
        mgmt.save(update_fields=["adapter_device_id", "last_sync_at", "last_sync_status"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }

        def make_resp(method, url, **kwargs):
            if "/interfaces" in url:
                return make_response(200, json_data=[])
            if "/state" in url:
                return make_response(
                    200,
                    json_data={
                        "device_id": 16,
                        "managed_interfaces": 0,
                        "by_status": {},
                        "last_checked_at": None,
                    },
                )
            return make_response(
                200,
                json_data={
                    "id": 16,
                    "last_sync_at": "2025-06-01T10:00:00Z",
                    "last_sync_status": "succeeded",
                },
            )

        session = make_session()
        session.request = make_resp
        mock_session_cls.return_value = session

        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])
        if response.status_code == 200:
            body = response.content.decode()
            # No leaked Django comments (illegal multi-line {# #} renders as text).
            self.assertNotIn("{#", body)
            self.assertNotIn("#}", body)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_device_nso_tab_adapter_error_uses_snapshot(self, mock_session_cls, mock_cfg):
        """DeviceNSOTabView falls back to state_snapshot on AdapterError."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 17
        mgmt.state_snapshot = {"interfaces": [{"name": "eth0"}], "compliance": {"compliant": False}}
        mgmt.save(update_fields=["adapter_device_id", "state_snapshot"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(
            response=make_response(
                503, json_data={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
            )
        )
        mock_session_cls.return_value = session

        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

        mgmt.adapter_device_id = None
        mgmt.state_snapshot = None
        mgmt.save(update_fields=["adapter_device_id", "state_snapshot"])


# ── Interface intent delivery ──────────────────────────────────────────────────────


class TestInterfaceIntentDelivery(ViewTestBase):
    """Tests for rendering and durable delivery of interface intent."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_pushes_accepted_states(self, mock_session_cls, mock_cfg):
        """The interface render carries every accepted state (#1503 Appendix O: rendered, then sent)."""
        from netbox_nso_plugin.delivery import deliver

        content_bulk_update(self.iface_state, status="accepted")
        self.addCleanup(content_bulk_update, self.iface_state, status="changed")
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 20
        mgmt.save(update_fields=["adapter_device_id"])
        self.addCleanup(mirror_update, mgmt, adapter_device_id=None)

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(200, json_data={}))
        mock_session_cls.return_value = session

        deliver("interface", self.device.pk, mgmt.adapter_device_id)
        session.request.assert_called_once()
        sent = session.request.call_args.kwargs["json"]["attributes"]
        assert sent == [
            {
                "interface": self.interface.name,
                "attribute": "description",
                "intent_value": self.interface.description,
                "accepted_at": None,
            }
        ]

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_pushes_enabled_attribute(self, mock_session_cls, mock_cfg):
        """The interface render includes 'enabled' attribute states."""
        from netbox_nso_plugin.delivery import deliver

        # Create an 'enabled' interface state in accepted status
        enabled_state = NSOInterfaceState.objects.create(
            interface=self.interface,
            attribute="enabled",
            status="accepted",
            nso_value="true",
        )
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 21
        mgmt.save(update_fields=["adapter_device_id"])
        self.addCleanup(mirror_update, mgmt, adapter_device_id=None)

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(200, json_data={}))
        mock_session_cls.return_value = session

        deliver("interface", self.device.pk, mgmt.adapter_device_id)
        session.request.assert_called_once()
        sent = session.request.call_args.kwargs["json"]["attributes"]
        assert sent == [
            {
                "interface": self.interface.name,
                "attribute": "enabled",
                "intent_value": str(self.interface.enabled).lower(),
                "accepted_at": None,
            }
        ]

        enabled_state.delete()

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_skips_unknown_attribute(self, mock_session_cls, mock_cfg):
        """The interface render skips accepted states with an unknown attribute."""
        from netbox_nso_plugin.delivery import deliver

        content_bulk_update(self.iface_state, status="accepted")
        self.addCleanup(content_bulk_update, self.iface_state, status="changed")
        # Create a state with an unknown attribute — should be skipped
        unknown_state = NSOInterfaceState.objects.create(
            interface=self.interface,
            attribute="mtu",
            status="accepted",
            nso_value="1500",
        )
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 22
        mgmt.save(update_fields=["adapter_device_id"])
        self.addCleanup(mirror_update, mgmt, adapter_device_id=None)

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = make_session(response=make_response(200, json_data={}))
        mock_session_cls.return_value = session

        deliver("interface", self.device.pk, mgmt.adapter_device_id)
        session.request.assert_called_once()
        sent = session.request.call_args.kwargs["json"]["attributes"]
        assert sent == [
            {
                "interface": self.interface.name,
                "attribute": "description",
                "intent_value": self.interface.description,
                "accepted_at": None,
            }
        ]

        unknown_state.delete()

    def test_the_intent_is_recorded_even_when_the_adapter_is_down(self):
        """The accept records the key; the drain owns the send, and the tick owns the retry.

        The direct push this replaced swallowed the failure of a request that had already
        published ownership, leaving nothing durable behind (#1503 Appendix O, §2).
        """
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        self.addCleanup(content_bulk_update, self.iface_state, status="changed")
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 23
        mgmt.save(update_fields=["adapter_device_id"])
        self.addCleanup(mirror_update, mgmt, adapter_device_id=None)

        session = make_session()
        session.request.side_effect = requests.exceptions.ConnectionError("adapter unavailable")
        with (
            patch(
                "netbox_nso_plugin.adapter_client._resolve_config",
                return_value={
                    "url": "http://adapter",
                    "token": "tok",
                    "verify_tls": True,
                    "ca_cert_path": None,
                    "timeout": 30,
                },
            ),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            self.captureOnCommitCallbacks(execute=True),
        ):
            with transaction.atomic():
                self.iface_state.status = "accepted"
                self.iface_state.save(update_fields={"status"})

        assert NSOIntentOutboxEntry.objects.filter(device=self.device, scope="interface").exists()
        assert session.request.called, "the test did not reach the transport failure"
        mgmt.refresh_from_db()
        assert "interface" in (mgmt.intent_push_errors or {}), "the failed send was recorded as complete"
        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        content_bulk_update(self.iface_state, status="changed")


class TestMergedInterfaceStateFilter(TestCase):
    """Unit tests for the matrix-view state quick-filter bucketing (pure function)."""

    class _I:
        def __init__(self, iface_id):
            self.id = iface_id

    def test_buckets_drift_pending_in_sync(self):
        from netbox_nso_plugin.views import _filter_ifaces_by_state

        a, b, c = self._I(1), self._I(2), self._I(3)
        ordered = [a, b, c]
        kinds = {1: {"drift"}, 2: {"pending"}, 3: {"in_sync"}}
        self.assertEqual(_filter_ifaces_by_state(ordered, kinds, "drift"), [a])
        self.assertEqual(_filter_ifaces_by_state(ordered, kinds, "pending"), [b])
        self.assertEqual(_filter_ifaces_by_state(ordered, kinds, "in_sync"), [c])
        self.assertEqual(_filter_ifaces_by_state(ordered, kinds, "all"), [a, b, c])

    def test_apply_failed_counts_as_pending_not_in_sync(self):
        from netbox_nso_plugin.views import _filter_ifaces_by_state

        x = self._I(1)
        self.assertEqual(_filter_ifaces_by_state([x], {1: {"apply_failed"}}, "pending"), [x])
        self.assertEqual(_filter_ifaces_by_state([x], {1: {"apply_failed"}}, "in_sync"), [])

    def test_mixed_interface_appears_in_both_drift_and_pending(self):
        from netbox_nso_plugin.views import _filter_ifaces_by_state

        x = self._I(1)
        kinds = {1: {"drift", "pending"}}
        self.assertEqual(_filter_ifaces_by_state([x], kinds, "drift"), [x])
        self.assertEqual(_filter_ifaces_by_state([x], kinds, "pending"), [x])
        self.assertEqual(_filter_ifaces_by_state([x], kinds, "in_sync"), [])  # not clean → excluded


class TestInterfaceMatrixStateChips(ViewTestBase):
    """The consolidated interface matrix renders the drift/pending/in-sync quick-filter."""

    def _url(self):
        return reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "interface"},
        )

    def test_matrix_renders_state_chips(self):
        resp = self.client.get(self._url(), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "nso-grid-state")
        self.assertContains(resp, 'data-state="drift"')
        self.assertContains(resp, 'data-state="pending"')

    def test_matrix_accepts_state_param(self):
        # The grid filters client-side now; a legacy ?state= URL must stay harmless.
        resp = self.client.get(self._url() + "?state=pending", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)


class TestOverlayFieldEditView(ViewTestBase):
    """Inline (popover) field edits on SNMP / logging / MTU overlay rows.

    One generic endpoint, per-model field whitelist. An inline edit TAKES
    OWNERSHIP (status → accepted, accepted_at set) — the tab's documented
    inline-edit semantic ("NetBox will own this value — same as Accept").
    Anything weaker is silently futile: the next category reconcile refreshes
    unowned rows from the device mirror and the edit evaporates (caught live)."""

    def _url(self, key, pk):
        return reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": key, "pk": pk})

    def test_edit_snmp_system_info_field_takes_ownership(self):
        from netbox_nso_plugin.models import NSOSnmpSystemInfoState

        row = NSOSnmpSystemInfoState.objects.create(
            management=self.mgmt, location="old-loc", contact="noc@old.example", status="imported"
        )
        r = self.client.post(self._url("snmp_system_info", row.pk), {"location": "rack C7"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        row.refresh_from_db()
        self.assertEqual(row.location, "rack C7")
        self.assertEqual(row.contact, "noc@old.example")  # untouched
        # Edited value diverges from the device → owned, pending apply — and safe
        # from the reconciler (which refreshes only unowned rows).
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)

    def test_snmp_community_rejects_access_the_writer_cannot_apply(self):
        from netbox_nso_plugin.models import NSOSnmpCommunityState

        row = NSOSnmpCommunityState.objects.create(
            management=self.mgmt,
            community_hash="1111222233334444",
            access="RO",
            status="imported",
        )

        response = self.client.post(self._url("snmp_community", row.pk), {"access": "READ"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("access", response.json()["errors"])
        row.refresh_from_db()
        self.assertEqual(row.access, "RO")
        self.assertEqual(row.status, "imported")

    def test_snmp_host_rejects_values_outside_writer_contract(self):
        from netbox_nso_plugin.models import NSOSnmpHostState

        row = NSOSnmpHostState.objects.create(
            management=self.mgmt,
            address="198.51.100.31",
            version="v2c",
            notify_type="trap",
            port=162,
            community_hash="1111222233334444",
            status="imported",
        )
        cases = (
            ({"version": "v4"}, "version"),
            ({"notify_type": "poll"}, "notify_type"),
            ({"port": "65536"}, "port"),
        )

        for values, field in cases:
            with self.subTest(field=field):
                response = self.client.post(self._url("snmp_host", row.pk), values)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json()["errors"])
                row.refresh_from_db()
                self.assertEqual(row.status, "imported")

    def test_snmp_host_rejects_v3_without_security_username(self):
        from netbox_nso_plugin.models import NSOSnmpHostState

        row = NSOSnmpHostState.objects.create(
            management=self.mgmt,
            address="198.51.100.32",
            version="v2c",
            notify_type="trap",
            community_hash="1111222233334444",
            status="imported",
        )

        response = self.client.post(
            self._url("snmp_host", row.pk),
            {"version": "v3", "username": ""},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.json()["errors"])
        row.refresh_from_db()
        self.assertEqual(row.version, "v2c")
        self.assertEqual(row.status, "imported")

    def test_snmp_category_compacts_sections_and_exposes_safe_inline_fields(self):
        from netbox_nso_plugin.models import (
            NSOSnmpCommunityState,
            NSOSnmpHostState,
            NSOSnmpSystemInfoState,
            NSOSnmpV3UserState,
        )

        system_info = NSOSnmpSystemInfoState.objects.create(
            management=self.mgmt,
            location="Lab A",
            contact="operations@example.test",
            status="imported",
        )
        community = NSOSnmpCommunityState.objects.create(
            management=self.mgmt,
            community_hash="5555666677778888",
            access="RO",
            acl="SNMP-MGMT",
            status="imported",
        )
        NSOSnmpV3UserState.objects.create(
            management=self.mgmt,
            username="monitor",
            auth_protocol="sha",
            priv_protocol="aes-128",
            status="imported",
        )
        host = NSOSnmpHostState.objects.create(
            management=self.mgmt,
            address="198.51.100.33",
            version="3",
            notify_type="informs",
            port=1162,
            username="monitor",
            status="imported",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "snmp"},
        )

        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._url("snmp_system_info", system_info.pk))
        self.assertContains(response, 'data-pe-fields="location:text:Location,contact:text:Contact"')
        self.assertContains(response, self._url("snmp_community", community.pk))
        self.assertContains(response, 'data-pe-fields="access:select:Access,acl:text:ACL"')
        self.assertContains(response, self._url("snmp_host", host.pk))
        self.assertContains(response, 'data-pe-v-version="v3"')
        self.assertContains(response, 'data-pe-v-notify_type="inform"')
        self.assertContains(
            response,
            'data-pe-fields="version:select:Version,notify_type:select:Notification,port:number:Port,username:text:v3 User"',
        )
        self.assertContains(response, "Status / Synced")
        self.assertNotContains(response, "<th>Last Synced</th>", html=True)
        self.assertNotContains(response, "<th>Device auth/priv</th>", html=True)
        self.assertNotContains(response, "<th>Protocols</th>", html=True)
        self.assertNotContains(response, "<th>Version</th>", html=True)
        self.assertNotContains(response, "<th>Port</th>", html=True)

    def test_edit_logging_host_fields_take_ownership(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        row = NSOLoggingHostState.objects.create(
            management=self.mgmt, address="198.51.100.9", severity="warning", status="imported"
        )
        r = self.client.post(self._url("logging_host", row.pk), {"severity": "informational", "port": "1514"})
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.severity, "informational")
        self.assertEqual(row.port, 1514)
        self.assertEqual(row.status, "accepted")

    def test_logging_host_rejects_port_outside_writer_uint16(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        row = NSOLoggingHostState.objects.create(
            management=self.mgmt, address="198.51.100.19", port=514, status="imported"
        )

        response = self.client.post(self._url("logging_host", row.pk), {"port": "65536"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("port", response.json()["errors"])
        row.refresh_from_db()
        self.assertEqual(row.port, 514)
        self.assertEqual(row.status, "imported")

    def test_logging_host_rejects_transport_the_writer_cannot_apply(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        row = NSOLoggingHostState.objects.create(
            management=self.mgmt, address="198.51.100.20", transport="udp", status="imported"
        )

        response = self.client.post(self._url("logging_host", row.pk), {"transport": "sctp"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("transport", response.json()["errors"])
        row.refresh_from_db()
        self.assertEqual(row.transport, "udp")
        self.assertEqual(row.status, "imported")

    def test_logging_category_groups_inline_fields_into_compact_columns(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        row = NSOLoggingHostState.objects.create(
            management=self.mgmt,
            address="198.51.100.21",
            port=1514,
            severity="warning",
            facility="local7",
            transport="tcp",
            vrf="management",
            source="Loopback0",
            status="imported",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "logging"},
        )

        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        edit_url = self._url("logging_host", row.pk)
        self.assertContains(response, edit_url)
        self.assertContains(response, 'data-pe-fields="port:number:Port,transport:select:Transport"')
        self.assertContains(response, 'data-pe-fields="severity:text:Severity,facility:text:Facility"')
        self.assertContains(response, 'data-pe-fields="source:text:Source,vrf:text:VRF"')
        self.assertContains(response, "Status / Synced")
        full_edit_url = reverse("plugins:netbox_nso_plugin:nsologginghoststate_edit", kwargs={"pk": row.pk})
        self.assertNotContains(response, full_edit_url)
        self.assertNotContains(response, 'data-pe-fields="address:text:Address"')
        self.assertNotContains(response, "<th>Port</th>", html=True)
        self.assertNotContains(response, "<th>Severity</th>", html=True)
        self.assertNotContains(response, "<th>Facility</th>", html=True)
        self.assertNotContains(response, "<th>Source</th>", html=True)
        self.assertNotContains(response, "<th>VRF</th>", html=True)

    def test_editing_an_address_onto_a_sibling_row_is_a_400_not_a_500(self):
        """field.clean() is FIELD-level only — it never checks unique/unique_together. The
        logging_host popover exposes `address`, half of NSOLoggingHostState's (management,
        address) unique constraint, so retyping it as a sibling's address sailed through
        validation into obj.save() and raised an unhandled IntegrityError: HTTP 500, and a
        popover that just spins with no error shown.
        """
        from netbox_nso_plugin.models import NSOLoggingHostState

        NSOLoggingHostState.objects.create(management=self.mgmt, address="198.51.100.1", status="imported")
        row = NSOLoggingHostState.objects.create(management=self.mgmt, address="198.51.100.2", status="imported")

        r = self.client.post(self._url("logging_host", row.pk), {"address": "198.51.100.1"})

        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["status"], "error")
        self.assertTrue(body.get("errors") or body.get("message"), "the collision must be reported to the popover")
        row.refresh_from_db()
        self.assertEqual(row.address, "198.51.100.2", "the colliding edit must not be persisted")

    def test_edit_mtu_takes_ownership_and_adopts_native_l2(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        row = NSOInterfaceMtuState.objects.create(
            management=self.mgmt, interface=self.interface, l2_mtu=9214, status="imported"
        )
        r = self.client.post(self._url("interface_mtu", row.pk), {"l2_mtu": "9000"})
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.l2_mtu, 9000)
        self.assertEqual(row.status, "accepted")
        # Same side effect as the Accept view: the native NetBox interface MTU follows.
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mtu, 9000)

    def test_edit_mtu_keeps_owned_status(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        row = NSOInterfaceMtuState.objects.create(
            management=self.mgmt, interface=self.interface, l2_mtu=9214, status="accepted"
        )
        r = self.client.post(self._url("interface_mtu", row.pk), {"l2_mtu": "9100"})
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.l2_mtu, 9100)
        self.assertEqual(row.status, "accepted")

    def test_edit_bfd_updates_overlay_and_native_profile(self):
        from netbox_routing.models import BFDInterface, BFDProfile

        from netbox_nso_plugin.models import NSOBFDInterfaceState

        old_profile = BFDProfile.objects.create(
            name="bfd-inline-old",
            min_tx_int=300,
            min_rx_int=300,
            multiplier=3,
        )
        native = BFDInterface.objects.create(
            interface=self.interface,
            bfd_profile=old_profile,
            micro_bfd=False,
            enabled=True,
        )
        row = NSOBFDInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            micro_bfd=False,
            status="imported",
        )

        response = self.client.post(
            self._url("bfd", row.pk),
            {"min_tx": "500", "min_rx": "600", "multiplier": "5", "micro_bfd": "True"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        self.assertEqual((row.min_tx, row.min_rx, row.multiplier, row.micro_bfd), (500, 600, 5, True))
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)
        native.refresh_from_db()
        self.assertTrue(native.micro_bfd)
        self.assertEqual(
            (native.bfd_profile.min_tx_int, native.bfd_profile.min_rx_int, native.bfd_profile.multiplier),
            (500, 600, 5),
        )

    def test_edit_bfd_rejects_out_of_range_timer_without_writing(self):
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        row = NSOBFDInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="imported",
        )

        response = self.client.post(self._url("bfd", row.pk), {"min_tx": "59"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("min_tx", response.json()["errors"])
        row.refresh_from_db()
        self.assertEqual(row.min_tx, 300)
        self.assertEqual(row.status, "imported")

    def test_edit_ospf_interface_updates_overlay_and_native_object(self):
        from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

        from netbox_nso_plugin.models import NSOOSPFInterfaceState

        instance = OSPFInstance.objects.create(
            device=self.device,
            name="inline-ospf",
            process_id="7",
            router_id="192.0.2.7",
        )
        old_area = OSPFArea.objects.create(area_id="0.0.0.0", area_type="standard")
        native = OSPFInterface.objects.create(
            instance=instance,
            area=old_area,
            interface=self.interface,
            passive=False,
            cost=10,
            network_type="broadcast",
        )
        row = NSOOSPFInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            process_id="7",
            area_id="0.0.0.0",
            passive=False,
            cost=10,
            network_type="broadcast",
            status="imported",
        )

        response = self.client.post(
            self._url("ospf_interface", row.pk),
            {
                "area_id": "0.0.0.1",
                "network_type": "point-to-point",
                "cost": "25",
                "passive": "True",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)
        self.assertEqual(
            (row.area_id, row.network_type, row.cost, row.passive), ("0.0.0.1", "point-to-point", 25, True)
        )
        self.assertEqual(native.area.area_id, "0.0.0.1")
        self.assertEqual((native.network_type, native.cost, native.passive), ("point-to-point", 25, True))

    def test_edit_ospf_interface_rejects_invalid_config_without_writing(self):
        from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

        from netbox_nso_plugin.models import NSOOSPFInterfaceState

        instance = OSPFInstance.objects.create(
            device=self.device,
            name="invalid-inline-ospf",
            process_id="8",
            router_id="192.0.2.8",
        )
        area = OSPFArea.objects.create(area_id="0.0.0.0", area_type="standard")
        native = OSPFInterface.objects.create(
            instance=instance,
            area=area,
            interface=self.interface,
            passive=False,
            cost=10,
            network_type="broadcast",
        )
        row = NSOOSPFInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            process_id="8",
            area_id="0.0.0.0",
            passive=False,
            cost=10,
            network_type="broadcast",
            status="imported",
        )

        response = self.client.post(
            self._url("ospf_interface", row.pk),
            {"area_id": "not-an-area", "network_type": "invented", "cost": "0"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()["errors"]), {"area_id", "network_type", "cost"})
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual((row.area_id, row.network_type, row.cost), ("0.0.0.0", "broadcast", 10))
        self.assertEqual((native.area.area_id, native.network_type, native.cost), ("0.0.0.0", "broadcast", 10))
        self.assertEqual(row.status, "imported")

    def test_edit_ospf_instance_router_id_updates_native_object(self):
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.models import NSOOSPFInstanceState

        native = OSPFInstance.objects.create(
            device=self.device,
            name="router-id-inline-ospf",
            process_id="9",
            router_id="192.0.2.9",
        )
        row = NSOOSPFInstanceState.objects.create(
            management=self.mgmt,
            process_id="9",
            router_id="192.0.2.9",
            ospf_instance=native,
            status="imported",
        )

        response = self.client.post(
            self._url("ospf_instance", row.pk),
            {"router_id": "192.0.2.10"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual(row.router_id, "192.0.2.10")
        self.assertEqual(str(native.router_id), "192.0.2.10")
        self.assertEqual(row.status, "accepted")

    def test_edit_isis_interface_updates_overlay_and_native_object(self):
        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOISISInterfaceState

        instance = ISISInstance.objects.create(device=self.device, process_tag="CORE")
        native = ISISInterface.objects.create(
            instance=instance,
            interface=self.interface,
            address_family="ipv4",
            circuit_type="level-1-2",
            network_type="broadcast",
            metric=10,
            passive=False,
        )
        row = NSOISISInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            af="ipv4",
            process_tag="CORE",
            circuit_type="level-1-2",
            network_type="broadcast",
            metric=10,
            passive=False,
            isis_interface=native,
            status="imported",
        )

        response = self.client.post(
            self._url("isis_interface", row.pk),
            {
                "circuit_type": "level-2-only",
                "network_type": "point-to-point",
                "metric": "25",
                "passive": "True",
                "bfd_enabled": "True",
                "frr_enabled": "True",
                "frr_protection": "node",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)
        self.assertEqual(
            (
                row.circuit_type,
                row.network_type,
                row.metric,
                row.passive,
                row.bfd_enabled,
                row.frr_enabled,
                row.frr_protection,
            ),
            ("level-2-only", "point-to-point", 25, True, True, True, "node"),
        )
        self.assertEqual(
            (
                native.circuit_type,
                native.network_type,
                native.metric,
                native.passive,
                native.bfd_enabled,
                native.frr_enabled,
                native.frr_protection,
            ),
            ("level-2-only", "point-to-point", 25, True, True, True, "node"),
        )

    def test_edit_isis_instance_updates_safe_core_fields(self):
        from netbox_routing.models import ISISInstance

        from netbox_nso_plugin.models import NSOISISInstanceState

        native = ISISInstance.objects.create(
            device=self.device,
            process_tag="CORE",
            net="49.0001.0000.0000.0001.00",
            is_type="level-1-2",
            metric_style="narrow",
        )
        row = NSOISISInstanceState.objects.create(
            management=self.mgmt,
            process_tag="CORE",
            net=native.net,
            is_type=native.is_type,
            metric_style=native.metric_style,
            isis_instance=native,
            status="imported",
        )

        response = self.client.post(
            self._url("isis_instance", row.pk),
            {
                "net": "49.0001.0000.0000.0002.00",
                "is_type": "level-2-only",
                "metric_style": "wide",
                "overload_bit": "True",
                "fast_reroute": "ti-lfa",
                "microloop_avoidance": "True",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        native.refresh_from_db()
        expected = ("49.0001.0000.0000.0002.00", "level-2-only", "wide", True, "ti-lfa", True)
        self.assertEqual(
            (row.net, row.is_type, row.metric_style, row.overload_bit, row.fast_reroute, row.microloop_avoidance),
            expected,
        )
        self.assertEqual(
            (
                native.net,
                native.is_type,
                native.metric_style,
                native.overload_bit,
                native.fast_reroute,
                native.microloop_avoidance,
            ),
            expected,
        )
        self.assertEqual(row.status, "accepted")

    def test_edit_isis_rejects_invalid_net_and_inconsistent_frr(self):
        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOISISInstanceState, NSOISISInterfaceState

        instance = ISISInstance.objects.create(
            device=self.device,
            process_tag="EDGE",
            net="49.0001.0000.0000.0003.00",
        )
        process_row = NSOISISInstanceState.objects.create(
            management=self.mgmt,
            process_tag="EDGE",
            net=instance.net,
            isis_instance=instance,
            status="imported",
        )
        native_iface = ISISInterface.objects.create(
            instance=instance,
            interface=self.interface,
            address_family="ipv4",
        )
        iface_row = NSOISISInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.interface,
            af="ipv4",
            process_tag="EDGE",
            isis_interface=native_iface,
            status="imported",
        )

        bad_net = self.client.post(self._url("isis_instance", process_row.pk), {"net": "not-a-net"})
        bad_frr = self.client.post(
            self._url("isis_interface", iface_row.pk),
            {"frr_enabled": "False", "frr_protection": "node"},
        )

        self.assertEqual(bad_net.status_code, 400)
        self.assertIn("net", bad_net.json()["errors"])
        self.assertEqual(bad_frr.status_code, 400)
        self.assertIn("frr_protection", bad_frr.json()["errors"])
        process_row.refresh_from_db()
        iface_row.refresh_from_db()
        self.assertEqual(process_row.net, "49.0001.0000.0000.0003.00")
        self.assertIsNone(iface_row.frr_enabled)
        self.assertEqual(iface_row.frr_protection, "")
        self.assertEqual((process_row.status, iface_row.status), ("imported", "imported"))

    def test_edit_bgp_peer_updates_remote_as_enabled_and_native_object(self):
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        from netbox_nso_plugin.models import NSOBGPPeerState

        rir = RIR.objects.create(name="Inline BGP Private", slug="inline-bgp-private", is_private=True)
        local_as = ASN.objects.create(asn=64512, rir=rir)
        old_remote = ASN.objects.create(asn=64513, rir=rir)
        router = BGPRouter.objects.create(
            name="inline-bgp",
            assigned_object_type=ContentType.objects.get_for_model(self.device),
            assigned_object_id=self.device.pk,
            asn=local_as,
        )
        scope = BGPScope.objects.create(router=router)
        peer_ip = IPAddress.objects.create(address="192.0.2.20/32")
        native = BGPPeer.objects.create(scope=scope, peer=peer_ip, remote_as=old_remote, enabled=True)
        row = NSOBGPPeerState.objects.create(
            management=self.mgmt,
            asn_str="64512",
            peer_address_str="192.0.2.20",
            remote_as_str="64513",
            enabled=True,
            bgp_peer=native,
            status="imported",
        )

        response = self.client.post(
            self._url("bgp_peer", row.pk),
            {"remote_as_str": "64514", "enabled": "False"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual((row.remote_as_str, row.enabled, row.status), ("64514", False, "accepted"))
        self.assertIsNotNone(row.accepted_at)
        self.assertEqual(native.remote_as.asn, 64514)
        self.assertFalse(native.enabled)

    def test_edit_bgp_peer_rejects_out_of_range_asn_without_writing(self):
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        from netbox_nso_plugin.models import NSOBGPPeerState

        rir = RIR.objects.create(name="Invalid Inline BGP", slug="invalid-inline-bgp", is_private=True)
        local_as = ASN.objects.create(asn=64520, rir=rir)
        remote_as = ASN.objects.create(asn=64521, rir=rir)
        router = BGPRouter.objects.create(
            name="invalid-inline-bgp",
            assigned_object_type=ContentType.objects.get_for_model(self.device),
            assigned_object_id=self.device.pk,
            asn=local_as,
        )
        scope = BGPScope.objects.create(router=router)
        native = BGPPeer.objects.create(
            scope=scope,
            peer=IPAddress.objects.create(address="192.0.2.21/32"),
            remote_as=remote_as,
            enabled=True,
        )
        row = NSOBGPPeerState.objects.create(
            management=self.mgmt,
            asn_str="64520",
            peer_address_str="192.0.2.21",
            remote_as_str="64521",
            enabled=True,
            bgp_peer=native,
            status="imported",
        )

        response = self.client.post(self._url("bgp_peer", row.pk), {"remote_as_str": "9999999999"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("remote_as_str", response.json()["errors"])
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual((row.remote_as_str, row.status), ("64521", "imported"))
        self.assertEqual(native.remote_as.asn, 64521)

    def test_bgp_template_json_does_not_require_a_nonexistent_detail_url(self):
        from ipam.models import ASN, RIR
        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        rir = RIR.objects.create(name="Template Inline BGP", slug="template-inline-bgp", is_private=True)
        remote_as = ASN.objects.create(asn=64530, rir=rir)
        template = BGPPeerTemplate.objects.create(name="EDGE-PEERS", remote_as=remote_as)
        NSOBGPPeerTemplateState.objects.create(
            management=self.mgmt,
            template_name=template.name,
            template=template,
            remote_as_str=str(remote_as.asn),
            status="imported",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "bgp"},
        )

        response = self.client.get(url + "?format=json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200, response.content)
        template_row = response.json()["templates"]["rows"][0]
        self.assertEqual(template_row["template"], {"label": "EDGE-PEERS"})

    def test_edit_redistribution_updates_policy_fields_and_native_object(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution, RouteMap

        from netbox_nso_plugin.models import NSORedistributionState

        instance = OSPFInstance.objects.create(
            device=self.device,
            name="inline-redistribution",
            process_id="10",
            router_id="192.0.2.10",
        )
        old_route_map = RouteMap.objects.create(name="RM-OLD")
        new_route_map = RouteMap.objects.create(name="RM-NEW")
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(instance),
            destination_id=instance.pk,
            source_protocol="static",
            route_map=old_route_map,
            metric=10,
            metric_type="2",
        )
        row = NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="ospf",
            dest_ref="10",
            source_protocol="static",
            route_map=old_route_map.name,
            metric=10,
            metric_type="2",
            redistribution=native,
            status="imported",
        )

        response = self.client.post(
            self._url("redistribution", row.pk),
            {"route_map": new_route_map.name, "metric": "25", "metric_type": "1"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual((row.route_map, row.metric, row.metric_type), ("RM-NEW", 25, "1"))
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)
        self.assertEqual((native.route_map, native.metric, native.metric_type), (new_route_map, 25, "1"))

    def test_edit_redistribution_rejects_missing_route_map_without_writing(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution, RouteMap

        from netbox_nso_plugin.models import NSORedistributionState

        instance = OSPFInstance.objects.create(
            device=self.device,
            name="invalid-inline-redistribution",
            process_id="11",
            router_id="192.0.2.11",
        )
        route_map = RouteMap.objects.create(name="RM-KEEP")
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(instance),
            destination_id=instance.pk,
            source_protocol="connected",
            route_map=route_map,
            metric=5,
            metric_type="1",
        )
        row = NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="ospf",
            dest_ref="11",
            source_protocol="connected",
            route_map=route_map.name,
            metric=5,
            metric_type="1",
            redistribution=native,
            status="imported",
        )

        response = self.client.post(self._url("redistribution", row.pk), {"route_map": "RM-MISSING"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("route_map", response.json()["errors"])
        invalid_type = self.client.post(self._url("redistribution", row.pk), {"metric_type": "external"})
        self.assertEqual(invalid_type.status_code, 400)
        self.assertIn("metric_type", invalid_type.json()["errors"])
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual((row.route_map, row.metric, row.metric_type, row.status), ("RM-KEEP", 5, "1", "imported"))
        self.assertEqual(native.route_map, route_map)

    def test_edit_static_route_updates_native_policy_and_takes_ownership(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        native = StaticRoute.objects.create(
            prefix="198.51.100.0/24",
            next_hop="192.0.2.1",
            metric=10,
            permanent=False,
        )
        row = NSOStaticRouteState.objects.create(
            management=self.mgmt,
            static_route=native,
            nso_prefix=str(native.prefix),
            nso_next_hop=str(native.next_hop),
            status="imported",
        )

        response = self.client.post(
            self._url("static_route", row.pk),
            {"metric": "25", "permanent": "True", "tag": "120"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual((native.metric, native.permanent, native.tag), (25, True, 120))
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)

    def test_edit_static_route_rejects_metric_above_device_model_limit(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        native = StaticRoute.objects.create(
            prefix="203.0.113.0/24",
            next_hop="192.0.2.2",
            metric=10,
            permanent=False,
        )
        row = NSOStaticRouteState.objects.create(
            management=self.mgmt,
            static_route=native,
            nso_prefix=str(native.prefix),
            nso_next_hop=str(native.next_hop),
            status="imported",
        )

        response = self.client.post(self._url("static_route", row.pk), {"metric": "256"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("metric", response.json()["errors"])
        row.refresh_from_db()
        native.refresh_from_db()
        self.assertEqual(native.metric, 10)
        self.assertEqual(row.status, "imported")

    def test_edit_lacp_bundle_updates_parameters_and_owns_members(self):
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOLACPMemberState

        lag = Interface.objects.create(device=self.device, name="Port-channel10", type="lag")
        member = Interface.objects.create(device=self.device, name="GigabitEthernet0/10", type="1000base-t")
        bundle = NSOLACPBundleState.objects.create(
            management=self.mgmt,
            interface=lag,
            lag_id=10,
            min_links=1,
            system_priority=32768,
            timer="slow",
            status="imported",
        )
        member_state = NSOLACPMemberState.objects.create(
            management=self.mgmt,
            interface=member,
            lag_bundle=lag,
            mode="active",
            port_priority=32768,
            status="imported",
        )

        response = self.client.post(
            self._url("lacp_bundle", bundle.pk),
            {"min_links": "2", "system_priority": "100", "timer": "fast", "admin_key": "10"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        bundle.refresh_from_db()
        member_state.refresh_from_db()
        self.assertEqual(
            (bundle.min_links, bundle.system_priority, bundle.timer, bundle.admin_key), (2, 100, "fast", 10)
        )
        self.assertEqual((bundle.status, member_state.status), ("accepted", "in_sync"))
        self.assertIsNotNone(bundle.accepted_at)
        self.assertIsNotNone(member_state.accepted_at)

    def test_edit_lacp_member_owns_bundle_and_rejects_values_outside_yang_contract(self):
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOLACPMemberState

        lag = Interface.objects.create(device=self.device, name="ae10", type="lag")
        member = Interface.objects.create(device=self.device, name="ge-0/0/10", type="1000base-t")
        bundle = NSOLACPBundleState.objects.create(
            management=self.mgmt,
            interface=lag,
            lag_id=10,
            min_links=1,
            status="imported",
        )
        member_state = NSOLACPMemberState.objects.create(
            management=self.mgmt,
            interface=member,
            lag_bundle=lag,
            mode="active",
            port_priority=100,
            status="imported",
        )

        response = self.client.post(
            self._url("lacp_member", member_state.pk),
            {"mode": "passive", "port_priority": "65535"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        bundle.refresh_from_db()
        member_state.refresh_from_db()
        self.assertEqual((member_state.mode, member_state.port_priority), ("passive", 65535))
        self.assertEqual((bundle.status, member_state.status), ("accepted", "accepted"))

        bad_priority = self.client.post(self._url("lacp_member", member_state.pk), {"port_priority": "65536"})
        bad_mode = self.client.post(self._url("lacp_member", member_state.pk), {"mode": "unknown"})
        self.assertEqual(bad_priority.status_code, 400)
        self.assertIn("port_priority", bad_priority.json()["errors"])
        self.assertEqual(bad_mode.status_code, 400)
        self.assertIn("mode", bad_mode.json()["errors"])
        member_state.refresh_from_db()
        self.assertEqual((member_state.mode, member_state.port_priority), ("passive", 65535))

    def test_edit_lacp_member_repends_the_deploying_bundle(self):
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOLACPMemberState

        lag = Interface.objects.create(device=self.device, name="Port-channel11", type="lag")
        edited = Interface.objects.create(device=self.device, name="GigabitEthernet0/11", type="1000base-t")
        sibling = Interface.objects.create(device=self.device, name="GigabitEthernet0/12", type="1000base-t")
        bundle = NSOLACPBundleState.objects.create(
            management=self.mgmt,
            interface=lag,
            lag_id=11,
            min_links=1,
            status="deploying",
        )
        edited_state = NSOLACPMemberState.objects.create(
            management=self.mgmt,
            interface=edited,
            lag_bundle=lag,
            mode="active",
            port_priority=100,
            status="deploying",
        )
        sibling_state = NSOLACPMemberState.objects.create(
            management=self.mgmt,
            interface=sibling,
            lag_bundle=lag,
            mode="active",
            port_priority=200,
            status="deploying",
        )

        response = self.client.post(
            self._url("lacp_member", edited_state.pk),
            {"mode": "passive"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        bundle.refresh_from_db()
        edited_state.refresh_from_db()
        sibling_state.refresh_from_db()
        self.assertEqual((bundle.status, edited_state.status, sibling_state.status), ("accepted",) * 3)

    def test_edit_shared_vlan_name_updates_native_object_and_owns_every_attached_device(self):
        from django.utils import timezone
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOVLANState

        group = VLANGroup.objects.create(name="Shared Inline VLANs", slug="shared-inline-vlans")
        vlan = VLAN.objects.create(group=group, vid=120, name="OLD-NAME")
        first = NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="OLD-NAME",
            status="deploying",
            accepted_at=timezone.now(),
            apply_attempt_id=uuid4(),
        )
        other_device = Device.objects.create(
            name="view-router-vlan-02",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        other_mgmt = NSODeviceManagement.objects.create(
            device=other_device,
            nso_instance=self.nso_instance,
            nso_device_name=other_device.name,
        )
        second = NSOVLANState.objects.create(
            management=other_mgmt,
            vlan=vlan,
            device_name="OLD-NAME",
            status="imported",
        )

        response = self.client.post(self._url("vlan_name", first.pk), {"name": "CUSTOMER-A"})

        self.assertEqual(response.status_code, 200, response.content)
        vlan.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(vlan.name, "CUSTOMER-A")
        self.assertEqual((first.status, second.status), ("accepted", "accepted"))
        self.assertIsNone(first.apply_attempt_id)
        self.assertIsNotNone(first.accepted_at)
        self.assertIsNotNone(second.accepted_at)

    def test_edit_vlan_name_reports_when_the_vlan_is_deleted_before_save(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.intent_state import deletion_footprint_for_instance, intent_transaction, vlan_footprint
        from netbox_nso_plugin.models import NSOIntentRevision, NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push

        group = VLANGroup.objects.create(name="Removed Inline VLANs", slug="removed-inline-vlans")
        vlan = VLAN.objects.create(group=group, vid=123, name="REMOVED-NAME")
        state = NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="REMOVED-NAME",
            status="imported",
        )
        revisions_after_delete = []

        def delete_then_resolve(vlan_id, scopes, **kwargs):
            doomed = VLAN.objects.get(pk=vlan_id)
            with suppress_intent_push(), intent_transaction(deletion_footprint_for_instance(doomed)):
                doomed.delete()
            revisions_after_delete.append(NSOIntentRevision.objects.get(device=self.device, scope="vlan").revision)
            return vlan_footprint(vlan_id, scopes, **kwargs)

        with patch("netbox_nso_plugin.intent_state.vlan_footprint", new=delete_then_resolve):
            response = self.client.post(self._url("vlan_name", state.pk), {"name": "UNSAVED-NAME"})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("no longer exists", " ".join(response.json()["errors"]["name"]).lower())
        self.assertFalse(VLAN.objects.filter(pk=vlan.pk).exists())
        self.assertFalse(NSOVLANState.objects.filter(pk=state.pk).exists())
        self.assertFalse(VLAN.objects.filter(name="UNSAVED-NAME").exists())
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        self.assertEqual(revision.revision, revisions_after_delete[0])

    def test_edit_svi_vrf_takes_ownership_without_changing_structural_identity(self):
        from django.utils import timezone
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOSVIState

        group = VLANGroup.objects.create(name="Inline SVI VLANs", slug="inline-svi-vlans")
        vlan = VLAN.objects.create(group=group, vid=220, name="CUSTOMER-A")
        interface = Interface.objects.create(device=self.device, name="Vlan220", type="virtual")
        state = NSOSVIState.objects.create(
            management=self.mgmt,
            interface=interface,
            vlan=vlan,
            svi_type="svi",
            vrf="OLD-VRF",
            status="deploying",
            accepted_at=timezone.now(),
            apply_attempt_id=uuid4(),
        )

        response = self.client.post(self._url("svi", state.pk), {"vrf": "CUSTOMER"})

        self.assertEqual(response.status_code, 200, response.content)
        state.refresh_from_db()
        self.assertEqual(state.vrf, "CUSTOMER")
        self.assertEqual(state.status, "accepted")
        self.assertIsNone(state.apply_attempt_id)
        self.assertIsNotNone(state.accepted_at)
        self.assertEqual(state.interface_id, interface.pk)
        self.assertEqual(state.vlan_id, vlan.pk)
        self.assertEqual(state.svi_type, "svi")

    def test_edit_subinterface_l3_values_takes_ownership_without_changing_identity(self):
        from netbox_nso_plugin.models import NSOSubinterfaceState

        parent = Interface.objects.create(device=self.device, name="GigabitEthernet0/1", type="1000base-t")
        interface = Interface.objects.create(
            device=self.device,
            name="GigabitEthernet0/1.100",
            type="virtual",
            parent=parent,
        )
        state = NSOSubinterfaceState.objects.create(
            management=self.mgmt,
            interface=interface,
            parent_interface=parent,
            dot1q_vlan=100,
            vrf="OLD-VRF",
            status="imported",
        )

        response = self.client.post(
            self._url("subinterface", state.pk),
            {"dot1q_vlan": "220", "vrf": "CUSTOMER"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        state.refresh_from_db()
        self.assertEqual((state.dot1q_vlan, state.vrf), (220, "CUSTOMER"))
        self.assertEqual(state.status, "accepted")
        self.assertIsNotNone(state.accepted_at)
        self.assertEqual(state.interface_id, interface.pk)
        self.assertEqual(state.parent_interface_id, parent.pk)

    def test_edit_subinterface_rejects_invalid_duplicate_or_unpushable_values(self):
        from netbox_nso_plugin.models import NSOSubinterfaceState

        parent = Interface.objects.create(device=self.device, name="GigabitEthernet0/2", type="1000base-t")
        first_interface = Interface.objects.create(
            device=self.device,
            name="GigabitEthernet0/2.100",
            type="virtual",
            parent=parent,
        )
        second_interface = Interface.objects.create(
            device=self.device,
            name="GigabitEthernet0/2.200",
            type="virtual",
            parent=parent,
        )
        NSOSubinterfaceState.objects.create(
            management=self.mgmt,
            interface=first_interface,
            parent_interface=parent,
            dot1q_vlan=100,
            status="imported",
        )
        state = NSOSubinterfaceState.objects.create(
            management=self.mgmt,
            interface=second_interface,
            parent_interface=parent,
            dot1q_vlan=200,
            status="imported",
        )
        orphan_interface = Interface.objects.create(device=self.device, name="orphan.300", type="virtual")
        orphan = NSOSubinterfaceState.objects.create(
            management=self.mgmt,
            interface=orphan_interface,
            dot1q_vlan=300,
            status="changed",
        )

        too_high = self.client.post(self._url("subinterface", state.pk), {"dot1q_vlan": "4095"})
        duplicate = self.client.post(self._url("subinterface", state.pk), {"dot1q_vlan": "100"})
        missing_parent = self.client.post(self._url("subinterface", orphan.pk), {"vrf": "CUSTOMER"})

        self.assertEqual(too_high.status_code, 400)
        self.assertIn("dot1q_vlan", too_high.json()["errors"])
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("dot1q_vlan", duplicate.json()["errors"])
        self.assertEqual(missing_parent.status_code, 400)
        self.assertIn("vrf", missing_parent.json()["errors"])
        state.refresh_from_db()
        orphan.refresh_from_db()
        self.assertEqual((state.dot1q_vlan, state.status), (200, "imported"))
        self.assertEqual((orphan.vrf, orphan.status), ("", "changed"))

    def test_edit_vlan_name_rejects_a_group_name_collision_without_writing(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOVLANState

        group = VLANGroup.objects.create(name="Collision Inline VLANs", slug="collision-inline-vlans")
        vlan = VLAN.objects.create(group=group, vid=121, name="KEEP-NAME")
        VLAN.objects.create(group=group, vid=122, name="TAKEN-NAME")
        state = NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="KEEP-NAME",
            status="imported",
        )

        response = self.client.post(self._url("vlan_name", state.pk), {"name": "TAKEN-NAME"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json()["errors"])
        vlan.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(vlan.name, "KEEP-NAME")
        self.assertEqual(state.status, "imported")

    def test_edit_vlan_name_reports_a_qinq_collision_created_after_validation(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.models import NSOVLANState

        first_group = VLANGroup.objects.create(name="First Q-in-Q Group", slug="first-qinq-group")
        second_group = VLANGroup.objects.create(name="Second Q-in-Q Group", slug="second-qinq-group")
        service_vlan = VLAN.objects.create(vid=3000, name="SERVICE", qinq_role="svlan")
        vlan = VLAN.objects.create(
            group=first_group,
            vid=121,
            name="KEEP-NAME",
            qinq_role="cvlan",
            qinq_svlan=service_vlan,
        )
        state = NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="KEEP-NAME",
            status="imported",
        )
        original_footprint = intent_state.vlan_footprint

        def resolve_then_collide(vlan_id, scopes, **kwargs):
            result = original_footprint(vlan_id, scopes, **kwargs)
            VLAN.objects.create(
                group=second_group,
                vid=122,
                name="TAKEN-NAME",
                qinq_role="cvlan",
                qinq_svlan=service_vlan,
            )
            return result

        with patch("netbox_nso_plugin.intent_state.vlan_footprint", side_effect=resolve_then_collide):
            response = self.client.post(self._url("vlan_name", state.pk), {"name": "TAKEN-NAME"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json()["errors"])
        vlan.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(vlan.name, "KEEP-NAME")
        self.assertEqual(state.status, "imported")

    def test_edit_vlan_name_reports_a_collision_created_after_validation(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.models import NSOVLANState

        group = VLANGroup.objects.create(name="Raced Inline VLANs", slug="raced-inline-vlans")
        vlan = VLAN.objects.create(group=group, vid=124, name="KEEP-NAME")
        state = NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="KEEP-NAME",
            status="imported",
        )
        original_footprint = intent_state.vlan_footprint

        def resolve_then_collide(vlan_id, scopes, **kwargs):
            result = original_footprint(vlan_id, scopes, **kwargs)
            VLAN.objects.create(group=group, vid=125, name="RACED-NAME")
            return result

        with patch("netbox_nso_plugin.intent_state.vlan_footprint", side_effect=resolve_then_collide):
            response = self.client.post(self._url("vlan_name", state.pk), {"name": "RACED-NAME"})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("name", response.json()["errors"])
        vlan.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(vlan.name, "KEEP-NAME")
        self.assertEqual(state.status, "imported")

    def test_edit_route_map_name_updates_shared_object_overlays_and_dependent_intent(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution, RouteMap

        from netbox_nso_plugin.models import (
            NSOOSPFInstanceState,
            NSORedistributionState,
            NSORoutePolicyObjectClass,
            NSORoutePolicyState,
        )

        route_map = RouteMap.objects.create(name="RM-INLINE-OLD")
        route_map_ct = ContentType.objects.get_for_model(RouteMap)
        row = NSORoutePolicyState.objects.create(
            management=self.mgmt,
            family="route_map",
            object_name=route_map.name,
            content_type=route_map_ct,
            object_id=route_map.pk,
            status="imported",
        )
        policy_class = NSORoutePolicyObjectClass.objects.create(
            family="route_map", object_name=route_map.name, mode="master"
        )
        ospf = OSPFInstance.objects.create(
            device=self.device,
            name="inline-ospf",
            process_id="7",
            router_id="192.0.2.7",
        )
        ospf_ct = ContentType.objects.get_for_model(OSPFInstance)
        redistribution = Redistribution.objects.create(
            destination_type=ospf_ct,
            destination_id=ospf.pk,
            source_protocol="connected",
            route_map=route_map,
        )
        NSOOSPFInstanceState.objects.create(
            management=self.mgmt,
            process_id="7",
            status="accepted",
        )
        NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="ospf",
            dest_ref="7",
            source_protocol="connected",
            redistribution=redistribution,
            status="accepted",
        )
        mirror_update(self.mgmt, adapter_device_id=321)

        with (
            patch("netbox_nso_plugin.adapter_client.put_route_policy_intent") as put_policy,
            patch("netbox_nso_plugin.adapter_client.put_ospf_intent") as put_ospf,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                self._url("route_map_name", row.pk),
                {"object_name": "RM-INLINE-NEW"},
            )

        self.assertEqual(response.status_code, 200, response.content)
        route_map.refresh_from_db()
        row.refresh_from_db()
        policy_class.refresh_from_db()
        self.assertEqual(route_map.name, "RM-INLINE-NEW")
        self.assertEqual(row.object_name, "RM-INLINE-NEW")
        self.assertEqual(row.status, "accepted")
        self.assertEqual(policy_class.object_name, "RM-INLINE-NEW")
        self.assertEqual(put_policy.call_args.args[1][0]["name"], "RM-INLINE-NEW")
        ospf_payload = put_ospf.call_args.args[1]
        self.assertEqual(ospf_payload["instances"][0]["redistribution"][0]["route_map"], "RM-INLINE-NEW")

    def test_edit_route_map_name_repends_every_deploying_attachment(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin import status_machine as sm
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.views import _save_route_map_name_edit

        route_map = RouteMap.objects.create(name="RM-IN-FLIGHT-OLD")
        row = NSORoutePolicyState.objects.create(
            management=self.mgmt,
            family="route_map",
            object_name=route_map.name,
            content_type=ContentType.objects.get_for_model(RouteMap),
            object_id=route_map.pk,
            status="deploying",
            apply_attempt_id=uuid4(),
        )
        _other_device, other_management = make_managed("route-map-rename", 322)
        attached = NSORoutePolicyState.objects.create(
            management=other_management,
            family="route_map",
            object_name=route_map.name,
            content_type=row.content_type,
            object_id=route_map.pk,
            status="deploying",
            apply_attempt_id=uuid4(),
        )

        row.object_name = "RM-IN-FLIGHT-NEW"
        with suppress_intent_push():
            _save_route_map_name_edit(row, route_map.name)

        row.refresh_from_db()
        attached.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNone(row.apply_attempt_id)
        self.assertEqual(attached.object_name, "RM-IN-FLIGHT-NEW")
        self.assertEqual(attached.status, "accepted")
        self.assertIsNone(attached.apply_attempt_id)
        row.status = sm.on_apply_result(row.status, ok=True)
        row.save(update_fields=["status"])
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")

    def test_edit_route_map_name_rechecks_fallback_references_after_locking(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.intent_state import intent_transaction, offline_mutation
        from netbox_nso_plugin.models import NSORedistributionState, NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.views import _save_route_map_name_edit

        route_map = RouteMap.objects.create(name="RM-RACE-OLD")
        state = NSORoutePolicyState.objects.create(
            management=self.mgmt,
            family="route_map",
            object_name=route_map.name,
            content_type=ContentType.objects.get_for_model(RouteMap),
            object_id=route_map.pk,
            status="accepted",
        )
        fallback = NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="ospf",
            source_protocol="connected",
            route_map=route_map.name,
            status="accepted",
        )

        def acquire_after_competing_edit(footprint):
            with offline_mutation():
                NSORedistributionState.objects.filter(pk=fallback.pk).update(route_map="RM-RACE-OTHER")
            return intent_transaction(footprint)

        state.object_name = "RM-RACE-NEW"
        with (
            suppress_intent_push(),
            patch("netbox_nso_plugin.intent_state.intent_transaction", side_effect=acquire_after_competing_edit),
        ):
            _save_route_map_name_edit(state, route_map.name)

        fallback.refresh_from_db()
        self.assertEqual(fallback.route_map, "RM-RACE-OTHER")

    def test_edit_route_map_name_rejects_native_name_collision_without_writing(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyState

        route_map = RouteMap.objects.create(name="RM-KEEP")
        RouteMap.objects.create(name="RM-TAKEN")
        row = NSORoutePolicyState.objects.create(
            management=self.mgmt,
            family="route_map",
            object_name=route_map.name,
            content_type=ContentType.objects.get_for_model(RouteMap),
            object_id=route_map.pk,
            status="imported",
        )

        response = self.client.post(self._url("route_map_name", row.pk), {"object_name": "rm-taken"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("object_name", response.json()["errors"])
        route_map.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(route_map.name, "RM-KEEP")
        self.assertEqual(row.object_name, "RM-KEEP")
        self.assertEqual(row.status, "imported")

    def test_noop_edit_does_not_claim_ownership(self):
        from netbox_nso_plugin.models import NSOSnmpSystemInfoState

        row = NSOSnmpSystemInfoState.objects.create(management=self.mgmt, location="same", status="imported")
        r = self.client.post(self._url("snmp_system_info", row.pk), {"location": "same"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["changed"], [])
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")  # nothing changed → nothing owned

    def test_non_whitelisted_field_rejected(self):
        from netbox_nso_plugin.models import NSOSnmpSystemInfoState

        row = NSOSnmpSystemInfoState.objects.create(management=self.mgmt, location="keep", status="imported")
        r = self.client.post(self._url("snmp_system_info", row.pk), {"status": "in_sync"})
        self.assertEqual(r.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.status, "imported")

    def test_invalid_value_rejected_with_field_error(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        row = NSOInterfaceMtuState.objects.create(
            management=self.mgmt, interface=self.interface, l2_mtu=9214, status="imported"
        )
        r = self.client.post(self._url("interface_mtu", row.pk), {"l2_mtu": "not-a-number"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("l2_mtu", r.json()["errors"])
        row.refresh_from_db()
        self.assertEqual(row.l2_mtu, 9214)

    def test_unknown_key_400(self):
        r = self.client.post(self._url("does_not_exist", 1), {"anything": "x"})
        self.assertEqual(r.status_code, 400)


class TestOverlayFieldEditViewRenameRace(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username="route-map-race-admin",
            password=TEST_PASSWORD,
            email="route-map-race@test.example",
        )
        with without_commit_drain(), transaction.atomic():
            fixtures = _make_fixtures()
        self.device = fixtures["device"]
        self.mgmt = fixtures["mgmt"]

    def test_concurrent_case_variant_route_map_insert_returns_a_field_error(self):
        from threading import Barrier, Thread

        from django.contrib.contenttypes.models import ContentType
        from django.db import close_old_connections, connections
        from django.test import Client
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin import views
        from netbox_nso_plugin.models import NSOIntentRevision, NSORoutePolicyObjectClass, NSORoutePolicyState

        with without_commit_drain(), transaction.atomic():
            route_map = RouteMap.objects.create(name="RM-RACE-SOURCE")
            state = NSORoutePolicyState.objects.create(
                management=self.mgmt,
                family="route_map",
                object_name=route_map.name,
                content_type=ContentType.objects.get_for_model(RouteMap),
                object_id=route_map.pk,
                status="imported",
            )
            policy_class = NSORoutePolicyObjectClass.objects.create(
                family="route_map",
                object_name=route_map.name,
                mode="master",
            )
        before_states = list(
            NSORoutePolicyState.objects.filter(content_type_id=state.content_type_id, object_id=route_map.pk)
            .order_by("pk")
            .values_list("object_name", "status", "accepted_at")
        )
        before_revisions = list(
            NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
        )
        url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit",
            kwargs={"key": "route_map_name", "pk": state.pk},
        )
        validation_finished = Barrier(2)
        collision_committed = Barrier(2)
        results = {}
        original_save = views._save_route_map_name_edit

        def save_after_collision(*args, **kwargs):
            validation_finished.wait(timeout=20)
            collision_committed.wait(timeout=20)
            return original_save(*args, **kwargs)

        def request_edit():
            close_old_connections()
            try:
                client = Client()
                client.force_login(User.objects.get(pk=self.superuser.pk))
                results["response"] = client.post(url, {"object_name": "RM-RACE-TARGET"})
            except Exception as exc:  # noqa: BLE001
                results["request_error"] = exc
            finally:
                connections.close_all()
                results["request_connection_closed"] = connections["default"].connection is None

        def create_collision():
            close_old_connections()
            try:
                validation_finished.wait(timeout=20)
                with without_commit_drain(), transaction.atomic():
                    RouteMap.objects.create(name="rm-race-target")
            except Exception as exc:  # noqa: BLE001
                results["collision_error"] = exc
            finally:
                collision_committed.wait(timeout=20)
                connections.close_all()
                results["collision_connection_closed"] = connections["default"].connection is None

        request_thread = Thread(target=request_edit)
        collision_thread = Thread(target=create_collision)
        with patch("netbox_nso_plugin.views._save_route_map_name_edit", side_effect=save_after_collision):
            request_thread.start()
            collision_thread.start()
            request_thread.join(timeout=30)
            collision_thread.join(timeout=30)

        self.assertFalse(request_thread.is_alive(), "the request worker did not finish")
        self.assertFalse(collision_thread.is_alive(), "the collision worker did not finish")
        self.assertTrue(results.get("request_connection_closed"), results)
        self.assertTrue(results.get("collision_connection_closed"), results)
        self.assertNotIn("collision_error", results, results.get("collision_error"))
        response = results.get("response")
        self.assertIsNotNone(response, results.get("request_error"))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("object_name", response.json()["errors"])
        route_map.refresh_from_db()
        policy_class.refresh_from_db()
        self.assertEqual(route_map.name, "RM-RACE-SOURCE")
        self.assertEqual(policy_class.object_name, "RM-RACE-SOURCE")
        self.assertEqual(
            list(
                NSORoutePolicyState.objects.filter(content_type_id=state.content_type_id, object_id=route_map.pk)
                .order_by("pk")
                .values_list("object_name", "status", "accepted_at")
            ),
            before_states,
        )
        self.assertEqual(
            list(
                NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
            ),
            before_revisions,
        )

    def test_concurrent_case_variant_classification_insert_returns_a_field_error(self):
        from threading import Barrier, Thread

        from django.contrib.contenttypes.models import ContentType
        from django.db import close_old_connections, connections
        from django.test import Client
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin import views
        from netbox_nso_plugin.models import NSOIntentRevision, NSORoutePolicyObjectClass, NSORoutePolicyState

        with without_commit_drain(), transaction.atomic():
            route_map = RouteMap.objects.create(name="RM-CLASS-RACE-SOURCE")
            state = NSORoutePolicyState.objects.create(
                management=self.mgmt,
                family="route_map",
                object_name=route_map.name,
                content_type=ContentType.objects.get_for_model(RouteMap),
                object_id=route_map.pk,
                status="imported",
            )
            policy_class = NSORoutePolicyObjectClass.objects.create(
                family="route_map",
                object_name=route_map.name,
                mode="master",
            )
        before_states = list(
            NSORoutePolicyState.objects.filter(content_type_id=state.content_type_id, object_id=route_map.pk)
            .order_by("pk")
            .values_list("object_name", "status", "accepted_at")
        )
        before_revisions = list(
            NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
        )
        url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit",
            kwargs={"key": "route_map_name", "pk": state.pk},
        )
        validation_finished = Barrier(2)
        collision_committed = Barrier(2)
        results = {}
        original_save = views._save_route_map_name_edit

        def save_after_collision(*args, **kwargs):
            validation_finished.wait(timeout=20)
            collision_committed.wait(timeout=20)
            return original_save(*args, **kwargs)

        def request_edit():
            close_old_connections()
            try:
                client = Client()
                client.force_login(User.objects.get(pk=self.superuser.pk))
                results["response"] = client.post(url, {"object_name": "RM-CLASS-RACE-TARGET"})
            except Exception as exc:  # noqa: BLE001
                results["request_error"] = exc
            finally:
                connections.close_all()
                results["request_connection_closed"] = connections["default"].connection is None

        def create_collision():
            close_old_connections()
            try:
                validation_finished.wait(timeout=20)
                with without_commit_drain(), transaction.atomic():
                    NSORoutePolicyObjectClass.objects.create(
                        family="route_map",
                        object_name="rm-class-race-target",
                        mode="local",
                    )
            except Exception as exc:  # noqa: BLE001
                results["collision_error"] = exc
            finally:
                collision_committed.wait(timeout=20)
                connections.close_all()
                results["collision_connection_closed"] = connections["default"].connection is None

        request_thread = Thread(target=request_edit)
        collision_thread = Thread(target=create_collision)
        with patch("netbox_nso_plugin.views._save_route_map_name_edit", side_effect=save_after_collision):
            request_thread.start()
            collision_thread.start()
            request_thread.join(timeout=30)
            collision_thread.join(timeout=30)

        self.assertFalse(request_thread.is_alive(), "the request worker did not finish")
        self.assertFalse(collision_thread.is_alive(), "the collision worker did not finish")
        self.assertTrue(results.get("request_connection_closed"), results)
        self.assertTrue(results.get("collision_connection_closed"), results)
        self.assertNotIn("collision_error", results, results.get("collision_error"))
        response = results.get("response")
        self.assertIsNotNone(response, results.get("request_error"))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("object_name", response.json()["errors"])
        route_map.refresh_from_db()
        policy_class.refresh_from_db()
        self.assertEqual(route_map.name, "RM-CLASS-RACE-SOURCE")
        self.assertEqual(policy_class.object_name, "RM-CLASS-RACE-SOURCE")
        self.assertEqual(
            NSORoutePolicyObjectClass.objects.filter(
                family="route_map",
                object_name__iexact="RM-CLASS-RACE-TARGET",
            ).count(),
            1,
        )
        self.assertEqual(
            list(
                NSORoutePolicyState.objects.filter(content_type_id=state.content_type_id, object_id=route_map.pk)
                .order_by("pk")
                .values_list("object_name", "status", "accepted_at")
            ),
            before_states,
        )
        self.assertEqual(
            list(
                NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
            ),
            before_revisions,
        )


class TestPagedCategoryQuickSelect(ViewTestBase):
    """Behavioral guard: EVERY paged category in the device NSO tab renders the
    drift/pending/in-sync quick-select. Driven by the view's own paged-category spec,
    so adding a paged category without the filter (or removing the shared pills) fails
    here — the 'route-policy is missing the quick-select, fix category by category'
    regression. This would have failed before the pills were centralized in _cat_search.
    """

    def test_every_paged_category_renders_state_pills(self):
        from netbox_nso_plugin.views import NSOCategoryView

        keys = list(NSOCategoryView()._paged_category_specs().keys())
        self.assertIn("vlan", keys)  # guard the introspection found the specs (route_policy is a grid now)
        for key in keys:
            url = reverse(
                "plugins:netbox_nso_plugin:device_nso_category",
                kwargs={"pk": self.device.pk, "key": key},
            )
            resp = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertEqual(resp.status_code, 200, key)
            # The quick-select pills (rendered by the shared _cat_search) must be present,
            # even with zero rows (counts all=0) — so no category can ship without them.
            self.assertContains(resp, 'data-cat-state="drift"', msg_prefix=key)
            self.assertContains(resp, 'data-cat-state="pending"', msg_prefix=key)
            self.assertContains(resp, 'data-cat-state="in_sync"', msg_prefix=key)


class TestCategoryQuickSelectStructure(SimpleTestCase):
    """Fast structural backstop (no DB): any category template that renders per-row
    status (data-status) must also pull in a quick-select provider, so a new/edited
    category template can't omit it.
    """

    # A category provides the quick-select via one of these (paged: _cat_search pills;
    # non-paged: _table_filter; the bespoke interface views: nso-ifm/if-state).
    _FILTER_MARKERS = ("_table_filter.html", "_cat_search.html", "nso-ifm-state", "nso-if-state")

    def _category_dir(self):
        from pathlib import Path

        import netbox_nso_plugin

        return Path(netbox_nso_plugin.__file__).parent / "templates" / "netbox_nso_plugin" / "categories"

    def test_status_tables_include_a_quick_select(self):
        offenders = []
        for path in sorted(self._category_dir().glob("*.html")):
            if path.name.startswith("_"):  # shared partials are not standalone categories
                continue
            text = path.read_text(encoding="utf-8")
            if "data-status=" not in text:
                continue  # no status table → nothing to filter
            if not any(marker in text for marker in self._FILTER_MARKERS):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"category templates render status rows without a quick-select: {offenders}")


class TestSharedObjectVersionsAndMaterialize(ViewTestBase):
    """The operator-facing 'show every device's version + pick which to own' flow.

    End-to-end through the real reconciler, real netbox_routing models, and the real
    views — so the universal shared-object ownership UX (route-policy today, ACL later)
    is exercised the way an operator drives it.
    """

    def _second_mgmt(self):
        d2 = Device.objects.create(
            name="view-router-02", device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        mgmt2 = NSODeviceManagement.objects.create(
            device=d2, nso_instance=self.nso_instance, nso_device_name="view-router-02", adapter_device_id=2
        )
        return d2, mgmt2

    def _pl(self, prefixes):
        entries = [{"sequence": 10 * (i + 1), "action": "permit", "prefix": p} for i, p in enumerate(prefixes)]
        return {"prefix_lists": [{"name": "PL-VIEW", "entries": entries}]}

    def _seed_divergent(self):
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self.mgmt.adapter_device_id = 1
        self.mgmt.save(update_fields=["adapter_device_id"])
        d2, _ = self._second_mgmt()
        reconcile_route_policy(self.device, self._pl(["10.0.0.0/8"]))
        reconcile_route_policy(d2, self._pl(["10.0.0.0/8", "192.168.0.0/16"]))
        return d2

    def _http_or_skip(self, fn):
        """Run an HTTP/URL-resolving call, skipping on the broken-librenms env fault.

        The sibling netbox-librenms-plugin (mid-rebase in this devcontainer) fails to import
        its urls.py (ImportError: PortStackLagPattern) / lists a dangling nav URL
        (NoReverseMatch: portstacklagpattern_list), which breaks Django URL resolution for
        EVERY plugin — unrelated to this feature. Skip rather than report a false failure;
        these run for real in a healthy environment / CI. The behaviour itself is covered by
        the data-level test above and the reconciler-level rematerialize test."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — narrow by message below, re-raise otherwise
            msg = str(exc).lower()
            if "portstacklag" in msg or "librenms" in msg:
                self.skipTest("devcontainer librenms plugin breaks URL resolution; env fault")
            raise

    def test_versions_lists_every_device_owner_first(self):
        """The versions surface enumerates every device's version, owner first, flagging
        which match the materialized one and which diverge.

        Asserts the view's data assembly directly (shared_object_ownership.version_items),
        not a full-page render: the full NetBox nav transitively reverses a librenms plugin
        URL that is broken in this devcontainer (NoReverseMatch portstacklagpattern_list),
        an environment fault unrelated to this feature."""
        from netbox_nso_plugin import shared_object_ownership as ownership
        from netbox_nso_plugin.models import NSORoutePolicyState

        d2 = self._seed_divergent()
        items = ownership.version_items(NSORoutePolicyState, "prefix_list", "PL-VIEW")
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["is_owner"])  # owner sorted first
        self.assertEqual(items[0]["device"], self.device)  # d1 imported first → owner
        self.assertEqual({it["device"] for it in items}, {self.device, d2})
        d2_item = next(it for it in items if it["device"] == d2)
        self.assertFalse(d2_item["is_owner"])
        self.assertFalse(d2_item["matches_owner"])  # divergent content
        self.assertEqual(d2_item["entry_count"], 2)

    def _seed_divergent_cl(self):
        """Two devices report the SAME-named community-list with DIFFERENT members."""
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self.mgmt.adapter_device_id = 1
        self.mgmt.save(update_fields=["adapter_device_id"])
        d2, _ = self._second_mgmt()

        def cl(members):
            entries = [{"sequence": 10 * (i + 1), "action": "permit", "community": m} for i, m in enumerate(members)]
            return {"community_lists": [{"name": "CL-VIEW", "entries": entries}]}

        reconcile_route_policy(self.device, cl(["65000:1", "65000:2"]))  # owner (imported first)
        reconcile_route_policy(d2, cl(["65000:1"]))  # divergent — missing 65000:2
        return d2

    def test_versions_community_list_diverges_when_spec_registered(self):
        """With the family spec registered (the normal app state after the ready()-time
        import), a community-list whose sibling has different members is correctly reported
        as NOT matching the owner — the real example-comm scenario (7 vs 9 members)."""
        from netbox_nso_plugin import shared_object_ownership as ownership
        from netbox_nso_plugin.models import NSORoutePolicyState

        d2 = self._seed_divergent_cl()
        items = ownership.version_items(NSORoutePolicyState, "community_list", "CL-VIEW")
        sib = next(it for it in items if it["device"] == d2)
        self.assertTrue(sib["comparable"])
        self.assertFalse(sib["matches_owner"])  # genuinely different members → diverges
        self.assertEqual(sib["entry_count"], 1)

    def test_versions_never_false_match_when_spec_unregistered(self):
        """Regression for the import-order trap: a web worker that renders the versions page
        before the reconciler module loaded has an EMPTY spec registry, so hash_captured
        returns '' for every row. version_items must then report 'not comparable' — never a
        false 'matches' that makes divergent content look in-sync (the bug that showed the
        7-member ra1 version as 'matches' the 9-member owner)."""
        from unittest.mock import patch

        from netbox_nso_plugin import shared_object_ownership as ownership
        from netbox_nso_plugin.models import NSORoutePolicyState

        d2 = self._seed_divergent_cl()
        with patch.dict(ownership._REGISTRY, {}, clear=True):  # simulate unregistered specs
            items = ownership.version_items(NSORoutePolicyState, "community_list", "CL-VIEW")
        sib = next(it for it in items if it["device"] == d2)
        self.assertFalse(sib["matches_owner"])  # MUST NOT false-match divergent content
        self.assertFalse(sib["comparable"])  # no spec → no honest basis to compare

    def test_versions_page_renders_when_nav_available(self):
        """The versions page renders (guards the template + URL wiring).

        Skips on the broken-librenms env fault (see _http_or_skip); the data-level test
        above already covers the behaviour."""
        from netbox_nso_plugin.models import NSORoutePolicyState

        self._seed_divergent()
        row = NSORoutePolicyState.objects.get(management__device=self.device, object_name="PL-VIEW")

        def _do():
            url = reverse("plugins:netbox_nso_plugin:routing_route_policy_versions", args=[row.pk])
            return self.client.get(url)

        resp = self._http_or_skip(_do)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Use this version", resp.content.decode())

    def test_materialize_repoints_ownership_via_view(self):
        """POSTing the materialize action re-points ownership end-to-end through the view.

        Skips on the broken-librenms env fault (see _http_or_skip); the re-point behaviour
        itself is also covered without URLs by test_rematerialize_repoints_ownership."""
        from netbox_routing.models import PrefixList, PrefixListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState

        d2 = self._seed_divergent()
        s2 = NSORoutePolicyState.objects.get(management__device=d2, object_name="PL-VIEW")
        self.assertEqual(s2.status, "conflict")  # second device diverges from the owner

        def _do():
            url = reverse("plugins:netbox_nso_plugin:routing_materialize_route_policy", args=[s2.pk])
            return self.client.post(url)

        resp = self._http_or_skip(_do)
        self.assertEqual(resp.status_code, 302)

        pl = PrefixList.objects.get(name="PL-VIEW")
        self.assertEqual(PrefixListEntry.objects.filter(prefix_list=pl).count(), 2)  # now d2's content
        s1 = NSORoutePolicyState.objects.get(management__device=self.device, object_name="PL-VIEW")
        s2.refresh_from_db()
        self.assertTrue(s2.is_materialized)
        self.assertFalse(s1.is_materialized)
        self.assertEqual(s1.status, "conflict")


class TestDeviceNSOTabCapability(ViewTestBase):
    """The NSO tab surfaces the device_capability matrix's unsupported/skipped scopes (I2)."""

    def _url(self):
        return reverse("dcim:device_nso", kwargs={"pk": self.device.pk})

    def _set_adapter_id(self, aid=16):
        self.mgmt.adapter_device_id = aid
        self.mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client.get_device", return_value={})
    def test_capability_gaps_render_in_nso_tab(self, _mock_dev):
        self._set_adapter_id()
        cap = {
            "known": True,
            "ned_id": "cisco-ios-cli-6.114",
            "sw_version": "17.15",
            "elements": [
                {"scope": "static_route", "name": "static_route", "status": "unsupported", "detail": "NED rejected"},
                {"scope": "bgp", "name": "bgp", "status": "native", "detail": ""},
            ],
        }
        with patch("netbox_nso_plugin.adapter_client.get_device_capability", return_value=cap):
            resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "mdi-alert-octagon-outline")  # the capability panel rendered
        self.assertContains(resp, "static_route")
        self.assertContains(resp, "text-bg-danger")  # unsupported badge
        # the supported (native) scope is NOT listed as a gap
        self.assertNotContains(resp, "bgp</strong>")

    @patch("netbox_nso_plugin.adapter_client.get_device", return_value={})
    def test_no_panel_when_all_supported(self, _mock_dev):
        self._set_adapter_id()
        cap = {
            "known": True,
            "ned_id": "x",
            "sw_version": "y",
            "elements": [{"scope": "bgp", "name": "bgp", "status": "native", "detail": ""}],
        }
        with patch("netbox_nso_plugin.adapter_client.get_device_capability", return_value=cap):
            resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "mdi-alert-octagon-outline")

    @patch("netbox_nso_plugin.adapter_client.get_device", return_value={})
    def test_read_rows_are_not_listed_as_apply_gaps(self, _mock_dev):
        """source='read' rows describe read-support (mirror completeness, H3), not rejected
        applies — the tab's 'won't apply / a prior apply was rejected' banner must not caption
        them as apply rejections. They render on the capabilities page instead."""
        self._set_adapter_id()
        cap = {
            "known": True,
            "ned_id": "arcos-v8.1.2X-nc-1.0",
            "sw_version": "",
            "elements": [
                {
                    "scope": "vlan",
                    "name": "read",
                    "status": "skipped",
                    "detail": "not applicable on this platform",
                    "source": "read",
                },
                {
                    "scope": "bgp",
                    "name": "read",
                    "status": "unsupported",
                    "detail": "expected read but got empty",
                    "source": "read",
                },
            ],
        }
        with patch("netbox_nso_plugin.adapter_client.get_device_capability", return_value=cap):
            resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "mdi-alert-octagon-outline")


class TestNSOAreaTabs(ViewTestBase):
    """The Settings/Links area screens render a cross-page tab bar (inc/_settings_tabs.html and
    inc/_links_tabs.html) with the current tab active. Exercises the real request -> view ->
    template render, so it catches a renamed URL, a broken partial, or a dropped template_name."""

    def _norm(self, url_name, args=None):
        import re

        resp = self.client.get(reverse(f"plugins:netbox_nso_plugin:{url_name}", args=args))
        self.assertEqual(resp.status_code, 200)
        return re.sub(r"\s+", " ", resp.content.decode())

    def _url(self, url_name):
        return reverse(f"plugins:netbox_nso_plugin:{url_name}")

    def test_settings_list_renders_all_settings_tabs_with_active(self):
        html = self._norm("nsoinstance_list")
        for label in ("NSO Instances", "Adapter Connection", "Failover Settings", "Derived Intent", "NED Mappings"):
            self.assertIn(label, html)
        # the four tabs link to their screens, and the current one is active
        self.assertIn(f'nav-link active" href="{self._url("nsoinstance_list")}">NSO Instances', html)
        self.assertIn(f'href="{self._url("adapterconnection")}">Adapter Connection', html)
        self.assertIn(f'href="{self._url("nsofailoversettings")}">Failover Settings', html)

    def test_settings_edit_view_also_renders_tabs_with_active(self):
        """The singleton config EDIT screens (Adapter Connection / Failover) carry the tabs too."""
        html = self._norm("adapterconnection")
        self.assertIn(f'nav-link active" href="{self._url("adapterconnection")}">Adapter Connection', html)
        self.assertIn(">NSO Instances</a>", html)  # sibling tab present

    def test_settings_crud_pages_keep_area_tabs(self):
        """Entering an instance form/detail/delete must not drop the Settings navigation."""
        deletable = NSOInstance.objects.create(name="nav-delete", adapter_instance_id="nav-delete")
        pages = (
            ("nsoinstance_add", None),
            ("nsoinstance", [self.nso_instance.pk]),
            ("nsoinstance_edit", [self.nso_instance.pk]),
            ("nsoinstance_delete", [deletable.pk]),
            ("nsoderivedintenttemplate_add", None),
            ("nsoplatformnedmapping_add", None),
        )
        for url_name, args in pages:
            with self.subTest(url_name=url_name):
                html = self._norm(url_name, args)
                self.assertIn(">NSO Instances</a>", html)
                self.assertIn(">Adapter Connection</a>", html)
                self.assertIn(">Platform &rarr; NED Mappings</a>", html)

    def test_links_list_renders_all_links_tabs_with_active(self):
        html = self._norm("nsolinkrole_list")
        for label in ("Link Roles", "Link Assignments", "Interface Drift"):
            self.assertIn(label, html)
        self.assertIn(f'nav-link active" href="{self._url("nsolinkrole_list")}">Link Roles', html)

    def test_link_assignments_active_without_lighting_up_link_roles(self):
        """'nsolinkrole' is a prefix of 'nsolinkroleassignment' — the Link Roles tab must NOT be
        active on the Link Assignments page (the partial explicitly excludes the assignment views)."""
        html = self._norm("nsolinkroleassignment_list")
        self.assertIn(f'nav-link active" href="{self._url("nsolinkroleassignment_list")}">Link Assignments', html)
        self.assertIn(f'nav-link" href="{self._url("nsolinkrole_list")}">Link Roles', html)
        self.assertNotIn(f'nav-link active" href="{self._url("nsolinkrole_list")}">Link Roles', html)

    def test_links_crud_pages_keep_area_tabs(self):
        """Entering a role form/detail/delete or drift detail must retain sibling navigation."""
        from netbox_nso_plugin.models import NSOLinkRole

        role = NSOLinkRole.objects.create(name="Nav role", slug="nav-role", assign_ipv4=False)
        pages = (
            ("nsolinkrole_add", None),
            ("nsolinkrole", [role.pk]),
            ("nsolinkrole_edit", [role.pk]),
            ("nsolinkrole_delete", [role.pk]),
            ("nsolinkroleassignment_add", None),
            ("nsointerfacestate", [self.iface_state.pk]),
        )
        for url_name, args in pages:
            with self.subTest(url_name=url_name):
                html = self._norm(url_name, args)
                self.assertIn(">Link Roles</a>", html)
                self.assertIn(">Link Assignments</a>", html)
                self.assertIn(">Interface Drift</a>", html)

    def test_bulk_delete_pages_keep_area_tabs(self):
        from netbox_nso_plugin.models import NSOLinkRole

        role = NSOLinkRole.objects.create(name="Bulk nav role", slug="bulk-nav-role", assign_ipv4=False)
        pages = (
            ("nsoinstance_bulk_delete", self.nso_instance.pk, ">Adapter Connection</a>"),
            ("nsolinkrole_bulk_delete", role.pk, ">Link Assignments</a>"),
            ("nsointerfacestate_bulk_delete", self.iface_state.pk, ">Interface Drift</a>"),
        )
        for url_name, pk, sibling in pages:
            with self.subTest(url_name=url_name):
                response = self.client.post(self._url(url_name), {"pk": pk})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, sibling)

    def test_area_tabs_use_presentation_role_not_a_fake_tablist(self):
        """The area nav is cross-page NAVIGATION, not an in-page tab widget. NetBox marks the same
        pattern (its object sub-page tabs, generic/object.html) role='presentation'; our bar must too.
        role='tablist' would stack a second, FAKE tablist over the native Results/Filters tablist —
        announcing a "tab list" to assistive tech for links that navigate away and implement none of
        the tablist keyboard/panel semantics. The area <ul> is identified by its mb-3 spacing class,
        so this never touches the native Results tablist (class 'nav nav-tabs', no mb-3)."""
        for url_name in ("nsoinstance_list", "nsolinkrole_list"):
            html = self._norm(url_name)
            self.assertIn('class="nav nav-tabs mb-3" role="presentation"', html)
            self.assertNotIn('class="nav nav-tabs mb-3" role="tablist"', html)


class TestNSOAdapterLinkRetryView(ViewTestBase):
    """The retry-link action re-fires sync_scope_to_adapter (via a re-save) so an operator can
    recover a device that failed to onboard/link to the adapter, and refreshes adapter_link_error
    for the tab banner. Exercises the real request -> view -> signal -> row update."""

    _MOD = "netbox_nso_plugin.adapter_client"

    def _url(self):
        return reverse("plugins:netbox_nso_plugin:nsodevicemanagement_link_retry", args=[self.mgmt.pk])

    def test_retry_success_clears_error_and_links(self):
        mirror_update(
            self.mgmt,
            adapter_device_id=None,
            adapter_link_error="prior fail",
            onboard_status="",
        )
        with (
            patch(f"{self._MOD}.onboard_device", return_value={"id": 321}),
            patch(f"{self._MOD}.set_scope", return_value={}),
            patch(f"{self._MOD}.sync_notify", return_value=None),
            # the link retry pushes via transaction.on_commit — TestCase rolls back
            self.captureOnCommitCallbacks(execute=True),
        ):
            resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)  # redirect to the NSO tab
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_link_error, "")  # cleared on success
        self.assertEqual(self.mgmt.adapter_device_id, 321)  # now linked

    def test_retry_relinks_a_device_the_adapter_no_longer_knows(self):
        """A dead adapter_device_id is re-onboarded by the retry, not just re-reported.

        The adapter's device row can vanish under a live management row (a provision that
        rolled back, a manual delete, a restored DB). The link path then skips onboarding —
        the id is set, so ``created or adapter_device_id is None`` is False — and every scope
        push 404s against the dead id, so the banner's own Retry button could never heal it.
        """
        mirror_update(
            self.mgmt,
            adapter_device_id=196,
            adapter_link_error="Device not found",
            onboard_status="",
        )
        scope_calls = []

        def fake_set_scope(adapter_device_id, *args, **kwargs):
            scope_calls.append(adapter_device_id)
            if adapter_device_id == 196:
                raise AdapterError("Device not found", code="not_found")
            return {}

        with (
            patch(f"{self._MOD}.onboard_device", return_value={"id": 700}),
            patch(f"{self._MOD}.set_scope", side_effect=fake_set_scope),
            patch(f"{self._MOD}.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            resp = self.client.post(self._url())

        self.assertEqual(resp.status_code, 302)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_device_id, 700)  # re-onboarded onto a live row
        self.assertEqual(self.mgmt.adapter_link_error, "")  # healed, banner goes away
        self.assertEqual(scope_calls, [196, 700])  # dead id tried, then the fresh one

    def test_retry_does_not_reonboard_on_a_transient_scope_failure(self):
        """Only a not-found scope push means the id is dead — an outage must not re-onboard.

        Re-onboarding on any error would mint a second adapter device row for a device that
        already has a live one every time the adapter was briefly unreachable.
        """
        mirror_update(
            self.mgmt,
            adapter_device_id=196,
            adapter_link_error="",
            onboard_status="",
        )
        with (
            patch(f"{self._MOD}.onboard_device") as onboard,
            patch(f"{self._MOD}.set_scope", side_effect=AdapterError("adapter down", code="nso_unreachable")),
            self.captureOnCommitCallbacks(execute=True),
        ):
            resp = self.client.post(self._url())

        self.assertEqual(resp.status_code, 302)
        onboard.assert_not_called()
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_device_id, 196)  # mapping left alone
        self.assertEqual(self.mgmt.adapter_link_error, "The NSO adapter request failed. See the server log.")

    def test_retry_failure_refreshes_error(self):
        mirror_update(
            self.mgmt,
            adapter_device_id=None,
            adapter_link_error="",
            onboard_status="",
        )
        with (
            patch(f"{self._MOD}.onboard_device", side_effect=AdapterError("still down", code="nso_unreachable")),
            self.captureOnCommitCallbacks(execute=True),  # deferred push records the error
        ):
            resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_link_error, "The NSO adapter request failed. See the server log.")
        self.assertIsNone(self.mgmt.adapter_device_id)  # still unlinked


class TestNSOIntentResyncView(ViewTestBase):
    def test_unexpected_failure_uses_a_fixed_public_message(self):
        from django.contrib.messages import get_messages

        supplied = "Traceback: private resync path"
        mirror_update(self.mgmt, adapter_device_id=196)
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_intent_resync", args=[self.mgmt.pk])

        with patch("netbox_nso_plugin.intent_drift.resync_intent", side_effect=RuntimeError(supplied)):
            response = self.client.post(url)

        rendered = " ".join(str(message) for message in get_messages(response.wsgi_request))
        self.assertEqual(response.status_code, 302)
        self.assertIn("Intent re-sync failed. See the server log.", rendered)
        self.assertNotIn(supplied, rendered)


class TestBfdGrid(ViewTestBase):
    """The BFD category renders as a client-side grid (nso-grid.js).

    BFD owns the whole ROW — one status, one accept_url — unlike Interfaces, where each
    attribute is its own overlay row with its own Accept. These assert the payload the
    grid actually consumes: the kinds the badges/quick-filter buckets are drawn from,
    and Accept visibility.
    """

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        self.ifaces = {}
        for name in ("Gi0/1", "Gi0/2", "Gi0/3"):
            self.ifaces[name] = Interface.objects.create(device=self.device, name=name, type="1000base-t")
        # changed + unowned -> drift; accepted -> owned -> pending; in_sync -> in sync.
        NSOBFDInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.ifaces["Gi0/1"],
            status="changed",
            min_tx=300,
            min_rx=300,
            multiplier=3,
        )
        NSOBFDInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.ifaces["Gi0/2"],
            status="accepted",
            min_tx=50,
            min_rx=50,
            multiplier=3,
            micro_bfd=True,
        )
        NSOBFDInterfaceState.objects.create(
            management=self.mgmt,
            interface=self.ifaces["Gi0/3"],
            status="in_sync",
            min_tx=100,
            min_rx=100,
            multiplier=5,
        )

    def _url(self):
        return reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "bfd"},
        )

    def test_json_reload_serves_rows_without_touching_the_adapter(self):
        """?format=json is the grid's post-action reload — it must read persisted state.

        Nothing here is patched: if the reload reconciled (as the on-expand fragment
        does), it would hit the adapter and blow up rather than return rows. That is
        the whole point of serving this path from the DB — an Accept click would
        otherwise trigger a fresh device read per click.
        """
        resp = self.client.get(self._url() + "?format=json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)

        self.assertEqual(data["counts"], {"all": 3, "drift": 1, "pending": 1})
        by_iface = {r["iface"]["name"]: r for r in data["rows"]}
        self.assertEqual(set(by_iface), {"Gi0/1", "Gi0/2", "Gi0/3"})

        # The kind drives the badge AND the quick-filter bucket — assert it, not the status.
        self.assertEqual(by_iface["Gi0/1"]["state"], "drift")
        self.assertEqual(by_iface["Gi0/2"]["state"], "pending")
        self.assertEqual(by_iface["Gi0/3"]["state"], "in_sync")

        # Accept shows only for a not-yet-owned row (mirrors _accept_cell.html).
        self.assertTrue(by_iface["Gi0/1"]["accept_url"])
        self.assertIsNone(by_iface["Gi0/2"]["accept_url"])
        self.assertIsNone(by_iface["Gi0/3"]["accept_url"])

        # Timers + mode survive the round trip.
        self.assertEqual(by_iface["Gi0/1"]["min_tx"], 300)
        self.assertEqual(by_iface["Gi0/1"]["multiplier"], 3)
        self.assertTrue(by_iface["Gi0/2"]["micro_bfd"])
        self.assertFalse(by_iface["Gi0/1"]["micro_bfd"])
        self.assertIn(f"/overlay/bfd/{by_iface['Gi0/1']['pk']}/edit-field/", by_iface["Gi0/1"]["edit_url"])

    def test_accept_url_actually_resolves_and_takes_ownership(self):
        """The accept_url the grid ships must be a real, working endpoint.

        A grid that renders a dead Accept button looks identical to a working one until
        an operator clicks it, so drive the URL from the payload end-to-end.
        """
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        resp = self.client.get(self._url() + "?format=json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        row = next(r for r in json.loads(resp.content)["rows"] if r["iface"]["name"] == "Gi0/1")

        with self.captureOnCommitCallbacks(execute=True):
            accepted = self.client.post(row["accept_url"])
        self.assertIn(accepted.status_code, (200, 302))

        st = NSOBFDInterfaceState.objects.get(pk=row["pk"])
        self.assertIsNotNone(st.accepted_at)  # NetBox now owns the timers

        # ... and the reloaded grid reflects it: owned -> pending, Accept gone.
        again = self.client.get(self._url() + "?format=json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = json.loads(again.content)
        row2 = next(r for r in data["rows"] if r["iface"]["name"] == "Gi0/1")
        self.assertEqual(row2["state"], "pending")
        self.assertIsNone(row2["accept_url"])
        self.assertEqual(data["counts"]["drift"], 0)
        self.assertEqual(data["counts"]["pending"], 2)

    @patch("netbox_nso_plugin.adapter_client.get_bfd", return_value={"interfaces": []})
    def test_fragment_embeds_the_grid_payload(self, _mock_bfd):
        """The on-expand fragment paints from an embedded payload — no second request."""
        resp = self.client.get(self._url(), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="nso-bfd-data"', body)  # json_script payload
        self.assertIn("nso-grid-state", body)  # quick-filter pills
        self.assertIn("nso-grid-table", body)  # grid mount point
        self.assertIn("NSOGridBfd.mount", body)
        self.assertNotIn("{#", body)  # no leaked multi-line Django comment

    def test_unlinked_device_still_shows_persisted_rows(self):
        """An unlinked device (no adapter_device_id) must not read as an EMPTY category.

        reconcile_category returns an empty context when there is no adapter_device_id,
        so a panel keyed off that context renders "No BFD-configured interfaces" while
        NetBox is holding rows for them — the operator sees their accepted BFD timers
        vanish the moment the device is unlinked. The grid renders persisted state
        instead (the Interfaces grid already worked this way).
        """
        self.assertIsNone(self.mgmt.adapter_device_id)  # the precondition under test

        resp = self.client.get(self._url(), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()

        # Not the empty-state <p>. (Bare-substring matching would pass either way: the
        # same wording is also the grid's Tabulator placeholder, so it appears even on a
        # fully-populated grid.)
        self.assertNotIn('<p class="text-muted mb-0">No BFD-configured', body)

        # The embedded payload really carries the persisted rows.
        embedded = json.loads(re.search(r'<script id="nso-bfd-data"[^>]*>(.*?)</script>', body, re.S).group(1))
        self.assertEqual({r["iface"]["name"] for r in embedded["rows"]}, {"Gi0/1", "Gi0/2", "Gi0/3"})
        self.assertEqual(embedded["counts"], {"all": 3, "drift": 1, "pending": 1})


class TestGridCategoryPayloads(ViewTestBase):
    """Structural guard for every client-side grid category (nso-grid.js).

    Driven by the view's OWN _GRID_CATEGORIES / _grid_specs, so a category added to the
    grid path without a working payload fails here rather than at an operator's expand.
    """

    def _url(self, key):
        return reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": key},
        )

    def test_json_reload_serves_every_grid_category_without_the_adapter(self):
        """?format=json is the post-action reload — it must come from persisted state.

        Nothing is patched: the grid re-fetches after every Accept, so a category that
        reconciled here would hit the adapter once per click. If any of them starts
        reconciling, this blows up instead of quietly costing a device read per click.
        """
        from netbox_nso_plugin.views import NSOCategoryView

        view = NSOCategoryView()
        specs = view._grid_specs()
        self.assertTrue(view._GRID_CATEGORIES)  # guard the introspection found something

        for key in view._GRID_CATEGORIES:
            with self.subTest(category=key):
                resp = self.client.get(self._url(key) + "?format=json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(resp.status_code, 200)
                data = json.loads(resp.content)

                for name in specs[key]["sections"]:
                    body = data if name is None else data[name]
                    self.assertIn("rows", body)
                    # The pills filter on these three buckets; a missing one silently
                    # renders an empty chip.
                    self.assertEqual(set(body["counts"]), {"all", "drift", "pending"})

    def test_fragment_renders_for_every_grid_category(self):
        """Each grid fragment paints from an embedded payload — no second request."""
        from netbox_nso_plugin.views import NSOCategoryView

        for key in NSOCategoryView()._GRID_CATEGORIES:
            with self.subTest(category=key):
                resp = self.client.get(self._url(key), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(resp.status_code, 200)
                # A multi-line {# #} comment renders as literal text (the CR-P16 leak).
                self.assertNotIn("{#", resp.content.decode())


class TestUnlinkedReconcileOnExpandCategories(ViewTestBase):
    """The six reconcile-on-expand categories must render PERSISTED rows when the
    reconcile cannot run — an unlinked device (adapter_device_id is None) or a dead
    adapter. reconcile_category yields an empty context in both cases, and a panel
    keyed off that context claims "nothing configured" while NetBox is holding rows:
    the operator watches their accepted config vanish. The grids already render from
    persisted state (TestBfdGrid.test_unlinked_device_still_shows_persisted_rows);
    these six were left on the old path.
    """

    # category -> (row marker that must render, empty-state text that must not)
    CASES = {
        "interface_ips": ("192.0.2.5/30", "No interface IP addresses reported"),
        "interface_mtu": ("9111", "No interface MTU reported"),
        "lacp": ("Po1", "No LACP bundles reported"),
        "logging": ("192.0.2.99", "No remote syslog servers configured"),
        "snmp": ("abcd1234abcd1234", "No SNMP configuration"),
        "switchport": ("Gi0/11", "No switchports reported"),
    }

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from netbox_nso_plugin.models import (
            NSOInterfaceIPState,
            NSOInterfaceMtuState,
            NSOLACPBundleState,
            NSOLACPMemberState,
            NSOLoggingHostState,
            NSOSnmpCommunityState,
            NSOSwitchportState,
        )

        cls.gi = Interface.objects.create(device=cls.device, name="Gi0/11", type="1000base-t")
        cls.lag = Interface.objects.create(device=cls.device, name="Po1", type="lag")
        NSOInterfaceIPState.objects.create(interface=cls.gi, address="192.0.2.5/30", family="ipv4", status="imported")
        NSOInterfaceMtuState.objects.create(management=cls.mgmt, interface=cls.gi, l2_mtu=9111, status="imported")
        NSOLACPBundleState.objects.create(
            management=cls.mgmt, interface=cls.lag, lag_id=1, min_links=2, status="imported"
        )
        NSOLACPMemberState.objects.create(
            management=cls.mgmt, interface=cls.gi, lag_bundle=cls.lag, mode="active", status="imported"
        )
        NSOLoggingHostState.objects.create(
            management=cls.mgmt, address="192.0.2.99", severity="warning", status="imported"
        )
        NSOSnmpCommunityState.objects.create(
            management=cls.mgmt, community_hash="abcd1234abcd1234", access="RO", status="imported"
        )
        NSOSwitchportState.objects.create(management=cls.mgmt, interface=cls.gi, mode="access", status="imported")

    def _get(self, key):
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": key})
        return self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_unlinked_device_renders_persisted_rows_not_empty(self):
        self.assertIsNone(self.mgmt.adapter_device_id)  # the precondition under test

        for key, (marker, empty_msg) in self.CASES.items():
            with self.subTest(category=key):
                resp = self._get(key)
                self.assertEqual(resp.status_code, 200)
                body = resp.content.decode()
                self.assertNotIn(empty_msg, body)
                self.assertIn(marker, body)

    def test_lacp_members_render_under_their_bundle(self):
        """The member sub-loop reads a reverse relation off the LAG interface — a
        DB-only rebuild must reach it (bundle -> interface -> member rows)."""
        self.assertIsNone(self.mgmt.adapter_device_id)
        body = self._get("lacp").content.decode()
        self.assertIn("Gi0/11", body)  # the member row, not just the bundle

    def test_lacp_grid_rolls_member_drift_into_bundle_state_and_accept_action(self):
        """A nested member is part of the bundle row, so its drift must drive that row."""
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOLACPMemberState

        content_bulk_update(
            NSOLACPBundleState.objects.get(management=self.mgmt, interface=self.lag),
            status="in_sync",
        )
        for member in NSOLACPMemberState.objects.filter(management=self.mgmt, lag_bundle=self.lag):
            content_bulk_update(member, status="changed")

        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "lacp"},
        )
        response = self.client.get(url + "?format=json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counts"], {"all": 1, "drift": 1, "pending": 0})
        self.assertEqual(payload["rows"][0]["state"], "drift")
        self.assertIsNotNone(payload["rows"][0]["accept_url"])

    def test_accept_refused_for_vpc_sensitive_lacp_bundle(self):
        """NX-P2: a vPC-protected LACP bundle cannot be onboarded — Accept is refused and the
        row stays unowned (the lag-reconciler refuses a vPC bundle zero-write; adopting a vPC
        peer-link and retracting would delete it -> dual-active split-brain)."""
        from netbox_nso_plugin.models import NSOLACPBundleState

        state = NSOLACPBundleState.objects.get(management=self.mgmt, interface=self.lag)
        state.vpc_sensitive = True
        state.save(update_fields=["vpc_sensitive"])
        url = reverse("plugins:netbox_nso_plugin:lacp_accept_bundle", kwargs={"pk": state.pk})

        self.client.post(url)

        state.refresh_from_db()
        self.assertEqual(state.status, "imported")  # refused → NOT accepted/owned
        self.assertIsNone(state.accepted_at)

    def test_adapter_error_still_renders_persisted_rows(self):
        """Adapter down on a LINKED device is the same bug: the error banner must not
        come with a false 'nothing configured' underneath it."""
        self.mgmt.adapter_device_id = 4242
        self.mgmt.save()

        with patch(
            "netbox_nso_plugin.adapter_client.get_logging_config",
            side_effect=AdapterError("adapter is down", code="unreachable"),
        ):
            resp = self._get("logging")

        body = resp.content.decode()
        self.assertIn("The NSO adapter request failed. See the server log.", body)
        self.assertNotIn("adapter is down", body)
        self.assertNotIn("No remote syslog servers configured", body)
        self.assertIn("192.0.2.99", body)  # persisted rows still render


class TestRoutePolicyGrid(ViewTestBase):
    """route_policy as a client-side grid: the payload is built from persisted
    NSORoutePolicyState (no adapter call), rows carry the per-device / unsupported
    badge inputs and the Diff / Versions urls only once a NetBox object is matched."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyObjectClass, NSORoutePolicyState

        route_map = RouteMap.objects.create(name="RM-EDGE")
        ct = ContentType.objects.get_for_model(RouteMap)
        cls.rp_linked = NSORoutePolicyState.objects.create(
            management=cls.mgmt,
            family="route_map",
            object_name="RM-EDGE",
            content_type=ct,
            object_id=route_map.id,
            status="imported",
        )
        cls.rp_unmatched = NSORoutePolicyState.objects.create(
            management=cls.mgmt,
            family="prefix_list",
            object_name="PL-LOOPBACKS",
            status="in_sync",
        )
        cls.rp_flagged = NSORoutePolicyState.objects.create(
            management=cls.mgmt,
            family="community_list",
            object_name="CL-DIVERGENT",
            status="imported",
            unsupported_members=["65000:1", "65000:2"],
        )
        NSORoutePolicyObjectClass.objects.create(family="community_list", object_name="CL-DIVERGENT", mode="local")

    def _url(self):
        return reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "route_policy"}
        )

    def test_json_payload_serves_persisted_rows_without_adapter(self):
        with patch("netbox_nso_plugin.adapter_client.get_route_policy") as getter:
            resp = self.client.get(self._url(), {"format": "json"})
        getter.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)

        rows = {r["name"]: r for r in data["rows"]}
        self.assertEqual(set(rows), {"RM-EDGE", "PL-LOOPBACKS", "CL-DIVERGENT"})
        # family, object_name ordering
        self.assertEqual([r["name"] for r in data["rows"]], ["CL-DIVERGENT", "PL-LOOPBACKS", "RM-EDGE"])

        linked = rows["RM-EDGE"]
        self.assertIsNotNone(linked["accept_url"])  # imported → acceptable
        self.assertEqual(linked["obj"]["label"], "RM-EDGE")
        self.assertEqual(linked["obj"]["url"], self.rp_linked.assigned_object.get_absolute_url())
        self.assertIn(f"/overlay/route_map_name/{self.rp_linked.pk}/edit-field/", linked["edit_url"])
        self.assertIn(str(self.rp_linked.pk), linked["diff_url"])
        self.assertIn(str(self.rp_linked.pk), linked["versions_url"])
        self.assertFalse(linked["per_device"])
        self.assertEqual(linked["unsupported"], [])

        unmatched = rows["PL-LOOPBACKS"]
        self.assertIsNone(unmatched["accept_url"])  # in_sync → owned, no Accept
        self.assertIsNone(unmatched["obj"])
        self.assertIsNone(unmatched["diff_url"])  # no NetBox object -> nothing to diff
        self.assertIsNone(unmatched["versions_url"])

        flagged = rows["CL-DIVERGENT"]
        self.assertTrue(flagged["per_device"])
        self.assertEqual(flagged["unsupported"], ["65000:1", "65000:2"])

        self.assertEqual(data["counts"]["all"], 3)

    def test_fragment_embeds_payload_and_keeps_the_action_buttons(self):
        resp = self.client.get(self._url(), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="nso-rp-data"', body)  # embedded grid payload
        self.assertNotIn("nso-cat-pager", body)  # the server-side pager is gone
        self.assertIn("Add Route-Policy", body)
        self.assertIn("Capabilities", body)
        self.assertIn("Resolve divergent", body)
        self.assertIn("NSOGridRoutePolicy.mount", body)
        self.assertNotIn("{#", body)  # the CR-P16 multi-line comment leak

    def test_unlinked_device_still_serves_rows(self):
        """Grid payloads come from the DB — an unlinked device keeps its rows."""
        self.assertIsNone(self.mgmt.adapter_device_id)
        data = json.loads(self.client.get(self._url(), {"format": "json"}).content)
        self.assertEqual(len(data["rows"]), 3)


class TestApplyRefusesAStaleSnmpStore(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """codex O1 r4 F2: the Apply may not commit an SNMP store a refused push left stale.

    The Apply refreshes SNMP store-only ahead of ``trigger_apply`` so the adapter commits
    what NetBox owns now. That push is refused while the key holds deletion authority
    (§4.3(d)), and the refusal was discarded: the Apply then committed the adapter's stored
    SNMP intent, which still carries the community the operator deleted.
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username="applysnmpadmin", password=TEST_PASSWORD, email="applysnmp@test.example"
        )
        self.client.force_login(self.user)
        self.adapter = ReceiptAdapter(respond=lambda body: {"job_id": 7712, "count": 0})
        self.device, self.mgmt = make_managed("apsnmp", 7790)

    def _apply(self):
        from django.contrib.messages import get_messages

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.mgmt.pk, "apply"])
        config, session = self.adapter.patches()
        with config, session:
            response = self.client.post(url)
        applied = [request for request in self.adapter.requests if "/actions/apply" in request["url"]]
        return applied, [str(message) for message in get_messages(response.wsgi_request)]

    def _own_a_community(self):
        from netbox_nso_plugin.models import NSOSnmpCommunityState

        with without_commit_drain(), transaction.atomic():
            return NSOSnmpCommunityState.objects.create(
                management=self.mgmt,
                community_hash="ab12cd34ef56ab78",
                access="RO",
                status="accepted",
                vault_ref="test/snmp/community",
            )

    def _own_then_delete_a_community(self):
        community = self._own_a_community()
        with without_commit_drain(), transaction.atomic():
            community.delete()

    def test_a_pending_snmp_deletion_stops_the_apply(self):
        self._own_then_delete_a_community()

        applied, messages_shown = self._apply()

        assert applied == [], (
            "the Apply committed the adapter's stored SNMP intent, which still holds the community the operator deleted"
        )
        assert any("SNMP" in message for message in messages_shown), messages_shown

    def test_an_acknowledged_refresh_still_applies(self):
        """The guard is a precondition, not a new refusal: the normal path is unchanged."""
        from netbox_nso_plugin import drain

        self._own_a_community()
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "snmp") == drain.SUCCEEDED

        applied, _messages_shown = self._apply()

        assert len(applied) == 1


class TestDeviceNSOTabDegradedDeletions(ViewTestBase):
    """codex O1 r4 F3 (§4.3(c)): the durable degradation record needs an operator surface.

    ``degraded_deletions`` had exactly one production reader, the acknowledgement command,
    so a deletion that left the device configured was recorded where nobody looked. The tab
    renders it as its own banner, from the database alone, until the command clears it.
    """

    def _url(self):
        return reverse("dcim:device_nso", kwargs={"pk": self.device.pk})

    def _record(self):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        return NSOIntentOutboxState.objects.create(
            device=self.device,
            scope="static_route",
            degraded_deletions=[
                {
                    "route_ids": [424242],
                    "triples": [{"vrf": "BLUEVRF", "prefix": "198.51.100.0/28", "next_hop": "198.51.100.9"}],
                    "at": "2026-08-11T05:00:00+00:00",
                    "reason": "pre_fence_detach",
                    "device": self.device.pk,
                }
            ],
        )

    def test_the_record_renders_as_its_own_banner(self):
        self._record()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mdi-delete-alert-outline")
        self.assertContains(response, "Static route")  # the delivery key's label
        self.assertContains(response, "424242")
        self.assertContains(response, "198.51.100.0/28")  # the triple actually removed
        self.assertContains(response, "198.51.100.9")
        self.assertContains(response, "BLUEVRF")
        self.assertContains(response, "detached before the fence opened")
        self.assertContains(response, "nso_acknowledge_degraded_deletions")
        self.assertNotContains(response, "{#")

    def test_the_headline_counts_records_not_deleted_objects(self):
        """One record can name many objects, so the count must not be read as a deletion count.

        The banner counts entries in ``degraded_deletions``; wording it as "1 deletion" while
        the entry lists three route ids understates what stayed on the device.
        """
        from netbox_nso_plugin.models import NSOIntentOutboxState

        NSOIntentOutboxState.objects.create(
            device=self.device,
            scope="static_route",
            degraded_deletions=[
                {
                    "route_ids": [1, 2, 3],
                    "triples": [],
                    "at": "2026-08-11T05:00:00+00:00",
                    "reason": "pre_fence_detach",
                    "device": self.device.pk,
                }
            ],
        )

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mdi-delete-alert-outline")
        self.assertNotContains(response, "1 deletion left")
        self.assertContains(response, "1 degraded deletion record")

    def test_a_device_with_no_record_renders_no_banner(self):
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "mdi-delete-alert-outline")

    def test_only_the_acknowledgement_clears_the_banner(self):
        from io import StringIO

        from django.core.management import call_command

        self._record()
        self.assertContains(self.client.get(self._url()), "424242")

        out = StringIO()
        call_command("nso_acknowledge_degraded_deletions", device_id=self.device.pk, stdout=out)

        assert "198.51.100.0/28 via 198.51.100.9 (vrf BLUEVRF)" in out.getvalue(), out.getvalue()
        self.assertNotContains(self.client.get(self._url()), "mdi-delete-alert-outline")


class TestReviewRegressionPins(ViewTestBase):
    def test_interface_ip_residue_uses_the_adapter_removal_scope(self):
        # Residue flows adapter -> plugin under the adapter's removal-scope vocabulary
        # (nso_adapter/core/removal.py VALID_REMOVAL_SCOPES), where interface-IP residue
        # is "interface_config". The plugin's outbound delivery key "ip" names a
        # different direction; #1591 owns unifying the two.
        from netbox_nso_plugin.views import _residue_matchers

        assert _residue_matchers()["interface_ips"][0] == "interface_config"

    def test_redistribution_bulk_accept_schedules_only_owned_destinations(self):
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.views import NSORedistributionBulkAcceptView

        NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="bgp",
            dest_ref="65001",
            source_protocol="static",
            status="accepted",
        )
        NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="bgp",
            dest_ref="65001",
            source_protocol="connected",
            status="in_sync",
        )
        NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="isis",
            dest_ref="CORE",
            source_protocol="static",
            status="imported",
        )
        NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="unknown",
            source_protocol="static",
            status="accepted",
        )
        # A delivery key that is NOT a redistribution destination: adapter payload data
        # populates this column, and the signal path refuses what this path must too.
        NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="vlan",
            source_protocol="static",
            status="accepted",
        )

        with patch("netbox_nso_plugin.views._schedule_intent_push") as schedule:
            NSORedistributionBulkAcceptView()._push(self.mgmt)

        schedule.assert_called_once_with((self.device.pk, "bgp"))

    def test_routing_bulk_accept_stamps_first_ownership_time(self):
        from netbox_nso_plugin.models import NSORedistributionState

        row = NSORedistributionState.objects.create(
            management=self.mgmt,
            dest_protocol="bgp",
            source_protocol="static",
            status="changed",
        )

        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_bulk_accept_redistribution", args=[self.device.pk])
        )

        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNotNone(row.accepted_at)
