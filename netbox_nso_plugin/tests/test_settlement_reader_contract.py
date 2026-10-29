# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Bound the adapter response boundaries in the direct settlement call graph."""

import ast
from importlib.util import resolve_name
from pathlib import Path

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
