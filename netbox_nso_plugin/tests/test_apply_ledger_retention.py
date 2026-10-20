# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Retain live evidence and prune bounded, expired terminal history."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
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
