# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Local capture refusal must not invert the existing drain lock order."""

import threading
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

from dcim.models import Device
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, connection, transaction
from django.test import TransactionTestCase
from ipam.models import ASN, RIR, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPRouter, BGPScope

from netbox_nso_plugin import drain, read_gate
from netbox_nso_plugin.apply_state import lock_device_intent_transaction
from netbox_nso_plugin.models import NSOBGPPeerState, NSOIntentOutboxState

from ._outbox_case import (
    ReceiptAdapter,
    entries,
    expire_claim,
    make_managed,
    own_vlan,
    state_of,
    wait_until_postgres_blocks,
    without_commit_drain,
)
from ._ownership_case import acquire_bgp_router, acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestDurableClaimRefusal(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        for transport in self.adapter.patches():
            transport.start()
            self.addCleanup(transport.stop)
        self.device, self.management = make_managed("claim-refusal", 1767)
        with without_commit_drain(), transaction.atomic():
            rir = RIR.objects.create(name="Placeholder private ASNs", slug="placeholder-private-asns", is_private=True)
            asn = ASN.objects.create(asn=64512, rir=rir)
            self.router = BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(Device),
                assigned_object_id=self.device.pk,
                asn=asn,
                router_id="198.18.0.1",
            )
            scope = BGPScope.objects.create(router=self.router)
            BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
            peer = BGPPeer.objects.create(
                scope=scope, peer=IPAddress.objects.create(address="198.18.0.2/32"), name=None
            )
            acquire_overlay(
                NSOBGPPeerState,
                management=self.management,
                bgp_peer=peer,
                asn_str="64512",
                vrf_name="",
                peer_address_str="198.18.0.2",
                status="accepted",
            )
        with self.assertRaises(drain._CaptureRefused) as caught:
            drain.claim(self.device.pk, "bgp")
        self.refusal = caught.exception

    def test_capture_refusal_preserves_work_when_intent_lock_is_busy(self):
        locked = threading.Event()
        release = threading.Event()
        before = [row.pk for row in entries(self.device, "bgp", unconsumed=True)]
        before_state = NSOIntentOutboxState.objects.filter(device=self.device, scope="bgp").values().first()
        self.management.refresh_from_db()
        errors = self.management.intent_push_errors

        def hold_intent():
            try:
                with transaction.atomic():
                    lock_device_intent_transaction(self.device.pk)
                    locked.set()
                    if not release.wait(15):
                        raise AssertionError("The test did not release the intent lock")
            finally:
                connection.close()

        def refuse():
            try:
                return drain._record_capture_refusal(self.device.pk, "bgp", self.refusal)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            holder = workers.submit(hold_intent)
            self.assertTrue(locked.wait(10))
            refusal = workers.submit(refuse)
            try:
                try:
                    result = refusal.result(timeout=3)
                except TimeoutError:
                    self.fail("Capture refusal waited for the device intent lock instead of preserving pending work")
                self.assertFalse(result)
            finally:
                release.set()
                holder.result(timeout=10)
                refusal.result(timeout=10)
        self.assertEqual([row.pk for row in entries(self.device, "bgp", unconsumed=True)], before)
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_errors, errors)
        self.assertEqual(
            NSOIntentOutboxState.objects.filter(device=self.device, scope="bgp").values().first(), before_state
        )

    def test_stale_capture_refusal_does_not_deadlock_successor_outcome(self):
        with without_commit_drain():
            acquire_bgp_router(self.management, self.router)
        claimed = drain.claim(self.device.pk, "bgp")
        self.assertIsNotNone(claimed)
        state_locked = threading.Event()
        continue_outcome = threading.Event()
        outcome_pid = Queue()
        refusal_pid = Queue()
        sql_errors = Queue()

        def observe_error(execute, sql, params, many, context):
            try:
                return execute(sql, params, many, context)
            except OperationalError as exc:
                sql_errors.put(getattr(exc.__cause__, "sqlstate", None))
                raise

        def outcome_barrier(execute, sql, params, many, context):
            result = observe_error(execute, sql, params, many, context)
            if not state_locked.is_set() and '"netbox_nso_plugin_nsointentoutboxstate"' in sql and "FOR UPDATE" in sql:
                state_locked.set()
                if not continue_outcome.wait(15):
                    raise AssertionError("The test did not release the outcome state lock")
            return result

        def backend_pid():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                return cursor.fetchone()[0]

        def settle():
            try:
                outcome_pid.put(backend_pid())
                with connection.execute_wrapper(outcome_barrier):
                    return drain.settle(claimed, {"deleted_executed_ids": [999999]})
            finally:
                connection.close()

        def refuse():
            try:
                refusal_pid.put(backend_pid())
                with connection.execute_wrapper(observe_error):
                    return drain._record_capture_refusal(self.device.pk, "bgp", self.refusal)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcome = workers.submit(settle)
            self.assertTrue(state_locked.wait(10))
            refusal = workers.submit(refuse)
            try:
                waiting_pid = refusal_pid.get(timeout=10)
                blocking_pid = outcome_pid.get(timeout=10)
                wait_until_postgres_blocks(waiting_pid, description="stale capture refusal")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [blocking_pid, waiting_pid])
                    self.assertTrue(cursor.fetchone()[0])
            finally:
                continue_outcome.set()
            self.assertEqual(outcome.result(timeout=15), drain.UNACKNOWLEDGED)
            self.assertFalse(refusal.result(timeout=15))
        observed = []
        while not sql_errors.empty():
            observed.append(sql_errors.get_nowait())
        self.assertNotIn("40P01", observed, "The refusal inverted the normal outcome's real PostgreSQL lock order")
        self.management.refresh_from_db()
        self.assertIn("unrequested ids", self.management.intent_push_errors["bgp"]["message"])
        self.assertEqual(state_of(self.device, "bgp").push_seq, claimed.push_seq)

    def test_capture_refusal_preserves_work_when_management_is_busy(self):
        own_vlan(self.management, 1767, "refusal-successor")
        claimed = drain.claim(self.device.pk, "vlan")
        self.assertIsNotNone(claimed)
        before = [row.pk for row in entries(self.device, "bgp", unconsumed=True)]
        before_state = NSOIntentOutboxState.objects.filter(device=self.device, scope="bgp").values().first()
        management_locked = threading.Event()
        continue_outcome = threading.Event()
        sql_errors = Queue()

        def observe_error(execute, sql, params, many, context):
            try:
                return execute(sql, params, many, context)
            except OperationalError as exc:
                sql_errors.put(getattr(exc.__cause__, "sqlstate", None))
                raise

        def management_barrier(execute, sql, params, many, context):
            result = observe_error(execute, sql, params, many, context)
            if (
                not management_locked.is_set()
                and '"netbox_nso_plugin_nsodevicemanagement"' in sql
                and "FOR UPDATE" in sql
            ):
                management_locked.set()
                if not continue_outcome.wait(15):
                    raise AssertionError("The test did not release the outcome management lock")
            return result

        def settle():
            try:
                with connection.execute_wrapper(management_barrier):
                    return drain.settle(claimed, {"deleted_executed_ids": [999999]})
            finally:
                connection.close()

        def refuse():
            try:
                with connection.execute_wrapper(observe_error):
                    return drain._record_capture_refusal(self.device.pk, "bgp", self.refusal)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcome = workers.submit(settle)
            self.assertTrue(management_locked.wait(10))
            refusal = workers.submit(refuse)
            try:
                try:
                    result = refusal.result(timeout=3)
                except TimeoutError:
                    self.fail("Capture refusal waited for management while holding the device intent lock")
                self.assertFalse(result)
            finally:
                continue_outcome.set()
                outcome.result(timeout=15)
                refusal.result(timeout=15)
            self.assertEqual(outcome.result(), drain.UNACKNOWLEDGED)
        observed = []
        while not sql_errors.empty():
            observed.append(sql_errors.get_nowait())
        self.assertNotIn("40P01", observed)
        self.assertEqual([row.pk for row in entries(self.device, "bgp", unconsumed=True)], before)
        self.assertEqual(
            NSOIntentOutboxState.objects.filter(device=self.device, scope="bgp").values().first(), before_state
        )
        self.management.refresh_from_db()
        self.assertNotIn("bgp", self.management.intent_push_errors)
        self.assertIn("unrequested ids", self.management.intent_push_errors["vlan"]["message"])
        self.assertEqual(state_of(self.device, "vlan").push_seq, claimed.push_seq)

    def _adopt_source_epoch(self, source_epoch):
        result = read_gate.gated_family_run(
            self.management,
            "bgp",
            {
                "outcome": "unavailable",
                "reason": "not_ready",
                "incarnation": "11111111-aaaa-4aaa-8aaa-111111111111",
                "incarnation_born": "2026-07-01T00:00:10Z",
                "source_epoch": source_epoch,
            },
            lambda: self.fail("an unavailable read must not run its body"),
            epoch=self.management.adapter_device_id,
        )
        self.assertEqual(result.disposition, read_gate.SKIPPED_UNAVAILABLE)
        self.management.refresh_from_db()

    def test_capture_refusal_after_a_takeover_abandon_is_recorded(self):
        with without_commit_drain():
            acquire_bgp_router(self.management, self.router)
        self._adopt_source_epoch(1)
        claimed = drain.claim(self.device.pk, "bgp")
        self.assertIsNotNone(claimed)
        self._adopt_source_epoch(2)
        with without_commit_drain():
            BGPPeer.objects.get(scope__router=self.router).delete()
        self.assertTrue(expire_claim(self.device, "bgp"))

        self.assertEqual(drain.drain_key(self.device.pk, "bgp"), drain.REJECTED)

        state = state_of(self.device, "bgp")
        self.assertIsNone(state.push_seq)
        self.assertIsNone(state.claimed_at)
        self.assertNotEqual(state.last_error_code, "")
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_errors["bgp"]["detail"]["reason"], "missing_bgp_peer")
