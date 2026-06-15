# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Greenfield route-policy attach + delete-propagation + entry serialization.

The route-policy push serializes a netbox-routing PrefixList's entries; this guards the
field mapping (prefix_list_entries / sequence / assigned_prefix.prefix) and the
greenfield attach + delete signals.
"""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from .mixins import IntentPushResetMixin


class _RPBase(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RpMfg", slug="rpmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RpDev", slug="rpdev")
        role = DeviceRole.objects.create(name="RpRole", slug="rprole")
        site = Site.objects.create(name="RpSite", slug="rpsite")
        cls.device = Device.objects.create(name="rp-router", device_type=dt, role=role, site=site)

    def _mgmt(self, adapter_device_id=196):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="rp-inst", defaults={"adapter_instance_id": "rp-inst"})
        return NSODeviceManagement.objects.create(
            device=self.device, nso_instance=inst, nso_device_name="nso-rp", adapter_device_id=adapter_device_id
        )

    def _prefix_list(self, name="TESTNSO-PL"):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import CustomPrefix, PrefixList, PrefixListEntry

        pl = PrefixList.objects.create(name=name)
        cp, _ = CustomPrefix.objects.get_or_create(prefix="10.99.0.0/16")
        PrefixListEntry.objects.create(
            prefix_list=pl,
            assigned_prefix_type=ContentType.objects.get_for_model(CustomPrefix),
            assigned_prefix_id=cp.pk,
            sequence=10,
            action="permit",
        )
        return pl


class TestRoutePolicyEntrySerialization(_RPBase):
    def test_prefix_list_entries_serialize_from_fork_model(self):
        from netbox_nso_plugin.signals import _build_route_policy_entries

        pl = self._prefix_list()
        entries = _build_route_policy_entries("prefix_list", pl)
        assert entries == [{"sequence": 10, "action": "permit", "prefix": "10.99.0.0/16"}]

    def test_community_list_entries_serialize_from_fork_model(self):
        """community_list reads CommunityList.communitylistentries (not .communities)."""
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        from netbox_nso_plugin.signals import _build_route_policy_entries

        cl = CommunityList.objects.create(name="TESTNSO-CL")
        comm = Community.objects.create(community="65000:1")
        CommunityListEntry.objects.create(community_list=cl, action="permit", community=comm)
        entries = _build_route_policy_entries("community_list", cl)
        assert entries == [{"sequence": 1, "action": "permit", "community": "65000:1"}]

    def test_community_list_extended_members_serialize_verbatim(self):
        """Regression (the EU_CDN_AS_EXT empty-intent bug): a list whose members are
        extended (target:/origin:/…) now stores them VERBATIM as Community rows in the one
        CommunityList, so the push emits them directly — no parallel list, no reconstruction,
        no silently-empty intent."""
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        from netbox_nso_plugin.signals import _build_route_policy_entries

        name = "100365038-EU_CDN_AS_EXT"
        cl = CommunityList.objects.create(name=name)
        comm = Community.objects.create(community="target:1111:100365038")
        CommunityListEntry.objects.create(community_list=cl, action="permit", community=comm)

        entries = _build_route_policy_entries("community_list", cl)
        assert entries == [{"sequence": 1, "action": "permit", "community": "target:1111:100365038"}]

    def test_community_list_mixed_member_kinds_serialize_verbatim(self):
        """Standard, extended, and large members all live in the one CommunityList and are
        emitted verbatim in a single entry list with contiguous sequence numbers (order is
        the model's, an implementation detail — assert the member set, not a fixed order)."""
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        from netbox_nso_plugin.signals import _build_route_policy_entries

        cl = CommunityList.objects.create(name="TESTNSO-CL-MIX")
        values = {"65000:7", "origin:64500:9", "large:65000:1:2"}
        for value in values:
            CommunityListEntry.objects.create(
                community_list=cl, action="permit", community=Community.objects.create(community=value)
            )

        entries = _build_route_policy_entries("community_list", cl)
        assert {e["community"] for e in entries} == values
        assert all(e["action"] == "permit" for e in entries)
        assert sorted(e["sequence"] for e in entries) == [1, 2, 3]

    def test_as_path_entries_serialize_from_fork_model(self):
        """as_path reads ASPath.aspath_entries (sequence/action/pattern)."""
        from netbox_routing.models import ASPath, ASPathEntry

        from netbox_nso_plugin.signals import _build_route_policy_entries

        ap = ASPath.objects.create(name="TESTNSO-AP")
        ASPathEntry.objects.create(aspath=ap, sequence=5, action="permit", pattern="^65000_")
        entries = _build_route_policy_entries("as_path", ap)
        assert entries == [{"sequence": 5, "action": "permit", "pattern": "^65000_"}]

    def test_route_map_entries_serialize_from_fork_model(self):
        """route_map reads RouteMap.route_map_entries (not .entries)."""
        from netbox_routing.models import RouteMap, RouteMapEntry

        from netbox_nso_plugin.signals import _build_route_policy_entries

        rm = RouteMap.objects.create(name="TESTNSO-RM")
        RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit", match={"x": 1}, set={"y": 2})
        entries = _build_route_policy_entries("route_map", rm)
        assert entries == [
            {
                "sequence": 10,
                "action": "permit",
                "match-prefix-lists": [],
                "match-community-lists": [],
                "match-as-paths": [],
                "match-json": '{"x": 1}',
                "set-json": '{"y": 2}',
            }
        ]

    def test_route_map_entry_body_serializes_match_refs_and_json(self):
        """The intent body must carry the M2M match refs + match/set JSON — a route-map
        with a prefix-list match, from/to-protocol and next-hop self (PCE-BGP-EXPORT
        shape) must not push a hollow body."""
        import json

        from netbox_routing.models import RouteMap, RouteMapEntry

        from netbox_nso_plugin.signals import _build_route_policy_entries

        rm = RouteMap.objects.create(name="TESTNSO-PCE-EXPORT")
        pl = self._prefix_list(name="TESTNSO-PCE-EXPORT-PL")
        e1 = RouteMapEntry.objects.create(
            route_map=rm,
            sequence=10,
            action="permit",
            match={"protocol": ["direct", "static", "bgp"], "to_protocol": ["bgp"]},
            set={"next_hop_self": True},
        )
        e1.match_prefix_list.add(pl)
        RouteMapEntry.objects.create(route_map=rm, sequence=20, action="deny")

        entries = _build_route_policy_entries("route_map", rm)
        assert entries[0]["match-prefix-lists"] == ["TESTNSO-PCE-EXPORT-PL"]
        assert json.loads(entries[0]["match-json"]) == {
            "protocol": ["direct", "static", "bgp"],
            "to_protocol": ["bgp"],
        }
        assert json.loads(entries[0]["set-json"]) == {"next_hop_self": True}
        assert entries[1] == {
            "sequence": 20,
            "action": "deny",
            "match-prefix-lists": [],
            "match-community-lists": [],
            "match-as-paths": [],
            "match-json": "{}",
            "set-json": "{}",
        }

    def test_route_map_entry_flow_control_reinjected_into_set_json(self):
        """flow_control is lifted out of set-json on read — the write path puts it back."""
        import json

        from netbox_routing.models import RouteMap, RouteMapEntry

        from netbox_nso_plugin.signals import _build_route_policy_entries

        rm = RouteMap.objects.create(name="TESTNSO-RM-FC")
        RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit", flow_control=20)
        entries = _build_route_policy_entries("route_map", rm)
        assert json.loads(entries[0]["set-json"]) == {"flow_control": 20}


class TestRoutePolicyDeletePropagation(_RPBase):
    def test_delete_prefix_list_pushes_removal_to_attached_device(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push

        mgmt = self._mgmt()
        pl = self._prefix_list()
        with suppress_intent_push():
            NSORoutePolicyState.objects.create(
                management=mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status="in_sync",
            )

        pushed = []
        with patch(
            "netbox_nso_plugin.adapter_client.put_route_policy_intent",
            side_effect=lambda adapter_id, objects: pushed.append((adapter_id, objects)),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                PrefixList.objects.get(pk=pl.pk).delete()

        assert NSORoutePolicyState.objects.filter(object_name="TESTNSO-PL").count() == 0
        assert pushed and pushed[-1][0] == 196
        assert pushed[-1][1] == []  # reduced snapshot → removal propagates


class TestRoutePolicyEditOwnsAndPushes(_RPBase):
    """Editing an OWNED route-policy object (or its members) re-owns the overlay and pushes —
    so an operator edit to an in_sync community-list actually reaches the device on Apply,
    instead of Accept being a silent no-op (the gap found in the live write e2e)."""

    def _community_list_with_overlay(self, status="in_sync"):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push

        mgmt = self._mgmt()
        cl = CommunityList.objects.create(name="TESTNSO-CL-EDIT")
        with suppress_intent_push():
            state = NSORoutePolicyState.objects.create(
                management=mgmt,
                family="community_list",
                object_name=cl.name,
                content_type=ContentType.objects.get_for_model(CommunityList),
                object_id=cl.pk,
                status=status,
            )
        return cl, state

    def test_adding_member_to_owned_list_owns_and_pushes(self):
        """Adding a member to an in_sync community-list flips its overlay to accepted and
        pushes the updated intent (so Accept/Apply deploys the edit)."""
        from netbox_routing.models import Community, CommunityListEntry

        cl, state = self._community_list_with_overlay(status="in_sync")

        pushed = []
        with patch(
            "netbox_nso_plugin.adapter_client.put_route_policy_intent",
            side_effect=lambda adapter_id, objects: pushed.append((adapter_id, objects)),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                comm = Community.objects.create(community="65000:1")
                CommunityListEntry.objects.create(community_list=cl, action="permit", community=comm)

        state.refresh_from_db()
        assert state.status == "accepted"  # re-owned by the edit
        assert pushed and pushed[-1][0] == 196  # intent pushed to the adapter

    def test_editing_brownfield_list_is_not_force_owned(self):
        """An un-owned (imported / brownfield) overlay is NOT force-owned by an edit — the
        edit must surface via the 3-way reconcile (changed/conflict), not silently push."""
        from netbox_routing.models import Community, CommunityListEntry

        cl, state = self._community_list_with_overlay(status="imported")

        pushed = []
        with patch(
            "netbox_nso_plugin.adapter_client.put_route_policy_intent",
            side_effect=lambda adapter_id, objects: pushed.append((adapter_id, objects)),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                comm = Community.objects.create(community="65000:2")
                CommunityListEntry.objects.create(community_list=cl, action="permit", community=comm)

        state.refresh_from_db()
        assert state.status == "imported"  # left for reconcile to surface
        assert pushed == []


class TestOwnershipCascade(_RPBase):
    """Owning a route-map cascades ownership to its referenced prefix-lists / community-lists /
    as-paths — otherwise the route-map's ``match`` references dangle on the device (the gap that
    left an ``ip as-path access-list`` missing after a route-map apply)."""

    def _route_map_with_refs(self, name="RM-CASCADE"):
        from netbox_routing.models import ASPath, CommunityList, RouteMap, RouteMapEntry

        rm = RouteMap.objects.create(name=name)
        e = RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit")
        ap = ASPath.objects.create(name="50")
        cl = CommunityList.objects.create(name="CL-CASCADE")
        pl = self._prefix_list(name="PL-CASCADE")
        from netbox_nso_plugin.signals import suppress_intent_push

        with suppress_intent_push():
            e.match_aspath.add(ap)
            e.match_community_list.add(cl)
            e.match_prefix_list.add(pl)
        return rm, ap, cl, pl

    def test_cascade_owns_referenced_contributors(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import _own_route_map_contributors

        mgmt = self._mgmt()
        rm, _ap, _cl, _pl = self._route_map_with_refs()
        _own_route_map_contributors(mgmt, rm)

        owned = {(s.family, s.object_name): s.status for s in NSORoutePolicyState.objects.filter(management=mgmt)}
        assert owned.get(("as_path", "50")) == "accepted"
        assert owned.get(("community_list", "CL-CASCADE")) == "accepted"
        assert owned.get(("prefix_list", "PL-CASCADE")) == "accepted"

    def test_cascade_does_not_clobber_already_owned_contributor(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ASPath

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import _own_route_map_contributors, suppress_intent_push

        mgmt = self._mgmt()
        rm, ap, _cl, _pl = self._route_map_with_refs()
        with suppress_intent_push():
            NSORoutePolicyState.objects.create(
                management=mgmt,
                family="as_path",
                object_name="50",
                content_type=ContentType.objects.get_for_model(ASPath),
                object_id=ap.pk,
                status="in_sync",
            )
        _own_route_map_contributors(mgmt, rm)
        st = NSORoutePolicyState.objects.get(management=mgmt, family="as_path", object_name="50")
        assert st.status == "in_sync"  # an already-owned contributor is left untouched

    def test_editing_owned_route_map_cascades_to_new_reference(self):
        """The real fix: adding an as-path to an OWNED route-map auto-owns the as-path
        (so the apply pushes the list, not just `match as-path`)."""
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ASPath, RouteMap, RouteMapEntry

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push

        mgmt = self._mgmt()
        rm = RouteMap.objects.create(name="RM-EDIT-CASCADE")
        e = RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit")
        with suppress_intent_push():
            NSORoutePolicyState.objects.create(
                management=mgmt,
                family="route_map",
                object_name="RM-EDIT-CASCADE",
                content_type=ContentType.objects.get_for_model(RouteMap),
                object_id=rm.pk,
                status="in_sync",
            )
        ap = ASPath.objects.create(name="50")

        with patch("netbox_nso_plugin.adapter_client.put_route_policy_intent", side_effect=lambda a, o: None):
            with self.captureOnCommitCallbacks(execute=True):
                e.match_aspath.add(ap)
                e.save()  # → _on_routing_policy_entry_save → cascade

        assert NSORoutePolicyState.objects.filter(
            management=mgmt, family="as_path", object_name="50", status="accepted"
        ).exists()
