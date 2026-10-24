# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Collection coverage controls removal differences through the HTTP view."""

import copy
from uuid import uuid4

import pytest
from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from ipam.models import VLAN, VLANGroup
from netbox_routing.models import OSPFArea, OSPFInstance, RouteMap, RouteMapEntry

from netbox_nso_plugin.models import (
    NSOFamilyObservation,
    NSOFamilyReadState,
    NSOOSPFInstanceState,
    NSORoutePolicyState,
)
from netbox_nso_plugin.observations import observation_defaults

from ._routing_observation_case import DOCUMENTS, routing_observation
from ._scope_observation_case import entry, scope_observation
from .test_gated_reconcile import _make


@pytest.fixture
def comparison_page(db, client):
    device, management = _make(f"collection{uuid4().hex[:8]}")
    client.force_login(get_user_model().objects.create_superuser(username=f"collection{uuid4().hex[:8]}"))

    def page(family, document):
        observe = routing_observation if family in DOCUMENTS else scope_observation
        observed = observe(family, document=document)
        read_state, _ = NSOFamilyReadState.objects.get_or_create(management=management, family=family)
        NSOFamilyObservation.objects.update_or_create(
            read_state=read_state, defaults=observation_defaults(family, 1, 1, observed)
        )
        response = client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_differences", kwargs={"pk": device.pk}),
            {"scope": family},
        )
        assert response.status_code == 200
        return list(response.context["page_obj"].object_list)

    return device, management, page


@pytest.mark.parametrize("covered", [False, True])
def test_ospf_area_removal_requires_explicit_collection_presence(comparison_page, covered):
    device, management, page = comparison_page
    native = OSPFInstance.objects.create(device=device, name="OSPF", process_id="1", router_id="198.18.0.1")
    area = OSPFArea.objects.create(area_id="0.0.0.0", area_type="standard")
    NSOOSPFInstanceState.objects.create(
        management=management, ospf_instance=native, process_id="1", areas=[{"area-id": area.area_id}]
    )
    parent = entry(process_id="1", vrf="", router_id="198.18.0.1", area=[])
    if not covered:
        del parent["present"]
    rows = page(
        "ospf",
        {"instances": [parent], "interfaces": [], "present": ["instances", "interfaces"], "unprojectable": []},
    )
    removals = [row for row in rows if row["kind"] == "netbox_only"]
    assert bool(removals) is covered
    if not covered:
        assert any(row["kind"] == "unavailable" and row["attribute"] == "area" for row in rows)


@pytest.mark.parametrize("covered", [False, True])
def test_policy_entry_removal_requires_explicit_collection_presence(comparison_page, covered):
    _device, management, page = comparison_page
    native = RouteMap.objects.create(name="IMPORT")
    RouteMapEntry.objects.create(route_map=native, sequence=1, action="permit")
    NSORoutePolicyState.objects.create(
        management=management,
        family="route_map",
        object_name=native.name,
        content_type=ContentType.objects.get_for_model(native),
        object_id=native.pk,
    )
    document = copy.deepcopy(DOCUMENTS["route_policy"])
    for name in document["present"]:
        document[name] = []
    parent = entry(name="IMPORT", entry=[])
    if not covered:
        del parent["present"]
    document["route_maps"] = [parent]
    rows = page("route_policy", document)
    assert any(row["kind"] == "netbox_only" for row in rows) is covered
    if not covered:
        assert any(row["kind"] == "unavailable" and row["attribute"] == "entry" for row in rows)


@pytest.mark.parametrize("unknown", ["unmarked", "null"])
def test_policy_match_collection_unknown_is_unavailable(comparison_page, unknown):
    _device, management, page = comparison_page
    native = RouteMap.objects.create(name="IMPORT")
    RouteMapEntry.objects.create(route_map=native, sequence=1, action="permit")
    NSORoutePolicyState.objects.create(
        management=management,
        family="route_map",
        object_name=native.name,
        content_type=ContentType.objects.get_for_model(native),
        object_id=native.pk,
    )
    item = entry(
        sequence=10,
        action="permit",
        match_prefix_lists=[],
        match_community_lists=[],
        match_as_paths=[],
        match_json="",
        set_json="",
    )
    if unknown == "null":
        item["match_prefix_lists"] = None
    else:
        item["present"].remove("match_prefix_lists")
    document = {name: [] for name in ("prefix_lists", "community_lists", "as_paths", "route_maps")}
    document["present"] = list(document)
    document.update(route_maps=[entry(name="IMPORT", entry=[item])], unprojectable=[])
    rows = page("route_policy", document)
    assert any(row["kind"] == "unavailable" and row["attribute"] == "match_prefix_lists" for row in rows)
    assert not any(row["kind"] == "mismatch" for row in rows)


@pytest.mark.parametrize("value", ["missing", "null"])
def test_access_port_tagged_vlan_value_has_no_collection_gap(comparison_page, value):
    device, _management, page = comparison_page
    port = Interface.objects.get(device=device)
    port.name, port.type, port.mode = "Ethernet1", "1000base-t", "tagged"
    port.save()
    group = VLANGroup.objects.create(name="Example", slug="example")
    port.tagged_vlans.add(VLAN.objects.create(group=group, vid=10, name="Example"))
    observed = entry(interface_name=port.name, mode="access", untagged_vlan=10)
    if value == "null":
        observed["tagged_vlans"] = None
        observed["present"].append("tagged_vlans")
    rows = page(
        "switchport",
        {
            "interfaces": [observed],
            "present": ["interfaces"],
            "unprojectable": [],
        },
    )
    tagged = [row for row in rows if row["attribute"] == "tagged_vlans"]
    assert not any(row["kind"] == "unavailable" for row in tagged)
    assert not any("tagged_vlans coverage" in row.get("reason", "") for row in rows)
    assert any(row["kind"] == "mismatch" and row["attribute"] == "mode" for row in rows)
