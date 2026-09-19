# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require secret verification callers to reject the wrong Vault reference kind."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

from ._ast_scope import resolve_call_target, scope_bindings, scoped_walk

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "netbox_nso_plugin"
_VERIFY_TARGET = "netbox_nso_plugin.adapter_client.verify_secret"
_PARSE_TARGET = "netbox_nso_plugin.vault_refs.parse_vault_ref"
_ERROR_TARGET = "netbox_nso_plugin.vault_refs.VaultRefError"


def _production_modules():
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative_path = path.relative_to(_PACKAGE_ROOT)
        if "tests" not in relative_path.parts and "migrations" not in relative_path.parts:
            yield path


def _is_call_to(node: ast.AST, target: str, bindings: dict[ast.AST, dict[str, str]]) -> bool:
    return isinstance(node, ast.Call) and resolve_call_target(node, bindings[node]) == target


def _verification_sites_in_module(
    tree: ast.Module,
    bindings: dict[ast.AST, dict[str, str]] | None = None,
):
    bindings = bindings or scope_bindings(tree)
    function_types = ast.FunctionDef | ast.AsyncFunctionDef
    for function in (node for node in ast.walk(tree) if isinstance(node, function_types)):
        for node in scoped_walk(function.body):
            if _is_call_to(node, _VERIFY_TARGET, bindings):
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


def _rejects_vault_ref_error(node: ast.Try, bindings: dict[ast.AST, dict[str, str]]) -> bool:
    return any(
        handler.type is not None
        and resolve_call_target(handler.type, bindings[handler.type]) == _ERROR_TARGET
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


def _is_validation_try(
    node: ast.stmt,
    verification_argument: str,
    bindings: dict[ast.AST, dict[str, str]],
) -> bool:
    if not isinstance(node, ast.Try) or len(node.body) != 1:
        return False
    statement = node.body[0]
    if not isinstance(statement, ast.Expr) or not _is_call_to(statement.value, _PARSE_TARGET, bindings):
        return False
    parse_call = statement.value
    return bool(
        parse_call.args
        and ast.unparse(parse_call.args[0]) == verification_argument
        and _has_literal_polarity(parse_call)
        and _rejects_vault_ref_error(node, bindings)
    )


def _has_prior_validation(
    verification: ast.Call,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: dict[ast.AST, dict[str, str]],
) -> bool:
    if not verification.args:
        return False
    verification_argument = ast.unparse(verification.args[0])
    chain = _statement_list_chain(function, verification)
    return chain is not None and any(
        _is_validation_try(statement, verification_argument, bindings)
        for statements, verification_index in chain
        for statement in statements[:verification_index]
    )


def _violations_in_module(tree: ast.Module, label: str | Path) -> list[str]:
    bindings = scope_bindings(tree)
    return [
        f"{label}:{verification.lineno}"
        for verification, function in _verification_sites_in_module(tree, bindings)
        if not _has_prior_validation(verification, function, bindings)
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

    def test_import_aliases_are_checked_and_guarded_aliases_are_allowed(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client as client
from netbox_nso_plugin.adapter_client import verify_secret as verify
from .adapter_client import verify_secret as relative_verify
from netbox_nso_plugin.vault_refs import parse_vault_ref as parse_ref
from netbox_nso_plugin.vault_refs import VaultRefError as InvalidVaultRef

def module_alias(ref):
    client.verify_secret(ref)

def symbol_alias(ref):
    verify(ref)

def relative_alias(ref):
    relative_verify(ref)

def guarded_alias(ref):
    try:
        parse_ref(ref, require_key=False)
    except InvalidVaultRef:
        return
    verify(ref)
"""
        )

        self.assertEqual(
            _violations_in_module(tree, "snippet.py"),
            ["snippet.py:8", "snippet.py:11", "snippet.py:14"],
        )

    def test_function_import_does_not_bind_a_sibling_parameter(self):
        tree = ast.parse(
            """\
def guarded(ref):
    from netbox_nso_plugin import adapter_client as client
    from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref
    try:
        parse_vault_ref(ref, require_key=False)
    except VaultRefError:
        return
    client.verify_secret(ref)

def unrelated(client, ref):
    client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), [])

    def test_rebound_parser_does_not_certify_validation(self):
        tree = ast.parse(
            """\
def verify(ref):
    from netbox_nso_plugin import adapter_client
    from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref
    parse_vault_ref = lambda *args, **kwargs: None
    try:
        parse_vault_ref(ref, require_key=False)
    except VaultRefError:
        return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:9"])

    def test_parser_parameter_does_not_certify_validation(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref

def verify(ref, parse_vault_ref):
    try:
        parse_vault_ref(ref, require_key=False)
    except VaultRefError:
        return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:9"])

    def test_later_same_scope_import_does_not_certify_validation(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref

def verify(ref):
    from netbox_nso_plugin.vault_refs import parse_vault_ref
    from netbox_nso_plugin.fakes import parse_vault_ref
    try:
        parse_vault_ref(ref, require_key=False)
    except VaultRefError:
        return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:11"])

    def test_conflicting_branch_imports_do_not_certify_validation(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError

def verify(ref, use_real_parser):
    if use_real_parser:
        from netbox_nso_plugin.vault_refs import parse_vault_ref
    else:
        from netbox_nso_plugin.fakes import parse_vault_ref
    try:
        parse_vault_ref(ref, require_key=False)
    except VaultRefError:
        return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:13"])

    def test_later_matching_import_certifies_validation(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError

def verify(ref):
    from netbox_nso_plugin.vault_refs import parse_vault_ref
    from netbox_nso_plugin.vault_refs import parse_vault_ref
    try:
        parse_vault_ref(ref, require_key=False)
    except VaultRefError:
        return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), [])

    def test_conditional_validation_does_not_guard_later_verification(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref
def verify(ref):
    if False:
        try:
            parse_vault_ref(ref, require_key=False)
        except VaultRefError:
            return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:9"])

    def test_validation_of_different_argument_does_not_guard_verification(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref
def verify(ref, other):
    try:
        parse_vault_ref(other, require_key=False)
    except VaultRefError:
        return
    adapter_client.verify_secret(ref)
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:8"])

    def test_preceding_function_validation_guards_nested_verification(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref
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
from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.vault_refs import VaultRefError, parse_vault_ref
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
