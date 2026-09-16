# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared AST traversal helpers for structural tests."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


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


def scoped_walk(nodes: ast.AST | Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Yield boundary decorators/defaults/annotations/bases/keywords in the parent scope, but never bodies."""
    roots = (nodes,) if isinstance(nodes, ast.AST) else tuple(nodes)
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPE_BOUNDARIES):
            stack.extend(reversed(tuple(_boundary_expressions(node))))
            continue
        stack.extend(reversed(tuple(ast.iter_child_nodes(node))))
