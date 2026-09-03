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

import requests
from django.db import transaction
from django.test import TransactionTestCase

from ._outbox_case import (
    ReceiptAdapter,
    enqueue,
    entries,
    expire_claim,
    make_managed,
    own_route,
    own_vlan,
    state_of,
    without_commit_drain,
)
from ._static_route_case import _unassign_and_retire
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
            for _state in states:
                enqueue(self.device, "vlan")

        assert len(entries(self.device, "vlan", unconsumed=True)) == 3

        with (
            patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes"),
            patch("netbox_nso_plugin.delivery.render", wraps=delivery.render) as render,
        ):
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
            for device, _mgmt in others:
                enqueue(device, "vlan")
            # One key carrying more entries than the whole pass may claim keys: the cap is
            # over keys, and a fold truncated at it would ship a body without its authority.
            for _ in range(drain.DRAIN_BATCH + 1):
                enqueue(self.device, "vlan")

        config, session = self.adapter.patches()
        with patch.object(drain, "DRAIN_BATCH", 2), config, session:
            drained, failed = drain.drain_intent_outbox()

        assert (drained, failed) == (2, 0)
        mine = [request for request in self.adapter.requests if f"/devices/{self.adapter_device_id}/" in request["url"]]
        assert len(mine) == 1, "the fold covered every entry of the key, in one send"
        assert entries(self.device, "vlan", unconsumed=True) == []
        left = sorted(len(entries(device, "vlan", unconsumed=True)) for device, _mgmt in others)
        assert left == [0, 1], "the pass claims at most DRAIN_BATCH keys and leaves the rest"


class TestRepairContributionsAreMarkingNeutral(_ClaimCase):
    tag = "repairmark"
    adapter_device_id = 7514

    def test_a_repair_cannot_strip_an_ordinary_deletion_mark(self):
        from netbox_nso_plugin import drain, outbox

        enqueue(self.device, "vlan", delete_origin=True)
        enqueue(self.device, "vlan", kind=outbox.CONTRIBUTION_KIND_REPAIR)

        claimed = drain.claim(self.device.pk, "vlan")

        assert claimed.mark is True
        assert claimed.mark_any is True

    def test_a_repair_only_claim_has_no_marking_partition(self):
        from netbox_nso_plugin import drain, outbox

        enqueue(
            self.device,
            "vlan",
            delete_origin=True,
            kind=outbox.CONTRIBUTION_KIND_REPAIR,
        )

        claimed = drain.claim(self.device.pk, "vlan")

        assert claimed.mark is None
        assert claimed.mark_any is False


class TestCoalescedRoutePolicyClaimPreservesSuccessHook(_ClaimCase):
    tag = "routepolicyhook"
    adapter_device_id = 7513

    def test_two_contributions_store_unsupported_members_from_the_claim_response(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push

        name = "claim-community"
        unsupported = "color:0:12."
        with transaction.atomic():
            community_list = CommunityList.objects.create(name=name)
            CommunityListEntry.objects.create(
                community_list=community_list,
                action="permit",
                community=Community.objects.create(community=unsupported),
            )
        state = NSORoutePolicyState(
            management=self.mgmt,
            family="community_list",
            object_name=name,
            content_type=ContentType.objects.get_for_model(CommunityList),
            object_id=community_list.pk,
            status="accepted",
        )
        with suppress_intent_push(), intent_transaction(footprint_for_instance(state)):
            state.save()

        enqueue(self.device, "route_policy")
        enqueue(self.device, "route_policy")
        assert len(entries(self.device, "route_policy", unconsumed=True)) == 2

        claimed = drain.claim(self.device.pk, "route_policy")
        assert claimed is not None
        self.adapter._respond = lambda _body: {
            "objects": [],
            "unsupported_members": {name: [unsupported]},
        }
        config, session = self.adapter.patches()
        with config, session:
            response = drain.send_claim(claimed)
        assert drain.settle(claimed, response) == drain.SUCCEEDED

        state.refresh_from_db()
        assert state.unsupported_members == [unsupported]


class TestDigestEqualClaimRetiresItsRows(_ClaimCase):
    """O1.7 (R13-M1): the dropped claim has no outcome transaction, so it retires its own."""

    tag = "dig"
    adapter_device_id = 7503

    def test_an_unchanged_save_sends_nothing_retires_its_rows_and_the_gate_then_passes(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 840, self.tag)
        assert self.drain() == drain.SUCCEEDED
        sent = len(self.adapter.requests)

        with without_commit_drain(), transaction.atomic():
            enqueue(self.device, "vlan")
        assert len(entries(self.device, "vlan", unconsumed=True)) == 1

        assert self.drain() == drain.NOTHING

        assert len(self.adapter.requests) == sent
        assert entries(self.device, "vlan") == []
        assert drain.gate_blockers() == []

    def test_a_forced_call_takes_its_own_claim_and_the_digest_never_skips_it(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 841, self.tag)
        assert self.drain() == drain.SUCCEEDED
        first = state_of(self.device, "vlan").last_success_identity

        assert self.drain(force=True) == drain.SUCCEEDED

        state = state_of(self.device, "vlan")
        assert state.last_success_identity == first
        assert len(self.adapter.requests) == 2
        assert self.adapter.sequences[1] > self.adapter.sequences[0]


class TestFailureKeepsTheWorkAndTheBaseline(_ClaimCase):
    """O1.8: a failed attempt moves neither the authority nor the acknowledged baseline."""

    tag = "fail"
    adapter_device_id = 7504

    def test_a_failed_push_keeps_the_work_pending_and_counts_the_attempt(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 850, self.tag)
        self.adapter.fail_with = requests.exceptions.ConnectionError("adapter down")

        assert self.drain() == drain.FAILED

        state = state_of(self.device, "vlan")
        assert state.push_seq is not None, "the operation is replayed, so it stays unacknowledged"
        assert state.claimed_at is None, "the lease is released so the next tick may take it over"
        assert state.attempts == 1
        assert state.last_success_identity == ""
        assert [e.consumed_by_push_seq for e in entries(self.device, "vlan")] == [state.push_seq]

    def test_the_baseline_names_the_last_acknowledged_body(self):
        from netbox_nso_plugin import delivery, drain

        state = own_vlan(self.mgmt, 851, self.tag)
        self.adapter.fail_with = requests.exceptions.ConnectionError("adapter down")
        assert self.drain() == drain.FAILED
        failed_seq = state_of(self.device, "vlan").push_seq

        self.adapter.fail_with = None
        from netbox_nso_plugin.vlan_reconciler import save_vlan_content

        with without_commit_drain(), transaction.atomic():
            state.vlan.name = "cl-dig-renamed"
            save_vlan_content(state.vlan, update_fields=("name",))

        assert self.drain() == drain.SUCCEEDED

        row = state_of(self.device, "vlan")
        assert row.push_seq is None and row.attempts == 0
        rendered = delivery.render("vlan", self.device.pk, self.adapter_device_id)
        assert row.last_success_identity == drain.request_identity(
            rendered.payload,
            mode="normal",
            marking_mode=delivery.MARKING_QUERY_FLAG,
            deletions=[],
            mark=None,
            epoch=drain.mapping_epoch(self.mgmt),
        )
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

    def test_a_claim_without_a_revision_is_rejected_as_corrupt(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        own_vlan(self.mgmt, 863, self.tag)
        claimed = drain.claim(self.device.pk, "vlan")
        NSOIntentOutboxState.objects.filter(device=self.device, scope="vlan").update(
            claim_revision=None,
            claimed_at=None,
        )

        with self.assertRaisesRegex(drain.ProtocolViolation, "durable intent revision"):
            drain.claim(self.device.pk, "vlan")

        self.assertEqual(state_of(self.device, "vlan").push_seq, claimed.push_seq)

    def test_a_crash_after_the_send_replays_into_the_receipt_and_clears_the_authority(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.0/28", "198.51.100.1")
        with without_commit_drain():
            _unassign_and_retire(route, self.device)

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

    def test_a_mapping_change_abandons_the_old_claim_before_any_replay(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement

        own_vlan(self.mgmt, 861, self.tag)
        claimed = drain.claim(self.device.pk, "vlan")
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(adapter_device_id=7599)
        expire_claim(self.device, "vlan")

        assert self.drain() == drain.SUCCEEDED
        assert len(self.adapter.requests) == 1
        assert "/devices/7599/" in self.adapter.requests[0]["url"]
        row = state_of(self.device, "vlan")
        assert row.push_seq is None
        assert self.adapter.sequences[-1] > claimed.push_seq


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
        assert row.attempts == 0 and row.last_success_identity == ""
        assert [e.consumed_by_push_seq for e in entries(self.device, "vlan")] == [second.push_seq]


class TestAForcedCallFormsItsOwnClaim(_ClaimCase):
    """Codex O1 F2 (§4.2): outstanding work resolves first, then THIS call's mode is owed."""

    tag = "mode"
    adapter_device_id = 7508

    def _stale_unacknowledged_claim(self, scope="vlan"):
        """Leave the key holding an operation the adapter never answered."""
        from netbox_nso_plugin import drain

        self.adapter.fail_with = requests.exceptions.ConnectionError("adapter down")
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

    def test_a_forced_replay_and_its_new_claim_share_one_send_deadline(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 878, self.tag)
        self._stale_unacknowledged_claim()
        deadlines = []

        def answer(_rendered, _payload, **kwargs):
            deadlines.append(kwargs["deadline"])
            return {"count": 1}

        ticks = iter((100, 100, 102, 105))
        with (
            patch("netbox_nso_plugin.delivery.send", new=answer),
            patch("netbox_nso_plugin.drain._send_clock", new=lambda: next(ticks)),
        ):
            outcome = drain.drain_key(self.device.pk, "vlan", force=True, deadline=10)

        assert outcome == drain.SUCCEEDED
        assert deadlines == [8, 5]

    def test_an_exhausted_chain_never_reports_a_preparatory_replay_as_this_call(self):
        from netbox_nso_plugin import delivery, drain

        own_vlan(self.mgmt, 879, self.tag)
        stale = self._stale_unacknowledged_claim()

        config, session = self.adapter.patches()
        with config, session:
            outcome, answer = drain._drain_once(
                self.device.pk,
                "vlan",
                mode=delivery.MODE_STORE_ONLY,
                force=True,
                chain=0,
            )

        assert (outcome, answer) == (drain.NOTHING, None)
        assert self.adapter.sequences == [stale], "only the preparatory replay reached the adapter"
        assert state_of(self.device, "vlan").push_seq is None

    def test_an_ordinary_drain_is_still_answered_by_the_replay_alone(self):
        """The re-entry belongs to a call whose mode differs; it must not fire on every takeover."""
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 877, self.tag)
        stale = self._stale_unacknowledged_claim()

        assert self.drain() == drain.SUCCEEDED

        assert self.adapter.sequences == [stale]

    def test_a_quiesced_latency_tail_does_not_reclassify_a_settled_success(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.deployment import DeploymentQuiesced

        own_vlan(self.mgmt, 880, self.tag)
        claimed = drain.claim(self.device.pk, "vlan")
        assert drain.settle(claimed, {"count": 1}) == drain.SUCCEEDED

        with (
            patch.object(drain, "_answered_other_work", return_value=False),
            patch.object(drain, "_pending", return_value=True),
            patch.object(drain, "_drain_once", side_effect=DeploymentQuiesced("deployment started")),
        ):
            continued = drain._after_success(
                claimed,
                mode=delivery.MODE_NORMAL,
                force=False,
                chain=1,
                deadline=None,
                deadline_at=None,
                chained=False,
            )

        assert continued is None


class TestUnmanagedClaimIsParked(_ClaimCase):
    """O1.13 (R11-m1): unmanaging is not a third abandon cause; the claim simply waits."""

    tag = "park"
    adapter_device_id = 7507

    def _claim_with_authority(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.16/28", "198.51.100.2")
        with without_commit_drain():
            _unassign_and_retire(route, self.device)
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
            assert drain.send_claim(claimed) is drain._PARKED_SEND

        assert self.adapter.requests == []
        row = state_of(self.device, "static_route")
        assert row.push_seq == claimed.push_seq
        assert [d["route_id"] for d in row.claim_deletions] == [d["route_id"] for d in claimed.deletions]
        assert row.queued_deletions == []
        parked = entries(self.device, "static_route")
        assert parked, "the parked claim kept the rows it consumed"
        assert [e.consumed_by_push_seq for e in parked] == [claimed.push_seq] * len(parked)

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


class TestControlOutcomesCannotCollideWithAdapterAnswers(_ClaimCase):
    tag = "answer"
    adapter_device_id = 7509

    def test_a_bare_parked_answer_is_settled_as_an_adapter_answer(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 862, self.tag)
        self.adapter._respond = lambda body: drain.PARKED

        assert self.drain() == drain.SUCCEEDED
        assert state_of(self.device, "vlan").push_seq is None


class TestTheBaselineIsBoundToTheAdapterMapping(_ClaimCase):
    """codex O1 r5 F3 (O-P22): a repaired link points the same body at a device that never got it.

    ``last_success_identity`` names a body the adapter acknowledged, and the link repair can
    move the management row from one adapter device to another (``_MOVED`` adopts the id it
    found, ``_MISSING`` re-onboards onto a fresh one). An identity blind to that mapping
    reads the next unchanged edit as already delivered and retires it without a request, so
    the device the plugin now points at never receives the intent it owns.
    """

    tag = "remap"
    adapter_device_id = 7505
    moved_device_id = 7506

    def test_an_unchanged_edit_sends_to_the_device_the_repair_moved_the_row_to(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement

        own_vlan(self.mgmt, 860, self.tag)
        assert self.drain() == drain.SUCCEEDED
        assert f"/devices/{self.adapter_device_id}/" in self.adapter.requests[-1]["url"]

        # The link repair's own write: our node turned up under a different adapter id.
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(adapter_device_id=self.moved_device_id)
        with without_commit_drain(), transaction.atomic():
            enqueue(self.device, "vlan")

        assert self.drain() == drain.SUCCEEDED, "the moved device has never been sent this intent"

        assert f"/devices/{self.moved_device_id}/" in self.adapter.requests[-1]["url"]
        assert entries(self.device, "vlan") == []

    def test_a_rebuilt_adapter_store_under_the_same_id_sends_too(self):
        """The mapping is the pair: the same id can name a store that holds nothing of ours."""
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement

        own_vlan(self.mgmt, 861, self.tag)
        assert self.drain() == drain.SUCCEEDED
        sent = len(self.adapter.requests)

        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(adapter_incarnation="cl-remap-rebuilt")
        with without_commit_drain(), transaction.atomic():
            enqueue(self.device, "vlan")

        assert self.drain() == drain.SUCCEEDED
        assert len(self.adapter.requests) == sent + 1


def _read_state(incarnation, born):
    """One wire ``read_state`` block (D3 shape), carrying no source epoch.

    A source epoch would fence the observation on its own ratchet and return before the
    incarnation pair is read at all, which is not the transition these pins are about.
    """
    return {
        "outcome": "present",
        "reason": None,
        "freshness": "fresh",
        "result": "replaced",
        "succeeded": True,
        "read_at": "2026-07-21T10:00:00Z",
        "attempt_id": 1,
        "incarnation": incarnation,
        "incarnation_born": born,
        "source_epoch": None,
        "payload_revision": 1,
    }


class TestTheBaselineSeesARebuiltStoreBeforeAnythingAdoptsIt(_ClaimCase):
    """codex O1 gate (O-P22, second arm): the epoch may not wait for the read gate to adopt.

    ``adapter_incarnation`` is written only where a gated read publication ADOPTS the new
    pair, and a store rebuilt under the same numeric device id changes nothing else the
    management row carries. An epoch read from the adopted field alone is therefore the dead
    store's for the whole adoption window: an unchanged save draining in it matches the
    acknowledged baseline, is retired without a request, and nothing re-enqueues it, so the
    new store never receives the intent it owns. The row already records the observed pair,
    and that record moves first.
    """

    tag = "window"
    adapter_device_id = 7507

    inc_a = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    born_a = "2026-07-01T00:00:10Z"
    inc_b = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    born_b = "2026-07-01T00:00:20Z"

    def setUp(self):
        super().setUp()
        from django.utils.dateparse import parse_datetime

        from netbox_nso_plugin.models import NSODeviceManagement

        # An adopted store to be rebuilt from: the tab observes nothing without one.
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(
            adapter_incarnation=self.inc_a, adapter_incarnation_born=parse_datetime(self.born_a)
        )
        self.mgmt.refresh_from_db()

    def _observe(self, incarnation, born):
        """Record the tab's own observation of a store the read gate has not adopted."""
        from netbox_nso_plugin.read_gate import observe_aggregate

        observe_aggregate(self.mgmt, {"vlan": _read_state(incarnation, born)}, epoch=self.adapter_device_id)
        self.mgmt.refresh_from_db()

    def test_an_unchanged_save_sends_to_the_store_the_tab_has_only_observed(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 862, self.tag)
        assert self.drain() == drain.SUCCEEDED
        sent = len(self.adapter.requests)

        self._observe(self.inc_b, self.born_b)
        assert self.mgmt.adapter_incarnation == self.inc_a, "the publication has not adopted the rebuilt store"
        assert self.mgmt.reset_pending_incarnation == self.inc_b

        with without_commit_drain(), transaction.atomic():
            enqueue(self.device, "vlan")

        assert self.drain() == drain.SUCCEEDED, "the rebuilt store has never been sent this intent"
        assert len(self.adapter.requests) == sent + 1
        assert entries(self.device, "vlan") == []

    def test_a_pair_the_gate_may_never_adopt_still_moves_the_epoch(self):
        """Equal born, different UUID: a durable conflict the gate refuses to adopt, for good.

        This is the case an adoption-gated epoch can never reach: the store is provably not
        the one that acknowledged the baseline, and the field that would say so never moves.
        """
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 863, self.tag)
        assert self.drain() == drain.SUCCEEDED
        sent = len(self.adapter.requests)

        self._observe(self.inc_b, self.born_a)
        assert self.mgmt.adapter_incarnation == self.inc_a, "an ambiguous pair is never adopted"
        assert self.mgmt.reset_conflict_born is not None
        assert self.mgmt.reset_pending_incarnation == self.inc_b

        with without_commit_drain(), transaction.atomic():
            enqueue(self.device, "vlan")

        assert self.drain() == drain.SUCCEEDED
        assert len(self.adapter.requests) == sent + 1


class TestTheCapturedSequenceNamesTheCallersOwnClaim(_ClaimCase):
    """§4.2's selector identifies the claim the caller settled, not a later chained one."""

    tag = "cap"
    adapter_device_id = 7509

    def test_a_chained_normal_drain_does_not_overwrite_the_captured_sequence(self):
        from netbox_nso_plugin import delivery, drain

        own_vlan(self.mgmt, 881, self.tag)

        # A save that lands while the first claim is in flight leaves work pending, which is
        # what makes the MODE_NORMAL chain run a second successful pass.
        real_send = delivery.send
        appended = []

        def send_then_append(*args, **kwargs):
            answer = real_send(*args, **kwargs)
            if not appended:
                appended.append(True)
                # A real second operation: the render differs, so the chained pass is not
                # dropped as digest-equal against the acknowledged baseline.
                with without_commit_drain():
                    own_vlan(self.mgmt, 882, self.tag)
            return answer

        config, session = self.adapter.patches()
        with config, session, patch("netbox_nso_plugin.delivery.send", new=send_then_append):
            with drain.capture_successful_pushes() as pushed:
                outcome = drain.drain_key(self.device.pk, "vlan")

        assert outcome == drain.SUCCEEDED
        assert len(self.adapter.sequences) == 2, "the chain ran a second pass"
        assert pushed["vlan"].push_seq == self.adapter.sequences[0], (
            "the selector must name the caller's own claim, not the chained one"
        )

    def test_a_second_call_in_one_capture_still_names_its_own_later_claim(self):
        """Apply captures across several calls, and each one re-settles the scope for real."""
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 883, self.tag)

        config, session = self.adapter.patches()
        with config, session:
            with drain.capture_successful_pushes() as pushed:
                assert drain.drain_key(self.device.pk, "vlan") == drain.SUCCEEDED
                first = pushed["vlan"].push_seq
                with without_commit_drain():
                    own_vlan(self.mgmt, 884, self.tag)
                assert drain.drain_key(self.device.pk, "vlan") == drain.SUCCEEDED

        assert first == self.adapter.sequences[0]
        assert pushed["vlan"].push_seq == self.adapter.sequences[-1], (
            "a caller-owned re-settle supersedes the sequence the earlier call named"
        )


class TestARepairCommittedBeforeTheSnapshotIsClaimedAtItsOwnRevision(_ClaimCase):
    """P4 M16 — the claim's own pre-capture audit is not the only repair it can meet.

    Every other case in the outbox suites patches ``audit_renderer_scopes`` out, so the one
    interleaving the audit exists for was untested: another connection repairs the same key
    AFTER this claim's audit returned and BEFORE its repeatable-read transaction takes its
    snapshot. The claim must fold that repair's contribution and bracket its send with the
    revision the repair left, never the one it read a moment earlier.
    """

    tag = "auditrace"
    adapter_device_id = 7520

    def _bypass_the_writer_then_repair(self):
        """A foreign rename, then the audit that finds it: one committed repair."""
        from ipam.models import VLAN

        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        VLAN.objects.filter(pk=self.state.vlan_id).update(name="audit-race-renamed")
        audit_renderer_scopes(self.device.pk, ("vlan",), trigger="foreign-cadence")

    def test_a_repair_landing_between_the_audit_and_the_snapshot_is_folded(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.models import NSOIntentRevision

        from ._outbox_case import in_thread

        self.state = own_vlan(self.mgmt, 861, self.tag)
        assert self.drain() == drain.SUCCEEDED  # trusted baseline, so the claim's own audit is a no-op
        self.clear_entries()
        with without_commit_drain(), transaction.atomic():
            enqueue(self.device, "vlan")

        before = []
        after_audit = drain._claim_after_audit

        def repair_then_claim(device_id, scope, **kwargs):
            if not before:
                before.append(NSOIntentRevision.objects.get(device=self.device, scope="vlan").revision)
                in_thread(self._bypass_the_writer_then_repair)
            return after_audit(device_id, scope, **kwargs)

        with patch("netbox_nso_plugin.drain._claim_after_audit", repair_then_claim):
            claimed = drain.claim(self.device.pk, "vlan")

        assert claimed is not None
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        assert revision.revision > before[0], "the foreign repair advanced the durable revision"
        assert claimed.revision == revision.revision, "the claim bracketed a revision the repair superseded"
        assert delivery.canonical_fingerprint(claimed.payload) == revision.verified_fingerprint
        assert [item["name"] for item in claimed.payload] == ["audit-race-renamed"]
        assert entries(self.device, "vlan", unconsumed=True) == [], "the repair contribution folded into the claim"
