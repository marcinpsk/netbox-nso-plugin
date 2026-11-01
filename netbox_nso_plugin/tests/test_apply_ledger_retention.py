# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Retain live evidence and prune bounded, expired terminal history."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.management import CommandError, call_command
from django.test import TransactionTestCase
from django.utils import timezone

from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.models import (
    NSOAcceptedSelection,
    NSOApplyAttempt,
    NSOApplyExecution,
    NSOApplyOverlaySnapshot,
    NSODeviceManagement,
    NSOOverlaySettlementFence,
)

from ._outbox_case import make_managed
from ._settlement_adapter import FakeAdapter, LoopbackOnlySession
from .mixins import _CascadeFlushMixin


class TestApplyLedgerRetention(_CascadeFlushMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("ledger-retention", 1767)
        self.adapter = FakeAdapter()
        self.addCleanup(self.adapter.stop)
        self.adapter.store.add_device(nso_instance="placeholder", nso_device_name="placeholder", device_id=1767)
        for boundary in (
            patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession),
            patch(
                "netbox_nso_plugin.adapter_client._resolve_config",
                return_value={
                    "url": self.adapter.url,
                    "token": "placeholder",
                    "verify_tls": True,
                    "ca_cert_path": None,
                    "timeout": 10,
                },
            ),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    def history(self, days=31, management=None):
        management = management or self.management
        stamp = timezone.now() - timedelta(days=days)
        attempt = NSOApplyAttempt.objects.create(
            management=management,
            adapter_device_id=management.adapter_device_id,
            trigger_kind="cadence",
            selected={"bgp": 41},
            lifecycle="terminal",
            completed_at=stamp,
        )
        identity = NSOApplyExecution.objects.count() + 1
        generation = {
            "generation_id": identity,
            "seq": 1,
            "status": "settled",
            "sections": ["bgp"],
            "stream_revisions": {"bgp": 7},
            "source_push_seq": {"bgp": 41},
            "carrier_job_id": identity,
            "carrier_job_status": "succeeded",
            "carrier_job_result": {
                "bgp_count_by_outcome": dict(in_sync=0, apply_failed=0, not_applied=0, unavailable=0),
                "row_outcomes": [],
                "refusals": [],
                "settlement_owners": [],
                "commit_acknowledged": True,
            },
            "carrier_job_error": None,
            "updated_at": stamp.isoformat(),
            "row_outcomes": [],
            "refusals": [],
        }
        execution = NSOApplyExecution.objects.create(
            attempt=attempt,
            management=management,
            mapping_epoch=mapping_epoch(management),
            adapter_device_id=management.adapter_device_id,
            store_incarnation=self.adapter.store.incarnation,
            generation_id=identity,
            generation_seq=1,
            carrier_job_id=identity,
            settle_seq=1,
            source_selectors={"source_push_seq": {"bgp": 41}, "stream_revisions": {"bgp": 7}},
            commit_acknowledged=True,
            terminal_projection=generation,
            terminal_at=stamp,
            consumption_state="consumed",
            consumed_at=stamp,
        )
        NSODeviceManagement.objects.filter(pk=management.pk).update(
            settle_cursor_device_id=management.adapter_device_id,
            settle_cursor_incarnation=execution.store_incarnation,
            settle_cursor_seq=2,
        )
        return attempt, execution

    def adapter_down(self):
        """Fail every feed read as an unreachable adapter; a stopped fake still serves kept-alive connections."""
        from netbox_nso_plugin import adapter_client

        return patch.object(
            adapter_client,
            "get_settlement_feed",
            side_effect=adapter_client.AdapterError("Adapter unreachable.", code="nso_unreachable"),
        )

    def prune(self, **options):
        return call_command("nso_prune_apply_ledger", stdout=StringIO(), **options)

    def test_completed_unreferenced_attempt_is_prunable(self):
        attempt, _execution = self.history()
        self.prune(execute=True, batch_size=1)
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())

    def test_pruning_keeps_active_refusal_and_referenced_prior_attempt(self):
        attempt, execution = self.history()
        from netbox_nso_plugin.models import NSOLoggingHostState
        from netbox_nso_plugin.signals import suppress_intent_push

        from ._outbox_case import without_commit_drain
        from ._ownership_case import acquire_overlay

        with without_commit_drain(), suppress_intent_push():
            current = acquire_overlay(
                NSOLoggingHostState,
                management=self.management,
                address="198.18.0.17",
                status="apply_failed",
                apply_attempt_id=attempt.pk,
                last_apply_error="placeholder current refusal",
            )
        label = current._meta.label_lower
        snapshot = NSOApplyOverlaySnapshot.objects.create(
            attempt=attempt,
            model_label=label,
            overlay_pk=current.pk,
            mapping_epoch=execution.mapping_epoch,
            prior_status="accepted",
            expected_status="deploying",
            content_digest="a" * 64,
            settlement_disposition="refused",
        )
        snapshot.executions.add(execution)
        fence, _created = NSOOverlaySettlementFence.objects.get_or_create(
            management=self.management,
            model_label=label,
            overlay_pk=current.pk,
            mapping_epoch=execution.mapping_epoch,
        )
        fence.current_attempt = attempt
        fence.last_execution = execution
        fence.save(update_fields=("current_attempt", "last_execution"))
        self.prune(execute=True)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())
        fence.delete()
        from ._outbox_case import mirror_update

        mirror_update(current, apply_attempt_id=None)
        other = NSOApplyAttempt.objects.create(management=self.management)
        snapshot.pk = None
        snapshot.attempt = other
        snapshot.prior_apply_attempt = attempt
        snapshot.save()
        self.prune(execute=True)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())

    def test_pruning_keeps_repeatable_accepted_selection(self):
        attempt, execution = self.history()
        selection = NSOAcceptedSelection.objects.create(
            management=self.management,
            mapping_epoch=execution.mapping_epoch,
            stream="bgp",
            source_push_seq=41,
            plugin_revision=1,
            request_digest="b" * 64,
            publication_state="sent",
            acknowledged_at=timezone.now(),
            binding_state="bound",
            store_incarnation=execution.store_incarnation,
            frozen_revision=7,
        )
        NSOApplyOverlaySnapshot.objects.create(
            attempt=attempt,
            selection=selection,
            model_label="netbox_nso_plugin.nsobgprouterstate",
            overlay_pk=17,
            mapping_epoch=execution.mapping_epoch,
            prior_status="accepted",
            expected_status="deploying",
            content_digest="a" * 64,
            settlement_disposition="in_sync",
        )
        self.prune(execute=True)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertTrue(NSOAcceptedSelection.objects.filter(pk=selection.pk).exists())

    def test_retention_floor_keeps_29_days_and_prunes_31_days(self):
        young, _execution = self.history(29)
        self.prune(execute=True)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=young.pk).exists())
        old, _execution = self.history(31)
        self.prune(execute=True)
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=old.pk).exists())

    def test_quiesced_gate_reports_a_command_error_and_deletes_nothing(self):
        from django.core.management.base import CommandError

        from netbox_nso_plugin import deployment

        attempt, _execution = self.history()
        deployment.quiesce()
        self.addCleanup(deployment.resume)
        with self.assertRaisesRegex(CommandError, "Apply ledger retention"):
            self.prune(execute=True)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())

    def test_prune_dry_run_deletes_nothing(self):
        attempt, _execution = self.history()
        self.prune()
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())

    def test_daily_job_uses_registered_prune_path(self):
        from core.choices import JobIntervalChoices
        from core.models import Job
        from netbox.registry import registry

        from netbox_nso_plugin.jobs import PruneApplyLedgerJob

        attempt, _execution = self.history()
        job = Job.objects.create(name=PruneApplyLedgerJob.name, job_id=uuid4())
        PruneApplyLedgerJob(job).run()
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertIn(PruneApplyLedgerJob, registry["system_jobs"])
        self.assertEqual(registry["system_jobs"][PruneApplyLedgerJob]["interval"], JobIntervalChoices.INTERVAL_DAILY)

    def test_daily_job_drains_more_than_one_batch(self):
        from core.models import Job

        from netbox_nso_plugin.apply_retention import DEFAULT_BATCH_SIZE
        from netbox_nso_plugin.jobs import PruneApplyLedgerJob

        for _index in range(DEFAULT_BATCH_SIZE + 1):
            self.history()
        job = Job.objects.create(name=PruneApplyLedgerJob.name, job_id=uuid4())
        PruneApplyLedgerJob(job).run()
        self.assertFalse(NSOApplyAttempt.objects.exists())
        self.assertIsNone(job.data["apply_ledger_cursor"])

    def test_daily_job_keeps_the_cursor_when_its_time_budget_ends(self):
        from core.models import Job

        from netbox_nso_plugin import jobs
        from netbox_nso_plugin.apply_retention import DEFAULT_BATCH_SIZE

        for _index in range(DEFAULT_BATCH_SIZE + 1):
            self.history()
        job = Job.objects.create(name=jobs.PruneApplyLedgerJob.name, job_id=uuid4())
        with patch.object(jobs, "PRUNE_TIME_BUDGET_SECONDS", 0):
            jobs.PruneApplyLedgerJob(job).run()
        self.assertEqual(NSOApplyAttempt.objects.count(), 1)
        self.assertIsNotNone(job.data["apply_ledger_cursor"])

    def test_adapter_outage_on_one_device_retains_only_that_device(self):
        _device, other = make_managed("ledger-retention-other", 1768, index=2)
        other.refresh_from_db()
        self.adapter.store.add_device(nso_instance="placeholder", nso_device_name="placeholder-2", device_id=1768)
        unreachable, _execution = self.history(days=32, management=other)
        removable, _execution = self.history()
        self.adapter.store.feed_error_devices.add(1768)
        output = StringIO()
        call_command("nso_prune_apply_ledger", execute=True, stdout=output)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=unreachable.pk).exists())
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=removable.pk).exists())
        self.assertIn("deleted=1", output.getvalue())

    def test_duplicate_generations_retain_one_device_and_continue_the_batch(self):
        from urllib.parse import urlsplit

        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.apply_retention import prune_apply_ledger

        from ._adapter_http import make_generation_listing, make_response, make_transport_session

        _device, other = make_managed("ledger-retention-invalid", 1768, index=2)
        other.refresh_from_db()
        retained, _execution = self.history(days=32, management=other)
        removable, _execution = self.history()
        row = make_generation_listing()
        requests = []
        session = make_transport_session(make_response(200, []))

        def send(request, **kwargs):
            requests.append(request.url)
            path = urlsplit(request.url).path
            body = [row, {**row, "seq": row["seq"] + 1}] if path.endswith("/1768/generations") else []
            from ._settlement_wire import generation_wire

            response = make_response(200, [generation_wire(value, 1768) for value in body])
            response.headers["X-Store-Incarnation"] = self.adapter.store.incarnation
            return response

        session.send.side_effect = send
        adapter_client.reset_session()
        try:
            with patch("netbox_nso_plugin.adapter_client.requests.Session", autospec=True, return_value=session):
                result = prune_apply_ledger(execute=True)
        finally:
            adapter_client.reset_session()
        self.assertEqual((result.examined, result.eligible, result.deleted), (2, 1, 1))
        self.assertFalse(result.adapter_unavailable)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=retained.pk).exists())
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=removable.pk).exists())
        self.assertTrue(any("/1767/generations" in url for url in requests))

    def test_unreachable_adapter_stops_the_batch_at_the_first_device(self):
        from netbox_nso_plugin import apply_retention
        from netbox_nso_plugin.apply_retention import prune_apply_ledger

        kept = [self.history(days=33)[0]]
        for index, device_id in enumerate((1768, 1769), start=2):
            _device, management = make_managed(f"ledger-retention-{device_id}", device_id, index=index)
            management.refresh_from_db()
            kept.append(self.history(days=32, management=management)[0])
        with (
            self.adapter_down(),
            patch.object(apply_retention, "_remote_generations", wraps=apply_retention._remote_generations) as read,
        ):
            result = prune_apply_ledger(execute=True, cursor=None)
        self.assertEqual(read.call_count, 1)
        self.assertTrue(result.adapter_unavailable)
        self.assertEqual((result.examined, result.eligible, result.deleted, result.next_cursor), (0, 0, 0, None))
        self.assertEqual(NSOApplyAttempt.objects.filter(pk__in=[attempt.pk for attempt in kept]).count(), 3)

    def test_unreachable_adapter_resumes_after_the_examined_candidates(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.apply_retention import prune_apply_ledger

        removable, _execution = self.history(days=33)
        _device, other = make_managed("ledger-retention-1768", 1768, index=2)
        other.refresh_from_db()
        self.history(days=32, management=other)
        real = adapter_client.get_settlement_feed

        def feed(device_id, **kwargs):
            if device_id == 1768:
                raise adapter_client.AdapterError("Adapter did not respond.", code="nso_timeout")
            return real(device_id, **kwargs)

        with patch.object(adapter_client, "get_settlement_feed", side_effect=feed):
            result = prune_apply_ledger(execute=True, cursor=None)
        self.assertTrue(result.adapter_unavailable)
        self.assertEqual((result.examined, result.deleted), (1, 1))
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=removable.pk).exists())
        _stamp, attempt_id = result.next_cursor.split("|", 1)
        self.assertEqual(attempt_id, str(removable.pk))

    def test_prune_command_fails_when_the_adapter_is_unreachable(self):
        attempt, _execution = self.history()
        output = StringIO()
        with self.adapter_down(), self.assertRaisesMessage(CommandError, "If next_cursor is none, omit --after"):
            call_command("nso_prune_apply_ledger", execute=True, stdout=output)
        self.assertIn("examined=0 eligible=0 deleted=0 execute=True next_cursor=none", output.getvalue())
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())

    def test_a_finished_scan_reports_no_cursor(self):
        self.history()
        output = StringIO()
        call_command("nso_prune_apply_ledger", stdout=output)
        self.assertIn("eligible=1 deleted=0 execute=False next_cursor=none", output.getvalue())

    def test_a_malformed_cursor_is_a_named_command_error(self):
        for cursor in ("none", "start", "end"):
            with self.subTest(cursor=cursor), self.assertRaisesMessage(CommandError, "next_cursor value"):
                call_command("nso_prune_apply_ledger", after=cursor, stdout=StringIO())

    def test_daily_job_stops_when_the_adapter_is_unreachable(self):
        from core.models import Job

        from netbox_nso_plugin import apply_retention
        from netbox_nso_plugin.apply_retention import DEFAULT_BATCH_SIZE
        from netbox_nso_plugin.jobs import PruneApplyLedgerJob

        for _index in range(DEFAULT_BATCH_SIZE + 1):
            self.history()
        job = Job.objects.create(name=PruneApplyLedgerJob.name, job_id=uuid4())
        with (
            self.adapter_down(),
            patch.object(apply_retention, "_remote_generations", wraps=apply_retention._remote_generations) as read,
        ):
            PruneApplyLedgerJob(job).run()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(NSOApplyAttempt.objects.count(), DEFAULT_BATCH_SIZE + 1)
        self.assertIsNone(job.data["apply_ledger_cursor"])

    def test_remote_generations_are_read_once_per_device(self):
        for _index in range(3):
            self.history()
        self.adapter.store.feed_requests.clear()
        self.prune(execute=True)
        self.assertFalse(NSOApplyAttempt.objects.exists())
        self.assertEqual(len(self.adapter.store.feed_requests), 2)

    def test_certificate_check_reads_every_listed_generation_in_one_query(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.apply_retention import _historical_certificate_required, _remote_generations

        attempt, execution = self.history()
        for seq in range(1, 6):
            self.adapter.store.add_generation(
                1767,
                generation_id=100 + seq,
                seq=seq,
                status="settled",
                job_id=None,
                carrier_job_id=1000 + seq,
                carrier_job_status="succeeded",
                mode="detach",
                settlement_cohort=None,
                digest="a" * 64,
                stream_revisions={"bgp": 7},
                source_push_seq={"bgp": 41},
            )
        remote = _remote_generations(self.management.pk)
        self.management.refresh_from_db()
        with CaptureQueriesContext(connection) as unlisted:
            self.assertFalse(_historical_certificate_required(attempt, self.management, remote))
        self.assertEqual(len(unlisted), 2)
        NSOApplyExecution.objects.filter(pk=execution.pk).update(generation_id=103)
        self.assertTrue(_historical_certificate_required(attempt, self.management, remote))

    def test_terminal_metadata_certifies_every_listed_carrier_in_one_query(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.apply_settlement import _BoundEvidence, _require_terminal_metadata

        listing = []
        for _index in range(3):
            _attempt, execution = self.history()
            listing.append(
                {
                    **{key: execution.terminal_projection[key] for key in ("generation_id", "seq", "status")},
                    **{key: execution.terminal_projection[key] for key in ("stream_revisions", "source_push_seq")},
                    "carrier_job_id": execution.carrier_job_id,
                    "carrier_job_status": "succeeded",
                }
            )
        bound = _BoundEvidence(attempts=[])
        bound.settlement_sequences = {}
        bound.automatic_generations = []
        self.management.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            _require_terminal_metadata(bound, listing, self.management, self.adapter.store.incarnation)
        reads = [query for query in queries if NSOApplyExecution._meta.db_table in query["sql"]]
        self.assertEqual(len(reads), 1)
        self.assertEqual(
            [row["carrier_job_id"] for row in bound.automatic_generations], [row["carrier_job_id"] for row in listing]
        )

    def test_unconsumed_malformed_and_cursor_gap_history_stays(self):
        for change in (
            {"consumption_state": "unconsumed"},
            {"invariant_code": "invalid_terminal_batch"},
            {"terminal_projection": {"invalid": True}},
            {"settle_seq": 3},
        ):
            with self.subTest(change=change):
                attempt, execution = self.history()
                NSOApplyExecution.objects.filter(pk=execution.pk).update(**change)
                self.prune(execute=True)
                self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())
        attempt, _execution = self.history()
        NSOApplyAttempt.objects.filter(pk=attempt.pk).update(selected={"bgp": 42})
        self.prune(execute=True)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())

    def test_prune_batch_is_bounded(self):
        for _index in range(3):
            self.history()
        self.prune(execute=True, batch_size=1)
        self.assertEqual(NSOApplyAttempt.objects.count(), 2)

    def test_pruning_continues_past_retained_history(self):
        blocked, execution = self.history()
        NSOApplyExecution.objects.filter(pk=execution.pk).update(invariant_code="invalid_terminal_batch")
        removable, _execution = self.history()
        output = StringIO()
        call_command("nso_prune_apply_ledger", execute=True, batch_size=1, stdout=output)
        cursor = output.getvalue().strip().split("next_cursor=", 1)[1]
        call_command("nso_prune_apply_ledger", execute=True, batch_size=1, after=cursor, stdout=StringIO())
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=blocked.pk).exists())
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=removable.pk).exists())

    def acknowledged_history(self, *, listed):
        from netbox_nso_plugin.models import NSOSettlementIssue

        attempt, execution = self.history()
        now = timezone.now()
        issue = NSOSettlementIssue.objects.create(
            management=self.management,
            mapping_epoch=mapping_epoch(self.management),
            adapter_device_id=self.management.adapter_device_id,
            store_incarnation=execution.store_incarnation,
            job_id=900,
            settle_seq=1,
            request_fence={},
            stall_class="evidence_invariant",
            stall_reason="missing_generation_evidence",
            position="historical",
            cursor_before=2,
            cursor_after=2,
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
        NSOApplyExecution.objects.create(
            attempt=attempt,
            management=self.management,
            mapping_epoch=mapping_epoch(self.management),
            adapter_device_id=self.management.adapter_device_id,
            store_incarnation=execution.store_incarnation,
            generation_id=900,
            generation_seq=1,
            carrier_job_id=900,
            settle_seq=1,
            consumption_state="acknowledged",
            settlement_issue=issue,
        )
        if listed:
            self.adapter.store.add_generation(
                1767,
                generation_id=900,
                seq=1,
                status="failed",
                job_id=900,
                carrier_job_id=900,
                carrier_job_status="succeeded",
                mode="networked",
                settlement_cohort=None,
                digest="a" * 64,
                stream_revisions={"bgp": 7},
                source_push_seq={"bgp": 41},
            )
        return attempt, issue

    def test_an_acknowledged_obligation_stays_while_the_adapter_lists_its_generation(self):
        attempt, _issue = self.acknowledged_history(listed=True)
        self.prune(execute=True)
        self.assertTrue(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())

    def test_an_unlisted_acknowledged_obligation_is_pruned_and_its_audit_stays(self):
        from netbox_nso_plugin.models import NSOSettlementIssue

        attempt, issue = self.acknowledged_history(listed=False)
        self.prune(execute=True)
        self.assertFalse(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertFalse(NSOApplyExecution.objects.filter(settlement_issue=issue).exists())
        self.assertEqual(NSOSettlementIssue.objects.get(pk=issue.pk).state, "acknowledged")
