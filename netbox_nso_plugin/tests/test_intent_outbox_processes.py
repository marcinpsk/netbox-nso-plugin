# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1.12): the claim across a real process boundary.

Threads share a Python process, so a thread pin can pass on an in-memory guard that does not
exist in production, where the drain runs in whatever worker the tick landed in. This drives
two real operating-system processes against the real PostgreSQL test database and a real
HTTP server: only one of them may hold the key, and the sequences the far side receives must
be increasing, because a body sent after a newer one would overwrite it.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.db import connection, transaction
from django.test import TransactionTestCase

from ._outbox_case import make_managed, own_route, own_vlan, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

#: Run by both processes. The role decides whether it holds the key or races for it.
_SCRIPT = """
import os, pathlib, time
from netbox_nso_plugin import drain

device_id = int(os.environ["O1_DEVICE"])
work = pathlib.Path(os.environ["O1_DIR"])

if os.environ["O1_ROLE"] == "holder":
    claimed = drain.claim(device_id, "vlan")
    print("CLAIMED", None if claimed is None else claimed.push_seq, flush=True)
    (work / "claimed").write_text("1")
    while not (work / "go").exists():
        time.sleep(0.05)
    print("OUTCOME", drain.settle(claimed, drain.send_claim(claimed)), flush=True)
else:
    while not (work / "claimed").exists():
        time.sleep(0.05)
    seen = []
    deadline = time.monotonic() + float(os.environ["O1_SECONDS"])
    while time.monotonic() < deadline:
        taken = drain.claim(device_id, "vlan")
        seen.append(None if taken is None else taken.push_seq)
        if taken is not None:
            print("OUTCOME", drain.settle(taken, drain.send_claim(taken)), flush=True)
            break
        time.sleep(0.2)
    print("ATTEMPTS", seen, flush=True)
"""

_STATIC_SCRIPT = """
import os, pathlib, time
from netbox_nso_plugin import drain

device_id = int(os.environ["O1_DEVICE"])
work = pathlib.Path(os.environ["O1_DIR"])
role = os.environ["O1_ROLE"]
(work / ("ready-" + role)).write_text("1")
while not (work / "go").exists():
    time.sleep(0.05)
print("OUTCOME", drain.drain_key(device_id, "static_route", chain=0), flush=True)
"""


class _Recorder(BaseHTTPRequestHandler):
    """A real HTTP far side: it records the arrival order and answers every push."""

    def do_PUT(self):  # noqa: N802 (BaseHTTPRequestHandler's own naming)
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.received.append(
            {
                "path": self.path,
                "push_seq": self.headers.get("X-Push-Seq"),
                "body": payload,
                "at": time.monotonic(),
            }
        )
        self.server.hold.wait(timeout=30)  # the send barrier: the first sends overlap
        deleted = [record["route_id"] for record in payload.get("deleted_routes") or []]
        response = {"count": 1}
        if "deleted_routes" in payload:
            response.update(
                {
                    "count": len(payload.get("routes") or []),
                    "deleted_executed_ids": deleted,
                    "deleted_degraded_ids": [],
                    "deleted_moot_ids": [],
                    "removed_uncorrelated": [],
                    "routes": [],
                }
            )
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence the default stderr access log."""


def _manage_py_dir():
    """The directory the NetBox ``manage.py`` lives in, taken from the installed package."""
    import netbox

    root = pathlib.Path(netbox.__file__).resolve().parents[1]
    # No skip on absence: this pin is the only cross-process evidence, so it must fail loud.
    assert (root / "manage.py").is_file(), f"no manage.py under {root} — the claim pin cannot run"
    return root


class TestOneClaimerAcrossTwoProcesses(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O1.12: two operating-system processes, one PostgreSQL, one claimer."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.server.received = []
        self.server.hold = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _adapter_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _spawn(self, role, work, device_id, seconds=8, script=_SCRIPT):
        # The child names the test database directly, under the standard settings: the isolated
        # harness refuses to build a settings module whose live NAME is already the test name.
        env = dict(os.environ)
        env.pop("TEST_DB_NAME", None)
        env["DB_NAME"] = connection.settings_dict["NAME"]
        env["DJANGO_SETTINGS_MODULE"] = "netbox.settings"
        env.update({"O1_ROLE": role, "O1_DIR": str(work), "O1_DEVICE": str(device_id), "O1_SECONDS": str(seconds)})
        process = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
            [sys.executable, "manage.py", "shell", "-c", script],
            cwd=_manage_py_dir(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Cleanups run LIFO, so the kill goes on last and runs first. A child left alive
        # holds a connection to the test database and the flush blocks on it.
        self.addCleanup(process.wait, 30)
        self.addCleanup(process.kill)
        return process

    def test_two_processes_yield_one_claimer_and_increasing_sequences(self):
        from netbox_nso_plugin.models import AdapterConnection

        device, mgmt = make_managed("proc", 7950)
        state = own_vlan(mgmt, 895, "proc")
        # Pointed at the recorder only now: the fixture's own onboarding calls are not the
        # traffic under test, and a 501 for them would say nothing about the claim.
        AdapterConnection.objects.create(url=self._adapter_url(), enabled=True, verify_tls=False, timeout_seconds=30)

        with tempfile.TemporaryDirectory() as raw:
            work = pathlib.Path(raw)
            holder = self._spawn("holder", work, device.pk)
            # The racer's clock starts at the ``claimed`` marker but it cannot claim until the
            # holder settles, which the parent gates behind a 1s send barrier: 8s is too tight
            # on a loaded worker.
            racer = self._spawn("racer", work, device.pk, seconds=30)
            self._await(work / "claimed", holder, racer)

            # A second edit that really changes the body, so the racer has an operation of
            # its own rather than a digest-equal claim the drop path retires.
            self._rename(state)
            (work / "go").write_text("1")
            # The send barrier: the holder's request sits inside the far side while the
            # racer keeps trying, so the two overlap on the wire and not only in the claim.
            time.sleep(1.0)
            self.server.hold.set()

            holder_out = self._finish(holder)
            racer_out = self._finish(racer)

        claimed = [line for line in holder_out.splitlines() if line.startswith("CLAIMED")]
        assert claimed and claimed[0] != "CLAIMED None", holder_out
        attempts = [line for line in racer_out.splitlines() if line.startswith("ATTEMPTS")]
        assert attempts, racer_out
        refused = attempts[0].removeprefix("ATTEMPTS ")
        assert refused.count("None") >= 1, f"the racer never met the live claim: {refused}"
        assert "OUTCOME succeeded" in holder_out, holder_out
        assert "OUTCOME succeeded" in racer_out, racer_out

        pushes = [request for request in self.server.received if request["path"].endswith("/vlan-intent")]
        assert all(request["push_seq"] for request in pushes), pushes
        sequences = [int(request["push_seq"]) for request in pushes]
        assert len(sequences) == 2, self.server.received
        assert sequences == sorted(sequences), f"an older body was sent after a newer one: {sequences}"
        assert len(set(sequences)) == 2, "both processes sent the same logical operation"

    def test_static_route_marking_is_decided_from_the_locked_rows_across_processes(self):
        """O3.8: two workers race the mixed shrink, but only the locked claim decides it."""
        from netbox_nso_plugin.models import AdapterConnection, NSOIntentOutboxEntry, NSOStaticRouteState

        device, mgmt = make_managed("proc-static", 7951)
        retract = own_route(mgmt, "198.18.2.0/28", "198.18.2.1")
        detach = own_route(mgmt, "198.18.2.16/28", "198.18.2.17")
        NSOIntentOutboxEntry.objects.all().delete()
        with without_commit_drain(), transaction.atomic():
            NSOStaticRouteState.objects.filter(management=mgmt, static_route=detach).update(status="imported")
            retract.devices.remove(device)
        AdapterConnection.objects.create(url=self._adapter_url(), enabled=True, verify_tls=False, timeout_seconds=30)

        with tempfile.TemporaryDirectory() as raw:
            work = pathlib.Path(raw)
            first = self._spawn("first", work, device.pk, script=_STATIC_SCRIPT)
            second = self._spawn("second", work, device.pk, script=_STATIC_SCRIPT)
            self._await(work / "ready-first", first, second)
            self._await(work / "ready-second", first, second)
            (work / "go").write_text("1")
            self._await_request("/static-route-intent", first, second)
            time.sleep(1.0)
            self.server.hold.set()
            outcomes = self._finish(first) + self._finish(second)

        assert outcomes.count("OUTCOME succeeded") == 1, outcomes
        assert outcomes.count("OUTCOME nothing") == 1, outcomes
        [request] = [r for r in self.server.received if r["path"].split("?", 1)[0].endswith("/static-route-intent")]
        assert request["body"]["routes"] == []
        assert [record["route_id"] for record in request["body"]["deleted_routes"]] == [retract.pk]

    def _rename(self, state):
        """One operator edit, recorded as an entry and sent by nobody but the drain."""
        from django.db import transaction

        from ._outbox_case import without_commit_drain

        with without_commit_drain(), transaction.atomic():
            state.vlan.name = "proc-renamed"
            state.vlan.save()
            state.save()

    def _await(self, marker, *processes):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if marker.exists():
                return
            for process in processes:
                if process.poll() is not None:
                    out, err = process.communicate()
                    raise AssertionError(f"a worker exited before the claim landed:\n{out}\n{err}")
            time.sleep(0.1)
        raise AssertionError("no worker claimed the key")

    def _await_request(self, suffix, *processes):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any(request["path"].split("?", 1)[0].endswith(suffix) for request in self.server.received):
                return
            for process in processes:
                returncode = process.poll()
                if returncode is not None and returncode != 0:
                    out, err = process.communicate()
                    raise AssertionError(f"a worker exited before the request landed:\n{out}\n{err}")
            if all(process.poll() is not None for process in processes):
                raise AssertionError(f"all workers exited before the request ending in {suffix} landed")
            time.sleep(0.1)
        raise AssertionError(f"no request ending in {suffix} reached the adapter")

    def test_await_request_ignores_a_successful_loser_until_the_winner_reaches_the_wire(self):
        loser = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
            [sys.executable, "-c", "pass"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        loser.wait(timeout=10)
        self.assertEqual(loser.returncode, 0)
        host, port = self.server.server_address[:2]
        winner_script = (
            "import time, urllib.request; "
            "time.sleep(0.3); "
            f"request=urllib.request.Request({('http://' + host + ':' + str(port) + '/winner')!r}, "
            "data=b'{}', method='PUT'); "
            "urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request).read()"
        )
        winner = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
            [sys.executable, "-c", winner_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self._await_request("/winner", loser, winner)
        finally:
            self.server.hold.set()
            loser_out, loser_err = loser.communicate(timeout=10)
            winner_out, winner_err = winner.communicate(timeout=10)

        self.assertEqual(loser.returncode, 0, f"{loser_out}\n{loser_err}")
        self.assertEqual(winner.returncode, 0, f"{winner_out}\n{winner_err}")

    def test_await_request_rejects_a_nonzero_worker_exit(self):
        loser = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
            [sys.executable, "-c", "raise SystemExit(7)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        winner = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with self.assertRaisesRegex(AssertionError, "a worker exited before the request landed"):
                self._await_request("/never", loser, winner)
        finally:
            winner.terminate()
            loser.communicate(timeout=10)
            winner.communicate(timeout=10)

    def test_await_request_rejects_when_all_workers_exit_without_a_request(self):
        workers = [
            subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
                [sys.executable, "-c", "pass"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        try:
            with self.assertRaisesRegex(
                AssertionError, "all workers exited before the request ending in /never landed"
            ):
                self._await_request("/never", *workers)
        finally:
            for worker in workers:
                worker.communicate(timeout=10)

    def _finish(self, process):
        out, err = process.communicate(timeout=180)
        assert process.returncode == 0, f"{out}\n{err}"
        return out
