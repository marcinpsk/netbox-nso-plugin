# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for api/serializers.py — specifically the get_intent_value method
on NSOInterfaceStateSerializer which contains pure Python logic independent of
the Django ORM.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch


def _serializer_stubs():
    """Return sys.modules stubs needed to import api/serializers.py without Django stack."""

    class _FakeNetBoxModelSerializer:
        pass

    nb_api_ser = MagicMock()
    nb_api_ser.NetBoxModelSerializer = _FakeNetBoxModelSerializer

    rf_serializers = MagicMock()

    rf_mod = MagicMock()
    rf_mod.serializers = rf_serializers

    return {
        "rest_framework": rf_mod,
        "rest_framework.serializers": rf_serializers,
        "netbox.api": MagicMock(),
        "netbox.api.serializers": nb_api_ser,
        "netbox_nso_plugin.models": MagicMock(),
    }


def _import_serializer():
    """Import NSOInterfaceStateSerializer with all stubs in place."""
    stubs = _serializer_stubs()
    sys.modules.pop("netbox_nso_plugin.api.serializers", None)
    with patch.dict(sys.modules, stubs):
        from netbox_nso_plugin.api import serializers as ser_mod  # noqa: PLC0415

        return ser_mod.NSOInterfaceStateSerializer


class TestNSOInterfaceStateSerializerGetIntentValue(unittest.TestCase):
    """Tests for NSOInterfaceStateSerializer.get_intent_value pure logic."""

    def setUp(self):
        cls = _import_serializer()
        self._ser = object.__new__(cls)

    def _obj(self, attribute, description=None, enabled=True):
        obj = MagicMock()
        obj.attribute = attribute
        obj.interface = MagicMock()
        obj.interface.description = description
        obj.interface.enabled = enabled
        return obj

    def test_returns_description(self):
        obj = self._obj("description", description="uplink to core")
        self.assertEqual(self._ser.get_intent_value(obj), "uplink to core")

    def test_returns_empty_string_when_description_is_none(self):
        obj = self._obj("description", description=None)
        self.assertEqual(self._ser.get_intent_value(obj), "")

    def test_returns_enabled_true(self):
        obj = self._obj("enabled", enabled=True)
        self.assertEqual(self._ser.get_intent_value(obj), "True")

    def test_returns_enabled_false(self):
        obj = self._obj("enabled", enabled=False)
        self.assertEqual(self._ser.get_intent_value(obj), "False")

    def test_returns_none_for_null_interface(self):
        obj = MagicMock()
        obj.attribute = "description"
        obj.interface = None
        self.assertIsNone(self._ser.get_intent_value(obj))

    def test_returns_none_for_unknown_attribute(self):
        obj = self._obj("custom_field")
        self.assertIsNone(self._ser.get_intent_value(obj))
