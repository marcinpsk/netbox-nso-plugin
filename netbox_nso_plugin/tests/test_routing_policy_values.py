# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Pure routing identities, nested coverage, and semantic policy values."""

import copy
from types import SimpleNamespace

import pytest

from netbox_nso_plugin.routing_policy_projection import _policy_entry_values, nested_gaps, parse_comparison_asn

from ._routing_observation_case import DOCUMENTS
from ._scope_observation_case import entry


@pytest.mark.parametrize("collection", ["peer_address_family", "peer_group_address_family"])
@pytest.mark.parametrize("enabled", [None, True, False])
def test_bgp_af_omissions_match_reconciler_defaults_through_comparison(collection, enabled):
    from netbox_nso_plugin.bgp_reconciler import _af_device_content
    from netbox_nso_plugin.comparison_values import ProjectedEntry
    from netbox_nso_plugin.device_differences import SCOPE_SPECS, _projected_group_rows
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.routing_policy_projection import _device_bgp

    from ._routing_observation_case import routing_observation

    af = entry(afi="ipv4-unicast", **({} if enabled is None else {"enabled": enabled}))
    document = copy.deepcopy(DOCUMENTS["bgp"])
    scope = document["routers"][0]["scope"][0]
    owner = scope["peer" if collection == "peer_address_family" else "peer_group"][0]
    owner[collection] = [af]
    if collection not in owner["present"]:
        owner["present"].append(collection)
    validated = observation_defaults("bgp", 1, 1, routing_observation("bgp", document=document))
    device = next(row for row in _device_bgp(validated["document"], {}, {}, {}) if row.identity[-2] == collection)
    reconciled = _af_device_content(
        [{"af": af["afi"], **({} if enabled is None else {"enabled": enabled})}],
        route_maps_by_name={},
        prefix_lists_by_name={},
    )[0]
    assert reconciled == {
        "af": "ipv4-unicast",
        "enabled": True if enabled is None else enabled,
        "routemap_in": None,
        "routemap_out": None,
        "prefixlist_in": None,
        "prefixlist_out": None,
    }
    native = ProjectedEntry(device.identity, {name: value for name, value in reconciled.items() if name != "af"})
    assert _projected_group_rows(SCOPE_SPECS["bgp"], [native], [device], SimpleNamespace(**validated), "") == []


def test_community_delete_projection_keeps_the_resolved_visibility_dependency():
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.routing_policy_projection import _device_policy

    from ._routing_observation_case import routing_observation

    hidden = object()
    document = {
        "prefix_lists": [],
        "community_lists": [],
        "as_paths": [],
        "route_maps": [
            entry(
                name="IMPORT",
                entry=[entry(sequence=10, action="permit", set_json='{"community_delete":"HIDDEN-COMMUNITY"}')],
            )
        ],
        "present": ["prefix_lists", "community_lists", "as_paths", "route_maps"],
        "unprojectable": [],
    }
    validated = observation_defaults("route_policy", 1, 1, routing_observation("route_policy", document=document))
    rows = _device_policy(validated["document"], {"community_lists": {"HIDDEN-COMMUNITY": hidden}})
    projected = next(row for row in rows if row.identity == ("route_maps", "IMPORT", "entry", 1))
    assert "HIDDEN-COMMUNITY" in repr(projected.values["set_json"])
    assert hidden in projected.objects


@pytest.mark.parametrize("value,number", [("64512", 64512), ("0.64512", 64512), ("1.0", 65536), (0, 0), ("0.0", 0)])
def test_asplain_and_asdot_have_one_uint32_identity(value, number):
    assert parse_comparison_asn(value) == number


@pytest.mark.parametrize("value", [True, "064512", "1.00", "+64512", " 64512", "٦٤٥١٢", "65536.1", "4294967296"])
def test_non_rfc_asn_spellings_fail_closed(value):
    with pytest.raises(ValueError):
        parse_comparison_asn(value)


def test_policy_prefix_limits_use_the_shared_match_unit():
    implicit = _policy_entry_values("prefix_lists", {"action": "permit", "prefix": "198.18.0.0/24"})
    explicit = _policy_entry_values("prefix_lists", {"action": "permit", "prefix": "198.18.0.0/24", "ge": 24, "le": 24})
    assert implicit == explicit


def test_omitted_route_map_fields_keep_shared_canonical_defaults():
    from netbox_nso_plugin.comparison_values import ProjectedEntry
    from netbox_nso_plugin.device_differences import SCOPE_SPECS, _projected_group_rows
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.routing_policy_projection import _device_policy

    from ._routing_observation_case import routing_observation

    document = {
        "prefix_lists": [],
        "community_lists": [],
        "as_paths": [],
        "route_maps": [entry(name="IMPORT", entry=[entry(sequence=10, action="permit")])],
        "present": ["prefix_lists", "community_lists", "as_paths", "route_maps"],
        "unprojectable": [],
    }
    validated = observation_defaults("route_policy", 1, 1, routing_observation("route_policy", document=document))
    device = next(row for row in _device_policy(validated["document"], {}) if "entry" in row.identity)
    native = ProjectedEntry(
        device.identity,
        _policy_entry_values(
            "route_maps",
            {
                "action": "permit",
                "match_prefix_lists": [],
                "match_community_lists": [],
                "match_as_paths": [],
                "match_json": "",
                "set_json": "",
            },
            prefix_resolver={}.get,
        ),
    )
    assert (
        _projected_group_rows(SCOPE_SPECS["route_policy"], [native], [device], SimpleNamespace(**validated), "") == []
    )


@pytest.mark.parametrize("limits", [{}, {"ge": 26}, {"le": 28}, {"ge": None, "le": None}])
def test_omitted_prefix_limits_match_through_projection_and_comparison(limits):
    from netbox_nso_plugin.comparison_values import ProjectedEntry
    from netbox_nso_plugin.device_differences import SCOPE_SPECS, _projected_group_rows
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.routing_policy_projection import _device_policy

    from ._routing_observation_case import routing_observation

    item = entry(sequence=10, action="permit", prefix="198.18.0.0/24", **limits)
    document = {
        "prefix_lists": [entry(name="EXAMPLE", family=4, entry=[item])],
        "community_lists": [],
        "as_paths": [],
        "route_maps": [],
        "present": ["prefix_lists", "community_lists", "as_paths", "route_maps"],
        "unprojectable": [],
    }
    validated = observation_defaults("route_policy", 1, 1, routing_observation("route_policy", document=document))
    device = next(row for row in _device_policy(validated["document"], {}) if "entry" in row.identity)
    native = ProjectedEntry(
        device.identity,
        {"action": "permit", "prefix": "198.18.0.0/24", "ge": limits.get("ge") or 24, "le": limits.get("le") or 24},
    )
    rows = _projected_group_rows(SCOPE_SPECS["route_policy"], [native], [device], SimpleNamespace(**validated), "")
    assert rows == []


def test_policy_family_and_knob_spellings_use_shared_semantics():
    base = {"action": "permit", "match_json": '{"family":"inet","protocol":"bgp"}', "set_json": ""}
    alternate = {**base, "match_json": '{"family":["ipv4"],"protocol":["bgp"]}'}
    assert _policy_entry_values("route_maps", base) == _policy_entry_values("route_maps", alternate)


@pytest.mark.parametrize("blob", ["[]", '"invalid"', "{invalid"])
def test_invalid_policy_json_fails_closed(blob):
    with pytest.raises(ValueError):
        _policy_entry_values("route_maps", {"action": "permit", "match_json": blob})


def test_policy_reference_names_keep_their_case():
    base = {"action": "permit", "match_prefix_lists": ["Example"], "match_json": "", "set_json": ""}
    alternate = {**base, "match_prefix_lists": ["EXAMPLE"]}
    assert _policy_entry_values("route_maps", base) != _policy_entry_values("route_maps", alternate)


@pytest.mark.parametrize("null", [True, False])
def test_missing_bgp_collection_blocks_all_peer_descendants(null):
    document = copy.deepcopy(DOCUMENTS["bgp"])
    scope = document["routers"][0]["scope"][0]
    scope["peer"] = None if null else []
    if not null:
        scope["present"].remove("peer")
    gaps = nested_gaps("bgp", SimpleNamespace(document=document))
    assert (("router", 64512, "scope", "", "peer"), "peer") in gaps


@pytest.mark.parametrize("null", [True, False])
def test_missing_policy_entries_keep_exact_parent_names(null):
    document = copy.deepcopy(DOCUMENTS["route_policy"])
    policy = document["route_maps"][0]
    policy["entry"] = None if null else []
    if not null:
        policy["present"].remove("entry")
    gaps = nested_gaps("route_policy", SimpleNamespace(document=document))
    assert (("route_maps", "IMPORT", "entry"), "entry") in gaps


@pytest.mark.parametrize("field,value", [("peer_address", "not-an-address"), ("remote_as", "064512")])
def test_invalid_schema_valid_peer_values_are_ambiguous(field, value):
    from netbox_nso_plugin.routing_policy_projection import _device_bgp

    document = copy.deepcopy(DOCUMENTS["bgp"])
    document["routers"][0]["scope"][0]["peer"][0][field] = value
    rows = _device_bgp(document, {}, {}, {})
    assert any("invalid device BGP peer" in row.reason for row in rows)


def test_named_prefix_and_inline_route_filter_use_the_same_units():
    named = {"action": "permit", "match_prefix_lists": ["EXAMPLE"], "match_json": "", "set_json": ""}
    inline = {
        "action": "permit",
        "match_prefix_lists": [],
        "match_json": '{"_junos_route_filter":[{"prefix":"198.18.0.0/24","match":"exact"}]}',
        "set_json": "",
    }
    units = {"EXAMPLE": (("permit", "198.18.0.0/24", 24, 24),)}
    assert _policy_entry_values("route_maps", named, prefix_resolver=units.__getitem__) == _policy_entry_values(
        "route_maps", inline, prefix_resolver=units.__getitem__
    )


def test_missing_observed_prefix_content_is_unavailable():
    from netbox_nso_plugin.routing_policy_projection import _policy_comparison_values

    values, unavailable = _policy_comparison_values(
        "route_maps", {"action": "permit", "match_prefix_lists": ["EXAMPLE"], "match_json": "", "set_json": ""}, {}
    )
    assert set(unavailable) == {"match_prefix_lists", "match_json"}
    assert "match_prefix_lists" not in values
    assert "match_json" not in values


def test_bgp_address_references_keep_the_observed_scope_vrf():
    from netbox_nso_plugin.routing_policy_projection import _bgp_reference_names

    document = copy.deepcopy(DOCUMENTS["bgp"])
    document["routers"][0]["scope"][0]["vrf"] = "EXAMPLE"
    _asns, addresses, _interfaces, _vrfs, _groups = _bgp_reference_names(document)
    assert ("EXAMPLE", "198.18.0.2") in addresses
    assert ("EXAMPLE", "198.18.0.1") in addresses


def test_named_junos_prefix_filter_uses_the_same_cached_units():
    filtered = {"action": "permit", "match_json": '{"_junos_prefix_list_filter":[{"list":"EXAMPLE"}]}', "set_json": ""}
    named = {"action": "permit", "match_prefix_lists": ["EXAMPLE"], "match_json": "", "set_json": ""}
    units = {"EXAMPLE": (("permit", "198.18.0.0/24", 24, 24),)}
    assert _policy_entry_values("route_maps", filtered, prefix_resolver=units.__getitem__) == _policy_entry_values(
        "route_maps", named, prefix_resolver=units.__getitem__
    )


def test_named_junos_filter_without_observed_content_is_unavailable():
    from netbox_nso_plugin.routing_policy_projection import _policy_comparison_values

    values, unavailable = _policy_comparison_values(
        "route_maps", {"action": "permit", "match_json": '{"_junos_prefix_list_filter":[{"list":"EXAMPLE"}]}'}, {}
    )
    assert set(unavailable) == {"match_prefix_lists", "match_json"}
    assert "match_json" not in values


def test_each_bgp_scope_starts_at_its_router():
    from netbox_nso_plugin.routing_policy_projection import _device_bgp

    document = copy.deepcopy(DOCUMENTS["bgp"])
    second = copy.deepcopy(document["routers"][0]["scope"][0])
    second["vrf"] = "EXAMPLE"
    document["routers"][0]["scope"].append(second)
    rows = _device_bgp(document, {}, {}, {})
    assert ("router", 64512, "scope", "EXAMPLE") in [row.identity for row in rows]


def test_omitted_bgp_scope_vrf_uses_the_schema_global_default():
    from netbox_nso_plugin.routing_policy_projection import _bgp_reference_names, _device_bgp

    document = copy.deepcopy(DOCUMENTS["bgp"])
    scope = document["routers"][0]["scope"][0]
    del scope["vrf"]
    scope["present"].remove("vrf")
    rows = _device_bgp(document, {}, {}, {})
    assert ("router", 64512, "scope", "") in [row.identity for row in rows]
    assert ("", "198.18.0.2") in _bgp_reference_names(document)[1]
    nested_gaps("bgp", SimpleNamespace(document=document))
