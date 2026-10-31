# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""A foreign Interface delete must leave deletion evidence for the owners it cascades away."""

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase
from django.urls import reverse

from netbox_nso_plugin.lifecycle_fence import advance_fence, deleted_owner_digest, owner_deletion_proven
from netbox_nso_plugin.models import (
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSOIntentOutboxEntry,
    NSOOverlaySettlementFence,
    NSOOwnershipManifest,
    NSOSubinterfaceState,
    NSOSVIState,
)
from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestCascadedOwnerDeletion(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("cascade-owner", 1852)

    def _snapshot(self, owner):
        attempt = NSOApplyAttempt.objects.create(management=self.management)
        with transaction.atomic():
            fence = advance_fence(owner, current_attempt=attempt)
        return NSOApplyOverlaySnapshot.objects.create(
            attempt=attempt,
            mapping_epoch=fence.mapping_epoch,
            model_label=owner._meta.label_lower,
            overlay_pk=owner.pk,
            prior_status=owner.status,
            expected_status=owner.status,
            content_digest=fence.content_digest,
            expected_fence_revision=fence.bookkeeping_revision,
            expected_owner_revision=fence.owner_revision,
        )

    def _subinterface(self):
        with without_commit_drain(), transaction.atomic():
            parent = Interface.objects.create(device=self.device, name="ae99", type="lag")
            child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=parent)
        owner = acquire_overlay(
            NSOSubinterfaceState,
            management=self.management,
            interface=child,
            parent_interface=parent,
            dot1q_vlan=100,
            status="accepted",
        )
        return "subinterface", child, owner

    def _svi(self):
        with without_commit_drain(), transaction.atomic():
            irb = Interface.objects.create(device=self.device, name="irb.100", type="virtual")
        owner = acquire_overlay(
            NSOSVIState, management=self.management, interface=irb, svi_type="irb", status="accepted"
        )
        return "svi", irb, owner

    def _orm_delete(self, interface):
        with without_commit_drain():
            Interface.objects.filter(pk=interface.pk).delete()

    def _view_delete(self, interface):
        self.client.force_login(get_user_model().objects.create_user("cascade-admin", is_superuser=True))
        with without_commit_drain():
            response = self.client.post(reverse("dcim:interface_delete", args=[interface.pk]), {"confirm": True})
        self.assertEqual(response.status_code, 302)

    def _assert_deletion_proven(self, build, delete):
        scope, interface, owner = build()
        snapshot = self._snapshot(owner)
        delete(interface)
        self.assertFalse(type(owner).objects.filter(pk=owner.pk).exists())
        # The foreign delete itself is neutral; the planner pass is the deferred path that retracts it.
        with without_commit_drain():
            reconcile_scope_ownership(self.device.pk, [scope])
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope=scope)
        self.assertEqual((manifest.ownership_state, manifest.deletion_authority), ("retired", True))
        self.assertTrue(NSOIntentOutboxEntry.objects.filter(device=self.device, scope=scope, mark_any=True).exists())
        fence = NSOOverlaySettlementFence.objects.get(
            management=self.management,
            mapping_epoch=snapshot.mapping_epoch,
            model_label=owner._meta.label_lower,
            overlay_pk=owner.pk,
        )
        self.assertEqual(fence.content_digest, deleted_owner_digest(owner._meta.label_lower, owner.pk))
        self.assertIsNone(fence.current_attempt_id)
        self.assertTrue(owner_deletion_proven(self.management, snapshot))

    def test_orm_interface_delete_proves_cascaded_subinterface_owner_deletion(self):
        self._assert_deletion_proven(self._subinterface, self._orm_delete)

    def test_orm_interface_delete_proves_cascaded_svi_owner_deletion(self):
        self._assert_deletion_proven(self._svi, self._orm_delete)

    def test_ui_interface_delete_proves_cascaded_subinterface_owner_deletion(self):
        self._assert_deletion_proven(self._subinterface, self._view_delete)

    def test_ui_interface_delete_proves_cascaded_svi_owner_deletion(self):
        self._assert_deletion_proven(self._svi, self._view_delete)
