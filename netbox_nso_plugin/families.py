# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The vendored read-outcome family vocabulary (READSEM S4 D8).

A COPY of the adapter's canonical registry (``nso_adapter/core/families.py``) — the two
repos have no shared build artifact, so the contract is layered: this module pins the
expected keys + ``FAMILIES_VERSION``; the adapter's aggregate read-state endpoint serves
its version and key set, and a mismatch surfaces as a VISIBLE runtime warning (never a
silent drop); the live rehearsal is the true cross-repo check.

``CATEGORY_FAMILIES`` maps each device-tab category to the families it actually DISPLAYS
(codex R2-6: reconciliation dependencies are NOT display dependencies — LACP renders
lag_config rows only; the ``lag`` topology outcome has no production consumer and is an
explicit non-goal). ``AGGREGATION_ORDER`` ranks per-family states worst-first for merged
category chips (D8; healthy = fresh-present OR authoritative-empty, D10).
"""

from __future__ import annotations

# Must equal the adapter's FAMILIES_VERSION; bump in lockstep.
FAMILIES_VERSION = 1

# The adapter outcome store's 19 canonical family keys (underscore vocabulary).
ALL_FAMILY_KEYS: tuple[str, ...] = (
    "lag",
    "logging",
    "snmp",
    "bgp",
    "svi",
    "subinterface",
    "interface_ip",
    "isis",
    "vlan",
    "switchport",
    "bfd",
    "l2_service",
    "static_route",
    "interface_mtu",
    "lag_config",
    "ospf",
    "route_policy",
    "redistribution",
    "interface_attributes",
)

# Device-tab category → the families whose read state that category DISPLAYS
# (keys match summary._CATEGORIES; verified against the categories' actual row sources).
CATEGORY_FAMILIES: dict[str, tuple[str, ...]] = {
    "interface": ("interface_attributes", "interface_ip", "interface_mtu", "switchport"),
    "lacp": ("lag_config",),  # lag topology outcome NOT displayed (no production consumer)
    "vlan": ("vlan",),
    "svi": ("svi",),
    "subinterface": ("subinterface",),
    "static": ("static_route",),
    "isis": ("isis",),
    "ospf": ("ospf",),
    "bgp": ("bgp",),
    "bfd": ("bfd",),
    "route_policy": ("route_policy",),
    "redistribution": ("redistribution",),
    "snmp": ("snmp",),
    "logging": ("logging",),
    "l2_services": ("l2_service",),
}

# Worst-first severity for merging a category's family states into one chip (D8/D10).
# 'healthy' = fresh-present OR authoritative-empty (absent_authoritative/cleared/True).
AGGREGATION_ORDER: tuple[str, ...] = (
    "unavailable",  # incl. materialization error — red
    "unknown",  # unrecognized/malformed — red, fail-closed visible
    "stale",  # amber
    "aged",  # amber
    "not_authoritative",  # muted outline
    "unsupported",  # muted outline
    "healthy",  # no chip
)
