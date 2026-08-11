# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1): what an outcome may conclude, and what it must record.

O1.26 holds the single-home invariant at every step of a replay and separates the moot
world from the genuine one. O1.31 makes response validation a real PARTITION check, run
identically in the outcome path and in the restore's same-sequence arm. O1.32 pins the seven
stamping shapes. O1.36 retires the rows an acknowledged success consumed, without which the
deployment gate can never pass again. O1.27 keeps the degradation record durable across
every later success, so only an operator clears it.
"""

from __future__ import annotations

import threading

from django.db import transaction
from django.test import TransactionTestCase

from ._outbox_case import (
    ReceiptAdapter,
    as_per_object,
    entries,
    last_acked,
    make_managed,
    own_route,
    own_vlan,
    partition,
    state_of,
    triple,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class _OutcomeCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """A managed device, a recorded far side, and a route the operator can un-own."""

    tag = "out"
    adapter_device_id = 7700

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def clear_entries(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()

    def drain(self, scope="static_route", **kwargs):
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_key(self.device.pk, scope, **kwargs)

    def unown(self, route):
        """Remove the device from the route, which is what records the deletion."""
        with without_commit_drain():
            route.devices.remove(self.device)

    def reown(self, route):
        from netbox_nso_plugin.signals import _accept_static_route_for_device

        with without_commit_drain(), transaction.atomic():
            _accept_static_route_for_device(route, self.device)


class TestTheSingleHomeInvariantHoldsAcrossAReplay(_OutcomeCase):
    """O1.26(a) (R5-B4): a failed claim is replayed, so its authority is never duplicated."""

    tag = "home"
    adapter_device_id = 7701

    def test_a_failed_claim_keeps_its_authority_in_exactly_one_home(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.0/28", "198.51.100.1")
        self.clear_entries()
        self.unown(route)
        # Neither home exists yet: the authority is still a transition in the entry, and the
        # state row is created by the first drain-side operation.
        assert state_of(self.device, "static_route") is None

        self.adapter.fail_with = ConnectionError("adapter down")
        assert self.drain() == drain.FAILED
        self.assertHome(route, "claim")
        failed_seq = state_of(self.device, "static_route").push_seq

        self.adapter.fail_with = None
        assert self.drain() == drain.SUCCEEDED
        self.assertHome(route, None)
        assert self.adapter.sequences == [failed_seq], "replayed, never reallocated"

    def assertHome(self, route, expected):
        """The id is in *expected* home and no other, or in neither when *expected* is None."""
        state = state_of(self.device, "static_route")
        homes = {
            "queued": {int(r["route_id"]) for r in state.queued_deletions},
            "claim": {int(r["route_id"]) for r in state.claim_deletions},
        }
        assert not (homes["queued"] & homes["claim"]), "an id is in one home or the other, never both"
        assert {name for name, ids in homes.items() if route.pk in ids} == ({expected} if expected else set()), homes


class TestTheMootAndGenuineWorldsAreDeterministic(_OutcomeCase):
    """O1.26(c), (d) (R8-M1): the same history, decided by which request reaches the adapter."""

    tag = "moot"
    adapter_device_id = 7702

    def test_a_post_acknowledgement_cycle_folds_to_one_moot_operation(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.16/28", "198.51.100.2")
        self.clear_entries()
        self.unown(route)
        with as_per_object("static_route"):
            self.adapter._respond = lambda body: partition(executed=[route.pk])
            assert self.drain() == drain.SUCCEEDED
            assert state_of(self.device, "static_route").claim_deletions == []

            # One post-K operation: the re-own and the re-delete fold together, so the body
            # cannot re-create the route and the adapter has nothing left to remove.
            self.reown(route)
            self.unown(route)
            self.adapter._respond = lambda body: partition(moot=[route.pk])
            assert self.drain() == drain.SUCCEEDED

        state = state_of(self.device, "static_route")
        assert (state.queued_deletions, state.claim_deletions, state.revoked_ids) == ([], [], [])
        assert entries(self.device, "static_route") == []

    def test_a_re_own_that_reaches_the_adapter_first_makes_the_deletion_genuine(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.32/28", "198.51.100.3")
        self.clear_entries()
        self.unown(route)
        with as_per_object("static_route"):
            self.adapter._respond = lambda body: partition(executed=[route.pk])
            assert self.drain() == drain.SUCCEEDED

            self.reown(route)
            self.adapter._respond = lambda body: partition()
            assert self.drain() == drain.SUCCEEDED  # the re-creation lands on its own claim
            recreated = len(self.adapter.requests)

            self.unown(route)
            self.adapter._respond = lambda body: partition(executed=[route.pk])
            assert self.drain() == drain.SUCCEEDED

        assert len(self.adapter.requests) == recreated + 1
        assert self.adapter.requests[-1]["push_seq"] > self.adapter.requests[recreated - 1]["push_seq"]
        state = state_of(self.device, "static_route")
        assert (state.queued_deletions, state.claim_deletions) == ([], [])


class TestAResponseMustPartitionTheClaim(_OutcomeCase):
    """O1.31 (R8-M2): a union check settles contradictions; a partition check refuses them."""

    tag = "part"
    adapter_device_id = 7703

    def _claim_two(self):
        from netbox_nso_plugin import drain

        first = own_route(self.mgmt, "198.51.100.48/28", "198.51.100.4")
        second = own_route(self.mgmt, "198.51.100.64/28", "198.51.100.5")
        self.clear_entries()
        self.unown(first)
        self.unown(second)
        claimed = drain.claim(self.device.pk, "static_route")
        assert {r["route_id"] for r in claimed.deletions} == {first.pk, second.pk}
        return claimed, first, second

    def test_every_violation_is_refused_in_the_outcome_path(self):
        from netbox_nso_plugin import drain

        claimed, first, second = self._claim_two()
        cases = {
            "one id in two lists": partition(executed=[first.pk], degraded=[first.pk], moot=[second.pk]),
            "an id the claim never requested": partition(executed=[first.pk, second.pk, 999_999]),
            "a duplicate within one list": {
                **partition(executed=[first.pk, second.pk]),
                "deleted_executed_ids": [
                    first.pk,
                    first.pk,
                    second.pk,
                ],
            },
            "short coverage": partition(executed=[first.pk]),
        }
        with as_per_object("static_route"):
            for label, response in cases.items():
                with self.subTest(violation=label):
                    assert drain.settle(claimed, response) == drain.UNACKNOWLEDGED
                    state = state_of(self.device, "static_route")
                    assert state.push_seq == claimed.push_seq, "the operation is unresolved, not settled"
                    assert state.last_error_code == "ack_not_exact"
                    assert state.last_success_identity == ""

    def test_the_restore_same_sequence_arm_runs_the_identical_check(self):
        from netbox_nso_plugin import drain

        claimed, first, second = self._claim_two()
        state = state_of(self.device, "static_route")
        contradiction = partition(executed=[first.pk], degraded=[first.pk], moot=[second.pk])
        receipt = {
            "accepted_push_seq": claimed.push_seq,
            "request_digest": state.claim_wire_digest,
            "stored_response": contradiction,
        }
        with as_per_object("static_route"):
            assert drain.resolve_restored_claim(self.device.pk, "static_route", receipt) == drain.RESTORE_FAILED_CLOSED
            assert state_of(self.device, "static_route").push_seq == claimed.push_seq

            receipt["stored_response"] = partition(executed=[first.pk, second.pk])
            assert drain.resolve_restored_claim(self.device.pk, "static_route", receipt) == drain.RESTORE_SETTLED

        assert state_of(self.device, "static_route").push_seq is None

    def test_a_violation_is_reported_where_an_operator_reads_it(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement

        claimed, first, second = self._claim_two()
        with as_per_object("static_route"):
            drain.settle(claimed, partition(executed=[first.pk], degraded=[first.pk], moot=[second.pk]))

        errors = NSODeviceManagement.objects.get(pk=self.mgmt.pk).intent_push_errors or {}
        assert "static_route" in errors, errors
        assert "overlap" in errors["static_route"]["message"], errors["static_route"]

    def test_a_malformed_id_is_a_violation_rather_than_an_exception(self):
        """§4.4: coercing it raised out of the outcome, leaving the claim active forever."""
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement

        claimed, first, second = self._claim_two()
        malformed = partition(executed=[first.pk, "not-an-id"], moot=[second.pk])
        with as_per_object("static_route"):
            assert drain.settle(claimed, malformed) == drain.UNACKNOWLEDGED

        state = state_of(self.device, "static_route")
        assert state.push_seq == claimed.push_seq, "the operation is unresolved, not settled"
        assert state.claimed_at is None, "the lease is released, so the claim is recoverable"
        assert state.last_error_code == "ack_not_exact"
        errors = NSODeviceManagement.objects.get(pk=self.mgmt.pk).intent_push_errors or {}
        assert "not-an-id" in errors["static_route"]["message"], errors["static_route"]

    def test_a_claim_carrying_no_authority_refuses_a_malformed_id_too(self):
        """The boundary is the response, not the claim: a VLAN claim requests no id at all."""
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 903, self.tag)
        claimed = drain.claim(self.device.pk, "vlan")
        assert claimed is not None and claimed.deletions == []

        assert drain.settle(claimed, {"count": 1, "deleted_executed_ids": ["oops"]}) == drain.UNACKNOWLEDGED
        assert state_of(self.device, "vlan").last_error_code == "ack_not_exact"


class TestStampingFollowsTheAcknowledgedBody(_OutcomeCase):
    """O1.32 (R8-M3, R11-M1): seven shapes, and only the ones the adapter accepted stamp."""

    tag = "stamp"
    adapter_device_id = 7704

    def setUp(self):
        super().setUp()
        self.route = own_route(self.mgmt, "198.51.100.128/28", "198.51.100.8")
        self.clear_entries()
        self.mirror = triple("198.51.100.128/28", "198.51.100.8")

    def _touch(self):
        """One ordinary operator edit, so the key owes a send."""
        with without_commit_drain(), transaction.atomic():
            self.mgmt.static_route_states.get(static_route=self.route).save()

    def test_a_normal_success_stamps_the_body_it_sent(self):
        from netbox_nso_plugin import drain

        self._touch()
        assert self.drain() == drain.SUCCEEDED
        assert last_acked(self.mgmt, self.route) == self.mirror

    def test_a_store_only_success_stamps_too(self):
        from netbox_nso_plugin import delivery, drain

        self._touch()
        assert self.drain(mode=delivery.MODE_STORE_ONLY) == drain.SUCCEEDED
        assert last_acked(self.mgmt, self.route) == self.mirror

    def test_a_receipt_replay_stamps_what_the_stored_response_acknowledged(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOStaticRouteState

        self._touch()
        claimed = drain.claim(self.device.pk, "static_route")
        config, session = self.adapter.patches()
        with config, session:
            drain.send_claim(claimed)  # committed on the far side; the outcome never ran
        NSOStaticRouteState.objects.filter(management=self.mgmt).update(last_acked_triple=None)

        from ._outbox_case import expire_claim

        expire_claim(self.device, "static_route")
        assert self.drain() == drain.SUCCEEDED
        assert self.adapter.replays == 1
        assert last_acked(self.mgmt, self.route) == self.mirror

    def test_an_exact_acknowledgement_carrying_degraded_ids_still_stamps(self):
        from netbox_nso_plugin import drain

        other = own_route(self.mgmt, "198.51.100.144/28", "198.51.100.9")
        self.clear_entries()
        self.unown(other)
        with as_per_object("static_route"):
            self.adapter._respond = lambda body: partition(
                degraded=[other.pk], removed=[triple("10.0.0.0/8", "10.0.0.1")]
            )
            assert self.drain() == drain.SUCCEEDED
        assert last_acked(self.mgmt, self.route) == self.mirror

    def test_a_non_exact_acknowledgement_stamps_nothing(self):
        from netbox_nso_plugin import drain

        other = own_route(self.mgmt, "198.51.100.160/28", "198.51.100.10")
        self.clear_entries()
        self.unown(other)
        claimed = drain.claim(self.device.pk, "static_route")
        with as_per_object("static_route"):
            assert drain.settle(claimed, partition()) == drain.UNACKNOWLEDGED
        assert last_acked(self.mgmt, self.route) is None

    def test_a_late_acknowledgement_stamps_even_though_the_overlay_advanced(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOStaticRouteState

        self._touch()
        claimed = drain.claim(self.device.pk, "static_route")
        # The generation moves on while the answer is in flight. Reusing the expectation
        # hook's CAS here would skip the stamp, and the acknowledged intermediate triple is
        # exactly the one the lineage exists to remember.
        NSOStaticRouteState.objects.filter(management=self.mgmt).update(intent_generation=999_999)

        assert drain.settle(claimed, {"count": 1}) == drain.SUCCEEDED
        assert last_acked(self.mgmt, self.route) == self.mirror

    def test_a_backfill_only_success_never_stamps(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.models import NSOStaticRouteState

        # The adapter's content for this route has DRIFTED from the mirror; a backfill
        # adopts the id and accepts no content, so it may not claim the mirror was accepted.
        NSOStaticRouteState.objects.filter(management=self.mgmt).update(last_acked_triple=None)
        self._touch()

        assert self.drain(mode=delivery.MODE_BACKFILL_ONLY, force=True) == drain.SUCCEEDED
        assert self.adapter.requests[-1]["params"].get("backfill_only") == "true"
        assert last_acked(self.mgmt, self.route) is None
        assert entries(self.device, "static_route", unconsumed=True), "the real work is still owed"


class TestAnAcknowledgedSuccessRetiresItsRows(_OutcomeCase):
    """O1.36 (R12-M1): rows carrying a push_seq are unreachable by compaction, so retire them."""

    tag = "retire"
    adapter_device_id = 7705

    def test_the_row_count_returns_to_zero_and_the_gate_passes(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        state = own_vlan(self.mgmt, 900, self.tag)
        for index in range(4):
            with without_commit_drain(), transaction.atomic():
                state.vlan.name = f"cl-{self.tag}-{index}"
                state.vlan.save()
                state.save()
            assert self.drain("vlan") == drain.SUCCEEDED
            assert NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").count() == 0

        assert drain.gate_blockers(self.device.pk) == []


class TestTheGateRefusesAnUnacknowledgedOperation(_OutcomeCase):
    """§4.6, codex O1 r3 F4: a bare push_seq is an operation the far side may still hold."""

    tag = "gate"
    adapter_device_id = 7709

    def test_a_failed_claim_that_consumed_nothing_still_blocks_the_gate(self):
        from netbox_nso_plugin import delivery, drain

        own_vlan(self.mgmt, 908, self.tag)
        assert self.drain("vlan") == drain.SUCCEEDED
        assert entries(self.device, "vlan") == [], "the success retired every row it consumed"

        # A forced store-only claim consumes nothing, so its failure leaves the key with a
        # sequence to replay and not one other trace of the operation.
        self.adapter.fail_with = ConnectionError("adapter down")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "vlan", mode=delivery.MODE_STORE_ONLY, force=True) is None

        state = state_of(self.device, "vlan")
        assert state.push_seq is not None and state.claimed_at is None
        assert (state.claim_deletions, state.queued_deletions, state.revoked_ids) == ([], [], [])
        assert entries(self.device, "vlan") == []

        assert drain.gate_blockers(self.device.pk) == [f"{self.device.pk}/vlan: an unacknowledged operation"]


class TestTheDegradationRecordOutlivesEverySuccess(_OutcomeCase):
    """O1.27 (R5-M1): the transient error entry is popped by the next success; this is not."""

    tag = "degr"
    adapter_device_id = 7706

    def test_only_the_operator_acknowledgement_clears_it(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.176/28", "198.51.100.11")
        self.clear_entries()
        self.unown(route)
        residue = triple("192.0.2.0/24", "192.0.2.1")

        with as_per_object("static_route"):
            self.adapter._respond = lambda body: partition(degraded=[route.pk], removed=[residue])
            assert self.drain() == drain.SUCCEEDED

        recorded = state_of(self.device, "static_route").degraded_deletions
        assert [r["route_ids"] for r in recorded] == [[route.pk]]
        assert recorded[0]["triples"] == [residue], "the triples of the rows actually removed"
        assert recorded[0]["reason"] == drain.PRE_FENCE_DETACH

        # The degraded resend succeeds, and then an unrelated push succeeds too.
        self.adapter._respond = lambda body: partition()
        own_vlan(self.mgmt, 901, self.tag)
        assert self.drain("vlan") == drain.SUCCEEDED
        own_route(self.mgmt, "198.51.100.192/28", "198.51.100.12")
        assert self.drain() == drain.SUCCEEDED

        assert state_of(self.device, "static_route").degraded_deletions == recorded, "still named"

        assert drain.acknowledge_degraded_deletions(self.device.pk, "static_route") == [
            (self.device.pk, "static_route", recorded)
        ]
        assert state_of(self.device, "static_route").degraded_deletions == []

    def test_a_push_outcome_never_clears_it(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        own_vlan(self.mgmt, 902, self.tag)
        assert self.drain("vlan") == drain.SUCCEEDED
        NSOIntentOutboxState.objects.filter(device=self.device, scope="vlan").update(
            degraded_deletions=[{"route_ids": [1], "reason": drain.LEGACY_MARK_DOWNGRADED}]
        )
        state = self.mgmt.vlan_states.first()
        with without_commit_drain(), transaction.atomic():
            state.vlan.name = "cl-degr-renamed"
            state.vlan.save()
            state.save()

        assert self.drain("vlan") == drain.SUCCEEDED
        assert state_of(self.device, "vlan").degraded_deletions != []


class TestTheAcknowledgementClearsOnlyWhatItReported(_OutcomeCase):
    """O1.27, codex O1 r3 F5: the operator clears the records they were shown, and no others."""

    tag = "ackn"
    adapter_device_id = 7710

    def _record(self, route_id):
        from netbox_nso_plugin import drain

        return {
            "route_ids": [route_id],
            "triples": [],
            "at": "2026-08-11T00:00:00+00:00",
            "reason": drain.LEGACY_MARK_DOWNGRADED,
            "device": self.device.pk,
        }

    def _appender(self, record, done):
        """Record one degradation exactly as an outcome does: under the key's own lock."""
        from django.db import connection

        from netbox_nso_plugin import drain

        def work():
            try:
                with transaction.atomic():
                    state = drain._lock_state(self.device.pk, "vlan")
                    state.degraded_deletions = [*state.degraded_deletions, record]
                    state.save(update_fields=["degraded_deletions"])
                done.set()
            finally:
                connection.close()

        return threading.Thread(target=work)

    def test_a_degradation_recorded_while_the_acknowledgement_runs_is_not_cleared(self):
        from django.core.management import call_command

        from netbox_nso_plugin.models import NSOIntentOutboxState

        first, second = self._record(11), self._record(12)
        NSOIntentOutboxState.objects.create(device=self.device, scope="vlan", degraded_deletions=[first])

        recorded = threading.Event()
        appender = self._appender(second, recorded)

        class _Reporter:
            """Records the second degradation the moment the command reports the first."""

            def __init__(self):
                self.lines: list[str] = []

            def write(self, text):
                self.lines.append(text)
                if not appender.is_alive() and not recorded.is_set():
                    appender.start()
                    recorded.wait(timeout=10)

            def flush(self):
                """Django's OutputWrapper flushes what it wrote."""

        reporter = _Reporter()
        call_command("nso_acknowledge_degraded_deletions", device_id=self.device.pk, stdout=reporter)
        appender.join(timeout=30)
        assert recorded.is_set(), "the concurrent degradation never landed, so this pin proves nothing"

        assert any("route(s) [11]" in line for line in reporter.lines), reporter.lines
        assert not any("route(s) [12]" in line for line in reporter.lines), "it was never reported"
        assert state_of(self.device, "vlan").degraded_deletions == [second], (
            "a record the operator never saw may not be cleared by their acknowledgement"
        )


class TestStoreOnlyCarriesNoAuthority(_OutcomeCase):
    """§4.3(d), codex O1 F3: a store-only claim carries nothing and clears nothing."""

    tag = "store"
    adapter_device_id = 7708

    def test_a_store_only_claim_is_refused_while_a_deletion_is_pending(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.models import NSODeviceManagement

        route = own_route(self.mgmt, "198.51.100.240/28", "198.51.100.15")
        self.clear_entries()
        self.unown(route)
        pending = [row.pk for row in entries(self.device, "static_route", unconsumed=True)]
        assert pending, "the deletion is recorded and nothing has folded it yet"

        config, session = self.adapter.patches()
        with config, session:
            answer = drain.push_now(self.device.pk, "static_route", mode=delivery.MODE_STORE_ONLY, force=True)

        assert answer is None, "refused, so the caller reports it instead of believing it landed"
        assert self.adapter.requests == [], "a store-only request writes no tombstone, so it may not carry one"
        assert [row.pk for row in entries(self.device, "static_route", unconsumed=True)] == pending
        state = state_of(self.device, "static_route")
        assert state is None or (state.push_seq, state.claim_deletions, state.queued_deletions) == (None, [], [])

        errors = NSODeviceManagement.objects.get(pk=self.mgmt.pk).intent_push_errors or {}
        assert "static_route" in errors, errors

        claimed = drain.claim(self.device.pk, "static_route")
        assert [int(record["route_id"]) for record in claimed.deletions] == [route.pk], (
            "the store-only claim consumed the authority the ordinary claim has to deliver"
        )

    def test_a_store_only_claim_with_nothing_queued_still_sends(self):
        from netbox_nso_plugin import delivery, drain

        own_vlan(self.mgmt, 904, self.tag)
        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "vlan", mode=delivery.MODE_STORE_ONLY, force=True) is not None

        assert self.adapter.requests[-1]["params"].get("store_only") == "true"

    def test_an_ordinary_entry_outlives_a_store_only_send_and_is_delivered_after_it(self):
        """codex O1 r2 F1: a settled store-only success retired the delivery it never made."""
        from netbox_nso_plugin import delivery, drain

        own_vlan(self.mgmt, 905, self.tag)
        pending = [row.pk for row in entries(self.device, "vlan", unconsumed=True)]
        assert pending, "the edit is recorded and nothing has folded it yet"

        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "vlan", mode=delivery.MODE_STORE_ONLY, force=True) is not None

        assert [request["params"].get("store_only") for request in self.adapter.requests] == ["true"], (
            "one store-only send: a pass that retires nothing may not redrain either"
        )
        assert [row.pk for row in entries(self.device, "vlan", unconsumed=True)] == pending, (
            "store-only clears nothing, the ordinary entries included"
        )

        # The digest names the request MODE, so the store-only baseline can never drop the
        # ordinary claim behind it as digest-equal.
        assert self.drain("vlan") == drain.SUCCEEDED
        assert entries(self.device, "vlan", unconsumed=True) == []
        assert self.adapter.requests[-1]["params"].get("store_only") is None, "the delivery the resync did not make"

    def test_a_store_only_claim_is_refused_while_a_marked_deletion_is_pending(self):
        """codex O1 r3 F1: a query_flag deletion is authority, though its fold yields no ids."""
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.models import NSODeviceManagement

        own_vlan(self.mgmt, 906, self.tag)
        leaving = own_vlan(self.mgmt, 907, self.tag)
        assert self.drain("vlan") == drain.SUCCEEDED
        on_device = self.adapter.on_device[self.adapter_device_id]
        assert on_device == {("vlan_id", 906), ("vlan_id", 907)}
        self.clear_entries()

        with without_commit_drain(), transaction.atomic():
            leaving.delete()
        pending = [row.pk for row in entries(self.device, "vlan", unconsumed=True)]
        assert pending, "the marked deletion is recorded and nothing has folded it yet"
        sent = len(self.adapter.requests)

        config, session = self.adapter.patches()
        with config, session:
            answer = drain.push_now(self.device.pk, "vlan", mode=delivery.MODE_STORE_ONLY, force=True)

        assert answer is None, "refused: a store-only request writes no removal, so it may not carry one"
        assert len(self.adapter.requests) == sent, "and it never reached the wire"
        assert [row.pk for row in entries(self.device, "vlan", unconsumed=True)] == pending
        errors = NSODeviceManagement.objects.get(pk=self.mgmt.pk).intent_push_errors or {}
        assert "vlan" in errors, errors

        assert self.drain("vlan") == drain.SUCCEEDED
        assert self.adapter.requests[-1]["params"].get("delete_origin") == "true", "the marked retract still goes"
        assert self.adapter.on_device[self.adapter_device_id] == {("vlan_id", 906)}
        assert self.adapter.detached[self.adapter_device_id] == set()


class TestTheFenceWithholdsEverySendButTheBackfill(_OutcomeCase):
    """§4.3(c): a proven-no-effect 409 abandons, and only the backfill pass may send after it."""

    tag = "fence"
    adapter_device_id = 7707

    def test_a_fence_shut_rejection_abandons_rehomes_and_withholds(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError

        route = own_route(self.mgmt, "198.51.100.208/28", "198.51.100.13")
        self.clear_entries()
        self.unown(route)
        self.adapter.fail_with = AdapterError("fence shut", code="conflict", detail={"reason": "fence_shut"})

        with as_per_object("static_route"):
            assert self.drain() == drain.WITHHELD

        state = state_of(self.device, "static_route")
        assert [r["route_id"] for r in state.queued_deletions] == [route.pk]
        assert state.queued_deletions[0]["triples"], "rehomed as a WHOLE record, not a bare id"
        assert state.push_seq is None and state.fence_withheld_since is not None
        assert entries(self.device, "static_route", unconsumed=True), "the rows came back"

    def test_the_backfill_pass_opens_the_fence_and_the_deletion_then_executes(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError

        route = own_route(self.mgmt, "198.51.100.224/28", "198.51.100.14")
        self.clear_entries()
        self.unown(route)
        residue = triple("203.0.113.0/24", "203.0.113.1")
        self.adapter.fail_with = AdapterError("fence shut", code="conflict", detail={"reason": "fence_shut"})

        with as_per_object("static_route"):
            assert self.drain() == drain.WITHHELD
            burned = state_of(self.device, "static_route").last_error_code

            self.adapter.fail_with = None
            self.adapter._respond = lambda body: partition(removed=[residue])
            assert self.drain(chain=0) == drain.SUCCEEDED, "the withheld key drains its backfill claim"
            assert self.adapter.requests[-1]["params"].get("backfill_only") == "true"
            assert state_of(self.device, "static_route").fence_withheld_since is None

            self.adapter._respond = lambda body: partition(executed=[route.pk])
            assert self.drain() == drain.SUCCEEDED

        assert burned == "conflict"
        assert self.adapter.requests[-1]["params"].get("backfill_only") is None
        state = state_of(self.device, "static_route")
        assert (state.queued_deletions, state.claim_deletions) == ([], [])
        seqs = [r["push_seq"] for r in self.adapter.requests]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "no sequence is reused"

    def test_the_backfill_never_answers_for_the_push_the_caller_asked_for(self):
        """codex O1 r2 F2: the fence substitutes a backfill, and its success was the answer."""
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError

        route = own_route(self.mgmt, "198.51.100.32/28", "198.51.100.16")
        self.clear_entries()
        self.unown(route)
        self.adapter.fail_with = AdapterError("fence shut", code="conflict", detail={"reason": "fence_shut"})

        with as_per_object("static_route"):
            assert self.drain() == drain.WITHHELD

            def open_the_fence_then_break(body):
                """The backfill lands; the push the caller actually asked for does not."""
                self.adapter.fail_with = ConnectionError("the adapter went away")
                return partition()

            self.adapter.fail_with = None
            self.adapter._respond = open_the_fence_then_break
            config, session = self.adapter.patches()
            with config, session:
                answer = drain.push_now(self.device.pk, "static_route", force=True)

        assert answer is None, "the backfill is preparatory: the answer is the requested push's"
        state = state_of(self.device, "static_route")
        assert state.fence_withheld_since is None, "the backfill did open the fence"
        assert state.push_seq is not None and state.last_error_code == "ConnectionError", (
            "the deletion the caller asked to push is unacknowledged and replayed"
        )
        assert [int(r["route_id"]) for r in state.claim_deletions] == [route.pk]

    def test_a_deletion_committed_after_the_fence_shut_is_attributed_too(self):
        """codex O1 r4 F1: a backfill folds nothing, so that deletion lives only in an entry."""
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError

        withheld = own_route(self.mgmt, "198.51.100.240/28", "198.51.100.17")
        later = own_route(self.mgmt, "203.0.113.16/28", "198.51.100.18")
        self.clear_entries()
        self.unown(withheld)
        residue = triple("203.0.113.32/28", "203.0.113.2")
        self.adapter.fail_with = AdapterError("fence shut", code="conflict", detail={"reason": "fence_shut"})

        with as_per_object("static_route"):
            assert self.drain() == drain.WITHHELD

            # The operator un-owns a second route while the key is withheld. The backfill
            # claim consumes nothing and moves nothing, so this authority is in an
            # unconsumed entry and in neither home of the state row.
            self.unown(later)
            state = state_of(self.device, "static_route")
            assert [int(r["route_id"]) for r in state.queued_deletions] == [withheld.pk]
            assert entries(self.device, "static_route", unconsumed=True)

            self.adapter.fail_with = None
            self.adapter._respond = lambda body: partition(removed=[residue])
            assert self.drain(chain=0) == drain.SUCCEEDED

        recorded = state_of(self.device, "static_route").degraded_deletions
        assert [r["route_ids"] for r in recorded] == [sorted([withheld.pk, later.pk])], (
            "the backfill pruned the row behind the later deletion and recorded nothing, so the next "
            "ordinary claim folds it after its row is gone and the adapter moots it silently"
        )
        assert recorded[0]["triples"] == [residue]
        assert recorded[0]["reason"] == drain.PRE_FENCE_DETACH


class TestARestoredClaimSettlesAgainstTheReceiptsOwnDigest(_OutcomeCase):
    """codex O1 r4 F4 (§4.4): the receipt digests the BODY the adapter received, nothing else.

    A restored database holds an operation whose response was lost, and the same-sequence
    arm is the only thing that can resolve it without re-sending. It compares the receipt's
    digest against the one the claim persisted, so the two must be the same function of the
    same bytes: an identity over plugin-internal material could never match, and the restore
    failed closed for every key it was asked about.
    """

    tag = "rest"
    adapter_device_id = 7711

    def _lost_response(self, scope="vlan"):
        """Send one claim and drop its answer, which is the state a restore inherits."""
        from netbox_nso_plugin import drain

        claimed = drain.claim(self.device.pk, scope)
        config, session = self.adapter.patches()
        with config, session:
            drain.send_claim(claimed)
        return claimed

    def _receipt(self) -> dict:
        """What ``GET /api/v1/intent-receipts`` returns, built from the far side's own record."""
        [stored] = self.adapter.receipts.values()
        return {
            "accepted_push_seq": stored["push_seq"],
            "request_digest": stored["digest"],
            "stored_response": stored["response"],
        }

    def test_a_same_sequence_receipt_settles_the_restored_claim(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 910, self.tag)
        claimed = self._lost_response()
        receipt = self._receipt()
        assert receipt["accepted_push_seq"] == claimed.push_seq

        assert drain.resolve_restored_claim(self.device.pk, "vlan", receipt) == drain.RESTORE_SETTLED, (
            "the restore compared the adapter's body digest against plugin-internal material, "
            "so a matching receipt could never match and every restore failed closed"
        )

        state = state_of(self.device, "vlan")
        assert state.push_seq is None, "the operation is resolved, so the key can allocate again"
        assert entries(self.device, "vlan") == []

    def test_a_receipt_naming_another_body_still_fails_closed(self):
        """The arm is a real check: only the body the adapter accepted settles the claim."""
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 911, self.tag)
        claimed = self._lost_response()
        receipt = {**self._receipt(), "request_digest": "f" * 64}

        assert drain.resolve_restored_claim(self.device.pk, "vlan", receipt) == drain.RESTORE_FAILED_CLOSED
        assert state_of(self.device, "vlan").push_seq == claimed.push_seq
