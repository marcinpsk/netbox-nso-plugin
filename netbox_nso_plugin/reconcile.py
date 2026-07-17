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
        "lacp_bundle_states": [],
        "vlan_states": [],
        "switchport_states": [],
    }


def _mark_scope_error(mgmt, model_names: tuple[str, ...]) -> None:
    """Flip a scope's unowned overlay rows to ``error`` after a reconcile fault.

    Owned rows (accepted/deploying/in_sync/apply_failed) are preserved by
    ``status_machine.on_reconcile_error`` — a crash in the read path must never drop
    operator ownership. The next successful reconcile recovers the errored rows.
    """
    from . import models
    from . import status_machine as sm

    for name in model_names:
        model = getattr(models, name)
        for row in model.objects.filter(management=mgmt):
            new_status = sm.on_reconcile_error(row.status)
            if new_status != row.status:
                row.status = new_status
                row.save(update_fields=["status"])


def _safe_reconcile(ctx: dict, key: str, mgmt, model_names: tuple[str, ...], fn, *args) -> None:
    """Run one reconciler, storing its result in ``ctx[key]``; isolate its failures.

    ``AdapterError`` is never caught here — it is raised while *fetching* the payload
    (before ``fn`` runs) and is handled by the caller as a whole-device transient. Any
    other exception is a genuine reconcile fault: the scope's rows are flipped to
    ``error`` (owned rows preserved) so the failure is visible, ``ctx[key]`` keeps its
    empty default, and the remaining scopes still reconcile instead of the whole device
    sync — and the worker — dying on one bad payload.
    """
    from .adapter_client import AdapterError

    try:
        ctx[key] = fn(*args)
    except AdapterError:
        raise
    except Exception:  # noqa: BLE001 — isolate a faulty scope; mark it + keep going
        logger.exception("nso reconcile: %s failed; marking %s rows error", fn.__name__, ",".join(model_names))
        _mark_scope_error(mgmt, model_names)


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
        _safe_reconcile(
            ctx,
            "static_routes",
            mgmt,
            ("NSOStaticRouteState",),
            _reconcile_static_routes,
            device,
            client.get_static_routes(dev_id),
        )
    if mgmt.manage_isis:
        isis_payload = client.get_isis_interfaces(dev_id)
        _safe_reconcile(
            ctx,
            "isis_interfaces",
            mgmt,
            ("NSOISISInterfaceState",),
            _reconcile_isis_interfaces,
            device,
            isis_payload.get("interfaces", []),
        )
        _safe_reconcile(
            ctx,
            "isis_processes",
            mgmt,
            ("NSOISISInstanceState",),
            _reconcile_isis_process,
            device,
            isis_payload.get("processes", []),
        )
    if mgmt.manage_route_policy:
        _safe_reconcile(
            ctx,
            "route_policy_states",
            mgmt,
            ("NSORoutePolicyState",),
            reconcile_route_policy,
            device,
            client.get_route_policy(dev_id),
        )
    if mgmt.manage_ospf:
        _safe_reconcile(
            ctx,
            "ospf_data",
            mgmt,
            ("NSOOSPFInstanceState", "NSOOSPFInterfaceState"),
            _reconcile_ospf,
            device,
            client.get_ospf(dev_id),
        )
    if mgmt.manage_bgp:
        _safe_reconcile(
            ctx,
            "bgp_peers",
            mgmt,
            ("NSOBGPPeerState",),
            _reconcile_bgp_config,
            device,
            client.get_bgp_config(dev_id),
        )
    # BFD is interface-level + protocol-agnostic; reconcile it whenever any of the
    # protocols that ride it (BGP/IS-IS/OSPF) are managed.
    if mgmt.manage_bgp or mgmt.manage_isis or mgmt.manage_ospf:
        _safe_reconcile(
            ctx,
            "bfd_interfaces",
            mgmt,
            ("NSOBFDInterfaceState",),
            reconcile_bfd,
            device,
            client.get_bfd(dev_id).get("interfaces", []),
        )
        from .models import NSOBFDInterfaceState

        ctx["bfd_states"] = list(
            NSOBFDInterfaceState.objects.filter(management__device=device).select_related("interface")
        )
    # Redistribution runs LAST: its destination is a netbox_routing OSPFInstance /
    # ISISInstance / BGPAddressFamily created by the protocol reconciles above, so
    # those must run first (BGP especially — BGP-dest redistribution needs its AF).
    if mgmt.manage_redistribution:
        _safe_reconcile(
            ctx,
            "redistribution_states",
            mgmt,
            ("NSORedistributionState",),
            reconcile_redistribution,
            device,
            client.get_redistribution(dev_id),
        )


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
            _safe_reconcile(
                ctx,
                "interface_states",
                mgmt,
                ("NSOInterfaceState",),
                _upsert_interface_states,
                device,
                ctx["interfaces"],
            )
            # materialise SVIs/IRBs (virtual interfaces + VLAN link) BEFORE the IP
            # reconcile, which only attaches IPs to interfaces that already exist —
            # otherwise an SVI's IPs are dropped until the next refresh.
            from .svi_reconciler import reconcile_svi

            _safe_reconcile(ctx, "svi_states", mgmt, ("NSOSVIState",), reconcile_svi, device, client.get_svi(dev_id))
            # materialise dot1q subinterfaces (virtual interface + Interface.parent
            # link) BEFORE the IP reconcile, for the same ordering reason as SVIs.
            from .subinterface_reconciler import reconcile_subinterface

            _safe_reconcile(
                ctx,
                "subinterface_states",
                mgmt,
                ("NSOSubinterfaceState",),
                reconcile_subinterface,
                device,
                client.get_subinterface(dev_id),
            )
            # Phase 2b: per-interface MTU read mirror (read-only display).
            from .interface_mtu_reconciler import reconcile_interface_mtu

            _safe_reconcile(
                ctx,
                "interface_mtu_states",
                mgmt,
                ("NSOInterfaceMtuState",),
                reconcile_interface_mtu,
                device,
                client.get_interface_mtu(dev_id),
            )
            # Import interface IP addresses onto their (now first-class, logical-named)
            # NetBox interfaces. Runs AFTER the adapter sync created the interfaces;
            # gated internally by interface_ip_auto_create (off → lands as pending).
            _safe_reconcile(
                ctx,
                "interface_ips",
                mgmt,
                ("NSOInterfaceIPState",),
                _reconcile_interface_ips,
                device,
                client.get_interface_ips(dev_id),
            )
            # LACP/LAG bundle + member overlay states (interface-level).
            from .lacp_reconciler import reconcile_lag_config

            _safe_reconcile(
                ctx,
                "lacp_bundle_states",
                mgmt,
                ("NSOLACPBundleState", "NSOLACPMemberState"),
                reconcile_lag_config,
                device,
                client.get_lag_config(dev_id),
            )
            # VLAN database + L2 switchport (VLAN DB first — switchport links to it).
            from .vlan_reconciler import reconcile_switchport, reconcile_vlan_database

            _safe_reconcile(
                ctx,
                "vlan_states",
                mgmt,
                ("NSOVLANState",),
                reconcile_vlan_database,
                device,
                client.get_vlan_database(dev_id),
            )
            _safe_reconcile(
                ctx,
                "switchport_states",
                mgmt,
                ("NSOSwitchportState",),
                reconcile_switchport,
                device,
                client.get_switchport(dev_id),
            )
        if mgmt.manage_snmp:
            _safe_reconcile(
                ctx,
                "snmp_data",
                mgmt,
                ("NSOSnmpCommunityState", "NSOSnmpV3UserState", "NSOSnmpHostState", "NSOSnmpSystemInfoState"),
                _reconcile_snmp_config,
                device,
                client.get_snmp_config(dev_id),
            )
        if mgmt.manage_logging:
            _safe_reconcile(
                ctx,
                "logging_data",
                mgmt,
                ("NSOLoggingHostState",),
                _reconcile_logging_config,
                device,
                client.get_logging_config(dev_id),
            )
        if getattr(mgmt, "manage_l2", False):
            # Nokia L2 SAP overlays. Kept in the full reconcile (not just
            # on-expand) so the periodic sync-complete refresh keeps them current —
            # the tab reads these persisted rows without reconciling on expand.
            from .l2_service_reconciler import reconcile_l2_services

            _safe_reconcile(
                ctx,
                "l2_sap_states",
                mgmt,
                ("NSOL2SapState",),
                reconcile_l2_services,
                device,
                client.get_l2_services(dev_id),
            )
        _reconcile_routing(device, mgmt, client, ctx)
    return ctx


def reconcile_category(device, mgmt, key: str) -> dict:  # noqa: C901
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
        if key == "interface":
            # Merged "Interfaces" card: refresh all four per-interface scalar
            # overlays (enabled/description, IPs, MTU, switchport) so the
            # consolidated row-per-interface table reflects the latest device read.
            from .interface_mtu_reconciler import reconcile_interface_mtu
            from .subinterface_reconciler import reconcile_subinterface
            from .svi_reconciler import reconcile_svi
            from .vlan_reconciler import reconcile_switchport, reconcile_vlan_database

            ctx["interfaces"] = client.get_interfaces(dev_id)
            ctx["state"] = client.get_state(dev_id)
            ctx["interface_states"] = _upsert_interface_states(device, ctx["interfaces"])
            ctx["svi_states"] = reconcile_svi(device, client.get_svi(dev_id))  # before IPs
            ctx["subinterface_states"] = reconcile_subinterface(device, client.get_subinterface(dev_id))
            ctx["interface_ips"] = _reconcile_interface_ips(device, client.get_interface_ips(dev_id))
            ctx["interface_mtu_states"] = reconcile_interface_mtu(device, client.get_interface_mtu(dev_id))
            # VLAN DB first so switchport vid lookups resolve in the per-device group.
            reconcile_vlan_database(device, client.get_vlan_database(dev_id))
            ctx["switchport_states"] = reconcile_switchport(device, client.get_switchport(dev_id))
        elif key == "interfaces":
            ctx["interfaces"] = client.get_interfaces(dev_id)
            ctx["state"] = client.get_state(dev_id)
            ctx["interface_states"] = _upsert_interface_states(device, ctx["interfaces"])
            from .svi_reconciler import reconcile_svi

            ctx["svi_states"] = reconcile_svi(device, client.get_svi(dev_id))  # before IPs
            from .subinterface_reconciler import reconcile_subinterface

            ctx["subinterface_states"] = reconcile_subinterface(device, client.get_subinterface(dev_id))
            ctx["interface_ips"] = _reconcile_interface_ips(device, client.get_interface_ips(dev_id))
        elif key == "interface_ips":
            from .subinterface_reconciler import reconcile_subinterface
            from .svi_reconciler import reconcile_svi

            ctx["svi_states"] = reconcile_svi(device, client.get_svi(dev_id))  # SVIs exist before IPs
            ctx["subinterface_states"] = reconcile_subinterface(device, client.get_subinterface(dev_id))
            ctx["interface_ips"] = _reconcile_interface_ips(device, client.get_interface_ips(dev_id))
        elif key == "lacp":
            from .lacp_reconciler import reconcile_lag_config

            ctx["lacp_bundle_states"] = reconcile_lag_config(device, client.get_lag_config(dev_id))
        elif key == "vlan":
            from .vlan_reconciler import reconcile_vlan_database

            ctx["vlan_states"] = reconcile_vlan_database(device, client.get_vlan_database(dev_id))
        elif key == "switchport":
            from .vlan_reconciler import reconcile_switchport, reconcile_vlan_database

            # VLAN DB first so switchport vid lookups resolve in the per-device group.
            reconcile_vlan_database(device, client.get_vlan_database(dev_id))
            ctx["switchport_states"] = reconcile_switchport(device, client.get_switchport(dev_id))
        elif key == "svi":
            from .svi_reconciler import reconcile_svi

            ctx["svi_states"] = reconcile_svi(device, client.get_svi(dev_id))
        elif key == "subinterface":
            from .subinterface_reconciler import reconcile_subinterface

            ctx["subinterface_states"] = reconcile_subinterface(device, client.get_subinterface(dev_id))
        elif key == "interface_mtu":
            from .interface_mtu_reconciler import reconcile_interface_mtu

            ctx["interface_mtu_states"] = reconcile_interface_mtu(device, client.get_interface_mtu(dev_id))
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
            from .models import NSOBGPPeerTemplateState

            ctx["bgp_peers"] = _reconcile_bgp_config(device, client.get_bgp_config(dev_id))
            ctx["bgp_peer_templates"] = list(
                NSOBGPPeerTemplateState.objects.filter(management=mgmt).select_related("template")
            )
        elif key == "bfd":
            from .bfd_reconciler import reconcile_bfd
            from .models import NSOBFDInterfaceState

            ctx["bfd_interfaces"] = reconcile_bfd(device, client.get_bfd(dev_id).get("interfaces", []))
            ctx["bfd_states"] = list(
                NSOBFDInterfaceState.objects.filter(management__device=device).select_related("interface")
            )
        elif key == "route_policy":
            ctx["route_policy_states"] = reconcile_route_policy(device, client.get_route_policy(dev_id))
        elif key == "redistribution":
            ctx["redistribution_states"] = reconcile_redistribution(device, client.get_redistribution(dev_id))
        elif key == "l2_services":
            # reconcile into native vpn.L2VPN + L2VPNTermination + NSOL2SapState
            # (value-aware drift/accept). The dot1q tag stays per-SAP interface-local encap.
            from .l2_service_reconciler import reconcile_l2_services

            ctx["l2_sap_states"] = reconcile_l2_services(device, client.get_l2_services(dev_id))
    return ctx


# ── Off-request reconcile: RQ job fired by the adapter's sync-complete callback ──

_RECONCILE_QUEUE = "default"

# Scopes that move owned rows accepted→deploying on Apply (views._prepare_apply), so a
# stuck 'deploying' is the failure signal. scope key → NSO*State model attribute name.
_APPLY_DEPLOYING_SCOPES = {
    "vlan": "NSOVLANState",
    "svi": "NSOSVIState",
    "subinterface": "NSOSubinterfaceState",
    "bfd": "NSOBFDInterfaceState",
    "interface_mtu": "NSOInterfaceMtuState",
    "route_policy": "NSORoutePolicyState",
    "static_route": "NSOStaticRouteState",
    "l2_sap": "NSOL2SapState",
}


_GENERIC_APPLY_ERROR = "Apply reported a failure for this scope (see the adapter apply job)."


def _scope_failure_messages(job: dict | None, scope: str) -> str:
    """Join the real per-item error messages for *scope* from a failed apply job.

    The adapter records each failed item under ``job.error.detail.items`` as
    ``{"type": <scope>, "error": <message>, ...}`` (jobs API exposes ``error``).
    Returns a de-duplicated, human-readable string, or "" when none are present.
    """
    if not job:
        return ""
    items = (((job.get("error") or {}).get("detail") or {}).get("items")) or []
    msgs: list[str] = []
    for it in items:
        if not isinstance(it, dict) or it.get("type") != scope:
            continue
        name = it.get("name") or it.get("interface") or it.get("attribute") or ""
        err = str(it.get("error") or "").strip()
        if not err:
            continue
        msg = f"{name}: {err}" if name else err
        if msg not in msgs:
            msgs.append(msg)
    return "; ".join(msgs)


def _settle_apply_failures(mgmt, apply_result: dict | None, job: dict | None = None) -> None:
    """Mark rows still 'deploying' in a scope whose apply reported failures → apply_failed.

    Called AFTER the post-sync reconcile, so rows whose apply succeeded have already
    settled deploying→in_sync (the device reflects them). Any row still 'deploying' in a
    scope the apply job counted as failed is therefore a genuine failure — convert it via
    on_apply_result so it's no longer stuck, with last_apply_error for the operator. The
    next reconcile recovers it to in_sync (device caught up) or re-pends to accepted.

    ``job`` (the full apply job, optional) carries the per-item commit errors under
    ``job.error.detail.items``; when present, last_apply_error records the REAL device
    rejection reason instead of a generic pointer.
    """
    if not apply_result:
        return
    from . import models
    from . import status_machine as sm

    for scope, model_name in _APPLY_DEPLOYING_SCOPES.items():
        counts = apply_result.get(f"{scope}_count_by_outcome") or {}
        if (counts.get("apply_failed") or 0) <= 0:
            continue
        detail = _scope_failure_messages(job, scope) or _GENERIC_APPLY_ERROR
        model = getattr(models, model_name)
        for row in model.objects.filter(management=mgmt, status="deploying"):
            new_status = sm.on_apply_result(row.status, ok=False)
            if new_status != row.status:
                row.status = new_status
                row.last_apply_error = detail
                row.save(update_fields=["status", "last_apply_error"])


_STUCK_DEPLOYING_ERROR = (
    "Apply job #{job_id} reported success, but the device never showed this value on any "
    "later sync — the NED or service writer most likely dropped it silently (the adapter's "
    "post-apply verify diffs against NSO's CDB, which a silent drop leaves clean). Check "
    "device/NED support for this construct, then re-apply; the value is still safe in NetBox."
)


def _stuck_deploying_grace():
    """How long a 'deploying' row may outlive a SUCCEEDED apply before it's a silent drop.

    Must comfortably cover the apply-triggered refresh + the follow-up sync/reconcile
    round-trip, so a row that DID land has had every chance to value-match settle first.
    Configurable: PLUGINS_CONFIG["netbox_nso_plugin"]["stuck_deploying_grace_minutes"].
    """
    from datetime import timedelta

    from django.conf import settings as dj_settings

    cfg = dj_settings.PLUGINS_CONFIG.get("netbox_nso_plugin", {})
    return timedelta(minutes=cfg.get("stuck_deploying_grace_minutes", 10))


def _parse_adapter_ts(value):
    """Parse an adapter job timestamp (naive-UTC isoformat + 'Z') to an aware datetime."""
    from datetime import UTC, datetime

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _escalate_stuck_deploying(mgmt, job: dict | None) -> None:
    """Rows still 'deploying' long after a SUCCEEDED apply → apply_failed (silent drop, #26).

    The adapter's post-apply verify re-issues the committed payload as a native dry-run,
    which diffs against NSO's CDB — a writer/NED that silently dropped a value leaves the
    CDB service tree matching the payload, so that verify passes and the job counts the
    item in_sync (proven live on rg03: static route absent from the device, job succeeded).
    The device-truth check is the reconcile value-match settle, which runs right before
    this in run_device_reconcile: a row the device really reflects has already settled
    deploying→in_sync. A row STILL deploying once the grace has elapsed therefore never
    landed — surface it as apply_failed instead of letting it spin forever while the job
    history claims success. Callers must skip this while an apply is queued/running (its
    _prepare_apply just re-marked rows deploying; judging those by the OLD job's age
    would misfire).

    Only scopes THIS job actually applied are judged. The job's result carries a
    ``<scope>_count_by_outcome`` per scope it carried; a scope missing from it (or carrying
    zero items) was never in this apply, so the job's success says nothing about that scope's
    rows. Judging them anyway fabricated a failure — flipping, say, an in-flight route-policy
    row to apply_failed with a "the NED dropped it silently" message about a job that had
    only ever applied VLANs.
    """
    from django.utils import timezone

    from . import models
    from . import status_machine as sm

    if not job or job.get("status") != "succeeded":
        return  # failed applies are the failure-settle's job; no job → nothing to judge
    finished = _parse_adapter_ts(job.get("updated_at"))
    if finished is None or timezone.now() - finished < _stuck_deploying_grace():
        return
    result = job.get("result") or {}
    detail = _STUCK_DEPLOYING_ERROR.format(job_id=job.get("id"))
    for scope, model_name in _APPLY_DEPLOYING_SCOPES.items():
        counts = result.get(f"{scope}_count_by_outcome") or {}
        if sum(int(count or 0) for count in counts.values()) <= 0:
            continue  # this job never applied this scope — it cannot testify about its rows
        model = getattr(models, model_name)
        for row in model.objects.filter(management=mgmt, status="deploying"):
            new_status = sm.on_apply_result(row.status, ok=False)
            if new_status != row.status:
                row.status = new_status
                row.last_apply_error = detail
                row.save(update_fields=["status", "last_apply_error"])
                logger.warning(
                    "nso reconcile: %s %s stuck deploying after successful apply #%s — "
                    "escalated to apply_failed (silent drop)",
                    model_name,
                    row.pk,
                    job.get("id"),
                )


def _apply_job_state(adapter_device_id) -> tuple[dict | None, bool]:
    """Best-effort: (most recent terminal apply job, is an apply queued/running now).

    One jobs fetch serves both the failure-settle (which needs the last terminal
    apply's result) and the stuck-deploying escalation (which must stand down while
    a new apply is in flight).
    """
    from . import adapter_client as client

    try:
        jobs = client.list_jobs(adapter_device_id)  # most-recent-first
    except Exception:  # noqa: BLE001 — adapter transient; settling is best-effort
        return None, False
    last, active = None, False
    for job in jobs or []:
        if job.get("type") != "apply":
            continue
        if job.get("status") in ("queued", "running"):
            active = True
        elif last is None and job.get("status") in ("succeeded", "failed"):
            last = job
    return last, active


def _last_apply_job(adapter_device_id) -> dict | None:
    """Best-effort: the device's most recent terminal apply job (full dict).

    Returns the whole job ``{id, type, status, result}`` so callers can read both the
    per-scope outcome (``result``) and the job id (apply-journal idempotency key).
    """
    job, _active = _apply_job_state(adapter_device_id)
    return job


def _last_apply_result(adapter_device_id) -> dict | None:
    """Best-effort: the result of the device's most recent terminal apply job."""
    job = _last_apply_job(adapter_device_id)
    return job.get("result") if job else None


def _journal_route_policy_apply(mgmt, job: dict | None) -> None:
    """Write a coarse per-object JournalEntry recording this device's route-policy apply.

    Route-policy is the one intent scope with no deploying→in_sync settle, so the
    operator has no per-row apply outcome for it. To give a consolidated "what applied
    where" view we drop a single JournalEntry onto each linked netbox-routing object
    (community-list / route-map / prefix-list / as-path) carrying the device + the
    route_policy scope outcome; the per-member skip/error detail stays on the device's
    NSO tab. Idempotent per apply job via ``mgmt.last_journaled_apply_job`` so a re-run
    of this post-apply reconcile does not re-post the same apply.
    """
    if not job:
        return
    job_id = str(job.get("id") or "")
    if not job_id or job_id == (mgmt.last_journaled_apply_job or ""):
        return
    counts = (job.get("result") or {}).get("route_policy_count_by_outcome") or {}
    applied = int(counts.get("in_sync") or 0)
    failed = int(counts.get("apply_failed") or 0)
    # Mark the job seen FIRST so a failure mid-write can't double-post next reconcile.
    mgmt.last_journaled_apply_job = job_id
    mgmt.save(update_fields=["last_journaled_apply_job"])
    if applied == 0 and failed == 0:
        return  # this apply committed no route-policy scope → nothing to journal

    from django.urls import reverse
    from django.utils import timezone
    from extras.models import JournalEntry

    from . import models
    from . import status_machine as sm

    # Only objects the operator owns on this device were part of the apply — an
    # imported-only object was never pushed, so it gets no apply log here (it still
    # shows on the "Applied to devices" panel, which lists every device).
    rows = list(
        models.NSORoutePolicyState.objects.filter(
            management=mgmt,
            content_type__isnull=False,
            object_id__isnull=False,
            status__in=sm.OWNED_STATES,
        )
    )
    if not rows:
        return

    if failed:
        kind, verb = "danger", f"**failed** ({applied} applied, {failed} failed)"
    else:
        kind, verb = "success", f"**succeeded** ({applied} applied)"
    try:
        tab = reverse("dcim:device_nso", kwargs={"pk": mgmt.device_id})
        pointer = f" Member-level detail is on the device's [NSO tab]({tab})."
    except Exception:  # noqa: BLE001 — URL reverse is best-effort decoration
        pointer = " Member-level detail is on the device's NSO tab."
    detail = _scope_failure_messages(job, "route_policy") if failed else ""
    failure_note = f" Failures: {detail}." if detail else ""
    comment = f"NSO apply on **{mgmt.device}** — route-policy {verb}, adapter job #{job_id}.{failure_note}{pointer}"

    # Device-level journal entry: the operator is pointed at the device journal for apply
    # outcomes, so record the apply (with the real failure reason) there too — not only on
    # the per-object journals.
    try:
        device = mgmt.device
        if device is not None:
            JournalEntry.objects.create(assigned_object=device, kind=kind, created_by=None, comments=comment)
    except Exception as exc:  # noqa: BLE001 — the device entry must not block per-object ones
        logger.warning("nso journal: route-policy apply device entry skipped for %s: %s", mgmt.device_id, exc)

    now = timezone.now()
    for row in rows:
        obj = row.assigned_object
        if obj is None:
            continue
        try:
            JournalEntry.objects.create(assigned_object=obj, kind=kind, created_by=None, comments=comment)
        except Exception as exc:  # noqa: BLE001 — one object's journal must not block the rest
            logger.warning("nso journal: route-policy apply entry skipped for %s: %s", row.object_name, exc)
            continue
        # Stamp the apply time so the "Applied to devices" panel shows a real timestamp.
        row.last_apply_at = now
        row.save(update_fields=["last_apply_at"])


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

    # Step 4: after the post-sync reconcile, settle any rows left 'deploying' whose
    # scope's last apply reported a failure → apply_failed (no longer stuck), escalate
    # rows a SUCCEEDED apply left 'deploying' past the grace (silent drop, #26), and
    # record the route-policy apply outcome in the netbox-routing journals (idempotent).
    try:
        mgmt = device.nso_management
        if mgmt is not None and mgmt.adapter_device_id is not None:
            job, apply_active = _apply_job_state(mgmt.adapter_device_id)
            _settle_apply_failures(mgmt, job.get("result") if job else None, job)
            if not apply_active:
                _escalate_stuck_deploying(mgmt, job)
            _journal_route_policy_apply(mgmt, job)
    except Exception as exc:  # noqa: BLE001 — settling is best-effort, never crash the worker
        logger.warning("nso reconcile: apply-failure settle skipped for device %s: %s", device_id, exc)

    summary = {"device_id": device_id, "interface_states": len(ctx.get("interface_states") or {})}
    logger.info("nso reconcile complete: %s", summary)
    return summary


#: How long an RQ job tracked by NEITHER registry must have gone untouched before we treat
#: it as an orphan and reclaim its id. Covers the worker HAND-OFF window (see below); an
#: orphan from a dead worker is minutes-to-months stale, so it is still reclaimed promptly.
_ORPHAN_RECLAIM_GRACE_S = 60


def _job_recently_touched(job) -> bool:
    """Whether *job* carries a fresh timestamp — i.e. it is mid-hand-off, not an orphan.

    An RQ worker pops the job (it leaves ``queue.get_job_ids()``) and only then adds it to
    ``StartedJobRegistry``. In between it is in NEITHER, and is indistinguishable from a job
    orphaned by a dead worker. Reclaiming it there deletes a job that is about to run and
    enqueues a second one, so two reconciles for the same device run CONCURRENTLY and race
    on the overlay upserts (get_or_create → IntegrityError). Its timestamps are seconds old,
    though, while a real orphan's are not — that is the distinction.
    """
    from datetime import UTC, datetime

    stamps = [
        getattr(job, "last_heartbeat", None),
        getattr(job, "started_at", None),
        getattr(job, "enqueued_at", None),
    ]
    latest = max((s for s in stamps if s is not None), default=None)
    if latest is None:
        return False
    if latest.tzinfo is None:  # RQ historically stamped naive UTC
        latest = latest.replace(tzinfo=UTC)
    return (datetime.now(UTC) - latest).total_seconds() < _ORPHAN_RECLAIM_GRACE_S


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
        from rq.registry import StartedJobRegistry
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
        # Only skip re-enqueue when the job is GENUINELY in flight — actually present in the
        # pending queue, or actively running in the started registry. Do NOT trust the job's
        # status field: a job whose worker died mid-run (dev auto-reload, prod redeploy/crash)
        # is left with a stale 'queued'/'started' status yet is tracked by NEITHER registry, and
        # keying the skip off status alone lets such an orphan block EVERY future reconcile for
        # this device forever (observed live: an nso-reconcile-<id> stuck 'queued' for a month
        # silently no-op'd every sync/apply notify, so rows only settled on inline tab renders).
        pending = job_id in queue.get_job_ids()
        running = job_id in StartedJobRegistry(queue=queue).get_job_ids()
        if pending or running:
            return existing  # truly in flight — don't pile up
        if _job_recently_touched(existing):
            # In neither registry, but freshly stamped: a worker has just DEQUEUED it and has
            # not yet registered it as started. Reclaiming here would run a SECOND reconcile
            # for this device alongside the one about to start.
            logger.debug("nso reconcile: job %s is mid-hand-off to a worker; not re-enqueuing", job_id)
            return existing
        existing.delete()  # orphaned / finished / failed: clear so the id can be reused
    return queue.enqueue(run_device_reconcile, device_id, job_id=job_id, result_ttl=300, job_timeout=600)


def run_onboard_advance(mgmt_id: int):
    """RQ job: advance one provisioning onboarding row (fired by the provision-complete callback).

    Fired by :class:`~netbox_nso_plugin.api.views.ProvisionCompleteView`. Idempotent — a no-op once
    the row is terminal. See :func:`netbox_nso_plugin.onboarding.advance_provisioning`.
    """
    from .models import NSODeviceManagement
    from .onboarding import advance_provisioning

    mgmt = NSODeviceManagement.objects.filter(pk=mgmt_id).first()
    if mgmt is None:
        logger.debug("onboard advance: mgmt %s no longer exists", mgmt_id)
        return
    advance_provisioning(mgmt)


def enqueue_onboard_advance(mgmt_id: int):
    """Enqueue a background advance of a provisioning row (fired by the provision-complete callback).

    No deterministic job id: advance_provisioning is idempotent, so a duplicate enqueue is harmless
    — and a fixed id risks an orphaned RQ job blocking every future advance (see the reconcile
    dedup note above). Runs inline if RQ is unavailable. Returns the RQ job (or None on the inline
    path).
    """
    try:
        import django_rq
    except ImportError:  # pragma: no cover - RQ ships with NetBox
        logger.warning("django_rq unavailable; advancing onboard row %s inline", mgmt_id)
        run_onboard_advance(mgmt_id)
        return None

    queue = django_rq.get_queue(_RECONCILE_QUEUE)
    return queue.enqueue(run_onboard_advance, mgmt_id, result_ttl=300, job_timeout=300)
