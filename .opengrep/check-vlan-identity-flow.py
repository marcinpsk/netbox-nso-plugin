# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Reject VLAN identity carried from interface names through local variables."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from python_check_paths import scan_python_paths

try:
    import astroid
    from astroid import nodes
except ImportError as error:
    raise SystemExit("nso-vlan-identity-flow: astroid 4.x is required; install it for python3") from error

_RULE_ID = "nso-vlan-identity-flow"
_ANNOTATION = re.compile(r"#\s*(ruleid|ok):\s*(.+?)\s*$")
_SUFFIX_METHODS = {"rsplit", "split", "partition", "removeprefix"}
_REGEX_METHODS = {"match", "search", "fullmatch"}
_SUFFIX_TARGETS = {"vid", "dot1q_vlan", "attribute", "keyword", "dict", "conditional"}
_REGEX_TARGETS = {"vid", "vlan", "attribute", "keyword", "dict"}


def _interface_name(node) -> bool:
    return isinstance(node, nodes.Attribute) and node.attrname == "name"


def _lower_source(node) -> bool:
    if _interface_name(node):
        return True
    return (
        isinstance(node, nodes.BoolOp)
        and node.op == "or"
        and len(node.values) == 2
        and _interface_name(node.values[0])
        and isinstance(node.values[1], nodes.Const)
        and node.values[1].value == ""
    )


def _sources(node, visited: set) -> set[str]:
    if isinstance(node, (nodes.Name, nodes.AssignName)):
        scope, assignments = node.lookup(node.name)
        if scope is not node.scope() or not isinstance(scope, nodes.FunctionDef):
            return set()
        sources = set()
        for assignment in assignments:
            if assignment in visited or not isinstance(assignment, nodes.AssignName):
                continue
            statement = assignment.parent
            if isinstance(statement, (nodes.Assign, nodes.AnnAssign)):
                value = assignment if statement.value is None else statement.value
                sources.update(_sources(value, visited | {assignment}))
        return sources
    if isinstance(node, nodes.Subscript):
        return _sources(node.value, visited)
    if not isinstance(node, nodes.Call) or not isinstance(node.func, nodes.Attribute):
        return set()
    method = node.func.attrname
    receiver = node.func.expr
    if method in _SUFFIX_METHODS and _interface_name(receiver):
        return {"suffix"}
    if method == "lower" and not node.args and not node.keywords and _lower_source(receiver):
        return {"lower"}
    if (
        method in _REGEX_METHODS
        and isinstance(receiver, nodes.Name)
        and receiver.name == "re"
        and node.args
        and _interface_name(node.args[-1])
        and not node.keywords
    ):
        return {"regex"}
    return _sources(receiver, visited)


def _assignment_target(node) -> str | None:
    if isinstance(node, nodes.AssignName) and node.name in {"vid", "vlan", "dot1q_vlan"}:
        return node.name
    if isinstance(node, nodes.AssignAttr) and node.attrname == "dot1q_vlan":
        return "attribute"
    return None


def _targets(call) -> set[str]:
    parent = call.parent
    if isinstance(parent, (nodes.Assign, nodes.AnnAssign)):
        assigned = parent.targets if isinstance(parent, nodes.Assign) else [parent.target]
        return {target for node in assigned if (target := _assignment_target(node)) is not None}
    if isinstance(parent, nodes.IfExp) and parent.body is call and "attribute" in _targets(parent):
        return {"conditional"}
    if isinstance(parent, nodes.Keyword) and parent.arg == "dot1q_vlan":
        return {"keyword"}
    if isinstance(parent, nodes.Dict):
        return {
            "dict"
            for key, value in parent.items
            if isinstance(key, nodes.Const) and key.value == "dot1q_vlan" and value is call
        }
    if (
        isinstance(parent, nodes.Tuple)
        and len(parent.elts) == 2
        and parent.elts[1] is call
        and isinstance(parent.elts[0], nodes.Const)
        and parent.elts[0].value in {"svi", "irb"}
        and isinstance(parent.parent, nodes.Return)
    ):
        return {"return"}
    return set()


def _return_slice(node) -> bool:
    return (
        isinstance(node, nodes.Subscript)
        and isinstance(node.value, nodes.Name)
        and isinstance(node.slice, nodes.Slice)
        and isinstance(node.slice.lower, nodes.Const)
        and node.slice.lower.value == 4
        and node.slice.upper is None
        and node.slice.step is None
    )


def _name_argument(node) -> tuple[str, nodes.Name] | None:
    if isinstance(node, nodes.Name):
        return "suffix", node
    if _return_slice(node):
        return "lower", node.value
    if (
        isinstance(node, nodes.Call)
        and isinstance(node.func, nodes.Attribute)
        and node.func.attrname == "group"
        and isinstance(node.func.expr, nodes.Name)
        and len(node.args) == 1
        and not node.keywords
    ):
        return "regex", node.func.expr
    return None


def scan(path: Path) -> list[int]:
    module = astroid.parse(path.read_text(encoding="utf-8"), path=str(path))
    findings = set()
    for call in module.nodes_of_class(nodes.Call):
        if not isinstance(call.func, nodes.Name) or call.func.name != "int" or len(call.args) != 1 or call.keywords:
            continue
        argument = _name_argument(call.args[0])
        if argument is None:
            continue
        kind, name = argument
        targets = _targets(call)
        allowed = _SUFFIX_TARGETS if kind == "suffix" else _REGEX_TARGETS if kind == "regex" else {"return"}
        if targets & allowed and kind in _sources(name, set()):
            findings.add(call.statement().lineno)
    return sorted(findings)


def _annotated_lines(path: Path) -> set[int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    expected = set()
    for index, line in enumerate(lines):
        match = _ANNOTATION.search(line)
        if (
            match is None
            or match.group(1) != "ruleid"
            or _RULE_ID not in {part.strip() for part in match.group(2).split(",")}
        ):
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
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    if args.mode == "test":
        if len(args.paths) != 1:
            parser.error("test requires exactly one fixture path")
        fixture = args.paths[0]
        actual = set(scan(fixture))
        expected = _annotated_lines(fixture)
        if actual != expected:
            print(f"{_RULE_ID}: expected lines {sorted(expected)}, got {sorted(actual)}")
            return 1
        return 0

    failed = False
    defaults = (
        path for path in Path("netbox_nso_plugin").rglob("*.py") if not {"tests", "migrations"} & set(path.parts)
    )
    try:
        paths = scan_python_paths(args.paths, defaults)
    except ValueError as error:
        parser.error(str(error))
    for path in paths:
        for line in scan(path):
            failed = True
            print(f"{path}:{line}: {_RULE_ID}: take VLAN identity from an explicit field")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
