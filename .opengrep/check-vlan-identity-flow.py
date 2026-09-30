# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Reject VLAN identity carried from interface names through local variables."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

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
_COMPREHENSIONS = (nodes.ListComp, nodes.SetComp, nodes.DictComp, nodes.GeneratorExp)


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


def _branch_assigns(body, name: str) -> bool:
    for statement in body:
        if isinstance(statement, (nodes.Assign, nodes.AnnAssign)):
            if isinstance(statement, nodes.AnnAssign) and statement.value is None:
                continue
            targets = statement.targets if isinstance(statement, nodes.Assign) else [statement.target]
            if any(
                binding.name == name
                for target in targets
                if isinstance(target, (nodes.AssignName, nodes.Tuple, nodes.List))
                for binding in target.nodes_of_class(nodes.AssignName)
            ):
                return True
        if isinstance(statement, nodes.If):
            if _branch_assigns(statement.body, name) and _branch_assigns(statement.orelse, name):
                return True
        elif isinstance(statement, (nodes.Return, nodes.Raise, nodes.Break, nodes.Continue)):
            return False
    return False


def _overwrite_position(use) -> tuple[int, int]:
    position = (-1, -1)
    child = use
    while child is not use.scope():
        parent = child.parent
        for field in ("body", "orelse", "finalbody"):
            body = getattr(parent, field, None)
            if not isinstance(body, list) or child not in body:
                continue
            for statement in body[: body.index(child)]:
                if _branch_assigns([statement], use.name):
                    position = max(position, (statement.lineno, statement.col_offset))
        child = parent
    return position


def _reaching_bindings(node, scope):
    loop_body = []
    child = node
    while child is not scope:
        parent = child.parent
        if isinstance(parent, (nodes.For, nodes.AsyncFor, nodes.While)) and child in parent.body:
            loop_body.extend(parent.body)
        child = parent
    bindings = []
    for binding in scope.locals.get(node.name, []):
        statement = binding.assign_type() if isinstance(binding, nodes.AssignName) else binding
        if (
            isinstance(statement, (nodes.Assign, nodes.AnnAssign, nodes.NamedExpr))
            and statement.value is not None
            and (statement.value is node or statement.value.parent_of(node))
        ):
            continue
        if (binding.lineno, binding.col_offset) < (node.lineno, node.col_offset) or any(
            statement is binding or statement.parent_of(binding) for statement in loop_body
        ):
            bindings.append(binding)
    return bindings


def _lookup_name(node):
    inner = node.scope()
    while isinstance(inner, (nodes.Lambda, nodes.FunctionDef)):
        expressions = [*inner.args.defaults, *inner.args.kw_defaults]
        if isinstance(inner, nodes.FunctionDef) and inner.decorators is not None:
            expressions.append(inner.decorators)
        if not any(value is not None and (value is node or value.parent_of(node)) for value in expressions):
            break
        node = nodes.Name(
            node.name,
            inner.lineno,
            inner.col_offset,
            inner.parent,
            end_lineno=inner.end_lineno,
            end_col_offset=inner.end_col_offset,
        )
        inner = node.scope()
    while isinstance(node.scope(), (*_COMPREHENSIONS, nodes.Lambda)):
        inner = node.scope()
        scope, assignments = node.lookup(node.name)
        if isinstance(inner, _COMPREHENSIONS):
            assignments = []
            for generator in inner.generators:
                if generator.iter is node or generator.iter.parent_of(node):
                    break
                targets = [
                    target for target in generator.target.nodes_of_class(nodes.AssignName) if target.name == node.name
                ]
                if targets:
                    assignments = targets
                if any(condition is node or condition.parent_of(node) for condition in generator.ifs):
                    break
            scope = inner if assignments else None
        if scope is inner:
            return node, scope, assignments
        node = nodes.Name(
            node.name,
            inner.lineno,
            inner.col_offset,
            inner.parent,
            end_lineno=inner.end_lineno,
            end_col_offset=inner.end_col_offset,
        )
    scope = node.scope()
    return node, scope, _reaching_bindings(node, scope) if isinstance(scope, nodes.FunctionDef) else []


def _source(node) -> set[str]:
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
    return set()


def _binding_expressions(statement, assignment):
    if isinstance(statement, (nodes.Comprehension, nodes.For, nodes.AsyncFor)):
        return [statement.iter]
    if isinstance(statement, (nodes.With, nodes.AsyncWith)):
        return [
            context
            for context, target in statement.items
            if target is not None and (target is assignment or target.parent_of(assignment))
        ]
    if isinstance(statement, (nodes.MatchAs, nodes.MatchStar, nodes.MatchMapping)):
        match = statement.parent
        while not isinstance(match, nodes.Match):
            match = match.parent
        return [match.subject]
    if isinstance(
        statement,
        (
            nodes.Arguments,
            nodes.Import,
            nodes.ImportFrom,
            nodes.ExceptHandler,
            nodes.Global,
            nodes.Nonlocal,
            nodes.FunctionDef,
            nodes.ClassDef,
        ),
    ):
        return []
    return [
        child
        for child in statement.get_children()
        if not child.is_statement and not isinstance(child, (nodes.AssignName, nodes.AssignAttr))
    ]


def _assignment_value(statement, node):
    value = statement.value
    target = node.parent
    if (
        isinstance(statement, nodes.Assign)
        and isinstance(target, (nodes.Tuple, nodes.List))
        and target in statement.targets
        and all(isinstance(element, nodes.AssignName) for element in target.elts)
        and isinstance(value, (nodes.Tuple, nodes.List))
        and len(target.elts) == len(value.elts)
        and not any(isinstance(element, nodes.Starred) for element in value.elts)
    ):
        value = value.elts[target.elts.index(node)]
    return value


def _sources(node, cache: dict, incomplete: set, *, binding: bool = False) -> set[str]:
    key = (binding, node)
    if key in cache:
        if cache[key] is None:
            incomplete.update(active for active, result in cache.items() if result is None)
            return set()
        return cache[key]
    cache[key] = None
    sources = set()
    if binding:
        statement = node.assign_type() if isinstance(node, nodes.AssignName) else node
        if isinstance(statement, (nodes.Assign, nodes.AnnAssign, nodes.NamedExpr)):
            value = _assignment_value(statement, node)
            if value is not None:
                sources.update(_sources(value, cache, incomplete))
        elif isinstance(statement, nodes.AugAssign):
            sources.update(_sources(node, cache, incomplete))
            sources.update(_sources(statement.value, cache, incomplete))
        else:
            for value in _binding_expressions(statement, node):
                sources.update(_sources(value, cache, incomplete))
    elif isinstance(node, (nodes.Name, nodes.AssignName)):
        use, scope, assignments = _lookup_name(node)
        if scope is use.scope() and isinstance(scope, (nodes.FunctionDef, *_COMPREHENSIONS, nodes.Lambda)):
            overwrite = _overwrite_position(use)
            for assignment in assignments:
                if (assignment.lineno, assignment.col_offset) >= overwrite:
                    sources.update(_sources(assignment, cache, incomplete, binding=True))
    else:
        pending = [node]
        while pending:
            child = pending.pop()
            if isinstance(child, (nodes.Name, nodes.AssignName)):
                sources.update(_sources(child, cache, incomplete))
            else:
                sources.update(_source(child))
                pending.extend(child.get_children())
    # A cycle cut makes every active result path-dependent.
    if key in incomplete:
        incomplete.remove(key)
        del cache[key]
    else:
        cache[key] = sources
    return sources


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
    cache = {}
    incomplete = set()
    for call in module.nodes_of_class(nodes.Call):
        if not isinstance(call.func, nodes.Name) or call.func.name != "int" or len(call.args) != 1 or call.keywords:
            continue
        argument = _name_argument(call.args[0])
        if argument is None:
            continue
        kind, name = argument
        targets = _targets(call)
        allowed = _SUFFIX_TARGETS if kind == "suffix" else _REGEX_TARGETS if kind == "regex" else {"return"}
        if targets & allowed and kind in _sources(name, cache, incomplete):
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


def _test_long_chains() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "chain.py"
        for source, expected in (("interface.cf['vlan']", []), ("interface.name.split('.')[-1]", [203])):
            path.write_text(
                f"def chain(interface):\n    suffix = {source}\n"
                + '    suffix = suffix.strip() if suffix else "100"\n' * 200
                + "    vid = int(suffix)\n",
                encoding="utf-8",
            )
            started = perf_counter()
            actual = scan(path)
            elapsed = perf_counter() - started
            assert actual == expected, (source, actual)
            assert elapsed < 0.5, f"{source}: scan took {elapsed:.3f}s"


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
        _test_long_chains()
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
