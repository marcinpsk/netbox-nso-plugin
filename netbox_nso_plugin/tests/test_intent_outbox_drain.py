# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1), the drain pass: the tick's third call, and what it may see.

O1.23 pins the pass itself: it is the tick's third call, it re-queries its own candidates
after the link repair, it is bounded and candidate-filtered, and one key's failure isolates.
O1.24 pins commit visibility with a real second connection: an entry allocated first and
committed last is simply unconsumed, which is Appendix S's r3-B1 re-derived on this side.
O1.33 pins fairness: the attempt stamp goes on before anything can refuse, so a replayably
failing head rotates to the back instead of occupying every pass.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ._outbox_case import (
    ReceiptAdapter,
    enqueue,
    entries,
    make_managed,
    mirror_update,
    own_route,
    own_vlan,
    state_of,
    wait_until_postgres_blocks,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class _DrainCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()

    def managed(self, tag, adapter_device_id, index=1, vid=900):
        """A managed device owning one VLAN, which is what its render puts on the wire."""
        device, mgmt = make_managed(tag, adapter_device_id, index=index)
        own_vlan(mgmt, vid, tag)
        return device, mgmt

    def edit(self, mgmt):
        """One operator edit of the device's VLAN intent, which leaves one entry."""
        with without_commit_drain(), transaction.atomic():
            enqueue(mgmt.device, "vlan")

    def run_drain(self, **kwargs):
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_intent_outbox(**kwargs)

    def clear_entries(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()


class TestTheTickDrainsTheTail(_DrainCase):
    """O1.23 (R4-B3, R5-M3): the pass the synchronous chain leaves its tail to."""

    def test_the_drain_is_the_tick_s_third_call(self):
        from netbox_nso_plugin import jobs

        order = []

        def record(name, result):
            def _call(*args, **kwargs):
                order.append(name)
                return result

            return _call

        with (
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], {}, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", side_effect=record("refresh", (0, 0))),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", side_effect=record("repair", (0, 0))),
            patch("netbox_nso_plugin.drain.drain_intent_outbox", side_effect=record("drain", (0, 0))),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", side_effect=record("settle", (0, 0))),
        ):
            jobs.RefreshDeviceSyncCacheJob.run(None)  # self is unused by run()

        assert order == ["refresh", "repair", "drain", "settle"]

    def test_an_adapter_outage_still_runs_database_compaction(self):
        from netbox_nso_plugin import jobs

        with (
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], None, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
            patch("netbox_nso_plugin.drain.compact_intent_outbox") as compact,
            patch("netbox_nso_plugin.drain.drain_intent_outbox") as drain_all,
        ):
            jobs.RefreshDeviceSyncCacheJob.run(None)

        compact.assert_called_once_with()
        drain_all.assert_not_called()

    def test_an_adapter_outage_reports_compaction_and_settlement_durations_separately(self):
        from itertools import chain, repeat

        from netbox_nso_plugin import jobs

        with (
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], None, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
            patch("netbox_nso_plugin.drain.compact_intent_outbox"),
            patch.object(jobs, "time") as clock,
            patch.object(jobs.logger, "info") as log,
        ):
            clock.monotonic.side_effect = chain((10.0, 20.0), repeat(21.0))
            jobs.RefreshDeviceSyncCacheJob.run(None)

        # Rendered, not indexed: a value added to that one `logger.info` shifts every index,
        # and the test would then assert the wrong duration and still pass.
        message = log.call_args.args[0] % log.call_args.args[1:]
        assert "outbox drain 10.000s" in message, f"the outbox duration omitted outage compaction: {message}"
        assert "settlement sweep 1.000s" in message, f"the settlement duration included outage compaction: {message}"

    def test_a_mid_tick_quiesce_stops_without_recording_a_key_failure(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.deployment import DeploymentQuiesced

        with (
            patch.object(drain, "_compact_intent_outbox") as compact,
            patch.object(drain, "compact_intent_outbox") as guarded_compact,
            patch.object(drain, "drain_candidates", return_value=[(1, "vlan"), (2, "vlan")]),
            patch.object(drain, "drain_key", side_effect=DeploymentQuiesced("deployment started")) as drain_key,
            patch.object(drain, "_restamp_attempt") as restamp,
        ):
            result = drain._drain_intent_outbox()

        assert result == (0, 0)
        compact.assert_called_once_with(None)
        guarded_compact.assert_not_called()
        drain_key.assert_called_once_with(1, "vlan")
        restamp.assert_not_called()

    def test_the_tick_enters_the_deployment_gate_once_before_compaction(self):
        from netbox_nso_plugin import drain

        with (
            CaptureQueriesContext(connection) as queries,
            patch.object(drain, "_deployment_operation", wraps=drain._deployment_operation) as operation,
        ):
            assert drain.drain_intent_outbox() == (0, 0)

        admissions = [query["sql"] for query in queries if "pg_try_advisory_lock_shared" in query["sql"].lower()]
        assert len(admissions) == 1, admissions
        operation.assert_called_once_with("intent outbox tick")

    def test_a_queued_exclusive_transition_stops_a_real_drain_between_keys(self):
        from netbox_nso_plugin import deployment, drain, jobs

        first, first_mgmt = self.managed("gatefirst", 7607, index=1, vid=907)
        second, second_mgmt = self.managed("gatesecond", 7608, index=2, vid=908)
        self.edit(first_mgmt)
        self.edit(second_mgmt)

        waiter_pid = []
        waiter_errors = []
        thread_connections_closed = []
        # The drain catches broad Exception, and `self.failureException` is one: a `self.fail`
        # inside the patched send is logged and swallowed, and never reaches the runner.
        send_failures = []
        original_send = drain.send_claim
        started_waiter = False

        def request_quiesce():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    waiter_pid.append(cursor.fetchone()[0])
                deployment.quiesce()
            except BaseException as exc:
                waiter_errors.append(exc)
            finally:
                connection.close()
                thread_connections_closed.append(connection.connection is None)

        waiter = threading.Thread(target=request_quiesce)

        def send_after_exclusive_queues(*args, **kwargs):
            nonlocal started_waiter
            if not started_waiter:
                started_waiter = True
                waiter.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if waiter_pid:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                "WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                                [waiter_pid[0]],
                            )
                            if cursor.fetchone()[0]:
                                break
                    time.sleep(0.01)
                else:
                    send_failures.append("the exclusive transition did not queue during the first key")
            return original_send(*args, **kwargs)

        config, session = self.adapter.patches()
        try:
            with (
                config,
                session,
                patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], {}, {})),
                patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
                patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
                patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
                patch.object(drain, "send_claim", side_effect=send_after_exclusive_queues),
            ):
                jobs.RefreshDeviceSyncCacheJob.run(None)
            waiter.join(timeout=10)

            self.assertEqual(send_failures, [], send_failures[0] if send_failures else "")
            self.assertFalse(waiter.is_alive(), "the exclusive transition did not finish")
            self.assertEqual(waiter_errors, [])
            self.assertEqual(thread_connections_closed, [True])
            self.assertEqual(len(self.adapter.requests), 1)
            self.assertEqual(
                sorted(bool(entries(device, "vlan", unconsumed=True)) for device in (first, second)),
                [False, True],
            )
        finally:
            if waiter.is_alive():
                waiter.join(timeout=10)
            if deployment.is_quiesced():
                deployment.resume()

    def test_the_maintenance_job_holds_the_deployment_lock_for_every_pass(self):
        from netbox_nso_plugin import deployment, jobs

        snapshot_started = threading.Event()
        release_snapshot = threading.Event()
        quiesce_finished = threading.Event()
        waiter_pid = []
        errors = []
        thread_connections_closed = []

        def snapshot(rows):
            snapshot_started.set()
            release_snapshot.wait(10)
            return [], {}, {}

        def run_job():
            close_old_connections()
            try:
                jobs.RefreshDeviceSyncCacheJob.run(None)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()
                thread_connections_closed.append(connection.connection is None)

        def request_quiesce():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    waiter_pid.append(cursor.fetchone()[0])
                deployment.quiesce()
                quiesce_finished.set()
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()
                thread_connections_closed.append(connection.connection is None)

        worker = threading.Thread(target=run_job)
        waiter = threading.Thread(target=request_quiesce)
        queued = False
        try:
            with (
                patch("netbox_nso_plugin.sync_cache._snapshot", side_effect=snapshot),
                patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
                patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
                patch("netbox_nso_plugin.drain.drain_intent_outbox", return_value=(0, 0)),
                patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
            ):
                worker.start()
                self.assertTrue(snapshot_started.wait(5), "the maintenance job did not reach its snapshot")
                waiter.start()

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if waiter_pid:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                "WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                                [waiter_pid[0]],
                            )
                            queued = cursor.fetchone()[0]
                    if queued or quiesce_finished.is_set():
                        break
                    time.sleep(0.01)

                release_snapshot.set()
                worker.join(timeout=10)
                waiter.join(timeout=10)

            self.assertTrue(queued, "the maintenance job did not hold the deployment lock")
            self.assertFalse(worker.is_alive(), "the maintenance job did not finish")
            self.assertFalse(waiter.is_alive(), "the exclusive transition did not finish")
            self.assertEqual(errors, [])
            self.assertEqual(thread_connections_closed, [True, True])
        finally:
            release_snapshot.set()
            if worker.ident is not None:
                worker.join(timeout=10)
            if waiter.ident is not None:
                waiter.join(timeout=10)
            if deployment.is_quiesced():
                deployment.resume()

    def test_outage_compaction_respects_the_gate_and_the_normal_tick_still_runs(self):
        from netbox_nso_plugin import jobs
        from netbox_nso_plugin.deployment import quiesce, resume

        device, mgmt = self.managed("gatecompact", 7609, vid=909)
        self.edit(mgmt)
        before = [row.pk for row in entries(device, "vlan", unconsumed=True)]
        assert len(before) == 2

        quiesce()
        try:
            with (
                patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], None, {})),
                patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
                patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
            ):
                with self.assertLogs("netbox_nso_plugin.jobs", level="INFO") as logged:
                    jobs.RefreshDeviceSyncCacheJob.run(None)
            assert "paused for an intent deployment" in "\n".join(logged.output)
            assert [row.pk for row in entries(device, "vlan", unconsumed=True)] == before
        finally:
            resume()

        with (
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], None, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", return_value=(0, 0)),
        ):
            jobs.RefreshDeviceSyncCacheJob.run(None)
        assert [row.pk for row in entries(device, "vlan", unconsumed=True)] == [before[-1]]

        assert self.run_drain() == (1, 0)
        assert entries(device, "vlan", unconsumed=True) == []

    def test_the_tail_left_by_the_chain_drains_within_one_interval(self):
        from netbox_nso_plugin import drain

        assert drain.DRAIN_BATCH >= drain.DRAIN_CHAIN_MAX + 1
        keys = [
            self.managed(f"tail{index}", 7600 + index, index=index, vid=900 + index)
            for index in range(drain.DRAIN_CHAIN_MAX + 2)
        ]
        for _device, mgmt in keys:
            self.edit(mgmt)

        # One key drains synchronously, and its chain is over its own key alone: with nothing
        # further committing, only the tick can carry the rest.
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(keys[0][0].pk, "vlan") == drain.SUCCEEDED
        assert all(entries(device, "vlan", unconsumed=True) for device, _mgmt in keys[1:])

        drained, failed = self.run_drain()

        assert (drained, failed) == (len(keys) - 1, 0)
        assert [len(entries(device, "vlan", unconsumed=True)) for device, _mgmt in keys] == [0] * len(keys)

    def test_the_pass_reads_the_adapter_id_the_repair_wrote_not_the_stale_object(self):
        """O-P22: the repair writes the database and leaves the caller's row stale in two of
        its three branches, so a pass trusting those rows would skip one device and push to a
        dead id for the other."""
        from netbox_nso_plugin import jobs
        from netbox_nso_plugin.models import NSODeviceManagement

        reused, reused_mgmt = self.managed("reuse", 7610, index=1, vid=910)
        missing, missing_mgmt = self.managed("miss", 7611, index=2, vid=911)
        self.edit(reused_mgmt)
        self.edit(missing_mgmt)

        def repair(rows, snapshot=None):
            """The two branches that leave the caller holding something untrue."""
            mirror_update(NSODeviceManagement.objects.get(pk=reused_mgmt.pk), adapter_device_id=7710)
            mirror_update(NSODeviceManagement.objects.get(pk=missing_mgmt.pk), adapter_device_id=7711)
            for row in rows:
                if row.pk == reused_mgmt.pk:
                    row.adapter_device_id = None  # _REUSED drops the pointer on the object
                # _MISSING leaves the dead id standing on the object
            return 2, 2

        config, session = self.adapter.patches()
        with (
            config,
            session,
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], {}, {})),
            patch("netbox_nso_plugin.sync_cache.refresh_sync_caches", return_value=(0, 0)),
            patch("netbox_nso_plugin.sync_cache.reconcile_device_links", side_effect=repair),
            patch("netbox_nso_plugin.settlement.sweep_static_route_settlements", return_value=(0, 0)),
        ):
            jobs.RefreshDeviceSyncCacheJob.run(None)  # self is unused by run()

        sent = sorted(request["url"].split("/devices/")[1].split("/")[0] for request in self.adapter.requests)
        assert sent == ["7710", "7711"]
        assert entries(reused, "vlan", unconsumed=True) == []
        assert entries(missing, "vlan", unconsumed=True) == []

    def test_a_per_key_failure_does_not_abort_the_pass(self):
        broken, broken_mgmt = self.managed("brk", 7620, index=1, vid=920)
        healthy, healthy_mgmt = self.managed("hlt", 7621, index=2, vid=921)
        self.edit(broken_mgmt)
        self.edit(healthy_mgmt)
        self.adapter.fail_devices = {7620}

        drained, failed = self.run_drain()

        assert (drained, failed) == (1, 1)
        assert entries(healthy, "vlan", unconsumed=True) == []
        assert entries(broken, "vlan", unconsumed=False), "the failed work is replayed, not dropped"

    def test_candidates_are_the_full_work_pending_predicate(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        queued, queued_mgmt = self.managed("qd", 7630, index=1, vid=930)
        idle, _idle_mgmt = self.managed("idle", 7631, index=2, vid=931)
        self.clear_entries()
        NSOIntentOutboxState.objects.create(
            device=queued_mgmt.device,
            scope="static_route",
            queued_deletions=[{"route_id": 4242, "triples": [], "unverified": True}],
        )

        candidates = drain.drain_candidates()

        assert (queued.pk, "static_route") in candidates
        assert [key for key in candidates if key[0] == idle.pk] == []

    def test_candidate_queries_apply_the_requested_limit_in_the_database(self):
        from netbox_nso_plugin import drain

        for index in range(3):
            _device, mgmt = self.managed(f"bound{index}", 7632 + index, index=index + 3, vid=932 + index)
            self.edit(mgmt)

        with CaptureQueriesContext(connection) as queries:
            assert len(drain.drain_candidates(limit=1)) == 1

        candidate_queries = [
            query["sql"].upper()
            for query in queries
            if "NSOINTENTOUTBOXENTRY" in query["sql"].upper() or "NSOINTENTOUTBOXSTATE" in query["sql"].upper()
        ]
        assert candidate_queries
        assert all("LIMIT 1" in query for query in candidate_queries), candidate_queries


class TestOutOfOrderCommitVisibility(_DrainCase):
    """A-21/A-23: claims cannot pass an open mutation of their device and scope."""

    def test_a_claim_waits_for_an_open_revocation_and_folds_it(self):
        from netbox_nso_plugin import drain, outbox
        from netbox_nso_plugin.intent_state import content_mutation

        device, mgmt = make_managed("late", 7640)
        keeper = own_route(mgmt, "198.51.100.48/28", "198.51.100.4")
        leaving = own_route(mgmt, "198.51.100.64/28", "198.51.100.5")
        self.clear_entries()
        with without_commit_drain():
            leaving.devices.remove(device)
        removal_ids = [row.pk for row in entries(device, "static_route")]
        assert removal_ids, "the removal recorded the deletion this claim will carry"

        inserted = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def late_writer():
            """Hold the scope revision lock after recording a revocation."""
            try:
                with without_commit_drain(), content_mutation({(device.pk, "static_route")}):
                    outbox.enqueue(device.pk, "static_route", transitions=[outbox.revoke_transition(leaving.pk)])
                    inserted.set()
                    assert release.wait(timeout=30)
            except BaseException as exc:  # noqa: BLE001 (reported, not swallowed)
                errors.append(exc)
            finally:
                connection.close()

        writer = threading.Thread(target=late_writer)
        writer.start()
        # LIFO, so the release fires before the join: a failure below never hangs the suite.
        self.addCleanup(writer.join, 30)
        self.addCleanup(release.set)
        assert inserted.wait(timeout=30)

        claims: list = []
        claim_started = threading.Event()
        claimant_pid: list[int] = []

        def claim():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    claimant_pid.append(cursor.fetchone()[0])
                claim_started.set()
                claims.append(drain.claim(device.pk, "static_route"))
            except BaseException as exc:  # noqa: BLE001 (reported, not swallowed)
                errors.append(exc)
            finally:
                connection.close()

        claimant = threading.Thread(target=claim)
        claimant.start()
        self.addCleanup(claimant.join, 30)
        self.addCleanup(release.set)
        assert claim_started.wait(timeout=30), "the claimant never opened its database connection"
        wait_until_postgres_blocks(claimant_pid[0], "the claim", locktype="transactionid")

        release.set()
        writer.join(timeout=30)
        claimant.join(timeout=30)
        assert errors == []
        assert not writer.is_alive()
        assert not claimant.is_alive()

        [claimed] = claims
        assert claimed is not None
        assert entries(device, "static_route", unconsumed=True) == []
        assert claimed.deletions == [], "the committed revocation withdrew the removal authority"
        assert [row["route_id"] for row in claimed.payload] == [keeper.pk]

    def test_a_null_route_id_in_a_revoke_record_does_not_poison_the_key(self):
        from netbox_nso_plugin import drain, outbox
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        device, mgmt = make_managed("null-revoke", 7641)
        route = own_route(mgmt, "198.51.100.80/28", "198.51.100.6")
        self.clear_entries()
        with without_commit_drain():
            route.devices.remove(device)
        claimed = drain.claim(device.pk, "static_route")
        NSOIntentOutboxEntry.objects.create(
            device=device,
            scope="static_route",
            batch_id=1,
            transitions=[{"op": outbox.OP_REVOKE, "route_id": None}],
        )

        assert drain.revocation_hit(claimed) is False


class TestFairSelectionRotatesAFailingHead(_DrainCase):
    """O1.33 (R9-B1): isolation is not fairness; the stamp is what rotates the head."""

    def test_a_healthy_key_behind_failing_ones_is_claimed_within_the_bound(self):
        from netbox_nso_plugin import drain

        failing = [self.managed(f"fl{index}", 7650 + index, index=index, vid=950 + index) for index in range(2)]
        healthy, healthy_mgmt = self.managed("well", 7659, index=9, vid=959)
        for _device, mgmt in failing:
            self.edit(mgmt)
        self.edit(healthy_mgmt)
        self.adapter.fail_devices = {7650, 7651}

        with patch.object(drain, "DRAIN_BATCH", len(failing)):
            assert self.run_drain() == (0, 2)
            assert entries(healthy, "vlan", unconsumed=True), "the failing head took the whole first pass"
            second = self.run_drain()

        assert second[0] == 1
        assert entries(healthy, "vlan", unconsumed=True) == []
        assert all(state_of(device, "vlan").last_drain_attempted_at is not None for device, _mgmt in failing)

    def test_a_key_whose_claim_raises_before_it_commits_still_rotates(self):
        """codex O1 r2 F4: the in-transaction stamp rolls back with the claim that raised."""
        from netbox_nso_plugin import delivery, drain

        failing = [self.managed(f"rz{index}", 7670 + index, index=index, vid=970 + index) for index in range(2)]
        healthy, healthy_mgmt = self.managed("rzok", 7679, index=9, vid=979)
        for _device, mgmt in failing:
            self.edit(mgmt)
        self.edit(healthy_mgmt)

        real_render = delivery.render
        broken = {device.pk for device, _mgmt in failing}

        def render(key, device_id, adapter_device_id):
            if device_id in broken:
                raise RuntimeError(f"the {key} push rendered 0 bodies, expected exactly one")
            return real_render(key, device_id, adapter_device_id)

        with (
            patch.object(drain, "DRAIN_BATCH", len(failing)),
            patch("netbox_nso_plugin.delivery.render", side_effect=render),
        ):
            assert self.run_drain() == (0, 2)
            assert entries(healthy, "vlan", unconsumed=True), "the raising head took the whole first pass"
            second = self.run_drain()

        assert second[0] == 1, "the healthy key is claimed within the O1.33 bound"
        assert entries(healthy, "vlan", unconsumed=True) == []
        assert all(state_of(device, "vlan").last_drain_attempted_at is not None for device, _mgmt in failing)

    def test_a_never_attempted_key_is_ordered_ahead_of_a_stamped_one(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        stamped, stamped_mgmt = self.managed("stmp", 7660, index=1, vid=960)
        fresh, fresh_mgmt = self.managed("frsh", 7661, index=2, vid=961)
        self.edit(stamped_mgmt)
        self.edit(fresh_mgmt)
        NSOIntentOutboxState.objects.create(device=stamped, scope="vlan", last_drain_attempted_at=timezone.now())

        candidates = drain.drain_candidates()

        assert candidates.index((fresh.pk, "vlan")) < candidates.index((stamped.pk, "vlan"))
