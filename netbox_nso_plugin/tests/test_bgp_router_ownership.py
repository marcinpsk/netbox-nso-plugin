# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Router acquisition and comparison use durable identity and published reads."""

from uuid import uuid4

from dcim.models import Device
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from ipam.models import ASN, RIR
from netbox_routing.models import BGPAddressFamily, BGPRouter, BGPScope

from netbox_nso_plugin.device_differences import differences, observation_snapshots
from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState, NSOOwnershipManifest
from netbox_nso_plugin.observations import observation_defaults
from netbox_nso_plugin.ownership_planner import (
    OwnershipAction,
    OwnershipNotQualified,
    _manifest_lifecycle_action,
    reconcile_scope_ownership,
    require_qualifying_ownership,
)

from ._outbox_case import content_update, make_device, make_managed, mirror_update, without_commit_drain
from ._ownership_case import acquire_overlay
from ._routing_observation_case import routing_observation
from ._scope_observation_case import entry


class TestBgpRouterOwnership(TestCase):
    def setUp(self):
        self.device, self.management = make_managed(f"router{uuid4().hex[:8]}", 1767)
        registry = RIR.objects.create(name=f"example-{self.device.pk}", slug=f"example-{self.device.pk}")
        self.asn = ASN.objects.create(rir=registry, asn=64512)
        self.router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=self.asn,
            router_id="198.18.0.1",
        )
        self.scope = BGPScope.objects.create(router=self.router)
        BGPAddressFamily.objects.create(scope=self.scope, address_family="ipv4-unicast")
        self.user = get_user_model().objects.create_superuser(username=f"router{uuid4().hex[:8]}")

    def router_model(self):
        return apps.get_model("netbox_nso_plugin", "NSOBGPRouterState")

    def own_router(self):
        return acquire_overlay(
            self.router_model(),
            management=self.management,
            bgp_router=self.router,
            asn_str="64512",
            router_id="198.18.0.1",
            status="accepted",
        )

    def publish(self, router_id="198.18.0.2", *, revision=1, scope_present=True):
        router = entry(asn="64512", router_id=router_id)
        if scope_present:
            router["scope"] = [entry(vrf="", address_family=[entry(afi="ipv4-unicast")], peer=[], peer_group=[])]
            router["present"].append("scope")
        document = {"routers": [router], "present": ["routers"], "unprojectable": []}
        observed = routing_observation("bgp", document=document, revision=revision)
        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family="bgp")
        NSOFamilyObservation.objects.update_or_create(
            read_state=state, defaults=observation_defaults("bgp", revision, 1, observed)
        )

    def test_router_manifest_reowns_only_same_native_identity(self):
        model = self.router_model()
        state = self.own_router()
        mirror_update(state, status="apply_failed", last_apply_error="Previous failure", apply_attempt_id=uuid4())
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, state_model_label=model._meta.label_lower)
        self.assertEqual(
            manifest.native_key,
            {
                "assigned_object_type_id": self.router.assigned_object_type_id,
                "assigned_object_id": self.device.pk,
                "asn_id": self.asn.pk,
            },
        )
        self.assertEqual(manifest.state_key, {"asn_str": "64512"})
        with without_commit_drain():
            model.objects.filter(pk=state.pk).delete()
            reconcile_scope_ownership(self.device.pk, ["bgp"])
        replacement = model.objects.get(management=self.management, asn_str="64512")
        self.assertNotEqual(replacement.pk, state.pk)
        self.assertEqual(replacement.bgp_router_id, self.router.pk)
        self.assertEqual(replacement.router_id, "198.18.0.1")
        self.assertEqual(replacement.status, "accepted")
        self.assertEqual(replacement.last_apply_error, "")
        self.assertIsNone(replacement.apply_attempt_id)
        self.assertIsNone(replacement.last_apply_at)
        manifest.refresh_from_db()
        self.assertEqual(manifest.native_id, self.router.pk)
        self.assertEqual(manifest.ownership_state, "owned")
        with without_commit_drain():
            model.objects.filter(pk=replacement.pk).delete()
            BGPAddressFamily.objects.filter(scope=self.scope).delete()
            self.scope.delete()
            self.router.delete()
            BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(Device),
                assigned_object_id=self.device.pk,
                asn=self.asn,
                router_id="198.18.0.3",
            )
            reconcile_scope_ownership(self.device.pk, ["bgp"])
        self.assertFalse(model.objects.filter(management=self.management).exists())
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")

    def test_router_native_move_retracts_old_manifest(self):
        model = self.router_model()
        state = self.own_router()
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, state_model_label=model._meta.label_lower)
        other = make_device(f"move{uuid4().hex[:8]}")
        with without_commit_drain():
            content_update(self.router, assigned_object_id=other.pk)
            content_update(state, last_apply_error="Previous failure")
            manifest.refresh_from_db()
            self.assertEqual(manifest.native_key["assigned_object_id"], self.device.pk)
            lifecycle = _manifest_lifecycle_action(manifest, frozenset({"bgp"}))
            self.assertEqual(lifecycle[-1], OwnershipAction.RETRACT)
            reconcile_scope_ownership(self.device.pk, ["bgp"])
        manifest.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertNotIn(state.status, {"accepted", "deploying", "in_sync", "apply_failed"})
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=other.pk, ownership_state="owned").exists())

    def test_router_acquisition_rejects_mismatched_asn_or_device(self):
        model = self.router_model()
        other = make_device(f"foreign{uuid4().hex[:8]}")
        for asn, device_id in (("64513", self.device.pk), ("64512", other.pk)):
            with self.subTest(asn=asn, device_id=device_id):
                if device_id != self.device.pk:
                    content_update(self.router, assigned_object_id=device_id)
                candidate = model(management=self.management, bgp_router=self.router, asn_str=asn, status="accepted")
                with self.assertRaises(OwnershipNotQualified):
                    require_qualifying_ownership(candidate)
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="bgp").exists())

    def test_unlinked_router_cannot_acquire_through_an_exact_writer(self):
        model = self.router_model()
        with self.assertRaises(OwnershipNotQualified):
            acquire_overlay(model, management=self.management, asn_str="64512", status="accepted")
        self.assertFalse(model.objects.filter(management=self.management).exists())
        self.assertFalse(NSOOwnershipManifest.objects.filter(device_id=self.device.pk, scope="bgp").exists())

    def test_router_qualification_checks_native_identity_once(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.bgp_router_content import qualified_router

        state = self.own_router()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(qualified_router(state), self.router)
        self.assertEqual(sum(f'FROM "{BGPRouter._meta.db_table}"' in query["sql"] for query in queries), 1)

    def test_router_native_asn_move_cannot_reown_old_identity(self):
        model = self.router_model()
        state = self.own_router()
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, state_model_label=model._meta.label_lower)
        other_asn = ASN.objects.create(rir=self.asn.rir, asn=64513)
        with without_commit_drain():
            content_update(self.router, asn=other_asn)
            self.assertEqual(_manifest_lifecycle_action(manifest, frozenset({"bgp"}))[-1], OwnershipAction.RETRACT)
            reconcile_scope_ownership(self.device.pk, ["bgp"])
        manifest.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertNotIn(state.status, {"accepted", "deploying", "in_sync", "apply_failed"})
        self.assertFalse(model.objects.filter(management=self.management, asn_str="64513").exists())

    def test_router_explicit_unaccept_detaches_manifest(self):
        model = self.router_model()
        state = self.own_router()
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, state_model_label=model._meta.label_lower)
        with without_commit_drain():
            content_update(state, status="imported", accepted_at=None)
            reconcile_scope_ownership(self.device.pk, ["bgp"])
            model.objects.filter(pk=state.pk).delete()
            reconcile_scope_ownership(self.device.pk, ["bgp"])
        manifest.refresh_from_db()
        self.assertEqual(manifest.ownership_state, "detached")
        self.assertFalse(model.objects.filter(management=self.management).exists())

    def test_router_differences_use_same_observation_revision(self):
        from netbox_nso_plugin.difference_projection import native_entries

        state = self.own_router()
        self.publish(revision=1)
        snapshots = observation_snapshots(self.management)
        self.publish(router_id="198.18.0.3", revision=2)
        rows = [row for row in differences(self.management, user=self.user, snapshots=snapshots) if row.scope == "bgp"]
        router_difference = next(
            row for row in rows if row.identity == ("router", 64512) and row.attribute == "router_id"
        )
        self.assertEqual(router_difference.device_value, "198.18.0.2")
        self.assertEqual(snapshots["bgp"].revision, 1)
        router_entries = [
            item for item in native_entries("bgp", self.management) if item.identity[:2] == ("router", 64512)
        ]
        self.assertTrue(router_entries)
        self.assertTrue(all(state in item.objects for item in router_entries))
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_router_scope_coverage_gap_is_unavailable(self):
        state = self.own_router()
        self.publish(scope_present=False)
        rows = [row for row in differences(self.management, user=self.user) if row.scope == "bgp"]
        self.assertTrue(any(row.kind == "unavailable" and "scope" in row.reason for row in rows))
        self.assertFalse(any(row.kind == "netbox_only" and len(row.identity) > 2 for row in rows))
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertTrue(BGPScope.objects.filter(pk=self.scope.pk).exists())

    def test_router_accept_refuses_malformed_observed_asn(self):
        from django.contrib.messages import get_messages
        from django.urls import reverse

        state = self.router_model().objects.create(
            management=self.management, bgp_router=self.router, asn_str="64512", status="imported"
        )
        self.publish()
        observation = NSOFamilyObservation.objects.get(read_state__management=self.management, read_state__family="bgp")
        observation.document["routers"].append(entry(asn="not-an-asn"))
        observation.save(update_fields=("document",))
        self.client.force_login(self.user)
        response = self.client.post(reverse("plugins:netbox_nso_plugin:routing_accept_bgp_router", args=(state.pk,)))
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "imported")
        [message] = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(message.endswith(": The observed BGP router ASN is invalid. Refresh BGP before accepting it."))
