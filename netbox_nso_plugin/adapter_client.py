# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Thin wrapper around the nso-adapter REST API (see api-contract.md).

Config resolution (per call, ~30 s in-process cache):
  - URL + non-secret settings: AdapterConnection singleton (if enabled) → PLUGINS_CONFIG/env.
  - Bearer token: PLUGINS_CONFIG/env ONLY — never the database.
"""

import contextvars
import json
import logging
import socket
import threading
import time
import weakref
from contextlib import contextmanager

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# When set, every adapter request carries ``?store_only=true``: the adapter updates its
# intent STORE but suppresses the device-touching job enqueues (shrink-removal +
# auto-apply). This is what lets intent re-sync keep its "never touches the device"
# promise — without it, a reduced snapshot PUT auto-enqueued a removal job that
# retracted FASTMAP-owned config from the real device (tracker #103, ra1.lab).
_store_only_push = contextvars.ContextVar("nso_store_only_push", default=False)


@contextmanager
def store_only_pushes():
    """Mark every adapter request in this context as store-only (no device writes)."""
    token = _store_only_push.set(True)
    try:
        yield
    finally:
        _store_only_push.reset(token)


# When set, every adapter request carries ``?delete_origin=true``: this intent push was
# born from a NetBox object DELETION, so the adapter may retract the shrink from the
# device for real. Every UNMARKED shrink is treated as an un-own and DETACHES instead
# (no-networking + sync-from, device untouched) — tracker #106: a real PUT-replace of an
# ADOPTED entry plays FASTMAP's reverse diff against the live device and stripped an IOS
# route-map filter.
_delete_origin_push = contextvars.ContextVar("nso_delete_origin_push", default=False)


@contextmanager
def delete_origin_pushes():
    """Mark every adapter request in this context as deletion-born (may retract)."""
    token = _delete_origin_push.set(True)
    try:
        yield
    finally:
        _delete_origin_push.reset(token)


# When set, every adapter request carries ``?backfill_only=true``: the adapter adopts the
# ``route_id`` of every row the payload still names, prunes the uncorrelated NULL-id rows
# that hold its replacement fence shut, and does nothing else — no removal, no tombstone, no
# job. It is what opens a fence a pending genuine deletion cannot open for itself, because
# any ordinary push omitting that route would destroy its before-image (#1503 §4.4, OQ-O-8).
# It is expressly NOT a delivery mechanism: it accepts no content and carries no authority.
_backfill_only_push = contextvars.ContextVar("nso_backfill_only_push", default=False)

# Static-route per-object deletion authority. The default is the required empty list, so
# direct client users and claims with no deletion both send the activated body shape.
_static_route_deletions = contextvars.ContextVar("nso_static_route_deletions", default=())


@contextmanager
def backfill_only_pushes():
    """Mark every adapter request in this context as an id backfill (no content, no jobs)."""
    token = _backfill_only_push.set(True)
    try:
        yield
    finally:
        _backfill_only_push.reset(token)


@contextmanager
def static_route_deletions(records):
    """Put one claim's folded static-route deletion records on its request body."""
    token = _static_route_deletions.set(tuple(records))
    try:
        yield
    finally:
        _static_route_deletions.reset(token)


# The logical operation a request belongs to (#1503 Appendix O, §4.4). It rides in a header
# rather than in every mirrored-scope body model, so one emitter covers every in-protocol
# delivery key and no call site can forget it. The adapter admits it against its own
# ``(device_id, scope)`` receipt, which is what makes a lost response resolvable by replay.
_push_seq = contextvars.ContextVar("nso_push_seq", default=None)


@contextmanager
def push_seq(seq: int):
    """Carry *seq* as ``X-Push-Seq`` on every adapter request made in this context."""
    token = _push_seq.set(int(seq))
    try:
        yield
    finally:
        _push_seq.reset(token)


_CACHE_TTL = 30  # seconds
_cfg_cache: dict = {}
_cfg_cache_lock = threading.Lock()
_cfg_cache_generation = 0
# Distinguishes "caller did not supply this field" (omit → adapter preserves it) from an
# explicit ``None`` (send null → adapter clears it). Used by set_scope's failover IPs.
_UNSET = object()
# Connect phase is capped well below the (longer) read timeout so a genuinely
# unreachable adapter fails fast, while a connected-but-slow adapter still gets
# the full read window before we conclude it is hung.
_CONNECT_TIMEOUT = 5  # seconds
# The live store incarnation, served on the job collection's 200s. Named once here so the
# consumer and the adapter cannot drift on the spelling.
STORE_INCARNATION_HEADER = "X-Store-Incarnation"
_GENERATION_PAGE_LIMIT = 500
# One UI poll can hold at most 10,000 generation rows.
_GENERATION_PAGE_MAX = 20

# Process-wide pooled session, reused across calls so connections to the (internal)
# adapter are kept alive instead of a fresh TCP+TLS handshake per request. Keyed by the
# bound ``requests.Session`` class: in production that never changes (one pooled session
# for the life of the process); the adapter test-suite patches ``requests.Session`` per
# test, which changes the class identity, so the pool transparently rebuilds from the
# patched class and every ``@patch`` is honoured.
_session = None
_session_cls = None

# A caller that must be able to ABORT its own request cannot use the pooled session: closing
# it would cut off every other request in the process. So a send under a wall-clock deadline
# builds its own (#1503 §4.2, O-P16) and binds it here for the duration of that one call.
_bound_session = contextvars.ContextVar("nso_bound_session", default=None)


class AbortableTransport(requests.adapters.HTTPAdapter):
    """An HTTP transport that can end the requests it has in flight (#1503 O-P16).

    ``Session.close()`` empties the connection pool and nothing else: a connection a worker
    already borrowed is not in the pool, so a thread blocked on a dripping response never
    notices. Only the socket ends that read, so this remembers the connection each request
    checked out and shuts its socket down on demand.
    """

    def __init__(self, *args, **kwargs):
        self._lock = threading.Lock()
        self._live: set = set()
        self._instrumented: set = set()
        self._instrumented_connections: weakref.WeakSet = weakref.WeakSet()
        self._aborted = False
        super().__init__(*args, **kwargs)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        """Return the pool this request will use, instrumented to record its connections."""
        pool = super().get_connection_with_tls_context(request, verify, proxies=proxies, cert=cert)
        with self._lock:
            fresh = pool not in self._instrumented
            self._instrumented.add(pool)
        return self._instrument(pool) if fresh else pool

    def _instrument(self, pool):
        """Wrap the pool's checkout and release, which is where a connection is identifiable."""
        checkout, release = pool._get_conn, pool._put_conn
        checked_out = threading.local()

        def _checkout_stack() -> list:
            stack = getattr(checked_out, "stack", None)
            if stack is None:
                stack = []
                checked_out.stack = stack
            return stack

        def _remove_checkout(conn):
            stack = getattr(checked_out, "stack", None)
            if not stack:
                return None
            if conn is None:
                actual = stack.pop()
            else:
                actual = None
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index] is conn:
                        actual = stack.pop(index)
                        break
            if not stack:
                del checked_out.stack
            return actual

        def _get_conn(timeout=None):
            conn = checkout(timeout)
            with self._lock:
                self._live.add(conn)
                _checkout_stack().append(conn)
                fresh = conn not in self._instrumented_connections
                self._instrumented_connections.add(conn)
                aborted = self._aborted
            if fresh:
                self._instrument_connection(conn)
            if aborted:
                self._shutdown(conn)
            return conn

        def _put_conn(conn):
            with self._lock:
                actual = _remove_checkout(conn)
                self._live.discard(actual if actual is not None else conn)
            return release(conn)

        pool._get_conn, pool._put_conn = _get_conn, _put_conn
        return pool

    def _instrument_connection(self, conn) -> None:
        """Shut down a connection that finishes connecting after this transport was aborted."""
        connect = conn.connect

        def _connect():
            result = connect()
            with self._lock:
                aborted = self._aborted
            if aborted:
                self._shutdown(conn)
            return result

        conn.connect = _connect

    @staticmethod
    def _shutdown(conn) -> None:
        """Shut down *conn* when it has a socket, leaving descriptor ownership with its pool."""
        sock = conn.sock
        if sock is None:
            return
        try:
            # Shutdown only. The pool closes its connection after the owning thread returns.
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # already gone: the request finished or the far side closed first
            pass

    def abort(self) -> None:
        """Shut down the socket of every request in flight, so its reader comes back."""
        with self._lock:
            self._aborted = True
            live = list(self._live)
        for conn in live:
            self._shutdown(conn)


def new_session(transport=None):
    """Build one session configured like the pooled one, for a caller that owns its lifetime.

    *transport* is mounted for both schemes when given, which is how a caller that must be
    able to abort its own request gets a handle on the connection it is using.
    """
    session = requests.Session()
    session.trust_env = False  # Adapter is always internal — never route through system proxy.
    if transport is not None:
        session.mount("http://", transport)
        session.mount("https://", transport)
    return session


@contextmanager
def bound_session(session):
    """Send every adapter request made in this context on *session*, not on the pool."""
    token = _bound_session.set(session)
    try:
        yield
    finally:
        _bound_session.reset(token)


def _get_session():
    """Return this context's bound session, or the process-wide pooled one."""
    global _session, _session_cls

    bound = _bound_session.get()
    if bound is not None:
        return bound
    if _session is None or _session_cls is not requests.Session:
        _session = new_session()
        _session_cls = requests.Session
    return _session


def reset_session():
    """Drop the pooled session (tests; or to force a re-read of proxy/env on next call)."""
    global _session, _session_cls
    if _session is not None:
        try:
            _session.close()
        except Exception:  # noqa: BLE001 — best-effort close; a half-built/mock session may not implement it
            pass
    _session = None
    _session_cls = None


def reset_config_cache():
    """Discard cached adapter settings so the next request resolves them again."""
    global _cfg_cache_generation
    with _cfg_cache_lock:
        _cfg_cache_generation += 1
        _cfg_cache.clear()


class AdapterError(Exception):
    """Raised when the nso-adapter returns an error or is unreachable."""

    def __init__(self, message, code=None, detail=None, status_code=None, response=None):
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.response = response

    @property
    def definitely_not_enqueued(self) -> bool:
        """Whether this failure is a deterministic rejection, so no work can have been enqueued."""
        return self.code == "configuration_error" or (type(self.status_code) is int and 400 <= self.status_code < 500)


_PUBLIC_ERROR_MESSAGES = {
    "configuration_error": "The NSO adapter is not configured. See the server log.",
    "invalid_response": "The NSO adapter returned an invalid response. See the server log.",
}
_PUBLIC_ERROR_DEFAULT = "The NSO adapter request failed. See the server log."


def public_error_message(error: AdapterError) -> str:
    """Map an adapter failure code to fixed text that is safe for HTTP responses."""
    return _PUBLIC_ERROR_MESSAGES.get(error.code, _PUBLIC_ERROR_DEFAULT)


def _resolve_config() -> dict:
    """Return resolved config dict, using a short in-process cache."""
    while True:
        now = time.monotonic()
        with _cfg_cache_lock:
            cached = _cfg_cache.get("data")
            if cached and (_cfg_cache.get("ts", 0) + _CACHE_TTL > now):
                return cached
            generation = _cfg_cache_generation

        plugin_cfg = settings.PLUGINS_CONFIG.get("netbox_nso_plugin", {})
        # Token is ALWAYS from env/PLUGINS_CONFIG — never the DB.
        token = plugin_cfg.get("adapter_token", "")

        # Try AdapterConnection singleton for URL and non-secret settings.
        conn = None
        try:
            from .models import AdapterConnection  # noqa: PLC0415

            conn = AdapterConnection.objects.filter(enabled=True).first()
        except Exception as exc:  # noqa: BLE001 — DB may be mid-migration; fall back to PLUGINS_CONFIG
            # Log so a real DB error isn't silently masked as a config fallback (which would surface
            # later as a misleading "Adapter URL is not configured").
            logger.debug("AdapterConnection lookup failed, falling back to PLUGINS_CONFIG: %s", exc)

        if conn:
            url = conn.url or plugin_cfg.get("adapter_url", "")
            verify_tls = conn.verify_tls
            ca_cert_path = conn.ca_cert_path or None
            timeout = conn.timeout_seconds
        else:
            url = plugin_cfg.get("adapter_url", "")
            verify_tls = True
            ca_cert_path = None
            timeout = 30

        data = {
            "url": url.rstrip("/") if url else "",
            "token": token,
            "verify_tls": verify_tls,
            "ca_cert_path": ca_cert_path,
            "timeout": timeout,
        }
        with _cfg_cache_lock:
            if generation != _cfg_cache_generation:
                continue
            _cfg_cache["data"] = data
            _cfg_cache["ts"] = now
            return data


# When set, a request is not made at all: its JSON body is recorded and ``None`` comes back.
# The claim needs the EXACT body the client would send, because that is what the adapter
# digests into its receipt (#1503 Appendix O, §4.4), and the wrapper each endpoint puts
# around a payload lives in this module. Capturing through the real call is what keeps the
# two sides on one definition instead of a second table of scope-to-envelope names.
_capture_body: contextvars.ContextVar[list | None] = contextvars.ContextVar("nso_capture_body", default=None)


@contextmanager
def capture_wire_body():
    """Record the serialized JSON bytes of every request, and send none of them."""
    sink: list = []
    token = _capture_body.set(sink)
    try:
        yield sink
    finally:
        _capture_body.reset(token)


def _serialize_json_body(body) -> bytes:
    """Serialize one canonical JSON body once, for both capture and transport.

    Claims persist their payload in JSONB, which does not preserve object key order. Sorting
    makes the restored value reproduce the bytes the adapter received without storing a
    second copy of the request.
    """
    return json.dumps(body, allow_nan=False, sort_keys=True).encode()


def _attach_serialized_json(kwargs) -> None:
    """Attach the canonical wire bytes while retaining the structured request body."""
    if "json" not in kwargs:
        return
    # Requests sends ``data`` when both are present. Keep ``json`` for transport
    # instrumentation and tests. The canonical ``data`` value goes on the socket.
    kwargs["data"] = _serialize_json_body(kwargs["json"])


def _request(method, path, **kwargs):
    sink = _capture_body.get()
    if sink is not None:
        if "json" not in kwargs:
            # Skipping it silently would make the caller's "expected exactly one" count lie.
            raise AdapterError(
                f"cannot capture a wire body for {method} {path}: the request carries no JSON body.",
                code="capture_without_body",
            )
        sink.append(_serialize_json_body(kwargs["json"]))
        return None
    resp = _request_response(method, path, **kwargs)
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise AdapterError("Adapter returned a response that is not valid JSON.", code="invalid_response") from exc


def _error_envelope(resp):
    """Return the adapter's ``{"error": {...}}`` member, or ``{}`` when the body is not that shape.

    An error body reaches us from any intermediary (a proxy 502, a gateway), so unlike the
    adapter's own typed payloads its shape is genuinely unknown and must be checked here.
    """
    try:
        body = resp.json()
    except ValueError:
        return {}
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


def _request_response(method, path, **kwargs):
    """Send one adapter request and return the raw response, for callers that read a header."""
    cfg = _resolve_config()

    if not cfg["url"]:
        raise AdapterError("Adapter URL is not configured.", code="configuration_error")
    if not cfg["token"]:
        raise AdapterError("Adapter token is not configured.", code="configuration_error")

    url = f"{cfg['url']}{path}"
    headers = {"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"}
    _attach_serialized_json(kwargs)

    seq = _push_seq.get()
    if seq is not None:
        headers["X-Push-Seq"] = str(seq)

    if _store_only_push.get():
        params = dict(kwargs.pop("params", None) or {})
        params["store_only"] = "true"
        kwargs["params"] = params

    if _delete_origin_push.get():
        params = dict(kwargs.pop("params", None) or {})
        params["delete_origin"] = "true"
        kwargs["params"] = params

    if _backfill_only_push.get():
        params = dict(kwargs.pop("params", None) or {})
        params["backfill_only"] = "true"
        kwargs["params"] = params

    if not cfg["verify_tls"]:
        verify = False
        logger.warning("TLS verification is DISABLED for adapter requests (verify_tls=False) — MITM exposure.")
    elif cfg["ca_cert_path"]:
        verify = cfg["ca_cert_path"]
    else:
        verify = True

    # READSEM S4 (R5-5): callers may pass an endpoint-specific (connect, read)
    # timeout — e.g. the tab's live read-state fetch — instead of the configured one.
    override = kwargs.pop("timeout", None)
    if override is not None:
        connect_timeout, read_timeout = override
    else:
        read_timeout = cfg["timeout"]
        connect_timeout = min(_CONNECT_TIMEOUT, read_timeout)
    try:
        session = _get_session()
        resp = session.request(
            method, url, headers=headers, timeout=(connect_timeout, read_timeout), verify=verify, **kwargs
        )
    except requests.exceptions.ReadTimeout as exc:
        # Connected but no response within the read window — the adapter is up but
        # hung (e.g. blocked event loop). Surface this distinctly so it is NOT
        # mistaken for "unreachable" and is visible in logs.
        logger.warning(
            "nso-adapter accepted the connection but did not respond within %ss for %s %s — it may be hung",
            read_timeout,
            method,
            path,
        )
        raise AdapterError(
            f"Adapter did not respond within {read_timeout}s (it may be hung).", code="nso_timeout"
        ) from exc
    except requests.exceptions.ConnectTimeout as exc:
        raise AdapterError(f"Adapter connect timed out after {connect_timeout}s.", code="nso_unreachable") from exc
    except requests.RequestException as exc:
        # Never the exception text: requests' InvalidHeader repeats the offending header
        # value, so a malformed configured token would land in the log verbatim.
        logger.warning("nso-adapter request failed for %s %s (%s)", method, path, type(exc).__name__)
        raise AdapterError(
            f"Adapter unreachable ({type(exc).__name__}); see the server log.", code="nso_unreachable"
        ) from exc

    if not resp.ok:
        err = _error_envelope(resp)
        try:
            response = resp.json()
        except ValueError:
            response = None
        detail = err.get("detail")
        # ``or`` not ``.get(key, default)``: the adapter emits nulls, and a present null
        # would otherwise beat the fallback and leave the operator an empty message.
        raise AdapterError(
            err.get("message") or resp.text,
            code=str(err.get("code") or resp.status_code),
            detail=detail if isinstance(detail, dict) else None,
            status_code=resp.status_code,
            response=response,
        )
    return resp


def onboard_device(nso_instance, nso_device_name, netbox_device_id):
    """POST /api/v1/devices — onboard a device."""
    return _request(
        "POST",
        "/api/v1/devices",
        json={
            "nso_instance": nso_instance,
            "nso_device_name": nso_device_name,
            "netbox_device_id": netbox_device_id,
        },
    )


def provision_device(
    nso_instance, device_name, address, ned_id, authgroup, *, admin_state="unlocked", sync=True, oob_ip=None
):
    """POST /api/v1/devices/provision — create the device in NSO and bring it up.

    Returns {"ok", "steps", "device_id"}. ``netbox_device_id`` is intentionally
    omitted: the plugin creates the NSODeviceManagement row afterwards, whose
    post_save signal does the adapter mapping + scope + sync-notify.

    ``oob_ip`` (optional) is the device's out-of-band fallback address. When set, the
    adapter probes the primary first and, if unreachable, bootstraps NSO over OOB so a
    fresh device (whose in-band loopback is not yet configured) is still onboardable.
    """
    payload = {
        "nso_instance": nso_instance,
        "device_name": device_name,
        "address": address,
        "ned_id": ned_id,
        "authgroup": authgroup,
        "admin_state": admin_state,
        "sync": sync,
    }
    if oob_ip is not None:
        payload["oob_ip"] = oob_ip
    return _request("POST", "/api/v1/devices/provision", json=payload)


def get_failover_config():
    """GET /api/v1/config/failover — the adapter's effective failover config.

    Includes ``deployment_enabled`` — the adapter's static ``scheduler.enable_failover``
    master switch. When it is False the whole failover feature is off at the deployment
    level (the probe loop isn't registered and onboarding won't bootstrap over OOB), so the
    runtime ``enabled`` toggle (and the plugin's failover settings) have no effect until the
    adapter operator sets ``enable_failover: true`` and restarts.
    """
    return _request("GET", "/api/v1/config/failover")


def put_failover_config(payload):
    """PUT /api/v1/config/failover — push the global mgmt-IP failover tuning singleton.

    ``payload`` is a dict of the failover knobs (enabled, primary_probe_interval,
    oob_probe_interval, failure_threshold, success_threshold, probe_timeout,
    probe_concurrency, max_flips_per_tick, sync_from_after_switch). The adapter applies
    them on its next base tick. Returns the adapter's effective config dict.
    """
    return _request("PUT", "/api/v1/config/failover", json=payload)


def set_scope(
    adapter_device_id, attributes, auto_apply=False, sync_before_apply=True, *, primary_ip=_UNSET, oob_ip=_UNSET
):
    """PUT /api/v1/devices/{id}/scope — update managed attributes and settings.

    ``primary_ip`` / ``oob_ip`` (optional) carry the device's management addresses for the
    failover loop. They follow explicit-null semantics: pass a host string to set, ``None``
    to clear, or omit entirely (``_UNSET``) to leave the adapter's stored value untouched.
    """
    payload = {
        "attributes": attributes,
        "auto_apply": auto_apply,
        "sync_before_apply": sync_before_apply,
    }
    if primary_ip is not _UNSET:
        payload["primary_ip"] = primary_ip
    if oob_ip is not _UNSET:
        payload["oob_ip"] = oob_ip
    return _request("PUT", f"/api/v1/devices/{adapter_device_id}/scope", json=payload)


def patch_device(adapter_device_id, nso_instance=None, nso_device_name=None):
    """PATCH /api/v1/devices/{id} — re-key device mapping."""
    payload = {}
    if nso_instance is not None:
        payload["nso_instance"] = nso_instance
    if nso_device_name is not None:
        payload["nso_device_name"] = nso_device_name
    return _request("PATCH", f"/api/v1/devices/{adapter_device_id}", json=payload)


def delete_device(adapter_device_id):
    """DELETE /api/v1/devices/{id} — offboard device."""
    _request("DELETE", f"/api/v1/devices/{adapter_device_id}")


def get_device(adapter_device_id):
    """GET /api/v1/devices/{id}."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}")


def list_devices():
    """GET /api/v1/devices — every device the adapter knows, across all NSO instances."""
    return _request("GET", "/api/v1/devices")


def get_intent_receipts(*, device_id: int | None = None, section: str | None = None) -> dict:
    """GET the adapter's per-key receipts and fleet-wide restore watermarks."""
    params = {}
    if device_id is not None:
        params["device_id"] = int(device_id)
    if section is not None:
        params["section"] = section
    return _request("GET", "/api/v1/intent-receipts", params=params)


def get_interfaces(adapter_device_id):
    """GET /api/v1/devices/{id}/interfaces."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/interfaces")


def get_lag_topology(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/lag-topology."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/lag-topology")


def get_lag_config(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/lag-config."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/lag-config")


def get_vlan_database(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/vlan-database."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/vlan-database")


def get_switchport(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/switchport."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/switchport")


def get_svi(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/svi — L3 VLAN interfaces (SVIs/IRBs)."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/svi")


def get_subinterface(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/subinterface — dot1q L3 subinterfaces."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/subinterface")


def get_interface_mtu(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/interface-mtu — per-interface MTU (Phase 2b)."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/interface-mtu")


def put_interface_mtu_intent(adapter_device_id, interfaces):
    """PUT /api/v1/devices/{id}/interface-mtu-intent — push full MTU intent (Phase 2b).

    ``interfaces`` is a list of dicts:
      [{"interface_name": "X", "mtu": 9216, "ip_mtu": 9000, "mpls_mtu": None,
        "accepted_at": "...Z"}, ...]
    Empty list clears all MTU intent for the device. Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/interface-mtu-intent",
        json={"interfaces": interfaces},
    )


def put_bfd_intent(adapter_device_id, interfaces):
    """PUT /api/v1/devices/{id}/bfd-intent — push full per-interface BFD intent.

    ``interfaces`` is a list of dicts:
      [{"interface_name": "X", "min_tx": 300, "min_rx": 300, "multiplier": 3,
        "micro_bfd": false, "accepted_at": "...Z"}, ...]
    Empty list clears all BFD intent for the device. Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/bfd-intent",
        json={"interfaces": interfaces},
    )


def put_vlan_intent(adapter_device_id, vlans):
    """PUT /api/v1/devices/{id}/vlan-intent — push full VLAN-database intent (write).

    ``vlans`` is a list of dicts: [{"vlan_id": 2213, "name": "X", "accepted_at": "...Z"}, ...].
    Empty list clears all VLAN intent for the device. Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/vlan-intent",
        json={"vlans": vlans},
    )


def apply_switchport_config(adapter_device_id, interfaces):
    """POST /api/v1/devices/{id}/switchport/apply — push + apply L2 switchport intent.

    ``interfaces`` is a list of dicts:
      [{"interface_name": "Gi0/1", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}, ...]
    Empty list clears the device's switchport-reconciler service. Returns the apply envelope.
    """
    return _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/switchport/apply",
        json={"interfaces": interfaces},
    )


def get_interface_ips(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/interface-ips."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/interface-ips")


def get_intent_summary(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/intent-summary → per-scope adapter intent counts."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/intent-summary")


def get_apply_diff(adapter_device_id: int, outformat: str = "native") -> dict:
    """GET /api/v1/devices/{id}/actions/apply-diff → per-scope diff (NSO dry-run, no commit).

    ``outformat="native"``: device-native rendering (CLI for cli NEDs, edit-config XML
    for netconf NEDs). ``outformat="cli"``: NSO's NED-uniform ``+``/``-`` tree diff —
    what the apply-preview renders through the vendored diff2html.
    """
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/actions/apply-diff", params={"outformat": outformat})


# READSEM S4 D4/R2-4: per-adapter capability memo for /interfaces-doc. A route-level 404
# (client code "404"/"route_not_found" — NOT the ErrorEnvelope's "not_found", which means
# device-absent and must raise) marks the adapter as pre-S4; the legacy list endpoint is
# then used directly until the TTL lapses (same 30s discipline as _resolve_config).
_ifdoc_capability: dict = {}


def reset_interfaces_doc_capability() -> None:
    """Drop the /interfaces-doc capability memo (tests; or after an adapter upgrade)."""
    _ifdoc_capability.clear()


def _legacy_interfaces_as_doc(adapter_device_id: int) -> dict:
    """Wrap the legacy bare-list /interfaces as a KEY-ABSENT doc.

    No read_state key ⇒ the reconcile gate takes the legacy path (D3).
    """
    interfaces = _request("GET", f"/api/v1/devices/{adapter_device_id}/interfaces")
    return {"device_id": adapter_device_id, "interfaces": interfaces}


def get_interfaces_doc(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/interfaces-doc — the S4 object doc (interfaces + read_state).

    S3-floor compatibility: a ROUTE-level 404 falls back to the legacy list wrapped as a
    key-absent doc and memoizes the capability; a device-level 404 ("not_found") raises
    on either path.
    """
    cfg = _resolve_config()
    memo = _ifdoc_capability.get(cfg["url"])
    if memo is not None and memo["legacy"] and (time.monotonic() - memo["at"]) < _CACHE_TTL:
        return _legacy_interfaces_as_doc(adapter_device_id)
    try:
        return _request("GET", f"/api/v1/devices/{adapter_device_id}/interfaces-doc")
    except AdapterError as exc:
        if exc.code in ("404", "route_not_found"):
            _ifdoc_capability[cfg["url"]] = {"legacy": True, "at": time.monotonic()}
            return _legacy_interfaces_as_doc(adapter_device_id)
        raise


def get_device_read_state(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/read-state — all 19 families' declared read states.

    READSEM S4 (D8b/R5-5): called LIVE on tab render beside ``get_device`` with a
    SHORT endpoint-specific budget (connect 5s / read 5s — never the configurable
    default) so a hung adapter cannot stall the page; its failure is handled
    separately (family chips fall back to persisted rows).
    """
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/read-state", timeout=(5, 5))


def get_snmp_config(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/snmp-config."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/snmp-config")


def get_logging_config(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/logging-config."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/logging-config")


def get_neds(adapter_instance_id: str) -> list:
    """GET /api/v1/nso-instances/{id}/neds — available NED packages on the instance."""
    return _request("GET", f"/api/v1/nso-instances/{adapter_instance_id}/neds")


def list_instance_devices(adapter_instance_id: str) -> list:
    """GET /api/v1/nso-instances/{id}/devices — NSO device inventory (onboarded x-ref)."""
    return _request("GET", f"/api/v1/nso-instances/{adapter_instance_id}/devices")


def get_static_routes(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/static-routes."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/static-routes")


def get_isis_interfaces(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/isis-interfaces → dict with 'processes'/'interfaces' (+ read_state).

    READSEM S4 D4: a 404 RAISES (device-not-found is its only meaning) — the old
    404→empty fabrication carried no read_state and masqueraded as authoritative-empty
    under the reconcile gate. The shape rebuild passes ``read_state`` through.
    """
    data = _request("GET", f"/api/v1/devices/{adapter_device_id}/isis-interfaces")
    out = {"processes": data.get("processes", []), "interfaces": data.get("interfaces", [])}
    if "read_state" in data:
        out["read_state"] = data["read_state"]
    return out


def get_l2_services(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/l2-services → dict with 'services' (Nokia epipe/vpls + SAPs).

    READSEM S4 D4: a 404 RAISES — no fabricated empties (see get_isis_interfaces).
    The shape rebuild passes ``read_state`` through.
    """
    data = _request("GET", f"/api/v1/devices/{adapter_device_id}/l2-services")
    out = {"services": data.get("services", [])}
    if "read_state" in data:
        out["read_state"] = data["read_state"]
    return out


def get_bfd(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/bfd → dict with 'interfaces' (per-interface BFD).

    READSEM S4 D4: a 404 RAISES — no fabricated empties (see get_isis_interfaces).
    The shape rebuild passes ``read_state`` through.
    """
    data = _request("GET", f"/api/v1/devices/{adapter_device_id}/bfd")
    out = {"interfaces": data.get("interfaces", [])}
    if "read_state" in data:
        out["read_state"] = data["read_state"]
    return out


def get_bgp_config(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/bgp-config → BGP router hierarchy dict.

    READSEM S4 D4: a 404 RAISES — no fabricated empties (see get_isis_interfaces).
    """
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/bgp-config")


def get_route_policy(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/route-policy → route-policy objects dict.

    READSEM S4 D4: a 404 RAISES — no fabricated empties (see get_isis_interfaces).
    """
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/route-policy")


def get_ospf(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/ospf → dict with 'instances' and 'interfaces' lists.

    READSEM S4 D4: a 404 RAISES — no fabricated empties (see get_isis_interfaces).
    """
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/ospf")


def get_redistribution(adapter_device_id: int) -> dict:
    """GET /api/v1/devices/{id}/redistribution → dict with 'entries' list.

    READSEM S4 D4: a 404 RAISES — no fabricated empties (see get_isis_interfaces).
    """
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/redistribution")


def put_ospf_intent(adapter_device_id: int, payload: dict) -> dict:
    """PUT /api/v1/devices/{id}/ospf-intent → push full OSPF intent snapshot."""
    return _request("PUT", f"/api/v1/devices/{adapter_device_id}/ospf-intent", json=payload)


def get_state(adapter_device_id):
    """GET /api/v1/devices/{id}/state."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/state")


def trigger_sync(adapter_device_id):
    """POST /api/v1/devices/{id}/actions/sync → job_id."""
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/sync")


def trigger_sync_from_nso(adapter_device_id):
    """POST /api/v1/devices/{id}/actions/sync-from-nso → job_id.

    S5a: comprehensive CDB-only mirror read — no device round-trip (Sync Now runs the
    device ``sync-from`` first; this button re-reads what NSO already knows).
    """
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/sync-from-nso")


def trigger_detect_drift(adapter_device_id):
    """POST /api/v1/devices/{id}/actions/detect-drift → job_id."""
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/detect-drift")


def trigger_connect(adapter_device_id):
    """POST /api/v1/devices/{id}/actions/connect → job_id."""
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/connect")


def trigger_force_removal(adapter_device_id, scope):
    """POST /api/v1/devices/{id}/actions/force-removal → job_id.

    Re-runs *scope*'s removal with the adapter's collateral guard DISABLED — the
    operator override after reviewing a ``removal_blocked_collateral`` job's orphan
    list and dry-run preview. The orphaned service rows are deliberately retracted
    from the live device.
    """
    return _request("POST", f"/api/v1/devices/{adapter_device_id}/actions/force-removal", json={"scope": scope})


def retry_generation(adapter_device_id: int, generation_id: int) -> dict:
    """Retry the exact blocked generation the operator inspected."""
    return _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/actions/retry-generation",
        json={"generation_id": generation_id},
    )


def abandon_generation(adapter_device_id: int, generation_id: int) -> dict:
    """Abandon the exact blocked generation the operator inspected."""
    return _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/actions/abandon-generation",
        json={"generation_id": generation_id},
    )


def get_device_apply_state(adapter_device_id: int) -> dict:
    """Return the device-wide executable Apply head and barrier state."""
    return _request("GET", f"/api/v1/devices/{adapter_device_id}/apply-state")


def sync_notify(adapter_device_id):
    """POST /api/v1/devices/{id}/sync-notify — notify adapter of scope/intent change.

    Triggers an immediate sync so the user sees results without waiting for the
    scheduled poll. A 409 conflict returns the adapter's detail payload, which names the queued incumbent.
    """
    try:
        return _request("POST", f"/api/v1/devices/{adapter_device_id}/sync-notify")
    except AdapterError as exc:
        if exc.code == "conflict":
            return exc.detail  # existing job info — caller may log it
        raise


#: Members the adapter's JobOut model types ``dict | None`` (../nso-adapter/nso_adapter/api/jobs.py).
_JOB_MAPPING_MEMBERS = ("result", "error", "context")


def _validated_job(job, what):
    """Return *job* once it matches JobOut's shape, else refuse the whole payload.

    The producer cannot emit a scalar in these members, so one is a broken adapter, not a
    value to tolerate. Refusing here means every reader downstream may treat them as
    ``dict | None`` without restating the check.
    """
    if not isinstance(job, dict):
        raise AdapterError(f"Adapter returned a malformed {what}.", code="invalid_response")
    for member in _JOB_MAPPING_MEMBERS:
        value = job.get(member)
        if value is not None and not isinstance(value, dict):
            raise AdapterError(
                f"Adapter returned a malformed {what}: {member} is not an object.",
                code="invalid_response",
            )
    return job


def _validated_jobs(jobs, what):
    """Return *jobs* once every entry matches JobOut's shape, else refuse the whole page."""
    if not isinstance(jobs, list):
        raise AdapterError(f"Adapter returned a malformed {what}.", code="invalid_response")
    for job in jobs:
        _validated_job(job, what)
    return jobs


def get_job(job_id):
    """GET /api/v1/jobs/{id}."""
    return _validated_job(_request("GET", f"/api/v1/jobs/{job_id}"), "job")


def list_jobs(adapter_device_id):
    """GET /api/v1/jobs?device_id={id} — the device's jobs, most-recent-first."""
    return _validated_jobs(_request("GET", "/api/v1/jobs", params={"device_id": adapter_device_id}), "jobs listing")


def list_device_generations(adapter_device_id, *, since_seq=None):
    """GET a bounded set of ascending generation pages after ``since_seq``."""
    if since_seq is not None and (type(since_seq) is not int or since_seq < 0):
        raise ValueError("since_seq must be a non-negative integer")
    generations = []
    generation_ids = set()
    last_seq = since_seq
    for _ in range(_GENERATION_PAGE_MAX):
        params = {"limit": _GENERATION_PAGE_LIMIT}
        if last_seq is not None:
            params["since_seq"] = last_seq
        page = _request("GET", f"/api/v1/devices/{adapter_device_id}/generations", params=params)
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise AdapterError("Adapter returned a malformed generations listing.", code="invalid_response")
        for row in page:
            generation_id = row.get("generation_id")
            seq = row.get("seq")
            job_id = row.get("job_id")
            status = row.get("status")
            settlement_cohort = row.get("settlement_cohort")
            if (
                "settlement_cohort" not in row
                or type(generation_id) is not int
                or generation_id <= 0
                or generation_id in generation_ids
                or type(seq) is not int
                or seq <= 0
                or (job_id is not None and (type(job_id) is not int or job_id <= 0))
                or not isinstance(status, str)
                or not status
                or (last_seq is not None and seq <= last_seq)
                or (settlement_cohort is not None and (type(settlement_cohort) is not int or settlement_cohort <= 0))
            ):
                raise AdapterError("Adapter returned a malformed generations listing.", code="invalid_response")
            generation_ids.add(generation_id)
            last_seq = seq
        generations.extend(page)
        if len(page) < _GENERATION_PAGE_LIMIT:
            return generations
    raise AdapterError("Adapter returned more generation pages than one read may fetch.", code="invalid_response")


def get_settlement_feed(adapter_device_id, *, after_settle_seq, limit):
    """GET the device's ordered settlement feed → ``(jobs, store_incarnation)``.

    Ascending by ``settle_seq``, which the adapter allocates under a per-device lock held
    to COMMIT, so the page is in commit order. Queued and running jobs carry no sequence
    and the ``> cursor`` predicate is NULL-false, so they are invisible until terminal.

    The incarnation rides a header rather than the body because the page that proves a
    store is gone is an EMPTY one: a cursor past the end of a restarted sequence returns
    no rows at all, and a per-row field would say nothing in exactly that state.
    """
    resp = _request_response(
        "GET",
        "/api/v1/jobs",
        params={
            "device_id": adapter_device_id,
            "order": "asc",
            "after_settle_seq": after_settle_seq,
            "limit": limit,
        },
    )
    incarnation = resp.headers.get(STORE_INCARNATION_HEADER)
    if not incarnation:
        # Without it the cursor epoch cannot be compared, and applying a cursor whose
        # store may be dead is the silent-skip this header exists to prevent.
        raise AdapterError(
            f"Adapter served the settlement feed without a {STORE_INCARNATION_HEADER} header.",
            code="missing_store_incarnation",
        )
    # This reader decodes the body itself (it needs the header too), so it owes the same
    # typed refusal _request gives every other caller: a gateway's HTML 200 must not reach
    # the settlement walk as a bare requests JSONDecodeError.
    try:
        page = resp.json() if resp.content else []
    except ValueError as exc:
        raise AdapterError(
            "Adapter returned a settlement feed that is not valid JSON.", code="invalid_response"
        ) from exc
    return _validated_jobs(page, "settlement feed"), incarnation


def get_static_route_intent(adapter_device_id):
    """GET /api/v1/devices/{id}/static-route-intent — re-serve what the last PUT echoed.

    The lost-response recovery path: the adapter commits its store write before it
    answers, so a response lost in flight leaves the pusher holding a committed intent it
    recorded no expectation for, and this is the only other way to obtain one.

    StaticRouteIntentOut always carries a ``routes`` list. Degrading a malformed body to an
    empty one would record ZERO expectations and silently mis-settle, so it is refused
    instead: the settlement stall bound counts a read-back it cannot obtain, but an empty
    one looks like a legitimate answer and is counted as settled.
    """
    echoed = _request("GET", f"/api/v1/devices/{adapter_device_id}/static-route-intent")
    if not isinstance(echoed, dict) or not isinstance(echoed.get("routes"), list):
        raise AdapterError("Adapter returned a malformed static-route intent read-back.", code="invalid_response")
    return echoed


def put_intent(adapter_device_id, attributes):
    """PUT /api/v1/devices/{id}/intent — push full intent snapshot.

    ``attributes`` is a list of dicts:
      [{"interface": "...", "attribute": "...", "intent_value": ..., "accepted_at": "...Z"}, ...]
    Empty list clears the mirror.
    Returns {"device_id": ..., "attribute_count": N, "updated_at": "...Z"}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/intent",
        json={"attributes": attributes},
    )


def put_ip_intent(adapter_device_id, addresses):
    """PUT /api/v1/devices/{id}/ip-intent — push full IP intent snapshot.

    ``addresses`` is a list of dicts:
      [{"interface": "...", "address": "ip/plen", "family": "ipv4|ipv6",
        "secondary": bool, "vrf": "...", "accepted_at": "...Z"}, ...]
    Empty list clears the mirror.
    Returns {"device_id": ..., "address_count": N, "updated_at": "...Z"}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/ip-intent",
        json={"addresses": addresses},
    )


def put_snmp_intent(adapter_device_id, communities, v3_users, hosts, system_info):
    """PUT /api/v1/devices/{id}/snmp-intent — push full SNMP intent snapshot.

    ``communities`` list of dicts: {label, vault_ref, access, acl?, accepted_at?}
    ``v3_users``    list of dicts: {username, auth_vault_ref?, priv_vault_ref?, accepted_at?}
    ``hosts``       list of dicts: {address, version, notify_type, community_or_user, accepted_at?}
    ``system_info`` dict or None:  {location?, contact?, accepted_at?}

    Empty lists / None clears the respective sections.
    Returns {"device_id": ..., "community_count": N, ..., "updated_at": "...Z"}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/snmp-intent",
        json={
            "communities": communities,
            "v3_users": v3_users,
            "hosts": hosts,
            "system_info": system_info,
        },
    )


def set_secret(vault_ref, values):
    """POST /api/v1/secrets — merge-write secret fields at a Vault ref.

    ``vault_ref`` "mount/path" (multi-field) or "mount/path#key" (that field);
    ``values`` {field: plaintext}. The plaintext transits this one call and is
    never persisted plugin-side. Returns {"vault_ref", "version", "hashes"}
    where hashes are sha256[:16] fingerprints per field.
    """
    return _request("POST", "/api/v1/secrets", json={"vault_ref": vault_ref, "values": values})


def verify_secret(vault_ref):
    """POST /api/v1/secrets/verify — resolve a ref without exposing values.

    Returns {"vault_ref", "exists", "fields", "hashes", "version"}.
    """
    return _request("POST", "/api/v1/secrets/verify", json={"vault_ref": vault_ref})


def harvest_community(adapter_device_id, community_hash, vault_ref):
    """POST /api/v1/devices/{id}/secrets/harvest-community.

    Adopt a device-held community string into Vault by its read-mirror
    fingerprint. Returns {"vault_ref", "secret_hash", "version", "access", "acl"}.
    """
    return _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/secrets/harvest-community",
        json={"community_hash": community_hash, "vault_ref": vault_ref},
    )


def put_static_route_intent(adapter_device_id, routes):
    """PUT /api/v1/devices/{id}/static-route-intent — push full static route intent.

    ``routes`` is a list of dicts:
      [{"vrf": "", "prefix": "10.0.0.0/8", "next_hop": "192.168.1.1",
        "metric": None, "permanent": None, "tag": None,
        "accepted_at": "...Z"}, ...]
    ``deleted_routes`` comes from the active claim's deletion context. It is always present,
    including as an empty list for direct callers and store-only or backfill-only pushes.
    An empty ``routes`` list clears all static route intent for the device.
    Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/static-route-intent",
        json={"routes": routes, "deleted_routes": list(_static_route_deletions.get())},
    )


def put_l2_sap_intent(adapter_device_id, saps):
    """PUT /api/v1/devices/{id}/l2-sap-intent — push full Nokia L2 SAP intent.

    ``saps`` is a list of dicts:
      [{"service_name": "TL", "service_type": "epipe", "sap_id": "lag-60:3999",
        "port": "lag-60", "outer_tag": 3999, "inner_tag": None,
        "accepted_at": "...Z"}, ...]
    Empty list clears all L2 SAP intent for the device.
    Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/l2-sap-intent",
        json={"saps": saps},
    )


def put_logging_intent(adapter_device_id, hosts, local_levels):
    """PUT /api/v1/devices/{id}/logging-intent — push full logging intent.

    ``hosts`` is a list of dicts:
      [{"address": "10.0.0.1", "port": None, "severity": "informational",
        "facility": "", "transport": "", "vrf": "", "source": "",
        "accepted_at": "...Z"}, ...]
    Empty list clears all logging host intent for the device.

    ``local_levels`` is the owned local-severity singleton: a dict of set OC
    severities ({"console_severity": "CRITICAL", ...}) or None. The key is ALWAYS
    sent — the adapter reads it presence-sensitively, and None (JSON null) means
    "un-manage" (delete the levels intent + retract the owned leaves). Omitting
    the key would mean "preserve", which is never what the plugin's full-replace
    snapshot intends.
    Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/logging-intent",
        json={"hosts": hosts, "local_levels": local_levels},
    )


def put_svi_intent(adapter_device_id, interfaces):
    """PUT /api/v1/devices/{id}/svi-intent — push full SVI/IRB intent snapshot.

    ``interfaces`` is a list of dicts:
      [{"interface_name": "Vlan100", "vlan_id": 100, "type": "svi", "vrf": "",
        "accepted_at": "...Z"}, ...]
    Empty list clears all SVI intent for the device.
    Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/svi-intent",
        json={"interfaces": interfaces},
    )


def put_subinterface_intent(adapter_device_id, interfaces):
    """PUT /api/v1/devices/{id}/subinterface-intent — push full subinterface intent.

    ``interfaces`` is a list of dicts:
      [{"interface_name": "GigabitEthernet0/1.100", "parent_interface": "GigabitEthernet0/1",
        "dot1q_vlan": 100, "type": "subinterface", "vrf": "", "accepted_at": "...Z"}, ...]
    Empty list clears all subinterface intent for the device.
    Returns {"device_id": ..., "count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/subinterface-intent",
        json={"interfaces": interfaces},
    )


def apply_lag_config(adapter_device_id, bundles):
    """POST /api/v1/devices/{id}/lag-config/apply — push + apply full LACP bundle intent.

    ``bundles`` is a list of dicts:
      [{"name": "Port-channel1", "lag_id": 1, "min_links": 2, "system_priority": 100,
        "system_id": "...", "timer": "fast", "admin_key": 33,
        "members": [{"interface_name": "Gi0/1", "mode": "active", "port_priority": 128}]}, ...]
    Empty list clears all LACP bundles owned by the device's lag-reconciler service.
    Returns the adapter apply envelope ({"status": "deployed", ...} or an error envelope).
    """
    return _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/lag-config/apply",
        json={"bundles": bundles},
    )


def put_isis_interface_intent(adapter_device_id, interfaces, processes=None):
    """PUT /api/v1/devices/{id}/isis-interface-intent — push full IS-IS intent.

    ``interfaces`` is a list of interface intent dicts.
    ``processes`` is an optional list of process intent dicts:
      [{"process_tag": "", "net": "49.0001.0001.0001.00", "is_type": "level-2",
        "metric_style": "wide", "overload_bit": None,
        "area_auth_type": None, "area_auth_key": None,
        "domain_auth_type": None, "domain_auth_key": None,
        "accepted_at": "...Z"}, ...]
    Empty lists clear all intent of the respective type for the device.
    Returns {"device_id": ..., "interface_count": N, "process_count": M}.
    """
    payload = {"interfaces": interfaces, "processes": processes or []}
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/isis-interface-intent",
        json=payload,
    )


def put_isis_flex_algo_intent(adapter_device_id, flex_algos):
    """PUT /api/v1/devices/{id}/isis-flex-algo-intent — push full Flex-Algo intent.

    ``flex_algos`` is a list of dicts:
      [{"process_tag": "CORE", "algo_id": 130, "metric_type": "delay-metric",
        "priority": 200, "admin_group_exclude": "RED", ...}, ...]
    Empty list clears all Flex-Algo intent for the device.
    Returns {"device_id": ..., "flex_algo_count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/isis-flex-algo-intent",
        json={"flex_algos": flex_algos},
    )


def put_bgp_intent(adapter_device_id, routers):
    """PUT /api/v1/devices/{id}/bgp-intent — push full BGP intent snapshot.

    ``routers`` is a list of dicts following the BgpIntentUpdate schema:
      [{"asn": "65100", "scopes": [{"vrf": "", "address_families": [...], "peers": [...]}]}]
    Empty list clears all BGP intent for the device.
    Returns {"device_id": ..., "router_count": N}.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/bgp-intent",
        json={"routers": routers},
    )


def put_route_policy_intent(adapter_device_id, objects):
    """PUT /api/v1/devices/{id}/route-policy-intent — push accepted route-policy objects.

    ``objects`` is a list of dicts:
      [{"family": "prefix_list", "name": "PL-RFC1918", "entries": [...], "accepted": true}]
    Returns the updated intent state for all objects on this device.
    """
    return _request(
        "PUT",
        f"/api/v1/devices/{adapter_device_id}/route-policy-intent",
        json={"objects": objects},
    )


def get_device_capability(adapter_device_id, refresh=False):
    """GET /api/v1/devices/{id}/capability — the route-policy capability verdict for this device.

    ``refresh=False`` (default) is the cheap cache-only read used to render the panel badge;
    ``refresh=True`` forces a fresh NSO probe ("check now"). Returns the adapter dict
    ``{known, ned_id, sw_version, elements:[{scope,name,status,detail,source}]}``. Fails open:
    on any adapter error returns ``{known: False, elements: []}`` so the UI degrades to
    "unknown" rather than erroring.
    """
    suffix = "?refresh=true" if refresh else ""
    try:
        return _request("GET", f"/api/v1/devices/{adapter_device_id}/capability{suffix}")
    except AdapterError as exc:
        logger.warning("capability read failed for device %s: %s", adapter_device_id, exc)
        return {"known": False, "ned_id": "", "sw_version": "", "elements": []}


def preflight_route_policy(
    adapter_device_id,
    community_members=(),
    set_keys=(),
    match_keys=(),
    aspath_names=(),
    refresh=True,
    raise_on_error=False,
):
    """POST /api/v1/devices/{id}/route-policy/preflight — check an attach against the matrix.

    ``refresh=True`` (default — the authoritative attach-time check) probes the device once;
    ``refresh=False`` reads the last-learned verdict. Returns
    ``{known, fully_supported, unsupported:[{scope,element,status,detail}], ned_id, sw_version}``.
    Fails open: any adapter error → ``{known: False, fully_supported: True, unsupported: []}`` so
    an unreachable adapter never blocks an attach (we block only on a KNOWN-negative verdict).

    ``raise_on_error=True`` re-raises instead of failing open — used by the per-row panel
    annotation so it can short-circuit the whole render on the FIRST adapter failure rather than
    paying a timeout per device row.
    """
    suffix = "?refresh=false" if not refresh else ""
    try:
        return _request(
            "POST",
            f"/api/v1/devices/{adapter_device_id}/route-policy/preflight{suffix}",
            json={
                "community_members": list(community_members),
                "set_keys": list(set_keys),
                "match_keys": list(match_keys),
                "aspath_names": list(aspath_names),
            },
        )
    except AdapterError as exc:
        logger.warning("route-policy preflight failed for device %s: %s", adapter_device_id, exc)
        if raise_on_error:
            raise
        return {"known": False, "fully_supported": True, "unsupported": [], "coverage_unknown": False}


def trigger_apply(adapter_device_id, apply_attempt_id, selected):
    """Promote the exact stored intent receipts named by a frozen selector.

    Raise AdapterError when a device job is already queued or running.
    """
    return _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/actions/apply",
        json={"apply_attempt_id": str(apply_attempt_id), "selected": dict(selected)},
    )


DEPLOYMENT_EVIDENCE_FIELDS = frozenset(
    {
        "device_id",
        "head",
        "blocked",
        "write_work_pending",
        "held_jobs",
        "pending_generations",
        "attempts",
        "unknown_apply_attempt_ids",
    }
)


def get_deployment_evidence(adapter_device_id, apply_attempt_ids):
    """Return barrier and durable evidence for the named Apply attempts."""
    evidence = _request(
        "POST",
        f"/api/v1/devices/{adapter_device_id}/deployment-evidence",
        json={"apply_attempt_ids": [str(attempt_id) for attempt_id in apply_attempt_ids]},
    )
    if not isinstance(evidence, dict):
        raise AdapterError("Adapter returned no deployment evidence body.", code="invalid_response")
    return evidence


def list_nso_devices(nso_instance_id: str) -> list[dict]:
    """GET /api/v1/nso-instances/{id}/devices — enriched device list.

    Returns a list of dicts, one per NSO device, with the fields:
      name, address, ned_id, platform, auth_group, admin_state,
      onboarded, onboarded_device_id, onboarded_netbox_device_id.
    All fields are always present; nullable fields are null when absent.
    Returns [] if the adapter returns an empty list.
    """
    result = _request("GET", f"/api/v1/nso-instances/{nso_instance_id}/devices")
    return result or []


def get_device_by_nso(nso_instance_id: str, nso_device_name: str) -> dict | None:
    """GET /api/v1/devices/by-nso — resolve (instance, name) → adapter device.

    Returns the device dict (same shape as GET /api/v1/devices/{id}) on hit.
    Returns None on 404 (device not onboarded).
    Raises AdapterError on any other error.
    """
    try:
        return _request(
            "GET",
            "/api/v1/devices/by-nso",
            params={"instance": nso_instance_id, "name": nso_device_name},
        )
    except AdapterError as exc:
        if exc.code == "not_found":
            return None
        raise
