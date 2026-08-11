# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1), the claim protocol: one fold, one render, one send.

Every pin here crosses a transaction boundary, so every case is a ``TransactionTestCase``:
the claim renders inside its own repeatable-read transaction and sends outside every
transaction, and a wrapping test transaction would hide exactly the boundary under test.

O1.6 bounds the work a burst of saves costs, O1.7 retires the rows a digest-equal claim
consumed, O1.8 keeps failed work pending without moving the acknowledged baseline, O1.9 and
O1.10 replay a crashed attempt at its own sequence, O1.11 refuses an outcome the sequence
has moved past, and O1.13 parks an unmanaged claim rather than abandoning it.
"""

from __future__ import annotations

from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase

from ._outbox_case import (
    ReceiptAdapter,
    entries,
    expire_claim,
    make_managed,
    own_route,
    own_vlan,
    state_of,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class _ClaimCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """One managed device, a recorded far side, and the entries an operator edit leaves."""

    tag = "cl"
    adapter_device_id = 7501

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def drain(self, scope="vlan", **kwargs):
        """Run the whole drain for one key against the recorded far side."""
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_key(self.device.pk, scope, **kwargs)

    def clear_entries(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()


class TestClaimFoldsEveryEntryOnce(_ClaimCase):
    """O1.6 (R8-B1): N saves cost one claim, and a batched pass truncates keys, not folds."""

    tag = "fold"
    adapter_device_id = 7502

    def test_n_saves_of_one_key_take_one_claim_one_render_and_one_send(self):
        from netbox_nso_plugin import delivery, drain

        states = [own_vlan(self.mgmt, 800 + index, self.tag) for index in range(3)]
        self.clear_entries()
        with without_commit_drain(), transaction.atomic():
            for state in states:
                state.save()

        assert len(entries(self.device, "vlan", unconsumed=True)) == 3

        with patch("netbox_nso_plugin.delivery.render", wraps=delivery.render) as render:
            outcome = self.drain()

        assert outcome == drain.SUCCEEDED
        assert render.call_count == 1
        assert len(self.adapter.requests) == 1
        assert entries(self.device, "vlan") == []

    def test_a_pass_truncates_keys_never_a_fold(self):
        from netbox_nso_plugin import drain

        others = [make_managed(f"{self.tag}{index}", 7510 + index, index=index) for index in range(2)]
        for index, (_device, mgmt) in enumerate(others):
            own_vlan(mgmt, 820 + index, f"{self.tag}{index}")
        own_vlan(self.mgmt, 830, self.tag)
        self.clear_entries()

        with without_commit_drain(), transaction.atomic():
            for _device, mgmt in others:
                mgmt.vlan_states.first().save()
            # One key carrying more entries than the whole pass may claim keys: the cap is
            # over keys, and a fold truncated at it would ship a body without its authority.
            for _ in range(drain.DRAIN_BATCH + 1):
                self.mgmt.vlan_states.first().save()

        config, session = self.adapter.patches()
        with patch.object(drain, "DRAIN_BATCH", 2), config, session:
            drained, failed = drain.drain_intent_outbox()

        assert (drained, failed) == (2, 0)
        mine = [request for request in self.adapter.requests if f"/devices/{self.adapter_device_id}/" in request["url"]]
        assert len(mine) == 1, "the fold covered every entry of the key, in one send"
        assert entries(self.device, "vlan", unconsumed=True) == []
        left = sorted(len(entries(device, "vlan", unconsumed=True)) for device, _mgmt in others)
        assert left == [0, 1], "the pass claims at most DRAIN_BATCH keys and leaves the rest"


class TestDigestEqualClaimRetiresItsRows(_ClaimCase):
    """O1.7 (R13-M1): the dropped claim has no outcome transaction, so it retires its own."""

    tag = "dig"
    adapter_device_id = 7503

    def test_an_unchanged_save_sends_nothing_retires_its_rows_and_the_gate_then_passes(self):
        from netbox_nso_plugin import drain

        state = own_vlan(self.mgmt, 840, self.tag)
        assert self.drain() == drain.SUCCEEDED
        sent = len(self.adapter.requests)

        with without_commit_drain(), transaction.atomic():
            state.save()
        assert len(entries(self.device, "vlan", unconsumed=True)) == 1

        assert self.drain() == drain.NOTHING

        assert len(self.adapter.requests) == sent
        assert entries(self.device, "vlan") == []
        assert drain.gate_blockers() == []

    def test_a_forced_call_takes_its_own_claim_and_the_digest_never_skips_it(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 841, self.tag)
        assert self.drain() == drain.SUCCEEDED
        first = state_of(self.device, "vlan").last_success_digest

        assert self.drain(force=True) == drain.SUCCEEDED

        state = state_of(self.device, "vlan")
        assert state.last_success_digest == first
        assert len(self.adapter.requests) == 2
        assert self.adapter.sequences[1] > self.adapter.sequences[0]


class TestFailureKeepsTheWorkAndTheBaseline(_ClaimCase):
    """O1.8: a failed attempt moves neither the authority nor the acknowledged baseline."""

    tag = "fail"
    adapter_device_id = 7504

    def test_a_failed_push_keeps_the_work_pending_and_counts_the_attempt(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 850, self.tag)
        self.adapter.fail_with = ConnectionError("adapter down")

        assert self.drain() == drain.FAILED

        state = state_of(self.device, "vlan")
        assert state.push_seq is not None, "the operation is replayed, so it stays unacknowledged"
        assert state.claimed_at is None, "the lease is released so the next tick may take it over"
        assert state.attempts == 1
        assert state.last_success_digest == ""
        assert [e.consumed_by_push_seq for e in entries(self.device, "vlan")] == [state.push_seq]

    def test_the_baseline_names_the_last_acknowledged_body(self):
        from netbox_nso_plugin import delivery, drain

        state = own_vlan(self.mgmt, 851, self.tag)
        self.adapter.fail_with = ConnectionError("adapter down")
        assert self.drain() == drain.FAILED
        failed_seq = state_of(self.device, "vlan").push_seq

        self.adapter.fail_with = None
        with without_commit_drain(), transaction.atomic():
            state.vlan.name = "cl-dig-renamed"
            state.vlan.save()
            state.save()

        assert self.drain() == drain.SUCCEEDED

        row = state_of(self.device, "vlan")
        assert row.push_seq is None and row.attempts == 0
        rendered = delivery.render("vlan", self.device.pk, self.adapter_device_id)
        assert row.last_success_digest == drain.request_digest(rendered.payload, mode="normal", deletions=[], mark=None)
        assert self.adapter.sequences[0] == failed_seq, "the failed operation is replayed, never reallocated"


class TestCrashedAttemptsReplayAtTheirOwnSequence(_ClaimCase):
    """O1.9, O1.10: the sequence names the operation, so a crash is resolvable either side."""

    tag = "crash"
    adapter_device_id = 7505

    def test_a_crash_between_claim_and_send_replays_the_same_sequence(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 860, self.tag)
        claimed = drain.claim(self.device.pk, "vlan")
        assert claimed is not None and claimed.push_seq is not None

        expire_claim(self.device, "vlan")
        assert self.drain() == drain.SUCCEEDED

        assert self.adapter.sequences == [claimed.push_seq]
        assert self.adapter.replays == 0
        assert entries(self.device, "vlan") == []

    def test_a_crash_after_the_send_replays_into_the_receipt_and_clears_the_authority(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.0/28", "198.51.100.1")
        with without_commit_drain():
            route.devices.remove(self.device)

        claimed = drain.claim(self.device.pk, "static_route")
        assert [d["route_id"] for d in claimed.deletions] == [route.pk]
        config, session = self.adapter.patches()
        with config, session:
            drain.send_claim(claimed)  # the far side committed; the outcome never ran

        assert state_of(self.device, "static_route").push_seq == claimed.push_seq
        expire_claim(self.device, "static_route")
        assert self.drain("static_route") == drain.SUCCEEDED

        assert self.adapter.sequences == [claimed.push_seq, claimed.push_seq]
        assert len(self.adapter.applied) == 1, "the replay must not apply the operation twice"
        assert self.adapter.replays == 1
        row = state_of(self.device, "static_route")
        assert (row.claim_deletions, row.queued_deletions, row.push_seq) == ([], [], None)
        assert entries(self.device, "static_route") == []


class TestOutcomeCasRefusesASupersededAttempt(_ClaimCase):
    """O1.11: an outcome may only settle the operation the key is still on."""

    tag = "cas"
    adapter_device_id = 7506

    def test_an_outcome_arriving_after_a_takeover_changes_nothing(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 870, self.tag)
        first = drain.claim(self.device.pk, "vlan")
        drain.abandon(first)  # the sequence is burned and the rows return unconsumed
        second = drain.claim(self.device.pk, "vlan")
        assert second.push_seq > first.push_seq

        assert drain.settle(first, {"count": 0}) == drain.SUPERSEDED
        drain.record_failure(first, ConnectionError("late failure"))

        row = state_of(self.device, "vlan")
        assert row.push_seq == second.push_seq
        assert row.attempts == 0 and row.last_success_digest == ""
        assert [e.consumed_by_push_seq for e in entries(self.device, "vlan")] == [second.push_seq]


class TestAForcedCallFormsItsOwnClaim(_ClaimCase):
    """Codex O1 F2 (§4.2): outstanding work resolves first, then THIS call's mode is owed."""

    tag = "mode"
    adapter_device_id = 7508

    def _stale_unacknowledged_claim(self, scope="vlan"):
        """Leave the key holding an operation the adapter never answered."""
        from netbox_nso_plugin import drain

        self.adapter.fail_with = ConnectionError("adapter down")
        assert self.drain(scope) == drain.FAILED
        self.adapter.fail_with = None
        return state_of(self.device, scope).push_seq

    def test_a_store_only_call_is_never_answered_by_a_normal_replay(self):
        from netbox_nso_plugin import delivery, drain

        own_vlan(self.mgmt, 875, self.tag)
        stale = self._stale_unacknowledged_claim()

        config, session = self.adapter.patches()
        with config, session:
            answer = drain.push_now(self.device.pk, "vlan", mode=delivery.MODE_STORE_ONLY, force=True)

        assert answer is not None
        assert self.adapter.requests[-1]["params"].get("store_only") == "true", (
            "the store-only request went out as a normal push, so the adapter enqueued the shrink removal"
        )
        assert self.adapter.sequences[0] == stale, "the outstanding operation is replayed at its own sequence"
        assert self.adapter.sequences[-1] > stale, "and this call then takes one of its own"

    def test_a_forced_call_sends_what_it_renders_now_not_the_stale_body(self):
        from netbox_nso_plugin import drain

        state = own_vlan(self.mgmt, 876, self.tag)
        self._stale_unacknowledged_claim()

        with without_commit_drain(), transaction.atomic():
            state.vlan.name = "cl-mode-renamed"
            state.vlan.save()

        config, session = self.adapter.patches()
        with config, session:
            assert drain.push_now(self.device.pk, "vlan", force=True) is not None

        assert "cl-mode-renamed" in str(self.adapter.requests[-1]["body"]), self.adapter.requests[-1]["body"]
        assert state_of(self.device, "vlan").push_seq is None, "both operations are resolved"

    def test_an_ordinary_drain_is_still_answered_by_the_replay_alone(self):
        """The re-entry belongs to a call whose mode differs; it must not fire on every takeover."""
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 877, self.tag)
        stale = self._stale_unacknowledged_claim()

        assert self.drain() == drain.SUCCEEDED

        assert self.adapter.sequences == [stale]


class TestUnmanagedClaimIsParked(_ClaimCase):
    """O1.13 (R11-m1): unmanaging is not a third abandon cause; the claim simply waits."""

    tag = "park"
    adapter_device_id = 7507

    def _claim_with_authority(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.16/28", "198.51.100.2")
        with without_commit_drain():
            route.devices.remove(self.device)
        claimed = drain.claim(self.device.pk, "static_route")
        assert claimed.deletions
        return claimed

    def test_unmanaging_after_the_claim_parks_it_rather_than_sending(self):
        from netbox_nso_plugin import drain

        claimed = self._claim_with_authority()
        with patch("netbox_nso_plugin.adapter_client.delete_device"):
            self.mgmt.delete()

        config, session = self.adapter.patches()
        with config, session:
            assert drain.send_claim(claimed) == drain.PARKED

        assert self.adapter.requests == []
        row = state_of(self.device, "static_route")
        assert row.push_seq == claimed.push_seq
        assert [d["route_id"] for d in row.claim_deletions] == [d["route_id"] for d in claimed.deletions]
        assert row.queued_deletions == []
        assert [e.consumed_by_push_seq for e in entries(self.device, "static_route")] == [claimed.push_seq] * len(
            entries(self.device, "static_route")
        )

    def test_unmanaging_between_send_and_outcome_still_records_the_outcome(self):
        from netbox_nso_plugin import drain

        claimed = self._claim_with_authority()
        config, session = self.adapter.patches()
        with config, session:
            response = drain.send_claim(claimed)
        with patch("netbox_nso_plugin.adapter_client.delete_device"):
            self.mgmt.delete()

        assert drain.settle(claimed, response) == drain.SUCCEEDED

        row = state_of(self.device, "static_route")
        assert (row.push_seq, row.claim_deletions, row.queued_deletions) == (None, [], [])
        assert entries(self.device, "static_route") == []
