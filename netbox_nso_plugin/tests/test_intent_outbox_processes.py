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

from django.db import connection
from django.test import TransactionTestCase

from ._outbox_case import make_managed, own_vlan
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


class _Recorder(BaseHTTPRequestHandler):
    """A real HTTP far side: it records the arrival order and answers every push."""

    def do_PUT(self):  # noqa: N802 (BaseHTTPRequestHandler's own naming)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.received.append(
            {"path": self.path, "push_seq": self.headers.get("X-Push-Seq"), "at": time.monotonic()}
        )
        self.server.hold.wait(timeout=30)  # the send barrier: the first sends overlap
        body = json.dumps({"count": 1}).encode()
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

    def _spawn(self, role, work, device_id, seconds=8):
        # The child names the test database directly, under the standard settings: the isolated
        # harness refuses to build a settings module whose live NAME is already the test name.
        env = dict(os.environ)
        env.pop("TEST_DB_NAME", None)
        env["DB_NAME"] = connection.settings_dict["NAME"]
        env["DJANGO_SETTINGS_MODULE"] = "netbox.settings"
        env.update({"O1_ROLE": role, "O1_DIR": str(work), "O1_DEVICE": str(device_id), "O1_SECONDS": str(seconds)})
        return subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
            [sys.executable, "manage.py", "shell", "-c", _SCRIPT],
            cwd=_manage_py_dir(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

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
            racer = self._spawn("racer", work, device.pk)
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

    def _finish(self, process):
        out, err = process.communicate(timeout=180)
        assert process.returncode == 0, f"{out}\n{err}"
        return out
