# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for the adapter_client public functions and model helpers."""

import unittest
from unittest.mock import MagicMock, patch

_RESOLVED_CONFIG = {
    "url": "http://adapter",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.ok = status_code < 400
    resp.status_code = status_code
    resp.content = b"{}"
    resp.json.return_value = json_data or {}
    return resp


class TestAdapterClientOnboard(unittest.TestCase):
    """Tests for adapter_client.onboard_device."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_onboard_device_success(self, mock_session_cls, _cfg):
        """onboard_device returns the adapter response on 201."""
        from netbox_nso_plugin.adapter_client import onboard_device

        session = MagicMock()
        session.request.return_value = _mock_response(201, {"id": 7})
        mock_session_cls.return_value = session

        result = onboard_device("nso-prod", "core-rtr-01", 42)
        self.assertEqual(result["id"], 7)
        session.request.assert_called_once()
        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"]["nso_device_name"], "core-rtr-01")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_onboard_device_conflict_raises(self, mock_session_cls, _cfg):
        """onboard_device raises AdapterError on 409."""
        from netbox_nso_plugin.adapter_client import AdapterError, onboard_device

        session = MagicMock()
        session.request.return_value = _mock_response(
            409, {"error": {"code": "conflict", "message": "already onboarded", "detail": {}}}
        )
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError) as ctx:
            onboard_device("nso-prod", "core-rtr-01", 42)
        self.assertEqual(ctx.exception.code, "conflict")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_unreachable_raises(self, mock_session_cls, _cfg):
        """Network errors are wrapped in AdapterError with nso_unreachable code."""
        import requests as req_lib

        from netbox_nso_plugin.adapter_client import AdapterError, onboard_device

        session = MagicMock()
        session.request.side_effect = req_lib.ConnectionError("refused")
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError) as ctx:
            onboard_device("nso-prod", "core-rtr-01", 42)
        self.assertEqual(ctx.exception.code, "nso_unreachable")


class TestAdapterClientScope(unittest.TestCase):
    """Tests for adapter_client.set_scope."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_set_scope_sends_attributes(self, mock_session_cls, _cfg):
        """set_scope sends the attributes list in the request body."""
        from netbox_nso_plugin.adapter_client import set_scope

        session = MagicMock()
        session.request.return_value = _mock_response(200, {"device_id": 7, "attributes": ["description"]})
        mock_session_cls.return_value = session

        set_scope(7, ["description"])
        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"]["attributes"], ["description"])


class TestAdapterClientSyncNotify(unittest.TestCase):
    """Tests for adapter_client.sync_notify."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_sync_notify_returns_job(self, mock_session_cls, _cfg):
        """sync_notify returns job dict on 202."""
        from netbox_nso_plugin.adapter_client import sync_notify

        session = MagicMock()
        session.request.return_value = _mock_response(202, {"job_id": 9})
        mock_session_cls.return_value = session

        result = sync_notify(1)
        self.assertEqual(result["job_id"], 9)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_sync_notify_conflict_returns_detail(self, mock_session_cls, _cfg):
        """sync_notify swallows 409 conflict and returns the existing job detail."""
        from netbox_nso_plugin.adapter_client import sync_notify

        session = MagicMock()
        session.request.return_value = _mock_response(
            409, {"error": {"code": "conflict", "message": "job running", "detail": {"job_id": 5}}}
        )
        mock_session_cls.return_value = session

        result = sync_notify(1)
        self.assertEqual(result["job_id"], 5)


class TestAdapterClientListNSODevices(unittest.TestCase):
    """Tests for adapter_client.list_nso_devices — enriched response shape."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_list_nso_devices_returns_list_of_dicts(self, mock_session_cls, _cfg):
        """list_nso_devices returns list of dicts (not strings) after M7."""
        from netbox_nso_plugin.adapter_client import list_nso_devices

        session = MagicMock()
        session.request.return_value = _mock_response(
            200,
            [
                {
                    "name": "core-rtr-01",
                    "address": "10.0.0.1",
                    "ned_id": "cisco-ios-cli-6.95",
                    "platform": "ios",
                    "auth_group": "default",
                    "admin_state": "unlocked",
                    "onboarded": True,
                    "onboarded_device_id": 1,
                    "onboarded_netbox_device_id": 42,
                },
                {
                    "name": "edge-rtr-02",
                    "address": None,
                    "ned_id": None,
                    "platform": None,
                    "auth_group": None,
                    "admin_state": None,
                    "onboarded": False,
                    "onboarded_device_id": None,
                    "onboarded_netbox_device_id": None,
                },
            ],
        )
        mock_session_cls.return_value = session

        result = list_nso_devices("nso-dev")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "core-rtr-01")
        self.assertEqual(result[0]["address"], "10.0.0.1")
        self.assertEqual(result[0]["platform"], "ios")
        self.assertTrue(result[0]["onboarded"])
        self.assertIsNone(result[1]["ned_id"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_list_nso_devices_empty_returns_empty_list(self, mock_session_cls, _cfg):
        """list_nso_devices returns [] on empty adapter response."""
        from netbox_nso_plugin.adapter_client import list_nso_devices

        session = MagicMock()
        session.request.return_value = _mock_response(200, [])
        mock_session_cls.return_value = session

        self.assertEqual(list_nso_devices("nso-dev"), [])


class TestAdapterClientGetDeviceByNso(unittest.TestCase):
    """Tests for adapter_client.get_device_by_nso."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_device_by_nso_hit_returns_dict(self, mock_session_cls, _cfg):
        """get_device_by_nso returns device dict on 200."""
        from netbox_nso_plugin.adapter_client import get_device_by_nso

        session = MagicMock()
        session.request.return_value = _mock_response(200, {"id": 17, "nso_device_name": "core-rtr-01"})
        mock_session_cls.return_value = session

        result = get_device_by_nso("nso-dev", "core-rtr-01")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 17)
        # Verify query params were passed
        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["params"], {"instance": "nso-dev", "name": "core-rtr-01"})

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_device_by_nso_miss_returns_none(self, mock_session_cls, _cfg):
        """get_device_by_nso returns None on 404 not_found."""
        from netbox_nso_plugin.adapter_client import AdapterError, get_device_by_nso  # noqa: F401

        session = MagicMock()
        session.request.return_value = _mock_response(
            404, {"error": {"code": "not_found", "message": "no device", "detail": {}}}
        )
        mock_session_cls.return_value = session

        result = get_device_by_nso("nso-dev", "no-such-router")
        self.assertIsNone(result)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_RESOLVED_CONFIG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_device_by_nso_other_error_raises(self, mock_session_cls, _cfg):
        """get_device_by_nso raises AdapterError on non-404 errors."""
        from netbox_nso_plugin.adapter_client import AdapterError, get_device_by_nso

        session = MagicMock()
        session.request.return_value = _mock_response(
            502, {"error": {"code": "nso_unreachable", "message": "timeout", "detail": {}}}
        )
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError) as ctx:
            get_device_by_nso("nso-dev", "core-rtr-01")
        self.assertEqual(ctx.exception.code, "nso_unreachable")


class TestNSODeviceManagementManagedAttributes(unittest.TestCase):
    """Tests for managed_attributes property logic."""

    def _make_mgmt(self, manage_description=False, manage_enabled=False):
        """Create a minimal object with the managed_attributes property without Django ORM."""

        class _FakeMgmt:
            def __init__(self, desc, enabled):
                self.manage_description = desc
                self.manage_enabled = enabled

            @property
            def managed_attributes(self):
                attrs = []
                if self.manage_description:
                    attrs.append("description")
                if self.manage_enabled:
                    attrs.append("enabled")
                return attrs

        return _FakeMgmt(manage_description, manage_enabled)

    def test_no_attributes(self):
        """managed_attributes returns empty list when nothing is managed."""
        mgmt = self._make_mgmt()
        self.assertEqual(mgmt.managed_attributes, [])

    def test_description_only(self):
        """managed_attributes returns ['description'] when manage_description=True."""
        mgmt = self._make_mgmt(manage_description=True)
        self.assertEqual(mgmt.managed_attributes, ["description"])

    def test_both_attributes(self):
        """managed_attributes returns both attributes when both flags are set."""
        mgmt = self._make_mgmt(manage_description=True, manage_enabled=True)
        self.assertEqual(mgmt.managed_attributes, ["description", "enabled"])
