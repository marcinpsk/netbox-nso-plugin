# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Exact owner deletion persists only with its committed writer transaction."""

from dcim.models import Interface
from django.db import transaction
from django.test import TransactionTestCase

from netbox_nso_plugin.delivery import canonical_fingerprint
from netbox_nso_plugin.models import (
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSOInterfaceState,
    NSOOverlaySettlementFence,
)
from netbox_nso_plugin.ownership_grants import OwnershipGrant
from netbox_nso_plugin.renderer_writer import (
    RendererMutationPlan,
    planned_delete,
    renderer_mirror_writes,
    renderer_writes,
)

from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestOwnerDeletionFence(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("owner-deletion-fence", 1767)
        with without_commit_drain(), transaction.atomic():
            self.management.manage_description = True
            self.management.save(update_fields={"manage_description"})
            interface = Interface.objects.create(
                device=self.device, name="Ethernet1", type="1000base-t", description="placeholder desired"
            )
            self.owner = acquire_overlay(
                NSOInterfaceState, interface=interface, attribute="description", status="accepted"
            )
        self.owner_pk = self.owner.pk
        from netbox_nso_plugin.lifecycle_fence import fence_for

        with transaction.atomic():
            self.fence = fence_for(self.owner)
        self.before = (self.fence.bookkeeping_revision, self.fence.content_digest, self.fence.current_attempt_id)
        self.snapshot = NSOApplyOverlaySnapshot.objects.create(
            attempt=NSOApplyAttempt.objects.create(management=self.management),
            mapping_epoch=self.fence.mapping_epoch,
            model_label=self.owner._meta.label_lower,
            overlay_pk=self.owner_pk,
            prior_status=self.owner.status,
            expected_status=self.owner.status,
            content_digest=self.fence.content_digest,
            expected_fence_revision=self.fence.bookkeeping_revision,
            expected_owner_revision=self.fence.owner_revision,
        )

    def _delete(self):
        plan = RendererMutationPlan.build(grant=OwnershipGrant("operator_edit"), deletes=(planned_delete(self.owner),))
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with without_commit_drain(), mutation(plan) as writer:
            writer.delete(self.owner)

    def test_never_selected_unowned_delete_preserves_lifecycle_authority(self):
        from netbox_nso_plugin.lifecycle_fence import fence_for

        from ._outbox_case import mirror_update
        from ._ownership_case import save_overlay_fixture

        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(device=self.device, name="Ethernet3", type="1000base-t")
            self.owner = save_overlay_fixture(
                NSOInterfaceState(interface=interface, attribute="description", status="imported")
            )
            fence = fence_for(self.owner)
        mirror_update(self.owner, last_apply_error="placeholder observed error")
        fence.refresh_from_db()
        self.assertGreater(fence.bookkeeping_revision, 0)
        self.assertEqual(fence.owner_revision, 0)
        before = (fence.bookkeeping_revision, fence.owner_revision, fence.content_digest)
        self._delete()
        fence.refresh_from_db()
        self.assertEqual((fence.bookkeeping_revision, fence.owner_revision, fence.content_digest), before)

    def test_released_selected_owner_retains_deletion_evidence(self):
        from netbox_nso_plugin.lifecycle_fence import owner_deletion_proven

        from ._outbox_case import content_update

        content_update(self.owner, status="imported")
        self._delete()
        self.assertTrue(owner_deletion_proven(self.management, self.snapshot))

    def test_historical_selection_retains_deletion_evidence_after_release(self):
        from ._outbox_case import content_update

        content_update(self.owner, status="imported", accepted_at=None, apply_attempt_id=None)
        self.fence.refresh_from_db()
        self.fence.owner_revision = 0
        self.fence.accepted_at = None
        self.fence.save(update_fields=("owner_revision", "accepted_at"))
        self.snapshot.expected_owner_revision = None
        self.snapshot.save(update_fields=("expected_owner_revision",))
        before = self.fence.bookkeeping_revision
        self._delete()
        self.fence.refresh_from_db()
        self.assertGreater(self.fence.bookkeeping_revision, before)
        self.assertEqual(
            self.fence.content_digest,
            canonical_fingerprint(("deleted-owner", self.owner._meta.label_lower, self.owner_pk)),
        )

    def test_exact_writer_deletion_retains_a_new_absent_owner_fence(self):
        self._delete()

        self.assertFalse(NSOInterfaceState.objects.filter(pk=self.owner_pk).exists())
        self.fence.refresh_from_db()
        self.assertGreater(self.fence.bookkeeping_revision, self.before[0])
        self.assertEqual(
            self.fence.content_digest,
            canonical_fingerprint(("deleted-owner", self.owner._meta.label_lower, self.owner_pk)),
        )
        self.assertIsNone(self.fence.current_attempt_id)

    def test_rolled_back_deletion_restores_owner_and_original_fence(self):
        with self.assertRaisesRegex(RuntimeError, "roll back deletion"):
            with transaction.atomic():
                self._delete()
                raise RuntimeError("roll back deletion")

        self.assertTrue(NSOInterfaceState.objects.filter(pk=self.owner_pk).exists())
        self.fence.refresh_from_db()
        self.assertEqual(
            (self.fence.bookkeeping_revision, self.fence.content_digest, self.fence.current_attempt_id), self.before
        )

    def test_deletion_proof_requires_exact_mapping_and_a_newer_fence(self):
        from netbox_nso_plugin.lifecycle_fence import owner_deletion_proven

        self._delete()
        self.assertTrue(owner_deletion_proven(self.management, self.snapshot))
        self.snapshot.mapping_epoch = "another-mapping"
        self.assertFalse(owner_deletion_proven(self.management, self.snapshot))
        self.snapshot.mapping_epoch = self.fence.mapping_epoch
        self.fence.refresh_from_db()
        self.snapshot.expected_fence_revision = self.fence.bookkeeping_revision
        self.assertFalse(owner_deletion_proven(self.management, self.snapshot))

    def test_retained_deleted_fence_locks_before_live_overlays(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.intent_state import (
            MutationFootprint,
            SourceRow,
            footprint_for_instance,
            mirror_transaction,
        )

        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(
                device=self.device, name="Ethernet2", type="1000base-t", description="placeholder sibling"
            )
            sibling = acquire_overlay(
                NSOInterfaceState, interface=interface, attribute="description", status="accepted"
            )
        self._delete()
        footprint = MutationFootprint.merge(
            footprint_for_instance(sibling),
            MutationFootprint(overlay_rows=(SourceRow(self.owner._meta.label_lower, self.owner_pk),)),
        )
        with CaptureQueriesContext(connection) as captured, mirror_transaction(footprint):
            pass
        locks = [query["sql"] for query in captured.captured_queries if "FOR UPDATE" in query["sql"]]
        fence_locks = [
            index for index, sql in enumerate(locks) if 'FROM "netbox_nso_plugin_nsooverlaysettlementfence"' in sql
        ]
        overlay_locks = [
            index for index, sql in enumerate(locks) if f'FROM "{NSOInterfaceState._meta.db_table}"' in sql
        ]
        self.assertGreaterEqual(len(fence_locks), 2)
        self.assertTrue(overlay_locks)
        self.assertLess(max(fence_locks), min(overlay_locks))

    def test_unexplained_absence_rolls_back_without_deletion_evidence(self):
        from netbox_nso_plugin.intent_state import (
            IntentMutationProtocolError,
            footprint_for_instance,
            intent_transaction,
        )

        with self.assertRaisesRegex(IntentMutationProtocolError, "vanished before owner finalization"):
            with without_commit_drain(), intent_transaction(footprint_for_instance(self.owner)):
                NSOInterfaceState.objects.filter(pk=self.owner_pk)._raw_delete("default")

        self.assertTrue(NSOInterfaceState.objects.filter(pk=self.owner_pk).exists())
        self.fence.refresh_from_db()
        self.assertEqual(
            (self.fence.bookkeeping_revision, self.fence.content_digest, self.fence.current_attempt_id), self.before
        )

    def test_unexplained_fence_loss_with_live_management_rolls_back_deletion(self):
        plan = RendererMutationPlan.build(grant=OwnershipGrant("operator_edit"), deletes=(planned_delete(self.owner),))
        with self.assertRaises(NSOOverlaySettlementFence.NotUpdated):
            with without_commit_drain(), renderer_writes(plan) as writer:
                NSOOverlaySettlementFence.objects.filter(pk=self.fence.pk).delete()
                writer.delete(self.owner)

        self.assertTrue(NSOInterfaceState.objects.filter(pk=self.owner_pk).exists())
        self.fence.refresh_from_db()
        self.assertEqual(
            (self.fence.bookkeeping_revision, self.fence.content_digest, self.fence.current_attempt_id), self.before
        )
