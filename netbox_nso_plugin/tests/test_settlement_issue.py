# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Publish identified settlement stalls and acknowledge them as outcome unknown."""

import copy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.models import (
    NSOApplyAttempt,
    NSOApplyExecution,
    NSOApplyOverlaySnapshot,
    NSOOverlaySettlementFence,
    NSOSettlementIssue,
)
from netbox_nso_plugin.settlement import consume_static_route_settlements
from netbox_nso_plugin.settlement_acknowledgement import AcknowledgementRefused, acknowledge
from netbox_nso_plugin.settlement_issue import publish_stall_issue

from ._adapter_http import make_generation_listing, make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._settlement_observation import INCARNATION, failed_item, published_job, serve_observation
from ._settlement_wire import generation_wire, job_wire
from .test_coalesced_settlement import CoalescedCarrierHelpers
from .test_durable_bgp_capture import BGPCaptureCase, _CaptureHandler
from .test_settlement_prefix import CarrierPageHelpers

ADVISORY = "Outcome unknown for adapter job {}. Settlement evidence was acknowledged. This does not certify deployment."


class SettlementIssueHelpers:
    def operator(self, name="placeholder-settlement-operator"):
        from uuid import uuid4

        return get_user_model().objects.create_user(username=f"{name}-{uuid4().hex[:8]}")

    def invalid_feed_job(self, job_id=501):
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs") and response.status_code == 200:
                rows = [failed_item(item) if item["identity"]["id"] == job_id else item for item in response.json()]
                return make_response(200, rows)
            return response

        self.adapter._handle = handle

    def append_feed_jobs(self, *jobs):
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs") and response.status_code == 200:
                after = int(kwargs["params"]["after_settle_seq"])
                rows = response.json() + [job_wire(job) for job in jobs if job["settle_seq"] > after]
                return make_response(200, rows)
            return response

        self.adapter._handle = handle

    def sync_job(self, job_id, settle_seq):
        return {
            "id": job_id,
            "type": "sync",
            "device_id": self.management.adapter_device_id,
            "status": "succeeded",
            "result": None,
            "row_outcomes": None,
            "refusals": None,
            "error": None,
            "context": None,
            "created_at": self.stamp.isoformat(),
            "updated_at": self.stamp.isoformat(),
            "started_at": None,
            "heartbeat_at": None,
            "settle_seq": settle_seq,
        }

    def stall_invalid_manual_carrier(self):
        """A manual Apply whose carrier job 501 holds an identified invalid result."""
        self.prepare()
        self.generation = self.terminal(refused=None)
        self.serve_terminal_evidence(self.generation, manual=True)
        self.invalid_feed_job()
        serve_observation(self)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        return NSOSettlementIssue.objects.get(management=self.management, state="open")

    def owner_state(self):
        rows = []
        for owner in self.owners:
            owner.refresh_from_db()
            rows.append((owner.status, owner.last_apply_error, owner.apply_attempt_id, owner.last_apply_at))
        return rows


class TestSettlementIssueAcknowledgement(SettlementIssueHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def test_an_identified_invalid_carrier_publishes_one_exact_issue_revision(self):
        issue = self.stall_invalid_manual_carrier()
        self.assertEqual((issue.job_id, issue.settle_seq, issue.position), (501, 1, "next_feed"))
        self.assertEqual((issue.cursor_before, issue.cursor_after), (0, 1))
        self.assertEqual(issue.stall_class, "invalid_evidence")
        self.assertEqual(issue.store_incarnation, INCARNATION)
        self.assertEqual(
            issue.proposal["obligations"],
            [
                {
                    "generation_id": 81,
                    "carrier_job_id": 501,
                    "generation_seq": 4,
                    "attempt_id": str(self.attempt.pk),
                    "placeholder_execution_id": None,
                }
            ],
        )
        owners = {(owner.model_label, owner.overlay_pk): owner for owner in issue.owners.all()}
        self.assertEqual(set(owners), {(row._meta.label_lower, row.pk) for row in self.owners})
        for owner in owners.values():
            self.assertEqual(owner.proposed_effect, "unavailable")
            self.assertIsNone(owner.committed_effect)
            self.assertEqual(owner.attempt_id, self.attempt.pk)
            self.assertEqual(owner.unknown, owner.contributors)
            snapshot = NSOApplyOverlaySnapshot.objects.get(pk__in=owner.snapshot_ids)
            self.assertEqual(snapshot.attempt_id, self.attempt.pk)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["exit"], "acknowledgeable")
        self.assertEqual(self.management.settle_evidence_error["class"], "invalid_evidence")
        self.assertFalse(NSOApplyExecution.objects.exists())

    def test_publication_is_pure_reads_apart_from_the_issue_and_its_exit(self):
        self.prepare()
        self.generation = self.terminal(refused=None)
        self.serve_terminal_evidence(self.generation, manual=True)
        self.invalid_feed_job()
        serve_observation(self)
        with patch("netbox_nso_plugin.settlement._publish_stall_issue"):
            consume_static_route_settlements(self.management)
        writes = []

        def record(execute, sql, params, many, context):
            if sql.split(" ", 1)[0] in {"INSERT", "UPDATE", "DELETE"}:
                writes.append(sql.split('"')[1])
            return execute(sql, params, many, context)

        refused = AssertionError("publication must not replay, load or record evidence")
        with (
            patch("netbox_nso_plugin.adapter_client.trigger_apply", side_effect=refused),
            patch("netbox_nso_plugin.apply_settlement.load_deployment_evidence", side_effect=refused),
            patch("netbox_nso_plugin.outcome_settlement._record_invariant", side_effect=refused),
            connection.execute_wrapper(record),
        ):
            issue = publish_stall_issue(self.management)
        self.assertIsNotNone(issue)
        self.assertEqual(
            set(writes),
            {
                "netbox_nso_plugin_nsodevicemanagement",
                "netbox_nso_plugin_nsosettlementissue",
                "netbox_nso_plugin_nsosettlementissueowner",
            },
        )
        self.assertEqual([request["job_id"] for request in self.observation_requests], [501])

    def test_republishing_unchanged_evidence_keeps_the_revision_and_changed_evidence_supersedes_it(self):
        issue = self.stall_invalid_manual_carrier()
        self.assertEqual(publish_stall_issue(self.management).pk, issue.pk)
        self.assertEqual(NSOSettlementIssue.objects.count(), 1)

        def change(document):
            document["job"]["digest"] = "c" * 64
            document["job"]["evidence_error"]["digest"] = "c" * 64

        serve_observation(self, edit=change)
        revision = publish_stall_issue(self.management)
        self.assertNotEqual(revision.pk, issue.pk)
        self.assertNotEqual(revision.revision_digest, issue.revision_digest)
        issue.refresh_from_db()
        self.assertEqual(issue.state, "superseded")
        self.assertEqual(revision.state, "open")
        self.assertEqual((revision.job_id, revision.store_incarnation), (issue.job_id, issue.store_incarnation))

    def test_acknowledgement_records_outcome_unknown_and_advances_past_exactly_that_job(self):
        issue = self.stall_invalid_manual_carrier()
        operator = self.operator()
        applied = {}
        for owner in self.owners:
            owner.refresh_from_db()
            applied[owner.pk] = owner.last_apply_at
        with patch("netbox_nso_plugin.adapter_client.trigger_apply", side_effect=AssertionError("no Apply")):
            receipt = acknowledge(issue, issue.revision_digest, operator, "  the carrier result cannot be read  ")
        self.assertFalse(receipt.replayed)
        issue.refresh_from_db()
        self.assertEqual(issue.state, "acknowledged")
        self.assertEqual(issue.actor_id, operator.pk)
        self.assertEqual(issue.actor_label, operator.username)
        self.assertEqual(issue.acknowledgement_reason, "the carrier result cannot be read")
        self.assertIsNotNone(issue.acknowledged_at.utcoffset())
        self.assertEqual(
            sorted(issue.committed_effects, key=lambda row: row["overlay_pk"]),
            sorted(
                (
                    {"model_label": row._meta.label_lower, "overlay_pk": row.pk, "effect": "unavailable"}
                    for row in self.owners
                ),
                key=lambda row: row["overlay_pk"],
            ),
        )
        execution = NSOApplyExecution.objects.get()
        self.assertEqual(
            (execution.generation_id, execution.carrier_job_id, execution.settle_seq, execution.consumption_state),
            (81, 501, 1, "acknowledged"),
        )
        self.assertEqual(execution.settlement_issue, issue)
        self.assertEqual(execution.attempt, self.attempt)
        self.assertIsNone(execution.terminal_projection)
        self.assertIsNone(execution.commit_acknowledged)
        self.assertIsNone(execution.consumed_at)
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "apply_failed")
            self.assertEqual(owner.last_apply_error, ADVISORY.format(501))
            self.assertEqual(owner.apply_attempt_id, self.attempt.pk)
            self.assertEqual(owner.last_apply_at, applied[owner.pk])
            fence = NSOOverlaySettlementFence.objects.get(model_label=owner._meta.label_lower, overlay_pk=owner.pk)
            self.assertEqual((fence.current_attempt_id, fence.last_execution_id), (self.attempt.pk, execution.pk))
        self.assertEqual(
            set(self.attempt.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"unavailable"}
        )
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.lifecycle, "terminal")
        self.management.refresh_from_db()
        self.assertEqual(
            (self.management.settle_cursor_seq, self.management.settle_cursor_incarnation),
            (1, INCARNATION),
        )
        self.assertEqual(self.management.settle_evidence_error, {})
        self.append_feed_jobs(self.sync_job(502, 2))
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.consumed, result.cursor, result.stalled), (1, 2, False))
        self.assertEqual([owner[0] for owner in self.owner_state()], ["apply_failed"] * len(self.owners))

    def test_a_repeated_acknowledgement_returns_the_receipt_without_writes(self):
        issue = self.stall_invalid_manual_carrier()
        acknowledge(issue, issue.revision_digest, self.operator(), "first")
        issue.refresh_from_db()
        owners = self.owner_state()
        fences = list(NSOOverlaySettlementFence.objects.order_by("pk").values_list("bookkeeping_revision", flat=True))
        writes = []

        def record(execute, sql, params, many, context):
            if sql.split(" ", 1)[0] in {"INSERT", "UPDATE", "DELETE"}:
                writes.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(record):
            receipt = acknowledge(issue, issue.revision_digest, self.operator("placeholder-second-operator"), "again")
        self.assertTrue(receipt.replayed)
        self.assertEqual(receipt.issue.acknowledgement_reason, "first")
        self.assertEqual([sql for sql in writes if "nsoapplyexecution" in sql or "nsosettlementissue" in sql], [])
        self.assertEqual(self.owner_state(), owners)
        self.assertEqual(
            list(NSOOverlaySettlementFence.objects.order_by("pk").values_list("bookkeeping_revision", flat=True)),
            fences,
        )
        self.assertEqual(NSOApplyExecution.objects.count(), 1)

    def assert_refused(self, issue, code, digest=None, reason="operator reason", actor=None):
        owners = self.owner_state()
        self.management.refresh_from_db()
        cursor = self.management.settle_cursor_seq
        with self.assertRaises(AcknowledgementRefused) as refused:
            acknowledge(issue, digest or issue.revision_digest, actor or self.operator(), reason)
        self.assertEqual(refused.exception.code, code)
        issue.refresh_from_db()
        self.assertNotEqual(issue.state, "acknowledged")
        self.assertEqual(self.owner_state(), owners)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="acknowledged").exists())
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, cursor)

    def test_an_acknowledgement_for_another_revision_is_refused(self):
        issue = self.stall_invalid_manual_carrier()
        self.assert_refused(issue, "revision_mismatch", digest="f" * 64)

    def test_an_empty_reason_and_an_anonymous_actor_are_refused(self):
        from django.contrib.auth.models import AnonymousUser

        issue = self.stall_invalid_manual_carrier()
        self.assert_refused(issue, "invalid_reason", reason="   ")
        self.assert_refused(issue, "invalid_actor", actor=AnonymousUser())

    def test_changed_remote_evidence_refuses_the_displayed_revision(self):
        issue = self.stall_invalid_manual_carrier()
        for change in (
            lambda document: document["job"].update(digest="c" * 64),
            lambda document: document["generations"][0].update(digest="d" * 64),
            lambda document: document["deployment"].update(head=None),
            lambda document: document["absent"].update(receipt_sections=["bgp"]),
        ):
            with self.subTest(change=change):
                serve_observation(self, edit=change)
                self.assert_refused(issue, "stale")

    def test_a_changed_request_fence_refuses_before_any_effect(self):
        from ._outbox_case import mirror_update

        issue = self.stall_invalid_manual_carrier()
        mirror_update(self.management, settle_cursor_seq=7)
        self.assert_refused(issue, "stale")

    def test_changed_owner_authority_refuses_the_displayed_revision(self):
        from ._outbox_case import mirror_update

        issue = self.stall_invalid_manual_carrier()
        mirror_update(self.owners[2], last_apply_error="placeholder changed bookkeeping")
        self.assert_refused(issue, "stale")

    def test_an_unavailable_adapter_changes_nothing(self):
        issue = self.stall_invalid_manual_carrier()
        original = self.adapter._handle

        def unavailable(method, url, **kwargs):
            if url.endswith("/settlement-observation"):
                return make_response(503, {"error": {"code": "nso_unreachable", "message": "placeholder outage"}})
            return original(method, url, **kwargs)

        self.adapter._handle = unavailable
        self.assert_refused(issue, "observation_unavailable")

    def test_a_released_owner_is_superseded_and_keeps_its_state(self):
        from ._outbox_case import content_update

        self.prepare()
        self.serve_terminal_evidence(self.terminal(refused=None), manual=True)
        self.invalid_feed_job()
        serve_observation(self)
        with patch("netbox_nso_plugin.settlement._publish_stall_issue"):
            consume_static_route_settlements(self.management)
        released = self.owners[2]
        content_update(released, status="imported")
        issue = publish_stall_issue(self.management)
        effects = {(owner.model_label, owner.overlay_pk): owner.proposed_effect for owner in issue.owners.all()}
        self.assertEqual(effects.pop((released._meta.label_lower, released.pk)), "superseded")
        self.assertEqual(set(effects.values()), {"unavailable"})
        acknowledge(issue, issue.revision_digest, self.operator(), "carrier result unreadable")
        released.refresh_from_db()
        self.assertEqual(released.status, "imported")

    def test_a_missing_owner_is_recorded_without_recreating_it_or_writing_a_tombstone(self):
        self.prepare()
        generation = self.terminal(refused=None)
        self.serve_terminal_evidence(generation, manual=True)
        serve_observation(self)
        missing = self.owners[1]
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {missing._meta.db_table} WHERE id = %s", [missing.pk])
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["class"], "evidence_invariant")
        issue = NSOSettlementIssue.objects.get(state="open")
        effects = {(owner.model_label, owner.overlay_pk): owner.proposed_effect for owner in issue.owners.all()}
        self.assertEqual(effects[(missing._meta.label_lower, missing.pk)], "missing_owner")
        self.assertEqual(
            {effects[(row._meta.label_lower, row.pk)] for row in (self.owners[0], self.owners[2])}, {"unavailable"}
        )
        missing_fences = NSOOverlaySettlementFence.objects.filter(
            model_label=missing._meta.label_lower, overlay_pk=missing.pk
        )
        fence_digests = list(missing_fences.values_list("content_digest", flat=True))
        acknowledge(issue, issue.revision_digest, self.operator(), "the peer was deleted by a cascade")
        self.assertFalse(type(missing).objects.filter(pk=missing.pk).exists())
        self.assertEqual(list(missing_fences.values_list("content_digest", flat=True)), fence_digests)
        snapshot = self.attempt.overlay_snapshots.get(model_label=missing._meta.label_lower, overlay_pk=missing.pk)
        self.assertEqual(snapshot.settlement_disposition, "unavailable")
        execution = NSOApplyExecution.objects.get(generation_id=81, carrier_job_id=501)
        self.assertEqual(execution.consumption_state, "acknowledged")
        self.assertEqual(execution.invariant_code, "invalid_terminal_batch")
        for row in (self.owners[0], self.owners[2]):
            row.refresh_from_db()
            self.assertEqual(row.status, "apply_failed")


class HistoricalStore:
    """A store whose terminal generation is behind the cursor and has no local binding."""

    def serve_history(self, *, generation_id=60, carrier_id=40, settle_seq=3, cursor=5, source=999):
        from ._outbox_case import mirror_update

        class Handler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", INCARNATION)
                super().end_headers()

        self.server.RequestHandlerClass = Handler
        adapter_client.reset_session()
        device_id = self.management.adapter_device_id
        legacy = {
            "id": carrier_id,
            "type": "apply",
            "device_id": device_id,
            "status": "succeeded",
            "result": {"bgp_count_by_outcome": {"in_sync": 1, "apply_failed": 0, "not_applied": 0, "unavailable": 0}},
            "row_outcomes": None,
            "refusals": None,
            "error": None,
            "context": None,
            "created_at": "2026-10-01T10:00:00+00:00",
            "updated_at": "2026-10-01T10:01:00+00:00",
            "started_at": None,
            "heartbeat_at": None,
            "settle_seq": settle_seq,
        }
        self.history_jobs = [legacy]
        self.history_listing = [
            make_generation_listing(
                generation_id=generation_id,
                seq=2,
                job_id=carrier_id,
                carrier_job_id=carrier_id,
                stream_revisions={"bgp": 3},
                source_push_seq={"bgp": source},
            )
        ]

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                return make_response(
                    200, [published_job(job) for job in self.history_jobs if job["settle_seq"] > after]
                )
            if method == "GET" and url.endswith("/generations"):
                return make_response(200, [generation_wire(row, device_id) for row in self.history_listing])
            if method == "POST" and url.endswith("/deployment-evidence"):
                requested = kwargs["json"]["apply_attempt_ids"]
                return make_response(
                    200,
                    {
                        "device_id": device_id,
                        "head": None,
                        "blocked": False,
                        "write_work_pending": False,
                        "held_jobs": [],
                        "pending_generations": 0,
                        "attempts": [],
                        "unknown_apply_attempt_ids": requested,
                    },
                )
            return make_response(404, {"error": {"code": "not_found", "message": "unexpected request"}})

        self.adapter._handle = handle
        serve_observation(self)
        mirror_update(
            self.management,
            settle_cursor_seq=cursor,
            settle_cursor_incarnation=INCARNATION,
            settle_cursor_device_id=device_id,
        )

    def manual_reference(self, *, promoted=True, **response):
        from uuid import uuid4

        attempt_id = uuid4()
        link = {
            "generation_id": 60,
            "seq": 2,
            "job_id": 40,
            "mode": "networked",
            "source_push_seq": {"bgp": 999},
            "stream_revisions": {"bgp": 3},
            "digest": "a" * 64,
        }
        return NSOApplyAttempt.objects.create(
            id=attempt_id,
            adapter_apply_attempt_id=attempt_id if promoted else None,
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            source_epoch=self.management.adapter_source_epoch,
            selected={"bgp": 999},
            scope_revisions={"bgp": 1},
            http_status=202,
            response={
                "device_id": self.management.adapter_device_id,
                "outcome": "promoted",
                "selected": {"bgp": 999},
                "skipped": {},
                "generations": [link],
                **response,
            },
        )


class TestHistoricalAcknowledgement(SettlementIssueHelpers, HistoricalStore, BGPCaptureCase):
    def stall_history(self):
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["code"], "missing_generation_evidence")
        return NSOSettlementIssue.objects.filter(management=self.management, state="open").first()

    def test_an_unbound_historical_obligation_is_released_in_the_ledger_only(self):
        self.serve_history()
        issue = self.stall_history()
        self.assertEqual((issue.position, issue.cursor_before, issue.cursor_after), ("historical", 5, 5))
        self.assertEqual(issue.owners.count(), 0)
        placeholder = NSOApplyExecution.objects.get(generation_id=60, carrier_job_id=40)
        self.assertEqual(issue.proposal["obligations"][0]["placeholder_execution_id"], placeholder.pk)
        acknowledge(issue, issue.revision_digest, self.operator(), "pre-ledger generation")
        placeholder.refresh_from_db()
        self.assertEqual((placeholder.consumption_state, placeholder.settle_seq), ("acknowledged", 3))
        self.assertEqual(placeholder.attempt.lifecycle, "terminal")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 5)
        self.assertEqual(self.management.settle_evidence_error, {})
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.assertTrue(result.drained)
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "acknowledged")

    def test_each_historical_carrier_needs_its_own_acknowledgement(self):
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
        first = self.stall_history()
        acknowledge(first, first.revision_digest, self.operator(), "first pre-ledger carrier")
        following = self.stall_history()
        self.assertEqual((following.job_id, following.settle_seq), (41, 4))
        acknowledge(following, following.revision_digest, self.operator("placeholder-other"), "second carrier")
        self.assertTrue(consume_static_route_settlements(self.management).drained)

    def test_attempt_answers_the_decision_reads_stay_locked_until_the_commit(self):
        from uuid import uuid4

        from django.db import OperationalError, transaction

        self.serve_history()
        lost_id = uuid4()
        lost = NSOApplyAttempt.objects.create(
            id=lost_id,
            adapter_apply_attempt_id=lost_id,
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            source_epoch=self.management.adapter_source_epoch,
            selected={"bgp": 999},
            scope_revisions={"bgp": 1},
        )
        issue = self.stall_history()
        self.assertIn(str(lost.pk), issue.proposal["attempt_locks"])
        armed, outcome = [], {}

        def record_answer(_document):
            if not armed:
                return
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '500ms'")
                    NSOApplyAttempt.objects.filter(pk=lost.pk, response__isnull=True).update(
                        http_status=202, response={"generations": []}
                    )
                outcome["recorded"] = True
            except OperationalError:
                outcome["recorded"] = False

        serve_observation(self, edit=record_answer)
        armed.append(True)
        acknowledge(issue, issue.revision_digest, self.operator(), "pre-ledger generation")
        self.assertEqual(outcome, {"recorded": False})
        lost.refresh_from_db()
        self.assertIsNone(lost.response)

    def assert_no_exit(self, code):
        issue = self.stall_history()
        self.assertIsNone(issue)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["exit"], code)

    def test_an_admission_and_a_deploying_owner_without_snapshots_have_no_exit(self):
        self.serve_history()
        attempt = self.manual_reference()
        owner = self.router_owner()
        from ._outbox_case import mirror_update

        mirror_update(owner, status="deploying", apply_attempt_id=attempt.pk)
        self.assert_no_exit("ownership_unbounded")

    def test_a_pre_ledger_admission_and_a_deploying_owner_have_no_exit(self):
        from ._outbox_case import mirror_update

        self.serve_history()
        attempt = self.manual_reference(promoted=False)
        owner = self.router_owner()
        mirror_update(owner, status="deploying", apply_attempt_id=attempt.pk)
        self.assert_no_exit("ownership_unbounded")

    def test_a_fence_only_dependency_has_no_exit(self):
        self.serve_history()
        attempt = self.manual_reference()
        owner = self.router_owner()
        NSOOverlaySettlementFence.objects.filter(model_label=owner._meta.label_lower, overlay_pk=owner.pk).update(
            current_attempt=attempt
        )
        self.assertTrue(NSOOverlaySettlementFence.objects.filter(current_attempt=attempt).exists())
        self.assert_no_exit("ownership_unbounded")

    def test_a_placeholder_attached_to_a_manual_attempt_is_not_exempt(self):
        from netbox_nso_plugin.outcome_settlement import _record_invariant

        self.serve_history()
        attempt = self.manual_reference()
        _record_invariant(
            self.management,
            {"generation_id": 60, "carrier_job_id": 40},
            INCARNATION,
            attempt,
            "missing_generation_evidence",
        )
        self.assert_no_exit("ownership_unbounded")

    def test_a_pending_receipt_claim_for_the_generation_has_no_exit(self):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        self.serve_history()
        NSOIntentOutboxState.objects.create(device_id=self.management.device_id, scope="bgp", push_seq=999)
        self.assert_no_exit("ownership_unbounded")

    def test_a_job_that_another_unconsumed_job_precedes_has_no_exit(self):
        self.serve_history(settle_seq=7, cursor=5)
        earlier = copy.deepcopy(self.history_jobs[0])
        earlier.update(id=39, settle_seq=6, type="sync", result=None)
        self.history_jobs.insert(0, earlier)
        from ._outbox_case import mirror_update

        mirror_update(
            self.management,
            settle_evidence_error={
                "request_fence": {
                    "mapping_epoch": mapping_epoch(self.management),
                    "cursor_device_id": self.management.adapter_device_id,
                    "cursor_incarnation": INCARNATION,
                    "cursor_seq": 5,
                },
                "store_incarnation": INCARNATION,
                "reason": "invalid settlement evidence",
                "class": "invalid_evidence",
                "job_id": 40,
            },
        )
        self.assertIsNone(publish_stall_issue(self.management))
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["exit"], "unordered")

    def test_an_unidentified_stall_has_no_exit(self):
        self.serve_history()
        original = self.adapter._handle

        def headerless(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                return make_response(200, [])
            return original(method, url, **kwargs)

        self.server.RequestHandlerClass = _CaptureHandler
        self.adapter._handle = headerless
        adapter_client.reset_session()
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.management.refresh_from_db()
        self.assertNotIn("job_id", self.management.settle_evidence_error)
        self.assertEqual(self.management.settle_evidence_error["exit"], "unidentified")
        self.assertFalse(NSOSettlementIssue.objects.exists())


class TestCoalescedCarrierAcknowledgement(
    SettlementIssueHelpers, CarrierPageHelpers, CoalescedCarrierHelpers, BGPSettlementHelpers, BGPCaptureCase
):
    def test_one_carrier_releases_each_generation_with_its_exact_owner_subset(self):
        from netbox_nso_plugin.models import NSOAcceptedSelection

        generations = self.prepare_carrier()
        self.serve_carriers(generations)
        self.invalid_feed_job(501)
        serve_observation(self)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        issue = NSOSettlementIssue.objects.get(state="open")
        self.assertEqual([row["generation_id"] for row in issue.proposal["obligations"]], [81, 82])
        owners = {(owner.model_label, owner.overlay_pk): owner for owner in issue.owners.all()}
        router, vlan = self.owners
        self.assertEqual(owners[(router._meta.label_lower, router.pk)].contributors[0][0], "bgp")
        self.assertEqual(owners[(vlan._meta.label_lower, vlan.pk)].contributors[0][0], "vlan")
        self.assertEqual(
            {stream for owner in owners.values() for stream, _subject, _key in owner.contributors}, {"bgp", "vlan"}
        )
        for owner in owners.values():
            [selection] = NSOAcceptedSelection.objects.filter(pk__in=owner.selection_ids)
            self.assertEqual(selection.stream, owner.contributors[0][0])
        acknowledge(issue, issue.revision_digest, self.operator(), "coalesced carrier unreadable")
        self.assertEqual(
            sorted(NSOApplyExecution.objects.values_list("generation_id", "carrier_job_id", "consumption_state")),
            [(81, 501, "acknowledged"), (82, 501, "acknowledged")],
        )
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "apply_failed")
