# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared AST traversal helpers for structural tests."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_PACKAGE_NAME = "netbox_nso_plugin"


def _import_targets(node: ast.Import | ast.ImportFrom) -> dict[str, str]:
    targets = {}
    if isinstance(node, ast.Import):
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".")[0]
            targets[local_name] = alias.name if alias.asname else alias.name.split(".")[0]
        return targets
    module = node.module or ""
    if node.level:
        module = ".".join(part for part in (_PACKAGE_NAME, module) if part)
    for alias in node.names:
        if alias.name != "*":
            targets[alias.asname or alias.name] = ".".join(part for part in (module, alias.name) if part)
    return targets


def _argument_names(arguments: ast.arguments) -> set[str]:
    args = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    if arguments.vararg is not None:
        args.append(arguments.vararg)
    if arguments.kwarg is not None:
        args.append(arguments.kwarg)
    return {argument.arg for argument in args}


def _without_names(bindings: dict[str, str], names: Iterable[str]) -> dict[str, str]:
    remaining = dict(bindings)
    for name in names:
        remaining.pop(name, None)
    return remaining


def _stored_names(node: ast.AST) -> set[str]:
    return {
        target.id
        for target in ast.walk(node)
        if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store | ast.Del)
    }


def _merge_bindings(branches: Iterable[dict[str, str]]) -> dict[str, str]:
    branch_list = list(branches)
    if not branch_list:
        return {}
    common_names = set(branch_list[0]).intersection(*(set(branch) for branch in branch_list[1:]))
    return {
        name: branch_list[0][name]
        for name in common_names
        if all(branch[name] == branch_list[0][name] for branch in branch_list[1:])
    }


def _definition_result(
    bindings: dict[str, str],
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    registered: dict[str, str] | None,
) -> dict[str, str]:
    result = _without_names(bindings, (node.name,))
    if registered is not None and node.name in registered:
        result[node.name] = registered[node.name]
    return result


def _block_result(
    statements: Iterable[ast.stmt],
    bindings: dict[str, str],
    registered: dict[str, str] | None = None,
) -> dict[str, str]:
    result = dict(bindings)
    for statement in statements:
        result = _statement_result(statement, result, registered)
    return result


def _try_result(
    node: ast.Try | ast.TryStar,
    bindings: dict[str, str],
    registered: dict[str, str] | None,
) -> dict[str, str]:
    normal_result = _block_result(node.orelse, _block_result(node.body, bindings, registered), registered)
    branches = [normal_result]
    for handler in node.handlers:
        handler_bindings = bindings
        if handler.name is not None:
            handler_bindings = _without_names(handler_bindings, (handler.name,))
        handler_result = _block_result(handler.body, handler_bindings, registered)
        if handler.name is not None:
            handler_result = _without_names(handler_result, (handler.name,))
        branches.append(handler_result)
    return _block_result(node.finalbody, _merge_bindings(branches), registered)


def _statement_result(
    node: ast.stmt,
    bindings: dict[str, str],
    registered: dict[str, str] | None,
) -> dict[str, str]:
    if isinstance(node, ast.Import | ast.ImportFrom):
        return {**bindings, **_import_targets(node)}
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return _definition_result(bindings, node, registered)
    if isinstance(node, ast.Assign):
        return _without_names(bindings, (name for target in node.targets for name in _stored_names(target)))
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return _without_names(bindings, _stored_names(node.target))
    if isinstance(node, ast.Delete):
        return _without_names(bindings, (name for target in node.targets for name in _stored_names(target)))
    if isinstance(node, ast.If):
        return _merge_bindings(
            (
                _block_result(node.body, bindings, registered),
                _block_result(node.orelse, bindings, registered),
            )
        )
    if isinstance(node, ast.Try | ast.TryStar):
        return _try_result(node, bindings, registered)
    if isinstance(node, ast.Match):
        return _merge_bindings([bindings, *(_block_result(case.body, bindings, registered) for case in node.cases)])
    if isinstance(node, ast.For | ast.AsyncFor):
        body_bindings = _without_names(bindings, _stored_names(node.target))
        loop_result = _merge_bindings((bindings, _block_result(node.body, body_bindings, registered)))
        return _block_result(node.orelse, loop_result, registered)
    if isinstance(node, ast.While):
        loop_result = _merge_bindings((bindings, _block_result(node.body, bindings, registered)))
        return _block_result(node.orelse, loop_result, registered)
    if isinstance(node, ast.With | ast.AsyncWith):
        body_bindings = dict(bindings)
        for item in node.items:
            if item.optional_vars is not None:
                body_bindings = _without_names(body_bindings, _stored_names(item.optional_vars))
        return _block_result(node.body, body_bindings, registered)
    return dict(bindings)


class _ScopeBindingMapper:
    def __init__(self) -> None:
        self.by_node: dict[ast.AST, dict[str, str]] = {}

    def build(
        self,
        tree: ast.Module,
        registered: dict[str, str] | None,
    ) -> dict[ast.AST, dict[str, str]]:
        module_bindings = _block_result(tree.body, {}, registered)
        self.by_node[tree] = dict(module_bindings)
        self.visit_block(tree.body, {}, module_bindings, registered)
        for node in ast.walk(tree):
            self.by_node.setdefault(node, dict(module_bindings))
        return self.by_node

    def visit_block(
        self,
        statements: Iterable[ast.stmt],
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None = None,
    ) -> dict[str, str]:
        result = dict(bindings)
        for statement in statements:
            result = self.visit(statement, result, closure_bindings, registered)
        return result

    def visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
    ) -> None:
        self.by_node[node] = dict(bindings)
        first, *remaining = node.generators
        self.visit(first.iter, bindings, closure_bindings)
        targets = {
            target.id
            for generator in node.generators
            for target in ast.walk(generator.target)
            if isinstance(target, ast.Name)
        }
        inner_bindings = {name: target for name, target in bindings.items() if name not in targets}
        self.visit(first.target, inner_bindings, inner_bindings)
        for condition in first.ifs:
            self.visit(condition, inner_bindings, inner_bindings)
        for generator in remaining:
            self.visit(generator.iter, inner_bindings, inner_bindings)
            self.visit(generator.target, inner_bindings, inner_bindings)
            for condition in generator.ifs:
                self.visit(condition, inner_bindings, inner_bindings)
        if isinstance(node, ast.DictComp):
            self.visit(node.key, inner_bindings, inner_bindings)
            self.visit(node.value, inner_bindings, inner_bindings)
        else:
            self.visit(node.elt, inner_bindings, inner_bindings)

    def visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None,
    ) -> dict[str, str]:
        for expression in _boundary_expressions(node):
            self.visit(expression, bindings, closure_bindings)
        local_bindings = _without_names(closure_bindings, _argument_names(node.args))
        local_closure_bindings = _block_result(node.body, local_bindings)
        self.visit_block(node.body, local_bindings, local_closure_bindings)
        return _definition_result(bindings, node, registered)

    def visit_class(
        self,
        node: ast.ClassDef,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None,
    ) -> dict[str, str]:
        for expression in [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]:
            self.visit(expression, bindings, closure_bindings)
        class_bindings = dict(bindings)
        for statement in node.body:
            class_bindings = self.visit(statement, class_bindings, closure_bindings)
        return _definition_result(bindings, node, registered)

    def visit_lambda(
        self,
        node: ast.Lambda,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
    ) -> dict[str, str]:
        for expression in _boundary_expressions(node):
            self.visit(expression, bindings, closure_bindings)
        local_bindings = _without_names(closure_bindings, _argument_names(node.args))
        self.visit(node.body, local_bindings, local_bindings)
        return dict(bindings)

    def visit_if(
        self,
        node: ast.If,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None,
    ) -> dict[str, str]:
        self.visit(node.test, bindings, closure_bindings)
        return _merge_bindings(
            (
                self.visit_block(node.body, bindings, closure_bindings, registered),
                self.visit_block(node.orelse, bindings, closure_bindings, registered),
            )
        )

    def visit_try(
        self,
        node: ast.Try | ast.TryStar,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None,
    ) -> dict[str, str]:
        normal_result = self.visit_block(node.body, bindings, closure_bindings, registered)
        normal_result = self.visit_block(node.orelse, normal_result, closure_bindings, registered)
        branches = [normal_result]
        for handler in node.handlers:
            self.by_node[handler] = dict(bindings)
            if handler.type is not None:
                self.visit(handler.type, bindings, closure_bindings)
            handler_bindings = bindings
            if handler.name is not None:
                handler_bindings = _without_names(handler_bindings, (handler.name,))
            handler_result = self.visit_block(handler.body, handler_bindings, closure_bindings, registered)
            if handler.name is not None:
                handler_result = _without_names(handler_result, (handler.name,))
            branches.append(handler_result)
        merged = _merge_bindings(branches)
        return self.visit_block(node.finalbody, merged, closure_bindings, registered)

    def visit_match(
        self,
        node: ast.Match,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None,
    ) -> dict[str, str]:
        self.visit(node.subject, bindings, closure_bindings)
        branches = [bindings]
        for case in node.cases:
            self.by_node[case] = dict(bindings)
            self.visit(case.pattern, bindings, closure_bindings)
            if case.guard is not None:
                self.visit(case.guard, bindings, closure_bindings)
            branches.append(self.visit_block(case.body, bindings, closure_bindings, registered))
        return _merge_bindings(branches)

    def visit_loop(
        self,
        node: ast.For | ast.AsyncFor | ast.While,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None,
    ) -> dict[str, str]:
        body_bindings = bindings
        if isinstance(node, ast.For | ast.AsyncFor):
            self.visit(node.iter, bindings, closure_bindings)
            self.visit(node.target, bindings, closure_bindings)
            body_bindings = _without_names(bindings, _stored_names(node.target))
        else:
            self.visit(node.test, bindings, closure_bindings)
        body_result = self.visit_block(node.body, body_bindings, closure_bindings, registered)
        loop_result = _merge_bindings((bindings, body_result))
        return self.visit_block(node.orelse, loop_result, closure_bindings, registered)

    def visit_with(
        self,
        node: ast.With | ast.AsyncWith,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None,
    ) -> dict[str, str]:
        body_bindings = dict(bindings)
        for item in node.items:
            self.by_node[item] = dict(body_bindings)
            self.visit(item.context_expr, body_bindings, closure_bindings)
            if item.optional_vars is not None:
                self.visit(item.optional_vars, body_bindings, closure_bindings)
                body_bindings = _without_names(body_bindings, _stored_names(item.optional_vars))
        return self.visit_block(node.body, body_bindings, closure_bindings, registered)

    def visit(
        self,
        node: ast.AST,
        bindings: dict[str, str],
        closure_bindings: dict[str, str],
        registered: dict[str, str] | None = None,
    ) -> dict[str, str]:
        self.by_node[node] = dict(bindings)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return self.visit_function(node, bindings, closure_bindings, registered)
        if isinstance(node, ast.Lambda):
            return self.visit_lambda(node, bindings, closure_bindings)
        if isinstance(node, ast.ClassDef):
            return self.visit_class(node, bindings, closure_bindings, registered)
        if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            self.visit_comprehension(node, bindings, closure_bindings)
            return dict(bindings)
        if isinstance(node, ast.If):
            return self.visit_if(node, bindings, closure_bindings, registered)
        if isinstance(node, ast.Try | ast.TryStar):
            return self.visit_try(node, bindings, closure_bindings, registered)
        if isinstance(node, ast.Match):
            return self.visit_match(node, bindings, closure_bindings, registered)
        if isinstance(node, ast.For | ast.AsyncFor | ast.While):
            return self.visit_loop(node, bindings, closure_bindings, registered)
        if isinstance(node, ast.With | ast.AsyncWith):
            return self.visit_with(node, bindings, closure_bindings, registered)
        for child in ast.iter_child_nodes(node):
            self.visit(child, bindings, closure_bindings)
        if isinstance(node, ast.Import | ast.ImportFrom):
            return {**bindings, **_import_targets(node)}
        if isinstance(node, ast.Assign):
            return _without_names(bindings, (name for target in node.targets for name in _stored_names(target)))
        if isinstance(node, ast.AnnAssign | ast.AugAssign):
            return _without_names(bindings, _stored_names(node.target))
        if isinstance(node, ast.Delete):
            return _without_names(bindings, (name for target in node.targets for name in _stored_names(target)))
        return dict(bindings)


def scope_bindings(
    tree: ast.Module,
    registered: dict[str, str] | None = None,
) -> dict[ast.AST, dict[str, str]]:
    """Map every node to the imports and registered definitions visible in its lexical scope.

    The model does not treat comprehension-internal walrus assignments or definition-time default expressions as enclosing-scope bindings. Match pattern captures (MatchAs, MatchStar, and MatchMapping rest) do not shadow imports. Comprehension bodies inherit the enclosing scope as written, and class-body bindings are not special-cased. These are accepted limits for a review aid, not a security boundary.
    """
    return _ScopeBindingMapper().build(tree, registered)


def import_bindings(tree: ast.Module) -> dict[str, str]:
    """Map names bound by imports in the module scope to their qualified targets."""
    return scope_bindings(tree)[tree]


def resolve_call_target(node: ast.AST, bindings: dict[str, str]) -> str | None:
    """Return the qualified target for a call or callable expression."""
    function = node.func if isinstance(node, ast.Call) else node
    parts = []
    while isinstance(function, ast.Attribute):
        parts.append(function.attr)
        function = function.value
    if not isinstance(function, ast.Name):
        return None
    parts.append(function.id)
    parts.reverse()
    target = bindings.get(parts[0])
    if target is None:
        return None
    parts[0] = target
    return ".".join(parts)


def _boundary_expressions(node: ast.AST) -> Iterator[ast.AST]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield from node.decorator_list
        yield from node.args.defaults
        yield from (default for default in node.args.kw_defaults if default is not None)
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        yield from (argument.annotation for argument in arguments if argument.annotation is not None)
        if node.returns is not None:
            yield node.returns
    elif isinstance(node, ast.Lambda):
        yield from node.args.defaults
        yield from (default for default in node.args.kw_defaults if default is not None)
    elif isinstance(node, ast.ClassDef):
        yield from node.decorator_list
        yield from node.bases
        yield from (keyword.value for keyword in node.keywords)
        yield from node.body


def scoped_walk(nodes: ast.AST | Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Yield one scope: function and lambda bodies are boundaries; class bodies run in the enclosing scope and are walked; definition-time expressions of every nested definition are walked."""
    roots = (nodes,) if isinstance(nodes, ast.AST) else tuple(nodes)
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPE_BOUNDARIES):
            stack.extend(reversed(tuple(_boundary_expressions(node))))
            continue
        stack.extend(reversed(tuple(ast.iter_child_nodes(node))))
