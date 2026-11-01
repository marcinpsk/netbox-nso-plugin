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


class CarrierPageHelpers:
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


class TestCommittedSettlementPrefix(CarrierPageHelpers, CoalescedCarrierHelpers, BGPSettlementHelpers, BGPCaptureCase):
    def test_invalid_attempt_identity_records_a_stall_without_advancing_the_cursor(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations)
        self.management.refresh_from_db()
        cursor = self.management.settle_cursor_seq
        original = self.adapter._handle

        def malformed_identity(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method != "POST" or not url.endswith("/deployment-evidence"):
                return response
            body = response.json()
            attempt = body["attempts"][0]
            attempt["identity"]["apply_attempt_id"] = "not-a-uuid"
            attempt["projection"]["apply_attempt_id"] = "not-a-uuid"
            attempt["generations"] = []
            return make_response(response.status_code, body)

        self.adapter._handle = malformed_identity
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.assertEqual(result.consumed, 0)
        self.assertEqual(result.cursor, cursor)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, cursor)
        issue = settlement_stall_projection(self.management)
        self.assertEqual(issue["reason"], "invalid settlement evidence")
        self.assertNotIn("job_id", issue)
        self.assertNotIn("identity", issue)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "deploying")

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

    def test_an_unrelated_failed_generation_outside_the_feed_preserves_the_complete_prefix(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations)
        original = self.adapter._handle
        identity = {
            "device_id": self.management.adapter_device_id,
            "generation_id": 83,
            "seq": 6,
            "carrier_job_id": 502,
            "apply_attempt_id": None,
        }
        failed = make_evidence_envelope(None, identity=identity, error="execution_contract", digest="c" * 64)

        def fail_unrelated_generation(method, url, **kwargs):
            response = original(method, url, **kwargs)
            body = response.json()
            if method == "GET" and url.endswith("/jobs"):
                body = [item for item in body if item["identity"]["id"] == 501]
            elif method == "GET" and url.endswith("/generations"):
                body.append(failed)
            elif method == "POST" and url.endswith("/deployment-evidence"):
                body["head"] = failed
            return make_response(response.status_code, body)

        self.adapter._handle = fail_unrelated_generation
        result = consume_static_route_settlements(self.management)
        self.assertEqual(result.consumed, 1)
        self.assertEqual(result.cursor, 1)
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 1)
        issue = settlement_stall_projection(self.management)
        self.assertEqual(issue["job_id"], 502)
        self.assertEqual(issue["identity"], identity)
        self.assertEqual(issue["digest"], "c" * 64)
        self.assertEqual(issue["store_incarnation"], "00000000-0000-4000-8000-000000000004")
        self.assertEqual(NSOApplyExecution.objects.filter(carrier_job_id=501, consumption_state="consumed").count(), 2)
        self.assertFalse(NSOApplyExecution.objects.filter(carrier_job_id=502).exists())
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_a_truncated_listing_stalls_before_an_earlier_carrier_commits(self):
        generations = self.prepare_carrier()
        self.serve_carriers(generations)
        original = self.adapter._handle
        device_id = self.management.adapter_device_id

        def truncate_after_the_carrier(method, url, **kwargs):
            response = original(method, url, **kwargs)
            body = response.json()
            if method == "GET" and url.endswith("/jobs"):
                body = [item for item in body if item["identity"]["id"] == 501]
            elif method == "GET" and url.endswith("/generations"):
                seq = max(item["identity"]["seq"] for item in body) + 1
                sibling = make_generation_envelope(
                    {**body[0]["projection"], "generation_id": 83, "seq": seq, "carrier_job_id": 502, "job_id": 502},
                    device_id=device_id,
                )
                body.extend([sibling, sibling])
            return make_response(response.status_code, body)

        self.adapter._handle = truncate_after_the_carrier
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.assertEqual(result.consumed, 0)
        self.assertIsNone(result.cursor)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        issue = settlement_stall_projection(self.management)
        self.assertEqual(issue["job_id"], 502)
        self.assertEqual(issue["identity"]["generation_id"], 83)
        self.assertEqual(issue["identity"]["carrier_job_id"], 502)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "deploying")

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

    def test_direct_settlement_resolves_a_carrierless_admission_beside_a_carrier(self):
        from uuid import UUID

        import requests
        from django.urls import reverse

        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts
        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._outbox_case import own_vlan
        from .test_apply_selector import _no_op

        self.prepare()
        generation = self.terminal(refused=None)
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
        no_op = _no_op(attempt.selected)
        self.serve_terminal_evidence(generation, manual=True)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method != "POST" or not url.endswith("/deployment-evidence"):
                return response
            document = response.json()
            document["unknown_apply_attempt_ids"] = [
                value for value in document["unknown_apply_attempt_ids"] if value != str(attempt.pk)
            ]
            document["attempts"].append(
                make_evidence_envelope(
                    {
                        "apply_attempt_id": str(attempt.pk),
                        "admission_state": "admitted",
                        "http_status": 200,
                        "response": no_op,
                        "selected": attempt.selected,
                        "generation_ids": [],
                    },
                    identity={"device_id": self.management.adapter_device_id, "apply_attempt_id": str(attempt.pk)},
                    generations=[],
                )
            )
            return make_response(200, document)

        self.adapter._handle = handle
        settle_device_apply_attempts(self.management)
        attempt.refresh_from_db()
        self.assertEqual(attempt.response, no_op)
        vlan.refresh_from_db()
        self.assertEqual(vlan.status, "accepted")
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_pending_admission_write_cannot_roll_back_an_identified_carrier(self):
        from uuid import uuid4

        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.models import NSOApplyAttempt

        from .test_apply_settlement import _attempt, _payload

        generations = self.prepare_carrier()
        pending_id = uuid4()
        pending = _attempt(pending_id, self.management.adapter_device_id, 83, {"vlan": 99}, "pending")
        pending["response"]["job_id"] = None
        pending["response"]["generations"][0]["job_id"] = None
        pending["generations"][0].update(carrier_job_id=None, carrier_job_status=None)
        local = NSOApplyAttempt.objects.create(
            id=pending_id,
            adapter_apply_attempt_id=pending_id,
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            selected=pending["response"]["selected"],
            scope_revisions=pending["response"]["selected"],
        )
        evidence = _payload(
            self.management.adapter_device_id,
            [
                {
                    "apply_attempt_id": str(self.attempt.pk),
                    "admission_state": "admitted",
                    "http_status": 202,
                    "response": self.attempt.response,
                    "generations": generations,
                },
                pending,
            ],
        )
        evidence.settlement_sequences = {501: 1}

        def reject_pending_admission(execute, sql, params, many, context):
            if sql.startswith("UPDATE") and NSOApplyAttempt._meta.db_table in sql and pending_id in params:
                raise RuntimeError("Pending admission write crossed the carrier boundary")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_pending_admission):
            settle_apply_attempts(self.management, evidence)

        self.assertEqual(NSOApplyExecution.objects.filter(carrier_job_id=501, consumption_state="consumed").count(), 2)
        self.assertFalse(NSOApplyExecution.objects.filter(generation_id=83).exists())
        local.refresh_from_db()
        self.assertIsNone(local.response)
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")
