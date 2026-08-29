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


def _reachable_calls(functions, entry):
    """Every call *entry* reaches through its own module's helpers.

    An entry point may front its captures through a private helper — ``views._prepare_apply``
    does — and the property is about the capture being audited, not about which frame makes
    the call.
    """
    seen = {entry}
    pending = [entry]
    calls = []
    while pending:
        function = functions.get(pending.pop())
        if function is None:
            continue
        for call in _calls(function):
            calls.append(call)
            if call in functions and call not in seen:
                seen.add(call)
                pending.append(call)
    return calls


def _render_names(path):
    """Every name this module can reach ``delivery.render`` through.

    A module that imports the function directly calls it under a bare (or aliased) name, so
    the sweep resolves the import rather than trusting the ``delivery.`` prefix.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"delivery.render"}
    if path.name == "delivery.py":
        names.add("render")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[-1] == "delivery":
            names |= {alias.asname or alias.name for alias in node.names if alias.name == "render"}
    return names


class TestRendererCaptureSitesAreAuditFronted(SimpleTestCase):
    def test_public_and_recursive_capture_entry_points_call_the_audit(self):
        delivery = _functions(PLUGIN / "delivery.py")
        drain = _functions(PLUGIN / "drain.py")
        views = _functions(PLUGIN / "views.py")

        self.assertIn("audit_renderer_scopes", _calls(delivery["deliver"]))
        self.assertIn("audit_renderer_scopes", _calls(drain["claim"]))
        self.assertIn("audit_renderer_scopes", _calls(drain["_drain_once"]))
        self.assertIn("audit_renderer_scopes", _reachable_calls(views, "_prepare_apply"))

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
        for path in sorted(PLUGIN.rglob("*.py")):
            if {"tests", "migrations"} & set(path.relative_to(PLUGIN).parts):
                continue
            names = _render_names(path)
            for function_name, function in _functions(path).items():
                for call in _calls(function):
                    if call in names:
                        found.add((path.name, function_name, call))

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


class TestRendererAuditScopeBudget(SimpleTestCase):
    def test_the_configured_scope_cap_never_falls_below_the_delivery_registry(self):
        """A cap under the registry size fails every pre-capture gate closed.

        Operator Apply, drain, deliver and the baseline cutover all audit the complete key
        set, so the effective cap has to admit it.
        """
        from django.conf import settings

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.renderer_audit import _default_scope_batch_cap

        configured = settings.PLUGINS_CONFIG["netbox_nso_plugin"].get(
            "renderer_audit_scope_batch_cap",
            _default_scope_batch_cap(),
        )

        self.assertGreaterEqual(configured, len(delivery.delivery_keys()))
