# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Repository checks for the renderer pre-capture audit boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

from ._ast_scope import import_bindings, resolve_call_target, scope_bindings, scoped_walk

PLUGIN = Path(__file__).resolve().parent.parent
_CLAIM_TARGET = "netbox_nso_plugin.drain.claim"
_CLAIM_AFTER_AUDIT_TARGET = "netbox_nso_plugin.drain._claim_after_audit"
_RENDER_TARGET = "netbox_nso_plugin.delivery.render"


def _functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.setdefault(node.name, []).append(node)
    return functions


def _calls(functions):
    return [
        ast.unparse(node.func)
        for function in functions
        for node in scoped_walk(function.body)
        if isinstance(node, ast.Call)
    ]


def _call_sites(module_path, tree, names):
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    registered = {}
    if module_path == "delivery.py":
        registered["render"] = _RENDER_TARGET
    elif module_path == "drain.py":
        registered["claim"] = _CLAIM_TARGET
        registered["_claim_after_audit"] = _CLAIM_AFTER_AUDIT_TARGET
    bindings = scope_bindings(tree, registered)
    functions = {"<module>": [tree]}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            qualified_name = ["<lambda>" if isinstance(node, ast.Lambda) else node.name]
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                    qualified_name.append("<lambda>" if isinstance(parent, ast.Lambda) else parent.name)
                parent = parents.get(parent)
            functions.setdefault(".".join(reversed(qualified_name)), []).append(node)
    return {
        (module_path, function_name, ast.unparse(node.func))
        for function_name, function in functions.items()
        for candidate in function
        for node in scoped_walk(candidate.body)
        if isinstance(node, ast.Call) and resolve_call_target(node, bindings[node]) in names
    }


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
        candidates = functions.get(pending.pop())
        if candidates is None:
            continue
        for call in _calls(candidates):
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
    for local_name, target in import_bindings(tree).items():
        if target == "netbox_nso_plugin.delivery":
            names.add(f"{local_name}.render")
        elif target == _RENDER_TARGET:
            names.add(local_name)
    return names


class TestStructureScanHelpers(SimpleTestCase):
    def test_same_named_functions_all_remain_reachable(self):
        from types import SimpleNamespace

        source = "def entry():\n    target()\nclass Other:\n    def entry(self):\n        sibling()\n"
        functions = _functions(SimpleNamespace(read_text=lambda **_kwargs: source))

        self.assertEqual(set(_reachable_calls(functions, "entry")), {"target", "sibling"})

    def test_delivery_module_aliases_are_detected(self):
        from types import SimpleNamespace

        source = "from . import delivery as dlv\ndef capture():\n    dlv.render()\n"
        path = SimpleNamespace(name="capture.py", read_text=lambda **_kwargs: source)

        self.assertIn("dlv.render", _render_names(path))

    def test_call_sites_resolve_delivery_import_aliases(self):
        source = """\
from netbox_nso_plugin import delivery as dlv
from netbox_nso_plugin.delivery import render as render_payload
from . import delivery as local_delivery

def module_alias():
    dlv.render()

def symbol_alias():
    render_payload()

def relative_alias():
    local_delivery.render()
"""

        self.assertEqual(
            _call_sites("sample.py", ast.parse(source), {"netbox_nso_plugin.delivery.render"}),
            {
                ("sample.py", "module_alias", "dlv.render"),
                ("sample.py", "symbol_alias", "render_payload"),
                ("sample.py", "relative_alias", "local_delivery.render"),
            },
        )

    def test_call_sites_keep_function_imports_in_their_lexical_scope(self):
        source = """\
def plugin_call():
    from netbox_nso_plugin import delivery as dlv
    dlv.render()

def unrelated(dlv):
    dlv.render()
"""

        self.assertEqual(
            _call_sites("sample.py", ast.parse(source), {_RENDER_TARGET}),
            {("sample.py", "plugin_call", "dlv.render")},
        )

    def test_call_sites_ignore_unbound_parameter_methods(self):
        source = """\
def unrelated(dlv):
    dlv.render()
"""

        self.assertEqual(_call_sites("sample.py", ast.parse(source), {"dlv.render"}), set())

    def test_call_sites_preserve_enclosing_definition_identity(self):
        source = """\
from netbox_nso_plugin import delivery, drain

def capture():
    drain.claim()
    delivery.render()

class First:
    def capture(self):
        drain.claim()
        delivery.render()

class Second:
    def capture(self):
        drain.claim()
        delivery.render()
"""
        names = {_CLAIM_TARGET, _RENDER_TARGET}
        allowed = {
            ("sample.py", "First.capture", "drain.claim"),
            ("sample.py", "First.capture", "delivery.render"),
        }

        offenders = _call_sites("sample.py", ast.parse(source), names) - allowed

        self.assertEqual(
            offenders,
            {
                ("sample.py", "capture", "drain.claim"),
                ("sample.py", "capture", "delivery.render"),
                ("sample.py", "Second.capture", "drain.claim"),
                ("sample.py", "Second.capture", "delivery.render"),
            },
        )

    def test_call_sites_inventory_nested_class_bodies(self):
        source = """\
from netbox_nso_plugin import delivery, drain

def entry():
    class Cfg:
        claimed = drain.claim()
        rendered = delivery.render()
"""

        self.assertEqual(
            _call_sites("sample.py", ast.parse(source), {_CLAIM_TARGET, _RENDER_TARGET}),
            {
                ("sample.py", "entry", "drain.claim"),
                ("sample.py", "entry", "delivery.render"),
            },
        )

    def test_call_sites_inventory_definition_time_expressions(self):
        source = """\
from netbox_nso_plugin import audit, delivery, drain
from netbox_nso_plugin.config import build_base

def capture(value=delivery.render("snmp", 1, 42)):
    pass

@audit.decorator()
def decorated():
    pass

class Config(build_base()):
    pass

def outer():
    def nested(value=drain.claim()):
        pass
"""

        self.assertEqual(
            _call_sites(
                "sample.py",
                ast.parse(source),
                {
                    "netbox_nso_plugin.audit.decorator",
                    "netbox_nso_plugin.config.build_base",
                    _CLAIM_TARGET,
                    _RENDER_TARGET,
                },
            ),
            {
                ("sample.py", "<module>", "audit.decorator"),
                ("sample.py", "<module>", "build_base"),
                ("sample.py", "<module>", "delivery.render"),
                ("sample.py", "outer", "drain.claim"),
            },
        )

    def test_nested_scopes_do_not_certify_outer_entries(self):
        from types import SimpleNamespace

        source = """\
from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

def direct():
    audit_renderer_scopes()

def nested_function():
    def helper():
        audit_renderer_scopes()

def nested_async_function():
    async def async_helper():
        audit_renderer_scopes()

def nested_lambda():
    helper = lambda: audit_renderer_scopes()

def nested_class():
    class Helper:
        def audit(self):
            audit_renderer_scopes()

def called_helper():
    def audit_helper():
        audit_renderer_scopes()

    audit_helper()
"""
        functions = _functions(SimpleNamespace(read_text=lambda **_kwargs: source))
        entries = {
            "direct",
            "nested_function",
            "nested_async_function",
            "nested_lambda",
            "nested_class",
            "called_helper",
        }

        offenders = {entry for entry in entries if "audit_renderer_scopes" not in _reachable_calls(functions, entry)}

        self.assertEqual(
            offenders,
            {"nested_function", "nested_async_function", "nested_lambda", "nested_class"},
        )
        self.assertEqual(
            _call_sites(
                "sample.py",
                ast.parse(source),
                {"netbox_nso_plugin.renderer_audit.audit_renderer_scopes"},
            ),
            {
                ("sample.py", "direct", "audit_renderer_scopes"),
                ("sample.py", "nested_function.helper", "audit_renderer_scopes"),
                ("sample.py", "nested_async_function.async_helper", "audit_renderer_scopes"),
                ("sample.py", "nested_lambda.<lambda>", "audit_renderer_scopes"),
                ("sample.py", "nested_class.Helper.audit", "audit_renderer_scopes"),
                ("sample.py", "called_helper.audit_helper", "audit_renderer_scopes"),
            },
        )


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
            module_path = path.relative_to(PLUGIN).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found |= _call_sites(module_path, tree, {_CLAIM_TARGET, _CLAIM_AFTER_AUDIT_TARGET})

        self.assertEqual(
            found,
            {
                ("drain.py", "_claim_or_wait", "_claim_after_audit"),
                ("drain.py", "claim", "_claim_after_audit"),
                ("management/commands/nso_intent_deployment_gate.py", "Command._verify", "drain.claim"),
            },
        )

    def test_every_current_payload_render_is_owned_by_a_reviewed_capture_or_proof_path(self):
        found = set()
        for path in sorted(PLUGIN.rglob("*.py")):
            if {"tests", "migrations"} & set(path.relative_to(PLUGIN).parts):
                continue
            module_path = path.relative_to(PLUGIN).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found |= _call_sites(module_path, tree, {_RENDER_TARGET})

        self.assertEqual(
            found,
            {
                ("delivery.py", "deliver", "render"),
                ("drain.py", "_form", "delivery.render"),
                ("drain.py", "_form_backfill", "delivery.render"),
                ("drain.py", "_form_store_only", "delivery.render"),
                ("drain.py", "_sent_wire_digest", "delivery.render"),
                ("drain.py", "_takeover", "delivery.render"),
                ("renderer_audit.py", "_optimistic_candidates", "delivery.render"),
                ("renderer_audit.py", "_repair_candidates", "delivery.render"),
                ("renderer_writer.py", "_finalize_fingerprints", "delivery.render"),
                ("switching_preparation.py", "_capture", "delivery.render"),
                ("switching_preparation.py", "cancel_if_rendered", "delivery.render"),
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
