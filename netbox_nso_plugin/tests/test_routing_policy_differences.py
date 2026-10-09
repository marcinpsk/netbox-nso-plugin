# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Compare routing graphs through real native models and immutable observations."""

import copy
from uuid import uuid4

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from ipam.models import ASN, RIR, IPAddress
from netbox_routing.models import BGPPeer, BGPRouter, BGPScope, RouteMap, RouteMapEntry

from netbox_nso_plugin.device_differences import NOT_VISIBLE, differences
from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState, NSORoutePolicyState
from netbox_nso_plugin.observations import observation_defaults

from ._routing_observation_case import DOCUMENTS, routing_observation
from ._scope_observation_case import entry
from .test_gated_reconcile import _make


class _RoutingDifferences:
    def setUp(self):
        self.device, self.management = _make(
            f"routing{uuid4().hex[:8]}", manage_routing=True, **{f"manage_{self.scope}": True}
        )
        self.user = get_user_model().objects.create_superuser(username=f"routing{uuid4().hex[:8]}")
        self.document = copy.deepcopy(DOCUMENTS[self.scope])
        self.make_native()

    def publish(self, document=None, coverage=None):
        observed = routing_observation(
            self.scope, document=self.document if document is None else document, coverage=coverage
        )
        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family=self.scope)
        NSOFamilyObservation.objects.update_or_create(
            read_state=state, defaults=observation_defaults(self.scope, 1, 1, observed)
        )

    def rows(self):
        return [row for row in differences(self.management, user=self.user) if row.scope == self.scope]

    def test_match(self):
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_mismatch(self):
        self.changed_entry()[self.changed_attribute] = self.changed_value
        self.publish()
        self.assertIn(("mismatch", self.changed_attribute), [(row.kind, row.attribute) for row in self.rows()])

    def test_netbox_only(self):
        document = copy.deepcopy(self.document)
        for name in document["present"]:
            document[name] = []
        self.publish(document)
        self.assertIn("netbox_only", [row.kind for row in self.rows()])

    def test_device_only(self):
        self.remove_native()
        self.publish()
        self.assertIn("device_only", [row.kind for row in self.rows()])

    def test_unprojectable_is_ambiguous(self):
        self.document["unprojectable"] = [{"index": 0, "reason": "invalid identity"}]
        self.publish()
        self.assertIn(("ambiguous", "invalid identity"), [(row.kind, row.reason) for row in self.rows()])

    def test_no_read_is_unavailable(self):
        self.assertEqual([(row.kind, row.reason) for row in self.rows()], [("unavailable", "no successful read yet")])

    def test_not_comparable_is_unavailable(self):
        self.publish(coverage={"attributes": [], "not_comparable": [self.changed_attribute]})
        self.assertIn(("unavailable", self.changed_attribute), [(row.kind, row.attribute) for row in self.rows()])
        self.assertFalse([row for row in self.rows() if row.kind == "mismatch"])

    def test_hidden_objects_are_redacted(self):
        self.user = get_user_model().objects.create_user(username=f"hidden{uuid4().hex[:8]}")
        self.publish()
        rows = self.rows()
        self.assertTrue(any(row.reason == NOT_VISIBLE for row in rows))
        self.assertNotIn(self.hidden_name, repr(rows))


class TestBgpDifferences(_RoutingDifferences, TestCase):
    scope = "bgp"
    changed_attribute, changed_value = "enabled", False
    hidden_name = "198.18.0.2"

    def make_native(self):
        rir = RIR.objects.create(name=f"example-{self.device.pk}", slug=f"example-{self.device.pk}")
        asn = ASN.objects.create(rir=rir, asn=64512)
        self.remote_as = ASN.objects.create(rir=rir, asn=64513)
        self.router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            name="Example",
            asn=asn,
            router_id="198.18.0.1",
        )
        self.native_scope = BGPScope.objects.create(router=self.router)
        self.native = BGPPeer.objects.create(
            scope=self.native_scope,
            peer=IPAddress.objects.create(address="198.18.0.2/32"),
            name=None,
            remote_as=self.remote_as,
            enabled=True,
        )
        self.document["routers"][0]["scope"][0] = entry(
            vrf="",
            address_family=[],
            peer_group=[],
            peer=[
                entry(
                    peer_address="198.18.0.2",
                    peer_group=None,
                    remote_as="64513",
                    local_as=None,
                    enabled=True,
                    ttl=None,
                    source=None,
                    description="",
                    bfd_enabled=None,
                    password_present=False,
                    peer_address_family=[],
                )
            ],
        )

    def changed_entry(self):
        return self.document["routers"][0]["scope"][0]["peer"][0]

    def remove_native(self):
        self.native.delete()

    def test_observed_peer_af_inherits_hidden_native_scope_af(self):
        from core.models import ObjectType
        from netbox_routing.models import BGPAddressFamily
        from users.models import ObjectPermission

        from netbox_nso_plugin.difference_projection import native_entries

        scope_af = BGPAddressFamily.objects.create(scope=self.native_scope, address_family="ipv4-unicast")
        self.user = get_user_model().objects.create_user(username=f"afparent{uuid4().hex[:8]}")
        models = {
            type(obj) for item in native_entries("bgp", self.management) for obj in item.objects if obj is not None
        }
        for model in models - {BGPAddressFamily}:
            permission = ObjectPermission.objects.create(name=f"Visible {model._meta.model_name}", actions=["view"])
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        self.changed_entry()["peer_address_family"] = [entry(afi="ipv4-unicast", routemap_in="EXAMPLE-IMPORT")]
        self.publish()
        rows = self.rows()
        self.assertTrue(any(row.kind == "ambiguous" for row in rows))
        self.assertNotIn("EXAMPLE-IMPORT", repr(rows))
        self.assertFalse(
            any(
                row.identity
                == ("router", 64512, "scope", "", "peer", "198.18.0.2", "peer_address_family", "ipv4-unicast")
                for row in rows
            )
        )
        permission = ObjectPermission.objects.create(
            name="Visible scope AF", actions=["view"], constraints={"pk": scope_af.pk}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(BGPAddressFamily))
        permission.users.add(self.user)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.assertIn("EXAMPLE-IMPORT", repr(self.rows()))

    def test_asplain_and_asdot_are_equivalent(self):
        self.document["routers"][0]["asn"] = "0.64512"
        self.changed_entry()["remote_as"] = "0.64513"
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_omitted_peer_and_group_af_fields_match_after_reconciliation(self):
        from netbox_routing.models import BGPPeerAddressFamily

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peer = self.changed_entry()
        peer["peer_group"] = "EXAMPLE"
        peer["peer_address_family"] = [entry(afi="ipv4-unicast")]
        scope = self.document["routers"][0]["scope"][0]
        scope["address_family"] = [entry(afi="ipv4-unicast")]
        scope["peer_group"] = [
            entry(name="EXAMPLE", remote_as="64513", peer_group_address_family=[entry(afi="ipv4-unicast")])
        ]
        self.native.delete()
        _reconcile_bgp_config(
            self.device,
            {
                "routers": [
                    {
                        "asn": "64512",
                        "router_id": "198.18.0.1",
                        "scopes": [
                            {
                                "vrf": "",
                                "address_families": ["ipv4-unicast"],
                                "peers": [{**peer, "address_families": [{"af": "ipv4-unicast", "enabled": True}]}],
                                "peer_groups": [
                                    {
                                        "name": "EXAMPLE",
                                        "remote_as": "64513",
                                        "address_families": [{"af": "ipv4-unicast"}],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        )
        self.native = BGPPeer.objects.get(scope=self.native_scope, peer__address="198.18.0.2/32")
        owners = (
            (BGPPeer, self.native.pk),
            (type(self.native.peer_group), self.native.peer_group_id),
        )
        for model, pk in owners:
            with self.subTest(owner=model._meta.model_name):
                af = BGPPeerAddressFamily.objects.get(
                    assigned_object_type=ContentType.objects.get_for_model(model), assigned_object_id=pk
                )
                self.assertTrue(af.enabled)
                for field in ("routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out"):
                    self.assertIsNone(getattr(af, field))
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_peer_without_remote_as_stays_in_comparison(self):
        self.native.remote_as = None
        self.native.save(update_fields=["remote_as"])
        self.publish()
        self.assertTrue(any(row.kind == "ambiguous" and "remote AS" in row.reason for row in self.rows()))

    def test_password_presence_is_never_a_comparable_value(self):
        self.publish()
        self.assertIn(("unavailable", "password"), [(row.kind, row.attribute) for row in self.rows()])
        self.assertNotIn("password_present", [row.attribute for row in self.rows() if row.kind == "mismatch"])

    def test_missing_peer_collection_never_reports_netbox_only(self):
        scope = self.document["routers"][0]["scope"][0]
        del scope["peer"]
        scope["present"].remove("peer")
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind == "netbox_only"])
        self.assertIn(("unavailable", "peer"), [(row.kind, row.attribute) for row in self.rows()])

    def test_hidden_scope_gap_does_not_name_a_vrf(self):
        from ipam.models import VRF

        vrf = VRF.objects.create(name="hidden-routing-vrf")
        self.native_scope.vrf = vrf
        self.native_scope.save(update_fields=["vrf"])
        scope = self.document["routers"][0]["scope"][0]
        scope["vrf"] = vrf.name
        scope["peer"] = None
        self.user = get_user_model().objects.create_user(username=f"gap{uuid4().hex[:8]}")
        self.publish()
        self.assertNotIn(vrf.name, repr(self.rows()))

    def test_unused_router_template_stays_in_comparison(self):
        from netbox_routing.models import BGPPeerTemplate

        template = BGPPeerTemplate.objects.create(name="EXAMPLE", remote_as=self.remote_as)
        self.router.peer_templates.add(template)
        self.publish()
        self.assertIn(
            ("ambiguous", ("router", 64512, "peer_group", "EXAMPLE")),
            [(row.kind, row.identity) for row in self.rows()],
        )

    def test_unrelated_hidden_vrf_address_does_not_hide_the_bound_peer(self):
        from core.models import ObjectType
        from ipam.models import VRF
        from users.models import ObjectPermission

        unrelated = VRF.objects.create(name="unrelated-hidden-vrf")
        IPAddress.objects.create(address="198.18.0.2/32", vrf=unrelated)
        self.user = get_user_model().objects.create_user(username=f"vrf{uuid4().hex[:8]}")
        for model in (ASN, BGPRouter, BGPScope, BGPPeer):
            permission = ObjectPermission.objects.create(name=f"Visible {model._meta.model_name}", actions=["view"])
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        permission = ObjectPermission.objects.create(
            name="Visible global addresses",
            actions=["view"],
            constraints={"vrf__isnull": True},
        )
        permission.object_types.add(ObjectType.objects.get_for_model(IPAddress))
        permission.users.add(self.user)
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])
        self.assertNotIn(unrelated.name, repr(self.rows()))

    def test_query_count_is_flat_for_many_peers(self):
        self.publish()
        self.rows()
        with CaptureQueriesContext(connection) as before:
            self.rows()
        peers = self.document["routers"][0]["scope"][0]["peer"]
        for number in range(3, 43):
            BGPPeer.objects.create(
                scope=self.native_scope,
                peer=IPAddress.objects.create(address=f"198.18.0.{number}/32"),
                name=None,
                remote_as=self.remote_as,
                enabled=True,
            )
            peers.append(entry(**{key: value for key, value in peers[0].items() if key != "present"}))
            peers[-1]["peer_address"] = f"198.18.0.{number}"
        self.publish()
        with CaptureQueriesContext(connection) as after:
            self.rows()
        self.assertEqual(len(before), len(after))


class TestRoutePolicyDifferences(_RoutingDifferences, TestCase):
    scope = "route_policy"
    changed_attribute, changed_value = "action", "deny"
    hidden_name = "IMPORT"

    def make_native(self):
        self.native = RouteMap.objects.create(name="IMPORT")
        self.native_entry = RouteMapEntry.objects.create(route_map=self.native, sequence=1, action="permit")
        self.overlay = NSORoutePolicyState.objects.create(
            management=self.management,
            family="route_map",
            object_name=self.native.name,
            content_type=ContentType.objects.get_for_model(self.native),
            object_id=self.native.pk,
        )
        for collection in ("prefix_lists", "community_lists", "as_paths"):
            self.document[collection] = []
        self.document["route_maps"][0]["entry"][0] = entry(
            sequence=10,
            action="permit",
            match_prefix_lists=[],
            match_community_lists=[],
            match_as_paths=[],
            match_json="",
            set_json="",
        )

    def changed_entry(self):
        return self.document["route_maps"][0]["entry"][0]

    def remove_native(self):
        self.overlay.delete()
        self.native.delete()

    def test_names_are_case_sensitive(self):
        self.document["route_maps"][0]["name"] = "Import"
        self.publish()
        self.assertEqual(
            {
                row.kind
                for row in self.rows()
                if row.identity[:2] in (("route_maps", "Import"), ("route_maps", "IMPORT"))
            },
            {"device_only", "netbox_only"},
        )

    def test_omitted_route_map_fields_match_native_empty_content(self):
        self.document["route_maps"][0]["entry"] = [entry(sequence=10, action="permit")]
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_duplicate_entries_are_ambiguous(self):
        self.document["route_maps"][0]["entry"].append(copy.deepcopy(self.changed_entry()))
        self.publish()
        self.assertIn("ambiguous", [row.kind for row in self.rows()])

    def test_missing_entry_collection_never_reports_netbox_only(self):
        parent = self.document["route_maps"][0]
        del parent["entry"]
        parent["present"].remove("entry")
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind == "netbox_only"])
        self.assertIn(("unavailable", "entry"), [(row.kind, row.attribute) for row in self.rows()])

    def test_hidden_parent_gap_does_not_name_the_policy(self):
        self.document["route_maps"][0]["entry"] = None
        self.user = get_user_model().objects.create_user(username=f"gap{uuid4().hex[:8]}")
        self.publish()
        self.assertNotIn(self.native.name, repr(self.rows()))

    def test_hidden_reference_is_redacted(self):
        from core.models import ObjectType
        from netbox_routing.models import PrefixList
        from users.models import ObjectPermission

        prefix_list = PrefixList.objects.create(name="hidden-prefix-list", family=4)
        self.native_entry.match_prefix_list.add(prefix_list)
        self.changed_entry()["match_prefix_lists"] = [prefix_list.name]
        self.user = get_user_model().objects.create_user(username=f"reference{uuid4().hex[:8]}")
        for model in (RouteMap, RouteMapEntry, NSORoutePolicyState):
            permission = ObjectPermission.objects.create(name=f"Visible {model._meta.model_name}", actions=["view"])
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        self.publish()
        self.assertNotIn(prefix_list.name, repr(self.rows()))
        self.assertTrue(any(row.reason == NOT_VISIBLE for row in self.rows()))

    def test_vendor_family_tokens_use_shared_route_policy_normalization(self):
        self.native_entry.match = {"family": "inet"}
        self.native_entry.save(update_fields=["match"])
        self.changed_entry()["match_json"] = '{"family":"ipv4"}'
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_hidden_community_delete_reference_is_redacted(self):
        from core.models import ObjectType
        from netbox_routing.models import CommunityList
        from users.models import ObjectPermission

        hidden = CommunityList.objects.create(name="hidden-delete-community-list")
        self.changed_entry()["set_json"] = '{"community_delete":"hidden-delete-community-list"}'
        self.user = get_user_model().objects.create_user(username=f"delete{uuid4().hex[:8]}")
        for model in (RouteMap, RouteMapEntry, NSORoutePolicyState):
            permission = ObjectPermission.objects.create(name=f"Visible {model._meta.model_name}", actions=["view"])
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        self.publish()
        rows = self.rows()
        self.assertNotIn(hidden.name, repr(rows))
        self.assertTrue(any(row.reason == NOT_VISIBLE for row in rows))

    def test_structured_address_family_edit_is_compared(self):
        self.native_entry.match_afi = ["ipv6"]
        self.native_entry.save(update_fields=["match_afi"])
        self.publish()
        self.assertIn(("mismatch", "match_json"), [(row.kind, row.attribute) for row in self.rows()])

    def test_structured_edit_invalidates_the_first_raw_policy_body(self):
        self.native_entry.match = {"_rpl_raw": "pass"}
        self.native_entry.save(update_fields=["match"])
        RouteMapEntry.objects.create(route_map=self.native, sequence=2, action="permit", match_afi=["ipv6"])
        self.changed_entry()["match_json"] = '{"_rpl_raw":"pass"}'
        self.document["route_maps"][0]["entry"].append(
            entry(
                sequence=20,
                action="permit",
                match_prefix_lists=[],
                match_community_lists=[],
                match_as_paths=[],
                match_json='{"family":["ipv6"]}',
                set_json="",
            )
        )
        self.publish()
        self.assertIn(
            ("mismatch", ("route_maps", "IMPORT", "entry", 1), "match_json"),
            [(row.kind, row.identity, row.attribute) for row in self.rows()],
        )

    def test_structured_set_community_rows_drive_native_comparison(self):
        from netbox_routing.models import CommunityList, RouteMapEntrySetCommunity

        community_list = CommunityList.objects.create(name="EXAMPLE")
        structured = RouteMapEntrySetCommunity.objects.create(
            route_map_entry=self.native_entry,
            operation="add",
            community_list=community_list,
        )
        self.changed_entry()["set_json"] = '{"community_add":["EXAMPLE"]}'
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])
        structured.operation = "set"
        structured.save(update_fields=["operation"])
        self.assertIn(("mismatch", "set_json"), [(row.kind, row.attribute) for row in self.rows()])

    def test_hidden_set_community_row_is_redacted(self):
        from core.models import ObjectType
        from netbox_routing.models import CommunityList, RouteMapEntrySetCommunity
        from users.models import ObjectPermission

        community_list = CommunityList.objects.create(name="hidden-set-community-list")
        RouteMapEntrySetCommunity.objects.create(
            route_map_entry=self.native_entry,
            operation="add",
            community_list=community_list,
        )
        self.changed_entry()["set_json"] = '{"community_add":["hidden-set-community-list"]}'
        self.user = get_user_model().objects.create_user(username=f"setrow{uuid4().hex[:8]}")
        for model in (RouteMap, RouteMapEntry, NSORoutePolicyState, CommunityList):
            permission = ObjectPermission.objects.create(name=f"Visible {model._meta.model_name}", actions=["view"])
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        self.publish()
        self.assertNotIn(community_list.name, repr(self.rows()))
        self.assertTrue(any(row.reason == NOT_VISIBLE for row in self.rows()))

    def test_named_prefix_and_inline_route_filter_match(self):
        from netbox_routing.models import CustomPrefix, PrefixList, PrefixListEntry

        prefix_list = PrefixList.objects.create(name="EXAMPLE", family=4)
        prefix = CustomPrefix.objects.create(prefix="198.18.0.0/24")
        PrefixListEntry.objects.create(
            prefix_list=prefix_list,
            assigned_prefix_type=ContentType.objects.get_for_model(prefix),
            assigned_prefix_id=prefix.pk,
            sequence=1,
            action="permit",
        )
        self.native_entry.match_prefix_list.add(prefix_list)
        self.changed_entry()["match_json"] = '{"_junos_route_filter":[{"prefix":"198.18.0.0/24","match":"exact"}]}'
        self.publish()
        policy_rows = [row for row in self.rows() if row.identity[:2] == ("route_maps", "IMPORT")]
        self.assertFalse([row for row in policy_rows if row.kind != "unavailable"])
        self.changed_entry()["match_json"] = '{"_junos_route_filter":[{"prefix":"198.18.1.0/24","match":"exact"}]}'
        self.publish()
        self.assertIn(("mismatch", "match_prefix_lists"), [(row.kind, row.attribute) for row in self.rows()])

    def test_omitted_prefix_limits_match_native_exact_prefix(self):
        from netbox_routing.models import CustomPrefix, PrefixList, PrefixListEntry

        prefix_list = PrefixList.objects.create(name="EXAMPLE", family=4)
        prefix = CustomPrefix.objects.create(prefix="198.18.0.0/24")
        PrefixListEntry.objects.create(
            prefix_list=prefix_list,
            assigned_prefix_type=ContentType.objects.get_for_model(prefix),
            assigned_prefix_id=prefix.pk,
            sequence=1,
            action="permit",
        )
        NSORoutePolicyState.objects.create(
            management=self.management,
            family="prefix_list",
            object_name=prefix_list.name,
            content_type=ContentType.objects.get_for_model(prefix_list),
            object_id=prefix_list.pk,
        )
        self.document["prefix_lists"] = [
            entry(name="EXAMPLE", family=4, entry=[entry(sequence=10, action="permit", prefix="198.18.0.0/24")])
        ]
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])

    def test_named_prefix_without_observed_content_is_unavailable(self):
        self.changed_entry()["match_prefix_lists"] = ["EXAMPLE"]
        self.publish()
        self.assertIn(("unavailable", "match_prefix_lists"), [(row.kind, row.attribute) for row in self.rows()])
        self.assertIn(("unavailable", "match_json"), [(row.kind, row.attribute) for row in self.rows()])

    def test_malformed_observed_json_is_ambiguous(self):
        self.changed_entry()["match_json"] = "{invalid"
        self.publish()
        self.assertIn(("ambiguous", "invalid device policy content"), [(row.kind, row.reason) for row in self.rows()])

    def test_invalid_native_json_is_ambiguous(self):
        RouteMapEntry.objects.filter(pk=self.native_entry.pk).update(set=[])
        self.publish()
        self.assertIn(("ambiguous", "invalid native policy content"), [(row.kind, row.reason) for row in self.rows()])

    def test_scalar_and_list_policy_knobs_compare_equally(self):
        self.native_entry.match = {"protocol": "bgp"}
        self.native_entry.save(update_fields=["match"])
        self.changed_entry()["match_json"] = '{"protocol":["bgp"]}'
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])


class _SimplePolicyDifferences(_RoutingDifferences):
    scope = "route_policy"
    changed_attribute, changed_value = "action", "deny"
    hidden_name = "EXAMPLE"

    def make_native(self):
        self.native = self.make_policy()
        self.overlay = NSORoutePolicyState.objects.create(
            management=self.management,
            family=self.policy_family,
            object_name=self.native.name,
            content_type=ContentType.objects.get_for_model(self.native),
            object_id=self.native.pk,
        )
        for collection in ("prefix_lists", "community_lists", "as_paths", "route_maps"):
            if collection != self.collection:
                self.document[collection] = []

    def changed_entry(self):
        return self.document[self.collection][0]["entry"][0]

    def remove_native(self):
        self.overlay.delete()
        self.native.delete()


class TestPrefixListDifferences(_SimplePolicyDifferences, TestCase):
    policy_family, collection = "prefix_list", "prefix_lists"

    def make_policy(self):
        from netbox_routing.models import CustomPrefix, PrefixList, PrefixListEntry

        native = PrefixList.objects.create(name="EXAMPLE", family=4)
        prefix = CustomPrefix.objects.create(prefix="198.18.0.0/24")
        PrefixListEntry.objects.create(
            prefix_list=native,
            assigned_prefix_type=ContentType.objects.get_for_model(prefix),
            assigned_prefix_id=prefix.pk,
            sequence=1,
            action="permit",
            ge=None,
            le=32,
        )
        return native

    def test_null_prefix_limits_use_reconciler_equivalence(self):
        self.changed_entry()["ge"] = 24
        self.publish()
        self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])


class TestCommunityListDifferences(_SimplePolicyDifferences, TestCase):
    policy_family, collection = "community_list", "community_lists"
    hidden_name = "SCRUBBER"

    def make_policy(self):
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        native = CommunityList.objects.create(name="SCRUBBER", invert_match=True)
        self.member = Community.objects.create(community="no-export")
        CommunityListEntry.objects.create(community_list=native, community=self.member, action="permit")
        return native

    def test_canonical_large_and_color_members_match_observations(self):
        for member in ("large:64512:1:2", "color:0:128"):
            with self.subTest(member=member):
                self.member.community = member
                self.member.save(update_fields=["community"])
                self.changed_entry()["community"] = member
                self.publish()
                self.assertFalse([row for row in self.rows() if row.kind != "unavailable"])


class TestAsPathDifferences(_SimplePolicyDifferences, TestCase):
    policy_family, collection = "as_path", "as_paths"

    def make_policy(self):
        from netbox_routing.models import ASPath, ASPathEntry

        native = ASPath.objects.create(name="EXAMPLE")
        ASPathEntry.objects.create(aspath=native, sequence=1, action="permit", pattern="^64512$")
        return native
