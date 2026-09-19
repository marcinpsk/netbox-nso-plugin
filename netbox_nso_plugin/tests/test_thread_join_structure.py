# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require test thread joins to use bounded waits."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

_TESTS_ROOT = Path(__file__).resolve().parent


def _imports_threading(tree: ast.Module) -> bool:
    return any(
        (isinstance(node, ast.Import) and any(alias.name == "threading" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "threading")
        for node in ast.walk(tree)
    )


def _unbounded_thread_joins(tree: ast.Module) -> list[ast.Call]:
    if not _imports_threading(tree):
        return []
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "join":
            continue
        timeout = next((keyword.value for keyword in node.keywords if keyword.arg == "timeout"), None)
        positional_none = node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value is None
        if (
            positional_none
            or (not node.args and timeout is None)
            or (isinstance(timeout, ast.Constant) and timeout.value is None)
        ):
            violations.append(node)
    return violations


class TestThreadJoinStructure(SimpleTestCase):
    def test_test_thread_joins_use_bounded_waits(self):
        violations = []
        for path in sorted(_TESTS_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(f"{path.name}:{node.lineno}" for node in _unbounded_thread_joins(tree))

        self.assertEqual(violations, [], "\n".join(violations))

    def test_timeout_none_is_unbounded_but_a_numeric_timeout_is_bounded(self):
        tree = ast.parse(
            """\
import threading
worker = threading.Thread()
worker.join(timeout=None)
worker.join(timeout=5)
"""
        )

        self.assertEqual([node.lineno for node in _unbounded_thread_joins(tree)], [3])

    def test_imported_thread_with_zero_argument_join_is_unbounded(self):
        tree = ast.parse(
            """\
from threading import Thread
worker = Thread()
worker.join()
"""
        )

        self.assertEqual([node.lineno for node in _unbounded_thread_joins(tree)], [3])

    def test_positional_none_is_unbounded_but_other_joins_are_allowed(self):
        tree = ast.parse(
            """\
import threading
worker = threading.Thread()
worker.join(None)
worker.join(5)
worker.join(timeout=5)
",".join(items)
"""
        )

        self.assertEqual([node.lineno for node in _unbounded_thread_joins(tree)], [3])
