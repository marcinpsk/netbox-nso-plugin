# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Resolve acknowledged obligations by type in every settlement consumer."""

import copy

from django.utils import timezone

from netbox_nso_plugin import durable_capture as capture
from netbox_nso_plugin.apply_settlement import deployment_evidence_attempt_ids
from netbox_nso_plugin.deployment_probe import ProbeBlocked, authority
from netbox_nso_plugin.lifecycle_fence import fence_for
from netbox_nso_plugin.models import (
    NSOApplyExecution,
    NSOSettlementIssue,
    NSOSettlementIssueOwner,
)
from netbox_nso_plugin.settlement import consume_static_route_settlements
from netbox_nso_plugin.settlement_acknowledgement import acknowledge
from netbox_nso_plugin.settlement_resolution import instant

from ._adapter_http import make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import content_update, mirror_update
from ._settlement_observation import INCARNATION, serve_observation
from .test_durable_bgp_capture import BGPCaptureCase
from .test_settlement_issue import ADVISORY, HistoricalStore, SettlementIssueHelpers


class TestAcknowledgedObligationConsumers(SettlementIssueHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def acknowledged_carrier(self):
        issue = self.stall_invalid_manual_carrier()
        acknowledge(issue, issue.revision_digest, self.operator(), "carrier result unreadable")
        issue.refresh_from_db()
        return issue

    def serve_retry(self, *, dispositions=None):
        """Retry generation 81 under carrier 503, joined to the manual attempt through the head."""
        retry = self.terminal(refused=None)
        retry["carrier_job_id"] = 503
        if dispositions is not None:
            for outcome in retry["row_outcomes"]:
                outcome["disposition"] = dispositions
        self.serve_terminal_evidence(retry, settle_seq=2)
        original = self.adapter._handle
        attempt_id = str(self.attempt.pk)

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence") and response.json()["head"] is not None:
                document = response.json()
                document["head"]["identity"]["apply_attempt_id"] = attempt_id
                document["head"]["projection"]["apply_attempt_id"] = attempt_id
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        return retry

    def unknown_owner(self, row, contribution, job_id):
        """Record one more acknowledged unknown contribution under the owner's current acceptance."""
        fence = fence_for_row(row)
        now = timezone.now()
        issue = NSOSettlementIssue.objects.create(
            management=self.management,
            mapping_epoch=capture.mapping_epoch(self.management),
            adapter_device_id=self.management.adapter_device_id,
            store_incarnation=INCARNATION,
            job_id=job_id,
            settle_seq=1,
            request_fence={},
            stall_class="invalid_evidence",
            stall_reason="invalid job envelope",
            position="historical",
            cursor_before=1,
            cursor_after=1,
            evidence_digest="b" * 64,
            impact_digest="c" * 64,
            contract_fingerprint="d" * 64,
            revision_digest="e" * 64,
            proposal={"obligations": []},
            state="acknowledged",
            closed_at=now,
            actor_id=1,
            actor_label="placeholder-operator",
            acknowledgement_reason="placeholder reason",
            acknowledged_at=now,
            committed_effects=[],
        )
        NSOSettlementIssueOwner.objects.create(
            issue=issue,
            model_label=row._meta.label_lower,
            overlay_pk=row.pk,
            contributors=[contribution],
            unknown=[contribution],
            authority={"accepted_at": instant(row.accepted_at), "owner_revision": fence.owner_revision},
            order_bound=1,
            proposed_effect="unavailable",
            committed_effect="unavailable",
        )

    def test_a_later_retry_certificate_covers_the_unknown_contribution(self):
        self.acknowledged_carrier()
        self.serve_retry()
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.consumed, result.cursor, result.stalled), (1, 2, False))
        retry = NSOApplyExecution.objects.get(carrier_job_id=503)
        self.assertEqual(retry.consumption_state, "consumed")
        self.assertEqual(retry.attempt.trigger_kind, "explicit_retry")
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")
        covered = NSOSettlementIssueOwner.objects.filter(issue__job_id=501)
        self.assertTrue(all(owner.covered == owner.unknown for owner in covered))

    def test_an_unknown_contribution_that_no_later_certificate_decides_keeps_the_owner_unknown(self):
        self.acknowledged_carrier()
        router = self.owners[0]
        router.refresh_from_db()
        self.unknown_owner(router, ["bgp", "bgp.router", "f" * 64], 499)
        self.serve_retry()
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.cursor, result.stalled), (2, False))
        router.refresh_from_db()
        self.assertEqual(router.status, "apply_failed")
        self.assertEqual(router.last_apply_error, ADVISORY.format(499))
        for owner in self.owners[1:]:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_the_not_applied_restore_cannot_restore_success_over_an_unknown_contribution(self):
        self.prepare()
        peer = self.owners[2]
        peer.refresh_from_db()
        self.assertEqual(peer.status, "in_sync")
        snapshots = self.attempt.overlay_snapshots.filter(model_label=peer._meta.label_lower, overlay_pk=peer.pk)
        ref = snapshots.get().contributors["rows"][0]["logical_ref"]
        self.unknown_owner(peer, ["bgp", ref["subject"], ref["key"]], 499)
        capture.restore_attempt(self.attempt.pk, keep_streams=())
        peer.refresh_from_db()
        self.assertEqual(peer.status, "apply_failed")
        self.assertEqual(peer.last_apply_error, ADVISORY.format(499))
        self.assertEqual(snapshots.get().settlement_disposition, "unavailable")
        router = self.owners[0]
        router.refresh_from_db()
        self.assertEqual(router.status, "accepted")

    def test_a_new_acceptance_fences_the_unknown_contribution_into_history(self):
        from netbox_nso_plugin.settlement_resolution import unknown_contributions

        self.acknowledged_carrier()
        router, peer = self.owners[0], self.owners[1]
        router.refresh_from_db()
        peer.refresh_from_db()
        self.assertTrue(unknown_contributions(router, fence_for_row(router)))
        content_update(router, accepted_at=timezone.now())
        router.refresh_from_db()
        self.assertEqual(unknown_contributions(router, fence_for_row(router)), [])
        self.assertTrue(unknown_contributions(peer, fence_for_row(peer)))

    def test_a_rewalk_of_the_same_store_passes_the_acknowledged_job_by_its_identity(self):
        self.acknowledged_carrier()
        mirror_update(self.management, settle_cursor_device_id=self.management.adapter_device_id + 1)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.epoch_reset)
        self.assertEqual((result.cursor, result.stalled), (1, False))
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})

    def test_probe_authority_and_policy_journals_accept_only_certificates(self):
        from extras.models import JournalEntry

        from netbox_nso_plugin.models import NSOApplyAttempt
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        self.acknowledged_carrier()
        NSOApplyAttempt.objects.filter(pk=self.attempt.pk).update(selected={"bgp": self.attempt.selected["bgp"]})
        with self.assertRaisesRegex(ProbeBlocked, "no captured terminal evidence"):
            authority(self.attempt.pk, completed=True)
        _journal_route_policy_apply(self.management, {"id": 501, "generation_id": 81, "store_incarnation": INCARNATION})
        self.assertFalse(JournalEntry.objects.exists())

    def test_an_attempt_keeps_outstanding_sibling_generations_discoverable(self):
        self.assert_outstanding_sibling_keeps_the_attempt_open(other_store_certificate=False)

    def test_a_sibling_certificate_from_another_store_does_not_complete_the_attempt(self):
        self.assert_outstanding_sibling_keeps_the_attempt_open(other_store_certificate=True)

    def assert_outstanding_sibling_keeps_the_attempt_open(self, *, other_store_certificate):
        from uuid import UUID

        from django.urls import reverse

        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._adapter_http import make_generation_listing
        from ._settlement_wire import generation_wire
        from .test_apply_selector import _promoted

        def admission(selected):
            response = _promoted(selected)
            for link in response["generations"]:
                for key in ("stream_revisions", "source_push_seq"):
                    link[key] = {stream: value for stream, value in link[key].items() if stream == "bgp"}
            response["skipped"] = {stream: "no_receipt" for stream in selected if stream != "bgp"}
            return 202, response

        self.adapter.apply_response = admission
        scope = self.scope()
        self.owners = [self.router_owner(), self.peer(scope, "198.18.0.2"), self.peer(scope, "198.18.0.3")]
        answer = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(answer.status_code, 200, answer.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        link = self.attempt.response["generations"][1]
        generation = self.terminal(refused=None)
        pending = copy.deepcopy(generation)
        pending.update(
            generation_id=82,
            seq=5,
            status="pending",
            carrier_job_id=None,
            carrier_job_status=None,
            carrier_job_result=None,
            row_outcomes=None,
            refusals=None,
        )
        listed = make_generation_listing(
            generation_id=82,
            seq=5,
            status="pending",
            job_id=None,
            carrier_job_id=None,
            carrier_job_status=None,
            mode="detach",
            settlement_cohort=None,
            digest=link["digest"],
            stream_revisions=link["stream_revisions"],
            source_push_seq=link["source_push_seq"],
        )
        self.serve_terminal_evidence(generation, manual=True)
        original = self.adapter._handle

        def with_pending(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                return make_response(
                    200, [*response.json(), generation_wire(listed, self.management.adapter_device_id)]
                )
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                for attempt in document["attempts"]:
                    attempt["generations"].append(generation_wire(pending, self.management.adapter_device_id))
                    attempt["projection"]["generation_ids"] = [81, 82]
                return make_response(200, document)
            return response

        self.adapter._handle = with_pending
        self.invalid_feed_job()
        serve_observation(self)
        if other_store_certificate:
            _other_store_certificate(self.management, 82)
        consume_static_route_settlements(self.management)
        issue = NSOSettlementIssue.objects.get(state="open")
        acknowledge(issue, issue.revision_digest, self.operator(), "carrier result unreadable")
        self.attempt.refresh_from_db()
        self.assertFalse(self.attempt.overlay_snapshots.filter(settlement_disposition="unresolved").exists())
        self.assertEqual(self.attempt.lifecycle, "open")
        self.assertIn(self.attempt.pk, deployment_evidence_attempt_ids(self.management))
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.lifecycle, "open")


def _other_store_certificate(management, generation_id):
    """Record a consumed certificate whose generation id belongs to an earlier store."""
    from netbox_nso_plugin.models import NSOApplyAttempt

    attempt = NSOApplyAttempt.objects.create(
        management=management,
        adapter_device_id=management.adapter_device_id,
        trigger_kind="cadence",
        lifecycle="terminal",
        completed_at=timezone.now(),
    )
    return NSOApplyExecution.objects.create(
        attempt=attempt,
        management=management,
        mapping_epoch=capture.mapping_epoch(management),
        adapter_device_id=management.adapter_device_id,
        store_incarnation="00000000-0000-4000-8000-000000000009",
        generation_id=generation_id,
        generation_seq=1,
        carrier_job_id=900,
        settle_seq=1,
        terminal_projection={"generation_id": generation_id},
        terminal_at=timezone.now(),
        consumption_state="consumed",
        consumed_at=timezone.now(),
    )


def fence_for_row(row):
    from django.db import transaction

    with transaction.atomic():
        return fence_for(row)


class TestAcknowledgedHistoryConsumers(SettlementIssueHelpers, HistoricalStore, BGPCaptureCase):
    def acknowledged_history(self):
        self.serve_history()
        consume_static_route_settlements(self.management)
        issue = NSOSettlementIssue.objects.get(state="open")
        acknowledge(issue, issue.revision_digest, self.operator(), "pre-ledger generation")
        return issue

    def test_an_acknowledgement_from_an_earlier_mapping_epoch_still_releases_the_obligation(self):
        self.acknowledged_history()
        before = capture.mapping_epoch(self.management)
        mirror_update(self.management, adapter_source_epoch=(self.management.adapter_source_epoch or 0) + 1)
        self.management.refresh_from_db()
        self.assertNotEqual(capture.mapping_epoch(self.management), before)
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.assertTrue(result.drained)

    def test_readiness_resolves_the_acknowledged_generation_without_a_certificate(self):
        from django.db import transaction

        from netbox_nso_plugin.deployment_checks import check_active_streams

        from ._outbox_case import without_commit_drain

        with without_commit_drain(), transaction.atomic():
            self.router.delete()
        self.prior_attempt.delete()
        self.serve_history()
        original = self.adapter._handle

        def inventory(method, url, **kwargs):
            if url.endswith("/intent-summary"):
                return make_response(
                    200, {"device_id": self.management.adapter_device_id, "scopes": {}, "pending_clear": {}}
                )
            if url.endswith("/intent-receipts"):
                return make_response(200, {"receipts": [], "global_max_push_seq": None, "global_max_route_id": None})
            return original(method, url, **kwargs)

        self.adapter._handle = inventory
        with transaction.atomic(), self.assertRaisesRegex(ProbeBlocked, "missing_generation_evidence"):
            check_active_streams()
        consume_static_route_settlements(self.management)
        issue = NSOSettlementIssue.objects.get(state="open")
        acknowledge(issue, issue.revision_digest, self.operator(), "pre-ledger generation")
        with transaction.atomic():
            check_active_streams()
