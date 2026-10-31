# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Acknowledge settlement stalls from the device NSO tab: authorization, audit, advisories and resume."""

import threading
from unittest.mock import patch
from uuid import uuid4

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, SimpleTestCase
from django.urls import reverse

from netbox_nso_plugin.models import (
    NSOApplyExecution,
    NSODeviceManagement,
    NSOOverlaySettlementFence,
    NSOSettlementIssue,
)
from netbox_nso_plugin.settlement import consume_static_route_settlements, sweep_static_route_settlements

from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import make_managed, mirror_update
from ._refusal_display import RefusalDisplayHelpers
from ._settlement_observation import serve_observation
from .test_durable_bgp_capture import BGPCaptureCase
from .test_refusal_display_parity import OwnerHTML
from .test_request_authorization import _grant
from .test_settlement_issue import ADVISORY, HistoricalStore, SettlementIssueHelpers

ACKNOWLEDGE = "acknowledge_settlement_issue"
STALE = "Acknowledgement refused because the evidence or settlement position changed. Review the current issue."
BUTTON = "Acknowledge outcome unknown"
WARNING = "The device may have changed."
HISTORY = "Settlement acknowledgements"


class ResumeQueueMixin:
    """Send the post-commit settlement pass to a throwaway RQ queue, never the shared default queue."""

    def setUp(self):
        super().setUp()
        import django_rq
        from rq import Queue

        self.queue = Queue(f"nso-1851-{uuid4().hex[:8]}", connection=django_rq.get_queue("default").connection)
        self.addCleanup(self._drop_queue)
        for target in (
            patch("django_rq.get_queue", return_value=self.queue),
            patch("netbox_nso_plugin.reconcile.reconcile_device", return_value={}),
        ):
            target.start()
            self.addCleanup(target.stop)

    def _drop_queue(self):
        from netbox_nso_plugin.read_gate import carrier_key, marker_key

        self.queue.connection.delete(carrier_key(self.device.pk), marker_key(self.device.pk))
        self.queue.delete(delete_jobs=True)

    def queued_devices(self):
        return [job.args[0] for job in self.queue.get_jobs()]

    def drain_queue(self):
        from rq import SimpleWorker

        SimpleWorker([self.queue], connection=self.queue.connection).work(burst=True, with_scheduler=False)
        self.assertEqual(self.queue.failed_job_registry.get_job_ids(), [])


class AcknowledgementViewHelpers(ResumeQueueMixin, SettlementIssueHelpers, RefusalDisplayHelpers):
    def tab_url(self, device=None):
        return reverse("dcim:device_nso", args=((device or self.device).pk,))

    def action_url(self, device=None):
        return reverse("plugins:netbox_nso_plugin:settlement_issue_acknowledge", args=((device or self.device).pk,))

    def acknowledger(self, *, actions=("change", ACKNOWLEDGE), constraints=None):
        user = get_user_model().objects.create_user(username=f"placeholder-acknowledger-{uuid4().hex[:8]}")
        _grant(user, ("view",), Device)
        _grant(user, ("view",), NSODeviceManagement)
        for action in actions:
            _grant(user, (action,), NSODeviceManagement, constraints)
        return user

    def client_for(self, user, **kwargs):
        client = Client(**kwargs)
        client.force_login(user)
        return client

    def form(self, issue, reason="placeholder carrier result is unreadable", **values):
        return {"issue": issue.pk, "revision_digest": issue.revision_digest, "reason": reason, **values}

    def effects(self):
        """Capture every write an acknowledgement could make."""
        self.management.refresh_from_db()
        return (
            list(NSOSettlementIssue.objects.order_by("pk").values_list("state", "actor_id")),
            list(NSOApplyExecution.objects.order_by("pk").values_list("consumption_state", "settlement_issue_id")),
            self.owner_state(),
            list(NSOOverlaySettlementFence.objects.order_by("pk").values_list("bookkeeping_revision", flat=True)),
            (self.management.settle_cursor_seq, self.management.settle_resume_pending),
            self.queued_devices(),
        )


class TestAcknowledgementAction(AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def test_the_tab_shows_the_exact_issue_and_an_authorized_operator_acknowledges_it(self):
        issue = self.stall_invalid_manual_carrier()
        client = self.client_for(self.acknowledger())
        tab = client.get(self.tab_url())
        self.assertContains(tab, "Settlement is paused", count=1)
        self.assertContains(tab, "Adapter job 501, settle_seq 1")
        self.assertContains(tab, "Stall class: invalid evidence")
        self.assertContains(tab, "Affected generations: 1 (81)")
        self.assertContains(tab, "Affected rows: 3 outcome unknown, 0 kept with a newer state, 0 missing.")
        for row in self.owners:
            self.assertContains(tab, f'data-owner="{row._meta.label_lower}:{row.pk}"', count=1)
        self.assertContains(tab, WARNING)
        self.assertContains(tab, f'name="revision_digest" value="{issue.revision_digest}"', count=1)
        self.assertContains(tab, f'name="issue" value="{issue.pk}"', count=1)
        self.assertContains(tab, BUTTON, count=1)
        self.assertNotContains(tab, HISTORY)

        observed = {}

        def record(device_id):
            observed["atomic"] = connection.in_atomic_block
            observed["issue"] = NSOSettlementIssue.objects.get(pk=issue.pk).state
            observed["resume"] = NSODeviceManagement.objects.get(pk=self.management.pk).settle_resume_pending
            return real(device_id)

        from netbox_nso_plugin import reconcile

        real = reconcile.enqueue_device_reconcile
        with patch("netbox_nso_plugin.reconcile.enqueue_device_reconcile", side_effect=record):
            response = client.post(self.action_url(), self.form(issue, reason="  placeholder unreadable carrier  "))
        self.assertRedirects(response, self.tab_url(), fetch_redirect_response=False)
        self.assertEqual(observed, {"atomic": False, "issue": "acknowledged", "resume": True})
        self.assertEqual(self.queued_devices(), [self.device.pk])
        issue.refresh_from_db()
        self.assertEqual(issue.acknowledgement_reason, "placeholder unreadable carrier")
        self.assertEqual(issue.actor_label, get_user_model().objects.get(pk=issue.actor_id).username)
        self.management.refresh_from_db()
        self.assertEqual((self.management.settle_cursor_seq, self.management.settle_resume_pending), (1, True))

        page = client.get(self.tab_url())
        self.assertContains(page, "Acknowledged adapter job 501 as outcome unknown. Settlement resumes.")
        self.assertNotContains(page, "Settlement is paused")
        self.assertNotContains(page, BUTTON)
        self.assertContains(page, HISTORY, count=1)
        self.assertContains(page, issue.actor_label)
        self.assertContains(page, "placeholder unreadable carrier")
        self.assertContains(page, 'data-acknowledgement-job="501"', count=1)
        self.assertContains(page, 'data-acknowledgement-generations="81"', count=1)
        self.assertContains(page, "Cursor 0 to 1")
        for row in self.owners:
            self.assertContains(page, f'data-effect="{row._meta.label_lower}:{row.pk}:unavailable"', count=1)

        self.drain_queue()
        self.management.refresh_from_db()
        self.assertFalse(self.management.settle_resume_pending)

    def test_the_tab_offers_only_the_issue_published_for_the_current_fence(self):
        from netbox_nso_plugin.settlement_evidence import request_fence

        issue = self.stall_invalid_manual_carrier()
        self.management.refresh_from_db()
        diagnostic = dict(self.management.settle_evidence_error)
        mirror_update(self.management, settle_cursor_seq=0 if issue.request_fence["cursor_seq"] is None else None)
        current = request_fence(NSODeviceManagement.objects.get(pk=self.management.pk))
        self.assertNotEqual(current, issue.request_fence)
        mirror_update(self.management, settle_evidence_error={**diagnostic, "request_fence": current})
        tab = self.client_for(self.acknowledger()).get(self.tab_url())
        self.assertContains(tab, "Settlement is paused", count=1)
        self.assertContains(tab, "The exit for this stall is not evaluated yet.")
        self.assertNotContains(tab, BUTTON)
        self.assertNotContains(tab, issue.revision_digest)

    def test_a_viewer_without_the_acknowledge_permission_sees_the_issue_without_the_action(self):
        issue = self.stall_invalid_manual_carrier()
        tab = self.client_for(self.acknowledger(actions=("change",))).get(self.tab_url())
        self.assertContains(tab, "Affected generations: 1 (81)")
        self.assertContains(tab, WARNING)
        self.assertNotContains(tab, BUTTON)
        self.assertNotContains(tab, issue.revision_digest)
        self.assertContains(tab, "Acknowledging needs the change and acknowledge_settlement_issue permissions")

    def test_a_repeated_post_and_a_post_after_a_lost_response_return_the_receipt(self):
        issue = self.stall_invalid_manual_carrier()
        client = self.client_for(self.acknowledger())
        client.post(self.action_url(), self.form(issue, reason="placeholder first reason"))
        before = self.effects()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                response = client.post(
                    self.action_url(), self.form(issue, reason="placeholder second reason"), follow=True
                )
                self.assertContains(response, "Adapter job 501 was already acknowledged as outcome unknown.")
                self.assertEqual(self.effects(), before)
        issue.refresh_from_db()
        self.assertEqual(issue.acknowledgement_reason, "placeholder first reason")
        self.assertEqual(self.queued_devices(), [self.device.pk])

    def test_a_lost_enqueue_after_commit_is_resumed_by_the_sweep(self):
        from redis.exceptions import ConnectionError as RedisConnectionError

        issue = self.stall_invalid_manual_carrier()
        client = self.client_for(self.acknowledger())
        with patch(
            "netbox_nso_plugin.reconcile.enqueue_device_reconcile", side_effect=RedisConnectionError("placeholder")
        ):
            response = client.post(self.action_url(), self.form(issue), follow=True)
        self.assertContains(response, "Acknowledged adapter job 501 as outcome unknown.")
        self.assertEqual(self.queued_devices(), [])
        self.management.refresh_from_db()
        self.assertTrue(self.management.settle_resume_pending)
        self.assertEqual(sweep_static_route_settlements(), (1, 0))
        self.management.refresh_from_db()
        self.assertFalse(self.management.settle_resume_pending)

    def test_a_concurrent_post_commits_once_and_the_other_gets_the_receipt(self):
        issue = self.stall_invalid_manual_carrier()
        entered, release = threading.Event(), threading.Event()
        original = self.adapter._handle

        def held(method, url, **kwargs):
            if url.endswith("/settlement-observation") and not entered.is_set():
                entered.set()
                release.wait(10)
            return original(method, url, **kwargs)

        self.adapter._handle = held
        responses = {}

        def submit(name, reason):
            try:
                responses[name] = self.client_for(self.acknowledger()).post(
                    self.action_url(), self.form(issue, reason=reason), follow=True
                )
            finally:
                connections.close_all()

        first = threading.Thread(target=submit, args=("first", "placeholder first reason"))
        first.start()
        self.assertTrue(entered.wait(10))
        second = threading.Thread(target=submit, args=("second", "placeholder second reason"))
        second.start()
        self.wait_for_lock_waiter()
        release.set()
        first.join(30)
        second.join(30)
        self.assertContains(responses["first"], "Acknowledged adapter job 501 as outcome unknown.")
        self.assertContains(responses["second"], "Adapter job 501 was already acknowledged as outcome unknown.")
        issue.refresh_from_db()
        self.assertEqual(issue.acknowledgement_reason, "placeholder first reason")
        self.assertEqual(NSOApplyExecution.objects.filter(consumption_state="acknowledged").count(), 1)
        self.assertEqual(self.queued_devices(), [self.device.pk])

    def wait_for_lock_waiter(self, timeout=10.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid())"
                )
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.01)
        self.fail("the second acknowledgement never waited on a lock")

    def test_a_stale_submission_is_refused_for_each_fence_field_and_for_changed_evidence(self):
        issue = self.stall_invalid_manual_carrier()
        client = self.client_for(self.acknowledger())
        reason = "placeholder reason that must not carry over"
        self.management.refresh_from_db()
        changes = {
            "mapping_epoch": ("adapter_source_epoch", (self.management.adapter_source_epoch or 0) + 1),
            "cursor_device_id": ("settle_cursor_device_id", 9999),
            "cursor_incarnation": ("settle_cursor_incarnation", "00000000-0000-4000-8000-0000000000ff"),
            "cursor_seq": ("settle_cursor_seq", 7),
        }
        before = self.effects()
        for fence_field, (column, value) in changes.items():
            with self.subTest(fence=fence_field):
                original = getattr(NSODeviceManagement.objects.get(pk=self.management.pk), column)
                mirror_update(self.management, **{column: value})
                response = client.post(self.action_url(), self.form(issue, reason=reason), follow=True)
                self.assertContains(response, STALE)
                self.assertNotContains(response, reason)
                self.assertNotContains(response, "Settlement is paused")
                self.assertNotContains(response, BUTTON)
                mirror_update(self.management, **{column: original})
                self.assertEqual(self.effects(), before)
        serve_observation(self, edit=lambda document: document["job"].update(digest="c" * 64))
        response = client.post(self.action_url(), self.form(issue, reason=reason), follow=True)
        self.assertContains(response, STALE)
        self.assertNotContains(response, reason)
        self.assertEqual(self.effects(), before)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        revision = NSOSettlementIssue.objects.get(state="open")
        self.assertNotEqual(revision.pk, issue.pk)
        page = client.get(self.tab_url())
        self.assertContains(page, f'name="issue" value="{revision.pk}"', count=1)
        self.assertNotContains(page, reason)
        stale = client.post(self.action_url(), self.form(issue, reason=reason), follow=True)
        self.assertContains(stale, STALE)
        self.assertNotContains(stale, reason)

    def test_an_unavailable_adapter_and_an_empty_reason_change_nothing(self):
        from ._adapter_http import make_response

        issue = self.stall_invalid_manual_carrier()
        client = self.client_for(self.acknowledger())
        before = self.effects()
        response = client.post(self.action_url(), self.form(issue, reason="   "), follow=True)
        self.assertContains(response, "Acknowledgement refused: enter a reason of at most 2000 characters.")
        self.assertEqual(self.effects(), before)
        original = self.adapter._handle

        def unavailable(method, url, **kwargs):
            if url.endswith("/settlement-observation"):
                return make_response(503, {"error": {"code": "nso_unreachable", "message": "placeholder outage"}})
            return original(method, url, **kwargs)

        self.adapter._handle = unavailable
        response = client.post(self.action_url(), self.form(issue), follow=True)
        self.assertContains(response, "Acknowledgement refused because the adapter evidence could not be read.")
        self.assertEqual(self.effects(), before)

    def test_malformed_inputs_are_rejected_before_any_effect(self):
        issue = self.stall_invalid_manual_carrier()
        client = self.client_for(self.acknowledger())
        before = self.effects()
        observed = len(self.observation_requests)
        for label, values in (
            ("missing issue", {"revision_digest": issue.revision_digest, "reason": "placeholder"}),
            ("non-numeric issue", {**self.form(issue), "issue": "placeholder"}),
            ("missing digest", {"issue": issue.pk, "reason": "placeholder"}),
            ("malformed digest", self.form(issue, revision_digest="F" * 64)),
        ):
            with self.subTest(input=label):
                self.assertEqual(client.post(self.action_url(), values).status_code, 400)
                self.assertEqual(self.effects(), before)
        self.assertEqual(len(self.observation_requests), observed)

    def test_acknowledged_rows_show_the_job_advisory_and_never_the_success_state(self):
        issue = self.stall_invalid_manual_carrier()
        self.client_for(self.acknowledger()).post(self.action_url(), self.form(issue))
        expected = {
            "state": "apply_failed",
            "label": "outcome unknown",
            "pending": True,
            "advisory": ADVISORY.format(501),
            "error": None,
        }
        parsed = OwnerHTML()
        parsed.feed(self.category("bgp").content.decode())
        payload = dict(_owner_rows(self.category("bgp", format="json").json()))
        for row in self.owners:
            key = (row._meta.model_name, row.pk)
            with self.subTest(owner=key):
                for surface in (parsed.owners[key], payload[key]):
                    self.assertEqual({name: surface[name] for name in expected}, expected)

    def test_a_collection_reads_the_acknowledged_outcomes_once(self):
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.refusal_display import project_rows, row_projection

        issue = self.stall_invalid_manual_carrier()
        self.client_for(self.acknowledger()).post(self.action_url(), self.form(issue))
        rows = [type(row).objects.get(pk=row.pk) for row in self.owners]
        with CaptureQueriesContext(connection) as batched:
            project_rows(rows)
        with CaptureQueriesContext(connection) as projected:
            states = [row_projection(row)["label"] for row in rows]
        self.assertEqual(states, ["outcome unknown"] * len(rows))
        owner_reads = [query for query in batched if "nsosettlementissueowner" in query["sql"]]
        self.assertEqual(len(owner_reads), 1)
        self.assertFalse([query for query in projected if "nsosettlementissueowner" in query["sql"]])


def _owner_rows(value):
    if isinstance(value, dict):
        if "model" in value and "pk" in value:
            yield (value["model"], value["pk"]), value
        for child in value.values():
            yield from _owner_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from _owner_rows(child)


class TestAcknowledgementAuthorization(AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def test_each_missing_permission_or_restriction_is_refused_without_effects(self):
        issue = self.stall_invalid_manual_carrier()
        other_device, other = make_managed("placeholder-other", 1559, index=2)
        before = self.effects()
        observed = len(self.observation_requests)
        cases = (
            ("no change permission", {"actions": (ACKNOWLEDGE,)}, 403),
            ("no acknowledge permission", {"actions": ("change",)}, 403),
            ("view only", {"actions": ()}, 403),
            ("change restricted elsewhere", {"restricted": "change"}, 404),
            ("acknowledge restricted elsewhere", {"restricted": ACKNOWLEDGE}, 404),
        )
        for label, case, status in cases:
            with self.subTest(case=label):
                user = self.acknowledger(actions=case.get("actions", ()))
                if "restricted" in case:
                    for action in ("change", ACKNOWLEDGE):
                        constraints = {"pk": other.pk} if action == case["restricted"] else None
                        _grant(user, (action,), NSODeviceManagement, constraints)
                response = self.client_for(user).post(self.action_url(), self.form(issue))
                self.assertEqual(response.status_code, status)
                self.assertEqual(self.effects(), before)
        with self.subTest(case="issue of another management"):
            permitted = self.acknowledger()
            response = self.client_for(permitted).post(self.action_url(other_device), self.form(issue))
            self.assertEqual(response.status_code, 404)
        with self.subTest(case="anonymous"):
            response = Client().post(self.action_url(), self.form(issue))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response["Location"])
        with self.subTest(case="get"):
            self.assertEqual(self.client_for(self.acknowledger()).get(self.action_url()).status_code, 405)
        with self.subTest(case="missing csrf token"):
            client = self.client_for(self.acknowledger(), enforce_csrf_checks=True)
            self.assertEqual(client.post(self.action_url(), self.form(issue)).status_code, 403)
        self.assertEqual(self.effects(), before)
        self.assertEqual(len(self.observation_requests), observed)

    def test_the_restricted_management_still_permits_its_own_acknowledgement(self):
        issue = self.stall_invalid_manual_carrier()
        user = self.acknowledger(constraints={"pk": self.management.pk})
        response = self.client_for(user).post(self.action_url(), self.form(issue), follow=True)
        self.assertContains(response, "Acknowledged adapter job 501 as outcome unknown.")
        issue.refresh_from_db()
        self.assertEqual(issue.state, "acknowledged")


class TestHistoricalAcknowledgementView(AcknowledgementViewHelpers, HistoricalStore, BGPCaptureCase):
    def stall_history(self):
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        return NSOSettlementIssue.objects.filter(management=self.management, state="open").first()

    def test_a_historical_acknowledgement_says_the_cursor_was_already_past_the_carrier(self):
        self.serve_history()
        issue = self.stall_history()
        client = self.client_for(self.acknowledger())
        tab = client.get(self.tab_url())
        self.assertContains(tab, "Closed reason: <code>missing_generation_evidence</code>", html=True)
        self.assertContains(tab, "The cursor is already past the carrier of this job. The cursor does not move.")
        self.assertContains(tab, "Affected rows: 0 outcome unknown, 0 kept with a newer state, 0 missing.")
        response = client.post(
            self.action_url(), self.form(issue, reason="placeholder pre-ledger carrier"), follow=True
        )
        self.assertContains(response, "Acknowledged adapter job 40 as outcome unknown.")
        self.assertContains(response, 'data-acknowledgement-job="40"', count=1)
        self.assertContains(response, "The cursor was already past the carrier of this job.")
        self.assertContains(response, "No rows")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 5)

    def test_a_stall_without_an_exit_shows_its_reason_and_no_action(self):
        self.serve_history()
        attempt = self.manual_reference()
        owner = self.router_owner()
        mirror_update(owner, status="deploying", apply_attempt_id=attempt.pk)
        self.assertIsNone(self.stall_history())
        tab = self.client_for(self.acknowledger()).get(self.tab_url())
        self.assertContains(tab, "Settlement is paused", count=1)
        self.assertContains(tab, "The rows that this job can affect cannot be bounded.")
        self.assertContains(tab, "No acknowledgement is available for this stall.")
        self.assertNotContains(tab, BUTTON)
        self.assertNotContains(tab, WARNING)

    def test_an_unidentified_stall_shows_evidence_unavailable(self):
        from ._adapter_http import make_response
        from .test_durable_bgp_capture import _CaptureHandler

        self.serve_history()
        original = self.adapter._handle

        def headerless(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                return make_response(200, [])
            return original(method, url, **kwargs)

        from netbox_nso_plugin import adapter_client

        self.server.RequestHandlerClass = _CaptureHandler
        self.adapter._handle = headerless
        adapter_client.reset_session()
        self.assertIsNone(self.stall_history())
        tab = self.client_for(self.acknowledger()).get(self.tab_url())
        self.assertContains(tab, "Settlement is paused: invalid settlement evidence.", html=True)
        self.assertContains(tab, "Evidence unavailable: the job or its evidence store cannot be identified.")
        self.assertNotContains(tab, BUTTON)


class TestMissingOwnerHistory(AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def test_missing_owners_stay_visible_in_the_banner_and_the_history(self):
        self.prepare()
        self.serve_terminal_evidence(self.terminal(refused=None), manual=True)
        serve_observation(self)
        missing = self.owners[1]
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {missing._meta.db_table} WHERE id = %s", [missing.pk])
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        issue = NSOSettlementIssue.objects.get(state="open")
        client = self.client_for(self.acknowledger())
        tab = client.get(self.tab_url())
        self.assertContains(tab, "Affected rows: 2 outcome unknown, 0 kept with a newer state, 1 missing.")
        self.assertContains(tab, f'data-owner="{missing._meta.label_lower}:{missing.pk}"', count=1)
        page = client.post(self.action_url(), self.form(issue), follow=True)
        label = missing._meta.label_lower
        self.assertContains(page, f'data-effect="{label}:{missing.pk}:missing_owner"', count=1)
        self.assertContains(page, f"{missing._meta.verbose_name} #{missing.pk} (row deleted)")


class TestSettlementResume(AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def acknowledged(self):
        issue = self.stall_invalid_manual_carrier()
        self.client_for(self.acknowledger()).post(self.action_url(), self.form(issue))
        self.management.refresh_from_db()
        self.assertTrue(self.management.settle_resume_pending)
        self.assertEqual(self.queued_devices(), [self.device.pk])
        return issue

    def test_the_flag_commits_with_the_acknowledgement_and_a_drained_pass_clears_it(self):
        self.acknowledged()
        self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.management.refresh_from_db()
        self.assertFalse(self.management.settle_resume_pending)

    def test_a_pass_that_stalls_again_keeps_the_flag_and_the_sweep_keeps_polling(self):
        self.acknowledged()
        self.append_feed_jobs(self.sync_job(502, 2))
        self.invalid_feed_job(502)
        polled = self.record_sweeps(2)
        self.assertEqual(polled, [self.management.pk, self.management.pk])
        self.management.refresh_from_db()
        self.assertTrue(self.management.settle_resume_pending)
        self.assertEqual(self.management.settle_evidence_error["job_id"], 502)

    def record_sweeps(self, ticks):
        from netbox_nso_plugin import settlement

        polled = []
        real = settlement.settle_static_routes

        def record(pk):
            polled.append(pk)
            return real(pk)

        with patch.object(settlement, "settle_static_routes", side_effect=record):
            for _tick in range(ticks):
                sweep_static_route_settlements()
        return polled

    def test_only_a_flagged_device_without_other_work_is_swept(self):
        self.assertEqual(self.record_sweeps(1), [])
        mirror_update(self.management, settle_resume_pending=True)
        self.assertEqual(self.record_sweeps(1), [self.management.pk])

    def test_a_pass_that_began_before_the_flag_was_set_does_not_clear_it(self):
        self.acknowledged()
        self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.management.refresh_from_db()
        self.assertFalse(self.management.settle_resume_pending)
        original = self.adapter._handle
        armed = []

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs") and not armed:
                armed.append(True)
                self.elsewhere(settle_resume_pending=True)
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.assertEqual(armed, [True])
        self.management.refresh_from_db()
        self.assertTrue(self.management.settle_resume_pending)
        self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.management.refresh_from_db()
        self.assertFalse(self.management.settle_resume_pending)

    def test_a_cursor_change_before_the_clear_keeps_the_flag(self):
        from netbox_nso_plugin import settlement

        self.acknowledged()
        real = settlement._complete_evidence_result
        moved = []

        def move(*args, **kwargs):
            result = real(*args, **kwargs)
            moved.append(True)
            self.elsewhere(settle_cursor_seq=3)
            return result

        with patch.object(settlement, "_complete_evidence_result", side_effect=move):
            self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.assertEqual(moved, [True])
        self.management.refresh_from_db()
        self.assertTrue(self.management.settle_resume_pending)

    def elsewhere(self, **values):
        """Commit a change from a separate connection, as a concurrent writer would."""

        def write():
            try:
                mirror_update(NSODeviceManagement.objects.get(pk=self.management.pk), **values)
            finally:
                connections.close_all()

        worker = threading.Thread(target=write)
        worker.start()
        worker.join(10)


class TestSettlementDisplayText(SimpleTestCase):
    def test_every_closed_code_has_display_text(self):
        from netbox_nso_plugin.refusal_ledger import OWNER_EFFECTS
        from netbox_nso_plugin.settlement_acknowledgement import REFUSAL_CODES
        from netbox_nso_plugin.settlement_display import EFFECT_BADGE, EFFECT_TEXT, EXIT_TEXT, REFUSAL_TEXT
        from netbox_nso_plugin.settlement_issue import EXIT_ACKNOWLEDGEABLE, EXIT_CODES

        self.assertEqual(set(EXIT_TEXT), EXIT_CODES - {EXIT_ACKNOWLEDGEABLE})
        self.assertEqual(set(REFUSAL_TEXT), REFUSAL_CODES)
        self.assertEqual(set(EFFECT_TEXT), set(OWNER_EFFECTS))
        self.assertEqual(set(EFFECT_BADGE), set(OWNER_EFFECTS))
        self.assertFalse([badge for badge in EFFECT_BADGE.values() if "secondary" in badge])
        self.assertEqual({REFUSAL_TEXT[code] for code in ("stale", "revision_mismatch", "not_open")}, {STALE})
