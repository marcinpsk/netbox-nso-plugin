# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Reject AdapterError validation after a continue for the same loop."""

from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

_RULE_ID = "nso-adapter-error-after-continue"
_ANNOTATION = re.compile(r"#\s*ast-(finding|clean):\s*" + re.escape(_RULE_ID) + r"\s*$")


def _dotted(node) -> tuple[str, ...]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _parameter_names(node) -> set[str]:
    arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    names = {argument.arg for argument in arguments}
    if node.args.vararg is not None:
        names.add(node.args.vararg.arg)
    if node.args.kwarg is not None:
        names.add(node.args.kwarg.arg)
    return names


@dataclass
class _Aliases:
    packages: set[str] = field(default_factory=set)
    clients: set[str] = field(default_factory=set)
    errors: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)

    def nested(self, blocked) -> _Aliases:
        blocked = set(blocked)
        return _Aliases(
            packages=self.packages - blocked,
            clients=self.clients - blocked,
            errors=self.errors - blocked,
            blocked=blocked,
        )


@dataclass
class _Loop:
    checked: bool
    has_continue: bool = False
    raises: list[int] = field(default_factory=list)


class _Scanner(ast.NodeVisitor):
    def __init__(self):
        self.aliases = _Aliases()
        self.loops: list[_Loop] = []
        self.findings: list[int] = []

    def visit_Import(self, node):
        for name in node.names:
            bound = name.asname or name.name.split(".")[0]
            if bound in self.aliases.blocked:
                continue
            if name.name == "netbox_nso_plugin":
                self.aliases.packages.add(bound)
            elif name.name == "netbox_nso_plugin.adapter_client":
                if name.asname:
                    self.aliases.clients.add(bound)
                else:
                    self.aliases.packages.add("netbox_nso_plugin")

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for name in node.names:
            bound = name.asname or name.name
            if bound in self.aliases.blocked:
                continue
            if name.name == "adapter_client" and module in ("", "netbox_nso_plugin"):
                self.aliases.clients.add(bound)
            elif name.name == "AdapterError" and module.endswith("adapter_client"):
                self.aliases.errors.add(bound)

    def _visit_function(self, node):
        previous_aliases = self.aliases
        previous_loops = self.loops
        self.aliases = previous_aliases.nested(_parameter_names(node))
        self.loops = []
        for statement in node.body:
            self.visit(statement)
        self.aliases = previous_aliases
        self.loops = previous_loops

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _visit_loop(self, node, *, checked):
        if hasattr(node, "target"):
            self.visit(node.target)
            self.visit(node.iter)
        else:
            self.visit(node.test)
        loop = _Loop(checked=checked)
        self.loops.append(loop)
        for statement in node.body:
            self.visit(statement)
        self.loops.pop()
        if loop.checked and loop.has_continue:
            self.findings.extend(loop.raises)
        for statement in node.orelse:
            self.visit(statement)

    def visit_For(self, node):
        self._visit_loop(node, checked=True)

    def visit_AsyncFor(self, node):
        self._visit_loop(node, checked=True)

    def visit_While(self, node):
        self._visit_loop(node, checked=False)

    def visit_Continue(self, node):
        if self.loops:
            self.loops[-1].has_continue = True

    def visit_Raise(self, node):
        if self.loops and self._is_adapter_error(node.exc):
            self.loops[-1].raises.append(node.lineno)
        self.generic_visit(node)

    def _is_adapter_error(self, exception) -> bool:
        if not isinstance(exception, ast.Call):
            return False
        parts = _dotted(exception.func)
        if parts == ("netbox_nso_plugin", "adapter_client", "AdapterError"):
            return True
        if len(parts) == 3 and parts[0] in self.aliases.packages:
            return parts[1:] == ("adapter_client", "AdapterError")
        if len(parts) == 2 and parts[0] in self.aliases.clients:
            return parts[1] == "AdapterError"
        return len(parts) == 1 and parts[0] in self.aliases.errors


def scan(path: Path) -> list[int]:
    scanner = _Scanner()
    scanner.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return sorted(scanner.findings)


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
    for path in sorted(Path("netbox_nso_plugin").glob("*_reconciler.py")):
        for line in scan(path):
            failed = True
            print(f"{path}:{line}: {_RULE_ID}: validate before the loop can continue")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
