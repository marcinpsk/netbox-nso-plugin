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
    def test_set_update_rejects_a_selected_row_changed_after_plan_build(self):
        from django.db import transaction

        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.renderer_writer import (
            IntentPlanStaleError,
            RendererMutationPlan,
            planned_set_update,
            renderer_writes,
        )

        _first_device, first_management = make_managed("writer-set-race-first", 16272)
        second_device, second_management = make_managed("writer-set-race-second", 16273)
        vlan = VLAN.objects.create(vid=1629, name="writer-set-race")
        state = NSOVLANState.objects.create(management=first_management, vlan=vlan, status="imported")
        NSOSVIState.objects.create(
            management=second_management,
            interface=Interface.objects.create(device=second_device, name="Vlan1629", type="virtual"),
            vlan=vlan,
            svi_type="svi",
            status="imported",
        )
        plan = RendererMutationPlan.build(
            set_updates=(
                planned_set_update(
                    NSOVLANState.objects.filter(management=first_management),
                    status="accepted",
                ),
            )
        )
        with transaction.atomic(), offline_mutation():
            NSOVLANState.objects.filter(pk=state.pk).update(management_id=second_management.pk)

        with self.assertRaises(IntentPlanStaleError), renderer_writes(plan) as writer:
            writer.set_update(NSOVLANState, plan.write_set[0], status="accepted")

        state.refresh_from_db()
        self.assertEqual(state.management_id, second_management.pk)
        self.assertEqual(state.status, "imported")

    def test_set_update_rejects_a_selected_row_changed_before_plan_build(self):
        from django.db import transaction

        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.renderer_writer import (
            IntentPlanStaleError,
            RendererMutationPlan,
            planned_set_update,
        )

        _first_device, first_management = make_managed("writer-set-build-race-first", 16274)
        _second_device, second_management = make_managed("writer-set-build-race-second", 16275)
        vlan = VLAN.objects.create(vid=1630, name="writer-set-build-race")
        state = NSOVLANState.objects.create(management=first_management, vlan=vlan, status="imported")
        proposed = planned_set_update(
            NSOVLANState.objects.filter(management=first_management),
            status="accepted",
        )
        with transaction.atomic(), offline_mutation():
            NSOVLANState.objects.filter(pk=state.pk).update(management_id=second_management.pk)

        with self.assertRaises(IntentPlanStaleError):
            RendererMutationPlan.build(set_updates=(proposed,))

        state.refresh_from_db()
        self.assertEqual(state.management_id, second_management.pk)
        self.assertEqual(state.status, "imported")

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
    def test_effective_after_clears_a_stale_relation_cache(self):
        from netbox_nso_plugin.intent_state import _effective_after
        from netbox_nso_plugin.models import NSOLoggingHostState

        _first_device, first_management = make_managed("writer-relation-first", 16289)
        _second_device, second_management = make_managed("writer-relation-second", 16290)
        state = NSOLoggingHostState.objects.create(
            management=first_management,
            address="198.18.0.89",
        )
        before = NSOLoggingHostState.objects.select_related("management").get(pk=state.pk)
        candidate = copy.copy(before)
        candidate.management_id = second_management.pk

        effective = _effective_after(candidate, before, ("management",))

        self.assertEqual(effective.management_id, second_management.pk)
        self.assertEqual(effective.management.pk, second_management.pk)

    def test_a_missing_named_vrf_does_not_bind_a_global_ip_address(self):
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.renderer_writer import _manifest_binding

        device, management = make_managed("writer-missing-vrf", 16297)
        interface = Interface.objects.create(device=device, name="Loopback16297", type="virtual")
        IPAddress.objects.create(
            address="198.18.97.1/32",
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=interface.pk,
        )
        state = NSOInterfaceIPState.objects.create(
            interface=interface,
            address="198.18.97.1/32",
            vrf="missing-vrf",
            status="accepted",
        )
        state.management = management

        self.assertIsNone(_manifest_binding(state))

    def test_renderer_writer_declares_one_reference_resolver(self):
        import ast
        import inspect

        from netbox_nso_plugin.renderer_writer import RendererWriter

        renderer_writer = ast.parse(inspect.getsource(RendererWriter)).body[0]
        resolvers = [
            node
            for node in renderer_writer.body
            if isinstance(node, ast.FunctionDef) and node.name == "_resolve_reference"
        ]

        self.assertEqual(len(resolvers), 1)

    def test_route_map_consumers_ignore_undeclared_redistribution_scopes(self):
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.intent_state import footprint_for_instance
        from netbox_nso_plugin.models import NSORedistributionState

        device, management = make_managed("writer-route-map-scope", 16291)
        route_map = RouteMap.objects.create(name="RM-WRITER-SCOPE")
        NSORedistributionState.objects.create(
            management=management,
            dest_protocol="undeclared",
            source_protocol="static",
            route_map=route_map.name,
            status="accepted",
        )

        footprint = footprint_for_instance(route_map)

        self.assertNotIn((device.pk, "undeclared"), footprint.revision_keys)

    def test_stale_plan_error_is_a_renderer_protocol_error(self):
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.renderer_writer import IntentPlanStaleError

        self.assertTrue(issubclass(IntentPlanStaleError, IntentMutationProtocolError))

    def test_planned_creation_adopts_a_natural_key_race_winner(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
            renderer_writes,
        )

        device, _management = make_managed("writer-create-race", 16288)
        planned = Interface(device=device, name="Loopback1627", type="virtual")
        planned._site = device.site
        planned._location = device.location
        planned._rack = device.rack
        plan = RendererMutationPlan.build(
            saves=(planned_save(planned, force_insert=True, natural_key=("device", "name")),)
        )
        self.assertFalse(plan.changes_content)
        existing = Interface.objects.create(device=device, name="Loopback1627", type="virtual")
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes

        with mutation(plan) as writer:
            writer.save(planned, force_insert=True)

        self.assertEqual(planned.pk, existing.pk)
        self.assertEqual(Interface.objects.filter(device=device, name="Loopback1627").count(), 1)

    def test_non_route_policy_plan_has_no_visibility_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save

        _device, management = make_managed("writer-plan-queries", 16286)
        candidate = copy.copy(management)
        candidate.adapter_link_error = "planned"

        with CaptureQueriesContext(connection) as captured:
            RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("adapter_link_error",)),))

        baseline_queries = [
            query
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and 'FROM "netbox_nso_plugin_nsodevicemanagement"' in query["sql"]
        ]
        self.assertEqual(len(baseline_queries), 1)

    def test_static_route_manifest_carries_only_acknowledged_lineage(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        device, management = make_managed("writer-static-lineage", 16283)
        route = StaticRoute.objects.create(prefix="198.18.83.0/24", next_hop="198.18.0.83", metric=1)
        route.devices.add(device)
        acknowledged = {
            "vrf": "",
            "prefix": "198.18.82.0/24",
            "next_hop": "198.18.0.82",
        }
        state = NSOStaticRouteState.objects.create(
            management=management,
            static_route=route,
            status="imported",
            nso_prefix=str(route.prefix),
            nso_next_hop=str(route.next_hop),
            last_acked_triple=acknowledged,
        )
        candidate = copy.copy(state)
        candidate.status = "accepted"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("status",)),))

        with renderer_writes(plan) as writer:
            writer.save(candidate, update_fields=("status",))

        manifest = NSOOwnershipManifest.objects.get(device_id=device.pk, scope="static_route")
        assert manifest.acknowledged_lineage == [acknowledged]

    def test_one_plan_can_create_unregistered_native_rows_and_registered_overlay(self):
        from netbox_routing.models import BFDInterface, BFDProfile

        from netbox_nso_plugin.models import NSOBFDInterfaceState
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        device, management = make_managed("writer-bfd-native-create", 16281)
        interface = Interface.objects.create(device=device, name="Ethernet1/10", type="1000base-t")
        profile = BFDProfile(name="writer-bfd-profile", min_tx_int=300, min_rx_int=300, multiplier=3)
        native = BFDInterface(interface=interface, bfd_profile=profile, micro_bfd=False, enabled=True)
        state = NSOBFDInterfaceState(
            management=management,
            interface=interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="imported",
        )
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(profile, force_insert=True, natural_key=("name",)),
                planned_save(native, force_insert=True, natural_key=("interface",)),
                planned_save(state, force_insert=True, natural_key=("management", "interface")),
            )
        )

        with renderer_mirror_writes(plan) as writer:
            writer.save(profile, force_insert=True)
            writer.save(native, force_insert=True)
            writer.save(state, force_insert=True)

        assert native.bfd_profile_id == profile.pk
        assert NSOBFDInterfaceState.objects.filter(pk=state.pk).exists()

    def test_one_plan_can_delete_an_unregistered_native_row(self):
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            renderer_mirror_writes,
        )

        device, _management = make_managed("writer-bfd-native-delete", 16282)
        interface = Interface.objects.create(device=device, name="Ethernet1/11", type="1000base-t")
        native = BFDInterface.objects.create(interface=interface, enabled=True)
        plan = RendererMutationPlan.build(deletes=(planned_delete(native),))

        with renderer_mirror_writes(plan) as writer:
            writer.delete(native)

        assert not BFDInterface.objects.filter(pk=native.pk).exists()

    def test_one_plan_can_delete_a_child_before_its_set_null_parent(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            renderer_mirror_writes,
        )

        _device, management = make_managed("writer-related-delete", 16284)
        native = StaticRoute.objects.create(prefix="198.18.84.0/24", next_hop="198.18.0.84", metric=1)
        state = NSOStaticRouteState.objects.create(
            management=management,
            static_route=native,
            status="imported",
        )
        plan = RendererMutationPlan.build(
            deletes=(
                planned_delete(state),
                planned_delete(native),
            )
        )

        with renderer_mirror_writes(plan) as writer:
            writer.delete(state)
            writer.delete(native)

        assert not NSOStaticRouteState.objects.filter(pk=state.pk).exists()
        assert not StaticRoute.objects.filter(pk=native.pk).exists()

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

    def test_one_plan_can_create_a_referenced_support_row(self):
        from tenancy.models import Tenant

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        tenant = Tenant(name="Writer support tenant", slug="writer-support-tenant")
        vlan = VLAN(tenant=tenant, vid=1638, name="writer-support-vlan")
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(tenant, force_insert=True, natural_key=("slug",)),
                planned_save(vlan, force_insert=True, natural_key=("group", "vid")),
            )
        )

        with renderer_mirror_writes(plan) as writer:
            writer.save(tenant, force_insert=True)
            writer.save(vlan, force_insert=True)

        assert vlan.tenant_id == tenant.pk

    def test_referenced_support_creation_adopts_a_natural_key_race_winner(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        group = VLANGroup(name="Writer raced support group", slug="writer-raced-support-group")
        vlan = VLAN(group=group, vid=1644, name="writer-raced-support-vlan")
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(group, force_insert=True, natural_key=("slug",)),
                planned_save(vlan, force_insert=True, natural_key=("group", "vid")),
            )
        )
        winner = VLANGroup.objects.create(name=group.name, slug=group.slug)
        vlan.group = winner

        with renderer_mirror_writes(plan) as writer:
            self.assertTrue(writer.consume_existing_creation(winner))
            writer.save(vlan, force_insert=True)

        self.assertEqual(vlan.group_id, winner.pk)

    def test_plan_rejects_a_forward_creation_reference(self):
        from ipam.models import VLANGroup

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save

        group = VLANGroup(name="Writer forward group", slug="writer-forward-group")
        vlan = VLAN(group=group, vid=1640, name="writer-forward-vlan")

        with self.assertRaisesRegex(IntentMutationProtocolError, "references a row planned after it"):
            RendererMutationPlan.build(
                saves=(
                    planned_save(
                        vlan,
                        force_insert=True,
                        natural_key=("group", "vid"),
                        references=(("group", group),),
                    ),
                    planned_save(group, force_insert=True, natural_key=("slug",)),
                )
            )

    def test_plan_refuses_an_unreferenced_support_row(self):
        from tenancy.models import Tenant

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save

        tenant = Tenant(name="Writer unreferenced tenant", slug="writer-unreferenced-tenant")

        with self.assertRaisesRegex(IntentMutationProtocolError, "not a registered renderer input"):
            RendererMutationPlan.build(saves=(planned_save(tenant, force_insert=True, natural_key=("slug",)),))

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

    def test_m2m_set_refuses_a_previously_attached_unregistered_related_model(self):
        from extras.models import Tag

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_m2m_set

        device, management = make_managed("writer-unregistered-existing-m2m", 16290)
        interface = Interface.objects.create(device=device, name="Ethernet1/11", type="1000base-t")
        state = NSOSwitchportState.objects.create(
            management=management,
            interface=interface,
            mode="tagged",
            status="imported",
        )
        tag = Tag.objects.create(name="existing unregistered relation", slug="existing-unregistered-relation")
        state.tags.add(tag)

        with self.assertRaisesRegex(IntentMutationProtocolError, "unregistered.*related"):
            RendererMutationPlan.build(m2m_writes=(planned_m2m_set(state, "tags", ()),))

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
        self.assertEqual(plan.write_set[0].selected_pks, (lower.pk, higher.pk))

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
            device_id=device.pk,
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
        from netbox_nso_plugin import renderer_writer
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

        with self.assertRaisesRegex(IntentMutationProtocolError, "changed after planning") as caught:
            with renderer_mirror_writes(plan) as writer:
                writer.save(candidate, update_fields=("adapter_link_error",))

        stale_type = getattr(renderer_writer, "IntentPlanStaleError", None)
        self.assertIsNotNone(stale_type)
        self.assertIsInstance(caught.exception, stale_type)
        management.refresh_from_db()
        assert management.adapter_link_error == "concurrent"

    def test_save_rejects_a_planned_value_mutated_before_pre_save(self):
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
        )

        _device, management = make_managed("writer-pre-save-mutation", 16285)
        candidate = copy.copy(management)
        candidate.adapter_link_error = "planned"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("adapter_link_error",)),))
        model_save = candidate.save

        def mutate_before_pre_save(*args, **kwargs):
            candidate.adapter_link_error = "mutated"
            return model_save(*args, **kwargs)

        candidate.save = mutate_before_pre_save

        with self.assertRaisesRegex(IntentMutationProtocolError, "bypassed the active renderer writer"):
            with renderer_mirror_writes(plan) as writer:
                writer.save(candidate, update_fields=("adapter_link_error",))

        management.refresh_from_db()
        assert management.adapter_link_error == ""

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

    def test_delete_executes_a_complete_registered_collector_cascade(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        device, management = make_managed("writer-complete-cascade", 16287)
        route = StaticRoute.objects.create(prefix="198.18.87.0/24", next_hop="198.18.0.87", metric=1)
        state = NSOStaticRouteState.objects.create(
            management=management,
            static_route=route,
            status="accepted",
        )
        manifest = NSOOwnershipManifest.objects.create(
            device_id=device.pk,
            scope="static_route",
            native_model_label=route._meta.label_lower,
            native_key={
                "vrf_id": None,
                "prefix": str(route.prefix),
                "next_hop": str(route.next_hop),
                "interface_next_hop": None,
            },
        )
        plan = RendererMutationPlan.build(deletes=(planned_delete(management),))

        with renderer_writes(plan) as writer:
            writer.delete(management)

        assert not type(management).objects.filter(pk=management.pk).exists()
        assert not NSOStaticRouteState.objects.filter(pk=state.pk).exists()
        assert type(device).objects.filter(pk=device.pk).exists()
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "detached")

    def test_queryset_management_delete_detaches_manifest_after_renderer_locks(self):
        from django.db import connection
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.apply_state import _DEVICE_INTENT_LOCK_NAMESPACE

        device, management = make_managed("writer-queryset-delete", 16293)
        route = StaticRoute.objects.create(prefix="198.18.93.0/24", next_hop="198.18.0.93", metric=1)
        manifest = NSOOwnershipManifest.objects.create(
            device_id=device.pk,
            scope="static_route",
            native_model_label=route._meta.label_lower,
            native_key={
                "vrf_id": None,
                "prefix": str(route.prefix),
                "next_hop": str(route.next_hop),
                "interface_next_hop": None,
            },
        )
        statements = []

        def observe_sql(execute, sql, params, many, context):
            statements.append((str(sql), tuple(params or ())))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(observe_sql):
            type(management).objects.filter(pk=management.pk).delete()

        management_table = management._meta.db_table
        manifest_table = manifest._meta.db_table
        device_lock_index = next(
            index
            for index, (sql, params) in enumerate(statements)
            if "pg_advisory_xact_lock" in sql and params == (_DEVICE_INTENT_LOCK_NAMESPACE, device.pk)
        )
        management_lock_index = next(
            index
            for index, (sql, _params) in enumerate(statements)
            if f'FROM "{management_table}"' in sql and "FOR UPDATE" in sql
        )
        manifest_update_indices = [
            index
            for index, (sql, params) in enumerate(statements)
            if f'UPDATE "{manifest_table}"' in sql and "detached" in params
        ]

        self.assertEqual(len(manifest_update_indices), 1)
        manifest_update_index = manifest_update_indices[0]
        self.assertLess(device_lock_index, manifest_update_index)
        self.assertLess(management_lock_index, manifest_update_index)
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "detached")

    def _assert_device_delete_retires_manifest_after_renderer_locks(self, *, queryset):
        from django.db import connection
        from django.db.models.signals import pre_delete
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.apply_state import _DEVICE_INTENT_LOCK_NAMESPACE

        octet = 95 if queryset else 94
        device, management = make_managed(f"writer-device-delete-{octet}", 16200 + octet)
        device_id = device.pk
        route = StaticRoute.objects.create(
            prefix=f"198.18.{octet}.0/24",
            next_hop=f"198.18.0.{octet}",
            metric=1,
        )
        manifest = NSOOwnershipManifest.objects.create(
            device_id=device_id,
            scope="static_route",
            native_model_label=route._meta.label_lower,
            native_key={
                "vrf_id": None,
                "prefix": str(route.prefix),
                "next_hop": str(route.next_hop),
                "interface_next_hop": None,
            },
        )
        statements = []
        management_guard_finished = False

        def mark_management_guard_finished(sender, instance, **kwargs):
            nonlocal management_guard_finished
            management_guard_finished = True

        def observe_sql(execute, sql, params, many, context):
            statements.append((str(sql), tuple(params or ()), management_guard_finished))
            return execute(sql, params, many, context)

        dispatch_uid = f"test_device_delete_manifest_order_{octet}"
        pre_delete.connect(
            mark_management_guard_finished,
            sender=type(management),
            dispatch_uid=dispatch_uid,
            weak=False,
        )
        try:
            with connection.execute_wrapper(observe_sql):
                if queryset:
                    type(device).objects.filter(pk=device_id).delete()
                else:
                    device.delete()
        finally:
            pre_delete.disconnect(sender=type(management), dispatch_uid=dispatch_uid)

        management_table = management._meta.db_table
        manifest_table = manifest._meta.db_table
        device_lock_index = next(
            index
            for index, (sql, params, _guard_finished) in enumerate(statements)
            if "pg_advisory_xact_lock" in sql and params == (_DEVICE_INTENT_LOCK_NAMESPACE, device_id)
        )
        management_lock_index = next(
            index
            for index, (sql, _params, _guard_finished) in enumerate(statements)
            if f'FROM "{management_table}"' in sql and "FOR UPDATE" in sql
        )
        manifest_updates = [
            (index, guard_finished)
            for index, (sql, params, guard_finished) in enumerate(statements)
            if f'UPDATE "{manifest_table}"' in sql and "retired" in params
        ]

        self.assertEqual(len(manifest_updates), 1)
        manifest_update_index, guard_finished = manifest_updates[0]
        self.assertLess(device_lock_index, manifest_update_index)
        self.assertLess(management_lock_index, manifest_update_index)
        self.assertFalse(guard_finished)
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")

    def test_device_delete_retires_manifest_after_renderer_locks(self):
        self._assert_device_delete_retires_manifest_after_renderer_locks(queryset=False)

    def test_queryset_device_delete_retires_manifest_after_renderer_locks(self):
        self._assert_device_delete_retires_manifest_after_renderer_locks(queryset=True)

    def test_delete_plan_orders_each_collector_model_by_primary_key(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete

        _device, management = make_managed("writer-cascade-order", 16292)
        routes = [
            StaticRoute.objects.create(
                prefix=f"198.18.{octet}.0/24",
                next_hop="198.18.0.1",
                metric=1,
            )
            for octet in (51, 52, 53)
        ]
        states = [
            NSOStaticRouteState.objects.create(
                management=management,
                static_route=route,
                status="imported",
            )
            for route in reversed(routes)
        ]

        plan = RendererMutationPlan.build(deletes=(planned_delete(management),))
        state_pks = [
            write.pk
            for write in plan.write_set
            if write.operation == "delete" and write.model_label == states[0]._meta.label_lower
        ]

        self.assertEqual(state_pks, sorted(state_pks))

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

    def test_delete_plan_counts_an_owned_cascade_as_content(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete

        device, management = make_managed("writer-owned-cascade", 16291)
        interface = Interface.objects.create(device=device, name="Ethernet1/11", type="1000base-t")
        NSOSwitchportState.objects.create(
            management=management,
            interface=interface,
            mode="access",
            status="accepted",
        )

        plan = RendererMutationPlan.build(deletes=(planned_delete(interface),))

        self.assertTrue(plan.changes_content)
        self.assertIn((device.pk, "switchport"), plan.content_keys)

    def test_delete_authorizes_registered_collector_child_tables(self):
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_mirror_writes

        community_list = CommunityList.objects.create(name="WRITER-CASCADE-CHILD")
        community = Community.objects.create(community="64512:1627")
        entry = CommunityListEntry.objects.create(
            community_list=community_list,
            action="permit",
            community=community,
        )
        plan = RendererMutationPlan.build(deletes=(planned_delete(community_list),))

        with renderer_mirror_writes(plan) as writer:
            writer.delete(community_list)

        assert not CommunityList.objects.filter(pk=community_list.pk).exists()
        assert not CommunityListEntry.objects.filter(pk=entry.pk).exists()

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
