# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Extended unit tests for adapter_client: _resolve_config, _request error paths,
and the remaining API call functions not covered by test_models.py.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


def _mock_response(status_code=200, json_data=None, content=b"{}"):
    resp = MagicMock()
    resp.ok = status_code < 400
    resp.status_code = status_code
    resp.content = content
    resp.text = content.decode() if content else ""
    resp.json.return_value = json_data or {}
    return resp


class TestResolveConfig(unittest.TestCase):
    """Tests for adapter_client._resolve_config()."""

    def setUp(self):
        # Clear the in-process cache before each test so they are independent.
        import netbox_nso_plugin.adapter_client as ac

        ac._cfg_cache.clear()

    def _fake_models(self, conn=None):
        mod = MagicMock()
        mod.AdapterConnection = MagicMock()
        mod.AdapterConnection.objects.filter.return_value.first.return_value = conn
        return mod

    def test_reads_from_plugin_config_when_no_db_connection(self):
        import netbox_nso_plugin.adapter_client as ac

        mock_settings = MagicMock()
        mock_settings.PLUGINS_CONFIG = {
            "netbox_nso_plugin": {
                "adapter_token": "env-token",
                "adapter_url": "http://env-adapter",
            }
        }
        fake_models = self._fake_models(conn=None)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch("netbox_nso_plugin.adapter_client.settings", mock_settings),
        ):
            result = ac._resolve_config()

        self.assertEqual(result["token"], "env-token")
        self.assertEqual(result["url"], "http://env-adapter")
        self.assertTrue(result["verify_tls"])
        self.assertIsNone(result["ca_cert_path"])
        self.assertEqual(result["timeout"], 30)

    def test_adapter_connection_overrides_url(self):
        import netbox_nso_plugin.adapter_client as ac

        conn = MagicMock()
        conn.url = "http://db-adapter"
        conn.verify_tls = False
        conn.ca_cert_path = ""
        conn.timeout_seconds = 15

        mock_settings = MagicMock()
        mock_settings.PLUGINS_CONFIG = {"netbox_nso_plugin": {"adapter_token": "tok", "adapter_url": "http://fallback"}}
        fake_models = self._fake_models(conn=conn)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch("netbox_nso_plugin.adapter_client.settings", mock_settings),
        ):
            result = ac._resolve_config()

        self.assertEqual(result["url"], "http://db-adapter")
        self.assertFalse(result["verify_tls"])
        self.assertEqual(result["timeout"], 15)

    def test_adapter_connection_ca_cert_path(self):
        import netbox_nso_plugin.adapter_client as ac

        conn = MagicMock()
        conn.url = "https://adapter"
        conn.verify_tls = True
        conn.ca_cert_path = "/etc/ssl/ca.pem"
        conn.timeout_seconds = 30

        mock_settings = MagicMock()
        mock_settings.PLUGINS_CONFIG = {"netbox_nso_plugin": {"adapter_token": "tok"}}
        fake_models = self._fake_models(conn=conn)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch("netbox_nso_plugin.adapter_client.settings", mock_settings),
        ):
            result = ac._resolve_config()

        self.assertEqual(result["ca_cert_path"], "/etc/ssl/ca.pem")

    def test_url_trailing_slash_stripped(self):
        import netbox_nso_plugin.adapter_client as ac

        mock_settings = MagicMock()
        mock_settings.PLUGINS_CONFIG = {"netbox_nso_plugin": {"adapter_token": "tok", "adapter_url": "http://adapter/"}}
        fake_models = self._fake_models(conn=None)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch("netbox_nso_plugin.adapter_client.settings", mock_settings),
        ):
            result = ac._resolve_config()

        self.assertEqual(result["url"], "http://adapter")

    def test_cache_used_on_second_call(self):
        """Second call within TTL returns cached value without re-resolving."""
        import netbox_nso_plugin.adapter_client as ac

        mock_settings = MagicMock()
        mock_settings.PLUGINS_CONFIG = {"netbox_nso_plugin": {"adapter_token": "tok", "adapter_url": "http://x"}}
        fake_models = self._fake_models(conn=None)

        call_count = [0]
        original_filter = fake_models.AdapterConnection.objects.filter

        def counting_filter(*a, **kw):
            call_count[0] += 1
            return original_filter(*a, **kw)

        fake_models.AdapterConnection.objects.filter = counting_filter

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch("netbox_nso_plugin.adapter_client.settings", mock_settings),
        ):
            r1 = ac._resolve_config()
            r2 = ac._resolve_config()  # should hit cache

        self.assertEqual(r1["token"], r2["token"])
        # AdapterConnection was only queried once
        self.assertEqual(call_count[0], 1)

    def test_models_import_failure_falls_back_to_plugin_config(self):
        """If importing models raises, _resolve_config falls back to PLUGINS_CONFIG."""
        import netbox_nso_plugin.adapter_client as ac

        mock_settings = MagicMock()
        mock_settings.PLUGINS_CONFIG = {
            "netbox_nso_plugin": {"adapter_token": "fallback-tok", "adapter_url": "http://fallback"}
        }

        # Simulate models import failure (e.g., outside devcontainer)
        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": None}),
            patch("netbox_nso_plugin.adapter_client.settings", mock_settings),
        ):
            result = ac._resolve_config()

        self.assertEqual(result["token"], "fallback-tok")
        self.assertEqual(result["url"], "http://fallback")


class TestRequestErrorPaths(unittest.TestCase):
    """Tests for _request error branches not covered by other tests."""

    def test_missing_url_raises_config_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        cfg = {**_BASE_CFG, "url": ""}
        with patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=cfg):
            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")
        self.assertEqual(ctx.exception.code, "configuration_error")

    def test_missing_token_raises_config_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        cfg = {**_BASE_CFG, "token": ""}
        with patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=cfg):
            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")
        self.assertEqual(ctx.exception.code, "configuration_error")

    def test_verify_false_passes_false_to_requests(self):
        from netbox_nso_plugin.adapter_client import _request

        cfg = {**_BASE_CFG, "verify_tls": False}
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=cfg),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = MagicMock()
            session.request.return_value = _mock_response(200, {})
            mock_s.return_value = session
            _request("GET", "/test")

        _, kwargs = session.request.call_args
        self.assertFalse(kwargs["verify"])

    def test_ca_cert_path_passed_as_verify(self):
        from netbox_nso_plugin.adapter_client import _request

        cfg = {**_BASE_CFG, "ca_cert_path": "/etc/certs/ca.pem"}
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=cfg),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = MagicMock()
            session.request.return_value = _mock_response(200, {})
            mock_s.return_value = session
            _request("GET", "/test")

        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["verify"], "/etc/certs/ca.pem")

    def test_error_response_non_json_falls_back_to_status_code(self):
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = MagicMock()
            resp = MagicMock()
            resp.ok = False
            resp.status_code = 503
            resp.content = b"Service Unavailable"
            resp.text = "Service Unavailable"
            resp.json.side_effect = ValueError("not JSON")
            session.request.return_value = resp
            mock_s.return_value = session

            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")

        self.assertEqual(ctx.exception.code, "503")


class TestAdapterClientRemainingFunctions(unittest.TestCase):
    """Smoke tests for API functions not covered in test_models.py."""

    def _make_session(self, status=200, json_data=None, content=None):
        session = MagicMock()
        if content is None:
            content = b"{}" if json_data is not None else b""
        session.request.return_value = _mock_response(status, json_data, content)
        return session

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_patch_device_both_fields(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import patch_device

        session = self._make_session(200, {"id": 5})
        mock_s.return_value = session
        patch_device(5, nso_instance="prod", nso_device_name="rtr")

        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"]["nso_instance"], "prod")
        self.assertEqual(kwargs["json"]["nso_device_name"], "rtr")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_patch_device_one_field(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import patch_device

        session = self._make_session(200, {"id": 5})
        mock_s.return_value = session
        patch_device(5, nso_device_name="new-name")

        _, kwargs = session.request.call_args
        self.assertNotIn("nso_instance", kwargs["json"])
        self.assertEqual(kwargs["json"]["nso_device_name"], "new-name")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_delete_device_no_error(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import delete_device

        session = self._make_session(204, None, b"")
        mock_s.return_value = session
        delete_device(42)  # should not raise
        session.request.assert_called_once()
        args, _ = session.request.call_args
        self.assertEqual(args[0], "DELETE")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_device(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import get_device

        session = self._make_session(200, {"id": 5, "nso_device_name": "rtr1"})
        mock_s.return_value = session
        result = get_device(5)
        self.assertEqual(result["id"], 5)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_interfaces(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import get_interfaces

        session = self._make_session(200, {"interfaces": []})
        mock_s.return_value = session
        result = get_interfaces(5)
        self.assertIn("interfaces", result)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_state(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import get_state

        session = self._make_session(200, {"in_sync": 3, "changed": 1})
        mock_s.return_value = session
        result = get_state(5)
        self.assertEqual(result["in_sync"], 3)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_trigger_sync(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import trigger_sync

        session = self._make_session(202, {"job_id": 11})
        mock_s.return_value = session
        result = trigger_sync(5)
        self.assertEqual(result["job_id"], 11)
        args, _ = session.request.call_args
        self.assertIn("sync", args[1])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_trigger_detect_drift(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import trigger_detect_drift

        session = self._make_session(202, {"job_id": 12})
        mock_s.return_value = session
        result = trigger_detect_drift(5)
        self.assertEqual(result["job_id"], 12)
        args, _ = session.request.call_args
        self.assertIn("detect-drift", args[1])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_trigger_connect(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import trigger_connect

        session = self._make_session(202, {"job_id": 13})
        mock_s.return_value = session
        result = trigger_connect(5)
        self.assertEqual(result["job_id"], 13)
        args, _ = session.request.call_args
        self.assertIn("connect", args[1])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_job(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import get_job

        session = self._make_session(200, {"id": 99, "status": "done"})
        mock_s.return_value = session
        result = get_job(99)
        self.assertEqual(result["status"], "done")
        args, _ = session.request.call_args
        self.assertIn("jobs/99", args[1])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_put_intent(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import put_intent

        attrs = [{"interface": "Gi0/0", "attribute": "description", "intent_value": "uplink", "accepted_at": None}]
        session = self._make_session(200, {"device_id": 5, "attribute_count": 1})
        mock_s.return_value = session
        result = put_intent(5, attrs)
        self.assertEqual(result["attribute_count"], 1)
        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"]["attributes"], attrs)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_trigger_apply(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import trigger_apply

        session = self._make_session(202, {"job_id": 20})
        mock_s.return_value = session
        result = trigger_apply(5)
        self.assertEqual(result["job_id"], 20)
        _, kwargs = session.request.call_args
        self.assertTrue(kwargs["json"]["force"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_sync_notify_non_conflict_error_propagates(self, mock_s, _cfg):
        """sync_notify re-raises AdapterError when code != conflict."""
        from netbox_nso_plugin.adapter_client import AdapterError, sync_notify

        session = MagicMock()
        session.request.return_value = _mock_response(
            503, {"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
        )
        mock_s.return_value = session

        with self.assertRaises(AdapterError) as ctx:
            sync_notify(5)
        self.assertEqual(ctx.exception.code, "nso_unreachable")
