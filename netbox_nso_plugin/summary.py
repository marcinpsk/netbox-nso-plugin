# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Read-only per-category counts for the device NSO tab's collapsed view.

The tab now renders counts-first: each managed scope shows a total and a status
breakdown, computed with cheap aggregate queries over the persisted NSO*State
tables — NO adapter calls and NO reconcile writes. Rows are fetched lazily when a
category is expanded (see views.NSOCategoryView). The persisted state is kept fresh
by the background reconcile (reconcile.run_device_reconcile) the adapter triggers on
each sync-complete, so these counts reflect the last sync without touching the page.
"""

from __future__ import annotations

from django.db.models import Count

from .status_machine import OWNED_STATES

# The display state is TWO independent dimensions:
#   sync     — does the device match NetBox?  match (imported/in_sync) vs differ
#   owned    — is NetBox the source of truth?  owned == status in OWNED_STATES
#              (accepted/deploying/in_sync/apply_failed). This is the canonical
#              ownership test (status_machine.is_owned) and exactly what Apply pushes —
#              NOT ``accepted_at``, which is a one-shot timestamp never cleared on
#              un-own, so a row reverted/drifted back to ``imported`` keeps a stale
#              accepted_at and would otherwise read as owned forever.
# Combined into the operator-facing buckets:
#   in sync       = device matches NetBox (whether owned or not)
#   drift         = device differs AND NetBox does NOT own it (device changed out-of-band)
#   pending apply = device differs AND NetBox owns it (Apply will push NetBox's value)
_MATCH_STATUSES = ("imported", "in_sync")
_DIFFER_STATUSES = ("changed", "conflict", "accepted", "apply_failed")


def display_state(status: str, owned: bool):
    """Return (kind, label) for a row from its sync *status* and *owned* flag.

    kind ∈ in_sync | drift | pending | deploying | unknown — drives badge colour.
    """
    if status == "deploying":
        return ("deploying", "deploying")
    if status in _MATCH_STATUSES:
        return ("in_sync", "in sync")
    if status in _DIFFER_STATUSES:
        return ("pending", "pending apply") if owned else ("drift", "drift")
    return ("unknown", status or "unknown")


def matches_device_value(attribute, netbox_value, nso_value):
    """Return True if a NetBox attribute value equals the device (NSO) value.

    The device value is stored as a string in ``NSOInterfaceState.nso_value``
    ("True"/"False" for enabled), so compare in the attribute's native type.
    """
    if attribute == "enabled":
        nso = (str(nso_value) if nso_value is not None else "").strip().lower()
        if nso == "":
            # Device did not report 'enabled' (e.g. a NED that doesn't expose it). There
            # is nothing to compare against, so don't manufacture drift — and do it
            # symmetrically (previously False matched but True drifted).
            return True
        return bool(netbox_value) == (nso == "true")
    # String attrs (description): compare ignoring leading/trailing whitespace. Device
    # configs frequently carry cosmetic surrounding spaces (e.g. a trailing space in a
    # Junos `description "..."`) that NetBox stores stripped — comparing raw would
    # manufacture drift on values that are identical to the operator.
    return (netbox_value or "").strip() == (nso_value or "").strip()


# Interface attributes whose NetBox value we can read directly and compare against
# the device value — so the display does not have to trust the adapter's status,
# which lags (one-sync) and is blind to a value typed straight into NetBox.
_COMPARABLE_IFACE_ATTRS = ("description", "enabled")


def _netbox_value_for(attribute, iface):
    """Return the raw NetBox value for a comparable interface attribute, else None."""
    if attribute == "description":
        return iface.description
    if attribute == "enabled":
        return iface.enabled
    return None


def interface_row_state(st, iface):
    """Return (kind, label, owned) for one interface-attr row — value-aware.

    For description/enabled we compare the *actual* NetBox value against the device
    value rather than trusting the adapter-reported status: the adapter only learns a
    NetBox value once it writes it back, so a value set directly in NetBox shows as
    ``unknown``/``imported`` and would hide in the "in sync" remainder. When the
    values match it is in sync; when they differ it is pending apply (owned) or drift
    (not owned). In-flight ("deploying") and non-comparable attributes fall back to
    the status-driven :func:`display_state`.
    """
    owned = st.status in OWNED_STATES
    if st.status == "deploying" or st.attribute not in _COMPARABLE_IFACE_ATTRS:
        kind, label = display_state(st.status, owned)
        return (kind, label, owned)
    matches = matches_device_value(st.attribute, _netbox_value_for(st.attribute, iface), st.nso_value)
    if matches:
        return ("in_sync", "in sync", owned)
    # Values differ. Surface a failed apply distinctly so the operator knows the last
    # push errored, instead of it hiding as an ordinary "pending apply".
    if st.status == "apply_failed":
        return ("apply_failed", "apply failed", owned)
    return ("pending", "pending apply", owned) if owned else ("drift", "drift", owned)


def interface_status_breakdown(qs) -> dict:
    """Value-aware {total, drift, pending} for interface-attr states.

    Mirrors :func:`_status_breakdown` but classifies each row through
    :func:`interface_row_state` (real NetBox vs device value) so a value set
    directly in NetBox is bucketed correctly instead of vanishing into "in sync".
    """
    out = {"total": 0, "drift": 0, "pending": 0}
    for st in qs.select_related("interface"):
        out["total"] += 1
        kind, _label, _owned = interface_row_state(st, st.interface)
        if kind in ("pending", "deploying", "apply_failed"):
            out["pending"] += 1
        elif kind == "drift":
            out["drift"] += 1
    return out


# Each category: key -> (label, mdi-icon, scope-flag on NSODeviceManagement).
# Order here is the display order on the tab.
# manage_interfaces and manage_snmp are standalone scopes — not gated by the
# manage_routing master switch (see category_summaries).
# The per-interface scalar attributes (enabled/description, IPs, MTU, switchport)
# render as ONE consolidated "Interfaces" card — a row per interface with a
# column per attribute and a column-select filter — instead of four scattered
# cards. The individual keys still exist (partials/accept/reconcile) for direct
# fetch and per-cell reuse; they're just no longer separate cards on the tab.
_CATEGORIES = [
    ("interface", "Interfaces", "ethernet", "manage_interfaces"),
    ("lacp", "LACP", "link-variant", "manage_interfaces"),
    ("vlan", "VLANs", "format-list-numbered", "manage_interfaces"),
    ("svi", "SVIs / IRBs", "ip-network", "manage_interfaces"),
    ("subinterface", "Subinterfaces", "vector-difference", "manage_interfaces"),
    ("static", "Static Routes", "sign-direction", "manage_static"),
    ("isis", "IS-IS", "lan", "manage_isis"),
    ("ospf", "OSPF", "lan", "manage_ospf"),
    ("bgp", "BGP", "router-network", "manage_bgp"),
    # R3-7: BFD is reconciled whenever ANY of bgp/isis/ospf is managed
    # (reconcile.py's rider predicate) — its card must be visible for each.
    ("bfd", "BFD", "pulse", ("manage_isis", "manage_bgp", "manage_ospf")),
    ("route_policy", "Route Policy", "script-text", "manage_route_policy"),
    ("redistribution", "Redistribution", "swap-horizontal", "manage_redistribution"),
    ("snmp", "SNMP", "console-network", "manage_snmp"),
    ("logging", "Logging", "file-document-outline", "manage_logging"),
    ("l2_services", "L2 Services", "lan-connect", "manage_l2"),
]

# Scopes that stand alone (not under the manage_routing master kill-switch).
_NON_ROUTING_FLAGS = {"manage_interfaces", "manage_snmp", "manage_logging", "manage_l2"}


def _status_breakdown(qs) -> dict:
    """Return owned-aware {total, drift, pending} buckets for a state queryset.

    Owned = ``status in OWNED_STATES`` (the canonical test — what Apply pushes), NOT
    ``accepted_at`` (a stale, never-cleared timestamp). A differing status is therefore
    *intrinsically* owned-or-not: accepted/apply_failed are owned → pending apply;
    changed/conflict are unowned → drift. match (imported/in_sync) → in sync (the
    implicit remainder). A row left at ``unknown`` (or any unrecognized status) is an
    *anomaly* — reconcilers always set a concrete status — so it is surfaced under drift
    (needs attention) rather than hidden in the in-sync remainder.
    """
    rows = qs.values_list("status").annotate(total=Count("id"))
    out = {"total": 0, "drift": 0, "pending": 0}
    for status, total in rows:
        out["total"] += total
        if status == "deploying":
            out["pending"] += total
        elif status in _DIFFER_STATUSES:
            if status in OWNED_STATES:
                out["pending"] += total  # accepted / apply_failed — owned differ
            else:
                out["drift"] += total  # changed / conflict — unowned differ
        elif status in _MATCH_STATUSES:
            pass  # in sync — the implicit remainder
        else:
            out["drift"] += total  # unknown/unrecognized → surface, don't hide
    return out


def _category_counts(key: str, device, mgmt) -> dict:  # noqa: C901
    """Compute the count breakdown for one category from persisted NSO*State."""
    from .models import (
        NSOBGPPeerState,
        NSOBGPPeerTemplateState,
        NSOInterfaceState,
        NSOISISInstanceState,
        NSOISISInterfaceState,
        NSOOSPFInstanceState,
        NSOOSPFInterfaceState,
        NSORedistributionState,
        NSORoutePolicyState,
        NSOStaticRouteState,
    )

    dev_id = device.id
    if key == "interface":
        # Merged card: aggregate the four per-interface scalar overlays so the
        # headline count/badges cover everything shown in the consolidated table.
        from .models import NSOInterfaceIPState, NSOInterfaceMtuState, NSOSwitchportState

        out = {"total": 0, "drift": 0, "pending": 0}
        parts = [
            interface_status_breakdown(NSOInterfaceState.objects.filter(interface__device_id=dev_id)),
            _status_breakdown(NSOInterfaceIPState.objects.filter(interface__device_id=dev_id)),
            _status_breakdown(NSOInterfaceMtuState.objects.filter(interface__device_id=dev_id)),
            _status_breakdown(NSOSwitchportState.objects.filter(interface__device_id=dev_id)),
        ]
        for part in parts:
            for bucket in ("total", "drift", "pending"):
                out[bucket] += part.get(bucket, 0)
        return out
    if key == "interfaces":
        return interface_status_breakdown(NSOInterfaceState.objects.filter(interface__device_id=dev_id))
    if key == "interface_ips":
        from .models import NSOInterfaceIPState

        return _status_breakdown(NSOInterfaceIPState.objects.filter(interface__device_id=dev_id))
    if key == "bfd":
        # Read-only (no NSO*State overlay): just the count of BFD-configured interfaces.
        try:
            from netbox_routing.models import BFDInterface

            n = BFDInterface.objects.filter(interface__device_id=dev_id).count()
        except Exception:
            n = 0
        return {"total": n, "drift": 0, "pending": 0}
    if key == "isis":
        # interfaces + instances combined for the headline; expand shows both.
        ifaces = _status_breakdown(NSOISISInterfaceState.objects.filter(interface__device_id=dev_id))
        procs = NSOISISInstanceState.objects.filter(management=mgmt).count()
        ifaces["total"] = ifaces.get("total", 0) + procs
        ifaces["processes"] = procs
        return ifaces
    if key == "ospf":
        ifaces = _status_breakdown(NSOOSPFInterfaceState.objects.filter(interface__device_id=dev_id))
        insts = NSOOSPFInstanceState.objects.filter(management=mgmt).count()
        ifaces["total"] = ifaces.get("total", 0) + insts
        ifaces["instances"] = insts
        return ifaces
    if key == "static":
        return _status_breakdown(NSOStaticRouteState.objects.filter(management=mgmt))
    if key == "bgp":
        # Peers + peer-group templates share the BGP headline; expand shows both tables.
        out = _status_breakdown(NSOBGPPeerState.objects.filter(management=mgmt))
        tmpl = _status_breakdown(NSOBGPPeerTemplateState.objects.filter(management=mgmt))
        for bucket in ("total", "drift", "pending"):
            out[bucket] = out.get(bucket, 0) + tmpl.get(bucket, 0)
        out["templates"] = tmpl["total"]
        return out
    if key == "route_policy":
        return _status_breakdown(NSORoutePolicyState.objects.filter(management=mgmt))
    if key == "redistribution":
        return _status_breakdown(NSORedistributionState.objects.filter(management=mgmt))
    if key == "snmp":
        from .models import (
            NSOSnmpCommunityState,
            NSOSnmpHostState,
            NSOSnmpSystemInfoState,
            NSOSnmpV3UserState,
        )

        return _snmp_breakdown(
            (
                NSOSnmpCommunityState.objects.filter(management=mgmt),
                NSOSnmpV3UserState.objects.filter(management=mgmt),
                NSOSnmpHostState.objects.filter(management=mgmt),
                NSOSnmpSystemInfoState.objects.filter(management=mgmt),
            )
        )
    if key == "logging":
        from .models import NSOLoggingHostState, NSOLoggingLevelState

        return _snmp_breakdown(
            (
                NSOLoggingHostState.objects.filter(management=mgmt),
                NSOLoggingLevelState.objects.filter(management=mgmt),
            )
        )
    if key == "l2_services":
        from .models import NSOL2SapState

        return _status_breakdown(NSOL2SapState.objects.filter(management=mgmt))
    if key == "lacp":
        from .models import NSOLACPBundleState, NSOLACPMemberState

        # bundles + members combined for the headline; expand shows both.
        out = _status_breakdown(NSOLACPBundleState.objects.filter(management=mgmt))
        members = _status_breakdown(NSOLACPMemberState.objects.filter(management=mgmt))
        out["total"] = out.get("total", 0) + members.get("total", 0)
        out["drift"] = out.get("drift", 0) + members.get("drift", 0)
        out["pending"] = out.get("pending", 0) + members.get("pending", 0)
        out["members"] = members.get("total", 0)
        return out
    if key == "vlan":
        from .models import NSOVLANState

        return _status_breakdown(NSOVLANState.objects.filter(management=mgmt))
    if key == "switchport":
        from .models import NSOSwitchportState

        return _status_breakdown(NSOSwitchportState.objects.filter(management=mgmt))
    if key == "svi":
        from .models import NSOSVIState

        return _status_breakdown(NSOSVIState.objects.filter(management=mgmt))
    if key == "subinterface":
        from .models import NSOSubinterfaceState

        return _status_breakdown(NSOSubinterfaceState.objects.filter(management=mgmt))
    if key == "interface_mtu":
        from .models import NSOInterfaceMtuState

        return _status_breakdown(NSOInterfaceMtuState.objects.filter(management=mgmt))
    return {"total": 0}


def _snmp_breakdown(querysets) -> dict:
    """Status breakdown for SNMP overlays (no owned/accepted_at dimension).

    Like _status_breakdown, but the SNMP state models have no accepted_at field so
    rows are classified by status alone.
    """
    out = {"total": 0, "drift": 0, "pending": 0}
    for qs in querysets:
        for status, total in qs.values_list("status").annotate(total=Count("id")):
            out["total"] += total
            if status in ("deploying", "accepted", "apply_failed"):
                out["pending"] += total  # apply_failed is owned + retryable → pending, not drift
            elif status in _MATCH_STATUSES:
                pass  # in sync — the implicit remainder
            else:
                out["drift"] += total  # changed/conflict/error/unknown
    return out


# ── READSEM S4 (D10): per-category read chips from NSOFamilyReadState ──────────

_RS_AUTHORITATIVE_OUTCOMES = ("present", "absent_authoritative")
_RS_ADMIT_RESULTS = ("replaced", "cleared")

#: worst-first merge order for a category's family display states (D8 + the two
#: pending states). Healthy renders NO chip, so it never appears here.
_CHIP_SEVERITY = (
    "reset_pending",
    "unavailable",
    "unknown",
    "stale",
    "aged",
    "refresh_pending",
    "not_authoritative",
    "unsupported",
)

#: D10 render matrix — css classes only from the approved palette (never bg-secondary).
_CHIP_RENDER = {
    "reset_pending": {
        "css": "text-bg-warning text-dark",
        "label": "refresh pending — data reset",
        "tip": "The adapter's data store was rebuilt. Showing last-known state until the next reconcile adopts the new dataset.",
    },
    "unavailable": {
        "css": "text-bg-danger",
        "label": "NSO read unavailable — last-known data",
        "tip": "The newest declared read for this category failed (export down / read error / not ready) or its materialization errored. Rows show the last successful read.",
    },
    "unknown": {
        "css": "text-bg-danger",
        "label": "unknown read state",
        "tip": "The adapter reported a read state this plugin does not recognize — failing closed. Rows show last-known data.",
    },
    "stale": {
        "css": "text-bg-warning text-dark",
        "label": "showing last-known data",
        "tip": "The newest read succeeded but served an older cached snapshot (stale). Rows were still replaced from it.",
    },
    "aged": {
        "css": "text-bg-warning text-dark",
        "label": "showing last-known data",
        "tip": "The newest read succeeded but its snapshot is aged. Rows were still replaced from it.",
    },
    "refresh_pending": {
        "css": "text-bg-info",
        "label": "refresh pending",
        "tip": "A newer successful read was observed but its rows have not been applied yet. The next reconcile settles this.",
    },
    "not_authoritative": {
        "css": "border text-muted",
        "label": "no authoritative read",
        "tip": "The adapter has no authoritative read for this category on this device yet.",
    },
    "unsupported": {
        "css": "border text-muted",
        "label": "unsupported on this platform",
        "tip": "This device's NED does not support reading this category; nothing to mirror.",
    },
}


def _family_read_display(row, adopted_incarnation: str) -> str | None:
    """Collapse one NSOFamilyReadState row to its D10 display state (None = no chip).

    Legacy rows (blank outcome) and rows from a NON-adopted incarnation are ignored
    entirely (D3/D5). Healthy — fresh-present OR authoritative-empty — returns None:
    a successful clear must never render unknown despite its null freshness (R2-7).
    Healthy additionally requires the observation to be APPLIED: a newer unapplied
    observation renders refresh-pending, never healthy (R5-3).
    """
    if not row.observed_outcome:
        return None
    if adopted_incarnation and row.observed_incarnation != adopted_incarnation:
        return None
    outcome, reason = row.observed_outcome, row.observed_reason
    if outcome == "unavailable":
        if reason in ("not_authoritative", "unsupported"):
            return reason
        if reason in ("export_down", "read_error", "not_ready"):
            return "unavailable"
        return "unknown"
    if outcome in _RS_AUTHORITATIVE_OUTCOMES:
        if row.observed_result == "error":
            return "unavailable"  # materialization error — red (D10)
        if not (row.observed_succeeded is True and row.observed_result in _RS_ADMIT_RESULTS):
            return "unknown"  # inconsistent tuple — fail closed, visibly
        obs, applied = row.observed_attempt_id, row.applied_attempt_id
        if obs is not None and (applied is None or obs > applied):
            return "refresh_pending"
        if row.observed_freshness in ("stale", "aged"):
            return row.observed_freshness
        return None  # healthy
    return "unknown"


def category_read_chip(mgmt, key: str, rows_by_family: dict) -> dict | None:
    """Merge a category's family read states into ONE chip dict (None = no chip).

    Worst-first across the families the category DISPLAYS (families.CATEGORY_FAMILIES);
    a set reset-pending marker overrides everything device-wide (R11/R12 — old rows
    must never render healthy while a reset is pending).
    """
    from .families import CATEGORY_FAMILIES

    if mgmt.reset_pending_born is not None:
        state = "reset_pending"
    else:
        states = set()
        for family in CATEGORY_FAMILIES.get(key, ()):
            row = rows_by_family.get(family)
            if row is None:
                continue
            display = _family_read_display(row, mgmt.adapter_incarnation)
            if display:
                states.add(display)
        if not states:
            return None
        state = next(s for s in _CHIP_SEVERITY if s in states)
    return {"state": state, **_CHIP_RENDER[state]}


def category_summaries(device, mgmt) -> list[dict]:
    """Return the collapsed-view summary for every scope this device opted into.

    Read-only: cheap aggregate queries over persisted NSO*State.
    Each entry: {key, label, icon, counts:{...}, read: chip-dict|None} — ``read`` is
    the D10 per-category read chip from persisted NSOFamilyReadState rows.
    """
    from .models import NSOFamilyReadState

    if mgmt is None:
        return []
    rows_by_family = {r.family: r for r in NSOFamilyReadState.objects.filter(management=mgmt)}
    summaries = []
    for key, label, icon, flag in _CATEGORIES:
        # Routing leaves are also gated by the manage_routing master kill-switch;
        # standalone scopes (interfaces, SNMP, L2) are not. A tuple flag = any-of.
        flags = flag if isinstance(flag, tuple) else (flag,)
        if any(f not in _NON_ROUTING_FLAGS for f in flags) and not mgmt.manage_routing:
            continue
        if not any(getattr(mgmt, f, False) for f in flags):
            continue
        summaries.append(
            {
                "key": key,
                "label": label,
                "icon": icon,
                "counts": _category_counts(key, device, mgmt),
                "read": category_read_chip(mgmt, key, rows_by_family),
            }
        )
    return summaries
