# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for the derived-intent field registry (Phase 3).

Pure unit tests: no DB, no Django ORM.  Each test exercises register(),
registered_fields(), and fields_for_attribute(), and verifies that the
module-global _REGISTRY is cleared between test cases.
"""

from django.test import SimpleTestCase

from netbox_nso_plugin.derived_intent import (
    _REGISTRY,
    ConfigError,
    DerivedField,
    _register_description_from_cable,
    fields_for_attribute,
    register,
    registered_fields,
)


def _dummy_compute(interface, sentinel):
    return None


def _dummy_is_managed(value, templates):
    return None


def _make_field(name="f1", attr="description"):
    return DerivedField(
        name=name,
        target_attribute=attr,
        compute=_dummy_compute,
        is_managed=_dummy_is_managed,
    )


class TestRegistry(SimpleTestCase):
    """Tests for the registry API."""

    def setUp(self):
        _REGISTRY.clear()

    def tearDown(self):
        _REGISTRY.clear()

    def test_register_single_field(self):
        field = _make_field("my_field")
        register(field)
        self.assertIn("my_field", _REGISTRY)

    def test_registered_fields_returns_list(self):
        field = _make_field("f1")
        register(field)
        result = registered_fields()
        self.assertEqual(result, [field])

    def test_registered_fields_empty_initially(self):
        self.assertEqual(registered_fields(), [])

    def test_double_register_raises(self):
        register(_make_field("dup"))
        with self.assertRaises(ConfigError):
            register(_make_field("dup"))

    def test_fields_for_attribute_matching(self):
        f1 = _make_field("desc_cable", attr="description")
        f2 = _make_field("enabled_cable", attr="enabled")
        register(f1)
        register(f2)
        result = fields_for_attribute("description")
        self.assertEqual(result, [f1])

    def test_fields_for_attribute_no_match(self):
        register(_make_field("f1", attr="description"))
        result = fields_for_attribute("mtu")
        self.assertEqual(result, [])

    def test_fields_for_attribute_multiple_match(self):
        f1 = _make_field("desc1", attr="description")
        f2 = _make_field("desc2", attr="description")
        register(f1)
        register(f2)
        result = fields_for_attribute("description")
        self.assertIn(f1, result)
        self.assertIn(f2, result)
        self.assertEqual(len(result), 2)

    def test_register_description_from_cable(self):
        """_register_description_from_cable registers the field in _REGISTRY."""
        _register_description_from_cable()
        self.assertIn("description_from_cable", _REGISTRY)
        field = _REGISTRY["description_from_cable"]
        self.assertEqual(field.target_attribute, "description")
