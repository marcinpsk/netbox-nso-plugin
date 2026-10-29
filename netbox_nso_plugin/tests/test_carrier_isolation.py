# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep carrier evidence, ordering, and admission effects in their own transaction."""

import copy
from uuid import UUID

import requests
from django.urls import reverse

from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyExecution
from netbox_nso_plugin.refusal_display import settlement_stall_projection
from netbox_nso_plugin.settlement import consume_static_route_settlements

from ._adapter_http import make_evidence_envelope, make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import own_vlan
from ._settlement_wire import generation_wire, job_wire
from .test_apply_selector import _no_op
from .test_coalesced_settlement import CoalescedCarrierHelpers
from .test_durable_bgp_capture import BGPCaptureCase


class CarrierIsolationHelpers:
    def owner_bookkeeping(self):
        values = []
        for owner in self.owners:
            owner.refresh_from_db()
            values.append((owner.status, owner.last_apply_at, owner.last_apply_error, owner.apply_attempt_id))
        return values

    def job(self, generation, sequence):
        return {
            "id": generation["carrier_job_id"],
            "type": "apply",
            "device_id": self.management.adapter_device_id,
            "status": generation["carrier_job_status"],
            "settle_seq": sequence,
            "result": generation["carrier_job_result"],
            "error": generation["carrier_job_error"],
            "row_outcomes": generation["row_outcomes"],
            "refusals": generation["refusals"],
            "context": None,
            "created_at": self.stamp.isoformat(),
            "updated_at": generation["updated_at"],
            "started_at": self.stamp.isoformat(),
            "heartbeat_at": None,
        }

    def serve_retry(self, first, retry, *, attempt_generation=None):
        self.serve_terminal_evidence(retry, head=attempt_generation is None)
        original = self.adapter._handle
        jobs = [self.job(first, 1), self.job(retry, 2)]

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                limit = int(kwargs["params"]["limit"])
                return make_response(200, [job_wire(job) for job in jobs if job["settle_seq"] > after][:limit])
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence") and attempt_generation is not None:
                document = response.json()
                document["attempts"] = [
                    make_evidence_envelope(
                        {
                            "apply_attempt_id": str(self.attempt.pk),
                            "admission_state": "admitted",
                            "http_status": 202,
                            "response": self.attempt.response,
                            "selected": self.attempt.selected,
                            "generation_ids": [attempt_generation["generation_id"]],
                        },
                        identity={
                            "device_id": self.management.adapter_device_id,
                            "apply_attempt_id": str(self.attempt.pk),
                        },
                        generations=[generation_wire(attempt_generation, self.management.adapter_device_id)],
                    )
                ]
                return make_response(200, document)
            return response

        self.adapter._handle = handle


class TestCarrierIsolation(CarrierIsolationHelpers, CoalescedCarrierHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def test_conflicting_historical_attempt_and_feed_cannot_publish_success(self):
        self.prepare()
        before = self.owner_bookkeeping()
        first = self.terminal(refused=None, disposition="not_applied")
        attempt_first = self.terminal(refused=None)
        retry = self.terminal()
        retry["carrier_job_id"] = 502
        self.serve_retry(first, retry, attempt_generation=attempt_first)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertEqual(result.consumed, 0)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        self.assertEqual(settlement_stall_projection(self.management)["job_id"], 501)
        self.assertEqual(self.owner_bookkeeping(), before)

    def test_failed_historical_carrier_recovers_before_a_successful_retry(self):
        self.prepare_automatic()
        first = self.terminal()
        first["carrier_job_status"] = "failed"
        first["carrier_job_error"] = {"code": "refused", "message": "The carrier was refused.", "detail": {}}
        retry = self.terminal(refused=None)
        retry["carrier_job_id"] = 502
        self.serve_retry(first, retry)
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.assertEqual((result.consumed, result.cursor), (2, 2))
        self.assertEqual(dict(NSOApplyExecution.objects.values_list("carrier_job_id", "settle_seq")), {501: 1, 502: 2})
        historical = NSOApplyExecution.objects.get(carrier_job_id=501)
        self.assertEqual(historical.terminal_projection["carrier_job_result"], first["carrier_job_result"])
        self.assertFalse(historical.terminal_projection["carrier_job_result"]["commit_acknowledged"])
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_missing_attempt_generations_stalls_its_independently_named_carrier(self):
        self.prepare()
        before = self.owner_bookkeeping()
        self.serve_terminal_evidence(self.terminal(refused=None), manual=True, head=False)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                document["attempts"][0]["generations"] = []
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertEqual(result.consumed, 0)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        self.assertEqual(settlement_stall_projection(self.management)["job_id"], 501)
        self.assertEqual(self.owner_bookkeeping(), before)

    def test_new_terminal_carrier_beyond_the_page_cannot_precede_invalid_work(self):
        from netbox_nso_plugin.refusal_registry import SECTION_COUNTER_KEYS

        first, later = self.prepare_carrier()
        before = self.owner_bookkeeping()
        later["carrier_job_id"] = 601
        for generation in (first, later):
            result = copy.deepcopy(generation["carrier_job_result"])
            counters = {key for section in generation["sections"] for key in SECTION_COUNTER_KEYS[section]}
            result = {
                key: value for key, value in result.items() if not key.endswith("count_by_outcome") or key in counters
            }
            for key in ("row_outcomes", "settlement_owners"):
                result[key] = [row for row in result[key] if row["generation_id"] == generation["generation_id"]]
            generation.update(carrier_job_result=result, row_outcomes=result["row_outcomes"])
        self.serve_terminal_evidence(later, manual=True)
        original = self.adapter._handle
        later_job = self.job(later, 101)
        page = [
            job_wire(
                {
                    **later_job,
                    "id": 500 + seq,
                    "settle_seq": seq,
                    "type": "sync",
                    "result": None,
                    "row_outcomes": None,
                    "refusals": None,
                }
            )
            for seq in range(1, 101)
        ]
        page[0] = job_wire(self.job(first, 1))
        page[1] = make_evidence_envelope(
            None,
            identity={"id": 502, "device_id": self.management.adapter_device_id, "settle_seq": 2},
            error="execution_contract",
            digest="b" * 64,
        )
        end_reads = 0

        def handle(method, url, **kwargs):
            nonlocal end_reads
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                if after == 100:
                    end_reads += 1
                    return make_response(200, [] if end_reads == 1 else [job_wire(later_job)])
                return make_response(200, page)
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                template = response.json()[0]["projection"]
                return make_response(
                    200,
                    [
                        generation_wire(
                            {
                                **template,
                                **{
                                    key: generation[key]
                                    for key in (
                                        "generation_id",
                                        "seq",
                                        "status",
                                        "carrier_job_id",
                                        "carrier_job_status",
                                        "stream_revisions",
                                        "source_push_seq",
                                        "updated_at",
                                    )
                                },
                                "job_id": generation["carrier_job_id"],
                                "digest": link["digest"],
                                "settlement_cohort": 9 + index,
                            },
                            self.management.adapter_device_id,
                        )
                        for index, (generation, link) in enumerate(
                            zip((first, later), self.attempt.response["generations"], strict=True)
                        )
                    ],
                )
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                document["head"]["projection"]["settlement_cohort"] = 10
                document["attempts"][0]["generations"] = [
                    generation_wire(generation, self.management.adapter_device_id) for generation in (first, later)
                ]
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertEqual((result.consumed, result.cursor), (1, 1))
        self.assertEqual(NSOApplyExecution.objects.get(carrier_job_id=501).settle_seq, 1)
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id=601).exists())
        after = self.owner_bookkeeping()
        self.assertEqual(after[0][0], "in_sync")
        self.assertEqual(after[1], before[1])

    def test_replayed_no_op_restoration_waits_for_the_ordered_walk_to_drain(self):
        self.prepare_automatic()
        generation = self.terminal(refused=None)
        self.serve_terminal_evidence(generation)
        vlan = own_vlan(self.management, 79, "unrelated-admission")

        def lost_answer(_selected):
            raise requests.ConnectionError("The admission response was lost.")

        self.adapter.apply_response = lost_answer
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 502)
        attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        self.adapter.apply_response = lambda selected: (200, _no_op(selected))
        original = self.adapter._handle
        evidence_reads = 0

        def handle(method, url, **kwargs):
            nonlocal evidence_reads
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                items = [
                    job_wire(self.job(generation, 1)),
                    make_evidence_envelope(
                        None,
                        identity={"id": 502, "device_id": self.management.adapter_device_id, "settle_seq": 2},
                        error="execution_contract",
                        digest="b" * 64,
                    ),
                ]
                return make_response(200, [item for item in items if item["identity"]["settle_seq"] > after])
            if method == "POST" and url.endswith("/deployment-evidence"):
                evidence_reads += 1
                document = response.json()
                if evidence_reads == 1:
                    document["unknown_apply_attempt_ids"] = [str(attempt.pk)]
                else:
                    document["attempts"].append(
                        make_evidence_envelope(
                            {
                                "apply_attempt_id": str(attempt.pk),
                                "admission_state": "admitted",
                                "http_status": 200,
                                "response": _no_op(attempt.selected),
                                "selected": attempt.selected,
                                "generation_ids": [],
                            },
                            identity={
                                "device_id": self.management.adapter_device_id,
                                "apply_attempt_id": str(attempt.pk),
                            },
                            generations=[],
                        )
                    )
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertEqual((result.consumed, result.cursor), (1, 1))
        attempt.refresh_from_db()
        self.assertIsNone(attempt.response)
        vlan.refresh_from_db()
        self.assertEqual(vlan.status, "deploying")
        self.assertTrue(attempt.overlay_snapshots.filter(settlement_disposition="unresolved").exists())
