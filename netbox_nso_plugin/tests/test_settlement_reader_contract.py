# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Bound the adapter response boundaries in the direct settlement call graph."""

import ast
import json
from contextlib import contextmanager
from importlib.resources import files
from importlib.util import resolve_name
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.settlement_schemas import MAX_NESTING, canonical_uuid

from ._adapter_http import make_evidence_envelope, make_generation_envelope, make_response, make_transport_session

SETTLEMENT_EVIDENCE_READERS = frozenset(
    {
        "get_settlement_feed",
        "list_device_generations",
        "get_deployment_evidence",
        "get_settlement_observation",
        "get_intent_receipts",
        "trigger_apply",
    }
)
ADAPTER_LOCAL_HELPERS = frozenset(
    {
        "EvidenceItem",
        "InvalidJobEvidence",
        "_validated_job",
        "_validated_generation_listing",
        "capture_wire_body",
        "raise_evidence_errors",
        "raise_item_error",
        "static_route_deletions",
    }
)
# Complete JSON that json.loads rejects with RecursionError on an 8 MiB stack; a larger stack decodes it.
DEEP_JSON = b"[" * 100_000 + b"0" + b"]" * 100_000
# Complete JSON that json.loads decodes on any stack but that nests past MAX_NESTING.
NESTED_ARRAY_JSON = b"[" * (MAX_NESTING * 16) + b"0" + b"]" * (MAX_NESTING * 16)
# Complete JSON that decodes but nests past what jsonschema can render on Python 3.14.
NESTED_OBJECT_JSON = b'{"device_id":' + b'{"a":' * 40_000 + b"0" + b"}" * 40_000 + b"}"


def _read_response(reader):
    if reader == "get_settlement_feed":
        return adapter_client.get_settlement_feed(7, after_settle_seq=0, limit=100)
    if reader == "get_intent_receipts":
        return adapter_client.get_intent_receipts(device_id=7)
    if reader == "trigger_apply":
        return adapter_client.trigger_apply(7, UUID(int=1), {"bgp": 1})
    if reader == "get_deployment_evidence":
        return adapter_client.get_deployment_evidence(7, [])
    if reader == "get_settlement_observation":
        return adapter_client.get_settlement_observation(7, job_id=1, after_settle_seq=0, apply_attempt_ids=[])
    assert reader == "list_device_generations"
    return adapter_client.list_device_generations(7)


def _observation(**values):
    """Return an empty SettlementObservationOut for job 1 after cursor 0 on device 7."""
    return {
        "request": {"job_id": 1, "after_settle_seq": 0, "apply_attempt_ids": []},
        "store_incarnation": "00000000-0000-4000-8000-000000000001",
        "device": {"device_id": 7, "netbox_device_id": None, "nso_instance": "placeholder", "nso_device_name": "dev"},
        "job": None,
        "next_feed": None,
        "generations": [],
        "deployment": {
            "device_id": 7,
            "head": None,
            "blocked": False,
            "write_work_pending": False,
            "held_jobs": [],
            "pending_generations": 0,
            "attempts": [],
            "unknown_apply_attempt_ids": [],
        },
        "receipts": [],
        "absent": {"job_id": 1, "apply_attempt_ids": [], "receipt_sections": []},
        "digest": "a" * 64,
        **values,
    }


@contextmanager
def _serve_response(response, *, error=None):
    config = {"url": "http://adapter.invalid", "token": "placeholder", "verify_tls": True, "timeout": 5}
    config["ca_cert_path"] = None
    adapter_client.reset_session()
    try:
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", autospec=True, return_value=config),
            patch(
                "netbox_nso_plugin.adapter_client.requests.Session",
                autospec=True,
                return_value=make_transport_session(response, error=error),
            ),
        ):
            yield
    finally:
        adapter_client.reset_session()


def test_feed_keeps_valid_siblings_and_identified_error_items():
    from .test_adapter_client_ext import _JOB_OUT

    first = {**_JOB_OUT, "device_id": 7, "id": 10, "settle_seq": 1}
    last = {**first, "id": 12, "settle_seq": 3}
    failed = make_evidence_envelope(
        None,
        identity={"id": 11, "device_id": 7, "settle_seq": 2},
        error="execution_contract",
    )
    response = make_response(200, [make_evidence_envelope(first), failed, make_evidence_envelope(last)])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response):
        jobs, incarnation = _read_response("get_settlement_feed")
    assert jobs[0] == first
    assert jobs[2] == last
    assert jobs[1] == {"id": 11, "device_id": 7, "settle_seq": 2}
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(jobs[1])
    assert (raised.value.job_id, raised.value.settle_seq, raised.value.store_incarnation) == (11, 2, incarnation)
    assert raised.value.reason == "invalid job envelope"


def test_feed_projection_wire_failure_preserves_identified_siblings():
    from .test_adapter_client_ext import _JOB_OUT

    first = {**_JOB_OUT, "device_id": 7, "id": 10, "settle_seq": 1}
    failed = {**first, "id": 11, "settle_seq": 2, "context": {"n": 2**63}}
    last = {**first, "id": 12, "settle_seq": 3}
    response = make_response(200, [make_evidence_envelope(job) for job in (first, failed, last)])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response):
        jobs, incarnation = _read_response("get_settlement_feed")
    assert jobs[0] == first
    assert jobs[2] == last
    assert jobs[1] == {"id": 11, "device_id": 7, "settle_seq": 2}
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(jobs[1])
    assert (raised.value.job_id, raised.value.settle_seq, raised.value.store_incarnation) == (11, 2, incarnation)


@pytest.mark.parametrize("sequence", [None, 0, -1], ids=["absent", "zero", "negative"])
def test_feed_unordered_job_preserves_identified_siblings_without_a_sequence(sequence):
    from .test_adapter_client_ext import _JOB_OUT

    first = {**_JOB_OUT, "device_id": 7, "id": 10, "settle_seq": 1}
    failed = {**first, "id": 11, "settle_seq": sequence}
    last = {**first, "id": 12, "settle_seq": 3}
    response = make_response(200, [make_evidence_envelope(job) for job in (first, failed, last)])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response):
        jobs, incarnation = _read_response("get_settlement_feed")
    assert jobs[0] == first
    assert jobs[2] == last
    assert jobs[1].evidence_identity == {"id": 11, "device_id": 7, "settle_seq": sequence}
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(jobs[1])
    assert raised.value.job_id == 11
    assert raised.value.settle_seq is None
    assert raised.value.store_incarnation == incarnation
    assert raised.value.reason == "invalid job envelope"


@pytest.mark.parametrize("field", ["device_id", "settle_seq"])
def test_feed_projection_identity_float_preserves_trusted_integer_identity(field):
    from .test_adapter_client_ext import _JOB_OUT

    first = {**_JOB_OUT, "device_id": 7, "id": 10, "settle_seq": 1}
    failed = {**first, "id": 11, "settle_seq": 2}
    last = {**first, "id": 12, "settle_seq": 3}
    item = make_evidence_envelope(failed)
    item["projection"] = {**failed, field: float(failed[field])}
    response = make_response(200, [make_evidence_envelope(first), item, make_evidence_envelope(last)])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response):
        jobs, incarnation = _read_response("get_settlement_feed")
    assert jobs[0] == first
    assert jobs[2] == last
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(jobs[1])
    assert (raised.value.job_id, raised.value.settle_seq, raised.value.store_incarnation) == (11, 2, incarnation)
    assert type(jobs[1][field]) is int
    assert raised.value.reason == "invalid job envelope"


def test_feed_independent_device_identity_float_is_untrusted():
    from .test_adapter_client_ext import _JOB_OUT

    job = {**_JOB_OUT, "device_id": 7, "settle_seq": 1}
    item = make_evidence_envelope(job, identity={"id": job["id"], "device_id": 7.0, "settle_seq": 1})
    response = make_response(200, [item])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response), pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _read_response("get_settlement_feed")
    assert raised.value.job_id is None
    assert raised.value.settle_seq is None
    assert raised.value.reason == "invalid job envelope"


def test_feed_semantically_invalid_job_is_an_item_failure():
    from .test_adapter_client_ext import _JOB_OUT

    job = {**_JOB_OUT, "device_id": 7, "type": "", "settle_seq": 1}
    response = make_response(200, [make_evidence_envelope(job)])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response):
        jobs, _ = _read_response("get_settlement_feed")
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(jobs[0])
    assert raised.value.job_id == job["id"]
    assert raised.value.reason == "invalid job envelope"


def test_strict_evidence_consumers_refuse_nested_failed_items():
    identity = {"device_id": 7, "apply_attempt_id": str(UUID(int=1))}
    generation = {
        "device_id": 7,
        "generation_id": 81,
        "seq": 4,
        "carrier_job_id": 501,
        "apply_attempt_id": identity["apply_attempt_id"],
    }
    failed = make_evidence_envelope(None, identity=generation, error="execution_contract")
    attempt = make_evidence_envelope(
        None, identity=identity, error="attempt_generation_membership", generations=[failed]
    )
    body = {
        "device_id": 7,
        "head": None,
        "blocked": False,
        "write_work_pending": False,
        "held_jobs": [],
        "pending_generations": 0,
        "attempts": [attempt],
        "unknown_apply_attempt_ids": [],
    }
    with _serve_response(make_response(200, body)):
        evidence = _read_response("get_deployment_evidence")
    assert evidence["attempts"][0].evidence_generations[0] == generation
    assert evidence["attempts"][0].evidence_identity == identity
    assert evidence["attempts"][0].evidence_digest == "a" * 64
    assert evidence["attempts"][0].evidence_generations[0].evidence_identity == generation
    assert evidence["attempts"][0].evidence_generations[0].evidence_digest == "a" * 64
    with pytest.raises(adapter_client.InvalidJobEvidence):
        evidence["attempts"][0]["generations"]
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_evidence_errors(evidence)
    assert raised.value.reason == "invalid settlement evidence"


@pytest.mark.parametrize(
    "failure",
    [
        "schema",
        "identity",
        "error_digest",
        "unknown_code",
        "missing_error_code",
        "missing_error_digest",
        "both",
        "neither",
    ],
)
def test_job_envelope_failures_use_only_the_stored_identity(failure):
    from .test_adapter_client_ext import _JOB_OUT

    job = {**_JOB_OUT, "device_id": 7, "settle_seq": 1}
    envelope = make_evidence_envelope(job)
    if failure == "schema":
        envelope["projection"] = {**job, "context": "invalid"}
    elif failure == "identity":
        envelope["projection"] = {**job, "id": job["id"] + 1, "settle_seq": 2}
    elif failure == "error_digest":
        envelope["projection"] = None
        envelope["evidence_error"] = {"code": "execution_contract", "digest": "b" * 64}
    elif failure == "unknown_code":
        envelope["projection"] = None
        envelope["evidence_error"] = {"code": "unknown_failure", "digest": "a" * 64}
    elif failure.startswith("missing_error_"):
        envelope["projection"] = None
        envelope["evidence_error"] = {"code": "execution_contract", "digest": "a" * 64}
        del envelope["evidence_error"][failure.removeprefix("missing_error_")]
    elif failure == "both":
        envelope["evidence_error"] = {"code": "execution_contract", "digest": "a" * 64}
    else:
        envelope["projection"] = None
    response = make_response(200, [envelope])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response):
        items, _ = _read_response("get_settlement_feed")
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(items[0])
    assert (raised.value.job_id, raised.value.settle_seq) == (job["id"], 1)


@pytest.mark.parametrize("field", ["id", "device_id", "settle_seq"])
def test_job_envelope_requires_independent_identity_fields(field):
    identity = {"id": 10, "device_id": 7, "settle_seq": 1}
    del identity[field]
    envelope = make_evidence_envelope(None, identity=identity, error="execution_contract")
    response = make_response(200, [envelope])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response), pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _read_response("get_settlement_feed")
    assert raised.value.job_id is None
    assert raised.value.reason == "invalid job envelope"


def test_failed_item_survives_copy_without_losing_its_identity():
    from copy import deepcopy

    error = adapter_client.InvalidJobEvidence({"id": 10, "device_id": 7, "settle_seq": 1}, "invalid job envelope")
    error.store_incarnation = str(UUID(int=1))
    item = adapter_client.EvidenceItem({"id": 10, "device_id": 7, "settle_seq": 1}, error, digest="a" * 64)
    copied = deepcopy(item)
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(copied)
    assert raised.value is not error
    assert raised.value.store_incarnation == error.store_incarnation
    assert copied.evidence_digest == "a" * 64


@pytest.mark.parametrize(
    "access",
    [
        lambda item: item["status"],
        lambda item: item.get("status"),
        lambda item: item.get("status", "pending"),
        lambda item: "status" in item,
        lambda item: iter(item),
        lambda item: item.keys(),
        lambda item: item.values(),
        lambda item: item.items(),
        lambda item: dict(item),
        lambda item: {**item},
    ],
    ids=["index", "get", "default", "membership", "iteration", "keys", "values", "items", "dict", "unpack"],
)
def test_failed_item_projection_access_raises_its_typed_error(access):
    identity = {"id": 502, "device_id": 7, "settle_seq": 2}
    error = adapter_client.InvalidJobEvidence(identity, "invalid job envelope")
    item = adapter_client.EvidenceItem(identity, error, digest="b" * 64)
    assert item["id"] == item.get("id") == 502
    assert "id" in item
    assert item.evidence_identity == identity
    assert item.evidence_digest == "b" * 64
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        access(item)
    assert raised.value is error


def test_strict_evidence_validation_traverses_nested_projection_collections():
    error = adapter_client.InvalidJobEvidence({"id": 501, "device_id": 7}, "invalid settlement evidence")
    item = adapter_client.EvidenceItem({"carrier_job_id": 501}, error, digest="a" * 64)
    document = {"head": None, "attempts": [{"generations": [{"generation_id": 80}, item]}]}
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_evidence_errors(document)
    assert raised.value is error


def test_generation_order_failure_preserves_the_valid_prefix():
    from ._adapter_http import make_generation_listing

    first = make_generation_listing(generation_id=80, seq=3, job_id=500, carrier_job_id=500)
    failed = make_generation_listing(generation_id=80, seq=4)
    last = make_generation_listing(generation_id=82, seq=5, job_id=502, carrier_job_id=502)
    page = [make_generation_envelope(item) for item in (first, failed, last)]
    with _serve_response(make_response(200, page)):
        rows = _read_response("list_device_generations")
    assert rows[0] == first
    assert len(rows) == 2
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(rows[1])
    assert raised.value.job_id == 501
    assert rows[1]["seq"] == 4
    assert rows[1].evidence_identity == page[1]["identity"]
    assert rows[1].evidence_digest == page[1]["digest"]


@pytest.mark.parametrize("reader", sorted(SETTLEMENT_EVIDENCE_READERS))
@pytest.mark.parametrize(
    "content",
    [b"", b"{", b"<html>placeholder</html>", DEEP_JSON, NESTED_ARRAY_JSON],
    ids=["empty", "truncated", "html", "deep", "nested"],
)
def test_reader_undecodable_or_unbounded_bodies_keep_the_declared_caller_contract(reader, content):
    response = make_response(200, content=content)
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    if reader == "trigger_apply" and content == b"":
        with _serve_response(response):
            assert _read_response(reader) is None
        return
    with _serve_response(response), pytest.raises(adapter_client.AdapterError) as raised:
        _read_response(reader)
    assert raised.value.code == "invalid_response"
    if reader in {"trigger_apply", "get_intent_receipts"}:
        assert type(raised.value) is adapter_client.AdapterError
    else:
        assert isinstance(raised.value, adapter_client.InvalidJobEvidence)
        assert raised.value.device_id == 7
        assert raised.value.job_id is None
        assert raised.value.settle_seq is None
        assert raised.value.reason == (
            "invalid job envelope" if reader == "get_settlement_feed" else "invalid settlement evidence"
        )


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "x" * 65,
        "placeholder-invalid",
        "00000000000040008000000000000001",
        "{00000000-0000-4000-8000-000000000001}",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_feed_header_failures_are_device_only_evidence(header):
    response = make_response(200, [])
    if header is not None:
        response.headers["X-Store-Incarnation"] = header
    with _serve_response(response), pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _read_response("get_settlement_feed")
    assert raised.value.device_id == 7
    assert raised.value.job_id is None
    assert raised.value.settle_seq is None
    assert raised.value.store_incarnation is None
    assert raised.value.reason == "invalid settlement evidence"


def _nested(depth):
    value = 0
    for _ in range(depth):
        value = {"a": value}
    return value


def _deployment_envelope_page():
    attempt_id = str(UUID(int=1))
    generation = {
        "generation_id": 81,
        "seq": 4,
        "status": "settled",
        "sections": ["bgp"],
        "source_push_seq": {"bgp": 1},
        "stream_revisions": {"bgp": 7},
        "carrier_job_id": 501,
        "carrier_job_status": "succeeded",
        "carrier_job_result": {},
        "row_outcomes": None,
        "refusals": None,
        "carrier_job_error": None,
        "updated_at": "2026-10-20T10:01:00+00:00",
    }
    head = {
        **generation,
        "mode": "networked",
        "settlement_cohort": 9,
        "apply_attempt_id": attempt_id,
        "created_at": "2026-10-20T10:00:00+00:00",
    }
    attempt = {
        "apply_attempt_id": attempt_id,
        "admission_state": "admitted",
        "http_status": 202,
        "response": {},
        "selected": {"bgp": 1},
        "generation_ids": [81, 82],
    }
    later = {**generation, "generation_id": 82, "seq": 5, "carrier_job_id": 502}
    return {
        "device_id": 7,
        "head": make_generation_envelope(head),
        "blocked": False,
        "write_work_pending": False,
        "held_jobs": [],
        "pending_generations": 0,
        "attempts": [
            make_evidence_envelope(
                attempt,
                identity={"device_id": 7, "apply_attempt_id": attempt_id},
                generations=[make_generation_envelope(row) for row in (generation, later)],
            )
        ],
        "unknown_apply_attempt_ids": [],
    }


def _deployment_target(body, location):
    if location == "head":
        return body["head"]
    if location == "attempt":
        return body["attempts"][0]
    return body["attempts"][0]["generations"][1]


@pytest.mark.parametrize(
    "invalid_uuid", ["not-a-uuid", "ABCDEFAB-1234-4567-89AB-ABCDEFABCDEF", "abcdefab1234456789ababcdefabcdef"]
)
@pytest.mark.parametrize("location", ["head", "attempt", "generation"])
def test_deployment_invalid_identity_uuid_refuses_the_response(location, invalid_uuid):
    body = _deployment_envelope_page()
    target = _deployment_target(body, location)
    target["identity"]["apply_attempt_id"] = invalid_uuid
    target["projection"]["apply_attempt_id"] = invalid_uuid
    with _serve_response(make_response(200, body)), pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _read_response("get_deployment_evidence")
    assert raised.value.reason == "invalid settlement evidence"
    assert raised.value.job_id is None
    assert raised.value.settle_seq is None


@pytest.mark.parametrize(
    "invalid_uuid", ["not-a-uuid", "ABCDEFAB-1234-4567-89AB-ABCDEFABCDEF", "abcdefab1234456789ababcdefabcdef"]
)
@pytest.mark.parametrize("location", ["head", "attempt"])
def test_deployment_invalid_projection_uuid_keeps_the_item_identity(location, invalid_uuid):
    body = _deployment_envelope_page()
    target = _deployment_target(body, location)
    target["projection"]["apply_attempt_id"] = invalid_uuid
    with _serve_response(make_response(200, body)):
        evidence = _read_response("get_deployment_evidence")
    decoded = _deployment_target(evidence, location)
    assert decoded.evidence_identity == target["identity"]
    assert decoded.evidence_digest == target["digest"]
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(decoded)
    assert raised.value.reason == "invalid settlement evidence"
    children = (
        evidence["attempts"][0].evidence_generations
        if location == "attempt"
        else evidence["attempts"][0]["generations"]
    )
    for child in children:
        adapter_client.raise_item_error(child)


@pytest.mark.parametrize(
    "invalid_uuid", ["not-a-uuid", "ABCDEFAB-1234-4567-89AB-ABCDEFABCDEF", "abcdefab1234456789ababcdefabcdef"]
)
def test_deployment_invalid_unknown_attempt_uuid_refuses_the_response(invalid_uuid):
    body = _deployment_envelope_page()
    body["unknown_apply_attempt_ids"] = [invalid_uuid]
    with _serve_response(make_response(200, body)), pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _read_response("get_deployment_evidence")
    assert raised.value.reason == "invalid settlement evidence"
    assert raised.value.job_id is None


@pytest.mark.parametrize("location,projection_depth", [("head", 3), ("attempt", 4), ("generation", 6)])
@pytest.mark.parametrize("failure", [None, "depth", "integer"])
def test_deployment_item_wire_bounds_use_the_absolute_response_depth(location, projection_depth, failure):
    body = _deployment_envelope_page()
    target = _deployment_target(body, location)
    field = "response" if location == "attempt" else "carrier_job_result"
    payload = {"n": 2**63} if failure == "integer" else _nested(MAX_NESTING - projection_depth + (failure == "depth"))
    target["projection"][field] = payload
    with _serve_response(make_response(200, body)):
        evidence = _read_response("get_deployment_evidence")
    decoded = _deployment_target(evidence, location)
    attempt = evidence["attempts"][0]
    children = (
        attempt.evidence_generations if isinstance(attempt, adapter_client.EvidenceItem) else attempt["generations"]
    )
    assert children[0] == body["attempts"][0]["generations"][0]["projection"]
    adapter_client.raise_item_error(children[0])
    if location != "generation":
        assert children[1] == body["attempts"][0]["generations"][1]["projection"]
        adapter_client.raise_item_error(children[1])
    if failure is None:
        adapter_client.raise_item_error(decoded)
        assert decoded[field] == payload
    else:
        assert decoded.evidence_identity == target["identity"]
        with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
            adapter_client.raise_item_error(decoded)
        assert raised.value.reason == "invalid settlement evidence"


@pytest.mark.parametrize("field", ["digest", "evidence_error"])
@pytest.mark.parametrize("value", [{"n": 2**63}, "\x00", "\ud800"])
def test_item_wire_diagnostics_discard_unsafe_envelope_metadata(field, value):
    from netbox_nso_plugin.settlement_schemas import within_wire_bounds

    identity = {"id": 10, "device_id": 7, "settle_seq": 1}
    item = make_evidence_envelope(None, identity=identity, error="execution_contract")
    item[field] = value
    response = make_response(200, [item])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response):
        jobs, _ = _read_response("get_settlement_feed")
    assert within_wire_bounds(jobs[0].evidence_identity)
    assert jobs[0].evidence_digest == (None if field == "digest" else "a" * 64)
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(jobs[0])
    assert raised.value.job_id == 10
    assert raised.value.reason == "invalid job envelope"


@pytest.mark.parametrize("reader", ["get_settlement_feed", "list_device_generations", "get_deployment_evidence"])
def test_readers_reject_decodable_bodies_nested_past_the_bound(reader):
    response = make_response(200, content=NESTED_OBJECT_JSON)
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    with _serve_response(response), pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _read_response(reader)
    assert raised.value.device_id == 7
    assert raised.value.job_id is None
    assert raised.value.settle_seq is None


def test_schema_nesting_bound_accepts_the_limit_and_rejects_one_more_level():
    from netbox_nso_plugin.settlement_schemas import validate_settlement_response

    from .test_adapter_client_ext import _JOB_OUT

    # The page, envelope and projection take three levels; the result fills the rest.
    job = {**_JOB_OUT, "device_id": 7, "row_outcomes": None, "refusals": None}
    at_limit = {**job, "result": _nested(MAX_NESTING - 3)}
    assert validate_settlement_response("JobEvidenceOut", [make_evidence_envelope(at_limit)], 7) == [at_limit]
    over = [make_evidence_envelope({**job, "result": _nested(MAX_NESTING - 2)})]
    items = validate_settlement_response("JobEvidenceOut", over, 7)
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(items[0])
    assert raised.value.job_id == job["id"]
    assert raised.value.reason == "invalid job envelope"


INT64_MAX = 2**63 - 1


@pytest.mark.parametrize("value", [INT64_MAX + 1, -INT64_MAX - 2], ids=["above", "below"])
def test_schema_integer_bound_accepts_int64_and_rejects_values_outside_it(value):
    from netbox_nso_plugin.settlement_schemas import validate_settlement_response

    from .test_adapter_client_ext import _JOB_OUT

    job = {**_JOB_OUT, "device_id": 7}
    at_limit = [{**job, "settle_seq": INT64_MAX, "result": {**job["result"], "n": -INT64_MAX - 1}}]
    assert (
        validate_settlement_response("JobEvidenceOut", [make_evidence_envelope(row) for row in at_limit], 7) == at_limit
    )
    identity_failure = [{**job, "settle_seq": value}]
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        validate_settlement_response("JobEvidenceOut", [make_evidence_envelope(row) for row in identity_failure], 7)
    assert raised.value.job_id is None
    assert raised.value.reason == "invalid job envelope"
    projection_failure = [{**job, "result": {**job["result"], "n": value}}]
    items = validate_settlement_response(
        "JobEvidenceOut", [make_evidence_envelope(row) for row in projection_failure], 7
    )
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(items[0])
    assert raised.value.job_id == job["id"]
    assert raised.value.reason == "invalid job envelope"


@pytest.mark.parametrize("reader", sorted(SETTLEMENT_EVIDENCE_READERS))
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/json; charset=utf-8"])
def test_readers_decode_valid_json_without_a_content_type_precondition(reader, content_type):
    bodies = {
        "get_settlement_feed": [],
        "list_device_generations": [],
        "get_deployment_evidence": {
            "device_id": 7,
            "head": None,
            "blocked": False,
            "write_work_pending": False,
            "held_jobs": [],
            "pending_generations": 0,
            "attempts": [],
            "unknown_apply_attempt_ids": [],
        },
        "get_settlement_observation": _observation(),
        "get_intent_receipts": {"receipts": [], "global_max_push_seq": None, "global_max_route_id": None},
        "trigger_apply": {
            "device_id": 7,
            "outcome": "no_op",
            "job_id": None,
            "selected": {"bgp": 1},
            "skipped": {"bgp": "already_applied"},
            "skipped_detail": None,
            "generations": [],
        },
    }
    assert bodies.keys() == SETTLEMENT_EVIDENCE_READERS
    response = make_response(200, bodies[reader])
    response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000001"
    if content_type is not None:
        response.headers["Content-Type"] = content_type
    with _serve_response(response):
        result = _read_response(reader)
    assert result == (
        ([], "00000000-0000-4000-8000-000000000001") if reader == "get_settlement_feed" else bodies[reader]
    )


@pytest.mark.parametrize("reader", sorted(SETTLEMENT_EVIDENCE_READERS))
@pytest.mark.parametrize("failure", ["http", "transport"])
def test_reader_http_and_transport_failures_keep_the_existing_error(reader, failure):
    import requests

    response = make_response(503, content=b"placeholder unavailable")
    error = requests.exceptions.ReadTimeout() if failure == "transport" else None
    with _serve_response(response, error=error), pytest.raises(adapter_client.AdapterError) as raised:
        _read_response(reader)
    assert type(raised.value) is adapter_client.AdapterError
    assert raised.value.code == ("nso_timeout" if error else "503")
    assert raised.value.status_code == (None if error else 503)


def _import_bindings(nodes, module):
    bindings = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            source = resolve_name("." * node.level + (node.module or ""), module.rpartition(".")[0])
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{source}.{alias.name}"
    return bindings


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _settlement_adapter_calls(sources, entry):
    """Follow named function calls and import aliases; omit methods and dynamic dispatch."""
    trees = {name: ast.parse(source) for name, source in sources.items()}
    functions = {
        f"{module}.{node.name}": (module, node)
        for module, tree in trees.items()
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    pending, visited, readers = [entry], set(), set()
    while pending:
        target = pending.pop()
        if target in visited:
            continue
        visited.add(target)
        module, function = functions[target]
        bindings = _import_bindings(trees[module].body, module)
        bindings.update(_import_bindings(ast.walk(function), module))
        for node in ast.walk(function):
            name = _call_name(node.func) if isinstance(node, ast.Call) else None
            if not name:
                continue
            first, separator, rest = name.partition(".")
            target = bindings.get(first, f"{module}.{first}") + (separator + rest if separator else "")
            if target.startswith("netbox_nso_plugin.adapter_client."):
                readers.add(target.rsplit(".", 1)[1])
            elif target in functions:
                pending.append(target)
    return readers


def test_settlement_adapter_calls_have_declared_evidence_readers():
    root = Path(__file__).resolve().parents[1]
    sources = {f"netbox_nso_plugin.{path.stem}": path.read_text(encoding="utf-8") for path in root.glob("*.py")}
    assert _settlement_adapter_calls(sources, "netbox_nso_plugin.settlement.consume_static_route_settlements") == (
        SETTLEMENT_EVIDENCE_READERS | ADAPTER_LOCAL_HELPERS
    )


def test_reader_guard_detects_a_new_reader_through_import_aliases():
    sources = {
        "netbox_nso_plugin.settlement": "from .helpers import load\ndef consume():\n    load()\n",
        "netbox_nso_plugin.helpers": (
            "def load():\n"
            "    from . import adapter_client as client\n"
            "    from .adapter_client import get_intent_receipts as receipts\n"
            "    receipts()\n"
            "    client.new_reader()\n"
        ),
    }
    readers = _settlement_adapter_calls(sources, "netbox_nso_plugin.settlement.consume")
    assert readers == {"get_intent_receipts", "new_reader"}
    assert readers - SETTLEMENT_EVIDENCE_READERS == {"new_reader"}


@pytest.mark.parametrize("depth", [MAX_NESTING - 3, MAX_NESTING - 2, 600])
def test_receipt_nesting_bound_keeps_the_reader_exception_contract(depth):
    from netbox_nso_plugin.intent_receipt_contract import validate_intent_receipts
    from netbox_nso_plugin.refusal_contract import ContractError

    receipt = {
        "device_id": 7,
        "section": "bgp",
        "push_seq": 1,
        "request_digest": "a" * 64,
        "store_only": False,
        "delete_origin": False,
        "backfill_only": False,
        "status_code": 200,
        "response": _nested(depth),
        "generation_id": 1,
        "created_at": "2026-01-02T03:04:05+00:00",
        "updated_at": None,
    }
    document = {"receipts": [receipt], "global_max_push_seq": None, "global_max_route_id": None}
    response = make_response(200, document)
    if depth == MAX_NESTING - 3:
        assert validate_intent_receipts(document) is document
        with _serve_response(response):
            assert _read_response("get_intent_receipts") == document
    else:
        with pytest.raises(ContractError, match="invalid receipt bounds"):
            validate_intent_receipts(document)
        with _serve_response(response), pytest.raises(adapter_client.AdapterError) as raised:
            _read_response("get_intent_receipts")
        assert type(raised.value) is adapter_client.AdapterError
        assert raised.value.code == "invalid_response"


@pytest.mark.parametrize(
    "failure",
    [
        "provenance",
        "timestamp",
        "carrier",
        "order",
        "unpaired_carrier_status",
        "settled_failed_carrier",
        "settled_missing_carrier",
    ],
)
def test_generation_semantic_failures_keep_trusted_item_identity(failure):
    from ._adapter_http import make_generation_listing

    row = make_generation_listing()
    if failure == "provenance":
        row["source_push_seq"] = {}
    elif failure == "timestamp":
        row["updated_at"] = "2026-01-02T03:04:05"
    elif failure == "carrier":
        row["job_id"] += 1
    elif failure == "settled_failed_carrier":
        row["carrier_job_status"] = "failed"
    elif failure == "settled_missing_carrier":
        row.update(carrier_job_id=None, carrier_job_status=None)
    elif failure == "unpaired_carrier_status":
        row.update(status="failed", carrier_job_id=None)
    page = [row, {**row, "seq": row["seq"] + 1}] if failure == "order" else [row]
    response = make_response(200, [make_generation_envelope(item) for item in page])
    with _serve_response(response):
        items = _read_response("list_device_generations")
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(items[-1])
    assert raised.value.device_id == 7
    assert raised.value.job_id == row["carrier_job_id"]
    assert raised.value.reason == "invalid settlement evidence"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation_id", 0),
        ("generation_id", True),
        ("seq", -1),
        ("seq", False),
        ("status", "invalid"),
        ("job_id", 0),
        ("job_id", True),
        ("carrier_job_id", -1),
        ("carrier_job_id", True),
        ("settlement_cohort", 0),
        ("settlement_cohort", False),
        ("stream_revisions", {"bgp": 0}),
        ("stream_revisions", {"bgp": True}),
        ("stream_revisions", {"bgp": None}),
        ("stream_revisions", []),
        ("source_push_seq", {"bgp": -1}),
        ("source_push_seq", {"bgp": False}),
        ("source_push_seq", []),
        ("source_push_seq", {}),
        ("created_at", "invalid"),
        ("created_at", "2026-01-02T03:04:05"),
        ("updated_at", "invalid"),
        ("updated_at", "2026-01-02T03:04:05"),
        ("carrier_job_status", "partial"),
        ("carrier_job_status", None),
        ("job_id", 502),
    ],
)
def test_generation_listing_rejects_invalid_shared_and_admission_metadata(field, value):
    from ._adapter_http import make_generation_listing

    row = make_generation_listing(status="failed")
    envelope = make_generation_envelope(row)
    envelope["projection"] = {**row, field: value}
    with _serve_response(make_response(200, [envelope])):
        [item] = adapter_client.list_device_generations(7)
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(item)
    assert raised.value.reason == "invalid settlement evidence"
    assert raised.value.device_id == 7
    assert raised.value.job_id == row["carrier_job_id"]
    assert item.evidence_identity == envelope["identity"]


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"status": "pending", "job_id": 502},
        {"status": "running", "job_id": 502},
        {"status": "failed", "carrier_job_id": None, "carrier_job_status": None},
        {"status": "outcome_unknown", "carrier_job_status": "failed"},
        {"status": "abandoned", "job_id": None},
        {"source_push_seq": {"bgp": None}},
        {"source_push_seq": {"bgp": 1, "vlan": None}},
        {"settlement_cohort": None},
        {"created_at": "2026-01-02T03:04:05Z", "updated_at": "2026-01-02T03:04:05.123456Z"},
    ],
)
def test_generation_listing_accepts_valid_shared_and_admission_metadata(metadata):
    from ._adapter_http import make_generation_listing

    row = make_generation_listing(**metadata)
    with _serve_response(make_response(200, [make_generation_envelope(row)])):
        [item] = adapter_client.list_device_generations(7)
    adapter_client.raise_item_error(item)
    assert item == row


JSONB_REJECTED = {
    "nul value": {"detail": "\x00"},
    "nul key": {"\x00": 1},
    "lone surrogate value": {"detail": "\ud800"},
    "lone surrogate key": {"\ud800": 1},
    "infinite number": {"n": float("inf")},
    "not a number": {"n": float("nan")},
}


@pytest.mark.parametrize("case", sorted(JSONB_REJECTED))
def test_schema_bound_rejects_values_postgresql_jsonb_cannot_store(case):
    from netbox_nso_plugin.settlement_schemas import validate_settlement_response

    from .test_adapter_client_ext import _JOB_OUT

    stored = [
        {**_JOB_OUT, "device_id": 7, "result": {**_JOB_OUT["result"], "detail": "caf\u00e9 \U0001f600", "n": 1.5}}
    ]
    assert validate_settlement_response("JobEvidenceOut", [make_evidence_envelope(row) for row in stored], 7) == stored
    page = [{**_JOB_OUT, "device_id": 7, "result": {**_JOB_OUT["result"], **JSONB_REJECTED[case]}}]
    items = validate_settlement_response("JobEvidenceOut", [make_evidence_envelope(row) for row in page], 7)
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(items[0])
    assert raised.value.job_id == _JOB_OUT["id"]
    assert raised.value.reason == "invalid job envelope"


@pytest.mark.parametrize("case", sorted(JSONB_REJECTED))
def test_receipt_jsonb_bound_keeps_the_reader_exception_contract(case):
    from netbox_nso_plugin.intent_receipt_contract import validate_intent_receipts
    from netbox_nso_plugin.refusal_contract import ContractError

    receipt = {
        "device_id": 7,
        "section": "bgp",
        "push_seq": 1,
        "request_digest": "a" * 64,
        "store_only": False,
        "delete_origin": False,
        "backfill_only": False,
        "status_code": 200,
        "response": JSONB_REJECTED[case],
        "generation_id": 1,
        "created_at": "2026-01-02T03:04:05+00:00",
        "updated_at": None,
    }
    with pytest.raises(ContractError, match="invalid receipt bounds"):
        validate_intent_receipts({"receipts": [receipt], "global_max_push_seq": None, "global_max_route_id": None})


def test_apply_skip_reasons_follow_the_vendored_schema():
    from netbox_nso_plugin.apply_settlement import apply_stream_partition
    from netbox_nso_plugin.settlement_schemas import _SCHEMA

    schema = _SCHEMA["components"]["schemas"]["ActionApplyOut"]["properties"]["skipped"]["additionalProperties"]
    for reason in schema["enum"]:
        selected, skipped, error = apply_stream_partition(
            {"selected": {"bgp": 1}, "skipped": {"bgp": reason}}, {"bgp": 1}
        )
        assert (selected, skipped, error) == ({"bgp": 1}, {"bgp": reason}, None), reason


@pytest.mark.parametrize("field", ["push_seq", "response"])
def test_receipt_integer_bound_keeps_the_reader_exception_contract(field):
    from netbox_nso_plugin.intent_receipt_contract import validate_intent_receipts
    from netbox_nso_plugin.refusal_contract import ContractError

    receipt = {
        "device_id": 7,
        "section": "bgp",
        "push_seq": 1,
        "request_digest": "a" * 64,
        "store_only": False,
        "delete_origin": False,
        "backfill_only": False,
        "status_code": 200,
        "response": {"count": 1},
        "generation_id": 1,
        "created_at": "2026-01-02T03:04:05+00:00",
        "updated_at": None,
    }
    assert validate_intent_receipts({"receipts": [receipt], "global_max_push_seq": None, "global_max_route_id": None})
    receipt[field] = INT64_MAX + 1 if field == "push_seq" else {"count": INT64_MAX + 1}
    document = {"receipts": [receipt], "global_max_push_seq": None, "global_max_route_id": None}
    with pytest.raises(ContractError):
        validate_intent_receipts(document)
    with _serve_response(make_response(200, document)), pytest.raises(adapter_client.AdapterError) as raised:
        adapter_client.get_intent_receipts(device_id=7)
    assert type(raised.value) is adapter_client.AdapterError
    assert raised.value.code == "invalid_response"


def test_settlement_schema_publishes_observation_and_evidence_roots():
    schemas = json.loads(files("netbox_nso_plugin").joinpath("schemas/settlements.json").read_text(encoding="utf-8"))[
        "components"
    ]["schemas"]
    assert {
        "SettlementObservationIn",
        "SettlementObservationOut",
        "JobEvidenceOut",
        "DeviceGenerationEvidenceOut",
        "DeploymentGenerationEvidenceOut",
        "DeploymentHeadEvidenceOut",
        "DeploymentAttemptEvidenceOut",
        "SettlementReceiptEvidenceOut",
    } <= schemas.keys()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("abcdefab-1234-4567-89ab-abcdefabcdef", True),
        ("ABCDEFAB-1234-4567-89AB-ABCDEFABCDEF", False),
        ("abcdefab1234456789ababcdefabcdef", False),
        ("not-a-uuid", False),
        (UUID(int=1), False),
    ],
)
def test_canonical_uuid_accepts_only_the_canonical_string(value, expected):
    assert canonical_uuid(value) is expected


def _observe(body):
    response = make_response(200, body)
    with _serve_response(response):
        return _read_response("get_settlement_observation")


def test_observation_keeps_identified_item_failures_beside_valid_siblings():
    from ._adapter_http import make_generation_listing

    job = make_evidence_envelope(None, identity={"id": 1, "device_id": 7, "settle_seq": 2}, error="execution_contract")
    listing = make_generation_listing(generation_id=81, seq=4, job_id=1, carrier_job_id=1)
    failed = make_evidence_envelope(
        None,
        identity={"device_id": 7, "generation_id": 82, "seq": 5, "carrier_job_id": 1, "apply_attempt_id": None},
        error="projection_construction",
        digest="b" * 64,
    )
    valid = make_generation_envelope(listing)
    observation = _observe(
        _observation(
            job=job,
            next_feed={"id": 1, "device_id": 7, "settle_seq": 2},
            generations=[valid, failed],
            absent={"job_id": None, "apply_attempt_ids": [], "receipt_sections": []},
        )
    )
    assert observation["job"].evidence_identity == {"id": 1, "device_id": 7, "settle_seq": 2}
    assert observation["generations"][0] == listing
    assert observation["generations"][0].evidence_digest == "a" * 64
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        adapter_client.raise_item_error(observation["generations"][1])
    assert raised.value.job_id == 1
    assert observation["generations"][1].evidence_digest == "b" * 64


@pytest.mark.parametrize(
    "values",
    [
        {"request": {"job_id": 2, "after_settle_seq": 0, "apply_attempt_ids": []}},
        {"request": {"job_id": 1, "after_settle_seq": 3, "apply_attempt_ids": []}},
        {"absent": {"job_id": None, "apply_attempt_ids": [], "receipt_sections": []}},
        {"absent": {"job_id": 2, "apply_attempt_ids": [], "receipt_sections": []}},
        {"next_feed": {"id": 3, "device_id": 7, "settle_seq": 0}},
        {"next_feed": {"id": 3, "device_id": 8, "settle_seq": 4}},
        {"device": {"device_id": 8, "netbox_device_id": None, "nso_instance": "placeholder", "nso_device_name": "d"}},
        {"store_incarnation": "placeholder-store"},
        {"digest": "not-a-digest"},
    ],
    ids=[
        "other-job",
        "other-cursor",
        "missing-absence",
        "foreign-absence",
        "next-feed-behind-cursor",
        "next-feed-other-device",
        "other-device",
        "non-uuid-store",
        "malformed-digest",
    ],
)
def test_observation_frame_failures_refuse_the_whole_response(values):
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _observe(_observation(**values))
    assert raised.value.device_id == 7
    assert raised.value.job_id is None


def test_observation_request_names_each_attempt_once_in_order():
    from uuid import UUID

    attempts = [UUID(int=2), UUID(int=1), UUID(int=2)]
    body = _observation(
        request={"job_id": 1, "after_settle_seq": 0, "apply_attempt_ids": [str(UUID(int=1)), str(UUID(int=2))]}
    )
    response = make_response(200, body)
    session = make_transport_session(response)
    config = {"url": "http://adapter.invalid", "token": "placeholder", "verify_tls": True, "timeout": 5}
    config["ca_cert_path"] = None
    adapter_client.reset_session()
    try:
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", autospec=True, return_value=config),
            patch("netbox_nso_plugin.adapter_client.requests.Session", autospec=True, return_value=session),
        ):
            adapter_client.get_settlement_observation(7, job_id=1, after_settle_seq=0, apply_attempt_ids=attempts)
    finally:
        adapter_client.reset_session()
    sent = session.send.call_args.args[0]
    assert sent.url == "http://adapter.invalid/api/v1/devices/7/settlement-observation"
    assert json.loads(sent.body) == body["request"]
