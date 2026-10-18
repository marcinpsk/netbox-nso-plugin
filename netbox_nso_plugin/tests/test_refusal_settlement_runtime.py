# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Consume complete logical BGP evidence after real HTTP Apply admission."""

import copy
from datetime import timedelta
from uuid import UUID

from django.urls import reverse

from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyExecution

from .test_apply_selector import _promoted
from .test_durable_bgp_capture import BGPCaptureCase


class TestRefusalSettlementRuntime(BGPCaptureCase):
    def prepare(self, *, redistribution=False, mode="networked"):
        def admission(selected):
            response = _promoted(selected)
            response["generations"] = response["generations"][:1]
            response["generations"][0]["mode"] = mode
            return 202, response

        self.adapter.apply_response = admission
        scope = self.scope()
        self.owners = [
            self.router_owner(),
            self.peer(scope, "198.18.0.2"),
            self.peer(scope, "198.18.0.3", status="in_sync"),
        ]
        if redistribution:
            self.owners.append(self.redistribution(scope))
        self.prior = {
            (row._meta.label_lower, row.pk): (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id)
            for row in self.owners
        }
        answer = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(answer.status_code, 200, answer.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))

    def terminal(self, refused=1, *, disposition=None):
        import json
        from pathlib import Path

        cause = json.loads((Path(__file__).parent / "fixtures/refusal_settlement.json").read_text(encoding="utf-8"))[
            "bgp"
        ]["refusal"]
        snapshots = list(self.attempt.overlay_snapshots.order_by("model_label", "overlay_pk"))
        selected = self.attempt.selected

        def identity(ref, accepted_key="acceptance_identity"):
            return {
                "stream": "bgp",
                "row": {"subject": ref["subject"], "key": ref["key"]},
                "source_push_seq": selected["bgp"],
                "frozen_revision": 7,
                accepted_key: self.stamp.isoformat(),
                "generation_id": 81,
                "settlement_cohort": 9,
            }

        owner = self.owners[refused] if refused is not None else None
        outcomes = []
        for snapshot in snapshots:
            ref = snapshot.contributors["rows"][0]["logical_ref"]
            failed = owner is not None and (snapshot.model_label, snapshot.overlay_pk) == (
                owner._meta.label_lower,
                owner.pk,
            )
            outcome_cause = {**cause, "row": {"subject": ref["subject"], "key": ref["key"]}}
            outcomes.append(
                {
                    **identity(ref),
                    "carrier": identity(snapshot.contributors["rows"][0]["carrier_ref"], "accepted_at"),
                    "disposition": disposition or ("refused" if failed else "not_applied" if owner else "in_sync"),
                    "refusals": [outcome_cause] if failed else [],
                }
            )
        failed = next((row for row in outcomes if row["disposition"] == "refused"), None)
        causes = (
            []
            if failed is None
            else [
                {
                    **failed["refusals"][0],
                    "logical_rows": [
                        {key: value for key, value in failed.items() if key not in {"disposition", "refusals"}}
                    ],
                    "provenance": "current",
                    "sender_phase": "pre_commit_dry_run",
                }
            ]
        )
        counter = dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0)
        carriers = {}
        priority = {"in_sync": 0, "not_applied": 1, "unavailable": 2, "refused": 3}
        for outcome in outcomes:
            key = json.dumps(outcome["carrier"], sort_keys=True)
            previous = carriers.get(key)
            if previous is None or priority[outcome["disposition"]] > priority[previous["disposition"]]:
                carriers[key] = {**outcome["carrier"], "disposition": outcome["disposition"]}
        for carrier in carriers.values():
            counter["apply_failed" if carrier["disposition"] == "refused" else carrier["disposition"]] += 1
        result = {
            "row_outcomes": outcomes,
            "refusals": causes,
            "settlement_owners": list(carriers.values()),
            "bgp_count_by_outcome": counter,
            "commit_acknowledged": owner is None,
        }
        return {
            "generation_id": 81,
            "seq": 4,
            "status": "failed" if owner else "settled",
            "sections": ["bgp"],
            "stream_revisions": {"bgp": 7},
            "source_push_seq": {"bgp": selected["bgp"]},
            "carrier_job_id": 501,
            "carrier_job_status": "succeeded",
            "carrier_job_result": result,
            "carrier_job_error": None,
            "updated_at": (self.stamp + timedelta(days=1)).isoformat(),
            "row_outcomes": outcomes,
            "refusals": causes,
        }

    def settle(self, generation):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        class Evidence(dict):
            pass

        evidence = Evidence(
            device_id=self.management.adapter_device_id,
            head=None,
            blocked=False,
            write_work_pending=False,
            held_jobs=[],
            pending_generations=0,
            attempts=[
                {
                    "apply_attempt_id": str(self.attempt.pk),
                    "admission_state": "admitted",
                    "http_status": 202,
                    "response": self.attempt.response,
                    "generations": [generation],
                }
            ],
            unknown_apply_attempt_ids=[],
        )
        evidence.store_incarnation = "placeholder-store"
        evidence.settlement_sequences = {501: 1}
        settle_apply_attempts(self.management, evidence)

    def test_child_refusal_restores_unnamed_peer(self):
        self.prepare()
        self.settle(self.terminal())
        for index, row in enumerate(self.owners):
            row.refresh_from_db()
            if index == 1:
                self.assertEqual(row.status, "apply_failed")
                self.assertEqual(row.last_apply_at, self.stamp)
                self.assertIn("device rejected", row.last_apply_error)
            else:
                self.assertEqual(
                    (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                    self.prior[(row._meta.label_lower, row.pk)],
                )
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")

    def test_router_and_two_peers_settle_success_with_one_carrier(self):
        self.prepare()
        generation = self.terminal(refused=None)
        self.settle(generation)
        self.settle(copy.deepcopy(generation))
        self.assertEqual(NSOApplyExecution.objects.count(), 1)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
            self.assertEqual(row.last_apply_at.isoformat(), generation["updated_at"])
            self.assertEqual(row.last_apply_error, "")
            self.assertEqual(row.apply_attempt_id, self.attempt.pk)

    def test_unknown_duplicate_and_unpushed_outcomes_fail_batch_closed(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError

        self.prepare()
        generation = self.terminal()
        generation["row_outcomes"].append(copy.deepcopy(generation["row_outcomes"][0]))
        for row in self.owners:
            row.refresh_from_db()
        before = [(row.status, row.last_apply_error) for row in self.owners]
        with self.assertRaises(EvidenceInvariantError):
            self.settle(generation)
        for row, prior in zip(self.owners, before, strict=True):
            row.refresh_from_db()
            self.assertEqual((row.status, row.last_apply_error), prior)
        execution = NSOApplyExecution.objects.get()
        self.assertEqual(execution.consumption_state, "unconsumed")
        self.assertTrue(execution.invariant_code)

    def test_router_id_refusal_restores_both_peers(self):
        self.prepare()
        self.settle(self.terminal(refused=0))
        for index, row in enumerate(self.owners):
            row.refresh_from_db()
            if index == 0:
                self.assertEqual(row.status, "apply_failed")
                self.assertEqual(row.last_apply_at, self.stamp)
            else:
                self.assertEqual(
                    (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                    self.prior[(row._meta.label_lower, row.pk)],
                )

    def test_router_scope_af_refusal_settles_router_owner(self):
        self.prepare()
        generation = self.terminal(refused=0)
        snapshot = self.attempt.overlay_snapshots.get(model_label=self.owners[0]._meta.label_lower)
        child = next(
            ref
            for ref in snapshot.contributors["rows"][0]["emitted_refs"]
            if ref["subject"] == "bgp.router.scope.address-family"
        )
        cause = next(outcome for outcome in generation["row_outcomes"] if outcome["disposition"] == "refused")[
            "refusals"
        ][0]
        cause["row"] = {key: child[key] for key in ("subject", "key")}
        generation["refusals"][0]["row"] = cause["row"]
        self.settle(generation)
        self.owners[0].refresh_from_db()
        self.assertEqual(self.owners[0].status, "apply_failed")
        for row in self.owners[1:]:
            row.refresh_from_db()
            self.assertEqual(row.last_apply_error, self.prior[(row._meta.label_lower, row.pk)][2])

    def test_child_refusal_preserves_successor_peer(self):
        from ._outbox_case import content_update

        self.prepare()
        generation = self.terminal()
        content_update(self.owners[1].bgp_peer, password="placeholder-successor-secret")
        successor = type(self.owners[1]).objects.get(pk=self.owners[1].pk)
        before = (successor.status, successor.last_apply_at, successor.last_apply_error, successor.apply_attempt_id)
        self.settle(generation)
        successor.refresh_from_db()
        self.assertEqual(
            (successor.status, successor.last_apply_at, successor.last_apply_error, successor.apply_attempt_id), before
        )
        for row in (self.owners[0], self.owners[2]):
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[(row._meta.label_lower, row.pk)],
            )

    def test_unavailable_restores_exact_prior_bookkeeping(self):
        self.prepare()
        generation = self.terminal(refused=None, disposition="unavailable")
        self.settle(generation)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[(row._meta.label_lower, row.pk)],
            )
        self.assertEqual(
            set(NSOApplyExecution.objects.get().overlay_snapshots.values_list("settlement_disposition", flat=True)),
            {"unavailable"},
        )

    def prepare_automatic(self):
        from netbox_nso_plugin import drain

        scope = self.scope()
        self.owners = [
            self.router_owner(),
            self.peer(scope, "198.18.0.2"),
            self.peer(scope, "198.18.0.3", status="in_sync"),
        ]
        self.prior = {
            (row._meta.label_lower, row.pk): (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id)
            for row in self.owners
        }
        drain.drain_intent_outbox()
        self.attempt = NSOApplyAttempt.objects.get(
            trigger_kind="push", overlay_snapshots__model_label=self.owners[0]._meta.label_lower
        )

    def test_cadence_settles_without_manual_uuid(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        generation = self.terminal()
        execution = settle_terminal_generation(self.management, generation, "placeholder-store", settle_seq=1)
        self.assertEqual(execution.attempt.trigger_kind, "cadence")
        self.assertIsNone(execution.attempt.adapter_apply_attempt_id)
        self.assertEqual(self.adapter.apply_requests, [])
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")

    def test_cadence_captures_fresh_prior_after_earlier_execution(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        success = self.terminal(refused=None)
        first = settle_terminal_generation(self.management, success, "placeholder-store", settle_seq=1)
        refusal = self.terminal()
        refusal["carrier_job_id"] = 502
        second = settle_terminal_generation(self.management, refusal, "placeholder-store", settle_seq=2)
        self.assertEqual(second.attempt.trigger_kind, "explicit_retry")
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        for snapshot in second.overlay_snapshots.all():
            self.assertEqual(snapshot.prior_status, "in_sync")
            self.assertEqual(snapshot.prior_last_apply_at, first.terminal_at)
        self.owners[2].refresh_from_db()
        self.assertEqual(self.owners[2].last_apply_at, first.terminal_at)
        self.assertEqual(self.owners[2].apply_attempt_id, first.attempt_id)

    def test_delayed_automatic_execution_preserves_newer_certified_execution(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        late = self.terminal()
        success = self.terminal(refused=None)
        success["generation_id"], success["seq"], success["carrier_job_id"] = 82, 5, 502
        for outcome in success["row_outcomes"]:
            outcome["generation_id"] = outcome["carrier"]["generation_id"] = 82
        for carrier in success["carrier_job_result"]["settlement_owners"]:
            carrier["generation_id"] = 82
        newest = settle_terminal_generation(self.management, success, "placeholder-store", settle_seq=2)
        delayed = settle_terminal_generation(self.management, late, "placeholder-store", settle_seq=1)
        self.assertEqual(delayed.consumption_state, "superseded")
        self.assertFalse(delayed.overlay_snapshots.exists())
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
            self.assertEqual(row.apply_attempt_id, newest.attempt_id)

    def test_store_reset_cannot_reuse_old_execution_binding(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        generation = self.terminal(refused=None)
        settle_terminal_generation(self.management, generation, "placeholder-old-store", settle_seq=1)
        with self.assertRaises(EvidenceInvariantError):
            settle_terminal_generation(self.management, generation, "placeholder-new-store", settle_seq=1)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")

    def serve_terminal_evidence(self, generation, *, epoch_change=False, manual=False, head=True, pruned=False):
        from ._adapter_http import make_response
        from .test_durable_bgp_capture import _CaptureHandler

        class EvidenceHandler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", handler.server.evidence_incarnation)
                super().end_headers()

        self.server.RequestHandlerClass = EvidenceHandler
        self.server.evidence_incarnation = "placeholder-store"
        from netbox_nso_plugin import adapter_client

        adapter_client.reset_session()
        original = self.adapter._handle
        self.evidence_requests = []
        feed_requests = 0

        def handle(method, url, **kwargs):
            nonlocal feed_requests
            self.evidence_requests.append((method, url))
            if method == "GET" and url.endswith("/jobs"):
                feed_requests += 1
                if epoch_change and feed_requests > 1:
                    self.server.evidence_incarnation = "placeholder-reset-store"
                job = {
                    "id": generation["carrier_job_id"],
                    "type": "apply",
                    "device_id": self.management.adapter_device_id,
                    "status": generation["carrier_job_status"],
                    "result": generation["carrier_job_result"],
                    "row_outcomes": generation["row_outcomes"],
                    "refusals": generation["refusals"],
                    "error": None,
                    "context": None,
                    "created_at": self.stamp.isoformat(),
                    "updated_at": generation["updated_at"],
                    "started_at": self.stamp.isoformat(),
                    "heartbeat_at": None,
                    "settle_seq": 1,
                }
                after = int(kwargs["params"]["after_settle_seq"])
                return make_response(200, [] if pruned or job["settle_seq"] <= after else [job])
            if method == "GET" and url.endswith("/generations"):
                return make_response(
                    200,
                    [
                        {
                            "generation_id": generation["generation_id"],
                            "seq": generation["seq"],
                            "status": generation["status"],
                            "job_id": generation["carrier_job_id"],
                            "mode": "networked",
                            "settlement_cohort": 9,
                            "digest": "a" * 64,
                            "stream_revisions": generation["stream_revisions"],
                            "source_push_seq": generation["source_push_seq"],
                            "created_at": self.stamp.isoformat(),
                            "updated_at": generation["updated_at"],
                        }
                    ],
                )
            if method == "POST" and url.endswith("/deployment-evidence"):
                head = {key: value for key, value in generation.items() if key != "stream_revisions"}
                head.update(
                    mode="networked", settlement_cohort=9, apply_attempt_id=None, created_at=self.stamp.isoformat()
                )
                return make_response(
                    200,
                    {
                        "device_id": self.management.adapter_device_id,
                        "head": head if head_enabled else None,
                        "blocked": generation["status"] == "failed",
                        "write_work_pending": False,
                        "held_jobs": [],
                        "pending_generations": 0,
                        "attempts": [
                            {
                                "apply_attempt_id": str(self.attempt.pk),
                                "admission_state": "admitted",
                                "http_status": 202,
                                "response": self.attempt.response,
                                "generations": [generation],
                            }
                        ]
                        if manual and str(self.attempt.pk) in kwargs["json"]["apply_attempt_ids"]
                        else [],
                        "unknown_apply_attempt_ids": [],
                    },
                )
            return original(method, url, **kwargs)

        head_enabled = head
        self.adapter._handle = handle

    def test_cadence_http_feed_settles_without_manual_uuid(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal())
        evidence = settle_device_apply_attempts(self.management)
        self.assertEqual(evidence["attempts"], [])
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertEqual(self.adapter.apply_requests, [])
        self.assertEqual(NSOApplyExecution.objects.get().settle_seq, 1)

    def test_malformed_feed_identity_cannot_settle_or_advance_the_cursor(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.settlement import settle_static_routes

        from ._adapter_http import make_response

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal())
        original = self.adapter._handle
        invalid = {}
        absent = object()

        def malformed(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs"):
                jobs = response.json()
                for field, value in invalid.items():
                    if value is absent:
                        jobs[0].pop(field)
                    else:
                        jobs[0][field] = value
                return make_response(200, jobs)
            return response

        self.adapter._handle = malformed
        for field, value in (
            ("id", absent),
            ("id", None),
            ("id", True),
            ("id", "501"),
            ("id", 0),
            ("type", absent),
            ("type", None),
            ("type", ""),
            ("type", True),
        ):
            with self.subTest(field=field, value=value):
                invalid.clear()
                invalid[field] = value
                with self.assertRaises(AdapterError) as raised:
                    settle_static_routes(self.management)
                self.assertEqual(raised.exception.code, "invalid_response")
                self.management.refresh_from_db()
                self.assertIsNone(self.management.settle_cursor_seq)
                self.assertFalse(NSOApplyExecution.objects.exists())

    def test_persisted_cursor_skips_large_retained_feed_and_keeps_execution_order(self):
        from netbox_nso_plugin.settlement import settle_static_routes

        from ._adapter_http import make_response
        from ._outbox_case import mirror_update

        self.prepare_automatic()
        generation = self.terminal()
        self.serve_terminal_evidence(generation)
        first = settle_static_routes(self.management)
        self.assertEqual(first.cursor, 1)
        self.management.refresh_from_db()
        mirror_update(self.management, settle_cursor_seq=10001)
        original = self.adapter._handle
        template = original("GET", self.server.adapter_url + "/api/v1/jobs", params={"after_settle_seq": 0}).json()[0]
        starts = []

        def retained_history(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs"):
                params = kwargs["params"]
                after, limit = int(params["after_settle_seq"]), int(params["limit"])
                starts.append(after)
                jobs = []
                for sequence in range(after + 1, min(after + limit, 10002) + 1):
                    job = copy.deepcopy(template)
                    if sequence != 1:
                        job.update(id=100000 + sequence, type="import", result=None, row_outcomes=None, refusals=None)
                    job["settle_seq"] = sequence
                    jobs.append(job)
                return make_response(200, jobs)
            return response

        self.adapter._handle = retained_history
        result = settle_static_routes(self.management)
        self.assertFalse(result.stalled)
        self.assertEqual(result.cursor, 10002)
        self.assertEqual(result.consumed, 1)
        self.assertTrue(starts)
        self.assertTrue(all(start >= 10001 for start in starts), starts)
        self.assertEqual(result.deployment_evidence.settlement_sequences[generation["carrier_job_id"]], 1)
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")

    def test_cursor_from_another_store_restarts_evidence_from_zero(self):
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        from ._adapter_http import make_response
        from ._outbox_case import mirror_update

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal())
        mirror_update(
            self.management,
            settle_cursor_seq=10001,
            settle_cursor_incarnation="placeholder-dead-store",
            settle_cursor_device_id=self.management.adapter_device_id,
        )
        original = self.adapter._handle
        starts = []

        def feed(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                starts.append(after)
                if after:
                    return make_response(200, [])
            return response

        self.adapter._handle = feed
        evidence = load_deployment_evidence(self.management)
        self.assertEqual(starts[:2], [10001, 0])
        self.assertEqual(evidence.settlement_sequences, {501: 1})

    def test_historical_manual_evidence_requires_an_unchanged_ordered_certificate(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence
        from netbox_nso_plugin.settlement import settle_static_routes

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), manual=True)
        self.assertEqual(settle_static_routes(self.management).cursor, 1)
        self.management.refresh_from_db()
        evidence = load_deployment_evidence(self.management, attempt_ids=(self.attempt.pk,))
        self.assertEqual(len(evidence["attempts"]), 1)
        self.assertEqual(evidence.settlement_sequences, {501: 1})
        execution = NSOApplyExecution.objects.get()
        projection = copy.deepcopy(execution.terminal_projection)
        changed = copy.deepcopy(projection)
        changed["carrier_job_error"] = {"message": "placeholder changed error"}
        for sequence, document in ((None, projection), (1, {}), (1, changed)):
            with self.subTest(sequence=sequence, document=document):
                NSOApplyExecution.objects.filter(pk=execution.pk).update(
                    settle_seq=sequence, terminal_projection=document
                )
                with self.assertRaises(EvidenceInvariantError):
                    load_deployment_evidence(self.management, attempt_ids=(self.attempt.pk,))
                self.management.refresh_from_db()
                self.assertEqual(self.management.settle_cursor_seq, 1)
        NSOApplyExecution.objects.filter(pk=execution.pk).update(settle_seq=1, terminal_projection=projection)

    def test_job_that_finishes_during_evidence_fetch_retains_its_sequence(self):
        from netbox_nso_plugin.settlement import settle_static_routes

        from ._adapter_http import make_response

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal())
        original = self.adapter._handle
        first_feed = True

        def late_job(method, url, **kwargs):
            nonlocal first_feed
            response = original(method, url, **kwargs)
            if first_feed and method == "GET" and url.endswith("/jobs"):
                first_feed = False
                return make_response(200, [])
            return response

        self.adapter._handle = late_job
        self.assertEqual(settle_static_routes(self.management).cursor, 1)
        self.assertEqual(NSOApplyExecution.objects.get().settle_seq, 1)
        second = settle_static_routes(self.management)
        self.assertFalse(second.stalled)
        self.assertEqual(second.cursor, 1)
        self.assertEqual(NSOApplyExecution.objects.count(), 1)

    def _serve_retry_evidence(self, first, retry):
        from ._adapter_http import make_response

        self.serve_terminal_evidence(retry)
        original = self.adapter._handle
        template = original("GET", self.server.adapter_url + "/api/v1/jobs", params={"after_settle_seq": 0}).json()[0]
        jobs = []
        for sequence, generation in enumerate((first, retry), start=1):
            job = copy.deepcopy(template)
            job.update(
                id=generation["carrier_job_id"],
                status=generation["carrier_job_status"],
                result=generation["carrier_job_result"],
                error=generation["carrier_job_error"],
                row_outcomes=generation["row_outcomes"],
                refusals=generation["refusals"],
                updated_at=generation["updated_at"],
                settle_seq=sequence,
            )
            jobs.append(job)

        def retry_feed(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                params = kwargs["params"]
                after, limit = int(params["after_settle_seq"]), int(params["limit"])
                return make_response(200, [job for job in jobs if job["settle_seq"] > after][:limit])
            return original(method, url, **kwargs)

        self.adapter._handle = retry_feed

    def test_interrupted_cursor_fetch_recovers_order_before_a_newer_retry(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.settlement import settle_static_routes

        from ._adapter_http import make_response

        self.prepare_automatic()
        first = self.terminal(refused=None)
        self.serve_terminal_evidence(first)
        original = self.adapter._handle
        feed_requests = 0

        def interrupted_poll(method, url, **kwargs):
            nonlocal feed_requests
            if method == "GET" and url.endswith("/jobs"):
                feed_requests += 1
                if feed_requests == 1:
                    return make_response(200, [])
                if feed_requests >= 3:
                    return make_response(503, {"detail": "placeholder interrupted cursor fetch"})
            return original(method, url, **kwargs)

        self.adapter._handle = interrupted_poll
        with self.assertRaises(AdapterError):
            settle_static_routes(self.management)
        self.assertIsNone(NSOApplyExecution.objects.get().settle_seq)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")

        retry = self.terminal()
        retry["carrier_job_id"] = 502
        self._serve_retry_evidence(first, retry)
        result = settle_static_routes(self.management)
        self.assertFalse(result.stalled)
        self.assertEqual((result.consumed, result.cursor), (2, 2))
        self.assertEqual(dict(NSOApplyExecution.objects.values_list("carrier_job_id", "settle_seq")), {501: 1, 502: 2})
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        for row in (self.owners[0], self.owners[2]):
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
        replay = settle_static_routes(self.management)
        self.assertFalse(replay.stalled)
        self.assertEqual((replay.consumed, replay.cursor), (0, 2))
        self.assertEqual(NSOApplyExecution.objects.count(), 2)
        self.assertEqual(self.adapter.apply_requests, [])

    def test_changed_historical_carrier_stalls_before_retry_owner_writes(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation
        from netbox_nso_plugin.settlement import settle_static_routes

        self.prepare_automatic()
        first = self.terminal(refused=None)
        settle_terminal_generation(self.management, first, "placeholder-store", settle_seq=1)
        prior = []
        for row in self.owners:
            row.refresh_from_db()
            prior.append((row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id))
        changed = copy.deepcopy(first)
        changed["carrier_job_error"] = {"message": "placeholder changed historical error"}
        retry = self.terminal()
        retry["carrier_job_id"] = 502
        self._serve_retry_evidence(changed, retry)
        result = settle_static_routes(self.management)
        self.assertTrue(result.stalled)
        for row, expected in zip(self.owners, prior, strict=True):
            row.refresh_from_db()
            self.assertEqual((row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id), expected)
        self.assertEqual(NSOApplyExecution.objects.filter(consumption_state="consumed").count(), 1)
        self.assertEqual(NSOApplyExecution.objects.get().settle_seq, 1)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)

    def test_consumed_execution_rejects_a_different_settlement_sequence(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        generation = self.terminal(refused=None)
        settle_terminal_generation(self.management, generation, "placeholder-store", settle_seq=1)
        with self.assertRaisesRegex(EvidenceInvariantError, "invalid_terminal_batch"):
            settle_terminal_generation(self.management, generation, "placeholder-store", settle_seq=2)
        self.assertEqual(NSOApplyExecution.objects.get().settle_seq, 1)

    def test_evidence_fetch_rejects_incarnation_change(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal(), epoch_change=True)
        with self.assertRaisesRegex(EvidenceInvariantError, "evidence_epoch_changed"):
            settle_device_apply_attempts(self.management)
        self.assertFalse(NSOApplyExecution.objects.exists())
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[(row._meta.label_lower, row.pk)],
            )

    def test_abandoned_generation_projection_preserves_consumed_certificate(self):
        self.prepare()
        generation = self.terminal()
        self.settle(generation)
        execution = NSOApplyExecution.objects.get()
        original_projection = copy.deepcopy(execution.terminal_projection)
        generation["status"] = "abandoned"
        generation["updated_at"] = (self.stamp + timedelta(days=2)).isoformat()
        self.settle(generation)
        execution.refresh_from_db()
        self.assertEqual(execution.terminal_projection, original_projection)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].last_apply_at, self.stamp)

    def test_missing_manual_owner_without_deletion_evidence_fails_batch_closed(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError

        self.prepare()
        generation = self.terminal()
        type(self.owners[1]).objects.filter(pk=self.owners[1].pk).delete()
        before = []
        for row in (self.owners[0], self.owners[2]):
            row.refresh_from_db()
            before.append((row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id))
        with self.assertRaises(EvidenceInvariantError):
            self.settle(generation)
        for row, expected in zip((self.owners[0], self.owners[2]), before, strict=True):
            row.refresh_from_db()
            self.assertEqual((row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id), expected)
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "unconsumed")

    def _delete_owner(self, owner):
        from netbox_nso_plugin.ownership_grants import OwnershipGrant
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        from ._outbox_case import without_commit_drain

        plan = RendererMutationPlan.build(grant=OwnershipGrant("operator_edit"), deletes=(planned_delete(owner),))
        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.delete(owner)

    def test_deleted_manual_owner_is_superseded_without_blocking_its_sibling(self):
        self.prepare()
        generation = self.terminal(refused=2)
        owner = self.owners[1]
        owner_key = owner._meta.label_lower, owner.pk
        self._delete_owner(owner)
        self.settle(generation)
        execution = NSOApplyExecution.objects.get()
        self.assertEqual(execution.consumption_state, "consumed")
        self.assertEqual(
            execution.overlay_snapshots.get(model_label=owner_key[0], overlay_pk=owner_key[1]).settlement_disposition,
            "superseded",
        )
        self.owners[2].refresh_from_db()
        self.assertEqual(self.owners[2].status, "apply_failed")
        self.assertIn("inexpressible-on-ned", self.owners[2].last_apply_error)
        self.owners[0].refresh_from_db()
        self.assertEqual(self.owners[0].status, self.prior[(self.owners[0]._meta.label_lower, self.owners[0].pk)][0])

    def test_deleted_automatic_owner_is_superseded_without_blocking_its_sibling(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        generation = self.terminal(refused=2)
        owner = self.owners[1]
        owner_key = owner._meta.label_lower, owner.pk
        self._delete_owner(owner)
        execution = settle_terminal_generation(self.management, generation, "placeholder-store", settle_seq=1)
        self.assertEqual(execution.consumption_state, "consumed")
        self.assertEqual(
            execution.overlay_snapshots.get(model_label=owner_key[0], overlay_pk=owner_key[1]).settlement_disposition,
            "superseded",
        )
        self.owners[2].refresh_from_db()
        self.assertEqual(self.owners[2].status, "apply_failed")
        self.assertIn("inexpressible-on-ned", self.owners[2].last_apply_error)

    def test_text_refusal_includes_catalog_reason(self):
        self.prepare()
        self.settle(self.terminal())
        self.owners[1].refresh_from_db()
        self.assertIn("inexpressible-on-ned: The device rejected", self.owners[1].last_apply_error)

    def test_redistribution_refusal_is_independent(self):
        self.prepare(redistribution=True)
        generation = self.terminal(refused=3)
        self.settle(generation)
        self.owners[3].refresh_from_db()
        self.assertEqual(self.owners[3].status, "apply_failed")
        for row in self.owners[:3]:
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[(row._meta.label_lower, row.pk)],
            )
        self.assertEqual(
            generation["carrier_job_result"]["bgp_count_by_outcome"],
            {"in_sync": 0, "apply_failed": 1, "not_applied": 1, "unavailable": 0},
        )

    def test_peer_af_cause_settles_only_owning_peer(self):
        self.prepare()
        generation = self.terminal()
        snapshot = self.attempt.overlay_snapshots.get(
            model_label=self.owners[1]._meta.label_lower, overlay_pk=self.owners[1].pk
        )
        child = next(
            ref
            for ref in snapshot.contributors["rows"][0]["emitted_refs"]
            if ref["subject"] == "bgp.router.scope.peer.peer-address-family"
        )
        cause = next(outcome for outcome in generation["row_outcomes"] if outcome["disposition"] == "refused")[
            "refusals"
        ][0]
        cause["row"] = {key: child[key] for key in ("subject", "key")}
        generation["refusals"][0]["row"] = cause["row"]
        self.settle(generation)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        for row in (self.owners[0], self.owners[2]):
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[(row._meta.label_lower, row.pk)],
            )

    def test_detach_generation_records_its_admitted_mode(self):
        self.prepare(mode="detach")
        self.settle(self.terminal(refused=None))
        self.assertEqual(NSOApplyExecution.objects.get().mode, "detach")

    def test_successful_manual_bgp_without_executable_head_uses_its_snapshots(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        self.prepare()
        self.serve_terminal_evidence(self.terminal(refused=None), manual=True, head=False)
        evidence = settle_device_apply_attempts(self.management)
        self.assertEqual(len(evidence["attempts"]), 1)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
        self.assertEqual(NSOApplyExecution.objects.get().attempt_id, self.attempt.pk)

    def test_retry_of_manual_generation_captures_current_bookkeeping(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare()
        first = settle_terminal_generation(
            self.management, self.terminal(), "placeholder-store", attempt=self.attempt, settle_seq=1
        )
        refusal = self.terminal(refused=2)
        refusal["carrier_job_id"] = 502
        second = settle_terminal_generation(
            self.management, refusal, "placeholder-store", attempt=self.attempt, settle_seq=2
        )
        self.owners[1].refresh_from_db()
        self.owners[2].refresh_from_db()
        self.assertEqual(self.owners[2].status, "apply_failed")
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertNotEqual(second.attempt_id, first.attempt_id)
        self.assertEqual(second.attempt.trigger_kind, "explicit_retry")
        self.assertIsNone(second.attempt.adapter_apply_attempt_id)
        snapshot = second.overlay_snapshots.get(
            model_label=self.owners[1]._meta.label_lower, overlay_pk=self.owners[1].pk
        )
        self.assertEqual(snapshot.prior_status, "apply_failed")
        self.assertEqual(snapshot.prior_apply_attempt_id, first.attempt_id)

    def test_pruned_nonhead_terminal_generation_stalls_before_consumption(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal(), head=False, pruned=True)
        with self.assertRaisesRegex(EvidenceInvariantError, "missing_generation_evidence"):
            settle_device_apply_attempts(self.management)
        execution = NSOApplyExecution.objects.get()
        self.assertEqual(execution.consumption_state, "unconsumed")
        self.assertEqual(execution.invariant_code, "missing_generation_evidence")
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[(row._meta.label_lower, row.pk)],
            )

    def test_automatic_split_streams_reconstruct_the_document_sections(self):
        from netbox_nso_plugin.apply_settlement import _generation_from_carrier
        from netbox_nso_plugin.refusal_contract import validate_generation

        for stream, section, counters in (
            ("ip", "interface_config", ("attribute_count_by_outcome", "ip_count_by_outcome")),
            ("isis_flex_algo", "isis", ("isis_count_by_outcome",)),
        ):
            with self.subTest(stream=stream):
                result = {
                    **{
                        key: dict.fromkeys(("in_sync", "apply_failed", "not_applied", "unavailable"), 0)
                        for key in counters
                    },
                    "row_outcomes": [],
                    "refusals": [],
                    "settlement_owners": [],
                    "commit_acknowledged": False,
                }
                generation = {
                    "generation_id": 81,
                    "seq": 4,
                    "status": "failed",
                    "stream_revisions": {stream: 7},
                    "source_push_seq": {stream: 41},
                    "updated_at": self.stamp.isoformat(),
                }
                carrier = {
                    "carrier_job_id": 501,
                    "carrier_job_status": "succeeded",
                    "carrier_job_result": result,
                    "carrier_job_error": None,
                    "row_outcomes": [],
                    "refusals": [],
                }
                reconstructed = _generation_from_carrier(generation, carrier)
                self.assertEqual(reconstructed["sections"], [section])
                self.assertEqual(validate_generation(reconstructed), reconstructed)

    def test_pruned_nonhead_generation_reuses_its_consumed_certificate(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        generation = self.terminal()
        original = settle_terminal_generation(self.management, generation, "placeholder-store", settle_seq=1)
        self.serve_terminal_evidence(generation, head=False, pruned=True)
        settle_device_apply_attempts(self.management)
        self.assertEqual(NSOApplyExecution.objects.count(), 1)
        original.refresh_from_db()
        self.assertEqual(original.consumption_state, "consumed")
        self.assertEqual(original.settle_seq, 1)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")

    def test_epoch_change_during_manual_noop_replay_does_not_restore_rows(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence

        from ._adapter_http import make_response
        from .test_apply_selector import _no_op

        self.prepare()
        NSOApplyAttempt.objects.filter(pk=self.attempt.pk).update(response=None, http_status=None)
        self.attempt.refresh_from_db()
        before = {}
        for row in self.owners:
            row.refresh_from_db()
            before[(row._meta.label_lower, row.pk)] = (
                row.status,
                row.last_apply_at,
                row.last_apply_error,
                row.apply_attempt_id,
            )
        self.adapter.apply_response = lambda selected: (200, _no_op(selected))
        self.serve_terminal_evidence(self.terminal(), epoch_change=True, head=False, pruned=True)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "POST" and url.endswith("/deployment-evidence"):
                return make_response(
                    200,
                    {
                        "device_id": self.management.adapter_device_id,
                        "head": None,
                        "blocked": False,
                        "write_work_pending": False,
                        "held_jobs": [],
                        "pending_generations": 0,
                        "attempts": [],
                        "unknown_apply_attempt_ids": kwargs["json"]["apply_attempt_ids"],
                    },
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        with self.assertRaisesRegex(EvidenceInvariantError, "evidence_epoch_changed"):
            load_deployment_evidence(self.management)
        self.assertEqual(len(self.adapter.apply_requests), 2)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                before[(row._meta.label_lower, row.pk)],
            )
        self.assertFalse(self.attempt.overlay_snapshots.exclude(settlement_disposition="unresolved").exists())

    def test_noop_restore_rolls_back_when_another_generation_has_a_missing_owner(self):
        import requests

        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        from ._outbox_case import own_vlan
        from .test_apply_selector import _no_op
        from .test_apply_settlement import _payload

        self.prepare()
        generation = self.terminal()
        manual = self.attempt
        vlan = own_vlan(self.management, 79, "restore-batch")

        def lost_answer(_selected):
            raise requests.ConnectionError("The admission response was lost.")

        self.adapter.apply_response = lost_answer
        answer = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(answer.status_code, 502, answer.content)
        no_op = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        self.assertNotEqual(no_op.pk, manual.pk)
        vlan.refresh_from_db()
        before = (vlan.status, vlan.last_apply_at, vlan.last_apply_error, vlan.apply_attempt_id)
        self.assertEqual(vlan.status, "deploying")
        type(self.owners[1]).objects.filter(pk=self.owners[1].pk).delete()
        evidence = _payload(
            self.management.adapter_device_id,
            [
                {
                    "apply_attempt_id": str(manual.pk),
                    "admission_state": "admitted",
                    "http_status": 202,
                    "response": manual.response,
                    "generations": [generation],
                },
                {
                    "apply_attempt_id": str(no_op.pk),
                    "admission_state": "admitted",
                    "http_status": 200,
                    "response": _no_op(no_op.selected),
                    "generations": [],
                },
            ],
        )
        evidence.settlement_sequences = {501: 1}
        with self.assertRaises(EvidenceInvariantError):
            settle_apply_attempts(self.management, evidence)
        vlan.refresh_from_db()
        self.assertEqual((vlan.status, vlan.last_apply_at, vlan.last_apply_error, vlan.apply_attempt_id), before)
        self.assertFalse(no_op.overlay_snapshots.exclude(settlement_disposition="unresolved").exists())
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "unconsumed")

    def test_coalesced_head_uuid_binds_only_its_manual_generations(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.apply_settlement import _bound_evidence, settle_apply_attempts

        from ._outbox_case import own_vlan
        from .test_apply_settlement import _payload
        from .test_refusal_settlement_families import TestRefusalSettlementFamilies

        self.prepare()
        manual = self.attempt
        first = self.terminal(refused=None)
        vlan = own_vlan(self.management, 78, "coalesced-automatic")
        drain.drain_intent_outbox()
        self.attempt = NSOApplyAttempt.objects.get(
            trigger_kind="push",
            overlay_snapshots__model_label=vlan._meta.label_lower,
            overlay_snapshots__overlay_pk=vlan.pk,
        )
        second = TestRefusalSettlementFamilies.evidence(self, dispositions={"vlan.vlan": "in_sync"})
        second.update(generation_id=82, seq=5)
        for identity in second["row_outcomes"] + second["carrier_job_result"]["settlement_owners"]:
            identity["generation_id"] = 82
            if "carrier" in identity:
                identity["carrier"]["generation_id"] = 82
        result = first["carrier_job_result"]
        result.update(vlan_count_by_outcome=second["carrier_job_result"]["vlan_count_by_outcome"])
        result["row_outcomes"].extend(second["row_outcomes"])
        result["settlement_owners"].extend(second["carrier_job_result"]["settlement_owners"])
        for generation in (first, second):
            generation.update(
                carrier_job_result=copy.deepcopy(result), row_outcomes=copy.deepcopy(result["row_outcomes"])
            )
        head = {key: value for key, value in first.items() if key != "stream_revisions"}
        head.update(
            apply_attempt_id=str(manual.pk), mode="networked", settlement_cohort=9, created_at=self.stamp.isoformat()
        )
        metadata = [
            {
                **{
                    key: generation[key]
                    for key in ("generation_id", "seq", "status", "stream_revisions", "source_push_seq", "updated_at")
                },
                "job_id": 501,
                "mode": "networked",
                "settlement_cohort": 9,
                "digest": "a" * 64,
                "created_at": self.stamp.isoformat(),
            }
            for generation in (first, second)
        ]
        bound = _bound_evidence(
            _payload(self.management.adapter_device_id, [], head=head),
            [],
            metadata,
            "placeholder-store",
            management=self.management,
        )
        bound.settlement_sequences = {501: 1}
        self.assertEqual(bound.manual_attempt_by_generation[(81, 501)].pk, manual.pk)
        self.assertNotIn((82, 501), bound.manual_attempt_by_generation)
        settle_apply_attempts(self.management, bound)
        for row in [*self.owners, vlan]:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
        self.assertEqual(NSOApplyExecution.objects.filter(consumption_state="consumed").count(), 2)

    def _serve_bgp_observation(self, generation=None, *, drift_peer=None):
        from ._adapter_http import make_response
        from ._routing_observation_case import routing_observation
        from ._scope_observation_case import entry
        from .test_read_gate import _rs

        self.serve_terminal_evidence(self.terminal() if generation is None else generation)
        self.server.evidence_incarnation = self.management.adapter_incarnation
        original = self.adapter._handle
        observation = routing_observation(
            "bgp",
            document={
                "routers": [
                    entry(
                        asn="64512",
                        router_id="198.18.0.1",
                        scope=[
                            entry(
                                vrf="",
                                address_family=[entry(afi="ipv4-unicast")],
                                peer_group=[],
                                peer=[
                                    entry(
                                        peer_address=row.peer_address_str,
                                        remote_as="64513",
                                        enabled=row.pk != getattr(drift_peer, "pk", None),
                                        password_present=True,
                                        peer_address_family=[entry(afi="ipv4-unicast", enabled=True)],
                                    )
                                    for row in self.owners[1:]
                                ],
                            )
                        ],
                    )
                ],
                "present": ["routers"],
                "unprojectable": [],
            },
        )
        peers = [
            {
                "peer_address": row.peer_address_str,
                "remote_as": "64513",
                "enabled": row.pk != getattr(drift_peer, "pk", None),
                "password": "placeholder-secret-canary",
                "address_families": [{"af": "ipv4-unicast", "enabled": True}],
            }
            for row in self.owners[1:]
        ]
        document = {
            "routers": [
                {
                    "asn": "64512",
                    "router_id": "198.18.0.1",
                    "scopes": [{"vrf": "", "address_families": ["ipv4-unicast"], "peers": peers, "peer_groups": []}],
                }
            ],
            "read_state": _rs(),
            "observation": observation,
        }

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/bgp-config"):
                return make_response(200, document)
            if method == "GET" and url.endswith("/bfd"):
                return make_response(200, {"interfaces": []})
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def _notify_with_bgp_observation(self):
        from unittest.mock import patch
        from uuid import uuid4

        import django_rq
        from rest_framework.test import APIClient
        from rq import Queue, SimpleWorker
        from users.constants import TOKEN_PREFIX
        from users.models import Token, User

        self._serve_bgp_observation()
        queue = Queue(f"nso-observation-{uuid4().hex}", connection=django_rq.get_queue("default").connection)
        self.addCleanup(queue.delete, delete_jobs=True)
        from netbox_nso_plugin.read_gate import carrier_key, marker_key

        self.addCleanup(queue.connection.delete, carrier_key(self.device.pk), marker_key(self.device.pk))
        user = User.objects.create_user(username=f"placeholder-callback-{uuid4().hex[:8]}", is_superuser=True)
        token = Token.objects.create(user=user)
        api = APIClient()
        with patch("django_rq.get_queue", return_value=queue):
            response = api.post(
                "/api/plugins/nso/sync-complete/",
                {"netbox_device_id": self.device.pk},
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}",
            )
            self.assertEqual(response.status_code, 202, response.content)
            SimpleWorker([queue], connection=queue.connection).work(burst=True, with_scheduler=False)
        self.assertFalse(queue.failed_job_registry.get_job_ids())
        self.assertTrue(queue.finished_job_registry.get_job_ids())

    def _prepare_bgp_observation_capture(self):
        from datetime import datetime

        from ._outbox_case import mirror_update, without_commit_drain
        from .test_read_gate import _INC_A

        mirror_update(
            self.management,
            adapter_incarnation=_INC_A[0],
            adapter_incarnation_born=datetime.fromisoformat(_INC_A[1]),
            adapter_source_epoch=1,
            source_epoch_aware=True,
        )
        with without_commit_drain():
            self.management.manage_routing = True
            self.management.save(update_fields=("manage_routing",))
        self.prepare_automatic()

    def test_callback_consumes_first_refusal_before_new_bgp_observation(self):
        from netbox_nso_plugin.models import NSOFamilyObservation, NSOIntentOutboxEntry

        self._prepare_bgp_observation_capture()
        before = NSOIntentOutboxEntry.objects.count()
        puts = len(self.adapter.capture_reads)
        self._notify_with_bgp_observation()
        self.assertTrue(NSOFamilyObservation.objects.filter(read_state__family="bgp", revision=1).exists())
        execution = NSOApplyExecution.objects.get()
        self.assertEqual(execution.consumption_state, "consumed")
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertTrue(self.owners[1].last_apply_error)
        self.assertEqual(NSOIntentOutboxEntry.objects.count(), before)
        self.assertEqual(len(self.adapter.capture_reads), puts)
        self.assertEqual(self.adapter.apply_requests, [])

    def _assert_redacted_capture_settles(self, *, manual):
        from django.db import transaction

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.lifecycle_fence import fence_for
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        from ._outbox_case import mirror_update

        scope = self.scope()
        self.owners = [self.router_owner(), self.peer(scope, "198.18.0.2"), self.peer(scope, "198.18.0.3")]
        mirror_update(self.owners[1], last_apply_error="device refused placeholder-secret-canary")
        drain.drain_intent_outbox()
        self.attempt = NSOApplyAttempt.objects.get(
            trigger_kind="push", overlay_snapshots__model_label=self.owners[0]._meta.label_lower
        )
        if manual:
            mirror_update(self.owners[1], last_apply_error="device refused placeholder-secret-canary")

            def admission(selected):
                response = _promoted(selected)
                response["generations"] = response["generations"][:1]
                return 202, response

            self.adapter.apply_response = admission
            answer = self.client.post(
                reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(answer.status_code, 200, answer.content)
            self.attempt = NSOApplyAttempt.objects.get(
                pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"])
            )
        snapshot = self.attempt.overlay_snapshots.get(
            model_label=self.owners[1]._meta.label_lower, overlay_pk=self.owners[1].pk
        )
        self.assertNotIn("placeholder-secret-canary", snapshot.prior_last_apply_error)
        with transaction.atomic():
            self.assertEqual(snapshot.expected_fence_revision, fence_for(self.owners[1]).bookkeeping_revision)
        puts = len(self.adapter.capture_reads)
        generation = self.terminal()
        if manual:
            self.settle(generation)
            execution = NSOApplyExecution.objects.get()
        else:
            execution = settle_terminal_generation(self.management, generation, "placeholder-store", settle_seq=1)
        self.assertEqual(execution.consumption_state, "consumed")
        self.assertEqual(len(self.adapter.capture_reads), puts)
        self.assertEqual(len(self.adapter.apply_requests), int(manual))
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertNotIn("placeholder-secret-canary", self.owners[1].last_apply_error)

    def test_redacted_automatic_capture_remains_eligible_for_refusal(self):
        self._assert_redacted_capture_settles(manual=False)

    def test_redacted_manual_capture_remains_eligible_for_refusal(self):
        self._assert_redacted_capture_settles(manual=True)

    def _read_bgp_category(self):
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category", args=(self.device.pk, "bgp")),
            {"refresh": "1"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_category_observation_after_evidence_fetch_preserves_selected_refusal(self):
        from django.db import transaction

        from netbox_nso_plugin.apply_settlement import load_deployment_evidence, settle_apply_attempts
        from netbox_nso_plugin.lifecycle_fence import fence_for
        from netbox_nso_plugin.models import NSOFamilyObservation

        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation(self.terminal(refused=2), drift_peer=self.owners[2])
        evidence = load_deployment_evidence(self.management)
        self._read_bgp_category()
        self.assertTrue(NSOFamilyObservation.objects.filter(read_state__family="bgp").exists())
        self.owners[2].refresh_from_db()
        self.assertEqual(self.owners[2].status, "accepted")
        with transaction.atomic():
            captured = self.attempt.overlay_snapshots.get(
                model_label=self.owners[2]._meta.label_lower, overlay_pk=self.owners[2].pk
            )
            self.assertEqual(fence_for(self.owners[2]).owner_revision, captured.expected_owner_revision)
        puts = len(self.adapter.capture_reads)
        settle_apply_attempts(self.management, evidence)
        self.owners[2].refresh_from_db()
        self.assertEqual(self.owners[2].status, "apply_failed")
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")
        self.assertEqual(len(self.adapter.capture_reads), puts)
        self.assertEqual(self.adapter.apply_requests, [])

    def test_category_observation_survives_unavailable_terminal_evidence(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts
        from netbox_nso_plugin.models import NSOFamilyObservation

        from ._adapter_http import make_response

        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation()
        original = self.adapter._handle

        def unavailable(method, url, **kwargs):
            if url.endswith(("/deployment-evidence", "/jobs")):
                return make_response(503, {"error": {"code": "unavailable", "message": "Evidence unavailable"}})
            return original(method, url, **kwargs)

        self.adapter._handle = unavailable
        self._read_bgp_category()
        self.assertTrue(NSOFamilyObservation.objects.filter(read_state__family="bgp").exists())
        with self.assertRaises(AdapterError):
            settle_device_apply_attempts(self.management)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "accepted")
        self.assertFalse(NSOApplyExecution.objects.exists())
        self.adapter._handle = original
        settle_device_apply_attempts(self.management)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")

    def test_category_observation_does_not_reassociate_an_aba_intent(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        from ._outbox_case import content_update

        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation()
        native = self.owners[1].bgp_peer
        content_update(native, enabled=False)
        content_update(native, enabled=True)
        self._read_bgp_category()
        settle_device_apply_attempts(self.management)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "accepted")
        self.assertIsNone(self.owners[1].apply_attempt_id)
        self.assertFalse(
            NSOApplyExecution.objects.get()
            .overlay_snapshots.filter(model_label=self.owners[1]._meta.label_lower, overlay_pk=self.owners[1].pk)
            .exists()
        )

    def test_unchanged_push_preserves_an_older_automatic_execution(self):
        from django.db import transaction

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts
        from netbox_nso_plugin.lifecycle_fence import fence_for

        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation()
        original = self.attempt
        with transaction.atomic():
            fence = fence_for(self.owners[1])
            before = (fence.bookkeeping_revision, fence.owner_revision, fence.current_attempt_id)
        answer = drain.push_now(self.device.pk, "bgp", force=True)
        self.assertIsNotNone(answer)
        selector = self.adapter.capture_reads[-1]["selector"]
        self.assertNotEqual(selector, original.selected["bgp"])
        with transaction.atomic():
            fence = fence_for(self.owners[1])
            self.assertEqual((fence.bookkeeping_revision, fence.owner_revision, fence.current_attempt_id), before)
        self._read_bgp_category()
        settle_device_apply_attempts(self.management)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")

    def test_background_observation_preserves_later_automatic_refusal(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts
        from netbox_nso_plugin.reconcile import reconcile_device

        self._prepare_bgp_observation_capture()
        self._serve_bgp_observation()
        puts = len(self.adapter.capture_reads)
        context = reconcile_device(self.device, self.management)
        self.assertEqual(context["_gate"]["bgp"], "ran")
        settle_device_apply_attempts(self.management)
        self.owners[1].refresh_from_db()
        self.assertEqual(self.owners[1].status, "apply_failed")
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")
        self.assertEqual(len(self.adapter.capture_reads), puts)
        self.assertEqual(self.adapter.apply_requests, [])

    def test_older_automatic_carrier_cannot_borrow_unresolved_manual_promotion(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        self.prepare()
        before = {}
        for row in self.owners:
            row.refresh_from_db()
            before[row.pk, row._meta.label_lower] = (
                row.status,
                row.last_apply_at,
                row.last_apply_error,
                row.apply_attempt_id,
            )
        generation = self.terminal(refused=None)
        generation.update(generation_id=80, seq=3, carrier_job_id=500)
        for identity in generation["row_outcomes"] + generation["carrier_job_result"]["settlement_owners"]:
            identity["generation_id"] = 80
            if "carrier" in identity:
                identity["carrier"]["generation_id"] = 80
        self.serve_terminal_evidence(generation)
        settle_device_apply_attempts(self.management)
        for row in self.owners:
            row.refresh_from_db()
            prior = before[row.pk, row._meta.label_lower]
            if prior[0] == "deploying":
                self.assertEqual((row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id), prior)
            else:
                self.assertEqual(row.status, "in_sync")
                self.assertEqual(row.last_apply_at.isoformat(), generation["updated_at"])
        self.assertFalse(self.attempt.overlay_snapshots.exclude(settlement_disposition="unresolved").exists())

    def test_historical_selection_without_owner_revision_cannot_settle(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        self.attempt.overlay_snapshots.update(expected_owner_revision=None)
        with self.assertRaises(EvidenceInvariantError):
            settle_terminal_generation(self.management, self.terminal(), "placeholder-store", settle_seq=1)
        execution = NSOApplyExecution.objects.get()
        self.assertEqual(execution.consumption_state, "unconsumed")
        self.assertTrue(execution.invariant_code)
        self.assertFalse(execution.overlay_snapshots.exists())
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(
                (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id),
                self.prior[row._meta.label_lower, row.pk],
            )

    def test_new_selection_records_initial_zero_owner_revision(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        row = self.owners[-1]
        snapshot = self.attempt.overlay_snapshots.get(model_label=row._meta.label_lower, overlay_pk=row.pk)
        self.assertEqual(snapshot.expected_owner_revision, 0)
        execution = settle_terminal_generation(
            self.management, self.terminal(refused=None), "placeholder-store", settle_seq=1
        )
        self.assertEqual(execution.consumption_state, "consumed")
        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")
        self.assertEqual(row.apply_attempt_id, execution.attempt_id)

    def test_historical_manual_snapshot_without_owner_revision_cannot_settle(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError

        self.prepare()
        row = self.owners[1]
        row.refresh_from_db()
        before = (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id)
        self.attempt.overlay_snapshots.filter(model_label=row._meta.label_lower, overlay_pk=row.pk).update(
            expected_owner_revision=None
        )
        with self.assertRaises(EvidenceInvariantError):
            self.settle(self.terminal(refused=None))
        row.refresh_from_db()
        self.assertEqual((row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id), before)
        execution = NSOApplyExecution.objects.get()
        self.assertEqual(execution.consumption_state, "unconsumed")
        self.assertTrue(execution.invariant_code)
