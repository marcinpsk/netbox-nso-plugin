# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for derived_intent.load_sentinel_templates (Phase 2).

Pure unit tests: no DB, no Django ORM.  Each test exercises the config
validation logic in derived_intent.py so that failures surface at boot time.
"""

from django.test import SimpleTestCase

from netbox_nso_plugin.derived_intent import (
    ConfigError,
    SentinelTemplate,
    load_sentinel_templates,
)


class TestLoadSentinelTemplatesValid(SimpleTestCase):
    """Happy-path tests for load_sentinel_templates."""

    def test_empty_list_returns_empty(self):
        """Empty list = feature off; no error."""
        result = load_sentinel_templates([])
        self.assertEqual(result, [])

    def test_single_bare_sentinel(self):
        """A template equal to its sentinel (no placeholders) is valid."""
        result = load_sentinel_templates([{"sentinel": "[auto]", "template": "[auto]"}])
        self.assertEqual(result, [SentinelTemplate(sentinel="[auto]", template="[auto]")])

    def test_single_template_with_placeholders(self):
        """Standard canonical template."""
        raw = [{"sentinel": "[auto]", "template": "[auto] to {peer_host}:{peer_iface}"}]
        result = load_sentinel_templates(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].sentinel, "[auto]")
        self.assertIn("{peer_host}", result[0].template)

    def test_multi_template_order_preserved(self):
        """Input order is preserved in the returned list."""
        raw = [
            {"sentinel": "[auto]", "template": "[auto] to {peer_host}:{peer_iface}"},
            {"sentinel": "[short]", "template": "[short] {peer_host}"},
        ]
        result = load_sentinel_templates(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].sentinel, "[auto]")
        self.assertEqual(result[1].sentinel, "[short]")

    def test_all_known_placeholders_accepted(self):
        """All six known placeholders may appear in one template."""
        tmpl = "[auto] {peer_host} {peer_iface} {peer_site} {peer_role} {self_host} {self_iface}"
        result = load_sentinel_templates([{"sentinel": "[auto]", "template": tmpl}])
        self.assertEqual(len(result), 1)


class TestLoadSentinelTemplatesInvalid(SimpleTestCase):
    """Error-path tests: every validation rule must raise ConfigError."""

    def test_non_list_root_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates({"sentinel": "[auto]", "template": "[auto]"})

    def test_non_dict_item_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates(["[auto]"])

    def test_missing_sentinel_key_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"template": "[auto] to {peer_host}"}])

    def test_missing_template_key_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"sentinel": "[auto]"}])

    def test_typo_templates_key_raises(self):
        """'templates' (plural) instead of 'template' is a common typo."""
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"sentinel": "[auto]", "templates": "[auto] to {peer_host}"}])

    def test_empty_sentinel_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"sentinel": "", "template": "[auto]"}])

    def test_template_not_starting_with_sentinel_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"sentinel": "[auto]", "template": "prefix to {peer_host}"}])

    def test_unknown_placeholder_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"sentinel": "[auto]", "template": "[auto] to {unknown_field}"}])

    def test_prefix_overlap_raises(self):
        """'[auto]' is a prefix of '[auto] short' — ambiguous and rejected."""
        with self.assertRaises(ConfigError):
            load_sentinel_templates(
                [
                    {"sentinel": "[auto]", "template": "[auto] to {peer_host}"},
                    {"sentinel": "[auto] short", "template": "[auto] short {peer_host}"},
                ]
            )

    def test_prefix_overlap_raises_when_an_unrelated_sentinel_sorts_between(self):
        """Every pair is checked; an unrelated length must not hide an ambiguous prefix."""
        with self.assertRaises(ConfigError):
            load_sentinel_templates(
                [
                    {"sentinel": "[a]", "template": "[a] {peer_host}"},
                    {"sentinel": "[bb]", "template": "[bb] {peer_host}"},
                    {"sentinel": "[a]-edge", "template": "[a]-edge {peer_host}"},
                ]
            )

    def test_non_string_sentinel_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"sentinel": 42, "template": "42 to {peer_host}"}])

    def test_non_string_template_raises(self):
        with self.assertRaises(ConfigError):
            load_sentinel_templates([{"sentinel": "[auto]", "template": 42}])
