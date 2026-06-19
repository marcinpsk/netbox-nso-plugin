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
    return {"any_diff": any_diff, "default_action": default_action, "extra": [], "entries": entries}


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


# Per-entry display fields for the simple (non-route-map) families. Each captured entry / NetBox
# entry-row is projected to the same dict so the device and NetBox sides compare apples-to-apples.
_SIMPLE_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "prefix_list": (("prefix", "Prefix"), ("action", "Action"), ("ge", "GE"), ("le", "LE")),
    "as_path": (("action", "Action"), ("pattern", "Pattern")),
    "community_list": (("action", "Action"), ("community", "Community")),
}


def _norm_action(action) -> str:
    """Normalise a device action the same way the reconciler stores it (permit/deny)."""
    a = (action or "").strip().lower()
    if a in ("deny", "reject"):
        return "deny"
    return "permit"


def _render_scalar(value) -> str:
    return "" if value in (None, "") else str(value)


def _device_simple_entries(family: str, captured: dict | None) -> list[dict]:
    """Project a device's captured entries for a simple family into comparable dicts."""
    out: list[dict] = []
    for e in (captured or {}).get("entries") or []:
        if family == "prefix_list":
            prefix = (e.get("prefix") or "").strip()
            if not prefix:
                continue
            out.append(
                {"prefix": prefix, "action": _norm_action(e.get("action")), "ge": e.get("ge"), "le": e.get("le")}
            )
        elif family == "as_path":
            out.append({"action": _norm_action(e.get("action")), "pattern": (e.get("pattern") or "").strip()})
        elif family == "community_list":
            community = (e.get("community") or "").strip()
            if not community:
                continue
            out.append({"action": _norm_action(e.get("action")), "community": community})
    return out


def _netbox_simple_entries(family: str, obj) -> list[dict]:
    """Reconstruct comparable entry dicts from a materialised prefix-list/as-path/community-list."""
    out: list[dict] = []
    if family == "prefix_list":
        from netbox_routing.models import PrefixListEntry

        for e in PrefixListEntry.objects.filter(prefix_list=obj).order_by("sequence"):
            cp = e.assigned_prefix
            out.append({"prefix": str(cp.prefix) if cp else "", "action": e.action, "ge": e.ge, "le": e.le})
    elif family == "as_path":
        from netbox_routing.models import ASPathEntry

        for e in ASPathEntry.objects.filter(aspath=obj).order_by("sequence"):
            out.append({"action": e.action, "pattern": e.pattern or ""})
    elif family == "community_list":
        from netbox_routing.models import CommunityListEntry

        for e in CommunityListEntry.objects.filter(community_list=obj).select_related("community").order_by("pk"):
            out.append({"action": e.action, "community": e.community.community if e.community_id else ""})
    return out


def _entry_list_diff(device_entries: list[dict], netbox_entries: list[dict], field_specs) -> dict:
    """Positional diff of two entry lists into the shared diff shape (no default_action)."""
    entries = []
    any_diff = False
    for i, (de, ne) in enumerate(zip_longest(device_entries, netbox_entries), start=1):
        presence = "both" if de and ne else ("device_only" if de else "netbox_only")
        fields = []
        for key, label in field_specs:
            dv = _render_scalar((de or {}).get(key))
            nv = _render_scalar((ne or {}).get(key))
            if not dv and not nv:
                continue
            fields.append({"label": label, "device": dv or "—", "netbox": nv or "—", "differs": dv != nv})
        differs = presence != "both" or any(f["differs"] for f in fields)
        any_diff = any_diff or differs
        entries.append(
            {"sequence": i, "netbox_sequence": None, "presence": presence, "differs": differs, "fields": fields}
        )
    return {"any_diff": any_diff, "default_action": None, "extra": [], "entries": entries}


def _simple_family_diff(state, obj, removed: bool) -> dict:
    """Diff a prefix-list / as-path / community-list row (device capture vs NetBox object)."""
    family = state.family
    device_entries = [] if removed else _device_simple_entries(family, state.captured)
    diff = _entry_list_diff(device_entries, _netbox_simple_entries(family, obj), _SIMPLE_FIELDS[family])
    if family == "community_list":
        dev_inv = "" if removed else _render_scalar((state.captured or {}).get("invert_match"))
        nb_inv = _render_scalar(getattr(obj, "invert_match", None))
        if dev_inv or nb_inv:
            differs = dev_inv != nb_inv
            diff["extra"].append(
                {"label": "Invert match", "device": dev_inv or "—", "netbox": nb_inv or "—", "differs": differs}
            )
            diff["any_diff"] = diff["any_diff"] or differs
    return diff


def route_policy_state_diff(state) -> dict | None:
    """Diff a NSORoutePolicyState row — device capture vs the materialised NetBox object.

    Dispatches by family: route-maps use the rich structured :func:`route_map_diff`; prefix-lists,
    as-paths and community-lists use a positional entry-list diff. Returns the shared diff shape
    (``any_diff``, ``default_action``, ``extra``, ``entries``, ``removed_on_device``), or ``None``
    when there is no materialised object to compare against.

    When ``device_present`` is False the device has REMOVED the object: its ``captured`` is stale
    (last-seen) and would falsely match the object, so the device side is compared as empty —
    every NetBox entry reads "only in NetBox" and ``removed_on_device`` / ``any_diff`` are set, so
    the delta agrees with the row's ``changed`` status.
    """
    obj = state.assigned_object
    if obj is None:
        return None
    removed_on_device = not getattr(state, "device_present", True)
    if state.family == "route_map":
        device_captured = {} if removed_on_device else state.captured
        diff = route_map_diff(device_captured, netbox_route_map_captured(obj))
    elif state.family in _SIMPLE_FIELDS:
        diff = _simple_family_diff(state, obj, removed_on_device)
    else:
        return None
    diff["removed_on_device"] = removed_on_device
    if removed_on_device:
        diff["any_diff"] = True
    return diff


_REDIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("route_map", "Route map"),
    ("metric", "Metric"),
    ("metric_type", "Metric type"),
)


def redistribution_diff(state) -> dict:
    """Diff a NSORedistributionState row's device-reported values vs the NetBox object.

    The device side is the overlay's own ``route_map`` / ``metric`` / ``metric_type`` (last
    synced from the device); the NetBox side is the linked ``Redistribution`` object. Returns
    ``{linked, removed_on_device, any_diff, fields}``.

    When ``device_present`` is False the device has REMOVED this redistribution: the stored
    fields are stale (last-seen) and would falsely match the object, so the device side reads
    "removed" for every field the object still carries and ``removed_on_device``/``any_diff``
    are set — the delta then agrees with the row's ``changed`` status instead of claiming "no
    drift". When no object is linked yet every field reads device-only.
    """
    rd = state.redistribution
    removed_on_device = not getattr(state, "device_present", True)
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
    any_diff = removed_on_device  # a removal is itself drift, even if the object carries no values
    for key, label in _REDIST_FIELDS:
        nv = "" if netbox[key] in (None, "") else str(netbox[key])
        if removed_on_device:
            # The device no longer reports the entry — show it as gone, not the stale fields.
            rows.append({"label": label, "device": "removed", "netbox": nv or "—", "differs": nv != ""})
            continue
        dv = "" if device[key] in (None, "") else str(device[key])
        differs = dv != nv
        any_diff = any_diff or differs
        rows.append({"label": label, "device": dv or "—", "netbox": nv or "—", "differs": differs})
    return {"linked": rd is not None, "removed_on_device": removed_on_device, "any_diff": any_diff, "fields": rows}
