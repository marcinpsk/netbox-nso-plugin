# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Symmetric ownership execution at the real database seam."""

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, TransactionTestCase

from ._outbox_case import make_device, make_managed, mirror_update, own_route, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestSymmetricOwnershipExecutor(TestCase):
    def setUp(self):
        super().setUp()
        set_scope = patch("netbox_nso_plugin.adapter_client.set_scope", return_value={})
        set_scope.start()
        self.addCleanup(set_scope.stop)
        self.device, self.management = make_managed("ownership-executor", 16271)

    def _make_native_bgp_peers(self, *addresses):
        from dcim.models import Device
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        rir = RIR.objects.create(name="Ownership private", slug="ownership-private")
        local_as = ASN.objects.create(asn=64520, rir=rir)
        remote_as = ASN.objects.create(asn=64521, rir=rir)
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=local_as,
            name="64520",
        )
        scope = BGPScope.objects.create(router=router)
        return tuple(
            BGPPeer.objects.create(
                scope=scope,
                peer=IPAddress.objects.create(address=address),
                remote_as=remote_as,
                enabled=True,
            )
            for address in addresses
        )

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

        local_instance = ISISInstance.objects.create(
            device=self.device,
            process_tag="CORE",
            net="49.0001.0198.0180.1741.00",
        )
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

        redistribution_reads = [
            query["sql"]
            for query in after.captured_queries
            if 'FROM "netbox_routing_redistribution"' in query["sql"]
            and query["sql"].lstrip().upper().startswith("SELECT")
        ]
        self.assertTrue(redistribution_reads)
        for sql in redistribution_reads:
            self.assertIn(
                f'"netbox_routing_redistribution"."destination_id" = {local_instance.pk}',
                sql,
            )
            self.assertNotIn(
                f'"netbox_routing_redistribution"."destination_id" = {foreign_instance.pk}',
                sql,
            )
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
            table = NSOVLANState._meta.db_table
            selects = [
                query["sql"]
                for query in captured.captured_queries
                if query["sql"].lstrip().upper().startswith("SELECT") and f'FROM "{table}"' in query["sql"]
            ]
            self.assertEqual(len(selects), 3 * rows + 3)
            return len(captured.captured_queries)

        measure(1)
        two, three, four = measure(2), measure(3), measure(4)

        # One device scan builds the plan; revalidating a planned row is O(1), so the cost
        # per extra owned overlay is constant. A per-row re-scan makes it grow.
        self.assertEqual(four - three, three - two)

    def test_native_create_planning_batches_manifest_and_overlay_reads(self):
        from cProfile import Profile

        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.ownership_planner import _native_create_actions, converted_scope_rules

        def measure(rows):
            device, _management = make_managed(f"owncreate{rows}", 16300 + rows, index=rows)
            group = VLANGroup.objects.create(name=f"Ownership create {rows}", slug=f"nso-{device.pk}")
            for index in range(rows):
                VLAN.objects.create(group=group, vid=1760 + index, name=f"ownership-create-{rows}-{index}")
            with Profile() as profile, CaptureQueriesContext(connection) as captured:
                planned = _native_create_actions(device.pk, frozenset({"vlan"}))
            self.assertEqual(len(planned), rows)
            rule_calls = sum(
                entry.callcount for entry in profile.getstats() if entry.code is converted_scope_rules.__code__
            )
            self.assertEqual(rule_calls, 1)
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

    def test_reowned_interface_overlay_does_not_copy_native_metadata(self):
        from core.models import ObjectType
        from dcim.models import Interface
        from extras.choices import CustomFieldTypeChoices
        from extras.models import CustomField

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        interface = Interface.objects.create(
            device=self.device,
            name="Ethernet2.1",
            type="1000base-t",
            mtu=9216,
        )
        reconcile_scope_ownership(self.device.pk, ["interface_mtu"])
        original = NSOInterfaceMtuState.objects.get(interface=interface)
        custom_field = CustomField.objects.create(
            name="ownership_native_note",
            label="Ownership native note",
            type=CustomFieldTypeChoices.TYPE_TEXT,
        )
        custom_field.object_types.add(ObjectType.objects.get_for_model(Interface))
        interface.custom_field_data = {custom_field.name: "native only"}
        interface.save(update_fields=["custom_field_data"])
        original.delete()

        completed = reconcile_scope_ownership(self.device.pk, ["interface_mtu"])

        replacement = NSOInterfaceMtuState.objects.get(interface=interface)
        interface.refresh_from_db()
        self.assertEqual(interface.custom_field_data, {custom_field.name: "native only"})
        self.assertEqual(replacement.custom_field_data, {})
        self.assertEqual((replacement.l2_mtu, replacement.status), (9216, "accepted"))
        replacement.full_clean()
        self.assertIn(("interface_mtu", replacement.pk), completed)

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

    def test_malformed_bgp_identity_does_not_abort_ownership(self):
        from netbox_nso_plugin.models import NSOBGPPeerState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        malformed_peer, other_peer = self._make_native_bgp_peers(
            "198.18.173.2/32",
            "198.18.173.3/32",
        )
        malformed = NSOBGPPeerState.objects.create(
            management=self.management,
            bgp_peer=malformed_peer,
            asn_str="invalid",
            vrf_name="",
            peer_address_str="198.18.173.2",
            status="imported",
        )
        cases = (
            ("nonnumeric ASN", "invalid", "198.18.173.2"),
            ("out-of-range ASN", "0", "198.18.173.2"),
            ("invalid address", "64520", "not-an-address"),
        )

        for label, asn_str, peer_address_str in cases:
            with self.subTest(label=label):
                NSOBGPPeerState.objects.filter(pk=malformed.pk).update(
                    asn_str=asn_str,
                    peer_address_str=peer_address_str,
                )
                malformed.refresh_from_db()
                row_before = NSOBGPPeerState.objects.values().get(pk=malformed.pk)

                reconcile_scope_ownership(self.device.pk, ["bgp"])

                malformed.refresh_from_db()
                self.assertEqual(NSOBGPPeerState.objects.values().get(pk=malformed.pk), row_before)
                self.assertFalse(
                    NSOOwnershipManifest.objects.filter(
                        device_id=self.device.pk,
                        scope="bgp",
                        state_model_label=malformed._meta.label_lower,
                        state_key={
                            "asn_str": asn_str,
                            "vrf_name": "",
                            "peer_address_str": peer_address_str,
                        },
                    ).exists()
                )
                canonical = NSOBGPPeerState.objects.get(
                    management=self.management,
                    bgp_peer=malformed_peer,
                    asn_str="64520",
                    vrf_name="",
                    peer_address_str="198.18.173.2",
                )
                sibling = NSOBGPPeerState.objects.get(
                    management=self.management,
                    bgp_peer=other_peer,
                    asn_str="64520",
                    vrf_name="",
                    peer_address_str="198.18.173.3",
                )
                self.assertEqual((canonical.status, sibling.status), ("accepted", "accepted"))
                self.assertEqual(
                    NSOOwnershipManifest.objects.filter(
                        device_id=self.device.pk,
                        scope="bgp",
                        ownership_state="owned",
                    ).count(),
                    2,
                )

    def test_owned_malformed_bgp_identity_is_demoted_without_blocking_a_valid_sibling(self):
        from netbox_nso_plugin.models import NSOBGPPeerState, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        nonnumeric_peer, range_peer, address_peer, valid_peer = self._make_native_bgp_peers(
            "198.18.173.4/32",
            "198.18.173.6/32",
            "198.18.173.7/32",
            "198.18.173.5/32",
        )
        malformed = (
            NSOBGPPeerState.objects.create(
                management=self.management,
                bgp_peer=nonnumeric_peer,
                asn_str="invalid",
                vrf_name="",
                peer_address_str="198.18.173.4",
                status="accepted",
            ),
            NSOBGPPeerState.objects.create(
                management=self.management,
                bgp_peer=range_peer,
                asn_str="0",
                vrf_name="",
                peer_address_str="198.18.173.6",
                status="accepted",
            ),
            NSOBGPPeerState.objects.create(
                management=self.management,
                bgp_peer=address_peer,
                asn_str="64520",
                vrf_name="",
                peer_address_str="not-an-address",
                status="accepted",
            ),
        )
        valid = NSOBGPPeerState.objects.create(
            management=self.management,
            bgp_peer=valid_peer,
            asn_str="64520",
            vrf_name="",
            peer_address_str="198.18.173.5",
            status="accepted",
        )

        completed = reconcile_scope_ownership(self.device.pk, ["bgp"])

        for state in malformed:
            state.refresh_from_db()
        valid.refresh_from_db()
        self.assertEqual([state.status for state in malformed], ["imported"] * 3)
        self.assertEqual(valid.status, "accepted")
        for state in malformed:
            self.assertFalse(
                NSOOwnershipManifest.objects.filter(
                    device_id=self.device.pk,
                    state_model_label=state._meta.label_lower,
                    state_key={
                        "asn_str": state.asn_str,
                        "vrf_name": state.vrf_name,
                        "peer_address_str": state.peer_address_str,
                    },
                ).exists()
            )
        self.assertTrue(
            NSOOwnershipManifest.objects.filter(
                device_id=self.device.pk,
                state_model_label=valid._meta.label_lower,
                state_key={
                    "asn_str": "64520",
                    "vrf_name": "",
                    "peer_address_str": "198.18.173.5",
                },
                ownership_state="owned",
            ).exists()
        )
        for state in malformed:
            self.assertIn(("bgp", state.pk), completed)
        self.assertIn(("bgp", valid.pk), completed)

    def test_native_routing_graph_creates_every_owned_overlay(self):
        from dcim.models import Device, Interface
        from django.contrib.contenttypes.models import ContentType
        from django.db import transaction
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import (
            BGPPeer,
            BGPRouter,
            BGPScope,
            ISISFlexAlgo,
            ISISInstance,
            ISISInterface,
            OSPFArea,
            OSPFInstance,
            OSPFInterface,
            Redistribution,
        )

        from netbox_nso_plugin.models import (
            NSOBGPPeerState,
            NSOISISFlexAlgoState,
            NSOISISInstanceState,
            NSOISISInterfaceState,
            NSOOSPFInstanceState,
            NSOOSPFInterfaceState,
            NSOOwnershipManifest,
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
        flex_algo = ISISFlexAlgo.objects.create(
            instance=isis_instance,
            algo_id=173,
            metric_type="delay-metric",
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

        reconcile_scope_ownership(self.device.pk, ["bgp", "isis", "isis_flex_algo", "ospf"])

        self.assertEqual(NSOBGPPeerState.objects.get(bgp_peer=peer).status, "accepted")
        self.assertEqual(NSOISISInstanceState.objects.get(isis_instance=isis_instance).status, "accepted")
        self.assertEqual(NSOISISInterfaceState.objects.get(isis_interface=isis_interface).metric, 17)
        self.assertEqual(NSOISISFlexAlgoState.objects.get(isis_flex_algo=flex_algo).status, "accepted")
        ospf_state = NSOOSPFInstanceState.objects.get(ospf_instance=ospf_instance)
        self.assertEqual(ospf_state.router_id, "198.18.173.1")
        self.assertEqual(ospf_state.areas, [{"area-id": "0.0.0.0", "area-type": "standard"}])
        self.assertEqual(NSOOSPFInterfaceState.objects.get(interface=interface).cost, 17)
        self.assertEqual(NSORedistributionState.objects.get(redistribution=redistribution).dest_protocol, "isis")

        cases = (
            ("BGP peer", NSOBGPPeerState.objects.get(bgp_peer=peer), "bgp_peer", peer, {"bgp"}),
            (
                "redistribution",
                NSORedistributionState.objects.get(redistribution=redistribution),
                "redistribution",
                redistribution,
                {"isis"},
            ),
            (
                "ISIS interface",
                NSOISISInterfaceState.objects.get(isis_interface=isis_interface),
                "isis_interface",
                isis_interface,
                {"isis"},
            ),
            (
                "ISIS instance",
                NSOISISInstanceState.objects.get(isis_instance=isis_instance),
                "isis_instance",
                isis_instance,
                {"isis"},
            ),
            (
                "Flex-Algo",
                NSOISISFlexAlgoState.objects.get(isis_flex_algo=flex_algo),
                "isis_flex_algo",
                flex_algo,
                {"isis_flex_algo"},
            ),
            (
                "OSPF instance",
                NSOOSPFInstanceState.objects.get(ospf_instance=ospf_instance),
                "ospf_instance",
                ospf_instance,
                {"ospf"},
            ),
        )
        for label, state, native_field, native, scopes in cases:
            with self.subTest(label=label), transaction.atomic():
                model = type(state)
                natural_key = tuple(model._meta.unique_together[0])
                self.assertNotIn(native_field, natural_key)
                natural_filter = {
                    model._meta.get_field(name).attname: getattr(state, model._meta.get_field(name).attname)
                    for name in natural_key
                }
                model.objects.filter(pk=state.pk).update(
                    status="imported",
                    accepted_at=None,
                    **{model._meta.get_field(native_field).attname: None},
                )
                NSOOwnershipManifest.objects.filter(
                    device_id=self.device.pk,
                    native_id=native.pk,
                    state_model_label=state._meta.label_lower,
                ).delete()

                reconcile_scope_ownership(self.device.pk, scopes)

                current = model.objects.get(pk=state.pk)
                self.assertEqual(current.status, "imported")
                self.assertIsNone(getattr(current, model._meta.get_field(native_field).attname))
                self.assertEqual(model.objects.filter(**natural_filter).count(), 1)
                self.assertFalse(
                    NSOOwnershipManifest.objects.filter(
                        device_id=self.device.pk,
                        native_id=native.pk,
                        state_model_label=state._meta.label_lower,
                    ).exists()
                )

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


class TestOwnershipActionRecheck(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        self.device = make_device("ownership-recheck")
        instance = NSOInstance.objects.create(
            name="ownership-recheck-instance",
            adapter_instance_id="ownership-recheck-instance",
        )
        self.management = NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=instance,
            nso_device_name="ownership-recheck-device",
            adapter_device_id=16321,
            onboard_status="provisioning",
        )
        NSODeviceManagement.objects.filter(pk=self.management.pk).update(onboard_status="")
        self.management.refresh_from_db()

    def _mtu_state(self, *, native_mtu, status, manifest):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.ownership_planner import maintain_manifest
        from netbox_nso_plugin.signals import suppress_intent_push

        with suppress_intent_push():
            interface = Interface.objects.create(
                device=self.device,
                name=f"Ethernet{NSOInterfaceMtuState.objects.count() + 50}",
                type="1000base-t",
                mtu=native_mtu,
            )
            state = NSOInterfaceMtuState.objects.create(
                management=self.management,
                interface=interface,
                l2_mtu=9216,
                status=status,
            )
        if manifest:
            maintain_manifest(state)
        return interface, state

    def _accept_mtu(self, state):
        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.views import NSOInterfaceMtuStateAcceptView

        current = NSOInterfaceMtuState.objects.select_related("interface", "management").get(pk=state.pk)
        with suppress_intent_push():
            NSOInterfaceMtuStateAcceptView._accept(current)

    def _run_after_plan(self, *, planner_name, scope, matches_action, accept):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connections

        from netbox_nso_plugin import ownership_planner

        original = getattr(ownership_planner, planner_name)
        plan_ready = threading.Event()
        resume_reconciliation = threading.Event()

        def barrier(*args, **kwargs):
            planned = original(*args, **kwargs)
            self.assertTrue(any(matches_action(entry) for entry in planned))
            plan_ready.set()
            if not resume_reconciliation.wait(30):
                raise AssertionError("the ownership reconciliation was not released")
            return planned

        def reconcile():
            try:
                ownership_planner.reconcile_scope_ownership(self.device.pk, [scope])
            finally:
                connections.close_all()

        with patch.object(ownership_planner, planner_name, new=barrier), ThreadPoolExecutor(max_workers=1) as workers:
            reconciliation = workers.submit(reconcile)
            try:
                self.assertTrue(plan_ready.wait(15), "ownership planning did not reach the barrier")
                accept()
            finally:
                resume_reconciliation.set()
            reconciliation.result(timeout=30)

    def _assert_mtu_ownership_survives(self, interface, state):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOOwnershipManifest
        from netbox_nso_plugin.status_machine import is_owned

        interface.refresh_from_db()
        state.refresh_from_db()
        manifest = NSOOwnershipManifest.objects.get(
            device_id=self.device.pk,
            scope="interface_mtu",
            state_model_label=state._meta.label_lower,
        )
        self.assertEqual(interface.mtu, 9216)
        self.assertTrue(is_owned(state.status))
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertIn(
            {
                "interface_name": interface.name,
                "mtu": 9216,
                "ip_mtu": None,
                "mpls_mtu": None,
            },
            delivery.render("interface_mtu", self.device.pk, self.management.adapter_device_id).payload,
        )
        self.assertFalse(
            NSOIntentOutboxEntry.objects.filter(
                device=self.device,
                scope="interface_mtu",
                mark_and=True,
            ).exists()
        )

    def test_accept_after_manifest_less_demotion_plan_preserves_ownership(self):
        from netbox_nso_plugin.ownership_planner import OwnershipAction

        interface, state = self._mtu_state(native_mtu=None, status="accepted", manifest=False)

        self._run_after_plan(
            planner_name="_manifest_record_actions",
            scope="interface_mtu",
            matches_action=lambda entry: entry[0] is OwnershipAction.RETRACT,
            accept=lambda: self._accept_mtu(state),
        )

        self._assert_mtu_ownership_survives(interface, state)

    def test_accept_after_retract_plan_preserves_ownership(self):
        from netbox_nso_plugin.ownership_planner import OwnershipAction

        interface, state = self._mtu_state(native_mtu=9216, status="accepted", manifest=True)
        type(interface).objects.filter(pk=interface.pk).update(mtu=None)

        self._run_after_plan(
            planner_name="_manifest_lifecycle_actions",
            scope="interface_mtu",
            matches_action=lambda entry: entry[-1] is OwnershipAction.RETRACT,
            accept=lambda: self._accept_mtu(state),
        )

        self._assert_mtu_ownership_survives(interface, state)

    def test_accept_after_detach_plan_preserves_ownership(self):
        from netbox_nso_plugin.ownership_planner import OwnershipAction

        interface, state = self._mtu_state(native_mtu=9216, status="accepted", manifest=True)
        type(state).objects.filter(pk=state.pk).update(status="imported", accepted_at=None)

        self._run_after_plan(
            planner_name="_manifest_lifecycle_actions",
            scope="interface_mtu",
            matches_action=lambda entry: entry[-1] is OwnershipAction.DETACH,
            accept=lambda: self._accept_mtu(state),
        )

        self._assert_mtu_ownership_survives(interface, state)

    def test_accept_after_retire_plan_preserves_recreated_ownership(self):
        from dcim.models import Interface
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOBFDInterfaceState, NSOIntentOutboxEntry, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import OwnershipAction, maintain_manifest
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.status_machine import is_owned
        from netbox_nso_plugin.views import NSOBFDInterfaceStateAcceptView

        interface = Interface.objects.create(device=self.device, name="Ethernet90", type="1000base-t")
        with suppress_intent_push():
            state = NSOBFDInterfaceState.objects.create(
                management=self.management,
                interface=interface,
                min_tx=300,
                min_rx=300,
                multiplier=3,
                status="accepted",
            )
        maintain_manifest(state)
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="bfd")
        with suppress_intent_push():
            NSOBFDInterfaceState.objects.filter(pk=state.pk).delete()
        replacement = []

        def recreate_and_accept():
            with suppress_intent_push():
                current = NSOBFDInterfaceState.objects.create(
                    management=self.management,
                    interface=interface,
                    min_tx=300,
                    min_rx=300,
                    multiplier=3,
                    status="imported",
                )
                request = RequestFactory().post("/")
                request.session = {}
                request._messages = FallbackStorage(request)
                response = NSOBFDInterfaceStateAcceptView().post(request, current.pk)
            self.assertEqual(response.status_code, 302)
            replacement.append(current.pk)

        self._run_after_plan(
            planner_name="_manifest_lifecycle_actions",
            scope="bfd",
            matches_action=lambda entry: entry[-1] is OwnershipAction.RETIRE,
            accept=recreate_and_accept,
        )

        current = NSOBFDInterfaceState.objects.get(pk=replacement[0])
        manifest.refresh_from_db()
        self.assertTrue(is_owned(current.status))
        self.assertEqual(manifest.ownership_state, "owned")
        self.assertIn(
            {
                "interface_name": interface.name,
                "min_tx": 300,
                "min_rx": 300,
                "multiplier": 3,
                "micro_bfd": False,
            },
            delivery.render("bfd", self.device.pk, self.management.adapter_device_id).payload,
        )
        self.assertFalse(
            NSOIntentOutboxEntry.objects.filter(
                device=self.device,
                scope="bfd",
                mark_and=True,
            ).exists()
        )
