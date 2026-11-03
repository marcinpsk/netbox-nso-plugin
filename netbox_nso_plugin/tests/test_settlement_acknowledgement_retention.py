# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Prove that acknowledgement records outlive the real Apply ledger retention path.

Each case builds its acknowledgement through the settlement pass and the operator POST, then runs
prune_apply_ledger at a clock past the retention floor. Only the adapter double changes between
prunes, as adapter-side generation retention would.
"""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from netbox_nso_plugin.apply_retention import RETENTION_DAYS, prune_apply_ledger
from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyExecution, NSOSettlementIssue
from netbox_nso_plugin.refusal_display import row_projection
from netbox_nso_plugin.settlement import consume_static_route_settlements

from ._adapter_http import make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._settlement_observation import serve_observation
from .test_durable_bgp_capture import BGPCaptureCase
from .test_settlement_acknowledgement_view import AcknowledgementViewHelpers
from .test_settlement_issue import ADVISORY, HistoricalStore


class RetentionHelpers:
    def prune_after_retention(self):
        """Run the real retention path at a clock one day past the retention floor."""
        later = timezone.now() + timedelta(days=RETENTION_DAYS + 1)
        with patch("django.utils.timezone.now", return_value=later):
            return prune_apply_ledger(execute=True)

    def audit(self, issue):
        issue = NSOSettlementIssue.objects.get(pk=issue.pk)
        return (
            issue.state,
            issue.closed_at,
            issue.revision_digest,
            issue.proposal,
            issue.actor_id,
            issue.actor_label,
            issue.acknowledgement_reason,
            issue.acknowledged_at,
            issue.committed_effects,
            list(
                issue.owners.order_by("model_label", "overlay_pk").values_list(
                    "model_label",
                    "overlay_pk",
                    "snapshot_ids",
                    "attempt_id",
                    "unknown",
                    "covered",
                    "proposed_effect",
                    "committed_effect",
                )
            ),
        )


class TestHistoricalAcknowledgementRetention(
    RetentionHelpers, AcknowledgementViewHelpers, HistoricalStore, BGPCaptureCase
):
    def setUp(self):
        super().setUp()
        self.owners = []

    def test_the_acknowledged_obligation_stays_while_listed_and_its_audit_outlives_the_prune(self):
        self.serve_history()
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        superseded = NSOSettlementIssue.objects.get(state="open")

        def change(document):
            document["job"]["digest"] = "c" * 64

        serve_observation(self, edit=change)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        issue = NSOSettlementIssue.objects.get(state="open")
        self.assertNotEqual(issue.pk, superseded.pk)
        client = self.client_for(self.acknowledger())
        client.post(self.action_url(), self.form(issue, reason="placeholder pre-ledger carrier"))
        self.drain_queue()
        execution = NSOApplyExecution.objects.get(generation_id=60, carrier_job_id=40)
        self.assertEqual(execution.consumption_state, "acknowledged")
        audits = {row.pk: self.audit(row) for row in (superseded, issue)}
        self.assertEqual(audits[superseded.pk][0], "superseded")
        self.assertEqual(audits[issue.pk][0], "acknowledged")

        # The adapter still lists the generation, so the acknowledged disposition must stay.
        result = self.prune_after_retention()
        self.assertEqual((result.eligible, result.deleted), (0, 0))
        self.assertTrue(NSOApplyExecution.objects.filter(pk=execution.pk, consumption_state="acknowledged").exists())
        self.assertTrue(consume_static_route_settlements(self.management).drained)

        # Adapter-side retention drops the generation; the local history is then free to go.
        self.history_listing.clear()
        result = self.prune_after_retention()
        self.assertEqual((result.eligible, result.deleted), (1, 1))
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=execution.attempt_id).exists())
        self.assertFalse(NSOApplyExecution.objects.exists())
        self.assertEqual({row.pk: self.audit(row) for row in (superseded, issue)}, audits)
        page = client.get(self.tab_url())
        self.assertContains(page, 'data-acknowledgement-job="40"', count=1)
        self.assertContains(page, "placeholder pre-ledger carrier")
        self.assertContains(page, audits[issue.pk][5])
        self.assertContains(page, "The cursor was already past the carrier of this job.")
        self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})


class TestOwnerAcknowledgementRetention(
    RetentionHelpers, AcknowledgementViewHelpers, BGPSettlementHelpers, BGPCaptureCase
):
    def test_the_attempt_behind_unknown_owner_outcomes_outlives_the_prune(self):
        issue = self.stall_invalid_manual_carrier()
        client = self.client_for(self.acknowledger())
        client.post(self.action_url(), self.form(issue, reason="placeholder unreadable carrier"))
        self.drain_queue()
        attempt = NSOApplyAttempt.objects.get(pk=self.attempt.pk)
        self.assertEqual(attempt.lifecycle, "terminal")
        audit = self.audit(issue)
        original = self.adapter._handle

        def unlisted(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                return make_response(200, [])
            return response

        self.adapter._handle = unlisted
        result = self.prune_after_retention()
        self.assertEqual((result.eligible, result.deleted), (0, 0))
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertTrue(
            NSOApplyExecution.objects.filter(
                attempt=attempt, carrier_job_id=501, consumption_state="acknowledged"
            ).exists()
        )
        self.assertEqual(self.audit(issue), audit)
        for row in self.owners:
            row.refresh_from_db()
            projection = row_projection(row)
            self.assertEqual((row.status, projection["label"]), ("apply_failed", "outcome unknown"))
            self.assertEqual(projection["advisory"], ADVISORY.format(501))
