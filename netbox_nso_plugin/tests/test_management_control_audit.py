# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Management control state is reconciled by one lock-owning adapter writer."""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Interface
from django.db import connection
from django.test import TransactionTestCase
from ipam.models import IPAddress

from ._outbox_case import make_managed
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestManagementControlAudit(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("control-audit", 16275)

    def test_reloads_all_five_authoritative_fields_inside_the_lock_window(self):
        from netbox_nso_plugin.management_lifecycle import reconcile_management_control
        from netbox_nso_plugin.models import NSODeviceManagement

        interface = Interface.objects.create(device=self.device, name="Loopback1627", type="virtual")
        primary = IPAddress.objects.create(address="198.18.175.1/32", assigned_object=interface)
        self.device.primary_ip4 = primary
        self.device.oob_ip = None
        self.device.save(update_fields=["primary_ip4", "oob_ip"])
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(
            manage_description=True,
            manage_enabled=True,
            auto_apply=True,
            sync_before_apply=False,
        )

        def assert_lock_window(*args, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(args, (self.management.adapter_device_id, ["description", "enabled"]))
            self.assertEqual(
                kwargs,
                {
                    "auto_apply": True,
                    "sync_before_apply": False,
                    "primary_ip": "198.18.175.1",
                    "oob_ip": None,
                },
            )
            return {}

        with patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=assert_lock_window) as set_scope:
            self.assertTrue(reconcile_management_control(self.device.pk))

        set_scope.assert_called_once()

    def test_cadence_runs_control_reconciliation_before_renderer_comparison(self):
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        order = []

        def control(*args, **kwargs):
            order.append("control")
            return True

        def optimistic(*args, **kwargs):
            order.append("fingerprint")
            return (), ()

        with (
            patch("netbox_nso_plugin.management_lifecycle.reconcile_management_control", side_effect=control),
            patch("netbox_nso_plugin.renderer_audit._optimistic_candidates", side_effect=optimistic),
        ):
            audit_renderer_scopes(self.device.pk, ("vlan",), trigger="cadence")

        self.assertEqual(order, ["control", "fingerprint"])
