# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Route-policy exact ownership workflows and entry serialization.

The route-policy push serializes a netbox-routing PrefixList's entries; this guards the
field mapping (prefix_list_entries / sequence / assigned_prefix.prefix) and the
explicit attach acquisition and foreign native signal neutrality.
"""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from .mixins import IntentPushDeliveryMixin


def _save_without_push(instance):
    from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
    from netbox_nso_plugin.signals import suppress_intent_push

    with suppress_intent_push(), intent_transaction(footprint_for_instance(instance)):
        instance.save()
    return instance


def _execute_route_map_acquisition(mgmt, route_map):
    """Execute the production acquisition plan for one route-map fixture."""
    from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes
    from netbox_nso_plugin.signals import _route_policy_acquisition_plan

    plan, operations, result = _route_policy_acquisition_plan(mgmt, route_maps=(route_map,))
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer:
        for candidate, fields, created in operations:
            writer.save(candidate, update_fields=fields, force_insert=created)
    return result


class _RPBase(IntentPushDeliveryMixin, TestCase):
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


class TestRoutePolicySignalQueries(_RPBase):
    def test_state_save_without_writer_does_not_load_management(self):
        from django.contrib.contenttypes.models import ContentType
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import _on_route_policy_state_save

        mgmt = self._mgmt()
        prefix_list = self._prefix_list("QUERY-PL")
        state = _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="prefix_list",
                object_name=prefix_list.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=prefix_list.pk,
                status="imported",
            )
        )
        state = NSORoutePolicyState.objects.only("pk", "management_id").get(pk=state.pk)

        with CaptureQueriesContext(connection) as captured:
            _on_route_policy_state_save(sender=NSORoutePolicyState, instance=state)

        self.assertEqual(captured.captured_queries, [])


class TestRoutePolicyIntentAcceptedFlag(_RPBase):
    """Every OWNED route-policy object is pushed accepted=True so the adapter keeps it eligible
    for Apply. Keying accepted off status=='accepted' dropped the flag once a row advanced to
    deploying/in_sync/apply_failed → the adapter stamped no accepted_at → the object was
    ineligible → Apply pushed 0 route-policy items and the row stuck in 'deploying' (live on rg03:
    an owned as-path whose adapter intent had accepted_at=NULL never applied and never settled)."""

    def _overlay(self, status):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSOApplyAttempt, NSORoutePolicyState

        mgmt = self._mgmt()
        pl = self._prefix_list()
        attempt = NSOApplyAttempt.objects.create(management=mgmt) if status == "deploying" else None
        _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status=status,
                apply_attempt_id=attempt.pk if attempt is not None else None,
            )
        )
        return mgmt

    def _push_and_capture(self, mgmt):
        from netbox_nso_plugin.delivery import deliver

        pushed = []
        with patch(
            "netbox_nso_plugin.adapter_client.put_route_policy_intent",
            side_effect=lambda adapter_id, objects: pushed.append(objects),
        ):
            deliver("route_policy", mgmt.device_id, mgmt.adapter_device_id)
        return pushed[-1] if pushed else []

    def test_deploying_row_pushed_accepted_true(self):
        objs = self._push_and_capture(self._overlay("deploying"))
        assert len(objs) == 1 and objs[0]["name"] == "TESTNSO-PL"
        assert objs[0]["accepted"] is True  # owned intent stays eligible on the adapter

    def test_in_sync_row_pushed_accepted_true(self):
        objs = self._push_and_capture(self._overlay("in_sync"))
        assert objs[0]["accepted"] is True


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


class TestStructuredFieldsProjectIntoJson(_RPBase):
    """Operator-authored STRUCTURED RouteMapEntry fields must land on the device.

    The reader derives the structured fields (match_afi / call_policy / set_communities /
    vendor_ext) FROM the match/set blobs and keeps the blobs authoritative for the write side
    (route_policy_reconciler ~283). So a structured field the operator sets by hand — with no
    corresponding key in the blob — used to be SILENTLY DROPPED on Apply: the serializer only
    emitted match/set-json + the M2M refs. That violates the no-silent-drop intent-integrity
    rule. The write path now projects each structured field back into the exact blob key the
    nso-packages writer consumes (verified against route-policy-reconciler):

      call_policy   -> match ``_junos_from_policy`` (list)      [_junos.py:293]
      apply_policy  -> set   ``apply`` (_as_list-tolerant)      [_iosxr.py:198]
      match_afi     -> match ``family`` + ``_junos_family``     [_junos.py:295, scalar Junos token]
      set_community -> set   ``community``/``community_additive``/``community_delete``
      vendor_ext    -> the namespaced ``_rpl_``/``_junos_``/``_timos_`` keys

    Structured WINS on divergence (the operator edited it), but an agreeing blob is left
    byte-identical so the brownfield round-trip does not churn.
    """

    def _entry(self, name, **kwargs):
        from netbox_routing.models import RouteMap, RouteMapEntry

        rm = RouteMap.objects.create(name=name)
        e = RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit", **kwargs)
        return rm, e

    def _push(self, rm):
        import json

        from netbox_nso_plugin.signals import _build_route_policy_entries

        entries = _build_route_policy_entries("route_map", rm)
        return json.loads(entries[0]["match-json"]), json.loads(entries[0]["set-json"])

    def test_call_policy_projects_into_match_json(self):
        """call_policy (Junos from-policy / IOS-XR subroutine) → match ``_junos_from_policy``."""
        from netbox_routing.models import RouteMap

        target = RouteMap.objects.create(name="TESTNSO-SUBR")
        rm, e = self._entry("TESTNSO-RM-CALL")
        e.call_policy = target
        e.save()

        match_json, _ = self._push(rm)
        assert match_json["_junos_from_policy"] == ["TESTNSO-SUBR"]

    def test_apply_policy_projects_into_set_json(self):
        """apply_policy (IOS-XR ``apply`` tail-call / Junos chaining) → set ``apply``."""
        from netbox_routing.models import RouteMap

        target = RouteMap.objects.create(name="TESTNSO-TAIL")
        rm, e = self._entry("TESTNSO-RM-APPLY")
        e.apply_policy = target
        e.save()

        _, set_json = self._push(rm)
        assert set_json["apply"] == ["TESTNSO-TAIL"]

    def test_match_afi_projects_family_keys(self):
        """match_afi → canonical ``family`` + the Junos-token scalar ``_junos_family``."""
        rm, e = self._entry("TESTNSO-RM-AFI", match_afi=["ipv6"])

        match_json, _ = self._push(rm)
        assert match_json["family"] == ["ipv6"]
        # the Junos writer does `frm.family = str(fam)` — it needs the Junos spelling
        assert match_json["_junos_family"] == "inet6"

    def test_set_community_add_projects_into_set_json(self):
        """An 'add' row → ``community`` + ``community_additive`` + the Junos per-name verb.

        SCALAR when there is a single target: IOS-XR renders ``set community ({community})``
        by direct interpolation, so a list would emit ``set community (['X'])``. IOS and Junos
        both accept a scalar (they wrap it), so scalar is the safe universal shape.
        """
        from netbox_routing.models import CommunityList, RouteMapEntrySetCommunity

        add_cl = CommunityList.objects.create(name="TESTNSO-CL-ADD")
        rm, e = self._entry("TESTNSO-RM-SC-ADD")
        RouteMapEntrySetCommunity.objects.create(route_map_entry=e, operation="add", community_list=add_cl)

        _, set_json = self._push(rm)
        assert set_json["community"] == "TESTNSO-CL-ADD"
        assert set_json["community_additive"] is True
        assert set_json["_junos_community_op"] == ["add"]

    def test_set_community_delete_projects_scalar_delete_key(self):
        """A 'delete' row → ``community_delete`` ONLY (IOS-XR ``delete community X``, scalar).

        The delete target must NOT also land in ``community`` — IOS-XR reads both keys, so it
        would emit ``set community (X)`` AND ``delete community X`` for the same list.
        """
        from netbox_routing.models import CommunityList, RouteMapEntrySetCommunity

        del_cl = CommunityList.objects.create(name="TESTNSO-CL-DEL")
        rm, e = self._entry("TESTNSO-RM-SC-DEL")
        RouteMapEntrySetCommunity.objects.create(route_map_entry=e, operation="delete", community_list=del_cl)

        _, set_json = self._push(rm)
        assert set_json["community_delete"] == "TESTNSO-CL-DEL"
        assert "community" not in set_json

    def test_vendor_ext_projects_namespaced_keys(self):
        """vendor_ext {"junos": {"priority": "high"}} → the flat ``_junos_priority`` key."""
        rm, e = self._entry(
            "TESTNSO-RM-VE",
            vendor_ext={"junos": {"priority": "high"}, "timos": {"description": "d"}},
        )

        match_json, set_json = self._push(rm)
        merged = {**match_json, **set_json}
        assert merged["_junos_priority"] == "high"
        assert merged["_timos_description"] == "d"

    def test_structured_edit_drops_rpl_raw_verbatim_body(self):
        """IOS-XR edit-invalidation: a structured edit must DROP ``_rpl_raw``.

        IOS-XR RPL is opaque text, so the reader preserves the exact body under ``_rpl_raw`` on
        the route-map's FIRST entry and the writer prefers it verbatim
        (``body = raw if raw is not None else self._iosxr_rpl_body(...)``, _iosxr.py:245). If we
        project a structured edit but leave ``_rpl_raw`` in place, the writer replays the STALE
        body and the operator's edit never reaches the device — a silent drop.
        """
        from netbox_routing.models import RouteMap

        target = RouteMap.objects.create(name="TESTNSO-SUBR-X")
        rm, e = self._entry(
            "TESTNSO-RM-XR-EDIT",
            match={"_rpl_raw": "if destination in (10.0.0.0/8) then\n  pass\nendif"},
        )
        e.call_policy = target  # the operator edit
        e.save()

        match_json, _ = self._push(rm)
        assert "_rpl_raw" not in match_json, "stale verbatim RPL body would override the edit"
        assert match_json["_junos_from_policy"] == ["TESTNSO-SUBR-X"]

    def test_unedited_policy_keeps_rpl_raw_verbatim_body(self):
        """The other half: an UNEDITED brownfield policy must KEEP ``_rpl_raw``.

        Dropping it unconditionally would force every IOS-XR policy to re-render from the
        structured parse, which cannot reproduce the opaque RPL text byte-for-byte — turning a
        clean round-trip into a fleet-wide spurious diff (455 lines on ra1 before verbatim).
        """
        body = "if destination in (10.0.0.0/8) then\n  pass\nendif"
        rm, _ = self._entry("TESTNSO-RM-XR-CLEAN", match={"_rpl_raw": body})

        match_json, _ = self._push(rm)
        assert match_json["_rpl_raw"] == body

    def test_agreeing_blob_is_not_churned(self):
        """Brownfield round-trip: when the blob already expresses the construct, the raw vendor
        token is preserved byte-identical (projecting canonical over it would churn the diff)."""
        rm, e = self._entry(
            "TESTNSO-RM-NOCHURN",
            # 'inet' is the Junos token the reader captured; structure_entry maps it to ipv4.
            match={"_junos_family": "inet", "protocol": ["bgp"]},
            match_afi=["ipv4"],
        )

        match_json, _ = self._push(rm)
        # unchanged — NOT rewritten to canonical 'ipv4'
        assert match_json["_junos_family"] == "inet"
        assert match_json["protocol"] == ["bgp"]


class TestRoutePolicyDeletePropagation(_RPBase):
    def test_foreign_prefix_list_delete_does_not_retire_attached_intent(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        pl = self._prefix_list()
        _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status="in_sync",
            )
        )

        pushed = []
        with patch(
            "netbox_nso_plugin.adapter_client.put_route_policy_intent",
            side_effect=lambda adapter_id, objects: pushed.append((adapter_id, objects)),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                PrefixList.objects.get(pk=pl.pk).delete()

        state = NSORoutePolicyState.objects.get(object_name="TESTNSO-PL")
        assert state.assigned_object is None
        assert pushed == []


class TestRoutePolicyEditOwnsAndPushes(_RPBase):
    """Foreign native entry edits do not establish route-policy ownership."""

    def _community_list_with_overlay(self, status="in_sync"):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        cl = CommunityList.objects.create(name="TESTNSO-CL-EDIT")
        state = _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="community_list",
                object_name=cl.name,
                content_type=ContentType.objects.get_for_model(CommunityList),
                object_id=cl.pk,
                status=status,
            )
        )
        return cl, state

    def test_adding_member_to_owned_list_is_foreign_and_does_not_push(self):
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
        assert state.status == "in_sync"
        assert pushed == []

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

        mgmt = self._mgmt()
        rm, _ap, _cl, _pl = self._route_map_with_refs()
        _execute_route_map_acquisition(mgmt, rm)

        owned = {(s.family, s.object_name): s.status for s in NSORoutePolicyState.objects.filter(management=mgmt)}
        assert owned.get(("as_path", "50")) == "accepted"
        assert owned.get(("community_list", "CL-CASCADE")) == "accepted"
        assert owned.get(("prefix_list", "PL-CASCADE")) == "accepted"

    def test_cascade_does_not_clobber_already_owned_contributor(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ASPath

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        rm, ap, _cl, _pl = self._route_map_with_refs()
        _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="as_path",
                object_name="50",
                content_type=ContentType.objects.get_for_model(ASPath),
                object_id=ap.pk,
                status="in_sync",
            )
        )
        _execute_route_map_acquisition(mgmt, rm)
        st = NSORoutePolicyState.objects.get(management=mgmt, family="as_path", object_name="50")
        assert st.status == "in_sync"  # an already-owned contributor is left untouched

    def test_cascade_skips_drifted_reference_and_reports_it(self):
        """A referenced object that DIVERGES on the device (conflict) is NOT force-owned — the
        cascade leaves it (no silent overwrite of the device's version) and reports it so the
        operator can resolve the drift explicitly. The reference still resolves against the
        device's existing object, so the route-map is not left dangling."""
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        rm, _ap, _cl, pl = self._route_map_with_refs()
        _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status="conflict",
            )
        )
        cascade = _execute_route_map_acquisition(mgmt, rm)

        st = NSORoutePolicyState.objects.get(management=mgmt, family="prefix_list", object_name=pl.name)
        assert st.status == "conflict"  # left for explicit resolution, NOT overwritten
        assert ("prefix_list", pl.name) in cascade.drifted  # reported to the caller (the Accept view warns)
        # the greenfield references are still owned (only the drifted one is skipped)
        assert NSORoutePolicyState.objects.get(management=mgmt, family="as_path", object_name="50").status == "accepted"

    def test_cascade_owns_set_community_list_reference(self):
        """A community-list referenced by a route-map's SET action (`set comm-list delete <CL>`)
        is a dependency too — the cascade owns it, else the device rejects the undefined list
        (the same dangling-reference class as a match reference)."""
        from netbox_routing.models import CommunityList, RouteMap, RouteMapEntry, RouteMapEntrySetCommunity

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        rm = RouteMap.objects.create(name="RM-SET-COMM")
        e = RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit")
        cl = CommunityList.objects.create(name="CL-SET-DELETE")
        _save_without_push(RouteMapEntrySetCommunity(route_map_entry=e, operation="delete", community_list=cl))

        _execute_route_map_acquisition(mgmt, rm)

        st = NSORoutePolicyState.objects.get(management=mgmt, family="community_list", object_name="CL-SET-DELETE")
        assert st.status == "accepted"  # set-referenced list owned alongside the route-map

    def test_cascade_reports_cross_device_provenance_for_greenfield_reference(self):
        """A GREENFIELD reference (no overlay on THIS device) whose shared NetBox content was
        materialized from ANOTHER device is still owned — the route-map needs it — but the
        cascade reports its provenance so the Accept view can warn that owning the route-map
        here pushes the other device's version of that object onto this device."""
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSORoutePolicyState

        mgmt = self._mgmt()  # owning onto self.device
        rm, _ap, _cl, pl = self._route_map_with_refs()

        # A SECOND device already materialized PL-CASCADE into NetBox (it is the NetBox source).
        other_dev = Device.objects.create(
            name="rp-router-2",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        inst, _ = NSOInstance.objects.get_or_create(name="rp-inst", defaults={"adapter_instance_id": "rp-inst"})
        other_mgmt = NSODeviceManagement.objects.create(
            device=other_dev, nso_instance=inst, nso_device_name="nso-rp-2", adapter_device_id=297
        )
        _save_without_push(
            NSORoutePolicyState(
                management=other_mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status="in_sync",
                is_materialized=True,
            )
        )

        cascade = _execute_route_map_acquisition(mgmt, rm)

        # Greenfield reference is still owned on this device (route-map needs it)…
        st = NSORoutePolicyState.objects.get(management=mgmt, family="prefix_list", object_name=pl.name)
        assert st.status == "accepted"
        # …but its cross-device provenance is reported (family, name, source device name).
        assert ("prefix_list", pl.name, other_dev.name) in cascade.cross_device

    def test_foreign_owned_route_map_edit_does_not_acquire_new_reference(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import ASPath, RouteMap, RouteMapEntry

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        rm = RouteMap.objects.create(name="RM-EDIT-CASCADE")
        e = RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit")
        _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="route_map",
                object_name="RM-EDIT-CASCADE",
                content_type=ContentType.objects.get_for_model(RouteMap),
                object_id=rm.pk,
                status="in_sync",
            )
        )
        ap = ASPath.objects.create(name="50")

        with patch("netbox_nso_plugin.adapter_client.put_route_policy_intent", side_effect=lambda a, o: None):
            with self.captureOnCommitCallbacks(execute=True):
                e.match_aspath.add(ap)
                e.save()  # → _on_routing_policy_entry_save → cascade

        assert not NSORoutePolicyState.objects.filter(management=mgmt, family="as_path", object_name="50").exists()


class TestUnsupportedMembersStorage(_RPBase):
    """The adapter reports community members this device's NED can't hold; the push
    stores them on the overlay so the UI can show "unsupported on <ned>" instead of a
    suspicious unexplained "pending apply". Real ORM rows; only the adapter PUT is faked."""

    def _community_overlay(self, name="example-comm", members=("64500:*", "color:0:12.")):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        cl = CommunityList.objects.create(name=name)
        for i, value in enumerate(members, start=1):
            CommunityListEntry.objects.create(
                community_list=cl, action="permit", community=Community.objects.create(community=value)
            )
        state = _save_without_push(
            NSORoutePolicyState(
                management=mgmt,
                family="community_list",
                object_name=name,
                content_type=ContentType.objects.get_for_model(CommunityList),
                object_id=cl.pk,
                status="accepted",
            )
        )
        return mgmt, state

    def _push(self, mgmt, resp):
        from netbox_nso_plugin.delivery import deliver

        with patch("netbox_nso_plugin.adapter_client.put_route_policy_intent", side_effect=lambda a, o: resp):
            deliver("route_policy", mgmt.device_id, mgmt.adapter_device_id)

    def test_unsupported_members_stored_from_adapter_response(self):
        mgmt, state = self._community_overlay()
        self._push(mgmt, {"objects": [], "unsupported_members": {"example-comm": ["color:0:12."]}})
        state.refresh_from_db()
        assert state.unsupported_members == ["color:0:12."]

    def test_object_absent_from_map_is_cleared_to_empty(self):
        # A previously-flagged member that now applies (or a fully-representable object)
        # must be cleared, else a stale "unsupported" badge lingers forever.
        mgmt, state = self._community_overlay()
        state.unsupported_members = ["color:0:12."]
        state.save(update_fields=["unsupported_members"])
        self._push(mgmt, {"objects": [], "unsupported_members": {}})
        state.refresh_from_db()
        assert state.unsupported_members == []
