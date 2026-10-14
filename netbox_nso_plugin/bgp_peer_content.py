# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Render native and observed peers with one canonical wire content definition."""

import ipaddress
from contextlib import suppress


def _native_peer_values(peer):
    source = None
    if peer.update_source is not None:
        source = peer.update_source.name
    elif peer.source is not None:
        source = str(peer.source.address.ip)
    return {
        "enabled": peer.enabled,
        "remote_as": peer.remote_as.asn if peer.remote_as_id else None,
        "local_as": peer.local_as.asn if peer.local_as_id else None,
        "peer_group": peer.peer_group.name if peer.peer_group_id else None,
        "source": source,
        "ttl": peer.ttl,
        "password": peer.password,
    }


def _peer_af_content(row):
    if isinstance(row, dict):
        values = row
        af = row["af"]
    else:
        af = row.address_family.address_family
        values = {"enabled": row.enabled}
        for field in ("routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out"):
            value = getattr(row, field)
            values[field] = value.name if value is not None else None
    content = {"af": af, "enabled": values.get("enabled") is not False}
    for field in ("routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out"):
        if values.get(field) not in (None, ""):
            content[field] = values[field]
    return content


def peer_content(peer, address_families=None):
    """Return the canonical wire leaves of a native peer or a device peer entry."""
    from .bgp_reconciler import _parse_asn

    if isinstance(peer, dict):
        values = peer
        rows = peer["address_families"] if address_families is None else address_families
    else:
        values = _native_peer_values(peer)
        rows = peer.address_families.all() if address_families is None else address_families
    content = {"enabled": values.get("enabled") is not False}
    for field in ("remote_as", "local_as", "peer_group", "source", "ttl", "password"):
        value = values.get(field)
        if value in (None, ""):
            continue
        if field in ("remote_as", "local_as"):
            value = str(_parse_asn(str(value)))
        elif field == "source":
            with suppress(ValueError):
                value = ipaddress.ip_interface(value).ip.compressed
        content[field] = value
    content["address_families"] = sorted((_peer_af_content(row) for row in rows), key=lambda row: row["af"])
    return content
