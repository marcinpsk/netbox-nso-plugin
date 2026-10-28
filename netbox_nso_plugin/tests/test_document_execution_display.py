# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Document causes and invalid feed evidence remain visible without local owners."""

import copy

from django.urls import reverse

from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.models import NSOApplyExecution
from netbox_nso_plugin.refusal_display import device_execution_summaries, document_projection

from ._bgp_settlement import BGPSettlementHelpers
from .test_durable_bgp_capture import BGPCaptureCase


class TestDocumentExecutionDisplay(BGPSettlementHelpers, BGPCaptureCase):
    def serve_preceding_sync_jobs(self, generation, *, full_page=False):
        from ._adapter_http import make_response

        self.serve_terminal_evidence(generation, settle_seq=42)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                params = {**kwargs["params"], "after_settle_seq": 0}
                job = original(method, url, **{**kwargs, "params": params}).json()[0]
                sequences = range(1, 101) if full_page else [41]
                preceding = [
                    {
                        **job,
                        "id": 300 + sequence if full_page else 500,
                        "type": "sync",
                        "result": None,
                        "row_outcomes": None,
                        "refusals": None,
                        "settle_seq": sequence,
                    }
                    for sequence in sequences
                ]
                if after == 0:
                    return make_response(200, [*preceding, job][:100])
                if full_page and after == 100:
                    return make_response(200, [job])
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def ownerless(self, *, provenance="prior", phase="pre_commit_dry_run"):
        self.prepare()
        generation = self.terminal()
        result = generation["carrier_job_result"]
        result["refusals"][0].update(provenance=provenance, sender_phase=phase)
        result["commit_acknowledged"] = phase == "post_commit_verification"
        result["row_outcomes"] = generation["row_outcomes"] = []
        result["settlement_owners"] = []
        result["bgp_count_by_outcome"] = dict.fromkeys(result["bgp_count_by_outcome"], 0)
        execution = NSOApplyExecution.objects.create(
            attempt=self.attempt,
            management=self.management,
            mapping_epoch=mapping_epoch(self.management),
            adapter_device_id=self.management.adapter_device_id,
            store_incarnation="placeholder-store",
            generation_id=81,
            generation_seq=4,
            carrier_job_id=501,
            terminal_projection=generation,
            terminal_at=self.stamp,
            consumed_at=self.stamp,
            consumption_state="consumed",
        )
        self.assertFalse(execution.overlay_snapshots.exists())
        return result

    def test_ownerless_prior_cause_appears_on_device_tab(self):
        result = self.ownerless()
        cause = result["refusals"][0]
        expected = (
            f"Blocked by {cause['family']} {cause['row']['subject']} {cause['row']['key']} "
            f"(not part of this Apply): {cause['message']}"
        )
        summaries = device_execution_summaries(self.management)
        self.assertEqual(len(summaries), 1)
        self.assertIn(expected, summaries[0]["summary"])
        self.assertIn("commit not attempted", summaries[0]["summary"])
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, expected)

    def test_ownerless_current_cause_uses_foreign_text(self):
        result = self.ownerless(provenance="current", phase="commit")
        projected = document_projection(result)
        self.assertIn("nothing committed", projected["summary"])
        self.assertIn("(not part of this Apply)", projected["summary"])

    def test_dry_run_refusal_says_commit_not_attempted(self):
        self.prepare()
        result = self.terminal()["carrier_job_result"]
        self.assertIn("commit not attempted", document_projection(result)["summary"])

    def test_post_commit_cause_does_not_claim_rollback(self):
        result = self.ownerless(phase="post_commit_verification")
        projected = document_projection(result)
        self.assertIn("Execution evidence", projected["summary"])
        self.assertNotIn("nothing committed", projected["summary"])
        self.assertNotIn("commit not attempted", projected["summary"])

    def test_prior_cause_at_another_fence_does_not_name_an_attempted_row(self):
        self.prepare()
        cause = copy.deepcopy(self.terminal()["refusals"][0])
        cause["provenance"] = "prior"
        cause["logical_rows"][0]["frozen_revision"] += 1
        result = self.terminal(refused=None, disposition="not_applied")["carrier_job_result"]
        result.update(refusals=[cause], commit_acknowledged=False)
        projected = document_projection(result)
        self.assertIn("(not part of this Apply)", projected["summary"])
        self.assertIn("commit not attempted", projected["summary"])

    def test_invalid_feed_stall_is_durable_and_visible(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.prepare()
        generation = copy.deepcopy(self.terminal())
        generation["row_outcomes"][0]["disposition"] = "placeholder-secret-canary"
        self.serve_terminal_evidence(generation, settle_seq=42)
        stale = type(self.management).objects.get(pk=self.management.pk)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.management.refresh_from_db()
        self.assertEqual(
            {key: self.management.settle_evidence_error[key] for key in ("job_id", "settle_seq", "reason")},
            {"job_id": 501, "settle_seq": 42, "reason": "invalid disposition"},
        )
        self.assertIsNone(self.management.settle_cursor_seq)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused")
        self.assertContains(response, "Adapter job 501")
        self.assertContains(response, "settle_seq 42")
        self.assertContains(response, "invalid disposition")
        self.assertNotContains(response, "placeholder-secret-canary")
        stale.save()
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["job_id"], 501)

    def test_repaired_feed_clears_invalid_evidence_banner(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.prepare()
        generation = self.terminal()
        broken = copy.deepcopy(generation)
        broken["row_outcomes"][0]["disposition"] = "placeholder-secret-canary"
        self.serve_terminal_evidence(broken, settle_seq=42)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        self.serve_terminal_evidence(generation, settle_seq=42)
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertEqual(self.management.settle_cursor_seq, 42)

    def test_retained_cause_outside_frozen_contribution_stalls_with_device_banner(self):
        from netbox_nso_plugin.refusal_contract import validate_generation
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.prepare()
        generation = self.terminal()
        cause = generation["refusals"][0]
        cause["provenance"] = "retained"
        cause["row"]["key"] = "f" * 64
        for outcome in generation["row_outcomes"]:
            for linked in outcome["refusals"]:
                linked["row"]["key"] = "f" * 64
        validate_generation(generation)
        self.serve_preceding_sync_jobs(generation)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        execution = NSOApplyExecution.objects.get(management=self.management, carrier_job_id=501)
        self.assertEqual(execution.invariant_code, "invalid_terminal_batch")
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertNotContains(response, "Adapter job 500")
        self.assertContains(response, "settle_seq 42")
        self.assertContains(response, "invalid settlement evidence")
        self.assertEqual(self.management.settle_evidence_error["reason"], "invalid settlement evidence")
        self.serve_terminal_evidence(self.terminal(), settle_seq=42)
        self.assertFalse(consume_static_route_settlements(self.management).stalled)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertNotContains(response, "Settlement is paused")

    def test_invalid_order_on_second_feed_page_names_the_failing_job(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.prepare()
        self.serve_preceding_sync_jobs(self.terminal(), full_page=True)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertNotContains(response, "Adapter job 301")
        self.assertContains(response, "settle_seq 42")
        self.assertEqual(self.management.settle_evidence_error["job_id"], 501)
        self.assertEqual(self.management.settle_evidence_error["settle_seq"], 42)

    def test_invalid_evidence_banner_does_not_follow_a_mapping_change(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.prepare()
        generation = self.terminal()
        generation["row_outcomes"][0]["disposition"] = "placeholder-secret-canary"
        self.serve_terminal_evidence(generation, settle_seq=42)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(adapter_device_id=1559)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertNotContains(response, "Settlement is paused")
