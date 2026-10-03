# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Offboard lock contention rolls back, and old refusals cannot cross episodes."""

import threading

from dcim.models import Interface
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from utilities.exceptions import AbortRequest

from netbox_nso_plugin import delivery, drain, signals
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOIntentOutboxState,
    NSOInterfaceState,
)

from ._outbox_case import make_device, make_mgmt, wait_until_postgres_blocks
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestOffboardingOutboxRaces(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.devices = []
        self.managements = []
        self.overlays = []
        self.states = []
        with signals.suppress_intent_push():
            for index in range(3):
                device = make_device("offboard-race", index + 1)
                management = make_mgmt(device, "offboard-race", 4100 + index)
                interface = Interface.objects.create(device=device, name="Ethernet1", type="1000base-t")
                overlay = NSOInterfaceState.objects.create(
                    interface=interface, attribute="description", status="imported"
                )
                state = NSOIntentOutboxState.objects.create(
                    device=device,
                    scope="snmp",
                    last_error_code="validation_error",
                    last_error_at=timezone.now(),
                    attempts=2,
                    degraded_deletions=[{"reason": "legacy_mark_downgraded"}],
                )
                self.devices.append(device)
                self.managements.append(management)
                self.overlays.append(overlay)
                self.states.append(state)

    def _worker(self, work, *, release_events=()):
        failures = []

        def run():
            try:
                work()
            except BaseException as exc:  # noqa: BLE001 (asserted on the test thread)
                failures.append(exc)
            finally:
                connections.close_all()

        worker = threading.Thread(target=run, daemon=True)

        def finish():
            for event in release_events:
                event.set()
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive(), "the database worker survived cleanup")

        self.addCleanup(finish)
        worker.start()
        return worker, failures

    def _assert_finished(self, worker, failures):
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the database worker did not finish")
        self.assertEqual(failures, [])

    def _refusal(self):
        device = self.devices[0]
        NSOIntentOutboxEntry.objects.create(
            device=device, scope="static_route", batch_id=1, mark_and=True, mark_any=True
        )
        with self.assertRaises(drain.AuthorityPending) as caught:
            drain.claim(device.pk, "static_route", mode=delivery.MODE_STORE_ONLY)
        self.assertEqual(caught.exception.management_id, self.managements[0].pk)
        self.assertFalse(NSOIntentOutboxState.objects.filter(device=device, scope="static_route").exists())
        return caught.exception

    def test_busy_later_state_rolls_back_the_whole_queryset_and_retry_succeeds(self):
        locked = threading.Event()
        release = threading.Event()

        def hold_state():
            with transaction.atomic():
                NSOIntentOutboxState.objects.select_for_update().get(pk=self.states[1].pk)
                locked.set()
                if not release.wait(timeout=30):
                    raise AssertionError("the state holder was not released")

        holder, failures = self._worker(hold_state, release_events=(release,))
        self.assertTrue(locked.wait(timeout=30), "the target State was not locked")
        before = list(NSOIntentOutboxState.objects.order_by("pk").values())
        management_ids = [row.pk for row in self.managements]
        overlay_ids = [row.pk for row in self.overlays]
        reset_attempts = []

        def observe_reset(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith('UPDATE "netbox_nso_plugin_nsointentoutboxstate"'):
                reset_attempts.append(params[-1])
            return result

        try:
            with connection.execute_wrapper(observe_reset), self.assertRaises(AbortRequest) as caught:
                NSODeviceManagement.objects.filter(pk__in=management_ids[:2]).delete()
            self.assertIn("Intent delivery is busy", caught.exception.message)
            self.assertIn(self.states[0].pk, reset_attempts, "the earlier member was not reset before refusal")
            self.assertEqual(list(NSOIntentOutboxState.objects.order_by("pk").values()), before)
            self.assertEqual(
                list(NSODeviceManagement.objects.order_by("pk").values_list("pk", flat=True)), management_ids
            )
            self.assertEqual(list(NSOInterfaceState.objects.order_by("pk").values_list("pk", flat=True)), overlay_ids)
        finally:
            release.set()
            self._assert_finished(holder, failures)

        NSODeviceManagement.objects.filter(pk__in=management_ids[:2]).delete()
        self.assertEqual(list(NSODeviceManagement.objects.values_list("pk", flat=True)), management_ids[2:])
        self.assertEqual(list(NSOInterfaceState.objects.values_list("pk", flat=True)), overlay_ids[2:])
        for state in self.states[:2]:
            state.refresh_from_db()
            self.assertEqual(state.last_error_code, "")
            self.assertIsNone(state.last_error_at)
            self.assertEqual(state.degraded_deletions, [])
            self.assertEqual(state.attempts, 0)
        self.states[2].refresh_from_db()
        self.assertEqual(self.states[2].last_error_code, "validation_error")
        self.assertEqual(self.states[2].degraded_deletions, [{"reason": "legacy_mark_downgraded"}])

    def test_an_old_snapshot_aborts_without_removing_management_or_diagnostics(self):
        before = list(NSOIntentOutboxState.objects.order_by("pk").values())
        for isolation in ("REPEATABLE READ", "SERIALIZABLE"):
            with self.subTest(isolation=isolation), self.assertRaises(AbortRequest) as caught:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
                    NSODeviceManagement.objects.filter(pk=self.managements[0].pk).delete()
            self.assertIn("read committed", caught.exception.message)
            self.assertTrue(NSODeviceManagement.objects.filter(pk=self.managements[0].pk).exists())
            self.assertTrue(NSOInterfaceState.objects.filter(pk=self.overlays[0].pk).exists())
            self.assertEqual(list(NSOIntentOutboxState.objects.order_by("pk").values()), before)

    def test_late_missing_state_refusal_waits_for_the_deleting_management_then_is_superseded(self):
        refusal = self._refusal()
        device_id = self.devices[0].pk
        management_id = self.managements[0].pk
        discovered = threading.Event()
        release_delete = threading.Event()
        inserted = threading.Event()
        management_lock = threading.Event()
        recorder_connected = threading.Event()
        recorder_pids = []
        accepted = []

        def observe_delete(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if '"netbox_nso_plugin_nsointentoutboxstate"' in sql and "NOWAIT" in sql:
                discovered.set()
                if not release_delete.wait(timeout=30):
                    raise AssertionError("the offboard discovery was not released")
            return result

        def offboard():
            with connection.execute_wrapper(observe_delete):
                NSODeviceManagement.objects.filter(pk=management_id).delete()

        deleter, delete_failures = self._worker(offboard, release_events=(release_delete,))
        try:
            self.assertTrue(discovered.wait(timeout=30), "offboard did not discover State under its management lock")

            def observe_recorder(execute, sql, params, many, context):
                if '"netbox_nso_plugin_nsodevicemanagement"' in sql and "FOR UPDATE" in sql:
                    management_lock.set()
                result = execute(sql, params, many, context)
                if sql.startswith('INSERT INTO "netbox_nso_plugin_nsointentoutboxstate"'):
                    inserted.set()
                return result

            def record_refusal():
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '15s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    recorder_pids.append(cursor.fetchone()[0])
                recorder_connected.set()
                with connection.execute_wrapper(observe_recorder):
                    accepted.append(drain._record_claim_refusal(device_id, "static_route", refusal))

            recorder, record_failures = self._worker(record_refusal, release_events=(release_delete,))
            self.assertTrue(recorder_connected.wait(timeout=5))
            self.assertTrue(inserted.wait(timeout=5), "the recorder did not create the missing State")
            self.assertTrue(management_lock.wait(timeout=5), "the recorder did not lock its originating Management")
            wait_until_postgres_blocks(recorder_pids[0], "the refusal recorder", locktype="transactionid")
            self.assertFalse(NSOIntentOutboxState.objects.filter(device_id=device_id, scope="static_route").exists())
        finally:
            release_delete.set()
            self._assert_finished(deleter, delete_failures)
        self._assert_finished(recorder, record_failures)
        self.assertEqual(accepted, [False])
        self.assertFalse(NSODeviceManagement.objects.filter(pk=management_id).exists())
        state = NSOIntentOutboxState.objects.get(device_id=device_id, scope="static_route")
        self.assertEqual(state.last_error_code, "")
        self.assertIsNone(state.last_error_at)
        self.assertEqual(state.attempts, 0)

    def test_late_missing_state_claim_cannot_form_after_offboard_commits(self):
        device_id = self.devices[0].pk
        management_id = self.managements[0].pk
        missing_state = threading.Event()
        release_creator = threading.Event()
        discovered = threading.Event()
        release_delete = threading.Event()
        inserted = threading.Event()
        management_lock = threading.Event()
        claimant_pids = []
        claims = []
        paused = False

        def observe_claim(execute, sql, params, many, context):
            nonlocal paused
            if inserted.is_set() and '"netbox_nso_plugin_nsodevicemanagement"' in sql and "FOR UPDATE" in sql:
                management_lock.set()
            result = execute(sql, params, many, context)
            if '"netbox_nso_plugin_nsointentoutboxstate"' in sql and "FOR UPDATE" in sql and not paused:
                paused = True
                missing_state.set()
                if not release_creator.wait(timeout=30):
                    raise AssertionError("the missing-State claimant was not released")
            if sql.startswith('INSERT INTO "netbox_nso_plugin_nsointentoutboxstate"'):
                inserted.set()
            return result

        def claim():
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                claimant_pids.append(cursor.fetchone()[0])
            with connection.execute_wrapper(observe_claim):
                claims.append(drain.claim(device_id, "static_route", force=True))

        def observe_delete(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if '"netbox_nso_plugin_nsointentoutboxstate"' in sql and "NOWAIT" in sql:
                discovered.set()
                if not release_delete.wait(timeout=30):
                    raise AssertionError("the offboard discovery was not released")
            return result

        def offboard():
            with connection.execute_wrapper(observe_delete):
                NSODeviceManagement.objects.filter(pk=management_id).delete()

        releases = (release_creator, release_delete)
        claimant, claim_failures = self._worker(claim, release_events=releases)
        self.assertTrue(missing_state.wait(timeout=30), "the public claim did not reach missing-State discovery")
        deleter, delete_failures = self._worker(offboard, release_events=releases)
        try:
            self.assertTrue(discovered.wait(timeout=30))
            release_creator.set()
            self.assertTrue(inserted.wait(timeout=5))
            self.assertTrue(management_lock.wait(timeout=5))
            wait_until_postgres_blocks(claimant_pids[0], "the late claimant", locktype="transactionid")
            self.assertFalse(NSOIntentOutboxState.objects.filter(device_id=device_id, scope="static_route").exists())
        finally:
            release_creator.set()
            release_delete.set()
            self._assert_finished(deleter, delete_failures)
            self._assert_finished(claimant, claim_failures)
        self.assertEqual(claims, [None])
        self.assertFalse(NSODeviceManagement.objects.filter(pk=management_id).exists())
        state = NSOIntentOutboxState.objects.get(device_id=device_id, scope="static_route")
        self.assertIsNone(state.push_seq)
        self.assertIsNone(state.claim_payload)

    def test_replacement_between_refusal_stamp_and_report_receives_no_old_error(self):
        refusal = self._refusal()
        device = self.devices[0]
        management_id = self.managements[0].pk
        self.assertTrue(drain._record_claim_refusal(device.pk, "static_route", refusal))
        self.assertEqual(
            NSOIntentOutboxState.objects.get(device=device, scope="static_route").last_error_code,
            refusal.code,
        )
        NSODeviceManagement.objects.filter(pk=management_id).delete()
        with signals.suppress_intent_push():
            replacement = make_mgmt(device, "offboard-race-replacement", 4104)
        self.assertNotEqual(replacement.pk, management_id)

        drain._report_refusal(device.pk, "static_route", refusal, expected_management_id=management_id)

        replacement.refresh_from_db()
        self.assertNotIn("static_route", replacement.intent_push_errors or {})
        state = NSOIntentOutboxState.objects.get(device=device, scope="static_route")
        self.assertEqual(state.last_error_code, "")
        self.assertIsNone(state.last_error_at)
