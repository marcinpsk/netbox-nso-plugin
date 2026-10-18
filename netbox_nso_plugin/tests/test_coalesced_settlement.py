# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""One carrier can certify several independent frozen generations atomically."""

import copy

import pytest

from .test_durable_bgp_capture import BGPCaptureCase
from .test_refusal_contract import generation
from .test_refusal_result import refused_bgp_result


def coalesced_generations():
    from netbox_nso_plugin.refusal_protocol import row_ref

    result = refused_bgp_result()
    result["commit_acknowledged"] = True
    result["refusals"] = []
    for row in result["row_outcomes"]:
        row.update(disposition="in_sync", refusals=[])
    for row in result["settlement_owners"]:
        row["disposition"] = "in_sync"
    result["bgp_count_by_outcome"] = {"in_sync": 1, "apply_failed": 0, "not_applied": 0, "unavailable": 0}
    ref = row_ref("vlan", "vlan.vlan", (("vlan", (71,)),))
    carrier = {
        "stream": "vlan",
        "row": {"subject": ref.subject, "key": ref.key},
        "source_push_seq": 42,
        "frozen_revision": 8,
        "accepted_at": None,
        "generation_id": 18,
        "settlement_cohort": 10,
    }
    logical = {key: value for key, value in carrier.items() if key != "accepted_at"}
    logical.update(acceptance_identity=None, carrier=carrier, disposition="in_sync", refusals=[])
    result["row_outcomes"].append(logical)
    result["settlement_owners"].append({**carrier, "disposition": "in_sync"})
    result["vlan_count_by_outcome"] = {"in_sync": 1, "apply_failed": 0, "not_applied": 0, "unavailable": 0}
    first = generation()
    first.update(carrier_job_result=result, row_outcomes=result["row_outcomes"], refusals=[])
    second = copy.deepcopy(first)
    second.update(
        generation_id=18, seq=10, sections=["vlan"], stream_revisions={"vlan": 8}, source_push_seq={"vlan": 42}
    )
    return [first, second]


def test_coalesced_carrier_accepts_each_generation_authority():
    from netbox_nso_plugin.refusal_contract import validate_generation

    for authority in coalesced_generations():
        assert validate_generation(authority) is authority


def test_coalesced_carrier_requires_complete_generation_authority():
    from netbox_nso_plugin.refusal_contract import ContractError, validate_carrier_generations

    with pytest.raises(ContractError):
        validate_carrier_generations(coalesced_generations()[:1])


def test_coalesced_carrier_rejects_changed_secondary_authority():
    from netbox_nso_plugin.refusal_contract import ContractError, validate_carrier_generations

    generations = coalesced_generations()
    generations[1]["stream_revisions"]["vlan"] = 99
    with pytest.raises(ContractError):
        validate_carrier_generations(generations)


class TestCoalescedOwnerSettlement(BGPCaptureCase):
    def prepare_carrier(self):
        from uuid import UUID

        from django.urls import reverse

        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._outbox_case import own_vlan
        from .test_apply_selector import _promoted
        from .test_refusal_settlement_families import TestRefusalSettlementFamilies

        self.owners = [self.router_owner(), own_vlan(self.management, 71, "coalesced")]
        for row in self.owners:
            self.bookkeeping(row)
        self.prior = {
            (row._meta.label_lower, row.pk): (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id)
            for row in self.owners
        }

        def admission(selected):
            response = _promoted(selected)
            for link in response["generations"]:
                streams = {stream for stream in selected if (stream == "bgp") == (link["generation_id"] == 81)}
                for key in ("source_push_seq", "stream_revisions"):
                    link[key] = {stream: value for stream, value in link[key].items() if stream in streams}
                link.update(job_id=501, mode="networked")
            return 202, response

        self.adapter.apply_response = admission
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        first = TestRefusalSettlementFamilies.evidence(
            self, dispositions={"bgp.router": "in_sync", "vlan.vlan": "in_sync"}
        )
        result = first["carrier_job_result"]
        for identity in result["row_outcomes"] + result["settlement_owners"]:
            if identity["stream"] != "bgp":
                identity["generation_id"] = 82
                identity["settlement_cohort"] = 10
                if "carrier" in identity:
                    identity["carrier"]["generation_id"] = 82
                    identity["carrier"]["settlement_cohort"] = 10
        second = copy.deepcopy(first)
        from netbox_nso_plugin.refusal_registry import SECTION_COUNTER_KEYS, document_section_for_stream

        for target, link in zip((first, second), self.attempt.response["generations"], strict=True):
            target.update(
                generation_id=link["generation_id"],
                seq=link["seq"],
                stream_revisions=link["stream_revisions"],
                source_push_seq=link["source_push_seq"],
                sections=sorted({document_section_for_stream(stream) for stream in link["stream_revisions"]}),
            )
        for authority in (first, second):
            for section in authority["sections"]:
                for counter in SECTION_COUNTER_KEYS[section]:
                    result.setdefault(counter, {"in_sync": 0, "apply_failed": 0, "not_applied": 0, "unavailable": 0})
        second["carrier_job_result"] = copy.deepcopy(result)
        return [first, second]

    def settle_carrier(self, generations, *, settle_seq=1):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        from .test_apply_settlement import _payload

        evidence = _payload(
            self.management.adapter_device_id,
            [
                {
                    "apply_attempt_id": str(self.attempt.pk),
                    "admission_state": "admitted",
                    "http_status": 202,
                    "response": self.attempt.response,
                    "generations": generations,
                }
            ],
        )
        evidence.settlement_sequences = {} if settle_seq is None else {501: settle_seq}
        settle_apply_attempts(self.management, evidence)

    def test_feed_binds_every_coalesced_certificate_before_advancing_the_cursor(self):
        from netbox_nso_plugin.models import NSOApplyExecution
        from netbox_nso_plugin.settlement import settle_static_routes

        from ._adapter_http import make_response
        from .test_refusal_settlement_runtime import TestRefusalSettlementRuntime

        generations = self.prepare_carrier()
        self.settle_carrier(generations, settle_seq=None)
        self.assertEqual(list(NSOApplyExecution.objects.values_list("settle_seq", flat=True)), [None, None])
        TestRefusalSettlementRuntime.serve_terminal_evidence(self, generations[0])
        original = self.adapter._handle

        def complete_metadata(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                metadata = response.json()
                metadata.append(
                    {
                        **metadata[0],
                        **{
                            key: generations[1][key]
                            for key in ("generation_id", "seq", "status", "stream_revisions", "source_push_seq")
                        },
                    }
                )
                return make_response(200, metadata)
            return response

        self.adapter._handle = complete_metadata
        self.assertEqual(settle_static_routes(self.management).cursor, 1)
        self.assertEqual(list(NSOApplyExecution.objects.values_list("settle_seq", flat=True)), [1, 1])
        self.assertFalse(settle_static_routes(self.management).stalled)

    def test_coalesced_bgp_and_vlan_settle_as_one_certified_carrier(self):
        from netbox_nso_plugin.models import NSOApplyExecution

        generations = self.prepare_carrier()
        self.settle_carrier(generations)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "in_sync")
        self.assertEqual(NSOApplyExecution.objects.filter(consumption_state="consumed").count(), 2)

    def test_invalid_secondary_generation_leaves_both_owners_deploying(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError
        from netbox_nso_plugin.models import NSOApplyExecution

        generations = self.prepare_carrier()
        generations[1]["stream_revisions"]["vlan"] = 999
        with self.assertRaises(EvidenceInvariantError):
            self.settle_carrier(generations)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "deploying")
        self.assertFalse(NSOApplyExecution.objects.filter(consumption_state="consumed").exists())

    def test_late_secondary_write_failure_rolls_back_primary_generation(self):
        from django.db import connection

        from netbox_nso_plugin.models import NSOVLANState

        generations = self.prepare_carrier()

        def reject_vlan(execute, sql, params, many, context):
            if sql.startswith("UPDATE") and NSOVLANState._meta.db_table in sql:
                raise RuntimeError("Certified VLAN write rejected by the test")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_vlan), self.assertRaisesRegex(RuntimeError, "Certified VLAN write"):
            self.settle_carrier(generations)
        for row in self.owners:
            row.refresh_from_db()
            self.assertEqual(row.status, "deploying")
