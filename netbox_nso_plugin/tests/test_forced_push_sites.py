# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — pin O1.16: the forced-push sites, enumerated by the compiler.

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
import re
from pathlib import Path

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase

from ._outbox_case import make_managed, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

PLUGIN = Path(__file__).resolve().parent.parent

#: (module, enclosing function, callee) for every production ``force=True`` push call.
FORCED_PUSH_SITES = {
    ("intent_drift.py", "resync_intent", "sc['push']"),
    ("intent_drift.py", "resync_static_route_intent_fleet", "signals._push_static_route_intent_for_device"),
    ("link_role.py", "apply_description_for_role", "_push_interface_intent_for_device"),
    ("link_role.py", "_push_provisioned", "fn"),
    ("views.py", "_prepare_apply", "push"),
    ("views.py", "_prepare_apply", "_push_snmp_intent_for_device"),
}
#: The claim a forced push routes through is itself forced. It is not a push site, and it is
#: named here so the exclusion cannot quietly widen to a module that is one.
FORCED_CLAIM_SITE = ("drain.py", "_claim_or_wait", "claim")


def _forced_calls() -> set[tuple[str, str, str]]:
    """Every production call passing ``force=True``, as the compiler sees it."""
    found = set()
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
            found.add((path.name, enclosing, ast.unparse(node.func)))
    return found


class TestForcedPushSitesAreEnumerated(SimpleTestCase):
    """O1.16 — six sites, found by the AST, and a scan that fails when a seventh appears."""

    def test_every_forced_site_is_still_where_the_enumeration_says(self):
        found = _forced_calls()
        for site in sorted(FORCED_PUSH_SITES):
            with self.subTest(site=site):
                assert site in found

    def test_no_forced_call_exists_outside_the_enumeration(self):
        assert _forced_calls() == FORCED_PUSH_SITES | {FORCED_CLAIM_SITE}

    def test_a_single_line_text_search_would_miss_one_of_them(self):
        """The named trap: one call spreads over three lines, so a grep enumerates five."""
        pattern = re.compile(r"\(.*force=True\)")
        by_text = set()
        for module, _function, _callee in FORCED_PUSH_SITES:
            source = (PLUGIN / module).read_text()
            by_text |= {module for line in source.splitlines() if pattern.search(line)}

        grepped = sum(
            1
            for module in {site[0] for site in FORCED_PUSH_SITES}
            for line in (PLUGIN / module).read_text().splitlines()
            if pattern.search(line)
        )
        assert grepped == len(FORCED_PUSH_SITES) - 1
        assert by_text, "the control: a text search does find most of them"


class TestAForcedCallInsideATransactionRaises(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O1.16 — silently deferring inside a caller's block is what this refuses to do."""

    def setUp(self):
        super().setUp()
        self.device, self.mgmt = make_managed("forced", 7670)
        own_vlan(self.mgmt, 970, "forced")

    def test_a_forced_drain_inside_an_atomic_block_raises(self):
        from netbox_nso_plugin import drain

        with transaction.atomic(), self.assertRaises(RuntimeError):
            drain.drain_key(self.device.pk, "vlan", force=True)

    def test_a_claim_inside_an_atomic_block_raises(self):
        from netbox_nso_plugin import drain

        with transaction.atomic(), self.assertRaises(RuntimeError):
            drain.claim(self.device.pk, "vlan")
