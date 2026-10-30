# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Document causes and invalid feed evidence remain visible without local owners."""

import copy
from unittest.mock import patch

from django.urls import reverse

from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.models import NSOApplyExecution
from netbox_nso_plugin.refusal_display import device_execution_summaries, document_projection

from ._bgp_settlement import BGPSettlementHelpers
from .test_durable_bgp_capture import BGPCaptureCase


class TestDocumentExecutionDisplay(BGPSettlementHelpers, BGPCaptureCase):
    def serve_preceding_sync_jobs(self, generation, *, full_page=False, manual=False):
        from ._adapter_http import make_response

        self.serve_terminal_evidence(generation, settle_seq=42, manual=manual)
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
            store_incarnation="00000000-0000-4000-8000-000000000004",
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

    def test_mixed_cause_names_each_row_outside_this_apply(self):
        self.prepare()
        result = self.terminal()["carrier_job_result"]
        cause = result["refusals"][0]
        foreign = copy.deepcopy(cause["logical_rows"][0])
        foreign["row"]["key"] = "f" * 64
        cause["logical_rows"].append(foreign)
        projected = document_projection(result)
        self.assertIn(f"{cause['reason']}: {cause['message']}", projected["summary"])
        self.assertIn(
            f"Blocked by {cause['family']} {foreign['row']['subject']} {'f' * 64} "
            f"(not part of this Apply): {cause['message']}",
            projected["summary"],
        )

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
            {"job_id": 501, "settle_seq": 42, "reason": "invalid job envelope"},
        )
        self.assertIsNone(self.management.settle_cursor_seq)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused")
        self.assertContains(response, "Adapter job 501")
        self.assertContains(response, "settle_seq 42")
        self.assertContains(response, "invalid job envelope")
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

    def test_repaired_head_clears_a_carrier_only_banner_after_consumption(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        from ._adapter_http import make_response

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), settle_seq=42)
        self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 42)
        original = self.adapter._handle
        broken = True

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if broken and method == "POST" and url.endswith("/deployment-evidence"):
                evidence = response.json()
                evidence["head"]["source_push_seq"] = {}
                return make_response(200, evidence)
            return response

        self.adapter._handle = handle
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertNotContains(response, "settle_seq")
        broken = False
        recovered = consume_static_route_settlements(self.management)
        self.assertTrue(recovered.drained)
        self.assertFalse(recovered.stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertNotContains(response, "Settlement is paused")
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

    def serve_carrier_status_mismatch(self):
        from ._adapter_http import make_response

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), settle_seq=42)
        original = self.adapter._handle
        self.settlement_pages = []

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                params = kwargs["params"]
                self.settlement_pages.append((int(params["after_settle_seq"]), int(params["limit"])))
                response = original(method, url, **{**kwargs, "params": {**params, "after_settle_seq": 0}})
                carrier = {**response.json()[0], "status": "failed"}
                sync = {
                    **carrier,
                    "id": 500,
                    "type": "sync",
                    "status": "succeeded",
                    "settle_seq": 41,
                    "result": None,
                    "row_outcomes": None,
                    "refusals": None,
                }
                jobs = [job for job in (sync, carrier) if job["settle_seq"] > int(params["after_settle_seq"])]
                jobs = jobs[: int(params["limit"])]
                return make_response(200, jobs)
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def test_carrier_status_mismatch_names_the_failing_job(self):
        self.serve_carrier_status_mismatch()
        self.assert_carrier_stall_banner()
        self.assertIn((0, 100), self.settlement_pages)

    def test_carrier_status_mismatch_on_a_later_page_names_the_failing_job(self):
        self.serve_carrier_status_mismatch()
        with patch("netbox_nso_plugin.settlement.SETTLE_FEED_PAGE", 1):
            self.assert_carrier_stall_banner()
        self.assertIn((0, 1), self.settlement_pages)
        self.assertIn((41, 1), self.settlement_pages)

    def assert_carrier_stall_banner(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertNotContains(response, "Adapter job 500")
        self.assertContains(response, "settle_seq 42")
        self.assertContains(response, "invalid settlement evidence")
        self.assertNotContains(response, "placeholder-secret-canary")
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        self.assertEqual(self.management.settle_evidence_error["job_id"], 501)
        self.assertEqual(self.management.settle_evidence_error["settle_seq"], 42)

    def test_invalid_store_marker_stall_shows_a_reason_without_a_job(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), settle_seq=42)
        original = self.adapter._handle
        feed_requests = 0

        def handle(method, url, **kwargs):
            nonlocal feed_requests
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs"):
                feed_requests += 1
                if feed_requests > 1:
                    self.server.evidence_incarnation = "placeholder-invalid-incarnation"
            return response

        self.adapter._handle = handle
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Settlement is paused: invalid settlement evidence.", html=True)
        self.assertNotContains(response, "Adapter job")
        self.assertNotContains(response, "settle_seq")
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        self.assertIsNone(self.management.settle_evidence_error.get("job_id"))
        self.assertIsNone(self.management.settle_evidence_error.get("settle_seq"))

    def test_store_change_during_the_evidence_read_is_not_shown_as_invalid_evidence(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), settle_seq=42, epoch_change=True)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.epoch_reset)
        self.assertTrue(result.stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertNotContains(response, "Settlement is paused")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertEqual(self.management.settle_cursor_incarnation, "00000000-0000-4000-8000-000000000003")

    def test_write_phase_stall_keeps_the_failing_carrier_through_batch_rejection(self):
        self.prepare()
        prior = self.terminal(refused=None)
        prior["carrier_job_id"] = 499
        self.settle(prior)
        NSOApplyExecution.objects.filter(management=self.management, carrier_job_id=499).update(settle_seq=None)
        self.serve_preceding_sync_jobs(self.terminal())
        self.assert_carrier_stall_banner()
        execution = NSOApplyExecution.objects.get(management=self.management, carrier_job_id=501)
        self.assertEqual(execution.invariant_code, "execution_order_unproven")

    def test_invalid_manual_generation_names_its_carrier(self):
        from ._adapter_http import make_response

        self.prepare()
        self.serve_preceding_sync_jobs(self.terminal(), manual=True)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                evidence = response.json()
                evidence["attempts"][0]["generations"][0]["row_outcomes"][0]["disposition"] = (
                    "placeholder-secret-canary"
                )
                return make_response(200, evidence)
            return response

        self.adapter._handle = handle
        self.assert_carrier_stall_banner()

    def serve_invalid_receipt_generation(self, *, pruned=False):
        from ._adapter_http import make_response

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), settle_seq=42, manual=True, pruned=pruned)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                evidence = response.json()
                evidence["attempts"][0]["generations"][0]["source_push_seq"] = {}
                return make_response(200, evidence)
            return response

        self.adapter._handle = handle

    def test_invalid_receipt_generation_names_its_carrier(self):
        self.serve_invalid_receipt_generation()
        self.assert_carrier_stall_banner()

    def test_invalid_receipt_generation_keeps_a_carrier_without_feed_order(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        self.serve_invalid_receipt_generation(pruned=True)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertNotContains(response, "settle_seq")
        self.assertContains(response, "invalid settlement evidence")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["job_id"], 501)
        self.assertIsNone(self.management.settle_evidence_error.get("settle_seq"))

    def test_generation_admission_mismatch_names_its_carrier(self):
        from ._adapter_http import make_response

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), settle_seq=42, manual=True)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                generations = response.json()
                generations[0]["digest"] = "b" * 64
                return make_response(200, generations)
            return response

        self.adapter._handle = handle
        self.assert_carrier_stall_banner()

    def test_noncanonical_store_incarnations_do_not_create_execution_evidence(self):
        from django.db import transaction

        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation, validate_terminal_generation

        self.prepare()
        generation = self.terminal()
        for incarnation in (
            "placeholder-invalid",
            "x" * 65,
            "00000000000040008000000000000001",
            "{00000000-0000-4000-8000-000000000001}",
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        ):
            for consume in (validate_terminal_generation, settle_terminal_generation):
                with self.subTest(incarnation=incarnation, consume=consume.__name__), transaction.atomic():
                    kwargs = {"settle_seq": 1} if consume is settle_terminal_generation else {}
                    with self.assertRaises(EvidenceInvariantError):
                        consume(self.management, generation, incarnation, attempt=self.attempt, **kwargs)
                    self.assertFalse(NSOApplyExecution.objects.exists())
            with self.subTest(incarnation=incarnation, consume="settle_apply_attempts"), transaction.atomic():
                with self.assertRaisesRegex(EvidenceInvariantError, "missing_store_incarnation"):
                    settle_apply_attempts(
                        self.management,
                        {"device_id": self.management.adapter_device_id},
                        store_incarnation=incarnation,
                    )
                self.assertFalse(NSOApplyExecution.objects.exists())

    def test_generation_validation_keeps_its_carrier_until_order_is_known(self):
        from netbox_nso_plugin.apply_settlement import (
            EvidenceInvariantError,
            _carrier_job,
            _validate_generation,
            identify_evidence_stall,
        )
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation, validate_terminal_generation
        from netbox_nso_plugin.selection_receipts import _source_pairs

        self.prepare()
        generation = self.terminal()
        generation["source_push_seq"] = {}
        validators = (
            _validate_generation,
            _source_pairs,
            lambda row: validate_terminal_generation(self.management, row, "00000000-0000-4000-8000-000000000004"),
            lambda row: settle_terminal_generation(self.management, row, "00000000-0000-4000-8000-000000000004"),
        )
        for validate in validators:
            with self.subTest(validate=validate, ordered=False):
                with self.assertRaises(EvidenceInvariantError) as caught:
                    validate(generation)
                error = getattr(caught.exception, "invalid_job", None)
                self.assertIsNotNone(error)
                self.assertEqual(error.job_id, 501)
                self.assertIsNone(error.settle_seq)
            with self.subTest(validate=validate, ordered=True):
                job = _carrier_job(self.management.adapter_device_id, 501, {501: 42})
                preceding = {**job, "id": 500, "settle_seq": 41}
                with self.assertRaises(EvidenceInvariantError) as caught:
                    with identify_evidence_stall(job, "00000000-0000-4000-8000-000000000004"):
                        with identify_evidence_stall(preceding, "00000000-0000-4000-8000-000000000004"):
                            validate(generation)
                error = caught.exception.invalid_job
                self.assertEqual(error.job_id, 501)
                self.assertEqual(error.settle_seq, 42)
                self.assertEqual(error.device_id, self.management.adapter_device_id)
                self.assertEqual(error.store_incarnation, "00000000-0000-4000-8000-000000000004")
                with self.assertRaises(EvidenceInvariantError) as caught:
                    with identify_evidence_stall(job, "00000000-0000-4000-8000-000000000004"):
                        validate({**generation, "carrier_job_id": None})
                self.assertEqual(caught.exception.invalid_job.job_id, 501)
                self.assertEqual(caught.exception.invalid_job.settle_seq, 42)

    def test_invalid_head_admission_names_its_carrier(self):
        from ._adapter_http import make_response

        self.prepare()
        self.serve_preceding_sync_jobs(self.terminal())
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                evidence = response.json()
                evidence["head"]["apply_attempt_id"] = "placeholder-secret-canary"
                return make_response(200, evidence)
            return response

        self.adapter._handle = handle
        self.assert_carrier_stall_banner()

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
