# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The mock-discipline guard runs as part of the suite, plus self-tests of the analyzer.

See ``mock_discipline.py`` for the policy: spec-less MagicMock/Mock (and object-shaped
AsyncMock) used as object stand-ins are flagged; bound (``spec=``/``wraps=``), inline-
``# mock-ok``-marked, or baseline-grandfathered usages are allowed.

This is a plain ``unittest.TestCase`` (no DB) so both Django's ``manage.py test`` runner
and pytest discover it. The analyzer self-tests parse real AST — no mocks of the thing
that hunts mocks.
"""

from __future__ import annotations

import unittest

from .mock_discipline import _counts_by_site, scan_source, scan_tree, unapproved


class MockDisciplineGuardTests(unittest.TestCase):
    def test_no_unapproved_mocks_beyond_baseline(self):
        """No new spec-less MagicMock/Mock has crept in past the grandfathered baseline.

        To resolve a failure, prefer (in order): use a real object, bound the mock with
        ``spec=`` / ``wraps=``, or add an inline ``# mock-ok: <reason>``. Only as a last
        resort regenerate the baseline:
        ``python netbox_nso_plugin/tests/mock_discipline.py --update-baseline``.
        """
        bad = unapproved()
        self.assertEqual(
            bad,
            [],
            "Unapproved attribute-fabricating mock(s):\n"
            + "\n".join(f"  {v}" for v in bad)
            + (
                "\n\nFix by: using a real object, binding with spec=/wraps=, or marking the "
                "line `# mock-ok: <reason>`. Last resort: "
                "python netbox_nso_plugin/tests/mock_discipline.py --update-baseline"
            ),
        )


class MockDisciplineAnalyzerTests(unittest.TestCase):
    """Self-tests of the AST analyzer (real parsing — no mocks)."""

    def test_flags_specless_magicmock(self):
        src = "from unittest.mock import MagicMock\n\ndef test_x():\n    row = MagicMock()\n"
        hits = scan_source(src, "t.py")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].mock, "MagicMock")
        self.assertEqual(hits[0].qualname, "test_x")
        self.assertEqual(hits[0].site, "t.py::test_x")

    def test_flags_bare_mock_and_aliased_import(self):
        src = "from unittest.mock import Mock as M\n\ndef test_x():\n    return M()\n"
        self.assertEqual([h.mock for h in scan_source(src, "t.py")], ["Mock"])

    def test_flags_attribute_access_form(self):
        src = "import unittest.mock as m\n\ndef test_x():\n    return m.MagicMock()\n"
        self.assertEqual([h.mock for h in scan_source(src, "t.py")], ["MagicMock"])

    def test_accepts_spec_bounded_mock(self):
        src = "from unittest.mock import MagicMock\nclass C: ...\n\ndef test_x():\n    return MagicMock(spec=C)\n"
        self.assertEqual(scan_source(src, "t.py"), [])

    def test_accepts_wraps_and_spec_set(self):
        src = (
            "from unittest.mock import MagicMock\n\n"
            "def test_x(real):\n"
            "    a = MagicMock(wraps=real)\n"
            "    b = MagicMock(spec_set=real)\n"
            "    return a, b\n"
        )
        self.assertEqual(scan_source(src, "t.py"), [])

    def test_accepts_inline_marker(self):
        src = (
            "from unittest.mock import MagicMock\n\n"
            "def test_x():\n"
            "    client = MagicMock()  # mock-ok: external adapter HTTP boundary\n"
            "    return client\n"
        )
        self.assertEqual(scan_source(src, "t.py"), [])

    def test_marker_must_be_in_a_comment_not_a_string(self):
        src = 'from unittest.mock import MagicMock\n\ndef test_x():\n    label = "mock-ok"\n    return MagicMock()\n'
        self.assertEqual(len(scan_source(src, "t.py")), 1)

    def test_asyncmock_callable_stub_is_not_flagged(self):
        src = "from unittest.mock import AsyncMock\n\ndef test_x():\n    return AsyncMock()\n"
        self.assertEqual(scan_source(src, "t.py"), [])

    def test_object_shaped_asyncmock_is_flagged(self):
        """An AsyncMock given >=2 distinct attributes is an object stand-in → flagged."""
        src = (
            "from unittest.mock import AsyncMock\n\n"
            "def test_x():\n"
            "    client = AsyncMock()\n"
            "    client.list_devices = AsyncMock(return_value=[])\n"
            "    client.get_interface = AsyncMock(return_value=None)\n"
            "    return client\n"
        )
        hits = scan_source(src, "t.py")
        self.assertEqual([h.mock for h in hits], ["AsyncMock"])

    def test_spec_bound_object_shaped_asyncmock_is_not_flagged(self):
        src = (
            "from unittest.mock import AsyncMock\nclass C: ...\n\n"
            "def test_x():\n"
            "    client = AsyncMock(spec=C)\n"
            "    client.a = 1\n"
            "    client.b = 2\n"
            "    return client\n"
        )
        self.assertEqual(scan_source(src, "t.py"), [])

    def test_object_shaped_attrs_in_nested_scope_do_not_count(self):
        src = (
            "from unittest.mock import AsyncMock\n\n"
            "def test_x():\n"
            "    m = AsyncMock()\n"
            "    def _inner():\n"
            "        m.a = 1\n"
            "        m.b = 2\n"
            "    return m, _inner\n"
        )
        self.assertEqual(scan_source(src, "t.py"), [])

    def test_marker_in_comment_block_above_statement_is_honoured(self):
        src = (
            "from unittest.mock import MagicMock\n\n"
            "def test_x():\n"
            "    # mock-ok: external boundary\n"
            "    client = MagicMock()\n"
            "    return client\n"
        )
        self.assertEqual(scan_source(src, "t.py"), [])

    def test_counts_by_site_groups_per_function(self):
        src = (
            "from unittest.mock import MagicMock\n\n"
            "def test_x():\n"
            "    a = MagicMock()\n"
            "    b = MagicMock()\n"
            "    return a, b\n"
        )
        self.assertEqual(_counts_by_site(scan_source(src, "t.py")), {"t.py::test_x": 2})

    def test_baseline_budget_allows_grandfathered_but_not_excess(self):
        """A site with N grandfathered mocks tolerates N but flags the N+1-th."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "tests"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "test_thing.py").write_text(
                "from unittest.mock import MagicMock\n\n"
                "def test_x():\n"
                "    a = MagicMock()\n"
                "    b = MagicMock()\n"
                "    return a, b\n"
            )
            # Budget of 1 for the two-mock site → exactly one excess is reported.
            self.assertEqual(len(unapproved(root=pkg, baseline={"test_thing.py::test_x": 1})), 1)
            # Budget of 2 → nothing reported.
            self.assertEqual(unapproved(root=pkg, baseline={"test_thing.py::test_x": 2}), [])

    def test_scan_tree_skips_the_guard_and_its_test(self):
        files = {v.path for v in scan_tree()}
        self.assertNotIn("mock_discipline.py", files)
        self.assertNotIn("test_mock_discipline.py", files)


if __name__ == "__main__":
    unittest.main()
