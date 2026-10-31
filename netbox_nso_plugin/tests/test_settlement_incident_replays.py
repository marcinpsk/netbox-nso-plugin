# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Replay the three settlement stalls of the #1851 incident and clear them through the operator exit.

Each replay seeds the incident state as fixture setup, then uses only production paths: the
settlement pass, the tab, the acknowledgement POST and the queued resume pass. No test edits a
database row after the fixture setup.
"""

import copy
from uuid import uuid4

from dcim.models import Interface
from django.db import transaction

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.lifecycle_fence import owner_deletion_proven
from netbox_nso_plugin.models import (
    NSOApplyAttempt,
    NSOApplyExecution,
    NSOOverlaySettlementFence,
    NSOSettlementIssue,
    NSOSubinterfaceState,
    NSOSVIState,
)
from netbox_nso_plugin.settlement import consume_static_route_settlements

from ._adapter_http import make_evidence_envelope, make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import mirror_update, without_commit_drain
from ._ownership_case import acquire_overlay
from ._settlement_observation import INCARNATION, published_job, serve_observation
from .test_coalesced_settlement import CoalescedCarrierHelpers
from .test_durable_bgp_capture import BGPCaptureCase, _CaptureHandler
from .test_settlement_acknowledgement_view import BUTTON, AcknowledgementViewHelpers
from .test_settlement_issue import ADVISORY
from .test_settlement_prefix import CarrierPageHelpers

#: The read routes a settlement pass, the tab and the acknowledgement may call.
READ_ROUTES = ("/jobs", "/generations", "/deployment-evidence", "/settlement-observation")
UNBOUNDED = "The rows that this job can affect cannot be bounded."
NO_EXIT = "No acknowledgement is available for this stall."


def assert_acknowledged_position(case, cursor):
    """The acknowledgement itself applies the cursor rule and commits the resume request."""
    case.management.refresh_from_db()
    case.assertEqual(
        (
            case.management.settle_cursor_seq,
            case.management.settle_evidence_error,
            case.management.settle_resume_pending,
        ),
        (cursor, {}, True),
    )


class LegacyStore:
    """Terminal generations settled before the execution ledger, all behind the settlement cursor."""

    LEGACY = 7
    CURSOR = 10

    def legacy_result(self, index):
        """A stored carrier result in the format from before row outcomes existed."""
        counter = {"in_sync": 1 + index, "apply_failed": 0, "not_applied": 0, "unavailable": 0}
        return {"bgp_count_by_outcome": counter, "vlan_count_by_outcome": dict(counter, in_sync=0)}

    def serve_legacy(self, *, admissions=False):
        device_id = self.management.adapter_device_id

        class Handler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", INCARNATION)
                super().end_headers()

        self.server.RequestHandlerClass = Handler
        adapter_client.reset_session()
        self.legacy_jobs, self.legacy_listing, self.admissions = [], [], []
        for index in range(self.LEGACY):
            carrier = 40 + index
            self.legacy_jobs.append(
                {
                    "id": carrier,
                    "type": "apply",
                    "device_id": device_id,
                    "status": "succeeded",
                    "result": self.legacy_result(index),
                    "row_outcomes": None,
                    "refusals": None,
                    "error": None,
                    "context": None,
                    "created_at": "2026-10-01T10:00:00+00:00",
                    "updated_at": "2026-10-01T10:01:00+00:00",
                    "started_at": None,
                    "heartbeat_at": None,
                    "settle_seq": 1 + index,
                }
            )
            row = {
                "generation_id": 60 + index,
                "seq": 1 + index,
                "status": "settled",
                "job_id": carrier,
                "carrier_job_id": carrier,
                "carrier_job_status": "succeeded",
                "mode": "networked",
                "settlement_cohort": None,
                "digest": f"{index:x}" * 64,
                "stream_revisions": {"bgp": 3 + index},
                "source_push_seq": {"bgp": 900 + index},
                "created_at": "2026-10-01T10:00:00+00:00",
                "updated_at": "2026-10-01T10:01:00+00:00",
            }
            # The adapter stored the identity of the manual Apply that admitted each generation.
            attempt_id = uuid4()
            self.legacy_listing.append((row, attempt_id))
            if admissions:
                self.admissions.append(self.pre_ledger_admission(attempt_id, row, index))
        self.later_jobs = []
        self.adapter_requests = []

        def handle(method, url, **kwargs):
            self.adapter_requests.append((method, url))
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                jobs = [*self.legacy_jobs, *self.later_jobs]
                return make_response(200, [published_job(job) for job in jobs if job["settle_seq"] > after])
            if method == "GET" and url.endswith("/generations"):
                return make_response(
                    200,
                    [
                        make_evidence_envelope(
                            row,
                            identity={
                                "device_id": device_id,
                                **{key: row[key] for key in ("generation_id", "seq", "carrier_job_id")},
                                "apply_attempt_id": str(attempt_id),
                            },
                        )
                        for row, attempt_id in self.legacy_listing
                    ],
                )
            if method == "POST" and url.endswith("/deployment-evidence"):
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
                        "unknown_apply_attempt_ids": kwargs["json"]["apply_attempt_ids"],
                    },
                )
            return make_response(404, {"error": {"code": "not_found", "message": "unexpected request"}})

        self.adapter._handle = handle
        serve_observation(self)
        mirror_update(
            self.management,
            settle_cursor_seq=self.CURSOR,
            settle_cursor_incarnation=INCARNATION,
            settle_cursor_device_id=device_id,
        )

    def pre_ledger_admission(self, attempt_id, row, index):
        """A manual Apply from before the ledger: promoted, answered, never closed and never bound."""
        link = {
            key: row[key] for key in ("generation_id", "seq", "mode", "digest", "stream_revisions", "source_push_seq")
        }
        # Two of the live admissions named the job that first carried the generation, not the retained carrier.
        link["job_id"] = row["carrier_job_id"] if index % 3 else 30 + index
        return NSOApplyAttempt.objects.create(
            id=attempt_id,
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            selected={"bgp": row["source_push_seq"]["bgp"]},
            scope_revisions={"bgp": 1},
            http_status=202,
            response={
                "device_id": self.management.adapter_device_id,
                "outcome": "promoted",
                "selected": {"bgp": row["source_push_seq"]["bgp"]},
                "skipped": {},
                "generations": [link],
            },
        )

    def assert_read_only_adapter_use(self):
        """The exit never asks the adapter to delete, abandon or retry a generation."""
        for method, url in self.adapter_requests:
            self.assertIn(method, {"GET", "POST"}, url)
            self.assertNotIn("/actions/", url)
            if method == "POST":
                self.assertTrue(url.endswith(READ_ROUTES), url)


class TestPreLedgerGenerationsReplay(AcknowledgementViewHelpers, LegacyStore, BGPCaptureCase):
    """Case 1: seven terminal generations whose stored results predate the row-outcome format."""

    def setUp(self):
        super().setUp()
        self.owners = []

    def test_seven_pre_ledger_generations_clear_through_one_acknowledgement_each(self):
        self.serve_legacy()
        client = self.client_for(self.acknowledger())
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        for index in range(self.LEGACY):
            carrier = 40 + index
            with self.subTest(carrier=carrier):
                issue = NSOSettlementIssue.objects.get(management=self.management, state="open")
                self.assertEqual((issue.job_id, issue.settle_seq, issue.position), (carrier, 1 + index, "historical"))
                self.assertEqual((issue.cursor_before, issue.cursor_after), (self.CURSOR, self.CURSOR))
                self.assertEqual(issue.stall_reason, "missing_generation_evidence")
                self.assertEqual(issue.owners.count(), 0)
                tab = client.get(self.tab_url())
                self.assertContains(tab, f"Adapter job {carrier}:")
                self.assertContains(tab, "Closed reason: <code>missing_generation_evidence</code>", html=True)
                self.assertContains(tab, "The cursor is already past the carrier of this job.")
                self.assertContains(tab, BUTTON, count=1)
                response = client.post(
                    self.action_url(), self.form(issue, reason=f"placeholder pre-ledger carrier {carrier}"), follow=True
                )
                self.assertContains(response, f"Acknowledged adapter job {carrier} as outcome unknown.")
                assert_acknowledged_position(self, self.CURSOR)
                # The post-commit enqueue runs the next pass, which stops at the next legacy carrier.
                self.drain_queue()
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, self.CURSOR)
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertFalse(self.management.settle_resume_pending)
        self.assertEqual(
            sorted(
                NSOApplyExecution.objects.values_list(
                    "generation_id", "carrier_job_id", "settle_seq", "consumption_state"
                )
            ),
            [(60 + index, 40 + index, 1 + index, "acknowledged") for index in range(self.LEGACY)],
        )
        self.assertEqual(NSOSettlementIssue.objects.filter(state="acknowledged").count(), self.LEGACY)
        self.assertEqual(len(self.legacy_listing), self.LEGACY)
        self.assert_read_only_adapter_use()
        page = client.get(self.tab_url())
        self.assertNotContains(page, "Settlement is paused")
        for index in range(self.LEGACY):
            self.assertContains(page, f'data-acknowledgement-job="{40 + index}"', count=1)
        later = {**copy.deepcopy(self.legacy_jobs[0]), "id": 48, "type": "sync", "result": None, "settle_seq": 11}
        self.later_jobs.append(later)
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.consumed, result.cursor, result.stalled, result.drained), (1, 11, False, True))

    def test_the_live_local_history_of_the_seven_generations_has_no_exit(self):
        """The live plugin kept an open, unpromoted, answered manual Apply for each generation.

        Design r3 change 1 counts a stored admission that names the generation as a dependency, so the
        stall has no exit. This pins the ratified behavior; AC2.6 for this shape is an open question.
        """
        self.serve_legacy(admissions=True)
        before = [
            (attempt.lifecycle, attempt.completed_at, attempt.response)
            for attempt in NSOApplyAttempt.objects.filter(pk__in=[row.pk for row in self.admissions]).order_by("pk")
        ]
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        self.management.refresh_from_db()
        diagnostic = self.management.settle_evidence_error
        self.assertEqual((diagnostic["job_id"], diagnostic["code"]), (40, "missing_generation_evidence"))
        self.assertEqual(diagnostic["exit"], "ownership_unbounded")
        self.assertFalse(NSOSettlementIssue.objects.exists())
        tab = self.client_for(self.acknowledger()).get(self.tab_url())
        self.assertContains(tab, "Settlement is paused", count=1)
        self.assertContains(tab, UNBOUNDED)
        self.assertContains(tab, NO_EXIT)
        self.assertNotContains(tab, BUTTON)
        self.assertEqual(
            [
                (attempt.lifecycle, attempt.completed_at, attempt.response)
                for attempt in NSOApplyAttempt.objects.filter(pk__in=[row.pk for row in self.admissions]).order_by("pk")
            ],
            before,
        )
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="acknowledged").exists())
        self.assert_read_only_adapter_use()


class TestCascadedOwnerDeletionReplay(AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase):
    """Case 2: three owners that a foreign Interface delete removed before any deletion evidence existed."""

    def owners_on_interfaces(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        with without_commit_drain(), transaction.atomic():
            vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=104, name="placeholder-irb")
            NSOVLANState.objects.create(management=self.management, vlan=vlan, status="imported")
            parent = Interface.objects.create(device=self.device, name="ae51", type="lag")
            other = Interface.objects.create(device=self.device, name="ae52", type="lag")
            children = [
                Interface.objects.create(device=self.device, name=f"ae51.{vid}", type="virtual", parent=parent)
                for vid in (101, 102)
            ]
            kept = Interface.objects.create(device=self.device, name="ae52.103", type="virtual", parent=other)
            irb = Interface.objects.create(device=self.device, name="irb.104", type="virtual")
        rows = [
            acquire_overlay(
                NSOSubinterfaceState,
                management=self.management,
                interface=child,
                parent_interface=child.parent,
                dot1q_vlan=vid,
                status="accepted",
            )
            for child, vid in zip((*children, kept), (101, 102, 103), strict=True)
        ]
        svi = acquire_overlay(
            NSOSVIState, management=self.management, interface=irb, vlan=vlan, svi_type="irb", status="accepted"
        )
        self.deleted_natives = [*children, irb]
        return [rows[0], rows[1], svi, rows[2]]

    def in_sync(self):
        subjects = {
            contributor["logical_ref"]["subject"]
            for snapshot in self.attempt.overlay_snapshots.all()
            for contributor in snapshot.contributors["rows"]
        }
        return self.evidence(dispositions=dict.fromkeys(subjects, "in_sync"))

    def test_three_cascaded_owners_without_evidence_clear_through_the_acknowledgement(self):
        owners = self.owners_on_interfaces()
        self.apply_owners(owners)
        self.serve_terminal_evidence(self.in_sync(), manual=True)
        serve_observation(self)
        missing, survivor = owners[:3], owners[3]
        snapshots = {
            self.key(row): self.attempt.overlay_snapshots.get(model_label=row._meta.label_lower, overlay_pk=row.pk)
            for row in missing
        }
        # The incident state: the foreign delete cascaded the owners and no deferred pass recorded evidence.
        with without_commit_drain():
            Interface.objects.filter(pk__in=[row.pk for row in self.deleted_natives]).delete()
        for row in missing:
            self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
        self.assertFalse(any(owner_deletion_proven(self.management, snapshot) for snapshot in snapshots.values()))
        fences = {
            key: NSOOverlaySettlementFence.objects.get(model_label=key[0], overlay_pk=key[1]).content_digest
            for key in snapshots
        }

        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["class"], "evidence_invariant")
        self.assertEqual(self.management.settle_evidence_error["exit"], "acknowledgeable")
        issue = NSOSettlementIssue.objects.get(state="open")
        self.assertEqual((issue.job_id, issue.position), (501, "next_feed"))
        effects = {(owner.model_label, owner.overlay_pk): owner.proposed_effect for owner in issue.owners.all()}
        self.assertEqual(effects, {**dict.fromkeys(snapshots, "missing_owner"), self.key(survivor): "unavailable"})
        client = self.client_for(self.acknowledger())
        tab = client.get(self.tab_url())
        self.assertContains(tab, "Affected rows: 1 outcome unknown, 0 kept with a newer state, 3 missing.")
        self.assertContains(tab, BUTTON, count=1)

        page = client.post(
            self.action_url(), self.form(issue, reason="placeholder cascade removed the rows"), follow=True
        )
        self.assertContains(page, "Acknowledged adapter job 501 as outcome unknown.")
        for label, pk in snapshots:
            self.assertContains(page, f'data-effect="{label}:{pk}:missing_owner"', count=1)
        for row in missing:
            self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
        for key, snapshot in snapshots.items():
            self.assertEqual(
                NSOOverlaySettlementFence.objects.get(model_label=key[0], overlay_pk=key[1]).content_digest, fences[key]
            )
            self.assertFalse(owner_deletion_proven(self.management, snapshot))
            snapshot.refresh_from_db()
            self.assertEqual(snapshot.settlement_disposition, "unavailable")
        survivor.refresh_from_db()
        self.assertEqual((survivor.status, survivor.last_apply_error), ("apply_failed", ADVISORY.format(501)))
        execution = NSOApplyExecution.objects.get(carrier_job_id=501)
        self.assertEqual((execution.consumption_state, execution.settle_seq), ("acknowledged", 1))
        assert_acknowledged_position(self, 1)

        self.append_feed_jobs(self.sync_job(502, 2))
        self.drain_queue()
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 2)
        self.assertFalse(self.management.settle_resume_pending)
        self.assertEqual(self.management.settle_evidence_error, {})

    def key(self, row):
        return row._meta.label_lower, row.pk


class TestInvalidPageItemReplay(
    AcknowledgementViewHelpers, CarrierPageHelpers, CoalescedCarrierHelpers, BGPSettlementHelpers, BGPCaptureCase
):
    """Case 3: one invalid job between two valid jobs of the same feed page."""

    def requested_attempts_only(self):
        """Answer deployment evidence only for the requested attempts, as the adapter does."""
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method != "POST" or not url.endswith("/deployment-evidence"):
                return response
            body = response.json()
            requested = kwargs["json"]["apply_attempt_ids"]
            body["attempts"] = [row for row in body["attempts"] if row["identity"]["apply_attempt_id"] in requested]
            served = {row["identity"]["apply_attempt_id"] for row in body["attempts"]}
            body["unknown_apply_attempt_ids"] = sorted(set(requested) - served)
            return make_response(200, body)

        self.adapter._handle = handle

    def test_the_invalid_item_alone_is_released_and_its_neighbours_settle(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations)
        self.requested_attempts_only()
        serve_observation(self)
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.consumed, result.cursor, result.stalled), (1, 1, True))
        certificates = list(
            NSOApplyExecution.objects.filter(carrier_job_id=501)
            .order_by("generation_id")
            .values_list("generation_id", "consumption_state", "terminal_projection")
        )
        self.assertEqual([row[:2] for row in certificates], [(81, "consumed"), (82, "consumed")])
        self.assertEqual([owner[0] for owner in self.owner_state()], ["in_sync", "in_sync"])
        self.management.refresh_from_db()
        self.assertEqual(
            {key: self.management.settle_evidence_error[key] for key in ("job_id", "settle_seq", "class", "exit")},
            {"job_id": 502, "settle_seq": 2, "class": "invalid_evidence", "exit": "acknowledgeable"},
        )
        self.assertEqual(self.management.settle_evidence_error["identity"]["id"], 502)
        issue = NSOSettlementIssue.objects.get(state="open")
        self.assertEqual(
            (issue.job_id, issue.position, issue.cursor_before, issue.cursor_after), (502, "next_feed", 1, 2)
        )
        self.assertEqual((issue.proposal["obligations"], issue.owners.count()), ([], 0))
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id__in=(502, 503)).exists())

        client = self.client_for(self.acknowledger())
        page = client.post(self.action_url(), self.form(issue, reason="placeholder unreadable page item"), follow=True)
        self.assertContains(page, "Acknowledged adapter job 502 as outcome unknown.")
        self.assertContains(page, "Cursor 1 to 2")
        assert_acknowledged_position(self, 2)
        self.drain_queue()
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 3)
        self.assertFalse(self.management.settle_resume_pending)
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertEqual(
            list(
                NSOApplyExecution.objects.filter(carrier_job_id=501)
                .order_by("generation_id")
                .values_list("generation_id", "consumption_state", "terminal_projection")
            ),
            certificates,
        )
        self.assertEqual([owner[0] for owner in self.owner_state()], ["in_sync", "in_sync"])
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id__in=(502, 503)).exists())
