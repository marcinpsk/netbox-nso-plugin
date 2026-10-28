# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Mirror planners require the matching observation to cover their children."""

import copy

import pytest
from dcim.models import Interface

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.models import NSOSwitchportState

from ._outbox_case import make_managed
from ._routing_observation_case import routing_observation
from ._scope_observation_case import entry, scope_observation
from .test_bgp_router_reconcile import router_document, router_model


@pytest.fixture
def admission_case(db):
    return make_managed("collection-admission", 1833)


@pytest.mark.parametrize("covered", [False, True])
def test_groupless_bgp_router_import_requires_covered_peer_groups(admission_case, covered):
    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

    device, management = admission_case
    scope = entry(vrf="", address_family=[entry(afi="ipv4-unicast")], peer=[], peer_group=[])
    if not covered:
        scope["present"].remove("peer_group")
    payload = router_document(device)
    payload["observation"] = routing_observation(
        "bgp",
        document={
            "routers": [entry(asn="64512", router_id="192.0.2.254", scope=[scope])],
            "present": ["routers"],
            "unprojectable": [],
        },
    )
    _reconcile_bgp_config(device, payload)
    assert router_model().objects.filter(management=management, status="imported").exists() is covered


def test_bgp_group_without_presence_cannot_certify_empty_address_families(admission_case):
    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

    device, management = admission_case
    group = {"name": "EXAMPLE", "peer_group_address_family": []}
    payload = router_document(device)
    payload["observation"] = routing_observation(
        "bgp",
        document={
            "routers": [
                entry(
                    asn="64512",
                    router_id="192.0.2.254",
                    scope=[entry(vrf="", address_family=[entry(afi="ipv4-unicast")], peer=[], peer_group=[group])],
                )
            ],
            "present": ["routers"],
            "unprojectable": [],
        },
    )
    _reconcile_bgp_config(device, payload)
    assert not router_model().objects.filter(management=management).exists()


@pytest.mark.parametrize("family", ["ospf", "route_policy", "l2_service", "lag_config"])
@pytest.mark.parametrize("unknown", ["unmarked", "null", "missing"])
def test_mirror_reconcile_refuses_unknown_child_collection(admission_case, family, unknown):
    from netbox_nso_plugin.l2_service_reconciler import reconcile_l2_services
    from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config
    from netbox_nso_plugin.ospf_reconciler import reconcile_ospf
    from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

    from ._routing_observation_case import DOCUMENTS as ROUTING_DOCUMENTS
    from ._scope_observation_case import DOCUMENTS

    device, _management = admission_case
    Interface.objects.create(device=device, name="Ethernet1", type="1000base-t")
    Interface.objects.create(device=device, name="Port-channel1", type="lag")
    documents = {**DOCUMENTS, **ROUTING_DOCUMENTS}
    document = copy.deepcopy(documents[family])
    collection, child = {
        "ospf": ("instances", "area"),
        "route_policy": ("prefix_lists", "entry"),
        "l2_service": ("services", "saps"),
        "lag_config": ("bundles", "member"),
    }[family]
    parent = document[collection][0]
    parent[child] = []
    if unknown == "unmarked":
        parent["present"].remove(child)
    elif unknown == "null":
        parent[child] = None
    else:
        del parent[child]
    payload = {
        "ospf": {"instances": [{"process_id": "1", "router_id": "198.18.0.1"}], "interfaces": []},
        "route_policy": {
            "prefix_lists": [{"name": "EXAMPLE", "family": 4}],
            "community_lists": [],
            "as_paths": [],
            "route_maps": [],
        },
        "l2_service": {"services": [{"service_name": "example-service", "service_type": "epipe", "service_id": 100}]},
        "lag_config": {"bundles": [{"name": "Port-channel1", "lag_id": 1}]},
    }[family]
    observe = routing_observation if family in ROUTING_DOCUMENTS else scope_observation
    payload["observation"] = observe(family, document=document)
    reconcile = {
        "ospf": reconcile_ospf,
        "route_policy": reconcile_route_policy,
        "l2_service": reconcile_l2_services,
        "lag_config": reconcile_lag_config,
    }[family]
    before = list(Interface.objects.filter(device=device).order_by("pk").values())
    with pytest.raises(AdapterError, match="coverage"):
        reconcile(device, payload)
    assert list(Interface.objects.filter(device=device).order_by("pk").values()) == before


@pytest.mark.parametrize("unknown", ["unmarked", "null", "missing"])
def test_switchport_omitted_tagged_vlan_value_allows_access_port_import(admission_case, unknown):
    from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

    device, management = admission_case
    port = Interface.objects.create(device=device, name="Ethernet1", type="1000base-t")
    seed = {"interfaces": [{"interface_name": port.name, "mode": "trunk", "untagged_vlan": None, "tagged_vlans": [10]}]}
    seed["observation"] = scope_observation(
        "switchport",
        document={
            "interfaces": [entry(interface_name=port.name, mode="trunk", untagged_vlan=None, tagged_vlans=[10])],
            "present": ["interfaces"],
            "unprojectable": [],
        },
    )
    reconcile_switchport(device, seed)
    before = list(port.tagged_vlans.order_by("pk").values_list("pk", flat=True))
    assert before
    payload = copy.deepcopy(seed)
    payload["interfaces"][0].update(mode="access", tagged_vlans=[])
    observed = payload["observation"]["document"]["interfaces"][0]
    observed.update(mode="access", tagged_vlans=[])
    if unknown == "null":
        observed["tagged_vlans"] = None
    elif unknown == "unmarked":
        observed["present"].remove("tagged_vlans")
    else:
        del observed["tagged_vlans"]
    reconcile_switchport(device, payload)
    port.refresh_from_db()
    assert port.mode == "access"
    assert not port.tagged_vlans.exists()
    state = NSOSwitchportState.objects.get(management=management, interface=port)
    assert state.mode == "access"
    assert not state.tagged_vlans.exists()
