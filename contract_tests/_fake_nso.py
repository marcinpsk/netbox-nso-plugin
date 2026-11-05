# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Answer the device-intent RESTCONF calls of an Apply preflight for the live-adapter contract job."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# The contract job's adapter config names this port as the NSO instance base_url.
FAKE_NSO_PORT = 18009
_DEVICE_INTENT_PATH = "/restconf/data/device-intent:device-intent="


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Send headers and body at once: Nagle plus delayed ACK adds about 40 ms to each keep-alive response.
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/yang-data+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _device(self) -> str | None:
        path = urlparse(self.path).path
        name = path.removeprefix(_DEVICE_INTENT_PATH)
        return name if path.startswith(_DEVICE_INTENT_PATH) and name and "/" not in name else None

    def do_GET(self):  # noqa: N802
        self._send(404, {"errors": "unknown service instance" if self._device() else "unsupported test surface"})

    def do_PUT(self):  # noqa: N802
        self._drain()
        if self._device() is None:
            self._send(404, {"errors": "unsupported test surface"})
            return
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if "dry-run" not in query:
            self._send(204)
        elif query["dry-run"] == ["cli"]:
            self._send(200, {"dry-run-result": {"cli": {"local-node": {"data": ""}}}})
        else:
            self._send(200, {"dry-run-result": {"native": {"device": [{"name": self._device(), "data": ""}]}}})

    def do_POST(self):  # noqa: N802
        self._drain()
        self._send(404, {"errors": "unsupported test surface"})

    do_PATCH = do_POST
    do_DELETE = do_GET


@contextmanager
def fake_nso():
    """Serve the fake on FAKE_NSO_PORT for the block."""
    server = ThreadingHTTPServer(("127.0.0.1", FAKE_NSO_PORT), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
