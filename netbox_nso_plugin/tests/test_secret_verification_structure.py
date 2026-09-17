# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require secret verification callers to reject the wrong Vault reference kind."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

from ._ast_scope import scoped_walk

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "netbox_nso_plugin"
_VERIFY_FUNCTIONS = {"adapter_client.verify_secret", "verify_secret"}
_PARSE_FUNCTIONS = {"parse_vault_ref", "vault_refs.parse_vault_ref"}
_ERROR_TYPES = {"VaultRefError", "vault_refs.VaultRefError"}


def _production_modules():
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative_path = path.relative_to(_PACKAGE_ROOT)
        if "tests" not in relative_path.parts and "migrations" not in relative_path.parts:
            yield path


def _is_call_to(node: ast.AST, functions: set[str]) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func) in functions


def _verification_sites_in_module(tree: ast.Module):
    function_types = ast.FunctionDef | ast.AsyncFunctionDef
    for function in (node for node in ast.walk(tree) if isinstance(node, function_types)):
        for node in scoped_walk(function.body):
            if _is_call_to(node, _VERIFY_FUNCTIONS):
                yield node, function


def _verification_sites():
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for verification, function in _verification_sites_in_module(tree):
            yield path, verification, function


def _has_literal_polarity(call: ast.Call) -> bool:
    return any(
        keyword.arg == "require_key" and isinstance(keyword.value, ast.Constant) and type(keyword.value.value) is bool
        for keyword in call.keywords
    )


def _rejects_vault_ref_error(node: ast.Try) -> bool:
    return any(
        handler.type is not None
        and ast.unparse(handler.type) in _ERROR_TYPES
        and bool(handler.body)
        and isinstance(handler.body[-1], ast.Return)
        for handler in node.handlers
    )


def _statement_list_chain(node: ast.AST, target: ast.AST) -> list[tuple[list[ast.stmt], int]] | None:
    if node is target:
        return []
    for _field, value in ast.iter_fields(node):
        if isinstance(value, list):
            for index, child in enumerate(value):
                if not isinstance(child, ast.AST):
                    continue
                child_chain = _statement_list_chain(child, target)
                if child_chain is None:
                    continue
                if all(isinstance(item, ast.stmt) for item in value):
                    return [(value, index), *child_chain]
                return child_chain
        elif isinstance(value, ast.AST):
            child_chain = _statement_list_chain(value, target)
            if child_chain is not None:
                return child_chain
    return None


def _is_validation_try(node: ast.stmt, verification_argument: str) -> bool:
    if not isinstance(node, ast.Try) or len(node.body) != 1:
        return False
    statement = node.body[0]
    if not isinstance(statement, ast.Expr) or not _is_call_to(statement.value, _PARSE_FUNCTIONS):
        return False
    parse_call = statement.value
    return bool(
        parse_call.args
        and ast.unparse(parse_call.args[0]) == verification_argument
        and _has_literal_polarity(parse_call)
        and _rejects_vault_ref_error(node)
    )


def _has_prior_validation(verification: ast.Call, function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if not verification.args:
        return False
    verification_argument = ast.unparse(verification.args[0])
    chain = _statement_list_chain(function, verification)
    return chain is not None and any(
        _is_validation_try(statement, verification_argument)
        for statements, verification_index in chain
        for statement in statements[:verification_index]
    )


def _violations_in_module(tree: ast.Module, label: str | Path) -> list[str]:
    return [
        f"{label}:{verification.lineno}"
        for verification, function in _verification_sites_in_module(tree)
        if not _has_prior_validation(verification, function)
    ]


class TestSecretVerificationStructure(SimpleTestCase):
    def test_secret_verification_callers_validate_reference_kind(self):
        violations = []
        for path in _production_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(_violations_in_module(tree, path.relative_to(_REPO_ROOT)))

        self.assertEqual(violations, [], "\n".join(violations))

    def test_secret_verification_scan_finds_callers(self):
        self.assertGreaterEqual(len(list(_verification_sites())), 2)

    def test_conditional_validation_does_not_guard_later_verification(self):
        tree = ast.parse(
            """\
def verify(ref):
    if False:
        try:
            parse_vault_ref(ref, require_key=False)
        except VaultRefError:
            return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:7"])

    def test_validation_of_different_argument_does_not_guard_verification(self):
        tree = ast.parse(
            """\
def verify(ref, other):
    try:
        parse_vault_ref(other, require_key=False)
    except VaultRefError:
        return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:6"])

    def test_preceding_function_validation_guards_nested_verification(self):
        tree = ast.parse(
            """\
def verify(ref):
    try:
        parse_vault_ref(ref, require_key=True)
    except VaultRefError:
        return
    try:
        result = adapter_client.verify_secret(ref)
    except AdapterError:
        return
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), [])

    def test_preceding_block_validation_guards_nested_verification(self):
        tree = ast.parse(
            """\
def verify(state):
    if state.vault_ref:
        try:
            parse_vault_ref(state.vault_ref, require_key=False)
        except VaultRefError:
            return
        try:
            result = adapter_client.verify_secret(state.vault_ref)
        except AdapterError:
            return
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), [])
