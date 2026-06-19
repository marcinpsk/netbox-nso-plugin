# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Device-vs-NetBox delta for route-policy + redistribution overlays.

The overlays surface a *status* (``conflict`` / ``changed``) when a device's config diverges
from what NetBox holds, but not WHAT diverges. This module computes the concrete delta so the
operator can see it:

* **Route-maps** — the device side is the row's own ``captured`` payload; the NetBox side is
  reconstructed from the materialised ``RouteMap`` / ``RouteMapEntry`` rows back into the same
  capture shape. BOTH sides then go through :func:`route_policy_structure.summarize_route_map`,
  so the comparison is apples-to-apples (identical normalisation) and a difference is a real
  config difference, never a serialisation artefact.
* **Redistribution** — a flat per-field compare of the overlay's device-reported values
  against the linked netbox-routing ``Redistribution`` object.

The route-map diff core (:func:`route_map_diff`) is a pure function over capture dicts so it is
directly unit-testable; the reconstruction + redistribution readers touch the DB (thin reads).
"""

from __future__ import annotations

from itertools import zip_longest

from .route_policy_structure import summarize_route_map

# Per-entry fields compared in the route-map diff, in display order. Each is projected by
# summarize_route_map into a uniform shape (str / list / dict / list-of-set-community).
_ENTRY_FIELDS: tuple[tuple[str, str], ...] = (
    ("action", "Action"),
    ("match_afi", "Match AFI"),
    ("match_prefix_lists", "Match prefix-lists"),
    ("match_community_lists", "Match community-lists"),
    ("match_as_paths", "Match as-paths"),
    ("match_knobs", "Match"),
    ("set_communities", "Set community"),
    ("call_policy", "Call policy"),
    ("set_knobs", "Set"),
    ("vendor_ext", "Vendor ext"),
)


def _render_field(key: str, value) -> str:
    """Normalise one summarised field to a stable, comparable display string.

    Lists/dicts are sorted so logically-equal content renders identically (and therefore
    compares equal); empty values render as ``""`` so a field absent on one side is comparable.
    """
    if value in (None, "", [], {}):
        return ""
    if key == "set_communities":
        return ", ".join(sorted(f"{c.get('operation')} {c.get('name')}" for c in value))
    if key == "vendor_ext":
        # {ns: {k: v}} → "ns:k=v" pairs, sorted.
        pairs = [f"{ns}:{k}={v}" for ns, kv in value.items() for k, v in kv.items()]
        return ", ".join(sorted(pairs))
    if isinstance(value, list):
        return ", ".join(str(v) for v in sorted(value, key=str))
    if isinstance(value, dict):
        return ", ".join(f"{k}={value[k]}" for k in sorted(value))
    return str(value)


def _diff_entry_fields(device_entry: dict | None, netbox_entry: dict | None) -> list[dict]:
    """Field-by-field diff of one aligned route-map entry (omits fields empty on both sides)."""
    rows = []
    for key, label in _ENTRY_FIELDS:
        dv = _render_field(key, (device_entry or {}).get(key))
        nv = _render_field(key, (netbox_entry or {}).get(key))
        if not dv and not nv:
            continue
        rows.append({"label": label, "device": dv or "—", "netbox": nv or "—", "differs": dv != nv})
    return rows


def route_map_diff(device_captured: dict | None, netbox_captured: dict | None) -> dict:
    """Diff a device's captured route-map against the NetBox-materialised version.

    Both inputs are capture-shaped dicts (``{"entries": [...]}``); they are summarised through
    the same projection and aligned by sequence. Returns ``{any_diff, default_action, entries}``
    where each entry carries its per-field rows and a ``presence`` of both / device_only /
    netbox_only. Pure — no DB.
    """
    dev = summarize_route_map(device_captured)
    nb = summarize_route_map(netbox_captured)

    # Align POSITIONALLY, not by sequence number: the reconciler renumbers entries 1..N when it
    # materialises them (device sequences can overflow the smallint column and Junos prefix-lists
    # have none), so a device capture (original sequences) and the NetBox object (positional) use
    # different numbering for the SAME ordered entries. Both summaries preserve entry order, so
    # position i ↔ position i; label each row with the device's sequence where present.
    entries = []
    any_diff = False
    for de, ne in zip_longest(dev["entries"], nb["entries"]):
        presence = "both" if de and ne else ("device_only" if de else "netbox_only")
        fields = _diff_entry_fields(de, ne)
        differs = presence != "both" or any(f["differs"] for f in fields)
        any_diff = any_diff or differs
        entries.append(
            {
                "sequence": (de or ne)["sequence"],
                "netbox_sequence": (ne or {}).get("sequence"),
                "presence": presence,
                "differs": differs,
                "fields": fields,
            }
        )

    default_action = {
        "device": dev["default_action"] or "—",
        "netbox": nb["default_action"] or "—",
        "differs": dev["default_action"] != nb["default_action"],
    }
    any_diff = any_diff or default_action["differs"]
    return {"any_diff": any_diff, "default_action": default_action, "entries": entries}


def netbox_route_map_captured(rm_obj) -> dict:
    """Reconstruct a capture-shaped dict from a materialised ``RouteMap`` (inverse of the fill).

    Mirrors what ``route_policy_reconciler._fill_route_map_entries`` wrote, so feeding this back
    through ``summarize_route_map`` yields the same projection as the device capture would for
    identical content. ``match`` / ``set`` blobs round-trip as dicts (``summarize`` accepts
    either dicts or JSON strings); ``flow_control`` is folded back into the set blob.
    """
    entries = []
    qs = rm_obj.route_map_entries.all().prefetch_related("match_prefix_list", "match_community_list", "match_aspath")
    for e in qs.order_by("sequence"):
        set_blob = dict(e.set or {})
        if e.flow_control is not None:
            set_blob["flow_control"] = e.flow_control
        entries.append(
            {
                "sequence": e.sequence,
                "action": e.action,
                "match": dict(e.match or {}),
                "set": set_blob,
                "match_prefix_lists": sorted(p.name for p in e.match_prefix_list.all()),
                "match_community_lists": sorted(c.name for c in e.match_community_list.all()),
                "match_as_paths": sorted(a.name for a in e.match_aspath.all()),
            }
        )
    return {"entries": entries}


def route_policy_state_diff(state) -> dict | None:
    """Diff a NSORoutePolicyState route-map row (device capture vs the NetBox object).

    Returns the :func:`route_map_diff` result, or ``None`` when the row isn't a route-map or
    has no materialised object to compare against.
    """
    if state.family != "route_map":
        return None
    rm_obj = state.assigned_object
    if rm_obj is None:
        return None
    return route_map_diff(state.captured, netbox_route_map_captured(rm_obj))


_REDIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("route_map", "Route map"),
    ("metric", "Metric"),
    ("metric_type", "Metric type"),
)


def redistribution_diff(state) -> dict:
    """Diff a NSORedistributionState row's device-reported values vs the NetBox object.

    The device side is the overlay's own ``route_map`` / ``metric`` / ``metric_type`` (last
    synced from the device); the NetBox side is the linked ``Redistribution`` object. Returns
    ``{linked, any_diff, fields}``; when no object is linked yet every field reads device-only.
    """
    rd = state.redistribution
    device = {"route_map": state.route_map, "metric": state.metric, "metric_type": state.metric_type}
    if rd is not None:
        netbox = {
            "route_map": rd.route_map.name if rd.route_map_id else "",
            "metric": rd.metric,
            "metric_type": rd.metric_type,
        }
    else:
        netbox = {"route_map": "", "metric": None, "metric_type": ""}

    rows = []
    any_diff = False
    for key, label in _REDIST_FIELDS:
        dv = "" if device[key] in (None, "") else str(device[key])
        nv = "" if netbox[key] in (None, "") else str(netbox[key])
        differs = dv != nv
        any_diff = any_diff or differs
        rows.append({"label": label, "device": dv or "—", "netbox": nv or "—", "differs": differs})
    return {"linked": rd is not None, "any_diff": any_diff, "fields": rows}
