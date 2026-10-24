# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Greenfield peers select existing BGP dependencies through native view permission."""

from dcim.models import Device
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from ipam.models import ASN, RIR, VRF, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPPeerAddressFamily, BGPRouter, BGPScope

from netbox_nso_plugin.models import NSOBGPPeerState, NSOBGPRouterState, NSODeviceManagement

from ._authority_case import AdapterCase, operator
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay


class TestBgpDependencySelection(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("bgp-dependency", 17220)
        NSODeviceManagement.objects.filter(pk=cls.management.pk).update(manage_bgp=True)
        rir = RIR.objects.create(name="BGP dependency", slug="bgp-dependency", is_private=True)
        cls.asn = ASN.objects.create(asn=64524, rir=rir)
        cls.vrf = VRF.objects.create(name="bgp-dependency")
        cls.router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=cls.device.pk,
            asn=cls.asn,
            router_id="198.18.0.24",
        )
        cls.scope = BGPScope.objects.create(router=cls.router, vrf=cls.vrf)
        cls.family = BGPAddressFamily.objects.create(scope=cls.scope, address_family="ipv4-unicast")
        cls.peer_ip = IPAddress.objects.create(address="198.18.0.25/32", vrf=cls.vrf)
        with without_commit_drain():
            acquire_overlay(
                NSOBGPRouterState,
                management=cls.management,
                bgp_router=cls.router,
                asn_str=str(cls.asn.asn),
                router_id="198.18.0.24",
                status="accepted",
            )

    def _user(self, hidden=None):
        grants = [
            (("view",), ASN, None),
            (("view",), VRF, None),
            (("view",), IPAddress, None),
            (("change",), BGPRouter, None),
        ]
        grants.extend(
            (("add",), model, None) for model in (BGPRouter, BGPScope, BGPAddressFamily, BGPPeer, BGPPeerAddressFamily)
        )
        grants.extend(
            (("view",), model, {"pk": row.pk})
            for model, row in ((BGPRouter, self.router), (BGPScope, self.scope), (BGPAddressFamily, self.family))
            if model is not hidden
        )
        return operator(*grants)

    def _post(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:bgp_peer_add", args=(self.device.pk,)),
            {
                "local_asn": self.asn.pk,
                "vrf": self.vrf.pk,
                "peer": self.peer_ip.pk,
                "enabled": "on",
                "address_families": ["ipv4-unicast"],
            },
        )

    def _assert_hidden_dependency_refuses_creation(self, model):
        with self.assert_refused_whole():
            response = self._post(self._user(hidden=model))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BGPRouter.objects.count(), 1)
        self.assertEqual(BGPScope.objects.count(), 1)
        self.assertEqual(BGPAddressFamily.objects.count(), 1)
        self.assertFalse(BGPPeer.objects.exists())
        self.assertFalse(BGPPeerAddressFamily.objects.exists())
        self.assertFalse(NSOBGPPeerState.objects.exists())

    def test_hidden_router_refuses_creation_without_a_duplicate(self):
        self._assert_hidden_dependency_refuses_creation(BGPRouter)

    def test_hidden_scope_refuses_creation_without_a_duplicate(self):
        self._assert_hidden_dependency_refuses_creation(BGPScope)

    def test_hidden_address_family_refuses_creation_without_a_duplicate(self):
        self._assert_hidden_dependency_refuses_creation(BGPAddressFamily)

    def test_visible_dependencies_are_reused(self):
        response = self._post(self._user())
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(BGPRouter.objects.count(), 1)
        self.assertEqual(BGPScope.objects.count(), 1)
        self.assertEqual(BGPAddressFamily.objects.count(), 1)
        peer = BGPPeer.objects.get()
        self.assertEqual(peer.scope_id, self.scope.pk)
        self.assertEqual(BGPPeerAddressFamily.objects.get().address_family_id, self.family.pk)
