# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Race the acknowledgement against settlement passes, fence writers and other acknowledgements.

Every racer runs on its own thread with its own database connection. The barriers hold the
acknowledgement inside its final adapter observation, where it owns its locks, and hold a
settlement pass after its locked page read, before its first carrier. A pass that waits is
detected from pg_stat_activity, never from a sleep.
"""

import copy
import threading
import time

from django.db import OperationalError, connection, connections
from django.db.models.signals import post_init

from netbox_nso_plugin.models import NSOApplyExecution, NSODeviceManagement, NSOSettlementIssue
from netbox_nso_plugin.settlement import consume_static_route_settlements, sweep_static_route_settlements

from ._adapter_http import make_generation_listing
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import mirror_update
from ._settlement_observation import serve_observation
from .test_durable_bgp_capture import BGPCaptureCase
from .test_settlement_acknowledgement_view import STALE, AcknowledgementViewHelpers
from .test_settlement_issue import HistoricalStore

WAIT = 30


class RaceHelpers:
    def hold_observation(self):
        """Hold the next settlement observation until released; the caller then owns its locks."""
        return self.hold_next("POST", "/settlement-observation")

    def hold_next(self, verb, route):
        """Hold the next adapter request for *route* until released."""
        entered, release = threading.Event(), threading.Event()
        original = self.adapter._handle
        armed = []

        def handle(method, url, **kwargs):
            if method == verb and url.endswith(route) and not armed:
                armed.append(True)
                entered.set()
                release.wait(WAIT)
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        return entered, release

    def pause_pass(self):
        """Pause the pass that run_pass starts after its locked page read, before its first carrier."""
        paused, resume = threading.Event(), threading.Event()
        state = {"thread": None, "locked": False, "done": False}

        def receiver(sender, instance, **kwargs):
            if threading.get_ident() != state["thread"] or state["done"]:
                return
            if connection.in_atomic_block:
                state["locked"] = True
            elif state["locked"]:
                state["done"] = True
                paused.set()
                resume.wait(WAIT)

        post_init.connect(receiver, sender=NSODeviceManagement, weak=False)
        self.addCleanup(post_init.disconnect, receiver, sender=NSODeviceManagement)
        return state, paused, resume

    def run_pass(self, state):
        outcome = {}

        def run():
            state["thread"] = threading.get_ident()
            try:
                outcome["result"] = consume_static_route_settlements(self.management.pk)
            except BaseException as exc:  # noqa: BLE001 - the main thread reports it
                outcome["error"] = exc
            finally:
                connections.close_all()

        thread = threading.Thread(target=run)
        thread.start()
        return thread, outcome

    def post_in_thread(self, issue, reason):
        outcome = {}
        user = self.acknowledger()

        def run():
            try:
                outcome["response"] = self.client_for(user).post(
                    self.action_url(), self.form(issue, reason=reason), follow=True
                )
            finally:
                connections.close_all()

        thread = threading.Thread(target=run)
        thread.start()
        return thread, outcome

    def wait_for_lock_waiter(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.01)
        self.fail("no racer ever waited on a lock")

    def join(self, *threads):
        for thread in threads:
            thread.join(WAIT)
            self.assertFalse(thread.is_alive(), "a racer did not finish")

    def race_pass_against_acknowledgement(self, issue, reason):
        """Let a pass read before the acknowledgement commits and write after it, then return both outcomes."""
        state, paused, resume = self.pause_pass()
        pass_thread, passed = self.run_pass(state)
        self.assertTrue(paused.wait(WAIT))
        entered, release = self.hold_observation()
        ack_thread, acknowledged = self.post_in_thread(issue, reason)
        self.assertTrue(entered.wait(WAIT))
        resume.set()
        self.wait_for_lock_waiter()
        release.set()
        self.join(ack_thread, pass_thread)
        self.assertNotIn("error", passed)
        return passed["result"], acknowledged["response"]

    def blocked_while_held(self, write):
        """Run *write* on another connection; report whether it had to wait for a lock."""
        outcome = {}

        def run():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '300ms'")
                write()
                outcome["blocked"] = False
            except OperationalError:
                outcome["blocked"] = True
            finally:
                connections.close_all()

        thread = threading.Thread(target=run)
        thread.start()
        self.join(thread)
        return outcome["blocked"]


class TestAcknowledgementAgainstSettlement(
    RaceHelpers, AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase
):
    def stall_next_after_an_acknowledgement(self):
        """Acknowledge job 501, then stall the next pass at the invalid job 502 with the resume request set."""
        first = self.stall_invalid_manual_carrier()
        self.client_for(self.acknowledger()).post(self.action_url(), self.form(first))
        self.append_feed_jobs(self.sync_job(502, 2))
        self.invalid_feed_job(502)
        serve_observation(self)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        issue = NSOSettlementIssue.objects.get(state="open")
        self.assertEqual((issue.job_id, issue.position), (502, "next_feed"))
        self.management.refresh_from_db()
        self.assertTrue(self.management.settle_resume_pending)
        return issue

    def test_a_stale_next_feed_stall_cannot_rewrite_the_banner_or_clear_the_resume_request(self):
        issue = self.stall_next_after_an_acknowledgement()
        result, response = self.race_pass_against_acknowledgement(issue, "placeholder racing next feed job")
        self.assertContains(response, "Acknowledged adapter job 502 as outcome unknown.")
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        issue.refresh_from_db()
        self.assertEqual(issue.state, "acknowledged")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertEqual(self.management.settle_cursor_seq, 2)
        self.assertTrue(self.management.settle_resume_pending)
        self.assertFalse(NSOSettlementIssue.objects.filter(state="open").exists())
        self.drain_queue()
        self.management.refresh_from_db()
        self.assertEqual((self.management.settle_cursor_seq, self.management.settle_resume_pending), (2, False))

    def test_writers_of_the_decision_inputs_wait_until_the_acknowledgement_commits(self):
        issue = self.stall_invalid_manual_carrier()
        entered, release = self.hold_observation()
        ack_thread, acknowledged = self.post_in_thread(issue, "placeholder held acknowledgement")
        self.assertTrue(entered.wait(WAIT))
        owner = self.owners[0]
        blocked = {
            "cursor": self.blocked_while_held(
                lambda: mirror_update(NSODeviceManagement.objects.get(pk=self.management.pk), settle_cursor_seq=7)
            ),
            "owner": self.blocked_while_held(
                lambda: mirror_update(type(owner).objects.get(pk=owner.pk), last_apply_error="placeholder edit")
            ),
        }
        release.set()
        self.join(ack_thread)
        self.assertEqual(blocked, {"cursor": True, "owner": True})
        self.assertContains(acknowledged["response"], "Acknowledged adapter job 501 as outcome unknown.")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 1)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "apply_failed")

    def test_a_fence_change_after_the_issue_is_read_refuses_the_acknowledgement(self):
        issue = self.stall_invalid_manual_carrier()
        before = self.effects()
        moved = []

        def move_cursor(sender, instance, **kwargs):
            if instance.pk != issue.pk or moved or threading.current_thread() is threading.main_thread():
                return
            moved.append(True)
            writer = threading.Thread(target=self.elsewhere, kwargs={"settle_cursor_seq": 7})
            writer.start()
            writer.join(WAIT)

        post_init.connect(move_cursor, sender=NSOSettlementIssue, weak=False)
        self.addCleanup(post_init.disconnect, move_cursor, sender=NSOSettlementIssue)
        ack_thread, acknowledged = self.post_in_thread(issue, "placeholder fence moves first")
        self.join(ack_thread)
        self.assertEqual(moved, [True])
        self.assertContains(acknowledged["response"], STALE)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 7)
        after = self.effects()
        self.assertEqual(after[:4], before[:4])
        self.assertEqual(after[5], before[5])

    def elsewhere(self, **values):
        try:
            mirror_update(NSODeviceManagement.objects.get(pk=self.management.pk), **values)
        finally:
            connections.close_all()


class TestTwoRevisions(RaceHelpers, AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def test_a_revision_that_loses_its_evidence_in_flight_refuses_and_its_successor_commits_once(self):
        first = self.stall_invalid_manual_carrier()
        changed = []

        def change(document):
            if changed:
                document["job"]["digest"] = "c" * 64
                document["job"]["evidence_error"]["digest"] = "c" * 64

        serve_observation(self, edit=change)
        entered, release = self.hold_observation()
        held, held_outcome = self.post_in_thread(first, "placeholder first revision")
        self.assertTrue(entered.wait(WAIT))
        # The adapter evidence changes before the held observation answers, and a pass waits to publish it.
        changed.append(True)
        pass_thread, passed = self.run_pass({"thread": None})
        self.wait_for_lock_waiter()
        release.set()
        self.join(held, pass_thread)
        self.assertContains(held_outcome["response"], STALE)
        self.assertNotIn("error", passed)
        self.assertTrue(passed["result"].stalled)
        first.refresh_from_db()
        self.assertEqual(first.state, "superseded")
        successor = NSOSettlementIssue.objects.get(state="open")
        self.assertNotEqual(successor.revision_digest, first.revision_digest)
        response = self.client_for(self.acknowledger()).post(
            self.action_url(), self.form(successor, reason="placeholder successor revision"), follow=True
        )
        self.assertContains(response, "Acknowledged adapter job 501 as outcome unknown.")
        retry = self.client_for(self.acknowledger()).post(
            self.action_url(), self.form(first, reason="placeholder first revision again"), follow=True
        )
        self.assertContains(retry, STALE)
        self.assertEqual(
            sorted(NSOSettlementIssue.objects.values_list("pk", "state")),
            sorted([(first.pk, "superseded"), (successor.pk, "acknowledged")]),
        )
        self.assertEqual(
            list(NSOApplyExecution.objects.values_list("consumption_state", "settlement_issue_id")),
            [("acknowledged", successor.pk)],
        )

    def test_a_superseded_and_the_current_revision_racing_commit_once(self):
        stale = self.stall_invalid_manual_carrier()

        def change(document):
            document["job"]["digest"] = "c" * 64
            document["job"]["evidence_error"]["digest"] = "c" * 64

        serve_observation(self, edit=change)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        current = NSOSettlementIssue.objects.get(state="open")
        stale.refresh_from_db()
        self.assertEqual(stale.state, "superseded")
        self.assertNotEqual(current.revision_digest, stale.revision_digest)
        entered, release = self.hold_observation()
        first, first_outcome = self.post_in_thread(current, "placeholder current revision")
        self.assertTrue(entered.wait(WAIT))
        second, second_outcome = self.post_in_thread(stale, "placeholder superseded revision")
        self.wait_for_lock_waiter()
        release.set()
        self.join(first, second)
        self.assertContains(first_outcome["response"], "Acknowledged adapter job 501 as outcome unknown.")
        self.assertContains(second_outcome["response"], STALE)
        self.assertEqual(
            sorted(NSOSettlementIssue.objects.values_list("pk", "state")),
            sorted([(stale.pk, "superseded"), (current.pk, "acknowledged")]),
        )
        current.refresh_from_db()
        self.assertEqual(current.acknowledgement_reason, "placeholder current revision")
        self.assertEqual(
            list(NSOApplyExecution.objects.values_list("consumption_state", "settlement_issue_id")),
            [("acknowledged", current.pk)],
        )
        self.assertEqual(self.queued_devices(), [self.device.pk])


class TestHistoricalAcknowledgementAgainstSettlement(
    RaceHelpers, AcknowledgementViewHelpers, HistoricalStore, BGPCaptureCase
):
    def setUp(self):
        super().setUp()
        self.owners = []

    def test_a_stale_historical_stall_cannot_rewrite_the_banner_or_clear_the_resume_request(self):
        self.serve_history()
        second = copy.deepcopy(self.history_jobs[0])
        second.update(id=41, settle_seq=4)
        self.history_jobs.append(second)
        self.history_listing.append(
            make_generation_listing(
                generation_id=61,
                seq=3,
                job_id=41,
                carrier_job_id=41,
                stream_revisions={"bgp": 4},
                source_push_seq={"bgp": 1000},
            )
        )
        client = self.client_for(self.acknowledger())
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        first = NSOSettlementIssue.objects.get(state="open")
        client.post(self.action_url(), self.form(first, reason="placeholder first carrier"))
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        issue = NSOSettlementIssue.objects.get(state="open")
        self.assertEqual((issue.job_id, issue.position), (41, "historical"))
        result, response = self.race_pass_against_acknowledgement(issue, "placeholder racing historical carrier")
        self.assertContains(response, "Acknowledged adapter job 41 as outcome unknown.")
        self.assertTrue(result.stalled)
        issue.refresh_from_db()
        self.assertEqual(issue.state, "acknowledged")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertEqual(self.management.settle_cursor_seq, 5)
        self.assertTrue(self.management.settle_resume_pending)
        page = client.get(self.tab_url())
        self.assertNotContains(page, "Settlement is paused")
        self.drain_queue()
        self.management.refresh_from_db()
        self.assertFalse(self.management.settle_resume_pending)
        self.assertEqual(self.management.settle_evidence_error, {})

    def test_a_stall_that_only_a_stale_pass_saw_stays_owed(self):
        self.serve_history()
        client = self.client_for(self.acknowledger())
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        issue = NSOSettlementIssue.objects.get(state="open")
        # The pass counts acknowledgements and reads the feed, then waits inside its listing read.
        entered, release = self.hold_next("GET", "/generations")
        pass_thread, passed = self.run_pass({"thread": None})
        self.assertTrue(entered.wait(WAIT))
        client.post(self.action_url(), self.form(issue, reason="placeholder pre-ledger carrier"))
        self.drain_queue()
        self.management.refresh_from_db()
        self.assertFalse(self.management.settle_resume_pending)
        # The adapter then publishes an invalid job that only the held pass will read.
        later = copy.deepcopy(self.history_jobs[0])
        later.update(id=52, type="sync", result=None, settle_seq=6)
        self.history_jobs.append(later)
        self.invalid_feed_job(52)
        release.set()
        self.join(pass_thread)
        self.assertNotIn("error", passed)
        self.assertTrue(passed["result"].stalled)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertTrue(self.management.settle_resume_pending)
        serve_observation(self)
        self.assertEqual(sweep_static_route_settlements(), (1, 0))
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["job_id"], 52)
        self.assertTrue(self.management.settle_resume_pending)
        self.assertEqual(NSOSettlementIssue.objects.get(state="open").job_id, 52)
