# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Extended unit tests for adapter_client: _resolve_config, _request error paths,
and the remaining API call functions not covered by test_models.py.
"""

import sys
import threading
import unittest
from types import MappingProxyType
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

    def test_abortable_transport_keeps_the_instrumented_pool_alive(self):
        from netbox_nso_plugin.adapter_client import AbortableTransport

        transport = AbortableTransport()
        self.addCleanup(transport.close)
        request = requests.Request("GET", "https://example.invalid").prepare()

        pool = transport.get_connection_with_tls_context(request, verify=True)

        self.assertIn(pool, transport._instrumented)


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

    def _error_from(self, status, body):
        """Raise ``_request`` against a real *body* served with *status*, return the AdapterError."""
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            session.request.return_value = make_response(status, json_data=body)
            mock_s.return_value = session

            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")
        return ctx.exception

    def test_error_body_whose_error_member_is_not_an_object_falls_back(self):
        """A scalar "error" must still raise a typed AdapterError.

        Reading .get() off the scalar raised AttributeError out of _request, which the
        operator sees as a 500 instead of the adapter's error.
        """
        exc = self._error_from(409, {"error": "boom"})

        self.assertEqual(exc.code, "409")
        self.assertEqual(str(exc), '{"error": "boom"}')
        self.assertIsNone(exc.detail)

    def test_a_null_message_or_code_falls_back_to_the_status_and_body(self):
        """Present-but-null members must not beat the fallbacks.

        ``.get(key, default)`` returns the default only when the key is ABSENT, so an
        emitted null produced code=None and message=None — an empty operator message.
        """
        exc = self._error_from(409, {"error": {"code": None, "message": None}})

        self.assertEqual(exc.code, "409")
        self.assertEqual(str(exc), '{"error": {"code": null, "message": null}}')

    def test_a_non_object_detail_is_normalized_away(self):
        """AdapterError.detail is typed dict|None, so every consumer may .get() it."""
        exc = self._error_from(409, {"error": {"code": "conflict", "detail": "boom"}})

        self.assertEqual(exc.code, "conflict")
        self.assertIsNone(exc.detail)

    def test_success_response_non_json_raises_invalid_response(self):
        """A 2xx whose body is not JSON is an adapter fault, not a plugin crash."""
        from netbox_nso_plugin.adapter_client import AdapterError, _request

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session") as mock_s,
        ):
            session = make_session()
            session.request.return_value = make_response(200, content=b"<html>gateway</html>")
            mock_s.return_value = session

            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")

        self.assertEqual(ctx.exception.code, "invalid_response")

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


#: One job exactly as the adapter's JobOut renders it — every key present, nullables null.
_JOB_OUT = {
    "id": 7,
    "type": "apply",
    "device_id": 10,
    "status": "succeeded",
    "result": {"ok": True},
    "error": None,
    "context": {"scope": "vlan"},
    "created_at": "2026-07-10T05:59:00Z",
    "updated_at": "2026-07-10T06:00:00Z",
    "started_at": None,
    "heartbeat_at": None,
    "settle_seq": 3,
}


def _job_with_scalar(member):
    """A JobOut-shaped job whose *member* carries a scalar the model cannot emit."""
    return {**_JOB_OUT, member: "boom"}


class TestJobBoundaryValidation(unittest.TestCase):
    """Job payloads are checked once here, not tolerated by every reader downstream.

    The adapter's ``JobOut`` model (``../nso-adapter/nso_adapter/api/jobs.py``) always emits
    every key and types ``result``, ``error`` and ``context`` as ``dict | None``. A scalar in
    one of those is a broken producer, not a value to defend against at each call site, so
    the client refuses the payload with a typed ``invalid_response`` the callers already
    handle.
    """

    def _session(self, payload=None, *, content=None, headers=None):
        """A pooled-session double serving one real response built from *payload*."""
        import netbox_nso_plugin.adapter_client as ac

        ac.reset_session()
        self.addCleanup(ac.reset_session)
        response = make_response(200, json_data=payload, content=content)
        if headers:
            response.headers.update(headers)
        return make_session(response=response)

    def _refuses(self, session, call):
        """Assert *call* raises the typed refusal while *session* is the transport."""
        from netbox_nso_plugin.adapter_client import AdapterError

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            with self.assertRaises(AdapterError) as ctx:
                call()
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_a_job_shaped_exactly_like_jobout_is_accepted_unchanged(self):
        """The contract pin: what the adapter's model emits must pass through untouched."""
        from netbox_nso_plugin.adapter_client import get_job

        session = self._session(_JOB_OUT)
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            self.assertEqual(get_job(7), _JOB_OUT)

    def test_get_job_refuses_a_payload_jobout_cannot_produce(self):
        from netbox_nso_plugin.adapter_client import get_job

        for payload in (
            "boom",
            ["not-a-job"],
            _job_with_scalar("result"),
            _job_with_scalar("error"),
            _job_with_scalar("context"),
        ):
            with self.subTest(payload=payload):
                self._refuses(self._session(payload), lambda: get_job(7))

    def test_an_empty_body_job_is_refused_rather_than_served_as_none(self):
        """_request returns None on an empty-body 200, which NSOJobStatusView then handed
        straight to JsonResponse and raised TypeError on. Refuse it at the source."""
        from netbox_nso_plugin.adapter_client import get_job

        self._refuses(self._session(content=b""), lambda: get_job(7))

    def test_list_jobs_refuses_a_listing_that_is_not_a_list_of_jobs(self):
        from netbox_nso_plugin.adapter_client import list_jobs

        for payload in ({"jobs": []}, ["not-a-job"], [_job_with_scalar("context")]):
            with self.subTest(payload=payload):
                self._refuses(self._session(payload), lambda: list_jobs(10))

    def test_settlement_feed_refuses_a_malformed_page(self):
        from netbox_nso_plugin.adapter_client import STORE_INCARNATION_HEADER, get_settlement_feed

        session = self._session([_job_with_scalar("result")], headers={STORE_INCARNATION_HEADER: "inc-1"})
        self._refuses(session, lambda: get_settlement_feed(10, after_settle_seq=0, limit=50))

    def test_settlement_feed_refuses_a_body_that_is_not_json(self):
        """The feed reads the body itself, so its own decode must raise the typed refusal.

        A gateway's HTML 200 otherwise surfaced as a bare requests JSONDecodeError, which
        no settlement caller handles: they are written against AdapterError.
        """
        from netbox_nso_plugin.adapter_client import STORE_INCARNATION_HEADER, get_settlement_feed

        session = self._session(content=b"<html>gateway</html>", headers={STORE_INCARNATION_HEADER: "inc-1"})
        self._refuses(session, lambda: get_settlement_feed(10, after_settle_seq=0, limit=50))

    def test_static_route_read_back_refuses_a_payload_without_routes(self):
        """StaticRouteIntentOut always carries a ``routes`` list; anything else is undecidable.

        Degrading to {} here would record ZERO expectations and silently mis-settle, so the
        read-back must fail loudly enough for the stall bound to count it.
        """
        from netbox_nso_plugin.adapter_client import get_static_route_intent

        for payload in ("boom", {"device_id": 10}, {"device_id": 10, "routes": "nope"}):
            with self.subTest(payload=payload):
                self._refuses(self._session(payload), lambda: get_static_route_intent(10))


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
    def test_device_generations_reject_a_malformed_listing(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, list_device_generations, reset_session

        self.addCleanup(reset_session)
        # The first two fail the shape check; the rest reach the per-row seq guard, which is
        # what stops a repeated or out-of-order page from looping the cursor forever.
        for payload in (
            {"generations": []},
            [None],
            [{"generation_id": 1, "status": "pending", "settlement_cohort": None}],
            [{"seq": 1, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "status": "pending"}],
            [{"generation_id": 1, "seq": 1, "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "status": None, "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "status": 123, "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "status": "", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "job_id": "501", "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "job_id": 0, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "job_id": -1, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "job_id": True, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": "1", "seq": 1, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": True, "seq": 1, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 0, "seq": 1, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": -1, "seq": 1, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 0, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": -1, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": True, "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": "1", "status": "pending", "settlement_cohort": None}],
            [{"generation_id": 1, "seq": 1, "status": "pending", "settlement_cohort": 0}],
            [{"generation_id": 1, "seq": 1, "status": "pending", "settlement_cohort": -1}],
            [
                {"generation_id": 2, "seq": 2, "status": "pending", "settlement_cohort": None},
                {"generation_id": 1, "seq": 1, "status": "pending", "settlement_cohort": None},
            ],
            [
                {"generation_id": 1, "seq": 1, "status": "pending", "settlement_cohort": 73},
                {"generation_id": 1, "seq": 2, "status": "pending", "settlement_cohort": 73},
            ],
        ):
            with self.subTest(payload=payload):
                reset_session()
                mock_s.return_value = self._make_session(200, payload)

                with self.assertRaisesRegex(AdapterError, "malformed generations listing") as raised:
                    list_device_generations(5)

                self.assertEqual(raised.exception.code, "invalid_response")

    def test_the_page_limit_stays_within_what_the_adapter_accepts(self):
        """The one thing deriving the fixtures from the constant cannot check: its VALUE.

        The adapter caps a page at ``LIMIT_MAX`` (../nso-adapter/nso_adapter/api/pagination.py)
        and answers 422 rather than clamping, so raising this past it fails every generations
        read at runtime while the derived tests above stay green.
        """
        from netbox_nso_plugin.adapter_client import _GENERATION_PAGE_LIMIT

        self.assertLessEqual(_GENERATION_PAGE_LIMIT, 500)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_device_generations_reads_every_ascending_page(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import _GENERATION_PAGE_LIMIT, list_device_generations

        # A page of exactly the limit is what makes the reader ask for another; the short one stops it.
        last_full_seq = _GENERATION_PAGE_LIMIT
        first_page = [
            {"generation_id": seq, "seq": seq, "status": "pending", "settlement_cohort": None}
            for seq in range(1, last_full_seq + 1)
        ]
        final_page = [
            {
                "generation_id": last_full_seq + 1,
                "seq": last_full_seq + 1,
                "job_id": None,
                "status": "pending",
                "settlement_cohort": None,
            }
        ]
        session = make_session()
        session.request.side_effect = [
            make_response(200, first_page),
            make_response(200, final_page),
        ]
        mock_s.return_value = session

        generations = list_device_generations(5)

        self.assertEqual([row["seq"] for row in generations], list(range(1, last_full_seq + 2)))
        self.assertEqual(
            [call.kwargs["params"] for call in session.request.call_args_list],
            [
                {"limit": _GENERATION_PAGE_LIMIT},
                {"limit": _GENERATION_PAGE_LIMIT, "since_seq": last_full_seq},
            ],
        )

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_device_generations_refuses_an_unbounded_history(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import (
            _GENERATION_PAGE_LIMIT,
            _GENERATION_PAGE_MAX,
            AdapterError,
            list_device_generations,
        )

        self.assertLessEqual(_GENERATION_PAGE_MAX, 20)

        page_number = 0

        def full_page(*_args, **_kwargs):
            nonlocal page_number
            page_number += 1
            if page_number > _GENERATION_PAGE_MAX:
                raise AssertionError("the generations reader requested an unbounded page")
            first_seq = (page_number - 1) * _GENERATION_PAGE_LIMIT + 1
            return make_response(
                200,
                [
                    {
                        "generation_id": seq,
                        "seq": seq,
                        "status": "pending",
                        "settlement_cohort": None,
                    }
                    for seq in range(first_seq, first_seq + _GENERATION_PAGE_LIMIT)
                ],
            )

        session = make_session()
        session.request.side_effect = full_page
        mock_s.return_value = session

        with self.assertRaisesRegex(AdapterError, "more generation pages") as raised:
            list_device_generations(5)

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(session.request.call_count, _GENERATION_PAGE_MAX)

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

    def test_the_canonical_bytes_are_what_requests_prepares(self):
        """Both keys go to ``session.request``; requests must put the canonical ``data`` on the wire."""
        from netbox_nso_plugin.adapter_client import _attach_serialized_json

        kwargs = {"json": {"b": 1, "a": 2}}
        _attach_serialized_json(kwargs)
        prepared = requests.Request("PUT", "https://example.invalid/x", **kwargs).prepare()
        self.assertEqual(prepared.body, b'{"a": 2, "b": 1}')

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_empty_static_route_intent_clears_the_mirror_on_the_wire(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import put_static_route_intent

        session = self._make_session(200, {"device_id": 5, "count": 0, "routes": []})
        mock_s.return_value = session

        put_static_route_intent(5, [])

        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"]["routes"], [])
        self.assertEqual(kwargs["json"]["deleted_routes"], [])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_trigger_apply_sends_the_frozen_selector_and_returns_the_generation_chain(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import trigger_apply

        # Copied from ../nso-adapter/docs/api-contract.md, actions/apply 202 response,
        # with the fields pinned by ActionApplyGenerationOut in openapi_snapshot.json.
        promoted = {
            "device_id": 5,
            "outcome": "promoted",
            "selected": {"vlan": 4711},
            "skipped": {},
            "generations": [
                {
                    "generation_id": 81,
                    "seq": 4,
                    "job_id": 501,
                    "mode": "networked",
                    "source_push_seq": {"vlan": 4711},
                    "stream_revisions": {"vlan": 7},
                    "digest": "a" * 64,
                },
                {
                    "generation_id": 82,
                    "seq": 5,
                    "job_id": None,
                    "mode": "detach",
                    "source_push_seq": {"vlan": 4711},
                    "stream_revisions": {"vlan": 7},
                    "digest": "b" * 64,
                },
            ],
        }
        session = self._make_session(202, promoted)
        mock_s.return_value = session
        selector = MappingProxyType({"vlan": 4711})

        apply_attempt_id = "8a2c9231-7ad8-4b17-a4b8-f5b4df745dd8"

        result = trigger_apply(5, apply_attempt_id, selector)

        self.assertEqual(result, promoted)
        _, kwargs = session.request.call_args
        self.assertEqual(
            kwargs["json"],
            {"apply_attempt_id": apply_attempt_id, "selected": {"vlan": 4711}},
        )

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_trigger_apply_propagates_the_adapters_bad_selector_response(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, trigger_apply

        # Copied from ../nso-adapter/tests/api/openapi_snapshot.json,
        # actions/apply 422 ErrorEnvelope response.
        bad_selector = {
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "detail": {"errors": [{"loc": ["body", "selected", "unknown"], "type": "value_error"}]},
            }
        }
        session = self._make_session(422, bad_selector)
        mock_s.return_value = session

        with self.assertRaises(AdapterError) as raised:
            trigger_apply(
                5,
                "8a2c9231-7ad8-4b17-a4b8-f5b4df745dd8",
                MappingProxyType({"unknown": 9}),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.code, "validation_error")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_trigger_apply_preserves_a_same_uuid_different_selection_conflict(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, trigger_apply

        conflict = {
            "error": {
                "code": "conflict",
                "message": "Apply attempt identity does not match the stored request",
                "detail": {"apply_attempt_id": "8a2c9231-7ad8-4b17-a4b8-f5b4df745dd8"},
            }
        }
        session = self._make_session(409, conflict)
        mock_s.return_value = session

        with self.assertRaises(AdapterError) as raised:
            trigger_apply(
                5,
                "8a2c9231-7ad8-4b17-a4b8-f5b4df745dd8",
                MappingProxyType({"vlan": 4712}),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(raised.exception.detail, conflict["error"]["detail"])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_deployment_evidence_posts_only_the_requested_attempt_ids(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import get_deployment_evidence

        attempt_id = "8a2c9231-7ad8-4b17-a4b8-f5b4df745dd8"
        evidence = {
            "device_id": 5,
            "head": None,
            "blocked": False,
            "write_work_pending": False,
            "held_jobs": [],
            "pending_generations": 0,
            "attempts": [],
            "unknown_apply_attempt_ids": [attempt_id],
        }
        session = self._make_session(200, evidence)
        mock_s.return_value = session

        result = get_deployment_evidence(5, [attempt_id])

        self.assertEqual(result, evidence)
        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"], {"apply_attempt_ids": [attempt_id]})

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_deployment_evidence_rejects_an_empty_response(self, mock_s, _cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, get_deployment_evidence

        mock_s.return_value = self._make_session(200, content=b"")

        with self.assertRaises(AdapterError) as raised:
            get_deployment_evidence(5, ["8a2c9231-7ad8-4b17-a4b8-f5b4df745dd8"])

        self.assertEqual(raised.exception.code, "invalid_response")

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


class TestCaptureWireBody(unittest.TestCase):
    """``delivery.wire_body`` counts the requests one push made, so a skipped one lies."""

    def test_a_request_with_no_json_body_is_refused_by_name(self):
        from netbox_nso_plugin.adapter_client import AdapterError, _request, capture_wire_body

        with capture_wire_body() as captured, self.assertRaises(AdapterError) as raised:
            _request("GET", "/api/v1/devices/1/static-routes")

        assert raised.exception.code == "capture_without_body"
        assert "GET /api/v1/devices/1/static-routes" in str(raised.exception)
        assert captured == []

    def test_a_json_body_is_captured_as_the_canonical_bytes(self):
        from netbox_nso_plugin.adapter_client import _request, capture_wire_body

        with capture_wire_body() as captured:
            assert _request("PUT", "/api/v1/devices/1/vlans", json={"b": 2, "a": 1}) is None

        assert captured == [b'{"a": 1, "b": 2}']
