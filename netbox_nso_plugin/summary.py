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

# The display state is TWO independent dimensions:
#   sync     — does the device match NetBox?  match (imported/in_sync) vs differ
#   owned    — is NetBox the source of truth?  owned == accepted_at is not None
# Combined into the operator-facing buckets:
#   in sync       = device matches NetBox (whether owned or not)
#   drift         = device differs AND NetBox does NOT own it (device changed out-of-band)
#   pending apply = device differs AND NetBox owns it (Apply will push NetBox's value)
_MATCH_STATUSES = ("imported", "in_sync")
_DIFFER_STATUSES = ("changed", "drifted", "conflict", "accepted", "apply_failed")


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
    owned = st.accepted_at is not None
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
_CATEGORIES = [
    ("interfaces", "Interfaces", "ethernet", "manage_interfaces"),
    ("interface_ips", "Interface IPs", "ip-network", "manage_interfaces"),
    ("static", "Static Routes", "sign-direction", "manage_static"),
    ("isis", "IS-IS", "lan", "manage_isis"),
    ("ospf", "OSPF", "lan", "manage_ospf"),
    ("bgp", "BGP", "router-network", "manage_bgp"),
    ("bfd", "BFD", "pulse", "manage_isis"),
    ("route_policy", "Route Policy", "script-text", "manage_route_policy"),
    ("redistribution", "Redistribution", "swap-horizontal", "manage_redistribution"),
    ("snmp", "SNMP", "console-network", "manage_snmp"),
    ("logging", "Logging", "file-document-outline", "manage_logging"),
]

# Scopes that stand alone (not under the manage_routing master kill-switch).
_NON_ROUTING_FLAGS = {"manage_interfaces", "manage_snmp", "manage_logging"}


def _status_breakdown(qs) -> dict:
    """Return owned-aware {total, drift, pending} buckets for a state queryset.

    Owned = accepted_at set. differ + owned → pending apply; differ + not-owned → drift;
    match (imported/in_sync) → in sync (the implicit remainder). A row left at
    ``unknown`` (or any unrecognized status) is an *anomaly* — reconcilers always set a
    concrete status — so it is surfaced under drift (needs attention) rather than hidden
    in the in-sync remainder, where it would read as a false "in sync".
    """
    from django.db.models import Q

    rows = qs.values_list("status").annotate(
        total=Count("id"),
        owned=Count("id", filter=Q(accepted_at__isnull=False)),
    )
    out = {"total": 0, "drift": 0, "pending": 0}
    for status, total, owned in rows:
        out["total"] += total
        if status == "deploying":
            out["pending"] += total
        elif status in _DIFFER_STATUSES:
            out["pending"] += owned
            out["drift"] += total - owned
        elif status in _MATCH_STATUSES:
            pass  # in sync — the implicit remainder
        else:
            out["drift"] += total  # unknown/unrecognized → surface, don't hide
    return out


def _category_counts(key: str, device, mgmt) -> dict:
    """Compute the count breakdown for one category from persisted NSO*State."""
    from .models import (
        NSOBGPPeerState,
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
        return _status_breakdown(NSOBGPPeerState.objects.filter(management=mgmt))
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
        from .models import NSOLoggingHostState

        return _snmp_breakdown((NSOLoggingHostState.objects.filter(management=mgmt),))
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
            if status in ("deploying", "accepted"):
                out["pending"] += total
            elif status in _MATCH_STATUSES:
                pass  # in sync — the implicit remainder
            else:
                out["drift"] += total  # changed/conflict/apply_failed/error/unknown
    return out


def _l2_service_count(mgmt) -> dict:
    """Count Nokia L2 SAPs for the tile via a defensive adapter call (M37 P1).

    L2 services are display-only (no NSO*State overlay, no manage_* flag), so the
    count can't come from the DB like other categories — it's fetched live from the
    adapter and degrades to 0 on any error so the tab never breaks. Mirrors how BFD
    counts from outside NSO*State.
    """
    if mgmt is None or mgmt.adapter_device_id is None:
        return {"total": 0, "drift": 0, "pending": 0}
    try:
        from . import adapter_client as client

        services = client.get_l2_services(mgmt.adapter_device_id).get("services", [])
    except Exception:
        return {"total": 0, "drift": 0, "pending": 0}
    total = sum(len(s.get("saps", [])) for s in services)
    return {"total": total, "drift": 0, "pending": 0}


def category_summaries(device, mgmt) -> list[dict]:
    """Return the collapsed-view summary for every scope this device opted into.

    Read-only: cheap aggregate queries over persisted NSO*State. The one exception is
    the L2-services tile (M37 P1, display-only) whose count is a guarded live adapter
    call — see _l2_service_count. Each entry: {key, label, icon, counts:{...,'total':N}}.
    """
    if mgmt is None:
        return []
    summaries = []
    for key, label, icon, flag in _CATEGORIES:
        # Routing leaves are also gated by the manage_routing master kill-switch;
        # standalone scopes (interfaces, SNMP) are not.
        if flag not in _NON_ROUTING_FLAGS and not mgmt.manage_routing:
            continue
        if not getattr(mgmt, flag, False):
            continue
        summaries.append({"key": key, "label": label, "icon": icon, "counts": _category_counts(key, device, mgmt)})
    # L2 services (Nokia epipe/vpls) — no overlay/flag; show the tile only when the
    # adapter actually reports SAPs, so non-Nokia / un-synced devices get no empty tile.
    l2 = _l2_service_count(mgmt)
    if l2["total"]:
        summaries.append({"key": "l2_services", "label": "L2 Services", "icon": "lan-connect", "counts": l2})
    return summaries
