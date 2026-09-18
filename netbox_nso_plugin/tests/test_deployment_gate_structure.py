# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require management commands to report failures that can leave intent work quiesced."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

from ._ast_scope import resolve_call_target, scope_bindings

_COMMANDS_ROOT = Path(__file__).resolve().parents[1] / "management" / "commands"
_ALLOWED_RESUME_SITES = {
    # Abort is the requested operation, so its failure already names the failed operation.
    ("nso_intent_deployment_gate.py", "handle"),
}
_RESUME_TARGET = "netbox_nso_plugin.deployment.resume"


def _is_resume_call(node: ast.AST, bindings: dict[str, str]) -> bool:
    return isinstance(node, ast.Call) and resolve_call_target(node, bindings) == _RESUME_TARGET


def _resume_calls():
    for path in sorted(_COMMANDS_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, parents, bindings in _resume_calls_in_module(tree):
            yield path, node, parents, bindings


def _resume_calls_in_module(tree: ast.Module):
    bindings = scope_bindings(tree)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if _is_resume_call(node, bindings[node]):
            yield node, parents, bindings


def _ancestor(node: ast.AST, parents: dict[ast.AST, ast.AST], kind):
    while node in parents:
        node = parents[node]
        if isinstance(node, kind):
            return node
    return None


def _is_allowed(path: Path, node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    function = _ancestor(node, parents, ast.FunctionDef | ast.AsyncFunctionDef)
    return function is not None and (path.name, function.name) in _ALLOWED_RESUME_SITES


def _literal_messages(node: ast.AST) -> list[str]:
    if isinstance(node, ast.JoinedStr):
        return [
            "".join(
                segment.value
                for segment in node.values
                if isinstance(segment, ast.Constant) and isinstance(segment.value, str)
            )
        ]
    messages = [node.value] if isinstance(node, ast.Constant) and isinstance(node.value, str) else []
    for child in ast.iter_child_nodes(node):
        messages.extend(_literal_messages(child))
    return messages


def _is_best_effort_report(statement: ast.stmt, bindings: dict[str, str]) -> bool:
    if not isinstance(statement, ast.With) or len(statement.items) != 1:
        return False
    item = statement.items[0]
    context = item.context_expr
    if item.optional_vars is not None or not isinstance(context, ast.Call):
        return False
    if len(context.args) != 1 or context.keywords or resolve_call_target(context, bindings) != "contextlib.suppress":
        return False
    exception = context.args[0]
    if not isinstance(exception, ast.Name) or exception.id not in {"Exception", "BaseException"}:
        return False
    if len(statement.body) != 1 or not isinstance(statement.body[0], ast.Expr):
        return False
    report = statement.body[0].value
    return (
        isinstance(report, ast.Call)
        and ast.unparse(report.func) == "self.stderr.write"
        and bool(report.args)
        and any(
            "may remain quiesced" in message and "nso_intent_deployment_gate --abort" in message
            for message in _literal_messages(report.args[0])
        )
    )


def _has_failure_guidance(
    node: ast.Call,
    parents: dict[ast.AST, ast.AST],
    bindings: dict[ast.AST, dict[str, str]],
) -> bool:
    statement = _ancestor(node, parents, ast.stmt)
    if statement is None:
        return False
    guarded = parents.get(statement)
    if not isinstance(guarded, ast.Try) or guarded.body != [statement] or len(guarded.handlers) != 1:
        return False
    handler = guarded.handlers[0]
    if not isinstance(handler.type, ast.Name) or handler.type.id != "BaseException":
        return False
    if len(handler.body) != 2:
        return False
    report, reraise = handler.body
    return (
        _is_best_effort_report(report, bindings[report])
        and isinstance(reraise, ast.Raise)
        and reraise.exc is None
        and reraise.cause is None
    )


def _snippet_has_failure_guidance(message_expression: str) -> bool:
    tree = ast.parse(
        f"""
import contextlib

from netbox_nso_plugin import deployment

def handle(self):
    try:
        deployment.resume()
    except BaseException as exc:
        with contextlib.suppress(Exception):
            self.stderr.write(self.style.ERROR({message_expression}))
        raise
"""
    )
    resume_call, parents, bindings = next(_resume_calls_in_module(tree))
    return _has_failure_guidance(resume_call, parents, bindings)


def _violations_in_module(tree: ast.Module, label: str) -> list[str]:
    return [
        f"{label}:{node.lineno}"
        for node, parents, bindings in _resume_calls_in_module(tree)
        if not _is_allowed(Path(label), node, parents) and not _has_failure_guidance(node, parents, bindings)
    ]


class TestDeploymentGateResumeStructure(SimpleTestCase):
    def assert_resume_site_is_reported(self, source: str) -> None:
        tree = ast.parse(source)
        resume_call, _, _ = next(_resume_calls_in_module(tree))

        self.assertEqual(_violations_in_module(tree, "snippet.py"), [f"snippet.py:{resume_call.lineno}"])

    def test_import_aliases_are_checked_and_guarded_aliases_are_allowed(self):
        tree = ast.parse(
            """\
import contextlib
from netbox_nso_plugin import deployment as gate
from netbox_nso_plugin.deployment import resume as restart
from ... import deployment as relative_gate

def module_alias():
    gate.resume()

def symbol_alias():
    restart()

def relative_alias():
    relative_gate.resume()

def guarded_alias(self):
    try:
        gate.resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
"""
        )

        self.assertEqual(
            _violations_in_module(tree, "snippet.py"),
            ["snippet.py:7", "snippet.py:10", "snippet.py:13"],
        )

    def test_failure_guidance_accepts_f_string(self):
        self.assertTrue(
            _snippet_has_failure_guidance(
                'f"Intent work may remain quiesced: {exc}. Fix the cause and run nso_intent_deployment_gate --abort."'
            )
        )

    def test_failure_guidance_rejects_bare_variable(self):
        self.assertFalse(_snippet_has_failure_guidance("message"))

    def test_failure_guidance_requires_recovery_command(self):
        self.assertFalse(_snippet_has_failure_guidance('"Intent work may remain quiesced."'))

    def test_failure_guidance_requires_a_best_effort_stderr_write(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        self.stderr.write(
            self.style.ERROR(
                "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
            )
        )
        raise
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:5"])

    def test_cleanup_resume_under_an_exception_handler_requires_guidance(self):
        tree = ast.parse(
            """\
from netbox_nso_plugin.deployment import resume

def handle():
    try:
        fail()
    except BaseException:
        resume()
        raise
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), ["snippet.py:7"])

    def test_full_best_effort_failure_guidance_is_accepted(self):
        tree = ast.parse(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), [])

    def test_imported_suppress_failure_guidance_is_accepted(self):
        tree = ast.parse(
            """\
from contextlib import suppress
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        with suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
"""
        )

        self.assertEqual(_violations_in_module(tree, "snippet.py"), [])

    def test_try_except_pass_failure_guidance_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        try:
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        except Exception:
            pass
        raise
"""
        )

    def test_nested_function_failure_guidance_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        def report():
            with contextlib.suppress(Exception):
                self.stderr.write(
                    self.style.ERROR(
                        "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                    )
                )
        raise
"""
        )

    def test_conditional_failure_guidance_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        if should_report:
            with contextlib.suppress(Exception):
                self.stderr.write(
                    self.style.ERROR(
                        "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                    )
                )
        raise
"""
        )

    def test_failure_guidance_with_second_statement_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
            pass
        raise
"""
        )

    def test_failure_guidance_with_early_exit_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        if skip_reraise:
            return
        raise
"""
        )

    def test_failure_guidance_after_bare_raise_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        raise
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
"""
        )

    def test_failure_guidance_after_explicit_raise_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        raise RuntimeError("x")
        with contextlib.suppress(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
"""
        )

    def test_failure_guidance_with_wrong_exception_type_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        with contextlib.suppress(OSError):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
"""
        )

    def test_failure_guidance_with_another_context_manager_is_rejected(self):
        self.assert_resume_site_is_reported(
            """\
import contextlib
from netbox_nso_plugin.deployment import resume

def handle(self):
    try:
        resume()
    except BaseException:
        with contextlib.nullcontext(Exception):
            self.stderr.write(
                self.style.ERROR(
                    "Intent work may remain quiesced. Fix the cause and run nso_intent_deployment_gate --abort."
                )
            )
        raise
"""
        )

    def test_resume_failures_report_that_intent_work_may_remain_quiesced(self):
        violations = {
            f"{path.name}:{node.lineno}"
            for path, node, parents, bindings in _resume_calls()
            if not _is_allowed(path, node, parents) and not _has_failure_guidance(node, parents, bindings)
        }

        self.assertEqual(violations, set(), "\n".join(sorted(violations)))

    def test_resume_scan_finds_management_command_calls(self):
        guarded_sites = [node for path, node, parents, _ in _resume_calls() if not _is_allowed(path, node, parents)]

        self.assertGreaterEqual(len(guarded_sites), 4)
