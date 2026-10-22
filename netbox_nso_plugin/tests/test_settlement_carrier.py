# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Drive durable execution cursor consumption through the real callback and RQ worker."""

from __future__ import annotations

import threading
from unittest.mock import patch

from django.db import connections

from ._settlement_case import (
    _CarrierCase,
    _execution_result,
    _make_device,
    _make_mgmt,
    _own,
    _record_execution,
    _result,
    _route,
    _stale_clock,
)


def _repair_before_the_lock(mgmt_pk: int, new_id: int):
    """Commit a link repair to *new_id* in the window Step 4 leaves open.

    Anchored on the consumer's entry point: Step 4 has handed over its cached ``mgmt`` object
    and read whatever it reads, and the consumer has not taken its lock yet.
    """
    from netbox_nso_plugin import settlement
    from netbox_nso_plugin.models import NSODeviceManagement

    real_settle = settlement.settle_static_routes
    repaired = []

    def barrier(passed_mgmt, **kwargs):
        if not repaired:
            repaired.append(True)

            def other_connection():
                try:
                    NSODeviceManagement.objects.filter(pk=mgmt_pk).update(adapter_device_id=new_id)
                finally:
                    connections.close_all()

            thread = threading.Thread(target=other_connection)
            thread.start()
            thread.join(timeout=30)
            assert not thread.is_alive(), "the repair never committed, so the barrier proves nothing"
        return real_settle(passed_mgmt, **kwargs)

    return patch.object(settlement, "settle_static_routes", barrier)


class TestTheCarrier(_CarrierCase):
    """S5.4 — the adapter's notification reaches the consumer, through production wiring only."""

    def test_a_consumed_execution_advances_the_cursor_through_the_real_notify_path(self):
        device = _make_device("carry")
        mgmt = _make_mgmt(device, "carry", 10, adapter=self.adapter)
        sr = _route("10.30.0.0/16", "10.30.0.1", devices=[device])
        state = _own(sr, mgmt, generation=101)
        job = self.adapter.store.terminal_job(10, extra=_execution_result())
        _record_execution(mgmt, self.adapter.store, job)

        response = self._notify(device.pk)
        assert response.status_code == 202, response.data

        state.refresh_from_db()
        assert state.status == "deploying", "the 202 alone settled the row — queued is not run"
        assert self.adapter.store.feed_requests == [], "the feed was walked before any worker ran"

        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying", "cursor consumption cannot independently settle an overlay"
        assert self.adapter.store.feed_requests, "Step 4 never reached the consumer"
        assert self._cursor(mgmt).settle_cursor_seq == 1

    def test_a_raw_route_success_stalls_without_certifying_an_overlay(self):
        device = _make_device("raw-carry")
        mgmt = _make_mgmt(device, "raw-carry", 12, adapter=self.adapter)
        route = _route("198.18.12.0/24", "198.18.12.1", devices=[device])
        state = _own(route, mgmt, generation=105)
        self.adapter.store.terminal_job(12, results=[_result(route.pk, 105)])

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying"
        assert self._cursor(mgmt).settle_cursor_seq == 0
        assert self._cursor(mgmt).settle_stall_attempts == 1


class TestOrderingAndIsolation(_CarrierCase):
    """Consume known executions while preserving pending intent on feed failure."""

    def test_the_consumer_advances_a_consumed_execution_without_judging_aged_intent(self):
        device = _make_device("order")
        mgmt = _make_mgmt(device, "order", 20, adapter=self.adapter)
        sr = _route("10.31.0.0/16", "10.31.0.1", devices=[device])
        state = _own(sr, mgmt, generation=102)
        _stale_clock(state)  # Age alone is not execution evidence.
        job = self.adapter.store.terminal_job(20, extra=_execution_result())
        _record_execution(mgmt, self.adapter.store, job)

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying", "the cursor or age clock certified an overlay without its own outcome"
        assert state.last_apply_error == ""
        assert self._cursor(mgmt).settle_cursor_seq == 1

    def test_a_consumer_error_leaves_the_static_row_waiting(self):
        """(b) Fail closed: a static row waits when its exact-result feed fails."""
        device = _make_device("iso")
        mgmt = _make_mgmt(device, "iso", 21, adapter=self.adapter)
        sr = _route("10.32.0.0/16", "10.32.0.1", devices=[device])
        state = _own(sr, mgmt, generation=103)
        _stale_clock(state)
        self.adapter.store.terminal_job(
            21,
            results=[_result(sr.pk, 103)],
        )
        # Only the ASCENDING page fails, which is exactly the settlement request.
        self.adapter.store.feed_error_devices.add(21)

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying", "the static row was judged on an unconsumed feed"
        assert self._cursor(mgmt).settle_cursor_seq is None


class TestTheConsumerReadsTheLockedRow(_CarrierCase):
    """S5.6c — a carrier holding a cached row while a concurrent repair commits a new id."""

    def test_the_consumer_reads_its_device_id_from_the_locked_row(self):
        """The FIRST feed request must carry the id on the row, not the id on the argument."""
        device = _make_device("stale")
        mgmt = _make_mgmt(device, "stale", 10, adapter=self.adapter)
        sr = _route("10.33.0.0/16", "10.33.0.1", devices=[device])
        state = _own(sr, mgmt, generation=104)
        # The settlement lives on the REPAIRED device. Device 10 has nothing to serve, so an
        # end-state assertion alone could pass for the wrong reason — hence the outbound one.
        self.adapter.store.add_device(
            nso_instance=mgmt.nso_instance.name,
            nso_device_name=mgmt.nso_device_name,
            netbox_device_id=device.pk,
            device_id=11,
        )
        self.adapter.store.terminal_job(11, job_type="sync")

        # Anchored on the consumer's entry point, which is the contract Step 4 calls: Step 4
        # has handed over its `mgmt` object and the consumer has not taken its lock yet.
        with _repair_before_the_lock(mgmt.pk, 11):
            self._notify(device.pk)
            self._drain()

        assert self.adapter.store.feed_requests, "the consumer was never reached"
        assert self.adapter.store.feed_requests[0][0] == 11, (
            "the first feed request used the caller's cached adapter device id, "
            f"not the one on the row it locked: {self.adapter.store.feed_requests}"
        )
        state.refresh_from_db()
        assert state.status == "deploying"
        assert self._cursor(mgmt).settle_cursor_seq == 1


class TestTheApplyProbeNamesTheLockedDevice(_CarrierCase):
    """Keep mapping repair and activity probes aligned with the locked adapter identity."""

    def test_a_probe_read_for_another_device_does_not_fail_routes_mid_apply(self):
        device = _make_device("probe")
        mgmt = _make_mgmt(device, "probe", 60, adapter=self.adapter)
        sr = _route("10.38.0.0/16", "10.38.0.1", devices=[device])
        state = _own(sr, mgmt, generation=120)
        _stale_clock(state)
        # Device 60 is idle; the repaired device 61 has an apply in flight.
        self.adapter.store.add_device(
            nso_instance="se-probe-repaired-inst",
            nso_device_name="nso-se-probe-repaired",
            device_id=61,
        )
        self.adapter.store.queued_job(61)

        with _repair_before_the_lock(mgmt.pk, 61):
            self._notify(device.pk)
            self._drain()

        assert self.adapter.store.feed_requests[0][0] == 61, "the repair did not land before the lock"
        state.refresh_from_db()
        assert state.status == "deploying", (
            "the backstop stood on an apply probe read for adapter device 60 and failed a route "
            "on 61, where an apply is in flight"
        )

    def test_a_repaired_device_does_not_judge_an_orphan_attempt(self):
        """A repaired link does not make an orphan Apply UUID actionable."""
        device = _make_device("idle")
        mgmt = _make_mgmt(device, "idle", 62, adapter=self.adapter)
        # The generation probe must succeed. It 404s for a device the store does not hold, and an
        # unknown result stands the backstop down, which removes this test's premise.
        self.adapter.store.add_device(nso_instance="se-idle-inst", nso_device_name="nso-se-idle", device_id=63)
        sr = _route("10.39.0.0/16", "10.39.0.1", devices=[device])
        state = _own(sr, mgmt, generation=121, orphan=True)
        _stale_clock(state)

        with _repair_before_the_lock(mgmt.pk, 63):
            self._notify(device.pk)
            self._drain()

        assert self.adapter.store.feed_requests[0][0] == 63, "the repair did not land before the lock"
        state.refresh_from_db()
        assert state.status == "deploying"


class TestIncompleteFeedPreservesIntent(_CarrierCase):
    """Keep an unresolved head or unfinished page from certifying pending intent."""

    def test_a_stalled_walk_does_not_let_the_backstop_judge(self):
        device = _make_device("stall")
        mgmt = _make_mgmt(device, "stall", 40, adapter=self.adapter)
        sr = _route("10.34.0.0/16", "10.34.0.1", devices=[device])
        state = _own(sr, mgmt, generation=110, expected=False)
        _stale_clock(state)
        self.adapter.store.terminal_job(40, results=[_result(sr.pk, 110)])

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying", "an unresolved execution or a read-back outage judged pending intent"
        assert self._cursor(mgmt).settle_stall_attempts == 1

    def test_an_unfinished_page_does_not_let_the_backstop_judge(self):
        from netbox_nso_plugin.settlement import SETTLE_FEED_PAGE

        device = _make_device("page")
        mgmt = _make_mgmt(device, "page", 41, adapter=self.adapter)
        sr = _route("10.35.0.0/16", "10.35.0.1", devices=[device])
        state = _own(sr, mgmt, generation=111)
        _stale_clock(state)
        # A full first page of jobs that decide nothing, then THIS row's result behind it.
        for _ in range(SETTLE_FEED_PAGE):
            self.adapter.store.terminal_job(41, job_type="sync")
        self.adapter.store.terminal_job(41, results=[_result(sr.pk, 111)])

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying", "the row was failed while its own result sat on page two"
        assert self._cursor(mgmt).settle_cursor_seq == SETTLE_FEED_PAGE


class TestCursorConsumptionPushesNoIntent(_CarrierCase):
    """Keep cursor bookkeeping from publishing another static-route intent."""

    def test_an_orphan_attempt_sends_no_static_route_intent(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.signals import reset_intent_push_state

        device = _make_device("nopush")
        mgmt = _make_mgmt(device, "nopush", 42, adapter=self.adapter)
        self.adapter.store.add_device(nso_instance="se-nopush-inst", nso_device_name="nso-se-nopush", device_id=42)
        sr = _route("10.36.0.0/16", "10.36.0.1", devices=[device])
        state = _own(sr, mgmt, generation=112, orphan=True)
        _stale_clock(state)
        # An empty feed carries no outcome that could change this orphan intent.
        # The worker that runs this has never pushed for this device, so its change-detection
        # cache is empty. Building the fixture in-process warms that cache, and the payload
        # is status-free — deploying and apply_failed produce byte-identical intent — so a
        # warm cache would swallow the very push this pin is about.
        reset_intent_push_state()

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying"
        pushes = self._intent_puts()
        assert pushes == [], f"cursor processing re-pushed this device's static-route intent: {pushes}"
        # Positive control: the same filter DOES see a push issued through the client the
        # production path uses, so the empty list above is evidence and not a URL template
        # the filter stopped matching.
        adapter_client.put_static_route_intent(42, [])
        assert self._intent_puts(), "the filter cannot see a static-route push at all"

    def _intent_puts(self):
        """Every recorded static-route intent PUT, however the client spells the path."""
        return [
            (method, path)
            for method, path in self.adapter.store.requests
            if method == "PUT" and path.endswith("/static-route-intent")
        ]


class TestRawRouteResultsRemainUnconsumed(_CarrierCase):
    """Require structured execution authority before publishing lifecycle or advisory changes."""

    def test_a_raw_route_result_cannot_publish_an_advisory_or_judge_aged_intent(self):
        device = _make_device("unpr")
        mgmt = _make_mgmt(device, "unpr", 43, adapter=self.adapter)
        sr = _route("10.37.0.0/16", "10.37.0.1", devices=[device])
        state = _own(sr, mgmt, generation=113)
        _stale_clock(state)
        self.adapter.store.terminal_job(
            43,
            results=[
                _result(
                    sr.pk,
                    113,
                    outcome="unproven",
                    error={"code": "verify_unavailable", "message": "the device refused the verify read"},
                )
            ],
        )

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.last_result_advisory == ""
        assert state.status == "deploying", (
            "a raw route result or the age clock judged intent without structured execution evidence"
        )
        assert state.last_apply_error == ""
        assert self._cursor(mgmt).settle_cursor_seq == 0
        assert self._cursor(mgmt).settle_stall_attempts == 1
