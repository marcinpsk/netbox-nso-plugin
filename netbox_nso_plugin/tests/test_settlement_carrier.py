# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S5) — the production carrier for the settlement consumer.

Pins S5.4, S5.5 and S5.6c. A consumer nothing calls is dead code that looks alive, and
Appendix S removes the channel that used to settle static routes — so every pin here starts
at the **adapter's own callback endpoint** and reaches Step 4 through the real queued-carrier
arbiter, a real async RQ queue and a real worker. None of them calls the consumer,
``run_device_reconcile`` or the management command, and none of them asserts on the 202:
queued is not run, and CI starts Redis with no worker.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from django.db import connections

from ._settlement_case import _CarrierCase, _make_device, _make_mgmt, _own, _result, _route, _stale_clock


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

    def test_a_settlement_is_consumed_through_the_real_notify_path(self):
        device = _make_device("carry")
        mgmt = _make_mgmt(device, "carry", 10)
        sr = _route("10.30.0.0/16", "10.30.0.1", devices=[device])
        state = _own(sr, mgmt, generation=101)
        self.adapter.store.terminal_job(10, results=[_result(sr.pk, 101)])

        response = self._notify(device.pk)
        assert response.status_code == 202, response.data

        state.refresh_from_db()
        assert state.status == "deploying", "the 202 alone settled the row — queued is not run"
        assert self.adapter.store.feed_requests == [], "the feed was walked before any worker ran"

        self._drain()

        state.refresh_from_db()
        assert state.status == "in_sync", "nothing in production consumes the settlement feed"
        assert self.adapter.store.feed_requests, "Step 4 never reached the consumer"
        assert self._cursor(mgmt).settle_cursor_seq == 1


class TestOrderingAndIsolation(_CarrierCase):
    """S5.5 — the consumer runs before the backstop, and its failure costs only the backstop."""

    def test_the_consumer_precedes_the_backstop_in_one_invocation(self):
        """(a) A settlement one page away must not be pre-empted by the row's own age."""
        device = _make_device("order")
        mgmt = _make_mgmt(device, "order", 20)
        sr = _route("10.31.0.0/16", "10.31.0.1", devices=[device])
        state = _own(sr, mgmt, generation=102)
        _stale_clock(state)  # well past the grace: the backstop would fire if it ran first
        self.adapter.store.terminal_job(20, results=[_result(sr.pk, 102)])

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "in_sync", "the backstop judged a row whose settlement was unconsumed"
        assert state.last_apply_error == ""

    def test_a_consumer_error_suppresses_only_the_static_backstop(self):
        """(b) Fail closed and narrow: the other scopes settle, the static row waits."""
        from netbox_nso_plugin.models import NSOLoggingLevelState

        device = _make_device("iso")
        mgmt = _make_mgmt(device, "iso", 21)
        sr = _route("10.32.0.0/16", "10.32.0.1", devices=[device])
        state = _own(sr, mgmt, generation=103)
        _stale_clock(state)
        other_scope = NSOLoggingLevelState.objects.create(
            management=mgmt, console_severity="warning", status="deploying"
        )
        # One job carrying both channels' evidence: the per-route settlement the consumer
        # would read, and the per-scope counter the coarse settle reads.
        self.adapter.store.terminal_job(
            21,
            results=[_result(sr.pk, 103)],
            extra={"logging_count_by_outcome": {"apply_failed": 1}},
        )
        # Only the ASCENDING page fails, which is exactly the settlement request.
        self.adapter.store.feed_error_devices.add(21)

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        other_scope.refresh_from_db()
        assert other_scope.status == "apply_failed", "a consumer error skipped the other scopes' settle"
        assert state.status == "deploying", "the static backstop judged on an unconsumed feed"
        assert self._cursor(mgmt).settle_cursor_seq is None


class TestTheConsumerReadsTheLockedRow(_CarrierCase):
    """S5.6c — a carrier holding a cached row while a concurrent repair commits a new id."""

    def test_the_consumer_reads_its_device_id_from_the_locked_row(self):
        """The FIRST feed request must carry the id on the row, not the id on the argument."""
        from netbox_nso_plugin import settlement
        from netbox_nso_plugin.models import NSODeviceManagement

        device = _make_device("stale")
        mgmt = _make_mgmt(device, "stale", 10)
        sr = _route("10.33.0.0/16", "10.33.0.1", devices=[device])
        state = _own(sr, mgmt, generation=104)
        # The settlement lives on the REPAIRED device. Device 10 has nothing to serve, so an
        # end-state assertion alone could pass for the wrong reason — hence the outbound one.
        self.adapter.store.terminal_job(11, results=[_result(sr.pk, 104)])

        # Anchored on the consumer's entry point, which is the contract Step 4 calls: Step 4
        # has handed over its `mgmt` object and the consumer has not taken its lock yet, in
        # whatever order Step 4 does the rest of its work.
        real_settle = settlement.settle_static_routes
        barrier_done = []

        def commit_the_repair_before_the_consumer_locks(passed_mgmt, **kwargs):
            if not barrier_done:
                barrier_done.append(True)

                def other_connection():
                    try:
                        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(adapter_device_id=11)
                    finally:
                        connections.close_all()

                thread = threading.Thread(target=other_connection)
                thread.start()
                thread.join(timeout=30)
                assert not thread.is_alive(), "the repair never committed, so the barrier proves nothing"
            return real_settle(passed_mgmt, **kwargs)

        with patch.object(settlement, "settle_static_routes", commit_the_repair_before_the_consumer_locks):
            self._notify(device.pk)
            self._drain()

        assert barrier_done, "the barrier never ran — Step 4 never reached the consumer"
        assert self.adapter.store.feed_requests, "the consumer was never reached"
        assert self.adapter.store.feed_requests[0][0] == 11, (
            "the first feed request used the caller's cached adapter device id, "
            f"not the one on the row it locked: {self.adapter.store.feed_requests}"
        )
        state.refresh_from_db()
        assert state.status == "in_sync"


class TestTheApplyProbeNamesTheLockedDevice(_CarrierCase):
    """The apply-in-flight probe must be about the adapter device the consumer locked.

    Step 4 reads the job state for the id on its cached row and hands the verdict down to the
    escalation. A link repair that commits in that window moves the row to another adapter
    device, and a probe of the OLD one says nothing about an apply in flight on the NEW one.
    Reusing it fails every deploying static route on a device that is mid-apply, and an
    apply's own ``in_sync`` cannot lift a row back out of ``apply_failed``.
    """

    def test_a_probe_read_for_another_device_does_not_fail_routes_mid_apply(self):
        device = _make_device("probe")
        mgmt = _make_mgmt(device, "probe", 60)
        sr = _route("10.38.0.0/16", "10.38.0.1", devices=[device])
        state = _own(sr, mgmt, generation=120)
        _stale_clock(state)
        # Device 60 is idle; the repaired device 61 has an apply in flight.
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

    def test_the_backstop_still_judges_when_the_repaired_device_is_idle(self):
        """The control: standing down is the probe's verdict, not a disabled backstop."""
        device = _make_device("idle")
        mgmt = _make_mgmt(device, "idle", 62)
        sr = _route("10.39.0.0/16", "10.39.0.1", devices=[device])
        state = _own(sr, mgmt, generation=121)
        _stale_clock(state)

        with _repair_before_the_lock(mgmt.pk, 63):
            self._notify(device.pk)
            self._drain()

        assert self.adapter.store.feed_requests[0][0] == 63, "the repair did not land before the lock"
        state.refresh_from_db()
        assert state.status == "apply_failed"


class TestTheBackstopNeedsADrainedFeed(_CarrierCase):
    """Codex S5 P1 — a walk that did not reach the end proves nothing about a waiting row.

    The consumer returning normally is not the precondition; reaching the END of the feed
    is. A stalled walk stopped on a head it could not resolve, and a full page owes another
    — in both cases the result this row is waiting for may be the sequence the walk never
    got to, so escalating there is a false red on a healthy device, and on the stalled arm
    it also short-circuits the durable five-attempt bound that exists to survive exactly
    that outage.
    """

    def test_a_stalled_walk_does_not_let_the_backstop_judge(self):
        device = _make_device("stall")
        mgmt = _make_mgmt(device, "stall", 40)
        sr = _route("10.34.0.0/16", "10.34.0.1", devices=[device])
        state = _own(sr, mgmt, generation=110, expected=False)
        _stale_clock(state)
        self.adapter.store.terminal_job(40, results=[_result(sr.pk, 110)])
        self.adapter.store.intent_status = 503  # the read-back is down: undecided, not decided

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying", (
            "the first read-back outage failed the row, bypassing the five-attempt stall bound"
        )
        assert self._cursor(mgmt).settle_stall_attempts == 1

    def test_an_unfinished_page_does_not_let_the_backstop_judge(self):
        from netbox_nso_plugin.settlement import SETTLE_FEED_PAGE

        device = _make_device("page")
        mgmt = _make_mgmt(device, "page", 41)
        sr = _route("10.35.0.0/16", "10.35.0.1", devices=[device])
        state = _own(sr, mgmt, generation=111)
        _stale_clock(state)
        # A full first page of jobs that decide nothing, then THIS row's result behind it.
        for _ in range(SETTLE_FEED_PAGE):
            self.adapter.store.terminal_job(41, results=[])
        self.adapter.store.terminal_job(41, results=[_result(sr.pk, 111)])

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "deploying", "the row was failed while its own result sat on page two"
        assert self._cursor(mgmt).settle_cursor_seq == SETTLE_FEED_PAGE


class TestTheBackstopPushesNoIntent(_CarrierCase):
    """Codex S5 P1 — escalating is bookkeeping, and bookkeeping may not re-push intent.

    On an RQ worker the push cache is cold and ``suppress_intent_push()`` is not in scope, so
    a plain ``save()`` here fires the full static-route PUT. With adapter auto-apply that
    enqueues another apply for the row just declared failed, whose result cannot recover an
    ``apply_failed`` row — a loop started by the thing meant to end one.
    """

    def test_escalating_a_stuck_row_sends_no_static_route_intent(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.signals import reset_intent_push_state

        device = _make_device("nopush")
        mgmt = _make_mgmt(device, "nopush", 42)
        sr = _route("10.36.0.0/16", "10.36.0.1", devices=[device])
        state = _own(sr, mgmt, generation=112)
        _stale_clock(state)
        # An empty feed: drained, so the backstop may judge, and nothing else can push.
        # The worker that runs this has never pushed for this device, so its change-detection
        # cache is empty. Building the fixture in-process warms that cache, and the payload
        # is status-free — deploying and apply_failed produce byte-identical intent — so a
        # warm cache would swallow the very push this pin is about.
        reset_intent_push_state()

        self._notify(device.pk)
        self._drain()

        state.refresh_from_db()
        assert state.status == "apply_failed", "the backstop did not fire, so this proves nothing"
        pushes = self._intent_puts()
        assert pushes == [], f"escalating re-pushed this device's static-route intent: {pushes}"
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


class TestAnUnprovenResultIsNotAFailure(_CarrierCase):
    """Codex S5 P2 — the clock must not overrule a result that DID correlate.

    A route staged well before its Apply carries an old ``generation_started_at``, so the
    same invocation that records an ``unproven`` advisory and deliberately leaves the row
    ``deploying`` would then select it by age and call it ``apply_failed``. The advisory
    already says more than the clock can, and it is cleared whenever the generation
    advances, so it cannot go stale.
    """

    def test_an_unproven_advisory_survives_the_stuck_deploying_clock(self):
        device = _make_device("unpr")
        mgmt = _make_mgmt(device, "unpr", 43)
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
        assert "unproven" in state.last_result_advisory
        assert state.status == "deploying", (
            "the age clock overruled a correlated result that deliberately did not settle"
        )
        assert state.last_apply_error == ""
