# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""A foreign native delete must leave deletion evidence for the owners it cascades away."""

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


def _snapshot(management, owner):
    attempt = NSOApplyAttempt.objects.create(management=management)
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


class TestCascadedOwnerDeletion(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("cascade-owner", 1852)

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

    def _vlan(self):
        from ipam.models import VLAN

        from ._outbox_case import own_vlan

        owner = own_vlan(self.management, 1852, "cascade-owner")
        return "vlan", VLAN.objects.get(pk=owner.vlan_id), owner

    def _orm_delete(self, native):
        with without_commit_drain():
            type(native).objects.filter(pk=native.pk).delete()

    def _view_delete(self, interface):
        self.client.force_login(get_user_model().objects.create_user("cascade-admin", is_superuser=True))
        with without_commit_drain():
            response = self.client.post(reverse("dcim:interface_delete", args=[interface.pk]), {"confirm": True})
        self.assertEqual(response.status_code, 302)

    def _assert_deletion_proven(self, build, delete):
        scope, native, owner = build()
        snapshot = _snapshot(self.management, owner)
        delete(native)
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

    def test_orm_vlan_delete_proves_cascaded_vlan_owner_deletion(self):
        self._assert_deletion_proven(self._vlan, self._orm_delete)

    def test_ui_interface_delete_proves_cascaded_subinterface_owner_deletion(self):
        self._assert_deletion_proven(self._subinterface, self._view_delete)

    def test_ui_interface_delete_proves_cascaded_svi_owner_deletion(self):
        self._assert_deletion_proven(self._svi, self._view_delete)


class TestAbsentOwnerEvidence(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The deferred pass proves any owner a foreign writer removed, and nothing else."""

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("absent-owner", 18521)
        self.attempt = NSOApplyAttempt.objects.create(management=self.management)

    def _epoch(self, management=None):
        from netbox_nso_plugin.durable_capture import mapping_epoch

        return mapping_epoch(management or self.management)

    def _retained(self, label, pk, *, epoch=None, snapshot=True, snapshot_epoch=None, **fields):
        epoch = epoch or self._epoch()
        fence = NSOOverlaySettlementFence.objects.create(
            management=self.management, mapping_epoch=epoch, model_label=label, overlay_pk=pk, **fields
        )
        if snapshot:
            NSOApplyOverlaySnapshot.objects.create(
                attempt=self.attempt,
                mapping_epoch=snapshot_epoch or epoch,
                model_label=label,
                overlay_pk=pk,
                prior_status="accepted",
                expected_status="accepted",
                content_digest=fence.content_digest,
                expected_fence_revision=fence.bookkeeping_revision,
                expected_owner_revision=fence.owner_revision,
            )
        return fence

    def _state(self, fence):
        fence.refresh_from_db()
        return fence.bookkeeping_revision, fence.content_digest, fence.current_attempt_id

    def test_foreign_queryset_delete_of_an_owner_without_a_native_parent_is_proven(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        owner = acquire_overlay(
            NSOLoggingHostState, management=self.management, address="192.0.2.5", severity="critical", status="accepted"
        )
        snapshot = _snapshot(self.management, owner)
        NSOLoggingHostState.objects.filter(pk=owner.pk).delete()

        with without_commit_drain():
            reconcile_scope_ownership(self.device.pk, ["logging"])

        self.assertTrue(owner_deletion_proven(self.management, snapshot))

    def test_every_overlay_family_is_proven_from_its_fence(self):
        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners
        from netbox_nso_plugin.refusal_registry import OVERLAYS

        fences = [
            self._retained(label, 10**12 + index, current_attempt=self.attempt)
            for index, label in enumerate(sorted(OVERLAYS))
        ]
        scopes = {scope for spec in OVERLAYS.values() for scope in spec.delivery_keys}

        self.assertEqual(prove_absent_owners(self.device.pk, scopes), len(OVERLAYS))
        for fence in fences:
            fence.refresh_from_db()
            self.assertEqual(fence.content_digest, deleted_owner_digest(fence.model_label, fence.overlay_pk))
            self.assertIsNone(fence.current_attempt_id)
            self.assertEqual(fence.bookkeeping_revision, 1)
        self.assertEqual(prove_absent_owners(self.device.pk, scopes), 0)

    def test_owner_alive_on_another_device_gets_no_evidence(self):
        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners
        from netbox_nso_plugin.models import NSOLoggingHostState

        _other_device, other = make_managed("absent-owner-other", 18522)
        owner = acquire_overlay(
            NSOLoggingHostState, management=other, address="192.0.2.6", severity="critical", status="accepted"
        )
        fence = self._retained(owner._meta.label_lower, owner.pk, current_attempt=self.attempt)
        before = self._state(fence)

        self.assertEqual(prove_absent_owners(self.device.pk, {"logging"}), 0)
        self.assertEqual(self._state(fence), before)

    def test_reader_refuses_evidence_while_the_owner_row_exists(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        owner = acquire_overlay(
            NSOLoggingHostState, management=self.management, address="192.0.2.7", severity="critical", status="accepted"
        )
        snapshot = _snapshot(self.management, owner)
        NSOOverlaySettlementFence.objects.filter(
            management=self.management,
            mapping_epoch=snapshot.mapping_epoch,
            model_label=snapshot.model_label,
            overlay_pk=owner.pk,
        ).update(
            bookkeeping_revision=snapshot.expected_fence_revision + 1,
            content_digest=deleted_owner_digest(snapshot.model_label, owner.pk),
            current_attempt=None,
        )

        self.assertFalse(owner_deletion_proven(self.management, snapshot))

    def test_never_selected_and_other_epoch_fences_stay_unchanged(self):
        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners

        label = "netbox_nso_plugin.nsologginghoststate"
        unselected = self._retained(label, 10**12 + 1, snapshot=False)
        older = self._retained(label, 10**12 + 2, epoch="older-mapping", current_attempt=self.attempt)
        before = (self._state(unselected), self._state(older))

        self.assertEqual(prove_absent_owners(self.device.pk, {"logging"}), 0)
        self.assertEqual((self._state(unselected), self._state(older)), before)

    def test_snapshot_from_another_epoch_is_not_selection_history(self):
        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners

        fence = self._retained("netbox_nso_plugin.nsologginghoststate", 10**12 + 5, snapshot_epoch="older-mapping")
        before = self._state(fence)

        self.assertEqual(prove_absent_owners(self.device.pk, {"logging"}), 0)
        self.assertEqual(self._state(fence), before)

    def test_tombstone_older_than_a_retained_snapshot_is_advanced_once(self):
        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners

        label = "netbox_nso_plugin.nsologginghoststate"
        fence = self._retained(label, 10**12 + 6, current_attempt=self.attempt)
        self.assertEqual(prove_absent_owners(self.device.pk, {"logging"}), 1)
        fence.refresh_from_db()
        # A later incarnation of the key is captured at the tombstone revision, as capture does not advance it.
        later = NSOApplyOverlaySnapshot.objects.create(
            attempt=NSOApplyAttempt.objects.create(management=self.management),
            mapping_epoch=fence.mapping_epoch,
            model_label=label,
            overlay_pk=fence.overlay_pk,
            prior_status="accepted",
            expected_status="accepted",
            content_digest=fence.content_digest,
            expected_fence_revision=fence.bookkeeping_revision,
            expected_owner_revision=fence.owner_revision,
        )
        self.assertFalse(owner_deletion_proven(self.management, later))

        self.assertEqual(prove_absent_owners(self.device.pk, {"logging"}), 1)
        self.assertTrue(owner_deletion_proven(self.management, later))
        after = self._state(fence)
        self.assertEqual(prove_absent_owners(self.device.pk, {"logging"}), 0)
        self.assertEqual(self._state(fence), after)

    def test_owner_outside_the_requested_scopes_stays_unchanged(self):
        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners

        fence = self._retained("netbox_nso_plugin.nsovlanstate", 10**12 + 3, current_attempt=self.attempt)
        before = self._state(fence)

        self.assertEqual(prove_absent_owners(self.device.pk, {"logging"}), 0)
        self.assertEqual(self._state(fence), before)

    def test_proof_refuses_to_join_an_active_permit(self):
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError, MutationFootprint, mirror_transaction
        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners

        with (
            self.assertRaises(IntentMutationProtocolError),
            mirror_transaction(MutationFootprint(device_ids=(self.device.pk,))),
        ):
            prove_absent_owners(self.device.pk, {"logging"})

    def test_pass_without_candidates_takes_no_lock(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.lifecycle_fence import prove_absent_owners

        self._retained("netbox_nso_plugin.nsologginghoststate", 10**12 + 4, snapshot=False)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(prove_absent_owners(self.device.pk, {"logging", "vlan"}), 0)
        self.assertFalse(any("FOR UPDATE" in query["sql"] for query in captured.captured_queries))
