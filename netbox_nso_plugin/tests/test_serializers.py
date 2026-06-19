# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for api/serializers.py — the get_intent_value method on
NSOInterfaceStateSerializer, which is pure Python logic over the related
dcim.Interface (independent of the Django ORM).
"""

import inspect
import re
import unittest
from types import SimpleNamespace

# Plaintext-credential field-name shapes. Presence indicators (``has_*_secret``) and Vault
# pointers (``vault_ref``) are NOT secrets and are intentionally not matched.
_SECRET_FIELD_RE = re.compile(r"(auth_key|passphrase|private_key|^password$|_password$|^secret$|_secret$)")
_SECRET_FIELD_ALLOW = {"has_secret", "has_auth_secret", "has_priv_secret"}


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


class TestSerializersDoNotExposePlaintextSecrets(unittest.TestCase):
    """No serializer in api/serializers.py may serialize a plaintext-credential field.

    Most NSO*State serializers use ``fields = "__all__"``, which auto-exposes every model
    field. That is safe only while the overlays hold no plaintext secrets — but a future
    field could silently leak (e.g. into ObjectChange / webhook payloads). This introspects
    the *actual* serialized fields of every serializer class and fails if any plaintext-
    secret-shaped field is exposed. It is the teeth behind the ``__all__`` convention and
    directly guards the NSOISISInstanceState ``area_auth_key`` / ``domain_auth_key`` exclusion.
    """

    def _all_serializer_classes(self):
        from rest_framework.serializers import Serializer

        from netbox_nso_plugin.api import serializers as ser_mod

        return [
            cls
            for _name, cls in inspect.getmembers(ser_mod, inspect.isclass)
            if issubclass(cls, Serializer) and cls.__module__ == ser_mod.__name__
        ]

    def _exposed_fields(self, cls):
        """Return the set of field names the serializer actually exposes."""
        return set(cls().fields.keys())

    def test_no_serializer_exposes_a_plaintext_secret_field(self):
        offenders = []
        introspected = 0
        for cls in self._all_serializer_classes():
            fields = self._exposed_fields(cls)  # must not silently skip — a raise is a real failure
            introspected += 1
            for name in fields:
                if name in _SECRET_FIELD_ALLOW:
                    continue
                if _SECRET_FIELD_RE.search(name):
                    offenders.append(f"{cls.__name__}.{name}")
        self.assertEqual(offenders, [], f"serializers expose plaintext-secret fields: {offenders}")
        # Sanity: we really did introspect serializers (guards against a future refactor that
        # makes the loop silently no-op and turns this into a false green).
        self.assertGreater(introspected, 10)

    def test_isis_instance_serializer_excludes_auth_keys(self):
        """Explicit, mutation-proving check: the IS-IS auth keys are not in the serialized fields."""
        from netbox_nso_plugin.api.serializers import NSOISISInstanceStateSerializer

        fields = self._exposed_fields(NSOISISInstanceStateSerializer)
        self.assertNotIn("area_auth_key", fields)
        self.assertNotIn("domain_auth_key", fields)
        # The non-secret IS-IS fields are still exposed (we excluded only the keys).
        self.assertIn("net", fields)
        self.assertIn("area_auth_type", fields)
