# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require management commands to report failures that can leave intent work quiesced."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

_COMMANDS_ROOT = Path(__file__).resolve().parents[1] / "management" / "commands"
_ALLOWED_RESUME_SITES = {
    # Abort is the requested operation, so its failure already names the failed operation.
    ("nso_intent_deployment_gate.py", "handle"),
}


def _is_resume_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == "resume"
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "deployment"
        and node.func.attr == "resume"
    )


def _resume_calls():
    for path in sorted(_COMMANDS_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if _is_resume_call(node):
                yield path, node, parents


def _ancestor(node: ast.AST, parents: dict[ast.AST, ast.AST], kind):
    while node in parents:
        node = parents[node]
        if isinstance(node, kind):
            return node
    return None


def _is_allowed(path: Path, node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    if _ancestor(node, parents, ast.ExceptHandler) is not None:
        return True
    function = _ancestor(node, parents, ast.FunctionDef | ast.AsyncFunctionDef)
    return function is not None and (path.name, function.name) in _ALLOWED_RESUME_SITES


def _has_failure_guidance(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    statement = _ancestor(node, parents, ast.stmt)
    if statement is None:
        return False
    guarded = parents.get(statement)
    if not isinstance(guarded, ast.Try) or guarded.body != [statement] or len(guarded.handlers) != 1:
        return False
    handler = guarded.handlers[0]
    if not isinstance(handler.type, ast.Name) or handler.type.id != "BaseException":
        return False
    writes_guidance = any(
        isinstance(candidate, ast.Call) and ast.unparse(candidate.func) == "self.stderr.write"
        for handler_statement in handler.body
        for candidate in ast.walk(handler_statement)
    )
    last_statement = handler.body[-1] if handler.body else None
    reraises = isinstance(last_statement, ast.Raise) and last_statement.exc is None and last_statement.cause is None
    return writes_guidance and reraises


class TestDeploymentGateResumeStructure(SimpleTestCase):
    def test_resume_failures_report_that_intent_work_may_remain_quiesced(self):
        violations = {
            f"{path.name}:{node.lineno}"
            for path, node, parents in _resume_calls()
            if not _is_allowed(path, node, parents) and not _has_failure_guidance(node, parents)
        }

        self.assertEqual(violations, set(), "\n".join(sorted(violations)))

    def test_resume_scan_finds_management_command_calls(self):
        self.assertGreaterEqual(len(list(_resume_calls())), 3)
