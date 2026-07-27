# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""URL resolution sanity tests and template badge colour regression gate.

These tests do NOT require a live adapter or NSO — they validate that:
  1. Every URL name referenced in navigation.py resolves without error.
  2. Warning badges retain sufficient foreground contrast.
"""

import os

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse

# All URL names used by navigation.py or linked from plugin UI.
# Update this list whenever a new PluginMenuItem or PluginMenuButton lands.
NAVIGATION_LINK_NAMES = [
    "plugins:netbox_nso_plugin:nsodevicemanagement_list",
    "plugins:netbox_nso_plugin:nsodevicemanagement_add",
    "plugins:netbox_nso_plugin:nsoinstance_list",
    "plugins:netbox_nso_plugin:nsoinstance_add",
    "plugins:netbox_nso_plugin:nsointerfacestate_list",
    "plugins:netbox_nso_plugin:adapterconnection",
]

# URL names that require a ``pk`` kwarg — tested with pk=1 (the value itself
# does not matter for *resolution*; only that reverse() does not raise).
PK_LINK_NAMES = [
    "plugins:netbox_nso_plugin:nsoinstance",
    "plugins:netbox_nso_plugin:nsoinstance_edit",
    "plugins:netbox_nso_plugin:nsoinstance_delete",
    "plugins:netbox_nso_plugin:nsoinstance_changelog",
    "plugins:netbox_nso_plugin:nsoinstance_journal",
    "plugins:netbox_nso_plugin:nsodevicemanagement",
    "plugins:netbox_nso_plugin:nsodevicemanagement_edit",
    "plugins:netbox_nso_plugin:nsodevicemanagement_delete",
    "plugins:netbox_nso_plugin:nsodevicemanagement_changelog",
    "plugins:netbox_nso_plugin:nsodevicemanagement_journal",
    "plugins:netbox_nso_plugin:nsodevicemanagement_refresh",
    "plugins:netbox_nso_plugin:nsointerfacestate",
    "plugins:netbox_nso_plugin:nsointerfacestate_delete",
    "plugins:netbox_nso_plugin:nsointerfacestate_changelog",
    "plugins:netbox_nso_plugin:nsointerfacestate_journal",
    "plugins:netbox_nso_plugin:nsointerfacestate_accept",
]

# URL names that require a ``job_id`` kwarg instead of ``pk``.
JOB_ID_LINK_NAMES = [
    "plugins:netbox_nso_plugin:nsojob_status",
]

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "templates",
    "netbox_nso_plugin",
)


class TestNavigationLinksResolve(SimpleTestCase):
    """Parametrised URL resolution sanity test.

    Python won't error on a mis-typed URL name; NetBox silently drops the menu
    item.  These tests catch that failure mode before it reaches production.
    """

    def test_no_pk_links_resolve(self):
        """Every no-pk navigation link resolves without NoReverseMatch."""
        broken = []
        for name in NAVIGATION_LINK_NAMES:
            try:
                reverse(name)
            except NoReverseMatch as exc:
                broken.append(f"{name}: {exc}")
        if broken:
            self.fail("Broken URL names:\n" + "\n".join(broken))

    def test_pk_links_resolve(self):
        """Every pk-based URL resolves with pk=1."""
        broken = []
        for name in PK_LINK_NAMES:
            try:
                reverse(name, kwargs={"pk": 1})
            except NoReverseMatch as exc:
                broken.append(f"{name}: {exc}")
        if broken:
            self.fail("Broken pk URL names:\n" + "\n".join(broken))

    def test_job_id_links_resolve(self):
        """Every job_id-based URL resolves with job_id=1."""
        broken = []
        for name in JOB_ID_LINK_NAMES:
            try:
                reverse(name, kwargs={"job_id": 1})
            except NoReverseMatch as exc:
                broken.append(f"{name}: {exc}")
        if broken:
            self.fail("Broken job_id URL names:\n" + "\n".join(broken))

    def test_action_url_resolves(self):
        """Parametric action URL resolves with pk + action string."""
        url = reverse(
            "plugins:netbox_nso_plugin:nsodevicemanagement_action",
            kwargs={"pk": 1, "action": "sync"},
        )
        self.assertTrue(url.endswith("/sync/"))

    def test_bulk_accept_url_resolves(self):
        """Bulk-accept URL resolves with device_pk."""
        url = reverse(
            "plugins:netbox_nso_plugin:device_bulk_accept",
            kwargs={"device_pk": 1},
        )
        self.assertIn("/bulk-accept/", url)

    def test_ajax_nso_device_names_url_resolves(self):
        """Ajax NSO device names URL resolves with instance_pk."""
        url = reverse(
            "plugins:netbox_nso_plugin:ajax_nso_device_names",
            kwargs={"instance_pk": 1},
        )
        self.assertIn("/nso-device-names/", url)

    def test_bulk_delete_urls_resolve(self):
        """Every ObjectListView model must register a bulk_delete URL.

        Regression: NetBox's list view shows a 'Delete Selected' button by
        default; if the model has no <model>_bulk_delete view, ObjectAction
        resolves the URL to None and the button POSTs to '<list>/None' → 404.
        """
        for name in (
            "nsoinstance_bulk_delete",
            "nsoplatformnedmapping_bulk_delete",
            "nsodevicemanagement_bulk_delete",
            "nsointerfacestate_bulk_delete",
        ):
            with self.subTest(name=name):
                try:
                    url = reverse(f"plugins:netbox_nso_plugin:{name}")
                except NoReverseMatch as exc:
                    self.fail(f"{name} did not resolve: {exc}")
                self.assertTrue(url.endswith("/delete/"))


class TestWarningBadgeContrast(SimpleTestCase):
    """Guard against low-contrast warning badges in plugin templates."""

    def _load_all_templates(self):
        # Recursive: the category panels live in subdirectories (categories/…); a non-recursive
        # os.listdir silently skipped them, letting gray-on-gray badges slip through the guard.
        results = {}
        for dirpath, _dirs, files in os.walk(_TEMPLATES_DIR):
            for fname in files:
                if fname.endswith(".html"):
                    path = os.path.join(dirpath, fname)
                    rel = os.path.relpath(path, _TEMPLATES_DIR)
                    with open(path) as fh:
                        results[rel] = fh.read()
        return results

    def test_warning_badges_have_text_dark(self):
        """Every bg-warning badge in plugin templates also carries text-dark for contrast."""
        violations = []
        for fname, content in self._load_all_templates().items():
            import re

            for match in re.finditer(r'class="[^"]*bg-warning[^"]*"', content):
                cls_str = match.group()
                if "text-dark" not in cls_str:
                    violations.append(f"{fname}: {cls_str}")
        if violations:
            self.fail(
                "bg-warning badge is missing text-dark (unreadable on light backgrounds):\n" + "\n".join(violations)
            )
