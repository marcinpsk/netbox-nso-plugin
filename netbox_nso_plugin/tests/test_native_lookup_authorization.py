# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Operator-selected native dependencies require view permission on the selected row."""

from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from ipam.models import ASN, RIR, VRF, IPAddress
from netbox_routing.models import (
    BFDInterface,
    BFDProfile,
    BGPAddressFamily,
    BGPPeer,
    BGPRouter,
    BGPScope,
    ISISInstance,
    ISISInterface,
    OSPFArea,
    OSPFInstance,
    OSPFInterface,
    Redistribution,
    RouteMap,
)
from utilities.exceptions import PermissionsViolation

from netbox_nso_plugin.intent_state import offline_mutation
from netbox_nso_plugin.models import (
    NSOBFDInterfaceState,
    NSOBGPPeerState,
    NSODeviceManagement,
    NSOInterfaceIPState,
    NSOISISInterfaceState,
    NSOOSPFInstanceState,
    NSOOSPFInterfaceState,
    NSORedistributionState,
)
from netbox_nso_plugin.native_authorization import authority_scope
from netbox_nso_plugin.ownership_grants import OwnershipGrant
from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_mirror_writes

from ._authority_case import AdapterCase, FakeAdapter, grant, operator
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from ._routing_observation_case import store_redistribution_observation


class TestStoredInterfaceIpVrf(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("stored-ip-vrf", 17217)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet1", type="1000base-t")
        cls.vrf = VRF.objects.create(name="stored-ip-vrf")
        cls.native = IPAddress.objects.create(address="198.18.40.1/31", vrf=cls.vrf, assigned_object=cls.interface)
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSOInterfaceIPState,
                interface=cls.interface,
                address="198.18.40.1/31",
                vrf=cls.vrf.name,
                family="ipv4",
                status="in_sync",
            )

    def test_address_edit_preserves_stored_vrf_without_view_permission(self):
        user = get_user_model().objects.create_user(username="stored-ip-vrf-editor")
        grant(user, ("view",), Device, {"pk": self.device.pk})
        grant(user, ("view", "change"), NSODeviceManagement, {"pk": self.management.pk})
        grant(user, ("view", "change"), IPAddress, {"pk": self.native.pk})
        self.assertFalse(user.has_perm("ipam.view_vrf"))
        self.assertFalse(VRF.objects.restrict(user, "view").filter(pk=self.vrf.pk).exists())
        self.client.force_login(user)

        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", args=(self.state.pk,)),
            {"address": "198.18.40.3/31"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.native.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(str(self.native.address), "198.18.40.3/31")
        self.assertEqual(self.native.vrf_id, self.vrf.pk)
        self.assertEqual(self.native.assigned_object, self.interface)
        self.assertEqual(self.state.address, "198.18.40.3/31")
        self.assertEqual(self.state.vrf, self.vrf.name)
        self.assertEqual(self.state.status, "accepted")


class TestBfdProfileLookup(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-bfd", 17211)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet1", type="1000base-t")
        cls.profile = BFDProfile.objects.create(name="bfd-310-320-x3", min_tx_int=310, min_rx_int=320, multiplier=3)
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSOBFDInterfaceState,
                management=cls.management,
                interface=cls.interface,
                min_tx=310,
                min_rx=320,
                multiplier=3,
                status="accepted",
            )

    def _accept(self, user):
        self.client.force_login(user)
        return self.client.post(reverse("plugins:netbox_nso_plugin:bfd_accept", kwargs={"pk": self.state.pk}))

    def test_hidden_profile_refuses_create_without_a_duplicate(self):
        user = operator((("add",), BFDInterface, None), (("add",), BFDProfile, None))
        with self.assert_refused_whole():
            response = self._accept(user)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BFDInterface.objects.filter(interface=self.interface).exists())
        self.assertEqual(BFDProfile.objects.filter(name=self.profile.name).count(), 1)

    def test_visible_profile_is_reused_without_add_permission(self):
        user = operator((("add",), BFDInterface, None), (("view",), BFDProfile, {"pk": self.profile.pk}))
        response = self._accept(user)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(BFDInterface.objects.get(interface=self.interface).bfd_profile_id, self.profile.pk)

    def test_view_permission_excluding_the_profile_refuses_create(self):
        user = operator(
            (("add",), BFDInterface, None),
            (("add",), BFDProfile, None),
            (("view",), BFDProfile, {"name": "another-profile"}),
        )
        with self.assert_refused_whole():
            response = self._accept(user)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BFDInterface.objects.filter(interface=self.interface).exists())

    def test_accept_preserves_a_stored_profile_without_view_permission(self):
        native = BFDInterface.objects.create(interface=self.interface, bfd_profile=self.profile, enabled=True)
        response = self._accept(operator())
        self.assertEqual(response.status_code, 302, response.content)
        native.refresh_from_db()
        self.assertEqual(native.bfd_profile_id, self.profile.pk)

    def test_edit_preserves_a_stored_profile_without_view_permission(self):
        native = BFDInterface.objects.create(interface=self.interface, bfd_profile=self.profile, enabled=True)
        self.client.force_login(operator((("change",), BFDInterface, None)))
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "bfd", "pk": self.state.pk}),
            {"micro_bfd": "True"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        native.refresh_from_db()
        self.assertEqual(native.bfd_profile_id, self.profile.pk)
        self.assertTrue(native.micro_bfd)

    def test_refresh_reuses_an_observed_profile_without_view_permission(self):
        observed = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t")
        FakeAdapter.documents = {
            "/bfd": {"interfaces": [{"interface_name": observed.name, "min_tx": 310, "min_rx": 320, "multiplier": 3}]}
        }
        self.client.force_login(operator())
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device.pk, "key": "bfd"}),
            {"refresh": "1"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(BFDInterface.objects.get(interface=observed).bfd_profile_id, self.profile.pk)


class TestIsisProcessLookup(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-isis", 17212)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet1", type="1000base-t")
        cls.current = ISISInstance.objects.create(device=cls.device, process_tag="CURRENT")
        cls.target = ISISInstance.objects.create(device=cls.device, process_tag="TARGET")
        cls.native = ISISInterface.objects.create(
            interface=cls.interface, address_family="ipv4", instance=cls.current, circuit_type="level-2-only"
        )
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSOISISInterfaceState,
                management=cls.management,
                interface=cls.interface,
                isis_interface=cls.native,
                af="ipv4",
                process_tag="TARGET",
                circuit_type="level-2-only",
                status="accepted",
            )

    def _accept(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_accept_isis_interface", kwargs={"pk": self.state.pk})
        )

    def test_hidden_process_refuses_create_without_a_duplicate(self):
        user = operator((("change",), ISISInterface, None), (("add",), ISISInstance, None))
        with self.assert_refused_whole():
            response = self._accept(user)
        self.assertEqual(response.status_code, 403)
        self.native.refresh_from_db()
        self.assertEqual(self.native.instance_id, self.current.pk)
        self.assertEqual(ISISInstance.objects.filter(device=self.device, process_tag="TARGET").count(), 1)

    def test_visible_process_is_reused_without_add_permission(self):
        user = operator((("change",), ISISInterface, None), (("view",), ISISInstance, {"pk": self.target.pk}))
        response = self._accept(user)
        self.assertEqual(response.status_code, 302, response.content)
        self.native.refresh_from_db()
        self.assertEqual(self.native.instance_id, self.target.pk)


class TestOspfVrfLookup(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-ospf", 17213)
        cls.vrf = VRF.objects.create(name="lookup-vrf")
        cls.native = OSPFInstance.objects.create(device=cls.device, process_id="11", name="11", router_id="198.18.0.11")
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSOOSPFInstanceState,
                management=cls.management,
                ospf_instance=cls.native,
                process_id="11",
                router_id="198.18.0.11",
                vrf=cls.vrf.name,
                status="imported",
            )

    def _edit(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "ospf_instance", "pk": self.state.pk}
            ),
            {"router_id": "198.18.0.12"},
        )

    def test_hidden_vrf_has_the_same_validation_as_a_missing_vrf(self):
        user = operator((("change",), OSPFInstance, None))
        hidden = self._edit(user)
        self.assertEqual(hidden.status_code, 400, hidden.content)
        self.native.refresh_from_db()
        self.assertEqual(str(self.native.router_id), "198.18.0.11")
        self.vrf.delete()
        missing = self._edit(user)
        self.assertEqual(missing.status_code, hidden.status_code)
        self.assertEqual(missing.json(), hidden.json())

    def test_visible_vrf_is_selected(self):
        user = operator((("change",), OSPFInstance, None), (("view",), VRF, {"pk": self.vrf.pk}))
        response = self._edit(user)
        self.assertEqual(response.status_code, 200, response.content)
        self.native.refresh_from_db()
        self.assertEqual(self.native.vrf_id, self.vrf.pk)


class TestOspfAreaLookup(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-area", 17214)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet1", type="1000base-t")
        cls.instance = OSPFInstance.objects.create(
            device=cls.device, process_id="11", name="11", router_id="198.18.0.11"
        )
        cls.current = OSPFArea.objects.create(area_id="0.0.0.0", area_type="standard")
        cls.target = OSPFArea.objects.create(area_id="0.0.0.1", area_type="standard")
        cls.native = OSPFInterface.objects.create(
            interface=cls.interface, instance=cls.instance, area=cls.current, network_type="broadcast", cost=10
        )
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSOOSPFInterfaceState,
                management=cls.management,
                interface=cls.interface,
                process_id="11",
                area_id="0.0.0.0",
                network_type="broadcast",
                cost=10,
                status="imported",
            )

    def _edit(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "ospf_interface", "pk": self.state.pk}
            ),
            {"area_id": "0.0.0.1"},
        )

    def test_hidden_area_refuses_create_without_a_duplicate(self):
        user = operator((("change",), OSPFInterface, None), (("view",), OSPFInstance, None), (("add",), OSPFArea, None))
        with self.assert_refused_whole():
            response = self._edit(user)
        self.assertEqual(response.status_code, 403)
        self.native.refresh_from_db()
        self.assertEqual(self.native.area_id, self.current.pk)
        self.assertEqual(OSPFArea.objects.filter(area_id="0.0.0.1").count(), 1)

    def test_visible_area_is_reused_without_add_permission(self):
        user = operator(
            (("change",), OSPFInterface, None),
            (("view",), OSPFInstance, None),
            (("view",), OSPFArea, {"pk": self.target.pk}),
        )
        response = self._edit(user)
        self.assertEqual(response.status_code, 200, response.content)
        self.native.refresh_from_db()
        self.assertEqual(self.native.area_id, self.target.pk)

    def test_edit_preserves_stored_process_and_area_without_view_permission(self):
        self.client.force_login(operator((("change",), OSPFInterface, None)))
        response = self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "ospf_interface", "pk": self.state.pk}
            ),
            {"cost": "20"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.native.refresh_from_db()
        self.assertEqual(
            (self.native.instance_id, self.native.area_id, self.native.cost), (self.instance.pk, self.current.pk, 20)
        )


class TestBgpAsnLookup(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-asn", 17215)
        cls.rir = RIR.objects.create(name="NSO Auto-Discovered", slug="nso-auto-discovered", is_private=True)
        cls.local_asn = ASN.objects.create(asn=64520, rir=cls.rir)
        cls.target_asn = ASN.objects.create(asn=64521, rir=cls.rir)
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(cls.device),
            assigned_object_id=cls.device.pk,
            asn=cls.local_asn,
        )
        cls.native = BGPPeer.objects.create(
            scope=BGPScope.objects.create(router=router),
            peer=IPAddress.objects.create(address="198.18.0.20/32"),
            remote_as=cls.local_asn,
            enabled=True,
        )
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSOBGPPeerState,
                management=cls.management,
                asn_str="64520",
                peer_address_str="198.18.0.20",
                bgp_peer=cls.native,
                status="imported",
            )

    def _edit(self, user, number):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "bgp_peer", "pk": self.state.pk}),
            {"remote_as": str(number)},
        )

    def test_hidden_asn_refuses_create_without_a_duplicate(self):
        user = operator((("change",), BGPPeer, None), (("add",), ASN, None), (("view",), RIR, None))
        with self.assert_refused_whole():
            response = self._edit(user, self.target_asn.asn)
        self.assertEqual(response.status_code, 403)
        self.native.refresh_from_db()
        self.assertEqual(self.native.remote_as_id, self.local_asn.pk)
        self.assertEqual(ASN.objects.filter(asn=self.target_asn.asn).count(), 1)

    def test_visible_asn_is_reused_without_add_permission(self):
        user = operator((("change",), BGPPeer, None), (("view",), ASN, {"pk": self.target_asn.pk}))
        response = self._edit(user, self.target_asn.asn)
        self.assertEqual(response.status_code, 200, response.content)
        self.native.refresh_from_db()
        self.assertEqual(self.native.remote_as_id, self.target_asn.pk)

    def test_hidden_rir_refuses_asn_creation_without_a_duplicate(self):
        user = operator((("change",), BGPPeer, None), (("add",), ASN, None), (("add",), RIR, None))
        with self.assert_refused_whole():
            response = self._edit(user, 64522)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ASN.objects.filter(asn=64522).exists())
        self.assertEqual(RIR.objects.filter(name=self.rir.name).count(), 1)

    def test_visible_rir_is_reused_for_a_new_asn(self):
        user = operator((("change",), BGPPeer, None), (("add",), ASN, None), (("view",), RIR, {"pk": self.rir.pk}))
        response = self._edit(user, 64522)
        self.assertEqual(response.status_code, 200, response.content)
        self.native.refresh_from_db()
        self.assertEqual(self.native.remote_as.asn, 64522)
        self.assertEqual(self.native.remote_as.rir_id, self.rir.pk)


class TestConcurrentNativeLookup(AdapterCase):
    def _plan_with_winner(self):
        planned = BFDProfile(name="bfd-510-520-x3", min_tx_int=510, min_rx_int=520, multiplier=3)
        plan = RendererMutationPlan.build(
            grant=OwnershipGrant("create"),
            saves=(planned_save(planned, force_insert=True, natural_key=("name",)),),
        )
        winner = BFDProfile.objects.create(name=planned.name, min_tx_int=510, min_rx_int=520, multiplier=3)
        return plan, planned, winner

    def test_hidden_concurrent_winner_refuses_insert_adoption(self):
        plan, planned, winner = self._plan_with_winner()
        user = operator((("add",), BFDProfile, None))
        with self.assertRaises(PermissionsViolation), authority_scope("native_mutation", user):
            with renderer_mirror_writes(plan) as writer:
                writer.save(planned, force_insert=True)
        self.assertEqual(BFDProfile.objects.filter(name=winner.name).count(), 1)

    def test_visible_concurrent_winner_is_reused(self):
        plan, planned, winner = self._plan_with_winner()
        user = operator((("add",), BFDProfile, None), (("view",), BFDProfile, {"pk": winner.pk}))
        with authority_scope("native_mutation", user), renderer_mirror_writes(plan) as writer:
            writer.save(planned, force_insert=True)
        self.assertEqual(planned.pk, winner.pk)

    def test_hidden_concurrent_winner_refuses_existing_creation_adoption(self):
        plan, _planned, winner = self._plan_with_winner()
        user = operator((("add",), BFDProfile, None))
        with self.assertRaises(PermissionsViolation), authority_scope("native_mutation", user):
            with renderer_mirror_writes(plan) as writer:
                writer.consume_existing_creation(winner)


class TestRedistributionDestinationLookup(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-destination", 17216)
        cls.vrf = VRF.objects.create(name="lookup-destination-vrf")
        rir = RIR.objects.create(name="Lookup destination", slug="lookup-destination", is_private=True)
        cls.asn = ASN.objects.create(asn=64523, rir=rir)
        cls.router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(cls.device),
            assigned_object_id=cls.device.pk,
            asn=cls.asn,
        )
        cls.scope = BGPScope.objects.create(router=cls.router, vrf=cls.vrf)
        cls.family = BGPAddressFamily.objects.create(scope=cls.scope, address_family="ipv4-unicast")
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSORedistributionState,
                management=cls.management,
                dest_protocol="bgp",
                dest_ref=f"{cls.asn.asn}/{cls.vrf.name}/ipv4-unicast",
                source_protocol="static",
                status="accepted",
            )
        store_redistribution_observation(cls.management)

    def _user(self, hidden=None):
        grants = [(("add",), Redistribution, None)]
        grants.extend(
            (("view",), model, {"pk": row.pk})
            for model, row in (
                (ASN, self.asn),
                (VRF, self.vrf),
                (BGPRouter, self.router),
                (BGPScope, self.scope),
                (BGPAddressFamily, self.family),
            )
            if model is not hidden
        )
        return operator(*grants)

    def _accept(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", kwargs={"pk": self.state.pk})
        )

    def _assert_hidden_destination_is_missing(self, model):
        response = self._accept(self._user(hidden=model))
        self.assertEqual(response.status_code, 302, response.content)
        self.assertFalse(Redistribution.objects.exists())
        self.state.refresh_from_db()
        self.assertIsNone(self.state.redistribution_id)

    def test_hidden_bgp_router_is_missing(self):
        self._assert_hidden_destination_is_missing(BGPRouter)

    def test_hidden_bgp_scope_is_missing(self):
        self._assert_hidden_destination_is_missing(BGPScope)

    def test_hidden_bgp_address_family_has_the_same_result_as_missing(self):
        hidden = self._accept(self._user(hidden=BGPAddressFamily))
        self.assertEqual(hidden.status_code, 302, hidden.content)
        self.assertFalse(Redistribution.objects.exists())
        with offline_mutation():
            self.family.delete()
        missing = self._accept(self._user())
        self.assertEqual(missing.status_code, hidden.status_code)
        self.assertFalse(Redistribution.objects.exists())
        self.state.refresh_from_db()
        self.assertIsNone(self.state.redistribution_id)

    def test_visible_bgp_destination_is_selected(self):
        response = self._accept(self._user())
        self.assertEqual(response.status_code, 302, response.content)
        native = Redistribution.objects.get()
        self.assertEqual(native.destination_id, self.family.pk)


class TestRedistributionNativeLookup(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-native-redistribution", 17218)
        cls.instance = OSPFInstance.objects.create(
            device=cls.device, process_id="11", name="11", router_id="198.18.0.11"
        )
        cls.native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(OSPFInstance),
            destination_id=cls.instance.pk,
            source_protocol="static",
            source_ref="",
        )
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSORedistributionState,
                management=cls.management,
                dest_protocol="ospf",
                dest_ref="11",
                source_protocol="static",
                source_ref="",
                status="accepted",
            )
        store_redistribution_observation(cls.management)

    def _accept(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_accept_redistribution", kwargs={"pk": self.state.pk})
        )

    def test_hidden_redistribution_refuses_reuse_without_a_duplicate(self):
        user = operator((("view",), OSPFInstance, None), (("add",), Redistribution, None))
        with self.assert_refused_whole():
            response = self._accept(user)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Redistribution.objects.count(), 1)
        self.state.refresh_from_db()
        self.assertIsNone(self.state.redistribution_id)

    def test_visible_redistribution_is_reused_without_add_permission(self):
        user = operator((("view",), OSPFInstance, None), (("view",), Redistribution, {"pk": self.native.pk}))
        response = self._accept(user)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(Redistribution.objects.count(), 1)
        self.state.refresh_from_db()
        self.assertEqual(self.state.redistribution_id, self.native.pk)

    def test_missing_redistribution_requires_add_permission(self):
        with offline_mutation():
            self.native.delete()
        user = operator((("view",), OSPFInstance, None), (("view",), Redistribution, None))
        with self.assert_refused_whole():
            response = self._accept(user)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Redistribution.objects.exists())
        self.state.refresh_from_db()
        self.assertIsNone(self.state.redistribution_id)


class TestStoredRedistributionRouteMap(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("lookup-stored-route-map", 17219)
        instance = OSPFInstance.objects.create(device=cls.device, process_id="12", name="12", router_id="198.18.0.12")
        cls.route_map = RouteMap.objects.create(name="lookup-stored-route-map")
        cls.native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(OSPFInstance),
            destination_id=instance.pk,
            source_protocol="static",
            route_map=cls.route_map,
            metric=10,
        )
        with without_commit_drain():
            cls.state = acquire_overlay(
                NSORedistributionState,
                management=cls.management,
                redistribution=cls.native,
                dest_protocol="ospf",
                dest_ref="12",
                source_protocol="static",
                route_map=cls.route_map.name,
                metric=10,
                status="imported",
            )

    def test_metric_edit_preserves_a_stored_route_map_without_view_permission(self):
        self.client.force_login(operator((("change",), Redistribution, None)))
        response = self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "redistribution", "pk": self.state.pk}
            ),
            {"metric": "20"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.native.refresh_from_db()
        self.assertEqual((self.native.route_map_id, self.native.metric), (self.route_map.pk, 20))
