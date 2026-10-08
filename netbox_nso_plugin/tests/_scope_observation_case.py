# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Canonical documents from the adapter switching and service read fixtures."""

import copy
import hashlib
import json


def entry(**values):
    return {"present": sorted(values), **values}


DOCUMENTS = {
    "interface_attributes": {
        "interfaces": [
            {
                "name": "Ethernet1",
                "description": "",
                "enabled": True,
                "kind": None,
                "parent_binding": None,
                "encap_tag": None,
                "vrf": None,
                "service": None,
            }
        ],
    },
    "interface_ip": {
        "interfaces": [
            {
                "interface": "Ethernet1",
                "bound_port": None,
                "addresses": [
                    {"address": "198.18.0.1/24", "prefix_length": 24, "family": "ipv4", "secondary": False, "vrf": ""}
                ],
            }
        ],
    },
    "lag_config": {
        "bundles": [
            entry(
                name="Port-channel1",
                lag_id=1,
                min_links=2,
                system_priority=100,
                system_id="",
                timer="fast",
                admin_key=None,
                vpc_sensitive=False,
                member=[entry(interface_name="Ethernet1", mode="active", port_priority=200)],
            )
        ]
    },
    "vlan": {"vlans": [entry(vlan_id=10, name="MGMT")]},
    "switchport": {
        "interfaces": [entry(interface_name="Ethernet1", mode="trunk", untagged_vlan=10, tagged_vlans=[10, 20])]
    },
    "interface_mtu": {
        "interfaces": [entry(interface_name="Ethernet1", mtu=9216, ip_mtu=9000, mpls_mtu=None, bound_port=None)]
    },
    "svi": {"interfaces": [entry(interface_name="Vlan100", vlan_id=100, type="svi", vrf="MGMT")]},
    "subinterface": {
        "interfaces": [
            entry(
                interface_name="Ethernet1.100",
                parent_interface="Ethernet1",
                dot1q_vlan=100,
                type="subinterface",
                vrf="TENANT_A",
            )
        ]
    },
    "bfd": {
        "interfaces": [
            entry(
                interface_name="Ethernet1",
                bound_port=None,
                min_tx=300,
                min_rx=300,
                multiplier=3,
                micro_bfd=True,
                enabled=True,
            )
        ]
    },
    "l2_service": {
        "services": [
            entry(
                service_name="example-service",
                service_type="epipe",
                service_id=100,
                saps=[entry(sap_id="Ethernet1:100", port="Ethernet1", outer_tag=100, inner_tag=None)],
            )
        ]
    },
    "logging": {
        "hosts": [
            entry(
                address="198.18.0.1", port=514, severity="errors", facility="local6", transport="udp", vrf="", source=""
            )
        ],
        "local_levels": entry(console_severity="CRITICAL", monitor_severity="NOTICE", module_severity="NOTICE"),
    },
    "snmp": {
        "communities": [entry(name="abc123def456abcd", access="RO", acl="20", has_secret=True)],
        "users": [entry(username="placeholder-user", has_auth_secret=True, has_priv_secret=False)],
        "hosts": [entry(address="198.18.0.2", version="3", notify_type="inform", port=162, user="placeholder-user")],
        "system": entry(location="example-lab", contact="example"),
    },
    "static_route": {
        "routes": [
            entry(
                vrf="",
                prefix="198.18.0.0/24",
                next_hop="198.18.1.1",
                interface_next_hop=None,
                next_hop_vrf=None,
                metric=3,
                permanent=False,
                tag=None,
                name="",
            )
        ]
    },
}

for family, document in DOCUMENTS.items():
    if family not in {"interface_attributes", "interface_ip"}:
        document["present"] = sorted(document)
    document["unprojectable"] = []


COVERAGE = {
    "bfd": ["bound_port", "enabled", "interface_name", "micro_bfd", "min_rx", "min_tx", "multiplier"],
    "interface_attributes": ["description", "enabled"],
    "interface_ip": ["address", "prefix_length", "secondary", "vrf"],
    "interface_mtu": ["bound_port", "interface_name", "ip_mtu", "mpls_mtu", "mtu"],
    "l2_service": ["inner_tag", "outer_tag", "port", "sap_id", "service_id", "service_name", "service_type"],
    "lag_config": [
        "admin_key",
        "lag_id",
        "member",
        "member.interface_name",
        "member.mode",
        "member.port_priority",
        "min_links",
        "name",
        "system_id",
        "system_priority",
        "timer",
        "vpc_sensitive",
    ],
    "logging": [
        "address",
        "console_severity",
        "facility",
        "module_severity",
        "monitor_severity",
        "port",
        "severity",
        "source",
        "transport",
        "vrf",
    ],
    "snmp": [
        "access",
        "acl",
        "address",
        "contact",
        "has_auth_secret",
        "has_priv_secret",
        "has_secret",
        "location",
        "name",
        "notify_type",
        "port",
        "user",
        "username",
        "version",
    ],
    "static_route": [
        "interface_next_hop",
        "metric",
        "name",
        "next_hop",
        "next_hop_vrf",
        "permanent",
        "prefix",
        "tag",
        "vrf",
    ],
    "subinterface": ["dot1q_vlan", "interface_name", "parent_interface", "type", "vrf"],
    "svi": ["interface_name", "type", "vlan_id", "vrf"],
    "switchport": ["interface_name", "mode", "tagged_vlans", "untagged_vlan"],
    "vlan": ["name", "vlan_id"],
}


def scope_observation(family, *, document=None, coverage=None, revision=1, source_epoch=1):
    document = copy.deepcopy(DOCUMENTS[family] if document is None else document)
    if coverage is None:
        coverage = {"attributes": list(COVERAGE[family])}
        if family == "snmp":
            coverage["not_comparable"] = ["auth_secret", "priv_secret"]
    return {
        "family": family,
        "revision": revision,
        "source_epoch": source_epoch,
        "digest": hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "observed_at": "2026-10-01T12:00:00+00:00",
        "coverage": copy.deepcopy(coverage),
        "document": document,
    }


def publish_scope_observation(management, family, read_state, body, **kwargs):
    """Supply the typed observation when a gate test publishes a revisioned family."""
    from netbox_nso_plugin.read_gate import gated_family_run

    if family in DOCUMENTS and isinstance(read_state, dict) and "observation" not in kwargs:
        kwargs["observation"] = scope_observation(
            family, revision=read_state.get("payload_revision"), source_epoch=read_state.get("source_epoch")
        )
    return gated_family_run(management, family, read_state, body, **kwargs)
