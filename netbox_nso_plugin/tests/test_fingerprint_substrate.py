# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1627: durable renderer fingerprint substrate."""

from django.test import TestCase

from ._outbox_case import make_device


class TestIntentRevisionFingerprintFields(TestCase):
    def test_a_new_revision_has_an_unknown_fingerprint_baseline(self):
        from netbox_nso_plugin.models import NSOIntentRevision

        device = make_device("fingerprint")
        revision = NSOIntentRevision.objects.create(device=device, scope="vlan")

        assert revision.verified_revision is None
        assert revision.verified_fingerprint is None
        assert revision.verified_at is None
