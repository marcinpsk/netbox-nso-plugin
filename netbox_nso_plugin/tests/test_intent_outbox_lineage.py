# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1): what a deletion record remembers, and for how long.

A deletion carries a LINEAGE, not an id: the adapter holds either the triple it last
acknowledged or the one an unresolved claim may have delivered, so an id plus the current
triple can match nothing there, be classified moot and detach the route in silence.

O1.28 captures that record at provenance time and renders it verbatim. O1.30 gives it the
two lifecycle rules it needs — a truthful (NULL) initialization, and a transfer across
re-ownership, which is the only thing that keeps ``[A, C]`` formable. O1.35 bounds it: at
most two triples at every step, one carried triple per pk, cleared by the success algebra.
"""

from __future__ import annotations

from django.db import transaction
from django.test import TransactionTestCase

from ._outbox_case import (
    ReceiptAdapter,
    as_per_object,
    entries,
    last_acked,
    make_managed,
    own_route,
    partition,
    state_of,
    triple,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class _LineageCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """One managed device and the delete/re-own cycles a shared route can go through."""

    tag = "lin"
    adapter_device_id = 7800

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
        with without_commit_drain():
            route.devices.remove(self.device)

    def reown(self, route):
        from netbox_nso_plugin.signals import _accept_static_route_for_device

        with without_commit_drain():
            _accept_static_route_for_device(route, self.device)

    def retriple(self, route, prefix, next_hop):
        """Move the route's own content, which is what makes a re-own a DIFFERENT triple."""
        with without_commit_drain(), transaction.atomic():
            route.prefix, route.next_hop = prefix, next_hop
            route.save()
        route.refresh_from_db()

    def stamp(self, route, value):
        from netbox_nso_plugin.models import NSOStaticRouteState

        NSOStaticRouteState.objects.filter(management=self.mgmt, static_route=route).update(last_acked_triple=value)

    def records(self, op="delete"):
        """Every transition of *op* the key's unconsumed entries carry, in entry-id order."""
        return [
            record
            for row in entries(self.device, "static_route", unconsumed=True)
            for record in row.transitions
            if record.get("op") == op
        ]


class TestTheRecordIsCapturedAtProvenanceTime(_LineageCase):
    """O1.28 (R7-B1, R7-B2): the mirror is alive for exactly one statement, so capture it there."""

    tag = "prov"
    adapter_device_id = 7801

    def test_a_lost_callback_still_leaves_a_renderable_record(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.0/28", "198.51.100.1")
        self.stamp(route, triple("198.51.100.0/28", "198.51.100.1"))
        self.clear_entries()
        self.unown(route)  # the callback never runs; the overlay and the route are gone
        route_id = route.pk
        with without_commit_drain():
            route.delete()

        claimed = drain.claim(self.device.pk, "static_route")
        assert [r["route_id"] for r in claimed.deletions] == [route_id]
        assert claimed.deletions[0]["triples"] == [triple("198.51.100.0/28", "198.51.100.1")]
        assert claimed.deletions[0]["unverified"] is False

    def test_a_content_edit_whose_push_never_landed_leads_with_the_acknowledged_triple(self):
        acked = triple("198.51.100.16/28", "198.51.100.2")
        route = own_route(self.mgmt, "198.51.100.16/28", "198.51.100.2")
        self.stamp(route, acked)
        self.clear_entries()

        # The content transition re-mirrors the overlay; its push is never made.
        self.retriple(route, "198.51.100.16/28", "198.51.100.99")
        self.unown(route)

        [record] = self.records()
        assert record["triples"] == [acked, triple("198.51.100.16/28", "198.51.100.99")]
        assert record["unverified"] is False

    def test_equal_triples_deduplicate_to_one(self):
        route = own_route(self.mgmt, "198.51.100.32/28", "198.51.100.3")
        self.stamp(route, triple("198.51.100.32/28", "198.51.100.3"))
        self.clear_entries()
        self.unown(route)

        [record] = self.records()
        assert record["triples"] == [triple("198.51.100.32/28", "198.51.100.3")]

    def test_an_abandon_rehomes_whole_records_so_the_cross_check_can_run(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError

        acked = triple("198.51.100.48/28", "198.51.100.4")
        route = own_route(self.mgmt, "198.51.100.48/28", "198.51.100.4")
        self.stamp(route, acked)
        self.clear_entries()
        self.unown(route)
        self.adapter.fail_with = AdapterError("fence shut", code="conflict", detail={"reason": "fence_shut"})

        with as_per_object("static_route"):
            assert self.drain() == drain.WITHHELD
            rehomed = state_of(self.device, "static_route").queued_deletions
            assert rehomed[0]["triples"] == [acked], "the WHOLE record, not a bare id"

            # The backfill pass reports removing exactly the row that record names, and the
            # chained ordinary claim then delivers the deletion at a new sequence.
            self.adapter.fail_with = None
            self.adapter._respond = lambda body: partition(removed=[acked])
            assert self.drain(chain=0) == drain.SUCCEEDED

        recorded = state_of(self.device, "static_route").degraded_deletions
        assert [r["route_ids"] for r in recorded] == [[route.pk]]
        assert recorded[0]["triples"] == [acked]
        assert recorded[0]["reason"] == drain.PRE_FENCE_DETACH


class TestInitializationIsTruthfulOrAbsent(_LineageCase):
    """O1.30(a) (R8-B3): a stamped mirror labels content the adapter never accepted."""

    tag = "init"
    adapter_device_id = 7802

    def test_a_migrated_forward_overlay_carries_its_current_triple_as_unverified(self):
        from netbox_nso_plugin.models import NSOStaticRouteState

        route = own_route(self.mgmt, "198.51.100.64/28", "198.51.100.5")
        assert last_acked(self.mgmt, route) is None, "never fabricated from the live mirror"
        assert NSOStaticRouteState._meta.get_field("last_acked_triple").default is None
        self.clear_entries()

        self.unown(route)

        [record] = self.records()
        assert record["triples"] == [triple("198.51.100.64/28", "198.51.100.5")]
        assert record["unverified"] is True, "the wire flag the adapter's conservative rule reads"

    def test_an_unverified_deletion_that_matched_nothing_is_recorded_not_mooted(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.80/28", "198.51.100.6")
        self.clear_entries()
        self.unown(route)
        legacy = triple("198.51.100.80/28", "198.51.100.6")

        with as_per_object("static_route"):
            # The adapter removed a row no id claimed, so the detach is attributable.
            self.adapter._respond = lambda body: partition(degraded=[route.pk], removed=[legacy])
            assert self.drain() == drain.SUCCEEDED

        recorded = state_of(self.device, "static_route").degraded_deletions
        assert [r["route_ids"] for r in recorded] == [[route.pk]]
        assert recorded[0]["triples"] == [legacy]


class TestTheLineageTransfersAcrossReOwnership(_LineageCase):
    """O1.30(b) (R8-B3): removal deletes the overlay, so the carry is the only history left."""

    tag = "xfer"
    adapter_device_id = 7803

    def test_a_delete_re_own_re_delete_cycle_still_names_the_adapter_triple(self):
        first = triple("198.51.100.96/28", "198.51.100.7")
        route = own_route(self.mgmt, "198.51.100.96/28", "198.51.100.7")
        self.stamp(route, first)  # the adapter holds A
        self.clear_entries()

        self.unown(route)  # record [A]
        self.retriple(route, "198.51.100.96/28", "198.51.100.77")
        self.reown(route)  # the fresh overlay inherits A
        second = triple("198.51.100.96/28", "198.51.100.77")
        assert last_acked(self.mgmt, route) == first, "seeded from the carry, not left NULL"

        self.unown(route)  # record [A, C]

        record = self.records()[-1]
        assert record["triples"] == [first, second]
        assert record["unverified"] is False, "A is a real acknowledgement, so this is verified"

    def test_the_fold_carries_the_triple_onto_the_state_row(self):
        from netbox_nso_plugin import drain

        first = triple("198.51.100.112/28", "198.51.100.8")
        route = own_route(self.mgmt, "198.51.100.112/28", "198.51.100.8")
        self.stamp(route, first)
        self.clear_entries()
        self.unown(route)
        self.reown(route)

        claimed = drain.claim(self.device.pk, "static_route")
        assert claimed.deletions == [], "the revocation withdrew the authority"
        carry = state_of(self.device, "static_route").lineage_carry
        assert carry == {str(route.pk): first}, carry


class TestTheLineageIsBoundedAndCleared(_LineageCase):
    """O1.35 (R9-M2, R10-M1): two triples at most, one carry per pk, cleared by the success."""

    tag = "bound"
    adapter_device_id = 7804

    def test_repeated_cycles_never_grow_the_emitted_lineage(self):
        acked = triple("198.51.100.128/28", "198.51.100.9")
        route = own_route(self.mgmt, "198.51.100.128/28", "198.51.100.9")
        self.stamp(route, acked)
        self.clear_entries()

        seen = []
        for index in range(4):
            self.unown(route)
            seen.append([r["triples"] for r in self.records()])
            self.retriple(route, "198.51.100.128/28", f"198.51.100.1{index}")
            self.reown(route)
            carry = state_of(self.device, "static_route")
            assert len(carry.lineage_carry) <= 1 if carry else True
        self.unown(route)
        seen.append([r["triples"] for r in self.records()])

        for step, lineages in enumerate(seen):
            for lineage in lineages:
                assert 1 <= len(lineage) <= 2, (step, lineage)
                assert lineage[0] == acked, "the acknowledged triple always leads"

    def test_the_success_algebra_clears_the_carry_for_every_request_mode(self):
        """The clearing lives in the success algebra, never in one mode's own branch.

        A store-only claim reaches it carrying nothing (§4.3(d)), so it is refused while the
        deletion is pending and the ordinary claim behind it is what clears the carry. The
        carry survives that refusal, which is the point of refusing rather than folding.
        """
        from netbox_nso_plugin import delivery, drain

        for index, mode in enumerate((delivery.MODE_NORMAL, delivery.MODE_STORE_ONLY)):
            with self.subTest(mode=mode):
                prefix = f"198.51.100.{160 + index * 16}/28"
                route = own_route(self.mgmt, prefix, f"198.51.100.2{index}")
                self.stamp(route, triple(prefix, f"198.51.100.2{index}"))
                self.unown(route)
                self.reown(route)
                self.adapter._respond = lambda body: partition()
                assert self.drain(mode=mode) == drain.SUCCEEDED, "the fold is what writes the carry"
                assert state_of(self.device, "static_route").lineage_carry

                self.unown(route)
                with as_per_object("static_route"):
                    self.adapter._respond = lambda body, pk=route.pk: partition(executed=[pk])
                    if mode == delivery.MODE_STORE_ONLY:
                        assert self.drain(mode=mode) == drain.REFUSED
                        assert state_of(self.device, "static_route").lineage_carry, "still owed"
                    assert self.drain() == drain.SUCCEEDED

                state = state_of(self.device, "static_route")
                assert state.lineage_carry == {}, state.lineage_carry
                assert state.revoked_ids == []

    def test_a_restored_database_says_unverified_rather_than_guessing(self):
        from netbox_nso_plugin import drain

        acked = triple("198.51.100.208/28", "198.51.100.30")
        route = own_route(self.mgmt, "198.51.100.208/28", "198.51.100.30")
        self.stamp(route, acked)
        self.clear_entries()

        assert drain.clear_acknowledged_lineage() == 1
        self.unown(route)

        [record] = self.records()
        assert record["unverified"] is True
        assert record["triples"] == [acked], "the current mirror, which is all a restore knows"

    def test_a_backfill_leaves_the_row_unverified_so_the_next_deletion_says_so(self):
        from netbox_nso_plugin import delivery, drain

        route = own_route(self.mgmt, "198.51.100.224/28", "198.51.100.31")
        self.clear_entries()
        with without_commit_drain(), transaction.atomic():
            self.mgmt.static_route_states.get(static_route=route).save()

        assert self.drain(mode=delivery.MODE_BACKFILL_ONLY, force=True) == drain.SUCCEEDED
        assert last_acked(self.mgmt, route) is None

        self.clear_entries()
        self.unown(route)
        [record] = self.records()
        assert record["unverified"] is True
