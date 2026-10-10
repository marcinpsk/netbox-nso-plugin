# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Routing observations with the adapter's canonical read fixture shapes."""

import copy
import hashlib
import json

from ._scope_observation_case import entry

DOCUMENTS = {
    "bgp": {
        "routers": [
            entry(
                asn="64512",
                router_id="198.18.0.1",
                scope=[
                    entry(
                        vrf="",
                        address_family=[entry(afi="ipv4-unicast")],
                        peer=[
                            entry(
                                peer_address="198.18.0.2",
                                remote_as="64513",
                                enabled=True,
                                password_present=True,
                                source="198.18.0.1",
                                description="example-peer",
                                peer_address_family=[
                                    entry(afi="ipv4-unicast", enabled=True, routemap_in="IMPORT", routemap_out="EXPORT")
                                ],
                            )
                        ],
                        peer_group=[entry(name="EXAMPLE", remote_as="64513")],
                    )
                ],
            )
        ],
    },
    "isis": {
        "processes": [
            entry(
                process_tag="0",
                net="49.0001.0000.0000.0001.00",
                is_type="level-2",
                area_auth_key_present=True,
                flex_algo=[entry(algo_id=128, metric_type="igp", priority=100)],
                level=[entry(level=2, default_metric=10)],
            )
        ],
        "interfaces": [
            entry(
                interface_name="Ethernet1", af="ipv4", process_tag="0", circuit_type="level-2", metric=10, passive=False
            )
        ],
    },
    "ospf": {
        "instances": [
            entry(
                process_id="1",
                vrf="",
                router_id="198.18.0.1",
                enabled=True,
                area=[entry(area_id="0", area_type="normal")],
            )
        ],
        "interfaces": [
            entry(
                interface_name="Ethernet1", process_id="1", area_id="0", passive=False, cost=10, auth_key_present=True
            )
        ],
    },
    "route_policy": {
        "prefix_lists": [
            entry(
                name="EXAMPLE",
                family=4,
                entry=[entry(sequence=10, action="permit", prefix="198.18.0.0/24", ge=None, le=32)],
            )
        ],
        "community_lists": [
            entry(
                name="SCRUBBER", invert_match=True, entry=[entry(sequence=10, action="permit", community="no-export")]
            )
        ],
        "as_paths": [entry(name="EXAMPLE", entry=[entry(sequence=10, action="permit", pattern="^64512$")])],
        "route_maps": [
            entry(
                name="IMPORT",
                entry=[
                    entry(
                        sequence=10,
                        action="permit",
                        match_prefix_lists=["EXAMPLE"],
                        match_community_lists=[],
                        match_as_paths=[],
                        match_json="",
                        set_json="",
                    )
                ],
            )
        ],
    },
    "redistribution": {
        "entries": [
            entry(
                dest_protocol="ospf",
                dest_ref="1",
                dest_vrf="",
                source_protocol="bgp",
                source_ref="64512",
                route_map="IMPORT",
                metric=10,
                metric_type="type-2",
            )
        ],
        "components": [
            {
                "protocol": "ospf",
                "present": ["inventory"],
                "inventory": [
                    entry(
                        process_id="1",
                        vrf="",
                        redistribute=[
                            entry(
                                source_protocol="bgp",
                                source_ref="64512",
                                route_map="IMPORT",
                                metric=10,
                                metric_type="type-2",
                            )
                        ],
                    )
                ],
            }
        ],
    },
    "lag": {
        "bundles": [entry(name="Port-channel1", lag_id=1, member=[entry(interface_name="Ethernet1", mode="active")])]
    },
}

for family, document in DOCUMENTS.items():
    if family != "redistribution":
        document["present"] = sorted(document)
    document["unprojectable"] = []

COVERAGE = {
    "bgp": [
        "address_family",
        "afi",
        "asn",
        "bfd_enabled",
        "description",
        "enabled",
        "local_as",
        "name",
        "password_present",
        "peer",
        "peer_address",
        "peer_address_family",
        "peer_group",
        "peer_group_address_family",
        "prefixlist_in",
        "prefixlist_out",
        "remote_as",
        "routemap_in",
        "routemap_out",
        "router_id",
        "scope",
        "source",
        "ttl",
        "vrf",
    ],
    "isis": [
        "admin_group_exclude",
        "admin_group_include_all",
        "admin_group_include_any",
        "af",
        "algo_id",
        "algorithm",
        "area_auth_key_present",
        "area_auth_present",
        "area_auth_type",
        "argument_length",
        "auth_key_present",
        "auth_present",
        "auth_type",
        "bfd_enabled",
        "block_length",
        "bound_port",
        "circuit_type",
        "csnp_interval",
        "default_metric",
        "disabled",
        "distance",
        "domain_auth_key_present",
        "domain_auth_present",
        "domain_auth_type",
        "enabled",
        "explicit_null",
        "fast_reroute",
        "flavor",
        "flex_algo",
        "frr_enabled",
        "frr_protection",
        "function_length",
        "hello_auth_key_present",
        "hello_auth_present",
        "hello_auth_type",
        "hello_interval",
        "hello_multiplier",
        "ignore_attached_bit",
        "interface_name",
        "is_anycast",
        "is_micro_segment",
        "is_type",
        "isis_level",
        "key",
        "labeled_preference",
        "level",
        "lsp_initial_wait",
        "lsp_interval",
        "lsp_lifetime",
        "lsp_max_wait",
        "lsp_mtu",
        "lsp_refresh_interval",
        "maximum_paths",
        "maximum_sid_depth",
        "mesh_group",
        "metric",
        "metric_style",
        "metric_type",
        "microloop_avoidance",
        "n_flag",
        "name",
        "net",
        "network_type",
        "no_php",
        "node_length",
        "node_sid_index",
        "node_sid_label",
        "node_sid_v6_index",
        "node_sid_v6_label",
        "overload_bit",
        "overload_on_startup",
        "overload_timeout",
        "passive",
        "preference",
        "prefix",
        "prefix_sid",
        "prefix_sid_range",
        "priority",
        "process_tag",
        "readvertise",
        "reference_bandwidth",
        "retransmit_interval",
        "segment_routing",
        "segment_routing_configured",
        "segment_routing_reported",
        "setting",
        "sid_index",
        "sid_label",
        "spf_initial_wait",
        "spf_max_wait",
        "srgb_range",
        "srgb_start",
        "srlb_range",
        "srlb_start",
        "srv6_enabled",
        "srv6_locator",
        "suppress_attached_bit",
        "te_enabled",
        "tunnel_table_pref",
        "value",
        "wide_metrics_only",
    ],
    "lag": ["name", "lag_id", "member", "member.interface_name", "member.mode"],
    "ospf": [
        "area",
        "area_id",
        "area_type",
        "auth_key_present",
        "auth_present",
        "auth_type",
        "bfd_enabled",
        "cost",
        "enabled",
        "interface_name",
        "network_type",
        "passive",
        "priority",
        "process_id",
        "router_id",
        "vrf",
    ],
    "redistribution": [
        "dest_protocol",
        "dest_ref",
        "metric",
        "metric_type",
        "route_map",
        "source_protocol",
        "source_ref",
    ],
    "route_policy": [
        "action",
        "community",
        "entry",
        "family",
        "ge",
        "invert_match",
        "le",
        "match_as_paths",
        "match_community_lists",
        "match_json",
        "match_prefix_lists",
        "name",
        "pattern",
        "prefix",
        "sequence",
        "set_json",
    ],
}


def route_policy_document(payload):
    """The canonical route-policy document for the policies a route-policy read payload carries."""
    keys = ["as_paths", "community_lists", "prefix_lists", "route_maps"]
    document = {"present": keys, "unprojectable": []}
    for key in keys:
        # The adapter always returns all four lists, so a list the test payload omits is empty.
        document[key] = [
            entry(
                **{name: value for name, value in policy.items() if name != "entries"},
                entry=[entry(**values) for values in policy["entries"]],
            )
            for policy in payload.get(key, [])
        ]
    return document


def routing_observation(family, *, document=None, coverage=None, revision=1, source_epoch=1):
    document = copy.deepcopy(DOCUMENTS[family] if document is None else document)
    if coverage is None:
        coverage = {"attributes": list(COVERAGE[family])}
        credentials = {
            "bgp": ["password"],
            "isis": ["area_auth_key", "domain_auth_key", "hello_auth_key", "level.auth_key"],
            "ospf": ["auth_key"],
        }
        coverage["not_comparable"] = credentials.get(family, [])
        if family == "redistribution":
            coverage["components"] = [
                {"protocol": "ospf", "destinations": ["1"], "sources": [{"protocol": "bgp", "reference": "64512"}]}
            ]
    return {
        "family": family,
        "revision": revision,
        "source_epoch": source_epoch,
        "digest": hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "observed_at": "2026-10-01T12:00:00+00:00",
        "coverage": copy.deepcopy(coverage),
        "document": document,
    }
