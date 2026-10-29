# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Identify incomplete execution evidence and fence stale store diagnostics."""

import threading

from django.db import connections
from django.urls import reverse

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.refusal_display import settlement_stall_projection
from netbox_nso_plugin.settlement import consume_static_route_settlements

from ._adapter_http import make_response
from ._outbox_case import mirror_update
from .test_durable_bgp_capture import BGPCaptureCase, _CaptureHandler


class TestSettlementEvidenceBanner(BGPCaptureCase):
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
            "updated_at": "2026-01-02T03:04:05+00:00",
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
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            marker.incarnation = case.feed_incarnation
            if method == "GET" and url.endswith("/jobs"):
                marker.incarnation, page = case.feed_response()
                return make_response(200, page)
            if method == "GET" and url.endswith("/generations"):
                return make_response(200, [])
            if method == "POST" and url.endswith("/deployment-evidence"):
                return make_response(
                    200,
                    {
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
