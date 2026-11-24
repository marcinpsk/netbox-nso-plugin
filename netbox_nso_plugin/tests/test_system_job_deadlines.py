# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every plugin system job finishes inside its own RQ timeout (#1916).

RQ kills a run that passes its timeout, and NetBox then leaves the Job row ``running`` with
no successor, so one long run stops the job for good. Two things made runs long. The push
bookkeeping wrote the management row through the device's whole renderer footprint, which
locks and re-reads every interface of the device. And the passes had no shared deadline.
"""

from __future__ import annotations

import contextlib
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from dcim.models import Interface
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from requests.exceptions import ReadTimeout
from rq.timeouts import JobTimeoutException

from ._adapter_http import make_response, patch_matching_control_state
from ._outbox_case import (
    CFG,
    HeldRowLock,
    ReceiptAdapter,
    enqueue,
    make_managed,
    open_provision_attempt,
    reset_renderer_audit_rotation,
    short_lock_timeout,
    system_job,
    wait_until_postgres_blocks,
    without_commit_drain,
)
from ._settlement_adapter import LoopbackOnlySession
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def _add_interfaces(device, count, start=0):
    with without_commit_drain():
        Interface.objects.bulk_create(
            [Interface(device=device, name=f"et-0/0/{index}", type="1000base-t") for index in range(start, count)]
        )


def _rq_test_queue(test_case):
    """Return a private RQ queue, and delete every job it scheduled when the test ends."""
    import django_rq
    from rq import Queue

    queue = Queue(f"nso-test-job-timeout-{uuid4().hex}", connection=django_rq.get_queue("default").connection)

    def delete():
        # Queue.delete empties only the ready list, and NetBox schedules each run and its successor.
        for job_id in queue.scheduled_job_registry.get_job_ids():
            job = queue.fetch_job(job_id)
            if job is not None:
                job.delete()
        queue.delete(delete_jobs=True)

    test_case.addCleanup(delete)
    return queue


class TestPushBookkeepingLocksOnlyTheManagementRow(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The attempt mark and the outcome record are counters on one row, not renderer input."""

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("job-deadline-mark", 19160)

    def _queries(self, work):
        with CaptureQueriesContext(connection) as queries:
            work()
        return len(queries)

    def test_the_attempt_mark_costs_the_same_on_a_device_with_many_interfaces(self):
        from netbox_nso_plugin import signals

        _add_interfaces(self.device, 5)
        few = self._queries(lambda: signals._allocate_push_attempt(self.device.pk, "vlan"))
        _add_interfaces(self.device, 200, start=5)
        many = self._queries(lambda: signals._allocate_push_attempt(self.device.pk, "vlan"))

        self.assertEqual(many, few)
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_attempts, {"vlan": 2})

    def test_an_operator_lock_on_one_interface_does_not_block_the_attempt_mark(self):
        from netbox_nso_plugin import signals

        _add_interfaces(self.device, 3)
        short_lock_timeout(self)
        with HeldRowLock(Interface.objects.filter(device=self.device).first()):
            attempt = signals._allocate_push_attempt(self.device.pk, "vlan")

        self.assertEqual(attempt, 1)
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_attempts, {"vlan": 1})

    def test_an_operator_lock_on_one_interface_does_not_block_the_outcome_record(self):
        from netbox_nso_plugin import signals
        from netbox_nso_plugin.adapter_client import AdapterError

        _add_interfaces(self.device, 3)
        attempt = signals._allocate_push_attempt(self.device.pk, "vlan")
        short_lock_timeout(self)
        with HeldRowLock(Interface.objects.filter(device=self.device).first()):
            signals._record_push_outcome(self.device.pk, "vlan", attempt, AdapterError("refused", code="refused"))

        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_errors["vlan"]["code"], "refused")


class _SlowAdapter(ReceiptAdapter):
    """The far side, answering each intent request after a fixed delay."""

    DELAY = 0.5

    def _handle(self, method, url, **kwargs):
        if method == "PUT":
            time.sleep(self.DELAY)
        return super()._handle(method, url, **kwargs)


class _DeadlineCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    BUDGET = 3.0

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin import jobs

        self.adapter = _SlowAdapter()
        for patcher in (*self.adapter.patches(), patch.object(jobs, "SYSTEM_JOB_BUDGET_SECONDS", self.BUDGET)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _unconsumed(self, device):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        return NSOIntentOutboxEntry.objects.filter(device=device, consumed_by_push_seq__isnull=True).count()

    def _alarm_during_stamp(self, row, matches):
        def raise_when_blocked(waiting_pid, holder_pid):
            try:
                wait_until_postgres_blocks(waiting_pid, "progress stamp")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [holder_pid, waiting_pid])
                    self.assertTrue(cursor.fetchone()[0])
                    signal.raise_signal(signal.SIGALRM)
                    # Cancel the SQL before the test releases the row lock.
                    cursor.execute("SELECT pg_cancel_backend(%s)", [waiting_pid])
            finally:
                connection.close()

        def interrupt(execute, sql, params, many, context):
            if not matches(sql):
                return execute(sql, params, many, context)
            waiting_pid = connection.connection.info.backend_pid
            with HeldRowLock(row) as holder, ThreadPoolExecutor(max_workers=1) as observer:
                alarm = observer.submit(raise_when_blocked, waiting_pid, holder.pid)
                try:
                    return execute(sql, params, many, context)
                finally:
                    alarm.result(timeout=6)

        return connection.execute_wrapper(interrupt)


class TestTheRendererAuditJobStopsAtItsDeadline(_DeadlineCase):
    def setUp(self):
        super().setUp()
        reset_renderer_audit_rotation(self)
        patch_matching_control_state(self)

    def test_a_slow_adapter_leaves_the_repair_sends_to_the_outbox_tick(self):
        from netbox_nso_plugin import delivery, jobs
        from netbox_nso_plugin.models import NSOIntentRevision

        device, _management = make_managed("job-deadline-audit", 19161)
        NSOIntentRevision.objects.filter(device=device).update(verified_fingerprint=None)
        scopes = len(delivery.delivery_keys())
        # Without the deadline, every repaired key sends inline: far longer than the budget.
        self.assertGreater(scopes * _SlowAdapter.DELAY, 2 * self.BUDGET)

        started = time.monotonic()
        with self.assertLogs("netbox_nso_plugin.renderer_audit", level="INFO") as logs:
            result = system_job(jobs.AuditRendererScopesJob).run()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, self.BUDGET + 2 * _SlowAdapter.DELAY)
        self.assertEqual(result.repaired, scopes)
        self.assertTrue(any("left repair sends to the outbox tick" in line for line in logs.output), logs.output)
        self.assertGreater(self._unconsumed(device), 0)
        self.assertGreater(len(self.adapter.requests), 0)

    def test_a_long_audit_budget_still_gives_each_repair_send_the_send_deadline_at_most(self):
        from netbox_nso_plugin import delivery, drain, renderer_audit

        device, _management = make_managed("job-deadline-audit-cap", 19176)
        with without_commit_drain():
            with transaction.atomic():
                enqueue(device, "vlan")

        with patch.object(_SlowAdapter, "DELAY", 0), patch.object(delivery, "send", wraps=delivery.send) as send:
            renderer_audit._drain_repaired(device.pk, ("vlan",), renderer_audit._monotonic() + 230)

        self.assertEqual(self._unconsumed(device), 0)
        self.assertLessEqual(send.call_args.kwargs["deadline"], drain.SEND_DEADLINE.total_seconds())

    def test_lock_context_entry_can_use_up_the_repair_send_budget(self):
        from netbox_nso_plugin import drain, jobs, renderer_audit

        device, _management = make_managed("job-deadline-audit-lock-entry", 19198)
        with without_commit_drain(), transaction.atomic():
            enqueue(device, "vlan")
        now = time.monotonic()
        deadline = now + 1
        lock_waits_until = jobs.lock_waits_until

        @contextlib.contextmanager
        def slow_entry(deadline):
            nonlocal now
            with lock_waits_until(deadline):
                now = deadline + 1
                yield

        with (
            patch.object(jobs, "lock_waits_until", slow_entry),
            patch.object(renderer_audit, "_monotonic", side_effect=lambda: now),
            patch.object(_SlowAdapter, "DELAY", 0),
            patch.object(drain, "drain_key", wraps=drain.drain_key) as drain_key,
        ):
            renderer_audit._drain_repaired(device.pk, ("vlan",), deadline)

        drain_key.assert_not_called()
        self.assertEqual(self._unconsumed(device), 1)

    def test_an_expired_repair_send_budget_skips_lock_setup(self):
        from netbox_nso_plugin import drain, jobs, renderer_audit

        device, _management = make_managed("job-deadline-audit-expired", 19213)
        lock_entries = 0

        @contextlib.contextmanager
        def fail_on_entry(_deadline):
            nonlocal lock_entries
            lock_entries += 1
            raise DatabaseError("Lock setup failed.")
            yield

        with (
            patch.object(jobs, "lock_waits_until", fail_on_entry),
            patch.object(drain, "drain_key", wraps=drain.drain_key) as drain_key,
        ):
            renderer_audit._drain_repaired(device.pk, ("vlan", "bgp"), renderer_audit._monotonic() - 1)

        drain_key.assert_not_called()
        self.assertEqual(lock_entries, 0)

    def test_rq_timeout_during_a_provision_poll_skips_the_tombstone_stamp(self):
        from netbox_nso_plugin import adapter_client, jobs

        _device, management = make_managed("job-deadline-tombstone-alarm", 19199)
        tombstone = open_provision_attempt(management)
        _later_device, later_management = make_managed("job-deadline-tombstone-later", 19208, index=2)
        later = open_provision_attempt(later_management)
        expected_stamp = tombstone.updated_at
        alarm_times = []
        polls = []

        def interrupt_poll(method, url, **kwargs):
            self.assertIn("/provision-attempts/", url)
            polls.append(url.rsplit("/", 1)[1])
            alarm_times.append(time.monotonic())
            signal.raise_signal(signal.SIGALRM)

        adapter_client.reset_session()
        job = system_job(jobs.AuditRendererScopesJob)
        with (
            patch.object(jobs, "SYSTEM_JOB_LOCK_TIMEOUT", "1s"),
            patch.object(self.adapter, "_handle", interrupt_poll),
            HeldRowLock(tombstone),
            CaptureQueriesContext(connection) as queries,
            self.assertRaises(JobTimeoutException),
        ):
            job.run()

        self.assertEqual(len(alarm_times), 1)
        self.assertLess(time.monotonic() - alarm_times[0], 0.8)
        self.assertFalse(any('UPDATE "netbox_nso_plugin_nsoprovisiontombstone"' in query["sql"] for query in queries))
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.updated_at, expected_stamp)
        job.job.refresh_from_db()
        self.assertIn(str(tombstone.pk), job.job.data["fleet_rotation"]["tombstone_rotation"]["lock_skipped"])
        with patch.object(self.adapter, "_handle", interrupt_poll), self.assertRaises(JobTimeoutException):
            system_job(jobs.AuditRendererScopesJob).run()
        self.assertEqual(polls, [str(tombstone.pk), str(later.pk)])

    def test_an_alarm_during_the_tombstone_stamp_carries_the_rotation(self):
        from netbox_nso_plugin import adapter_client, jobs

        _device, management = make_managed("job-deadline-tombstone-stamp", 19211)
        tombstone = open_provision_attempt(management)
        expected_stamp = tombstone.updated_at
        evidence = {"status": "running", "provision_attempt_id": str(tombstone.pk)}
        job = system_job(jobs.AdvanceStaleOnboardingJob)

        adapter_client.reset_session()
        with (
            patch.object(self.adapter, "_handle", return_value=make_response(200, evidence)) as poll,
            self._alarm_during_stamp(tombstone, lambda sql: sql.startswith(f'UPDATE "{tombstone._meta.db_table}"')),
            self.assertRaises(JobTimeoutException),
        ):
            job.run()

        poll.assert_called_once()
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.updated_at, expected_stamp)
        job.job.refresh_from_db()
        self.assertIn(str(tombstone.pk), job.job.data["tombstone_rotation"].get("lock_skipped", {}))

    def test_a_held_outbox_row_does_not_hold_a_repair_send_past_the_audit_deadline(self):
        from netbox_nso_plugin import renderer_audit
        from netbox_nso_plugin.models import NSOIntentOutboxState

        device, _management = make_managed("job-deadline-audit-lock", 19177)
        with without_commit_drain():
            with transaction.atomic():
                enqueue(device, "vlan")
        # The job's own session cap, which is far longer than what is left of the audit.
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
        self.addCleanup(lambda: connection.cursor().execute("SET lock_timeout = 0"))

        started = time.monotonic()
        with (
            patch.object(_SlowAdapter, "DELAY", 0),
            HeldRowLock(NSOIntentOutboxState.objects.get_or_create(device=device, scope="vlan")[0]),
            self.assertLogs("netbox_nso_plugin.renderer_audit", level="WARNING") as logs,
        ):
            renderer_audit._drain_repaired(device.pk, ("vlan",), renderer_audit._monotonic() + 1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5)
        self.assertTrue(any("Intent outbox drain failed" in line for line in logs.output), logs.output)
        self.assertGreater(self._unconsumed(device), 0)


class TestTheSyncCacheJobStopsAtItsDeadline(_DeadlineCase):
    def test_the_inventory_read_runs_on_the_job_thread(self):
        """A worker thread would resolve the adapter settings on a database connection of its own."""
        from netbox_nso_plugin import jobs, sync_cache

        make_managed("job-deadline-snapshot-thread", 19209)
        threads = []
        read = sync_cache._snapshot

        def recording(rows):
            threads.append(threading.current_thread())
            return read(rows)

        with patch.object(sync_cache, "_snapshot", recording):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        self.assertEqual(threads, [threading.current_thread()])

    def test_a_slow_successful_snapshot_leaves_time_for_the_drain(self):
        from netbox_nso_plugin import adapter_client, jobs

        device, management = make_managed("job-deadline-snapshot", 19201)
        with without_commit_drain(), transaction.atomic():
            enqueue(device, "vlan")
        clock = time.monotonic
        delay = 0
        read_timeouts = []
        respond = self.adapter._handle

        def slow_inventory(method, url, **kwargs):
            nonlocal delay
            if method == "GET" and url.endswith("/api/v1/devices"):
                read_timeouts.append(kwargs["timeout"][1])
                delay = 190
                return make_response(200, self._listing(management))
            return respond(method, url, **kwargs)

        adapter_client.reset_session()
        with (
            patch.object(jobs, "SYSTEM_JOB_BUDGET_SECONDS", 240),
            patch.object(time, "monotonic", side_effect=lambda: clock() + delay),
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value={**CFG, "timeout": 220}),
            patch.object(self.adapter, "_handle", slow_inventory),
            patch.object(_SlowAdapter, "DELAY", 0),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        self.assertEqual(self._unconsumed(device), 0, "the inventory took all the send time")
        self.assertEqual(len(read_timeouts), 1)
        self.assertLessEqual(read_timeouts[0], 200)

    def test_a_snapshot_that_exceeds_its_budget_leaves_outbox_compaction_enabled(self):
        from netbox_nso_plugin import adapter_client, jobs

        device, _management = make_managed("job-deadline-snapshot-expired", 19202)
        for _ in range(2):
            with without_commit_drain(), transaction.atomic():
                enqueue(device, "vlan")
        read_timeouts = []
        respond = self.adapter._handle
        clock = time.monotonic
        delay = 0

        def unresponsive_inventory(method, url, **kwargs):
            nonlocal delay
            if method == "GET" and url.endswith("/api/v1/devices"):
                read_timeouts.append(kwargs["timeout"][1])
                # The read uses up its whole allowance before it times out.
                delay = kwargs["timeout"][1]
                raise ReadTimeout
            return respond(method, url, **kwargs)

        adapter_client.reset_session()
        with (
            patch.object(jobs, "SYSTEM_JOB_BUDGET_SECONDS", 6),
            patch.object(time, "monotonic", side_effect=lambda: clock() + delay),
            patch.object(self.adapter, "_handle", unresponsive_inventory),
            self.assertLogs(jobs.logger, level="WARNING") as logs,
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        self.assertEqual(len(read_timeouts), 1)
        self.assertLessEqual(read_timeouts[0], 5)
        self.assertIn(
            "RefreshDeviceSyncCacheJob: adapter snapshot unavailable, sends and sweep skipped",
            [record.getMessage() for record in logs.records],
        )
        self.assertEqual(self._unconsumed(device), 1)

    def test_a_snapshot_that_keeps_sending_past_its_cutoff_is_unavailable(self):
        """An HTTP/1.0 answer hands the socket to the response, and the cutoff must still reach it.

        Without a Content-Length the body ends at EOF, so a cut before its first byte looks like an empty listing.
        """
        cases = (("HTTP/1.1", True, 0), ("HTTP/1.0", True, 0), ("HTTP/1.0", False, 5))
        for index, (protocol, framed, stall) in enumerate(cases):
            with self.subTest(protocol=protocol, framed=framed):
                self._assert_trickled_snapshot_is_cut(protocol, framed, stall, 19216 + index)

    def _assert_trickled_snapshot_is_cut(self, protocol, framed, stall, adapter_device_id):
        import http.server
        import socketserver

        from netbox_nso_plugin import adapter_client, jobs

        make_managed(f"job-deadline-snapshot-trickle-{adapter_device_id}", adapter_device_id)
        responding = threading.Event()
        # A valid empty listing, one byte every 0.1 s: each read returns well inside its timeout.
        body = b"[" + b" " * 80 + b"]"

        class _Trickle(http.server.BaseHTTPRequestHandler):
            protocol_version = protocol

            def do_GET(self):
                responding.set()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if framed:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.flush()
                    time.sleep(stall)
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.1)

            def log_message(self, *args):
                pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Trickle)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cfg = {**CFG, "url": f"http://127.0.0.1:{server.server_address[1]}"}

        adapter_client.reset_session()
        started = time.monotonic()
        with (
            patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession),
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=cfg),
            self.assertLogs(jobs.logger, level="WARNING") as logs,
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()
        elapsed = time.monotonic() - started

        self.assertTrue(responding.is_set(), "no request reached the trickling server")
        self.assertLess(elapsed, self.BUDGET + 1, "the inventory read ran past its cutoff")
        self.assertIn(
            "RefreshDeviceSyncCacheJob: adapter snapshot unavailable, sends and sweep skipped",
            [record.getMessage() for record in logs.records],
        )

    def test_a_slow_adapter_defers_the_outbox_tail_and_the_sweep_still_runs(self):
        from netbox_nso_plugin import delivery, jobs

        device, management = make_managed("job-deadline-tick", 19162)
        keys = [key for key, entry in delivery.delivery_keys().items() if entry.in_protocol]
        with without_commit_drain():
            for scope in keys:
                with transaction.atomic():
                    enqueue(device, scope)
        listing = [
            {
                "id": management.adapter_device_id,
                "nso_instance": management.nso_instance.adapter_instance_id,
                "nso_device_name": management.nso_device_name,
                "netbox_device_id": device.pk,
                "last_sync_at": None,
                "last_sync_status": "",
                "degraded_surfaces": None,
            }
        ]
        self.assertGreater(len(keys) * _SlowAdapter.DELAY, 2 * self.BUDGET)
        swept = []

        def sweep(**kwargs):
            swept.append((time.monotonic(), kwargs["deadline"]))
            self.assertEqual(kwargs["feed_deadline"], kwargs["deadline"] + jobs.SETTLEMENT_FEED_GRACE_SECONDS)
            return 1, 0

        started = time.monotonic()
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", side_effect=sweep),
            self.assertLogs("netbox_nso_plugin.drain", level="INFO") as logs,
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, self.BUDGET + 2 * _SlowAdapter.DELAY)
        self.assertTrue(any("deferred" in line and "next tick" in line for line in logs.output), logs.output)
        self.assertGreater(self._unconsumed(device), 0)
        self.assertEqual(len(swept), 1, "the settlement sweep must run on every tick")
        # The sweep keeps its share of the budget: the drain stopped at the share, with one request in flight.
        called_at, run_deadline = swept[0]
        before_sweep = run_deadline - self.BUDGET * jobs.SETTLEMENT_SWEEP_SHARE
        self.assertLess(called_at - before_sweep, _SlowAdapter.DELAY + 0.1)

    def test_a_refresh_that_uses_its_whole_share_still_leaves_the_drain_its_own(self):
        from netbox_nso_plugin import jobs

        device, management = make_managed("job-deadline-drain-share", 19165)
        with without_commit_drain():
            with transaction.atomic():
                enqueue(device, "vlan")
        listing = [
            {
                "id": management.adapter_device_id,
                "nso_instance": management.nso_instance.adapter_instance_id,
                "nso_device_name": management.nso_device_name,
                "netbox_device_id": device.pk,
                "last_sync_at": None,
                "last_sync_status": "",
                "degraded_surfaces": None,
            }
        ]

        def slow_refresh(rows, *, snapshot, deadline, rotation):
            # A fleet whose mirror refresh runs until its deadline.
            time.sleep(max(0.0, deadline - time.monotonic()))
            return len(rows), 0

        with (
            patch.object(_SlowAdapter, "DELAY", 0),
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", side_effect=slow_refresh),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        self.assertEqual(self._unconsumed(device), 0, "the drain had no time left after the refresh")

    def test_a_link_repair_whose_adapter_calls_run_long_still_leaves_the_drain_its_own(self):
        from netbox_nso_plugin import adapter_client, jobs

        device, management = make_managed("job-deadline-repair-share", 19178)
        with without_commit_drain():
            with transaction.atomic():
                enqueue(device, "vlan")
        refused = []

        def busy_repair(rows, *, snapshot, deadline, rotation):
            # A link callback that started before the cutoff and keeps calling the adapter.
            stop = deadline + 10
            while time.monotonic() < stop:
                try:
                    adapter_client.get_device(management.adapter_device_id)
                except adapter_client.AdapterError as exc:
                    if exc.code == "request_limit":
                        refused.append(time.monotonic() - deadline)
                        break
                time.sleep(0.05)
            return 1, 1

        with (
            patch.object(_SlowAdapter, "DELAY", 0),
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=self._listing(management)),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", side_effect=busy_repair),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        self.assertEqual(len(refused), 1, "the repair's adapter calls ran past its cutoff")
        self.assertLess(refused[0], 0.5)
        self.assertEqual(self._unconsumed(device), 0, "the drain had no time left after the repair")

    def test_a_stage_request_limit_never_extends_the_run_limit(self):
        from netbox_nso_plugin import adapter_client

        with adapter_client.requests_until(time.monotonic() - 1), adapter_client.requests_until(time.monotonic() + 60):
            with self.assertRaises(adapter_client.AdapterError) as raised:
                adapter_client.get_device(1)

        self.assertEqual(raised.exception.code, "request_limit")
        self.assertEqual(self.adapter.requests, [])

    @staticmethod
    def _listing(*managements):
        return [
            {
                "id": management.adapter_device_id,
                "nso_instance": management.nso_instance.adapter_instance_id,
                "nso_device_name": management.nso_device_name,
                "netbox_device_id": management.device_id,
                "last_sync_at": None,
                "last_sync_status": "",
                "degraded_surfaces": None,
            }
            for management in managements
        ]

    def _outbox_only_tick(self, listing, *held, runs=1, after_each=None):
        from netbox_nso_plugin import jobs

        with (
            patch.object(jobs, "SYSTEM_JOB_BUDGET_SECONDS", 8.0),
            patch.object(_SlowAdapter, "DELAY", 0),
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
            contextlib.ExitStack() as stack,
        ):
            for row in held:
                stack.enter_context(HeldRowLock(row))
            for _run in range(runs):
                system_job(jobs.RefreshDeviceSyncCacheJob).run()
                if after_each is not None:
                    after_each()

    def test_a_held_compaction_candidate_still_leaves_the_sends_their_time(self):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        held_device, held_management = make_managed("job-deadline-compact-held", 19172)
        device, management = make_managed("job-deadline-compact-healthy", 19173, index=2)
        with without_commit_drain():
            for _ in range(2):
                with transaction.atomic():
                    enqueue(held_device, "vlan")
            with transaction.atomic():
                enqueue(device, "vlan")
        # Tried just now, so the send loop reaches the healthy, never-tried key first.
        held = NSOIntentOutboxState.objects.create(
            device=held_device, scope="vlan", last_drain_attempted_at=timezone.now()
        )

        self._outbox_only_tick(self._listing(held_management, management), held)

        self.assertEqual(self._unconsumed(device), 0, "the held compaction candidate took the sends' time")

    def test_a_held_compaction_candidate_goes_last_so_the_keys_behind_it_are_compacted(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentOutboxState

        held_device, _held_management = make_managed("job-deadline-compact-first", 19190)
        device, _management = make_managed("job-deadline-compact-second", 19191, index=2)
        with without_commit_drain():
            for target in (held_device, device):
                for _ in range(2):
                    with transaction.atomic():
                        enqueue(target, "vlan")
        # No adapter mapping, so only compaction can merge these rows.
        NSODeviceManagement.objects.filter(device__in=(held_device, device)).update(adapter_device_id=None)
        held = NSOIntentOutboxState.objects.create(device=held_device, scope="vlan")
        unconsumed = []

        self._outbox_only_tick([], held, runs=2, after_each=lambda: unconsumed.append(self._unconsumed(device)))

        self.assertEqual(unconsumed[0], 2, "the held key did not use the first run's compaction time")
        self.assertEqual(unconsumed[1], 1, "the held key took the second run's compaction time too")

    def test_a_held_drain_key_goes_last_so_the_keys_behind_it_get_their_turn(self):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        held_device, held_management = make_managed("job-deadline-drain-held", 19174)
        device, management = make_managed("job-deadline-drain-behind", 19175, index=2)
        with without_commit_drain():
            for target in (held_device, device):
                with transaction.atomic():
                    enqueue(target, "vlan")
        # Never tried, and the lower device id, so the held key is first in the send order.
        held = NSOIntentOutboxState.objects.create(device=held_device, scope="vlan")
        unconsumed = []

        self._outbox_only_tick(
            self._listing(held_management, management),
            held,
            runs=2,
            after_each=lambda: unconsumed.append(self._unconsumed(device)),
        )

        self.assertEqual(unconsumed[0], 1, "the held key did not use the first run's send time")
        self.assertEqual(unconsumed[1], 0, "the held key took the second run's send time too")

    def test_a_failed_repair_whose_fallback_stamp_waits_on_a_held_row_is_lock_skipped(self):
        from netbox_nso_plugin import sync_cache
        from netbox_nso_plugin.models import NSODeviceManagement

        _device, management = make_managed("job-deadline-relink-fallback", 19177)
        real_mirror = sync_cache._mirror_management
        holders = []

        def mirror(mgmt, **values):
            if not connection.in_atomic_block:
                # The repair rolled back its stamp. An operator edit now holds the row.
                holder = HeldRowLock(NSODeviceManagement.objects.get(pk=mgmt.pk))
                holder.__enter__()
                holders.append(holder)
            return real_mirror(mgmt, **values)

        rotation = {}
        short_lock_timeout(self)
        try:
            with (
                # The adapter no longer lists the device, so its mapping is broken.
                patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
                patch.object(sync_cache, "_mirror_management", side_effect=mirror),
                patch.object(sync_cache, "invalidate_delivery_baselines", side_effect=RuntimeError("repair failed")),
                self.assertLogs("netbox_nso_plugin.sync_cache", level="ERROR"),
            ):
                sync_cache.reconcile_device_links([management], rotation=rotation)
        finally:
            for holder in holders:
                holder.__exit__(None, None, None)

        self.assertEqual(len(holders), 1, "the fallback stamp did not run")
        management.refresh_from_db()
        self.assertIsNone(management.adapter_link_attempted_at)
        self.assertIn(str(management.pk), rotation["lock_skipped"])

    def test_lock_skipped_keys_that_fill_a_batch_still_let_the_next_key_in(self):
        from netbox_nso_plugin import drain

        devices = [
            make_managed(f"job-deadline-drain-batch-{index}", 19179 + index, index=index)[0] for index in range(3)
        ]
        with without_commit_drain():
            for device in devices:
                with transaction.atomic():
                    enqueue(device, "vlan")
        now = timezone.now()

        candidates = drain.drain_candidates(
            limit=2, lock_skipped={(devices[0].pk, "vlan"): now, (devices[1].pk, "vlan"): now}
        )

        self.assertEqual(candidates[0], (devices[2].pk, "vlan"))

    def test_two_held_keys_ahead_of_a_one_key_batch_still_let_the_next_key_in(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        devices = [
            make_managed(f"job-deadline-drain-pair-{index}", 19183 + index, index=index)[0] for index in range(3)
        ]
        with without_commit_drain():
            for device in devices:
                with transaction.atomic():
                    enqueue(device, "vlan")
        held = [NSOIntentOutboxState.objects.create(device=device, scope="vlan") for device in devices[:2]]
        tried = []
        real_drain_key = drain.drain_key

        def record(device_id, scope, **kwargs):
            tried.append(device_id)
            return real_drain_key(device_id, scope, **kwargs)

        rotation = {}
        short_lock_timeout(self)
        with (
            patch.object(_SlowAdapter, "DELAY", 0),
            patch.object(drain, "drain_key", side_effect=record),
            self.assertLogs("netbox_nso_plugin.drain", level="ERROR"),
            HeldRowLock(held[0]),
            HeldRowLock(held[1]),
        ):
            for _pass in range(3):
                drain.drain_intent_outbox(limit=1, deadline=time.monotonic() + 30, rotation=rotation)

        self.assertEqual(tried, [devices[0].pk, devices[1].pk, devices[2].pk])

    def test_a_failed_key_whose_fallback_stamp_waits_on_a_held_row_is_lock_skipped(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        device, _management = make_managed("job-deadline-drain-restamp", 19182)
        with without_commit_drain():
            with transaction.atomic():
                enqueue(device, "vlan")
        state = NSOIntentOutboxState.objects.create(device=device, scope="vlan")
        holders = []

        def fail_then_hold(device_id, scope, **_kwargs):
            # The key's drain fails, and an operator edit then takes its state row.
            holder = HeldRowLock(state)
            holder.__enter__()
            holders.append(holder)
            raise RuntimeError("render failed")

        rotation = {}
        short_lock_timeout(self)
        try:
            with (
                patch.object(drain, "drain_key", side_effect=fail_then_hold),
                self.assertLogs("netbox_nso_plugin.drain", level="ERROR"),
            ):
                drain.drain_intent_outbox(deadline=time.monotonic() + 30, rotation=rotation)
        finally:
            for holder in holders:
                holder.__exit__(None, None, None)

        self.assertEqual([entry[:2] for entry in rotation["lock_skipped"]], [[device.pk, "vlan"]])

    def test_a_drain_lock_skip_stays_in_the_rotation_when_a_later_key_overruns(self):
        from netbox_nso_plugin import drain, jobs
        from netbox_nso_plugin.models import NSOIntentOutboxState

        devices = [
            make_managed(f"job-deadline-drain-overrun-{index}", 19186 + index, index=index)[0] for index in range(2)
        ]
        with without_commit_drain():
            for device in devices:
                with transaction.atomic():
                    enqueue(device, "vlan")
        held = NSOIntentOutboxState.objects.create(device=devices[0], scope="vlan")
        real_drain_key = drain.drain_key

        def drain_or_overrun(device_id, scope, **kwargs):
            if device_id == devices[1].pk:
                # RQ's alarm, as the job's timeout handler raises it.
                raise jobs._RunTimedOut
            return real_drain_key(device_id, scope, **kwargs)

        rotation = {}
        short_lock_timeout(self)
        with (
            patch.object(_SlowAdapter, "DELAY", 0),
            patch.object(drain, "drain_key", side_effect=drain_or_overrun),
            self.assertLogs("netbox_nso_plugin.drain", level="ERROR"),
            HeldRowLock(held),
            self.assertRaises(jobs._RunTimedOut),
        ):
            drain.drain_intent_outbox(deadline=time.monotonic() + 30, rotation=rotation)

        self.assertEqual(
            [entry[:2] for entry in rotation["lock_skipped"]], [[devices[0].pk, "vlan"], [devices[1].pk, "vlan"]]
        )

    def test_a_key_whose_claim_the_timeout_interrupts_rotates_behind_an_untouched_key(self):
        from netbox_nso_plugin import drain, jobs

        devices = [
            make_managed(f"job-deadline-drain-alarm-{index}", 19217 + index, index=index)[0] for index in range(2)
        ]
        with without_commit_drain():
            for device in devices:
                with transaction.atomic():
                    enqueue(device, "vlan")
        tried = []
        real_drain_key = drain.drain_key

        def overrun_the_first_claim(device_id, scope, **kwargs):
            tried.append(device_id)
            if len(tried) == 1:
                # RQ's alarm during the claim: its attempt stamp rolls back with it.
                raise jobs._RunTimedOut
            return real_drain_key(device_id, scope, **kwargs)

        rotation = {}
        with (
            patch.object(_SlowAdapter, "DELAY", 0),
            patch.object(drain, "drain_key", side_effect=overrun_the_first_claim),
        ):
            with self.assertRaises(jobs._RunTimedOut):
                drain.drain_intent_outbox(limit=1, deadline=time.monotonic() + 30, rotation=rotation)
            drain.drain_intent_outbox(limit=1, deadline=time.monotonic() + 30, rotation=rotation)

        self.assertEqual(tried, [devices[0].pk, devices[1].pk])

    def test_a_key_whose_fallback_stamp_the_timeout_interrupts_stays_in_the_rotation(self):
        from netbox_nso_plugin import drain, jobs

        device, _management = make_managed("job-deadline-drain-restamp-alarm", 19219)
        with without_commit_drain():
            with transaction.atomic():
                enqueue(device, "vlan")

        rotation = {}
        with (
            patch.object(drain, "drain_key", side_effect=RuntimeError("render failed")),
            # RQ's alarm during the fallback stamp, after the claim rolled back.
            patch.object(drain, "_restamp_attempt", side_effect=jobs._RunTimedOut),
            self.assertLogs("netbox_nso_plugin.drain", level="ERROR"),
            self.assertRaises(jobs._RunTimedOut),
        ):
            drain.drain_intent_outbox(deadline=time.monotonic() + 30, rotation=rotation)

        self.assertEqual([entry[:2] for entry in rotation["lock_skipped"]], [[device.pk, "vlan"]])

    def test_a_key_whose_fallback_stamp_fails_on_an_unusable_connection_is_lock_skipped(self):
        from django.db import OperationalError

        from netbox_nso_plugin import drain

        device, _management = make_managed("job-deadline-drain-restamp-broken", 19220)
        with without_commit_drain():
            with transaction.atomic():
                enqueue(device, "vlan")

        rotation = {}
        unusable = OperationalError("the connection is not usable")
        with (
            # A cleanup statement replaced the claim's error, and the fallback stamp fails the same way.
            patch.object(drain, "drain_key", side_effect=unusable),
            patch.object(drain, "_stamp_attempt", side_effect=unusable),
            self.assertLogs("netbox_nso_plugin.drain", level="ERROR"),
        ):
            drain.drain_intent_outbox(deadline=time.monotonic() + 30, rotation=rotation)

        self.assertEqual([entry[:2] for entry in rotation["lock_skipped"]], [[device.pk, "vlan"]])

    def test_a_repair_lock_skip_stays_in_the_rotation_when_a_later_row_overruns(self):
        from netbox_nso_plugin import jobs, sync_cache
        from netbox_nso_plugin.models import NSODeviceManagement

        _first_device, first = make_managed("job-deadline-relink-overrun-a", 19188)
        _second_device, second = make_managed("job-deadline-relink-overrun-b", 19189, index=2)
        real_mirror = sync_cache._mirror_management
        holders = []

        def mirror(mgmt, **values):
            if mgmt.pk == first.pk and not connection.in_atomic_block:
                # The repair rolled back its stamp. An operator edit now holds the row.
                holder = HeldRowLock(NSODeviceManagement.objects.get(pk=mgmt.pk))
                holder.__enter__()
                holders.append(holder)
            return real_mirror(mgmt, **values)

        def fail_then_overrun(device_id):
            if device_id == first.device_id:
                raise RuntimeError("repair failed")
            # RQ's alarm, as the job's timeout handler raises it.
            raise jobs._RunTimedOut

        rotation = {}
        short_lock_timeout(self)
        try:
            with (
                # The adapter no longer lists the devices, so their mappings are broken.
                patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
                patch.object(sync_cache, "_mirror_management", side_effect=mirror),
                patch.object(sync_cache, "invalidate_delivery_baselines", side_effect=fail_then_overrun),
                self.assertLogs("netbox_nso_plugin.sync_cache", level="ERROR"),
                self.assertRaises(jobs._RunTimedOut),
            ):
                sync_cache.reconcile_device_links([first, second], rotation=rotation)
        finally:
            for holder in holders:
                holder.__exit__(None, None, None)

        self.assertEqual(len(holders), 1, "the fallback stamp did not run")
        self.assertEqual(list(rotation["lock_skipped"]), [str(first.pk), str(second.pk)])
        second.refresh_from_db()
        self.assertIsNone(second.adapter_link_attempted_at, "the interrupted repair ran its fallback stamp")

    def test_an_interrupted_repair_rotates_behind_the_next_broken_link(self):
        from netbox_nso_plugin import adapter_client, jobs

        _first_device, first = make_managed("job-deadline-relink-alarm", 19209)
        _later_device, later = make_managed("job-deadline-relink-later", 19210, index=2)
        management_table = first._meta.db_table

        def missing_mappings(method, url, **kwargs):
            return make_response(200, [] if url.endswith("/api/v1/devices") else {})

        def interrupt_before_commit(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if (
                sql.startswith(f'UPDATE "{management_table}"')
                and '"adapter_link_attempted_at" =' in sql
                and params[-1] == first.pk
            ):
                signal.raise_signal(signal.SIGALRM)
            return result

        adapter_client.reset_session()
        job = system_job(jobs.RefreshDeviceSyncCacheJob)
        with (
            patch.object(self.adapter, "_handle", missing_mappings),
            connection.execute_wrapper(interrupt_before_commit),
            without_commit_drain(),
        ):
            with self.assertRaises(JobTimeoutException):
                job.run()
            with self.assertRaises(JobTimeoutException):
                system_job(jobs.RefreshDeviceSyncCacheJob).run()

        first.refresh_from_db()
        later.refresh_from_db()
        self.assertIsNone(first.adapter_link_attempted_at)
        self.assertIsNotNone(later.adapter_link_attempted_at)
        job.job.refresh_from_db()
        self.assertIn(str(first.pk), job.job.data["repair_rotation"]["lock_skipped"])

    def test_an_alarm_during_a_deferral_warning_keeps_the_row_out_of_rotation(self):
        from netbox_nso_plugin import adapter_client, jobs, sync_cache

        _first_device, first = make_managed("job-deadline-relink-attempted", 19214)
        _second_device, second = make_managed("job-deadline-relink-deferred", 19215, index=2)
        management_table = second._meta.db_table
        warning = "Adapter mapping is broken; repair deferred to the next sweep."

        def interrupt_warning(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith(f'UPDATE "{management_table}"') and params[-1] == second.pk and warning in params:
                signal.raise_signal(signal.SIGALRM)
            return result

        adapter_client.reset_session()
        job = system_job(jobs.RefreshDeviceSyncCacheJob)
        with (
            patch.object(self.adapter, "_handle", return_value=make_response(200, [])),
            patch.object(sync_cache, "MAX_RELINKS_PER_RUN", 1),
            connection.execute_wrapper(interrupt_warning),
            without_commit_drain(),
            self.assertRaises(JobTimeoutException),
        ):
            job.run()

        first.refresh_from_db()
        self.assertIsNotNone(first.adapter_link_attempted_at)
        job.job.refresh_from_db()
        self.assertNotIn(str(second.pk), job.job.data.get("repair_rotation", {}).get("lock_skipped", {}))

    def test_an_alarm_during_the_fallback_link_stamp_carries_the_rotation(self):
        from netbox_nso_plugin import adapter_client, jobs, sync_cache

        _device, management = make_managed("job-deadline-relink-stamp", 19212)
        job = system_job(jobs.RefreshDeviceSyncCacheJob)
        repair_failed = False

        def fail_repair(_device_id):
            nonlocal repair_failed
            repair_failed = True
            raise RuntimeError("Repair failed.")

        def locks_management(sql):
            table = management._meta.db_table
            return repair_failed and f'FROM "{table}"' in sql and f'FOR UPDATE OF "{table}"' in sql

        adapter_client.reset_session()
        with (
            patch.object(jobs, "SYSTEM_JOB_BUDGET_SECONDS", 30),
            patch.object(self.adapter, "_handle", return_value=make_response(200, [])),
            patch.object(sync_cache, "invalidate_delivery_baselines", fail_repair),
            self._alarm_during_stamp(management, locks_management),
            self.assertRaises(JobTimeoutException),
        ):
            job.run()

        management.refresh_from_db()
        self.assertIsNone(management.adapter_link_attempted_at)
        job.job.refresh_from_db()
        self.assertIn(str(management.pk), job.job.data["repair_rotation"].get("lock_skipped", {}))

    def test_a_refresh_that_uses_its_whole_share_still_leaves_the_link_repair_its_own(self):
        from netbox_nso_plugin import jobs

        _device, management = make_managed("job-deadline-repair-share", 19168)

        def slow_refresh(rows, *, snapshot, deadline, rotation):
            time.sleep(max(0.0, deadline - time.monotonic()))
            return len(rows), 0

        with (
            patch.object(_SlowAdapter, "DELAY", 0),
            # The adapter no longer lists the device, so its mapping is broken.
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", side_effect=slow_refresh),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        management.refresh_from_db()
        self.assertIsNotNone(management.adapter_link_attempted_at, "the link repair had no time left")

    def test_a_held_management_row_skips_its_refresh_and_the_tick_goes_on(self):
        from netbox_nso_plugin import jobs

        device, management = make_managed("job-deadline-refresh-lock", 19166)
        listing = [
            {
                "id": management.adapter_device_id,
                "nso_instance": management.nso_instance.adapter_instance_id,
                "nso_device_name": management.nso_device_name,
                "netbox_device_id": device.pk,
                "last_sync_at": None,
                "last_sync_status": "succeeded",
                "degraded_surfaces": None,
            }
        ]
        swept = []
        with (
            patch.object(jobs, "SYSTEM_JOB_LOCK_TIMEOUT", "1s"),
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch(
                "netbox_nso_plugin.settlement.sweep_static_route_settlements",
                side_effect=lambda **_kwargs: swept.append(True) or (0, 0),
            ),
            self.assertLogs("netbox_nso_plugin.jobs", level="WARNING") as logs,
            HeldRowLock(management),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        self.assertEqual(swept, [True], "the lock timeout aborted the tick before the sweep")
        self.assertTrue(any("lock wait timed out" in line for line in logs.output), logs.output)
        management.refresh_from_db()
        self.assertEqual(management.last_sync_status, "")

    def test_a_held_row_over_the_repair_cap_skips_its_flag_and_the_tick_goes_on(self):
        from netbox_nso_plugin import jobs

        _device, management = make_managed("job-deadline-relink-lock", 19167)
        swept = []
        with (
            patch.object(jobs, "SYSTEM_JOB_LOCK_TIMEOUT", "1s"),
            patch("netbox_nso_plugin.sync_cache.MAX_RELINKS_PER_RUN", 0),
            # The adapter no longer lists the device, so its mapping is broken.
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch(
                "netbox_nso_plugin.settlement.sweep_static_route_settlements",
                side_effect=lambda **_kwargs: swept.append(True) or (0, 0),
            ),
            self.assertLogs("netbox_nso_plugin.jobs", level="WARNING") as logs,
            HeldRowLock(management),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        self.assertEqual(swept, [True], "the lock timeout aborted the tick before the sweep")
        self.assertTrue(any("Link reconcile skipped" in line for line in logs.output), logs.output)

    def test_held_repair_rows_go_last_so_the_rows_behind_them_get_their_turn(self):
        from netbox_nso_plugin import jobs

        _first_device, first = make_managed("job-deadline-relink-held-a", 19169)
        _second_device, second = make_managed("job-deadline-relink-held-b", 19171)
        _device, behind = make_managed("job-deadline-relink-behind", 19170)
        with (
            # One lock wait outlasts the repair share, so each tick reaches one row only.
            patch.object(jobs, "SYSTEM_JOB_LOCK_TIMEOUT", "2s"),
            patch.object(_SlowAdapter, "DELAY", 0),
            # The adapter no longer lists any of the devices, so every mapping is broken.
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
            HeldRowLock(first),
            HeldRowLock(second),
        ):
            for _tick in range(2):
                system_job(jobs.RefreshDeviceSyncCacheJob).run()
                behind.refresh_from_db()
                self.assertIsNone(behind.adapter_link_attempted_at, "a held row did not use the repair share")
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        behind.refresh_from_db()
        self.assertIsNotNone(behind.adapter_link_attempted_at, "the held rows took every tick")


class TestCompactionStopsAtTheDeadline(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Compaction runs one transaction per key, so a large backlog must not use the sweep's share."""

    def setUp(self):
        super().setUp()
        self.device, _management = make_managed("job-deadline-compact", 19163)
        with without_commit_drain():
            for _ in range(2):
                with transaction.atomic():
                    enqueue(self.device, "vlan")

    def _rows(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        return NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").count()

    def test_the_tick_leaves_compaction_to_the_next_tick_after_the_deadline(self):
        from netbox_nso_plugin import drain

        with self.assertLogs("netbox_nso_plugin.drain", level="INFO") as logs:
            drain.drain_intent_outbox(deadline=time.monotonic() - 1)
        self.assertEqual(self._rows(), 2)
        self.assertTrue(any("compaction deferred" in line for line in logs.output), logs.output)
        drain.compact_intent_outbox()
        self.assertEqual(self._rows(), 1)

    def test_an_expired_deadline_skips_candidate_discovery(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        table = NSOIntentOutboxEntry._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            drain.compact_intent_outbox(deadline=time.monotonic() - 1)
        self.assertFalse([query["sql"] for query in queries.captured_queries if table in query["sql"]])
        self.assertEqual(self._rows(), 2)

    def test_discovery_stops_reading_keys_when_the_deadline_passes(self):
        from netbox_nso_plugin import drain

        other, _management = make_managed("job-deadline-compact-b", 19164, index=2)
        with without_commit_drain():
            for _ in range(2):
                with transaction.atomic():
                    enqueue(other, "vlan")
        clock, read = [0.0], []
        unconsumed = drain._unconsumed

        def _read_key(device_id, scope):
            # The deadline passes while discovery reads the first key's entries.
            read.append((device_id, scope))
            clock[0] = 100.0
            return unconsumed(device_id, scope)

        with (
            patch("netbox_nso_plugin.drain._send_clock", lambda: clock[0]),
            patch("netbox_nso_plugin.drain._unconsumed", _read_key),
        ):
            drain.compact_intent_outbox(deadline=50.0)
        self.assertEqual(read, [(self.device.pk, "vlan")])

    def test_a_key_found_before_discovery_reaches_the_deadline_is_still_compacted(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        other, _management = make_managed("job-deadline-compact-c", 19166, index=2)
        with without_commit_drain():
            for _ in range(2):
                with transaction.atomic():
                    enqueue(other, "vlan")
        clock = [0.0]
        unconsumed = drain._unconsumed

        def _read_key(device_id, scope):
            # The deadline passes while discovery reads the second key's entries.
            if device_id == other.pk:
                clock[0] = 100.0
            return unconsumed(device_id, scope)

        with (
            patch("netbox_nso_plugin.drain._send_clock", lambda: clock[0]),
            patch("netbox_nso_plugin.drain._unconsumed", _read_key),
        ):
            drain.compact_intent_outbox(deadline=50.0)
        self.assertEqual(self._rows(), 1)
        self.assertEqual(NSOIntentOutboxEntry.objects.filter(device=other, scope="vlan").count(), 2)

    def test_a_key_whose_discovery_uses_the_deadline_goes_last_so_the_keys_behind_it_are_compacted(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        other, _management = make_managed("job-deadline-compact-d", 19167, index=2)
        with without_commit_drain():
            for _ in range(2):
                with transaction.atomic():
                    enqueue(other, "vlan")
        clock = [0.0]
        unconsumed = drain._unconsumed

        def _read_key(device_id, scope):
            # Reading the first key's entries uses the whole deadline on every tick.
            if device_id == self.device.pk:
                clock[0] = 100.0
            return unconsumed(device_id, scope)

        rotation = {}
        with (
            patch("netbox_nso_plugin.drain._send_clock", lambda: clock[0]),
            patch("netbox_nso_plugin.drain._unconsumed", _read_key),
        ):
            for _tick in range(2):
                clock[0] = 0.0
                drain.compact_intent_outbox(deadline=50.0, rotation=rotation)
        self.assertEqual(NSOIntentOutboxEntry.objects.filter(device=other, scope="vlan").count(), 1)

    def test_lock_waits_end_at_the_drain_s_deadline_so_the_sweep_keeps_its_share(self):
        from netbox_nso_plugin import jobs
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentOutboxState

        state, _created = NSOIntentOutboxState.objects.get_or_create(device=self.device, scope="vlan")
        queued = set(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").values_list("pk", flat=True))
        swept = []

        def sweep(**kwargs):
            swept.append((time.monotonic(), kwargs["deadline"]))
            return 0, 0

        budget = 8.0
        with (
            patch.object(jobs, "SYSTEM_JOB_BUDGET_SECONDS", budget),
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], {}, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", side_effect=sweep),
            # The compaction and the claim of this key each wait on the held state row.
            HeldRowLock(state),
            self.assertLogs("netbox_nso_plugin.drain", level="ERROR") as logs,
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        called_at, run_deadline = swept[0]
        self.assertLess(called_at - (run_deadline - budget * jobs.SETTLEMENT_SWEEP_SHARE), 1)
        # Not compacted. The claim's pre-capture audit can queue a repair entry beside them.
        self.assertEqual(len(queued), 2)
        self.assertEqual(NSOIntentOutboxEntry.objects.filter(pk__in=queued).count(), 2)
        self.assertTrue(
            any(
                record.getMessage() == f"intent outbox drain failed for {self.device.pk}/vlan"
                and record.exc_info
                and jobs.lock_wait_timed_out(record.exc_info[1])
                for record in logs.records
            ),
            logs.output,
        )

    def test_a_held_state_row_fails_the_compaction_at_the_job_lock_timeout(self):
        from netbox_nso_plugin import jobs
        from netbox_nso_plugin.models import NSOIntentOutboxState

        state, _created = NSOIntentOutboxState.objects.get_or_create(device=self.device, scope="vlan")
        started = time.monotonic()
        with (
            patch.object(jobs, "SYSTEM_JOB_LOCK_TIMEOUT", "1s"),
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], None, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
            self.assertLogs("netbox_nso_plugin.drain", level="ERROR") as logs,
            HeldRowLock(state),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 10, "the compaction waited for the held lock")
        self.assertTrue(any("compaction failed" in line for line in logs.output), logs.output)
        self.assertEqual(self._rows(), 2)

    def test_the_outage_branch_compacts_inside_the_drain_share(self):
        from netbox_nso_plugin import jobs

        with (
            patch.object(jobs, "SETTLEMENT_SWEEP_SHARE", 1.0),
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], None, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()
        self.assertEqual(self._rows(), 2)


class TestTheRotationsSurviveBetweenRuns(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """RQ runs each job in a forked work horse, so a cut pass resumes from the Job row, not memory."""

    def setUp(self):
        super().setUp()
        reset_renderer_audit_rotation(self)
        patch_matching_control_state(self)
        clock = time.monotonic
        self.clock_offset = 0
        patcher = patch.object(time, "monotonic", side_effect=lambda: clock() + self.clock_offset)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rows = [make_managed("job-rotation", 19170 + index, index=index)[1] for index in range(3)]

    @staticmethod
    def _carried(job_class, key):
        from core.models import Job

        return Job.objects.filter(name=job_class.name).order_by("-created").first().data[key]

    def test_the_settlement_sweep_resumes_after_the_device_the_previous_run_reached(self):
        from netbox_nso_plugin import jobs
        from netbox_nso_plugin.models import NSODeviceManagement

        NSODeviceManagement.objects.filter(pk__in=[row.pk for row in self.rows]).update(settle_resume_pending=True)
        listing = [
            {
                "id": row.adapter_device_id,
                "nso_instance": row.nso_instance.adapter_instance_id,
                "nso_device_name": row.nso_device_name,
                "netbox_device_id": row.device_id,
                "last_sync_at": None,
                "last_sync_status": "",
                "degraded_surfaces": None,
            }
            for row in self.rows
        ]
        polled = []

        def poll(pk, **kwargs):
            polled.append(pk)
            self.clock_offset += jobs.SYSTEM_JOB_BUDGET_SECONDS

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch("netbox_nso_plugin.settlement.settle_static_routes", side_effect=poll),
        ):
            for _run in range(4):
                system_job(jobs.RefreshDeviceSyncCacheJob).run()

        pks = sorted(row.pk for row in self.rows)
        self.assertEqual(polled, [*pks, pks[0]])
        self.assertEqual(self._carried(jobs.RefreshDeviceSyncCacheJob, "settlement_rotation"), {"after_pk": pks[0]})

    def test_the_last_sync_refresh_resumes_after_the_row_the_previous_run_reached(self):
        from netbox_nso_plugin import jobs

        listing = [
            {
                "id": row.adapter_device_id,
                "nso_instance": row.nso_instance.adapter_instance_id,
                "nso_device_name": row.nso_device_name,
                "netbox_device_id": row.device_id,
                "last_sync_at": None,
                "last_sync_status": "",
                "degraded_surfaces": None,
            }
            for row in self.rows
        ]
        refreshed = []

        def refresh(mgmt, _device):
            refreshed.append(mgmt.pk)
            self.clock_offset += jobs.SYSTEM_JOB_BUDGET_SECONDS
            return []

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_cache", side_effect=refresh),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
        ):
            for _run in range(4):
                system_job(jobs.RefreshDeviceSyncCacheJob).run()

        pks = sorted(row.pk for row in self.rows)
        self.assertEqual(refreshed, [*pks, pks[0]])
        self.assertEqual(self._carried(jobs.RefreshDeviceSyncCacheJob, "refresh_rotation"), {"after_pk": pks[0]})

    def test_the_renderer_audit_resumes_after_the_device_the_previous_run_reached(self):
        from netbox_nso_plugin import jobs
        from netbox_nso_plugin.renderer_audit import RendererAuditResult

        reached = []

        def audit(device_id, scopes, trigger, *, deadline=None, pre_capture=False):
            reached.append(device_id)
            return RendererAuditResult(("vlan",), ())

        with (
            patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit),
            patch("netbox_nso_plugin.renderer_audit._budget_expired", side_effect=[False, True] * 4),
        ):
            for _run in range(4):
                system_job(jobs.AuditRendererScopesJob).run()

        ids = sorted(row.device_id for row in self.rows)
        self.assertEqual(reached, [*ids, ids[0]])
        self.assertEqual(
            self._carried(jobs.AuditRendererScopesJob, "fleet_rotation"),
            {"after_device_id": ids[0], "tombstone_rotation": {"lock_skipped": {}}},
        )


class TestSystemJobsSetTheirOwnTimeout(_CascadeFlushMixin, TransactionTestCase):
    def test_every_plugin_system_job_enqueues_with_the_plugin_timeout(self):
        from netbox.registry import registry
        from rq.job import Job as RQJob

        from netbox_nso_plugin import jobs

        queue = _rq_test_queue(self)
        plugin_jobs = [cls for cls in registry["system_jobs"] if cls.__module__ == jobs.__name__]
        self.assertEqual(len(plugin_jobs), 4)
        self.assertGreater(jobs.SYSTEM_JOB_TIMEOUT_SECONDS, jobs.SYSTEM_JOB_REQUEST_LIMIT_SECONDS)
        self.assertGreater(
            jobs.SYSTEM_JOB_REQUEST_LIMIT_SECONDS, jobs.SYSTEM_JOB_BUDGET_SECONDS + jobs.SETTLEMENT_FEED_GRACE_SECONDS
        )
        # A poll that pages its feed until the grace ends still has time for its later reads.
        self.assertLessEqual(
            jobs.SETTLEMENT_FEED_GRACE_SECONDS, (jobs.SYSTEM_JOB_TIMEOUT_SECONDS - jobs.SYSTEM_JOB_BUDGET_SECONDS) / 2
        )
        for job_class in plugin_jobs:
            with patch("django_rq.get_queue", return_value=queue):
                job = job_class.enqueue(schedule_at=timezone.now() + timedelta(hours=1), interval=5)
            rq_job = RQJob.fetch(str(job.job_id), connection=queue.connection)
            self.addCleanup(rq_job.delete)
            self.assertEqual(rq_job.timeout, jobs.SYSTEM_JOB_TIMEOUT_SECONDS, job_class.name)

    def test_an_update_that_waits_on_a_held_row_ends_at_the_deadline(self):
        from core.models import Job
        from django.db import OperationalError

        from netbox_nso_plugin import jobs

        held = Job.objects.create(name="nso-test-held-row", job_id=uuid4())
        short_lock_timeout(self)
        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            previous = cursor.fetchone()[0]
        with HeldRowLock(held), self.assertRaises(OperationalError) as caught:
            started = time.monotonic()
            with jobs.lock_waits_until(time.monotonic() + 1):
                Job.objects.filter(pk=held.pk).update(error="held")
        self.assertLess(time.monotonic() - started, 1.75)
        self.assertTrue(jobs.lock_wait_timed_out(caught.exception), caught.exception)
        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            self.assertEqual(cursor.fetchone()[0], previous)

    def test_an_advisory_lock_wait_ends_at_the_deadline(self):
        from django.db import OperationalError

        from netbox_nso_plugin import jobs

        held, release = threading.Event(), threading.Event()

        def hold():
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(19167, 1)")
                    held.set()
                    release.wait(30)
            finally:
                connection.close()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(10))
        started = time.monotonic()
        with self.assertRaises(OperationalError) as caught:
            with jobs.lock_waits_until(time.monotonic() + 1), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [19167, 1])
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(jobs.lock_wait_timed_out(caught.exception), caught.exception)

    def test_every_plugin_system_job_bounds_its_lock_waits_and_restores_the_session(self):
        from netbox.registry import registry

        from netbox_nso_plugin import jobs

        def lock_timeout():
            with connection.cursor() as cursor:
                cursor.execute("SHOW lock_timeout")
                return cursor.fetchone()[0]

        before = lock_timeout()
        self.assertNotEqual(before, jobs.SYSTEM_JOB_LOCK_TIMEOUT)
        for job_class in [cls for cls in registry["system_jobs"] if cls.__module__ == jobs.__name__]:
            seen = []
            with patch.object(job_class, "tick", lambda _self, seen=seen: seen.append(lock_timeout())):
                system_job(job_class).run()
            self.assertEqual(seen, [jobs.SYSTEM_JOB_LOCK_TIMEOUT], job_class.name)
            self.assertEqual(lock_timeout(), before, job_class.name)

    def test_no_plugin_system_job_can_swallow_rq_s_timeout(self):
        import signal

        from netbox.registry import registry
        from rq.timeouts import JobTimeoutException, UnixSignalDeathPenalty

        from netbox_nso_plugin import jobs

        def tick(_self):
            with contextlib.suppress(Exception):
                signal.raise_signal(signal.SIGALRM)

        for job_class in [cls for cls in registry["system_jobs"] if cls.__module__ == jobs.__name__]:
            with (
                patch.object(job_class, "tick", tick),
                UnixSignalDeathPenalty(60, JobTimeoutException, job_id=job_class.name),
                self.assertRaises(JobTimeoutException, msg=job_class.name),
            ):
                system_job(job_class).run()

    def test_netbox_s_reschedule_after_a_run_keeps_the_plugin_timeout(self):
        from core.models import Job
        from rq.job import Job as RQJob

        from netbox_nso_plugin import jobs

        queue = _rq_test_queue(self)
        with patch("django_rq.get_queue", return_value=queue):
            first = jobs.PruneApplyLedgerJob.enqueue(schedule_at=timezone.now() + timedelta(hours=1), interval=1440)
            jobs.PruneApplyLedgerJob.handle(first)
        successor = (
            Job.objects.filter(name=jobs.PruneApplyLedgerJob.name, status="scheduled").exclude(pk=first.pk).get()
        )

        rq_job = RQJob.fetch(str(successor.job_id), connection=queue.connection)
        self.assertEqual(rq_job.timeout, jobs.SYSTEM_JOB_TIMEOUT_SECONDS)

    def _overrun(self, tick, job_class=None):
        """Run a plugin system job through NetBox's handle under RQ's own 1 s timeout alarm."""
        from core.models import Job
        from rq.timeouts import JobTimeoutException, UnixSignalDeathPenalty

        from netbox_nso_plugin import jobs

        job_class = job_class or jobs.PruneApplyLedgerJob
        queue = _rq_test_queue(self)
        with (
            patch("django_rq.get_queue", return_value=queue),
            patch.object(job_class, "tick", tick) if tick else contextlib.nullcontext(),
        ):
            first = job_class.enqueue(schedule_at=timezone.now() + timedelta(hours=1), interval=1440)
            started = time.monotonic()
            with UnixSignalDeathPenalty(1, JobTimeoutException, job_id=str(first.job_id)):
                job_class.handle(first)
            elapsed = time.monotonic() - started
        first.refresh_from_db()
        successors = Job.objects.filter(name=job_class.name, status="scheduled").exclude(pk=first.pk)
        return first, elapsed, successors.count()

    def test_an_overrun_inside_the_rotation_save_still_records_it_and_reschedules(self):
        from core.models import Job

        from netbox_nso_plugin import jobs

        def hold(pk, held):
            # An editor of this Job row, so that the run's own save waits until RQ's alarm fires.
            try:
                with transaction.atomic():
                    list(Job.objects.select_for_update().filter(pk=pk))
                    held.set()
                    time.sleep(2.5)
            finally:
                connection.close()

        def tick(job_self):
            job_self.rotation("fleet_rotation")["after_device_id"] = 7
            held = threading.Event()
            holder = threading.Thread(target=hold, args=(job_self.job.pk, held), daemon=True)
            holder.start()
            self.addCleanup(holder.join, 10)
            self.assertTrue(held.wait(10))

        job, elapsed, successors = self._overrun(tick, jobs.AuditRendererScopesJob)

        self.assertLess(elapsed, 5, "the run went on after RQ's timeout")
        self.assertEqual(job.status, "errored")
        self.assertEqual(successors, 1)
        self.assertEqual(job.data, {"fleet_rotation": {"after_device_id": 7}})

    def test_an_overrun_that_a_cleanup_error_replaces_still_records_the_rotation_and_reschedules(self):
        from django.db import OperationalError

        from netbox_nso_plugin import jobs

        def tick(job_self):
            job_self.rotation("fleet_rotation")["after_device_id"] = 7
            try:
                try:
                    time.sleep(3)
                finally:
                    # A cleanup statement on the connection the alarm interrupted fails in its place.
                    raise OperationalError("the connection is not usable")
            except Exception:  # noqa: BLE001
                pass
            # Work after the handler that caught the timeout.
            time.sleep(3)

        job, elapsed, successors = self._overrun(tick, jobs.AuditRendererScopesJob)

        self.assertLess(elapsed, 3, "the run went on after RQ's timeout")
        self.assertEqual(job.status, "errored")
        self.assertEqual(successors, 1)
        self.assertEqual(job.data, {"fleet_rotation": {"after_device_id": 7}})

    def test_a_lock_wait_bound_keeps_the_exception_in_flight_when_its_reset_fails(self):
        from netbox_nso_plugin import jobs

        class _InFlight(Exception):
            pass

        self.addCleanup(connection.close)
        with self.assertRaises(_InFlight):
            with jobs.lock_waits_until(time.monotonic() + 30):
                # The alarm can leave the connection unusable, so the reset statement fails.
                connection.connection.close()
                raise _InFlight

    def test_an_overrun_inside_a_statement_still_records_every_rotation(self):
        from netbox_nso_plugin import jobs

        def refresh(rows, *, snapshot, deadline, rotation):
            rotation["after_pk"] = 1
            return 0, 0

        def repair(rows, *, snapshot, deadline, rotation):
            rotation["lock_skipped"] = {"2": "2026-11-17T00:00:00+00:00"}
            return 0, 0

        def drain(*, deadline, compaction_deadline, rotation):
            rotation["lock_skipped"] = [[4, "vlan", "2026-11-17T00:00:00+00:00"]]
            return 0, 0

        def sweep(*, deadline, feed_deadline, rotation):
            rotation["after_pk"] = 3
            # RQ's alarm stops this statement in flight.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(6)")
            return 0, 0

        with (
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], {}, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", side_effect=refresh),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", side_effect=repair),
            patch("netbox_nso_plugin.drain.drain_intent_outbox", side_effect=drain),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", side_effect=sweep),
        ):
            job, elapsed, successors = self._overrun(None, jobs.RefreshDeviceSyncCacheJob)

        self.assertLess(elapsed, 4, "the run went on after RQ's timeout")
        self.assertEqual(job.status, "errored")
        self.assertEqual(successors, 1)
        self.assertEqual(
            job.data,
            {
                "refresh_rotation": {"after_pk": 1},
                "repair_rotation": {"lock_skipped": {"2": "2026-11-17T00:00:00+00:00"}},
                "outbox_rotation": {"lock_skipped": [[4, "vlan", "2026-11-17T00:00:00+00:00"]]},
                "settlement_rotation": {"after_pk": 3},
            },
        )

    def test_an_overrun_that_a_broad_handler_would_swallow_ends_at_the_timeout_and_reschedules(self):
        import http.server
        import socketserver

        import requests

        responding = threading.Event()

        class _Trickle(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                responding.set()
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.1)

            def log_message(self, *args):
                pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Trickle)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        # No proxy from the environment, and the real class: the suite blocks requests.Session.
        session = requests.sessions.Session()
        session.trust_env = False
        self.addCleanup(session.close)

        def tick(_self):
            # One slow response per key, each under a broad handler, as the drain and the sweep run.
            stop = time.monotonic() + 6
            while time.monotonic() < stop:
                with contextlib.suppress(Exception):
                    session.get(url, timeout=(1, 1))

        job, elapsed, successors = self._overrun(tick)

        self.assertTrue(responding.is_set(), "no request reached the trickling server")
        self.assertLess(elapsed, 4, "the run went on after RQ's timeout")
        self.assertEqual(job.status, "errored")
        self.assertEqual(successors, 1)

    def test_an_overrun_inside_a_lock_wait_leaves_netbox_a_working_connection_to_reschedule_on(self):
        from core.models import Job

        held = Job.objects.create(name="nso-test-held-row", job_id=uuid4())

        def tick(_self):
            stop = time.monotonic() + 6
            while time.monotonic() < stop:
                try:
                    with transaction.atomic():
                        list(Job.objects.select_for_update().filter(pk=held.pk))
                except Exception:  # noqa: BLE001
                    pass

        with HeldRowLock(held):
            job, elapsed, successors = self._overrun(tick)

        self.assertLess(elapsed, 4, "the run went on after RQ's timeout")
        self.assertEqual(job.status, "errored")
        self.assertEqual(successors, 1)
