# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Native endpoint effects include consumers on other managed devices."""

from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from ipam.models import ASN, RIR, VLAN, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPRouter, BGPScope, StaticRoute

from netbox_nso_plugin.models import (
    NSOBGPPeerState,
    NSODeviceManagement,
    NSOInterfaceIPState,
    NSORoutePolicyState,
    NSOStaticRouteState,
    NSOVLANState,
)
from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

from ._authority_case import AdapterCase, grant
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_bgp_router, acquire_overlay


class TestSharedNativeIpRequests(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("native-ip-local", 17301)
        cls.remote_device, cls.remote_management = make_managed("native-ip-consumer", 17302)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet1", type="1000base-t")
        cls.native = IPAddress.objects.create(address="198.18.30.1/31", assigned_object=cls.interface)
        cls.native.refresh_from_db()
        rir = RIR.objects.create(name="Native consumers", slug="native-consumers", is_private=True)
        cls.asn = ASN.objects.create(asn=64530, rir=rir)
        cls.router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=cls.remote_device.pk,
            asn=cls.asn,
        )
        scope = BGPScope.objects.create(router=cls.router)
        BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        peer_ip = IPAddress.objects.create(address="198.18.30.2/32")
        cls.peer = BGPPeer.objects.create(scope=scope, peer=peer_ip, source=cls.native)
        with without_commit_drain():
            acquire_bgp_router(cls.remote_management, cls.router)
            cls.remote_state = acquire_overlay(
                NSOBGPPeerState,
                management=cls.remote_management,
                asn_str=str(cls.asn.asn),
                peer_address_str="198.18.30.2",
                bgp_peer=cls.peer,
                status="in_sync",
            )
            cls.state = acquire_overlay(
                NSOInterfaceIPState,
                interface=cls.interface,
                address="198.18.30.1/31",
                family="ipv4",
                status="in_sync",
            )

    def _post(self, *, superuser, address="198.18.30.3/31"):
        user = get_user_model().objects.create_user(username="native-ip-editor", is_superuser=superuser)
        grant(user, ("view",), Device)
        grant(user, ("view", "change"), NSODeviceManagement, {"pk": self.management.pk})
        grant(user, ("view", "change"), IPAddress)
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", args=(self.state.pk,)),
            {"address": address},
        )

    def test_superuser_can_edit_an_address_used_by_another_management(self):
        response = self._post(superuser=True)
        self.assertEqual(response.status_code, 200)
        self.native.refresh_from_db()
        self.remote_state.refresh_from_db()
        self.assertEqual(str(self.native.address), "198.18.30.3/31")
        self.assertEqual(self.remote_state.status, "accepted")

    def test_target_only_change_refuses_an_address_used_by_another_management(self):
        with self.assert_refused_whole():
            response = self._post(superuser=False)
        self.assertEqual(response.status_code, 404)
        self.native.refresh_from_db()
        self.state.refresh_from_db()
        self.remote_state.refresh_from_db()
        self.assertEqual(str(self.native.address), "198.18.30.1/31")
        self.assertEqual(self.state.status, "in_sync")
        self.assertEqual(self.remote_state.status, "in_sync")

    def test_prefix_length_edit_keeps_the_remote_consumer_in_sync(self):
        response = self._post(superuser=False, address="198.18.30.1/32")
        self.assertEqual(response.status_code, 200)
        self.native.refresh_from_db()
        self.remote_state.refresh_from_db()
        self.assertEqual(str(self.native.address), "198.18.30.1/32")
        self.assertEqual(self.remote_state.status, "in_sync")


class TestTargetLocalSharedAcceptRequests(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("local-accept", 17303)
        cls.remote_device, cls.remote_management = make_managed("remote-accept", 17304)
        cls.route = StaticRoute.objects.create(prefix="198.18.31.0/24", next_hop="198.18.32.1")
        cls.route.devices.add(cls.device, cls.remote_device)
        cls.vlan = VLAN.objects.create(vid=230, name="shared-accept")
        with without_commit_drain():
            for management in (cls.management, cls.remote_management):
                acquire_overlay(
                    NSOStaticRouteState,
                    management=management,
                    static_route=cls.route,
                    nso_prefix="198.18.31.0/24",
                    nso_next_hop="198.18.32.1",
                    status="imported",
                )
                acquire_overlay(NSOVLANState, management=management, vlan=cls.vlan, status="imported")
            for device, prefix in ((cls.remote_device, "198.18.33.0/24"), (cls.device, "198.18.34.0/24")):
                reconcile_route_policy(
                    device,
                    {
                        "prefix_lists": [
                            {
                                "name": "SHARED-ACCEPT",
                                "family": 4,
                                "entries": [{"sequence": 10, "action": "permit", "prefix": prefix}],
                            }
                        ]
                    },
                )

    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user(username="target-accept")
        grant(user, ("view",), Device)
        grant(user, ("view", "change"), NSODeviceManagement, {"pk": self.management.pk})
        self.client.force_login(user)

    def _assert_target_local_accept(self, model, route, *, bulk=False):
        local = model.objects.get(management=self.management)
        remote_before = list(model.objects.filter(management=self.remote_management).values())
        key = self.device.pk if bulk else local.pk
        response = self.client.post(reverse(f"plugins:netbox_nso_plugin:{route}", args=(key,)))
        self.assertEqual(response.status_code, 302)
        local.refresh_from_db()
        self.assertIn(local.status, {"accepted", "in_sync"})
        self.assertIsNotNone(local.accepted_at)
        self.assertEqual(list(model.objects.filter(management=self.remote_management).values()), remote_before)

    def test_policy_accept_keeps_the_other_management_materialized_owner(self):
        self._assert_target_local_accept(NSORoutePolicyState, "routing_accept_route_policy")
        self.assertTrue(NSORoutePolicyState.objects.get(management=self.remote_management).is_materialized)

    def test_policy_bulk_accept_keeps_the_other_management_materialized_owner(self):
        self._assert_target_local_accept(NSORoutePolicyState, "routing_bulk_accept_route_policy", bulk=True)
        self.assertTrue(NSORoutePolicyState.objects.get(management=self.remote_management).is_materialized)

    def test_static_route_accept_keeps_the_other_management_generation(self):
        self._assert_target_local_accept(NSOStaticRouteState, "routing_accept_static_route")

    def test_static_route_bulk_accept_keeps_the_other_management_generation(self):
        self._assert_target_local_accept(NSOStaticRouteState, "routing_bulk_accept_static_routes", bulk=True)

    def test_vlan_accept_keeps_the_other_management_attachment(self):
        self._assert_target_local_accept(NSOVLANState, "vlan_accept")
