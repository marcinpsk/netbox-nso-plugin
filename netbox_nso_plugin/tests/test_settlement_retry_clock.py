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

from ._outbox_case import mirror_update, own_vlan
from ._settlement_case import (
    _CarrierCase,
    _make_device,
    _make_mgmt,
    _own,
    _pending_attempt_evidence,
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

    def test_a_drained_result_for_the_old_adapter_id_does_not_settle_attempts(self):
        from netbox_nso_plugin.settlement import ConsumeResult, sweep_static_route_settlements

        device = _make_device("remapped-after-drain")
        mgmt = _make_mgmt(device, "remapped-after-drain", 70)
        route = _route("198.18.70.0/24", "198.18.0.70", devices=[device])
        _own(route, mgmt, generation=270)
        mirror_update(mgmt, adapter_device_id=71)
        old_epoch = ConsumeResult(70, 1, False, False, False, 1, drained=True)

        with (
            patch("netbox_nso_plugin.settlement.settle_static_routes", return_value=old_epoch),
            patch("netbox_nso_plugin.apply_settlement.settle_device_apply_attempts") as settle_attempts,
        ):
            self.assertEqual(sweep_static_route_settlements(), (1, 0))

        settle_attempts.assert_not_called()


class TestTheRepairCapRotates(_SettlementCase):
    """S5.6d — a bounded loop over a fleet needs a durable least-recently-attempted order."""

    def _broken_row(self, index):
        """A management row pointing at an adapter device that does not exist."""
        device = _make_device(f"starve{index}")
        return device, _make_mgmt(device, f"starve{index}", 900 + index)

    def test_incomplete_attempts_have_controlled_unknown_evidence(self):
        from netbox_nso_plugin.adapter_client import get_deployment_evidence
        from netbox_nso_plugin.models import NSOApplyAttempt

        _device, management = self._broken_row(0)
        for response in (None, {}, {"generations": []}):
            with self.subTest(response=response):
                attempt = NSOApplyAttempt.objects.create(
                    management=management,
                    adapter_device_id=management.adapter_device_id,
                    selected={"static_route": 1},
                    response=response,
                )

                evidence = get_deployment_evidence(management.adapter_device_id, [attempt.pk])

                self.assertEqual(evidence["attempts"], [])
                self.assertEqual(evidence["unknown_apply_attempt_ids"], [str(attempt.pk)])

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

    def test_a_repair_save_failure_rotates_without_committing_content_changes(self):
        from django.db import connection
        from psycopg import sql

        from netbox_nso_plugin import sync_cache
        from netbox_nso_plugin.models import NSOIntentRevision

        head_device, head = self._broken_row(0)
        _tail_device, tail = self._broken_row(1)
        mirror_update(head, adapter_link_error="")
        original_adapter_id = head.adapter_device_id
        revisions = NSOIntentRevision.objects.filter(device=head_device).order_by("scope")
        original_revisions = list(revisions.values_list("scope", "revision"))
        table = sql.Identifier(head._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "CREATE FUNCTION pg_temp.reject_link_repair() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN IF NEW.id = {} THEN "
                    "RAISE check_violation USING MESSAGE = 'Repair save rejected by the test'; "
                    "END IF; RETURN NEW; END $$"
                ).format(sql.Literal(head.pk))
            )
            cursor.execute(
                sql.SQL(
                    "CREATE TRIGGER reject_link_repair BEFORE UPDATE OF nso_device_name ON {} "
                    "FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_link_repair()"
                ).format(table)
            )
        try:
            with patch.object(sync_cache, "MAX_RELINKS_PER_RUN", 1):
                self._tick()
                tail.refresh_from_db()
                self.assertIsNone(tail.adapter_link_attempted_at)
                self._tick()

            head.refresh_from_db()
            tail.refresh_from_db()
            self.assertIsNotNone(head.adapter_link_attempted_at)
            self.assertEqual(head.adapter_device_id, original_adapter_id)
            self.assertEqual(list(revisions.values_list("scope", "revision")), original_revisions)
            self.assertIsNotNone(tail.adapter_link_attempted_at)
            self.assertEqual(tail.adapter_device_id, 1)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP TRIGGER reject_link_repair ON {}").format(table))
                cursor.execute("DROP FUNCTION pg_temp.reject_link_repair()")

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


class TestTheClockDoesNotJudgeAnOrphanAttempt(_CarrierCase):
    """An exact feed result cannot replace missing Apply-attempt evidence.

    The tick still bounds and advances past an unresolvable legacy feed row. It must not
    turn that unrelated result into a verdict for a UUID that has no local attempt row.
    """

    def test_the_tick_leaves_an_orphan_attempt_non_actionable(self):
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
        state = _own(sr, mgmt, generation=207, expected=False, orphan=True)
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
        assert state.status == "deploying", "an exact Apply-attempt identity was invented from unrelated evidence"

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


class TestADrainErrorCannotStopTheSweep(_SettlementCase):
    """The sweep is the retry clock, so no earlier pass of the tick may abort it.

    ``drain_candidates``/``compaction_candidates`` are evaluated outside the drain's own
    per-key guard, so a repeating error there used to propagate out of the tick and silently
    stop the settlement clock on every five-minute run, with no summary line to show it.
    """

    def test_a_failed_drain_still_sweeps_and_still_summarises(self):
        device = _make_device("drainerr")
        mgmt = _make_mgmt(device, "drainerr", 21)
        sr = _route("10.48.0.0/16", "10.48.0.1", devices=[device])
        state = _own(sr, mgmt, generation=209)
        self.adapter.store.add_device(
            nso_instance="se-drainerr-inst",
            nso_device_name="nso-se-drainerr",
            netbox_device_id=device.pk,
            device_id=21,
        )
        self.adapter.store.terminal_job(21, results=[_result(sr.pk, 209)])

        with (
            patch("netbox_nso_plugin.drain.drain_intent_outbox", side_effect=RuntimeError("candidates exploded")),
            self.assertLogs("netbox_nso_plugin.jobs", level="INFO") as logs,
        ):
            self._tick()

        state.refresh_from_db()
        assert state.status == "in_sync", "a drain error stopped the settlement clock"
        messages = [record.getMessage() for record in logs.records]
        assert any("outbox drained" in message for message in messages), "the tick lost its summary line"
        assert any(
            record.levelname == "ERROR"
            and record.exc_info is not None
            and str(record.exc_info[1]) == "candidates exploded"
            for record in logs.records
        ), "the drain error was never reported"

    def test_a_failed_compaction_still_summarises_the_outage_tick(self):
        with (
            patch("netbox_nso_plugin.sync_cache._snapshot", return_value=([], None, {})),
            patch("netbox_nso_plugin.drain.compact_intent_outbox", side_effect=RuntimeError("compaction exploded")),
            self.assertLogs("netbox_nso_plugin.jobs", level="INFO") as logs,
        ):
            self._tick()

        messages = [record.getMessage() for record in logs.records]
        assert any("outbox drained" in message for message in messages), "the tick lost its summary line"
        assert any(
            record.levelname == "ERROR"
            and record.exc_info is not None
            and str(record.exc_info[1]) == "compaction exploded"
            for record in logs.records
        ), "the compaction error was never reported"


class TestTheTickSweepsEveryDeployingScope(_SettlementCase):
    """The sweep is the fleet's only plugin-to-adapter clock, so it must reach every scope.

    Its candidate query asked for static-route overlays alone, so a device whose only
    in-flight row was a VLAN, an SVI or an MTU had no tick at all: the adapter could finish
    that Apply and, with the callback channel dead, the row stayed ``deploying`` forever.
    """

    def _deploying_vlan(self, tag, adapter_device_id, vid, generation_id, push_seq):
        """One deploying VLAN overlay with the durable attempt the evidence is addressed to."""
        from netbox_nso_plugin.models import NSOApplyAttempt

        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id)
        self.adapter.store.add_device(
            nso_instance=f"se-{tag}-inst",
            nso_device_name=f"nso-se-{tag}",
            netbox_device_id=device.pk,
            device_id=adapter_device_id,
        )
        selected = {"vlan": push_seq}
        attempt = NSOApplyAttempt.objects.create(
            management=mgmt,
            adapter_device_id=adapter_device_id,
            scope_revisions=selected,
            selected=selected,
            http_status=202,
            response={
                "device_id": adapter_device_id,
                "outcome": "promoted",
                "selected": selected,
                "skipped": {},
                "generations": [{"generation_id": generation_id}],
            },
        )
        state = mirror_update(own_vlan(mgmt, vid, tag), status="deploying", apply_attempt_id=attempt.pk)
        return mgmt, state

    def test_the_tick_settles_a_deploying_vlan_with_no_static_route_overlay(self):
        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt, state = self._deploying_vlan("vlanonly", 60, 601, 310, 501)
        assert not NSOStaticRouteState.objects.filter(management=mgmt).exists(), "the pin grew a static-route candidate"
        self.settled_evidence_scopes = ("vlan",)

        # No callback of any kind: the tick is the only clock running.
        with patch(
            "netbox_nso_plugin.reconcile.enqueue_device_reconcile",
            side_effect=AssertionError("the pin fired a callback: the very channel this removes"),
        ):
            self._tick()

        state.refresh_from_db()
        assert state.status == "in_sync", "a device whose only in-flight row is a VLAN is never swept"

    def test_a_still_running_apply_keeps_its_vlan_deploying(self):
        """Control: the widened candidate set judges from evidence, not from being polled."""
        _mgmt, state = self._deploying_vlan("vlanrunning", 61, 611, 311, 502)

        self._tick()

        state.refresh_from_db()
        assert state.status == "deploying", "the sweep settled a VLAN whose Apply is still running"


class TestTheEvidenceEndpointClosesItsWorkerConnection(_SettlementCase):
    """The double's ORM-backed evidence endpoint runs on a keep-alive HTTP worker thread.

    That thread opens its own Django connection and holds it while it waits for the next
    request, so every completed request retains a PostgreSQL slot. Closing connections from
    the test thread cannot close another thread's connection.
    """

    def _worker_backend_pids(self):
        """Collect the backend pid of every connection opened outside the test thread."""
        import threading

        from django.db.backends.signals import connection_created

        pids = []
        test_thread = threading.get_ident()

        def record(sender, connection, **kwargs):
            if threading.get_ident() != test_thread:
                pids.append(connection.connection.info.backend_pid)

        connection_created.connect(record)
        self.addCleanup(connection_created.disconnect, record)
        return pids

    def _wait_until_disconnected(self, pid, timeout=5.0):
        """Poll ``pg_stat_activity`` from the test connection until *pid* has gone."""
        import time

        from django.db import connection

        deadline = time.monotonic() + timeout
        while True:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", [pid])
                if cursor.fetchone() is None:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _attempt(self, tag, adapter_device_id):
        from netbox_nso_plugin.models import NSOApplyAttempt

        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id)
        selected = {"static_route": 1}
        return NSOApplyAttempt.objects.create(
            management=mgmt,
            adapter_device_id=adapter_device_id,
            scope_revisions=selected,
            selected=selected,
            http_status=202,
            response={
                "device_id": adapter_device_id,
                "outcome": "promoted",
                "selected": selected,
                "skipped": {},
                "generations": [{"generation_id": 1}],
            },
        )

    def test_an_answered_evidence_request_leaves_no_worker_backend_connected(self):
        from netbox_nso_plugin.adapter_client import get_deployment_evidence

        attempt = self._attempt("evidenceslot", 80)
        pids = self._worker_backend_pids()

        # Two requests over the client's ONE kept-alive connection, so one worker serves both.
        get_deployment_evidence(80, [attempt.pk])
        after_first = list(pids)
        get_deployment_evidence(80, [attempt.pk])

        assert len(after_first) == 1, f"the endpoint ran no real ORM query on the worker: {after_first}"
        assert self._wait_until_disconnected(after_first[0]), (
            "the worker held its PostgreSQL backend after answering, so every completed "
            "request retains a connection slot for the life of the thread"
        )
        assert len(pids) == 2 and pids[0] != pids[1], f"the second request reused the retained backend: {pids}"

    def test_a_failing_evidence_query_still_closes_the_worker_connection(self):
        """The cleanup is a ``finally``: a query error must not retain the slot either."""
        import threading

        from django.db import connection
        from psycopg import sql

        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt = self._attempt("evidenceerr", 81)
        pids = self._worker_backend_pids()
        answered = threading.Event()
        release = threading.Event()
        failures = []

        def worker():
            try:
                _pending_attempt_evidence(81, [attempt.pk])
            except Exception as exc:  # noqa: BLE001 (the query error is what this pins)
                failures.append(exc)
            answered.set()
            # An HTTP worker outlives one request, so thread exit must not be the cleanup.
            release.wait(30)

        table = sql.Identifier(NSOApplyAttempt._meta.db_table)
        hidden = sql.Identifier(f"{NSOApplyAttempt._meta.db_table}_hidden")
        thread = threading.Thread(target=worker)
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(table, hidden))
        try:
            try:
                # Started inside the restore guard: no flush can undo a renamed table.
                thread.start()
                assert answered.wait(30), "the evidence query never returned"
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(hidden, table))
            assert failures, "renaming the table away did not make the real query fail"
            assert len(pids) == 1, f"the worker opened no connection of its own: {pids}"
            assert self._wait_until_disconnected(pids[0]), (
                "a failing evidence query retained the worker's PostgreSQL backend"
            )
        finally:
            release.set()
            if thread.ident is not None:
                thread.join(timeout=30)
