# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep carrier evidence, ordering, and admission effects in their own transaction."""

import copy
from uuid import UUID

import requests
from django.test import SimpleTestCase
from django.urls import reverse

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyExecution
from netbox_nso_plugin.refusal_display import settlement_stall_projection
from netbox_nso_plugin.settlement import consume_static_route_settlements

from ._adapter_http import make_evidence_envelope, make_generation_envelope, make_response
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
    def serve_listing_only_generation(self, generation, *, pruned=False):
        self.serve_terminal_evidence(generation, head=False, pruned=pruned)
        original = self.adapter._handle
        self.requested_attempts = []

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method != "POST" or not url.endswith("/deployment-evidence"):
                return response
            requested = kwargs["json"]["apply_attempt_ids"]
            self.requested_attempts.append(requested)
            document = response.json()
            self.assertIsNone(document["head"])
            self.assertEqual(document["attempts"], [])
            if str(self.attempt.pk) in requested:
                failed = make_generation_envelope(
                    generation, device_id=self.management.adapter_device_id, error="projection_construction"
                )
                failed["projection"] = None
                document["attempts"] = [
                    make_evidence_envelope(
                        {
                            "apply_attempt_id": str(self.attempt.pk),
                            "admission_state": "admitted",
                            "http_status": 202,
                            "response": self.attempt.response,
                            "selected": self.attempt.selected,
                            "generation_ids": [generation["generation_id"]],
                        },
                        identity={
                            "device_id": self.management.adapter_device_id,
                            "apply_attempt_id": str(self.attempt.pk),
                        },
                        generations=[failed],
                    )
                ]
            return make_response(200, document)

        self.adapter._handle = handle
        evidence = adapter_client.get_deployment_evidence(self.management.adapter_device_id, [self.attempt.pk])
        with self.assertRaises(adapter_client.InvalidJobEvidence):
            adapter_client.raise_item_error(evidence["attempts"][0]["generations"][0])
        self.requested_attempts.clear()
        [listed] = adapter_client.list_device_generations(self.management.adapter_device_id)
        adapter_client.raise_item_error(listed)
        self.assertEqual(listed["generation_id"], generation["generation_id"])

    def test_listing_only_generation_uses_independently_validated_feed_evidence(self):
        self.prepare_automatic()
        generation = self.terminal(refused=None)
        self.serve_listing_only_generation(generation)

        result = consume_static_route_settlements(self.management)

        self.assertFalse(result.stalled)
        self.assertTrue(result.drained)
        self.assertEqual((result.consumed, result.cursor), (1, 1))
        execution = NSOApplyExecution.objects.get(generation_id=generation["generation_id"])
        self.assertEqual((execution.consumption_state, execution.settle_seq), ("consumed", 1))
        self.assertIsNotNone(execution.consumed_at)
        self.assertEqual(execution.terminal_projection["carrier_job_result"], generation["carrier_job_result"])
        self.assertTrue(self.requested_attempts)
        self.assertTrue(all(str(self.attempt.pk) not in requested for requested in self.requested_attempts))
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_listing_only_generation_uses_an_ordered_consumed_certificate(self):
        self.prepare_automatic()
        generation = self.terminal(refused=None)
        self.serve_terminal_evidence(generation, head=False)
        self.assertFalse(consume_static_route_settlements(self.management).stalled)
        execution = NSOApplyExecution.objects.get(generation_id=generation["generation_id"])
        self.assertEqual((execution.consumption_state, execution.settle_seq), ("consumed", 1))
        self.assertIsNotNone(execution.consumed_at)
        before = self.owner_bookkeeping()
        projection = copy.deepcopy(execution.terminal_projection)
        self.serve_listing_only_generation(generation, pruned=True)

        result = consume_static_route_settlements(self.management)

        self.assertFalse(result.stalled)
        self.assertTrue(result.drained)
        self.assertEqual((result.consumed, result.cursor), (0, 1))
        self.assertEqual(self.owner_bookkeeping(), before)
        execution.refresh_from_db()
        self.assertEqual(execution.terminal_projection, projection)
        self.assertEqual(NSOApplyExecution.objects.count(), 1)
        self.assertTrue(self.requested_attempts)
        self.assertTrue(all(str(self.attempt.pk) not in requested for requested in self.requested_attempts))

    def test_listing_only_generation_without_feed_or_certificate_fails_closed(self):
        self.prepare_automatic()
        generation = self.terminal(refused=None)
        self.serve_listing_only_generation(generation, pruned=True)
        before = self.owner_bookkeeping()

        result = consume_static_route_settlements(self.management)

        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.assertEqual((result.consumed, result.cursor), (0, None))
        self.assertEqual(self.owner_bookkeeping(), before)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        self.assertEqual(settlement_stall_projection(self.management)["job_id"], generation["carrier_job_id"])
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())
        self.assertTrue(self.requested_attempts)
        self.assertTrue(all(str(self.attempt.pk) not in requested for requested in self.requested_attempts))

    def test_locked_feed_without_result_rolls_back_retained_success(self):
        self.prepare()
        before = self.owner_bookkeeping()
        generation = self.terminal(refused=None)
        self.serve_terminal_evidence(generation, manual=True, head=False)
        original = self.adapter._handle
        cursor_reads = 0

        def handle(method, url, **kwargs):
            nonlocal cursor_reads
            if method == "GET" and url.endswith("/jobs") and int(kwargs["params"]["after_settle_seq"]) == 0:
                cursor_reads += 1
                if cursor_reads == 2:
                    job = {**self.job(generation, 1), "result": None, "row_outcomes": None, "refusals": None}
                    return make_response(200, [job_wire(job)])
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        result = consume_static_route_settlements(self.management)
        self.assertEqual(cursor_reads, 2)
        self.assertTrue(result.stalled)
        self.assertEqual(result.consumed, 0)
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state__in=("consumed", "superseded")).exists())
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        issue = settlement_stall_projection(self.management)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["job_id"], 501)
        self.assertEqual(issue["settle_seq"], 1)
        self.assertEqual(self.owner_bookkeeping(), before)
        self.assertTrue(self.attempt.overlay_snapshots.filter(settlement_disposition="unresolved").exists())

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

    def test_each_later_carrier_reads_only_its_own_listing_and_feed_rows(self):
        from unittest.mock import patch

        from netbox_nso_plugin import settlement

        self.prepare_automatic()
        first = self.terminal()
        first["carrier_job_status"] = "failed"
        first["carrier_job_error"] = {"code": "refused", "message": "The carrier was refused.", "detail": {}}
        retry = self.terminal(refused=None)
        retry["carrier_job_id"] = 502
        self.serve_retry(first, retry)
        self.assertEqual(consume_static_route_settlements(self.management).cursor, 2)
        device_id = self.management.adapter_device_id
        syncs = [
            {
                **self.job(retry, 3 + index),
                "id": 601 + index,
                "type": "sync",
                "result": None,
                "row_outcomes": None,
                "refusals": None,
            }
            for index in range(12)
        ]
        pending = [
            {
                "generation_id": 1000 + index,
                "seq": 5 + index,
                "status": "pending",
                "job_id": None,
                "carrier_job_id": None,
                "carrier_job_status": None,
                "mode": "networked",
                "settlement_cohort": 9,
                "digest": "a" * 64,
                "stream_revisions": {"bgp": 8 + index},
                "source_push_seq": retry["source_push_seq"],
                "created_at": self.stamp.isoformat(),
                "updated_at": self.stamp.isoformat(),
            }
            for index in range(150)
        ]
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                rows = response.json() + [job_wire(job) for job in syncs if job["settle_seq"] > after]
                return make_response(200, rows[: int(kwargs["params"]["limit"])])
            if method == "GET" and url.endswith("/generations"):
                return make_response(200, response.json() + [generation_wire(row, device_id) for row in pending])
            return response

        self.adapter._handle = handle
        current, foreign, classes = [None], [], {}

        def counted(row):
            base = type(row)
            if base not in classes:

                def getitem(item, key, base=base):
                    identity = item.evidence_identity
                    if current[0] is not None and identity.get("id", identity.get("carrier_job_id")) != current[0]:
                        foreign[-1] += 1
                    return base.__getitem__(item, key)

                classes[base] = type(f"Counted{base.__name__}", (base,), {"__getitem__": getitem})
            row.__class__ = classes[base]
            return row

        feed, listing, commit = (
            adapter_client.get_settlement_feed,
            adapter_client.list_device_generations,
            settlement._commit_carrier,
        )

        def counted_feed(*args, **kwargs):
            jobs, incarnation = feed(*args, **kwargs)
            return [counted(job) for job in jobs], incarnation

        def counted_commit(management, read, carrier_id, *args, **kwargs):
            current[0] = carrier_id
            foreign.append(0)
            try:
                return commit(management, read, carrier_id, *args, **kwargs)
            finally:
                current[0] = None

        with (
            patch.object(adapter_client, "get_settlement_feed", counted_feed),
            patch.object(
                adapter_client,
                "list_device_generations",
                lambda *args, **kwargs: [counted(row) for row in listing(*args, **kwargs)],
            ),
            patch.object(settlement, "_commit_carrier", counted_commit),
        ):
            result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.assertTrue(result.drained)
        self.assertEqual((result.consumed, result.cursor), (12, 14))
        # The historical retry carrier commits first, then every sync carrier in feed order.
        self.assertEqual(len(foreign), 13)
        self.assertGreater(foreign[0], 0)
        self.assertEqual(foreign[1:], [0] * 12)

    def test_manual_carrier_before_retry_uses_its_independent_admission(self):
        self.prepare()
        first = self.terminal(refused=None)
        retry = self.terminal()
        retry["carrier_job_id"] = 502
        self.serve_retry(first, retry, attempt_generation=retry)
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.assertEqual((result.consumed, result.cursor), (2, 2))
        first_execution = NSOApplyExecution.objects.get(carrier_job_id=501)
        retry_execution = NSOApplyExecution.objects.get(carrier_job_id=502)
        self.assertEqual(first_execution.attempt_id, self.attempt.pk)
        self.assertEqual(first_execution.consumption_state, "consumed")
        self.assertNotEqual(retry_execution.attempt_id, self.attempt.pk)
        self.assertEqual(retry_execution.attempt.trigger_kind, "explicit_retry")
        self.assertEqual(retry_execution.consumption_state, "consumed")
        self.assertEqual((first_execution.settle_seq, retry_execution.settle_seq), (1, 2))
        self.assertEqual(self.owner_bookkeeping()[1][0], "apply_failed")
        for owner in (self.owners[0], self.owners[2]):
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


class TestEvidenceRows(SimpleTestCase):
    def test_indexed_rows_refuse_in_place_mutation_and_survive_copies(self):
        from netbox_nso_plugin.apply_settlement import _Rows

        rows = _Rows([{"id": 1, "carrier_job_id": 7}, {"id": 2, "carrier_job_id": 8}])
        self.assertEqual(rows.matching(carrier_job_id=[7]), [rows[0]])
        mutations = {
            "append": lambda: rows.append({"id": 3, "carrier_job_id": 7}),
            "extend": lambda: rows.extend([{"id": 3, "carrier_job_id": 7}]),
            "insert": lambda: rows.insert(0, {"id": 3, "carrier_job_id": 7}),
            "pop": rows.pop,
            "remove": lambda: rows.remove(rows[0]),
            "clear": rows.clear,
            "sort": lambda: rows.sort(key=lambda row: -row["id"]),
            "reverse": rows.reverse,
            "setitem": lambda: rows.__setitem__(0, {"id": 3, "carrier_job_id": 8}),
            "delitem": lambda: rows.__delitem__(0),
            "iadd": lambda: rows.__iadd__([{"id": 3, "carrier_job_id": 7}]),
            "imul": lambda: rows.__imul__(2),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name), self.assertRaises(TypeError):
                mutate()
        self.assertEqual(rows.matching(carrier_job_id=[7]), [{"id": 1, "carrier_job_id": 7}])
        for name, clone in (("copy", copy.copy), ("deepcopy", copy.deepcopy), ("pickle", _pickle_round_trip)):
            with self.subTest(clone=name):
                cloned = clone(rows)
                self.assertIs(type(cloned), _Rows)
                self.assertEqual(cloned, rows)
                self.assertEqual(cloned.matching(carrier_job_id=[8]), [rows[1]])


def _pickle_round_trip(value):
    import pickle

    return pickle.loads(pickle.dumps(value))
