# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every document capture repairs renderer drift before it freezes content."""

from unittest.mock import patch

from django.test import TransactionTestCase

from ._outbox_case import make_managed, own_vlan
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
        from netbox_nso_plugin import delivery, drain

        own_vlan(self.management, 1629, "renderer-audit-chain")
        calls = []

        def audit(device_id, scopes, trigger, **kwargs):
            calls.append((device_id, tuple(scopes), trigger, kwargs))

        with (
            patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit),
            patch("netbox_nso_plugin.drain._claim_or_compact", side_effect=[(None, False), (None, False)]),
        ):
            drain._drain_once(
                self.device.pk,
                "vlan",
                mode=delivery.MODE_NORMAL,
                force=False,
            )
            drain._drain_once(
                self.device.pk,
                "vlan",
                mode=delivery.MODE_NORMAL,
                force=False,
                _chained=True,
            )

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[3] == {"pre_capture": True} for call in calls))
