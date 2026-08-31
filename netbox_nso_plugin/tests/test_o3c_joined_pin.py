# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""O3.19: join plugin emission to adapter marking and removal execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from django.conf import settings
from django.db import connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, tag

from ._outbox_case import make_device, own_route, without_commit_drain
from ._settlement_adapter import LoopbackOnlySession
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test.yaml"


def _adapter_commit_from_workflow() -> str:
    """Read the joined adapter pin from the workflow that checks it out."""
    match = re.search(
        r"repository: marcinpsk/nso-adapter\s+ref: ([0-9a-f]{40})",
        _WORKFLOW.read_text(),
    )
    if match is None:
        raise AssertionError(f"O3c adapter commit is missing or malformed in {_WORKFLOW}")
    return match.group(1)


_ADAPTER_RUNTIME_DIGEST = "88461e15ffa5d24b6e10f25395e2dfbf9eb97f1bf95eb1d4638b3c200c06c2d1"
_ADAPTER_ROOT = Path(__file__).resolve().parents[2].parent / ".o3c-adapter"
_DSN_CREDENTIAL = re.compile(r"(?<=://)[^:/@\s]+:[^@/\s]+(?=@)")
_SR_PATH = "/restconf/data/static-route-reconciler:static-route-config"
_SR_ROOT = "static-route-reconciler:static-route-config"
_STATE_READ_PATH = "/restconf/data/network-state-export:device-state-read/run"
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "partial", "failed"})


def _wait_until(probe, message: str, *, timeout: float = 45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"{message}; last observation: {last!r}")


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _adapter_database_name() -> str:
    # Per-run derivation: concurrent suites and xdist workers must never share the store.
    plugin_test_db = str(connection.settings_dict["NAME"])
    if not plugin_test_db.startswith("test_"):
        raise AssertionError(f"O3c refuses to derive an adapter database from {plugin_test_db!r}")
    name = f"{plugin_test_db}_adapter"
    # PostgreSQL truncates at NAMEDATALEN-1 with only a notice, which would silently map two
    # base names onto one store and let reset_database() drop a database another worker owns.
    if len(name.encode()) > 63:
        raise AssertionError(f"the derived adapter database {name!r} exceeds PostgreSQL's 63 bytes")
    return name


class _AdapterWireSession(LoopbackOnlySession):
    """Use a real requests transport, limited to one adapter listener."""

    allowed_port: int | None = None
    records: list[dict] = []
    lock = threading.Lock()

    @classmethod
    def reset(cls, port: int) -> None:
        cls.allowed_port = port
        with cls.lock:
            cls.records = []

    @classmethod
    def snapshot(cls) -> list[dict]:
        """Return the wire records captured up to this point."""
        with cls.lock:
            return list(cls.records)

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != self.allowed_port:
            raise requests.exceptions.ConnectionError("adapter network blocked outside the O3c listener")
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        with self.lock:
            self.records.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "headers": dict(request.headers),
                    "body": json.loads(body) if body else None,
                }
            )
        return super().send(request, **kwargs)


class _RestconfState:
    """Stateful static-route service and device views at the NSO boundary."""

    def __init__(self):
        self.lock = threading.Lock()
        self.service: dict[str, list[dict]] = {}
        self.device: dict[str, list[dict]] = {}
        self.calls: list[dict] = []
        self.held_device: str | None = None
        self.removal_read_started = threading.Event()
        self.allow_removal = threading.Event()

    @staticmethod
    def key(entry: dict) -> tuple[str, str, str]:
        return (entry.get("vrf") or "", entry.get("prefix") or "", entry.get("next-hop") or "")

    def seed(self, device_name: str, routes: list[dict]) -> None:
        with self.lock:
            self.service[device_name] = [dict(route) for route in routes]
            self.device[device_name] = [dict(route) for route in routes]

    def hold_removal_for(self, device_name: str) -> None:
        self.held_device = device_name
        self.removal_read_started.clear()
        self.allow_removal.clear()

    def service_document(self, device_name: str) -> dict:
        with self.lock:
            routes = [dict(route) for route in self.service.get(device_name, [])]
        return {_SR_ROOT: [{"device": device_name, "route": routes}]}

    def write(self, method: str, device_name: str, routes: list[dict], *, dry_run: bool, query: str) -> None:
        record = {
            "method": method,
            "device": device_name,
            "routes": [dict(route) for route in routes],
            "dry_run": dry_run,
            "query": query,
        }
        with self.lock:
            self.calls.append(record)
            if dry_run:
                return
            owned = {self.key(route) for route in self.service.get(device_name, [])}
            replacement = {self.key(route) for route in routes}
            by_key = {
                self.key(route): dict(route)
                for route in self.device.get(device_name, [])
                if self.key(route) not in owned - replacement
            }
            for route in routes:
                by_key[self.key(route)] = dict(route)
            self.service[device_name] = [dict(route) for route in routes]
            self.device[device_name] = list(by_key.values())

    def device_section(self, device_name: str) -> dict:
        with self.lock:
            routes = [dict(route) for route in self.device.get(device_name, [])]
        return {"status": "ok", "route": routes}

    def retractions(self, device_name: str) -> list[dict]:
        with self.lock:
            return [
                dict(call)
                for call in self.calls
                if call["device"] == device_name and call["method"] == "PUT" and not call["dry_run"]
            ]


class _RestconfHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _RestconfState:
        return self.server.state

    def log_message(self, *args):
        pass

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/yang-data+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _service_device(path: str) -> str | None:
        if not path.startswith(f"{_SR_PATH}="):
            return None
        return unquote(path.removeprefix(f"{_SR_PATH}="))

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        device_name = self._service_device(parsed.path)
        if device_name is None:
            self._send_json(404, {"errors": "unsupported test surface"})
            return
        if device_name == self.state.held_device and not self.state.allow_removal.is_set():
            self.state.removal_read_started.set()
            self.state.allow_removal.wait(45)
        self._send_json(200, self.state.service_document(device_name))

    def _write_service(self, method: str) -> None:
        parsed = urlparse(self.path)
        body = self._json_body()
        entries = body.get(_SR_ROOT) or []
        if not entries:
            self._send_json(404, {"errors": "unsupported test surface"})
            return
        device_name = entries[0]["device"]
        dry_run = "dry-run=" in parsed.query
        routes = entries[0].get("route") or []
        self.state.write(method, device_name, routes, dry_run=dry_run, query=parsed.query)
        if dry_run:
            self._send_json(
                200,
                {"dry-run-result": {"native": {"device": [{"name": device_name, "data": ""}]}}},
            )
            return
        self._send_empty(204)

    def do_PUT(self):  # noqa: N802
        self._write_service("PUT")

    def do_PATCH(self):  # noqa: N802
        self._write_service("PATCH")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == _STATE_READ_PATH:
            body = self._json_body()
            action_input = body.get("network-state-export:input") or {}
            device_name = action_input["device"]
            sections = {
                family: (
                    self.state.device_section(device_name) if family == "static-route" else {"status": "unsupported"}
                )
                for family in action_input.get("family") or []
            }
            self._send_json(
                200,
                {
                    "network-state-export:output": {
                        "atomic": True,
                        "device-name": device_name,
                        "last-updated": "2026-08-13T00:00:00Z",
                        **sections,
                    }
                },
            )
            return
        if parsed.path.startswith("/api/plugins/"):
            self._json_body()
            self._send_empty(204)
            return
        self._json_body()
        self._send_json(404, {"errors": "unsupported test surface"})


class _O3CEnvironment:
    """Own the adapter process, its isolated store, and the RESTCONF fake."""

    def __init__(self):
        self.adapter_port = 0
        self.db_name = _adapter_database_name()
        self.serving = False
        self.restconf = _RestconfState()
        self.restconf_server = ThreadingHTTPServer(("127.0.0.1", 0), _RestconfHandler)
        self.restconf_server.daemon_threads = True
        self.restconf_server.state = self.restconf
        self.restconf_thread = threading.Thread(target=self.restconf_server.serve_forever, daemon=True)
        self.tempdir = tempfile.TemporaryDirectory(prefix="nso-o3c-")
        self.process: subprocess.Popen | None = None
        self.log_handle = None

    @property
    def adapter_url(self) -> str:
        return f"http://127.0.0.1:{self.adapter_port}"

    @property
    def restconf_url(self) -> str:
        host, port = self.restconf_server.server_address[:2]
        return f"http://{host}:{port}"

    @staticmethod
    def _database_settings() -> dict:
        return settings.DATABASES["default"]

    @staticmethod
    def _runtime_digest() -> str:
        files = [
            *(_ADAPTER_ROOT / "nso_adapter").rglob("*.py"),
            *(_ADAPTER_ROOT / "alembic").rglob("*.py"),
            *(
                _ADAPTER_ROOT / name
                for name in ("scripts/docker-entrypoint.sh", "alembic.ini", "pyproject.toml", "uv.lock")
            ),
        ]
        manifest = []
        for path in sorted(files, key=lambda item: item.relative_to(_ADAPTER_ROOT).as_posix()):
            relative = path.relative_to(_ADAPTER_ROOT).as_posix()
            if not path.is_file():
                raise AssertionError(f"O3c adapter checkout is missing {relative}")
            manifest.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
        return hashlib.sha256("".join(manifest).encode()).hexdigest()

    def _database_url(self) -> str:
        configured = self._database_settings()
        required = {name: configured.get(name) for name in ("HOST", "USER", "PASSWORD")}
        missing = [name for name, value in required.items() if value in (None, "")]
        if missing:
            raise AssertionError(f"O3c adapter database settings are missing: {', '.join(missing)}")
        port = configured.get("PORT")
        if port in (None, ""):
            with connection.cursor() as cursor:
                cursor.execute("SELECT inet_server_port()")
                port = cursor.fetchone()[0]
        user = quote(str(required["USER"]), safe="")
        password = quote(str(required["PASSWORD"]), safe="")
        host = str(required["HOST"])
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{self.db_name}"

    def reset_database(self) -> None:
        quoted = connection.ops.quote_name(self.db_name)
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
            cursor.execute(f"CREATE DATABASE {quoted}")

    def drop_database(self) -> None:
        quoted = connection.ops.quote_name(self.db_name)
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")

    def query_store(self, statement: str, params=()) -> list[tuple]:
        connect_params = connection.get_connection_params()
        connect_params["dbname"] = self.db_name
        store_connection = connection.Database.connect(**connect_params)
        try:
            with store_connection.cursor() as cursor:
                cursor.execute(statement, params)
                return list(cursor.fetchall())
        finally:
            store_connection.close()

    def _write_config(self) -> tuple[Path, str]:
        token = "o3c-adapter-token"
        config_path = Path(self.tempdir.name) / "config.yaml"
        config_path.write_text(
            "\n".join(
                (
                    "secrets:",
                    "  provider: local",
                    "nso_instances:",
                    "  - name: o3c-pin",
                    f"    base_url: {json.dumps(self.restconf_url)}",
                    '    username_ref: "O3C_NSO_USERNAME"',
                    '    password_ref: "O3C_NSO_PASSWORD"',
                    "netbox:",
                    f"  base_url: {json.dumps(self.restconf_url)}",
                    '  api_token_ref: "O3C_NETBOX_TOKEN"',
                    "api:",
                    '  adapter_token_ref: "O3C_ADAPTER_TOKEN"',
                    f"database_url: {json.dumps(self._database_url())}",
                    "log_level: WARNING",
                    "log_format: console",
                    "scheduler:",
                    "  enable_nso_streams: false",
                    "  orphan_reap_interval: 0",
                    "  static_route_reclaim_interval: 0",
                    "  worker_concurrency: 1",
                    "",
                )
            )
        )
        return config_path, token

    def start(self) -> None:
        if not _ADAPTER_ROOT.is_dir():
            raise AssertionError(f"O3c adapter worktree is missing at {_ADAPTER_ROOT}")
        digest = self._runtime_digest()
        if digest != _ADAPTER_RUNTIME_DIGEST:
            expected_commit = _adapter_commit_from_workflow()
            raise AssertionError(
                f"O3c requires adapter {expected_commit}; runtime digest is {digest}, "
                f"expected {_ADAPTER_RUNTIME_DIGEST}"
            )
        uv = shutil.which("uv")
        if uv is None:
            raise AssertionError("O3c requires uv inside the NetBox devcontainer")
        self.reset_database()
        self.restconf_thread.start()
        self.serving = True
        config_path, token = self._write_config()
        environment = {
            **os.environ,
            "CONFIG_FILE": str(config_path),
            "DATABASE_URL": self._database_url(),
            "O3C_ADAPTER_TOKEN": token,
            "O3C_NETBOX_TOKEN": "o3c-netbox-token",
            "O3C_NSO_USERNAME": "o3c-user",
            "O3C_NSO_PASSWORD": "o3c-password",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        log_path = Path(self.tempdir.name) / "adapter.log"
        self.log_handle = log_path.open("w+b")
        self.adapter_port = _free_loopback_port()
        self.process = subprocess.Popen(
            [
                uv,
                "run",
                "--native-tls",
                "--frozen",
                "--",
                "scripts/docker-entrypoint.sh",
                "uvicorn",
                "nso_adapter.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.adapter_port),
            ],
            cwd=_ADAPTER_ROOT,
            env=environment,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
        )
        self._wait_ready(log_path)

    def _wait_ready(self, log_path: Path) -> None:
        def ready():
            if self.process.poll() is not None:
                raise AssertionError(f"O3c adapter exited during startup:\n{self.log_text(log_path)}")
            try:
                client = HTTPConnection("127.0.0.1", self.adapter_port, timeout=1)
                client.request("GET", "/healthz")
                response = client.getresponse()
                response.read()
                client.close()
                return response.status == 200
            except OSError:
                return False

        try:
            _wait_until(ready, "O3c adapter did not become ready", timeout=180)
        except AssertionError as exc:
            raise AssertionError(f"{exc}\nAdapter output:\n{self.log_text(log_path)}") from exc

    def log_text(self, path: Path | None = None) -> str:
        if self.log_handle is not None:
            self.log_handle.flush()
        log_path = path or Path(self.tempdir.name) / "adapter.log"
        if not log_path.exists():
            return ""
        # The adapter runs against the store DSN, and a connection failure both logs it
        # and calls this method, whose output rides into assertion messages. Redact before
        # truncating: a cut inside the DSN drops the ``://`` the lookbehind matches on.
        text = _DSN_CREDENTIAL.sub("***:***", log_path.read_text(errors="replace"))
        return text[-12000:]

    def stop(self) -> None:
        self.restconf.allow_removal.set()
        # Independent releases, in order: one that raises must not strand the socket, the
        # database or the temp directory for every later attempt. `setUpClass` calls this on
        # the preflight-failure path, where a stuck adapter is exactly what is expected.
        # The first failure is re-raised at the end.
        failure: BaseException | None = None
        for release in (
            self._stop_process,
            self._close_log,
            self._shutdown_restconf,
            self.restconf_server.server_close,
            self._join_restconf_thread,
            self.drop_database,
            self.tempdir.cleanup,
        ):
            try:
                release()
            except BaseException as exc:  # noqa: BLE001 - every release still has to run
                failure = failure or exc
        if failure is not None:
            raise failure

    def _stop_process(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)

    def _close_log(self) -> None:
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    def _shutdown_restconf(self) -> None:
        if self.serving:
            self.restconf_server.shutdown()

    def _join_restconf_thread(self) -> None:
        if self.restconf_thread.is_alive():
            self.restconf_thread.join(timeout=10)


@tag("o3c", "cross_repository")
class TestO3CJoinedCrossRepositoryPin(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Run one joined edit through the plugin and pinned adapter processes."""

    @classmethod
    def setUpClass(cls):
        if not _ADAPTER_ROOT.is_dir():
            raise unittest.SkipTest(f"O3c adapter worktree is missing at {_ADAPTER_ROOT}")
        super().setUpClass()
        cls.environment = _O3CEnvironment()
        try:
            cls.environment.start()
        except BaseException as start_error:
            cleanup_failures = []
            for label, cleanup in (
                ("O3C environment cleanup", cls.environment.stop),
                ("Django class teardown", super().tearDownClass),
                ("Django class cleanups", cls.doClassCleanups),
            ):
                try:
                    cleanup()
                except Exception as cleanup_error:  # noqa: BLE001 - preserve the primary setup failure
                    cleanup_failures.append(f"{label} failed with {type(cleanup_error).__name__}")
                if label == "Django class cleanups":
                    for exception_type, _exception, _traceback in cls.tearDown_exceptions:
                        cleanup_failures.append(f"{label} failed with {exception_type.__name__}")
            for failure in cleanup_failures:
                start_error.add_note(failure)
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.environment.stop()
        finally:
            super().tearDownClass()

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin import adapter_client

        self.adapter_config = {
            "url": self.environment.adapter_url,
            "token": "o3c-adapter-token",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        _AdapterWireSession.reset(self.environment.adapter_port)
        session_patch = patch("netbox_nso_plugin.adapter_client.requests.Session", _AdapterWireSession)
        config_patch = patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=self.adapter_config)
        session_patch.start()
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.addCleanup(session_patch.stop)
        adapter_client.reset_session()
        self.addCleanup(adapter_client.reset_session)
        self.addCleanup(self.environment.restconf.allow_removal.set)

    def _link(self, device, instance, name: str):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import NSODeviceManagement

        management = NSODeviceManagement.objects.create(
            device=device,
            nso_instance=instance,
            nso_device_name=name,
            manage_routing=True,
            manage_static=True,
            sync_before_apply=False,
            onboard_status="provisioning",
        )
        linked = adapter_client.onboard_device(instance.adapter_instance_id, name, device.pk)
        adapter_client.set_scope(linked["id"], [], auto_apply=False, sync_before_apply=False)
        NSODeviceManagement.objects.filter(pk=management.pk).update(
            adapter_device_id=linked["id"],
            adapter_source_epoch=linked.get("source_epoch"),
            source_epoch_aware=linked.get("source_epoch") is not None,
            onboard_status="",
        )
        management.refresh_from_db()
        return management

    def _enable_auto_apply(self, management) -> None:
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import NSODeviceManagement

        adapter_client.set_scope(management.adapter_device_id, [], auto_apply=True, sync_before_apply=False)
        NSODeviceManagement.objects.filter(pk=management.pk).update(auto_apply=True, sync_before_apply=False)
        management.refresh_from_db()

    def _terminal_job(self, adapter_device_id: int, job_type: str):
        from netbox_nso_plugin import adapter_client

        jobs = adapter_client.list_jobs(adapter_device_id)
        matches = [job for job in jobs if job.get("type") == job_type]
        terminal = [job for job in matches if job.get("status") in _TERMINAL_JOB_STATUSES]
        return terminal[0] if terminal else None

    def test_pinned_adapter_exposes_the_paginated_generation_listing(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import NSOInstance

        instance = NSOInstance.objects.create(name="o3c-pin", adapter_instance_id="o3c-pin")
        management = self._link(make_device("generation-pin"), instance, "generation-pin")
        _AdapterWireSession.reset(self.environment.adapter_port)

        assert adapter_client.list_device_generations(management.adapter_device_id) == []

        [request] = _AdapterWireSession.snapshot()
        parsed = urlparse(request["url"])
        assert parsed.path == f"/api/v1/devices/{management.adapter_device_id}/generations"
        assert parse_qs(parsed.query) == {"limit": ["500"]}

    def test_one_joined_edit_retracts_only_the_removed_device_and_settles_the_retained_device(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOStaticRouteState
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        from ._outbox_case import content_update
        from ._static_route_case import _assign_and_accept

        instance = NSOInstance.objects.create(name="o3c-pin", adapter_instance_id="o3c-pin")
        removed_device = make_device("o3c-removed", index=1)
        retained_device = make_device("o3c-retained", index=2)
        removed = self._link(removed_device, instance, "o3c-removed")
        retained = self._link(retained_device, instance, "o3c-retained")

        route = own_route(removed, "198.18.3.0/28", "198.18.3.1")
        with without_commit_drain(), transaction.atomic():
            _assign_and_accept(route, retained_device)

        assert drain.drain_key(removed_device.pk, "static_route", chain=0) == drain.SUCCEEDED
        assert drain.drain_key(retained_device.pk, "static_route", chain=0) == drain.SUCCEEDED
        for state in NSOStaticRouteState.objects.filter(static_route=route):
            content_update(state, status="in_sync")
        self._enable_auto_apply(removed)
        self._enable_auto_apply(retained)

        old_route = {"vrf": "", "prefix": "198.18.3.0/28", "next-hop": "198.18.3.1", "metric": 1}
        self.environment.restconf.seed(removed.nso_device_name, [old_route])
        self.environment.restconf.seed(retained.nso_device_name, [old_route])
        self.environment.restconf.hold_removal_for(removed.nso_device_name)
        _AdapterWireSession.reset(self.environment.adapter_port)

        with transaction.atomic():
            route.next_hop = "198.18.3.2"
            route.save()
            route.devices.remove(removed_device)

        assert self.environment.restconf.removal_read_started.wait(45), (
            "the real adapter worker did not reach the removed device's RESTCONF read\n" + self.environment.log_text()
        )

        removed_puts = [
            record
            for record in _AdapterWireSession.snapshot()
            if record["method"] == "PUT"
            and f"/devices/{removed.adapter_device_id}/static-route-intent" in record["url"]
        ]
        assert len(removed_puts) == 1, removed_puts
        removed_wire = removed_puts[0]
        assert removed_wire["headers"].get("X-Push-Seq")
        assert removed_wire["body"]["routes"] == []
        assert [record["route_id"] for record in removed_wire["body"]["deleted_routes"]] == [route.pk]

        tombstones = self.environment.query_store(
            """
            SELECT tomb.route_id, tomb.marking, tomb.vrf, tomb.prefix, tomb.next_hop
            FROM static_route_tombstone AS tomb
            JOIN devices AS device ON device.id = tomb.device_id
            WHERE device.netbox_device_id = %s
            ORDER BY tomb.id
            """,
            [removed_device.pk],
        )
        assert tombstones == [(route.pk, "delete_origin", "", "198.18.3.0/28", "198.18.3.1")]

        self.environment.restconf.allow_removal.set()
        retraction = _wait_until(
            lambda: self.environment.restconf.retractions(removed.nso_device_name),
            "the removal never reached the RESTCONF PUT boundary",
        )[0]
        assert retraction["routes"] == []
        assert "no-networking" not in retraction["query"]

        removed_job = _wait_until(
            lambda: self._terminal_job(removed.adapter_device_id, "removal"),
            "the real adapter removal job did not finish",
        )
        retained_job = _wait_until(
            lambda: self._terminal_job(retained.adapter_device_id, "apply"),
            "the retained device's real adapter apply did not finish",
        )
        assert removed_job["status"] == "succeeded", removed_job
        assert retained_job["status"] == "succeeded", retained_job

        consume_static_route_settlements(retained)
        removed_outcome = consume_static_route_settlements(removed)
        retained_state = NSOStaticRouteState.objects.get(management=retained, static_route=route)
        assert retained_state.status == "in_sync"
        assert retained_state.nso_next_hop == "198.18.3.2"
        assert not NSOStaticRouteState.objects.filter(management=removed, static_route=route).exists()
        assert not removed_outcome.stalled

        removed.refresh_from_db()
        assert removed.settle_cursor_seq >= removed_job["settle_seq"]
        assert (
            self.environment.query_store(
                """
            SELECT tomb.id
            FROM static_route_tombstone AS tomb
            JOIN devices AS device ON device.id = tomb.device_id
            WHERE device.netbox_device_id = %s
            """,
                [removed_device.pk],
            )
            == []
        )

        retained_pushes = [
            record
            for record in _AdapterWireSession.snapshot()
            if record["method"] == "PUT"
            and f"/devices/{retained.adapter_device_id}/static-route-intent" in record["url"]
        ]
        assert len(retained_pushes) == 1, retained_pushes
        assert retained_pushes[0]["body"]["deleted_routes"] == []
        assert [entry["route_id"] for entry in retained_pushes[0]["body"]["routes"]] == [route.pk]
        assert retained_pushes[0]["body"]["routes"][0]["next_hop"] == "198.18.3.2"
        assert NSODeviceManagement.objects.get(pk=removed.pk).adapter_device_id == removed.adapter_device_id


@tag("o3c")
class TestO3CEnvironmentFailFast(SimpleTestCase):
    """Prove a failed preflight tears down without hanging the suite."""

    databases = {"default"}

    def test_joined_class_start_failure_releases_django_setup_and_keeps_the_original_error(self):
        from django.conf import settings

        events = []

        class FailingEnvironment:
            def start(self):
                events.append("start")
                raise RuntimeError("adapter start failed")

            def stop(self):
                events.append("stop")
                raise RuntimeError("adapter stop failed")

        class FailingJoinedCase(TestO3CJoinedCrossRepositoryPin):
            _overridden_settings = {"EMAIL_SUBJECT_PREFIX": "joined-pin-start"}

            def test_probe(self):
                pass

        def fail_class_cleanup():
            events.append("class-cleanup")
            raise LookupError("class cleanup failed")

        FailingJoinedCase.addClassCleanup(fail_class_cleanup)

        original_prefix = settings.EMAIL_SUBJECT_PREFIX
        with (
            patch(f"{__name__}._ADAPTER_ROOT", Path.cwd()),
            patch(f"{__name__}._O3CEnvironment", FailingEnvironment),
            self.assertRaisesRegex(RuntimeError, "^adapter start failed$") as raised,
        ):
            FailingJoinedCase.setUpClass()

        assert events == ["start", "stop", "class-cleanup"]
        assert settings.EMAIL_SUBJECT_PREFIX == original_prefix
        assert raised.exception.__notes__ == [
            "O3C environment cleanup failed with RuntimeError",
            "Django class cleanups failed with LookupError",
        ]

    def test_adapter_port_is_selected_only_when_starting_the_process(self):
        with patch(f"{__name__}._adapter_database_name", return_value="test_o3c_port_selection"):
            environment = _O3CEnvironment()
        self.addCleanup(environment.stop)

        assert environment.adapter_port == 0

    def test_a_missing_adapter_worktree_skips_the_joined_class(self):
        class MissingAdapterCase(TestO3CJoinedCrossRepositoryPin):
            def test_probe(self):
                pass

        result = unittest.TestResult()
        with tempfile.TemporaryDirectory() as directory:
            missing_root = Path(directory) / "missing-adapter"
            with (
                patch(f"{__name__}._adapter_database_name", lambda: f"test_o3c_missing_{Path(directory).name}"),
                patch(f"{__name__}._ADAPTER_ROOT", missing_root),
            ):
                unittest.TestSuite([MissingAdapterCase("test_probe")]).run(result)

        assert result.skipped, result.errors
        assert result.errors == []
        assert "O3c adapter worktree is missing" in result.skipped[0][1]

    def test_log_text_redacts_the_store_credential(self):
        environment = _O3CEnvironment()
        self.addCleanup(environment.stop)
        log_path = Path(environment.tempdir.name) / "adapter.log"
        log_path.write_text('connection to "postgresql+asyncpg://nso_user:s3cr3t-pw@db-host:5432/store" failed\n')

        text = environment.log_text(log_path)

        assert "s3cr3t-pw" not in text
        assert "nso_user" not in text
        assert "postgresql+asyncpg://***:***@db-host:5432/store" in text

    def test_log_text_redacts_a_credential_that_straddles_the_truncation(self):
        """Truncating first would drop the ``://`` the redaction's lookbehind needs."""
        environment = _O3CEnvironment()
        self.addCleanup(environment.stop)
        log_path = Path(environment.tempdir.name) / "adapter.log"
        # Place the cut between the scheme and the password: the kept tail is exactly 12000
        # characters and starts at the password itself.
        tail = 's3cr3t-pw@db-host:5432/store" failed\n'
        head = "A" * 5000 + 'connection to "postgresql+asyncpg://nso_user:'
        log_path.write_text(head + tail + "x" * (12000 - len(tail)))

        text = environment.log_text(log_path)

        assert "s3cr3t-pw" not in text, "the password survived the truncate-then-redact order"

    def test_stop_releases_every_resource_when_an_earlier_step_raises(self):
        """One failing release must not strand the socket and the temp directory."""
        environment = _O3CEnvironment()
        tempdir_name = environment.tempdir.name

        with patch.object(_O3CEnvironment, "drop_database", side_effect=RuntimeError("store still connected")):
            with self.assertRaises(RuntimeError):
                environment.stop()

        assert environment.restconf_server.socket.fileno() == -1, "the listening socket leaked"
        assert not Path(tempdir_name).exists(), "the temp directory leaked"

    @staticmethod
    def _database_exists(name: str) -> bool:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
            return cursor.fetchone() is not None

    def test_stop_releases_every_resource_when_the_process_will_not_die(self):
        """The three process/log/socket steps sat ABOVE the release loop, so a child that
        outlives both signals stranded the socket, the store database and the temp directory
        for every later attempt — on the preflight-failure path, where a stuck adapter is
        exactly what is expected."""

        class _StuckProcess:
            """A child that answers neither terminate() nor kill()."""

            def poll(self):
                return None

            def terminate(self):
                """The signal lands and the child ignores it."""

            def kill(self):
                """So does this one."""

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="adapter", timeout=timeout)

        environment = _O3CEnvironment()
        tempdir_name = environment.tempdir.name
        environment.reset_database()
        assert self._database_exists(environment.db_name), "the fixture never created the store"
        environment.process = _StuckProcess()

        with self.assertRaises(subprocess.TimeoutExpired):
            environment.stop()

        assert environment.restconf_server.socket.fileno() == -1, "the listening socket leaked"
        assert not self._database_exists(environment.db_name), "the adapter store database leaked"
        assert not Path(tempdir_name).exists(), "the temp directory leaked"

    def test_the_derived_adapter_database_name_fits_postgresql(self):
        """PostgreSQL truncates at 63 bytes with only a notice, so two runs could collide."""
        long_name = "test_" + "n" * 70
        with patch.dict(connection.settings_dict, {"NAME": long_name}):
            with self.assertRaisesRegex(AssertionError, "63"):
                _adapter_database_name()

    def test_stop_returns_before_the_restconf_thread_ever_served(self):
        environment = _O3CEnvironment()

        def stop_and_close():
            try:
                environment.stop()
            finally:
                connection.close()

        stopper = threading.Thread(target=stop_and_close, daemon=True)
        stopper.start()
        stopper.join(timeout=10)
        assert not stopper.is_alive(), "stop() hung: shutdown() waited for a serve loop that never ran"
