# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Exercise routing graph visibility through the public comparison interface."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from netbox_nso_plugin import difference_projection
from netbox_nso_plugin.comparison_values import MISSING, ProjectedEntry
from netbox_nso_plugin.device_differences import NOT_VISIBLE, SCOPE_SPECS, differences


class _ViewableObjects:
    """Represent the permission query contract without a database."""

    def __init__(self, pks=()):
        self.pks = set(pks)

    def restrict(self, user, action):
        assert action == "view"
        return _ViewableObjects(user)

    def filter(self, *, pk__in):
        return _ViewableObjects(self.pks.intersection(pk__in))

    def values_list(self, field, *, flat):
        assert (field, flat) == ("pk", True)
        return self.pks


@dataclass(frozen=True)
class _Dependency:
    pk: int

    objects = _ViewableObjects()


_ROUTER = ("router", 64512)
_SCOPE = (*_ROUTER, "scope", "")
_PEER = (*_SCOPE, "peer", "198.18.0.2")
_GROUP = (*_SCOPE, "peer_group", "EXAMPLE")
_INSTANCE = ("instance", "10", "")
_ISIS_INTERFACE = ("interface", "Ethernet1", "ipv4")
_REDIST_DESTINATION = ("bgp", "64512//ipv4-unicast", None)

# Each row names an independently specified structural edge, including non-prefix edges.
STRUCTURES = [
    ("lacp_member", "lacp", ("bundle", "Port-channel1"), ("member", "Ethernet1"), {}, {"bundle": "Port-channel1"}),
    ("l2_sap", "l2_sap", ("EXAMPLE",), ("EXAMPLE", "Ethernet1:100"), {}, {}),
    ("isis_process_setting", "isis", ("process", "CORE"), ("process", "CORE", "setting", "example-setting"), {}, {}),
    ("isis_process_level", "isis", ("process", "CORE"), ("process", "CORE", "level", 2), {}, {}),
    ("isis_segment_routing", "isis", ("process", "CORE"), ("process", "CORE", "segment_routing"), {}, {}),
    ("isis_srv6_locator", "isis", ("process", "CORE"), ("process", "CORE", "srv6_locator", "EXAMPLE"), {}, {}),
    ("isis_flex_algo", "isis_flex_algo", ("process", "CORE"), ("process", "CORE", "flex_algo", 128), {}, {}),
    ("isis_process_interface", "isis", ("process", "CORE"), _ISIS_INTERFACE, {}, {"process_tag": "CORE"}),
    ("isis_interface_setting", "isis", _ISIS_INTERFACE, (*_ISIS_INTERFACE, "setting", "example-setting"), {}, {}),
    ("isis_interface_level", "isis", _ISIS_INTERFACE, (*_ISIS_INTERFACE, "level", 2), {}, {}),
    ("isis_interface_prefix_sid", "isis", _ISIS_INTERFACE, (*_ISIS_INTERFACE, "prefix_sid", 128), {}, {}),
    ("ospf_area", "ospf", _INSTANCE, (*_INSTANCE, "area", "0.0.0.0"), {}, {}),
    ("ospf_instance_interface", "ospf", _INSTANCE, ("interface", "Ethernet1", "10"), {}, {"area_id": "0.0.0.0"}),
    (
        "ospf_area_interface",
        "ospf",
        (*_INSTANCE, "area", "0.0.0.0"),
        ("interface", "Ethernet1", "10"),
        {},
        {"area_id": "0.0.0.0"},
    ),
    ("bgp_scope", "bgp", _ROUTER, _SCOPE, {}, {}),
    ("bgp_address_family", "bgp", _SCOPE, (*_SCOPE, "address_family", "ipv4-unicast"), {}, {}),
    ("bgp_peer", "bgp", _SCOPE, _PEER, {}, {}),
    ("bgp_peer_group", "bgp", _SCOPE, _GROUP, {}, {}),
    ("bgp_unplaced_peer_group", "bgp", _ROUTER, (*_ROUTER, "peer_group", "EXAMPLE"), {}, {}),
    ("bgp_group_peer", "bgp", _GROUP, _PEER, {}, {"peer_group": "EXAMPLE"}),
    ("bgp_peer_af", "bgp", _PEER, (*_PEER, "peer_address_family", "ipv4-unicast"), {}, {}),
    ("bgp_group_af", "bgp", _GROUP, (*_GROUP, "peer_group_address_family", "ipv4-unicast"), {}, {}),
    (
        "bgp_scope_af_group_af",
        "bgp",
        (*_SCOPE, "address_family", "ipv4-unicast"),
        (*_GROUP, "peer_group_address_family", "ipv4-unicast"),
        {},
        {},
    ),
    (
        "bgp_scope_af_peer_af",
        "bgp",
        (*_SCOPE, "address_family", "ipv4-unicast"),
        (*_PEER, "peer_address_family", "ipv4-unicast"),
        {},
        {},
    ),
    ("prefix_list_entry", "route_policy", ("prefix_lists", "EXAMPLE"), ("prefix_lists", "EXAMPLE", "entry", 1), {}, {}),
    (
        "community_list_entry",
        "route_policy",
        ("community_lists", "EXAMPLE"),
        ("community_lists", "EXAMPLE", "entry", 1),
        {},
        {},
    ),
    ("as_path_entry", "route_policy", ("as_paths", "EXAMPLE"), ("as_paths", "EXAMPLE", "entry", 1), {}, {}),
    ("route_map_entry", "route_policy", ("route_maps", "EXAMPLE"), ("route_maps", "EXAMPLE", "entry", 1), {}, {}),
    ("redistribution_isis", "redistribution", ("isis", "CORE", None), ("isis", "CORE", None, "static", ""), {}, {}),
    ("redistribution_ospf", "redistribution", ("ospf", "10", ""), ("ospf", "10", "", "static", ""), {}, {}),
    (
        "redistribution_bgp_router_scope",
        "redistribution",
        ("bgp",),
        ("bgp",),
        {"router_asn": "64512"},
        {"router_asn": "64512", "vrf": ""},
    ),
    (
        "redistribution_bgp_scope_af",
        "redistribution",
        ("bgp",),
        _REDIST_DESTINATION,
        {"router_asn": "64512", "vrf": ""},
        {},
    ),
    ("redistribution_bgp_entry", "redistribution", _REDIST_DESTINATION, (*_REDIST_DESTINATION, "static", ""), {}, {}),
]


def _child_entry(scope, identity, values, objects=()):
    parents = None
    if scope == "ospf" and identity[0] == "interface":
        parents = (_INSTANCE, (*_INSTANCE, "area", values["area_id"]))
    return ProjectedEntry(identity, values, objects, parent_identities=parents)


def _graph_comparison(monkeypatch, scope, native, observed, visible=()):
    """Use recorded projection graphs at the routing persistence adapter seam."""
    if scope in {"lacp", "l2_sap"}:
        monkeypatch.setattr(difference_projection, "native_entries", lambda *args, **kwargs: native)
        monkeypatch.setattr(difference_projection, "device_interfaces", lambda management: ())
        projector = "_lag_device" if scope == "lacp" else "_l2_device"
        monkeypatch.setattr(difference_projection, projector, lambda *args: observed)
    else:
        projection = difference_projection.routing_projection(scope)
        adapter = SimpleNamespace(
            native_entries=lambda *args, **kwargs: native,
            device_entries=lambda *args, **kwargs: observed,
            component=projection.component,
            nested_gaps=lambda *args: {},
        )
        monkeypatch.setattr(difference_projection, "routing_projection", lambda requested: adapter)
    spec = SCOPE_SPECS[scope]
    document = {name: [] for name in spec.components}
    document.update(present=list(spec.components), unprojectable=[])
    if scope == "redistribution":
        document["components"] = [
            {"protocol": protocol, "inventory": [], "present": ["inventory"]} for protocol in spec.components
        ]
    snapshot = SimpleNamespace(
        document=document,
        coverage={"attributes": [], "components": [{"protocol": protocol} for protocol in spec.components]},
    )
    return [
        row
        for row in differences(SimpleNamespace(), user=visible, snapshots={spec.family: snapshot})
        if row.scope == scope
    ]


@pytest.mark.parametrize(
    "structure,scope,parent,child,parent_values,child_values", STRUCTURES, ids=[row[0] for row in STRUCTURES]
)
@pytest.mark.parametrize("hidden_side", ["native", "observed", "both"])
@pytest.mark.parametrize("child_side", ["native", "observed"])
def test_hidden_structural_parent_never_discloses_child(
    monkeypatch, structure, scope, parent, child, parent_values, child_values, hidden_side, child_side
):
    native_hidden, observed_hidden = _Dependency(1), _Dependency(2)
    native_objects = (native_hidden,) if hidden_side in {"native", "both"} else ()
    observed_objects = (observed_hidden,) if hidden_side in {"observed", "both"} else ()
    native = [ProjectedEntry(parent, dict(parent_values), native_objects, coverage_only=True)]
    observed = [ProjectedEntry(parent, dict(parent_values), observed_objects, coverage_only=True)]
    descendant = _child_entry(scope, child, {**child_values, "description": "example-child-value"})
    (native if child_side == "native" else observed).append(descendant)
    rows = _graph_comparison(monkeypatch, scope, native, observed)
    if child_side == "observed":
        assert rows
    assert all(row.kind == "ambiguous" and row.reason == NOT_VISIBLE for row in rows)
    assert all(dependency in descendant.objects for dependency in (*native_objects, *observed_objects))
    assert all(row.identity == "" and row.netbox_value is MISSING and row.device_value is MISSING for row in rows)
    assert "example-child-value" not in repr(rows)
    assert repr(child) not in repr(rows)
    if hidden_side == "both":
        for visible in ({native_hidden.pk}, {observed_hidden.pk}):
            rows = _graph_comparison(monkeypatch, scope, native, observed, visible=visible)
            assert all(row.identity == "" for row in rows)
            assert "example-child-value" not in repr(rows)
    visible = {dependency.pk for dependency in (*native_objects, *observed_objects)}
    rows = _graph_comparison(monkeypatch, scope, native, observed, visible=visible)
    assert any(row.identity == child for row in rows)
    assert "example-child-value" in repr(rows)


@pytest.mark.parametrize(
    "structure,scope,parent,child,parent_values,child_values", STRUCTURES, ids=[row[0] for row in STRUCTURES]
)
@pytest.mark.parametrize("child_side", ["native", "observed"])
def test_visible_structural_parent_preserves_visible_child(
    monkeypatch, structure, scope, parent, child, parent_values, child_values, child_side
):
    parent_object, child_object = _Dependency(1), _Dependency(2)
    native = [ProjectedEntry(parent, dict(parent_values), (parent_object,), coverage_only=True)]
    observed = [ProjectedEntry(parent, dict(parent_values), coverage_only=True)]
    descendant = _child_entry(scope, child, {**child_values, "description": "example-child-value"}, (child_object,))
    (native if child_side == "native" else observed).append(descendant)
    rows = _graph_comparison(monkeypatch, scope, native, observed, visible={parent_object.pk, child_object.pk})
    expected_kind = "netbox_only" if child_side == "native" else "device_only"
    assert [(row.kind, row.identity) for row in rows] == [(expected_kind, child)]
    assert "example-child-value" in repr(rows)


def test_duplicate_parents_and_transitive_ancestors_keep_all_dependencies(monkeypatch):
    hidden, other_hidden = _Dependency(1), _Dependency(2)
    native = [
        ProjectedEntry(_ROUTER, {}, (hidden,), coverage_only=True),
        ProjectedEntry((*_PEER, "peer_address_family", "ipv4-unicast"), {"description": "example-child-value"}),
    ]
    observed = [
        ProjectedEntry(_ROUTER, {}, coverage_only=True),
        ProjectedEntry(_SCOPE, {}, coverage_only=True),
        ProjectedEntry(_SCOPE, {}, (other_hidden,), coverage_only=True),
        ProjectedEntry(_PEER, {}, coverage_only=True),
    ]
    rows = _graph_comparison(monkeypatch, "bgp", native, observed)
    assert all(row.kind == "ambiguous" and row.identity == "" for row in rows)
    assert "example-child-value" not in repr(rows)
    assert hidden in native[-1].objects and other_hidden in native[-1].objects
    child = native[-1].identity
    rows = _graph_comparison(monkeypatch, "bgp", native, observed, visible={hidden.pk})
    assert all(row.identity == "" for row in rows)
    rows = _graph_comparison(monkeypatch, "bgp", native, observed, visible={hidden.pk, other_hidden.pk})
    assert any(row.identity == child for row in rows)


@pytest.mark.parametrize("scope", ["isis", "ospf", "bgp"])
def test_parent_links_from_both_sides_survive_different_bindings(monkeypatch, scope):
    hidden = _Dependency(1)
    if scope == "isis":
        child = _ISIS_INTERFACE
        visible_parent, hidden_parent = ("process", "CORE"), ("process", "OTHER")
        native_values, observed_values = {"process_tag": "CORE"}, {"process_tag": "OTHER"}
    elif scope == "ospf":
        child = ("interface", "Ethernet1", "10")
        visible_parent = (*_INSTANCE, "area", "0.0.0.0")
        hidden_parent = (*_INSTANCE, "area", "0.0.0.1")
        native_values, observed_values = {"area_id": "0.0.0.0"}, {"area_id": "0.0.0.1"}
    else:
        child = _PEER
        visible_parent, hidden_parent = _GROUP, (*_SCOPE, "peer_group", "OTHER")
        native_values, observed_values = {"peer_group": "EXAMPLE"}, {"peer_group": "OTHER"}
    native = [
        ProjectedEntry(visible_parent, {}, coverage_only=True),
        _child_entry(scope, child, {**native_values, "description": "example-native-value"}),
    ]
    observed = [
        ProjectedEntry(hidden_parent, {}, (hidden,), coverage_only=True),
        _child_entry(scope, child, {**observed_values, "description": "example-observed-value"}),
    ]
    rows = _graph_comparison(monkeypatch, scope, native, observed)
    assert rows and all(row.kind == "ambiguous" and row.identity == "" for row in rows)
    assert "example-native-value" not in repr(rows)
    assert "example-observed-value" not in repr(rows)
    assert hidden in native[-1].objects and hidden in observed[-1].objects


def test_redistribution_router_and_scope_dependencies_remain_separate(monkeypatch):
    hidden = _Dependency(1)
    hidden_child = ("bgp", "64512/PRIVATE/ipv4-unicast", None, "static", "")
    visible_children = [
        ("bgp", "64512/PUBLIC/ipv4-unicast", None, "static", ""),
        ("bgp", "64513/PRIVATE/ipv4-unicast", None, "static", ""),
    ]
    observed = [
        ProjectedEntry(("bgp",), {"router_asn": "64512"}, coverage_only=True),
        ProjectedEntry(("bgp",), {"router_asn": "64512", "vrf": "PRIVATE"}, (hidden,), coverage_only=True),
        ProjectedEntry(("bgp",), {"router_asn": "64512", "vrf": "PUBLIC"}, coverage_only=True),
        ProjectedEntry(("bgp",), {"router_asn": "64513"}, coverage_only=True),
        ProjectedEntry(hidden_child, {"description": "example-hidden-value"}),
        *(ProjectedEntry(identity, {"description": "example-visible-value"}) for identity in visible_children),
    ]
    rows = _graph_comparison(monkeypatch, "redistribution", [], observed)
    assert any(row.kind == "ambiguous" and row.identity == "" for row in rows)
    assert "example-hidden-value" not in repr(rows)
    assert {row.identity for row in rows if row.kind == "device_only"} == set(visible_children)
    assert "example-visible-value" in repr(rows)


def test_independent_peer_af_does_not_inherit_group_af_dependencies(monkeypatch):
    hidden = _Dependency(1)
    child = (*_PEER, "peer_address_family", "ipv4-unicast")
    observed = [
        ProjectedEntry(_GROUP, {}, coverage_only=True),
        ProjectedEntry((*_GROUP, "peer_group_address_family", "ipv4-unicast"), {}, (hidden,), coverage_only=True),
        ProjectedEntry(_PEER, {"peer_group": "EXAMPLE"}, coverage_only=True),
        ProjectedEntry(child, {"description": "example-visible-value"}),
    ]
    rows = _graph_comparison(monkeypatch, "bgp", [], observed)
    assert any(row.identity == child and row.kind == "device_only" for row in rows)
    assert "example-visible-value" in repr(rows)


@pytest.mark.parametrize("collection,child_name", [("peer", "198.18.0.2"), ("peer_group", "EXAMPLE")])
def test_bgp_same_router_children_keep_vrf_dependencies_separate(monkeypatch, collection, child_name):
    hidden = _Dependency(1)
    public = (*_ROUTER, "scope", "PUBLIC")
    private = (*_ROUTER, "scope", "PRIVATE")
    visible_child, hidden_child = (*public, collection, child_name), (*private, collection, child_name)
    observed = [
        ProjectedEntry(_ROUTER, {}, coverage_only=True),
        ProjectedEntry(public, {}, coverage_only=True),
        ProjectedEntry(private, {}, (hidden,), coverage_only=True),
        ProjectedEntry(visible_child, {"description": "example-visible-value"}),
        ProjectedEntry(hidden_child, {"description": "example-hidden-value"}),
    ]
    rows = _graph_comparison(monkeypatch, "bgp", [], observed)
    assert {row.identity for row in rows if row.kind == "device_only"} == {visible_child}
    assert any(row.kind == "ambiguous" and row.identity == "" for row in rows)
    assert "example-hidden-value" not in repr(rows)
    assert "example-visible-value" in repr(rows)
