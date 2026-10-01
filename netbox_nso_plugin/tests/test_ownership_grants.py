# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Ownership can start only at a classified explicit operation."""

import copy
import threading

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from ipam.models import IPAddress

from netbox_nso_plugin import status_machine as sm
from netbox_nso_plugin.intent_state import IntentMutationProtocolError
from netbox_nso_plugin.models import NSOInterfaceIPState, NSOInterfaceState, NSOOwnershipManifest
from netbox_nso_plugin.renderer_writer import (
    RendererMutationPlan,
    planned_save,
    planned_set_update,
    renderer_mirror_writes,
    renderer_writes,
)

from ._outbox_case import make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestOwnershipGrants(IntentPushResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("explicit-acquisition", 17581)
        self.management.manage_description = True
        self.management.save(update_fields=("manage_description",))
        self.interface = Interface.objects.create(device=self.device, name="Loopback1", type="virtual")
        self.user = get_user_model().objects.create_user(username="acquisition-operator", is_superuser=True)
        self.client.force_login(self.user)

    def state(self, status="imported"):
        return NSOInterfaceState.objects.create(interface=self.interface, attribute="description", status=status)

    def test_status_update_accepts_a_nullable_related_filter(self):
        from netbox_nso_plugin.models import NSORedistributionState

        state = NSORedistributionState.objects.create(
            management=self.management,
            dest_protocol="isis",
            source_protocol="static",
            status="unknown",
        )

        changed = NSORedistributionState.objects.filter(pk=state.pk, redistribution__metric__isnull=True).update(
            status="imported"
        )

        self.assertEqual(changed, 1)
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")

    def test_writer_saves_non_overlays_without_acquisition_evidence(self):
        from django.db import connection

        from netbox_nso_plugin.models import NSOOwnershipAcquisition

        def reject_acquisition_query(execute, sql, params, many, context):
            self.assertNotIn(NSOOwnershipAcquisition._meta.db_table, sql)
            return execute(sql, params, many, context)

        self.management.manage_description = False
        self.interface.description = "writer description"
        saves = (
            planned_save(self.management, update_fields=("manage_description",)),
            planned_save(self.interface, update_fields=("description",)),
        )
        with connection.execute_wrapper(reject_acquisition_query):
            plan = RendererMutationPlan.build(saves=saves)
            mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
            with without_commit_drain(), mutation(plan) as writer:
                writer.save(self.management, update_fields=("manage_description",))
                writer.save(self.interface, update_fields=("description",))

        self.management.refresh_from_db()
        self.interface.refresh_from_db()
        self.assertFalse(self.management.manage_description)
        self.assertEqual(self.interface.description, "writer description")

    def assert_grant(self, row, kind):
        from netbox_nso_plugin.ownership_planner import manifest_binding

        row.refresh_from_db()
        self.assertIn(row.status, sm.OWNED_STATES)
        binding = manifest_binding(row)
        self.assertIsNotNone(binding)
        _rule, scope, device_id, native_model_label, _native_id, native_key, state_model_label, state_key = binding
        manifest = NSOOwnershipManifest.objects.get(
            device_id=device_id,
            scope=scope,
            native_model_label=native_model_label,
            native_key=native_key,
            state_model_label=state_model_label,
            state_key=state_key,
        )
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertEqual(manifest.grant_kind, kind)
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        with without_commit_drain():
            self.assertEqual(reconcile_scope_ownership(device_id, (scope,)), (), self._testMethodName)
        row.refresh_from_db()
        manifest.refresh_from_db()
        self.assertIn(row.status, sm.OWNED_STATES)
        self.assertEqual(manifest.ownership_state, "owned")

    def test_unclassified_owned_insert_is_refused(self):
        for status in sm.OWNED_STATES:
            with self.subTest(status=status), transaction.atomic():
                with self.assertRaisesRegex(IntentMutationProtocolError, f"nsointerfacestate.*{status}"):
                    self.state(status)

    def test_unclassified_owned_update_without_writer_is_refused(self):
        state = self.state()
        state.status = "accepted"
        with self.assertRaisesRegex(IntentMutationProtocolError, f"{state.pk}.*accepted"):
            state.save(update_fields=("status",))
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")

    def test_status_expression_cannot_bypass_the_model_guard(self):
        from django.db.models import Value

        with self.assertRaises(IntentMutationProtocolError):
            self.state(Value("accepted"))

    def test_bulk_update_cannot_bypass_the_guard(self):
        state = self.state()
        state.status = "accepted"
        with self.assertRaises(IntentMutationProtocolError), transaction.atomic():
            type(state).objects.bulk_update([state], ("status",))
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")

    def test_manifest_reown_refuses_another_overlay_identity(self):
        from netbox_nso_plugin.ownership_grants import OwnershipGrant

        self.state()
        with without_commit_drain():
            self.client.post(reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=(self.device.pk,)))
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="interface")
        candidate = NSOInterfaceState.objects.create(interface=self.interface, attribute="enabled", status="imported")
        candidate.status = "accepted"
        with self.assertRaises(IntentMutationProtocolError):
            RendererMutationPlan.build(
                saves=(planned_save(candidate, update_fields=("status",)),),
                grant=OwnershipGrant("manifest_reown", manifest_pk=manifest.pk),
            )

    def test_manifest_reown_refuses_a_detached_or_retired_manifest(self):
        from netbox_nso_plugin.ownership_grants import OwnershipGrant

        state = self.state()
        with without_commit_drain():
            self.client.post(reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=(self.device.pk,)))
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="interface")
        type(state).objects.filter(pk=state.pk).update(status="imported")
        state.refresh_from_db()
        state.status = "accepted"
        for ownership_state in ("detached", "retired"):
            NSOOwnershipManifest.objects.filter(pk=manifest.pk).update(ownership_state=ownership_state)
            with self.subTest(ownership_state=ownership_state), self.assertRaises(IntentMutationProtocolError):
                RendererMutationPlan.build(
                    saves=(planned_save(state, update_fields=("status",)),),
                    grant=OwnershipGrant("manifest_reown", manifest_pk=manifest.pk),
                )

    def test_writer_without_grant_cannot_acquire(self):
        state = self.state()
        state.status = "accepted"
        with self.assertRaises(IntentMutationProtocolError):
            plan = RendererMutationPlan.build(saves=(planned_save(state, update_fields=("status",)),))
            mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
            with mutation(plan) as writer:
                writer.save(state, update_fields=("status",))

    def test_active_writer_without_grant_cannot_acquire(self):
        from netbox_nso_plugin.ownership_grants import OwnershipGrant

        state = self.state()
        state.status = "accepted"
        plan = RendererMutationPlan.build(
            saves=(planned_save(state, update_fields=("status",)),), grant=OwnershipGrant("accept")
        )
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with self.assertRaises(IntentMutationProtocolError), mutation(plan) as writer:
            writer.grant = None
            writer.save(state, update_fields=("status",))

    def test_queryset_owned_update_without_writer_is_refused(self):
        state = self.state()
        with self.assertRaises(IntentMutationProtocolError):
            NSOInterfaceState.objects.filter(pk=state.pk).update(status="accepted")
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")

    def test_status_update_excludes_rows_inserted_after_selection(self):
        from django.db import connection

        from ._ownership_case import acquire_overlay

        owned = acquire_overlay(NSOInterfaceState, interface=self.interface, attribute="description", status="accepted")
        inserted = []

        def insert_after_selection(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "FOR UPDATE" in sql and NSOInterfaceState._meta.db_table in sql and not inserted:
                inserted.append(True)
                inserted[0] = self.state_for_enabled()
            return result

        with connection.execute_wrapper(insert_after_selection):
            changed = NSOInterfaceState.objects.filter(interface=self.interface).update(status="deploying")

        self.assertEqual(changed, 1)
        owned.refresh_from_db()
        inserted[0].refresh_from_db()
        self.assertEqual(owned.status, "deploying")
        self.assertEqual(inserted[0].status, "imported")

    def state_for_enabled(self):
        return NSOInterfaceState.objects.create(interface=self.interface, attribute="enabled", status="imported")

    def test_empty_status_selection_does_not_update_a_later_insert(self):
        from django.db import connection

        inserted = []

        def insert_after_selection(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "FOR UPDATE" in sql and NSOInterfaceState._meta.db_table in sql and not inserted:
                inserted.append(True)
                inserted[0] = self.state()
            return result

        with connection.execute_wrapper(insert_after_selection):
            changed = NSOInterfaceState.objects.filter(interface=self.interface).update(status="accepted")

        self.assertEqual(changed, 0)
        inserted[0].refresh_from_db()
        self.assertEqual(inserted[0].status, "imported")

    def test_planned_set_update_without_grant_is_refused(self):
        state = self.state()
        with self.assertRaises(IntentMutationProtocolError):
            RendererMutationPlan.build(
                set_updates=(planned_set_update(NSOInterfaceState.objects.filter(pk=state.pk), status="accepted"),)
            )

    def test_exact_set_acquisition_records_the_grant(self):
        from django.utils import timezone

        from netbox_nso_plugin.ownership_grants import OwnershipGrant

        state = self.state()
        values = {"status": "accepted", "accepted_at": timezone.now()}
        plan = RendererMutationPlan.build(
            set_updates=(planned_set_update(NSOInterfaceState.objects.filter(pk=state.pk), **values),),
            grant=OwnershipGrant("accept"),
        )
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with without_commit_drain(), mutation(plan) as writer:
            writer.set_update(NSOInterfaceState, plan.write_set[0], **values)
        self.assert_grant(state, "accept")

    def test_owned_bulk_insert_is_refused(self):
        from django.apps import apps

        from netbox_nso_plugin.intent_state import OVERLAY_MODEL_RANKS

        for label in OVERLAY_MODEL_RANKS:
            model = apps.get_model(label)
            with self.subTest(model=label), self.assertRaises(IntentMutationProtocolError):
                model.objects.bulk_create([model(status="accepted")])

    def pending_ip_acquisition(self):
        from ._ownership_case import acquire_overlay

        return acquire_overlay(
            NSOInterfaceIPState, interface=self.interface, address="198.18.0.20/32", status="accepted"
        )

    def acquisition_evidence(self, state):
        from netbox_nso_plugin.models import NSOOwnershipAcquisition

        return NSOOwnershipAcquisition.objects.filter(state_model_label=state._meta.label_lower, state_id=state.pk)

    def test_delayed_manifest_consumes_acquisition_evidence(self):
        from netbox_nso_plugin.ownership_planner import maintain_manifest

        state = self.pending_ip_acquisition()
        self.assertEqual(self.acquisition_evidence(state).get().grant_kind, "create")
        IPAddress.objects.create(address=state.address, assigned_object=self.interface)

        maintain_manifest(state)

        self.assert_grant(state, "create")
        self.assertFalse(self.acquisition_evidence(state).exists())
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="ip").delete()
        with self.assertRaises(IntentMutationProtocolError):
            maintain_manifest(state)

    def test_owned_native_rebind_preserves_the_consumed_grant(self):
        from ipam.models import VLAN, VLANGroup

        from ._outbox_case import own_vlan

        state = own_vlan(self.management, 103, "owned-rebind")
        original = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        target = VLAN.objects.create(
            group=VLANGroup.objects.create(name="Rebind target", slug="rebind-target"), vid=103, name="rebind-target"
        )
        self.assertFalse(self.acquisition_evidence(state).exists())
        candidate = copy.copy(state)
        candidate.vlan = target
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("vlan",)),))

        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.save(candidate, update_fields=("vlan",))

        self.assert_grant(state, "create")
        self.assertFalse(self.acquisition_evidence(state).exists())
        original.refresh_from_db()
        self.assertEqual(original.ownership_state, "retired")

    def test_explicit_accept_restores_missing_manifest_with_fresh_evidence(self):
        from ._ownership_case import acquire_overlay

        state = acquire_overlay(NSOInterfaceState, interface=self.interface, attribute="description", status="accepted")
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="interface").delete()
        self.assertFalse(self.acquisition_evidence(state).exists())

        with without_commit_drain():
            response = self.client.post(reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", args=(state.pk,)))

        self.assertEqual(response.status_code, 302)
        self.assert_grant(state, "accept")
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_explicit_accept_refreshes_owned_and_ended_exact_manifests(self):
        from ._ownership_case import acquire_overlay

        state = acquire_overlay(NSOInterfaceState, interface=self.interface, attribute="description", status="accepted")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="interface")
        self.assertEqual(manifest.grant_kind, "create")
        for ownership_state in ("owned", "detached", "retired"):
            with self.subTest(ownership_state=ownership_state):
                NSOOwnershipManifest.objects.filter(pk=manifest.pk).update(ownership_state=ownership_state)

                with without_commit_drain():
                    response = self.client.post(
                        reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", args=(state.pk,))
                    )

                self.assertEqual(response.status_code, 302)
                self.assert_grant(state, "accept")
                manifest.refresh_from_db()
                self.assertEqual(manifest.ownership_state, "owned")
                self.assertFalse(self.acquisition_evidence(state).exists())

    def test_owned_set_rebind_preserves_the_consumed_grant(self):
        from ipam.models import VLAN, VLANGroup

        from ._outbox_case import own_vlan

        state = own_vlan(self.management, 105, "owned-set-rebind")
        original = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        target = VLAN.objects.create(
            group=VLANGroup.objects.create(name="Set rebind target", slug="set-rebind-target"),
            vid=105,
            name="set-rebind-target",
        )
        plan = RendererMutationPlan.build(
            set_updates=(planned_set_update(type(state).objects.filter(pk=state.pk), vlan_id=target.pk),)
        )

        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.set_update(type(state), plan.write_set[0], vlan_id=target.pk)

        self.assert_grant(state, "create")
        self.assertFalse(self.acquisition_evidence(state).exists())
        original.refresh_from_db()
        self.assertEqual(original.ownership_state, "retired")

    def test_owned_identity_restoration_uses_the_continuing_episode(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        from ._ownership_case import acquire_overlay

        state = acquire_overlay(
            NSOLoggingHostState, management=self.management, address="198.18.0.30", status="accepted"
        )
        for address in ("198.18.0.31", "198.18.0.30"):
            candidate = copy.copy(state)
            candidate.address = address
            plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("address",)),))

            with without_commit_drain(), renderer_writes(plan) as writer:
                writer.save(candidate, update_fields=("address",))

            self.assert_grant(state, "create")
            self.assertFalse(self.acquisition_evidence(state).exists())
            self.assertEqual(
                NSOOwnershipManifest.objects.filter(
                    device_id=self.device.pk, scope="logging", ownership_state="owned"
                ).count(),
                1,
            )

    def test_ended_manifest_cannot_authorize_a_native_rebind(self):
        from ipam.models import VLAN, VLANGroup

        from ._outbox_case import own_vlan

        state = own_vlan(self.management, 104, "ended-rebind")
        original_vlan_id = state.vlan_id
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        NSOOwnershipManifest.objects.filter(pk=manifest.pk).update(ownership_state="retired")
        target = VLAN.objects.create(
            group=VLANGroup.objects.create(name="Ended rebind target", slug="ended-rebind-target"),
            vid=104,
            name="ended-rebind-target",
        )
        candidate = copy.copy(state)
        candidate.vlan = target
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("vlan",)),))

        with self.assertRaisesRegex(IntentMutationProtocolError, "no acquisition grant"):
            with without_commit_drain(), renderer_writes(plan) as writer:
                writer.save(candidate, update_fields=("vlan",))

        state.refresh_from_db()
        manifest.refresh_from_db()
        self.assertEqual(state.vlan_id, original_vlan_id)
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_release_discards_unbound_acquisition_evidence(self):
        state = self.pending_ip_acquisition()
        state.status = "imported"
        state.save(update_fields=("status",))
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_queryset_release_discards_unbound_acquisition_evidence(self):
        state = self.pending_ip_acquisition()
        type(state).objects.filter(pk=state.pk).update(status="imported")
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_overlay_delete_discards_unbound_acquisition_evidence(self):
        state = self.pending_ip_acquisition()
        type(state).objects.filter(pk=state.pk).delete()
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_native_cascade_discards_unbound_acquisition_evidence(self):
        state = self.pending_ip_acquisition()
        Interface.objects.filter(pk=self.interface.pk).delete()
        self.assertFalse(type(state).objects.filter(pk=state.pk).exists())
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_management_teardown_discards_evidence_for_surviving_interface_overlays(self):
        state = self.pending_ip_acquisition()
        type(self.management).objects.filter(pk=self.management.pk).delete()
        self.assertTrue(type(state).objects.filter(pk=state.pk).exists())
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_device_manifest_termination_discards_unbound_evidence(self):
        from netbox_nso_plugin.ownership_planner import detach_device_manifests, retire_device_manifests

        for terminate in (detach_device_manifests, retire_device_manifests):
            with self.subTest(terminate=terminate.__name__), transaction.atomic():
                state = self.pending_ip_acquisition()
                terminate(self.device.pk)
                self.assertFalse(self.acquisition_evidence(state).exists())
                type(state).objects.filter(pk=state.pk).delete()

    def test_overlay_retirement_discards_unbound_evidence(self):
        from netbox_nso_plugin.ownership_planner import retire_overlay_manifest

        state = self.pending_ip_acquisition()
        retire_overlay_manifest(state)
        self.assertFalse(self.acquisition_evidence(state).exists())

    def test_released_evidence_cannot_restore_a_later_owned_episode(self):
        from netbox_nso_plugin.ownership_planner import maintain_manifest

        from ._ownership_case import save_overlay_fixture

        state = self.pending_ip_acquisition()
        type(state).objects.filter(pk=state.pk).update(status="imported")
        self.assertFalse(self.acquisition_evidence(state).exists())
        state.refresh_from_db()
        state.status = "accepted"
        save_overlay_fixture(state, update_fields=("status",))
        IPAddress.objects.create(address=state.address, assigned_object=self.interface)
        maintain_manifest(state)
        self.assertFalse(self.acquisition_evidence(state).exists())
        NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="ip").delete()
        with self.assertRaises(IntentMutationProtocolError):
            maintain_manifest(state)

    def test_acquisition_provenance_rolls_back_with_the_overlay(self):
        from netbox_nso_plugin.models import NSOOwnershipAcquisition

        with self.assertRaisesRegex(RuntimeError, "abort acquisition"), transaction.atomic():
            state = self.pending_ip_acquisition()
            evidence = NSOOwnershipAcquisition.objects.get(state_model_label=state._meta.label_lower, state_id=state.pk)
            self.assertEqual(evidence.grant_kind, "create")
            raise RuntimeError("abort acquisition")

        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.interface).exists())
        self.assertFalse(
            NSOOwnershipAcquisition.objects.filter(
                state_model_label=state._meta.label_lower, state_id=state.pk
            ).exists()
        )
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk).exists())

    def test_base_manager_cannot_bypass_acquisition(self):
        state = self.state()
        with self.assertRaises(IntentMutationProtocolError):
            type(state)._base_manager.filter(pk=state.pk).update(status="accepted")

    def test_bulk_interface_accept_records_grant(self):
        state = self.state()
        with without_commit_drain():
            response = self.client.post(reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=(self.device.pk,)))
        self.assertEqual(response.status_code, 302)
        self.assert_grant(state, "accept")

    def test_owned_repend_needs_no_new_grant(self):
        state = self.state()
        with without_commit_drain():
            self.client.post(reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=(self.device.pk,)))
        state.refresh_from_db()
        candidate = copy.copy(state)
        candidate.status = "accepted"
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("status",)),))
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with without_commit_drain(), mutation(plan) as writer:
            writer.save(candidate, update_fields=("status",))
        self.assert_grant(state, "accept")

    def test_owned_repend_without_writer_preserves_the_grant(self):
        state = self.state()
        with without_commit_drain():
            self.client.post(reverse("plugins:netbox_nso_plugin:device_bulk_accept", args=(self.device.pk,)))
        state.refresh_from_db()
        state.status = "accepted"
        state.save(update_fields=("status",))
        self.assert_grant(state, "accept")

    def test_rest_owned_status_is_refused_on_create_and_update(self):
        url = reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-list")
        state = self.state()
        for status in sm.OWNED_STATES:
            with self.subTest(status=status):
                response = self.client.post(
                    url, {"interface": self.interface.pk, "attribute": "enabled", "status": status}
                )
                self.assertEqual(response.status_code, 400)
                response = self.client.patch(f"{url}{state.pk}/", {"status": status}, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")

    def test_manifest_requires_grant_kind_without_default(self):
        from netbox_nso_plugin.ownership_grants import GRANTS

        field = NSOOwnershipManifest._meta.get_field("grant_kind")
        self.assertFalse(field.null)
        self.assertFalse(field.has_default())
        self.assertEqual({kind for kind, _label in field.choices}, GRANTS)

    def test_guard_covers_rule_table_and_peer_templates(self):
        from netbox_nso_plugin.intent_state import OVERLAY_MODEL_RANKS
        from netbox_nso_plugin.ownership_planner import converted_scope_rules

        table = {label for rule in converted_scope_rules().values() for label in rule.overlay_model_labels}
        self.assertEqual(set(OVERLAY_MODEL_RANKS), table | {"netbox_nso_plugin.nsobgppeertemplatestate"})

    def test_ip_intent_accept_preserves_native_address(self):
        native = IPAddress.objects.create(address="198.18.0.1/24", assigned_object=self.interface)
        state = NSOInterfaceIPState.objects.create(
            interface=self.interface, address=str(native.address), status="imported"
        )
        with without_commit_drain():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_accept", args=(state.pk,))
            )
        self.assertEqual(response.status_code, 302)
        native.refresh_from_db()
        self.assertEqual(str(native.address), "198.18.0.1/24")
        self.assert_grant(state, "accept")

    def test_create_view_records_grant(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOVLANState

        group = VLANGroup.objects.create(name="Explicit VLANs", slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.create(group=group, vid=101, name="explicit-vlan")
        with without_commit_drain():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:vlan_attach", args=(self.device.pk,)), {"vlan": vlan.pk}
            )
        self.assertEqual(response.status_code, 302)
        self.assert_grant(NSOVLANState.objects.get(management=self.management, vlan=vlan), "create")

    def test_inline_edit_records_grant(self):
        state = self.state()
        with without_commit_drain():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:nsointerfacestate_edit_field", args=(state.pk,)),
                {"value": "Explicit uplink"},
            )
        self.assertEqual(response.status_code, 200)
        self.assert_grant(state, "operator_edit")

    def test_overlay_only_inline_edit_records_grant(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        state = NSOLoggingHostState.objects.create(
            management=self.management, address="198.18.2.1", status="imported", severity="informational"
        )
        with without_commit_drain():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "logging_host", "pk": state.pk}),
                {"severity": "warning"},
            )
        self.assertEqual(response.status_code, 200)
        self.assert_grant(state, "operator_edit")

    def test_snmp_form_records_grant(self):
        from netbox_nso_plugin.forms import NSOSnmpSystemInfoStateForm
        from netbox_nso_plugin.models import NSOSnmpSystemInfoState

        state = NSOSnmpSystemInfoState.objects.create(management=self.management, status="imported", location="old")
        form = NSOSnmpSystemInfoStateForm(data={"location": "lab", "contact": "operator"}, instance=state)
        self.assertTrue(form.is_valid(), form.errors)
        with without_commit_drain():
            form.save()
        self.assert_grant(state, "operator_edit")

    def test_single_address_allocation_records_grant(self):
        from ipam.models import Prefix

        from netbox_nso_plugin.ip_autoassign import assign_ips_for_role
        from netbox_nso_plugin.models import NSOLinkRole

        pool = Prefix.objects.create(prefix="198.18.0.0/24")
        role = NSOLinkRole.objects.create(
            name="Explicit allocation",
            slug="explicit-allocation",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_prefix=pool,
        )
        with without_commit_drain():
            result = assign_ips_for_role(self.interface, role)
        self.assertFalse(result["errors"], result)
        self.assertEqual(len(result["allocated"]), 1, result)
        self.assert_grant(NSOInterfaceIPState.objects.get(pk=result["allocated"][0]["state_id"]), "autoassign")

    def test_point_to_point_allocation_records_both_grants(self):
        from ipam.models import Prefix

        from netbox_nso_plugin.ip_autoassign import assign_ips_for_role
        from netbox_nso_plugin.models import NSOLinkRole

        peer_device, _peer_management = make_managed("explicit-peer", 17582)
        peer = Interface.objects.create(device=peer_device, name="Loopback1", type="virtual")
        pool = Prefix.objects.create(prefix="198.18.1.0/24")
        role = NSOLinkRole.objects.create(
            name="Explicit pair", slug="explicit-pair", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        with without_commit_drain():
            result = assign_ips_for_role(self.interface, role, other_end=peer)
        self.assertFalse(result["errors"], result)
        rows = NSOInterfaceIPState.objects.filter(interface__in=(self.interface, peer))
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assert_grant(row, "autoassign")

    def test_link_role_provision_records_grant(self):
        from netbox_nso_plugin.link_role import provision_link_role
        from netbox_nso_plugin.models import NSOLinkRole, NSOLinkRoleAssignment

        role = NSOLinkRole.objects.create(
            name="Explicit description",
            slug="explicit-description",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            description_template="{self_host} uplink",
        )
        NSOLinkRoleAssignment.objects.create(role=role, interface=self.interface)
        with without_commit_drain():
            result = provision_link_role(self.interface)
        self.assertFalse(result["errors"], result)
        self.assert_grant(NSOInterfaceState.objects.get(interface=self.interface, attribute="description"), "link_role")

    def test_vlan_rescope_grants_unowned_survivor(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOVLANState

        source_group = VLANGroup.objects.create(name="Explicit source", slug=f"nso-{self.device.pk}")
        target_group = VLANGroup.objects.create(name="Explicit target", slug="explicit-target")
        source = VLAN.objects.create(group=source_group, vid=102, name="source")
        target = VLAN.objects.create(group=target_group, vid=102, name="target")
        survivor = NSOVLANState.objects.create(management=self.management, vlan=target, status="imported")
        with without_commit_drain():
            self.client.post(
                reverse("plugins:netbox_nso_plugin:vlan_attach", args=(self.device.pk,)), {"vlan": source.pk}
            )
            state = NSOVLANState.objects.get(management=self.management, vlan=source)
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:vlan_rescope", args=(state.pk,)), {"group": target_group.pk}
            )
        self.assertEqual(response.status_code, 302)
        self.assert_grant(survivor, "operator_edit")


class TestOwnershipReleaseConcurrency(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def test_release_preserves_concurrent_link_role_reacquisition_until_native_binding(self):
        from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

        from netbox_nso_plugin.intent_state import _discard_released_acquisition
        from netbox_nso_plugin.link_role import enable_igp_for_role
        from netbox_nso_plugin.models import NSOLinkRole, NSOOSPFInterfaceState, NSOOwnershipAcquisition
        from netbox_nso_plugin.ownership_planner import maintain_manifest

        device, management = make_managed("release-reacquisition", 17583)
        interface = Interface.objects.create(device=device, name="Loopback1", type="virtual")
        role = NSOLinkRole.objects.create(
            name="Release OSPF",
            slug="release-ospf",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="ospf",
            ospf_process_id="1",
            ospf_area="0",
        )
        with without_commit_drain():
            self.assertTrue(enable_igp_for_role(interface, role, push=False, mgmt=management)["enabled"])
        state = NSOOSPFInterfaceState.objects.get(management=management, interface=interface)
        evidence_rows = NSOOwnershipAcquisition.objects.filter(
            state_model_label=state._meta.label_lower, state_id=state.pk
        )
        released_evidence_pk = evidence_rows.get().pk
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=device.pk, scope="ospf").exists())
        release_written = threading.Event()
        peer_observed = threading.Event()
        release_finished = threading.Event()
        peer_finished = threading.Event()
        observed_statuses = []
        errors = []

        def reacquire():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '30s'")
                self.assertTrue(release_written.wait(30), "release did not reach its write")
                observed_statuses.append(NSOOSPFInterfaceState.objects.get(pk=state.pk).status)
                peer_observed.set()
                if observed_statuses[0] in sm.OWNED_STATES:
                    self.assertTrue(release_finished.wait(30), "release did not finish")
                result = enable_igp_for_role(
                    Interface.objects.get(pk=interface.pk), NSOLinkRole.objects.get(pk=role.pk), push=False
                )
                self.assertTrue(result["enabled"], result)
            except BaseException as exc:
                errors.append(exc)
            finally:
                peer_observed.set()
                peer_finished.set()
                connection.close()

        def pause_after_release_write(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if (
                not release_written.is_set()
                and sql.lstrip().upper().startswith("UPDATE")
                and state._meta.db_table in sql
            ):
                release_written.set()
                self.assertTrue(peer_observed.wait(30), "peer did not observe the release")
                if observed_statuses and observed_statuses[0] not in sm.OWNED_STATES:
                    self.assertTrue(peer_finished.wait(30), "peer did not reacquire the released overlay")
            return result

        peer = threading.Thread(target=reacquire)
        with without_commit_drain():
            peer.start()
            try:
                state.status = "imported"
                with connection.execute_wrapper(pause_after_release_write):
                    state.save(update_fields=("status",))
            finally:
                release_finished.set()
                peer.join(timeout=60)
        self.assertFalse(peer.is_alive(), "reacquisition did not finish")
        self.assertEqual(errors, [])
        self.assertTrue(release_written.is_set())
        fresh_evidence = evidence_rows.get()
        self.assertNotEqual(fresh_evidence.pk, released_evidence_pk)
        self.assertEqual(fresh_evidence.grant_kind, "link_role")

        _discard_released_acquisition(type(state), state, update_fields=("status",), using=state._state.db)

        self.assertTrue(evidence_rows.filter(pk=fresh_evidence.pk).exists())
        state.refresh_from_db()
        native = OSPFInterface.objects.create(
            interface=interface,
            instance=OSPFInstance.objects.create(device=device, name="1", process_id="1", router_id="198.18.0.1"),
            area=OSPFArea.objects.create(area_id="0", area_type="standard"),
        )

        maintain_manifest(state)

        manifest = NSOOwnershipManifest.objects.get(device_id=device.pk, scope="ospf", native_id=native.pk)
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertEqual(manifest.grant_kind, "link_role")
        self.assertFalse(evidence_rows.exists())
        self.assertEqual(observed_statuses, ["accepted"])


class TestOwnershipStatusUpdateLocks(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def test_status_update_locks_the_overlay_without_locking_joined_native_rows(self):
        from dcim.models import Device
        from django.db import DatabaseError

        device, _management = make_managed("status-update-locks", 17584)
        interface = Interface.objects.create(device=device, name="Loopback1", type="virtual")
        state = NSOInterfaceState.objects.create(interface=interface, attribute="description", status="unknown")
        outcomes = []
        errors = []

        def probe_locks():
            try:
                for label, model, pk in (
                    ("interface", Interface, interface.pk),
                    ("device", Device, device.pk),
                    ("overlay", NSOInterfaceState, state.pk),
                ):
                    try:
                        with transaction.atomic():
                            model.objects.select_for_update(of=("self",), nowait=True).get(pk=pk)
                    except DatabaseError as exc:
                        outcomes.append((label, getattr(exc.__cause__, "sqlstate", None)))
                    else:
                        outcomes.append((label, None))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        peer = threading.Thread(target=probe_locks)
        with transaction.atomic():
            changed = NSOInterfaceState.objects.filter(pk=state.pk, interface__device__name=device.name).update(
                status="imported"
            )
            peer.start()
            peer.join(timeout=30)
            self.assertFalse(peer.is_alive(), "lock probe did not finish")
            self.assertEqual(errors, [])
            self.assertEqual(outcomes, [("interface", None), ("device", None), ("overlay", "55P03")])

        self.assertEqual(changed, 1)
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")
