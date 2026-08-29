# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Exact renderer writer behavior at the real ORM and database seams."""

import copy
import dataclasses
from uuid import uuid4

from dcim.models import Interface
from django.test import TestCase
from ipam.models import VLAN

from netbox_nso_plugin.intent_state import IntentMutationProtocolError
from netbox_nso_plugin.models import (
    NSOIntentOutboxEntry,
    NSOIntentRevision,
    NSOOwnershipManifest,
    NSOStaticRouteState,
    NSOSwitchportState,
    NSOVLANState,
)

from ._outbox_case import make_managed, mirror_update, own_vlan, without_commit_drain
from .mixins import IntentPushResetMixin


class TestRendererSetUpdate(IntentPushResetMixin, TestCase):
    def test_set_update_cannot_capture_a_row_created_after_planning(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_set_update,
            renderer_mirror_writes,
        )

        _device, management = make_managed("writer-set", 16271)
        planned_row = own_vlan(management, 1627, "writer-set")
        plan = RendererMutationPlan.build(
            set_updates=(
                planned_set_update(
                    NSOVLANState.objects.filter(management=management),
                    last_apply_error="planned",
                ),
            )
        )
        late_row = own_vlan(management, 1628, "writer-set-late")

        with renderer_mirror_writes(plan) as writer:
            writer.set_update(NSOVLANState, plan.write_set[0], last_apply_error="planned")

        planned_row.refresh_from_db()
        late_row.refresh_from_db()
        assert planned_row.last_apply_error == "planned"
        assert late_row.last_apply_error == ""


class TestRendererContentWriter(IntentPushResetMixin, TestCase):
    def test_stale_plan_error_is_a_renderer_protocol_error(self):
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.renderer_writer import IntentPlanStaleError

        self.assertTrue(issubclass(IntentPlanStaleError, IntentMutationProtocolError))

    def test_one_plan_can_create_a_native_row_and_its_overlay(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        _device, management = make_managed("writer-related-create", 16278)
        group = VLANGroup.objects.create(name="Writer related create", slug="writer-related-create")
        vlan = VLAN(group=group, vid=1637, name="writer-related-create")
        state = NSOVLANState(
            management=management,
            vlan=vlan,
            device_name=vlan.name,
            status="imported",
        )
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(vlan, force_insert=True, natural_key=("group", "vid")),
                planned_save(
                    state,
                    force_insert=True,
                    natural_key=("management", "vlan"),
                ),
            )
        )

        with renderer_mirror_writes(plan) as writer:
            writer.save(vlan, force_insert=True)
            writer.save(state, force_insert=True)

        assert state.vlan_id == vlan.pk
        assert NSOVLANState.objects.filter(management=management, vlan=vlan).exists()

    def test_one_plan_can_create_an_owner_related_row_and_m2m_edge(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_m2m_set,
            planned_save,
            renderer_mirror_writes,
        )

        device, management = make_managed("writer-create-m2m", 16279)
        interface = Interface.objects.create(device=device, name="Ethernet1/9", type="1000base-t")
        vlan = VLAN(vid=1639, name="writer-create-m2m")
        state = NSOSwitchportState(
            management=management,
            interface=interface,
            mode="tagged",
            status="imported",
        )
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(vlan, force_insert=True, natural_key=("group", "vid")),
                planned_save(
                    state,
                    force_insert=True,
                    natural_key=("management", "interface"),
                ),
            ),
            m2m_writes=(planned_m2m_set(state, "tagged_vlans", (vlan,)),),
        )

        with renderer_mirror_writes(plan) as writer:
            writer.save(vlan, force_insert=True)
            writer.save(state, force_insert=True)
            writer.m2m_set(state, "tagged_vlans", (vlan,))

        assert set(state.tagged_vlans.values_list("pk", flat=True)) == {vlan.pk}

    def test_m2m_add_consumes_an_exact_frozen_edge_and_finalizes_fingerprint(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_m2m_add,
            renderer_writes,
        )

        device, management = make_managed("writer-m2m", 16277)
        mirror_update(management, auto_apply=True)
        interface = Interface.objects.create(device=device, name="Ethernet1/7", type="1000base-t")
        state = NSOSwitchportState.objects.create(
            management=management,
            interface=interface,
            mode="tagged",
            status="accepted",
        )
        planned_vlan = VLAN.objects.create(vid=1635, name="writer-m2m-planned")
        outside_vlan = VLAN.objects.create(vid=1636, name="writer-m2m-outside")
        NSOIntentOutboxEntry.objects.filter(device=device, scope="switchport").delete()
        revision, _created = NSOIntentRevision.objects.get_or_create(device=device, scope="switchport")
        before = revision.revision
        plan = RendererMutationPlan.build(m2m_writes=(planned_m2m_add(state, "tagged_vlans", (planned_vlan,)),))

        with self.assertRaises(IntentMutationProtocolError):
            with renderer_writes(plan) as writer:
                writer.m2m_add(state, "tagged_vlans", (outside_vlan,))

        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.m2m_add(state, "tagged_vlans", (planned_vlan,))

        revision.refresh_from_db()
        expected = delivery.canonical_fingerprint(
            delivery.render("switchport", device.pk, management.adapter_device_id).payload
        )
        assert set(state.tagged_vlans.values_list("pk", flat=True)) == {planned_vlan.pk}
        assert revision.revision == before + 1
        assert revision.verified_revision == revision.revision
        assert revision.verified_fingerprint == expected

    def test_m2m_plan_refuses_an_unregistered_related_model(self):
        from extras.models import Tag

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_m2m_add

        device, management = make_managed("writer-unregistered-m2m", 16288)
        interface = Interface.objects.create(device=device, name="Ethernet1/8", type="1000base-t")
        state = NSOSwitchportState.objects.create(
            management=management,
            interface=interface,
            mode="tagged",
            status="imported",
        )
        tag = Tag.objects.create(name="unregistered renderer relation", slug="unregistered-renderer-relation")

        with self.assertRaisesRegex(IntentMutationProtocolError, "unregistered.*related"):
            RendererMutationPlan.build(m2m_writes=(planned_m2m_add(state, "tags", (tag,)),))

    def test_an_unchanged_m2m_set_is_content_neutral_across_digit_widths(self):
        from netbox_nso_plugin.intent_state import MutationFootprint, SourceRow, mirror_transaction
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_m2m_set, renderer_writes
        from netbox_nso_plugin.signals import suppress_intent_push

        device, management = make_managed("writer-m2m-order", 16289)
        interface = Interface.objects.create(device=device, name="Ethernet1/10", type="1000base-t")
        state = NSOSwitchportState.objects.create(
            management=management,
            interface=interface,
            mode="tagged",
            status="accepted",
        )
        footprint = MutationFootprint.for_keys((), source_rows=(SourceRow(VLAN._meta.label_lower, None),))
        with mirror_transaction(footprint), suppress_intent_push():
            lower = VLAN.objects.create(pk=2_000_000, vid=1641, name="writer-m2m-lower")
            higher = VLAN.objects.create(pk=10_000_000, vid=1642, name="writer-m2m-higher")
        seed = RendererMutationPlan.build(m2m_writes=(planned_m2m_set(state, "tagged_vlans", (lower, higher)),))
        with renderer_writes(seed) as writer:
            writer.m2m_set(state, "tagged_vlans", (lower, higher))

        plan = RendererMutationPlan.build(m2m_writes=(planned_m2m_set(state, "tagged_vlans", (lower, higher)),))

        self.assertFalse(plan.changes_content)

    def test_content_save_bumps_repends_enqueues_and_finalizes_fingerprint(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_writes,
        )

        device, management = make_managed("writer-content", 16272)
        target = NSOVLANState.objects.create(
            management=management,
            vlan=VLAN.objects.create(vid=1627, name="writer-content-target"),
            status="imported",
        )
        deploying = own_vlan(management, 1628, "writer-content-peer")
        mirror_update(deploying, status="deploying", apply_attempt_id=uuid4())
        NSOIntentOutboxEntry.objects.filter(device=device, scope="vlan").delete()
        revision, _created = NSOIntentRevision.objects.get_or_create(device=device, scope="vlan")
        before = revision.revision

        candidate = copy.copy(target)
        candidate.status = "accepted"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("status",)),))
        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.save(candidate, update_fields=("status",))

        target.refresh_from_db()
        deploying.refresh_from_db()
        revision.refresh_from_db()
        expected = delivery.canonical_fingerprint(
            delivery.render("vlan", device.pk, management.adapter_device_id).payload
        )
        assert target.status == "accepted"
        assert deploying.status == "accepted"
        assert deploying.apply_attempt_id is None
        assert revision.revision == before + 1
        assert revision.verified_revision == revision.revision
        assert revision.verified_fingerprint == expected
        assert NSOIntentOutboxEntry.objects.filter(device=device, scope="vlan").count() == 1
        assert NSOOwnershipManifest.objects.filter(
            device=device,
            scope="vlan",
            native_model_label="ipam.vlan",
            native_key={"group_id": target.vlan.group_id, "vid": target.vlan.vid},
            ownership_state="owned",
            deletion_authority=True,
        ).exists()

    def test_save_outside_the_frozen_write_set_fails_before_dml(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        _device, management = make_managed("writer-exact", 16273)
        planned_row = own_vlan(management, 1630, "writer-exact-planned")
        other_row = own_vlan(management, 1631, "writer-exact-other")
        candidate = copy.copy(planned_row)
        candidate.last_apply_error = "planned"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("last_apply_error",)),))

        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan):
            other_row.last_apply_error = "outside"
            other_row.save(update_fields=("last_apply_error",))

        other_row.refresh_from_db()
        assert other_row.last_apply_error == ""

    def test_mirror_save_keeps_revision_and_fingerprint_unchanged(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        device, management = make_managed("writer-mirror", 16274)
        row = own_vlan(management, 1632, "writer-mirror")
        revision, _created = NSOIntentRevision.objects.get_or_create(device=device, scope="vlan")
        revision.verified_revision = revision.revision
        revision.verified_fingerprint = "a" * 64
        revision.save(update_fields=("verified_revision", "verified_fingerprint"))
        before = (revision.revision, revision.verified_revision, revision.verified_fingerprint)
        candidate = copy.copy(row)
        candidate.last_apply_error = "mirror-only"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("last_apply_error",)),))

        with renderer_mirror_writes(plan) as writer:
            writer.save(candidate, update_fields=("last_apply_error",))

        revision.refresh_from_db()
        assert (revision.revision, revision.verified_revision, revision.verified_fingerprint) == before

    def test_save_rejects_a_row_changed_after_plan_construction(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        _device, management = make_managed("writer-stale-management", 16280)
        candidate = copy.copy(management)
        candidate.adapter_link_error = "planned"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("adapter_link_error",)),))
        mirror_update(management, adapter_link_error="concurrent")

        with self.assertRaisesRegex(IntentMutationProtocolError, "changed after planning"):
            with renderer_mirror_writes(plan) as writer:
                writer.save(candidate, update_fields=("adapter_link_error",))

        management.refresh_from_db()
        assert management.adapter_link_error == "concurrent"

    def test_delete_refuses_a_plan_with_an_omitted_collector_target(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            renderer_writes,
        )

        device, management = make_managed("writer-cascade", 16275)
        route = StaticRoute.objects.create(prefix="198.18.44.0/24", next_hop="198.18.44.1", metric=1)
        state = NSOStaticRouteState.objects.create(
            management=management,
            static_route=route,
            status="accepted",
        )
        complete = RendererMutationPlan.build(deletes=(planned_delete(management),))
        incomplete = dataclasses.replace(
            complete,
            write_set=tuple(
                write
                for write in complete.write_set
                if not (write.model_label == state._meta.label_lower and write.pk == state.pk)
            ),
        )

        with self.assertRaisesRegex(IntentMutationProtocolError, "Collector cascade"), renderer_writes(incomplete):
            from netbox_nso_plugin.renderer_writer import active_renderer_writer

            active_renderer_writer().delete(management)

        assert type(management).objects.filter(pk=management.pk).exists()
        assert NSOStaticRouteState.objects.filter(pk=state.pk).exists()
        assert type(device).objects.filter(pk=device.pk).exists()

    def test_delete_plan_records_collector_set_null_updates(self):
        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete

        device, management = make_managed("writer-set-null", 16290)
        interface = Interface.objects.create(device=device, name="Vlan1643", type="virtual")
        vlan = VLAN.objects.create(vid=1643, name="writer-set-null")
        state = NSOSVIState.objects.create(
            management=management,
            interface=interface,
            vlan=vlan,
            status="imported",
        )

        plan = RendererMutationPlan.build(deletes=(planned_delete(vlan),))

        self.assertIn(
            ("set_update", state._meta.label_lower, state.pk, ("vlan",), (("vlan_id", None),)),
            tuple(
                (write.operation, write.model_label, write.pk, write.update_fields, write.values)
                for write in plan.write_set
            ),
        )

    def test_rollback_removes_content_bookkeeping_and_fingerprint_together(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        device, management = make_managed("writer-rollback", 16276)
        target = NSOVLANState.objects.create(
            management=management,
            vlan=VLAN.objects.create(vid=1633, name="writer-rollback-target"),
            status="imported",
        )
        deploying = own_vlan(management, 1634, "writer-rollback-peer")
        attempt_id = uuid4()
        mirror_update(deploying, status="deploying", apply_attempt_id=attempt_id)
        NSOIntentOutboxEntry.objects.filter(device=device, scope="vlan").delete()
        revision, _created = NSOIntentRevision.objects.get_or_create(device=device, scope="vlan")
        revision.verified_revision = revision.revision
        revision.verified_fingerprint = "b" * 64
        revision.save(update_fields=("verified_revision", "verified_fingerprint"))
        before = (revision.revision, revision.verified_revision, revision.verified_fingerprint)
        candidate = copy.copy(target)
        candidate.status = "accepted"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("status",)),))

        with self.assertRaisesRegex(RuntimeError, "rollback"), without_commit_drain():
            with renderer_writes(plan) as writer:
                writer.save(candidate, update_fields=("status",))
                raise RuntimeError("rollback writer")

        target.refresh_from_db()
        deploying.refresh_from_db()
        revision.refresh_from_db()
        assert target.status == "imported"
        assert deploying.status == "deploying"
        assert deploying.apply_attempt_id == attempt_id
        assert (revision.revision, revision.verified_revision, revision.verified_fingerprint) == before
        assert not NSOIntentOutboxEntry.objects.filter(device=device, scope="vlan").exists()
