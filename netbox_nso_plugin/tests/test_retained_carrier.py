# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Certify retained carriers against the complete adapter listing."""

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.models import NSOApplyExecution

from ._adapter_http import make_generation_listing, make_response
from ._bgp_settlement import BGPSettlementHelpers
from .test_durable_bgp_capture import BGPCaptureCase


class TestRetainedCarrier(BGPSettlementHelpers, BGPCaptureCase):
    def serve_listing(self, rows):
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/generations"):
                limit = int(kwargs["params"]["limit"])
                offset = limit if "since_seq" in kwargs["params"] else 0
                return make_response(200, rows[offset : offset + limit])
            return original(method, url, **kwargs)

        self.adapter._handle = handle

    def test_listing_rejects_every_missing_field_and_invalid_value_before_writes(self):
        from netbox_nso_plugin.refusal_display import settlement_stall_projection
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        from ._outbox_case import mirror_update

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal())
        valid = make_generation_listing(source_push_seq=self.attempt.selected)
        malformed = [{key: value for key, value in valid.items() if key != missing} for missing in valid]
        for key, values in {
            "generation_id": [True, 0, -1, "81"],
            "seq": [True, 0, -1, "4"],
            "job_id": [True, 0, -1, "501"],
            "carrier_job_id": [True, 0, -1, "501", 502, None],
            "carrier_job_status": [None, "unknown", True, []],
            "status": ["unknown", None, [], True],
            "mode": ["unknown", None, [], True],
            "settlement_cohort": [True, 0, -1, "9"],
            "digest": [None, 123, {}],
            "stream_revisions": [None, [], {"bgp": True}, {"bgp": 0}, {"bgp": -1}, {"bgp": "7"}],
            "source_push_seq": [None, [], {"bgp": True}, {"bgp": 0}, {"bgp": -1}, {"bgp": "1"}, {}],
            "created_at": [None, 123, "invalid", "2026-10-20T10:00:00"],
            "updated_at": [None, 123, "invalid", "2026-10-20T10:00:00"],
        }.items():
            malformed.extend({**valid, key: value} for value in values)
        for row in malformed:
            with self.subTest(row=row):
                self.serve_listing([row])
                mirror_update(self.management, settle_evidence_error={})
                result = consume_static_route_settlements(self.management)
                self.assertTrue(result.stalled)
                self.assertFalse(result.drained)
                self.assertFalse(NSOApplyExecution.objects.exists())
                self.management.refresh_from_db()
                self.assertIsNone(self.management.settle_cursor_seq)
                issue = settlement_stall_projection(self.management)
                self.assertIsNotNone(issue)
                self.assertEqual(issue["reason"], "invalid settlement evidence")

    def test_malformed_later_page_is_rejected_before_writes(self):
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal())
        rows = [
            make_generation_listing(
                generation_id=seq, seq=seq, status="pending", carrier_job_id=None, carrier_job_status=None
            )
            for seq in range(1, adapter_client._GENERATION_PAGE_LIMIT + 1)
        ]
        rows.append(make_generation_listing(generation_id=1001, seq=1001, mode="unknown"))
        self.serve_listing(rows)
        with self.assertRaises(adapter_client.AdapterError) as raised:
            load_deployment_evidence(self.management)
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertFalse(NSOApplyExecution.objects.exists())

    def test_empty_target_reissue_listing_is_valid(self):
        row = make_generation_listing(stream_revisions={}, source_push_seq={"bgp": None})
        self.serve_listing([row])
        self.assertEqual(adapter_client.list_device_generations(self.management.adapter_device_id), [row])

    def test_null_snapshot_blocks_until_the_terminal_carrier_is_retained(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts

        self.prepare_automatic()
        generation = self.terminal(refused=None)
        self.serve_terminal_evidence(generation)
        for job_id in (501, None):
            with self.subTest(job_id=job_id):
                self.serve_listing(
                    [
                        make_generation_listing(
                            job_id=job_id,
                            carrier_job_id=None,
                            carrier_job_status=None,
                            status=generation["status"],
                            source_push_seq=generation["source_push_seq"],
                        )
                    ]
                )
                with self.assertRaisesRegex(EvidenceInvariantError, "missing_generation_evidence"):
                    settle_device_apply_attempts(self.management)
                self.assertFalse(NSOApplyExecution.objects.filter(consumed_at__isnull=False).exists())
                self.management.refresh_from_db()
                self.assertIsNone(self.management.settle_cursor_seq)
        self.serve_listing(
            [
                make_generation_listing(
                    status=generation["status"],
                    source_push_seq=generation["source_push_seq"],
                    updated_at=generation["updated_at"],
                )
            ]
        )
        from netbox_nso_plugin.settlement import settle_static_routes

        result = settle_static_routes(self.management)
        self.assertEqual((result.consumed, result.cursor), (1, 1))
        self.assertEqual(NSOApplyExecution.objects.get(carrier_job_id=501).consumption_state, "consumed")

    def assert_raw_metadata_reconciled(self, *, manual):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence

        self.prepare() if manual else self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal(), manual=manual, head=not manual)
        original = self.adapter._handle
        change = {}

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                row = document["attempts"][0]["generations"][0] if manual else document["head"]
                row.update(change)
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        for change in (
            {"seq": 5},
            {"stream_revisions": {"bgp": 8}},
            {"source_push_seq": {"bgp": 999}},
            {"carrier_job_status": "failed"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(EvidenceInvariantError):
                    load_deployment_evidence(self.management)
                self.assertFalse(NSOApplyExecution.objects.exists())

    def test_raw_head_metadata_is_compared_before_binding(self):
        self.assert_raw_metadata_reconciled(manual=False)

    def test_raw_manual_metadata_is_compared_with_the_listing(self):
        self.assert_raw_metadata_reconciled(manual=True)

    def test_consumed_certificate_without_order_blocks_after_job_deletion(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation

        self.prepare_automatic()
        generation = self.terminal()
        execution = settle_terminal_generation(self.management, generation, "00000000-0000-4000-8000-000000000004")
        self.assertIsNone(execution.settle_seq)
        self.serve_terminal_evidence(generation, head=False, pruned=True)
        with self.assertRaisesRegex(EvidenceInvariantError, "missing_generation_evidence"):
            load_deployment_evidence(self.management)
        execution.refresh_from_db()
        self.assertIsNone(execution.settle_seq)
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)

    def test_null_terminal_carrier_records_the_missing_evidence_diagnostic(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_device_apply_attempts

        self.prepare_automatic()
        self.serve_terminal_evidence(self.terminal(refused=None), head=False, pruned=True)
        self.serve_listing(
            [
                make_generation_listing(
                    status="outcome_unknown",
                    job_id=501,
                    carrier_job_id=None,
                    carrier_job_status=None,
                    source_push_seq=self.attempt.selected,
                )
            ]
        )
        with self.assertLogs("netbox_nso_plugin.apply_settlement", level="WARNING") as recorded:
            with self.assertRaisesRegex(EvidenceInvariantError, "missing_generation_evidence"):
                settle_device_apply_attempts(self.management)
        self.assertIn("missing_generation_evidence", recorded.output[0])
        self.assertFalse(NSOApplyExecution.objects.exists())
        self.management.refresh_from_db()
        self.assertIsNone(self.management.settle_cursor_seq)

    def test_provenance_only_source_keys_do_not_add_settlement_targets(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        self.prepare_automatic()
        generation = self.terminal()
        generation["source_push_seq"]["logging"] = 999
        self.serve_terminal_evidence(generation)
        settle_device_apply_attempts(self.management)
        self.assertEqual(NSOApplyExecution.objects.get().consumption_state, "consumed")

    def test_active_retry_can_keep_its_previous_terminal_carrier_snapshot(self):
        for status in ("pending", "running"):
            with self.subTest(status=status):
                row = make_generation_listing(status=status, job_id=502, carrier_job_id=501)
                self.serve_listing([row])
                self.assertEqual(adapter_client.list_device_generations(self.management.adapter_device_id), [row])
