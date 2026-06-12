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
        assert entries == [{"sequence": 10, "action": "permit", "match": {"x": 1}, "set": {"y": 2}}]


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
