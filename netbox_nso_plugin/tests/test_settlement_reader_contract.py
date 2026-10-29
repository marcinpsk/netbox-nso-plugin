# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Bound the adapter response boundaries in the direct settlement call graph."""

import ast
from contextlib import contextmanager
from importlib.util import resolve_name
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.settlement_schemas import MAX_NESTING

from ._adapter_http import make_response, make_transport_session

SETTLEMENT_EVIDENCE_READERS = frozenset(
    {
        "get_settlement_feed",
        "list_device_generations",
        "get_deployment_evidence",
        "get_intent_receipts",
        "trigger_apply",
    }
)
ADAPTER_LOCAL_HELPERS = frozenset({"InvalidJobEvidence", "capture_wire_body", "static_route_deletions"})
# Complete JSON that json.loads rejects with RecursionError, not ValueError.
DEEP_JSON = b"[" * 100_000 + b"0" + b"]" * 100_000
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
    assert reader == "list_device_generations"
    return adapter_client.list_device_generations(7)


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


@pytest.mark.parametrize("reader", sorted(SETTLEMENT_EVIDENCE_READERS))
@pytest.mark.parametrize(
    "content", [b"", b"{", b"<html>placeholder</html>", DEEP_JSON], ids=["empty", "truncated", "html", "deep"]
)
def test_reader_json_decode_failures_keep_the_declared_caller_contract(reader, content):
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

    # The page and the job take two levels; the result fills the rest.
    at_limit = [{**_JOB_OUT, "result": _nested(MAX_NESTING - 2)}]
    assert validate_settlement_response("JobOut", at_limit, 7) is at_limit
    over = [{**_JOB_OUT, "result": _nested(MAX_NESTING - 1)}]
    with pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        validate_settlement_response("JobOut", over, 7)
    assert raised.value.job_id is None
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
        with pytest.raises(ContractError, match="invalid receipt nesting"):
            validate_intent_receipts(document)
        with _serve_response(response), pytest.raises(adapter_client.AdapterError) as raised:
            _read_response("get_intent_receipts")
        assert type(raised.value) is adapter_client.AdapterError
        assert raised.value.code == "invalid_response"


@pytest.mark.parametrize("failure", ["provenance", "timestamp", "carrier", "order"])
def test_generation_semantic_failures_keep_the_reader_exception_contract(failure):
    from ._adapter_http import make_generation_listing

    row = make_generation_listing()
    if failure == "provenance":
        row["source_push_seq"] = {}
    elif failure == "timestamp":
        row["updated_at"] = "2026-01-02T03:04:05"
    elif failure == "carrier":
        row["job_id"] += 1
    page = [row, {**row, "seq": row["seq"] + 1}] if failure == "order" else [row]
    with _serve_response(make_response(200, page)), pytest.raises(adapter_client.InvalidJobEvidence) as raised:
        _read_response("list_device_generations")
    assert raised.value.device_id == 7
    assert raised.value.job_id == row["carrier_job_id"]
    assert raised.value.reason == "invalid settlement evidence"
