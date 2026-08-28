# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""One-time conservative renderer-baseline cutover command."""

from __future__ import annotations

import io
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.test import TransactionTestCase

from ._outbox_case import make_managed, mirror_update, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestRendererBaselineCutover(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("renderer-cutover", 16274)

    def tearDown(self):
        from netbox_nso_plugin.deployment import is_quiesced, resume

        if is_quiesced():
            resume()
        super().tearDown()

    def test_unknown_baseline_is_repaired_and_verified_before_resume(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision

        state = own_vlan(self.management, 1674, "renderer-cutover")
        mirror_update(state, status="deploying", apply_attempt_id=uuid4())
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        mirror_update(
            revision,
            verified_revision=None,
            verified_fingerprint=None,
            verified_at=None,
        )
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        stdout = io.StringIO()

        call_command("nso_renderer_baseline_cutover", stdout=stdout)

        state.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertIsNone(state.apply_attempt_id)
        self.assertEqual(revision.verified_revision, revision.revision)
        self.assertTrue(revision.verified_fingerprint)
        self.assertTrue(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan", kind="repair").exists())
        self.assertIn("Renderer baseline cutover passed", stdout.getvalue())

    def test_failure_leaves_the_exclusive_gate_active(self):
        from netbox_nso_plugin.deployment import is_quiesced
        from netbox_nso_plugin.renderer_audit import RendererAuditRepairFailed

        with patch(
            "netbox_nso_plugin.renderer_audit.audit_renderer_scopes",
            side_effect=RendererAuditRepairFailed("racing"),
        ):
            with self.assertRaisesMessage(Exception, "racing"):
                call_command("nso_renderer_baseline_cutover", stdout=io.StringIO())

        self.assertTrue(is_quiesced())
