# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Symmetric ownership execution at the real database seam."""

from unittest.mock import patch

from django.test import TestCase

from ._outbox_case import make_managed, own_route, own_vlan


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

    def test_legacy_manifest_signature_is_upgraded_before_lifecycle_execution(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        state = own_vlan(self.management, 1714, "ownership-signature-upgrade")
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="vlan")
        NSOOwnershipManifest.objects.filter(pk=manifest.pk).update(
            native_id=None,
            state_model_label="",
            state_key={},
        )

        reconcile_scope_ownership(self.device.pk, ["vlan"])

        manifest.refresh_from_db()
        self.assertEqual(manifest.native_id, state.vlan_id)
        self.assertEqual(manifest.state_model_label, "netbox_nso_plugin.nsovlanstate")
        self.assertEqual(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="vlan").count(), 1)

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
        self.assertEqual(NSOOSPFInstanceState.objects.get(ospf_instance=ospf_instance).router_id, "198.18.173.1")
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
