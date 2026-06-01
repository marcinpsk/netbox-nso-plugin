# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile adapter state into the plugin's NSO*State tables (the write path).

Extracted from the device NSO tab render so the same logic can run OFF the request
path — in a background job (fired by the adapter's sync-complete callback) and from
the manual Refresh actions — not only when an operator happens to open the tab.

Every reconcile runs inside ``suppress_intent_push()`` so that mirroring adapter
state into NSO*State never pushes intent back to the adapter (those writes are an
import, not an operator accept). See signals._skip_on_render / suppress_intent_push.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _empty_context() -> dict:
    """Default (no-op) reconcile result — also the read-only fallback shape."""
    return {
        "interfaces": None,
        "compliance": None,
        "interface_states": {},
        "snmp_data": {},
        "static_routes": [],
        "isis_interfaces": [],
        "isis_processes": [],
        "route_policy_states": [],
        "ospf_data": {"instances": [], "interfaces": []},
        "redistribution_states": [],
        "bgp_peers": [],
    }


def _reconcile_routing(device, mgmt, client, ctx: dict) -> None:
    """Reconcile each opted-in routing protocol into *ctx* (gated by kill-switches)."""
    from .bgp_reconciler import _reconcile_bgp_config
    from .redistribution_reconciler import reconcile_redistribution
    from .route_policy_reconciler import reconcile_route_policy
    from .template_content import (
        _reconcile_isis_interfaces,
        _reconcile_isis_process,
        _reconcile_ospf,
        _reconcile_static_routes,
    )

    if not mgmt.manage_routing:
        return
    dev_id = mgmt.adapter_device_id

    if mgmt.manage_static:
        ctx["static_routes"] = _reconcile_static_routes(device, client.get_static_routes(dev_id))
    if mgmt.manage_isis:
        isis_payload = client.get_isis_interfaces(dev_id)
        ctx["isis_interfaces"] = _reconcile_isis_interfaces(device, isis_payload.get("interfaces", []))
        ctx["isis_processes"] = _reconcile_isis_process(device, isis_payload.get("processes", []))
    if mgmt.manage_route_policy:
        ctx["route_policy_states"] = reconcile_route_policy(device, client.get_route_policy(dev_id))
    if mgmt.manage_ospf:
        ctx["ospf_data"] = _reconcile_ospf(device, client.get_ospf(dev_id))
    if mgmt.manage_redistribution:
        ctx["redistribution_states"] = reconcile_redistribution(device, client.get_redistribution(dev_id))
    if mgmt.manage_bgp:
        ctx["bgp_peers"] = _reconcile_bgp_config(device, client.get_bgp_config(dev_id))


def reconcile_device(device, mgmt=None) -> dict:
    """Fetch adapter state for *device* and reconcile each opted-in scope.

    Persists NSO*State rows as a side effect (suppressed so it never pushes intent),
    and returns the per-scope display structures the tab template consumes. Safe to
    call off the request path (background job, Refresh action). Raises AdapterError
    on adapter failure — the caller decides how to surface it.
    """
    from . import adapter_client as client
    from .signals import suppress_intent_push
    from .template_content import _reconcile_snmp_config, _upsert_interface_states

    ctx = _empty_context()
    if mgmt is None:
        try:
            mgmt = device.nso_management
        except Exception:
            return ctx
    if mgmt is None or mgmt.adapter_device_id is None:
        return ctx

    dev_id = mgmt.adapter_device_id
    with suppress_intent_push():
        if mgmt.manage_interfaces:
            ctx["interfaces"] = client.get_interfaces(dev_id)
            ctx["compliance"] = client.get_compliance(dev_id)
            ctx["interface_states"] = _upsert_interface_states(device, ctx["interfaces"])
        if mgmt.manage_snmp:
            ctx["snmp_data"] = _reconcile_snmp_config(device, client.get_snmp_config(dev_id))
        _reconcile_routing(device, mgmt, client, ctx)
    return ctx
