# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every plugin system job finishes inside its own RQ timeout (#1916).

RQ kills a run that passes its timeout, and NetBox then leaves the Job row ``running`` with
no successor, so one long run stops the job for good. Two things made runs long. The push
bookkeeping wrote the management row through the device's whole renderer footprint, which
locks and re-reads every interface of the device. And the passes had no shared deadline.
"""

from __future__ import annotations

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


class _HeldInterfaceLock:
    """Hold FOR UPDATE on one interface row from a second connection, as an operator edit does."""

    def __init__(self, interface):
        self.interface = interface
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
                list(Interface.objects.select_for_update(of=("self",)).filter(pk=self.interface.pk).order_by())
                self.held.set()
                self.release.wait(30)
        finally:
            connection.close()

    def __enter__(self):
        self.thread.start()
        assert self.held.wait(10), "the holder never took the interface lock"
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
        with _HeldInterfaceLock(Interface.objects.filter(device=self.device).first()):
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
        with _HeldInterfaceLock(Interface.objects.filter(device=self.device).first()):
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
            swept.append(kwargs["deadline"])
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
        # The sweep keeps its share of the budget: the drain stopped before the sweep's deadline.
        self.assertGreater(swept[0] - (started + self.BUDGET * (1 - jobs.SETTLEMENT_SWEEP_SHARE)), -0.5)


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
            patch("netbox_nso_plugin.settlement.settle_static_routes", side_effect=polled.append),
        ):
            for _run in range(4):
                system_job(jobs.RefreshDeviceSyncCacheJob).run()

        pks = sorted(row.pk for row in self.rows)
        self.assertEqual(polled, [*pks, pks[0]])
        self.assertEqual(self._carried(jobs.RefreshDeviceSyncCacheJob, "settlement_rotation"), {"after_pk": pks[0]})

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
        self.assertGreater(jobs.SYSTEM_JOB_TIMEOUT_SECONDS, jobs.SYSTEM_JOB_BUDGET_SECONDS)
        for job_class in plugin_jobs:
            with patch("django_rq.get_queue", return_value=queue):
                job = job_class.enqueue(schedule_at=timezone.now() + timedelta(hours=1), interval=5)
            rq_job = RQJob.fetch(str(job.job_id), connection=queue.connection)
            self.addCleanup(rq_job.delete)
            self.assertEqual(rq_job.timeout, jobs.SYSTEM_JOB_TIMEOUT_SECONDS, job_class.name)

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
