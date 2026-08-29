# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S5) — what a correlated settlement does to the overlay.

Pins S5.1, S5.2 and S5.3, plus the P5 rows the appendix inherits verbatim (P5.1-P5.5,
P5.7, P5.9, P5.10). Every test drives the real consumer over the real HTTP adapter double:
the per-route verdict, the read-back and the cursor advance are the production ones.

The governing rule is that a result settles a row only when it names the generation the
row is waiting for **and** the fingerprint that generation echoed. Everything else is
non-settling, and non-settling splits three ways which this module keeps distinguishable:
the cursor advances (the result can never correlate), the cursor stalls (it cannot be
decided *yet*), or nothing at all happens (the result is about some other row).
"""

from __future__ import annotations

from ._settlement_case import (
    FINGERPRINT,
    _make_device,
    _make_mgmt,
    _own,
    _result,
    _route,
    _SettlementCase,
    _stale_clock,
)


class TestExpectationThreeWay(_SettlementCase):
    """S5.1 (P5.11) — the three ways an expectation can be absent, wrong, or unobtainable.

    They must not collapse into one another: burning a result that is merely early loses a
    settlement forever, and stalling on one that can never correlate blocks the device.
    """

    def test_expectation_three_way_before(self):
        """(a) The result overtook the PUT response — the read-back supplies the expectation."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("early")
        mgmt = _make_mgmt(device, "early", 10)
        sr = _route("10.10.0.0/16", "10.10.0.1", devices=[device])
        state = _own(sr, mgmt, generation=71, expected=False)
        # The adapter commits its intent write BEFORE answering the PUT, so the echo the
        # lost response would have carried is already re-servable.
        self.adapter.store.echo(10, sr.pk, 71, FINGERPRINT)
        self.adapter.store.terminal_job(10, results=[_result(sr.pk, 71)])

        outcome = consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "in_sync", "an early result was burned instead of resolved"
        assert state.expected_generation == 71, "the read-back's expectation was not recorded"
        assert self.adapter.store.readback_requests == [10]
        assert outcome.consumed == 1
        assert not outcome.stalled

    def test_expectation_three_way_lost(self):
        """(b) The response is gone and the read-back is down — undecided, not decided."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("lost")
        mgmt = _make_mgmt(device, "lost", 20)
        sr = _route("10.11.0.0/16", "10.11.0.1", devices=[device])
        state = _own(sr, mgmt, generation=72, expected=False)
        self.adapter.store.terminal_job(20, results=[_result(sr.pk, 72)])
        self.adapter.store.intent_status = 503

        outcome = consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "an undecided result settled the row"
        assert outcome.stalled, "the walk advanced past a result it could not decide"
        assert self._cursor(mgmt).settle_cursor_seq == 0, "the cursor moved past an undecided head"
        assert self._cursor(mgmt).settle_stall_attempts == 1

    def test_a_malformed_read_back_is_undecided_and_counts_against_the_bound(self):
        """(b') The read-back answers 200 with a body the contract cannot produce.

        Degrading it to an empty document would record ZERO expectations and settle the row
        against nothing. It must be undecided instead — the same leg as an unreachable
        read-back — so the durable stall bound counts it and eventually abandons it.
        """
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("malformed")
        mgmt = _make_mgmt(device, "malformed", 25)
        sr = _route("10.13.0.0/16", "10.13.0.1", devices=[device])
        state = _own(sr, mgmt, generation=74, expected=False)
        self.adapter.store.terminal_job(25, results=[_result(sr.pk, 74)])
        self.adapter.store.intent_malformed = True

        outcome = consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "an undecided result settled the row"
        assert outcome.stalled, "the walk advanced past a result it could not decide"
        assert self._cursor(mgmt).settle_cursor_seq == 0, "the cursor moved past an undecided head"
        assert self._cursor(mgmt).settle_stall_attempts == 1
        assert self.adapter.store.readback_requests == [25]

    def test_expectation_three_way_mismatch(self):
        """(c) A recorded expectation the result contradicts — decided, and decided 'no'."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("mism")
        mgmt = _make_mgmt(device, "mism", 30)
        sr = _route("10.12.0.0/16", "10.12.0.1", devices=[device])
        state = _own(sr, mgmt, generation=73)
        self.adapter.store.terminal_job(30, results=[_result(sr.pk, 73, fingerprint="fp-other")])

        outcome = consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "a contradicted result settled the row"
        assert not outcome.stalled, "a decided mismatch was treated as a stall"
        assert self._cursor(mgmt).settle_cursor_seq == 1, "the decided mismatch did not advance"
        assert self._cursor(mgmt).settle_stall_attempts == 0
        assert "fp-other" in state.last_result_advisory
        # No read-back: the expectation IS recorded, it simply disagrees.
        assert self.adapter.store.readback_requests == []


class TestNothingToCorrelate(_SettlementCase):
    """S5.2 and P5.9 — a job with no per-route results settles nothing and blocks nothing."""

    def test_a_removal_job_settles_nothing_and_advances(self):
        """A removal job carries no route ids at all; absence is not 'everything failed'."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("rem")
        mgmt = _make_mgmt(device, "rem", 40)
        sr = _route("10.13.0.0/16", "10.13.0.1", devices=[device])
        state = _own(sr, mgmt, generation=74)
        self.adapter.store.terminal_job(40, results=None, job_type="removal")

        outcome = consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "a removal job was read as this route's verdict"
        assert state.last_apply_error == ""
        assert outcome.consumed == 1
        assert self._cursor(mgmt).settle_cursor_seq == 1, "a job that decides nothing blocked the feed"

    def test_an_apply_without_static_route_results_settles_nothing(self):
        """P5.9: an apply that carried other scopes says nothing about this device's routes."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("noresult")
        mgmt = _make_mgmt(device, "noresult", 41)
        sr = _route("10.14.0.0/16", "10.14.0.1", devices=[device])
        state = _own(sr, mgmt, generation=75)
        self.adapter.store.terminal_job(41, results=[])

        consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying"
        assert self._cursor(mgmt).settle_cursor_seq == 1


class TestPerRouteVerdicts(_SettlementCase):
    """P5.2-P5.5, P5.7 and P5.10 — one result row decides one overlay, on its own evidence."""

    def test_an_auto_applied_row_settles_without_passing_through_deploying(self):
        """P5.2: an auto-applied route is never marked deploying, and still has a real result."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("auto")
        mgmt = _make_mgmt(device, "auto", 50)
        sr = _route("10.15.0.0/16", "10.15.0.1", devices=[device])
        state = _own(sr, mgmt, generation=76, status="accepted")
        self.adapter.store.terminal_job(50, results=[_result(sr.pk, 76)])

        consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "in_sync", "an auto-applied row can never leave 'accepted'"
        assert state.last_apply_at is not None

    def test_a_superseded_generation_does_not_settle(self):
        """P5.3: the overlay moved on mid-flight, so this result describes replaced content."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("super")
        mgmt = _make_mgmt(device, "super", 51)
        sr = _route("10.16.0.0/16", "10.16.0.1", devices=[device])
        state = _own(sr, mgmt, generation=78)
        self.adapter.store.terminal_job(51, results=[_result(sr.pk, 77)])

        consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "a superseded result settled the row"
        assert self._cursor(mgmt).settle_cursor_seq == 1, "a superseded result blocked the feed"
        # A superseded row is not an expectation problem, so it must not trigger a read-back.
        assert self.adapter.store.readback_requests == []

    def test_an_unproven_result_records_an_advisory_and_does_not_settle(self):
        """P5.4: 'unproven' is evidence the apply could not prove the value landed."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("unprov")
        mgmt = _make_mgmt(device, "unprov", 52)
        sr = _route("10.17.0.0/16", "10.17.0.1", devices=[device])
        state = _own(sr, mgmt, generation=79)
        self.adapter.store.terminal_job(
            52,
            results=[
                _result(
                    sr.pk,
                    79,
                    outcome="unproven",
                    error={"code": "verify_unavailable", "message": "the device refused the verify read"},
                )
            ],
        )

        consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "an unproven result was read as a green settle"
        assert "unproven" in state.last_result_advisory
        assert "the device refused the verify read" in state.last_result_advisory
        assert self._cursor(mgmt).settle_cursor_seq == 1

    def test_two_routes_in_one_job_settle_independently(self):
        """P5.5: per route, and the failed row carries ITS OWN error, not a scope message."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("pair")
        mgmt = _make_mgmt(device, "pair", 53)
        good = _route("10.18.0.0/16", "10.18.0.1", devices=[device])
        bad = _route("10.19.0.0/16", "10.19.0.1", devices=[device])
        good_state = _own(good, mgmt, generation=80)
        bad_state = _own(bad, mgmt, generation=80)
        self.adapter.store.terminal_job(
            53,
            results=[
                _result(good.pk, 80),
                _result(
                    bad.pk,
                    80,
                    outcome="apply_failed",
                    error={"code": "ned_reject", "message": "next-hop 10.19.0.1 is not reachable"},
                ),
            ],
        )

        consume_static_route_settlements(mgmt)

        good_state.refresh_from_db()
        bad_state.refresh_from_db()
        assert good_state.status == "in_sync"
        assert good_state.last_apply_error == ""
        assert bad_state.status == "apply_failed"
        assert "next-hop 10.19.0.1 is not reachable" in bad_state.last_apply_error

    def test_the_overlay_read_is_scoped_to_the_routes_the_job_names(self):
        """A job decides its own results, so the device's whole overlay table is not read."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("scoped")
        mgmt = _make_mgmt(device, "scoped", 60)
        named = _route("10.24.0.0/16", "10.24.0.1", devices=[device])
        unnamed = _route("10.25.0.0/16", "10.25.0.1", devices=[device])
        named_state = _own(named, mgmt, generation=85)
        unnamed_state = _own(unnamed, mgmt, generation=85)
        self.adapter.store.terminal_job(60, results=[_result(named.pk, 85)])

        with CaptureQueriesContext(connection) as queries:
            consume_static_route_settlements(mgmt)

        named_state.refresh_from_db()
        unnamed_state.refresh_from_db()
        assert named_state.status == "in_sync"
        assert unnamed_state.status == "deploying", "a route this job never named was settled"
        predicates = [
            sql.split(" WHERE ", 1)[1]
            for sql in (query["sql"] for query in queries.captured_queries)
            if (
                sql.startswith("SELECT")
                and "nsostaticroutestate" in sql
                and "management_id" in sql
                and " WHERE " in sql
            )
        ]
        device_predicates = [predicate for predicate in predicates if "management_id" in predicate]
        assert device_predicates, "the settle pass made no device-scoped overlay read"
        assert all("static_route_id" in predicate for predicate in device_predicates), (
            f"the settle pass re-read every overlay row of the device: {device_predicates}"
        )

    def test_a_newer_running_apply_does_not_gate_an_older_result(self):
        """P5.7: consumption is ordered by the feed and decided by generation, never gated."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("inflight")
        mgmt = _make_mgmt(device, "inflight", 54)
        sr = _route("10.20.0.0/16", "10.20.0.1", devices=[device])
        state = _own(sr, mgmt, generation=81)
        self.adapter.store.terminal_job(54, results=[_result(sr.pk, 81)])
        self.adapter.store.queued_job(54)  # a newer apply, still running

        consume_static_route_settlements(mgmt)

        state.refresh_from_db()
        assert state.status == "in_sync", "an in-flight apply suppressed an older correlated result"
        assert self._cursor(mgmt).settle_cursor_seq == 1

    def test_a_null_route_id_is_non_settling_and_its_siblings_still_settle(self):
        """P5.10: a null id fences that ROW, not the device — and never falls back to the triple."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("nullid")
        mgmt = _make_mgmt(device, "nullid", 55)
        backfilled = _route("10.21.0.0/16", "10.21.0.1", devices=[device])
        legacy = _route("10.0.0.0/8", "10.0.0.1", devices=[device])
        backfilled_state = _own(backfilled, mgmt, generation=82)
        legacy_state = _own(legacy, mgmt, generation=82)
        # The null-id entry's `key` is the legacy triple, which is exactly what a fallback
        # would match against.
        self.adapter.store.terminal_job(55, results=[_result(None, 82), _result(backfilled.pk, 82)])

        consume_static_route_settlements(mgmt)

        backfilled_state.refresh_from_db()
        legacy_state.refresh_from_db()
        assert backfilled_state.status == "in_sync", "one uncorrelated sibling fenced the whole device"
        assert legacy_state.status == "deploying", "the consumer fell back to the (vrf, prefix, next-hop) triple"


class TestMembershipRemoval(_SettlementCase):
    """S5.3 (P5.15-S) — the half Appendix S owns: the RETAINED device settles."""

    def test_only_the_retained_device_settles_after_a_membership_removal(self):
        """The removed device's overlay is gone, so nothing waits on it and nothing stalls."""
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        from ._static_route_case import _unassign_without_push

        kept_device = _make_device("kept")
        gone_device = _make_device("gone")
        kept = _make_mgmt(kept_device, "kept", 60)
        gone = _make_mgmt(gone_device, "gone", 61)
        sr = _route("10.22.0.0/16", "10.22.0.1", devices=[kept_device, gone_device])
        kept_state = _own(sr, kept, generation=90)
        gone_state = _own(sr, gone, generation=90)

        # The combined identity + membership edit: D leaves the route, so P8 deletes its
        # overlay. Its removal job carries no route_id at all (that arm is P5.15-O).
        _unassign_without_push(sr, gone_device)
        gone_state.delete()

        self.adapter.store.terminal_job(60, results=[_result(sr.pk, 90)])
        self.adapter.store.terminal_job(61, results=None, job_type="removal")

        consume_static_route_settlements(kept)
        gone_outcome = consume_static_route_settlements(gone)

        kept_state.refresh_from_db()
        assert kept_state.status == "in_sync"
        assert not NSOStaticRouteState.objects.filter(management=gone).exists()
        assert not gone_outcome.stalled, "the removed device's feed head blocked its cursor"
        assert self._cursor(gone).settle_cursor_seq == 1


class TestAVerdictCannotLandOnNewerIntent(_SettlementCase):
    """Codex S5 P1 — the consumer locks the MANAGEMENT row, not the overlay.

    One job can carry a route whose expectation is missing beside one whose expectation is
    recorded. Recovering the first costs a real HTTP round trip, and the second's overlay
    was loaded before it: an operator Accept or content edit in that window allocates a new
    generation and resets the status, and an unguarded save then puts an old result's
    verdict on intent the device has not been asked for yet.
    """

    def test_an_edit_during_the_read_back_cannot_be_overwritten_by_the_old_verdict(self):
        import threading

        from django.db import connections
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("cas")
        mgmt = _make_mgmt(device, "cas", 10)
        # `recovered` needs a read-back; `edited` is the row the operator moves during it.
        recovered = _route("10.50.0.0/16", "10.50.0.1", devices=[device])
        edited = _route("10.51.0.0/16", "10.51.0.1", devices=[device])
        recovered_state = _own(recovered, mgmt, generation=301, expected=False)
        edited_state = _own(edited, mgmt, generation=301)
        self.adapter.store.echo(10, recovered.pk, 301, FINGERPRINT)
        self.adapter.store.terminal_job(10, results=[_result(recovered.pk, 301), _result(edited.pk, 301)])

        def operator_edit_mid_flight():
            """A real second connection: the consumer holds the management row, not this one."""

            def commit():
                try:
                    from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction

                    current = NSOStaticRouteState.objects.get(pk=edited_state.pk)
                    with intent_transaction(footprint_for_instance(current)):
                        NSOStaticRouteState.objects.filter(pk=edited_state.pk).update(
                            intent_generation=302,
                            generation_started_at=timezone.now(),
                            status="accepted",
                            expected_generation=None,
                            expected_fingerprint="",
                        )
                finally:
                    connections.close_all()

            thread = threading.Thread(target=commit)
            thread.start()
            self._operator_edit = thread

        self.adapter.store.on_readback = operator_edit_mid_flight

        consume_static_route_settlements(mgmt)
        self._operator_edit.join(timeout=30)
        assert not self._operator_edit.is_alive(), "the operator edit never committed"

        recovered_state.refresh_from_db()
        edited_state.refresh_from_db()
        assert recovered_state.status == "in_sync", "the read-back arm stopped working"
        assert edited_state.intent_generation == 302
        assert edited_state.status == "accepted", (
            "a verdict computed for generation 301 landed on generation 302 — a green badge "
            "for content the device has not been asked for"
        )


class TestTheReadBackIsFetchedOncePerPass(_SettlementCase):
    """The intent read-back is keyed by device alone, so one pass may fetch it once.

    ``_settle_job`` needs it for every job that carries a result whose expectation the pusher
    never recorded. Fetching per job issues up to ``SETTLE_FEED_PAGE`` identical HTTP calls
    while the pass holds ``SELECT … FOR UPDATE`` on the management row, and every other writer
    of that row (the push recorder, the link repair, reconcile) waits for the sum of them.
    """

    def test_one_read_back_serves_every_job_on_the_page(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("readback")
        mgmt = _make_mgmt(device, "readback", 96)
        routes = [_route(f"10.60.{n}.0/24", f"10.60.{n}.1", devices=[device]) for n in range(3)]
        states = [_own(sr, mgmt, generation=310, expected=False) for sr in routes]
        for sr in routes:
            # Committed intent whose PUT response never arrived: the read-back is the recovery.
            self.adapter.store.echo(96, sr.pk, 310, FINGERPRINT)
            self.adapter.store.terminal_job(96, results=[_result(sr.pk, 310)])

        outcome = consume_static_route_settlements(mgmt)

        assert outcome.consumed == 3
        assert self.adapter.store.readback_requests == [96], (
            "the device-wide read-back was re-fetched per job, under the row lock"
        )
        for state in states:
            state.refresh_from_db()
            assert state.status == "in_sync"


class TestTheEscalationReusesStep4sJobState(_SettlementCase):
    """The static backstop must not re-read the apply-job state Step 4 handed it.

    Both reads answer the same question (may an apply be in flight) from the same descending
    jobs page, for the same adapter device id, so the second one is a wasted round trip whose
    answer can disagree with the first.

    Step 4 itself probes twice, once on each side of the settlement, and those two are about
    DIFFERENT devices whenever a link repair commits in between: the settlement resolves the
    id from the row it locks, and everything after it judges by the id the row holds then.
    """

    def _jobs_page_reads(self):
        """Reads of the DESCENDING jobs page: the apply-activity probe, not the feed."""
        store = self.adapter.store
        return len([path for _method, path in store.requests if path == "/api/v1/jobs"]) - len(store.feed_requests)

    def test_step_4_does_not_read_the_jobs_page_for_settlement(self):
        from unittest.mock import patch

        from netbox_nso_plugin.reconcile import run_device_reconcile

        device = _make_device("step4")
        mgmt = _make_mgmt(device, "step4", 97)
        self.adapter.store.add_device(nso_instance="se-step4-inst", nso_device_name="nso-se-step4", device_id=97)
        sr = _route("10.61.0.0/16", "10.61.0.1", devices=[device])
        state = _own(sr, mgmt, generation=320)
        _stale_clock(state)
        # The legacy exact-result feed remains empty. Attempt-addressable settlement must
        # not reconstruct activity from the descending jobs page.
        self.adapter.store.terminal_job(97)

        with patch("netbox_nso_plugin.reconcile.reconcile_device", return_value={}):
            run_device_reconcile(device.pk)

        state.refresh_from_db()
        assert state.status == "deploying"
        assert self._jobs_page_reads() == 0, "settlement reconstructed Apply evidence from the jobs page"
