# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Golden vectors for the Vault reference parser (mirror of the adapter's).

Keep the vectors identical to ``nso-adapter/tests/secrets/test_refs.py`` so
both repos agree on the grammar.
"""

import hashlib
import unittest

from netbox_nso_plugin.vault_refs import VaultRef, VaultRefError, parse_vault_ref, secret_fingerprint

GOOD_VECTORS = [
    (
        "network/netbox/snmp/community/9f2a41c3d0be77aa#community",
        VaultRef("network", "netbox/snmp/community/9f2a41c3d0be77aa", "community"),
    ),
    ("network/netbox/snmp/v3/nms", VaultRef("network", "netbox/snmp/v3/nms", None)),
    ("network/netbox/snmp/v3/nms#auth", VaultRef("network", "netbox/snmp/v3/nms", "auth")),
    ("kv/p#k", VaultRef("kv", "p", "k")),
]

BAD_VECTORS = [
    "",
    "no-slash",
    "no-slash#key",
    "mount/",
    "/path#key",
    "m//p#k",
    "m/p/#k",
    "m/p#",
    "m/p#a#b",
    "m/p a#k",
    "m/p\t#k",
]


class VaultRefParserTestCase(unittest.TestCase):
    def test_good_vectors_roundtrip(self):
        for ref, expected in GOOD_VECTORS:
            parsed = parse_vault_ref(ref)
            self.assertEqual(parsed, expected)
            self.assertEqual(str(parsed), ref)

    def test_bad_vectors_raise(self):
        for ref in BAD_VECTORS:
            with self.assertRaises(VaultRefError, msg=ref):
                parse_vault_ref(ref)

    def test_require_key_modes(self):
        with self.assertRaises(VaultRefError):
            parse_vault_ref("network/netbox/snmp/v3/nms", require_key=True)
        with self.assertRaises(VaultRefError):
            parse_vault_ref("network/netbox/snmp/v3/nms#auth", require_key=False)
        self.assertIsNone(parse_vault_ref("network/p", require_key=False).key)
        self.assertEqual(parse_vault_ref("network/p#k", require_key=True).key, "k")

    def test_secret_fingerprint_matches_read_mirror_digest(self):
        self.assertEqual(secret_fingerprint("s3cr3t"), hashlib.sha256(b"s3cr3t").hexdigest()[:16])
