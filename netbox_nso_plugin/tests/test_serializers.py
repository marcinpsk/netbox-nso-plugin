# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for api/serializers.py — the get_intent_value method on
NSOInterfaceStateSerializer, which is pure Python logic over the related
dcim.Interface (independent of the Django ORM).
"""

import unittest
from types import SimpleNamespace


def _state(attribute, *, description=None, enabled=True, interface=True):
    """Return a real stand-in for an NSOInterfaceState row.

    get_intent_value only reads ``obj.attribute`` and ``obj.interface.{description,
    enabled}``, so a SimpleNamespace exercises that exact contract — and raises
    AttributeError on any unexpected access, unlike a MagicMock which fabricates
    every attribute and would let a typo'd field read pass silently.
    """
    iface = None if interface is None else SimpleNamespace(description=description, enabled=enabled)
    return SimpleNamespace(attribute=attribute, interface=iface)


class TestNSOInterfaceStateSerializerGetIntentValue(unittest.TestCase):
    """Tests for NSOInterfaceStateSerializer.get_intent_value pure logic."""

    def setUp(self):
        # Import the real serializer (the devcontainer has the full DRF/NetBox stack)
        # and bypass DRF Serializer.__init__ (which needs request context) — the method
        # under test is plain logic that only reads its ``obj`` argument.
        from netbox_nso_plugin.api.serializers import NSOInterfaceStateSerializer

        self._ser = object.__new__(NSOInterfaceStateSerializer)

    def test_returns_description(self):
        obj = _state("description", description="uplink to core")
        self.assertEqual(self._ser.get_intent_value(obj), "uplink to core")

    def test_returns_empty_string_when_description_is_none(self):
        obj = _state("description", description=None)
        self.assertEqual(self._ser.get_intent_value(obj), "")

    def test_returns_enabled_true(self):
        obj = _state("enabled", enabled=True)
        self.assertEqual(self._ser.get_intent_value(obj), "True")

    def test_returns_enabled_false(self):
        obj = _state("enabled", enabled=False)
        self.assertEqual(self._ser.get_intent_value(obj), "False")

    def test_returns_none_for_null_interface(self):
        obj = _state("description", interface=None)
        self.assertIsNone(self._ser.get_intent_value(obj))

    def test_returns_none_for_unknown_attribute(self):
        obj = _state("custom_field")
        self.assertIsNone(self._ser.get_intent_value(obj))
