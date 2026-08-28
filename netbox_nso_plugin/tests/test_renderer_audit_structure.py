# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Repository checks for the renderer pre-capture audit boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

PLUGIN = Path(__file__).resolve().parent.parent


def _functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}


def _calls(function):
    return [ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)]


class TestRendererCaptureSitesAreAuditFronted(SimpleTestCase):
    def test_public_and_recursive_capture_entry_points_call_the_audit(self):
        delivery = _functions(PLUGIN / "delivery.py")
        drain = _functions(PLUGIN / "drain.py")
        views = _functions(PLUGIN / "views.py")

        self.assertIn("audit_renderer_scopes", _calls(delivery["deliver"]))
        self.assertIn("audit_renderer_scopes", _calls(drain["claim"]))
        self.assertIn("audit_renderer_scopes", _calls(drain["_drain_once"]))
        self.assertIn("audit_renderer_scopes", _calls(views["_prepare_apply"]))

    def test_every_production_claim_call_uses_an_audited_entry_point(self):
        found = set()
        for path in sorted(PLUGIN.rglob("*.py")):
            if {"tests", "migrations"} & set(path.relative_to(PLUGIN).parts):
                continue
            functions = _functions(path)
            for function_name, function in functions.items():
                for call in _calls(function):
                    if call in {"claim", "drain.claim", "_claim_after_audit"}:
                        found.add((path.name, function_name, call))

        self.assertEqual(
            found,
            {
                ("drain.py", "_claim_or_wait", "_claim_after_audit"),
                ("drain.py", "claim", "_claim_after_audit"),
                ("nso_intent_deployment_gate.py", "_verify", "drain.claim"),
            },
        )

    def test_every_current_payload_render_is_owned_by_a_reviewed_capture_or_proof_path(self):
        found = set()
        for filename in ("delivery.py", "drain.py", "renderer_audit.py", "renderer_writer.py"):
            path = PLUGIN / filename
            for function_name, function in _functions(path).items():
                for call in _calls(function):
                    if call in {"render", "delivery.render"}:
                        found.add((filename, function_name, call))

        self.assertEqual(
            found,
            {
                ("delivery.py", "deliver", "render"),
                ("drain.py", "_form", "delivery.render"),
                ("drain.py", "_form_backfill", "delivery.render"),
                ("drain.py", "_form_store_only", "delivery.render"),
                ("drain.py", "_sent_wire_digest", "delivery.render"),
                ("drain.py", "_take_direct_entries", "delivery.render"),
                ("drain.py", "_takeover", "delivery.render"),
                ("renderer_audit.py", "_optimistic_candidates", "delivery.render"),
                ("renderer_audit.py", "_repair_candidates", "delivery.render"),
                ("renderer_writer.py", "_finalize_fingerprints", "delivery.render"),
            },
        )
