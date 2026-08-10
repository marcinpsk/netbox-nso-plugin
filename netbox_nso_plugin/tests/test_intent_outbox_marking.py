# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1): one fold, one send, one flag, and what the device ends up with.

O1.5 and O1.14 are about the DEVICE, not about which request went out. A full-replace push
retracts what it omits when it is marked and detaches it when it is not, so a fold that
gets the flag wrong either destroys config nobody authorized deleting or strands a genuine
deletion. The rule reproduced here is today's: fold everything, AND the legacy marks, one
send. Improve the attribution, never the device outcome.

The AND is what makes both mark orders safe, so the two orders are separate arms: an
unmarked shrink followed by a marked deletion, and the reverse. Both must detach.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase

from ._outbox_case import (
    PUT_STATIC,
    PUT_VLAN,
    ReceiptAdapter,
    as_per_object,
    entries,
    in_thread,
    make_managed,
    own_route,
    own_vlan,
    state_of,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def vlan_member(vid):
    """How the VLAN wire names one object, which is what the device carries."""
    return ("vlan_id", vid)


class _MarkCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """A managed device whose VLAN intent is already on the recorded device."""

    tag = "mark"
    adapter_device_id = 7700

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def drain(self, scope="vlan", **kwargs):
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_key(self.device.pk, scope, **kwargs)

    def clear_entries(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()

    def own(self, *vids):
        """Own each VLAN and land it on the device, so a later shrink has something to lose."""
        states = {vid: own_vlan(self.mgmt, vid, self.tag) for vid in vids}
        assert self.drain() in {"succeeded", "nothing"}
        assert self.adapter.on_device[self.adapter_device_id] == {vlan_member(vid) for vid in vids}
        self.clear_entries()
        self.adapter.requests.clear()
        self.adapter.applied.clear()
        return states

    def unown(self, state):
        """An unmarked shrink: the operator hands the object back, the device keeps it."""
        with patch(PUT_VLAN), transaction.atomic():
            state.status = "imported"
            state.save()

    def delete_overlay(self, state):
        """A marked shrink: the object is destroyed in NetBox, so the device may lose it."""
        with patch(PUT_VLAN), transaction.atomic():
            state.delete()

    def sent(self):
        """The requests this test's device received, in order."""
        return [r for r in self.adapter.requests if f"/devices/{self.adapter_device_id}/" in r["url"]]

    def marks(self):
        return [request["params"].get("delete_origin") for request in self.sent()]


class TestOneTransactionOneMarkedPush(_MarkCase):
    """O1.5(a): two marked contributors in one transaction cost one marked send."""

    tag = "onetx"
    adapter_device_id = 7701

    def test_marked_and_marked_in_one_transaction_send_once_and_retract(self):
        from netbox_nso_plugin import drain

        states = self.own(801, 802, 803)
        with patch(PUT_VLAN), transaction.atomic():
            states[801].delete()
            states[802].delete()

        assert len(entries(self.device, "vlan", unconsumed=True)) == 2
        assert self.drain() == drain.SUCCEEDED

        assert self.marks() == ["true"], "one send, carrying the flag every contributor set"
        assert self.adapter.on_device[self.adapter_device_id] == {vlan_member(803)}
        assert self.adapter.detached[self.adapter_device_id] == set()


class TestASecondMarkedDeletionIsStillMarked(_MarkCase):
    """O1.5(b): a failed marked deletion never downgrades the next one."""

    tag = "second"
    adapter_device_id = 7702

    def test_the_second_independent_marked_deletion_ships_marked(self):
        from netbox_nso_plugin import drain

        states = self.own(811, 812, 813)
        self.adapter.fail_with = ConnectionError("adapter down")
        self.delete_overlay(states[811])
        assert self.drain() == drain.FAILED
        held = state_of(self.device, "vlan").push_seq

        self.adapter.fail_with = None
        self.delete_overlay(states[812])

        assert self.drain() == drain.SUCCEEDED

        assert self.marks() == ["true", "true"], "the replay and the new claim are both marked"
        assert self.adapter.sequences[0] == held, "the failed operation is replayed, never reallocated"
        assert self.adapter.sequences[1] > held
        assert self.adapter.on_device[self.adapter_device_id] == {vlan_member(813)}
        assert entries(self.device, "vlan") == []


class TestADelayedFoldAndsTheMarksAndRecordsIt(_MarkCase):
    """O1.5(c), O1.14(b), O1.14(c): both mark orders detach, and the downgrade is loud."""

    tag = "delay"
    adapter_device_id = 7703

    def _assert_both_detached(self, unowned, deleted, kept):
        from netbox_nso_plugin import drain

        assert self.drain() == drain.SUCCEEDED
        assert self.marks() == [None], "one send, whose flag is false AND true"
        device = self.adapter.on_device[self.adapter_device_id]
        assert device == {vlan_member(unowned), vlan_member(deleted), vlan_member(kept)}
        assert self.adapter.detached[self.adapter_device_id] == {vlan_member(unowned), vlan_member(deleted)}
        recorded = state_of(self.device, "vlan").degraded_deletions
        assert [entry["reason"] for entry in recorded] == [drain.LEGACY_MARK_DOWNGRADED]
        assert recorded[0]["device"] == self.device.pk

    def test_an_unmarked_edit_then_a_marked_deletion_fold_to_one_unmarked_send(self):
        states = self.own(821, 822, 823)
        self.unown(states[821])
        self.delete_overlay(states[822])

        self._assert_both_detached(unowned=821, deleted=822, kept=823)

    def test_a_marked_deletion_then_an_unmarked_edit_fold_the_same_way(self):
        states = self.own(831, 832, 833)
        self.delete_overlay(states[832])
        self.unown(states[831])

        self._assert_both_detached(unowned=831, deleted=832, kept=833)


class TestAnAllMarkedFoldRetracts(_MarkCase):
    """O1.14(f): when every contributor is marked, the send retracts what today's drain would."""

    tag = "allmark"
    adapter_device_id = 7704

    def test_every_contributor_marked_retracts_exactly_the_omitted_objects(self):
        from netbox_nso_plugin import drain

        states = self.own(841, 842, 843)
        self.delete_overlay(states[841])
        self.delete_overlay(states[842])

        assert self.drain() == drain.SUCCEEDED

        assert self.marks() == ["true"]
        assert self.adapter.on_device[self.adapter_device_id] == {vlan_member(843)}
        assert self.adapter.detached[self.adapter_device_id] == set()
        assert state_of(self.device, "vlan").degraded_deletions == [], "nothing was downgraded"


class TestALateEntryNeverAbandonsTheClaim(_MarkCase):
    """O1.14(a), O1.14(d): there is no pre-send invalidation, so the claim sends first time."""

    tag = "late"
    adapter_device_id = 7705

    def test_a_lower_id_entry_committing_before_the_send_does_not_abandon(self):
        from netbox_nso_plugin import drain, outbox

        route = own_route(self.mgmt, "198.51.100.80/28", "198.51.100.6")
        self.clear_entries()

        started = threading.Event()
        release = threading.Event()

        def late_writer():
            """Allocates its entry first and commits it last, so its id is the lower one."""
            with transaction.atomic():
                outbox.enqueue(self.device.pk, "static_route", transitions=[])
                started.set()
                assert release.wait(timeout=30)

        worker = threading.Thread(target=lambda: in_thread(late_writer))
        worker.start()
        assert started.wait(timeout=30)

        with patch(PUT_STATIC), transaction.atomic():
            self.mgmt.static_route_states.get(static_route=route).save()
        with as_per_object("static_route"):
            claimed = drain.claim(self.device.pk, "static_route")
            assert claimed is not None
            release.set()
            worker.join(timeout=30)

            config, session = self.adapter.patches()
            with config, session:
                answer = drain.send_claim(claimed)
            assert answer not in (drain.ABANDONED, drain.PARKED), "no rule invalidates a late entry"
            assert drain.settle(claimed, answer) == drain.SUCCEEDED

        assert len(self.sent()) == 1
        assert state_of(self.device, "static_route").attempts == 0
        assert len(entries(self.device, "static_route", unconsumed=True)) == 1

    def test_a_sustained_writer_never_stops_the_claim_from_sending(self):
        from netbox_nso_plugin import delivery, drain, outbox

        states = self.own(851, 852)
        self.unown(states[852])
        real_render = delivery.render

        def render_then_commit(*args, **kwargs):
            """A writer that commits one more entry before every attempt's render."""
            rendered = real_render(*args, **kwargs)
            in_thread(lambda: outbox.enqueue(self.device.pk, "vlan", transitions=[]))
            return rendered

        with patch("netbox_nso_plugin.delivery.render", side_effect=render_then_commit):
            outcome = self.drain(chain=0)

        assert outcome == drain.SUCCEEDED
        assert len(self.sent()) == 1, "the claim sent on its first attempt"
        assert state_of(self.device, "vlan").attempts == 0
        assert entries(self.device, "vlan", unconsumed=True), "the writer's later entry simply waits"


class TestACommittedRevocationAbandonsAndReformsOnce(_MarkCase):
    """O1.14(e): the pre-send scan reads the unconsumed transitions, then re-forms once."""

    tag = "revoke"
    adapter_device_id = 7706

    def test_a_revocation_committed_before_the_scan_is_folded_by_the_re_form(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.signals import _accept_static_route_for_device

        route = own_route(self.mgmt, "198.51.100.96/28", "198.51.100.7")
        with patch(PUT_STATIC):
            route.devices.remove(self.device)
        self.adapter.place(self.adapter_device_id, ("route_id", route.pk))

        real_scan = drain.revocation_hit
        scanned: list[bool] = []

        def scan_after_a_committed_re_own(claim):
            """The re-ownership commits while the claim is formed, before this scan runs."""
            if not scanned:
                with patch(PUT_STATIC):
                    in_thread(lambda: _accept_static_route_for_device(route, self.device))
            hit = real_scan(claim)
            scanned.append(hit)
            return hit

        with (
            as_per_object("static_route"),
            patch("netbox_nso_plugin.drain.revocation_hit", side_effect=scan_after_a_committed_re_own),
        ):
            outcome = self.drain("static_route")

        assert scanned == [True, False], "abandoned on the first scan, re-formed exactly once"
        assert outcome == drain.SUCCEEDED
        assert self.sent(), "the re-formed claim did send"
        assert self.sent()[-1]["body"], "and its body carries the route NetBox owns again"
        row = state_of(self.device, "static_route")
        assert (row.claim_deletions, row.queued_deletions) == ([], [])
        assert self.adapter.on_device[self.adapter_device_id] == {("route_id", route.pk)}
