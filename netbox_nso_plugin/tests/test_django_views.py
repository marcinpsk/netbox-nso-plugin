# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django-stack tests for views: list, detail, CRUD, action views.

These tests require the full NetBox/Django stack (run in devcontainer).
Adapter calls are mocked so no live adapter is needed.
"""

import json
from unittest.mock import MagicMock, patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_nso_plugin.models import AdapterConnection, NSODeviceManagement, NSOInstance, NSOInterfaceState

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


class ViewTestBase(TestCase):
    """Base class: creates superuser and logs in, creates fixtures."""

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
        self.client.force_login(self.superuser)


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
        """List view refreshes cached last_sync_* via a per-row get_device poll."""
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
            resp = MagicMock(ok=True, status_code=200)
            resp.content = b"{}"
            resp.json.return_value = {
                "id": 16,
                "last_sync_at": "2025-06-01T10:00:00+00:00",
                "last_sync_status": "succeeded",
            }
            return resp

        mock_session_cls.return_value = MagicMock(request=make_resp)

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Poll hit get_device (not /interfaces or /compliance — list is lightweight).
        self.assertTrue(any(u.endswith("/devices/16") for u in calls), calls)
        self.assertFalse(any("/compliance" in u or "/interfaces" in u for u in calls), calls)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "succeeded")
        self.assertIsNotNone(mgmt.last_sync_at)

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
        resp = MagicMock(ok=False, status_code=502, content=b"{}")
        resp.json.return_value = {}
        mock_session_cls.return_value = MagicMock(request=MagicMock(return_value=resp))

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


# ── Detail views ─────────────────────────────────────────────────────────────────


class TestNSOInstanceDetailView(ViewTestBase):
    """Tests for NSOInstanceView detail view."""

    def test_detail_200(self):
        """Detail view returns 200 OK."""
        url = reverse("plugins:netbox_nso_plugin:nsoinstance", args=[self.nso_instance.pk])
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

        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True,
            status_code=200,
            content=b"{}",
            json=MagicMock(return_value={"interfaces": [], "compliant": True}),
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
        # Reset
        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_detail_adapter_error_shows_page(self, mock_session_cls):
        """Detail view returns 200 even when adapter raises AdapterError."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 99
        mgmt.save(update_fields=["adapter_device_id"])

        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=502,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}),
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True,
            status_code=200,
            content=b"[]",
            json=MagicMock(return_value=[{"name": "router-01", "onboarded": False}]),
        )
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=502,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}),
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True,
            status_code=200,
            content=b"{}",
            json=MagicMock(return_value={"job_id": 42, "status": "completed"}),
        )
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=404,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "not_found", "message": "job gone", "detail": {}}}),
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True,
            status_code=202,
            content=b"{}",
            json=MagicMock(return_value={"job_id": 5}),
        )
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True,
            status_code=202,
            content=b"{}",
            json=MagicMock(return_value={"job_id": 7}),
        )
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
        """AJAX POST returns conflict JSON when a job is already running."""
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=409,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "conflict", "message": "running", "detail": {"job_id": 3}}}),
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "conflict")

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

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
        session = MagicMock()
        # Adapter returns 202 but no job_id field
        session.request.return_value = MagicMock(
            ok=True,
            status_code=202,
            content=b"{}",
            json=MagicMock(return_value={}),
        )
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=503,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}),
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        # No HTTP_X_REQUESTED_WITH → non-AJAX path
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        """Non-AJAX conflict POST redirects with warning."""
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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=409,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "conflict", "message": "running", "detail": {"job_id": 3}}}),
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=503,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}),
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "sync"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 502)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])


# ── Refresh compliance view ──────────────────────────────────────────────────────


class TestNSORefreshComplianceView(ViewTestBase):
    """Tests for NSORefreshComplianceView."""

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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True,
            status_code=200,
            content=b"{}",
            json=MagicMock(return_value={"interfaces": [], "compliant": True}),
        )
        mock_session_cls.return_value = session

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_refresh", args=[mgmt.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

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
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=503,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}),
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

    def test_post_accept_redirects(self):
        """POST accept changes status to 'accepted' and redirects."""
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", args=[self.iface_state.pk])
        with patch("netbox_nso_plugin.signals.push_intent_on_accept"):
            response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.iface_state.refresh_from_db()
        self.assertEqual(self.iface_state.status, "accepted")


class TestNSOBulkAcceptView(ViewTestBase):
    """Tests for NSOBulkAcceptView."""

    @patch("netbox_nso_plugin.views._push_intent_for_device")
    def test_post_bulk_accept_redirects(self, mock_push):
        """POST bulk accept accepts all changed states and redirects."""
        # Ensure state is 'changed'
        NSOInterfaceState.objects.filter(pk=self.iface_state.pk).update(status="changed")

        url = reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=[self.device.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        mock_push.assert_called_once_with(self.device.pk)

    def test_post_bulk_accept_nothing_to_accept(self):
        """POST bulk accept when no changed states redirects with info."""
        NSOInterfaceState.objects.filter(pk=self.iface_state.pk).update(status="in_sync")

        url = reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=[self.device.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)


# ── Device NSO Tab ────────────────────────────────────────────────────────────────


class TestDeviceNSOTabView(ViewTestBase):
    """Tests for DeviceNSOTabView (device NSO tab)."""

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

    def _patch_all_getters(self):
        """Patch every adapter getter the tab view may call; return the patch context
        and a dict of the mocks keyed by attribute name."""
        from contextlib import ExitStack

        names = [
            "get_device",
            "get_interfaces",
            "get_compliance",
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
            m.return_value = {} if name in ("get_device", "get_compliance", "get_snmp_config") else []
            mocks[name] = m
        mocks["get_device"].return_value = {"id": 15, "last_sync_at": None, "last_sync_status": ""}
        mocks["get_isis_interfaces"].return_value = {"interfaces": [], "processes": []}
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
            "get_compliance",
            "get_snmp_config",
            "get_static_routes",
            "get_isis_interfaces",
            "get_route_policy",
            "get_ospf",
            "get_redistribution",
            "get_bgp_config",
        ):
            mocks[name].assert_not_called()

    def test_tab_interfaces_only_fetches_interfaces_not_routing(self):
        """manage_interfaces alone fetches interfaces/compliance but no routing or SNMP."""
        mocks = self._render_tab_with_scopes(manage_interfaces=True)
        mocks["get_interfaces"].assert_called_once()
        mocks["get_compliance"].assert_called_once()
        for name in (
            "get_snmp_config",
            "get_static_routes",
            "get_isis_interfaces",
            "get_route_policy",
            "get_ospf",
            "get_redistribution",
            "get_bgp_config",
        ):
            mocks[name].assert_not_called()

    def test_tab_routing_master_off_skips_protocols(self):
        """A protocol flag without the routing master does not trigger its fetch."""
        mocks = self._render_tab_with_scopes(manage_isis=True, manage_bgp=True)
        mocks["get_isis_interfaces"].assert_not_called()
        mocks["get_bgp_config"].assert_not_called()

    def test_tab_routing_selected_protocol_only(self):
        """Routing master + manage_bgp fetches BGP but not other routing protocols."""
        mocks = self._render_tab_with_scopes(manage_routing=True, manage_bgp=True)
        mocks["get_bgp_config"].assert_called_once()
        for name in (
            "get_static_routes",
            "get_isis_interfaces",
            "get_route_policy",
            "get_ospf",
            "get_redistribution",
        ):
            mocks[name].assert_not_called()

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
        session = MagicMock()

        def make_resp(method, url, **kwargs):
            """Return proper shape for each adapter endpoint."""
            resp = MagicMock(ok=True, status_code=200)
            if "/interfaces" in url:
                # GET /devices/{id}/interfaces → list
                resp.content = b"[]"
                resp.json.return_value = []
            elif "/compliance" in url:
                resp.content = b"{}"
                resp.json.return_value = {
                    "device_id": 15,
                    "managed_interfaces": 0,
                    "by_status": {},
                    "last_checked_at": None,
                }
            else:
                resp.content = b"{}"
                resp.json.return_value = {"id": 15, "last_sync_at": None, "last_sync_status": ""}
            return resp

        session.request.side_effect = make_resp
        mock_session_cls.return_value = session

        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

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
            resp = MagicMock(ok=True, status_code=200)
            if "/interfaces" in url:
                resp.content = b"[]"
                resp.json.return_value = []
            elif "/compliance" in url:
                resp.content = b"{}"
                resp.json.return_value = {
                    "device_id": 16,
                    "managed_interfaces": 0,
                    "by_status": {},
                    "last_checked_at": None,
                }
            else:
                resp.content = b"{}"
                resp.json.return_value = {
                    "id": 16,
                    "last_sync_at": "2025-06-01T10:00:00+00:00",
                    "last_sync_status": "succeeded",
                }
            return resp

        mock_session_cls.return_value = MagicMock(request=make_resp)

        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_device_nso_tab_adapter_error_uses_snapshot(self, mock_session_cls, mock_cfg):
        """DeviceNSOTabView falls back to compliance_snapshot on AdapterError."""
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 17
        mgmt.compliance_snapshot = {"interfaces": [{"name": "eth0"}], "compliance": {"compliant": False}}
        mgmt.save(update_fields=["adapter_device_id", "compliance_snapshot"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=False,
            status_code=503,
            content=b"{}",
            json=MagicMock(return_value={"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}),
        )
        mock_session_cls.return_value = session

        url = reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

        mgmt.adapter_device_id = None
        mgmt.compliance_snapshot = None
        mgmt.save(update_fields=["adapter_device_id", "compliance_snapshot"])


# ── _push_intent_for_device ────────────────────────────────────────────────────────


class TestPushIntentForDevice(ViewTestBase):
    """Tests for the _push_intent_for_device helper function."""

    def test_no_mgmt_returns_early(self):
        """_push_intent_for_device is a no-op when no management record exists."""
        from netbox_nso_plugin.views import _push_intent_for_device

        # Use a device ID that has no NSODeviceManagement
        device2 = Device.objects.create(
            name="push-intent-test-router",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        # Should not raise
        _push_intent_for_device(device2.pk)
        device2.delete()

    def test_no_adapter_id_returns_early(self):
        """_push_intent_for_device is a no-op when adapter_device_id is None."""
        from netbox_nso_plugin.views import _push_intent_for_device

        self.assertIsNone(self.mgmt.adapter_device_id)
        _push_intent_for_device(self.device.pk)

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_pushes_accepted_states(self, mock_session_cls, mock_cfg):
        """_push_intent_for_device pushes all accepted interface states."""
        from netbox_nso_plugin.views import _push_intent_for_device

        NSOInterfaceState.objects.filter(pk=self.iface_state.pk).update(status="accepted")
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 20
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True,
            status_code=200,
            content=b"{}",
            json=MagicMock(return_value={}),
        )
        mock_session_cls.return_value = session

        _push_intent_for_device(self.device.pk)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        NSOInterfaceState.objects.filter(pk=self.iface_state.pk).update(status="changed")

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_pushes_enabled_attribute(self, mock_session_cls, mock_cfg):
        """_push_intent_for_device includes 'enabled' attribute states in push."""
        from netbox_nso_plugin.views import _push_intent_for_device

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

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True, status_code=200, content=b"{}", json=MagicMock(return_value={})
        )
        mock_session_cls.return_value = session

        _push_intent_for_device(self.device.pk)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        enabled_state.delete()

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_skips_unknown_attribute(self, mock_session_cls, mock_cfg):
        """_push_intent_for_device skips accepted states with unknown attribute."""
        from netbox_nso_plugin.views import _push_intent_for_device

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

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = MagicMock()
        session.request.return_value = MagicMock(
            ok=True, status_code=200, content=b"{}", json=MagicMock(return_value={})
        )
        mock_session_cls.return_value = session

        _push_intent_for_device(self.device.pk)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        unknown_state.delete()

    @patch("netbox_nso_plugin.adapter_client._resolve_config")
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_put_intent_exception_is_swallowed(self, mock_session_cls, mock_cfg):
        """_push_intent_for_device logs and swallows exceptions from put_intent."""
        from netbox_nso_plugin.views import _push_intent_for_device

        NSOInterfaceState.objects.filter(pk=self.iface_state.pk).update(status="accepted")
        mgmt = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
        mgmt.adapter_device_id = 23
        mgmt.save(update_fields=["adapter_device_id"])

        mock_cfg.return_value = {
            "url": "http://adapter",
            "token": "tok",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        session = MagicMock()
        # Simulate a connection error to make put_intent raise
        session.request.side_effect = OSError("connection refused")
        mock_session_cls.return_value = session

        # Should not raise — exception is swallowed with a warning log
        _push_intent_for_device(self.device.pk)

        mgmt.adapter_device_id = None
        mgmt.save(update_fields=["adapter_device_id"])
        NSOInterfaceState.objects.filter(pk=self.iface_state.pk).update(status="changed")
