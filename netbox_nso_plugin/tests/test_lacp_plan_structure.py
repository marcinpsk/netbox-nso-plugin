# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""LACP replacement inserts must follow complete episode retirement planning."""

import ast
from pathlib import Path
from unittest import TestCase


class TestLACPReplacementPlanning(TestCase):
    def test_overlay_inserts_have_one_episode_aware_planner(self):
        source = Path(__file__).resolve().parents[1] / "lacp_reconciler.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        insert_sites = []
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for method in (node for node in cls.body if isinstance(node, ast.FunctionDef)):
                for call in (node for node in ast.walk(method) if isinstance(node, ast.Call)):
                    if any(keyword.arg == "force_insert" for keyword in call.keywords):
                        insert_sites.append((cls.name, method.name, ast.unparse(call.func)))
        self.assertEqual(insert_sites, [("_LACPReconcilePlanner", "overlay_save", "planned_save")])
        planner = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        plan = next(node for node in planner.body if isinstance(node, ast.FunctionDef) and node.name == "plan")
        calls = [
            ast.unparse(node.value.func)
            for node in plan.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        self.assertLess(calls.index("self.retire_lost_overlays"), calls.index("self.publish_reported_overlays"))
