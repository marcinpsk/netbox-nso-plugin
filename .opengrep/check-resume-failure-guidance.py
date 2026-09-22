# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Require failure guidance around deployment resume calls."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from importlib import import_module
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "netbox_nso_plugin" / "tests"))
_ast_scope = import_module("_ast_scope")
resolve_call_target = _ast_scope.resolve_call_target
scope_bindings = _ast_scope.scope_bindings

_RULE_ID = "nso-resume-failure-guidance"
_RESUME_TARGET = "netbox_nso_plugin.deployment.resume"
_ANNOTATION = re.compile(r"#\s*ast-(finding|clean):\s*" + re.escape(_RULE_ID) + r"\s*$")
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)


def _parents(tree) -> dict:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _ancestor(node, parents, kinds):
    while node in parents:
        node = parents[node]
        if isinstance(node, kinds):
            return node
    return None


def _resume_calls(tree):
    bindings = scope_bindings(tree)
    parents = _parents(tree)
    return [
        (node, parents, bindings)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and resolve_call_target(node, bindings[node]) == _RESUME_TARGET
    ]


def _is_abort_test(node) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "options"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "abort"
    )


def _is_abort_call(node, parents) -> bool:
    child = node
    function = None
    call_function = _ancestor(node, parents, _FUNCTION_SCOPES)
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.If) and child in current.body and _is_abort_test(current.test):
            function = _ancestor(current, parents, _FUNCTION_SCOPES)
            break
        child = current
        current = parents.get(current)
    return (
        function is not None
        and function is call_function
        and function.name == "handle"
        and function.args.kwarg is not None
        and function.args.kwarg.arg == "options"
    )


def _literal_text(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.FormattedValue):
        return ""
    return "".join(_literal_text(child) for child in ast.iter_child_nodes(node))


def _is_base_exception(node) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "BaseException"
    return (
        isinstance(node, ast.Tuple)
        and len(node.elts) == 1
        and isinstance(node.elts[0], ast.Name)
        and node.elts[0].id == "BaseException"
    )


def _is_best_effort_report(statement, bindings) -> bool:
    if not isinstance(statement, ast.With) or len(statement.items) != 1:
        return False
    item = statement.items[0]
    context = item.context_expr
    if item.optional_vars is not None or not isinstance(context, ast.Call):
        return False
    if len(context.args) != 1 or context.keywords:
        return False
    if resolve_call_target(context, bindings[context]) != "contextlib.suppress":
        return False
    exception = context.args[0]
    if not isinstance(exception, ast.Name) or exception.id != "Exception":
        return False
    if len(statement.body) != 1 or not isinstance(statement.body[0], ast.Expr):
        return False
    report = statement.body[0].value
    if not isinstance(report, ast.Call) or ast.unparse(report.func) != "self.stderr.write" or not report.args:
        return False
    message = _literal_text(report.args[0])
    return "may remain quiesced" in message and "nso_intent_deployment_gate --abort" in message


def _has_failure_guidance(node, parents, bindings) -> bool:
    statement = _ancestor(node, parents, ast.stmt)
    if not isinstance(statement, ast.Expr) or statement.value is not node:
        return False
    guarded = parents.get(statement)
    if not isinstance(guarded, (ast.Try, ast.TryStar)):
        return False
    if guarded.body != [statement] or guarded.orelse or len(guarded.handlers) != 1:
        return False
    handler = guarded.handlers[0]
    if not _is_base_exception(handler.type) or len(handler.body) != 2:
        return False
    report, reraise = handler.body
    return (
        _is_best_effort_report(report, bindings)
        and isinstance(reraise, ast.Raise)
        and reraise.exc is None
        and reraise.cause is None
    )


def scan(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        node.lineno
        for node, parents, bindings in _resume_calls(tree)
        if not _is_abort_call(node, parents) and not _has_failure_guidance(node, parents, bindings)
    )


def _annotated_lines(path: Path) -> set[int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    expected = set()
    for index, line in enumerate(lines):
        match = _ANNOTATION.search(line)
        if match is None or match.group(1) != "finding":
            continue
        for statement_index in range(index + 1, len(lines)):
            statement = lines[statement_index].strip()
            if statement and not statement.startswith("#"):
                expected.add(statement_index + 1)
                break
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("scan", "test"))
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.mode == "test":
        actual = set(scan(args.path))
        expected = _annotated_lines(args.path)
        if actual != expected:
            print(f"{_RULE_ID}: expected lines {sorted(expected)}, got {sorted(actual)}")
            return 1
        return 0

    failed = False
    commands = Path("netbox_nso_plugin/management/commands")
    for path in sorted(commands.rglob("*.py")):
        for line in scan(path):
            failed = True
            print(f"{path}:{line}: {_RULE_ID}: report the quiesced state and abort recovery, then re-raise")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
