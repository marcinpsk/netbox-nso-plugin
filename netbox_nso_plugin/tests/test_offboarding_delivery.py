# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Offboarding retires positive claims and keeps pending deletion authority."""

import threading

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connections, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from netbox_nso_plugin import drain
from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentOutboxState

from ._outbox_case import (
    ReceiptAdapter,
    entries,
    make_managed,
    make_mgmt,
    own_route,
    partition,
    state_of,
    without_commit_drain,
)
from ._static_route_case import _unassign_and_retire
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestOffboardingDelivery(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("offboard-delivery", 4010)
        self.adapter = ReceiptAdapter()

    def test_busy_delivery_is_reported_by_the_delete_view_and_can_be_retried(self):
        state = NSOIntentOutboxState.objects.create(
            device=self.device, scope="snmp", last_error_code="validation_error"
        )
        self.client.force_login(
            get_user_model().objects.create_superuser(username="offboard-busy", password="test-only")
        )
        locked = threading.Event()
        release = threading.Event()
        errors = []

        def hold_state():
            try:
                with transaction.atomic():
                    NSOIntentOutboxState.objects.select_for_update().get(pk=state.pk)
                    locked.set()
                    if not release.wait(timeout=30):
                        raise AssertionError("the state holder was not released")
            except BaseException as exc:  # noqa: BLE001 (asserted on the test thread)
                errors.append(exc)
            finally:
                connections.close_all()

        worker = threading.Thread(target=hold_state, daemon=True)
        worker.start()
        delete_url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_delete", args=[self.management.pk])
        try:
            self.assertTrue(locked.wait(timeout=10))
            response = self.client.post(delete_url, {"confirm": "on"})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(
                any("Intent delivery is busy" in str(message) for message in get_messages(response.wsgi_request))
            )
            self.assertTrue(NSODeviceManagement.objects.filter(pk=self.management.pk).exists())
            state.refresh_from_db()
            self.assertEqual(state.last_error_code, "validation_error")
        finally:
            release.set()
            worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        config, session = self.adapter.patches()
        with config, session:
            self.assertEqual(self.client.post(delete_url, {"confirm": "on"}).status_code, 302)
        self.assertFalse(NSODeviceManagement.objects.filter(pk=self.management.pk).exists())
        state.refresh_from_db()
        self.assertEqual(state.last_error_code, "")

    def test_claimed_deletion_survives_without_replaying_old_positive_intent(self):
        deleted = own_route(self.management, "198.18.251.0/28", "198.18.251.1")
        owned = own_route(self.management, "198.18.251.16/28", "198.18.251.17")
        config, session = self.adapter.patches()
        with config, session:
            self.assertEqual(drain.drain_key(self.device.pk, "static_route"), drain.SUCCEEDED)
        with without_commit_drain():
            _unassign_and_retire(deleted, self.device)
        claimed = drain.claim(self.device.pk, "static_route")
        self.assertIsNotNone(claimed)
        self.assertEqual([item["route_id"] for item in claimed.payload], [owned.pk])
        pending = [row.pk for row in entries(self.device, "static_route")]
        state = state_of(self.device, "static_route")
        lineage = state.lineage_carry
        revoked = state.revoked_ids

        config, session = self.adapter.patches()
        with config, session:
            self.management.delete()
        state.refresh_from_db()
        self.assertIsNone(state.push_seq)
        self.assertIsNone(state.claim_payload)
        self.assertEqual(state.queued_deletions, claimed.deletions)
        self.assertEqual(state.lineage_carry, lineage)
        self.assertEqual(state.revoked_ids, revoked)
        self.assertEqual([row.pk for row in entries(self.device, "static_route")], pending)
        self.assertEqual([row.pk for row in entries(self.device, "static_route", unconsumed=True)], pending)
        self.assertEqual(drain.settle(claimed, partition(executed=[deleted.pk])), drain.SUPERSEDED)
        self.assertEqual(drain.record_failure(claimed, RuntimeError("old transport failure")), drain.SUPERSEDED)
        state.refresh_from_db()
        self.assertEqual(state.last_error_code, "")
        replacement = make_mgmt(self.device, "offboard-delivery-new", 4011)
        self.adapter.requests.clear()
        config, session = self.adapter.patches()
        with config, session:
            self.assertEqual(drain.drain_key(self.device.pk, "static_route", chain=0), drain.SUCCEEDED)
        [request] = [r for r in self.adapter.requests if r["url"].endswith("static-route-intent")]
        self.assertIn(f"/devices/{replacement.adapter_device_id}/", request["url"])
        self.assertEqual(request["body"]["routes"], [])
        self.assertEqual(
            request["body"]["deleted_routes"],
            [{key: value for key, value in record.items() if key != "op"} for record in claimed.deletions],
        )
        self.assertGreater(request["push_seq"], claimed.push_seq)

    def test_offboarding_keeps_the_pending_authority_fence(self):
        withheld_at = timezone.now()
        state = NSOIntentOutboxState.objects.create(
            device=self.device,
            scope="static_route",
            fence_withheld_since=withheld_at,
            last_error_code="conflict",
            last_error_at=withheld_at,
        )
        config, session = self.adapter.patches()
        with config, session:
            self.management.delete()
        state.refresh_from_db()
        self.assertEqual(state.fence_withheld_since, withheld_at)
        self.assertEqual(state.last_error_code, "")

    def test_late_fence_rejection_cannot_restore_offboarded_diagnostics(self):
        own_route(self.management, "198.18.251.32/28", "198.18.251.33")

        def respond(body):
            if body is None:
                return {"count": 0}
            self.management.delete()
            return 409, {"error": {"code": "conflict", "message": "fence shut", "detail": {"reason": "fence_shut"}}}

        adapter = ReceiptAdapter(respond=respond)
        config, session = adapter.patches()
        with config, session:
            self.assertEqual(drain.drain_key(self.device.pk, "static_route", chain=0), drain.SUPERSEDED)
        self.assertFalse(NSODeviceManagement.objects.filter(device=self.device).exists())
        state = state_of(self.device, "static_route")
        self.assertIsNone(state.push_seq)
        self.assertEqual(state.last_error_code, "")
        self.assertIsNone(state.last_error_at)
        self.assertIsNone(state.fence_withheld_since)
