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

import difflib
import re
from itertools import zip_longest

from .route_policy_structure import summarize_route_map

# Split a value into value-runs and separator-runs so the inline diff aligns on whole tokens
# (each ASN / prefix / community / list element) rather than characters — e.g. a single extra
# ``1239`` in an as-path regex highlights just that ASN, not a smear of shifted characters.
_TOKEN_RE = re.compile(r"\w+|\W+")


def _is_value(token: str) -> bool:
    """Return whether the token carries content (a value), as opposed to a pure separator run."""
    return any(ch.isalnum() for ch in token)


def _merge_segments(tokens: list[str], changed_idx: set[int]) -> list[dict]:
    """Fold a token list into ``{"text", "changed"}`` runs, merging adjacent same-state tokens."""
    segments: list[dict] = []
    for i, token in enumerate(tokens):
        changed = i in changed_idx
        if segments and segments[-1]["changed"] == changed:
            segments[-1]["text"] += token
        else:
            segments.append({"text": token, "changed": changed})
    return segments


def inline_token_diff(device: str, netbox: str) -> tuple[list[dict], list[dict]]:
    """Token-level delta between two field values, as per-side highlight segments.

    Returns ``(device_segments, netbox_segments)`` where each is a list of
    ``{"text", "changed"}`` runs whose concatenation reproduces the original string. A run is
    ``changed`` when it is a VALUE present on this side but not the other (device-only or
    netbox-only), so the template can highlight exactly what differs (red on the device side,
    green on the NetBox side) instead of flagging the whole value.

    The alignment runs over the VALUE tokens only — separators (``|``, ``, ``, spaces …) are
    never matched on and never highlighted. Otherwise two equal-length lists like
    ``1239|12956`` vs ``12956|15169`` could align their ``|`` delimiters and mis-flag the
    shared ``12956``; aligning on values alone keeps a shared element shared.
    """
    dt = _TOKEN_RE.findall(device)
    nt = _TOKEN_RE.findall(netbox)
    dev_values = [(i, t) for i, t in enumerate(dt) if _is_value(t)]
    nb_values = [(i, t) for i, t in enumerate(nt) if _is_value(t)]
    matcher = difflib.SequenceMatcher(a=[t for _, t in dev_values], b=[t for _, t in nb_values], autojunk=False)
    dev_changed: set[int] = set()
    nb_changed: set[int] = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        dev_changed.update(dev_values[k][0] for k in range(i1, i2))
        nb_changed.update(nb_values[k][0] for k in range(j1, j2))
    return _merge_segments(dt, dev_changed), _merge_segments(nt, nb_changed)


def _augment_segments(row: dict) -> dict:
    """Attach highlight segments to a ``{device, netbox, differs}`` row.

    When both sides carry a value, highlight the token-level delta. When only one side has a
    value (a field present on just the device or just NetBox — including every field of an
    inserted/removed entry), highlight that whole value, so a device-only difference reads red
    and a NetBox-only one green, consistent with the legend. The absent ``"—"`` side is left
    unhighlighted.
    """
    if not row["differs"]:
        return row
    dev_present = row["device"] not in ("", "—")
    nb_present = row["netbox"] not in ("", "—")
    if dev_present and nb_present:
        row["device_segments"], row["netbox_segments"] = inline_token_diff(row["device"], row["netbox"])
    elif dev_present:
        row["device_segments"] = [{"text": row["device"], "changed": True}]
    elif nb_present:
        row["netbox_segments"] = [{"text": row["netbox"], "changed": True}]
    return row


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
        rows.append(_augment_segments({"label": label, "device": dv or "—", "netbox": nv or "—", "differs": dv != nv}))
    return rows


def _route_map_entry_line(entry: dict) -> str:
    """One-line rendering of a summarised route-map entry for the in-order side-by-side view."""
    bits = [str(entry.get("action") or "")]
    for key, label in _ENTRY_FIELDS:
        if key == "action":
            continue
        value = _render_field(key, entry.get(key))
        if value:
            bits.append(f"{label.lower()}: {value}")
    return " · ".join(b for b in bits if b)


def _simple_entry_line(family: str, entry: dict) -> str:
    """One-line rendering of a simple-family entry (prefix-list / as-path / community-list)."""
    action = entry.get("action") or ""
    if family == "prefix_list":
        parts = [action, entry.get("prefix") or ""]
        if entry.get("ge") not in (None, ""):
            parts.append(f"ge {entry['ge']}")
        if entry.get("le") not in (None, ""):
            parts.append(f"le {entry['le']}")
    elif family == "as_path":
        parts = [action, entry.get("pattern") or ""]
    elif family == "community_list":
        parts = [action, entry.get("community") or ""]
    else:
        parts = [action]
    return " ".join(p for p in parts if p)


def _entry_line(device_line: str, netbox_line: str, differs: bool) -> dict:
    """Build the ``line`` cell for an aligned entry — the two one-line summaries + highlights.

    A push makes the device adopt NetBox's full ordered list, so the diff is shown in order
    (Device now | NetBox after push). The whole NetBox line of an added entry reads green, the
    whole Device line of a removed one red, and a changed entry highlights just its differing
    tokens — all via the shared :func:`_augment_segments`.
    """
    return _augment_segments({"device": device_line or "—", "netbox": netbox_line or "—", "differs": differs})


def _align_pairs(device_entries: list, netbox_entries: list, signature) -> list[tuple]:
    """Pair two entry lists by CONTENT so inserting/removing one entry does not cascade.

    Aligning by position means a single entry one side has but the other lacks (a Junos term
    added in the middle, an extra prefix-list line) shifts every following entry, so the whole
    list reads as changed when only one entry really differs. Instead we run an LCS over each
    entry's ``signature`` (a hashable digest of its comparable content — the sequence number is
    deliberately excluded, since renumbering on insert is exactly what we want to absorb):

    * equal signatures align (shown unchanged),
    * an entry only on one side becomes a single insert / delete row, and
    * a run that genuinely differs on both sides is paired positionally within that run so an
      in-place field change still shows a field-level delta.

    Returns ``(device_entry|None, netbox_entry|None)`` pairs in display order.
    """
    da = [signature(e) for e in device_entries]
    na = [signature(e) for e in netbox_entries]
    pairs: list[tuple] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=da, b=na, autojunk=False).get_opcodes():
        if tag == "equal":
            pairs.extend((device_entries[i1 + k], netbox_entries[j1 + k]) for k in range(i2 - i1))
        elif tag == "delete":
            pairs.extend((device_entries[k], None) for k in range(i1, i2))
        elif tag == "insert":
            pairs.extend((None, netbox_entries[k]) for k in range(j1, j2))
        else:  # replace — a genuinely-different run; pair up in order for field-level diffs
            pairs.extend(zip_longest(device_entries[i1:i2], netbox_entries[j1:j2]))
    return pairs


def _route_map_entry_signature(entry: dict) -> tuple:
    """Content digest of a summarised route-map entry (sequence excluded) for content alignment."""
    return tuple(_render_field(key, entry.get(key)) for key, _ in _ENTRY_FIELDS)


def route_map_diff(device_captured: dict | None, netbox_captured: dict | None) -> dict:
    """Diff a device's captured route-map against the NetBox-materialised version.

    Both inputs are capture-shaped dicts (``{"entries": [...]}``); they are summarised through
    the same projection and aligned by CONTENT (see :func:`_align_pairs`). Returns
    ``{any_diff, default_action, entries}`` where each entry carries its per-field rows and a
    ``presence`` of both / device_only / netbox_only. Pure — no DB.
    """
    dev = summarize_route_map(device_captured)
    nb = summarize_route_map(netbox_captured)

    # Align by CONTENT, not position: the reconciler renumbers entries 1..N on materialise, and a
    # single inserted/removed entry would otherwise shift the rest and read as a cascade of
    # changes. Label each row with the device's sequence where present, else the NetBox one.
    entries = []
    any_diff = False
    for de, ne in _align_pairs(dev["entries"], nb["entries"], _route_map_entry_signature):
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
                "line": _entry_line(
                    _route_map_entry_line(de) if de else "", _route_map_entry_line(ne) if ne else "", differs
                ),
            }
        )

    default_action = _augment_segments(
        {
            "device": dev["default_action"] or "—",
            "netbox": nb["default_action"] or "—",
            "differs": dev["default_action"] != nb["default_action"],
        }
    )
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


def _entry_list_diff(device_entries: list[dict], netbox_entries: list[dict], field_specs, family: str) -> dict:
    """Content-aligned diff of two entry lists into the shared diff shape (no default_action)."""
    entries = []
    any_diff = False

    def _sig(entry):
        return tuple(_render_scalar(entry.get(key)) for key, _ in field_specs)

    for i, (de, ne) in enumerate(_align_pairs(device_entries, netbox_entries, _sig), start=1):
        presence = "both" if de and ne else ("device_only" if de else "netbox_only")
        fields = []
        for key, label in field_specs:
            dv = _render_scalar((de or {}).get(key))
            nv = _render_scalar((ne or {}).get(key))
            if not dv and not nv:
                continue
            fields.append(
                _augment_segments({"label": label, "device": dv or "—", "netbox": nv or "—", "differs": dv != nv})
            )
        differs = presence != "both" or any(f["differs"] for f in fields)
        any_diff = any_diff or differs
        entries.append(
            {
                "sequence": i,
                "netbox_sequence": None,
                "presence": presence,
                "differs": differs,
                "fields": fields,
                "line": _entry_line(
                    _simple_entry_line(family, de) if de else "", _simple_entry_line(family, ne) if ne else "", differs
                ),
            }
        )
    return {"any_diff": any_diff, "default_action": None, "extra": [], "entries": entries}


def _simple_family_diff(state, obj, removed: bool) -> dict:
    """Diff a prefix-list / as-path / community-list row (device capture vs NetBox object)."""
    family = state.family
    device_entries = [] if removed else _device_simple_entries(family, state.captured)
    diff = _entry_list_diff(device_entries, _netbox_simple_entries(family, obj), _SIMPLE_FIELDS[family], family)
    if family == "community_list":
        dev_inv = "" if removed else _render_scalar((state.captured or {}).get("invert_match"))
        nb_inv = _render_scalar(getattr(obj, "invert_match", None))
        if dev_inv or nb_inv:
            differs = dev_inv != nb_inv
            diff["extra"].append(
                _augment_segments(
                    {"label": "Invert match", "device": dev_inv or "—", "netbox": nb_inv or "—", "differs": differs}
                )
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


# ── #91: canonical text rendering + unified diff (the two-panel view's source) ──


def _route_map_text_lines(summary: dict) -> list[str]:
    """Canonical, SEQUENCE-FREE text rendering of a summarised route-map.

    One block per entry: the action header plus an indented line per non-empty field,
    all through the same ``_render_field`` normalisation as the structured diff — so
    the two sides render identically exactly when the structured diff sees no
    difference. Sequence numbers are deliberately omitted (the reconciler renumbers
    1..N on materialise; numbering would read as drift on every insert).
    """
    lines: list[str] = []
    for entry in summary["entries"]:
        lines.append(f"entry {entry.get('action') or ''}".rstrip())
        for key, label in _ENTRY_FIELDS:
            if key == "action":
                continue
            value = _render_field(key, entry.get(key))
            if value:
                lines.append(f"  {label.lower()}: {value}")
    if summary.get("default_action"):
        lines.append(f"default action: {summary['default_action']}")
    return lines


def _simple_text_lines(state, obj, removed: bool) -> tuple[list[str], list[str]]:
    """Canonical per-entry text for a simple family, both sides (device, netbox)."""
    family = state.family
    device_entries = [] if removed else _device_simple_entries(family, state.captured)
    dev = [_simple_entry_line(family, e) for e in device_entries]
    nb = [_simple_entry_line(family, e) for e in _netbox_simple_entries(family, obj)]
    if family == "community_list":
        dev_inv = "" if removed else _render_scalar((state.captured or {}).get("invert_match"))
        nb_inv = _render_scalar(getattr(obj, "invert_match", None))
        if dev_inv or nb_inv:
            dev.append(f"invert match: {dev_inv or '—'}")
            nb.append(f"invert match: {nb_inv or '—'}")
    return dev, nb


def policy_text_sides(state) -> tuple[list[str], list[str]] | None:
    """Render BOTH sides of a route-policy state through ONE canonical pretty-printer.

    Returns ``(device_lines, netbox_lines)``, or None when there is no materialised
    object (or an unknown family). Uses the same readers and normalisation as
    :func:`route_policy_state_diff`, so the text panel and the structured table always
    agree on whether something differs.
    """
    obj = state.assigned_object
    if obj is None:
        return None
    removed = not getattr(state, "device_present", True)
    if state.family == "route_map":
        dev = summarize_route_map({} if removed else state.captured)
        nb = summarize_route_map(netbox_route_map_captured(obj))
        return _route_map_text_lines(dev), _route_map_text_lines(nb)
    if state.family in _SIMPLE_FIELDS:
        return _simple_text_lines(state, obj, removed)
    return None


def unified_policy_diff(state) -> str:
    """Unified-diff text of the canonical rendering of both sides (diff2html-ready).

    ``difflib.unified_diff`` emits the real ``---``/``+++``/``@@`` headers diff2html's
    parser requires; identical sides (or an unmaterialised state) yield ``""`` so the
    caller can skip the panel entirely. Both headers carry the SAME label — differing
    from/to names make diff2html flag the file as RENAMED, which is noise here (the
    panel caption already explains left=device / right=NetBox).
    """
    sides = policy_text_sides(state)
    if sides is None:
        return ""
    device_lines, netbox_lines = sides
    label = f"{state.family.replace('_', '-')} {state.object_name}"
    return "\n".join(difflib.unified_diff(device_lines, netbox_lines, fromfile=label, tofile=label, lineterm=""))


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
        rows.append(_augment_segments({"label": label, "device": dv or "—", "netbox": nv or "—", "differs": differs}))
    return {"linked": rd is not None, "removed_on_device": removed_on_device, "any_diff": any_diff, "fields": rows}
