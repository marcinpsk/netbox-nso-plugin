# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for adapter LAG topology fetch and reconciliation helpers."""

import unittest
from unittest.mock import MagicMock, patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


class TestGetLagTopology(unittest.TestCase):
    """Tests for adapter_client.get_lag_topology()."""

    def _make_session(self, status=200, json_data=None, content=None):
        response = MagicMock()
        response.ok = status < 400
        response.status_code = status
        response.content = b"{}" if content is None else content
        response.text = response.content.decode() if response.content else ""
        response.json.return_value = json_data or {}

        session = MagicMock()
        session.request.return_value = response
        return session

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_lag_topology_calls_expected_endpoint(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_lag_topology

        session = self._make_session(json_data={"lags": []})
        mock_session_cls.return_value = session

        get_lag_topology(42)

        args, _kwargs = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://adapter.local/api/v1/devices/42/lag-topology")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_lag_topology_returns_response_unchanged(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_lag_topology

        expected = {
            "device_id": 42,
            "last_refreshed_at": "2026-05-27T09:41:12.221Z",
            "refresh_source": "notification",
            "lags": [
                {
                    "name": "Port-channel10",
                    "id": 10,
                    "members": [
                        {"interface": "GigabitEthernet0/1", "mode": "active"},
                    ],
                }
            ],
        }
        session = self._make_session(json_data=expected)
        mock_session_cls.return_value = session

        result = get_lag_topology(42)

        self.assertEqual(result, expected)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_lag_topology_http_error_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, get_lag_topology

        session = self._make_session(
            status=503,
            json_data={
                "error": {
                    "code": "nso_unreachable",
                    "message": "adapter unavailable",
                    "detail": {},
                }
            },
        )
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError) as context:
            get_lag_topology(42)

        self.assertEqual(context.exception.code, "nso_unreachable")


class TestReconcileLagTopology(TestCase):
    """Tests for template_content._reconcile_lag_topology()."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="LagMfg", slug="lagmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="LagDevice", slug="lagdevice")
        role = DeviceRole.objects.create(name="LagRole", slug="lagrole")
        site = Site.objects.create(name="LagSite", slug="lagsite")
        cls.device = Device.objects.create(name="lag-router", device_type=device_type, role=role, site=site)
        cls.port_channel = Interface.objects.create(device=cls.device, name="Port-channel10", type="lag")
        cls.member_1 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")
        cls.member_2 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/2", type="1000base-t")

    def test_reconcile_matches_existing_lag_and_members(self):
        from netbox_nso_plugin.template_content import _reconcile_lag_topology

        lag_data = {
            "refresh_source": "notification",
            "last_refreshed_at": "2026-05-27T09:41:12.221Z",
            "lags": [
                {
                    "name": "Port-channel10",
                    "id": 10,
                    "members": [
                        {"interface": "GigabitEthernet0/1", "mode": "active"},
                        {"interface": "GigabitEthernet0/2", "mode": "active"},
                    ],
                }
            ],
        }

        result = _reconcile_lag_topology(self.device, lag_data)

        self.assertEqual(result["refresh_source"], "notification")
        self.assertEqual(result["last_refreshed_at"], "2026-05-27T09:41:12.221Z")
        self.assertEqual(result["lags"][0]["netbox_interface"], self.port_channel)
        self.assertEqual(result["lags"][0]["members"][0]["netbox_interface"], self.member_1)
        self.assertEqual(result["lags"][0]["members"][1]["netbox_interface"], self.member_2)

    def test_reconcile_sets_none_for_missing_lag_and_member_interfaces(self):
        from netbox_nso_plugin.template_content import _reconcile_lag_topology

        lag_data = {
            "refresh_source": "notification",
            "last_refreshed_at": None,
            "lags": [
                {
                    "name": "Port-channel99",
                    "id": 99,
                    "members": [
                        {"interface": "GigabitEthernet0/1", "mode": "active"},
                        {"interface": "GigabitEthernet0/99", "mode": "passive"},
                    ],
                }
            ],
        }

        result = _reconcile_lag_topology(self.device, lag_data)

        self.assertIsNone(result["lags"][0]["netbox_interface"])
        self.assertEqual(result["lags"][0]["members"][0]["netbox_interface"], self.member_1)
        self.assertIsNone(result["lags"][0]["members"][1]["netbox_interface"])

    def test_reconcile_handles_empty_lag_payload(self):
        from netbox_nso_plugin.template_content import _reconcile_lag_topology

        result = _reconcile_lag_topology(
            self.device,
            {"refresh_source": "never", "last_refreshed_at": None, "lags": []},
        )

        self.assertEqual(result, {"refresh_source": "never", "last_refreshed_at": None, "lags": []})

    def test_reconcile_writes_native_lag_membership(self):
        """Members get their `lag` FK set to the bundle; the bundle is type=lag."""
        from netbox_nso_plugin.template_content import _reconcile_lag_topology

        lag_data = {
            "refresh_source": "notification",
            "last_refreshed_at": None,
            "lags": [
                {
                    "name": "Port-channel10",
                    "id": 10,
                    "members": [
                        {"interface": "GigabitEthernet0/1", "mode": "active"},
                        {"interface": "GigabitEthernet0/2", "mode": "active"},
                    ],
                }
            ],
        }

        _reconcile_lag_topology(self.device, lag_data)

        self.port_channel.refresh_from_db()
        self.member_1.refresh_from_db()
        self.member_2.refresh_from_db()
        self.assertEqual(self.port_channel.type, "lag")
        self.assertEqual(self.member_1.lag_id, self.port_channel.id)
        self.assertEqual(self.member_2.lag_id, self.port_channel.id)

    def test_reconcile_unlinks_members_removed_from_bundle(self):
        """A member previously in the bundle but no longer reported by NSO is unlinked."""
        from netbox_nso_plugin.template_content import _reconcile_lag_topology

        # Pre-link both members to the bundle.
        self.member_1.lag = self.port_channel
        self.member_1.save(update_fields=["lag"])
        self.member_2.lag = self.port_channel
        self.member_2.save(update_fields=["lag"])

        # NSO now reports only member_1 in the bundle.
        lag_data = {
            "refresh_source": "notification",
            "last_refreshed_at": None,
            "lags": [
                {"name": "Port-channel10", "id": 10, "members": [{"interface": "GigabitEthernet0/1", "mode": "active"}]}
            ],
        }

        _reconcile_lag_topology(self.device, lag_data)

        self.member_1.refresh_from_db()
        self.member_2.refresh_from_db()
        self.assertEqual(self.member_1.lag_id, self.port_channel.id)
        self.assertIsNone(self.member_2.lag_id)
