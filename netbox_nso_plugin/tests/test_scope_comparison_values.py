# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Pure comparison checks for scope availability and typed observation values."""

from types import SimpleNamespace

import pytest

from netbox_nso_plugin.device_differences import MISSING, SCOPE_SPECS, differences
from netbox_nso_plugin.difference_projection import observed_value

B1_FAMILIES = {
    "lacp": "lag_config",
    "vlan": "vlan",
    "switchport": "switchport",
    "interface_mtu": "interface_mtu",
    "svi": "svi",
    "subinterface": "subinterface",
    "bfd": "bfd",
    "l2_sap": "l2_service",
    "logging": "logging",
    "snmp": "snmp",
    "static_route": "static_route",
}

B2_FAMILIES = {
    "bgp": "bgp",
    "isis": "isis",
    "isis_flex_algo": "isis",
    "ospf": "ospf",
    "redistribution": "redistribution",
    "route_policy": "route_policy",
}


@pytest.mark.parametrize("scope,family", (B1_FAMILIES | B2_FAMILIES).items())
def test_supported_scope_without_snapshot_reports_no_successful_read(scope, family):
    rows = differences(SimpleNamespace(), user=None, snapshots={})
    row = next(row for row in rows if row.scope == scope)
    assert (row.kind, row.reason) == ("unavailable", "no successful read yet")
    assert SCOPE_SPECS[scope].family == family


def test_every_converted_scope_has_a_comparison_spec():
    from netbox_nso_plugin.ownership_planner import converted_scope_rules

    assert set(SCOPE_SPECS) == set(converted_scope_rules())


@pytest.mark.parametrize("value", [None, "", False, 0, [], "example"])
def test_observed_values_keep_their_original_type_and_value(value):
    actual = observed_value({"present": ["value"], "value": value}, "value")
    assert type(actual) is type(value)
    assert actual == value


def test_default_value_does_not_claim_an_omitted_field_was_observed():
    assert observed_value({"present": [], "value": None}, "value") is MISSING
    assert observed_value({"present": []}, "value") is MISSING


@pytest.mark.parametrize(
    "scope,family", [(scope, family) for scope, family in B1_FAMILIES.items() if scope not in {"logging", "snmp"}]
)
@pytest.mark.parametrize("null", [True, False])
def test_unknown_component_cannot_claim_native_absence(scope, family, null):
    import copy

    from ._scope_observation_case import DOCUMENTS, scope_observation

    document = copy.deepcopy(DOCUMENTS[family])
    collection = SCOPE_SPECS[scope].components[0]
    document[collection] = None if null else []
    if not null:
        document["present"] = []
    snapshots = {family: SimpleNamespace(document=document, coverage=scope_observation(family)["coverage"])}
    rows = [row for row in differences(SimpleNamespace(), user=None, snapshots=snapshots) if row.scope == scope]
    assert [(row.kind, row.attribute) for row in rows] == [("unavailable", collection)]


@pytest.mark.parametrize(
    "scope,port,ned_id",
    [
        ("logging", 514, "timos-test"),
        ("logging", 514, "arcos-test"),
        ("snmp", 162, "timos-test"),
        ("snmp", 162, "arcos-test"),
        ("snmp", 162, "cisco-ios-cli-test"),
        ("snmp", 162, "cisco-iosxe-cli-test"),
    ],
)
def test_omitted_default_service_port_matches_existing_ned_comparator(scope, port, ned_id):
    from netbox_nso_plugin.device_differences import _projected_matches

    spec = SCOPE_SPECS[scope]
    projection = next(item for item in spec.attributes if item.name == "port")
    assert _projected_matches(spec, projection, port, MISSING, ned_id)
    assert not _projected_matches(spec, projection, port + 1, MISSING, ned_id)


@pytest.mark.parametrize("scope,port", [("logging", 514), ("snmp", 162)])
def test_omission_keeps_missing_semantics_outside_observed_ned_exception(scope, port):
    from netbox_nso_plugin.device_differences import _projected_matches

    spec = SCOPE_SPECS[scope]
    projection = next(item for item in spec.attributes if item.name == "port")
    assert not _projected_matches(spec, projection, port, MISSING, "juniper-junos-test")
    assert not _projected_matches(spec, projection, None, MISSING, "juniper-junos-test")


def test_lacp_config_is_the_only_required_observation():
    from ._scope_observation_case import scope_observation

    snapshots = {
        "lag_config": SimpleNamespace(
            document={"present": ["bundles"], "bundles": None, "unprojectable": []},
            coverage=scope_observation("lag_config")["coverage"],
        )
    }
    rows = [row for row in differences(SimpleNamespace(), user=None, snapshots=snapshots) if row.scope == "lacp"]
    assert [(row.kind, row.attribute, row.reason) for row in rows] == [
        ("unavailable", "bundles", "device component is null"),
    ]
