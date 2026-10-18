# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Recover a lost automatic publication response from its retained exact receipt."""

from netbox_nso_plugin.models import NSOApplyExecution

from . import test_durable_bgp_capture as bgp_case
from . import test_refusal_settlement_runtime as runtime_case


class TestLostPublicationReceipt(bgp_case.TestDurableBGPCapture):
    def test_corrected_automatic_receipt_retains_the_original_manual_attempt(self):
        import copy

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts
        from netbox_nso_plugin.models import NSOApplyAttempt, NSOIntentOutboxState

        from ._adapter_http import make_response
        from ._outbox_case import own_vlan
        from .test_refusal_settlement_families import TestRefusalSettlementFamilies

        runtime_case.TestRefusalSettlementRuntime.prepare(self)
        manual = self.attempt
        bgp = runtime_case.TestRefusalSettlementRuntime.terminal(self)
        vlan = own_vlan(self.management, 79, "lost-receipt-manual-sibling")
        self.adapter.drop_response_once = True
        drain.drain_intent_outbox()
        self.attempt = NSOApplyAttempt.objects.get(
            trigger_kind="push",
            overlay_snapshots__model_label=vlan._meta.label_lower,
            overlay_snapshots__overlay_pk=vlan.pk,
        )
        selection = self.attempt.overlay_snapshots.get().selection
        self.assertIsNone(selection.acknowledged_at)
        automatic = TestRefusalSettlementFamilies.evidence(self, dispositions={"vlan.vlan": "in_sync"})
        automatic.update(generation_id=82, seq=5, carrier_job_id=502)
        for identity in automatic["row_outcomes"] + automatic["carrier_job_result"]["settlement_owners"]:
            identity["generation_id"] = 82
            if "carrier" in identity:
                identity["carrier"]["generation_id"] = 82
        self.attempt = manual
        runtime_case.TestRefusalSettlementRuntime.serve_terminal_evidence(self, bgp, manual=True)
        original = self.adapter._handle
        invalid_receipt = True

        def mixed_evidence(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method != "GET":
                return response
            if url.endswith("/jobs"):
                jobs = response.json()
                jobs.append(
                    {
                        **jobs[0],
                        "id": automatic["carrier_job_id"],
                        "result": automatic["carrier_job_result"],
                        "row_outcomes": automatic["row_outcomes"],
                        "refusals": automatic["refusals"],
                        "updated_at": automatic["updated_at"],
                        "settle_seq": 2,
                    }
                )
                return make_response(200, jobs)
            if url.endswith("/generations"):
                generations = response.json()
                generations.append(
                    {
                        **generations[0],
                        **{
                            key: automatic[key]
                            for key in (
                                "generation_id",
                                "seq",
                                "status",
                                "stream_revisions",
                                "source_push_seq",
                                "updated_at",
                            )
                        },
                        "job_id": automatic["carrier_job_id"],
                    }
                )
                return make_response(200, generations)
            if url.endswith("/intent-receipts") and invalid_receipt:
                document = response.json()
                document["receipts"][0]["request_digest"] = "0" * 64
                return make_response(200, document)
            return response

        self.adapter._handle = mixed_evidence
        owners = [*self.owners, vlan]
        for row in owners:
            row.refresh_from_db()
        before = [
            (row.status, row.last_apply_at, copy.deepcopy(row.last_apply_error), row.apply_attempt_id) for row in owners
        ]
        puts = len(self.adapter.requests)
        with self.assertRaisesRegex(EvidenceInvariantError, "invalid_selection_receipt"):
            settle_device_apply_attempts(self.management)
        for row, prior in zip(owners, before, strict=True):
            row.refresh_from_db()
            self.assertEqual((row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id), prior)
        self.assertEqual(set(NSOApplyExecution.objects.values_list("consumption_state", flat=True)), {"unconsumed"})
        self.assertFalse(NSOApplyExecution.objects.filter(terminal_projection__isnull=False).exists())
        self.assertFalse(manual.overlay_snapshots.exclude(settlement_disposition="unresolved").exists())

        invalid_receipt = False
        settle_device_apply_attempts(self.management)
        execution = NSOApplyExecution.objects.get(generation_id=81)
        self.assertEqual(execution.attempt_id, manual.pk)
        self.assertEqual(execution.consumption_state, "consumed")
        self.assertEqual(execution.overlay_snapshots.count(), len(self.owners))
        self.assertFalse(manual.overlay_snapshots.filter(settlement_disposition="unresolved").exists())
        for index, row in enumerate(self.owners):
            row.refresh_from_db()
            if index == 1:
                self.assertEqual(row.status, "apply_failed")
                self.assertEqual(row.last_apply_at, self.stamp)
            else:
                self.assertEqual(
                    (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                    self.prior[(row._meta.label_lower, row.pk)],
                )
        vlan.refresh_from_db()
        selection.refresh_from_db()
        self.assertEqual(vlan.status, "in_sync")
        self.assertIsNotNone(selection.acknowledged_at)
        self.assertEqual(NSOApplyExecution.objects.get(generation_id=82).consumption_state, "consumed")
        self.assertIsNone(NSOIntentOutboxState.objects.get(device=self.device, scope="vlan").push_seq)
        settle_device_apply_attempts(self.management)
        drain.drain_intent_outbox()
        self.assertEqual(len(self.adapter.requests), puts)
        self.assertEqual(len(self.adapter.apply_requests), 1)
        self.assertEqual(NSOApplyExecution.objects.count(), 2)

    def capture_lost_publication(self, *, epoch_change=False):
        from ._adapter_http import make_response

        self.adapter.drop_response_once = True
        runtime_case.TestRefusalSettlementRuntime.prepare_automatic(self)
        self.selection = self.attempt.overlay_snapshots.first().selection
        self.assertIsNone(self.selection.acknowledged_at)
        self.assertEqual(len(self.adapter.capture_reads), 1)
        generation = runtime_case.TestRefusalSettlementRuntime.terminal(self)
        runtime_case.TestRefusalSettlementRuntime.serve_terminal_evidence(self, generation, epoch_change=epoch_change)
        original = self.adapter._handle
        stored = next(receipt for url, receipt in self.adapter.receipts.items() if url.endswith("/bgp-intent"))
        self.receipt = {
            "device_id": self.management.adapter_device_id,
            "section": "bgp",
            "push_seq": stored["push_seq"],
            "request_digest": stored["digest"],
            "store_only": stored["params"].get("store_only") == "true",
            "delete_origin": stored["params"].get("delete_origin") == "true",
            "backfill_only": stored["params"].get("backfill_only") == "true",
            "status_code": 200,
            "response": stored["response"],
            "generation_id": 81,
            "created_at": self.stamp.isoformat(),
            "updated_at": self.stamp.isoformat(),
        }
        self.receipt_reads = 0

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/intent-receipts"):
                self.receipt_reads += 1
                return make_response(
                    200,
                    {
                        "receipts": [self.receipt],
                        "global_max_push_seq": stored["push_seq"],
                        "global_max_route_id": None,
                    },
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def test_lost_automatic_response_recovers_without_another_put(self):
        from datetime import timedelta
        from unittest.mock import patch

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        self.capture_lost_publication()
        settle_device_apply_attempts(self.management)
        self.selection.refresh_from_db()
        self.assertIsNotNone(self.selection.acknowledged_at)
        self.assertEqual(self.selection.publication_state, "sent")
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")
        self.assertEqual(self.receipt_reads, 1)
        self.assertEqual(len(self.adapter.capture_reads), 1)
        self.assertEqual(self.adapter.apply_requests, [])
        with patch("netbox_nso_plugin.drain._db_now", autospec=True, return_value=self.stamp + timedelta(days=365)):
            drain.drain_intent_outbox()
        self.assertEqual(len(self.adapter.capture_reads), 1)

    def test_tampered_retained_claim_body_cannot_acknowledge_the_original_receipt(self):
        import copy

        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts
        from netbox_nso_plugin.models import NSOIntentOutboxState

        self.capture_lost_publication()
        state = NSOIntentOutboxState.objects.get(device=self.device, scope="bgp")
        payload = copy.deepcopy(state.claim_payload)
        payload[0]["router_id"] = "198.18.0.99"
        NSOIntentOutboxState.objects.filter(pk=state.pk).update(claim_payload=payload)
        with self.assertRaises(EvidenceInvariantError):
            settle_device_apply_attempts(self.management)
        self.selection.refresh_from_db()
        self.assertIsNone(self.selection.acknowledged_at)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())

    def test_receipt_digest_or_sequence_mismatch_keeps_the_selection_unacknowledged(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts

        self.capture_lost_publication()
        valid = dict(self.receipt)
        for change in ({"request_digest": "0" * 64}, {"push_seq": self.receipt["push_seq"] + 1}):
            with self.subTest(change=change):
                self.receipt = {**valid, **change}
                with self.assertRaises(EvidenceInvariantError):
                    settle_device_apply_attempts(self.management)
                self.selection.refresh_from_db()
                self.assertIsNone(self.selection.acknowledged_at)
                self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        self.assertEqual(len(self.adapter.capture_reads), 1)

    def test_epoch_change_never_acknowledges_a_recovered_receipt(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts

        self.capture_lost_publication(epoch_change=True)
        with self.assertRaisesRegex(EvidenceInvariantError, "evidence_epoch_changed"):
            settle_device_apply_attempts(self.management)
        self.selection.refresh_from_db()
        self.assertIsNone(self.selection.acknowledged_at)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())

    def test_invalid_sibling_owner_rolls_back_receipt_acknowledgment(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentOutboxState

        self.capture_lost_publication()
        sequence = self.receipt["push_seq"]
        type(self.owners[1]).objects.filter(pk=self.owners[1].pk).delete()
        with self.assertRaises(EvidenceInvariantError):
            settle_device_apply_attempts(self.management)
        self.selection.refresh_from_db()
        self.assertIsNone(self.selection.acknowledged_at)
        self.assertEqual(self.selection.publication_state, "recorded")
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        self.assertEqual(NSOIntentOutboxState.objects.get(device=self.device, scope="bgp").push_seq, sequence)
        self.assertTrue(
            NSOIntentOutboxEntry.objects.filter(device=self.device, scope="bgp", consumed_by_push_seq=sequence).exists()
        )

    def test_static_receipt_echo_certifies_success_without_another_put(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts
        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._outbox_case import partition, without_commit_drain
        from ._static_route_case import _own, _route
        from .test_refusal_settlement_families import TestRefusalSettlementFamilies

        with without_commit_drain():
            self.management.manage_static = True
            self.management.save(update_fields=("manage_static",))
            native = _route("198.18.79.0/24", "198.18.0.79", devices=[self.device])
            row = _own(native, self.management, status="accepted")
        fingerprint = "b" * 64
        self.adapter._respond = lambda body: {
            **partition(),
            "count": len(body.get("routes", [])),
            "routes": [{"route_id": native.pk, "generation": row.intent_generation, "fingerprint": fingerprint}],
        }
        original = self.adapter._handle

        def lose_static_response(method, url, **kwargs):
            if method == "PUT" and url.endswith("/static-route-intent"):
                self.adapter.drop_response_once = True
            return original(method, url, **kwargs)

        self.adapter._handle = lose_static_response
        drain.drain_intent_outbox()
        self.adapter._handle = original
        self.attempt = NSOApplyAttempt.objects.get(
            trigger_kind="push",
            overlay_snapshots__model_label=row._meta.label_lower,
            overlay_snapshots__overlay_pk=row.pk,
        )
        row.refresh_from_db()
        self.assertIsNone(row.expected_generation)
        generation = TestRefusalSettlementFamilies.evidence(self, dispositions={"static-route.route": "in_sync"})
        generation["carrier_job_result"]["static_route_results"] = [
            {
                "route_id": native.pk,
                "generation": row.intent_generation,
                "fingerprint": fingerprint,
                "outcome": "in_sync",
            }
        ]
        runtime_case.TestRefusalSettlementRuntime.serve_terminal_evidence(self, generation)
        settle_device_apply_attempts(self.management)
        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")
        self.assertEqual((row.expected_generation, row.expected_fingerprint), (row.intent_generation, fingerprint))
        drain.drain_intent_outbox()
        self.assertEqual(
            len([request for request in self.adapter.requests if request["url"].endswith("/static-route-intent")]), 1
        )

    def test_malformed_generation_sources_fail_as_safe_evidence_invariants(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts

        from ._adapter_http import make_response

        self.capture_lost_publication()
        original = self.adapter._handle
        invalid = object()
        missing = invalid

        def malformed(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                rows = response.json()
                if invalid is missing:
                    rows[0].pop("source_push_seq")
                else:
                    rows[0]["source_push_seq"] = invalid
                return make_response(200, rows)
            return response

        self.adapter._handle = malformed
        for invalid in (missing, None, [], {"bgp": True}):
            with self.subTest(source=repr(invalid)):
                with self.assertRaises(EvidenceInvariantError):
                    settle_device_apply_attempts(self.management)
                self.selection.refresh_from_db()
                self.assertIsNone(self.selection.acknowledged_at)
                self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
