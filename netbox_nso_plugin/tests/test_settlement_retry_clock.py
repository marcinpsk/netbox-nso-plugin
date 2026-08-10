# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S5) — the independent retry clock, and the fairness it needs.

Pins S5.6, S5.6b and S5.6d. The carrier and this tick share one consumer implementation
and nothing else: this tick runs plugin-to-adapter, so it survives the one failure the
callback channel cannot — an invalid adapter-to-NetBox token answers 401 on every
notification while the plugin's own reads stay healthy. A retry that rides the channel it
retries is not a retry, which is why no test here sends a second callback.

The sweep also depends on the link repair that runs before it, so two of its properties are
pinned here rather than in the sync-cache suite: a device repaired in this tick is settled
on its **new** adapter id in the **same** tick, and the repair's per-run cap rotates rather
than starving whatever sits behind a permanently broken head.
"""

from __future__ import annotations

from unittest.mock import patch

from ._settlement_case import (
    _CarrierCase,
    _make_device,
    _make_mgmt,
    _own,
    _result,
    _route,
    _SettlementCase,
    _stale_clock,
)


class TestTheScheduledTickIsAnIndependentClock(_CarrierCase):
    """S5.6 — the tick settles a row the dead callback channel can no longer settle."""

    def test_the_scheduled_tick_settles_without_any_callback(self):
        device = _make_device("clock")
        mgmt = _make_mgmt(device, "clock", 10)
        sr = _route("10.40.0.0/16", "10.40.0.1", devices=[device])
        state = _own(sr, mgmt, generation=201)
        self.adapter.store.add_device(
            nso_instance="se-clock-inst", nso_device_name="nso-se-clock", netbox_device_id=device.pk, device_id=10
        )
        self.adapter.store.terminal_job(10, results=[_result(sr.pk, 201)])

        # One consumer failure through the real carrier (S5.5(b)).
        self.adapter.store.feed_error_devices.add(10)
        self._notify(device.pk)
        self._drain()
        state.refresh_from_db()
        assert state.status == "deploying", "the failure arm did not leave the row waiting"

        # The channel that would have retried it now answers 401 on every call: no further
        # notification of any kind reaches the plugin.
        self.adapter.store.feed_error_devices.discard(10)
        with patch(
            "netbox_nso_plugin.reconcile.enqueue_device_reconcile",
            side_effect=AssertionError("the pin fired a second callback — the very channel it removes"),
        ):
            self._tick()

        state.refresh_from_db()
        assert state.status == "in_sync", "the row is stranded behind a channel that is never coming back"

    def test_the_sweep_is_bounded_and_isolated(self):
        """A quiet device is never polled, and one device's adapter cannot abort the tick."""
        busy = _make_device("busy")
        quiet = _make_device("quiet")
        broken = _make_device("brokenfeed")
        busy_mgmt = _make_mgmt(busy, "busy", 11)
        quiet_mgmt = _make_mgmt(quiet, "quiet", 12)
        broken_mgmt = _make_mgmt(broken, "brokenfeed", 13)
        for mgmt, device, tag, adapter_id in (
            (busy_mgmt, busy, "busy", 11),
            (quiet_mgmt, quiet, "quiet", 12),
            (broken_mgmt, broken, "brokenfeed", 13),
        ):
            row = self.adapter.store.add_device(
                nso_instance=f"se-{tag}-inst",
                nso_device_name=f"nso-se-{tag}",
                netbox_device_id=device.pk,
                device_id=adapter_id,
            )
            # A status only the mirror pass can put on the management row.
            row["last_sync_status"] = "succeeded"

        busy_route = _route("10.41.0.0/16", "10.41.0.1", devices=[busy])
        busy_state = _own(busy_route, busy_mgmt, generation=202)
        self.adapter.store.terminal_job(11, results=[_result(busy_route.pk, 202)])
        # The quiet device's only overlay is already terminal: nothing is owed.
        quiet_route = _route("10.42.0.0/16", "10.42.0.1", devices=[quiet])
        _own(quiet_route, quiet_mgmt, generation=203, status="in_sync")
        # The broken one is owed a settlement its adapter cannot serve.
        broken_route = _route("10.43.0.0/16", "10.43.0.1", devices=[broken])
        _own(broken_route, broken_mgmt, generation=204)
        self.adapter.store.feed_error_devices.add(13)

        self._tick()

        polled = {request[0] for request in self.adapter.store.feed_requests}
        assert 12 not in polled, "a device with nothing pending was polled anyway"
        assert {11, 13} <= polled
        busy_state.refresh_from_db()
        assert busy_state.status == "in_sync", "one device's adapter error aborted another's sweep"
        # The mirror pass ran for every row, including the one whose settlement failed.
        broken_mgmt.refresh_from_db()
        assert broken_mgmt.last_sync_status == "succeeded", (
            "the mirror never reached the device whose settlement failed, so the tick's first "
            "pass is hostage to its last"
        )


class TestTheSameTickSettlesARepairedDevice(_SettlementCase):
    """S5.6b — a mapping repaired in this tick is settled on its NEW id, in this tick.

    Ordering the sweep after the repair is not enough on its own: the repair writes the
    database and leaves the caller's row object holding ``None`` (reused) or the dead id
    (missing), and the real re-onboard runs in an ``on_commit`` callback that re-fetches by
    pk. ``_MOVED``, which does mutate the caller's object, is the control.
    """

    def _settle_after_repair(self, tag, octet, stored_id, expected_id, *, seed):
        """Run one tick over a device whose mapping *seed* has broken, and assert both halves."""
        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, stored_id)
        sr = _route(f"10.44.{octet}.0/24", f"10.44.{octet}.1", devices=[device])
        state = _own(sr, mgmt, generation=205)
        seed(device)
        # The settlement waits on the id the repair will produce, so a sweep that polled the
        # stale one would find nothing at all.
        self.adapter.store.terminal_job(expected_id, results=[_result(sr.pk, 205)])

        self._tick()

        mgmt.refresh_from_db()
        state.refresh_from_db()
        assert mgmt.adapter_device_id == expected_id, "the link repair did not run"
        assert self.adapter.store.feed_requests, "the sweep skipped the repaired device entirely"
        # Every request, not just the last: a stale id anywhere in the pass is the defect.
        assert {r[0] for r in self.adapter.store.feed_requests} == {expected_id}, (
            f"the feed was requested with a stale id: {self.adapter.store.feed_requests}"
        )
        assert state.status == "in_sync", "the repaired device waits another five minutes to settle"

    def test_the_same_tick_settles_a_repaired_device_moved(self):
        """Control: `_MOVED` adopts the new id ON the caller's object, so it cannot regress."""

        def seed(device):
            # Our node is present under a different id — the adapter row moved.
            self.adapter.store.add_device(
                nso_instance="se-moved-inst",
                nso_device_name="nso-se-moved",
                netbox_device_id=device.pk,
                device_id=101,
            )

        self._settle_after_repair("moved", 1, 100, 101, seed=seed)

    def test_the_same_tick_settles_a_repaired_device_reused(self):
        """`_REUSED` blanks the caller's id, so a sweep over the stale list would SKIP it."""

        def seed(_device):
            # Our stored id belongs to somebody else, and our node is nowhere: the pointer is
            # dropped and the re-onboard mints the next id, 201.
            self.adapter.store.add_device(
                nso_instance="other-inst", nso_device_name="other-node", netbox_device_id=None, device_id=200
            )

        self._settle_after_repair("reused", 2, 200, 201, seed=seed)

    def test_the_same_tick_settles_a_repaired_device_reonboard(self):
        """`_MISSING` leaves the DEAD id on the caller's object, so a stale sweep would poll it."""

        def seed(_device):
            """Nothing in the adapter at all: the scope push 404s and the re-onboard mints id 1."""

        self._settle_after_repair("reonboard", 3, 300, 1, seed=seed)


class TestTheRepairCapRotates(_SettlementCase):
    """S5.6d — a bounded loop over a fleet needs a durable least-recently-attempted order."""

    def _broken_row(self, index):
        """A management row pointing at an adapter device that does not exist."""
        device = _make_device(f"starve{index}")
        return device, _make_mgmt(device, f"starve{index}", 900 + index)

    def test_a_failing_head_cannot_starve_a_repairable_tail_row(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.sync_cache import MAX_RELINKS_PER_RUN

        rows = [self._broken_row(i) for i in range(MAX_RELINKS_PER_RUN + 1)]
        tail_device, tail_mgmt = rows[-1]
        sr = _route("10.45.0.0/16", "10.45.0.1", devices=[tail_device])
        state = _own(sr, tail_mgmt, generation=206)
        # The tail's repair mints adapter id 1 (nothing else is registered), and its
        # settlement is already waiting there.
        self.adapter.store.terminal_job(1, results=[_result(sr.pk, 206)])

        doomed = {mgmt.nso_device_name for _device, mgmt in rows[:-1]}
        real_onboard = adapter_client.onboard_device

        def onboard(nso_instance, nso_device_name, netbox_device_id):
            if nso_device_name in doomed:
                # The failure the on_commit callback swallows, so the attempt still counts.
                raise AdapterError("NSO refuses this node", code="nso_error")
            return real_onboard(nso_instance, nso_device_name, netbox_device_id)

        with patch("netbox_nso_plugin.adapter_client.onboard_device", side_effect=onboard):
            self._tick()
            tail_mgmt.refresh_from_db()
            assert tail_mgmt.adapter_link_attempted_at is None, "the setup did not put the tail behind the cap"
            state.refresh_from_db()
            assert state.status == "deploying"

            self._tick()

        tail_mgmt.refresh_from_db()
        state.refresh_from_db()
        assert tail_mgmt.adapter_link_attempted_at is not None, "a permanently failing head held the cap forever"
        assert tail_mgmt.adapter_device_id == 1
        assert state.status == "in_sync", "the starved row's settlement never used a live id"

    def test_repair_convergence_is_ceil_b_over_c_ticks(self):
        """The weaker case, which the fairness fix is NOT needed for — and so cannot prove."""
        from netbox_nso_plugin.sync_cache import MAX_RELINKS_PER_RUN

        count = 2 * MAX_RELINKS_PER_RUN + 1
        rows = [self._broken_row(i) for i in range(count)]
        _tail_device, tail_mgmt = rows[-1]

        self._tick()
        tail_mgmt.refresh_from_db()
        assert tail_mgmt.adapter_link_attempted_at is None, "the cap did not bound the first tick"

        self._tick()
        tail_mgmt.refresh_from_db()
        assert tail_mgmt.adapter_link_attempted_at is None, "the cap did not bound the second tick"

        self._tick()
        tail_mgmt.refresh_from_db()
        assert tail_mgmt.adapter_link_attempted_at is not None, "convergence is slower than ceil(B / C) ticks"


class TestTheClockAlsoEscalates(_CarrierCase):
    """Codex S5 P1 — a clock that only consumes is half a clock.

    With the callback channel dead, the tick is the only thing left running. It walks the
    feed, bounds an unresolvable result and advances past it on the fifth attempt — and then
    every later page is empty. If the timeout backstop rides only the carrier, that row is
    ``deploying`` for good: the same shared-failure-domain trap the tick exists to break,
    one level down.
    """

    def test_the_tick_escalates_a_row_whose_result_never_resolved(self):
        from netbox_nso_plugin.settlement import SETTLE_STALL_MAX_ATTEMPTS

        device = _make_device("noresolve")
        mgmt = _make_mgmt(device, "noresolve", 15)
        self.adapter.store.add_device(
            nso_instance="se-noresolve-inst",
            nso_device_name="nso-se-noresolve",
            netbox_device_id=device.pk,
            device_id=15,
        )
        sr = _route("10.46.0.0/16", "10.46.0.1", devices=[device])
        state = _own(sr, mgmt, generation=207, expected=False)
        _stale_clock(state)
        self.adapter.store.terminal_job(15, results=[_result(sr.pk, 207)])
        self.adapter.store.intent_status = 503  # this result can never be correlated

        # No callback of any kind: the tick is the only clock running.
        with patch(
            "netbox_nso_plugin.reconcile.enqueue_device_reconcile",
            side_effect=AssertionError("the pin fired a callback — the very channel this removes"),
        ):
            for tick in range(SETTLE_STALL_MAX_ATTEMPTS):
                self._tick()
                state.refresh_from_db()
                if tick < SETTLE_STALL_MAX_ATTEMPTS - 1:
                    assert state.status == "deploying", "the bound was short-circuited before attempt five"

        state.refresh_from_db()
        assert self._cursor(mgmt).settle_cursor_seq == 1, "the stall bound never released the cursor"
        assert state.status == "apply_failed", (
            "the row is stranded deploying forever: the independent clock consumed but never escalated"
        )

    def test_the_tick_does_not_escalate_while_an_apply_is_in_flight(self):
        """The clock the carrier had, which the tick must not be missing.

        ``_prepare_apply`` promotes a row to ``deploying`` without re-stamping its generation
        clock, so a route staged long before its Apply looks stuck the instant that Apply
        starts. Failing it there is unrecoverable: the apply's own ``in_sync`` cannot lift a
        row back out of ``apply_failed``.
        """
        device = _make_device("inflighttick")
        mgmt = _make_mgmt(device, "inflighttick", 17)
        self.adapter.store.add_device(
            nso_instance="se-inflighttick-inst",
            nso_device_name="nso-se-inflighttick",
            netbox_device_id=device.pk,
            device_id=17,
        )
        sr = _route("10.48.0.0/16", "10.48.0.1", devices=[device])
        state = _own(sr, mgmt, generation=209)
        _stale_clock(state)
        self.adapter.store.queued_job(17)  # the Apply that just re-marked this row

        self._tick()

        state.refresh_from_db()
        assert state.status == "deploying", "the clock failed a row the running apply is about to settle"


class TestTheSweepStandsDownOnAGlobalOutage(_SettlementCase):
    """Codex S5 P2 — per-device isolation is the wrong tool for a hung adapter.

    The tick's shared snapshot already proves whether the adapter answers at all. When it
    does not, polling every candidate in turn buys nothing and each one waits out the full
    read timeout, so a fleet can hold a five-minute job for the best part of an hour.
    """

    def test_a_failed_snapshot_skips_the_per_device_polling(self):
        device = _make_device("hung")
        mgmt = _make_mgmt(device, "hung", 16)
        sr = _route("10.47.0.0/16", "10.47.0.1", devices=[device])
        _own(sr, mgmt, generation=208)
        self.adapter.store.terminal_job(16, results=[_result(sr.pk, 208)])
        self.adapter.store.devices_status = 503  # the shared snapshot proves a global outage

        self._tick()

        assert self.adapter.store.feed_requests == [], (
            "the sweep polled every candidate after the adapter had already been proven hung: "
            f"{self.adapter.store.feed_requests}"
        )
