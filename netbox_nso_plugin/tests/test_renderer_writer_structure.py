# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Structural backstops for renderer-input writes in production modules."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

_SEMANTIC_WRITER_MODULES = {
    "api/views.py",
    "apply_state.py",
    "bfd_reconciler.py",
    "bgp_reconciler.py",
    "forms.py",
    "interface_mtu_reconciler.py",
    "ip_autoassign.py",
    "isis_reconciler.py",
    "intent_state.py",
    "l2_service_reconciler.py",
    "lacp_reconciler.py",
    "management_lifecycle.py",
    "onboarding.py",
    "ospf_reconciler.py",
    "ownership_planner.py",
    "read_gate.py",
    "reconcile.py",
    "redistribution_reconciler.py",
    "renderer_audit.py",
    "renderer_writer.py",
    "route_policy_reconciler.py",
    "route_policy_diff.py",
    "signals.py",
    "subinterface_reconciler.py",
    "svi_reconciler.py",
    "template_content.py",
    "views.py",
    "vlan_reconciler.py",
}
_MUTATION_METHODS = {
    "add",
    "bulk_create",
    "bulk_update",
    "clear",
    "delete",
    "remove",
    "save",
    "set",
    "update",
}
_MODEL_MODULE_SUFFIXES = (".models",)


def _registered_class_names():
    from netbox_nso_plugin.intent_state import renderer_input_specs

    return {spec.model.__name__ for spec in renderer_input_specs().values()}


def _imports_registered_model(tree, registered_names):
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not (node.module or "").endswith(_MODEL_MODULE_SUFFIXES):
            continue
        if any(alias.name in registered_names or alias.name == "*" for alias in node.names):
            return True
    return False


def _has_direct_mutation(tree):
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATION_METHODS
        for node in ast.walk(tree)
    )


class TestRendererWriterStructure(SimpleTestCase):
    def test_registered_model_mutation_modules_are_reviewed_semantic_writers(self):
        package = Path(__file__).resolve().parents[1]
        registered_names = _registered_class_names()
        offenders = []
        for path in package.rglob("*.py"):
            relative = path.relative_to(package).as_posix()
            if relative.startswith(("migrations/", "tests/")):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            if (
                relative not in _SEMANTIC_WRITER_MODULES
                and _imports_registered_model(tree, registered_names)
                and _has_direct_mutation(tree)
            ):
                offenders.append(relative)

        self.assertEqual(offenders, [])

    def test_no_process_global_sql_or_implicit_permit_guard_remains(self):
        package = Path(__file__).resolve().parents[1]
        forbidden = {
            "_IMPLICIT_PERMITS",
            "_authorize_dml",
            "_begin_delete_implicit",
            "_begin_implicit",
            "_begin_m2m_implicit",
            "_dml_guard",
            "_end_implicit",
            "_end_m2m_implicit",
            "_install_guard",
            "_parse_dml_target",
        }
        found = set()
        for path in package.rglob("*.py"):
            relative = path.relative_to(package).as_posix()
            if relative.startswith(("migrations/", "tests/")):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            found.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in forbidden)

        self.assertEqual(found, set())
