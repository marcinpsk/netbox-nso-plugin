# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1), pin O1.16: the forced-push sites, enumerated by the compiler.

A forced call is its own logical operation: it does not enqueue, does not coalesce and is
never dropped as unchanged. The enumeration is taken with the AST rather than with a text
search, because a wrapped call can span several lines. The scan fails when an unowned site
appears, so a new site is triaged before its outbox omission causes harm.

A forced call inside an open transaction raises: the claim sets its own isolation level,
which PostgreSQL accepts only before a transaction's first statement, and the send must hold
no lock at all. Neither is possible nested in a caller's block.
"""

from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase

from ._ast_scope import scoped_walk
from ._outbox_case import make_managed, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

PLUGIN = Path(__file__).resolve().parent.parent

#: (module, enclosing function, callee) → how many forced calls that site makes. Counted, not
#: set-collected: the Apply forces several scopes through one call in one function, and a set
#: would report one site where repeated calls could exist.
FORCED_PUSH_SITES = {
    ("intent_drift.py", "resync_intent", "drain.drain_key"): 1,
    ("intent_drift.py", "resync_static_route_intent_fleet", "drain.push_now"): 1,
    ("link_role.py", "_push_provisioned", "drain.push_now"): 1,
    ("views.py", "_prepare_apply", "drain.push_now"): 1,
    # Switching snapshots prepare after store-only pushes and report any partial failure.
    ("views.py", "_push_direct_snapshots", "drain.push_now"): 1,
    ("delivery.py", "deliver", "drain.push_now"): 1,
    # The Apply's SNMP refresh reads the OUTCOME rather than the answer: it aborts on a
    # refusal alone, which ``push_now`` reports as the same ``None`` as a failure.
    ("views.py", "_prepare_apply", "drain.drain_key"): 1,
}
#: The claim a forced push routes through is itself forced. It is not a push site, and it is
#: named here so the exclusion cannot quietly widen to a module that is one.
FORCED_CLAIM_SITE = ("drain.py", "_claim_or_wait", "_claim_after_audit")
#: The deployment gate forms one known no-deletion claim, sends it, and verifies its exact
#: receipt before it resumes mutation. It is an operator protocol, not a product push site.
DEPLOYMENT_VERIFICATION_CLAIM_SITE = (
    "nso_intent_deployment_gate.py",
    "_verify",
    "drain.claim",
)


def _function_call_names(source: str) -> dict[str, set[str]]:
    """Return direct lexical calls for each top-level function in parsed source."""
    tree = ast.parse(source)
    return {
        function.name: {ast.unparse(node.func) for node in scoped_walk(function.body) if isinstance(node, ast.Call)}
        for function in tree.body
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _forced_calls() -> collections.Counter:
    """Every production call passing ``force=True``, as the compiler sees it."""
    found: collections.Counter = collections.Counter()
    for path in sorted(PLUGIN.rglob("*.py")):
        # Relative to the plugin: an ancestor directory of that name would skip every module.
        if {"tests", "migrations"} & set(path.relative_to(PLUGIN).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            forced = any(
                keyword.arg == "force" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                for keyword in node.keywords
            )
            if not forced:
                continue
            enclosing = ""
            walker = node
            while walker in parents:
                walker = parents[walker]
                if isinstance(walker, ast.FunctionDef | ast.AsyncFunctionDef):
                    enclosing = walker.name
                    break
            found[path.name, enclosing, ast.unparse(node.func)] += 1
    return found


class TestForcedPushSitesAreEnumerated(SimpleTestCase):
    """O1.16: forced calls found by the AST, with a scan that fails on an unowned site."""

    def test_every_forced_site_is_still_where_the_enumeration_says(self):
        found = _forced_calls()
        for site, count in sorted(FORCED_PUSH_SITES.items()):
            with self.subTest(site=site):
                assert found[site] == count

    def test_no_forced_call_exists_outside_the_enumeration(self):
        expected = collections.Counter(FORCED_PUSH_SITES)
        expected[FORCED_CLAIM_SITE] += 1
        expected[DEPLOYMENT_VERIFICATION_CLAIM_SITE] += 1
        assert _forced_calls() == expected
        assert sum(FORCED_PUSH_SITES.values()) == 7

    def test_every_forced_site_routes_through_the_claim(self):
        """A forced call is a claim with those flags, never a push around it (§4.2).

        Both entry points ARE the claim: ``push_now`` is ``drain_key`` returning the answer
        instead of the outcome, so a site needing the outcome takes the other one and still
        sends nothing around the protocol.
        """
        import inspect

        from netbox_nso_plugin import drain

        callees = {site[2] for site in FORCED_PUSH_SITES}
        assert callees == {"drain.push_now", "drain.drain_key"}
        for entry_point in (drain.push_now, drain.drain_key):
            calls = _function_call_names(inspect.getsource(entry_point))[entry_point.__name__]
            assert calls == {"_drain_once"}, f"{entry_point.__name__} bypasses the shared drain: {calls}"

    def test_nested_drain_calls_do_not_certify_the_outer_entry_point(self):
        nested_sources = {
            "function": "def push_now():\n    def helper():\n        _drain_once()\n    return None\n",
            "async function": "def push_now():\n    async def helper():\n        _drain_once()\n    return None\n",
            "lambda": "def push_now():\n    helper = lambda: _drain_once()\n    return None\n",
            "class": (
                "def push_now():\n"
                "    class Helper:\n"
                "        def drain(self):\n"
                "            _drain_once()\n"
                "    return None\n"
            ),
        }
        for boundary, source in nested_sources.items():
            with self.subTest(boundary=boundary):
                assert _function_call_names(source)["push_now"] == set()

        direct_source = "def drain_key():\n    return _drain_once()\n"
        assert _function_call_names(direct_source)["drain_key"] == {"_drain_once"}

        called_helper_source = "def push_now():\n    def helper():\n        return _drain_once()\n    return helper()\n"
        assert _function_call_names(called_helper_source)["push_now"] == {"helper"}

    def test_class_body_calls_belong_to_the_enclosing_scope(self):
        sources = {
            "class body": (
                "def push_now():\n    class Config:\n        bypass_claim()\n    return _drain_once()\n",
                {"bypass_claim", "_drain_once"},
            ),
            "method body": (
                "def push_now():\n"
                "    class Config:\n"
                "        def configure(self):\n"
                "            bypass_claim()\n"
                "    return _drain_once()\n",
                {"_drain_once"},
            ),
            "nested class body": (
                "def push_now():\n"
                "    class Config:\n"
                "        class Nested:\n"
                "            bypass_claim()\n"
                "    return _drain_once()\n",
                {"bypass_claim", "_drain_once"},
            ),
        }
        for boundary, (source, expected) in sources.items():
            with self.subTest(boundary=boundary):
                assert _function_call_names(source)["push_now"] == expected

        class_body_calls = _function_call_names(sources["class body"][0])["push_now"]
        with self.assertRaisesRegex(AssertionError, "bypass_claim"):
            assert class_body_calls == {"_drain_once"}, f"extra calls: {class_body_calls - {'_drain_once'}}"

    def test_definition_time_calls_belong_to_the_enclosing_scope(self):
        sources = {
            "function default": (
                "def push_now():\n    def unused(value=bypass_claim()):\n        pass\n    return _drain_once()\n",
                {"bypass_claim", "_drain_once"},
            ),
            "function decorator": (
                "def push_now():\n"
                "    @forbidden_decorator()\n"
                "    def unused():\n"
                "        _drain_once()\n"
                "    return None\n",
                {"forbidden_decorator"},
            ),
            "lambda default": (
                "def push_now():\n"
                "    def unused(callback=lambda value=forbidden_default(): _drain_once()):\n"
                "        pass\n"
                "    return None\n",
                {"forbidden_default"},
            ),
            "class base": (
                "def push_now():\n"
                "    class Helper(forbidden_base()):\n"
                "        def drain(self):\n"
                "            _drain_once()\n"
                "    return None\n",
                {"forbidden_base"},
            ),
        }
        for boundary, (source, expected) in sources.items():
            with self.subTest(boundary=boundary):
                assert _function_call_names(source)["push_now"] == expected

        default_calls = _function_call_names(sources["function default"][0])["push_now"]
        with self.assertRaisesRegex(AssertionError, "bypass_claim"):
            assert default_calls == {"_drain_once"}, f"extra calls: {default_calls - {'_drain_once'}}"

    def test_the_scan_reads_calls_a_single_line_search_cannot(self):
        """The named trap: a wrapped call is invisible to a grep and plain to the compiler.

        Asserted against a source string rather than against the tree, because whether the
        formatter puts a forced call on one line is not the property under test.
        """
        source = (
            "def wrapped():\n"
            "    drain.push_now(\n"
            "        device_id,\n"
            "        scope,\n"
            "        force=True,\n"
            "    )\n"
            "def inline():\n"
            "    drain.push_now(device_id, scope, force=True)\n"
        )
        grepped = [line for line in source.splitlines() if re.search(r"\(.*force=True\)", line)]
        assert len(grepped) == 1, "a single-line search sees only the call that fits on one line"

        found = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and any(k.arg == "force" and getattr(k.value, "value", None) is True for k in node.keywords)
        ]
        assert len(found) == 2, "the compiler sees both, which is why the enumeration is an AST scan"


class TestAForcedCallInsideATransactionRaises(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O1.16: silently deferring inside a caller's block is what this refuses to do."""

    def setUp(self):
        super().setUp()
        self.device, self.mgmt = make_managed("forced", 7670)
        own_vlan(self.mgmt, 970, "forced")

    def test_a_forced_drain_inside_an_atomic_block_raises(self):
        from netbox_nso_plugin import drain

        with transaction.atomic(), self.assertRaises(RuntimeError):
            drain.drain_key(self.device.pk, "vlan", force=True)

    def test_the_forced_sites_own_entry_point_raises_too(self):
        """``push_now`` is the other forced entry point, so it is what the pin must refuse too."""
        from netbox_nso_plugin import drain

        with transaction.atomic(), self.assertRaises(RuntimeError):
            drain.push_now(self.device.pk, "vlan", force=True)

    def test_a_claim_inside_an_atomic_block_raises(self):
        from netbox_nso_plugin import drain

        with transaction.atomic(), self.assertRaises(RuntimeError):
            drain.claim(self.device.pk, "vlan")
