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

# Each category: key -> (label, mdi-icon, scope-flag on NSODeviceManagement).
# Order here is the display order on the tab.
# NOTE: SNMP is intentionally absent — the device tab has never rendered an SNMP
# section, so there is no partial for it.
_CATEGORIES = [
    ("interfaces", "Interfaces", "ethernet", "manage_interfaces"),
    ("static", "Static Routes", "sign-direction", "manage_static"),
    ("isis", "IS-IS", "lan", "manage_isis"),
    ("ospf", "OSPF", "lan", "manage_ospf"),
    ("bgp", "BGP", "router-network", "manage_bgp"),
    ("route_policy", "Route Policy", "script-text", "manage_route_policy"),
    ("redistribution", "Redistribution", "swap-horizontal", "manage_redistribution"),
]


def _status_breakdown(qs) -> dict:
    """Return {status: count} for a queryset, plus 'total'."""
    rows = qs.values_list("status").annotate(n=Count("id"))
    by_status = {s: n for s, n in rows}
    by_status["total"] = sum(by_status.values())
    return by_status


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
        return _status_breakdown(NSOInterfaceState.objects.filter(interface__device_id=dev_id))
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
    return {"total": 0}


def category_summaries(device, mgmt) -> list[dict]:
    """Return the collapsed-view summary for every scope this device opted into.

    Read-only: cheap aggregate queries over persisted NSO*State, no adapter calls.
    Each entry: {key, label, icon, counts:{status:n,'total':N,...}}.
    """
    if mgmt is None:
        return []
    summaries = []
    for key, label, icon, flag in _CATEGORIES:
        # Routing leaves are also gated by the manage_routing master kill-switch.
        if flag != "manage_interfaces" and not mgmt.manage_routing:
            continue
        if not getattr(mgmt, flag, False):
            continue
        summaries.append({"key": key, "label": label, "icon": icon, "counts": _category_counts(key, device, mgmt)})
    return summaries
