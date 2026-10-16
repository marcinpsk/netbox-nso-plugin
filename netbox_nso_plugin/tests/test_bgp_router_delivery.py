# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Router ownership through real views, graph writes, rendering, and the drain."""

from unittest.mock import patch

from dcim.models import Device
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from django.urls import reverse
from ipam.models import ASN, RIR, VRF, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPRouter, BGPScope

from netbox_nso_plugin import delivery, drain
from netbox_nso_plugin.models import NSOBGPPeerState, NSOIntentRevision, NSOOwnershipManifest
from netbox_nso_plugin.ownership_grants import OwnershipGrant
from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

from ._outbox_case import ReceiptAdapter, entries, make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestBGPRouterDelivery(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        transport = patch("netbox_nso_plugin.adapter_client.requests.Session", self.adapter.session)
        transport.start()
        self.addCleanup(transport.stop)
        self.device, self.management = make_managed("router-delivery", 17671)
        self.management.manage_bgp = True
        self.management.save(update_fields=("manage_bgp",))
        self.asn = ASN.objects.create(asn=65123, rir=RIR.objects.create(name="Router private", slug="router-private"))
        self.router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=self.asn,
            name="router-placeholder",
            router_id="198.18.0.1",
        )
        self.scope = BGPScope.objects.create(router=self.router)
        self.af = BGPAddressFamily.objects.create(scope=self.scope, address_family="ipv4-unicast")
        self.client.force_login(get_user_model().objects.create_user("router-operator", is_superuser=True))
        self.adapter.applied.clear()

    def router_state(self, owned=False):
        model = apps.get_model("netbox_nso_plugin", "NSOBGPRouterState")
        values = dict(management=self.management, asn_str="65123", bgp_router=self.router, router_id="198.18.0.1")
        return (
            acquire_overlay(model, status="accepted", **values)
            if owned
            else model.objects.create(status="imported", **values)
        )

    def peer_state(self):
        peer = BGPPeer.objects.create(
            scope=self.scope, peer=IPAddress.objects.create(address="198.18.0.2/32"), name=None
        )
        return acquire_overlay(
            NSOBGPPeerState,
            management=self.management,
            asn_str="65123",
            vrf_name="",
            peer_address_str="198.18.0.2",
            bgp_peer=peer,
            status="accepted",
        )

    def payload(self):
        return delivery.render("bgp", self.device.pk, self.management.adapter_device_id).payload

    def accept(self, state):
        return self.client.post(reverse("plugins:netbox_nso_plugin:routing_accept_bgp_router", args=(state.pk,)))

    def test_router_accept_persists_qualified_manifest_and_schedules_bgp(self):
        state = self.router_state()
        with without_commit_drain():
            response = self.accept(state)
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertIsNotNone(state.accepted_at)
        manifest = NSOOwnershipManifest.objects.get(
            device_id=self.device.pk, native_model_label="netbox_routing.bgprouter"
        )
        self.assertEqual(manifest.native_id, self.router.pk)
        self.assertEqual(manifest.state_key, {"asn_str": "65123"})
        self.assertEqual(manifest.grant_kind, "accept")
        self.assertTrue(entries(self.device, "bgp", unconsumed=True))
        drain.drain_intent_outbox()
        self.assertTrue(self.adapter.applied)

    def test_router_accept_of_an_imported_router_yields_accepted(self):
        state = self.router_state()
        with without_commit_drain():
            response = self.accept(state)
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_router_id_edit_validates_the_written_router_against_constraints(self):
        from core.models import ObjectType
        from users.models import ObjectPermission

        state = self.router_state(owned=True)
        limited = get_user_model().objects.create_user("router-constrained-editor")
        management_permission = ObjectPermission.objects.create(name="Router edit management", actions=["change"])
        management_permission.object_types.add(ObjectType.objects.get_for_model(type(self.management)))
        management_permission.users.add(limited)
        native_permission = ObjectPermission.objects.create(
            name="Router edit range", actions=["change"], constraints={"router_id": "198.18.0.1"}
        )
        native_permission.object_types.add(ObjectType.objects.get_for_model(BGPRouter))
        native_permission.users.add(limited)
        self.client.force_login(limited)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("bgp_router", state.pk)),
            {"router_id": "198.18.0.9"},
        )
        self.assertEqual(response.status_code, 403)
        self.router.refresh_from_db()
        self.assertEqual(str(self.router.router_id), "198.18.0.1")
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_router_only_accept_emits_router_id_and_scope_afs(self):
        state = self.router_state()
        response = self.accept(state)
        self.assertEqual(response.status_code, 302)
        body = self.adapter.applied[-1][1]
        self.assertEqual(body["routers"][0]["asn"], "65123")
        self.assertEqual(body["routers"][0]["router_id"], "198.18.0.1")
        self.assertEqual(
            body["routers"][0]["scopes"],
            [{"vrf": "", "address_families": [{"af": "ipv4-unicast", "redistribution": []}], "peers": []}],
        )
        self.assertFalse(NSOBGPPeerState.objects.exists())

    def test_peer_push_without_owned_router_fails_closed(self):
        self.peer_state()
        drain.drain_intent_outbox()
        self.assertEqual(self.adapter.applied, [], "An owned peer must not synthesize an unowned router")
        self.management.refresh_from_db()
        self.assertEqual(self.management.intent_push_errors["bgp"]["detail"]["reason"], "blocked_bgp_router")

    def test_unqualified_peer_blocker_keeps_the_operator_message_and_state_id(self):
        with without_commit_drain():
            self.router_state(owned=True)
            state = self.peer_state()
            BGPPeer.objects.filter(pk=state.bgp_peer_id).update(peer=IPAddress.objects.create(address="198.18.0.7/32"))
        blocked = self.payload()["blocked"]
        self.assertEqual(blocked["message"], "The bgp binding does not qualify for ownership.")
        self.assertEqual(blocked["detail"], {"reason": "blocked_bgp_router", "asn": "65123", "state_id": state.pk})

    def test_blocked_router_has_actionable_message_in_real_view(self):
        self.peer_state()
        drain.drain_intent_outbox()
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category", args=(self.device.pk, "bgp"))
        )
        self.assertContains(response, "Accept the router")
        self.assertContains(response, "NetBox did not send the BGP snapshot")
        self.assertContains(response, "nso-bgp-routers")

    def test_peer_bulk_accept_is_offered_only_for_unowned_peers(self):
        self.router_state()
        self.peer_state()
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", args=(self.device.pk, "bgp"))
        bulk_url = reverse("plugins:netbox_nso_plugin:routing_bulk_accept_bgp_peers", args=(self.device.pk,))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["grid_payload"]["routers"]["rows"][0]["accept_url"])
        self.assertNotContains(response, bulk_url)
        NSOBGPPeerState.objects.create(
            management=self.management,
            asn_str="65123",
            vrf_name="",
            peer_address_str="198.18.0.3",
            bgp_peer=BGPPeer.objects.create(
                scope=self.scope, peer=IPAddress.objects.create(address="198.18.0.3/32"), name=None
            ),
            status="imported",
        )
        response = self.client.get(url)
        self.assertContains(response, bulk_url)
        self.assertContains(response, "Accept All Peers")

    def test_missing_native_peer_is_reported_as_unsent_in_real_view(self):
        router = self.router_state(owned=True)
        state = self.peer_state()
        drain.drain_intent_outbox()
        self.adapter.applied.clear()
        with without_commit_drain():
            state.bgp_peer.delete()
        state.refresh_from_db()
        self.assertIsNone(state.bgp_peer_id)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("bgp_router", router.pk)),
            {"router_id": "198.18.0.9"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.adapter.applied, [])
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category", args=(self.device.pk, "bgp"))
        )
        push_error = response.context["push_error"]
        self.assertEqual(push_error["kind"], "unsent")
        self.assertEqual(push_error["detail"], {"reason": "missing_bgp_peer"})
        self.assertIn("NetBox did not send the BGP snapshot", push_error["headline"])

    def test_router_id_edit_pushes_without_peer_save(self):
        state = self.router_state()
        self.accept(state)
        before = len(self.adapter.applied)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("bgp_router", state.pk)),
            {"router_id": "198.18.0.9"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.router.refresh_from_db()
        self.assertEqual(str(self.router.router_id), "198.18.0.9")
        self.assertGreater(len(self.adapter.applied), before)
        self.assertEqual(self.adapter.applied[-1][1]["routers"][0]["router_id"], "198.18.0.9")
        self.assertFalse(NSOBGPPeerState.objects.exists())

    def test_router_id_edit_of_a_deploying_router_clears_its_apply_attempt(self):
        from uuid import uuid4

        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._outbox_case import mirror_update

        attempt = NSOApplyAttempt.objects.create(
            id=uuid4(),
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            selected={},
            scope_revisions={},
        )
        state = mirror_update(self.router_state(owned=True), status="deploying", apply_attempt_id=attempt.pk)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("bgp_router", state.pk)),
            {"router_id": "198.18.0.9"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        state.refresh_from_db()
        self.assertEqual((state.status, state.apply_attempt_id), ("accepted", None))

    def test_native_router_id_edit_pushes_without_peer_save(self):
        self.router_state(owned=True)
        before = self.payload()
        self.router.router_id = "198.18.0.8"
        plan = RendererMutationPlan.build(
            grant=OwnershipGrant("operator_edit"), saves=(planned_save(self.router, update_fields=("router_id",)),)
        )
        with renderer_writes(plan) as writer:
            writer.save(self.router, update_fields=("router_id",))
        self.assertNotEqual(self.payload(), before)
        self.assertEqual(self.adapter.applied[-1][1]["routers"][0]["router_id"], "198.18.0.8")

    def test_router_id_edit_can_choose_observed_value(self):
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        state = self.router_state(owned=True)
        _reconcile_bgp_config(
            self.device,
            {
                "device_id": self.device.pk,
                "routers": [
                    {
                        "asn": "65123",
                        "router_id": "198.18.0.2",
                        "scopes": [{"vrf": "", "address_families": ["ipv4-unicast"], "peers": [], "peer_groups": []}],
                    }
                ],
            },
        )
        state.refresh_from_db()
        self.assertEqual(state.router_id, "198.18.0.2")
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", args=("bgp_router", state.pk)),
            {"router_id": "198.18.0.2"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.router.refresh_from_db()
        self.assertEqual(str(self.router.router_id), "198.18.0.2")
        self.assertEqual(response.json()["changed"], ["router_id"])
        self.assertEqual(self.adapter.applied[-1][1]["routers"][0]["router_id"], "198.18.0.2")

    def test_router_scope_and_af_changes_advance_router_content(self):
        self.router_state(owned=True)
        before = delivery.canonical_fingerprint(self.payload())
        revision = NSOIntentRevision.objects.get(device=self.device, scope="bgp").revision
        vrf = VRF.objects.create(name="router-blue")
        scope = BGPScope(router=self.router, vrf=vrf)
        af = BGPAddressFamily(scope=scope, address_family="ipv6-unicast")
        plan = RendererMutationPlan.build(
            grant=OwnershipGrant("operator_edit"),
            saves=(
                planned_save(scope, force_insert=True, natural_key=("router", "vrf")),
                planned_save(af, force_insert=True, natural_key=("scope", "address_family")),
            ),
        )
        with renderer_writes(plan) as writer:
            writer.save(scope, force_insert=True)
            writer.save(af, force_insert=True)
        self.assertNotEqual(delivery.canonical_fingerprint(self.payload()), before)
        self.assertGreater(NSOIntentRevision.objects.get(device=self.device, scope="bgp").revision, revision)
        self.assertEqual(self.payload()[0]["scopes"][1]["vrf"], "router-blue")

    def test_router_preview_uses_native_desired_id_after_observed_drift(self):
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        self.router_state(owned=True)
        _reconcile_bgp_config(
            self.device,
            {
                "device_id": self.device.pk,
                "routers": [
                    {
                        "asn": "65123",
                        "router_id": "198.18.0.2",
                        "scopes": [{"vrf": "", "address_families": ["ipv4-unicast"], "peers": [], "peer_groups": []}],
                    }
                ],
            },
        )
        response = self.client.get(reverse("plugins:netbox_nso_plugin:device_apply_preview", args=(self.device.pk,)))
        self.assertEqual(response.status_code, 200)
        changes = [row for row in response.json()["routing_changes"] if row["category"] == "BGP router"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["detail"], "198.18.0.1")

    def test_peer_scalar_edit_keeps_router_contribution_unchanged(self):
        from netbox_nso_plugin.intent_state import renderer_input_specs

        state = self.router_state(owned=True)
        peer_state = self.peer_state()
        spec = renderer_input_specs()[state._meta.label_lower]
        before = spec.fragment(state)
        native = peer_state.bgp_peer
        native.ttl = 5
        plan = RendererMutationPlan.build(
            grant=OwnershipGrant("operator_edit"), saves=(planned_save(native, update_fields=("ttl",)),)
        )
        with renderer_writes(plan) as writer:
            writer.save(native, update_fields=("ttl",))
        state.refresh_from_db()
        self.assertEqual(spec.fragment(state), before)

    def test_greenfield_peer_creates_router_overlay_in_same_plan(self):
        model = apps.get_model("netbox_nso_plugin", "NSOBGPRouterState")
        self.af.delete()
        self.scope.delete()
        self.router.delete()
        ip = IPAddress.objects.create(address="198.18.0.3/32")
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:bgp_peer_add", args=(self.device.pk,)),
            {"local_asn": self.asn.pk, "peer": ip.pk, "enabled": "on", "address_families": ["ipv4-unicast"]},
        )
        self.assertEqual(response.status_code, 302)
        state = model.objects.get(management=self.management, asn_str="65123")
        self.assertIsNotNone(state.accepted_at)
        self.assertEqual(
            NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="bgp", ownership_state="owned").count(),
            2,
        )
        self.assertEqual(len(self.payload()[0]["scopes"][0]["peers"]), 1)

    def test_greenfield_peer_does_not_adopt_unowned_router(self):
        ip = IPAddress.objects.create(address="198.18.0.3/32")
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:bgp_peer_add", args=(self.device.pk,)),
            {"local_asn": self.asn.pk, "peer": ip.pk, "enabled": "on", "address_families": ["ipv4-unicast"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accept the router")
        self.assertFalse(NSOBGPPeerState.objects.exists())
        self.assertEqual(self.adapter.applied, [])

    def test_router_grid_has_native_permissions_and_accept_action(self):
        from core.models import ObjectType
        from users.models import ObjectPermission

        state = self.router_state()
        url = reverse("plugins:netbox_nso_plugin:device_nso_category", args=(self.device.pk, "bgp"))
        response = self.client.get(url + "?format=json")
        self.assertEqual(response.status_code, 200)
        row = response.json()["routers"]["rows"][0]
        self.assertEqual(row["router"]["url"], self.router.get_absolute_url())
        self.assertEqual(
            row["accept_url"], reverse("plugins:netbox_nso_plugin:routing_accept_bgp_router", args=(state.pk,))
        )
        self.assertIsNotNone(row["edit_url"])
        response = self.client.get(
            reverse("plugins-api:netbox_nso_plugin-api:nsobgprouterstate-detail", args=(state.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["asn_str"], "65123")
        limited = get_user_model().objects.create_user("router-scoped-operator")
        view_permission = ObjectPermission.objects.create(name="Router view", actions=["view"])
        view_permission.object_types.add(
            ObjectType.objects.get_for_model(BGPRouter), ObjectType.objects.get_for_model(type(state))
        )
        view_permission.users.add(limited)
        management_permission = ObjectPermission.objects.create(name="Router management", actions=["change"])
        management_permission.object_types.add(ObjectType.objects.get_for_model(type(self.management)))
        management_permission.users.add(limited)
        native_permission = ObjectPermission.objects.create(
            name="Other router change", actions=["change"], constraints={"pk": self.router.pk + 1}
        )
        native_permission.object_types.add(ObjectType.objects.get_for_model(BGPRouter))
        native_permission.users.add(limited)
        self.client.force_login(limited)
        response = self.client.get(url + "?format=json")
        self.assertEqual(response.status_code, 200)
        row = response.json()["routers"]["rows"][0]
        self.assertIsNone(row["accept_url"])
        self.assertIsNone(row["edit_url"])
        self.assertEqual(self.accept(state).status_code, 403)
        self.client.force_login(get_user_model().objects.create_user("router-read-only"))
        response = self.client.post(reverse("plugins:netbox_nso_plugin:routing_accept_bgp_router", args=(state.pk,)))
        self.assertEqual(response.status_code, 403)
        state.refresh_from_db()
        self.assertIsNone(state.accepted_at)

    def owned_peer(self, address):
        peer = BGPPeer.objects.create(
            scope=self.scope, peer=IPAddress.objects.create(address=f"{address}/32"), name=None
        )
        return acquire_overlay(
            NSOBGPPeerState,
            management=self.management,
            asn_str="65123",
            vrf_name="",
            peer_address_str=address,
            bgp_peer=peer,
            status="accepted",
        )

    def router_reads(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self.payload()
        return sum('FROM "netbox_routing_bgprouter"' in query["sql"] for query in queries.captured_queries)

    def test_peer_qualification_reads_routers_once_per_render(self):
        with without_commit_drain():
            self.router_state(owned=True)
            self.owned_peer("198.18.0.2")
        single = self.router_reads()
        with without_commit_drain():
            self.owned_peer("198.18.0.3")
            self.owned_peer("198.18.0.4")
        self.assertEqual(self.router_reads(), single)

    def test_redistribution_reference_with_slash_vrf_is_rejected_as_invalid(self):
        from netbox_nso_plugin.models import NSORedistributionState

        vrf = VRF.objects.create(name="placeholder/blue")
        scope = BGPScope.objects.create(router=self.router, vrf=vrf)
        BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        with without_commit_drain():
            self.router_state(owned=True)
            acquire_overlay(
                NSORedistributionState,
                management=self.management,
                dest_protocol="bgp",
                dest_ref="65123/placeholder/blue/ipv4-unicast",
                source_protocol="connected",
                source_ref="",
                status="accepted",
            )
        blocked = self.payload()["blocked"]
        self.assertEqual(blocked["detail"]["reason"], "blocked_bgp_router")
        self.assertEqual(blocked["message"], "BGP redistribution has an invalid destination identity.")
