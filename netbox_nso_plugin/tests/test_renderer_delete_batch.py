# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Frozen interface-IP deletion groups at the real ORM seam."""

import copy
import dataclasses

from dcim.models import Interface
from django.db import connection
from django.db.models.signals import post_delete
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from extras.models import Tag
from ipam.models import IPAddress

from netbox_nso_plugin.intent_state import IntentMutationProtocolError, mirror_transaction
from netbox_nso_plugin.models import NSOInterfaceIPState, NSOOwnershipAcquisition, NSOOwnershipManifest
from netbox_nso_plugin.renderer_writer import (
    RendererMutationPlan,
    consume_renderer_plan,
    planned_delete,
    planned_save,
    renderer_mirror_writes,
    renderer_writes,
)

from ._outbox_case import make_managed
from ._ownership_case import acquire_overlay


class TestRendererDeleteBatch(TestCase):
    def setUp(self):
        self.device, self.management = make_managed("ip-delete-batch", 18100)
        self.interface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        self.first = NSOInterfaceIPState.objects.create(
            interface=self.interface, address="198.18.0.1/32", status="imported"
        )
        self.second = NSOInterfaceIPState.objects.create(
            interface=self.interface, address="198.18.0.2/32", status="imported"
        )
        self.retained = NSOInterfaceIPState.objects.create(
            interface=self.interface, address="198.18.0.3/32", status="imported", peer_state=self.first
        )
        self.tag = Tag.objects.create(name="Delete group tag", slug="delete-group-tag")
        self.first.tags.add(self.tag)
        self.roots = (self.first, self.second)
        self.root_ids = tuple(row.pk for row in self.roots)

    def _plan(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        return RendererMutationPlan.build(delete_batches=(planned_delete_many(self.roots),))

    def test_batch_deletes_generic_children_and_clears_a_retained_peer(self):
        through = self.first.tags.through
        plan = self._plan()
        with renderer_mirror_writes(plan) as writer:
            writer.delete_many(tuple(reversed(self.roots)))
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).exists())
        self.assertFalse(through.objects.filter(object_id__in=self.root_ids).exists())
        self.retained.refresh_from_db()
        self.assertIsNone(self.retained.peer_state_id)
        self.assertEqual(self.retained.status, "imported")
        self.assertTrue(Tag.objects.filter(pk=self.tag.pk).exists())

    def test_batch_execution_does_not_reload_root_footprints(self):
        through = self.first.tags.through
        plan = self._plan()
        with renderer_mirror_writes(plan) as writer, CaptureQueriesContext(connection) as queries:
            writer.delete_many(self.roots)
        interface_reads = [query["sql"] for query in queries if 'FROM "dcim_interface"' in query["sql"]]
        self.assertEqual(interface_reads, [])
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).exists())
        self.assertFalse(through.objects.filter(object_id__in=self.root_ids).exists())
        self.retained.refresh_from_db()
        self.assertIsNone(self.retained.peer_state_id)

    def test_single_delete_execution_keeps_interface_reads_bounded(self):
        plan = RendererMutationPlan.build(deletes=(planned_delete(self.first),))
        with renderer_mirror_writes(plan) as writer, CaptureQueriesContext(connection) as queries:
            writer.delete(self.first)
        interface_reads = [query["sql"] for query in queries if 'FROM "dcim_interface"' in query["sql"]]
        self.assertLessEqual(len(interface_reads), 1, interface_reads)
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk=self.root_ids[0]).exists())
        self.assertTrue(NSOInterfaceIPState.objects.filter(pk=self.root_ids[1]).exists())
        self.retained.refresh_from_db()
        self.assertIsNone(self.retained.peer_state_id)

    def test_changed_root_rejects_the_entire_batch(self):
        plan = self._plan()
        self.second.nso_value = "changed observation"
        self.second.save(update_fields=["nso_value"])
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.delete_many(self.roots)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)

    def test_new_generic_child_rejects_the_entire_batch(self):
        plan = self._plan()
        new_tag = Tag.objects.create(name="New group tag", slug="new-group-tag")
        self.second.tags.add(new_tag)
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.delete_many(self.roots)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)
        self.assertEqual(self.second.tags.count(), 1)

    def test_omitted_generic_child_rejects_the_entire_batch(self):
        plan = self._plan()
        incomplete = dataclasses.replace(
            plan, write_set=tuple(write for write in plan.write_set if write.model_label != "extras.taggeditem")
        )
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(incomplete) as writer:
            writer.delete_many(self.roots)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)
        self.assertEqual(self.first.tags.count(), 1)

    def test_changed_peer_target_rejects_the_entire_batch(self):
        plan = self._plan()
        self.retained.peer_state = self.second
        self.retained.save(update_fields=["peer_state"])
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.delete_many(self.roots)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)
        self.retained.refresh_from_db()
        self.assertEqual(self.retained.peer_state_id, self.second.pk)

    def test_unplanned_root_cannot_join_the_batch(self):
        plan = self._plan()
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.delete_many((self.first, self.retained))
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)
        self.assertTrue(NSOInterfaceIPState.objects.filter(pk=self.retained.pk).exists())

    def test_batch_cannot_be_consumed_twice(self):
        plan = self._plan()
        replay = tuple(copy.copy(row) for row in self.roots)
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.delete_many(self.roots)
            writer.delete_many(replay)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)

    def test_delete_signal_failure_rolls_back_every_root_and_generic_child(self):
        plan = self._plan()

        def fail_delete(sender, instance, **kwargs):
            raise RuntimeError("delete observer failed")

        dispatch_uid = "test_interface_ip_batch_delete_failure"
        post_delete.connect(fail_delete, sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, "delete observer failed"), renderer_mirror_writes(plan) as writer:
                writer.delete_many(self.roots)
        finally:
            post_delete.disconnect(sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)
        self.assertEqual(self.first.tags.count(), 1)
        self.retained.refresh_from_db()
        self.assertEqual(self.retained.peer_state_id, self.root_ids[0])

    def test_generic_children_emit_delete_signals_before_roots(self):
        through = self.first.tags.through
        events = []

        def record_delete(sender, instance, **kwargs):
            events.append((sender, instance.pk))

        dispatch_uid = "test_interface_ip_batch_delete_order"
        for model in (through, NSOInterfaceIPState):
            post_delete.connect(record_delete, sender=model, dispatch_uid=dispatch_uid, weak=False)
        try:
            with renderer_mirror_writes(self._plan()) as writer:
                writer.delete_many(self.roots)
        finally:
            for model in (through, NSOInterfaceIPState):
                post_delete.disconnect(sender=model, dispatch_uid=dispatch_uid)
        child_positions = [i for i, (model, _) in enumerate(events) if model is through]
        root_positions = [i for i, (model, _) in enumerate(events) if model is NSOInterfaceIPState]
        self.assertEqual(len(child_positions), 1)
        self.assertEqual(len(root_positions), 2)
        self.assertLess(max(child_positions), min(root_positions))

    def test_missing_root_rejects_the_entire_batch(self):
        plan = self._plan()
        self.second.delete()
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.delete_many(self.roots)
        self.assertTrue(NSOInterfaceIPState.objects.filter(pk=self.root_ids[0]).exists())
        self.assertEqual(self.first.tags.count(), 1)

    def test_retained_peer_lifecycle_save_can_precede_the_batch(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        candidate = copy.copy(self.retained)
        candidate.last_sync_at = timezone.now()
        plan = RendererMutationPlan.build(
            saves=(planned_save(candidate, update_fields=("last_sync_at",)),),
            delete_batches=(planned_delete_many(self.roots),),
        )
        with renderer_mirror_writes(plan) as writer:
            writer.save(candidate, update_fields=("last_sync_at",))
            writer.delete_many(self.roots)
        self.retained.refresh_from_db()
        self.assertIsNone(self.retained.peer_state_id)
        self.assertEqual(self.retained.last_sync_at, candidate.last_sync_at)

    def test_conflicting_retained_peer_save_is_rejected_before_execution(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        candidate = copy.copy(self.retained)
        candidate.peer_state = self.second
        with self.assertRaises(IntentMutationProtocolError):
            RendererMutationPlan.build(
                saves=(planned_save(candidate, update_fields=("peer_state",)),),
                delete_batches=(planned_delete_many(self.roots),),
            )
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)

    def test_delete_failure_rolls_back_an_earlier_save(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        candidate = copy.copy(self.retained)
        candidate.last_sync_at = timezone.now()
        original_sync = self.retained.last_sync_at
        plan = RendererMutationPlan.build(
            saves=(planned_save(candidate, update_fields=("last_sync_at",)),),
            delete_batches=(planned_delete_many(self.roots),),
        )

        def fail_delete(sender, instance, **kwargs):
            raise RuntimeError("delete observer failed")

        dispatch_uid = "test_interface_ip_batch_save_rollback"
        post_delete.connect(fail_delete, sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, "delete observer failed"), renderer_mirror_writes(plan) as writer:
                writer.save(candidate, update_fields=("last_sync_at",))
                writer.delete_many(self.roots)
        finally:
            post_delete.disconnect(sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid)
        self.retained.refresh_from_db()
        self.assertEqual(self.retained.last_sync_at, original_sync)
        self.assertEqual(self.retained.peer_state_id, self.root_ids[0])
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)

    def test_owned_batch_retires_manifest_and_acquisition_evidence(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        IPAddress.objects.create(address="198.18.0.4/32", assigned_object=self.interface)
        owned = acquire_overlay(
            NSOInterfaceIPState, interface=self.interface, address="198.18.0.4/32", status="accepted"
        )
        unbound = acquire_overlay(
            NSOInterfaceIPState, interface=self.interface, address="198.18.0.5/32", status="accepted"
        )
        manifest = NSOOwnershipManifest.objects.get(state_model_label=owned._meta.label_lower)
        self.assertTrue(NSOOwnershipAcquisition.objects.filter(state_model_label=owned._meta.label_lower).exists())
        plan = RendererMutationPlan.build(delete_batches=(planned_delete_many((owned, unbound)),))
        self.assertTrue(plan.changes_content)
        with renderer_writes(plan) as writer:
            writer.delete_many((owned, unbound))
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertFalse(NSOOwnershipAcquisition.objects.filter(state_model_label=owned._meta.label_lower).exists())

    def test_duplicate_roots_are_rejected(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        with self.assertRaises(IntentMutationProtocolError):
            RendererMutationPlan.build(delete_batches=(planned_delete_many((self.first, self.first)),))

    def test_mixed_models_are_rejected(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        with self.assertRaises(IntentMutationProtocolError):
            RendererMutationPlan.build(delete_batches=(planned_delete_many((self.first, self.interface)),))

    def test_a_signal_cannot_delete_a_root_from_a_different_planned_batch(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        plan = RendererMutationPlan.build(
            delete_batches=(planned_delete_many((self.first,)), planned_delete_many((self.second,))),
        )

        def delete_other_group(sender, instance, **kwargs):
            self.second.delete()

        dispatch_uid = "test_interface_ip_delete_outside_active_group"
        post_delete.connect(delete_other_group, sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid, weak=False)
        try:
            with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
                writer.delete_many((self.first,))
                writer.delete_many((self.second,))
        finally:
            post_delete.disconnect(sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)

    def test_nondefault_database_roots_are_rejected(self):
        from netbox_nso_plugin.renderer_writer import planned_delete_many

        foreign = copy.copy(self.first)
        foreign._state.db = "unplanned_database"
        with self.assertRaises(IntentMutationProtocolError):
            RendererMutationPlan.build(delete_batches=(planned_delete_many((foreign, self.second)),))

    def test_missing_generic_child_rejects_the_entire_batch(self):
        plan = self._plan()
        self.first.tags.clear()
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.delete_many(self.roots)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)

    def test_caller_owned_transaction_checks_full_retained_peer_dependencies(self):
        plan = self._plan()
        self.retained.nso_value = "changed observation"
        self.retained.save(update_fields=("nso_value",))
        with (
            self.assertRaises(IntentMutationProtocolError),
            mirror_transaction(plan.lock_footprint) as permit,
            consume_renderer_plan(plan, permit, content=False) as writer,
        ):
            writer.delete_many(self.roots)
        self.assertEqual(NSOInterfaceIPState.objects.filter(pk__in=self.root_ids).count(), 2)
