# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Commit complete carriers before an identified settlement failure."""

from django.db import connection

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.models import NSOApplyExecution, NSOVLANState
from netbox_nso_plugin.refusal_display import settlement_stall_projection
from netbox_nso_plugin.settlement import consume_static_route_settlements

from ._adapter_http import make_evidence_envelope, make_generation_envelope, make_response
from ._bgp_settlement import BGPSettlementHelpers
from .test_coalesced_settlement import CoalescedCarrierHelpers
from .test_durable_bgp_capture import BGPCaptureCase, _CaptureHandler


class TestCommittedSettlementPrefix(CoalescedCarrierHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def serve_carriers(self, generations, *, invariant=False, duplicate=False):
        case = self

        class Handler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", "00000000-0000-4000-8000-000000000004")
                super().end_headers()

        self.server.RequestHandlerClass = Handler
        adapter_client.reset_session()
        first = generations[0]
        job = {
            "id": 501,
            "type": "apply",
            "device_id": self.management.adapter_device_id,
            "status": "succeeded",
            "settle_seq": 1,
            "result": first["carrier_job_result"],
            "row_outcomes": first["row_outcomes"],
            "refusals": first["refusals"],
            "error": None,
            "context": None,
            "created_at": self.stamp.isoformat(),
            "updated_at": first["updated_at"],
            "started_at": None,
            "heartbeat_at": None,
        }
        self.later_requests = []
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            device_id = case.management.adapter_device_id
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                case.later_requests.append(after)
                items = [
                    make_evidence_envelope(job, identity={key: job[key] for key in ("id", "device_id", "settle_seq")}),
                    make_evidence_envelope(
                        None,
                        identity={"id": 502, "device_id": device_id, "settle_seq": 2},
                        error="execution_contract",
                        digest="b" * 64,
                    ),
                    make_evidence_envelope(
                        {
                            **job,
                            "id": 503,
                            "type": "sync",
                            "settle_seq": 3,
                            "result": None,
                            "row_outcomes": None,
                            "refusals": None,
                        },
                        identity={"id": 503, "device_id": device_id, "settle_seq": 3},
                    ),
                ]
                if invariant:
                    items[1] = make_evidence_envelope(
                        {
                            **job,
                            "id": 502,
                            "type": "sync",
                            "status": "running",
                            "settle_seq": 2,
                            "result": None,
                            "row_outcomes": None,
                            "refusals": None,
                        },
                        identity={"id": 502, "device_id": device_id, "settle_seq": 2},
                    )
                if duplicate:
                    items[1] = make_evidence_envelope(
                        {
                            **job,
                            "type": "sync",
                            "settle_seq": 2,
                            "result": None,
                            "row_outcomes": None,
                            "refusals": None,
                        },
                        identity={"id": 501, "device_id": device_id, "settle_seq": 2},
                    )
                return make_response(200, [item for item in items if item["identity"]["settle_seq"] > after])
            if method == "GET" and url.endswith("/generations"):
                return make_response(
                    200,
                    [
                        make_generation_envelope(
                            {
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
                                    )
                                },
                                "job_id": 501,
                                "mode": "networked",
                                "settlement_cohort": 9 + index,
                                "digest": case.attempt.response["generations"][index]["digest"],
                                "created_at": case.stamp.isoformat(),
                                "updated_at": generation["updated_at"],
                            },
                            device_id=device_id,
                        )
                        for index, generation in enumerate(generations)
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
                        "attempts": [
                            make_evidence_envelope(
                                {
                                    "apply_attempt_id": str(case.attempt.pk),
                                    "admission_state": "admitted",
                                    "http_status": 202,
                                    "response": case.attempt.response,
                                    "selected": case.attempt.selected,
                                    "generation_ids": [row["generation_id"] for row in generations],
                                },
                                identity={"device_id": device_id, "apply_attempt_id": str(case.attempt.pk)},
                                generations=[make_generation_envelope(row, device_id=device_id) for row in generations],
                            )
                        ],
                        "unknown_apply_attempt_ids": [],
                    },
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def test_identified_error_commits_the_complete_prefix_and_stops_before_the_suffix(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations)
        result = consume_static_route_settlements(self.management)
        self.assertEqual(result.consumed, 1)
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 1)
        issue = settlement_stall_projection(self.management)
        self.assertEqual(issue["job_id"], 502)
        self.assertEqual(issue["settle_seq"], 2)
        self.assertEqual(NSOApplyExecution.objects.filter(carrier_job_id=501, consumption_state="consumed").count(), 2)
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id__in=(502, 503)).exists())
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_a_diagnostic_write_failure_cannot_roll_back_the_committed_carrier(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations)

        def reject_diagnostic(execute, sql, params, many, context):
            if sql.startswith("UPDATE") and '"settle_evidence_error" =' in sql:
                raise RuntimeError("Settlement diagnostic write rejected")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_diagnostic), self.assertRaisesRegex(RuntimeError, "diagnostic write"):
            consume_static_route_settlements(self.management)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 1)
        self.assertEqual(NSOApplyExecution.objects.filter(carrier_job_id=501, consumption_state="consumed").count(), 2)
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_a_second_generation_write_failure_rolls_back_the_whole_carrier(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations)

        def reject_vlan(execute, sql, params, many, context):
            if sql.startswith("UPDATE") and NSOVLANState._meta.db_table in sql:
                raise RuntimeError("Certified VLAN write rejected")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_vlan), self.assertRaisesRegex(RuntimeError, "Certified VLAN write"):
            consume_static_route_settlements(self.management)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "deploying")

    def test_a_later_ordering_invariant_preserves_the_committed_prefix(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations, invariant=True)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertEqual(result.consumed, 1)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 1)
        self.assertEqual(settlement_stall_projection(self.management)["job_id"], 502)
        self.assertEqual(NSOApplyExecution.objects.filter(carrier_job_id=501, consumption_state="consumed").count(), 2)
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id__in=(502, 503)).exists())
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_duplicate_feed_identity_stops_after_the_first_committed_carrier(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations, duplicate=True)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertEqual(result.consumed, 1)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 1)
        self.assertEqual(settlement_stall_projection(self.management)["job_id"], 501)
        self.assertEqual(settlement_stall_projection(self.management)["settle_seq"], 2)
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id=503).exists())

    def test_known_carrierless_admission_restores_owners_after_retained_history(self):
        from uuid import UUID

        import requests
        from django.urls import reverse

        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._outbox_case import own_vlan
        from ._settlement_wire import deployment_wire
        from .test_apply_selector import _no_op

        generations = self.prepare_carrier()
        self.serve_carriers(generations)
        self.assertEqual(consume_static_route_settlements(self.management).consumed, 1)
        vlan = own_vlan(self.management, 79, "carrierless")

        def lost_answer(_selected):
            raise requests.ConnectionError("The admission response was lost.")

        self.adapter.apply_response = lost_answer
        answer = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(answer.status_code, 502)
        attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        self.assertIsNone(attempt.response)
        vlan.refresh_from_db()
        self.assertEqual(vlan.status, "deploying")
        no_op = _no_op(attempt.selected)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                return make_response(200, [])
            if method == "POST" and url.endswith("/deployment-evidence"):
                return make_response(
                    200,
                    deployment_wire(
                        {
                            "device_id": self.management.adapter_device_id,
                            "head": None,
                            "blocked": False,
                            "write_work_pending": False,
                            "held_jobs": [],
                            "pending_generations": 0,
                            "attempts": [
                                {
                                    "apply_attempt_id": str(attempt.pk),
                                    "admission_state": "admitted",
                                    "http_status": 200,
                                    "response": no_op,
                                    "generations": [],
                                }
                            ],
                            "unknown_apply_attempt_ids": [],
                        }
                    ),
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        reads = len(self.adapter.manual_reads)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.drained)
        self.assertEqual(result.cursor, 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.response, no_op)
        vlan.refresh_from_db()
        self.assertEqual(vlan.status, "accepted")
        self.assertFalse(attempt.overlay_snapshots.filter(settlement_disposition="unresolved").exists())
        self.assertEqual(len(self.adapter.manual_reads), reads)
