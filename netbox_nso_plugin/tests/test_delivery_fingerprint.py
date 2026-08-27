# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1627: canonical delivery payload fingerprints."""

from django.test import SimpleTestCase


class TestCanonicalFingerprint(SimpleTestCase):
    def test_payload_uses_the_stable_request_identity_normalization(self):
        from netbox_nso_plugin.delivery import canonical_fingerprint

        payload = {"z": [2, 1], "a": {"enabled": True, "value": None}}

        assert canonical_fingerprint(payload) == "0521effd705002549d870075a311194f13369c3b0b541c6cf022e25c9a6adec7"

    def test_mapping_order_is_not_content_but_list_order_is(self):
        from netbox_nso_plugin.delivery import canonical_fingerprint

        first = {"items": [1, 2], "metadata": {"b": 2, "a": 1}}
        reordered_mapping = {"metadata": {"a": 1, "b": 2}, "items": [1, 2]}
        reordered_list = {"items": [2, 1], "metadata": {"a": 1, "b": 2}}

        assert canonical_fingerprint(first) == canonical_fingerprint(reordered_mapping)
        assert canonical_fingerprint(first) != canonical_fingerprint(reordered_list)
