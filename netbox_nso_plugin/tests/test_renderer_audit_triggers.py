# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every document capture repairs renderer drift before it freezes content."""

from unittest.mock import patch

from django.test import TransactionTestCase

from ._outbox_case import ReceiptAdapter, enqueue, entries, in_thread, make_managed, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestRendererAuditCaptureOrder(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("renderer-audit-trigger", 16271)

    def test_public_claim_repairs_before_it_captures_the_revision(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision

        own_vlan(self.management, 1627, "renderer-audit-claim")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        before_revision = revision.revision
        NSOIntentRevision.objects.filter(pk=revision.pk).update(
            verified_revision=None,
            verified_fingerprint=None,
            verified_at=None,
        )
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()

        claimed = drain.claim(self.device.pk, "vlan")

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.revision, before_revision + 1)

    def test_delivery_audits_inside_the_public_api_before_rendering(self):
        from netbox_nso_plugin import delivery

        own_vlan(self.management, 1628, "renderer-audit-deliver")
        order = []

        def audit(*args, **kwargs):
            order.append("audit")

        def render(*args, **kwargs):
            order.append("render")
            raise RuntimeError("stop after capture ordering")

        with (
            patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit),
            patch("netbox_nso_plugin.delivery.render", side_effect=render),
            self.assertRaisesRegex(RuntimeError, "stop after capture ordering"),
        ):
            delivery.deliver("vlan", self.device.pk, self.management.adapter_device_id)

        self.assertEqual(order, ["audit", "render"])

    def test_each_chained_drain_pass_audits_again_before_recapture(self):
        """The real chain, not two hand-made calls: a tail appended mid-send earns pass two.

        ``_after_success`` chains another pass when the key still has unconsumed entries, and
        every pass has to re-audit before it recaptures — the tail was written after the
        first pass proved its baseline.
        """
        from netbox_nso_plugin import drain, renderer_audit

        own_vlan(self.management, 1629, "renderer-audit-chain")
        adapter = ReceiptAdapter()
        real_respond = adapter._respond
        real_audit = renderer_audit.audit_renderer_scopes
        calls = []
        appended = []

        def respond(body):
            # An operator transaction committing during the send, on its own connection:
            # the key gains a tail after this pass proved its baseline, so the drain chains.
            if len(adapter.requests) == 1:
                in_thread(lambda: enqueue(self.device, "vlan"))
                appended.append(len(entries(self.device, "vlan", unconsumed=True)))
            return real_respond(body)

        def audit(device_id, scopes, trigger, **kwargs):
            calls.append((device_id, tuple(scopes), trigger, kwargs))
            return real_audit(device_id, scopes, trigger, **kwargs)

        adapter._respond = respond
        config, session = adapter.patches()
        with (
            config,
            session,
            patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit),
        ):
            outcome = drain.drain_key(self.device.pk, "vlan")

        self.assertEqual(outcome, drain.SUCCEEDED)
        self.assertEqual(len(calls), 2)
        self.assertEqual(appended, [1])
        self.assertEqual(entries(self.device, "vlan", unconsumed=True), [])
        self.assertTrue(all(call[3] == {"pre_capture": True} for call in calls))
        self.assertTrue(all(call[:3] == (self.device.pk, ("vlan",), "drain._drain_once") for call in calls))
