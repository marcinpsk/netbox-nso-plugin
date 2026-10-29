# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Identify incomplete execution evidence and fence stale store diagnostics."""

import copy
import threading
import time

from django.db import connections
from django.urls import reverse

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.refusal_display import settlement_stall_projection
from netbox_nso_plugin.settlement import consume_static_route_settlements

from ._adapter_http import make_generation_listing, make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import mirror_update
from .test_apply_settlement import _attempt, _payload
from .test_durable_bgp_capture import BGPCaptureCase, _CaptureHandler


def _resolve_schema(root, schema):
    if "$ref" in schema:
        for part in schema["$ref"].removeprefix("#/").split("/"):
            root = root[part]
        return root
    return schema


def _required_paths(root, schema, value, path=(), visited=None):
    """Visit one served array or map item for each schema path, including nullable refs."""
    schema = _resolve_schema(root, schema)
    if "anyOf" in schema:
        from jsonschema import Draft202012Validator

        for branch in schema["anyOf"]:
            if Draft202012Validator({"components": root["components"], **branch}).is_valid(value):
                yield from _required_paths(root, branch, value, path, visited)
                return
    if isinstance(value, dict):
        for name in schema.get("required", ()):
            child = schema["properties"][name]
            if visited is not None:
                visited.add((schema["title"], name))
            yield (*path, name), child
            yield from _required_paths(root, child, value[name], (*path, name), visited)
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict) and value:
            name = max(value, key=lambda key: len(str(value[key])))
            yield from _required_paths(root, additional, value[name], (*path, name), visited)
    elif isinstance(value, list) and value and "items" in schema:
        item_index = max(range(len(value)), key=lambda index: len(str(value[index])))
        yield from _required_paths(root, schema["items"], value[item_index], (*path, item_index), visited)


def _structural_mutations(root, schema):
    """Change JSON types only; enum values and semantic content stay out of scope."""
    from jsonschema import Draft202012Validator

    schema = _resolve_schema(root, schema)
    branches = schema.get("anyOf", (schema,))
    types = {_resolve_schema(root, branch).get("type") for branch in branches}
    yield "remove", None
    for kind, value in (
        ("null", None),
        ("array", []),
        ("object", {}),
        ("string", "x"),
        ("integer", 0),
        ("boolean", True),
    ):
        if kind not in types:
            self_validator = Draft202012Validator({"components": root["components"], **schema})
            assert not self_validator.is_valid(value)
            yield kind, value


class TestSettlementEvidenceBanner(BGPSettlementHelpers, BGPCaptureCase):
    def job(self, *, kind="apply", status="failed", result=None):
        return {
            "id": 501,
            "type": kind,
            "status": status,
            "device_id": self.management.adapter_device_id,
            "settle_seq": 42,
            "result": result,
            "row_outcomes": None,
            "refusals": None,
            "error": None,
            "context": None,
            "created_at": "2026-01-02T03:04:05+00:00",
            "updated_at": "2026-01-02T03:04:05+00:00",
            "started_at": None,
            "heartbeat_at": None,
        }

    def serve_feed(self, jobs, incarnation="placeholder-store-a"):
        """Serve each response's store marker on the real HTTP transport."""
        marker = threading.local()
        self.feed_incarnation = incarnation
        self.feed_response = lambda: (incarnation, jobs)
        case = self

        class FeedHandler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", marker.incarnation)
                super().end_headers()

        self.server.RequestHandlerClass = FeedHandler
        original = getattr(self, "_feed_base_handle", self.adapter._handle)
        self._feed_base_handle = original

        def handle(method, url, **kwargs):
            marker.incarnation = case.feed_incarnation
            if method == "GET" and url.endswith("/jobs"):
                marker.incarnation, page = case.feed_response()
                return make_response(200, page)
            if method == "GET" and url.endswith("/generations"):
                return make_response(200, case.generation_page if hasattr(case, "generation_page") else [])
            if method == "POST" and url.endswith("/deployment-evidence"):
                return make_response(
                    200,
                    case.deployment_body
                    if hasattr(case, "deployment_body")
                    else {
                        "device_id": case.management.adapter_device_id,
                        "head": None,
                        "blocked": False,
                        "write_work_pending": False,
                        "held_jobs": [],
                        "pending_generations": 0,
                        "attempts": [],
                        "unknown_apply_attempt_ids": [],
                    },
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        adapter_client.reset_session()

    def settlement_responses(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt = NSOApplyAttempt.objects.create(
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            selected={"bgp": 7},
        )
        attempt.adapter_apply_attempt_id = attempt.pk
        attempt.save(update_fields=("adapter_apply_attempt_id",))
        raw = _attempt(attempt.pk, self.management.adapter_device_id, 1, {"bgp": 7}, "pending")
        generation = raw["generations"][0]
        listing = make_generation_listing(
            generation_id=1,
            seq=1,
            status="pending",
            job_id=901,
            carrier_job_id=901,
            carrier_job_status="queued",
            settlement_cohort=None,
            digest=f"{1:064x}",
            source_push_seq={"bgp": 7},
        )
        head = {
            **generation,
            "mode": "networked",
            "settlement_cohort": None,
            "apply_attempt_id": str(attempt.pk),
            "created_at": generation["updated_at"],
        }
        return [], [listing], _payload(self.management.adapter_device_id, [raw], head=head)

    def serve_settlement_responses(self, feed, generations, deployment):
        self.generation_page = generations
        self.deployment_body = deployment
        self.serve_feed(feed)

    def assert_visible_stall(self):
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.management.refresh_from_db()
        issue = settlement_stall_projection(self.management)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["reason"], self.management.settle_evidence_error["reason"])

    def test_malformed_successful_apply_replays_stall_with_a_banner(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt = NSOApplyAttempt.objects.create(
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            selected={"bgp": 7},
        )
        attempt.adapter_apply_attempt_id = attempt.pk
        attempt.save(update_fields=("adapter_apply_attempt_id",))
        self.deployment_body = _payload(self.management.adapter_device_id, [])
        self.deployment_body["unknown_apply_attempt_ids"] = [str(attempt.pk)]
        self.serve_feed([])
        original = self.adapter._handle
        responses = [make_response(200, body) for body in ({}, None, [], {"outcome": []})]
        responses.extend(make_response(200, content=content) for content in (b"{", b""))
        no_op = {
            "device_id": self.management.adapter_device_id,
            "outcome": "no_op",
            "job_id": None,
            "selected": attempt.selected,
            "skipped": {"bgp": "already_applied"},
            "skipped_detail": None,
            "generations": [],
        }
        for key in ("device_id", "skipped_detail"):
            body = dict(no_op)
            del body[key]
            responses.append(make_response(200, body))
        for response in responses:
            with self.subTest(body=response.content):
                mirror_update(self.management, settle_evidence_error={})
                calls = []

                def handle(method, url, **kwargs):
                    if method == "POST" and url.endswith("/actions/apply"):
                        calls.append(kwargs["json"])
                        return response
                    return original(method, url, **kwargs)

                self.adapter._handle = handle
                self.assert_visible_stall()
                self.assertEqual(len(calls), 1)
                self.assertIsNone(self.management.settle_cursor_seq)
                attempt.refresh_from_db()
                self.assertIsNone(attempt.response)

    def test_malformed_response_bodies_show_a_banner(self):
        feed, generations, deployment = self.settlement_responses()
        cases = [
            ("feed body", {}, generations, deployment),
            ("generation body", feed, [{}], deployment),
        ]
        for field in ("attempts", "unknown_apply_attempt_ids"):
            cases.append((field, feed, generations, {**deployment, field: None}))
        missing_head = copy.deepcopy(deployment)
        del missing_head["head"]
        cases.append(("missing head", feed, generations, missing_head))
        missing_attempt = copy.deepcopy(deployment)
        del missing_attempt["head"]["apply_attempt_id"]
        cases.append(("head attempt", feed, generations, missing_attempt))
        for status in ([], {}):
            broken = copy.deepcopy(deployment)
            broken["attempts"][0]["generations"][0]["status"] = status
            cases.append((f"status {status}", feed, generations, broken))
        for label, jobs, rows, snapshot in cases:
            with self.subTest(case=label):
                mirror_update(self.management, settle_evidence_error={})
                self.serve_settlement_responses(jobs, rows, snapshot)
                self.assert_visible_stall()

    def test_undecodable_success_bodies_stall_with_a_banner(self):
        for method, endpoint in (("GET", "/jobs"), ("GET", "/generations"), ("POST", "/deployment-evidence")):
            for content in (b"{", b""):
                with self.subTest(endpoint=endpoint, content=content):
                    mirror_update(self.management, settle_evidence_error={})
                    self.serve_feed([])
                    original = self.adapter._handle

                    def malformed(verb, url, **kwargs):
                        response = original(verb, url, **kwargs)
                        if verb == method and url.endswith(endpoint):
                            return make_response(200, content=content)
                        return response

                    self.adapter._handle = malformed
                    self.assert_visible_stall()

    def test_open_admission_content_stalls_without_type_errors(self):
        feed, generations, deployment = self.settlement_responses()
        cases = []
        for value in ([], {}):
            for path in (
                ("outcome",),
                ("generations",),
                ("generations", 0, "generation_id"),
                ("generations", 0, "mode"),
            ):
                broken = copy.deepcopy(deployment)
                parent = broken["attempts"][0]["response"]
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                cases.append((path, value, broken))
        for path, value, broken in cases:
            with self.subTest(path=path, value=value):
                mirror_update(self.management, settle_evidence_error={})
                self.serve_settlement_responses(feed, generations, broken)
                self.assert_visible_stall()

    def test_schema_failure_attribution_uses_only_the_failing_carrier_path(self):
        feed, generations, deployment = self.settlement_responses()
        cases = []
        for path in (
            ("head", "updated_at"),
            ("attempts", 0, "generations", 0, "updated_at"),
            ("attempts", 0, "response"),
            ("blocked",),
        ):
            broken = copy.deepcopy(deployment)
            parent = broken
            for key in path[:-1]:
                parent = parent[key]
            del parent[path[-1]]
            cases.append((path, feed, generations, broken, 901 if path[0] == "head" or "generations" in path else None))
        broken_listing = copy.deepcopy(generations)
        del broken_listing[0]["updated_at"]
        cases.append(("listing", feed, broken_listing, deployment, 901))
        for label, jobs, rows, snapshot, job_id in cases:
            with self.subTest(path=label):
                mirror_update(self.management, settle_evidence_error={})
                self.serve_settlement_responses(jobs, rows, snapshot)
                self.assert_visible_stall()
                self.assertEqual(self.management.settle_evidence_error.get("job_id"), job_id)

    def test_required_response_fields_always_stall_with_a_visible_banner(self):
        from netbox_nso_plugin.settlement_schemas import VALIDATORS

        self.prepare()
        generation = self.terminal()

        def add_related(value):
            if isinstance(value, dict):
                if "related" in value:
                    value["related"] = {"family": value["family"], "row": copy.deepcopy(value["row"]), "field": None}
                for child in value.values():
                    add_related(child)
            elif isinstance(value, list):
                for child in value:
                    add_related(child)

        add_related(generation)
        self.serve_terminal_evidence(generation, manual=True)
        original = self.adapter._handle
        base = (
            original("GET", self.server.adapter_url + "/jobs", params={"after_settle_seq": 0}).json(),
            original("GET", self.server.adapter_url + "/generations").json(),
            original(
                "POST",
                self.server.adapter_url + "/deployment-evidence",
                json={"apply_attempt_ids": [str(self.attempt.pk)]},
            ).json(),
            copy.deepcopy(self.attempt.response),
        )
        skipped = next(iter(base[3]["skipped"]))
        base[3]["skipped_detail"] = {skipped: {"generation_id": 80, "seq": 3, "status": "failed"}}
        components = ("JobOut", "DeviceGenerationOut", "DeviceDeploymentEvidenceOut", "ActionApplyOut")
        started = time.monotonic()
        counts = {}
        visited = set()
        for index, component in enumerate(components):
            validator = VALIDATORS[component]
            validator.validate(base[index])
            count = 0
            for path, field_schema in _required_paths(validator.schema, validator.schema, base[index], visited=visited):
                for label, replacement in _structural_mutations(validator.schema, field_schema):
                    with self.subTest(component=component, path=path, mutation=label):
                        bodies = copy.deepcopy(base)
                        parent = bodies[index]
                        for key in path[:-1]:
                            parent = parent[key]
                        if label == "remove":
                            del parent[path[-1]]
                        else:
                            parent[path[-1]] = replacement
                        self.assertFalse(validator.is_valid(bodies[index]))
                        mirror_update(self.management, settle_evidence_error={})
                        if component == "ActionApplyOut":
                            unknown = _payload(self.management.adapter_device_id, [])
                            unknown["unknown_apply_attempt_ids"] = [str(self.attempt.pk)]
                            self.serve_settlement_responses([], [], unknown)
                            evidence_handler = self.adapter._handle

                            def replay(method, url, **kwargs):
                                if method == "POST" and url.endswith("/actions/apply"):
                                    return make_response(200, bodies[3])
                                return evidence_handler(method, url, **kwargs)

                            self.adapter._handle = replay
                        else:
                            self.serve_settlement_responses(*bodies[:3])
                        self.assert_visible_stall()
                        self.assertEqual(
                            self.management.settle_evidence_error["reason"],
                            "invalid job envelope" if index == 0 else "invalid settlement evidence",
                        )
                        self.assertIsNone(self.management.settle_cursor_seq)
                        count += 1
            counts[component] = count
            self.assertGreater(count, 0)
        required = {
            (schema["title"], name)
            for schema in validator.schema["components"]["schemas"].values()
            for name in schema.get("required", ())
        }
        self.assertEqual(visited, required)
        elapsed = time.monotonic() - started
        print(f"settlement structural mutants: {counts}; total={sum(counts.values())}; runtime={elapsed:.2f}s")
        self.adapter._handle = original
        self.serve_terminal_evidence(generation, manual=True)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.drained)
        self.assertEqual(result.cursor, 1)

    def test_terminal_jobs_without_execution_markers_pass_the_feed_validator(self):
        for kind in ("apply", "removal"):
            for status in ("succeeded", "failed"):
                for result in (None, {}, {"pre_execution_failure": True}):
                    with self.subTest(kind=kind, status=status, result=result):
                        job = self.job(kind=kind, status=status, result=result)
                        self.serve_feed([job])
                        served, incarnation = adapter_client.get_settlement_feed(
                            self.management.adapter_device_id, after_settle_seq=0, limit=100
                        )
                        self.assertEqual(served, [job])
                        self.assertEqual(incarnation, "placeholder-store-a")

    def test_nonterminal_and_unrelated_jobs_do_not_require_execution_results(self):
        jobs = [
            self.job(status="queued"),
            self.job(status="running"),
            self.job(kind="removal", status="queued"),
            self.job(kind="removal", status="running"),
            self.job(kind="sync", result={}),
        ]
        self.serve_feed(jobs)
        served, incarnation = adapter_client.get_settlement_feed(
            self.management.adapter_device_id, after_settle_seq=0, limit=100
        )
        self.assertEqual(served, jobs)
        self.assertEqual(incarnation, "placeholder-store-a")

    def test_out_of_order_feed_records_a_closed_evidence_reason(self):
        first = self.job(kind="sync")
        second = {**first, "id": 502, "settle_seq": 41}
        self.serve_feed([first, second])
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        self.management.refresh_from_db()
        self.assertEqual(
            {key: self.management.settle_evidence_error.get(key) for key in ("job_id", "settle_seq", "reason")},
            {"job_id": 502, "settle_seq": 41, "reason": "invalid settlement evidence"},
        )

    def test_missing_feed_fields_show_the_carrier_in_the_evidence_phase(self):
        for field in ("settle_seq", "device_id", "status", "row_outcomes", "result", "error", "refusals", "updated_at"):
            with self.subTest(field=field):
                mirror_update(
                    self.management,
                    settle_cursor_seq=40,
                    settle_cursor_device_id=self.management.adapter_device_id,
                    settle_cursor_incarnation="placeholder-store-a",
                    settle_evidence_error={},
                )
                job = self.job(kind="sync", status="succeeded")
                del job[field]
                self.serve_feed([job])
                self.assert_stalled_carrier_banner(cursor=40)

    def test_missing_feed_fields_show_the_carrier_after_a_concurrent_cursor_advance(self):
        for field in ("settle_seq", "device_id", "status", "row_outcomes", "result", "error", "refusals", "updated_at"):
            with self.subTest(field=field):
                mirror_update(
                    self.management,
                    settle_cursor_seq=40,
                    settle_cursor_device_id=self.management.adapter_device_id,
                    settle_cursor_incarnation="placeholder-store-a",
                    settle_evidence_error={},
                )
                job = self.job(kind="sync", status="succeeded")
                del job[field]
                advanced_job = {**self.job(kind="sync", status="succeeded"), "id": 502, "settle_seq": 41}
                self.serve_feed([])
                calls = 0
                advanced = []
                requests = []
                original = self.adapter._handle

                def feed():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        advanced.append(consume_static_route_settlements(self.management.pk))
                        return "placeholder-store-a", []
                    if calls in (3, 5):
                        return "placeholder-store-a", [advanced_job]
                    return "placeholder-store-a", [job] if calls >= 6 else []

                def handle(method, url, **kwargs):
                    if method == "GET" and url.endswith("/jobs"):
                        requests.append((int(kwargs["params"]["after_settle_seq"]), int(kwargs["params"]["limit"])))
                    return original(method, url, **kwargs)

                self.feed_response = feed
                self.adapter._handle = handle
                self.assert_stalled_carrier_banner(cursor=41)
                self.assertEqual(len(advanced), 1)
                self.assertTrue(advanced[0].drained)
                self.assertEqual(advanced[0].cursor, 41)
                self.assertEqual(requests[:6], [(40, 100), (40, 1), (40, 100), (41, 1), (40, 100), (41, 100)])

    def assert_stalled_carrier_banner(self, *, cursor):
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.assertEqual(result.consumed, 0)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, cursor)
        self.assertEqual(self.management.settle_evidence_error["job_id"], 501)
        self.assertEqual(self.management.settle_evidence_error["reason"], "invalid job envelope")
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertContains(response, "invalid job envelope")

    def test_mistyped_feed_fields_show_the_carrier_banner(self):
        for field, values in (
            ("settle_seq", (None, True, "42", 42.0, [], {}, 0, -1)),
            ("device_id", (None, True, "1558", 1558.0, [], {})),
            ("status", (None, True, 42, [], {})),
            ("updated_at", (None, True, 42, [], {})),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    mirror_update(
                        self.management,
                        settle_cursor_seq=40,
                        settle_cursor_device_id=self.management.adapter_device_id,
                        settle_cursor_incarnation="placeholder-store-a",
                        settle_evidence_error={},
                    )
                    self.serve_feed([{**self.job(kind="sync", status="succeeded"), field: value}])
                    self.assert_stalled_carrier_banner(cursor=40)

    def test_current_ordering_failure_at_the_cursor_keeps_a_rendered_banner(self):
        mirror_update(
            self.management,
            settle_cursor_seq=42,
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation="placeholder-store-a",
        )
        first = self.job(kind="sync", status="succeeded")
        self.serve_feed([first, {**first, "id": 502, "settle_seq": 43}])
        for _retry in range(2):
            self.assertTrue(consume_static_route_settlements(self.management).stalled)
            response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
            self.assertContains(response, "Settlement is paused", count=1)
            self.assertContains(response, "Adapter job 501")
            self.assertContains(response, "settle_seq 42")
            self.assertContains(response, "invalid settlement evidence")
            self.management.refresh_from_db()
            self.assertEqual(self.management.settle_cursor_seq, 42)
            self.assertEqual(self.management.settle_evidence_error["job_id"], 501)
            self.assertEqual(self.management.settle_evidence_error["settle_seq"], 42)

    def test_first_invalid_response_from_a_new_store_remains_visible(self):
        mirror_update(
            self.management,
            settle_cursor_seq=7,
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation="placeholder-store-a",
        )
        self.serve_feed([self.job(result={"row_outcomes": []})], incarnation="placeholder-store-b")
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        self.management.refresh_from_db()
        issue = settlement_stall_projection(self.management)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["store_incarnation"], "placeholder-store-b")
        self.assertEqual(self.management.settle_cursor_incarnation, "placeholder-store-a")

    def test_locked_request_failure_uses_the_cursor_after_another_consumer_advanced(self):
        mirror_update(
            self.management,
            settle_cursor_seq=40,
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation="placeholder-store-a",
        )
        job = self.job(kind="sync", status="succeeded")
        self.serve_feed([])
        calls = 0
        requests = []
        advanced = []
        original = self.adapter._handle

        def feed():
            nonlocal calls
            calls += 1
            if calls == 2:
                advanced.append(consume_static_route_settlements(self.management.pk))
                return "placeholder-store-a", []
            return "placeholder-store-a", [job] if calls in (3, 5, 6) else []

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                requests.append((int(kwargs["params"]["after_settle_seq"]), int(kwargs["params"]["limit"])))
            return original(method, url, **kwargs)

        self.feed_response = feed
        self.adapter._handle = handle
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        self.assertEqual(len(advanced), 1)
        self.assertTrue(advanced[0].drained)
        self.assertEqual(advanced[0].cursor, 42)
        self.assertEqual(requests[-1], (42, 100))
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertContains(response, "settle_seq 42")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_seq, 42)
        self.assertEqual(self.management.settle_evidence_error["job_id"], 501)

    def test_invalid_sequence_keeps_the_known_carrier_banner(self):
        self.serve_feed([{**self.job(kind="sync", status="succeeded"), "settle_seq": 0}])
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(response, "Settlement is paused", count=1)
        self.assertContains(response, "Adapter job 501")
        self.assertContains(response, "invalid job envelope")
        self.assertNotContains(response, "settle_seq")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["job_id"], 501)
        self.assertNotIn("settle_seq", self.management.settle_evidence_error)
        self.assertIsNone(self.management.settle_cursor_seq)

    def test_late_old_store_failure_cannot_restore_a_banner_after_an_empty_store_reset(self):
        mirror_update(
            self.management,
            settle_cursor_seq=7,
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation="placeholder-store-a",
        )
        broken = self.job(result={"row_outcomes": [], "commit_acknowledged": "placeholder-invalid"})
        self.serve_feed([broken])
        started, release = threading.Event(), threading.Event()
        errors, results = [], []

        def delayed_feed():
            started.set()
            if not release.wait(30):
                raise AssertionError("the late feed response was not released")
            return "placeholder-store-a", [broken]

        def consume_old_store():
            try:
                results.append(consume_static_route_settlements(self.management.pk))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        self.feed_response = delayed_feed
        worker = threading.Thread(target=consume_old_store)
        worker.start()
        try:
            self.assertTrue(started.wait(10), "the old-store request did not start")
            self.feed_incarnation = "placeholder-store-b"
            self.feed_response = lambda: ("placeholder-store-b", [])
            reset = consume_static_route_settlements(self.management)
            self.assertTrue(reset.drained)
        finally:
            release.set()
            worker.join(30)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(results[0].stalled)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_cursor_incarnation, "placeholder-store-b")
        self.assertEqual(self.management.settle_evidence_error, {})

    def test_empty_valid_feed_clears_and_hides_an_obsolete_store_banner(self):
        self.serve_feed([], incarnation="placeholder-store-b")
        mirror_update(
            self.management,
            settle_cursor_seq=0,
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation="placeholder-store-b",
            settle_evidence_error={
                "job_id": 501,
                "settle_seq": 42,
                "reason": "missing structured execution evidence",
                "mapping_epoch": mapping_epoch(self.management),
                "store_incarnation": "placeholder-store-a",
            },
        )
        self.assertIsNone(settlement_stall_projection(self.management))
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.drained)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})

    def test_empty_valid_snapshot_clears_an_unidentified_banner(self):
        mirror_update(
            self.management,
            settle_cursor_seq=40,
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation="placeholder-store-a",
        )
        self.serve_feed([])
        responses = iter((("placeholder-store-a", []), ("placeholder-store-b", [])))
        self.feed_response = lambda: next(responses)
        self.assertTrue(consume_static_route_settlements(self.management).stalled)
        self.management.refresh_from_db()
        self.assertIsNotNone(settlement_stall_projection(self.management))
        self.serve_feed([])
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.drained)
        self.assertFalse(result.stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertNotContains(response, "Settlement is paused")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertEqual(self.management.settle_cursor_seq, 40)

    def test_late_unidentified_failure_cannot_restore_a_banner_after_cursor_progress(self):
        mirror_update(
            self.management,
            settle_cursor_seq=40,
            settle_cursor_device_id=self.management.adapter_device_id,
            settle_cursor_incarnation="placeholder-store-a",
        )
        job = self.job(kind="sync", status="succeeded")
        self.serve_feed([job])
        started, release = threading.Event(), threading.Event()
        errors, results = [], []
        calls = 0
        calls_lock = threading.Lock()

        def delayed_feed():
            nonlocal calls
            with calls_lock:
                calls += 1
                current = calls
            if current == 2:
                started.set()
                if not release.wait(30):
                    raise AssertionError("the late snapshot was not released")
                return "placeholder-store-b", []
            return "placeholder-store-a", [job] if current in (3, 5) else []

        def consume_late_snapshot():
            try:
                results.append(consume_static_route_settlements(self.management.pk))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        self.feed_response = delayed_feed
        worker = threading.Thread(target=consume_late_snapshot)
        worker.start()
        try:
            self.assertTrue(started.wait(10), "the late snapshot request did not start")
            advanced = consume_static_route_settlements(self.management)
            self.assertTrue(advanced.drained)
            self.assertEqual(advanced.cursor, 42)
        finally:
            release.set()
            worker.join(30)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(results[0].stalled)
        response = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertNotContains(response, "Settlement is paused")
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})
        self.assertEqual(self.management.settle_cursor_seq, 42)
