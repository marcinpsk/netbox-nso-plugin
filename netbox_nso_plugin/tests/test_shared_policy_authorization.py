# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shared policy actions require the whole management group and native graph."""

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.urls import reverse
from netbox_routing.models import (
    Community,
    CustomPrefix,
    PrefixList,
    PrefixListEntry,
    RouteMap,
    RouteMapEntry,
    RouteMapEntrySetCommunity,
)

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOIntentOutboxState,
    NSOIntentRevision,
    NSORoutePolicyObjectClass,
    NSORoutePolicyState,
)
from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy, set_classification
from netbox_nso_plugin.shared_object_ownership import hash_captured

from ._authority_case import AdapterCase, grant
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import save_overlay_fixture

User = get_user_model()


class TestSharedPolicyAuthorization(AdapterCase):
    """Real requests, permissions, policy entries, ownership, revisions and outbox."""

    def setUp(self):
        super().setUp()
        self.first, self.first_management = make_managed("policy-auth-first", 941)
        self.second, self.second_management = make_managed("policy-auth-second", 942)
        with without_commit_drain():
            reconcile_route_policy(self.first, self._payload("Z-SHARED", "198.18.0.0/24"))
            reconcile_route_policy(
                self.second,
                {
                    "prefix_lists": [
                        *self._payload("A-PRIVATE", "198.18.1.0/24")["prefix_lists"],
                        *self._payload("Z-SHARED", "198.18.2.0/24")["prefix_lists"],
                    ]
                },
            )
        self.selected = NSORoutePolicyState.objects.get(management=self.second_management, object_name="Z-SHARED")
        self.user = User.objects.create_user(username="shared-policy-operator")

    @staticmethod
    def _payload(name, prefix):
        return {
            "prefix_lists": [
                {"name": name, "family": 4, "entries": [{"sequence": 10, "action": "permit", "prefix": prefix}]}
            ]
        }

    def _login(self, *, all_managements=False, native=True):
        constraints = None if all_managements else {"pk": self.second_management.pk}
        grant(self.user, ("view", "change"), NSODeviceManagement, constraints)
        grant(self.user, ("view",), Device, None if all_managements else {"pk": self.second.pk})
        if native:
            for model in (PrefixList, PrefixListEntry, CustomPrefix):
                grant(self.user, ("view", "add", "change", "delete"), model)
        self.client.force_login(self.user)

    def _post(self, action, data=None):
        return self.client.post(
            reverse(f"plugins:netbox_nso_plugin:routing_{action}_route_policy", kwargs={"pk": self.selected.pk}),
            data or {},
        )

    def _snapshot(self):
        return {
            model._meta.label_lower: list(model.objects.order_by("pk").values())
            for model in (
                PrefixList,
                PrefixListEntry,
                CustomPrefix,
                Community,
                RouteMap,
                RouteMapEntry,
                RouteMapEntrySetCommunity,
                RouteMapEntry.match_prefix_list.through,
                RouteMapEntrySetCommunity.communities.through,
                NSORoutePolicyState,
                NSORoutePolicyObjectClass,
                NSOIntentRevision,
                NSOIntentOutboxEntry,
                NSOIntentOutboxState,
            )
        }

    def _assert_denied(self, action, status, data=None):
        before = self._snapshot()
        with self.assert_refused_whole():
            response = self._post(action, data)
        self.assertEqual(response.status_code, status)
        self.assertEqual(self._snapshot(), before)

    def test_classify_local_refuses_a_hidden_group_management(self):
        self._login()
        self._assert_denied("classify", 404, {"mode": "local"})

    def test_classify_master_refuses_a_hidden_group_management(self):
        self._login()
        self._assert_denied("classify", 404, {"mode": "master"})

    def test_materialize_refuses_a_hidden_group_management(self):
        self._login()
        self._assert_denied("materialize", 404)

    def test_classify_includes_group_rows_without_captures(self):
        sibling = NSORoutePolicyState.objects.get(management=self.first_management, object_name="Z-SHARED")
        sibling.captured = {}
        save_overlay_fixture(sibling, update_fields=("captured",))
        self._login()
        self._assert_denied("classify", 404, {"mode": "local"})

    def test_bulk_classify_rolls_back_an_earlier_authorized_group(self):
        self._login()
        private = self._selectable_private_row()
        before = self._snapshot()
        with self.assert_refused_whole():
            response = self.client.post(
                reverse(
                    "plugins:netbox_nso_plugin:routing_classify_bulk_route_policy",
                    kwargs={"device_pk": self.second.pk},
                ),
                {"state": [private.pk, self.selected.pk]},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._snapshot(), before)

    def test_classify_local_needs_no_native_write_permission(self):
        self._login(all_managements=True, native=False)
        before = list(PrefixListEntry.objects.order_by("pk").values())
        response = self._post("classify", {"mode": "local"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(PrefixListEntry.objects.order_by("pk").values()), before)
        rows = NSORoutePolicyState.objects.filter(object_name="Z-SHARED")
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(not row.is_materialized and row.object_id is None for row in rows))
        self.assertEqual(NSORoutePolicyObjectClass.objects.get(object_name="Z-SHARED").mode, "local")

    def test_materialize_requires_child_delete_permission_and_rolls_back(self):
        self._login(all_managements=True, native=False)
        grant(self.user, ("view", "change"), PrefixList)
        grant(self.user, ("view", "add", "change"), PrefixListEntry)
        grant(self.user, ("view", "add", "change", "delete"), CustomPrefix)
        self._assert_denied("materialize", 403)

    def test_classify_master_requires_child_add_permission_and_rolls_back_classification(self):
        self._login(all_managements=True, native=False)
        grant(self.user, ("view", "add", "change", "delete"), PrefixList)
        grant(self.user, ("view", "change", "delete"), PrefixListEntry)
        grant(self.user, ("view", "add", "change", "delete"), CustomPrefix)
        self._assert_denied("classify", 403, {"mode": "master"})

    def test_materialize_checks_conditional_support_add_constraints(self):
        self._login(all_managements=True, native=False)
        grant(self.user, ("view", "add", "change", "delete"), PrefixList)
        grant(self.user, ("view", "add", "change", "delete"), PrefixListEntry)
        grant(self.user, ("view", "change", "delete"), CustomPrefix)
        grant(self.user, ("add",), CustomPrefix, {"prefix": "198.18.99.0/24"})
        self._assert_denied("materialize", 403)

    def test_materialize_with_all_authority_repoints_the_whole_group(self):
        self._login(all_managements=True)
        response = self._post("materialize")
        self.assertEqual(response.status_code, 302)
        self.selected.refresh_from_db()
        sibling = NSORoutePolicyState.objects.get(management=self.first_management, object_name="Z-SHARED")
        self.assertTrue(self.selected.is_materialized)
        self.assertFalse(sibling.is_materialized)
        self.assertEqual(sibling.status, "conflict")
        entry = PrefixListEntry.objects.get(prefix_list__name="Z-SHARED")
        self.assertEqual(str(entry.assigned_prefix.prefix), "198.18.2.0/24")

    def test_classification_without_a_request_keeps_its_existing_behavior(self):
        set_classification("prefix_list", "Z-SHARED", "local")
        self.assertEqual(NSORoutePolicyObjectClass.objects.get(object_name="Z-SHARED").mode, "local")

    def _route_map_group(self):
        for device, community in ((self.first, "64512:1"), (self.second, "64512:2")):
            with without_commit_drain():
                reconcile_route_policy(
                    device,
                    {
                        "prefix_lists": list(
                            NSORoutePolicyState.objects.filter(management__device=device, family="prefix_list")
                            .order_by("object_name")
                            .values_list("captured", flat=True)
                        ),
                        "route_maps": [
                            {
                                "name": "SHARED-MAP",
                                "entries": [
                                    {
                                        "action": "permit",
                                        "match_prefix_lists": ["Z-SHARED"],
                                        "set": {"community_add": [community]},
                                    }
                                ],
                            }
                        ],
                    },
                )
        self.selected = NSORoutePolicyState.objects.get(management=self.second_management, object_name="SHARED-MAP")

    def _grant_route_map_graph(self, *, community_add=True, set_add=True):
        for model in (RouteMap, RouteMapEntry, RouteMapEntrySetCommunity, Community):
            actions = ["view", "change", "delete"]
            if not (model is Community and not community_add or model is RouteMapEntrySetCommunity and not set_add):
                actions.append("add")
            grant(self.user, actions, model)

    def test_materialize_route_map_requires_conditional_community_add(self):
        self._route_map_group()
        self._login(all_managements=True)
        self._grant_route_map_graph(community_add=False)
        self._assert_denied("materialize", 403)

    def test_materialize_route_map_requires_set_community_child_add(self):
        self._route_map_group()
        self._login(all_managements=True)
        self._grant_route_map_graph(set_add=False)
        self._assert_denied("materialize", 403)

    def test_materialize_route_map_replays_matches_and_literal_community_edges(self):
        self._route_map_group()
        self._login(all_managements=True)
        self._grant_route_map_graph()
        response = self._post("materialize")
        self.assertEqual(response.status_code, 302)
        entry = RouteMapEntry.objects.get(route_map__name="SHARED-MAP")
        self.assertEqual(list(entry.match_prefix_list.values_list("name", flat=True)), ["Z-SHARED"])
        self.assertEqual(list(entry.set_communities.get().communities.values_list("community", flat=True)), ["64512:2"])
        self.selected.refresh_from_db()
        self.assertTrue(self.selected.is_materialized)

    def _hide_shared_prefix_list(self):
        grant(self.user, ("add", "change", "delete"), PrefixList)
        grant(self.user, ("view",), PrefixList, {"name": "A-PRIVATE"})
        for model in (PrefixListEntry, CustomPrefix):
            grant(self.user, ("view", "add", "change", "delete"), model)

    def test_materialize_refuses_a_hidden_native_root(self):
        self._login(all_managements=True, native=False)
        self._hide_shared_prefix_list()
        self._assert_denied("materialize", 403)

    def test_classify_master_refuses_a_hidden_native_root(self):
        self._login(all_managements=True, native=False)
        self._hide_shared_prefix_list()
        self._assert_denied("classify", 403, {"mode": "master"})

    def test_materialize_preserves_a_hidden_named_match_as_unmapped(self):
        self._route_map_group()
        self._login(all_managements=True, native=False)
        self._hide_shared_prefix_list()
        self._grant_route_map_graph()
        response = self._post("materialize")
        self.assertEqual(response.status_code, 302)
        entry = RouteMapEntry.objects.get(route_map__name="SHARED-MAP")
        self.assertFalse(entry.match_prefix_list.exists())
        self.assertEqual(entry.vendor_ext["unmapped"]["match_prefix_list"], ["Z-SHARED"])

    def test_materialize_refuses_an_existing_hidden_prefix_support_row(self):
        CustomPrefix.objects.create(prefix="198.18.2.0/24")
        self._login(all_managements=True, native=False)
        for model in (PrefixList, PrefixListEntry):
            grant(self.user, ("view", "add", "change", "delete"), model)
        grant(self.user, ("add", "change", "delete"), CustomPrefix)
        grant(self.user, ("view",), CustomPrefix, {"prefix": "198.18.0.0/24"})
        self._assert_denied("materialize", 403)

    def test_materialize_refuses_an_existing_hidden_literal_community(self):
        self._route_map_group()
        Community.objects.create(community="64512:2")
        self._login(all_managements=True)
        for model in (RouteMap, RouteMapEntry, RouteMapEntrySetCommunity):
            grant(self.user, ("view", "add", "change", "delete"), model)
        grant(self.user, ("add", "change", "delete"), Community)
        grant(self.user, ("view",), Community, {"community": "64512:1"})
        self._assert_denied("materialize", 403)

    def _assert_master_classification(self):
        response = self._post("classify", {"mode": "master"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NSORoutePolicyObjectClass.objects.get(object_name="Z-SHARED").mode, "master")
        self.assertEqual(NSORoutePolicyState.objects.filter(object_name="Z-SHARED", is_materialized=True).count(), 1)

    def _assert_bulk_classification(self):
        private = self._selectable_private_row()
        response = self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:routing_classify_bulk_route_policy",
                kwargs={"device_pk": self.second.pk},
            ),
            {"state": [private.pk, self.selected.pk]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(NSORoutePolicyObjectClass.objects.values_list("object_name", "mode")),
            {("A-PRIVATE", "local"), ("Z-SHARED", "local")},
        )
        self.assertFalse(NSORoutePolicyState.objects.filter(is_materialized=True).exists())

    def _selectable_private_row(self):
        private = NSORoutePolicyState.objects.get(management=self.second_management, object_name="A-PRIVATE")
        private.status = "conflict"
        private.is_materialized = False
        private.captured = {
            **private.captured,
            "entries": [{"sequence": 10, "action": "permit", "prefix": "198.18.3.0/24"}],
        }
        private.content_hash = hash_captured(private.family, private.captured)
        save_overlay_fixture(private, update_fields=("status", "is_materialized", "captured", "content_hash"))
        native_entry = PrefixListEntry.objects.get(prefix_list__name="A-PRIVATE")
        self.assertNotEqual(str(native_entry.assigned_prefix.prefix), private.captured["entries"][0]["prefix"])
        response = self.client.get(
            reverse(
                "plugins:netbox_nso_plugin:routing_classify_bulk_route_policy",
                kwargs={"device_pk": self.second.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row.pk for row in response.context["rows"]],
            [private.pk, self.selected.pk],
        )
        return private

    def _login_superuser(self):
        self.user.is_superuser = True
        self.user.save(update_fields=("is_superuser",))
        self.client.force_login(self.user)

    def test_classify_master_succeeds_with_unconstrained_permissions(self):
        self._login(all_managements=True)
        self._assert_master_classification()

    def test_bulk_classify_succeeds_with_unconstrained_permissions(self):
        self._login(all_managements=True)
        self._assert_bulk_classification()

    def test_classify_local_succeeds_for_a_superuser(self):
        self._login_superuser()
        response = self._post("classify", {"mode": "local"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NSORoutePolicyObjectClass.objects.get(object_name="Z-SHARED").mode, "local")

    def test_classify_master_succeeds_for_a_superuser(self):
        self._login_superuser()
        self._assert_master_classification()

    def test_bulk_classify_succeeds_for_a_superuser(self):
        self._login_superuser()
        self._assert_bulk_classification()

    def test_materialize_succeeds_for_a_superuser(self):
        self._login_superuser()
        response = self._post("materialize")
        self.assertEqual(response.status_code, 302)
        self.selected.refresh_from_db()
        self.assertTrue(self.selected.is_materialized)
