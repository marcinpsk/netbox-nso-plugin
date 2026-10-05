# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Exact policy identity through reconcile, acquisition, rendering, and the reset gate."""

from __future__ import annotations

import copy
import io
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from netbox_routing.models import (
    ASPath,
    CommunityList,
    PrefixList,
    PrefixListEntry,
    RouteMap,
    RouteMapEntry,
)

from netbox_nso_plugin import delivery, drain
from netbox_nso_plugin import shared_object_ownership as ownership
from netbox_nso_plugin.deployment import is_quiesced, resume
from netbox_nso_plugin.intent_state import offline_mutation, route_policy_footprint
from netbox_nso_plugin.models import (
    NSOIntentOutboxEntry,
    NSOIntentRevision,
    NSORoutePolicyObjectClass,
    NSORoutePolicyState,
)
from netbox_nso_plugin.reconcile import reconcile_category
from netbox_nso_plugin.renderer_audit import RendererAuditRepairFailed
from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, set_classification
from netbox_nso_plugin.signals import route_policy_intent_item, suppress_intent_push

from ._adapter_http import make_response
from ._outbox_case import ReceiptAdapter, content_update, make_managed, own_route, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_read_gate import _rs


class _PolicyAdapter(ReceiptAdapter):
    def __init__(self):
        super().__init__()
        self.captures = {}

    def _handle(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/route-policy"):
            device_id = int(url.split("/devices/")[1].split("/")[0])
            return make_response(200, self.captures[device_id])
        return super()._handle(method, url, **kwargs)


class TestExactPolicyIdentity(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device_a, self.mgmt_a = make_managed("policy-identity", 8101, index=1)
        self.device_b, self.mgmt_b = make_managed("policy-identity", 8102, index=2)
        self.adapter = _PolicyAdapter()
        self.user = get_user_model().objects.create_superuser("policy-identity-admin", "policy@example.test", "pw")
        self.client.force_login(self.user)
        self.addCleanup(self._release_gate)

    @staticmethod
    def _release_gate():
        if is_quiesced():
            resume()

    def _state(self, management, family, name):
        return NSORoutePolicyState.objects.get(management=management, family=family, object_name=name)

    def _accept(self, state):
        with without_commit_drain():
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:routing_accept_route_policy", args=[state.pk])
            )
        self.assertEqual(response.status_code, 302, response.content)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def _send(self, device):
        config, session = self.adapter.patches()
        with config, session:
            self.assertEqual(drain.drain_key(device.pk, "route_policy"), drain.SUCCEEDED)
        return self.adapter.requests[-1]["body"]["objects"]

    @staticmethod
    def _capture(family, name, alternate=False):
        entries = {
            "prefix_list": [
                {"sequence": 10, "action": "permit", "prefix": "198.18.1.0/24" if alternate else "198.18.0.0/24"}
            ],
            "community_list": [
                {"sequence": 10, "action": "permit", "community": "64512:2" if alternate else "64512:1"}
            ],
            "as_path": [{"sequence": 10, "action": "permit", "pattern": "^64513_" if alternate else "^64512_"}],
            "route_map": [{"sequence": 10, "action": "deny" if alternate else "permit"}],
        }
        return {"name": name, "entries": entries[family], **({"family": 4} if family == "prefix_list" else {})}

    @staticmethod
    def _payload(family, *captures):
        key = {
            "prefix_list": "prefix_lists",
            "community_list": "community_lists",
            "as_path": "as_paths",
            "route_map": "route_maps",
        }[family]
        return {key: list(captures)}

    def _offline(self, function):
        with transaction.atomic(), offline_mutation(), suppress_intent_push():
            return function()

    def test_case_pairs_complete_the_route_policy_scope_and_repeat_without_content_changes(self):
        payload = {
            "prefix_lists": [
                self._capture("prefix_list", "BGP_PEERS_V6"),
                self._capture("prefix_list", "BGP_PEERS_v6", True),
            ],
            "route_maps": [
                {
                    "name": "accept-all",
                    "entries": [{"sequence": 10, "action": "permit", "match_prefix_lists": ["BGP_PEERS_v6"]}],
                },
                {
                    "name": "ACCEPT-ALL",
                    "entries": [{"sequence": 10, "action": "deny", "match_prefix_lists": ["BGP_PEERS_V6"]}],
                },
            ],
            "read_state": _rs(),
        }
        self.adapter.captures[self.mgmt_a.adapter_device_id] = payload
        config, session = self.adapter.patches()
        with config, session:
            context = reconcile_category(self.device_a, self.mgmt_a, "route_policy")
        self.assertEqual(context["_gate"]["route_policy"], "ran")
        self.assertEqual(len(context["route_policy_states"]), 4)
        self.assertEqual(set(RouteMap.objects.values_list("name", flat=True)), {"accept-all", "ACCEPT-ALL"})
        self.assertEqual(set(PrefixList.objects.values_list("name", flat=True)), {"BGP_PEERS_V6", "BGP_PEERS_v6"})
        for name, reference in (("accept-all", "BGP_PEERS_v6"), ("ACCEPT-ALL", "BGP_PEERS_V6")):
            entry = RouteMapEntry.objects.get(route_map__name=name)
            self.assertEqual(list(entry.match_prefix_list.values_list("name", flat=True)), [reference])
            self.assertEqual(self._state(self.mgmt_a, "route_map", name).assigned_object.name, name)
        before_entries = list(RouteMapEntry.objects.values())
        before_revisions = list(
            NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
        )
        reconcile_route_policy(self.device_a, payload)
        self.assertEqual(list(RouteMapEntry.objects.values()), before_entries)
        self.assertEqual(
            list(
                NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
            ),
            before_revisions,
        )
        self.assertFalse(NSOIntentOutboxEntry.objects.exists())

    def test_accept_only_one_variant_and_its_exact_contributors(self):
        payload = {}
        for family in ("prefix_list", "community_list", "as_path"):
            payload.update(self._payload(family, self._capture(family, "LIST"), self._capture(family, "list", True)))
        payload["route_maps"] = [
            {
                "name": name,
                "entries": [
                    {
                        "sequence": 10,
                        "action": "permit",
                        "match_prefix_lists": [ref],
                        "match_community_lists": [ref],
                        "match_as_paths": [ref],
                        "set": json.dumps({"community_add": [ref]}),
                    }
                ],
            }
            for name, ref in (("POLICY", "LIST"), ("policy", "list"))
        ]
        reconcile_route_policy(self.device_a, payload)
        self._accept(self._state(self.mgmt_a, "route_map", "POLICY"))
        body = self._send(self.device_a)
        self.assertEqual(
            {(obj["family"], obj["name"]) for obj in body},
            {("route_map", "POLICY"), ("prefix_list", "LIST"), ("community_list", "LIST"), ("as_path", "LIST")},
        )
        entry = next(obj for obj in body if obj["family"] == "route_map")["entries"][0]
        self.assertEqual(entry["match-prefix-lists"], ["LIST"])
        self.assertEqual(entry["match-community-lists"], ["LIST"])
        self.assertEqual(entry["match-as-paths"], ["LIST"])
        for family, name in (
            ("route_map", "policy"),
            ("prefix_list", "list"),
            ("community_list", "list"),
            ("as_path", "list"),
        ):
            self.assertEqual(self._state(self.mgmt_a, family, name).status, "imported")

    def test_unresolved_match_references_are_preserved_and_refuse_accept(self):
        for family, key, marker, model in (
            ("prefix_list", "match_prefix_lists", "match_prefix_list", PrefixList),
            ("community_list", "match_community_lists", "match_community_list", CommunityList),
            ("as_path", "match_as_paths", "match_aspath", ASPath),
        ):
            with self.subTest(family=family):
                model.objects.create(name=f"missing-{family}")
                unresolved = f"MISSING-{family}"
                name = f"MATCH-{family}"
                reconcile_route_policy(
                    self.device_a,
                    {
                        "route_maps": [
                            {
                                "name": name,
                                "entries": [{"sequence": 10, "action": "permit", key: [unresolved, unresolved]}],
                            }
                        ]
                    },
                )
                entry = RouteMapEntry.objects.get(route_map__name=name)
                self.assertEqual(entry.vendor_ext["unmapped"][marker], [unresolved, unresolved])
                state = self._state(self.mgmt_a, "route_map", name)
                with self.assertRaisesRegex(RendererAuditRepairFailed, unresolved):
                    self._accept(state)
                state.refresh_from_db()
                self.assertEqual(state.status, "imported")
        self.assertEqual(self.adapter.requests, [])
        self.assertFalse(NSOIntentOutboxEntry.objects.exists())

    def test_cross_variant_binding_refuses_render_and_publication(self):
        from netbox_nso_plugin.signals import RoutePolicyBindingInvalid

        native = RouteMap.objects.create(name="accept-all")
        state = self._offline(
            lambda: NSORoutePolicyState.objects.create(
                management=self.mgmt_a,
                family="route_map",
                object_name="ACCEPT-ALL",
                content_type=ContentType.objects.get_for_model(RouteMap),
                object_id=native.pk,
                status="accepted",
            )
        )
        with self.assertRaisesRegex(RoutePolicyBindingInvalid, "ACCEPT-ALL.*accept-all"):
            route_policy_intent_item(state)
        with self.assertRaises(RoutePolicyBindingInvalid):
            delivery.render("route_policy", self.device_a.pk, self.mgmt_a.adapter_device_id)
        self.assertEqual(self.adapter.requests, [])

    def test_owned_native_contents_survive_an_old_cross_variant_materialized_owner(self):
        capture = self._capture("prefix_list", "policy")
        reconcile_route_policy(self.device_b, self._payload("prefix_list", capture))
        state = self._state(self.mgmt_b, "prefix_list", "policy")
        self._accept(state)
        content_update(state, is_materialized=False)
        root = state.assigned_object
        entry = PrefixListEntry.objects.get(prefix_list=root)
        self._offline(lambda: PrefixListEntry.objects.filter(pk=entry.pk).update(action="deny", le=32))
        self._offline(
            lambda: NSORoutePolicyState.objects.create(
                management=self.mgmt_a,
                family="prefix_list",
                object_name="POLICY",
                content_type=state.content_type,
                object_id=root.pk,
                status="imported",
                captured=self._capture("prefix_list", "POLICY"),
                is_materialized=True,
            )
        )
        before = list(PrefixListEntry.objects.filter(prefix_list=root).values())
        before_outbox = list(NSOIntentOutboxEntry.objects.values())
        for status in ("accepted", "deploying", "in_sync", "apply_failed"):
            with self.subTest(status=status):
                self._offline(
                    lambda: NSORoutePolicyState.objects.filter(pk=state.pk).update(
                        status=status, apply_attempt_id=uuid4() if status == "deploying" else None
                    )
                )
                reconcile_route_policy(self.device_b, self._payload("prefix_list", capture))
                state.refresh_from_db()
                self.assertEqual(state.status, status)
                self.assertEqual(list(PrefixListEntry.objects.filter(prefix_list=root).values()), before)
                self.assertEqual(list(NSOIntentOutboxEntry.objects.values()), before_outbox)
        self.assertEqual(self.adapter.requests, [])

    def test_native_rename_rebinds_unowned_peers_and_stale_owner_targets_the_exact_root(self):
        capture = self._capture("prefix_list", "POLICY")
        reconcile_route_policy(self.device_a, self._payload("prefix_list", capture))
        reconcile_route_policy(self.device_b, self._payload("prefix_list", capture))
        departing = self._state(self.mgmt_a, "prefix_list", "POLICY")
        old_root = departing.assigned_object
        content_update(old_root, name="policy")
        before = list(PrefixListEntry.objects.filter(prefix_list=old_root).values())
        reconcile_route_policy(
            self.device_b,
            self._payload(
                "prefix_list", {**capture, "entries": [{"sequence": 10, "action": "permit", "prefix": "198.18.2.0/24"}]}
            ),
        )
        live = self._state(self.mgmt_b, "prefix_list", "POLICY")
        self.assertNotEqual(live.object_id, old_root.pk)
        self.assertEqual(live.assigned_object.name, "POLICY")
        reconcile_route_policy(self.device_a, {})
        live.refresh_from_db()
        self.assertEqual(live.assigned_object.name, "POLICY")
        self.assertEqual(
            str(PrefixListEntry.objects.get(prefix_list=live.assigned_object).assigned_prefix.prefix), "198.18.2.0/24"
        )
        old_root.refresh_from_db()
        self.assertEqual(old_root.name, "policy")
        self.assertEqual(list(PrefixListEntry.objects.filter(prefix_list=old_root).values()), before)

    def test_explicit_rematerialization_of_an_unowned_renamed_target_creates_the_exact_root(self):
        capture = self._capture("prefix_list", "POLICY")
        reconcile_route_policy(self.device_a, self._payload("prefix_list", capture))
        state = self._state(self.mgmt_a, "prefix_list", "POLICY")
        root = state.assigned_object
        content_update(root, name="policy")
        before = list(PrefixListEntry.objects.filter(prefix_list=root).values())
        ownership.rematerialize(state)
        state.refresh_from_db()
        self.assertEqual(state.assigned_object.name, "POLICY")
        self.assertNotEqual(state.object_id, root.pk)
        self.assertEqual(list(PrefixListEntry.objects.filter(prefix_list=root).values()), before)

    def test_owned_renamed_target_refuses_reconcile_instead_of_rebinding(self):
        from netbox_nso_plugin.signals import RoutePolicyBindingInvalid

        capture = self._capture("prefix_list", "POLICY")
        reconcile_route_policy(self.device_a, self._payload("prefix_list", capture))
        state = self._state(self.mgmt_a, "prefix_list", "POLICY")
        self._accept(state)
        root = state.assigned_object
        self._offline(lambda: PrefixList.objects.filter(pk=root.pk).update(name="policy"))
        with self.assertRaises(RoutePolicyBindingInvalid):
            reconcile_route_policy(self.device_a, self._payload("prefix_list", capture))
        state.refresh_from_db()
        self.assertEqual(state.object_id, root.pk)
        self.assertFalse(PrefixList.objects.filter(name="POLICY").exists())

    def test_community_classifier_prefers_exact_lists_then_literals_and_refuses_unknown_names(self):
        CommunityList.objects.create(name="100")
        CommunityList.objects.create(name="no-export")
        captures = [
            {
                "name": name,
                "entries": [
                    {
                        "sequence": 10,
                        "action": "permit",
                        "set": json.dumps({"community": target, "community_additive": True}),
                    }
                ],
            }
            for name, target in (
                ("DECIMAL", "4259840001"),
                ("NUMERIC-LIST", "100"),
                ("KEYWORD-LIST", "no-export"),
                ("MISSING", "X"),
            )
        ]
        reconcile_route_policy(self.device_a, {"route_maps": captures})
        literal = RouteMapEntry.objects.get(route_map__name="DECIMAL")
        self.assertFalse((literal.vendor_ext or {}).get("unmapped"))
        set_row = literal.set_communities.get()
        self.assertIsNone(set_row.community_list_id)
        self.assertEqual(list(set_row.communities.values_list("community", flat=True)), ["4259840001"])
        self._accept(self._state(self.mgmt_a, "route_map", "DECIMAL"))
        rendered = self._send(self.device_a)
        self.assertEqual(json.loads(rendered[0]["entries"][0]["set-json"])["community"], "4259840001")
        for name, expected in (("NUMERIC-LIST", "100"), ("KEYWORD-LIST", "no-export")):
            self.assertEqual(
                RouteMapEntry.objects.get(route_map__name=name).set_communities.get().community_list.name, expected
            )
        missing = RouteMapEntry.objects.get(route_map__name="MISSING")
        self.assertEqual(missing.vendor_ext["unmapped"]["set_community"], [{"operation": "add", "name": "X"}])
        with self.assertRaisesRegex(RendererAuditRepairFailed, "X"):
            self._accept(self._state(self.mgmt_a, "route_map", "MISSING"))

    def test_decimal_community_bounds_and_unknown_afi_marker(self):
        for target in ("0", "4294967295", "4294967296", "-1"):
            name = f"DECIMAL-{target}"
            reconcile_route_policy(
                self.device_a,
                {
                    "route_maps": [
                        {
                            "name": name,
                            "entries": [{"sequence": 10, "action": "permit", "set": json.dumps({"community": target})}],
                        }
                    ]
                },
            )
            entry = RouteMapEntry.objects.get(route_map__name=name)
            if target in ("0", "4294967295"):
                self.assertEqual(
                    list(entry.set_communities.get().communities.values_list("community", flat=True)), [target]
                )
            else:
                self.assertEqual(entry.vendor_ext["unmapped"]["set_community"], [{"operation": "set", "name": target}])
        reconcile_route_policy(
            self.device_b,
            {
                "route_maps": [
                    {
                        "name": "AFI",
                        "entries": [
                            {"sequence": 10, "action": "permit", "match": json.dumps({"family": ["mvpn-ipv4"]})}
                        ],
                    }
                ]
            },
        )
        state = self._state(self.mgmt_b, "route_map", "AFI")
        self._accept(state)
        self.assertEqual(route_policy_intent_item(state)["name"], "AFI")

    def test_each_family_keeps_the_other_case_graph_ownership_and_revisions_isolated(self):
        device_c, mgmt_c = make_managed("policy-identity", 8103, index=3)
        for family in ("route_map", "prefix_list", "as_path", "community_list"):
            with self.subTest(family=family):
                upper, lower = f"ISOLATE-{family}", f"isolate-{family}"
                payload = self._payload(family, self._capture(family, upper), self._capture(family, lower, True))
                reconcile_route_policy(self.device_a, payload)
                reconcile_route_policy(self.device_b, self._payload(family, self._capture(family, upper, True)))
                reconcile_route_policy(device_c, self._payload(family, self._capture(family, lower, True)))
                other = self._state(self.mgmt_a, family, lower)
                self._accept(other)
                self._accept(self._state(mgmt_c, family, lower))
                native = other.assigned_object
                caller = RouteMap.objects.create(name=f"CALLER-{family}")
                reference = RouteMapEntry.objects.create(route_map=caller, sequence=1, action="permit")
                if family == "route_map":
                    content_update(reference, call_policy=native)
                else:
                    field = {
                        "prefix_list": "match_prefix_list",
                        "community_list": "match_community_list",
                        "as_path": "match_aspath",
                    }[family]
                    getattr(reference, field).add(native)
                before_content = ownership.get_spec(family).extract(native)
                before_states = list(
                    NSORoutePolicyState.objects.filter(family=family, object_name=lower)
                    .order_by("pk")
                    .values_list("pk", "status", "object_id", "is_materialized", "content_hash")
                )
                before_revisions = list(
                    NSOIntentRevision.objects.filter(device=device_c).order_by("scope").values_list("scope", "revision")
                )
                before_reference = list(RouteMapEntry.objects.filter(pk=reference.pk).values())
                before_edges = (
                    list(getattr(reference, field).values_list("pk", flat=True)) if family != "route_map" else []
                )
                footprint = route_policy_footprint(((family, upper),))
                self.assertEqual(set(footprint.shared_keys), {("route-policy", f"{family}:{upper}")})
                self.assertNotIn((device_c.pk, "route_policy"), footprint.revision_keys)
                self._accept(self._state(self.mgmt_a, family, upper))
                set_classification(family, upper, "local")
                set_classification(family, upper, "master")
                ownership.rematerialize(self._state(self.mgmt_b, family, upper))
                reconcile_route_policy(self.device_a, self._payload(family, self._capture(family, lower, True)))
                self.assertEqual(ownership.get_spec(family).extract(native), before_content)
                self.assertEqual(
                    list(
                        NSORoutePolicyState.objects.filter(family=family, object_name=lower)
                        .order_by("pk")
                        .values_list("pk", "status", "object_id", "is_materialized", "content_hash")
                    ),
                    before_states,
                )
                self.assertEqual(
                    list(
                        NSOIntentRevision.objects.filter(device=device_c)
                        .order_by("scope")
                        .values_list("scope", "revision")
                    ),
                    before_revisions,
                )
                self.assertEqual(list(RouteMapEntry.objects.filter(pk=reference.pk).values()), before_reference)
                if family != "route_map":
                    self.assertEqual(list(getattr(reference, field).values_list("pk", flat=True)), before_edges)
                before_revisions = list(
                    NSOIntentRevision.objects.order_by("device_id", "scope").values_list(
                        "device_id", "scope", "revision"
                    )
                )
                reconcile_route_policy(self.device_a, self._payload(family, self._capture(family, lower, True)))
                self.assertEqual(
                    list(
                        NSOIntentRevision.objects.order_by("device_id", "scope").values_list(
                            "device_id", "scope", "revision"
                        )
                    ),
                    before_revisions,
                )

    def test_exact_duplicate_native_names_still_fail(self):
        for model in (RouteMap, PrefixList, ASPath, CommunityList):
            with self.subTest(model=model):
                model.objects.create(name="pair")
                model.objects.create(name="PAIR")
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.create(name="pair")

    def test_reset_runbook_checks_the_claim_then_prepares_deletes_resumes_reconciles_accepts_and_verifies(self):
        lower = RouteMap.objects.create(name="policy")
        RouteMapEntry.objects.create(route_map=lower, sequence=1, action="permit")
        for management in (self.mgmt_a, self.mgmt_b):
            for name in ("POLICY", "policy"):
                self._offline(
                    lambda: NSORoutePolicyState.objects.create(
                        management=management,
                        family="route_map",
                        object_name=name,
                        content_type=ContentType.objects.get_for_model(RouteMap),
                        object_id=lower.pk,
                        status="accepted",
                    )
                )
        for name in ("POLICY", "policy"):
            NSORoutePolicyObjectClass.objects.create(family="route_map", object_name=name, mode="master")
        own_route(self.mgmt_a, "198.18.10.0/24", "198.18.0.1")
        pending = drain.claim(self.device_a.pk, "static_route")
        self.assertIsNotNone(pending)
        before_states = list(NSORoutePolicyState.objects.values())
        before_classes = list(NSORoutePolicyObjectClass.objects.values())
        with patch(
            "netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep", new=lambda _seconds: None
        ):
            with self.assertRaisesRegex(CommandError, "Deployment gate blocked"):
                call_command("nso_intent_deployment_gate", prepare=True, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertFalse(is_quiesced())
        self.assertEqual(list(NSORoutePolicyState.objects.values()), before_states)
        self.assertEqual(list(NSORoutePolicyObjectClass.objects.values()), before_classes)
        config, session = self.adapter.patches()
        with config, session:
            response = drain.send_claim(pending)
            self.assertEqual(drain.settle(pending, response), drain.SUCCEEDED)
        with patch(
            "netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep", new=lambda _seconds: None
        ):
            call_command("nso_intent_deployment_gate", prepare=True, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertTrue(is_quiesced())
        self.assertEqual(drain.gate_blockers(), [])
        with transaction.atomic(), offline_mutation(), suppress_intent_push():
            NSORoutePolicyState.objects.all().delete()
            NSORoutePolicyObjectClass.objects.all().delete()
        self.assertFalse(NSORoutePolicyState.objects.exists())
        self.assertFalse(NSORoutePolicyObjectClass.objects.exists())
        resume()
        self.assertFalse(is_quiesced())
        payload = {
            "route_maps": [self._capture("route_map", "POLICY"), self._capture("route_map", "policy", True)],
            "read_state": _rs(),
        }
        config, session = self.adapter.patches()
        with config, session:
            for device, management in ((self.device_a, self.mgmt_a), (self.device_b, self.mgmt_b)):
                self.adapter.captures[management.adapter_device_id] = copy.deepcopy(payload)
                context = reconcile_category(device, management, "route_policy")
                self.assertEqual(context["_gate"]["route_policy"], "ran")
                for name in ("POLICY", "policy"):
                    self._accept(self._state(management, "route_map", name))
                self.assertEqual(drain.drain_key(device.pk, "route_policy"), drain.SUCCEEDED)
            self.assertEqual(NSORoutePolicyState.objects.count(), 4)
            self.assertEqual(set(RouteMap.objects.values_list("name", flat=True)), {"POLICY", "policy"})
            self.assertEqual(RouteMapEntry.objects.get(route_map__name="POLICY").action, "permit")
            self.assertEqual(RouteMapEntry.objects.get(route_map__name="policy").action, "deny")
            for state in NSORoutePolicyState.objects.all():
                self.assertEqual(route_policy_intent_item(state)["name"], state.assigned_object.name)
            self.assertEqual(drain.gate_blockers(), [])
            with patch(
                "netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep", new=lambda _seconds: None
            ):
                call_command("nso_intent_deployment_gate", prepare=True, stdout=io.StringIO(), stderr=io.StringIO())
            stdout = io.StringIO()
            call_command(
                "nso_intent_deployment_gate",
                verify=True,
                device_id=self.device_a.pk,
                stdout=stdout,
                stderr=io.StringIO(),
            )
        self.assertIn("Deployment verification passed", stdout.getvalue())
        self.assertFalse(is_quiesced())
        self.assertEqual(drain.gate_blockers(), [])

    def test_reference_report_is_read_only_and_lists_old_unmarked_missing_edges(self):
        capture = {"name": "OLD-GRAPH", "entries": [{"sequence": 10, "action": "permit", "match_prefix_lists": ["X"]}]}
        root = RouteMap.objects.create(name=capture["name"])
        RouteMapEntry.objects.create(route_map=root, sequence=1, action="permit")
        self._offline(
            lambda: NSORoutePolicyState.objects.create(
                management=self.mgmt_a,
                family="route_map",
                object_name=root.name,
                content_type=ContentType.objects.get_for_model(RouteMap),
                object_id=root.pk,
                status="accepted",
                captured=capture,
            )
        )
        before_entries = list(RouteMapEntry.objects.values())
        before_revisions = list(NSOIntentRevision.objects.values())
        stdout = io.StringIO()
        call_command("nso_route_policy_reference_report", stdout=stdout)
        records = json.loads(stdout.getvalue())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["object_name"], "OLD-GRAPH")
        self.assertEqual(records[0]["missing"], {"match_prefix_list": ["X"]})
        self.assertEqual(list(RouteMapEntry.objects.values()), before_entries)
        self.assertEqual(list(NSOIntentRevision.objects.values()), before_revisions)

    def test_stale_owner_creates_an_exact_root_before_a_peer_reconciles_the_native_rename(self):
        capture = self._capture("prefix_list", "POLICY")
        reconcile_route_policy(self.device_a, self._payload("prefix_list", capture))
        reconcile_route_policy(self.device_b, self._payload("prefix_list", capture))
        state = self._state(self.mgmt_a, "prefix_list", "POLICY")
        old_root = state.assigned_object
        content_update(old_root, name="policy")
        before = list(PrefixListEntry.objects.filter(prefix_list=old_root).values())
        reconcile_route_policy(self.device_a, {})
        live = self._state(self.mgmt_b, "prefix_list", "POLICY")
        self.assertEqual(live.assigned_object.name, "POLICY")
        self.assertNotEqual(live.object_id, old_root.pk)
        self.assertEqual(list(PrefixListEntry.objects.filter(prefix_list=old_root).values()), before)

    def test_casing_only_owned_rename_marks_the_old_capture_stale_and_creates_a_new_state(self):
        capture = self._capture("prefix_list", "POLICY")
        reconcile_route_policy(self.device_a, self._payload("prefix_list", capture))
        old = self._state(self.mgmt_a, "prefix_list", "POLICY")
        self._accept(old)
        content_update(old, status="in_sync")
        reconcile_route_policy(
            self.device_a, self._payload("prefix_list", self._capture("prefix_list", "policy", True))
        )
        old.refresh_from_db()
        self.assertFalse(old.device_present)
        self.assertEqual(old.status, "changed")
        current = self._state(self.mgmt_a, "prefix_list", "policy")
        self.assertNotEqual(current.pk, old.pk)
        self.assertEqual(current.assigned_object.name, "policy")
        self.assertEqual(old.assigned_object.name, "POLICY")

    def test_rename_moves_only_exact_bgp_redistribution_and_calling_policy_consumers(self):
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import (
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPRouter,
            BGPScope,
            OSPFInstance,
        )

        from netbox_nso_plugin.models import NSOBGPPeerState, NSOOSPFInstanceState, NSORedistributionState

        from ._ownership_case import acquire_overlay

        rir = RIR.objects.create(name="Policy private", slug="policy-private", is_private=True)
        local = ASN.objects.create(asn=64512, rir=rir)
        remote = ASN.objects.create(asn=64513, rir=rir)
        fallbacks = []
        peer_families = []
        for index, (device, management, name) in enumerate(
            ((self.device_a, self.mgmt_a, "POLICY"), (self.device_b, self.mgmt_b, "policy")), start=1
        ):
            reconcile_route_policy(
                device,
                {
                    "route_maps": [
                        self._capture("route_map", name),
                        {
                            "name": f"CALLER-{index}",
                            "entries": [
                                {
                                    "sequence": 10,
                                    "action": "permit",
                                    "match": json.dumps({"_junos_from_policy": [name]}),
                                }
                            ],
                        },
                    ]
                },
            )
            root = RouteMap.objects.get(name=name)
            self._accept(self._state(management, "route_map", name))
            self._accept(self._state(management, "route_map", f"CALLER-{index}"))
            NSORoutePolicyObjectClass.objects.create(family="route_map", object_name=name, mode="master")
            with without_commit_drain():
                router = BGPRouter.objects.create(
                    name=f"policy-router-{index}",
                    assigned_object_type=ContentType.objects.get_for_model(device),
                    assigned_object_id=device.pk,
                    asn=local,
                )
                scope = BGPScope.objects.create(router=router)
                family = BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
                peer = BGPPeer.objects.create(
                    scope=scope,
                    peer=IPAddress.objects.create(address=f"198.18.0.{index}/32"),
                    remote_as=remote,
                    enabled=True,
                )
                peer_families.append(
                    BGPPeerAddressFamily.objects.create(
                        assigned_object_type=ContentType.objects.get_for_model(peer),
                        assigned_object_id=peer.pk,
                        address_family=family,
                        enabled=True,
                        routemap_in=root,
                    )
                )
                acquire_overlay(
                    NSOBGPPeerState,
                    management=management,
                    bgp_peer=peer,
                    asn_str="64512",
                    peer_address_str=f"198.18.0.{index}",
                    remote_as_str="64513",
                    status="accepted",
                )
                OSPFInstance.objects.create(
                    device=device, name=f"policy-ospf-{index}", process_id="7", router_id=f"198.18.1.{index}"
                )
                acquire_overlay(NSOOSPFInstanceState, management=management, process_id="7", status="accepted")
                fallbacks.append(
                    acquire_overlay(
                        NSORedistributionState,
                        management=management,
                        dest_protocol="ospf",
                        dest_ref="7",
                        source_protocol="connected",
                        route_map=name,
                        status="accepted",
                    )
                )
        before_b = list(
            NSOIntentRevision.objects.filter(device=self.device_b).order_by("scope").values_list("scope", "revision")
        )
        state = self._state(self.mgmt_a, "route_map", "POLICY")
        with without_commit_drain():
            response = self.client.post(
                reverse(
                    "plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "route_map_name", "pk": state.pk}
                ),
                {"object_name": "Policy"},
            )
        self.assertEqual(response.status_code, 200, response.content)
        for row in fallbacks:
            row.refresh_from_db()
        for row in peer_families:
            row.refresh_from_db()
        self.assertEqual([row.route_map for row in fallbacks], ["Policy", "policy"])
        self.assertEqual([row.routemap_in.name for row in peer_families], ["Policy", "policy"])
        self.assertEqual(RouteMapEntry.objects.get(route_map__name="CALLER-1").call_policy.name, "Policy")
        self.assertEqual(RouteMapEntry.objects.get(route_map__name="CALLER-2").call_policy.name, "policy")
        self.assertEqual(
            list(
                NSOIntentRevision.objects.filter(device=self.device_b)
                .order_by("scope")
                .values_list("scope", "revision")
            ),
            before_b,
        )
        self.assertEqual(
            set(NSORoutePolicyObjectClass.objects.values_list("object_name", flat=True)), {"Policy", "policy"}
        )
        self.assertIn(
            '"routemap_in": "Policy"',
            json.dumps(delivery.render("bgp", self.device_a.pk, self.mgmt_a.adapter_device_id).payload),
        )
        self.assertIn(
            '"route_map": "Policy"',
            json.dumps(delivery.render("ospf", self.device_a.pk, self.mgmt_a.adapter_device_id).payload),
        )

    @staticmethod
    def _native_reference_capture():
        PrefixList.objects.create(name="LIST")
        CommunityList.objects.create(name="100")
        ASPath.objects.create(name="PATH")
        RouteMap.objects.create(name="SUB")
        return {
            "route_maps": [
                {
                    "name": "POLICY",
                    "entries": [
                        {
                            "sequence": 10,
                            "action": "permit",
                            "match_prefix_lists": ["LIST"],
                            "match_community_lists": ["100"],
                            "match_as_paths": ["PATH"],
                            "match": json.dumps({"_junos_from_policy": ["SUB"]}),
                            "set": json.dumps({"community_add": ["100"]}),
                        }
                    ],
                }
            ]
        }

    def _assert_native_references(self, management):
        entry = RouteMapEntry.objects.get(route_map__name="POLICY")
        self.assertEqual(list(entry.match_prefix_list.values_list("name", flat=True)), ["LIST"])
        self.assertEqual(list(entry.match_community_list.values_list("name", flat=True)), ["100"])
        self.assertEqual(list(entry.match_aspath.values_list("name", flat=True)), ["PATH"])
        self.assertEqual(entry.call_policy.name, "SUB")
        self.assertEqual(entry.set_communities.get().community_list.name, "100")
        self.assertFalse((entry.vendor_ext or {}).get("unmapped"))
        self._accept(self._state(management, "route_map", "POLICY"))

    def test_stale_owner_rematerialization_resolves_exact_native_references_from_the_peer_capture(self):
        payload = self._native_reference_capture()
        reconcile_route_policy(self.device_a, payload)
        reconcile_route_policy(self.device_b, payload)
        reconcile_route_policy(self.device_a, {})
        self._assert_native_references(self.mgmt_b)

    def test_classification_resolves_exact_native_references_before_community_literals(self):
        payload = self._native_reference_capture()
        reconcile_route_policy(self.device_a, payload)
        set_classification("route_map", "POLICY", "local")
        set_classification("route_map", "POLICY", "master")
        self._assert_native_references(self.mgmt_a)

    def test_renamed_community_list_with_a_conflicting_capture_keeps_its_planned_reference(self):
        payload = self._payload("community_list", self._capture("community_list", "POLICY"))
        reconcile_route_policy(self.device_a, payload)
        reconcile_route_policy(self.device_b, payload)
        original = CommunityList.objects.get(name="POLICY")
        content_update(original, name="policy")
        changed = self._payload("community_list", self._capture("community_list", "POLICY", True))
        changed.update(
            {
                "route_maps": [
                    {
                        "name": "MATCH-POLICY",
                        "entries": [{"sequence": 10, "action": "permit", "match_community_lists": ["POLICY"]}],
                    }
                ],
                "read_state": _rs(),
            }
        )
        self.adapter.captures[self.mgmt_b.adapter_device_id] = changed
        config, session = self.adapter.patches()
        with config, session:
            context = reconcile_category(self.device_b, self.mgmt_b, "route_policy")
        self.assertEqual(context["_gate"]["route_policy"], "ran")
        state = self._state(self.mgmt_b, "community_list", "POLICY")
        self.assertEqual(state.status, "conflict")
        self.assertEqual(state.assigned_object.name, "POLICY")
        self.assertNotEqual(state.object_id, original.pk)
        self.assertFalse(state.assigned_object.communitylistentries.exists())
        entry = RouteMapEntry.objects.get(route_map__name="MATCH-POLICY")
        self.assertEqual(list(entry.match_community_list.values_list("name", flat=True)), ["POLICY"])
        self.assertFalse(entry.match_community.exists())
        self.assertFalse((entry.vendor_ext or {}).get("unmapped"))
        original.refresh_from_db()
        self.assertEqual(original.name, "policy")
        self.assertEqual(ownership.get_spec("community_list").extract(original)["entries"][0]["community"], "64512:1")
        self.assertFalse(NSOIntentOutboxEntry.objects.exists())
