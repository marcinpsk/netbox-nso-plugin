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
from .strict_writer import assert_each_operation_consumed_once, strict_writer_harness


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
        from netbox_nso_plugin.deployment import is_quiesced
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

        with strict_writer_harness() as records:
            call_command("nso_renderer_baseline_cutover", stdout=stdout)

        assert_each_operation_consumed_once(records)
        state.refresh_from_db()
        revision.refresh_from_db()
        self.assertFalse(is_quiesced())
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

    def test_setup_failure_reports_that_the_gate_remains_active(self):
        from django.core.management.base import CommandError

        from netbox_nso_plugin.deployment import is_quiesced

        stderr = io.StringIO()
        with patch("netbox_nso_plugin.delivery.delivery_keys", side_effect=RuntimeError("registry unavailable")):
            with self.assertRaisesRegex(CommandError, "registry unavailable"):
                call_command("nso_renderer_baseline_cutover", stderr=stderr)

        self.assertIn("intent work remains quiesced", stderr.getvalue())
        self.assertTrue(is_quiesced())

    def test_a_baseline_that_never_stops_repairing_fails_and_stays_quiesced(self):
        """A device whose every audit still repairs has no trusted baseline to hand over."""
        from django.core.management.base import CommandError

        from netbox_nso_plugin.deployment import is_quiesced
        from netbox_nso_plugin.renderer_audit import RendererAuditResult

        own_vlan(self.management, 1675, "renderer-cutover")
        audits = []

        def never_stabilizes(device_id, scopes, trigger, **kwargs):
            audits.append(device_id)
            return RendererAuditResult(tuple(scopes), tuple(scopes))

        with patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", never_stabilizes):
            with self.assertRaisesMessage(CommandError, "did not stabilize"):
                call_command("nso_renderer_baseline_cutover", stdout=io.StringIO())

        self.assertEqual(audits, [self.device.pk] * 3)
        self.assertTrue(is_quiesced())

    def test_a_run_started_under_an_existing_gate_leaves_that_gate_standing(self):
        """The operator who quiesced owns the resume; the cutover must not take it from them."""
        from netbox_nso_plugin.deployment import is_quiesced, quiesce

        own_vlan(self.management, 1676, "renderer-cutover")
        self.assertTrue(quiesce())
        stdout = io.StringIO()

        call_command("nso_renderer_baseline_cutover", stdout=stdout)

        self.assertIn("Renderer baseline cutover passed", stdout.getvalue())
        self.assertTrue(is_quiesced())
