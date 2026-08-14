# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Extended unit tests for adapter_client: _resolve_config, _request error paths,
and the remaining API call functions not covered by test_models.py.
"""

import sys
import threading
import unittest
from unittest.mock import patch

import requests
from django.db import connections
from django.db.models.signals import post_init
from django.test import TestCase, TransactionTestCase, override_settings

from netbox_nso_plugin.models import AdapterConnection

from ._adapter_http import make_response, make_session
from .mixins import _CascadeFlushMixin

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


class TestClientResetHelpers(unittest.TestCase):
    """Tests for public reset hooks that do not need a configured database."""

    def test_reset_config_cache_discards_cached_values(self):
        """The public hook removes both cached settings and their expiry timestamp."""
        import netbox_nso_plugin.adapter_client as ac

        self.addCleanup(ac.reset_config_cache)
        ac._cfg_cache.update({"data": {"url": "http://stale.invalid"}, "ts": 123.0})

        ac.reset_config_cache()

        self.assertEqual(ac._cfg_cache, {})


class TestResolveConfigCacheConcurrency(_CascadeFlushMixin, TransactionTestCase):
    """Database-backed concurrency coverage for adapter config invalidation."""

    @override_settings(
        PLUGINS_CONFIG={"netbox_nso_plugin": {"adapter_token": "test-token", "adapter_url": "http://fallback.invalid"}}
    )
    def test_reset_during_resolve_cannot_repopulate_stale_config(self):
        """A resolver already holding the old row must not refill the cleared cache."""
        import netbox_nso_plugin.adapter_client as ac

        ac.reset_config_cache()
        self.addCleanup(ac.reset_config_cache)
        AdapterConnection.objects.all().delete()
        conn = AdapterConnection.objects.create(url="http://old-adapter.invalid", enabled=True)
        row_loaded = threading.Event()
        resume_resolve = threading.Event()
        paused_once = threading.Event()

        def pause_after_old_row_load(sender, instance, **kwargs):
            if instance.pk != conn.pk or paused_once.is_set():
                return
            paused_once.set()
            row_loaded.set()
            resume_resolve.wait(timeout=10)

        post_init.connect(pause_after_old_row_load, sender=AdapterConnection, weak=False)
        self.addCleanup(post_init.disconnect, pause_after_old_row_load, sender=AdapterConnection)
        self.addCleanup(resume_resolve.set)

        results = []
        errors = []

        def resolve_config():
            try:
                results.append(ac._resolve_config())
            except Exception as exc:  # noqa: BLE001 — surfaced by the main test thread
                errors.append(exc)
            finally:
                connections.close_all()

        resolver = threading.Thread(target=resolve_config, daemon=True)
        resolver.start()
        self.assertTrue(row_loaded.wait(timeout=10), "resolver never loaded the old AdapterConnection row")

        AdapterConnection.objects.filter(pk=conn.pk).update(url="http://new-adapter.invalid")
        ac.reset_config_cache()
        resume_resolve.set()
        resolver.join(timeout=10)

        self.assertFalse(resolver.is_alive(), "resolver did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["url"], "http://new-adapter.invalid")
        self.assertEqual(ac._cfg_cache["data"]["url"], "http://new-adapter.invalid")


def _mock_response(status_code=200, json_data=None, content=None):
    return make_response(status_code, json_data, content)


class TestResolveConfig(TestCase):
    """Tests for adapter_client._resolve_config() against a real AdapterConnection row.

    _resolve_config reads ``settings.PLUGINS_CONFIG`` and queries
    ``AdapterConnection.objects.filter(enabled=True).first()``. Both are exercised for
    real here — @override_settings supplies a real PLUGINS_CONFIG dict and real model
    rows drive the DB branch — so a field rename or query change fails loudly, unlike
    the previous MagicMock'd ORM + settings which fabricated any attribute on demand.
    """

    def setUp(self):
        # Clear the in-process cache and start from an empty singleton table so each
        # test's AdapterConnection state is explicit.
        import netbox_nso_plugin.adapter_client as ac

        ac.reset_config_cache()
        AdapterConnection.objects.all().delete()

    @override_settings(
        PLUGINS_CONFIG={"netbox_nso_plugin": {"adapter_token": "env-token", "adapter_url": "http://env-adapter"}}
    )
    def test_reads_from_plugin_config_when_no_db_connection(self):
        import netbox_nso_plugin.adapter_client as ac

        # No AdapterConnection row → URL + non-secret settings come from PLUGINS_CONFIG.
        result = ac._resolve_config()

        self.assertEqual(result["token"], "env-token")
        self.assertEqual(result["url"], "http://env-adapter")
        self.assertTrue(result["verify_tls"])
        self.assertIsNone(result["ca_cert_path"])
        self.assertEqual(result["timeout"], 30)

    @override_settings(PLUGINS_CONFIG={"netbox_nso_plugin": {"adapter_token": "tok", "adapter_url": "http://fallback"}})
    def test_adapter_connection_overrides_url(self):
        import netbox_nso_plugin.adapter_client as ac

        AdapterConnection.objects.create(
            url="http://db-adapter", verify_tls=False, ca_cert_path="", timeout_seconds=15, enabled=True
        )
        result = ac._resolve_config()

        self.assertEqual(result["url"], "http://db-adapter")
        self.assertFalse(result["verify_tls"])
        self.assertEqual(result["timeout"], 15)

    @override_settings(PLUGINS_CONFIG={"netbox_nso_plugin": {"adapter_token": "tok"}})
    def test_adapter_connection_ca_cert_path(self):
        import netbox_nso_plugin.adapter_client as ac

        AdapterConnection.objects.create(
            url="https://adapter", verify_tls=True, ca_cert_path="/etc/ssl/ca.pem", timeout_seconds=30, enabled=True
        )
        result = ac._resolve_config()

        self.assertEqual(result["ca_cert_path"], "/etc/ssl/ca.pem")

    @override_settings(PLUGINS_CONFIG={"netbox_nso_plugin": {"adapter_token": "tok", "adapter_url": "http://adapter/"}})
    def test_url_trailing_slash_stripped(self):
        import netbox_nso_plugin.adapter_client as ac

        # No AdapterConnection row → URL from PLUGINS_CONFIG, trailing slash stripped.
        result = ac._resolve_config()

        self.assertEqual(result["url"], "http://adapter")

    @override_settings(PLUGINS_CONFIG={"netbox_nso_plugin": {"adapter_token": "tok", "adapter_url": "http://x"}})
    def test_cache_used_on_second_call(self):
        """Second call within the TTL returns the cached value without re-querying.

        Proven behaviorally with a real row: after the first resolve caches the value we
        mutate the AdapterConnection's URL in the DB but do NOT clear the cache. The
        second resolve must still return the OLD url — if caching regressed it would read
        the new url and the assert would fail. (The old test spied on a mock's filter()
        call count; this exercises the real query + real cache instead.)
        """
        import netbox_nso_plugin.adapter_client as ac

        conn = AdapterConnection.objects.create(url="http://first", timeout_seconds=30, enabled=True)
        r1 = ac._resolve_config()
        self.assertEqual(r1["url"], "http://first")

        conn.url = "http://second"
        conn.save(update_fields=["url"])

        r2 = ac._resolve_config()  # within the 30s TTL → served from cache
        self.assertEqual(r2["url"], "http://first")

    @override_settings(
        PLUGINS_CONFIG={"netbox_nso_plugin": {"adapter_token": "fallback-tok", "adapter_url": "http://fallback"}}
    )
    def test_models_import_failure_falls_back_to_plugin_config(self):
        """If importing models raises, _resolve_config falls back to PLUGINS_CONFIG.

        ``sys.modules[...] = None`` forces ``from .models import AdapterConnection`` to
        raise ImportError — a real import failure (the module genuinely unimportable),
        not a mock — exercising the handler's except-and-fall-back branch.
        """
        import netbox_nso_plugin.adapter_client as ac

        with patch.dict(sys.modules, {"netbox_nso_plugin.models": None}):
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
            session = make_session()
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
            session = make_session()
            session.request.return_value = _mock_response(200, {})
            mock_s.return_value = session
            _request("GET", "/test")

        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["verify"], "/etc/certs/ca.pem")

    def test_list_jobs_passes_device_id_as_query_param(self):
        """device_id is a proper query param (URL-encoded by requests), not concatenated into
        the path — so a non-int arg or later path preprocessing can't produce a malformed URL."""
        from netbox_nso_plugin.adapter_client import list_jobs

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            session.request.return_value = _mock_response(200, [])
            mock_s.return_value = session
            list_jobs(42)

        args, kwargs = session.request.call_args
        self.assertEqual(kwargs.get("params"), {"device_id": 42})
        self.assertNotIn("?", args[1])  # not embedded in the path

    def test_session_is_pooled_across_requests(self):
        """The session is built once and reused — connection pooling, not a handshake per call."""
        import netbox_nso_plugin.adapter_client as ac

        ac.reset_session()
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            session.request.return_value = _mock_response(200, {})
            mock_s.return_value = session
            ac._request("GET", "/a")
            ac._request("GET", "/b")
            # The Session class is instantiated ONCE (pooled) yet used for BOTH requests.
            self.assertEqual(mock_s.call_count, 1)
            self.assertEqual(session.request.call_count, 2)
            # Internal adapter: the pooled session never trusts the system proxy env.
            self.assertFalse(session.trust_env)
        ac.reset_session()

    def test_reset_session_forces_rebuild(self):
        """reset_session() drops the pool so the next call rebuilds (e.g. after a Session patch)."""
        import netbox_nso_plugin.adapter_client as ac

        ac.reset_session()
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            session.request.return_value = _mock_response(200, {})
            mock_s.return_value = session
            ac._request("GET", "/a")
            ac.reset_session()
            ac._request("GET", "/b")
            self.assertEqual(mock_s.call_count, 2)
        ac.reset_session()

    def test_error_response_non_json_falls_back_to_status_code(self):
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            # Real non-JSON body: resp.json() raises a real JSONDecodeError, so this
            # genuinely exercises _request's except-fallback to status_code — a MagicMock
            # with a hand-set json.side_effect only re-asserts our own assumption.
            session.request.return_value = make_response(503, content=b"Service Unavailable")
            mock_s.return_value = session

            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")

        self.assertEqual(ctx.exception.code, "503")

    def test_read_timeout_surfaces_as_nso_timeout(self):
        """A connected-but-hung adapter (ReadTimeout) → distinct nso_timeout code, not nso_unreachable."""
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            session.request.side_effect = requests.exceptions.ReadTimeout("read timed out")
            mock_s.return_value = session

            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")

        self.assertEqual(ctx.exception.code, "nso_timeout")

    def test_connect_error_surfaces_as_nso_unreachable(self):
        """A connection failure → nso_unreachable."""
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            session.request.side_effect = requests.exceptions.ConnectionError("refused")
            mock_s.return_value = session

            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")

        self.assertEqual(ctx.exception.code, "nso_unreachable")


class TestAdapterClientRemainingFunctions(unittest.TestCase):
    """Smoke tests for API functions not covered in test_models.py."""

    def _make_session(self, status=200, json_data=None, content=None):
        return make_session(status_code=status, json_data=json_data, content=content)

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
    def test_set_scope_includes_failover_ips_when_passed(self, mock_s, _cfg):
        """primary_ip/oob_ip are sent (incl. explicit None to clear) when passed."""
        from netbox_nso_plugin.adapter_client import set_scope

        session = self._make_session(200, {"device_id": 5})
        mock_s.return_value = session
        set_scope(5, ["description"], primary_ip="10.0.0.1", oob_ip=None)

        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"]["primary_ip"], "10.0.0.1")
        self.assertIn("oob_ip", kwargs["json"])  # explicit None included → clears adapter-side
        self.assertIsNone(kwargs["json"]["oob_ip"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_set_scope_omits_failover_ips_when_not_passed(self, mock_s, _cfg):
        """Omitting primary_ip/oob_ip leaves the keys out → adapter preserves stored values."""
        from netbox_nso_plugin.adapter_client import set_scope

        session = self._make_session(200, {"device_id": 5})
        mock_s.return_value = session
        set_scope(5, ["description"], auto_apply=True)

        _, kwargs = session.request.call_args
        self.assertNotIn("primary_ip", kwargs["json"])
        self.assertNotIn("oob_ip", kwargs["json"])
        self.assertTrue(kwargs["json"]["auto_apply"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_provision_device_includes_oob_ip(self, mock_s, _cfg):
        """oob_ip is included in the provision payload only when set (None → omitted)."""
        from netbox_nso_plugin.adapter_client import provision_device

        session = self._make_session(200, {"ok": True, "steps": [], "device_id": 1})
        mock_s.return_value = session
        provision_device("prod", "rtr", "10.0.0.1", "cisco-ios-cli-6.114", "network", oob_ip="192.0.2.5")
        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"]["oob_ip"], "192.0.2.5")

        provision_device("prod", "rtr", "10.0.0.1", "cisco-ios-cli-6.114", "network")
        _, kwargs = session.request.call_args
        self.assertNotIn("oob_ip", kwargs["json"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_put_failover_config_payload(self, mock_s, _cfg):
        """put_failover_config PUTs the tuning dict to /api/v1/config/failover."""
        from netbox_nso_plugin.adapter_client import put_failover_config

        session = self._make_session(200, {"enabled": False})
        mock_s.return_value = session
        put_failover_config({"enabled": False, "primary_probe_interval": 15, "probe_concurrency": 8})

        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "PUT")
        self.assertTrue(args[1].endswith("/api/v1/config/failover"))
        self.assertEqual(kwargs["json"]["primary_probe_interval"], 15)
        self.assertIs(kwargs["json"]["enabled"], False)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_get_failover_config_reads_deployment_switch(self, mock_s, _cfg):
        """get_failover_config GETs /api/v1/config/failover and returns deployment_enabled."""
        from netbox_nso_plugin.adapter_client import get_failover_config

        session = self._make_session(200, {"enabled": True, "deployment_enabled": False})
        mock_s.return_value = session
        result = get_failover_config()

        args, _kwargs = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("/api/v1/config/failover"))
        self.assertIs(result["deployment_enabled"], False)

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

    def test_static_route_intent_doc_names_the_list_that_clears_the_mirror(self):
        from netbox_nso_plugin.adapter_client import put_static_route_intent

        self.assertIn("empty ``routes`` list clears", put_static_route_intent.__doc__)

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

        session = make_session()
        session.request.return_value = _mock_response(
            503, {"error": {"code": "nso_unreachable", "message": "down", "detail": {}}}
        )
        mock_s.return_value = session

        with self.assertRaises(AdapterError) as ctx:
            sync_notify(5)
        self.assertEqual(ctx.exception.code, "nso_unreachable")


class TestGetApplyDiffOutformat(TestCase):
    def test_outformat_param_threaded(self):
        """outformat rides the query string so NSO renders cli (+/- tree diff) vs native."""
        from unittest.mock import patch

        from netbox_nso_plugin import adapter_client

        with patch("netbox_nso_plugin.adapter_client._request", return_value={"diffs": {}}) as req:
            adapter_client.get_apply_diff(5, outformat="cli")
        req.assert_called_once_with("GET", "/api/v1/devices/5/actions/apply-diff", params={"outformat": "cli"})
