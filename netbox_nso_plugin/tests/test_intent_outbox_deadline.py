# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1.21(c)): the total wall-clock deadline, against a real dripping far side.

The client's ``(connect, read)`` tuple measures the gap between bytes, so a response arriving
one byte at a time resets it forever (O-P16). The send therefore carries its own wall clock,
and an expired one has to END the request rather than walk away from it.

That last part is only provable against a real socket. ``requests.Session.close()`` empties
the connection pool; it does not touch a connection a worker already borrowed, so a double
whose ``close()`` raised under the request would prove a capability the real transport does
not have. Everything here drives a real ``ThreadingHTTPServer`` that answers forever, one
byte at a time, and asserts what only a genuine abort can produce: the sender thread ends,
and the far side loses the socket mid-response.
"""

from __future__ import annotations

import contextlib
import datetime
import gc
import threading
import time
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.test import TransactionTestCase

from ._outbox_case import make_managed, own_vlan, state_of
from ._settlement_adapter import LoopbackOnlySession
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def _senders() -> set:
    """The send workers alive right now, so a pin can speak about the ones IT started."""
    return {worker for worker in threading.enumerate() if worker.name == "nso-intent-push"}


def _senders_ended(before: set, timeout: float) -> bool:
    """Whether every sender started since *before* has ended, within *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while _senders() - before and time.monotonic() < deadline:
        time.sleep(0.05)
    return not (_senders() - before)


class _Drip(BaseHTTPRequestHandler):
    """A far side that answers forever, one byte at a time: O-P16's dripping response."""

    protocol_version = "HTTP/1.1"

    def do_PUT(self):  # noqa: N802 (BaseHTTPRequestHandler's own naming)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "1048576")
        self.end_headers()
        self.server.streaming.set()
        try:
            while not self.server.stop.is_set():
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError as exc:
            self.server.broken.append(type(exc).__name__)

    do_POST = do_PUT

    def log_message(self, *args):
        """Silence the default stderr access log."""


class _DripCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """A managed device and a real far side that never finishes answering."""

    tag = "drip"
    adapter_device_id = 7810

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin import adapter_client

        # The suite-wide hermetic guard fails EVERY request; these pins need a real socket
        # to the in-test far side, so narrow the guard to loopback instead of lifting it.
        unblocked = patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession)
        unblocked.start()
        self.addCleanup(unblocked.stop)
        adapter_client.reset_session()
        self.addCleanup(adapter_client.reset_session)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
        self.server.daemon_threads = True
        self.server.streaming = threading.Event()
        self.server.stop = threading.Event()
        self.server.broken: list[str] = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.stop.set)
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def url(self, path=""):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}{path}"

    @contextlib.contextmanager
    def pointed_at_the_drip(self):
        """Resolve the adapter to the dripping server. Config only: the transport is real."""
        config = {"url": self.url(), "token": "tok", "verify_tls": True, "ca_cert_path": None, "timeout": 30}
        with patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=config):
            yield

    def far_side_lost_the_socket(self, timeout: float) -> bool:
        """Whether the server's own write failed, which only a shut-down socket produces."""
        deadline = time.monotonic() + timeout
        while not self.server.broken and time.monotonic() < deadline:
            time.sleep(0.05)
        return bool(self.server.broken)


class TestTheSendCarriesItsOwnDeadline(_DripCase):
    """O1.21(c): a dripping response never trips a read window, so the sender needs a clock."""

    tag = "clock"
    adapter_device_id = 7811

    def test_the_total_deadline_is_shorter_than_the_lease(self):
        from netbox_nso_plugin import drain

        assert drain.SEND_DEADLINE < drain.LEASE

    def test_a_response_dripping_past_the_read_window_is_recorded_as_a_deadline_failure(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement

        own_vlan(self.mgmt, 880, self.tag)
        started = time.monotonic()
        with patch.object(drain, "SEND_DEADLINE", datetime.timedelta(seconds=1)), self.pointed_at_the_drip():
            outcome = drain.drain_key(self.device.pk, "vlan")
        elapsed = time.monotonic() - started

        assert outcome == drain.FAILED
        assert elapsed < 20, f"the send ran for {elapsed}s, outliving its deadline"
        assert self.server.streaming.is_set(), "the far side never began the response this pin needs"
        row = state_of(self.device, "vlan")
        assert (row.attempts, row.last_error_code) == (1, "nso_send_deadline")
        assert row.claimed_at is None and row.push_seq is not None, "the operation is replayed"
        errors = NSODeviceManagement.objects.get(pk=self.mgmt.pk).intent_push_errors or {}
        assert "vlan" in errors, errors


class TestTheTransportEndsItsOwnSocket(_DripCase):
    """The mechanism itself, at the transport: what close() cannot do and abort() does."""

    tag = "sock"
    adapter_device_id = 7813

    def test_discarding_a_broken_connection_removes_its_checkout(self):
        from urllib3.connectionpool import HTTPConnectionPool

        from netbox_nso_plugin import adapter_client

        transport = adapter_client.AbortableTransport()
        pool = HTTPConnectionPool("127.0.0.1", self.server.server_port)
        self.addCleanup(pool.close)
        transport._instrument(pool)

        connection = pool._get_conn()
        assert connection in transport._live

        # urllib3 closes a broken connection and calls _put_conn(None). The transport must
        # remove the connection it checked out, not the None sentinel.
        pool._put_conn(None)

        assert connection not in transport._live
        connection_ref = weakref.ref(connection)
        del connection
        gc.collect()
        assert connection_ref() is None, "the transport retained a discarded connection"

    def test_abort_continues_when_one_socket_close_fails(self):
        from netbox_nso_plugin import adapter_client

        class _Socket:
            """A socket whose shutdown may fail, and which records any close as a fault.

            ``abort`` must never close: the connection belongs to urllib3's pool and the
            file descriptor may be reissued to another thread between the close and the
            owner's next read, which is a cross-connection misdirect.
            """

            def __init__(self, *, shutdown_error=False):
                self.shutdown_error = shutdown_error
                self.shutdown_called = False
                self.close_called = False

            def shutdown(self, how):
                self.shutdown_called = True
                if self.shutdown_error:
                    raise OSError("already closed")

            def close(self):
                self.close_called = True

        class _Connection:
            def __init__(self, sock):
                self.sock = sock

        broken = _Socket(shutdown_error=True)
        healthy = _Socket()
        transport = adapter_client.AbortableTransport()
        transport._live = {_Connection(broken), _Connection(healthy)}

        transport.abort()

        assert broken.shutdown_called, "the failing socket was never shut down"
        assert healthy.shutdown_called, "one socket's failure skipped the other"
        assert not (broken.close_called or healthy.close_called), "abort closed a pooled socket"

    def test_closing_the_session_leaves_the_read_running_and_abort_ends_it(self):
        from netbox_nso_plugin import adapter_client

        transport = adapter_client.AbortableTransport()
        session = adapter_client.new_session(transport=transport)
        ended = threading.Event()
        errors: list[BaseException] = []

        def send():
            try:
                session.put(self.url("/api/v1/devices/1/vlan-intent"), json={"vlans": []}, timeout=(5, 30))
            except BaseException as exc:  # noqa: BLE001 (reported on the caller's thread)
                errors.append(exc)
            finally:
                ended.set()

        threading.Thread(target=send, daemon=True).start()
        assert self.server.streaming.wait(15), "the far side never began the response this pin needs"
        sockets = [conn.sock for conn in list(transport._live) if conn.sock is not None]
        assert sockets, "the transport tracked no connection to abort"

        session.close()
        assert not ended.wait(2), "closing the session ended a borrowed read, which it cannot do"

        transport.abort()
        assert ended.wait(15), "the worker never came back, so the abort did nothing"
        assert errors, "the aborted request answered instead of failing"
        assert [sock.fileno() for sock in sockets] == [-1] * len(sockets), "the socket outlived the abort"


class TestTheExpiredDeadlineAbortsTheRequest(_DripCase):
    """codex O1 r3 F3: close() empties the pool; only the socket ends a borrowed connection."""

    tag = "abort"
    adapter_device_id = 7812

    def test_a_deadline_during_connect_ends_the_sender_after_one_abort(self):
        from urllib3.connection import HTTPConnection

        from netbox_nso_plugin import adapter_client, drain

        own_vlan(self.mgmt, 882, self.tag)
        original_new_conn = HTTPConnection._new_conn
        original_abort = adapter_client.AbortableTransport.abort
        socket_obtained = threading.Event()
        release_connect = threading.Event()
        aborts = []

        def paused_new_conn(connection):
            sock = original_new_conn(connection)
            socket_obtained.set()
            assert release_connect.wait(15), "the deadline never released the paused connect"
            return sock

        def counted_abort(transport):
            # Always release the paused connect, or a failure here strands the sender.
            try:
                assert socket_obtained.wait(15), "the sender never reached its connect"
                assert not release_connect.is_set(), "connect returned before the deadline abort"
                aborts.append(transport)
                original_abort(transport)
            finally:
                release_connect.set()

        running = _senders()
        with (
            patch.object(HTTPConnection, "_new_conn", paused_new_conn),
            patch.object(adapter_client.AbortableTransport, "abort", counted_abort),
            patch.object(drain, "SEND_DEADLINE", datetime.timedelta(seconds=1)),
            self.pointed_at_the_drip(),
        ):
            assert drain.drain_key(self.device.pk, "vlan") == drain.FAILED
            assert _senders_ended(running, 15), "the sender survived the only deadline abort"

        assert len(aborts) == 1, "the sender needed a second abort to observe the late socket"

    def test_the_sender_ends_and_the_far_side_loses_the_socket(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 881, self.tag)
        running = _senders()

        with patch.object(drain, "SEND_DEADLINE", datetime.timedelta(seconds=1)), self.pointed_at_the_drip():
            assert drain.drain_key(self.device.pk, "vlan") == drain.FAILED

        assert self.server.streaming.is_set(), "the far side never began the response this pin needs"
        assert _senders_ended(running, 15), "the sender outlived the deadline that abandoned it"
        assert self.far_side_lost_the_socket(15), "the far side kept writing, so nothing was aborted"

    def test_a_teardown_failure_does_not_lose_an_answer_the_worker_already_had(self):
        """Teardown runs after the answer is in hand, so it must not decide the outcome.

        connections.close_all() raising left done unset, so the waiter sat out the whole
        budget and raised SendDeadlineExceeded for a push the adapter had already accepted:
        a delivered intent recorded as a timeout.
        """
        from netbox_nso_plugin import delivery

        with patch("django.db.connections.close_all", side_effect=RuntimeError("teardown boom")):
            answer = delivery._under_deadline(lambda body: {"ok": 1}, 0.5)({})

        assert answer == {"ok": 1}

    def test_a_completion_arriving_after_the_deadline_is_discarded(self):
        """The waiter's verdict is final: the attempt is already recorded failed."""
        from netbox_nso_plugin import delivery

        answered = threading.Event()

        def slow(body):
            time.sleep(1.5)
            answered.set()
            return {"count": 1}

        call = delivery._under_deadline(slow, 0.5)
        try:
            call({})
        except delivery.SendDeadlineExceeded:
            pass
        else:
            raise AssertionError("the deadline let a late answer through")

        assert answered.wait(10), "the late worker ran to completion, as this pin needs"
