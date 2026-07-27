# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Canonical SNMP version grain shared by reconcile and intent push."""

_VERSION_CANON = {
    "1": "1",
    "v1": "1",
    "snmpv1": "1",
    "2": "2c",
    "2c": "2c",
    "v2": "2c",
    "v2c": "2c",
    "snmpv2": "2c",
    "snmpv2c": "2c",
    "3": "3",
    "v3": "3",
    "snmpv3": "3",
}


def canonical_snmp_version(value) -> str:
    token = str(value or "2c").strip().lower()
    return _VERSION_CANON.get(token, token)
