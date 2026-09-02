# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for redistribution_reconciler.reconcile_redistribution create-side.

Verifies it CREATES (not just links) netbox_routing.Redistribution with the
destination scope resolved + route-map linked. Uses an IS-IS destination
(ISISInstance) since that needs no BGP object graph.
"""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.test import TestCase


class TestReconcileRedistribution(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RdMfg", slug="rdmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RdDev", slug="rddev")
        role = DeviceRole.objects.create(name="RdRole", slug="rdrole")
        site = Site.objects.create(name="RdSite", slug="rdsite")
        cls.device = Device.objects.create(name="rd-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="rd-inst", defaults={"adapter_instance_id": "rd-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "rd-dev", "adapter_device_id": self.device.pk},
        )[0]

    def _entry(self, **kw):
        e = {
            "dest_protocol": "isis",
            "dest_ref": "",
            "source_protocol": "static",
            "source_ref": "",
            "route_map": "",
            "metric": None,
            "metric_type": "",
        }
        e.update(kw)
        return e

    def _set_ned(self, ned_id: str, slug: str):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        platform = Platform.objects.create(name=slug, slug=slug)
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id=ned_id)
        self.device.platform = platform
        self.device.save(update_fields=["platform"])

    def test_creates_redistribution_for_isis_dest(self):
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution, RouteMap

        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        rm = RouteMap.objects.create(name="RM-REDIST")

        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        states = reconcile_redistribution(
            self.device,
            {"entries": [self._entry(route_map="RM-REDIST", metric=10, metric_type="external")]},
        )

        self.assertEqual(len(states), 1)
        s = states[0]
        self.assertTrue(s.redistribution_id is not None)
        self.assertEqual(s.status, "imported")  # unowned, materialized → imported (unified)

        r = Redistribution.objects.get(source_protocol="static")
        self.assertEqual(r.destination, inst)
        self.assertEqual(r.route_map_id, rm.pk)
        self.assertEqual(r.metric, 10)
        self.assertEqual(r.metric_type, "external")

    def test_owned_state_recreates_a_missing_native_redistribution(self):
        from django.db import transaction
        from django.utils import timezone
        from netbox_routing.models import ISISInstance, Redistribution

        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution
        from netbox_nso_plugin.signals import suppress_intent_push

        self._make_mgmt()
        ISISInstance.objects.create(device=self.device, process_tag="")
        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        state = NSORedistributionState.objects.get()
        state.status = "accepted"
        state.accepted_at = timezone.now()
        state.save(update_fields=["status", "accepted_at"])

        with transaction.atomic(), offline_mutation(), suppress_intent_push():
            state.redistribution.delete()
        state.refresh_from_db()
        self.assertIsNone(state.redistribution_id)

        reconciled = reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})[0]

        self.assertIsNotNone(reconciled.redistribution_id)
        self.assertEqual(Redistribution.objects.count(), 1)

    def test_category_reconcile_declares_its_native_and_overlay_writes(self):
        management = self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_category

        payload = {"entries": [self._entry(metric=10)]}
        with (
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch("netbox_nso_plugin.adapter_client.get_redistribution", return_value=payload),
        ):
            result = reconcile_category(self.device, management, "redistribution")

        self.assertEqual(result["redistribution_states"][0].status, "imported")
        self.assertEqual(NSORedistributionState.objects.filter(management=management).count(), 1)
        self.assertEqual(Redistribution.objects.filter(source_protocol="static").count(), 1)

    def test_preflight_plan_freezes_native_and_overlay_creates(self):
        self._make_mgmt()
        from netbox_routing.models import ISISInstance

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import redistribution_reconcile_plan

        plan = redistribution_reconcile_plan(self.device, {"entries": [self._entry(metric=10)]})

        assert [(write.operation, write.model_label) for write in plan.write_set] == [
            ("save", "netbox_routing.redistribution"),
            ("save", "netbox_nso_plugin.nsoredistributionstate"),
        ]
        assert plan.content_keys == ()

    def test_foreign_overlay_save_is_neutral(self):
        mgmt = self._make_mgmt()
        from netbox_nso_plugin.models import NSORedistributionState

        with patch("netbox_nso_plugin.signals._schedule_redistribution_push") as schedule:
            NSORedistributionState.objects.create(
                management=mgmt,
                dest_protocol="isis",
                source_protocol="static",
                status="accepted",
            )

        schedule.assert_not_called()

    def test_foreign_native_save_is_neutral(self):
        mgmt = self._make_mgmt()
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ISISInstance, Redistribution

        destination = ISISInstance.objects.create(device=self.device, process_tag="")
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(destination),
            destination_id=destination.pk,
            source_protocol="static",
        )
        from netbox_nso_plugin.models import NSORedistributionState

        NSORedistributionState.objects.create(
            management=mgmt,
            dest_protocol="isis",
            source_protocol="static",
            redistribution=native,
            status="accepted",
        )

        with patch("netbox_nso_plugin.signals._schedule_redistribution_push") as schedule:
            native.metric = 20
            native.save(update_fields=("metric",))

        schedule.assert_not_called()

    def test_missing_destination_stays_imported(self):
        """No matching ISISInstance → no Redistribution created, status=imported."""
        self._make_mgmt()
        from netbox_routing.models import Redistribution

        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        states = reconcile_redistribution(self.device, {"entries": [self._entry()]})
        self.assertEqual(len(states), 1)
        self.assertIsNone(states[0].redistribution_id)
        self.assertEqual(states[0].status, "imported")
        self.assertEqual(Redistribution.objects.count(), 0)

    def test_missing_destination_cannot_settle_owned_drift(self):
        """An unresolved destination cannot hide drift in the owned overlay."""
        self._make_mgmt()
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=30)]})
        state = NSORedistributionState.objects.get()
        state.metric = 20
        state.status = "accepted"
        state.save(update_fields=["metric", "status"])

        state = reconcile_redistribution(self.device, {"entries": [self._entry(metric=30)]})[0]

        self.assertEqual(state.metric, 20)
        self.assertEqual(state.status, "accepted")

    def test_idempotent(self):
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry()]})
        reconcile_redistribution(self.device, {"entries": [self._entry()]})
        self.assertEqual(Redistribution.objects.filter(source_protocol="static").count(), 1)

    def test_edit_surfaces_as_changed_and_survives(self):
        """Editing the Redistribution object → drift, and the edit is not clobbered."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        redist = Redistribution.objects.get(source_protocol="static")
        redist.metric = 99  # operator edit; device still reports 10
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction

        with intent_transaction(footprint_for_instance(redist)):
            redist.save()

        states = reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        self.assertEqual(states[0].status, "changed")  # edit surfaced as drift
        redist.refresh_from_db()
        self.assertEqual(redist.metric, 99)  # edit preserved, not reverted

    def test_device_change_auto_mirrors_when_untouched(self):
        """3-way: device metric change with object untouched → auto-mirror, in sync."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        # Device changes metric 10→20; object never edited → auto-mirror.
        self.assertFalse(
            redistribution_reconcile_plan(self.device, {"entries": [self._entry(metric=20)]}).changes_content
        )
        states = reconcile_redistribution(self.device, {"entries": [self._entry(metric=20)]})
        Redistribution.objects.get(source_protocol="static").refresh_from_db()
        self.assertEqual(Redistribution.objects.get(source_protocol="static").metric, 20)
        self.assertEqual(states[0].status, "imported")

    def test_device_change_to_native_row_used_by_owned_intent_changes_content(self):
        """A native mirror changes content when another owned overlay renders the row."""
        management = self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSODeviceManagement, NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        redistribution = Redistribution.objects.get(source_protocol="static")
        other_device = Device.objects.create(
            name="rd-router-owned",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        other_management = NSODeviceManagement.objects.create(
            device=other_device,
            nso_instance=management.nso_instance,
            nso_device_name=other_device.name,
        )
        NSORedistributionState.objects.create(
            management=other_management,
            dest_protocol="isis",
            source_protocol="static",
            redistribution=redistribution,
            status="accepted",
        )

        plan = redistribution_reconcile_plan(self.device, {"entries": [self._entry(metric=20)]})

        self.assertTrue(plan.changes_content)
        self.assertIn((other_device.pk, "isis"), plan.lock_footprint.revision_keys)
        from django.db import transaction

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.renderer_writer import IntentPlanStaleError, renderer_writes

        redistribution.metric = 99
        with transaction.atomic(), intent_transaction(footprint_for_instance(redistribution)):
            redistribution.save(update_fields=["metric"])
        with self.assertRaises(IntentPlanStaleError), renderer_writes(plan):
            reconcile_redistribution(self.device, {"entries": [self._entry(metric=20)]})

    def test_plan_validation_detects_destination_deletion(self):
        """The plan rejects a destination that disappears before acquisition."""
        management = self._make_mgmt()
        from django.db import transaction
        from netbox_routing.models import ISISInstance, Redistribution

        destination = ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.intent_state import RendererTargetsChanged, offline_mutation
        from netbox_nso_plugin.models import NSODeviceManagement, NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan
        from netbox_nso_plugin.renderer_writer import renderer_writes

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        redistribution = Redistribution.objects.get(source_protocol="static")
        other_device = Device.objects.create(
            name="rd-router-destination-lock",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        other_management = NSODeviceManagement.objects.create(
            device=other_device,
            nso_instance=management.nso_instance,
            nso_device_name=other_device.name,
        )
        NSORedistributionState.objects.create(
            management=other_management,
            dest_protocol="isis",
            source_protocol="static",
            redistribution=redistribution,
            status="accepted",
        )
        plan = redistribution_reconcile_plan(self.device, {"entries": [self._entry(metric=20)]})

        with transaction.atomic(), offline_mutation():
            destination.delete()

        with self.assertRaises(RendererTargetsChanged), renderer_writes(plan):
            reconcile_redistribution(self.device, {"entries": [self._entry(metric=20)]})

    def test_new_reported_entry_locks_and_revalidates_its_destination(self):
        """A reported entry without an overlay still protects its destination."""
        self._make_mgmt()
        from django.db import transaction
        from netbox_routing.models import ISISInstance

        destination = ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.intent_state import RendererTargetsChanged, SourceRow, offline_mutation
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes

        plan = redistribution_reconcile_plan(self.device, {"entries": [self._entry(metric=20)]})

        self.assertIn(SourceRow(destination._meta.label_lower, destination.pk), plan.lock_footprint.source_rows)
        with transaction.atomic(), offline_mutation():
            destination.delete()

        with self.assertRaises(RendererTargetsChanged), renderer_mirror_writes(plan):
            reconcile_redistribution(self.device, {"entries": [self._entry(metric=20)]})

    def test_plan_validation_detects_equal_content_relink(self):
        """The prediction identifies native rows even when their content is equal."""
        self._make_mgmt()
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from django.db import transaction

        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan
        from netbox_nso_plugin.renderer_writer import IntentPlanStaleError, renderer_mirror_writes

        state = reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})[0]
        other_device = Device.objects.create(
            name="rd-router-equal-relink",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        other_destination = ISISInstance.objects.create(device=other_device, process_tag="")
        other_redistribution = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(other_destination),
            destination_id=other_destination.pk,
            source_protocol="static",
            metric=10,
        )
        plan = redistribution_reconcile_plan(self.device, {"entries": [self._entry(metric=10)]})

        state.redistribution = other_redistribution
        with transaction.atomic(), offline_mutation():
            state.save(update_fields=["redistribution"])

        with self.assertRaises(IntentPlanStaleError), renderer_mirror_writes(plan):
            reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})

    def test_overlay_footprint_serializes_current_and_proposed_redistributions(self):
        """A relink locks both native redistribution dependencies."""
        self._make_mgmt()
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ISISInstance, Redistribution

        destination = ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.intent_state import SourceRow, footprint_for_instance
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        state = reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})[0]
        current_id = state.redistribution_id
        proposed = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(destination),
            destination_id=destination.pk,
            source_protocol="connected",
            metric=10,
        )
        state.redistribution = proposed

        footprint = footprint_for_instance(state)

        self.assertIn(("redistribution", str(current_id)), footprint.shared_keys)
        self.assertIn(("redistribution", str(proposed.pk)), footprint.shared_keys)
        self.assertIn(SourceRow("netbox_routing.redistribution", current_id), footprint.source_rows)
        self.assertIn(SourceRow("netbox_routing.redistribution", proposed.pk), footprint.source_rows)

    def test_ospf_destination_plan_acquires_its_source_row(self):
        """An OSPF destination participates in the declared source lock order."""
        self._make_mgmt()
        from netbox_routing.models import OSPFInstance

        destination = OSPFInstance.objects.create(
            name="process-1",
            router_id="198.18.0.1",
            process_id="1",
            device=self.device,
        )
        from netbox_nso_plugin.intent_state import SourceRow
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan

        entry = self._entry(dest_protocol="ospf", dest_ref="1", metric_type="1")
        reconcile_redistribution(self.device, {"entries": [entry]})
        plan = redistribution_reconcile_plan(self.device, {"entries": [entry]})
        self.assertIn(
            SourceRow(destination._meta.label_lower, destination.pk),
            plan.footprint.source_rows,
        )

        destination = OSPFInstance.objects.get(device=self.device, process_id="1")
        self.assertIn(SourceRow(destination._meta.label_lower, destination.pk), plan.lock_footprint.source_rows)

    def test_plan_locks_devices_that_share_a_route_map_without_expanding_revisions(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ISISInstance, RouteMap

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSORoutePolicyState
        from netbox_nso_plugin.redistribution_reconciler import redistribution_reconcile_plan

        self._make_mgmt()
        ISISInstance.objects.create(device=self.device, process_tag="")
        route_map = RouteMap.objects.create(name="RM-SHARED-REDIST")
        other_device = Device.objects.create(
            name="rd-router-policy-target",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        instance = NSOInstance.objects.get(name="rd-inst")
        other_management = NSODeviceManagement.objects.create(
            device=other_device,
            nso_instance=instance,
            nso_device_name=other_device.name,
        )
        NSORoutePolicyState.objects.create(
            management=other_management,
            content_type=ContentType.objects.get_for_model(RouteMap),
            object_id=route_map.pk,
            family="route_map",
            object_name=route_map.name,
        )

        plan = redistribution_reconcile_plan(
            self.device,
            {"entries": [self._entry(route_map=route_map.name, metric=10)]},
        )

        self.assertEqual(set(plan.lock_footprint.device_ids), {self.device.pk, other_device.pk})
        self.assertNotIn((other_device.pk, "route_policy"), plan.lock_footprint.revision_keys)

    def test_omitted_default_metric_type_migrates_prior_mirrored_default_to_absence(self):
        """A corrected reader omits a default-only metric-type.

        A prior unowned mirror may contain the old reader's fabricated default.
        Migrate that representation to absence so accepting it cannot write an
        explicit default back to the device.
        """
        self._make_mgmt()
        self._set_ned("cisco-ios-cli-6.114", "redist-ios-default")
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric_type="internal")]})
        corrected = self._entry()
        corrected.pop("metric_type")
        states = reconcile_redistribution(self.device, {"entries": [corrected]})

        native = Redistribution.objects.get(source_protocol="static")
        overlay = NSORedistributionState.objects.get()
        self.assertEqual(native.metric_type, "")
        self.assertEqual(overlay.metric_type, "")
        self.assertEqual(states[0].status, "imported")

    def test_omitted_default_metric_type_does_not_confirm_owned_nondefault(self):
        self._make_mgmt()
        self._set_ned("cisco-ios-cli-6.114", "redist-ios-owned")
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric_type="external")]})
        state = NSORedistributionState.objects.get()
        state.status = "accepted"
        state.save(update_fields=["status"])
        corrected = self._entry()
        corrected.pop("metric_type")

        state = reconcile_redistribution(self.device, {"entries": [corrected]})[0]

        self.assertEqual(Redistribution.objects.get().metric_type, "external")
        self.assertEqual(state.metric_type, "external")
        self.assertEqual(state.status, "accepted")

    def test_omitted_metric_type_does_not_confirm_owned_explicit_default(self):
        self._make_mgmt()
        self._set_ned("cisco-ios-cli-6.114", "redist-ios-explicit-default")
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry()]})
        state = NSORedistributionState.objects.get()
        state.redistribution.metric_type = "internal"
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction

        with intent_transaction(footprint_for_instance(state.redistribution)):
            state.redistribution.save(update_fields=["metric_type"])
        state.metric_type = "internal"
        state.status = "accepted"
        state.save(update_fields=["metric_type", "status"])
        corrected = self._entry()
        corrected.pop("metric_type")

        state = reconcile_redistribution(self.device, {"entries": [corrected]})[0]

        self.assertEqual(Redistribution.objects.get().metric_type, "internal")
        self.assertEqual(state.metric_type, "internal")
        self.assertEqual(state.status, "accepted")

    def test_non_ios_omission_does_not_invent_ios_metric_type(self):
        self._make_mgmt()
        self._set_ned("timos-nc-23.10", "redist-timos")
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        omitted = self._entry()
        omitted.pop("metric_type")
        reconcile_redistribution(self.device, {"entries": [omitted]})

        self.assertEqual(Redistribution.objects.get().metric_type, "")

    def test_both_moved_is_conflict(self):
        """3-way: object edited AND device changed since base → conflict, edit preserved."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        redist = Redistribution.objects.get(source_protocol="static")
        redist.metric = 99  # operator edit
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction

        with intent_transaction(footprint_for_instance(redist)):
            redist.save()
        states = reconcile_redistribution(self.device, {"entries": [self._entry(metric=20)]})  # device also moved
        self.assertEqual(states[0].status, "conflict")
        redist.refresh_from_db()
        self.assertEqual(redist.metric, 99)  # edit preserved

    def test_unowned_removed_redistribution_is_deleted(self):
        """An UNOWNED redistribution the device stops reporting is tracked away: the overlay
        row and its (leaf) Redistribution object are removed (no lingering false drift)."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        self.assertEqual(NSORedistributionState.objects.count(), 1)
        self.assertEqual(Redistribution.objects.count(), 1)

        reconcile_redistribution(self.device, {"entries": []})  # device removed it
        self.assertEqual(NSORedistributionState.objects.count(), 0)  # overlay gone
        self.assertEqual(Redistribution.objects.count(), 0)  # object gone

    def test_shared_native_is_deleted_when_all_stale_overlays_are_deleted(self):
        management = self._make_mgmt()
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ISISInstance, Redistribution

        destination = ISISInstance.objects.create(device=self.device, process_tag="")
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(destination),
            destination_id=destination.pk,
            source_protocol="static",
        )
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        for source_ref in ("first", "second"):
            NSORedistributionState.objects.create(
                management=management,
                dest_protocol="isis",
                source_protocol="static",
                source_ref=source_ref,
                redistribution=native,
                status="imported",
            )

        reconcile_redistribution(self.device, {"entries": []})

        self.assertFalse(NSORedistributionState.objects.exists())
        self.assertFalse(Redistribution.objects.filter(pk=native.pk).exists())

    def test_gated_removal_locks_and_deletes_the_native_redistribution(self):
        mgmt = self._make_mgmt()
        mgmt.manage_routing = True
        mgmt.manage_redistribution = True
        mgmt.save(update_fields=["manage_routing", "manage_redistribution"])
        from django.db import connection
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_category
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        native = Redistribution.objects.get()
        statements = []

        def observe_sql(execute, sql, params, many, context):
            statements.append((str(sql), tuple(params or ())))
            return execute(sql, params, many, context)

        with (
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch("netbox_nso_plugin.adapter_client.get_redistribution", return_value={"entries": []}),
            connection.execute_wrapper(observe_sql),
        ):
            context = reconcile_category(self.device, mgmt, "redistribution")

        self.assertEqual(context["_gate"]["redistribution"], "legacy")
        self.assertTrue(
            any(
                f'FROM "{native._meta.db_table}"' in sql and "FOR UPDATE" in sql and native.pk in params
                for sql, params in statements
            ),
            "the gated footprint did not lock the exact native redistribution row",
        )
        self.assertFalse(NSORedistributionState.objects.exists())
        self.assertFalse(Redistribution.objects.exists())

    def test_owned_removed_redistribution_kept_as_drift(self):
        """An ACCEPTED redistribution the device removes is KEPT and flagged: status=changed,
        device_present=False — operator intent is never auto-deleted."""
        from django.utils import timezone

        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        s = NSORedistributionState.objects.get(management__device=self.device)
        s.status = "accepted"
        s.accepted_at = timezone.now()
        s.save(update_fields=["status", "accepted_at"])

        reconcile_redistribution(self.device, {"entries": []})  # device removed it
        s.refresh_from_db()
        self.assertFalse(s.device_present)
        self.assertIn(s.status, ("accepted", "deploying", "in_sync", "apply_failed"))
        self.assertEqual(Redistribution.objects.count(), 1)  # object kept (operator owns it)


class TestBuildBgpRouterList(TestCase):
    """_build_bgp_router_list must materialize redistribution-only scopes.

    An accepted BGP redistribution whose (asn, vrf) has no owned peer previously
    produced an empty router list — the dest_ref join at apply time then found no
    AF and the redistribution silently never reached the device.
    """

    def test_redistribution_only_scope_materializes_router(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        redist = [{"source_protocol": "static", "source_ref": "", "route_map": "PCE-BGP-EXPORT"}]
        out = _build_bgp_router_list({}, {("2222", ""): {"ipv4-unicast": redist}})
        self.assertEqual(
            out,
            [
                {
                    "asn": "2222",
                    "scopes": [
                        {
                            "vrf": "",
                            "peers": [],
                            "address_families": [{"af": "ipv4-unicast", "redistribution": redist}],
                        }
                    ],
                }
            ],
        )

    def test_peer_scope_keeps_redistribution_and_no_duplicate(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        redist = [{"source_protocol": "static", "source_ref": ""}]
        routers = {
            "2222": {
                "asn": "2222",
                "scopes": {
                    "": {
                        "vrf": "",
                        "address_families": [],
                        "peers": [
                            {"peer_address": "192.0.2.1", "enabled": True, "remote_as": "1", "address_families": []}
                        ],
                    }
                },
            }
        }
        out = _build_bgp_router_list(routers, {("2222", ""): {"ipv4-unicast": redist}})
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["scopes"]), 1)
        scope = out[0]["scopes"][0]
        self.assertEqual(scope["address_families"], [{"af": "ipv4-unicast", "redistribution": redist}])
        self.assertEqual(len(scope["peers"]), 1)


def _peer(addr="192.0.2.1", afs=()):
    return {
        "peer_address": addr,
        "enabled": True,
        "remote_as": "1",
        "address_families": list(afs),
    }


def _routers(vrf="", peers=()):
    return {
        "2222": {
            "asn": "2222",
            "scopes": {vrf: {"vrf": vrf, "address_families": [], "peers": list(peers)}},
        }
    }


class TestScopeAddressFamiliesIncludePeerAfs(TestCase):
    """BGP-T1-1: the scope's address-family list was built from REDISTRIBUTION ROWS ONLY.

    `_build_bgp_router_list` did::

        af_map = scope_afs.get((asn_str, vrf_str), {})   # scope_afs <- accepted redistribution
        scope_out["address_families"] = afs_out          # OVERWRITES the scope's own list

    A peer's address-families are built separately into `peer["address_families"]` and never
    reached the scope. So on the ORDINARY path — peers accepted, no redistribution accepted —
    the scope arrived at bgp-reconciler with an EMPTY address-family list, and the writer drives
    all of the following off `scope.address_family`:

      * AF activation,
      * per-AF policy binding (route-map / prefix-list in+out), and
      * `_apply_ios_vrf_scope` in its ENTIRETY (its whole body is inside that loop).

    Consequences, all silent — the commit succeeds and NetBox reports in_sync:
      * the peer's route-maps and prefix-lists NEVER BIND -> the peer comes up UNFILTERED;
      * every IOS VRF peer is NEVER WRITTEN AT ALL;
      * IPv6 / VPNv4 address-families are never explicitly activated.

    (IOS auto-activates ipv4-unicast for a `neighbor ... remote-as`, which is why global peers
    still came up and the bug looked like nothing was wrong.)

    The scope's AFs must therefore be the UNION of the AFs carrying redistribution and the AFs
    any peer in that scope actually uses.
    """

    def test_peer_af_reaches_the_scope_without_any_redistribution(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        peer = _peer(afs=[{"af": "ipv4-unicast", "enabled": True, "routemap_in": "RM-IN"}])
        out = _build_bgp_router_list(_routers(peers=[peer]), {})
        scope = out[0]["scopes"][0]
        self.assertEqual(
            scope["address_families"],
            [{"af": "ipv4-unicast", "redistribution": []}],
            "the peer's AF never reached the scope — the writer binds no policy and the peer comes up UNFILTERED",
        )

    def test_peer_af_and_redistribution_af_are_unioned_not_replaced(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        redist = [{"source_protocol": "static", "source_ref": ""}]
        peer = _peer(afs=[{"af": "ipv6-unicast", "enabled": True}])
        out = _build_bgp_router_list(_routers(peers=[peer]), {("2222", ""): {"ipv4-unicast": redist}})
        afs = {a["af"]: a["redistribution"] for a in out[0]["scopes"][0]["address_families"]}
        self.assertEqual(afs, {"ipv4-unicast": redist, "ipv6-unicast": []})

    def test_an_af_carrying_both_is_not_duplicated_and_keeps_its_redistribution(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        redist = [{"source_protocol": "connected", "source_ref": ""}]
        peer = _peer(afs=[{"af": "ipv4-unicast", "enabled": True}])
        out = _build_bgp_router_list(_routers(peers=[peer]), {("2222", ""): {"ipv4-unicast": redist}})
        self.assertEqual(
            out[0]["scopes"][0]["address_families"],
            [{"af": "ipv4-unicast", "redistribution": redist}],
        )

    def test_vrf_scope_peer_af_reaches_the_scope(self):
        """The IOS VRF writer's ENTIRE body is inside `for af_entry in scope.address_family`."""
        from netbox_nso_plugin.signals import _build_bgp_router_list

        peer = _peer(addr="10.9.9.1", afs=[{"af": "ipv4-unicast", "enabled": True}])
        out = _build_bgp_router_list(_routers(vrf="CUST-A", peers=[peer]), {})
        scope = out[0]["scopes"][0]
        self.assertEqual(scope["vrf"], "CUST-A")
        self.assertEqual(
            scope["address_families"],
            [{"af": "ipv4-unicast", "redistribution": []}],
            "with an empty AF list the IOS VRF writer is a total no-op — the peer is never written",
        )

    def test_a_peer_with_no_afs_still_yields_an_empty_list(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        out = _build_bgp_router_list(_routers(peers=[_peer()]), {})
        self.assertEqual(out[0]["scopes"][0]["address_families"], [])
