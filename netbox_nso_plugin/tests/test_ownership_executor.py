# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Symmetric ownership execution at the real database seam."""

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from ._outbox_case import make_managed, mirror_update, own_route, own_vlan


class TestSymmetricOwnershipExecutor(TestCase):
    def setUp(self):
        super().setUp()
        set_scope = patch("netbox_nso_plugin.adapter_client.set_scope", return_value={})
        set_scope.start()
        self.addCleanup(set_scope.stop)
        self.device, self.management = make_managed("ownership-executor", 16271)

    def test_foreign_overlay_delete_reowns_from_the_surviving_manifest(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOVLANState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        state = own_vlan(self.management, 1710, "ownership-reown")
        vlan = state.vlan
        NSOVLANState.objects.filter(pk=state.pk).delete()

        completed = reconcile_scope_ownership(self.device.pk, ["vlan"])

        replacement = NSOVLANState.objects.get(management=self.management, vlan=vlan)
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        self.assertEqual(replacement.status, "accepted")
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertEqual(manifest.native_id, vlan.pk)
        self.assertIn(("vlan", replacement.pk), completed)

    def test_rescoped_owned_vlan_remains_owned_during_the_next_audit(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        state = own_vlan(self.management, 1717, "ownership-rescope")
        shared = VLANGroup.objects.create(name="Ownership shared", slug="ownership-shared")
        VLAN.objects.filter(pk=state.vlan_id).update(group=shared)

        reconcile_scope_ownership(self.device.pk, ["vlan"])

        state.refresh_from_db()
        manifest = NSOOwnershipManifest.objects.get(
            device_id=self.device.pk,
            scope="vlan",
            native_id=state.vlan_id,
            ownership_state="owned",
        )
        self.assertEqual(state.status, "accepted")
        self.assertEqual(manifest.native_key["group_id"], shared.pk)

    def test_native_delete_retracts_through_scope_deletion_authority(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        state = own_vlan(self.management, 1711, "ownership-retract")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        VLAN.objects.filter(pk=state.vlan_id).delete()

        completed = reconcile_scope_ownership(self.device.pk, ["vlan"])

        manifest.refresh_from_db()
        contribution = NSOIntentOutboxEntry.objects.get(device=self.device, scope="vlan")
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertTrue(contribution.mark_and)
        self.assertTrue(contribution.mark_any)
        self.assertIn(("vlan", manifest.pk), completed)

    def test_retract_refuses_when_its_contribution_cannot_be_written(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership
        from netbox_nso_plugin.signals import suppress_intent_push

        state = own_vlan(self.management, 1716, "ownership-suppressed")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        VLAN.objects.filter(pk=state.vlan_id).delete()

        # outbox.enqueue writes nothing while pushes are suppressed, so retiring here would
        # drop the deletion authority silently and plan_ownership never retries a retired row.
        with suppress_intent_push(), self.assertRaises(IntentMutationProtocolError):
            reconcile_scope_ownership(self.device.pk, ["vlan"])

        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").exists())

    def test_unowned_overlay_with_a_native_anchor_is_never_promoted(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOVLANState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        group = VLANGroup.objects.create(name="Ownership acquisition", slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.create(group=group, vid=1712, name="ownership-acquire")
        state = NSOVLANState.objects.create(
            management=self.management,
            vlan=vlan,
            device_name=vlan.name,
            status="imported",
        )

        completed = reconcile_scope_ownership(self.device.pk, ["vlan"])

        state.refresh_from_db()
        self.assertEqual(state.status, "imported")
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="vlan").exists())
        self.assertEqual(completed, ())

    def test_imported_overlay_survives_a_full_delivery_unpromoted(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOVLANState

        group = VLANGroup.objects.create(name="Ownership delivery", slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.create(group=group, vid=1715, name="ownership-deliver")
        state = NSOVLANState.objects.create(
            management=self.management,
            vlan=vlan,
            device_name=vlan.name,
            status="imported",
        )

        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as put_vlan:
            deliver("vlan", self.device.pk, self.management.adapter_device_id)

        put_vlan.assert_called_once()
        state.refresh_from_db()
        self.assertEqual(put_vlan.call_args[0][1], [])
        self.assertEqual(state.status, "imported")
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="vlan").exists())

    def test_retracted_overlay_leaves_the_rendered_document(self):
        from dcim.models import Interface

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOInterfaceMtuState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        interface = Interface.objects.create(
            device=self.device,
            name="Ethernet7",
            type="1000base-t",
            mtu=9216,
        )
        state = NSOInterfaceMtuState.objects.create(
            management=self.management,
            interface=interface,
            l2_mtu=9216,
            status="accepted",
        )
        reconcile_scope_ownership(self.device.pk, ["interface_mtu"])
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="interface_mtu")
        # A foreign edit clears the native anchor: the desired document no longer has an MTU.
        Interface.objects.filter(pk=interface.pk).update(mtu=None)

        completed = reconcile_scope_ownership(self.device.pk, ["interface_mtu"])

        manifest.refresh_from_db()
        payload = delivery.render("interface_mtu", self.device.pk, self.management.adapter_device_id).payload
        state.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertEqual((state.status, state.accepted_at), ("imported", None))
        self.assertEqual(payload, [])
        self.assertIn(("interface_mtu", manifest.pk), completed)

    def test_owned_overlay_without_a_manifest_leaves_the_rendered_document(self):
        from dcim.models import Interface

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOInterfaceMtuState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        # No MTU on the native row, so nothing qualifies this owned overlay for ownership
        # and no manifest was ever recorded for it.
        interface = Interface.objects.create(device=self.device, name="Ethernet10", type="1000base-t")
        state = NSOInterfaceMtuState.objects.create(
            management=self.management,
            interface=interface,
            l2_mtu=9216,
            status="accepted",
        )

        completed = reconcile_scope_ownership(self.device.pk, ["interface_mtu"])

        state.refresh_from_db()
        payload = delivery.render("interface_mtu", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual((state.status, state.accepted_at), ("imported", None))
        self.assertEqual(payload, [])
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="interface_mtu").exists())
        self.assertIn(("interface_mtu", state.pk), completed)

    def test_a_retract_takes_the_object_out_of_the_scope_render(self):
        """Every scope whose overlay can outlive its native anchor must stop rendering it.

        The ``existing_overlay`` scopes are not fixtured here: their overlay either IS the
        native row (l2_sap/logging/snmp) or cascades with it (bfd), so a retract leaves
        nothing to render. ``route_policy`` is the one exception and is reported separately.
        """
        from dcim.models import Interface
        from ipam.models import VLAN, VLANGroup
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import (
            NSOInterfaceMtuState,
            NSOOwnershipManifest,
            NSOSubinterfaceState,
            NSOSwitchportState,
        )
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        def build_interface_mtu(device, management):
            interface = Interface.objects.create(device=device, name="Ethernet20", type="1000base-t", mtu=9216)
            NSOInterfaceMtuState.objects.create(
                management=management,
                interface=interface,
                l2_mtu=9216,
                status="accepted",
            )
            return lambda: Interface.objects.filter(pk=interface.pk).update(mtu=None)

        def build_subinterface(device, management):
            parent = Interface.objects.create(device=device, name="Ethernet21", type="1000base-t")
            interface = Interface.objects.create(
                device=device,
                name="Ethernet21.40",
                type="virtual",
                parent=parent,
            )
            NSOSubinterfaceState.objects.create(
                management=management,
                interface=interface,
                parent_interface=parent,
                dot1q_vlan=40,
                status="accepted",
            )
            return lambda: Interface.objects.filter(pk=interface.pk).update(parent=None)

        def build_switchport(device, management):
            group = VLANGroup.objects.create(name=f"Ownership retract {device.pk}", slug=f"nso-{device.pk}")
            vlan = VLAN.objects.create(group=group, vid=1740, name="ownership-retract")
            interface = Interface.objects.create(
                device=device,
                name="Ethernet22",
                type="1000base-t",
                mode="access",
                untagged_vlan=vlan,
            )
            NSOSwitchportState.objects.create(
                management=management,
                interface=interface,
                mode="access",
                untagged_vlan=vlan,
                status="accepted",
            )
            return lambda: Interface.objects.filter(pk=interface.pk).update(mode="", untagged_vlan=None)

        def build_interface_attribute(device, management):
            type(management).objects.filter(pk=management.pk).update(manage_description=True)
            management.refresh_from_db()
            Interface.objects.create(
                device=device,
                name="Ethernet23",
                type="1000base-t",
                description="managed uplink",
            )
            return lambda: type(management).objects.filter(pk=management.pk).update(manage_description=False)

        def build_static_route(device, _management):
            route = StaticRoute.objects.create(prefix="198.18.175.0/24", next_hop="198.18.0.175", metric=1)
            route.devices.add(device)
            return lambda: StaticRoute.objects.filter(pk=route.pk).update(next_hop=None)

        scenarios = (
            ("interface_mtu", build_interface_mtu),
            ("subinterface", build_subinterface),
            ("switchport", build_switchport),
            ("interface", build_interface_attribute),
            ("static_route", build_static_route),
        )
        for index, (scope, build) in enumerate(scenarios):
            with self.subTest(scope=scope):
                device, management = make_managed(f"ownret{index}", 16290 + index, index=index)
                disqualify = build(device, management)

                reconcile_scope_ownership(device.pk, [scope])

                manifest = NSOOwnershipManifest.objects.get(device_id=device.pk, scope=scope)
                self.assertNotEqual(delivery.render(scope, device.pk, management.adapter_device_id).payload, [])

                disqualify()
                reconcile_scope_ownership(device.pk, [scope])

                manifest.refresh_from_db()
                self.assertEqual(manifest.ownership_state, "retired")
                self.assertEqual(delivery.render(scope, device.pk, management.adapter_device_id).payload, [])

    def test_owned_overlay_with_a_qualifying_anchor_is_never_demoted(self):
        from dcim.models import Interface
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOSVIState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        # An SVI qualifies when its interface name resolves a VLAN of the device's group:
        # a Vlan<vid> interface whose vid names no device VLAN is not a qualifying anchor.
        group = VLANGroup.objects.create(name="Ownership svi anchor", slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.create(group=group, vid=1731, name="ownership-svi-anchor")
        anchored = NSOSVIState.objects.create(
            management=self.management,
            interface=Interface.objects.create(device=self.device, name="Vlan1731", type="virtual"),
            vlan=vlan,
            svi_type="svi",
            status="accepted",
        )
        unanchored = NSOSVIState.objects.create(
            management=self.management,
            interface=Interface.objects.create(device=self.device, name="Vlan2213", type="virtual"),
            vlan=vlan,
            svi_type="svi",
            status="accepted",
        )

        reconcile_scope_ownership(self.device.pk, ["svi"])

        anchored.refresh_from_db()
        unanchored.refresh_from_db()
        self.assertEqual(anchored.status, "accepted")
        self.assertEqual(unanchored.status, "imported")
        self.assertEqual(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="svi").count(), 1)

    def test_foreign_overlay_delete_retires_a_scope_with_no_native_content(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOBFDInterfaceState, NSOIntentOutboxEntry, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        interface = Interface.objects.create(device=self.device, name="Ethernet8", type="1000base-t")
        state = NSOBFDInterfaceState.objects.create(
            management=self.management,
            interface=interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="accepted",
        )
        reconcile_scope_ownership(self.device.pk, ["bfd"])
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="bfd")
        NSOBFDInterfaceState.objects.filter(pk=state.pk).delete()
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="bfd").delete()

        completed = reconcile_scope_ownership(self.device.pk, ["bfd"])

        manifest.refresh_from_db()
        # BFD timers live only on the overlay: dcim.Interface carries no value to re-own from,
        # so a foreign overlay delete must retire the identity, never fabricate device intent.
        self.assertFalse(NSOBFDInterfaceState.objects.filter(interface=interface).exists())
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="bfd").exists())
        self.assertIn(("bfd", manifest.pk), completed)

    def test_foreign_ospf_process_overlay_delete_does_not_fabricate_intent(self):
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOOSPFInstanceState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        ospf_instance = OSPFInstance.objects.create(
            device=self.device,
            process_id="18",
            name="18",
            router_id="198.18.174.1",
        )
        state = NSOOSPFInstanceState.objects.create(
            management=self.management,
            ospf_instance=ospf_instance,
            process_id="18",
            router_id="198.18.174.1",
            areas=[{"area-id": "0.0.0.18", "area-type": "stub"}],
            enabled=False,
            status="accepted",
        )
        reconcile_scope_ownership(self.device.pk, ["ospf"])
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="ospf")
        NSOOSPFInstanceState.objects.filter(pk=state.pk).delete()
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="ospf").delete()

        completed = reconcile_scope_ownership(self.device.pk, ["ospf"])

        manifest.refresh_from_db()
        self.assertFalse(NSOOSPFInstanceState.objects.filter(ospf_instance=ospf_instance).exists())
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="ospf").exists())
        self.assertIn(("ospf", manifest.pk), completed)

    def test_scope_reconciliation_stays_device_scoped_as_the_fleet_grows(self):
        from django.contrib.contenttypes.models import ContentType
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from netbox_routing.models import ISISInstance, Redistribution

        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        ISISInstance.objects.create(device=self.device, process_tag="CORE", net="49.0001.0198.0180.1741.00")
        foreign_device, _foreign_management = make_managed("ownership-fleet", 16273, index=2)
        foreign_instance = ISISInstance.objects.create(
            device=foreign_device,
            process_tag="CORE",
            net="49.0001.0198.0180.1742.00",
        )
        isis_type = ContentType.objects.get_for_model(ISISInstance)
        # One steady-state pass first: the measured passes must differ only by the fleet rows.
        reconcile_scope_ownership(self.device.pk, ["isis"])

        with CaptureQueriesContext(connection) as before:
            reconcile_scope_ownership(self.device.pk, ["isis"])
        for index in range(5):
            Redistribution.objects.create(
                destination_type=isis_type,
                destination_id=foreign_instance.pk,
                source_protocol="static",
                source_ref=f"fleet-{index}",
            )
        with CaptureQueriesContext(connection) as after:
            reconcile_scope_ownership(self.device.pk, ["isis"])

        for query in after.captured_queries:
            sql = query["sql"]
            if "netbox_routing_redistribution" in sql and sql.lstrip().upper().startswith("SELECT"):
                self.assertIn(" WHERE ", sql, "the redistribution read has no device predicate")
        self.assertEqual(len(after.captured_queries), len(before.captured_queries))

    def test_recording_missing_manifests_costs_one_device_scan(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        def measure(rows):
            device, management = make_managed(f"ownscan{rows}", 16280 + rows, index=rows)
            group = VLANGroup.objects.create(name=f"Ownership scan {rows}", slug=f"nso-{device.pk}")
            for index in range(rows):
                vlan = VLAN.objects.create(group=group, vid=1750 + index, name=f"ownership-scan-{rows}-{index}")
                NSOVLANState.objects.create(
                    management=management,
                    vlan=vlan,
                    device_name=vlan.name,
                    status="accepted",
                )
            with CaptureQueriesContext(connection) as captured:
                reconcile_scope_ownership(device.pk, ["vlan"])
            return len(captured.captured_queries)

        measure(1)
        two, three, four = measure(2), measure(3), measure(4)

        # One device scan builds the plan; revalidating a planned row is O(1), so the cost
        # per extra owned overlay is constant. A per-row re-scan makes it grow.
        self.assertEqual(four - three, three - two)

    def test_native_create_planning_batches_manifest_and_overlay_reads(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.ownership_planner import _native_create_actions

        def measure(rows):
            device, _management = make_managed(f"owncreate{rows}", 16300 + rows, index=rows)
            group = VLANGroup.objects.create(name=f"Ownership create {rows}", slug=f"nso-{device.pk}")
            for index in range(rows):
                VLAN.objects.create(group=group, vid=1760 + index, name=f"ownership-create-{rows}-{index}")
            with CaptureQueriesContext(connection) as captured:
                planned = _native_create_actions(device.pk, frozenset({"vlan"}))
            self.assertEqual(len(planned), rows)
            return len(captured.captured_queries)

        self.assertEqual(measure(2), measure(4))

    def test_cleared_ownership_detaches_without_deletion_authority(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOOwnershipManifest, NSOVLANState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        state = own_vlan(self.management, 1713, "ownership-detach")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        NSOVLANState.objects.filter(pk=state.pk).update(status="imported", accepted_at=None)

        completed = reconcile_scope_ownership(self.device.pk, ["vlan"])

        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "detached")
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").exists())
        self.assertIn(("vlan", manifest.pk), completed)

    def test_static_route_delete_uses_manifest_id_and_lineage_authority(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        route = own_route(
            self.management,
            "198.18.171.0/24",
            "198.18.0.171",
            device=self.device,
        )
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="static_route")
        route_id = route.pk
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="static_route").delete()
        StaticRoute.objects.filter(pk=route_id).delete()

        completed = reconcile_scope_ownership(self.device.pk, ["static_route"])

        manifest.refresh_from_db()
        contribution = NSOIntentOutboxEntry.objects.get(device=self.device, scope="static_route")
        self.assertEqual(manifest.native_id, route_id)
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertFalse(contribution.mark_any)
        self.assertEqual(contribution.transitions[0]["route_id"], route_id)
        self.assertTrue(contribution.transitions[0]["unverified"])
        self.assertIn(("static_route", manifest.pk), completed)

    def test_assigned_native_static_route_creates_an_owned_overlay(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOStaticRouteState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        route = StaticRoute.objects.create(
            prefix="198.18.172.0/24",
            next_hop="198.18.0.172",
            metric=1,
        )
        route.devices.add(self.device)

        completed = reconcile_scope_ownership(self.device.pk, ["static_route"])

        state = NSOStaticRouteState.objects.get(management=self.management, static_route=route)
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="static_route")
        self.assertEqual(state.status, "accepted")
        self.assertGreater(state.intent_generation, 0)
        self.assertEqual(manifest.native_id, route.pk)
        self.assertIn(("static_route", state.pk), completed)

    def test_native_create_that_renders_nothing_is_a_mirror_write(self):
        from dcim.models import Interface

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import (
            NSOLACPBundleState,
            NSOLACPMemberState,
            NSOOwnershipManifest,
        )
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        bundle = Interface.objects.create(device=self.device, name="Port-channel19", type="lag")
        member = Interface.objects.create(device=self.device, name="Ethernet11", type="1000base-t", lag=bundle)
        # The bundle overlay is device-read state, so the LACP document owns nothing and the
        # member overlay this create seeds renders nothing either.
        NSOLACPBundleState.objects.create(
            management=self.management,
            interface=bundle,
            lag_id=19,
            status="imported",
        )

        completed = reconcile_scope_ownership(self.device.pk, ["lacp"])

        state = NSOLACPMemberState.objects.get(interface=member)
        payload = delivery.render("lacp", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual(state.lag_bundle, bundle)
        self.assertIn(("lacp", state.pk), completed)
        self.assertEqual(payload, [])
        self.assertTrue(
            NSOOwnershipManifest.objects.filter(
                device_id=self.device.pk,
                scope="lacp",
                state_model_label="netbox_nso_plugin.nsolacpmemberstate",
            ).exists()
        )

    def test_native_flex_algo_creates_an_owned_overlay(self):
        from netbox_routing.models import ISISFlexAlgo, ISISInstance

        from netbox_nso_plugin.models import NSOISISFlexAlgoState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        instance = ISISInstance.objects.create(device=self.device, process_tag="CORE")
        flex_algo = ISISFlexAlgo.objects.create(
            instance=instance,
            algo_id=172,
            metric_type="delay-metric",
            priority=120,
        )

        completed = reconcile_scope_ownership(self.device.pk, ["isis_flex_algo"])

        state = NSOISISFlexAlgoState.objects.get(management=self.management, isis_flex_algo=flex_algo)
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="isis_flex_algo")
        self.assertEqual(state.status, "accepted")
        self.assertEqual((state.process_tag, state.algo_id), ("CORE", 172))
        self.assertEqual(manifest.native_id, flex_algo.pk)
        self.assertIn(("isis_flex_algo", state.pk), completed)

    def test_native_interface_attributes_create_owned_overlays(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        type(self.management).objects.filter(pk=self.management.pk).update(
            manage_description=True,
            manage_enabled=True,
        )
        interface = Interface.objects.create(
            device=self.device,
            name="Ethernet1",
            type="1000base-t",
            description="managed uplink",
            enabled=False,
        )

        reconcile_scope_ownership(self.device.pk, ["interface"])

        states = NSOInterfaceState.objects.filter(interface=interface).order_by("attribute")
        self.assertEqual(
            list(states.values_list("attribute", "status")), [("description", "accepted"), ("enabled", "accepted")]
        )
        self.assertEqual(
            NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="interface").count(),
            2,
        )

    def test_native_interface_topology_creates_every_owned_overlay(self):
        from dcim.models import Interface
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import (
            NSOInterfaceMtuState,
            NSOLACPBundleState,
            NSOLACPMemberState,
            NSOSubinterfaceState,
            NSOSVIState,
            NSOSwitchportState,
            NSOVLANState,
        )
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        group = VLANGroup.objects.create(name="Ownership device VLANs", slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.create(group=group, vid=1723, name="ownership-native")
        parent = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t", mtu=9216)
        switchport = Interface.objects.create(
            device=self.device,
            name="Ethernet3",
            type="1000base-t",
            mode="access",
            untagged_vlan=vlan,
        )
        svi = Interface.objects.create(
            device=self.device,
            name="Vlan1723",
            type="virtual",
            untagged_vlan=vlan,
        )
        subinterface = Interface.objects.create(
            device=self.device,
            name="Ethernet2.1724",
            type="virtual",
            parent=parent,
        )
        bundle = Interface.objects.create(device=self.device, name="Port-channel17", type="lag")
        member = Interface.objects.create(
            device=self.device,
            name="Ethernet4",
            type="1000base-t",
            lag=bundle,
        )

        reconcile_scope_ownership(
            self.device.pk,
            ["vlan", "svi", "subinterface", "interface_mtu", "switchport", "lacp"],
        )

        self.assertEqual(NSOVLANState.objects.get(vlan=vlan).status, "accepted")
        self.assertEqual(NSOSVIState.objects.get(interface=svi).vlan, vlan)
        self.assertEqual(NSOSubinterfaceState.objects.get(interface=subinterface).dot1q_vlan, 1724)
        self.assertEqual(NSOInterfaceMtuState.objects.get(interface=parent).l2_mtu, 9216)
        self.assertEqual(NSOSwitchportState.objects.get(interface=switchport).untagged_vlan, vlan)
        self.assertEqual(NSOLACPBundleState.objects.get(interface=bundle).status, "accepted")
        self.assertEqual(NSOLACPMemberState.objects.get(interface=member).lag_bundle, bundle)

    def test_assigned_native_ip_creates_an_owned_overlay(self):
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        interface = Interface.objects.create(device=self.device, name="Ethernet5", type="1000base-t")
        address = IPAddress.objects.create(
            address="198.18.172.5/24",
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=interface.pk,
        )

        reconcile_scope_ownership(self.device.pk, ["ip"])

        state = NSOInterfaceIPState.objects.get(interface=interface, address=str(address.address), vrf="")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="ip")
        self.assertEqual(state.status, "accepted")
        self.assertEqual(manifest.native_id, address.pk)

    def test_deleted_native_ip_demotes_its_surviving_owned_overlay(self):
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOInterfaceIPState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        interface = Interface.objects.create(device=self.device, name="Ethernet5.2", type="1000base-t")
        address = IPAddress.objects.create(
            address="198.18.172.7/24",
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=interface.pk,
        )
        reconcile_scope_ownership(self.device.pk, ["ip"])
        state = NSOInterfaceIPState.objects.get(interface=interface, address=str(address.address), vrf="")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="ip")

        IPAddress.objects.filter(pk=address.pk).delete()
        reconcile_scope_ownership(self.device.pk, ["ip"])

        state.refresh_from_db()
        manifest.refresh_from_db()
        self.assertEqual(state.status, "imported")
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertEqual(delivery.render("ip", self.device.pk, self.management.adapter_device_id).payload, [])

    def test_an_unknown_vrf_cannot_bind_to_a_global_address(self):
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.ownership_planner import manifest_binding

        interface = Interface.objects.create(device=self.device, name="Ethernet5.1", type="virtual")
        address = IPAddress.objects.create(
            address="198.18.172.6/24",
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=interface.pk,
        )
        state = NSOInterfaceIPState(
            interface=interface,
            address=str(address.address),
            vrf="missing-vrf",
            status="accepted",
        )

        self.assertIsNone(manifest_binding(state))

    def test_native_routing_graph_creates_every_owned_overlay(self):
        from dcim.models import Device, Interface
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import (
            BGPPeer,
            BGPRouter,
            BGPScope,
            ISISInstance,
            ISISInterface,
            OSPFArea,
            OSPFInstance,
            OSPFInterface,
            Redistribution,
        )

        from netbox_nso_plugin.models import (
            NSOBGPPeerState,
            NSOISISInstanceState,
            NSOISISInterfaceState,
            NSOOSPFInstanceState,
            NSOOSPFInterfaceState,
            NSORedistributionState,
        )
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        interface = Interface.objects.create(device=self.device, name="Loopback17", type="virtual")
        rir = RIR.objects.create(name="Ownership private", slug="ownership-private")
        local_as = ASN.objects.create(asn=64520, rir=rir)
        remote_as = ASN.objects.create(asn=64521, rir=rir)
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=local_as,
            name="64520",
        )
        bgp_scope = BGPScope.objects.create(router=router)
        peer_ip = IPAddress.objects.create(address="198.18.173.2/32")
        peer = BGPPeer.objects.create(
            scope=bgp_scope,
            peer=peer_ip,
            remote_as=remote_as,
            enabled=True,
        )
        isis_instance = ISISInstance.objects.create(
            device=self.device,
            process_tag="CORE",
            net="49.0001.0198.0180.1731.00",
        )
        isis_interface = ISISInterface.objects.create(
            instance=isis_instance,
            interface=interface,
            address_family="ipv4",
            metric=17,
        )
        ospf_instance = OSPFInstance.objects.create(
            device=self.device,
            process_id="17",
            name="17",
            router_id="198.18.173.1",
        )
        area = OSPFArea.objects.create(area_id="0.0.0.0", area_type="standard")
        OSPFInterface.objects.create(
            instance=ospf_instance,
            area=area,
            interface=interface,
            cost=17,
        )
        redistribution = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(ISISInstance),
            destination_id=isis_instance.pk,
            source_protocol="static",
        )

        reconcile_scope_ownership(self.device.pk, ["bgp", "isis", "ospf"])

        self.assertEqual(NSOBGPPeerState.objects.get(bgp_peer=peer).status, "accepted")
        self.assertEqual(NSOISISInstanceState.objects.get(isis_instance=isis_instance).status, "accepted")
        self.assertEqual(NSOISISInterfaceState.objects.get(isis_interface=isis_interface).metric, 17)
        ospf_state = NSOOSPFInstanceState.objects.get(ospf_instance=ospf_instance)
        self.assertEqual(ospf_state.router_id, "198.18.173.1")
        self.assertEqual(ospf_state.areas, [{"area-id": "0.0.0.0", "area-type": "standard"}])
        self.assertEqual(NSOOSPFInterfaceState.objects.get(interface=interface).cost, 17)
        self.assertEqual(NSORedistributionState.objects.get(redistribution=redistribution).dest_protocol, "isis")

    def test_existing_overlay_strategies_require_explicit_owned_state(self):
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import (
            NSOBFDInterfaceState,
            NSOL2SapState,
            NSOLoggingLevelState,
            NSOOwnershipManifest,
            NSORoutePolicyState,
            NSOSnmpSystemInfoState,
        )
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        interface = Interface.objects.create(device=self.device, name="Ethernet6", type="1000base-t")
        policy = PrefixList.objects.create(name="OWNERSHIP-PREFIXES")
        rows = [
            NSOBFDInterfaceState.objects.create(
                management=self.management,
                interface=interface,
                min_tx=300,
                status="imported",
            ),
            NSOL2SapState.objects.create(
                management=self.management,
                service_name="ownership-service",
                sap_id="Ethernet6:1726",
                status="imported",
            ),
            NSOLoggingLevelState.objects.create(
                management=self.management,
                console_severity="warning",
                status="imported",
            ),
            NSORoutePolicyState.objects.create(
                management=self.management,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=policy.pk,
                family="prefix_list",
                object_name=policy.name,
                status="imported",
            ),
            NSOSnmpSystemInfoState.objects.create(
                management=self.management,
                location="ownership lab",
                status="imported",
            ),
        ]

        reconcile_scope_ownership(
            self.device.pk,
            ["bfd", "l2_sap", "logging", "route_policy", "snmp"],
        )

        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, "imported")
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk).exists())

        for row in rows:
            type(row).objects.filter(pk=row.pk).update(status="accepted")

        reconcile_scope_ownership(
            self.device.pk,
            ["bfd", "l2_sap", "logging", "route_policy", "snmp"],
        )

        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, "accepted")
        self.assertEqual(NSOOwnershipManifest.objects.filter(device_id=self.device.pk).count(), 5)

    def test_a_retract_demotes_the_row_its_own_transaction_repended(self):
        """A plan frozen before ``intent_transaction`` loses to the transaction's repend.

        A route whose next hop is an interface never qualifies for ownership, so an owned
        manifest for it retracts on every pass. ``intent_transaction`` re-pends every
        deploying row of the scope it bumps, which rewrites the very overlay the demotion
        plan captured, and the writer's full pre-image compare-and-set then fails the
        MANDATORY pre-capture audit closed for Apply, drain and deliver alike.
        """
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOOwnershipManifest, NSOStaticRouteState
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        from ._static_route_case import _assign_and_accept

        route = StaticRoute.objects.create(
            prefix="198.18.176.0/24",
            interface_next_hop="Ethernet40",
            metric=1,
        )
        _assign_and_accept(route, self.device)
        state = NSOStaticRouteState.objects.get(management=self.management, static_route=route)
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="static_route")
        self.assertEqual(manifest.ownership_state, "owned")
        mirror_update(state, status="deploying", apply_attempt_id=uuid4())

        result = audit_renderer_scopes(self.device.pk, ["static_route"], trigger="test", pre_capture=True)

        state.refresh_from_db()
        manifest.refresh_from_db()
        self.assertEqual(result.unknown, ())
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertEqual((state.status, state.accepted_at), ("imported", None))

    def test_a_manifest_less_demotion_survives_its_own_transaction_repend(self):
        """The record loop rebuilds the retract plan after its transaction re-pends rows."""
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState, NSOOwnershipManifest
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        # No MTU on the native row, so nothing qualifies this owned overlay and no manifest
        # was ever recorded for it: the retract runs through ``_demote_overlay``.
        interface = Interface.objects.create(device=self.device, name="Ethernet41", type="1000base-t")
        state = NSOInterfaceMtuState.objects.create(
            management=self.management,
            interface=interface,
            l2_mtu=9216,
            status="accepted",
        )
        mirror_update(state, status="deploying", apply_attempt_id=uuid4())

        result = audit_renderer_scopes(self.device.pk, ["interface_mtu"], trigger="test", pre_capture=True)

        state.refresh_from_db()
        self.assertEqual(result.unknown, ())
        self.assertEqual((state.status, state.accepted_at), ("imported", None))
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="interface_mtu").exists())
