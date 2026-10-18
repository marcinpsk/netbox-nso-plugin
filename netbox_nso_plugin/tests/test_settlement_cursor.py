# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The cursor epoch, durable unresolved evidence, and ordered consumption.

Pins S4.1 through S4.6. Every test drives the consumer over a **real HTTP** adapter double
on a loopback socket, so the parameters it builds, the header it reads and the errors it
maps are its own and not a canned return value. S4.3 goes further and drives two real
``manage.py`` subprocesses against the committed test database, because durable unresolved evidence must survive a real process restart.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading

from django.db import connection, connections
from django.utils import timezone

from ._settlement_case import (
    _execution_result,
    _make_device,
    _make_mgmt,
    _own,
    _record_execution,
    _route,
    _SettlementCase,
)


class TestConsumptionIsOnceAndMonotone(_SettlementCase):
    """S4.1 (P5.6) — one settlement, one advance, under one lock and one transaction."""

    def test_consumption_is_result_once_and_monotone(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("once")
        mgmt = _make_mgmt(device, "once", 10, adapter=self.adapter)
        sr = _route("10.1.0.0/16", "10.1.0.1", devices=[device])
        _own(sr, mgmt, generation=41, orphan=True)
        self.adapter.store.terminal_job(10, job_type="sync")

        first = consume_static_route_settlements(mgmt)
        assert first.consumed == 1
        assert first.cursor == 1
        assert self._cursor(mgmt).settle_cursor_seq == 1

        second = consume_static_route_settlements(mgmt)
        assert second.consumed == 0, "the same job was consumed twice"
        assert second.cursor == 1, "the cursor moved without a new settlement"
        # The second pass asked the adapter for what comes AFTER the cursor, so the already
        # consumed job was never served again.
        assert self.adapter.store.feed_requests[-1][1] == 1

    def test_racing_consumers_advance_the_cursor_once(self):
        """Two consumers on one device serialize on the management row, never double-advance."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("race")
        mgmt = _make_mgmt(device, "race", 20, adapter=self.adapter)
        sr = _route("10.2.0.0/16", "10.2.0.1", devices=[device])
        _own(sr, mgmt, generation=42, orphan=True)
        self.adapter.store.terminal_job(20, job_type="sync")

        results, errors = [], []
        start = threading.Barrier(2)

        def run():
            start.wait(timeout=10)
            try:
                results.append(consume_static_route_settlements(mgmt.pk))
            except Exception as exc:  # noqa: BLE001 — reported through the assertion below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads), "a consumer never returned"
        assert not errors, errors
        assert sorted(r.consumed for r in results) == [0, 1], "the one settlement was consumed twice"
        assert self._cursor(mgmt).settle_cursor_seq == 1

    def test_the_cursor_advance_does_not_refire_the_management_row_handlers(self):
        """Bookkeeping is not an intent change: a ``save()`` here would re-push the scope."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("fields")
        mgmt = _make_mgmt(device, "fields", 30, adapter=self.adapter)
        sr = _route("10.3.0.0/16", "10.3.0.1", devices=[device])
        _own(sr, mgmt, generation=43, orphan=True)
        store = self.adapter.store
        store.terminal_job(30, job_type="sync")
        store.requests.clear()

        consume_static_route_settlements(mgmt)

        assert self._cursor(mgmt).settle_cursor_seq == 1
        pushed = [
            req
            for req in store.requests
            if req[1].endswith(("/scope", "/sync-notify")) or req[1].endswith("/api/v1/devices")
        ]
        assert pushed == [], f"the cursor advance re-fired the row's adapter push: {pushed}"

    def test_the_consumer_polls_the_locked_rows_device_id_not_the_callers(self):
        """The epoch comes off the row the consumer locks, never off the object handed to it."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("locked")
        mgmt = _make_mgmt(device, "locked", 31, adapter=self.adapter)
        sr = _route("10.31.0.0/16", "10.31.0.1", devices=[device])
        _own(sr, mgmt, generation=44, orphan=True)
        self.adapter.store.terminal_job(31, job_type="sync")

        # A caller whose in-memory row is stale — exactly what the link repair leaves behind.
        mgmt.adapter_device_id = 999

        result = consume_static_route_settlements(mgmt)

        assert result.adapter_device_id == 31
        polled = {req[0] for req in self.adapter.store.feed_requests}
        assert polled == {31}, f"the stale caller id was polled: {polled}"
        assert result.consumed == 1


class TestCursorEpoch(_SettlementCase):
    """S4.2 / S4.2b / S4.2c — the epoch is compared on read, and neither half is cached."""

    def test_cursor_resets_on_incarnation_change(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("inc")
        mgmt = _make_mgmt(device, "inc", 40, adapter=self.adapter)
        sr = _route("10.4.0.0/16", "10.4.0.1", devices=[device])
        _own(sr, mgmt, generation=44, orphan=True)
        self.adapter.store.terminal_job(40, job_type="sync")

        # A cursor recorded against a store that is gone: everything below it would be
        # skipped forever if the epoch were not compared.
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
            settle_cursor_seq=100,
            settle_cursor_incarnation="a-dead-store",
            settle_cursor_device_id=40,
        )

        result = consume_static_route_settlements(mgmt)

        assert result.epoch_reset is True
        assert result.consumed == 1, "the settlement below the stale cursor was skipped"
        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1
        assert row.settle_cursor_incarnation == self.adapter.store.incarnation
        # The first page was fetched from the dead cursor, so it had to be re-requested.
        assert [request[1] for request in self.adapter.store.feed_requests][-2:] == [100, 0]

    def test_a_first_consumption_uses_one_cursor_page_after_epoch_brackets(self):
        """The epoch resets on a cursor that is already 0, so the page just fetched stands."""
        from netbox_nso_plugin.settlement import SETTLE_FEED_PAGE, consume_static_route_settlements

        device = _make_device("firstpass")
        mgmt = _make_mgmt(device, "firstpass", 42, adapter=self.adapter)
        sr = _route("10.42.0.0/16", "10.42.0.1", devices=[device])
        _own(sr, mgmt, generation=52, orphan=True)
        self.adapter.store.terminal_job(42, job_type="sync")

        result = consume_static_route_settlements(mgmt)

        assert result.epoch_reset is True
        assert result.consumed == 1
        assert self.adapter.store.feed_requests == [(42, 0, SETTLE_FEED_PAGE), (42, 1, 1), (42, 0, SETTLE_FEED_PAGE)], (
            f"expected two epoch brackets and one initial cursor page: {self.adapter.store.feed_requests}"
        )

    def test_a_recreated_management_row_starts_from_the_beginning(self):
        """P29 — a delete/recreate leaves no epoch, so the feed is walked from the start."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("recreate")
        mgmt = _make_mgmt(device, "recreate", 41, adapter=self.adapter)
        sr = _route("10.5.0.0/16", "10.5.0.1", devices=[device])
        _own(sr, mgmt, generation=45, orphan=True)
        self.adapter.store.terminal_job(41, job_type="sync")
        consume_static_route_settlements(mgmt)
        assert self._cursor(mgmt).settle_cursor_seq == 1

        mgmt.delete()
        fresh = _make_mgmt(device, "recreate", 41, adapter=self.adapter)
        sr2 = _route("10.5.1.0/24", "10.5.1.1", devices=[device])
        _own(sr2, fresh, generation=46, orphan=True)

        result = consume_static_route_settlements(fresh)

        assert result.epoch_reset is True
        assert result.consumed == 1, "the replayed job must be walked, not skipped"
        assert self._cursor(fresh).settle_cursor_seq == 1

    def test_a_device_remap_within_one_incarnation_resets_the_cursor(self):
        """S4.2b — the same store, a different adapter device, whose counter restarts at 1."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        for arm in ("moved", "reused", "reonboard"):
            with self.subTest(arm=arm):
                store = self.adapter.store
                device = _make_device(f"remap{arm}")
                mgmt = _make_mgmt(device, f"remap{arm}", 100, adapter=self.adapter)
                identity = mgmt.nso_instance.adapter_instance_id
                sr = _route(f"10.6.{len(arm)}.0/24", "10.6.0.1", devices=[device])
                _own(sr, mgmt, generation=50 + len(arm), orphan=True)

                # A cursor already well past this device's history, on adapter device 100.
                NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
                    settle_cursor_seq=100,
                    settle_cursor_incarnation=store.incarnation,
                    settle_cursor_device_id=100,
                    settle_stall_seq=100,
                    settle_stall_attempts=3,
                    settle_stall_first_seen_at=timezone.now(),
                )

                if arm == "moved":
                    # Our device is present under a different id.
                    store.devices.pop(100)
                    store.add_device(
                        nso_instance=identity,
                        nso_device_name=mgmt.nso_device_name,
                        netbox_device_id=device.pk,
                    )
                elif arm == "reused":
                    # Our id belongs to somebody else; our device is present under another id.
                    store.add_device(
                        nso_instance=identity,
                        nso_device_name="somebody-else",
                        netbox_device_id=device.pk + 9000,
                        device_id=100,
                    )
                    store.add_device(
                        nso_instance=identity,
                        nso_device_name=mgmt.nso_device_name,
                        netbox_device_id=device.pk,
                    )
                else:
                    # The missing device must be onboarded by the link repair.
                    store.devices.pop(100)

                reconcile_device_links([NSODeviceManagement.objects.get(pk=mgmt.pk)])

                row = NSODeviceManagement.objects.get(pk=mgmt.pk)
                assert row.adapter_device_id not in (None, 100), f"{arm}: the link was not repaired"
                new_id = row.adapter_device_id
                store.terminal_job(new_id, job_type="sync")

                result = consume_static_route_settlements(mgmt)

                assert result.epoch_reset is True, f"{arm}: the remap did not reset the epoch"
                assert result.consumed == 1, f"{arm}: sequence 1 on the new device was skipped"
                after = NSODeviceManagement.objects.get(pk=mgmt.pk)
                assert after.settle_cursor_seq == 1
                assert after.settle_cursor_device_id == new_id
                assert after.settle_stall_seq is None, f"{arm}: the stall triple survived the reset"
                assert after.settle_stall_attempts == 0

    def test_a_store_rebuild_is_caught_from_the_feed_not_the_cached_mirror(self):
        """S4.2c — a new adapter lifespan under the same device id and the same cached mirrors."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("rebuild")
        mgmt = _make_mgmt(device, "rebuild", 60, adapter=self.adapter)
        sr = _route("10.7.0.0/16", "10.7.0.1", devices=[device])
        _own(sr, mgmt, generation=47, orphan=True)

        for _ in range(3):
            self.adapter.store.terminal_job(60, job_type="sync")
        first = consume_static_route_settlements(mgmt)
        assert first.cursor == 3
        born = self.adapter.store.incarnation
        # The cached mirrors both still match: the tab adopted this incarnation and the
        # device pointer is unchanged. Only the feed can tell the store is gone.
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(adapter_incarnation=born)

        # A REAL new lifespan: the serving process is stopped and a new one is started
        # against a rebuilt store, which recreates the device under the same numeric id and
        # restarts its settlement counter at 1.
        rebuilt = self.adapter.rebuild()
        assert rebuilt.incarnation != born
        self._point_at(self.adapter)
        rebuilt.add_device(
            nso_instance=mgmt.nso_instance.name,
            nso_device_name=mgmt.nso_device_name,
            netbox_device_id=device.pk,
            device_id=60,
        )
        rebuilt.terminal_job(60, job_type="sync")

        row = NSODeviceManagement.objects.get(pk=mgmt.pk)
        assert row.adapter_device_id == 60
        assert row.adapter_incarnation == born, "the cached mirror must still name the dead store"

        result = consume_static_route_settlements(mgmt)

        assert result.epoch_reset is True
        assert result.consumed == 1, "settlement 1 of the rebuilt store was skipped behind a dead cursor"
        after = NSODeviceManagement.objects.get(pk=mgmt.pk)
        assert after.settle_cursor_seq == 1
        assert after.settle_cursor_incarnation == rebuilt.incarnation


class TestUnresolvedExecution(_SettlementCase):
    """An unresolved ordered execution remains retained until exact consumption."""

    def _unresolvable(self, tag, adapter_device_id):
        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id, adapter=self.adapter)
        sr = _route(f"198.18.{adapter_device_id % 250}.0/24", "198.18.0.1", devices=[device])
        _own(sr, mgmt, generation=70, expected=False, orphan=True)
        self.adapter.store.terminal_job(adapter_device_id, extra=_execution_result())
        return mgmt, sr

    def test_repeated_passes_never_discard_unresolved_execution(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, _sr = self._unresolvable("retained", 70)
        for attempt in range(1, 8):
            result = consume_static_route_settlements(mgmt)
            assert result.stalled
            assert not result.drained
            row = self._cursor(mgmt)
            assert row.settle_stall_attempts == attempt
            assert row.settle_cursor_seq == 0
            assert row.settle_stall_seq == 1
        assert len(self.adapter.store.jobs) == 1

    def test_stall_state_describes_exactly_one_sequence(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, _sr = self._unresolvable("onlyone", 71)
        consume_static_route_settlements(mgmt)
        consume_static_route_settlements(mgmt)
        assert self._cursor(mgmt).settle_stall_attempts == 2
        _record_execution(mgmt, self.adapter.store, self.adapter.store.jobs[-1])
        result = consume_static_route_settlements(mgmt)
        assert result.consumed == 1
        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1
        assert row.settle_stall_seq is None
        assert row.settle_stall_attempts == 0
        assert row.settle_stall_first_seen_at is None

        self.adapter.store.terminal_job(71, extra=_execution_result())
        consume_static_route_settlements(mgmt)
        row = self._cursor(mgmt)
        assert row.settle_stall_seq == 2
        assert row.settle_stall_attempts == 1

    def test_empty_readback_does_not_replace_missing_execution_proof(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, _sr = self._unresolvable("nothing", 72)
        result = consume_static_route_settlements(mgmt)
        assert result.stalled
        assert result.consumed == 0
        assert self._cursor(mgmt).settle_cursor_seq == 0
        assert self.adapter.store.readback_requests == []

    def test_exact_consumed_or_superseded_execution_advances_once(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        for index, state in enumerate(("consumed", "superseded")):
            with self.subTest(state=state):
                mgmt, _sr = self._unresolvable(state, 75 + index)
                _record_execution(mgmt, self.adapter.store, self.adapter.store.jobs[-1], state=state)
                first = consume_static_route_settlements(mgmt)
                second = consume_static_route_settlements(mgmt)
                assert first.consumed == 1
                assert second.consumed == 0
                assert first.cursor == second.cursor == 1

    def test_mismatched_consumed_projection_cannot_advance(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, _sr = self._unresolvable("wrongprojection", 77)
        execution = _record_execution(mgmt, self.adapter.store, self.adapter.store.jobs[-1])
        projection = execution.terminal_projection
        projection["carrier_job_result"]["commit_acknowledged"] = False
        execution.terminal_projection = projection
        execution.save(update_fields=("terminal_projection",))
        result = consume_static_route_settlements(mgmt)
        assert result.stalled
        assert self._cursor(mgmt).settle_cursor_seq == 0

    def test_a_queued_sibling_never_blocks_the_cursor(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("queued")
        mgmt = _make_mgmt(device, "queued", 80, adapter=self.adapter)
        store = self.adapter.store
        store.queued_job(80, job_type="sync")
        store.terminal_job(80, job_type="sync")
        assert consume_static_route_settlements(mgmt).consumed == 1
        assert self._cursor(mgmt).settle_cursor_seq == 1
        store.terminal_job(80, job_type="sync")
        assert consume_static_route_settlements(mgmt).consumed == 1
        assert self._cursor(mgmt).settle_cursor_seq == 2


class TestMalformedFeedPreservesCursor(_SettlementCase):
    """An invalid feed page cannot certify or discard any ordered execution."""

    def _broken_feed(self, tag, adapter_device_id):
        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id, adapter=self.adapter)
        broken = self.adapter.store.unsequenced_job(adapter_device_id, job_type="sync")
        self.adapter.store.terminal_job(adapter_device_id, job_type="sync")
        return mgmt, broken

    def test_malformed_head_keeps_sequenced_jobs_unconsumed_until_repaired(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, _broken = self._broken_feed("nosequence", 91)
        result = consume_static_route_settlements(mgmt)
        assert result.stalled
        assert not result.drained
        assert result.consumed == 0
        assert self._cursor(mgmt).settle_cursor_seq is None
        self.adapter.store.unsequenced_in_feed.clear()
        result = consume_static_route_settlements(mgmt)
        assert result.drained
        assert result.consumed == 1
        assert self._cursor(mgmt).settle_cursor_seq == 1

    def test_repeated_malformed_pages_never_advance_or_invent_stall_identity(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, _broken = self._broken_feed("noseqagain", 92)
        for _ in range(7):
            result = consume_static_route_settlements(mgmt)
            assert result.stalled
            assert not result.drained
            row = self._cursor(mgmt)
            assert row.settle_cursor_seq is None
            assert row.settle_stall_seq is None
            assert row.settle_stall_attempts == 0

    def test_malformed_page_preserves_existing_unresolved_sequence(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("noseqexisting")
        mgmt = _make_mgmt(device, "noseqexisting", 93, adapter=self.adapter)
        self.adapter.store.terminal_job(93, extra=_execution_result())
        consume_static_route_settlements(mgmt)
        consume_static_route_settlements(mgmt)
        before = self._cursor(mgmt)
        self.adapter.store.unsequenced_job(93, job_type="sync")
        for _ in range(7):
            result = consume_static_route_settlements(mgmt)
            assert result.stalled
            after = self._cursor(mgmt)
            assert after.settle_cursor_seq == before.settle_cursor_seq == 0
            assert after.settle_stall_seq == before.settle_stall_seq == 1
            assert after.settle_stall_attempts == before.settle_stall_attempts == 2
            assert after.settle_stall_first_seen_at == before.settle_stall_first_seen_at
        self.adapter.store.unsequenced_in_feed.clear()
        consume_static_route_settlements(mgmt)
        assert self._cursor(mgmt).settle_stall_attempts == 3


class TestStaleFullSaveCannotRewindTheCursor(_SettlementCase):
    """A holder of a pre-advance instance may not carry the cursor or unresolved evidence backwards.

    ``.update()`` protects the consumer's write from being *clobbered on the way out*; it
    does nothing about a full ``save()`` that lands afterwards from an instance loaded
    before it. Several sweeps load a management row, make an adapter call, and only then
    save — the window is real and it is where the row's other durable records already had
    to be protected. Monotonicity and unresolved execution retention are worth exactly as much as
    that protection covers them.
    """

    def _advanced(self, tag, adapter_device_id):
        """A device whose cursor has been advanced by the consumer, plus a stale holder."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id, adapter=self.adapter)
        sr = _route(f"10.12.{adapter_device_id % 250}.0/24", "10.12.0.1", devices=[device])
        _own(sr, mgmt, generation=80, orphan=True)
        self.adapter.store.terminal_job(adapter_device_id, job_type="sync")

        # The holder loads the row BEFORE the consumer runs, exactly as a sweep does.
        stale = NSODeviceManagement.objects.get(pk=mgmt.pk)
        assert stale.settle_cursor_seq is None

        result = consume_static_route_settlements(mgmt)
        assert result.consumed == 1
        return mgmt, stale, sr

    def test_a_stale_full_save_cannot_rewind_the_cursor(self):
        mgmt, stale, _sr = self._advanced("rewind", 110)
        incarnation = self._cursor(mgmt).settle_cursor_incarnation

        stale.save()

        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1, "a stale full save rewound the cursor"
        assert row.settle_cursor_incarnation == incarnation, "a stale full save rewound the epoch"
        assert row.settle_cursor_device_id == 110

    def test_a_stale_full_save_cannot_reset_unresolved_execution_tracking(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("bound2")
        mgmt = _make_mgmt(device, "bound2", 111, adapter=self.adapter)
        sr = _route("10.13.0.0/16", "10.13.0.1", devices=[device])
        _own(sr, mgmt, generation=81, expected=False, orphan=True)
        self.adapter.store.terminal_job(111, extra=_execution_result())

        stale = NSODeviceManagement.objects.get(pk=mgmt.pk)
        for _ in range(3):
            consume_static_route_settlements(mgmt)
        assert self._cursor(mgmt).settle_stall_attempts == 3

        stale.save()

        row = self._cursor(mgmt)
        assert row.settle_stall_attempts == 3, "a stale full save reset unresolved execution tracking"
        assert row.settle_stall_seq == 1
        assert row.settle_stall_first_seen_at is not None

    def test_the_link_repairs_save_cannot_rewind_the_cursor(self):
        """The named production holder: load the row, call the adapter, then save it."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        device = _make_device("repairsave")
        mgmt = _make_mgmt(device, "repairsave", 112, adapter=self.adapter)
        sr = _route("10.14.0.0/16", "10.14.0.1", devices=[device])
        _own(sr, mgmt, generation=82, orphan=True)
        self.adapter.store.terminal_job(112, job_type="sync")

        # The sweep materializes its candidate list first; the adapter has no device 112,
        # so this row is broken and will be re-saved once the snapshot comes back.
        rows = list(NSODeviceManagement.objects.filter(pk=mgmt.pk))

        assert consume_static_route_settlements(mgmt).consumed == 1
        assert self._cursor(mgmt).settle_cursor_seq == 1

        reconcile_device_links(rows)

        assert self._cursor(mgmt).settle_cursor_seq == 1, "the link repair's save rewound the cursor"

    def test_the_refresh_covers_every_field_the_consumer_writes(self):
        """Asserted explicitly: a settlement column added later cannot miss the refresh."""
        from netbox_nso_plugin.models import NSODeviceManagement

        # Derived from the model, not written out: a hand-written list is exactly what a new
        # settle_* column would be left out of.
        written = {f.name for f in NSODeviceManagement._meta.concrete_fields if f.name.startswith("settle_")}
        written.add("adapter_link_attempted_at")
        protected = set(NSODeviceManagement._STALE_SAVE_PROTECTED_FIELDS)
        assert written <= protected, f"unprotected against a stale full save: {sorted(written - protected)}"


def _manage_py_dir():
    """The directory the NetBox ``manage.py`` lives in, taken from the installed package."""
    import netbox

    root = pathlib.Path(netbox.__file__).resolve().parents[1]
    # No skip on absence: this pin is the only durable-stall evidence, so it must fail loud.
    assert (root / "manage.py").is_file(), f"no manage.py under {root} — the durable-stall pin cannot run"
    return root


class TestDurableStallAcrossProcesses(_SettlementCase):
    """Unresolved evidence survives a real process boundary."""

    def _run_consumer(self, device_id: int, passes: int):
        # The child names the test database directly, under the standard settings: the isolated
        # harness refuses to build a settings module whose live NAME is already the test name.
        env = dict(os.environ)
        env.pop("TEST_DB_NAME", None)
        env["DB_NAME"] = connection.settings_dict["NAME"]
        env["DJANGO_SETTINGS_MODULE"] = "netbox.settings"
        return subprocess.run(  # noqa: S603 — a fixed argv, no shell
            [
                sys.executable,
                "manage.py",
                "nso_consume_static_route_settlements",
                "--device",
                str(device_id),
                "--passes",
                str(passes),
            ],
            cwd=_manage_py_dir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_unresolved_evidence_survives_repeated_passes_and_real_restart(self):
        device = _make_device("proc")
        mgmt = _make_mgmt(device, "proc", 90, adapter=self.adapter)
        self.adapter.store.terminal_job(90, extra=_execution_result())
        first = self._run_consumer(device.pk, passes=6)
        assert first.returncode == 0, first.stderr
        before = self._cursor(mgmt)
        assert before.settle_stall_attempts == 6
        assert before.settle_cursor_seq == 0
        assert before.settle_stall_seq == 1

        second = self._run_consumer(device.pk, passes=2)
        assert second.returncode == 0, second.stderr
        after = self._cursor(mgmt)
        assert after.settle_cursor_seq == 0
        assert after.settle_stall_seq == 1
        assert after.settle_stall_attempts == 8
        assert after.settle_stall_first_seen_at == before.settle_stall_first_seen_at
        assert len(self.adapter.store.jobs) == 1


class TestTheConsumeCommandReportsTheFleet(_SettlementCase):
    """The operator's drain tool names every device it walked, on one candidate query."""

    def test_the_command_names_each_device_without_a_query_per_row(self):
        from io import StringIO

        from django.core.management import call_command
        from django.test.utils import CaptureQueriesContext

        for index, adapter_device_id in enumerate((120, 121, 122)):
            tag = f"cmd{index}"
            device = _make_device(tag)
            mgmt = _make_mgmt(device, tag, adapter_device_id, adapter=self.adapter)
            sr = _route(f"10.16.{index}.0/24", f"10.16.{index}.1", devices=[device])
            _own(sr, mgmt, generation=90 + index, orphan=True)
            self.adapter.store.terminal_job(adapter_device_id, job_type="sync")

        out = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("nso_consume_static_route_settlements", stdout=out)

        assert out.getvalue().count("consumed 1") == 3, out.getvalue()
        per_row = [query["sql"] for query in queries.captured_queries if 'FROM "dcim_device"' in query["sql"]]
        assert per_row == [], f"the command read each row's device one at a time: {per_row}"
