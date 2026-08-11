# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1): the lease, the snapshot bracket, and the order two writers land in.

O1.21(a) and (b) are about the two clocks: the lease is read on the database clock so a
skewed application clock cannot steal a live claim, and a takeover refreshes the lease so
two scavengers cannot both replay. O1.21(c), the send's own wall clock, needs a real socket
to say anything at all and lives in ``test_intent_outbox_deadline``.

O1.25 is the snapshot bracket. Under READ COMMITTED the fold and the render see different
worlds, so a deletion committing between them ships a body that omits a route while
carrying no authority for it. One snapshot makes it invisible to both.

O1.37 is why the fold may order by entry id at all: two transitions for the same route
serialize on that route's own rows, so the later committer's entry is the later row.
"""

from __future__ import annotations

import datetime
import threading
import time
from unittest.mock import patch

from django.db import OperationalError, transaction
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


class _ConcurrencyCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    tag = "conc"
    adapter_device_id = 7800

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


class TestTheLeaseIsReadOnTheDatabaseClock(_ConcurrencyCase):
    """O1.21(a): an application clock running ahead may not expire a live claim."""

    tag = "skew"
    adapter_device_id = 7801

    def test_a_clock_an_hour_ahead_of_the_database_steals_nothing(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 870, self.tag)
        held = drain.claim(self.device.pk, "vlan")
        assert held is not None

        skewed = drain._db_now() + drain.LEASE + datetime.timedelta(hours=1)
        with patch("django.utils.timezone.now", return_value=skewed):
            assert drain.claim(self.device.pk, "vlan") is None, "the live claim was stolen"
            assert (self.device.pk, "vlan") not in drain.drain_candidates()

        assert state_of(self.device, "vlan").push_seq == held.push_seq


class TestOnlyOneScavengerReplaysAnExpiredClaim(_ConcurrencyCase):
    """O1.21(b): the takeover refreshes the lease inside the lock that granted it."""

    tag = "scav"
    adapter_device_id = 7802

    def test_two_scavengers_on_one_expired_claim_yield_one_replay(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 871, self.tag)
        crashed = drain.claim(self.device.pk, "vlan")
        expire_claim(self.device, "vlan")
        before = state_of(self.device, "vlan").claimed_at

        taken: list = []
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def scavenge():
            from django.db import connection

            try:
                start.wait(timeout=30)
                taken.append(drain.claim(self.device.pk, "vlan"))
            except BaseException as exc:  # noqa: BLE001 (reported, not swallowed)
                errors.append(exc)
            finally:
                connection.close()

        scavengers = [threading.Thread(target=scavenge) for _ in range(2)]
        for thread in scavengers:
            thread.start()
        for thread in scavengers:
            thread.join(timeout=60)

        assert errors == []
        replayed = [claim for claim in taken if claim is not None]
        assert len(replayed) == 1, "both scavengers replayed the same operation"
        assert replayed[0].push_seq == crashed.push_seq
        assert replayed[0].replayed is True
        assert state_of(self.device, "vlan").claimed_at > before, "the takeover refreshed the lease"


class TestTheFoldAndTheRenderShareOneSnapshot(_ConcurrencyCase):
    """O1.25 (R5-B2): a deletion committing between them must be invisible to both."""

    tag = "snap"
    adapter_device_id = 7804

    def test_a_deletion_committing_before_the_render_leaves_body_and_authority_together(self):
        from netbox_nso_plugin import delivery, drain

        route = own_route(self.mgmt, "198.51.100.112/28", "198.51.100.8")
        keeper = own_route(self.mgmt, "198.51.100.128/28", "198.51.100.9")
        real_render = delivery.render
        removed = threading.Event()

        def remove_between_the_fold_and_the_render(*args, **kwargs):
            """The operator's removal commits after the fold read its entries."""
            if not removed.is_set():
                self._remove_in_another_connection(route)
                removed.set()
            return real_render(*args, **kwargs)

        with patch("netbox_nso_plugin.delivery.render", side_effect=remove_between_the_fold_and_the_render):
            claimed = drain.claim(self.device.pk, "static_route")

        assert claimed is not None
        sent_ids = {entry["route_id"] for entry in claimed.payload}
        assert sent_ids == {route.pk, keeper.pk}, "the body is rendered on the fold's own snapshot"
        assert claimed.deletions == []
        assert entries(self.device, "static_route", unconsumed=True), "the deletion entry is untouched"

        config, session = self.adapter.patches()
        with config, session:
            answer = drain.send_claim(claimed)
        assert drain.settle(claimed, answer) == drain.SUCCEEDED

        later = drain.claim(self.device.pk, "static_route")
        assert {entry["route_id"] for entry in later.payload} == {keeper.pk}
        assert [record["route_id"] for record in later.deletions] == [route.pk], (
            "the next claim ships the omission WITH its authority"
        )

    def _remove_in_another_connection(self, route):
        """Commit the operator's removal on its own connection, patching only from here."""
        from ._outbox_case import in_thread, without_commit_drain

        def remove():
            with transaction.atomic():
                route.devices.remove(self.device)

        with without_commit_drain():
            in_thread(remove)


class _SerializationCause(Exception):
    """What psycopg raises underneath, which is where the retry rule reads the sqlstate."""

    sqlstate = "40001"


def _serialization_failure(*_args, **_kwargs):
    """A serialization failure shaped exactly as the driver reports one."""
    error = OperationalError("could not serialize access due to concurrent update")
    error.__cause__ = _SerializationCause()
    return error


def _always_fail(*args, **kwargs):
    raise _serialization_failure(*args, **kwargs)


class TestTheWholeClaimTransactionIsRetried(_ConcurrencyCase):
    """O1.25, second arm: the retry wraps the transaction, and exhaustion changes nothing."""

    tag = "retry"
    adapter_device_id = 7805

    def test_a_serialization_failure_retries_the_whole_transaction(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 873, self.tag)
        real_claim = drain._claim_locked
        attempts: list[int] = []

        def fail_once(*args, **kwargs):
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise _serialization_failure()
            return real_claim(*args, **kwargs)

        with patch("netbox_nso_plugin.drain._claim_locked", side_effect=fail_once):
            claimed = drain.claim(self.device.pk, "vlan")

        assert len(attempts) == 2, "the retry resumed on a new snapshot, not inside the old one"
        assert claimed is not None and claimed.push_seq is not None

    def test_exhausting_the_retries_leaves_every_row_untouched(self):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, 874, self.tag)
        before = [(row.pk, row.consumed_by_push_seq) for row in entries(self.device, "vlan")]

        with patch("netbox_nso_plugin.drain._claim_locked", side_effect=_always_fail):
            try:
                drain.claim(self.device.pk, "vlan")
            except OperationalError:
                pass
            else:
                raise AssertionError("the exhausted retry swallowed its failure")

        assert [(row.pk, row.consumed_by_push_seq) for row in entries(self.device, "vlan")] == before
        assert state_of(self.device, "vlan") is None, "the rolled-back transaction created nothing"


class TestEntryIdOrderIsCommitOrderForOneRoute(_ConcurrencyCase):
    """O1.37 (R12-B2): the fold may order by id only because the route's own rows serialize."""

    tag = "order"
    adapter_device_id = 7806

    def _transitions_for(self, route_id):
        """The (entry id, op) pairs naming *route_id*, in entry-id order."""
        from netbox_nso_plugin import outbox

        found = []
        for row in entries(self.device, "static_route"):
            for record in row.transitions:
                if record.get("route_id") == route_id:
                    found.append((row.pk, record.get("op")))
        assert found, f"no transition was recorded for route {route_id}"
        return found, outbox.OP_DELETE, outbox.OP_REVOKE

    def _transaction(self, body, *, before=None, after=None, hold=None, committed=None):
        """One operator transaction: open, wait, work, announce it, hold, announce the commit.

        It patches nothing itself: ``mock.patch`` restores whatever it found on entry, so two
        threads entering the same patch concurrently leave the mock installed for good.
        """

        def work():
            with transaction.atomic():
                if before is not None:
                    assert before.wait(timeout=30)
                body()
                if after is not None:
                    after.set()
                if hold is not None:
                    assert hold.wait(timeout=30)
            if committed is not None:
                committed.set()

        return work

    def _remove(self, route, **barriers):
        return self._transaction(lambda: route.devices.remove(self.device), **barriers)

    def _reown(self, route, **barriers):
        from netbox_nso_plugin.signals import _accept_static_route_for_device

        def own():
            route.devices.add(self.device)
            _accept_static_route_for_device(route, self.device)

        return self._transaction(own, **barriers)

    def _run(self, *works):
        """Run every transaction concurrently and re-raise whatever any of them raised.

        Every patch these transactions need is entered HERE, once, on this thread, and held
        for the whole run: entering one from two threads at a time leaks it permanently.
        """
        from ._outbox_case import in_thread, without_commit_drain

        errors: list[BaseException] = []

        def guarded(work):
            try:
                in_thread(work, timeout=60)
            except BaseException as exc:  # noqa: BLE001 (reported on the caller's thread)
                errors.append(exc)

        with without_commit_drain():
            threads = [threading.Thread(target=guarded, args=(work,)) for work in works]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=90)
            assert not any(thread.is_alive() for thread in threads), "a transaction never finished"
        assert errors == [], errors

    def test_a_second_writer_of_one_route_waits_on_the_first(self):
        """The overlay row is the serialization point, which is what the id order rests on."""
        from netbox_nso_plugin.signals import _accept_static_route_for_device

        route = own_route(self.mgmt, "198.51.100.144/28", "198.51.100.10")
        self.clear_entries()
        deleted = threading.Event()
        release = threading.Event()
        waited: list[float] = []

        def own_the_same_route():
            started = time.monotonic()
            _accept_static_route_for_device(route, self.device)
            waited.append(time.monotonic() - started)

        threading.Timer(2.0, release.set).start()
        self._run(
            self._remove(route, after=deleted, hold=release),
            self._transaction(own_the_same_route, before=deleted),
        )

        assert waited and waited[0] > 1.0, f"the second writer never waited: {waited}"

    def test_a_delete_committing_first_takes_the_lower_entry_id(self):
        route = own_route(self.mgmt, "198.51.100.240/28", "198.51.100.16")
        self.clear_entries()
        removed = threading.Event()

        # The re-ownership's transaction is open the whole time; it touches the route only
        # after the removal committed, which is what READ COMMITTED gives every statement.
        self._run(
            self._remove(route, committed=removed),
            self._reown(route, before=removed),
        )

        found, op_delete, op_revoke = self._transitions_for(route.pk)
        assert [op for _pk, op in found] == [op_delete, op_revoke], f"id order is not commit order: {found}"

    def test_a_re_ownership_committing_first_takes_the_lower_entry_id(self):
        route = own_route(self.mgmt, "198.51.100.160/28", "198.51.100.11")
        with without_commit_drain():
            route.devices.remove(self.device)  # un-owned, so the re-ownership is a real one
        self.clear_entries()
        owned = threading.Event()

        # The removal's transaction is open the whole time; it reads the route only after
        # the re-ownership committed, which is what READ COMMITTED gives every statement.
        self._run(
            self._reown(route, committed=owned),
            self._remove(route, before=owned),
        )

        found, op_delete, op_revoke = self._transitions_for(route.pk)
        assert [op for _pk, op in found] == [op_revoke, op_delete], f"id order is not commit order: {found}"

    def test_two_different_routes_commute_and_never_wait_on_each_other(self):
        from netbox_nso_plugin import outbox

        leaving = own_route(self.mgmt, "198.51.100.176/28", "198.51.100.12")
        returning = own_route(self.mgmt, "198.51.100.192/28", "198.51.100.13")
        with without_commit_drain():
            returning.devices.remove(self.device)
        self.clear_entries()
        held = threading.Event()
        owned = threading.Event()
        release = threading.Event()

        def release_once_the_other_finished():
            assert owned.wait(timeout=30), "the re-ownership waited on the removal's rows"
            release.set()

        watcher = threading.Thread(target=release_once_the_other_finished)
        watcher.start()
        # The removal holds its transaction open across the whole of the other one: two
        # routes touch disjoint rows, so the re-ownership commits without waiting for it.
        self._run(
            self._remove(leaving, after=held, hold=release),
            self._reown(returning, before=held, committed=owned),
        )
        watcher.join(timeout=30)

        folded = outbox.fold_transitions(
            [record for row in entries(self.device, "static_route") for record in row.transitions]
        )
        assert sorted(folded.queued) == [leaving.pk]
        assert returning.pk not in folded.queued
