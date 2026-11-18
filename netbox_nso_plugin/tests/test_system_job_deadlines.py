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
import threading
import time
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from dcim.models import Interface
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ._adapter_http import patch_matching_control_state
from ._outbox_case import (
    ReceiptAdapter,
    enqueue,
    make_managed,
    reset_renderer_audit_rotation,
    system_job,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def _add_interfaces(device, count, start=0):
    with without_commit_drain():
        Interface.objects.bulk_create(
            [Interface(device=device, name=f"et-0/0/{index}", type="1000base-t") for index in range(start, count)]
        )


class _HeldRowLock:
    """Hold FOR UPDATE on one row from a second connection, as an operator edit does."""

    def __init__(self, row):
        self.row = row
        self.release = threading.Event()
        self.held = threading.Event()
        self.pid = None
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    self.pid = cursor.fetchone()[0]
                list(type(self.row).objects.select_for_update(of=("self",)).filter(pk=self.row.pk).order_by())
                self.held.set()
                self.release.wait(30)
        finally:
            connection.close()

    def __enter__(self):
        self.thread.start()
        assert self.held.wait(10), "the holder never took the row lock"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(10)


def _short_lock_timeout(test_case):
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '2s'")

    def reset():
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = 0")

    test_case.addCleanup(reset)


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
        _short_lock_timeout(self)
        with _HeldRowLock(Interface.objects.filter(device=self.device).first()):
            attempt = signals._allocate_push_attempt(self.device.pk, "vlan")

        self.assertEqual(attempt, 1)
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_attempts, {"vlan": 1})

    def test_an_operator_lock_on_one_interface_does_not_block_the_outcome_record(self):
        from netbox_nso_plugin import signals
        from netbox_nso_plugin.adapter_client import AdapterError

        _add_interfaces(self.device, 3)
        attempt = signals._allocate_push_attempt(self.device.pk, "vlan")
        _short_lock_timeout(self)
        with _HeldRowLock(Interface.objects.filter(device=self.device).first()):
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


class TestTheSyncCacheJobStopsAtItsDeadline(_DeadlineCase):
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
                stack.enter_context(_HeldRowLock(row))
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
            _HeldRowLock(management),
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
            _HeldRowLock(management),
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
            _HeldRowLock(first),
            _HeldRowLock(second),
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
            _HeldRowLock(state),
        ):
            system_job(jobs.RefreshDeviceSyncCacheJob).run()

        called_at, run_deadline = swept[0]
        self.assertLess(called_at - (run_deadline - budget * jobs.SETTLEMENT_SWEEP_SHARE), 1)
        # Not compacted. The claim's pre-capture audit can queue a repair entry beside them.
        self.assertEqual(len(queued), 2)
        self.assertEqual(NSOIntentOutboxEntry.objects.filter(pk__in=queued).count(), 2)

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
            _HeldRowLock(state),
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
        from netbox_nso_plugin import jobs

        reset_renderer_audit_rotation(self)
        patch_matching_control_state(self)
        # Every pass starts past its deadline, so each run reaches exactly one device.
        patcher = patch.object(jobs, "SYSTEM_JOB_BUDGET_SECONDS", -1)
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
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch(
                "netbox_nso_plugin.settlement.settle_static_routes",
                side_effect=lambda pk, **_kwargs: polled.append(pk),
            ),
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
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=listing),
            patch(
                "netbox_nso_plugin.sync_cache.refresh_sync_cache",
                side_effect=lambda mgmt, _device: refreshed.append(mgmt.pk) or [],
            ),
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
        self.assertEqual(self._carried(jobs.AuditRendererScopesJob, "fleet_rotation"), {"after_device_id": ids[0]})


class TestSystemJobsSetTheirOwnTimeout(_CascadeFlushMixin, TransactionTestCase):
    def test_every_plugin_system_job_enqueues_with_the_plugin_timeout(self):
        import django_rq
        from netbox.registry import registry
        from rq import Queue
        from rq.job import Job as RQJob

        from netbox_nso_plugin import jobs

        queue = Queue(f"nso-test-job-timeout-{uuid4().hex}", connection=django_rq.get_queue("default").connection)
        self.addCleanup(queue.delete, delete_jobs=True)
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
        started = time.monotonic()
        with _HeldRowLock(held), self.assertRaises(OperationalError) as caught:
            with jobs.lock_waits_until(time.monotonic() + 1):
                Job.objects.filter(pk=held.pk).update(error="held")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(jobs.lock_wait_timed_out(caught.exception), caught.exception)
        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            self.assertEqual(cursor.fetchone()[0], "0")

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
        import django_rq
        from core.models import Job
        from rq import Queue
        from rq.job import Job as RQJob

        from netbox_nso_plugin import jobs

        queue = Queue(f"nso-test-job-timeout-{uuid4().hex}", connection=django_rq.get_queue("default").connection)
        self.addCleanup(queue.delete, delete_jobs=True)
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
        import django_rq
        from core.models import Job
        from rq import Queue
        from rq.timeouts import JobTimeoutException, UnixSignalDeathPenalty

        from netbox_nso_plugin import jobs

        job_class = job_class or jobs.PruneApplyLedgerJob
        queue = Queue(f"nso-test-job-timeout-{uuid4().hex}", connection=django_rq.get_queue("default").connection)
        self.addCleanup(queue.delete, delete_jobs=True)
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

        class _Trickle(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
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

        def tick(_self):
            # One slow response per key, each under a broad handler, as the drain and the sweep run.
            stop = time.monotonic() + 6
            while time.monotonic() < stop:
                with contextlib.suppress(Exception):
                    requests.get(url, timeout=(1, 1))

        job, elapsed, successors = self._overrun(tick)

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

        with _HeldRowLock(held):
            job, elapsed, successors = self._overrun(tick)

        self.assertLess(elapsed, 4, "the run went on after RQ's timeout")
        self.assertEqual(job.status, "errored")
        self.assertEqual(successors, 1)
