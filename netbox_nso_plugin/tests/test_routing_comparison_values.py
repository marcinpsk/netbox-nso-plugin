# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Fail closed when routing observations do not cover the requested inventory."""

import copy
from types import SimpleNamespace

import pytest

from netbox_nso_plugin.device_differences import (
    NOT_VISIBLE,
    SCOPE_SPECS,
    _nested_component_gaps,
    _nested_entry_blocked,
    _routing_coverage_rows,
    differences,
)
from netbox_nso_plugin.difference_projection import ProjectedEntry

from ._routing_observation_case import DOCUMENTS, routing_observation


@pytest.mark.parametrize("scope", ["bgp", "isis", "isis_flex_algo", "ospf", "redistribution", "route_policy"])
def test_unobserved_routing_inventory_is_unavailable(scope):
    spec = SCOPE_SPECS[scope]
    observed = routing_observation(spec.family)
    document = observed["document"]
    if scope == "redistribution":
        document["components"] = []
        observed["coverage"]["components"] = []
    else:
        for name in document["present"]:
            document[name] = None
    snapshots = {spec.family: SimpleNamespace(document=document, coverage=observed["coverage"])}
    rows = [row for row in differences(SimpleNamespace(), user=None, snapshots=snapshots) if row.scope == scope]
    assert rows
    assert all(row.kind == "unavailable" for row in rows)


def test_unobserved_flex_algo_collection_blocks_native_absence():
    spec = SCOPE_SPECS["isis_flex_algo"]
    document = copy.deepcopy(DOCUMENTS["isis"])
    process = document["processes"][0]
    process["present"].remove("flex_algo")
    process.pop("flex_algo")
    gaps = _nested_component_gaps(spec, SimpleNamespace(document=document))
    native = ProjectedEntry(("process", "0", "flex_algo", 128), {"priority": 100})
    assert _nested_entry_blocked(spec, native, gaps)
    rows = _routing_coverage_rows(spec, gaps, [native], {id(native): True})
    assert [(row.kind, row.attribute) for row in rows] == [("unavailable", "flex_algo")]


def test_hidden_policy_parent_redacts_nested_coverage_identity():
    spec = SCOPE_SPECS["route_policy"]
    parent = ProjectedEntry(("prefix_lists", "HIDDEN-POLICY"), {"family": 4})
    gaps = {(("prefix_lists", "HIDDEN-POLICY", "entry"), "entry"): "entries were not observed"}
    rows = _routing_coverage_rows(spec, gaps, [parent], {id(parent): False})
    assert [(row.kind, row.identity, row.attribute, row.reason) for row in rows] == [("ambiguous", "", "", NOT_VISIBLE)]
    assert "HIDDEN-POLICY" not in repr(rows)
