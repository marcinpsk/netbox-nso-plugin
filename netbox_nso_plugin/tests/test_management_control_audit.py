# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Management control state is reconciled by one lock-owning adapter writer."""

from __future__ import annotations

import threading
from unittest.mock import patch

from dcim.models import Device, Interface
from django.db import OperationalError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from ipam.models import IPAddress

from ._outbox_case import make_managed
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def _probe_lock(queryset):
    """Try to take *queryset*'s row lock from a second connection; return the outcome.

    ``nowait`` turns "the row is locked" into an immediate error instead of a wait, so the
    probe is a fact about the lock window rather than a race against a timeout. ``of="self"``
    matches how the production footprint locks, and keeps a model whose ``Meta.ordering``
    joins another table from reporting that table's lock as its own.
    """
    outcome = []

    def run():
        close_old_connections()
        try:
            with transaction.atomic():
                list(queryset.select_for_update(of=("self",), nowait=True))
            outcome.append(None)
        except OperationalError as exc:
            outcome.append(exc)
        finally:
            close_old_connections()

    probe = threading.Thread(target=run)
    probe.start()
    probe.join(timeout=10)
    assert not probe.is_alive(), "the nowait probe never returned"
    return outcome[0]


class TestManagementControlAudit(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("control-audit", 16275)

    def test_matching_adapter_control_state_skips_the_scope_post(self):
        from netbox_nso_plugin.management_lifecycle import reconcile_management_control
        from netbox_nso_plugin.models import NSODeviceManagement

        primary_interface = Interface.objects.create(device=self.device, name="Loopback16271", type="virtual")
        oob_interface = Interface.objects.create(device=self.device, name="Management16271", type="1000base-t")
        primary = IPAddress.objects.create(address="198.18.175.11/32", assigned_object=primary_interface)
        oob = IPAddress.objects.create(address="198.18.175.12/32", assigned_object=oob_interface)
        self.device.primary_ip4 = primary
        self.device.oob_ip = oob
        self.device.save(update_fields=["primary_ip4", "oob_ip"])
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(
            manage_description=True,
            auto_apply=True,
            sync_before_apply=False,
        )

        with (
            patch(
                "netbox_nso_plugin.adapter_client.get_device",
                return_value={
                    "failover": {
                        "primary_ip": "198.18.175.11",
                        "oob_ip": "198.18.175.12",
                    }
                },
            ),
            patch(
                "netbox_nso_plugin.adapter_client.get_scope",
                return_value={
                    "attributes": ["description"],
                    "auto_apply": True,
                    "sync_before_apply": False,
                },
            ),
            patch("netbox_nso_plugin.adapter_client.set_scope") as set_scope,
        ):
            self.assertFalse(reconcile_management_control(self.device.pk))

        set_scope.assert_not_called()

    def test_each_adapter_control_divergence_posts_all_authoritative_fields(self):
        from netbox_nso_plugin.management_lifecycle import reconcile_management_control

        cases = (
            ("attributes", {"attributes": ["description"]}, None),
            ("auto_apply", {"auto_apply": True}, None),
            ("sync_before_apply", {"sync_before_apply": False}, None),
            (
                "primary_ip",
                {},
                {"primary_ip": "198.18.175.21", "oob_ip": None},
            ),
            (
                "oob_ip",
                {},
                {"primary_ip": None, "oob_ip": "198.18.175.22"},
            ),
        )
        for field_name, scope_override, failover in cases:
            with self.subTest(field_name=field_name):
                scope = {
                    "attributes": [],
                    "auto_apply": False,
                    "sync_before_apply": True,
                    **scope_override,
                }
                with (
                    patch(
                        "netbox_nso_plugin.adapter_client.get_device",
                        return_value={"failover": failover},
                    ),
                    patch(
                        "netbox_nso_plugin.adapter_client.get_scope",
                        return_value=scope,
                    ),
                    patch("netbox_nso_plugin.adapter_client.set_scope") as set_scope,
                ):
                    self.assertTrue(reconcile_management_control(self.device.pk))

                set_scope.assert_called_once_with(
                    self.management.adapter_device_id,
                    [],
                    auto_apply=False,
                    sync_before_apply=True,
                    primary_ip=None,
                    oob_ip=None,
                )

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

        with (
            patch("netbox_nso_plugin.adapter_client.get_device", return_value={"failover": None}),
            patch(
                "netbox_nso_plugin.adapter_client.get_scope",
                return_value={"attributes": [], "auto_apply": False, "sync_before_apply": True},
            ),
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=assert_lock_window) as set_scope,
        ):
            self.assertTrue(reconcile_management_control(self.device.pk))

        set_scope.assert_called_once()

    def test_delivery_family_locks_are_free_while_the_control_post_is_in_flight(self):
        """The control push must not hold the 18 family revision locks across the network call.

        The footprint used to merge ``reconcile_family_footprint`` over every delivery key, so
        one adapter round trip froze the whole device: no reconciler, no Apply and no renderer
        write for any family could proceed until the HTTP call returned.
        """
        from netbox_nso_plugin.management_lifecycle import reconcile_management_control
        from netbox_nso_plugin.models import NSOIntentRevision

        revision = NSOIntentRevision.objects.create(device=self.device, scope="vlan", revision=1)
        probes = []

        def probe_family_lock(*args, **kwargs):
            probes.append(_probe_lock(NSOIntentRevision.objects.filter(pk=revision.pk)))
            return {}

        with (
            patch("netbox_nso_plugin.adapter_client.get_device", return_value={"failover": None}),
            patch(
                "netbox_nso_plugin.adapter_client.get_scope",
                return_value={"attributes": ["description"], "auto_apply": False, "sync_before_apply": True},
            ),
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=probe_family_lock),
        ):
            self.assertTrue(reconcile_management_control(self.device.pk))

        self.assertEqual(probes, [None])

    def test_the_address_owners_stay_locked_while_the_control_post_is_in_flight(self):
        """The five posted values stay one snapshot: no primary-IP change can interleave.

        Narrowing the footprint must not narrow away the rows the payload is read from — a
        concurrent primary-IP move landing mid-POST would put half of an old payload and half
        of a new one on the adapter, with no later push to correct it.
        """
        from netbox_nso_plugin.management_lifecycle import reconcile_management_control

        interface = Interface.objects.create(device=self.device, name="Loopback1627", type="virtual")
        primary = IPAddress.objects.create(address="198.18.175.2/32", assigned_object=interface)
        self.device.primary_ip4 = primary
        self.device.save(update_fields=["primary_ip4"])
        probes = []

        def probe_source_locks(*args, **kwargs):
            probes.append(_probe_lock(Device.objects.filter(pk=self.device.pk)))
            probes.append(_probe_lock(IPAddress.objects.filter(pk=primary.pk)))
            return {}

        with (
            patch("netbox_nso_plugin.adapter_client.get_device", return_value={"failover": None}),
            patch(
                "netbox_nso_plugin.adapter_client.get_scope",
                return_value={"attributes": ["description"], "auto_apply": False, "sync_before_apply": True},
            ),
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=probe_source_locks),
        ):
            self.assertTrue(reconcile_management_control(self.device.pk))

        self.assertEqual(len(probes), 2)
        for outcome in probes:
            self.assertIsInstance(outcome, OperationalError)

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
