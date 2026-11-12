# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Guard direct overlay selectors in named render functions, without alias or helper analysis."""

import ast
from pathlib import Path

import pytest

from netbox_nso_plugin.delivery import delivery_keys
from netbox_nso_plugin.ownership_planner import _NATIVE_BINDING_BUILDERS
from netbox_nso_plugin.refusal_registry import OVERLAYS

CONVERTED_KEYS = frozenset(
    {
        "bgp",
        "interface",
        "interface_mtu",
        "ip",
        "isis",
        "isis_flex_algo",
        "lacp",
        "ospf",
        "static_route",
        "switchport",
        "vlan",
        "route_policy",
    }
)
SELECTORS = {delivery_keys()[key].push_name for key in CONVERTED_KEYS} | {
    "_bgp_owned_router_entries",
    "_bgp_redistribution_members",
    "_collect_redistribution_by_dest_ref",
}
OVERLAY_CLASSES = {spec.model.__name__ for spec in OVERLAYS.values()}


def unguarded_selectors(source, functions=SELECTORS):
    tree = ast.parse(source)
    failures = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name not in functions:
            continue
        parents = {child: node for node in ast.walk(function) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Attribute)
                and node.attr == "objects"
                and isinstance(node.value, ast.Name)
                and node.value.id in OVERLAY_CLASSES
            ):
                continue
            ancestor = node
            while ancestor in parents:
                ancestor = parents[ancestor]
                if (
                    isinstance(ancestor, ast.Call)
                    and isinstance(ancestor.func, ast.Name)
                    and ancestor.func.id == "render_rows"
                ):
                    break
            else:
                failures.append((function.name, node.lineno))
    return failures


def test_converted_renderers_select_overlays_through_candidate_selection():
    assert set(_NATIVE_BINDING_BUILDERS) | {"route_policy"} == CONVERTED_KEYS
    source = Path(__file__).parents[1].joinpath("signals.py").read_text(encoding="utf-8")
    names = {node.name for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    assert names >= SELECTORS
    assert not unguarded_selectors(source)


@pytest.mark.parametrize(
    "selection",
    (
        "NSOInterfaceMtuState.objects.filter(status__in=OWNED_STATES)",
        "list(NSOInterfaceMtuState.objects.filter(status__in=OWNED_STATES).select_related('interface'))",
        "NSOLACPMemberState.objects.filter(management__device_id=device_id).order_by('pk')",
    ),
)
def test_selector_guard_catches_direct_owned_and_nested_queries(selection):
    assert unguarded_selectors(f"def renderer():\n    rows = {selection}\n", functions={"renderer"})


def test_selector_guard_accepts_candidate_selection_and_native_queries():
    source = """def renderer():
    rows = render_rows(NSOInterfaceMtuState.objects.filter(management__device_id=device_id), scope='interface_mtu')
    native = BGPPeerAddressFamily.objects.select_related('address_family')
    manifests = NSOOwnershipManifest.objects.filter(ownership_state='owned')
"""
    assert not unguarded_selectors(source, functions={"renderer"})
