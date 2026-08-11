# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1), pin O1.16: the forced-push sites, enumerated by the compiler.

A forced call is its own logical operation: it does not enqueue, does not coalesce and is
never dropped as unchanged. There are six of them, and the enumeration is taken with the AST
rather than with a text search, because one of the six spreads its call over three lines and
a single-line grep reports five. The scan fails when a seventh appears, so a new site is
triaged rather than discovered later by its absence from the outbox.

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

from ._outbox_case import make_managed, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

PLUGIN = Path(__file__).resolve().parent.parent

#: (module, enclosing function, callee) → how many forced calls that site makes. Counted, not
#: set-collected: the Apply forces twelve scopes through one callee in one function, and a
#: set would report one site where there are twelve calls.
FORCED_PUSH_SITES = {
    ("intent_drift.py", "resync_intent", "drain.push_now"): 1,
    ("intent_drift.py", "resync_static_route_intent_fleet", "drain.push_now"): 1,
    ("link_role.py", "apply_description_for_role", "drain.push_now"): 1,
    ("link_role.py", "_push_provisioned", "drain.push_now"): 1,
    ("views.py", "_prepare_apply", "drain.push_now"): 1,
    # The Apply's SNMP refresh reads the OUTCOME rather than the answer: it aborts on a
    # refusal alone, which ``push_now`` reports as the same ``None`` as a failure.
    ("views.py", "_prepare_apply", "drain.drain_key"): 1,
}
#: The claim a forced push routes through is itself forced. It is not a push site, and it is
#: named here so the exclusion cannot quietly widen to a module that is one.
FORCED_CLAIM_SITE = ("drain.py", "_claim_or_wait", "claim")


def _forced_calls() -> collections.Counter:
    """Every production call passing ``force=True``, as the compiler sees it."""
    found: collections.Counter = collections.Counter()
    for path in sorted(PLUGIN.rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text())
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
    """O1.16: six calls, found by the AST, and a scan that fails when a seventh appears."""

    def test_every_forced_site_is_still_where_the_enumeration_says(self):
        found = _forced_calls()
        for site, count in sorted(FORCED_PUSH_SITES.items()):
            with self.subTest(site=site):
                assert found[site] == count

    def test_there_are_six_forced_calls_and_none_outside_the_enumeration(self):
        expected = collections.Counter(FORCED_PUSH_SITES)
        expected[FORCED_CLAIM_SITE] += 1
        assert _forced_calls() == expected
        assert sum(FORCED_PUSH_SITES.values()) == 6

    def test_every_forced_site_routes_through_the_claim(self):
        """A forced call is a claim with those flags, never a push around it (§4.2).

        Both entry points ARE the claim: ``push_now`` is ``drain_key`` returning the answer
        instead of the outcome, so a site needing the outcome takes the other one and still
        sends nothing around the protocol.
        """
        callees = {site[2] for site in FORCED_PUSH_SITES}
        assert callees == {"drain.push_now", "drain.drain_key"}

    def test_the_scan_reads_calls_a_single_line_search_cannot(self):
        """The named trap: a wrapped call is invisible to a grep and plain to the compiler.

        Asserted against a source string rather than against the tree, because how many of
        the six the formatter happens to fit on one line is not the property under test.
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
        """``push_now`` is what the six sites call, so it is what the pin must refuse."""
        from netbox_nso_plugin import drain

        with transaction.atomic(), self.assertRaises(RuntimeError):
            drain.push_now(self.device.pk, "vlan", force=True)

    def test_a_claim_inside_an_atomic_block_raises(self):
        from netbox_nso_plugin import drain

        with transaction.atomic(), self.assertRaises(RuntimeError):
            drain.claim(self.device.pk, "vlan")
