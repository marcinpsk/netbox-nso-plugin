# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Read-only category counts and execution evidence for the device NSO tab."""

from __future__ import annotations

from .families import AGGREGATION_ORDER


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
    """Read the common owner projection with the current interface values."""
    from .refusal_display import row_projection

    projected = row_projection(st, iface)
    return projected["state"], projected["label"], projected["owned"]


def interface_status_breakdown(qs) -> dict:
    """Count interface owners through the common projection."""
    return _status_breakdown(qs.select_related("interface"))


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
    """Count the same owner buckets used by REST, grids and Apply preview."""
    from .refusal_display import project_rows, row_projection

    out = {"total": 0, "drift": 0, "pending": 0}
    for row in project_rows(qs):
        projected = row_projection(row)
        out["total"] += 1
        if projected["bucket"] in out:
            out[projected["bucket"]] += 1
    return out


def _category_counts(key: str, device, mgmt) -> dict:  # noqa: C901
    """Compute the count breakdown for one category from persisted NSO*State."""
    from .models import (
        NSOBFDInterfaceState,
        NSOBGPPeerState,
        NSOBGPPeerTemplateState,
        NSOBGPRouterState,
        NSOInterfaceState,
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
        return _status_breakdown(NSOBFDInterfaceState.objects.filter(management=mgmt))
    if key == "isis":
        from .refusal_registry import document_owner_specs

        out = {"total": 0, "drift": 0, "pending": 0}
        for spec in document_owner_specs(key):
            counts = _status_breakdown(spec.model.objects.filter(**{spec.device_lookup: dev_id}))
            for bucket in ("total", "drift", "pending"):
                out[bucket] += counts[bucket]
            if spec.model._meta.model_name == "nsoisisinstancestate":
                out["processes"] = counts["total"]
        return out
    if key == "ospf":
        ifaces = _status_breakdown(NSOOSPFInterfaceState.objects.filter(interface__device_id=dev_id))
        insts = _status_breakdown(NSOOSPFInstanceState.objects.filter(management=mgmt))
        for bucket, total in insts.items():
            ifaces[bucket] += total
        ifaces["instances"] = insts["total"]
        return ifaces
    if key == "static":
        return _status_breakdown(NSOStaticRouteState.objects.filter(management=mgmt))
    if key == "bgp":
        # Peers + peer-group templates share the BGP headline; expand shows both tables.
        out = _status_breakdown(NSOBGPPeerState.objects.filter(management=mgmt))
        tmpl = _status_breakdown(NSOBGPPeerTemplateState.objects.filter(management=mgmt))
        for bucket in ("total", "drift", "pending"):
            out[bucket] = out.get(bucket, 0) + tmpl.get(bucket, 0)
        routers = _status_breakdown(NSOBGPRouterState.objects.filter(management=mgmt))
        for bucket in ("total", "drift", "pending"):
            out[bucket] = out.get(bucket, 0) + routers.get(bucket, 0)
        out["routers"] = routers["total"]
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
        for bucket, total in _status_breakdown(qs).items():
            out[bucket] += total
    return out


# ── READSEM S4 (D10): per-category read chips from NSOFamilyReadState ──────────

_RS_AUTHORITATIVE_OUTCOMES = ("present", "absent_authoritative")
_RS_ADMIT_RESULTS = ("replaced", "cleared")

#: worst-first merge order for a category's family display states (D8 + the two
#: pending states). Healthy renders NO chip, so it never appears here.
_CHIP_SEVERITY = tuple(state for state in AGGREGATION_ORDER if state != "healthy")

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
    "not_covered": {
        "css": "text-bg-danger",
        "label": "NSO collections unavailable, last-known data",
        "tip": "NSO did not report every collection for this category. The plugin keeps the last certified data and does not import from this read.",
    },
    "unknown": {
        "css": "text-bg-danger",
        "label": "unknown read state",
        "tip": "The adapter reported a read state this plugin does not recognize — failing closed. Rows show last-known data.",
    },
    "unproven": {
        "css": "text-bg-warning text-dark",
        "label": "payload revision unproven",
        "tip": "The adapter served a compatibility publication without an atomic payload revision. Rows were applied, but payload/pointer coherence cannot be proven until a revisioned read succeeds.",
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
        if reason in ("not_authoritative", "unsupported", "not_covered"):
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
        if row.applied_publication_sequence != row.publication_sequence or (
            obs is not None and (applied is None or obs > applied)
        ):
            return "refresh_pending"
        if row.applied_payload_revision is None:
            return "unproven"
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

    if mgmt.source_rekey_pending or mgmt.reset_pending_born is not None or mgmt.reset_pending_source_epoch is not None:
        state = "reset_pending"
    else:
        states = set()
        for family in CATEGORY_FAMILIES.get(key, ()):
            row = rows_by_family.get(family)
            if row is None:
                # A family with NO read-state row at ALL on an ADOPTED device (e.g.
                # pre-S4 overlay data right after the 0014 upgrade) must not render
                # healthy — the adapter has never declared a read for it (codex
                # B5-R2-3). A never-adopted device stays chip-free (pre-S4 render).
                if mgmt.adapter_incarnation:
                    states.add("not_authoritative")
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


def execution_summaries(management):
    from .refusal_display import device_execution_summaries

    return device_execution_summaries(management)
