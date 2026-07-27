# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Guard against multi-line Django ``{# #}`` comments.

Django's ``{# #}`` comment syntax is **single-line only**. A ``{#`` whose ``#}`` sits on a later
line is NOT a comment at all: the template engine emits the whole thing as literal text, so an
explanatory note written for the next developer ends up rendered in the page, in front of the
operator. It happened here in the SNMP category partial (CR-P16): a three-line ``{# ... #}`` above
the trap-host ``v3 User`` cell printed itself into the NSO tab.

Nothing else catches this:

* it is valid template SOURCE, so ``get_template()`` parses it happily — a compile check is blind;
* it produces no error, no warning and no exception, so every view test still passes;
* the 1385-test plugin suite went green with the leak in place. It was found only by *looking* at
  the rendered page (a live Playwright pass), which is exactly the kind of check nobody runs on
  every template, every time.

Hence a static scan. Same guard the librenms plugin carries, for the same bug.

A ``SimpleTestCase`` (no DB) so it costs nothing and runs under both the Django runner used by CI
and pytest.
"""

from __future__ import annotations

import pathlib

from django.test import SimpleTestCase

import netbox_nso_plugin

TEMPLATES_DIR = pathlib.Path(netbox_nso_plugin.__file__).parent / "templates"


def _html_templates() -> list[pathlib.Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


class TestTemplateComments(SimpleTestCase):
    def test_templates_are_actually_found(self):
        """The scan must have something to scan.

        Without this, a renamed/moved templates directory turns the guard below into a test that
        passes by looking at nothing — the most comfortable kind of false green.
        """
        self.assertTrue(_html_templates(), f"no .html templates found under {TEMPLATES_DIR}")

    def test_no_multiline_single_hash_comments(self):
        """No line may leave a ``{#`` unclosed — that is a multi-line comment, which is not one."""
        offenders = []
        for path in _html_templates():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.count("{#") != line.count("#}"):
                    offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{lineno}: {line.strip()}")

        self.assertFalse(
            offenders,
            "A multi-line `{# #}` is NOT a comment — Django renders it as literal text on the page. "
            "Use `{% comment %} ... {% endcomment %}`:\n" + "\n".join(offenders),
        )
