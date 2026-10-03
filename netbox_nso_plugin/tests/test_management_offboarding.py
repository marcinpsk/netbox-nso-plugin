# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Management deletion removes plugin state while retaining native NetBox objects."""

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from ipam.models import IPAddress

from netbox_nso_plugin import delivery
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOIntentOutboxState,
    NSOInterfaceIPState,
    NSOInterfaceState,
)

from ._outbox_case import content_update, make_managed, make_mgmt
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin


class TestManagementOffboarding(IntentPushResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("offboard-cleanup", 4001)
        self.other_device, self.other_management = make_managed("offboard-neighbor", 4002)
        for management in (self.management, self.other_management):
            management.manage_description = True
            management.save(update_fields=["manage_description"])
        self.interface = Interface.objects.create(
            device=self.device, name="Ethernet1", type="1000base-t", description="Keep this native value"
        )
        self.other_interface = Interface.objects.create(device=self.other_device, name="Ethernet1", type="1000base-t")
        self.address = IPAddress.objects.create(address="198.18.250.1/24", assigned_object=self.interface)
        self.other_address = IPAddress.objects.create(address="198.18.250.2/24", assigned_object=self.other_interface)
        self.description = acquire_overlay(
            NSOInterfaceState, interface=self.interface, attribute="description", status="accepted"
        )
        self.ip_state = acquire_overlay(
            NSOInterfaceIPState, interface=self.interface, address=str(self.address.address), status="in_sync"
        )
        self.other_description = acquire_overlay(
            NSOInterfaceState, interface=self.other_interface, attribute="description", status="accepted"
        )
        self.other_ip_state = acquire_overlay(
            NSOInterfaceIPState,
            interface=self.other_interface,
            address=str(self.other_address.address),
            status="in_sync",
        )
        self.outbox_state = NSOIntentOutboxState.objects.create(
            device=self.device,
            scope="snmp",
            last_error_code="validation_error",
            last_error_at=timezone.now(),
            last_success_identity="a" * 64,
            last_success_at=timezone.now(),
            last_drain_attempted_at=timezone.now(),
            attempts=3,
            degraded_deletions=[{"reason": "legacy_mark_downgraded"}],
        )
        self.other_outbox_state = NSOIntentOutboxState.objects.create(
            device=self.other_device,
            scope="snmp",
            last_error_code="validation_error",
            degraded_deletions=[{"reason": "legacy_mark_downgraded"}],
        )
        user = get_user_model().objects.create_superuser(username="offboard-operator", password="test-only")
        self.client.force_login(user)

    def test_delete_post_removes_interface_overlays_and_preserves_native_objects(self):
        management_id = self.management.pk

        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_delete", args=[management_id]),
            {"confirm": "on"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(NSODeviceManagement.objects.filter(pk=management_id).exists())
        self.assertFalse(NSOInterfaceState.objects.filter(interface__device=self.device).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface__device=self.device).exists())
        self.interface.refresh_from_db()
        self.address.refresh_from_db()
        self.assertEqual(self.interface.description, "Keep this native value")
        self.assertEqual(self.address.assigned_object_id, self.interface.pk)
        self.other_management.refresh_from_db()
        self.other_description.refresh_from_db()
        self.other_ip_state.refresh_from_db()
        self.assertEqual(self.other_description.status, "accepted")
        self.assertEqual(self.other_ip_state.status, "in_sync")
        self.outbox_state.refresh_from_db()
        self.assertEqual(self.outbox_state.last_error_code, "")
        self.assertIsNone(self.outbox_state.last_error_at)
        self.assertEqual(self.outbox_state.degraded_deletions, [])
        self.assertEqual(self.outbox_state.last_success_identity, "")
        self.assertIsNone(self.outbox_state.last_success_at)
        self.assertIsNone(self.outbox_state.last_drain_attempted_at)
        self.assertEqual(self.outbox_state.attempts, 0)
        self.other_outbox_state.refresh_from_db()
        self.assertEqual(self.other_outbox_state.last_error_code, "validation_error")
        self.assertEqual(self.other_outbox_state.degraded_deletions, [{"reason": "legacy_mark_downgraded"}])

        replacement = make_mgmt(self.device, "offboard-replacement", 4003)
        replacement.manage_description = True
        replacement.save(update_fields=["manage_description"])
        self.assertEqual(delivery.render("interface", self.device.pk, replacement.adapter_device_id).payload, [])
        self.assertEqual(delivery.render("ip", self.device.pk, replacement.adapter_device_id).payload, [])

    def test_bulk_delete_post_removes_interface_overlays(self):
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_bulk_delete"),
            {"pk": [self.management.pk], "_confirm": "Confirm", "confirm": "on"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(NSODeviceManagement.objects.filter(device=self.device).exists())
        self.assertFalse(NSOInterfaceState.objects.filter(interface__device=self.device).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface__device=self.device).exists())
        self.other_management.refresh_from_db()
        self.other_description.refresh_from_db()
        self.other_ip_state.refresh_from_db()
        self.assertEqual(self.other_description.status, "accepted")
        self.assertEqual(self.other_ip_state.status, "in_sync")
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.description, "Keep this native value")

    def test_queryset_delete_removes_interface_overlays(self):
        content_update(self.other_ip_state, peer_state=self.ip_state)
        entries_before = list(NSOIntentOutboxEntry.objects.values_list("pk", flat=True))

        NSODeviceManagement.objects.filter(pk=self.management.pk).delete()

        self.assertFalse(NSODeviceManagement.objects.filter(device=self.device).exists())
        self.assertFalse(NSOInterfaceState.objects.filter(interface__device=self.device).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface__device=self.device).exists())
        self.interface.refresh_from_db()
        self.address.refresh_from_db()
        self.other_ip_state.refresh_from_db()
        self.other_address.refresh_from_db()
        self.assertIsNone(self.other_ip_state.peer_state_id)
        self.assertEqual(self.other_ip_state.status, "in_sync")
        self.assertEqual(self.other_address.assigned_object_id, self.other_interface.pk)
        self.assertEqual(list(NSOIntentOutboxEntry.objects.values_list("pk", flat=True)), entries_before)

    def test_rollback_restores_management_and_interface_overlays(self):
        content_update(self.other_ip_state, peer_state=self.ip_state)
        management_id = self.management.pk
        with self.assertRaisesRegex(RuntimeError, "cancel offboard"), transaction.atomic():
            self.management.delete()
            self.assertFalse(NSOInterfaceState.objects.filter(interface__device=self.device).exists())
            self.assertFalse(NSOInterfaceIPState.objects.filter(interface__device=self.device).exists())
            raise RuntimeError("cancel offboard")

        self.assertTrue(NSODeviceManagement.objects.filter(pk=management_id).exists())
        self.description.refresh_from_db()
        self.ip_state.refresh_from_db()
        self.other_ip_state.refresh_from_db()
        self.assertEqual(self.description.status, "accepted")
        self.assertEqual(self.ip_state.status, "in_sync")
        self.assertEqual(self.other_ip_state.peer_state_id, self.ip_state.pk)
        self.outbox_state.refresh_from_db()
        self.assertEqual(self.outbox_state.last_error_code, "validation_error")
        self.assertEqual(self.outbox_state.attempts, 3)
        self.assertEqual(self.outbox_state.degraded_deletions, [{"reason": "legacy_mark_downgraded"}])
