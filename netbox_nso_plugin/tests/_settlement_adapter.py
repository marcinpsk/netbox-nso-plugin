# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""A real HTTP adapter double serving the settlement feed and the intent read-back.

Real socket, real ``requests`` transport, real response headers — so the consumer under
test exercises its own parameter building, header reading and error mapping rather than a
canned return value. It is also the only shape that a **separate process** can talk to,
which the durable-stall pin needs.

The store's incarnation is minted in :class:`SettlementStore`'s constructor and never
mutated. A store rebuild is therefore modelled the way the adapter actually behaves — the
server is stopped and a **new lifespan** is started with a new store — never by patching
an incarnation onto a live one, which would prove a scenario the system does not support.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import requests

STORE_INCARNATION_HEADER = "X-Store-Incarnation"


class LoopbackOnlySession(requests.Session):
    """A real transport that reaches the in-test double and nothing else.

    The suite is hermetic by an autouse fixture that fails every adapter request, because
    the devcontainer's configured ``adapter_url`` is the live adapter. These tests need a
    real socket, so they narrow that guard instead of lifting it: loopback is allowed, the
    live adapter is still refused exactly as before.
    """

    def request(self, method, url, *args, **kwargs):
        if not str(url).startswith("http://127.0.0.1:"):
            raise requests.exceptions.ConnectionError("adapter network blocked in tests")
        return super().request(method, url, *args, **kwargs)


class SettlementStore:
    """One adapter store: its incarnation, its jobs and its static-route intent echo."""

    def __init__(self):
        self.incarnation = uuid4().hex
        self.jobs: list[dict] = []
        self.routes: dict[int, list[dict]] = {}
        #: adapter device rows, keyed by adapter device id
        self.devices: dict[int, dict] = {}
        #: status the read-back GET answers with; anything but 200 makes it unresolvable
        self.intent_status = 200
        #: when set, the read-back answers 200 with a body StaticRouteIntentOut cannot produce
        self.intent_malformed = False
        #: status ``GET /api/v1/devices`` answers with — the maintenance tick's shared snapshot
        self.devices_status = 200
        #: called (on the server thread) while the read-back is in flight, so a test can
        #: commit a concurrent operator edit inside the consumer's real HTTP window
        self.on_readback = None
        #: adapter device ids whose ASCENDING (settlement) feed answers 503. Scoped to the
        #: ascending page on purpose: the coarse per-scope settle reads the same collection
        #: descending, and a knob that broke both could not tell the two channels apart.
        self.feed_error_devices: set[int] = set()
        #: adapter device ids whose DESCENDING (plain collection) jobs page answers 503.
        #: That page is the apply-activity probe, and it fails independently of the feed:
        #: a walk can drain while the probe times out.
        self.jobs_error_devices: set[int] = set()
        #: ids of jobs the ASCENDING page serves even though they hold no sequence: the
        #: feed contract broken the way only the adapter itself can break it
        self.unsequenced_in_feed: set[str] = set()
        #: every feed request the consumer made, as ``(device_id, after_settle_seq, limit)``
        self.feed_requests: list[tuple[int, int, int]] = []
        self.readback_requests: list[int] = []
        #: every request this store served, as ``(method, path)``
        self.requests: list[tuple[str, str]] = []
        self._next_seq: dict[int, int] = {}
        self._next_id = 0
        self._next_device_id = 0

    def add_device(self, *, nso_instance: str, nso_device_name: str, netbox_device_id=None, device_id=None) -> dict:
        """Register an adapter device row, minting an id unless one is forced."""
        if device_id is None:
            self._next_device_id += 1
            device_id = self._next_device_id
        else:
            self._next_device_id = max(self._next_device_id, device_id)
        row = {
            "id": device_id,
            "nso_instance": nso_instance,
            "nso_device_name": nso_device_name,
            "netbox_device_id": netbox_device_id,
            "source_epoch": 1,
        }
        self.devices[device_id] = row
        return row

    def _job(self, device_id: int, *, settle_seq, status, results, job_type, extra=None):
        self._next_id += 1
        result = None if results is None else {"static_route_results": results}
        if extra:
            result = {**(result or {}), **extra}
        return {
            "id": f"job-{self._next_id}",
            "type": job_type,
            "device_id": device_id,
            "status": status,
            "result": result,
            "error": None,
            "context": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "started_at": None,
            "heartbeat_at": None,
            "settle_seq": settle_seq,
        }

    def terminal_job(self, device_id: int, *, results=None, status="succeeded", job_type="apply", extra=None) -> dict:
        """Add a terminal job, allocating this device's next settlement sequence.

        *extra* merges into ``result`` — the per-scope ``<scope>_count_by_outcome`` counters
        the coarse settle reads, so one job can carry both channels' evidence.
        """
        seq = self._next_seq.get(device_id, 0) + 1
        self._next_seq[device_id] = seq
        job = self._job(device_id, settle_seq=seq, status=status, results=results, job_type=job_type, extra=extra)
        self.jobs.append(job)
        return job

    def queued_job(self, device_id: int, *, job_type="apply") -> dict:
        """Add a non-terminal job. It holds no sequence, so the feed cannot serve it."""
        job = self._job(device_id, settle_seq=None, status="queued", results=None, job_type=job_type)
        self.jobs.append(job)
        return job

    def unsequenced_job(self, device_id: int, *, results=None, job_type="apply") -> dict:
        """Add a TERMINAL job that the ascending feed serves with no sequence.

        The page's predicate is NULL-false by construction, so this is the adapter breaking
        its own feed contract, a state nothing but the server can produce, and the one the
        consumer must skip rather than stall on. Served at the head, where it would block.
        """
        job = self._job(device_id, settle_seq=None, status="succeeded", results=results, job_type=job_type)
        self.jobs.append(job)
        self.unsequenced_in_feed.add(job["id"])
        return job

    def echo(self, device_id: int, route_id: int, generation: int, fingerprint: str) -> None:
        """Record what the last intent PUT for *route_id* would re-serve."""
        self.routes.setdefault(device_id, []).append(
            {"route_id": route_id, "generation": generation, "fingerprint": fingerprint}
        )

    def put_static_route_intent(self, device_id: int, routes: list[dict]) -> dict:
        """Accept a static-route intent push and echo nothing.

        The settlement suites record their expectations directly, so the default store only
        has to answer 200. A suite that drives the push itself overrides this — it is the
        one seam where the double has to behave like the adapter rather than serve fixtures.
        """
        return {"device_id": device_id, "count": len(routes), "routes": []}

    def recent(self, device_id: int, limit: int) -> list[dict]:
        """The DEFAULT descending page: every job of the device, newest first.

        Unsequenced rows are invisible only to the ascending cursor page — the plain
        collection is what tells a caller an apply is queued or running right now.
        """
        rows = [job for job in self.jobs if job["device_id"] == device_id]
        return list(reversed(rows))[:limit]

    def feed(self, device_id: int, after: int, limit: int) -> list[dict]:
        """The adapter's ascending page: sequenced rows only, in commit order.

        Unless a job was registered through :meth:`unsequenced_job`, which serves it at the
        head with no sequence at all.
        """
        rows = [
            job
            for job in self.jobs
            if job["device_id"] == device_id
            and (job["settle_seq"] > after if job["settle_seq"] is not None else job["id"] in self.unsequenced_in_feed)
        ]
        rows.sort(key=lambda job: (job["settle_seq"] is not None, job["settle_seq"] or 0))
        return rows[:limit]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A003 — silence the stdlib access log
        pass

    @property
    def _store(self) -> SettlementStore:
        return self.server.store

    def _send(self, status: int, payload, headers: dict | None = None) -> None:
        self._store.requests.append((self.command, urlparse(self.path).path))
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _device_id_from_path(self, parsed) -> int:
        return int(parsed.path.split("/")[4])

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's dispatch name
        parsed = urlparse(self.path)
        store = self._store

        if parsed.path == "/api/v1/devices":
            body = self._body()
            row = store.add_device(
                nso_instance=body.get("nso_instance"),
                nso_device_name=body.get("nso_device_name"),
                netbox_device_id=body.get("netbox_device_id"),
            )
            self._send(201, row)
            return

        if parsed.path.endswith("/sync-notify"):
            self._send(200, {"job_id": "notify-1"})
            return

        self._send(404, {"error": {"code": "not_found", "message": f"unexpected request {self.path}"}})

    def do_PUT(self):  # noqa: N802 — BaseHTTPRequestHandler's dispatch name
        parsed = urlparse(self.path)
        store = self._store
        body = self._body()

        if parsed.path.endswith("/scope"):
            device_id = self._device_id_from_path(parsed)
            if device_id not in store.devices:
                self._send(404, {"error": {"code": "not_found", "message": "Device not found"}})
                return
            self._send(200, {"ok": True})
            return

        if parsed.path.endswith("/static-route-intent"):
            device_id = self._device_id_from_path(parsed)
            self._send(200, store.put_static_route_intent(device_id, body.get("routes") or []))
            return

        self._send(404, {"error": {"code": "not_found", "message": f"unexpected request {self.path}"}})

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's dispatch name
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        store = self._store

        if parsed.path == "/api/v1/devices":
            if store.devices_status != 200:
                self._send(store.devices_status, {"error": {"code": "nso_error", "message": "the adapter is hung"}})
                return
            self._send(200, list(store.devices.values()))
            return

        if parsed.path == "/api/v1/jobs":
            device_id = query.get("device_id")
            # Both orders: a missing id raised inside the descending branch and dropped the connection.
            if device_id is None:
                self._send(422, {"error": {"code": "validation_error", "message": "device_id is required"}})
                return
            after = int(query.get("after_settle_seq") or 0)
            limit = int(query.get("limit") or 100)
            if query.get("order") == "asc":
                store.feed_requests.append((int(device_id), after, limit))
                if int(device_id) in store.feed_error_devices:
                    self._send(503, {"error": {"code": "nso_error", "message": "the settlement feed is down"}})
                    return
                rows = store.feed(int(device_id), after, limit)
            else:
                if int(device_id) in store.jobs_error_devices:
                    self._send(503, {"error": {"code": "nso_error", "message": "the jobs list is unavailable"}})
                    return
                rows = store.recent(int(device_id), limit)
            self._send(200, rows, headers={STORE_INCARNATION_HEADER: store.incarnation})
            return

        if parsed.path.startswith("/api/v1/devices/") and parsed.path.endswith("/static-route-intent"):
            device_id = int(parsed.path.split("/")[4])
            store.readback_requests.append(device_id)
            if store.on_readback is not None:
                store.on_readback()
            if store.intent_status != 200:
                self._send(
                    store.intent_status,
                    {"error": {"code": "nso_error", "message": "the read-back is unavailable"}},
                )
                return
            if store.intent_malformed:
                # A 200 whose body is not StaticRouteIntentOut: no ``routes`` at all.
                self._send(200, {"device_id": device_id})
                return
            self._send(200, {"device_id": device_id, "routes": store.routes.get(device_id, [])})
            return

        self._send(404, {"error": {"code": "not_found", "message": f"unexpected request {self.path}"}})


class FakeAdapter:
    """A running adapter lifespan: one store, served over a real loopback socket."""

    def __init__(self, store: SettlementStore | None = None):
        self.store = store or SettlementStore()
        self._serve()

    def _serve(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.store = self.store
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def rebuild(self) -> SettlementStore:
        """End this lifespan and start a new one against a rebuilt store.

        The incarnation changes because the store is new, not because anything overwrote
        it — which is the only way the adapter itself can change it.
        """
        self.stop()
        self.store = SettlementStore()
        self._serve()
        return self.store
