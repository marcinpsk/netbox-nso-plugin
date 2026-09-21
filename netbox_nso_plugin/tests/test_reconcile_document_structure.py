# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Keep authoritative document validation separate from resolution defaults and skips."""

from __future__ import annotations

import ast
from pathlib import Path

from ._ast_scope import resolve_call_target, scope_bindings

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_NO_DEFAULT = object()


class _SyntheticSource:
    def __init__(self, name, source):
        self.name = name
        self.source = source

    def read_text(self, encoding=None):
        return self.source

    def __str__(self):
        return self.name

    def relative_to(self, _root):
        return self.name


def _module_constants(tree):
    constants = {}
    for statement in tree.body:
        targets = []
        value = None
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id.lstrip("_").isupper():
                constants[target.id] = value
    return constants


def _defaulted_constant_lookups(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants = _module_constants(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        default = (
            node.args[1]
            if len(node.args) > 1
            else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "default"),
                _NO_DEFAULT,
            )
        )
        key = node.args[0] if node.args else None
        constant = constants.get(owner.id) if isinstance(owner, ast.Name) else None
        mapping_members = [*constant.keys, *constant.values] if isinstance(constant, ast.Dict) else []
        literal_mapping = isinstance(constant, ast.Dict) and all(
            isinstance(member, ast.Constant) for member in mapping_members
        )
        mapping = ast.literal_eval(constant) if literal_mapping else {}
        preserves_unmapped_sentinel = (
            literal_mapping
            and isinstance(default, ast.Constant)
            and isinstance(key, ast.BoolOp)
            and isinstance(key.op, ast.Or)
            and isinstance(key.values[-1], ast.Constant)
            and key.values[-1].value == default.value
            and default.value not in mapping
            and default.value not in mapping.values()
        )
        if (
            node.func.attr == "get"
            and isinstance(owner, ast.Name)
            and owner.id in constants
            and default is not _NO_DEFAULT
            and not preserves_unmapped_sentinel
        ):
            violations.append(f"{path.relative_to(_PACKAGE_ROOT.parent)}:{node.lineno}")
    return violations


_ADAPTER_ERROR_TARGET = "netbox_nso_plugin.adapter_client.AdapterError"


def _raises_adapter_error(node, bindings):
    if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
        return False
    return resolve_call_target(node.exc, bindings) == _ADAPTER_ERROR_TARGET


def _validation_and_skip_loops(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bindings = scope_bindings(tree)
    violations = []
    for loop in (node for node in ast.walk(tree) if isinstance(node, (ast.For, ast.AsyncFor))):
        descendants = [node for statement in loop.body for node in ast.walk(statement)]
        if not any(isinstance(node, ast.Continue) for node in descendants):
            continue
        for raised in (node for node in descendants if _raises_adapter_error(node, bindings[node])):
            violations.append(
                f"{path.relative_to(_PACKAGE_ROOT.parent)}:{raised.lineno} "
                f"shares a loop with continue at line {loop.lineno}"
            )
    return violations


def test_reconcilers_do_not_default_module_constant_lookups():
    violations = []
    for path in sorted(_PACKAGE_ROOT.glob("*_reconciler.py")):
        violations.extend(_defaulted_constant_lookups(path))
    assert violations == []


def test_defaulted_constant_lookup_rejects_name_bound_mapping_values():
    source = _SyntheticSource(
        "synthetic_reconciler.py",
        'TYPE = "vpls"\n_MAP = {"epipe": TYPE}\n_MAP.get(service_type or "vpls", "vpls")',
    )

    assert _defaulted_constant_lookups(source) == ["synthetic_reconciler.py:3"]


def test_defaulted_constant_lookup_rejects_mapping_unpacking():
    source = _SyntheticSource(
        "synthetic_reconciler.py",
        'BASE = {"epipe": "vpws"}\n_MAP = {**BASE}\n_MAP.get(service_type or "", "")',
    )

    assert _defaulted_constant_lookups(source) == ["synthetic_reconciler.py:3"]


def test_defaulted_constant_lookup_allows_a_literal_unmapped_sentinel():
    source = _SyntheticSource(
        "synthetic_reconciler.py",
        '_MAP = {"access": "access", "trunk": "tagged"}\n_MAP.get(mode or "", "")',
    )

    assert _defaulted_constant_lookups(source) == []


def test_adapter_error_import_aliases_are_checked_and_guarded_aliases_are_allowed():
    source = _SyntheticSource(
        "snippet.py",
        """\
import netbox_nso_plugin.adapter_client as client
from netbox_nso_plugin.adapter_client import AdapterError as PayloadError
from .adapter_client import AdapterError as RelativePayloadError

def module_alias(items):
    for item in items:
        if not item:
            continue
        raise client.AdapterError("invalid")

def symbol_alias(items):
    for item in items:
        if not item:
            continue
        raise PayloadError("invalid")

def relative_alias(items):
    for item in items:
        if not item:
            continue
        raise RelativePayloadError("invalid")

def guarded_alias(items):
    for item in items:
        raise PayloadError("invalid")
""",
    )

    assert _validation_and_skip_loops(source) == [
        "snippet.py:9 shares a loop with continue at line 6",
        "snippet.py:15 shares a loop with continue at line 12",
        "snippet.py:21 shares a loop with continue at line 18",
    ]


def test_reconciler_validation_is_separate_from_resolution_skips():
    violations = []
    for path in sorted(_PACKAGE_ROOT.glob("*_reconciler.py")):
        violations.extend(_validation_and_skip_loops(path))
    assert violations == []
