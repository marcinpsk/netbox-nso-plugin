# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Peer actions and displays use native intent and published observations."""

from ipaddress import ip_interface
from unittest.mock import patch
from uuid import uuid4

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from ipam.models import ASN, RIR, VRF, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPRouter, BGPScope

from netbox_nso_plugin.delivery import render
from netbox_nso_plugin.models import NSOBGPPeerState
from netbox_nso_plugin.read_gate import gated_family_run

from ._adapter_http import make_apply_preview, make_response, make_transport_session
from ._outbox_case import make_managed, mirror_update, without_commit_drain
from ._ownership_case import acquire_bgp_router, acquire_overlay
from ._routing_observation_case import routing_observation
from ._scope_observation_case import entry
from .test_read_gate import _rs


class TestPeerNativeViews(TestCase):
    def setUp(self):
        self.device, self.management = make_managed(f"peerview{uuid4().hex[:8]}", 1803)
        self.rir = RIR.objects.create(
            name=f"example-{self.device.pk}", slug=f"example-{self.device.pk}", is_private=True
        )
        self.asn = ASN.objects.create(asn=64512, rir=self.rir)
        self.remote = ASN.objects.create(asn=64513, rir=self.rir)
        self.router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=self.asn,
        )
        self.scope = BGPScope.objects.create(router=self.router)
        BGPAddressFamily.objects.create(scope=self.scope, address_family="ipv4-unicast")
        self.ip = IPAddress.objects.create(address="198.18.0.2/32")
        self.peer = BGPPeer.objects.create(scope=self.scope, peer=self.ip, remote_as=self.remote, enabled=False)
        acquire_bgp_router(self.management, self.router)
        self.user = get_user_model().objects.create_superuser(username=f"peerview{uuid4().hex[:8]}")
        self.client.force_login(self.user)

    def state(self, *, status="imported", linked=True, **identity):
        return acquire_overlay(
            NSOBGPPeerState,
            management=self.management,
            asn_str="64512",
            vrf_name="",
            peer_address_str=identity.pop("peer_address_str", "198.18.0.2"),
            bgp_peer=self.peer if linked else None,
            status=status,
            **identity,
        )

    def edit(self, state, values):
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=["bgp_peer", state.pk])
        with without_commit_drain():
            return self.client.post(url, values)

    def grid(self):
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", args=[self.device.pk, "bgp"])
        response = self.client.get(url, {"format": "json"})
        self.assertEqual(response.status_code, 200, response.content)
        return {row["peer_address"]: row for row in response.json()["peers"]["rows"]}

    def preview(self):
        url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        session = make_transport_session(make_response(json_data=make_apply_preview(self.management.adapter_device_id)))
        with patch("requests.Session", new=lambda: session):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["diff_available"])
        return [row for row in response.json()["routing_changes"] if row["category"] == "BGP peer"]

    def publish(self, peers, *, routers=None, routers_null=False, projected_peers=None):
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan

        routers = routers if routers is not None else [entry(asn="0.64512", scope=[entry(vrf="", peer=peers)])]
        document = {"routers": None if routers_null else routers, "present": ["routers"], "unprojectable": []}
        payload = {
            "device_id": self.device.pk,
            "routers": [
                {
                    "asn": router["asn"],
                    "router_id": None,
                    "scopes": [
                        {
                            "vrf": scope.get("vrf") or "",
                            "address_families": [],
                            "peers": projected_peers
                            if projected_peers is not None
                            else [
                                {
                                    "peer_address": ip_interface(peer["peer_address"]).ip.compressed,
                                    "remote_as": peer.get("remote_as"),
                                    "enabled": peer.get("enabled") is not False,
                                    "address_families": [],
                                }
                                for peer in scope.get("peer", [])
                            ],
                            "peer_groups": [],
                        }
                        for scope in router.get("scope", [])
                    ],
                }
                for router in routers
            ],
        }
        observed = routing_observation("bgp", document=document)
        payload["observation"] = observed
        result = gated_family_run(
            self.management,
            "bgp",
            _rs(),
            lambda: _reconcile_bgp_config(self.device, payload),
            pre_body=lambda: bgp_reconcile_plan(self.device, payload),
            epoch=self.management.adapter_device_id,
            observation=observed,
        )
        self.assertEqual(result.disposition, "ran")

    def test_accept_never_uses_historical_agreement_as_current_match(self):
        state = self.state(status="in_sync")
        mirror_update(state, device_base_hash="a" * 64)
        url = reverse("plugins:netbox_nso_plugin:routing_accept_bgp_peer", args=[state.pk])
        with without_commit_drain():
            response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.device_base_hash, "a" * 64)

    def test_bulk_accept_imported_peer_remains_pending(self):
        state = self.state()
        url = reverse("plugins:netbox_nso_plugin:routing_bulk_accept_bgp_peers", args=[self.device.pk])
        with without_commit_drain():
            response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_inline_native_edit_resolves_asn_and_renders_intent(self):
        state = self.state()
        response = self.edit(state, {"remote_as": "64514", "enabled": "True"})
        self.assertEqual(response.status_code, 200, response.content)
        self.peer.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(self.peer.remote_as.asn, 64514)
        self.assertTrue(self.peer.enabled)
        self.assertEqual(state.status, "accepted")
        payload = render("bgp", self.device.pk, self.management.adapter_device_id).payload
        pushed = payload[0]["scopes"][0]["peers"][0]
        self.assertEqual(pushed["remote_as"], "64514")
        self.assertTrue(pushed["enabled"])
        self.assertEqual(self.grid()["198.18.0.2"]["remote_as"], "64514")
        self.assertEqual(self.preview()[0]["detail"], "AS 64514, enabled")

    def test_inline_invalid_asn_does_not_change_native_or_acquire(self):
        state = self.state()
        for raw in ("garbage", "0", "4294967296", "1.2.3"):
            with self.subTest(raw=raw):
                response = self.edit(state, {"remote_as": raw, "enabled": "True"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("remote_as", response.json()["errors"])
        self.peer.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(self.peer.remote_as_id, self.remote.pk)
        self.assertFalse(self.peer.enabled)
        self.assertEqual(state.status, "imported")

    def test_linked_display_reads_native_values(self):
        state = self.state(status="accepted")
        row = self.grid()[state.peer_address_str]
        self.assertEqual(row["remote_as"], "64513")
        self.assertIs(row["enabled"], False)
        self.assertIs(row["disabled"], True)
        self.assertEqual(self.preview()[0]["detail"], "AS 64513, disabled")

    def test_unlinked_display_uses_published_canonical_identity(self):
        state = self.state(linked=False, peer_address_str="2001:db8::2")
        self.publish([entry(peer_address="2001:0db8:0:0:0:0:0:2/128", remote_as="64515", enabled=False)])
        row = self.grid()[state.peer_address_str]
        self.assertEqual(row["remote_as"], "64515")
        self.assertIs(row["enabled"], False)
        self.assertIs(row["disabled"], True)

    def test_unlinked_duplicate_observations_keep_first_scalars_and_any_enabled(self):
        self.assert_duplicate_observations(first_enabled=True, second_enabled=False)

    def test_unlinked_later_enabled_observation_keeps_first_scalars(self):
        self.assert_duplicate_observations(first_enabled=False, second_enabled=True)

    def assert_duplicate_observations(self, *, first_enabled, second_enabled):
        state = self.state(status="accepted")
        with without_commit_drain():
            self.peer.delete()
        self.publish(
            [
                entry(peer_address="198.18.0.2/32", remote_as="64515", enabled=first_enabled),
                entry(peer_address="198.18.0.2", remote_as="64516", enabled=second_enabled),
            ],
            projected_peers=[
                {
                    "peer_address": "198.18.0.2",
                    "remote_as": "64515",
                    "enabled": True,
                    "address_families": [],
                }
            ],
        )
        state.refresh_from_db()
        self.assertIsNone(state.bgp_peer_id)
        row = self.grid()[state.peer_address_str]
        self.assertEqual(row["remote_as"], "64515")
        self.assertIs(row["enabled"], True)
        self.assertIs(row["disabled"], False)
        self.assertEqual(self.preview()[0]["detail"], "AS 64515, enabled")

    def test_unlinked_missing_and_null_observations_are_unavailable(self):
        missing = self.state(linked=False, peer_address_str="198.18.0.3")
        null = self.state(linked=False, peer_address_str="198.18.0.4")
        absent_leaf = self.state(linked=False, peer_address_str="198.18.0.5")
        for row in self.grid().values():
            self.assertIsNone(row["remote_as"])
            self.assertIsNone(row["enabled"])
        self.publish(
            [
                entry(peer_address=null.peer_address_str, remote_as=None, enabled=None),
                entry(peer_address=absent_leaf.peer_address_str),
            ]
        )
        rows = self.grid()
        for state in (missing, null, absent_leaf):
            self.assertIsNone(rows[state.peer_address_str]["remote_as"])
            self.assertIsNone(rows[state.peer_address_str]["enabled"])
            self.assertFalse(rows[state.peer_address_str]["disabled"])

    def test_observation_identity_includes_router_asn_and_vrf(self):
        ip = IPAddress.objects.create(address="2001:db8::5/128")
        for asn, vrf_name in (("64512", ""), ("64512", "example-blue"), ("64516", "")):
            asn_object = ASN.objects.filter(asn=int(asn)).first() or ASN.objects.create(asn=int(asn), rir=self.rir)
            router = (
                self.router
                if asn == "64512"
                else BGPRouter.objects.create(
                    assigned_object_type=ContentType.objects.get_for_model(Device),
                    assigned_object_id=self.device.pk,
                    asn=asn_object,
                    name=asn,
                )
            )
            vrf = VRF.objects.create(name=vrf_name) if vrf_name else None
            scope = BGPScope.objects.get_or_create(router=router, vrf=vrf)[0]
            peer = BGPPeer.objects.create(scope=scope, peer=ip, remote_as=self.remote, enabled=True)
            acquire_overlay(
                NSOBGPPeerState,
                management=self.management,
                asn_str=asn,
                vrf_name=vrf_name,
                peer_address_str="2001:db8::5",
                bgp_peer=peer,
                status="accepted",
            )
            with without_commit_drain():
                peer.delete()
        self.publish(
            [],
            routers=[
                entry(
                    asn="0.64512",
                    scope=[
                        entry(vrf="", peer=[entry(peer_address="2001:0db8::5/128", remote_as="64513", enabled=False)]),
                        entry(
                            vrf="example-blue",
                            peer=[entry(peer_address="2001:db8::5", remote_as="64514", enabled=True)],
                        ),
                    ],
                ),
                entry(
                    asn="64516",
                    scope=[
                        entry(
                            vrf="",
                            peer=[
                                entry(peer_address="2001:db8::5", remote_as="64515", enabled=False),
                            ],
                        )
                    ],
                ),
            ],
        )
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", args=[self.device.pk, "bgp"])
        response = self.client.get(url, {"format": "json"})
        self.assertEqual(response.status_code, 200, response.content)
        rows = {(row["asn"], row["vrf"]): row for row in response.json()["peers"]["rows"]}
        self.assertEqual(rows[("64512", "global")]["remote_as"], "64513")
        self.assertEqual(rows[("64512", "example-blue")]["remote_as"], "64514")
        self.assertEqual(rows[("64516", "global")]["remote_as"], "64515")
        self.assertIs(rows[("64512", "global")]["enabled"], False)
        self.assertIs(rows[("64512", "example-blue")]["enabled"], True)
        details = {row["detail"] for row in self.preview()}
        self.assertEqual(details, {"AS 64513, disabled", "AS 64514, enabled", "AS 64515, disabled"})

    def post_new_peer(self, ip):
        url = reverse("plugins:netbox_nso_plugin:bgp_peer_add", args=[self.device.pk])
        with without_commit_drain():
            return self.client.post(
                url,
                {
                    "local_asn": self.asn.pk,
                    "peer": ip.pk,
                    "remote_as": self.remote.pk,
                    "enabled": "on",
                    "address_families": ["ipv4-unicast"],
                },
            )

    def test_create_reuses_retained_unowned_identity(self):
        retained = self.state(linked=False, peer_address_str="198.18.0.9")
        ip = IPAddress.objects.create(address="198.18.0.9/32")
        response = self.post_new_peer(ip)
        self.assertEqual(response.status_code, 302, response.content)
        retained.refresh_from_db()
        self.assertEqual(retained.status, "accepted")
        self.assertEqual(retained.bgp_peer.remote_as_id, self.remote.pk)
        self.assertTrue(retained.bgp_peer.enabled)
        self.assertEqual(NSOBGPPeerState.objects.filter(management=self.management).count(), 1)

    def test_create_refuses_retained_owned_identity_without_partial_graph(self):
        state = self.state(status="accepted")
        counts = [model.objects.count() for model in (BGPRouter, BGPScope, BGPPeer, BGPAddressFamily)]
        response = self.post_new_peer(self.ip)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.context["form"].non_field_errors(),
            [
                "Resolve the existing BGP peer state for this address before adding a peer.",
            ],
        )
        self.assertEqual([model.objects.count() for model in (BGPRouter, BGPScope, BGPPeer, BGPAddressFamily)], counts)
        state.refresh_from_db()
        self.assertEqual(state.bgp_peer_id, self.peer.pk)

    def test_unlinked_preview_uses_observation_and_marks_missing_leaves(self):
        state = self.state(status="accepted")
        with without_commit_drain():
            self.peer.delete()
        state.refresh_from_db()
        self.assertIsNone(state.bgp_peer_id)
        self.assertEqual(self.preview()[0]["detail"], "remote AS unavailable, state unavailable")
        self.publish([entry(peer_address="198.18.0.2/32", remote_as="64515", enabled=False)])
        self.assertEqual(self.preview()[0]["detail"], "AS 64515, disabled")

    def test_inline_resolves_existing_asn_and_can_clear_optional_remote_as(self):
        state = self.state()
        existing = ASN.objects.create(asn=64514, rir=self.rir)
        response = self.edit(state, {"remote_as": "0.64514"})
        self.assertEqual(response.status_code, 200, response.content)
        self.peer.refresh_from_db()
        self.assertEqual(self.peer.remote_as_id, existing.pk)
        self.assertFalse(self.peer.enabled)
        response = self.edit(state, {"remote_as": ""})
        self.assertEqual(response.status_code, 200, response.content)
        self.peer.refresh_from_db()
        self.assertIsNone(self.peer.remote_as_id)
        self.assertEqual(self.grid()[state.peer_address_str]["remote_as"], None)

    def test_null_published_router_inventory_is_unavailable(self):
        state = self.state(status="accepted")
        with without_commit_drain():
            self.peer.delete()
        self.publish([], routers=[], routers_null=True)
        row = self.grid()[state.peer_address_str]
        self.assertIsNone(row["remote_as"])
        self.assertIsNone(row["enabled"])
        self.assertEqual(self.preview()[0]["detail"], "remote AS unavailable, state unavailable")

    def test_quarantined_malformed_identity_keeps_grid_usable(self):
        self.state(linked=False, status="error", peer_address_str="invalid-peer")
        row = self.grid()["invalid-peer"]
        self.assertIsNone(row["remote_as"])
        self.assertIsNone(row["enabled"])
