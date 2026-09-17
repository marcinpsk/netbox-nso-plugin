# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Require adapter document access to follow shared response-shape validation."""

from __future__ import annotations

import ast
from pathlib import Path

from ._ast_scope import scoped_walk

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_CLIENT_PATH = _PACKAGE_ROOT / "adapter_client.py"


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for item in target.elts for name in _target_names(item)]
    return []


def _binding(node):
    if isinstance(node, ast.Assign):
        return [name for target in node.targets for name in _target_names(target)], node.value
    if isinstance(node, ast.AnnAssign):
        return _target_names(node.target), node.value
    if isinstance(node, ast.NamedExpr):
        return _target_names(node.target), node.value
    return [], None


def _binding_source(value):
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id == "_request":
            return "request", None
        if value.func.id == "_document":
            return "document", None
    if isinstance(value, ast.Name):
        return "alias", value.id
    return "other", None


def _risky_names(node):
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
    ):
        return [node.func.value.id]
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return [node.value.id]
    if isinstance(node, ast.Compare) and any(isinstance(operator, ast.In | ast.NotIn) for operator in node.ops):
        operands = [node.left, *node.comparators]
        return [operand.id for operand in operands if isinstance(operand, ast.Name)]
    return []


def _unguarded_document_accesses(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    violations = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef) or not function.name.startswith("get_"):
            continue
        events = []
        for node in scoped_walk(function.body):
            names, value = _binding(node)
            if names:
                kind, source_name = _binding_source(value)
                events.append(
                    (
                        getattr(node, "end_lineno", node.lineno),
                        getattr(node, "end_col_offset", node.col_offset),
                        1,
                        "binding",
                        names,
                        kind,
                        source_name,
                    )
                )
            risky_names = _risky_names(node)
            if risky_names:
                events.append((node.lineno, node.col_offset, 0, "access", risky_names, node.lineno, None))

        bindings = {}
        for _line, _column, _priority, event, names, value, source_name in sorted(events):
            if event == "access":
                for name in names:
                    if bindings.get(name) == "request":
                        violations.append(f"{filename}:{value} {function.name}")
                continue
            for name in names:
                bindings[name] = bindings.get(source_name) if value == "alias" else value
    return violations


def test_get_readers_validate_request_documents_before_dictionary_access():
    filename = str(_CLIENT_PATH.relative_to(_PACKAGE_ROOT.parent))
    violations = _unguarded_document_accesses(_CLIENT_PATH.read_text(encoding="utf-8"), filename)

    assert violations == [], "\n".join(violations)


def test_scan_flags_aliases_and_each_dictionary_access_shape():
    source = """\
def get_example():
    response = _request("GET", "/example")
    alias = response
    response.get("items")
    alias["items"]
    "read_state" in alias
"""

    assert _unguarded_document_accesses(source, "synthetic.py") == [
        "synthetic.py:4 get_example",
        "synthetic.py:5 get_example",
        "synthetic.py:6 get_example",
    ]


def test_scan_requires_validation_before_dictionary_access():
    source = """\
def get_example():
    response = _request("GET", "/example")
    response.get("items")
    response = _document(response, "example document")
    response.get("items")
"""

    assert _unguarded_document_accesses(source, "synthetic.py") == ["synthetic.py:3 get_example"]


def test_scan_allows_document_validation_and_non_request_reassignment():
    source = """\
def get_example():
    response = _request("GET", "/example")
    response = _document(response, "example document")
    response.get("items")
    "read_state" in response
    response["read_state"]
    response = {}
    response.get("items")
"""

    assert _unguarded_document_accesses(source, "synthetic.py") == []


def test_scan_does_not_enter_nested_function_bodies():
    source = """\
def get_example():
    def nested():
        response = _request("GET", "/example")
        return response.get("items")
    return nested()
"""

    assert _unguarded_document_accesses(source, "synthetic.py") == []
