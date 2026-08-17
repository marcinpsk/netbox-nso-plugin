# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1): compaction, and the two classes of growth it does and does not bound.

Compaction collapses a key's unconsumed entries into one by concatenating their transitions
in row order and reducing per route, which is exact because a route's contribution depends
only on its last transition plus one fold-time constant. It is a locked operation, not a
predicated one: it takes the same state-row lock a claim takes, so the two cannot both be
reading an entry while one rewrites it.

Two rules keep the rewrite sound. It UPDATES the highest-id input row in place rather than
minting one, because a new row would take an id above anything that committed while
compaction ran and the later fold would then apply the transitions backwards (O1.38). And it
EXCLUDES routes an active claim already holds, which is what makes the exactness interval
real over the whole life of that claim (O1.34) and is why growth has two classes: compactable
rows collapse to one per key, while excluded rows stay one per edit.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from django.db import OperationalError, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from ._outbox_case import (
    ReceiptAdapter,
    as_per_object,
    entries,
    in_thread,
    make_managed,
    own_route,
    own_vlan,
    state_of,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

TRIPLE_A = {"vrf": "", "prefix": "203.0.113.0/28", "next_hop": "203.0.113.1"}
TRIPLE_C = {"vrf": "", "prefix": "203.0.113.16/28", "next_hop": "203.0.113.2"}


def _statement_timeout(*_args, **_kwargs):
    """A cancelled statement, shaped as the driver reports one."""

    class _Cause(Exception):
        sqlstate = "57014"

    error = OperationalError("canceling statement due to statement timeout")
    error.__cause__ = _Cause()
    raise error


class _CompactionCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    tag = "cmp"
    adapter_device_id = 7900

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def clear_entries(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()

    def append(self, *transitions, scope="static_route", delete_origin=False):
        """One operator transaction's contribution, appended the way the choke point does."""
        from netbox_nso_plugin import outbox

        with transaction.atomic():
            outbox.enqueue(self.device.pk, scope, transitions=list(transitions), delete_origin=delete_origin)

    def delete_of(self, route_id, *, last_acked=TRIPLE_A, current=TRIPLE_C):
        from netbox_nso_plugin import outbox

        return outbox.delete_transition(route_id, last_acked=last_acked, current=current)

    def revoke_of(self, route_id, **kwargs):
        from netbox_nso_plugin import outbox

        return outbox.revoke_transition(route_id, **kwargs)

    def rows(self, scope="static_route"):
        return entries(self.device, scope, unconsumed=True)

    def transitions(self, scope="static_route"):
        return [record for row in self.rows(scope) for record in row.transitions]

    def drain(self, scope="vlan", **kwargs):
        from netbox_nso_plugin import drain

        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_key(self.device.pk, scope, **kwargs)


class TestCompactionAndTheClaimAreMutuallyExclusive(_CompactionCase):
    """O1.22 (R5-B3): the state row is a lock, not a predicate two readers both satisfy."""

    tag = "excl"
    adapter_device_id = 7901

    def test_a_claimant_waits_for_a_compactor_holding_the_state_row(self):
        from netbox_nso_plugin import drain

        for index in range(3):
            self.append(self.delete_of(4000 + index))
        locked = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        real_unconsumed = drain._unconsumed

        def hold_the_lock(*args, **kwargs):
            rows = real_unconsumed(*args, **kwargs)
            if threading.current_thread().name == "compactor" and not locked.is_set():
                locked.set()
                assert release.wait(timeout=30)
            return rows

        def compact():
            from django.db import connection as own

            try:
                drain.compact(self.device.pk, "static_route")
            finally:
                own.close()
                finished.set()

        with patch("netbox_nso_plugin.drain._unconsumed", side_effect=hold_the_lock):
            compactor = threading.Thread(target=compact, name="compactor")
            compactor.start()
            assert locked.wait(timeout=30), "the compactor never took the lock"

            claimed: list = []
            claimant = threading.Thread(target=lambda: in_thread(lambda: claimed.append(self._claim())))
            claimant.start()
            claimant.join(timeout=3)
            assert claimant.is_alive(), "the claimant did not wait for the compactor's lock"

            release.set()
            claimant.join(timeout=60)
            compactor.join(timeout=60)

        assert finished.is_set()
        assert claimed and claimed[0] is not None
        folded = [record["route_id"] for record in claimed[0].deletions]
        assert sorted(folded) == [4000, 4001, 4002], "a contribution was duplicated or lost"
        assert self.rows() == [], "the compacted row was consumed with the rest"

    def _claim(self):
        from netbox_nso_plugin import drain

        return drain.claim(self.device.pk, "static_route")

    def test_the_writes_name_exact_primary_keys(self):
        import re

        from netbox_nso_plugin import drain

        def named_pks(sql):
            return {int(value) for value in re.findall(r"\b\d+\b", sql)}

        for index in range(3):
            self.append(self.delete_of(4010 + index))
        selected = [row.pk for row in self.rows()]

        with CaptureQueriesContext(connection) as queries:
            drain.compact(self.device.pk, "static_route")

        written = [q["sql"] for q in queries.captured_queries if "outboxentry" in q["sql"].lower()]
        selects = [sql for sql in written if sql.startswith("SELECT")]
        assert any("FOR UPDATE" in sql for sql in selects), selects
        deletes = [sql for sql in written if sql.startswith("DELETE")]
        assert deletes, written
        deleted = set().union(*(named_pks(sql) for sql in deletes))
        assert set(selected[:-1]) <= deleted, f"the delete did not name {set(selected[:-1]) - deleted}"
        assert selected[-1] not in deleted, "the survivor was deleted"
        assert all("IN (" in sql or "= " in sql for sql in deletes)
        updates = [sql for sql in written if sql.startswith("UPDATE")]
        assert updates and all(selected[-1] in named_pks(sql) for sql in updates), updates

    def test_a_write_touching_a_different_number_of_rows_aborts(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        self.append(self.delete_of(4020))
        row = self.rows()[0]

        try:
            drain._retire(NSOIntentOutboxEntry.objects.filter(pk__in=[row.pk, row.pk + 5000]), [row.pk, row.pk + 5000])
        except drain.ClaimConflict:
            pass
        else:
            raise AssertionError("a short delete was accepted")

        try:
            drain._consume([row.pk, row.pk + 5000], 1)
        except drain.ClaimConflict:
            pass
        else:
            raise AssertionError("a short update was accepted")


class TestTheTwoClassesOfGrowth(_CompactionCase):
    """O1.22 second arm, O1.34 (R14-M1, R15-M1): only compactable rows have a row bound."""

    tag = "grow"
    adapter_device_id = 7902

    def test_compactable_rows_collapse_to_one_per_key(self):
        from netbox_nso_plugin import drain

        for index in range(6):
            self.append(self.delete_of(4100 + index))
        assert len(self.rows()) == 6

        drain.compact(self.device.pk, "static_route")

        assert len(self.rows()) == 1
        assert sorted(record["route_id"] for record in self.transitions()) == list(range(4100, 4106))

    def test_a_string_route_id_held_by_a_claim_stays_out_of_compaction(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        self.append(self.delete_of("7"))
        self.append(self.delete_of(8))
        self.append(self.delete_of(9))
        NSOIntentOutboxState.objects.create(
            device=self.device,
            scope="static_route",
            claim_deletions=[{"route_id": 7}],
        )

        drain.compact(self.device.pk, "static_route")

        rows = self.rows()
        assert len(rows) == 2, "a held route must not be merged into the compacted row"
        assert [record["route_id"] for record in rows[0].transitions] == ["7"]
        assert sorted(record["route_id"] for row in rows[1:] for record in row.transitions) == [8, 9]

    def test_rows_an_active_claim_holds_stay_one_per_edit(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.0/28", "198.51.100.1")
        with without_commit_drain():
            route.devices.remove(self.device)
        held = drain.claim(self.device.pk, "static_route")
        assert [record["route_id"] for record in held.deletions] == [route.pk]
        self.clear_entries()

        for _ in range(4):
            self.append(self.revoke_of(route.pk))
            self.append(self.delete_of(route.pk))
        assert len(self.rows()) == 8

        drain.compact(self.device.pk, "static_route")

        assert len(self.rows()) == 8, "a route the live claim holds must not be merged away"

    def test_the_burst_a_stuck_claim_does_not_hold_still_collapses(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.16/28", "198.51.100.2")
        with without_commit_drain():
            route.devices.remove(self.device)
        held = drain.claim(self.device.pk, "static_route")
        assert held.deletions
        self.clear_entries()

        for index in range(6):
            # Alternating legacy marks: a per-alternation implementation would keep them apart.
            self.append(self.delete_of(4200 + index), delete_origin=index % 2 == 0)
        assert len(self.rows()) == 6

        drain.compact(self.device.pk, "static_route")

        assert len(self.rows()) == 1, "compaction is not skipped because a claim is in flight"
        survivor = self.rows()[0]
        assert (survivor.mark_and, survivor.mark_any) == (False, True)

    def test_the_same_burst_collapses_in_per_object_mode(self):
        from netbox_nso_plugin import drain

        for index in range(5):
            self.append(self.delete_of(4210 + index), delete_origin=index % 2 == 0)

        with as_per_object("static_route"):
            drain.compact(self.device.pk, "static_route")

        assert len(self.rows()) == 1
        assert sorted(record["route_id"] for record in self.transitions()) == list(range(4210, 4215))

    def test_the_tick_pass_compacts_a_key_no_claim_can_reach(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.48/28", "198.51.100.4")
        with without_commit_drain():
            route.devices.remove(self.device)
        held = drain.claim(self.device.pk, "static_route")
        assert held.deletions, "the key now carries a live claim, so no drain may take it"
        self.clear_entries()
        for index in range(5):
            self.append(self.delete_of(4220 + index))

        config, session = self.adapter.patches()
        with config, session:
            drain.drain_intent_outbox()

        assert len(self.rows()) == 1, "the tick's compaction pass never reached the stuck key"


class TestTheReductionAppliesTheAlgebra(_CompactionCase):
    """O1.34 second arm (R13-B2): reducing must not discard what the algebra needs."""

    tag = "algebra"
    adapter_device_id = 7903

    def test_a_delete_then_revoke_pair_carries_the_acknowledged_triple_forward(self):
        from netbox_nso_plugin import drain, outbox

        self.append(self.delete_of(4300, last_acked=TRIPLE_A, current=TRIPLE_C))
        self.append(self.revoke_of(4300))

        drain.compact(self.device.pk, "static_route")

        assert len(self.rows()) == 1
        [record] = self.transitions()
        assert record["op"] == outbox.OP_REVOKE
        assert record["carried_triple"] == TRIPLE_A, "the deleted record's history was dropped"

        folded = outbox.fold_transitions(self.transitions())
        assert folded.queued == {}
        assert folded.lineage_carry == {4300: TRIPLE_A}, "O1.30(b)'s [A, C] lineage can no longer form"

    def test_a_string_route_id_reuses_the_integer_route_lineage_when_reducing(self):
        from netbox_nso_plugin import drain, outbox

        self.append(self.delete_of(7, last_acked=TRIPLE_A, current=TRIPLE_C))
        self.append(self.revoke_of("7"))

        drain.compact(self.device.pk, "static_route")

        [record] = self.transitions()
        assert record["op"] == outbox.OP_REVOKE
        assert int(record["route_id"]) == 7
        assert record["carried_triple"] == TRIPLE_A

    def test_a_revoke_then_delete_pair_reduces_the_other_way(self):
        from netbox_nso_plugin import drain, outbox

        self.append(self.revoke_of(4310))
        self.append(self.delete_of(4310, last_acked=TRIPLE_A, current=TRIPLE_C))

        drain.compact(self.device.pk, "static_route")

        [record] = self.transitions()
        assert record["op"] == outbox.OP_DELETE
        assert record["triples"] == [TRIPLE_A, TRIPLE_C]
        folded = outbox.fold_transitions(self.transitions())
        assert sorted(folded.queued) == [4310], "the two histories reduced to the same thing"

    def test_an_unmarked_contributor_survives_as_mark_any_without_mark_and(self):
        from netbox_nso_plugin import drain

        self.append(self.delete_of(4320), delete_origin=False)
        self.append(self.delete_of(4321), delete_origin=True)

        drain.compact(self.device.pk, "static_route")

        survivor = self.rows()[0]
        assert survivor.mark_and is False
        assert survivor.mark_any is True, "the evidence a marked contributor was downgraded is gone"


class TestACompactedRowSendsWhatItsContributorsAuthorized(_CompactionCase):
    """O1.34 third arm (R13-B4): a compacted row must not inherit a per-entry mark."""

    tag = "sendcmp"
    adapter_device_id = 7904

    def test_an_unmarked_then_marked_row_goes_out_unmarked_and_records_the_downgrade(self):
        from netbox_nso_plugin import drain

        kept = own_vlan(self.mgmt, 880, self.tag)
        going = own_vlan(self.mgmt, 881, self.tag)
        assert self.drain() == drain.SUCCEEDED
        assert self.adapter.on_device[self.adapter_device_id] == {("vlan_id", 880), ("vlan_id", 881)}
        self.clear_entries()
        self.adapter.requests.clear()

        with without_commit_drain(), transaction.atomic():
            kept.status = "imported"  # an unmarked shrink: the operator un-owns it
            kept.save()
        with without_commit_drain(), transaction.atomic():
            going.delete()  # a marked shrink: the object is destroyed in NetBox
        assert len(self.rows("vlan")) == 2

        drain.compact(self.device.pk, "vlan")
        assert len(self.rows("vlan")) == 1

        assert self.drain() == drain.SUCCEEDED

        [request] = [r for r in self.adapter.requests if f"/devices/{self.adapter_device_id}/" in r["url"]]
        assert request["params"].get("delete_origin") is None, "the compacted row sent marked"
        assert self.adapter.on_device[self.adapter_device_id] == {("vlan_id", 880), ("vlan_id", 881)}
        recorded = state_of(self.device, "vlan").degraded_deletions
        assert [entry["reason"] for entry in recorded] == [drain.LEGACY_MARK_DOWNGRADED]


class TestAnOversizedFoldCompactsThenRetries(_CompactionCase):
    """O1.34: a timed-out fold is compacted and retried once, and never spins."""

    tag = "timeout"
    adapter_device_id = 7905

    def test_a_fold_that_times_out_is_compacted_and_retried(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 890, self.tag)
        for _ in range(3):
            self.append(scope="vlan")
        real_claim = drain._claim_locked
        compactions: list = []
        calls: list[int] = []

        def timeout_once(*args, **kwargs):
            calls.append(len(calls))
            if len(calls) == 1:
                _statement_timeout()
            return real_claim(*args, **kwargs)

        real_compact = drain.compact

        def record_compaction(*args, **kwargs):
            compactions.append(args)
            return real_compact(*args, **kwargs)

        with (
            patch("netbox_nso_plugin.drain._claim_locked", side_effect=timeout_once),
            patch("netbox_nso_plugin.drain.compact", side_effect=record_compaction),
        ):
            outcome = self.drain()

        assert compactions == [(self.device.pk, "vlan")], "the timeout did not compact before retrying"
        assert outcome == drain.SUCCEEDED
        assert len(calls) == 2

    def test_a_key_that_keeps_timing_out_is_left_with_its_attempt_stamped(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 891, self.tag)

        with patch("netbox_nso_plugin.drain._claim_locked", side_effect=_statement_timeout):
            outcome = self.drain()

        assert outcome == drain.FAILED
        row = state_of(self.device, "vlan")
        assert row is not None and row.last_drain_attempted_at is not None, (
            "an unstamped key stays at the head of every pass and starves the rest"
        )
        assert row.push_seq is None and self.rows("vlan"), "the work is untouched, so it keeps"


class TestCompactionRewritesInPlace(_CompactionCase):
    """O1.38 (R13-B3): a minted row would leapfrog whatever committed while compaction ran."""

    tag = "inplace"
    adapter_device_id = 7906

    def _append_re_own_and_report(self, route_id, seen):
        """Commit a re-ownership and report its entry id, read on the writer's own snapshot."""

        def work():
            from netbox_nso_plugin.models import NSOIntentOutboxEntry

            self.append(self.revoke_of(route_id))
            seen.append(NSOIntentOutboxEntry.objects.order_by("-pk").first().pk)

        return work

    def test_the_highest_id_input_row_is_updated_and_the_lower_ones_deleted(self):
        from netbox_nso_plugin import drain, outbox

        self.append(self.delete_of(4400, last_acked=TRIPLE_A, current=TRIPLE_C))
        self.append(self.delete_of(4401))
        selected = [row.pk for row in self.rows()]
        late: list[int] = []
        real_unconsumed = drain._unconsumed

        def commit_a_re_own_after_the_selection(*args, **kwargs):
            rows = real_unconsumed(*args, **kwargs)
            if not late:
                list(rows)  # the compactor has read the key, and only then does E2 commit
                in_thread(self._append_re_own_and_report(4400, late))
            return rows

        with patch("netbox_nso_plugin.drain._unconsumed", side_effect=commit_a_re_own_after_the_selection):
            drain.compact(self.device.pk, "static_route")

        assert late, "the concurrent re-ownership never committed, so this pin proves nothing"
        survivors = [row.pk for row in self.rows()]
        assert survivors == [max(selected), late[0]], f"selected={selected} late={late} left={survivors}"
        assert max(selected) < late[0], "the compacted content must keep an id below the later commit"

        folded = outbox.fold_transitions(self.transitions())
        assert sorted(folded.queued) == [4401], "the fold applied the transitions out of commit order"

    def test_compaction_never_inserts_a_row(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        for index in range(4):
            self.append(self.delete_of(4410 + index))
        before = NSOIntentOutboxEntry.objects.order_by("-pk").first().pk

        with CaptureQueriesContext(connection) as queries:
            drain.compact(self.device.pk, "static_route")

        inserts = [
            q["sql"]
            for q in queries.captured_queries
            if q["sql"].startswith("INSERT INTO") and "outbox" in q["sql"].lower()
        ]
        assert [sql for sql in inserts if "outboxentry" in sql.lower()] == [], inserts
        assert NSOIntentOutboxEntry.objects.order_by("-pk").first().pk == before

    def test_a_route_the_active_claim_holds_is_excluded_from_the_pass(self):
        from netbox_nso_plugin import drain

        route = own_route(self.mgmt, "198.51.100.32/28", "198.51.100.3")
        with without_commit_drain():
            route.devices.remove(self.device)
        held = drain.claim(self.device.pk, "static_route")
        assert [record["route_id"] for record in held.deletions] == [route.pk]
        self.clear_entries()

        self.append(self.revoke_of(route.pk))
        self.append(self.delete_of(4420))
        self.append(self.delete_of(4421))
        held_row = self.rows()[0].pk

        drain.compact(self.device.pk, "static_route")

        left = [row.pk for row in self.rows()]
        assert held_row in left, "the held route's row was merged while its claim was live"
        assert len(left) == 2, f"the two free rows did not collapse: {left}"
