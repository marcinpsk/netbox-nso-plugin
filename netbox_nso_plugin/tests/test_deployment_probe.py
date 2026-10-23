# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Admit one durable BGP probe through authenticated HTTP and asynchronous RQ."""

import json
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import django_rq
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections, transaction
from rq import Queue, SimpleWorker
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_nso_plugin import deployment
from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyExecution, NSOIntentDeploymentControl

from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import make_managed
from ._settlement_adapter import FakeAdapter, LoopbackOnlySession
from .test_durable_bgp_capture import BGPCaptureCase


class DeploymentCase(BGPSettlementHelpers, BGPCaptureCase):
    def terminal(self, *args, **kwargs):
        from django.utils import timezone

        generation = super().terminal(*args, **kwargs)
        generation["updated_at"] = timezone.now().isoformat()
        return generation

    def setUp(self):
        super().setUp()
        self._stop_adapter()
        self.socket_adapter = FakeAdapter()
        self.server = self.socket_adapter._server
        self.thread = self.socket_adapter._thread
        self.server.adapter = self.adapter
        self.server.management_id = self.management.pk
        self.server.adapter_url = self.socket_adapter.url
        from .test_durable_bgp_capture import _CaptureHandler

        class DeploymentHandler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", handler.server.store.incarnation)
                super().end_headers()

        self.server.RequestHandlerClass = DeploymentHandler
        transport = patch(
            "netbox_nso_plugin.adapter_client._resolve_config",
            return_value={
                "url": self.socket_adapter.url,
                "token": "placeholder",
                "verify_tls": True,
                "ca_cert_path": None,
                "timeout": 10,
            },
        )
        transport.start()
        self.addCleanup(transport.stop)
        transport = patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession)
        transport.start()
        self.addCleanup(transport.stop)
        from ._adapter_http import make_response

        original = self.adapter._handle

        def boundary(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                params = kwargs.get("params") or {}
                return make_response(
                    200,
                    self.socket_adapter.store.feed(
                        self.management.adapter_device_id,
                        int(params.get("after_settle_seq", 0)),
                        int(params.get("limit", 100)),
                    ),
                )
            if method == "GET" and url.endswith("/intent-receipts"):
                response = original(method, url, **kwargs)
                document = response.json()
                for endpoint, receipt in self.adapter.receipts.items():
                    if endpoint.endswith("/bgp-intent"):
                        document["receipts"].append(
                            {
                                "device_id": self.management.adapter_device_id,
                                "section": "bgp",
                                "push_seq": receipt["push_seq"],
                                "request_digest": receipt["digest"],
                                "store_only": receipt["params"].get("store_only") == "true",
                                "delete_origin": False,
                                "backfill_only": False,
                                "status_code": 200,
                                "response": receipt["response"],
                                "generation_id": None,
                                "created_at": self.stamp.isoformat(),
                                "updated_at": self.stamp.isoformat(),
                            }
                        )
                return make_response(200, document)
            return original(method, url, **kwargs)

        self.adapter._handle = boundary
        self.queue = Queue(f"nso-probe-{uuid4().hex}", connection=django_rq.get_queue("default").connection)
        self.addCleanup(self.queue.delete, delete_jobs=True)
        self.assertTrue(self.queue.is_async)
        user = User.objects.create_superuser(username=f"placeholder-probe-{uuid4().hex[:8]}")
        token = Token.objects.create(user=user)
        self.header = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"}

    def serve_terminal_evidence(self, *args, **kwargs):
        super().serve_terminal_evidence(*args, **kwargs)
        self.server.evidence_incarnation = self.socket_adapter.store.incarnation

    def _prepare_bgp_observation_capture(self):
        from datetime import datetime

        from ._outbox_case import mirror_update, without_commit_drain
        from .test_read_gate import _INC_A

        mirror_update(
            self.management,
            adapter_incarnation=self.socket_adapter.store.incarnation,
            adapter_incarnation_born=datetime.fromisoformat(_INC_A[1]),
            adapter_source_epoch=1,
            source_epoch_aware=True,
        )
        with without_commit_drain():
            self.management.manage_routing = True
            self.management.save(update_fields=("manage_routing",))
        self.prepare_automatic()

    def _serve_bgp_observation(self, *args, **kwargs):
        from ._adapter_http import make_response

        super()._serve_bgp_observation(*args, **kwargs)
        original = self.adapter._handle

        def read(method, url, **options):
            response = original(method, url, **options)
            if url.endswith("/bgp-config"):
                document = response.json()
                document["read_state"]["incarnation"] = self.management.adapter_incarnation
                return make_response(200, document)
            return response

        self.adapter._handle = read

    def command(self, **options):
        out = StringIO()
        call_command("nso_intent_deployment_gate", stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    def probe(self, refused=1, *, settle_prior=False):
        from .test_apply_selector import _promoted

        self._prepare_bgp_observation_capture()
        if settle_prior:
            from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

            generation = self.terminal(refused=None)
            generation.update(generation_id=80, seq=3, carrier_job_id=500)
            for outcome in generation["row_outcomes"]:
                outcome["generation_id"] = outcome["carrier"]["generation_id"] = 80
            for carrier in generation["carrier_job_result"]["settlement_owners"]:
                carrier["generation_id"] = 80
            settle_terminal_generation(self.management, generation, self.socket_adapter.store.incarnation, settle_seq=1)
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")

        def admission(selected):
            response = _promoted(selected)
            response["generations"] = response["generations"][:1]
            return 202, response

        self.adapter.apply_response = admission
        attempt_id = uuid4()
        self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        self.attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        generation = self.terminal(refused=refused)
        if refused == 0:
            for outcome in generation["row_outcomes"]:
                for cause in outcome["refusals"]:
                    cause["field"] = "bgp.router.router_id"
            generation["refusals"][0]["field"] = "bgp.router.router_id"
        self._serve_bgp_observation(generation)
        self.serve_terminal_evidence(generation, manual=True, settle_seq=2 if settle_prior else 1)
        self.command(register_probe=str(self.attempt.pk))

    def unown_router(self):
        import copy

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes
        from netbox_nso_plugin.signals import suppress_intent_push

        candidate = copy.copy(self.owners[0])
        candidate.status = "imported"
        candidate.accepted_at = None
        fields = ("status", "accepted_at")
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=fields),))
        with suppress_intent_push(), renderer_writes(plan) as writer:
            writer.save(candidate, update_fields=fields)

    def notify(self, device_id=None):
        with patch("django_rq.get_queue", return_value=self.queue):
            return self.client.post(
                "/api/plugins/nso/sync-complete/",
                {
                    "netbox_device_id": device_id or self.device.pk,
                },
                content_type="application/json",
                **self.header,
            )

    def work(self):
        with patch("django_rq.get_queue", return_value=self.queue):
            SimpleWorker([self.queue], connection=self.queue.connection).work(burst=True, with_scheduler=False)
        self.assertFalse(self.queue.failed_job_registry.get_job_ids())


class TestDeploymentProbe(DeploymentCase):
    def test_pruning_preserves_nonhead_certificate_when_remote_carrier_is_unavailable(self):
        from datetime import timedelta

        from django.utils import timezone

        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self._prepare_bgp_observation_capture()
        late = self.terminal()
        newest = self.terminal(refused=None)
        newest.update(generation_id=82, seq=5, carrier_job_id=502)
        for outcome in newest["row_outcomes"]:
            outcome["generation_id"] = outcome["carrier"]["generation_id"] = 82
        for carrier in newest["carrier_job_result"]["settlement_owners"]:
            carrier["generation_id"] = 82
        incarnation = self.socket_adapter.store.incarnation
        settle_terminal_generation(self.management, newest, incarnation, settle_seq=2)
        execution = settle_terminal_generation(self.management, late, incarnation, settle_seq=1)
        self.assertEqual(execution.consumption_state, "superseded")
        self.assertFalse(execution.overlay_snapshots.exists())
        NSOApplyAttempt.objects.filter(pk=execution.attempt_id).update(completed_at=timezone.now() - timedelta(days=31))
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation=incarnation,
            settle_cursor_seq=3,
        )
        self.serve_terminal_evidence(late, head=False, pruned=True)
        call_command("nso_prune_apply_ledger", execute=True, stdout=StringIO())
        with self.assertRaisesRegex(EvidenceInvariantError, "missing_generation_evidence"):
            load_deployment_evidence(self.management)
        execution.refresh_from_db()
        self.assertEqual(execution.consumption_state, "superseded")

    def test_deployment_verify_requires_cutover_contract_and_settlement_probe(self):
        deployment.quiesce(paired_activation=True)
        with self.assertRaisesRegex(CommandError, "contract|artifact|probe"):
            self.command(verify=True, device_id=self.device.pk)
        self.assertTrue(deployment.is_quiesced())
        self.assertEqual(self.adapter.apply_requests, [])

    def test_deployment_probe_authority_survives_process_restart(self):
        self.probe()
        connections.close_all()
        with transaction.atomic():
            control = NSOIntentDeploymentControl.objects.get(pk=1)
            self.assertEqual(control.verification_attempt_id, self.attempt.pk)
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "consumed")
        self.assertTrue(deployment.is_quiesced())

    def test_callback_worker_admits_only_bound_verification_attempt(self):
        self.probe()
        self.assertEqual(self.notify().status_code, 202)
        self.assertEqual(self.queue.count, 1)
        self.work()
        execution = NSOApplyExecution.objects.get(attempt=self.attempt)
        self.assertEqual(execution.consumption_state, "consumed")
        self.assertEqual(execution.carrier_job_id, 501)
        self.assertEqual(execution.generation_id, 81)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertTrue(deployment.is_quiesced())
        from netbox_nso_plugin import drain, outbox
        from netbox_nso_plugin.intent_drift import resync_intent
        from netbox_nso_plugin.reconcile import reconcile_device

        for operation in (lambda: reconcile_device(None), lambda: resync_intent(None, None)):
            with self.assertRaises(deployment.DeploymentQuiesced):
                operation()
        self.assertEqual(drain.drain_intent_outbox(), (0, 0))
        with self.assertRaises(deployment.DeploymentQuiesced), transaction.atomic():
            outbox.enqueue(self.device.pk, "bgp")

    def test_probe_worker_never_runs_unselected_family_or_overlay(self):
        self.probe()
        from netbox_nso_plugin.models import NSOBGPRouterState, NSOLoggingHostState, NSOOverlaySettlementFence
        from netbox_nso_plugin.signals import suppress_intent_push

        from ._ownership_case import acquire_overlay

        with deployment.gate_bypass(), suppress_intent_push(), transaction.atomic():
            unselected_bgp = acquire_overlay(
                NSOBGPRouterState, management=self.management, asn_str="64514", status="imported"
            )
            unselected = acquire_overlay(
                NSOLoggingHostState, management=self.management, address="198.18.0.17", status="accepted"
            )
        before = (unselected.status, unselected.last_apply_at, unselected.last_apply_error)
        bgp_before = list(
            NSOOverlaySettlementFence.objects.filter(
                model_label=unselected_bgp._meta.label_lower,
                overlay_pk=unselected_bgp.pk,
            ).values()
        )
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        unselected.refresh_from_db()
        unselected_bgp.refresh_from_db()
        self.assertEqual(unselected_bgp.status, "imported")
        self.assertEqual(
            list(
                NSOOverlaySettlementFence.objects.filter(
                    model_label=unselected_bgp._meta.label_lower,
                    overlay_pk=unselected_bgp.pk,
                ).values()
            ),
            bgp_before,
        )
        self.assertEqual((unselected.status, unselected.last_apply_at, unselected.last_apply_error), before)
        self.assertFalse(
            any(url.endswith(("/bfd", "/logging", "/interfaces")) for _method, url in self.evidence_requests)
        )
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "consumed")

    def test_stale_or_other_device_probe_is_blocked(self):
        self.probe()
        with deployment.gate_bypass():
            other, _management = make_managed("other-probe", 1768)
        blocked = self.notify(other.pk)
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.json(), {"detail": "Device reconcile is deferred while the deployment gate is active"})
        self.assertEqual(self.notify().status_code, 202)
        NSOIntentDeploymentControl.objects.filter(pk=1).update(verification_attempt=None)
        self.work()
        self.assertNotEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "consumed")
        self.assertTrue(deployment.is_quiesced())

    def test_probe_failure_revokes_permit_without_resume(self):
        self.probe()
        from ._adapter_http import make_response

        original = self.adapter._handle

        def failed(method, url, **kwargs):
            if url.endswith("/bgp-config"):
                return make_response(503, {"error": {"code": "unavailable", "message": "Unavailable"}})
            return original(method, url, **kwargs)

        self.adapter._handle = failed
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertTrue(deployment.is_quiesced())

    def test_failed_probe_retry_revokes_permit_before_queued_worker_can_consume(self):
        from ._adapter_http import make_response

        self.probe()
        self.assertEqual(self.notify().status_code, 202)
        self.assertEqual(self.queue.count, 1)
        snapshots = list(self.attempt.overlay_snapshots.values("model_label", "overlay_pk", "settlement_disposition"))
        original = self.adapter._handle

        def failed(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                return make_response(503, {"error": {"code": "unavailable", "message": "Settlement feed is down"}})
            return original(method, url, **kwargs)

        self.adapter._handle = failed
        with self.assertRaisesRegex(CommandError, "Settlement feed is down"):
            self.command(start_probe=str(self.attempt.pk), device_id=self.device.pk)
        permit = NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id
        self.adapter._handle = original
        self.work()
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "unconsumed")
        self.assertIsNone(permit)
        self.assertEqual(
            list(self.attempt.overlay_snapshots.values("model_label", "overlay_pk", "settlement_disposition")),
            snapshots,
        )
        self.assertTrue(deployment.is_quiesced())

    def test_probe_retry_cannot_revoke_another_attempts_permit(self):
        self.probe()
        self.assertEqual(self.notify().status_code, 202)
        with self.assertRaisesRegex(CommandError, "Revoke the probe permit"):
            self.command(start_probe=str(uuid4()), device_id=self.device.pk)
        self.assertEqual(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id, self.attempt.pk)
        self.work()
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "consumed")
        self.assertTrue(deployment.is_quiesced())

    def test_resume_requires_revoked_probe_authority(self):
        self.probe()
        with self.assertRaisesRegex(CommandError, "probe|permit|verification"):
            self.command(resume=True)
        self.assertTrue(deployment.is_quiesced())
        with self.assertRaisesRegex(CommandError, "probe|permit|verification"):
            self.command(abort=True)
        self.assertTrue(deployment.is_quiesced())

    def test_quiesced_router_import_never_acquires_ownership(self):
        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation()
        self.unown_router()
        deployment.quiesce(paired_activation=True)
        result = json.loads(self.command(import_routers=True, device_id=self.device.pk))
        self.owners[0].refresh_from_db()
        self.assertIsNone(self.owners[0].accepted_at)
        self.assertEqual(self.owners[0].status, "imported")
        self.assertEqual(result[0]["id"], self.owners[0].pk)
        self.assertIn("scopes", result[0]["content"])
        self.assertTrue(deployment.is_quiesced())

    def test_quiesced_router_accept_requires_exact_operator_selection(self):
        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation()
        self.unown_router()
        deployment.quiesce(paired_activation=True)
        with self.assertRaises(CommandError):
            self.command(accept_routers=[self.owners[0].pk, 999999], device_id=self.device.pk)
        self.owners[0].refresh_from_db()
        self.assertIsNone(self.owners[0].accepted_at)
        self.command(accept_routers=[self.owners[0].pk], device_id=self.device.pk)
        self.owners[0].refresh_from_db()
        self.assertEqual(self.owners[0].status, "accepted")
        self.assertIsNotNone(self.owners[0].accepted_at)
        self.assertTrue(deployment.is_quiesced())

    def test_router_inventory_reports_an_unqualified_overlay_per_row(self):
        from netbox_nso_plugin.models import NSOBGPRouterState

        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation()
        self.unown_router()
        unlinked = NSOBGPRouterState.objects.create(management=self.management, asn_str="65010", status="imported")
        deployment.quiesce(paired_activation=True)
        result = json.loads(self.command(accept_routers=[self.owners[0].pk], device_id=self.device.pk))
        self.owners[0].refresh_from_db()
        self.assertEqual(self.owners[0].status, "accepted")
        by_id = {entry["id"]: entry for entry in result}
        self.assertIn("scopes", by_id[self.owners[0].pk]["content"])
        self.assertEqual(
            by_id[unlinked.pk],
            {"id": unlinked.pk, "error": "Import and accept a linked BGP router before sending BGP intent."},
        )
        self.assertTrue(deployment.is_quiesced())

    def test_quiesced_router_preparation_emits_no_apply_and_keeps_gate_closed(self):
        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        puts = len(self.adapter.capture_reads)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        self.assertEqual(len(self.adapter.capture_reads), puts + 1)
        self.assertEqual(self.adapter.apply_requests, [])
        self.assertEqual(self.adapter.requests[-1]["params"].get("store_only"), "true")
        self.assertEqual(self.adapter.jobs, [])
        self.assertEqual(self.queue.count, 0)
        self.assertTrue(deployment.is_quiesced())
        from netbox_nso_plugin.models import NSOAcceptedSelection

        self.assertTrue(NSOAcceptedSelection.objects.filter(acknowledged_at__isnull=False).exists())

    def test_failed_router_preparation_stays_quiesced(self):
        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        from ._adapter_http import make_response

        original = self.adapter._handle

        def failed(method, url, **kwargs):
            if method == "PUT" and url.endswith("/bgp-intent"):
                return make_response(503, {"error": {"code": "unavailable", "message": "Unavailable"}})
            return original(method, url, **kwargs)

        self.adapter._handle = failed
        puts = len(self.adapter.capture_reads)
        with self.assertRaises(CommandError):
            self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        self.assertTrue(deployment.is_quiesced())
        self.assertEqual(self.adapter.apply_requests, [])
        self.assertEqual(len(self.adapter.capture_reads), puts + 1)

    def test_mismatched_execution_revokes_probe_before_owner_writes(self):
        self.probe()
        before = list(self.attempt.overlay_snapshots.values("model_label", "overlay_pk", "settlement_disposition"))
        generation = self.terminal()
        generation["carrier_job_id"] = 502
        self.serve_terminal_evidence(generation, manual=True)
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        self.assertEqual(
            list(self.attempt.overlay_snapshots.values("model_label", "overlay_pk", "settlement_disposition")), before
        )
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertTrue(deployment.is_quiesced())

    def test_quiesced_read_does_not_publish_a_full_family_observation(self):
        self.probe()
        from netbox_nso_plugin.models import NSOFamilyObservation

        before = list(NSOFamilyObservation.objects.values())
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        self.assertEqual(list(NSOFamilyObservation.objects.values()), before)
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "consumed")

    def test_quiesced_read_fences_its_owners_and_a_full_read_converges(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.deployment_probe import read_bgp
        from netbox_nso_plugin.lifecycle_fence import fence_for
        from netbox_nso_plugin.models import NSOFamilyReadState
        from netbox_nso_plugin.refusal_registry import OVERLAYS
        from netbox_nso_plugin.signals import suppress_intent_push

        def fences():
            with transaction.atomic():
                return {
                    (item.model_label, item.overlay_pk): fence_for(
                        OVERLAYS[item.model_label].model.objects.get(pk=item.overlay_pk)
                    )
                    for item in self.attempt.overlay_snapshots.all()
                }

        def applied():
            return NSOFamilyReadState.objects.filter(
                management=self.management, family="bgp", applied_payload_revision__isnull=False
            ).values_list("applied_payload_revision", flat=True)

        self.probe(refused=None)
        before = fences()
        applied_before = list(applied())
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        revision = adapter_client.get_bgp_config(self.management.adapter_device_id)["read_state"]["payload_revision"]
        probed = fences()
        self.assertEqual(list(applied()), applied_before)
        self.assertNotIn(revision, applied_before)
        # The bounded read refreshed these owners, so captures older than it must stop matching.
        for key, fence in probed.items():
            self.assertEqual(fence.observation_watermark["bgp"]["payload_revision"], revision)
            self.assertGreater(fence.bookkeeping_revision, before[key].bookkeeping_revision)
        self.management.refresh_from_db()
        with deployment.gate_bypass(), suppress_intent_push():
            read_bgp(self.management)
        self.assertEqual(list(applied()), [revision])
        self.assertEqual(
            {key: (fence.observation_watermark, fence.bookkeeping_revision) for key, fence in fences().items()},
            {key: (fence.observation_watermark, fence.bookkeeping_revision) for key, fence in probed.items()},
        )

    def test_success_probe_settles_full_membership_without_another_put(self):
        self.probe(refused=None)
        puts = len(self.adapter.capture_reads)
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
            self.assertEqual(row.apply_attempt_id, self.attempt.pk)
        self.assertEqual(len(self.adapter.capture_reads), puts)
        self.assertEqual(len(self.adapter.apply_requests), 1)
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)

    def test_router_refusal_probe_restores_both_peers(self):
        self.probe(refused=0)
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        self.owners[0].refresh_from_db()
        self.assertEqual(self.owners[0].status, "apply_failed")
        for row in self.owners[1:]:
            row.refresh_from_db()
            self.assertEqual(
                (row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[row._meta.label_lower, row.pk][1:],
            )
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)

    def test_adapter_only_active_stream_blocks_readiness(self):
        from netbox_nso_plugin.deployment_checks import check_active_streams
        from netbox_nso_plugin.deployment_probe import ProbeBlocked

        from ._adapter_http import make_response

        NSOApplyAttempt.objects.all().delete()
        original = self.adapter._handle

        def inventory(method, url, **kwargs):
            if url.endswith("/intent-summary"):
                return make_response(
                    200,
                    {
                        "device_id": self.management.adapter_device_id,
                        "scopes": {"bgp_router_intent": {"count": 1, "applied": 0, "failed": 0}},
                        "pending_clear": {},
                    },
                )
            return original(method, url, **kwargs)

        self.adapter._handle = inventory
        deployment.quiesce(paired_activation=True)
        with transaction.atomic(), self.assertRaisesRegex(ProbeBlocked, "captured|selection|preparation|provenance"):
            check_active_streams()
        self.assertTrue(deployment.is_quiesced())

    def test_quiesced_probe_start_registers_before_remote_admission(self):
        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        attempt_id = uuid4()
        observed = []
        from .test_apply_selector import _promoted

        def admission(selected):
            control = NSOIntentDeploymentControl.objects.get(pk=1)
            observed.append(
                (control.verification_attempt_id, NSOApplyAttempt.objects.get(pk=attempt_id).overlay_snapshots.count())
            )
            response = _promoted(selected)
            response["generations"] = response["generations"][:1]
            return 202, response

        self.adapter.apply_response = admission
        puts = len(self.adapter.capture_reads)
        self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        self.assertEqual(observed, [(attempt_id, 3)])
        self.assertEqual(len(self.adapter.capture_reads), puts)
        self.assertEqual(len(self.adapter.apply_requests), 1)
        self.assertTrue(deployment.is_quiesced())
        self.attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        self.assertEqual(self.notify().status_code, 503)
        self.assertEqual(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id, attempt_id)
        self._serve_bgp_observation()
        self.serve_terminal_evidence(self.terminal(), manual=True)
        self.command(register_probe=str(attempt_id))
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        self.assertEqual(NSOApplyExecution.objects.get(attempt_id=attempt_id).consumption_state, "consumed")
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)

    def paired_probes(self):

        from ._adapter_http import make_apply_preview, make_response
        from .test_apply_selector import _promoted

        NSOApplyAttempt.objects.filter(pk=self.prior_attempt.pk).update(
            lifecycle="terminal",
            completed_at=self.stamp,
            http_status=409,
            response={"error": {"code": "apply_unexecutable", "message": "Placeholder prior rejection"}},
        )
        self.probe(refused=None, settle_prior=True)
        attempts = [self.attempt.pk]
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        for index, refused in ((1, 1), (2, 0)):
            self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")

            def admission(selected):
                response = _promoted(selected)
                response["generations"] = response["generations"][:1]
                response["job_id"] += index
                response["generations"][0].update(generation_id=81 + index, seq=4 + index, job_id=501 + index)
                return 202, response

            self.adapter.apply_response = admission
            attempt_id = uuid4()
            self.command(start_probe=str(attempt_id), device_id=self.device.pk)
            self.attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
            generation = self.terminal(refused=refused)
            generation.update(generation_id=81 + index, seq=4 + index, carrier_job_id=501 + index)
            for outcome in generation["row_outcomes"]:
                outcome["generation_id"] = outcome["carrier"]["generation_id"] = 81 + index
                if refused == 0:
                    for cause in outcome["refusals"]:
                        cause["field"] = "bgp.router.router_id"
            for carrier in generation["carrier_job_result"]["settlement_owners"]:
                carrier["generation_id"] = 81 + index
            for cause in generation["refusals"]:
                if refused == 0:
                    cause["field"] = "bgp.router.router_id"
                for logical in cause["logical_rows"]:
                    logical["generation_id"] = logical["carrier"]["generation_id"] = 81 + index
            self._serve_bgp_observation(generation)
            self.serve_terminal_evidence(generation, manual=True, settle_seq=index + 2)
            self.command(register_probe=str(attempt_id))
            self.assertEqual(self.notify().status_code, 202)
            self.work()
            attempts.append(attempt_id)
        original = self.adapter._handle

        def preview(method, url, **kwargs):
            if url.endswith("/actions/apply-diff"):
                return make_response(200, make_apply_preview(self.management.adapter_device_id))
            if url.endswith("/intent-summary"):
                return make_response(
                    200,
                    {
                        "device_id": self.management.adapter_device_id,
                        "scopes": {"bgp_router_intent": {"count": 1, "applied": 0, "failed": 1}},
                        "pending_clear": {},
                    },
                )
            return original(method, url, **kwargs)

        self.adapter._handle = preview
        return attempts

    def test_full_paired_verification_resumes_after_callback_worker_settlement(self):
        attempts = self.paired_probes()
        output = self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertIn("normal intent operation resumed", output)
        self.assertFalse(deployment.is_quiesced())
        self.assertFalse(NSOIntentDeploymentControl.objects.filter(verification_attempt__isnull=False).exists())
        self.assertEqual(
            NSOApplyExecution.objects.filter(attempt_id__in=attempts, consumption_state="consumed").count(), 3
        )

    def test_an_empty_probe_value_never_runs_paired_verification(self):
        settled = [str(pk) for pk in self.paired_probes()]
        for action in ("start_probe", "register_probe"):
            with self.subTest(action=action), self.assertRaisesRegex(CommandError, "UUID"):
                self.command(**{action: ""}, device_id=self.device.pk, settled_probe=settled)
            self.assertTrue(deployment.is_quiesced())

    def test_a_malformed_settled_probe_is_a_named_command_error(self):
        settled = [str(pk) for pk in self.paired_probes()]
        args = ["--verify", "--device", str(self.device.pk)]
        for probe in [*settled, "not-a-uuid"]:
            args += ["--settled-probe", probe]
        with self.assertRaisesRegex(CommandError, "--settled-probe: invalid UUID value: 'not-a-uuid'"):
            call_command("nso_intent_deployment_gate", *args, stdout=StringIO(), stderr=StringIO())
        self.assertTrue(deployment.is_quiesced())

    def test_verify_rejects_terminal_remote_generation_without_consumed_execution(self):
        from ._adapter_http import make_response

        attempts = self.paired_probes()
        original = self.adapter._handle

        def gap(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                generations = response.json()
                historical = {**generations[0], "generation_id": 79, "seq": 2, "job_id": 499, "status": "failed"}
                return make_response(200, [historical, *generations])
            return response

        self.adapter._handle = gap
        with self.assertRaisesRegex(CommandError, "terminal.*consumed|historical.*evidence"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def test_verify_rejects_executed_abandoned_generation_after_job_deletion(self):
        attempts = self.paired_probes()
        historical = self.terminal()
        historical.update(generation_id=79, seq=2, carrier_job_id=499, status="abandoned")
        for outcome in historical["row_outcomes"]:
            outcome["generation_id"] = outcome["carrier"]["generation_id"] = 79
        for carrier in historical["carrier_job_result"]["settlement_owners"]:
            carrier["generation_id"] = 79
        for cause in historical["refusals"]:
            for logical in cause["logical_rows"]:
                logical["generation_id"] = logical["carrier"]["generation_id"] = 79
        self.serve_terminal_evidence(historical, head=False, pruned=True)
        self.assertIsNotNone(historical["carrier_job_result"])
        self.assertFalse(NSOApplyExecution.objects.filter(generation_id=79).exists())
        with self.assertRaisesRegex(CommandError, "terminal.*consumed|historical.*evidence"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def test_full_verification_accepts_settled_generation_after_job_deletion(self):
        from netbox_nso_plugin import adapter_client

        attempts = self.paired_probes()
        execution = NSOApplyExecution.objects.get(attempt_id=attempts[0])
        self.assertEqual(execution.terminal_projection["status"], "settled")
        self.assertGreater(execution.carrier_job_id, 0)
        self.serve_pruned_historical_carrier((execution, execution.terminal_projection))
        generations = adapter_client.list_device_generations(self.management.adapter_device_id)
        self.assertEqual(generations[0]["job_id"], None)
        output = self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertIn("normal intent operation resumed", output)
        self.assertFalse(deployment.is_quiesced())

    def test_retained_carriers_read_one_evidence_snapshot_per_operation(self):
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence
        from netbox_nso_plugin.deployment_checks import check_active_streams

        attempts = self.paired_probes()
        executions = list(NSOApplyExecution.objects.filter(attempt_id__in=attempts).order_by("generation_id"))
        self.assertEqual(len(executions), 3)
        self.serve_pruned_historical_carrier(*[(execution, execution.terminal_projection) for execution in executions])
        reads = self.count_snapshot_reads()
        with deployment.gate_bypass():
            load_deployment_evidence(self.management)
        self.assertEqual(reads, {"evidence": 2, "feed": 3})
        reads.update(evidence=0, feed=0)
        with transaction.atomic():
            check_active_streams()
        self.assertEqual(reads, {"evidence": 2, "feed": 3})

    def test_retained_carrier_evidence_reads_stay_within_the_adapter_attempt_limit(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.deployment_checks import check_active_streams

        attempts = self.paired_probes()
        executions = list(NSOApplyExecution.objects.filter(attempt_id__in=attempts).order_by("generation_id"))
        self.assertEqual(len({execution.attempt_id for execution in executions}), 3)
        self.serve_pruned_historical_carrier(*[(execution, execution.terminal_projection) for execution in executions])
        original = self.adapter._handle
        sizes = []

        def recorded(method, url, **kwargs):
            if method == "POST" and url.endswith("/deployment-evidence"):
                sizes.append(len(kwargs["json"]["apply_attempt_ids"]))
            return original(method, url, **kwargs)

        self.adapter._handle = recorded
        with patch.object(adapter_client, "DEPLOYMENT_EVIDENCE_ATTEMPT_LIMIT", 2), transaction.atomic():
            check_active_streams()
        self.assertEqual(sorted(size for size in sizes if size), [1, 2])

    def test_one_changed_retained_carrier_in_a_batch_is_missing_evidence(self):
        attempts = self.paired_probes()
        executions = list(NSOApplyExecution.objects.filter(attempt_id__in=attempts).order_by("generation_id"))
        changed = {**executions[1].terminal_projection, "carrier_job_error": {"message": "Placeholder changed"}}
        self.serve_pruned_historical_carrier(
            (executions[0], executions[0].terminal_projection),
            (executions[1], changed),
            (executions[2], executions[2].terminal_projection),
        )
        with self.assertRaisesRegex(CommandError, "missing_generation_evidence"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def count_snapshot_reads(self):
        original = self.adapter._handle
        reads = {"evidence": 0, "feed": 0}

        def counted(method, url, **kwargs):
            if method == "POST" and url.endswith("/deployment-evidence"):
                reads["evidence"] += 1
            if method == "GET" and url.endswith("/jobs"):
                reads["feed"] += 1
            return original(method, url, **kwargs)

        self.adapter._handle = counted
        return reads

    def serve_pruned_historical_carrier(self, *carriers):
        from ._adapter_http import make_response

        original = self.adapter._handle

        def pruned(method, url, **kwargs):
            response = original(method, url, **kwargs)
            document = response.json()
            if method == "GET" and url.endswith("/generations"):
                listed = {row["generation_id"]: row for row in document}
                for execution, retained in carriers:
                    listed[execution.generation_id] = {
                        **document[0],
                        **{
                            key: execution.terminal_projection[key]
                            for key in ("generation_id", "seq", "stream_revisions", "source_push_seq")
                        },
                        "status": retained["status"],
                        "job_id": None,
                    }
                return make_response(200, sorted(listed.values(), key=lambda row: row["seq"]))
            if method == "POST" and url.endswith("/deployment-evidence"):
                for execution, retained in carriers:
                    if str(execution.attempt_id) in kwargs["json"]["apply_attempt_ids"]:
                        document["attempts"].append(
                            {
                                "apply_attempt_id": str(execution.attempt_id),
                                "admission_state": "admitted",
                                "http_status": execution.attempt.http_status,
                                "response": execution.attempt.response,
                                "generations": [retained],
                            }
                        )
                return make_response(200, document)
            return response

        self.adapter._handle = pruned

    def test_verify_rejects_unseen_retry_after_job_deletion_and_newer_head(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence

        attempts = self.paired_probes()
        execution = NSOApplyExecution.objects.get(attempt_id=attempts[1])
        self.assertEqual(execution.terminal_projection["status"], "failed")
        retry = {**execution.terminal_projection, "carrier_job_id": 1502, "status": "abandoned"}
        self.serve_pruned_historical_carrier((execution, retry))
        generations = adapter_client.list_device_generations(self.management.adapter_device_id)
        self.assertIsNone(generations[0]["job_id"])
        self.assertGreater(generations[-1]["seq"], generations[0]["seq"])
        evidence = adapter_client.get_deployment_evidence(self.management.adapter_device_id, [execution.attempt_id])
        self.assertEqual(evidence["head"]["generation_id"], generations[-1]["generation_id"])
        self.assertEqual(evidence["attempts"][0]["generations"][0]["carrier_job_id"], 1502)
        jobs, _incarnation = adapter_client.get_settlement_feed(
            self.management.adapter_device_id, after_settle_seq=0, limit=100
        )
        self.assertNotIn(1502, [job["id"] for job in jobs])
        self.assertEqual(NSOApplyExecution.objects.filter(generation_id=execution.generation_id).count(), 1)
        self.management.refresh_from_db()
        cursor = self.management.settle_cursor_seq
        with self.assertRaisesRegex(CommandError, "missing_generation_evidence"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())
        with self.assertRaisesRegex(EvidenceInvariantError, "missing_generation_evidence"):
            load_deployment_evidence(self.management)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, cursor)
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id=1502, consumed_at__isnull=False).exists())

    def test_verify_rejects_pruned_generation_without_remote_carrier_identity(self):
        attempts = self.paired_probes()
        execution = NSOApplyExecution.objects.get(generation_id=80)
        self.assertNotEqual(execution.attempt.trigger_kind, "manual")
        self.serve_pruned_historical_carrier((execution, execution.terminal_projection))
        with self.assertRaisesRegex(CommandError, "missing_generation_evidence"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def test_verify_rejects_changed_terminal_identity_after_job_deletion(self):
        attempts = self.paired_probes()
        execution = NSOApplyExecution.objects.get(attempt_id=attempts[1])
        retained = {
            **execution.terminal_projection,
            "status": "abandoned",
            "carrier_job_error": {"message": "Placeholder changed error"},
        }
        self.serve_pruned_historical_carrier((execution, retained))
        with self.assertRaisesRegex(CommandError, "missing_generation_evidence"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def test_verify_rejects_ambiguous_pruned_generation_carriers(self):
        attempts = self.paired_probes()
        execution = NSOApplyExecution.objects.get(generation_id=80)
        self.serve_terminal_evidence(execution.terminal_projection, head=False, pruned=True)
        retry = NSOApplyExecution.objects.filter(pk=execution.pk).values().get()
        del retry["id"]
        retry["carrier_job_id"] += 1000
        retry["terminal_projection"] = {**retry["terminal_projection"], "carrier_job_id": retry["carrier_job_id"]}
        NSOApplyExecution.objects.create(**retry)
        with self.assertRaisesRegex(CommandError, "terminal.*consumed|historical.*evidence"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def assert_historical_probe_rejected(self, field):
        from datetime import timedelta

        attempts = self.paired_probes()
        control = NSOIntentDeploymentControl.objects.get(pk=1)
        old = control.quiesced_at - timedelta(seconds=1)
        execution = NSOApplyExecution.objects.get(attempt_id=attempts[0])
        self.assertGreater(execution.consumed_at, control.quiesced_at)
        if field == "capture":
            NSOApplyAttempt.objects.filter(pk=attempts[0]).update(created_at=old)
        else:
            projection = {**execution.terminal_projection, "updated_at": old.isoformat()}
            NSOApplyExecution.objects.filter(pk=execution.pk).update(terminal_at=old, terminal_projection=projection)
        with self.assertRaisesRegex(CommandError, "Historical|current deployment gate"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def test_verify_rejects_pre_gate_probe_capture(self):
        self.assert_historical_probe_rejected("capture")

    def test_verify_rejects_pre_gate_terminal_execution_consumed_after_quiescence(self):
        self.assert_historical_probe_rejected("terminal")

    def test_completed_verification_reports_resume_failure_and_keeps_the_gate(self):
        from django.db import DatabaseError

        attempts = self.paired_probes()
        failure = DatabaseError("connection lost")
        output, errors = StringIO(), StringIO()
        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.resume", side_effect=failure):
            with self.assertRaises(DatabaseError) as caught:
                call_command(
                    "nso_intent_deployment_gate",
                    verify=True,
                    device_id=self.device.pk,
                    settled_probe=[str(pk) for pk in attempts],
                    stdout=output,
                    stderr=errors,
                )
        self.assertIs(caught.exception, failure)
        self.assertIn("intent work may remain quiesced", errors.getvalue())
        self.assertIn(f"nso_intent_deployment_gate --verify --device {self.device.pk}", errors.getvalue())
        for attempt_id in attempts:
            self.assertIn(f"--settled-probe {attempt_id}", errors.getvalue())
        self.assertNotIn("normal intent operation resumed", output.getvalue())
        self.assertTrue(deployment.is_quiesced())

    def test_verification_resume_failure_survives_stderr_failure(self):
        from django.db import DatabaseError

        class BrokenStderr(StringIO):
            writes = 0

            def write(self, value):
                self.writes += 1
                raise OSError("stderr is unavailable")

        attempts = self.paired_probes()
        failure = DatabaseError("connection lost")
        errors = BrokenStderr()
        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.resume", side_effect=failure):
            with self.assertRaises(DatabaseError) as caught:
                call_command(
                    "nso_intent_deployment_gate",
                    verify=True,
                    device_id=self.device.pk,
                    settled_probe=[str(pk) for pk in attempts],
                    stdout=StringIO(),
                    stderr=errors,
                )
        self.assertIs(caught.exception, failure)
        self.assertGreater(errors.writes, 0)
        self.assertTrue(deployment.is_quiesced())

    def test_a_receipt_read_failure_is_a_named_command_failure(self):
        from ._adapter_http import make_response

        attempts = self.paired_probes()
        original = self.adapter._handle

        def failed(method, url, **kwargs):
            if url.endswith("/intent-receipts"):
                return make_response(503, {"detail": "Receipt read failed"})
            return original(method, url, **kwargs)

        self.adapter._handle = failed
        with self.assertRaisesRegex(CommandError, "Receipt read failed"):
            self.command(verify=True, device_id=self.device.pk, settled_probe=[str(pk) for pk in attempts])
        self.assertTrue(deployment.is_quiesced())

    def test_a_reprepare_after_a_failed_verification_keeps_the_gate(self):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        deployment.quiesce(paired_activation=True)
        with self.assertRaises(CommandError):
            self.command(verify=True, device_id=self.device.pk)
        NSOIntentOutboxState.objects.create(device=self.device, scope="static_route", revoked_ids=[11])
        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep"):
            with self.assertRaisesRegex(CommandError, "Deployment gate blocked.*revoked ids"):
                self.command(prepare=True)
        self.assertTrue(deployment.is_quiesced())

    def test_probe_read_identity_change_preserves_unselected_families(self):
        self.probe()
        from netbox_nso_plugin.models import NSOFamilyReadState

        from ._adapter_http import make_response
        from .test_read_gate import _INC_B

        NSOFamilyReadState.objects.create(
            management=self.management,
            family="logging",
            applied_incarnation=self.management.adapter_incarnation,
            applied_source_epoch=1,
            applied_payload_revision=7,
        )
        before = list(NSOFamilyReadState.objects.filter(family="logging").values())
        original = self.adapter._handle

        def reset_read(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if url.endswith("/bgp-config"):
                document = response.json()
                document["read_state"].update(incarnation=_INC_B[0], incarnation_born=_INC_B[1])
                return make_response(200, document)
            return response

        self.adapter._handle = reset_read
        self.assertEqual(self.notify().status_code, 202)
        self.work()
        self.assertEqual(list(NSOFamilyReadState.objects.filter(family="logging").values()), before)
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "unconsumed")
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertTrue(deployment.is_quiesced())

    def test_rejected_probe_is_terminal_and_cannot_repeat_restored_snapshots(self):
        from .test_apply_selector import _promoted

        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        self.adapter.apply_response = lambda selected: (
            409,
            {"error": {"code": "apply_unexecutable", "message": "Refused"}},
        )
        attempt_id = uuid4()
        with self.assertRaises(CommandError):
            self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        self.assertEqual(attempt.lifecycle, "terminal")
        self.assertEqual(attempt.http_status, 409)
        self.assertEqual(attempt.response["error"]["code"], "apply_unexecutable")
        self.assertIsNotNone(attempt.completed_at)
        self.assertEqual(
            set(attempt.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"not_applied"}
        )
        requests = len(self.adapter.apply_requests)
        with self.assertRaises(CommandError):
            self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        self.assertEqual(len(self.adapter.apply_requests), requests)

        def admitted(selected):
            response = _promoted(selected)
            response["generations"] = response["generations"][:1]
            return 202, response

        self.adapter.apply_response = admitted
        fresh_id = uuid4()
        self.command(start_probe=str(fresh_id), device_id=self.device.pk)
        self.assertEqual(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id, fresh_id)
        self.assertTrue(deployment.is_quiesced())

    def test_probe_retry_refuses_replacement_adapter_store_before_apply(self):
        from netbox_nso_plugin import adapter_client

        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        self.adapter.apply_response = lambda selected: (
            503,
            {"error": {"code": "unavailable", "message": "Unavailable"}},
        )
        attempt_id = uuid4()
        with self.assertRaises(CommandError):
            self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        self.assertIsNone(NSOApplyAttempt.objects.get(pk=attempt_id).response)
        connections.close_all()
        requests = len(self.adapter.apply_requests)
        handler = self.server.RequestHandlerClass
        self._stop_adapter()
        self.socket_adapter = FakeAdapter()
        self.server, self.thread = self.socket_adapter._server, self.socket_adapter._thread
        self.server.RequestHandlerClass = handler
        self.server.adapter, self.server.management_id = self.adapter, self.management.pk
        self.server.adapter_url = self.socket_adapter.url
        adapter_client.reset_session()
        with (
            patch(
                "netbox_nso_plugin.adapter_client._resolve_config",
                return_value={
                    "url": self.socket_adapter.url,
                    "token": "placeholder",
                    "verify_tls": True,
                    "ca_cert_path": None,
                    "timeout": 10,
                },
            ),
            self.assertRaisesRegex(CommandError, "store|identity|source"),
        ):
            self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        self.assertEqual(len(self.adapter.apply_requests), requests)
        self.assertTrue(deployment.is_quiesced())

    def test_probe_restart_recovers_captured_unsent_snapshots(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.durable_capture import capture_manual
        from netbox_nso_plugin.models import NSOAcceptedSelection, NSOIntentOutboxState

        from .test_apply_selector import _promoted

        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        selection = NSOAcceptedSelection.objects.filter(management=self.management, stream="bgp").latest("pk")
        state = NSOIntentOutboxState.objects.get(device=self.device, scope="bgp")
        pushed = {
            "bgp": drain.SuccessfulPush(
                selection.source_push_seq, state.last_success_identity, selection.plugin_revision
            )
        }
        attempt_id = uuid4()
        with deployment.gate_bypass(), transaction.atomic():
            attempt = NSOApplyAttempt.objects.create(
                pk=attempt_id,
                management=self.management,
                adapter_device_id=self.management.adapter_device_id,
                source_epoch=self.management.adapter_source_epoch,
                scope_revisions={"bgp": selection.plugin_revision},
                selected={"bgp": selection.source_push_seq},
            )
            capture_manual(attempt, self.management, {"bgp": delivery.delivery_keys()["bgp"]}, pushed, {}, {}, False)
        self.assertIsNone(attempt.adapter_apply_attempt_id)
        connections.close_all()

        def admission(selected):
            response = _promoted(selected)
            response["generations"] = response["generations"][:1]
            return 202, response

        self.adapter.apply_response = admission
        self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.adapter_apply_attempt_id, attempt.pk)
        self.assertEqual(attempt.response["outcome"], "promoted")
        self.assertEqual(len(self.adapter.apply_requests), 1)
        self.assertEqual(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id, attempt.pk)
        self.assertTrue(deployment.is_quiesced())

    def test_malformed_no_op_probe_keeps_unresolved_snapshots(self):
        from .test_apply_selector import _promoted

        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")

        def contradictory(selected):
            response = _promoted(selected)
            response["generations"] = response["generations"][:1]
            response["outcome"] = "no_op"
            return 200, response

        self.adapter.apply_response = contradictory
        attempt_id = uuid4()
        with self.assertRaises(CommandError):
            self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        self.assertEqual(attempt.lifecycle, "open")
        self.assertIsNone(attempt.response)
        self.assertEqual(
            set(attempt.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"unresolved"}
        )
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertTrue(deployment.is_quiesced())

    def test_valid_no_op_probe_closes_without_replaying_apply(self):
        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")

        def skipped(selected):
            return 200, {
                "device_id": self.management.adapter_device_id,
                "outcome": "no_op",
                "selected": selected,
                "skipped": {"bgp": "already_applied"},
                "generations": [],
            }

        self.adapter.apply_response = skipped
        attempt_id = uuid4()
        with self.assertRaisesRegex(CommandError, "did not execute"):
            self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        self.assertEqual(attempt.lifecycle, "terminal")
        self.assertEqual(attempt.http_status, 200)
        self.assertEqual(attempt.response["outcome"], "no_op")
        self.assertEqual(
            set(attempt.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"not_applied"}
        )
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertTrue(deployment.is_quiesced())

    def test_invalid_probe_registration_revokes_existing_permit(self):
        self.probe()
        generation = self.terminal()
        generation["carrier_job_id"] = 502
        self.serve_terminal_evidence(generation, manual=True)
        with self.assertRaisesRegex(CommandError, "carrier|admi|generation"):
            self.command(register_probe=str(self.attempt.pk))
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "unconsumed")
        self.assertEqual(
            set(self.attempt.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"unresolved"}
        )
        self.assertTrue(deployment.is_quiesced())

    def test_changed_probe_owner_footprint_returns_a_blocked_result(self):
        self.probe()
        from netbox_nso_plugin import bgp_reconciler

        plan = bgp_reconciler.bgp_reconcile_plan

        def plan_then_change(device, payload, **kwargs):
            planned = plan(device, payload, **kwargs)
            payload["routers"][0]["asn"] = "64599"
            return planned

        self.assertEqual(self.notify().status_code, 202)
        with patch.object(bgp_reconciler, "bgp_reconcile_plan", plan_then_change):
            self.work()
        (job_id,) = self.queue.finished_job_registry.get_job_ids()
        result = self.queue.fetch_job(job_id).return_value()
        self.assertEqual(result["blocked"], "BGP probe owner footprint changed")
        self.assertEqual(NSOApplyExecution.objects.get(attempt=self.attempt).consumption_state, "unconsumed")
        self.assertIsNone(NSOIntentDeploymentControl.objects.get(pk=1).verification_attempt_id)
        self.assertTrue(deployment.is_quiesced())
