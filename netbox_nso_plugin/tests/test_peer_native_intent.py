# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Native peer intent survives every admitted device observation."""

import ast
import copy
import inspect
import textwrap

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from ipam.models import ASN, VRF, IPAddress
from netbox_routing.models import BGPPeer, BGPPeerAddressFamily, BGPRouter, BGPScope, PrefixList, RouteMap

from netbox_nso_plugin import delivery
from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
from netbox_nso_plugin.models import NSOBGPPeerState, NSOBGPPeerTemplateState, NSOBGPRouterState

from ._outbox_case import content_update, make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def peer_document(device, **leaves):
    peer = {
        "peer_address": "198.18.0.2",
        "remote_as": "64513",
        "enabled": True,
        "address_families": [{"af": "ipv4-unicast", "enabled": True}],
        **leaves,
    }
    return {
        "device_id": device.pk,
        "routers": [
            {
                "asn": "64512",
                "router_id": "198.18.0.1",
                "scopes": [{"vrf": "", "address_families": ["ipv4-unicast"], "peers": [peer], "peer_groups": []}],
            }
        ],
    }


class TestNativePeerIntent(IntentPushResetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("native-peer", 1803)
        self.document = peer_document(self.device)
        _reconcile_bgp_config(self.device, self.document)
        self.state = NSOBGPPeerState.objects.get(management=self.management)
        self.router = NSOBGPRouterState.objects.get(management=self.management)
        self.user = get_user_model().objects.create_superuser("peer-operator", "", "placeholder")
        self.client.force_login(self.user)

    def own(self, status="in_sync"):
        with without_commit_drain():
            self.router = content_update(self.router, status="accepted")
            self.state = content_update(self.state, status=status)
        return self.state.device_base_hash

    def read(self, **leaves):
        _reconcile_bgp_config(self.device, peer_document(self.device, **leaves))
        self.state.refresh_from_db()
        return self.state.bgp_peer

    def accept(self):
        with without_commit_drain():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:routing_accept_bgp_peer", args=[self.state.pk])
            )
        self.assertEqual(response.status_code, 302)
        self.state.refresh_from_db()

    def test_historical_agreement_device_drift_accept_requires_current_match(self):
        base = self.own()
        for _ in range(2):
            native = self.read(remote_as="64514", enabled=False)
            self.assertEqual((native.remote_as.asn, native.enabled), (64513, True))
            self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
            self.accept()
            self.assertEqual(self.state.status, "accepted")
        self.read()
        self.assertEqual(self.state.status, "in_sync")

    def test_import_absence_accept_cannot_settle_until_peer_returns(self):
        absent = copy.deepcopy(self.document)
        absent["routers"][0]["scopes"][0]["peers"] = []
        _reconcile_bgp_config(self.device, absent)
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, "changed")
        self.accept()
        self.assertEqual(self.state.status, "accepted")
        _reconcile_bgp_config(self.device, absent)
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, "accepted")
        self.read()
        self.assertEqual(self.state.status, "in_sync")

    def test_owned_absence_then_device_drift_preserves_native_intent(self):
        base = self.own()
        absent = copy.deepcopy(self.document)
        absent["routers"][0]["scopes"][0]["peers"] = []
        _reconcile_bgp_config(self.device, absent)
        self.state.refresh_from_db()
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        native = self.read(
            remote_as="64514", enabled=False, address_families=[{"af": "ipv4-unicast", "enabled": False}]
        )
        self.assertEqual((native.remote_as.asn, native.enabled), (64513, True))
        self.assertTrue(BGPPeerAddressFamily.objects.get(assigned_object_id=native.pk).enabled)
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        self.read()
        self.assertEqual(self.state.status, "in_sync")

    def test_native_edit_and_repeated_divergence_preserve_peer_and_af_intent(self):
        base = self.own("accepted")
        native = self.state.bgp_peer
        af = BGPPeerAddressFamily.objects.get(assigned_object_id=native.pk)
        with without_commit_drain():
            content_update(native, remote_as=ASN.objects.create(asn=64514, rir=native.remote_as.rir), enabled=False)
            content_update(af, enabled=False)
        for remote_as in ("64514", "64515", "64516"):
            native = self.read(remote_as=remote_as)
            af.refresh_from_db()
            self.assertEqual((native.remote_as.asn, native.enabled, af.enabled), (64514, False, False))
            self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        payload = delivery.render("bgp", self.device.pk, self.management.adapter_device_id).payload
        rendered = payload[0]["scopes"][0]["peers"][0]
        self.assertEqual((rendered["remote_as"], rendered["enabled"]), ("64514", False))
        self.assertFalse(rendered["address_families"][0]["enabled"])

    def test_owned_native_address_edit_cannot_settle_old_identity(self):
        base = self.own("accepted")
        native = self.state.bgp_peer
        address = IPAddress.objects.create(address="198.18.0.3/32")
        content_update(native, peer=address)
        self.read()
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        self.assertEqual(self.state.bgp_peer.peer_id, address.pk)

    def test_owned_native_router_asn_edit_cannot_settle_old_identity(self):
        base = self.own("accepted")
        router = self.state.bgp_peer.scope.router
        asn = ASN.objects.create(asn=64514, rir=router.asn.rir)
        content_update(router, asn=asn)
        self.read()
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))

    def test_owned_native_scope_router_edit_cannot_settle_old_identity(self):
        base = self.own("accepted")
        native = self.state.bgp_peer
        router = native.scope.router
        other_router = BGPRouter.objects.create(
            assigned_object_type=router.assigned_object_type,
            assigned_object_id=router.assigned_object_id,
            asn=ASN.objects.create(asn=64514, rir=router.asn.rir),
        )
        other_scope = BGPScope.objects.create(router=other_router)
        content_update(native, scope=other_scope)
        self.read()
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        self.assertEqual(self.state.bgp_peer.scope_id, other_scope.pk)

    def test_owned_native_scope_vrf_edit_cannot_settle_old_identity(self):
        base = self.own("accepted")
        native = self.state.bgp_peer
        other_scope = BGPScope.objects.create(router=native.scope.router, vrf=VRF.objects.create(name="example-blue"))
        content_update(native, scope=other_scope)
        self.read()
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        self.assertEqual(self.state.bgp_peer.scope_id, other_scope.pk)

    def test_owned_native_scope_on_another_device_cannot_settle_matching_identity(self):
        from netbox_nso_plugin.ownership_planner import OwnershipNotQualified, require_qualifying_ownership

        self.own("in_sync")
        self.state = content_update(self.state, device_base_hash="")
        native = self.state.bgp_peer
        router = native.scope.router
        other_device, _management = make_managed("native-peer-other", 1804)
        other_router = BGPRouter.objects.create(
            assigned_object_type=router.assigned_object_type,
            assigned_object_id=other_device.pk,
            asn=router.asn,
        )
        other_scope = BGPScope.objects.create(router=other_router, vrf=native.scope.vrf)
        content_update(native, scope=other_scope)
        before = (list(BGPPeer.objects.values()), list(BGPPeerAddressFamily.objects.values()))
        plan = bgp_reconcile_plan(self.device, self.document)
        self.assertFalse(
            any(
                write.model_label in {"netbox_routing.bgppeer", "netbox_routing.bgppeeraddressfamily"}
                for write in plan.write_set
            )
        )
        self.read()
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", ""))
        self.assertEqual((list(BGPPeer.objects.values()), list(BGPPeerAddressFamily.objects.values())), before)
        self.assertTrue(BGPRouter.objects.filter(pk=router.pk, assigned_object_id=self.device.pk).exists())
        self.assertEqual(self.state.bgp_peer.scope_id, other_scope.pk)
        with self.assertRaises(OwnershipNotQualified):
            require_qualifying_ownership(self.state)
        payload = delivery.render("bgp", self.device.pk, self.management.adapter_device_id).payload
        self.assertIn("blocked", payload)

    def test_owned_native_disappearance_is_not_recreated_and_audit_retracts(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import (
            OwnershipAction,
            _manifest_lifecycle_actions,
            reconcile_scope_ownership,
        )

        self.own("accepted")
        with without_commit_drain():
            self.state.bgp_peer.delete()
        for _ in range(2):
            self.read()
            self.assertIsNone(self.state.bgp_peer_id)
            self.assertEqual(self.state.status, "accepted")
            self.assertFalse(BGPPeer.objects.exists())
        actions = _manifest_lifecycle_actions(self.device.pk, frozenset({"bgp"}))
        self.assertTrue(
            any(
                manifest.state_model_label == self.state._meta.label_lower and action is OwnershipAction.RETRACT
                for manifest, _rule, _native, _overlay, action in actions
            )
        )
        with without_commit_drain():
            reconcile_scope_ownership(self.device.pk, ("bgp",))
        manifest = NSOOwnershipManifest.objects.get(state_model_label=self.state._meta.label_lower)
        self.assertEqual(manifest.ownership_state, "retired")

    def test_observation_only_keeps_owned_bookkeeping_and_native_content(self):
        for status in ("accepted", "deploying"):
            self.own(status)
            with without_commit_drain():
                router = self.router.bgp_router
                content_update(router, asn=ASN.objects.get_or_create(asn=64520, defaults={"rir": router.asn.rir})[0])
            before = (list(BGPPeer.objects.values()), list(BGPPeerAddressFamily.objects.values()))
            plan = bgp_reconcile_plan(self.device, self.document)
            self.assertFalse(
                any(
                    write.model_label in {"netbox_routing.bgppeer", "netbox_routing.bgppeeraddressfamily"}
                    for write in plan.write_set
                )
            )
            self.read()
            self.assertEqual(self.state.status, status)
            self.assertEqual((list(BGPPeer.objects.values()), list(BGPPeerAddressFamily.objects.values())), before)
            self.assertFalse({"remote_as_str", "enabled"} & {field.name for field in self.state._meta.concrete_fields})

    def test_canonical_push_and_matching_read_share_wire_normalization(self):
        self.own("accepted")
        native = self.state.bgp_peer
        af = BGPPeerAddressFamily.objects.get(assigned_object_id=native.pk)
        with without_commit_drain():
            content_update(
                native,
                remote_as=ASN.objects.create(asn=4200000000, rir=native.remote_as.rir),
                bfd_enabled=True,
                password="",
            )
            content_update(af, enabled=None)
        payload = delivery.render("bgp", self.device.pk, self.management.adapter_device_id).payload
        rendered = payload[0]["scopes"][0]["peers"][0]
        self.assertEqual(rendered["remote_as"], "4200000000")
        self.assertTrue(rendered["address_families"][0]["enabled"])
        self.assertNotIn("bfd_enabled", rendered)
        self.read(remote_as="64086.59904", bfd_enabled=False)
        self.assertEqual(self.state.status, "in_sync")
        base = self.state.device_base_hash
        self.read(remote_as="4200000000", address_families=[{"af": "ipv4-unicast", "enabled": False}])
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        with without_commit_drain():
            content_update(native, password="placeholder-secret")
        self.read(remote_as="4200000000")
        self.assertEqual(self.state.status, "accepted")

    def test_unowned_merge_preserves_seed_equality_mirror_freeze_and_conflict(self):
        self.assertEqual(self.state.status, "imported")
        native = self.read()
        self.assertEqual(self.state.status, "imported")
        native = self.read(ttl=2)
        self.assertEqual((native.ttl, self.state.status), (2, "imported"))
        content_update(native, ttl=3)
        native = self.read(ttl=2)
        self.assertEqual((native.ttl, self.state.status), (3, "changed"))
        native = self.read(ttl=4)
        self.assertEqual((native.ttl, self.state.status), (3, "conflict"))
        self.assertFalse({"remote_as_str", "enabled"} & {field.name for field in self.state._meta.concrete_fields})

    def test_unowned_import_attaches_policy_when_missing_dependency_appears(self):
        policies = {
            "routemap_in": "example-in",
            "routemap_out": "example-out",
            "prefixlist_in": "example-prefix-in",
            "prefixlist_out": "example-prefix-out",
        }
        leaves = {"address_families": [{"af": "ipv4-unicast", "enabled": True, **policies}]}
        native = self.read(**leaves)
        af = BGPPeerAddressFamily.objects.get(assigned_object_id=native.pk)
        for field in policies:
            self.assertIsNone(getattr(af, f"{field}_id"))
        resolved = {
            field: (RouteMap if field.startswith("routemap") else PrefixList).objects.create(name=name)
            for field, name in policies.items()
        }
        for _ in range(2):
            self.read(**leaves)
            af.refresh_from_db()
            for field, policy in resolved.items():
                with self.subTest(field=field):
                    self.assertEqual(getattr(af, f"{field}_id"), policy.pk)
            self.assertEqual(self.state.status, "imported")

    def test_unowned_import_attaches_update_source_when_missing_interface_appears(self):
        native = self.read(source="Loopback1803")
        self.assertIsNone(native.update_source_id)
        interface = Interface.objects.create(device=self.device, name="Loopback1803", type="virtual")
        for _ in range(2):
            native = self.read(source=interface.name)
            self.assertEqual(native.update_source_id, interface.pk)
            self.assertEqual(self.state.status, "imported")

    def test_owned_unresolved_policy_cannot_settle_as_absent_native_policy(self):
        base = self.own("accepted")
        self.read(address_families=[{"af": "ipv4-unicast", "enabled": True, "routemap_in": "example-missing"}])
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        af = BGPPeerAddressFamily.objects.get(assigned_object_id=self.state.bgp_peer_id)
        self.assertIsNone(af.routemap_in_id)

    def test_owned_unresolved_update_source_cannot_settle_as_absent_native_source(self):
        base = self.own("accepted")
        native = self.read(source="Loopback1803")
        self.assertEqual((self.state.status, self.state.device_base_hash), ("accepted", base))
        self.assertIsNone(native.update_source_id)

    def test_peer_schema_contains_only_identity_link_and_lifecycle_fields(self):
        self.assertEqual(
            {field.name for field in NSOBGPPeerState._meta.concrete_fields},
            {
                "id",
                "created",
                "last_updated",
                "custom_field_data",
                "management",
                "asn_str",
                "vrf_name",
                "peer_address_str",
                "bgp_peer",
                "status",
                "last_sync_at",
                "accepted_at",
                "last_apply_at",
                "last_apply_error",
                "device_base_hash",
                "apply_attempt_id",
            },
        )

    def test_push_and_compare_call_the_same_peer_content_definition(self):
        from netbox_nso_plugin import bgp_reconciler, signals
        from netbox_nso_plugin.bgp_peer_content import peer_content

        for function in (signals.bgp_peer_intent_item, bgp_reconciler._BGPGraphPlanner.reconcile_peer):
            tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
            self.assertTrue(
                any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "bgp_peer_content"
                    and any(alias.name == "peer_content" for alias in node.names)
                    for node in ast.walk(tree)
                )
            )
            self.assertTrue(
                any(
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "peer_content"
                    for node in ast.walk(tree)
                )
            )
        self.assertEqual(
            peer_content(self.state.bgp_peer),
            {"enabled": True, "remote_as": "64513", "address_families": [{"af": "ipv4-unicast", "enabled": True}]},
        )

    def test_render_and_compare_call_the_same_peer_qualification_definition(self):
        from netbox_nso_plugin import bgp_reconciler, ownership_planner

        for function in (
            ownership_planner._require_qualifying_acquisition,
            bgp_reconciler._BGPGraphPlanner.reconcile_peer,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
            self.assertTrue(
                any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "bgp_peer_content"
                    and any(alias.name == "peer_binding_qualifies" for alias in node.names)
                    for node in ast.walk(tree)
                )
            )
            self.assertTrue(
                any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "peer_binding_qualifies"
                    for node in ast.walk(tree)
                )
            )

    def test_remote_asn_edit_advances_the_owned_peer_renderer_revision(self):
        from netbox_nso_plugin.models import NSOIntentRevision

        self.own("accepted")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="bgp")
        before = revision.revision
        content_update(self.state.bgp_peer.remote_as, asn=64517)
        revision.refresh_from_db()
        self.assertGreater(revision.revision, before)
        payload = delivery.render("bgp", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual(payload[0]["scopes"][0]["peers"][0]["remote_as"], "64517")

    def test_explicit_release_restores_unowned_merge_without_deleting_native(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        self.own("accepted")
        native_id = self.state.bgp_peer_id
        with without_commit_drain():
            self.state = content_update(self.state, status="imported", accepted_at=None)
            reconcile_scope_ownership(self.device.pk, ("bgp",))
        manifest = NSOOwnershipManifest.objects.get(state_model_label=self.state._meta.label_lower)
        self.assertEqual(manifest.ownership_state, "detached")
        native = self.read(remote_as="64514", enabled=False)
        self.assertEqual(
            (native.pk, native.remote_as.asn, native.enabled, self.state.status), (native_id, 64514, False, "imported")
        )
        payload = delivery.render("bgp", self.device.pk, self.management.adapter_device_id).payload
        self.assertEqual(payload[0]["scopes"][0]["peers"], [])


class TestPeerIntentMigration(_CascadeFlushMixin, TransactionTestCase):
    def test_migration_clears_only_peer_bases_and_rebaselines_without_native_overwrites(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes("netbox_nso_plugin")
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(leaves))
        device, management = make_managed("peer-migration", 1804)
        _reconcile_bgp_config(device, peer_document(device))
        peer = NSOBGPPeerState.objects.get(management=management)
        router = NSOBGPRouterState.objects.get(management=management)
        executor.migrate([("netbox_nso_plugin", "0036_direct_selection_capture")])
        old = executor.loader.project_state([("netbox_nso_plugin", "0036_direct_selection_capture")]).apps
        OldPeer = old.get_model("netbox_nso_plugin", "NSOBGPPeerState")
        OldPeer.objects.filter(pk=peer.pk).update(device_base_hash="a" * 64)
        owned = OldPeer.objects.create(
            management_id=management.pk,
            asn_str="64512",
            peer_address_str="198.18.0.3",
            status="accepted",
            device_base_hash="b" * 64,
        )
        old.get_model("netbox_nso_plugin", "NSOBGPPeerTemplateState").objects.create(
            management_id=management.pk, template_name="placeholder-template", device_base_hash="c" * 64
        )
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        new = executor.loader.project_state(leaves).apps.get_model("netbox_nso_plugin", "NSOBGPPeerState")
        self.assertEqual(set(new.objects.values_list("device_base_hash", flat=True)), {""})
        self.assertFalse({"remote_as_str", "enabled"} & {field.name for field in new._meta.concrete_fields})
        router.refresh_from_db()
        self.assertNotEqual(router.device_base_hash, "")
        self.assertEqual(NSOBGPPeerTemplateState.objects.get().device_base_hash, "c" * 64)
        _reconcile_bgp_config(device, peer_document(device))
        peer.refresh_from_db()
        self.assertEqual(peer.status, "imported")
        self.assertEqual(len(peer.device_base_hash), 64)
        self.assertEqual(peer.bgp_peer.remote_as.asn, 64513)
        with without_commit_drain():
            content_update(peer.bgp_peer, ttl=20)
            content_update(peer, device_base_hash="")
        _reconcile_bgp_config(device, peer_document(device))
        peer.refresh_from_db()
        self.assertEqual((peer.status, peer.bgp_peer.ttl), ("changed", 20))
        self.assertEqual(NSOBGPPeerState.objects.get(pk=owned.pk).device_base_hash, "")
        with without_commit_drain():
            content_update(peer, status="accepted", device_base_hash="")
        _reconcile_bgp_config(device, peer_document(device))
        peer.refresh_from_db()
        self.assertEqual((peer.status, peer.device_base_hash, peer.bgp_peer.ttl), ("accepted", "", 20))
