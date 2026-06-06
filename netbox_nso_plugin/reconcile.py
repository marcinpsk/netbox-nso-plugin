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
        "logging_data": {},
        "static_routes": [],
        "isis_interfaces": [],
        "isis_processes": [],
        "route_policy_states": [],
        "ospf_data": {"instances": [], "interfaces": []},
        "redistribution_states": [],
        "bgp_peers": [],
        "bfd_interfaces": [],
        "interface_ips": [],
        "l2_sap_states": [],
    }


def _reconcile_routing(device, mgmt, client, ctx: dict) -> None:
    """Reconcile each opted-in routing protocol into *ctx* (gated by kill-switches)."""
    from .bfd_reconciler import reconcile_bfd
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
    if mgmt.manage_bgp:
        ctx["bgp_peers"] = _reconcile_bgp_config(device, client.get_bgp_config(dev_id))
    # BFD is interface-level + protocol-agnostic; reconcile it whenever any of the
    # protocols that ride it (BGP/IS-IS/OSPF) are managed.
    if mgmt.manage_bgp or mgmt.manage_isis or mgmt.manage_ospf:
        ctx["bfd_interfaces"] = reconcile_bfd(device, client.get_bfd(dev_id).get("interfaces", []))
    # Redistribution runs LAST: its destination is a netbox_routing OSPFInstance /
    # ISISInstance / BGPAddressFamily created by the protocol reconciles above, so
    # those must run first (BGP especially — BGP-dest redistribution needs its AF).
    if mgmt.manage_redistribution:
        ctx["redistribution_states"] = reconcile_redistribution(device, client.get_redistribution(dev_id))


def reconcile_device(device, mgmt=None) -> dict:
    """Fetch adapter state for *device* and reconcile each opted-in scope.

    Persists NSO*State rows as a side effect (suppressed so it never pushes intent),
    and returns the per-scope display structures the tab template consumes. Safe to
    call off the request path (background job, Refresh action). Raises AdapterError
    on adapter failure — the caller decides how to surface it.
    """
    from . import adapter_client as client
    from .signals import suppress_intent_push
    from .template_content import (
        _reconcile_interface_ips,
        _reconcile_logging_config,
        _reconcile_snmp_config,
        _upsert_interface_states,
    )

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
            ctx["state"] = client.get_state(dev_id)
            ctx["interface_states"] = _upsert_interface_states(device, ctx["interfaces"])
            # Import interface IP addresses onto their (now first-class, logical-named)
            # NetBox interfaces. Runs AFTER the adapter sync created the interfaces;
            # gated internally by interface_ip_auto_create (off → lands as pending).
            ctx["interface_ips"] = _reconcile_interface_ips(device, client.get_interface_ips(dev_id))
        if mgmt.manage_snmp:
            ctx["snmp_data"] = _reconcile_snmp_config(device, client.get_snmp_config(dev_id))
        if mgmt.manage_logging:
            ctx["logging_data"] = _reconcile_logging_config(device, client.get_logging_config(dev_id))
        _reconcile_routing(device, mgmt, client, ctx)
    return ctx


def reconcile_category(device, mgmt, key: str) -> dict:
    """Reconcile a SINGLE category and return its display context (for lazy expand).

    Runs only the requested category's reconciler(s), suppress-wrapped (no intent
    push). Used by the lazy-load endpoint when an operator expands one category on
    the tab, so the page render itself stays counts-only. Raises AdapterError on
    adapter failure — the caller renders a per-category error.
    """
    from . import adapter_client as client
    from .bgp_reconciler import _reconcile_bgp_config
    from .redistribution_reconciler import reconcile_redistribution
    from .route_policy_reconciler import reconcile_route_policy
    from .signals import suppress_intent_push
    from .template_content import (
        _reconcile_interface_ips,
        _reconcile_isis_interfaces,
        _reconcile_isis_process,
        _reconcile_logging_config,
        _reconcile_ospf,
        _reconcile_snmp_config,
        _reconcile_static_routes,
        _upsert_interface_states,
    )

    ctx = _empty_context()
    if mgmt is None or mgmt.adapter_device_id is None:
        return ctx
    dev_id = mgmt.adapter_device_id

    with suppress_intent_push():
        if key == "interfaces":
            ctx["interfaces"] = client.get_interfaces(dev_id)
            ctx["state"] = client.get_state(dev_id)
            ctx["interface_states"] = _upsert_interface_states(device, ctx["interfaces"])
            ctx["interface_ips"] = _reconcile_interface_ips(device, client.get_interface_ips(dev_id))
        elif key == "interface_ips":
            ctx["interface_ips"] = _reconcile_interface_ips(device, client.get_interface_ips(dev_id))
        elif key == "snmp":
            ctx["snmp_data"] = _reconcile_snmp_config(device, client.get_snmp_config(dev_id))
        elif key == "logging":
            ctx["logging_data"] = _reconcile_logging_config(device, client.get_logging_config(dev_id))
        elif key == "static":
            ctx["static_routes"] = _reconcile_static_routes(device, client.get_static_routes(dev_id))
        elif key == "isis":
            isis_payload = client.get_isis_interfaces(dev_id)
            ctx["isis_interfaces"] = _reconcile_isis_interfaces(device, isis_payload.get("interfaces", []))
            ctx["isis_processes"] = _reconcile_isis_process(device, isis_payload.get("processes", []))
        elif key == "ospf":
            ctx["ospf_data"] = _reconcile_ospf(device, client.get_ospf(dev_id))
        elif key == "bgp":
            ctx["bgp_peers"] = _reconcile_bgp_config(device, client.get_bgp_config(dev_id))
        elif key == "bfd":
            from .bfd_reconciler import reconcile_bfd

            ctx["bfd_interfaces"] = reconcile_bfd(device, client.get_bfd(dev_id).get("interfaces", []))
        elif key == "route_policy":
            ctx["route_policy_states"] = reconcile_route_policy(device, client.get_route_policy(dev_id))
        elif key == "redistribution":
            ctx["redistribution_states"] = reconcile_redistribution(device, client.get_redistribution(dev_id))
        elif key == "l2_services":
            # M37 P2a: reconcile into native vpn.L2VPN + L2VPNTermination + NSOL2SapState
            # (value-aware drift/accept). The dot1q tag stays per-SAP interface-local encap.
            from .l2_service_reconciler import reconcile_l2_services

            ctx["l2_sap_states"] = reconcile_l2_services(device, client.get_l2_services(dev_id))
    return ctx


# ── Off-request reconcile: RQ job fired by the adapter's sync-complete callback ──

_RECONCILE_QUEUE = "default"


def run_device_reconcile(device_id: int) -> dict:
    """RQ entrypoint: reconcile one device by NetBox device id, off the request path.

    Runs in the rqworker (no HTTP request), so suppress_intent_push() — not the
    GET-render guard — is what keeps the NSO*State writes from pushing intent back.
    AdapterError is swallowed: a transient adapter outage must not crash the worker.
    """
    from dcim.models import Device

    from .adapter_client import AdapterError

    try:
        device = Device.objects.get(pk=device_id)
    except Device.DoesNotExist:
        logger.warning("nso reconcile: device %s no longer exists; skipping", device_id)
        return {"device_id": device_id, "skipped": "device_gone"}

    try:
        ctx = reconcile_device(device)
    except AdapterError as exc:
        logger.warning("nso reconcile: adapter error for device %s: %s", device_id, exc)
        return {"device_id": device_id, "error": str(exc)}

    summary = {"device_id": device_id, "interface_states": len(ctx.get("interface_states") or {})}
    logger.info("nso reconcile complete: %s", summary)
    return summary


def enqueue_device_reconcile(device_id: int):
    """Enqueue a background reconcile for *device_id*, deduped per device.

    Uses a deterministic job id so 15-min adapter sync cycles across many devices
    don't pile up: if a reconcile for this device is already queued/running, the
    call is a no-op. Returns the (existing or new) RQ job, or None if RQ is absent.
    """
    try:
        import django_rq
        from rq.exceptions import NoSuchJobError
        from rq.job import Job as RqJob
    except ImportError:  # pragma: no cover - RQ ships with NetBox
        logger.warning("django_rq unavailable; running reconcile for device %s inline", device_id)
        run_device_reconcile(device_id)
        return None

    queue = django_rq.get_queue(_RECONCILE_QUEUE)
    job_id = f"nso-reconcile-{device_id}"
    try:
        existing = RqJob.fetch(job_id, connection=queue.connection)
    except NoSuchJobError:
        existing = None
    if existing is not None:
        if existing.get_status(refresh=True) in ("queued", "started", "deferred", "scheduled"):
            return existing  # already pending — don't pile up
        existing.delete()  # finished/failed: clear so the id can be reused
    return queue.enqueue(run_device_reconcile, device_id, job_id=job_id, result_ttl=300, job_timeout=600)
