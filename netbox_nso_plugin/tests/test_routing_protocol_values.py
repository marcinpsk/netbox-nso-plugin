# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Check routing identities and coverage without a database."""

from types import SimpleNamespace

import pytest

from netbox_nso_plugin.routing_protocol_projection import (
    _native_values,
    _redist_identity,
    component_gaps,
    nested_gaps,
)


def test_native_projection_never_copies_authentication_keys():
    native = SimpleNamespace(area_auth_key="example", auth_key="example", net="49.0001.0198.0180.0001.00")
    assert _native_values(native, ("area_auth_key", "auth_key", "net")) == {"net": native.net}


def test_omitted_ospf_interface_fields_keep_shared_normalizer_defaults():
    from netbox_nso_plugin.comparison_values import ProjectedEntry
    from netbox_nso_plugin.device_differences import SCOPE_SPECS, _projected_group_rows
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.routing_protocol_projection import _ospf_device

    from ._routing_observation_case import routing_observation
    from ._scope_observation_case import entry

    document = {
        "instances": [],
        "interfaces": [entry(interface_name="Ethernet1", process_id="10", area_id="0")],
        "present": ["instances", "interfaces"],
        "unprojectable": [],
    }
    validated = observation_defaults("ospf", 1, 1, routing_observation("ospf", document=document))
    device = _ospf_device(validated["document"])[0]
    native = ProjectedEntry(
        device.identity,
        {
            "area_id": "0.0.0.0",
            "passive": False,
            "cost": None,
            "priority": None,
            "network_type": None,
            "auth_type": None,
        },
    )
    rows = _projected_group_rows(SCOPE_SPECS["ospf"], [native], [device], SimpleNamespace(**validated), "")
    assert not [row for row in rows if row.kind == "mismatch"]


@pytest.mark.parametrize("flag", ["microloop_avoidance", "overload_bit"])
def test_isis_omitted_process_flags_follow_reconciler_comparison(flag):
    from netbox_nso_plugin.comparison_values import MISSING, ProjectedEntry
    from netbox_nso_plugin.device_differences import SCOPE_SPECS, _projected_group_rows
    from netbox_nso_plugin.routing_protocol_projection import _apply_isis_defaults, _isis_omitted_defaults

    management = SimpleNamespace(device=SimpleNamespace(platform_id=None))
    item = {"process_tag": "CORE", "present": ["process_tag"]}
    flags = ("microloop_avoidance", "overload_bit")
    native = ProjectedEntry(("process", "CORE"), {name: False for name in flags})
    device = ProjectedEntry(native.identity, {name: MISSING for name in flags})
    _apply_isis_defaults(device, item, _isis_omitted_defaults(management)["process"])
    snapshot = SimpleNamespace(coverage={"attributes": list(flags)})
    assert _projected_group_rows(SCOPE_SPECS["isis"], [native], [device], snapshot, "") == []
    native.values[flag] = True
    rows = _projected_group_rows(SCOPE_SPECS["isis"], [native], [device], snapshot, "")
    assert [(row.kind, row.attribute, row.device_value) for row in rows] == [("mismatch", flag, False)]


@pytest.mark.parametrize("flag", ["microloop_avoidance", "overload_bit"])
@pytest.mark.parametrize("native_value,device_value", [(False, None), (None, False), (None, True)])
def test_isis_process_flag_comparison_keeps_reconciler_unset_semantics(flag, native_value, device_value):
    from netbox_nso_plugin.comparison_values import ProjectedEntry
    from netbox_nso_plugin.device_differences import SCOPE_SPECS, _projected_group_rows

    native = ProjectedEntry(("process", "CORE"), {flag: native_value})
    device = ProjectedEntry(native.identity, {flag: device_value})
    snapshot = SimpleNamespace(coverage={"attributes": [flag]})
    assert _projected_group_rows(SCOPE_SPECS["isis"], [native], [device], snapshot, "") == []


@pytest.mark.parametrize("reference", ["4200000010", "64086.59914"])
def test_redistribution_bgp_reference_spellings_share_uint32_identity(reference):
    item = {
        "dest_protocol": "bgp",
        "dest_ref": f"{reference}//ipv4-unicast",
        "source_protocol": "bgp",
        "source_ref": reference,
    }
    assert _redist_identity(item) == ("bgp", "4200000010//ipv4-unicast", None, "bgp", "4200000010")


@pytest.mark.parametrize(
    "reference", ["", "-1", "65536.1", "1.65536", "1.2.3", "+64512", " 64512", "064512", "64512/extra"]
)
def test_redistribution_bgp_source_requires_a_strict_asn(reference):
    with pytest.raises(ValueError):
        _redist_identity(
            {"dest_protocol": "isis", "dest_ref": "CORE", "source_protocol": "bgp", "source_ref": reference}
        )


@pytest.mark.parametrize("null", [False, True])
def test_flex_algo_unknown_collection_is_not_authoritative_empty(null):
    process = {"process_tag": "CORE", "present": ["process_tag"]}
    if null:
        process.update(flex_algo=None, present=["process_tag", "flex_algo"])
    snapshot = SimpleNamespace(document={"processes": [process], "interfaces": []})
    assert list(nested_gaps("isis_flex_algo", snapshot)) == [(("process", "CORE", "flex_algo"), "")]


def test_flex_algo_authoritative_empty_collection_has_no_gap():
    snapshot = SimpleNamespace(
        document={"processes": [{"process_tag": "CORE", "flex_algo": [], "present": ["process_tag", "flex_algo"]}]}
    )
    assert nested_gaps("isis_flex_algo", snapshot) == {}


def test_redistribution_coverage_is_per_component():
    snapshot = SimpleNamespace(
        document={"components": [{"protocol": "ospf", "inventory": [], "present": ["inventory"]}]},
        coverage={"components": [{"protocol": "ospf", "destinations": [], "sources": []}]},
    )
    assert set(component_gaps(snapshot)) == {"bgp", "isis"}


@pytest.mark.parametrize("null", [False, True])
def test_redistribution_inventory_presence_is_required_for_authority(null):
    component = {"protocol": "ospf", "inventory": [], "present": []}
    if null:
        component.update(inventory=None, present=["inventory"])
    snapshot = SimpleNamespace(document={"components": [component]}, coverage={"components": [{"protocol": "ospf"}]})
    assert set(component_gaps(snapshot)) == {"bgp", "isis", "ospf"}


def test_redistribution_uncovered_inventory_cannot_emit_named_nested_gap():
    snapshot = SimpleNamespace(
        document={
            "components": [
                {
                    "protocol": "isis",
                    "inventory": [{"process_tag": "example-hidden", "present": ["process_tag"]}],
                    "present": ["inventory"],
                }
            ]
        },
        coverage={"components": []},
    )
    assert nested_gaps("redistribution", snapshot) == {}


def test_redistribution_missing_sources_block_only_the_destination():
    snapshot = SimpleNamespace(
        document={
            "components": [
                {
                    "protocol": "ospf",
                    "inventory": [{"process_id": "10", "vrf": "", "present": ["process_id", "vrf"]}],
                    "present": ["inventory"],
                }
            ]
        },
        coverage={"components": [{"protocol": "ospf"}]},
    )
    assert list(nested_gaps("redistribution", snapshot)) == [(("ospf", "10", ""), "redistribute")]


def test_segment_routing_explicit_absence_is_authoritative():
    from netbox_nso_plugin.routing_protocol_projection import _collection_gap

    process = {
        "segment_routing": None,
        "segment_routing_reported": True,
        "segment_routing_configured": False,
        "present": ["segment_routing", "segment_routing_reported", "segment_routing_configured"],
    }
    assert _collection_gap(process, "segment_routing") == ""


def test_segment_routing_unobserved_presence_metadata_cannot_assert_absence():
    from netbox_nso_plugin.routing_protocol_projection import _collection_gap

    process = {
        "segment_routing": None,
        "segment_routing_reported": True,
        "segment_routing_configured": False,
        "present": ["segment_routing"],
    }
    assert _collection_gap(process, "segment_routing") == "segment_routing collection is not comparable"


@pytest.mark.parametrize("asn", [" 64512", "invalid-asn", "65536.1", "064512", "64512/extra"])
def test_schema_valid_invalid_redistribution_inventory_fails_closed(asn):
    from netbox_nso_plugin.device_differences import differences
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.routing_protocol_projection import _redist_coverage_entries

    from ._routing_observation_case import routing_observation

    document = {
        "entries": [],
        "components": [
            {
                "protocol": "bgp",
                "inventory": [{"asn": asn, "scope": [], "present": ["asn", "scope"]}],
                "present": ["inventory"],
            }
        ],
        "unprojectable": [],
    }
    coverage = {"attributes": [], "components": [{"protocol": "bgp", "destinations": [], "sources": []}]}
    validated = observation_defaults(
        "redistribution", 1, 1, routing_observation("redistribution", document=document, coverage=coverage)
    )
    snapshot = SimpleNamespace(document=validated["document"], coverage=validated["coverage"])
    assert _redist_coverage_entries(snapshot.document) == []
    assert nested_gaps("redistribution", snapshot) == {}
    assert component_gaps(snapshot)["bgp"] == "redistribution inventory has an invalid AS number"
    rows = [
        row
        for row in differences(SimpleNamespace(), user=None, snapshots={"redistribution": snapshot})
        if row.scope == "redistribution"
    ]
    assert all(row.kind == "unavailable" for row in rows)
    assert asn not in repr(rows)


def test_schema_valid_anonymous_isis_process_uses_empty_tag():
    from netbox_nso_plugin.observations import observation_defaults

    from ._routing_observation_case import routing_observation

    document = {
        "processes": [{"present": []}],
        "interfaces": [],
        "present": ["processes", "interfaces"],
        "unprojectable": [],
    }
    validated = observation_defaults("isis", 1, 1, routing_observation("isis", document=document))
    snapshot = SimpleNamespace(document=validated["document"])
    assert list(nested_gaps("isis_flex_algo", snapshot)) == [(("process", "", "flex_algo"), "")]


def test_schema_valid_redistribution_component_without_protocol_fails_closed():
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.routing_protocol_projection import _redist_coverage_entries

    from ._routing_observation_case import routing_observation

    document = {"entries": [], "components": [{"inventory": [], "present": ["inventory"]}], "unprojectable": []}
    coverage = {"attributes": [], "components": [{"protocol": "bgp", "destinations": [], "sources": []}]}
    validated = observation_defaults(
        "redistribution", 1, 1, routing_observation("redistribution", document=document, coverage=coverage)
    )
    snapshot = SimpleNamespace(document=validated["document"], coverage=validated["coverage"])
    assert set(component_gaps(snapshot)) == {"bgp", "isis", "ospf"}
    assert _redist_coverage_entries(snapshot.document) == []
    assert nested_gaps("redistribution", snapshot) == {}
