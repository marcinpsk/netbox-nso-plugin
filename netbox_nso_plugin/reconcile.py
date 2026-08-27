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

from .deployment import guarded as _deployment_guarded

logger = logging.getLogger(__name__)


class ReconcileScopeError(Exception):
    """Carry scope metadata out of the fenced transaction before error marking."""

    def __init__(self, mgmt, model_names, fn_name):
        super().__init__(fn_name)
        self.mgmt = mgmt
        self.model_names = model_names
        self.fn_name = fn_name


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
    except Exception as exc:  # noqa: BLE001 — the gate rolls the scope transaction back
        raise ReconcileScopeError(mgmt, model_names, fn.__name__) from exc


# ── READSEM S4 (D5/D9): the device-wide read mutex + per-family gate plumbing ───

#: RQ contention: bounded in-job retry budget before the marker handoff (patchable in tests).
_RQ_RETRY_BUDGET_S = 90.0


class _LeaseOutcome:
    """Result of a device-lease acquisition attempt (see _acquire_reconcile_lease)."""

    def __init__(self, lease=None, state: str = "held", attempts: int = 0):
        self.lease = lease
        self.state = state  # held | busy | deferred | no_mutex
        self.attempts = attempts


def _acquire_reconcile_lease(mgmt, device_pk: int, call_class: str) -> _LeaseOutcome:
    """Acquire the device-wide read mutex per call class (D5/R6-1).

    web → single attempt, fail fast (``busy``); rq → bounded backoff retries then the
    marker handoff (``deferred`` — the lease owner's release enqueues the successor).
    Raises :class:`~netbox_nso_plugin.read_gate.LockUnavailable` when redis
    coordination is unreachable — callers fail CLOSED. Without django_rq (no redis
    configured at all) the run proceeds unserialized (``no_mutex``), matching the
    enqueue fallback above.
    """
    from .read_gate import Deferred, acquire_for_rq, acquire_for_web, lease_key

    try:
        import django_rq
    except ImportError:  # pragma: no cover - RQ ships with NetBox
        logger.warning("django_rq unavailable; reconciling device %s without the read mutex", device_pk)
        return _LeaseOutcome(state="no_mutex")

    queue = django_rq.get_queue(_RECONCILE_QUEUE)
    key = lease_key(mgmt.pk)
    if call_class == "web":
        lease = acquire_for_web(queue.connection, key, device_id=device_pk, queue=queue)
        return _LeaseOutcome(state="busy") if lease is None else _LeaseOutcome(lease=lease)
    # S5a F (codex R6-4/R7-1): notify-class jobs must not burn the 90s retry budget on
    # general RQ workers — budget 0 = single attempt, defer-marker, then the shipped
    # MANDATORY post-marker attempt (closes the release-before-marker race exactly as
    # the rq path does; read_gate's regression covers the interleaving).
    budget = 0.0 if call_class == "notify" else _RQ_RETRY_BUDGET_S
    out = acquire_for_rq(queue.connection, key, device_pk, queue, retry_budget_s=budget)
    if isinstance(out, Deferred):
        return _LeaseOutcome(state="deferred", attempts=out.attempts)
    return _LeaseOutcome(lease=out)


def _gated(
    ctx: dict,
    mgmt,
    family: str,
    payload,
    body,
    *,
    epoch,
    ctx_key: str | None = None,
    pre_body=None,
):
    """Gate ONE family document (D9): record the disposition, run *body* iff admitted.

    ``payload`` is the fetched family document; its ``read_state`` key (absent on a
    pre-S4 adapter → legacy) drives the D3 gate. The disposition lands in
    ``ctx["_gate"][family]``; when *ctx_key* is given and the body ran, its return
    value is stored there. A skipped family's ctx entries keep their empty defaults
    (rendering paths fall back to persisted rows) and its overlay rows are untouched.
    """
    from .read_gate import LEGACY, RAN, gated_family_run

    read_state = payload.get("read_state") if isinstance(payload, dict) else None
    if read_state is None and isinstance(payload, dict) and "read_state" in payload:
        # explicit `"read_state": null` — a MALFORMED S4 block, not a pre-S4 adapter:
        # fail closed via the gate's incarnation check (codex B5-F4)
        read_state = {}
    context_before = dict(ctx)
    try:
        result = gated_family_run(mgmt, family, read_state, body, epoch=epoch, pre_body=pre_body)
    except ReconcileScopeError as exc:
        from .read_gate import (
            SKIPPED_STALE_ATTEMPT,
            SKIPPED_UNAVAILABLE,
            GateResult,
            mark_publication_error_if_current,
        )

        guard = getattr(exc, "_nso_publication_guard", None)
        error_management = exc.mgmt
        error_models = exc.model_names
        marked = bool(guard) and mark_publication_error_if_current(
            mgmt,
            guard[0],
            guard[1],
            guard[2],
            lambda: _mark_scope_error(error_management, error_models),
        )
        if marked:
            logger.exception(
                "nso reconcile: %s failed; marking %s rows error",
                exc.fn_name,
                ",".join(exc.model_names),
            )
        result = GateResult(SKIPPED_UNAVAILABLE if marked else SKIPPED_STALE_ATTEMPT)
    if result.disposition not in (RAN, LEGACY):
        # A body can assign display context before the final publication fence
        # detects supersession. Do not return uncommitted/stale values.
        ctx.clear()
        ctx.update(context_before)
    ctx.setdefault("_gate", {})[family] = result.disposition
    if ctx_key is not None and result.disposition in (RAN, LEGACY):
        ctx[ctx_key] = result.value
    return result


def _switchport_attempt_slot(device, payload):
    """Share ONE frozen switchport resolution between the gate's plan and its body."""
    from .vlan_reconciler import prepare_switchport_reconcile

    slot = []

    def plan():
        slot.clear()
        slot.append(prepare_switchport_reconcile(device, payload))
        return slot[0].plan

    return slot, plan


def _native_vlan_footprint(device, payload, family: str):
    """Resolve the complete native VLAN footprint before taking its first lock."""
    if family == "vlan":
        from .vlan_reconciler import vlan_reconcile_plan

        return vlan_reconcile_plan(device, payload)
    if family == "svi":
        from .svi_reconciler import svi_reconcile_plan

        return svi_reconcile_plan(device, payload)
    raise ValueError(f"unknown native VLAN dependency family: {family}")


def _mark_all_gated(ctx: dict, families, disposition: str) -> None:
    """Record one skip *disposition* for every family the aborted run would have gated."""
    gate = ctx.setdefault("_gate", {})
    for family in families:
        gate[family] = disposition


def _enabled_device_families(mgmt) -> list[str]:
    """List the families a full reconcile_device run would gate, per the scope flags."""
    fams = []
    if mgmt.manage_interfaces:
        fams += [
            "interface_attributes",
            "svi",
            "subinterface",
            "interface_mtu",
            "interface_ip",
            "lag_config",
            "vlan",
            "switchport",
        ]
    if mgmt.manage_snmp:
        fams.append("snmp")
    if mgmt.manage_logging:
        fams.append("logging")
    if getattr(mgmt, "manage_l2", False):
        fams.append("l2_service")
    if mgmt.manage_routing:
        if mgmt.manage_static:
            fams.append("static_route")
        if mgmt.manage_isis:
            fams.append("isis")
        if mgmt.manage_route_policy:
            fams.append("route_policy")
        if mgmt.manage_ospf:
            fams.append("ospf")
        if mgmt.manage_bgp:
            fams.append("bgp")
        if mgmt.manage_bgp or mgmt.manage_isis or mgmt.manage_ospf:
            fams.append("bfd")
        if mgmt.manage_redistribution:
            fams.append("redistribution")
    return fams


#: reconcile_category branch → the families that branch gates (busy/lock dispositions).
_CATEGORY_RECONCILE_FAMILIES: dict[str, tuple[str, ...]] = {
    "interface": ("interface_attributes", "svi", "subinterface", "interface_ip", "interface_mtu", "vlan", "switchport"),
    "interfaces": ("interface_attributes", "svi", "subinterface", "interface_ip"),
    "interface_ips": ("svi", "subinterface", "interface_ip"),
    "lacp": ("lag_config",),
    "vlan": ("vlan",),
    "switchport": ("vlan", "switchport"),
    "svi": ("svi",),
    "subinterface": ("subinterface",),
    "interface_mtu": ("interface_mtu",),
    "snmp": ("snmp",),
    "logging": ("logging",),
    "static": ("static_route",),
    "isis": ("isis",),
    "ospf": ("ospf",),
    "bgp": ("bgp",),
    "bfd": ("bfd",),
    "route_policy": ("route_policy",),
    "redistribution": ("redistribution",),
    "l2_services": ("l2_service",),
}

_ROUTE_POLICY_ATTEMPT_IDS = "_route_policy_attempt_ids"
_ROUTE_POLICY_ADAPTER_DEVICE_ID = "_route_policy_adapter_device_id"


def _reconcile_routing(device, mgmt, client, ctx: dict) -> None:
    """Reconcile each opted-in routing protocol into *ctx* (gated by kill-switches)."""
    from .bfd_reconciler import bfd_reconcile_plan, reconcile_bfd
    from .bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
    from .redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan
    from .route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan
    from .template_content import (
        _reconcile_isis_interfaces,
        _reconcile_isis_process,
        _reconcile_ospf,
        _reconcile_static_routes,
        static_route_reconcile_plan,
    )

    if not mgmt.manage_routing:
        return
    dev_id = mgmt.adapter_device_id

    if mgmt.manage_static:
        static_doc = client.get_static_routes(dev_id)
        _gated(
            ctx,
            mgmt,
            "static_route",
            static_doc,
            lambda: _safe_reconcile(
                ctx, "static_routes", mgmt, ("NSOStaticRouteState",), _reconcile_static_routes, device, static_doc
            ),
            epoch=dev_id,
            pre_body=lambda: static_route_reconcile_plan(device, static_doc),
        )
    if mgmt.manage_isis:
        # R3-6: ONE isis document → ONE gate decision → ONE compound body driving
        # both reconcilers (never two gate calls abusing the equality rerun rule).
        isis_payload = client.get_isis_interfaces(dev_id)

        def _isis_body():
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

        _gated(ctx, mgmt, "isis", isis_payload, _isis_body, epoch=dev_id)
    if mgmt.manage_route_policy:
        from .apply_settlement import route_policy_deploying_attempt_ids

        rp_doc = client.get_route_policy(dev_id)

        def _route_policy_body():
            ctx[_ROUTE_POLICY_ATTEMPT_IDS] = route_policy_deploying_attempt_ids(mgmt)
            ctx[_ROUTE_POLICY_ADAPTER_DEVICE_ID] = mgmt.adapter_device_id
            return _safe_reconcile(
                ctx,
                "route_policy_states",
                mgmt,
                ("NSORoutePolicyState",),
                reconcile_route_policy,
                device,
                rp_doc,
            )

        _gated(
            ctx,
            mgmt,
            "route_policy",
            rp_doc,
            _route_policy_body,
            epoch=dev_id,
            pre_body=lambda: route_policy_reconcile_plan(device, rp_doc),
        )
    if mgmt.manage_ospf:
        ospf_doc = client.get_ospf(dev_id)
        _gated(
            ctx,
            mgmt,
            "ospf",
            ospf_doc,
            lambda: _safe_reconcile(
                ctx,
                "ospf_data",
                mgmt,
                ("NSOOSPFInstanceState", "NSOOSPFInterfaceState"),
                _reconcile_ospf,
                device,
                ospf_doc,
            ),
            epoch=dev_id,
        )
    if mgmt.manage_bgp:
        bgp_doc = client.get_bgp_config(dev_id)
        _gated(
            ctx,
            mgmt,
            "bgp",
            bgp_doc,
            lambda: _safe_reconcile(
                ctx, "bgp_peers", mgmt, ("NSOBGPPeerState",), _reconcile_bgp_config, device, bgp_doc
            ),
            epoch=dev_id,
            pre_body=lambda: bgp_reconcile_plan(device, bgp_doc),
        )
    # BFD is interface-level + protocol-agnostic; reconcile it whenever any of the
    # protocols that ride it (BGP/IS-IS/OSPF) are managed.
    if mgmt.manage_bgp or mgmt.manage_isis or mgmt.manage_ospf:
        bfd_doc = client.get_bfd(dev_id)
        _gated(
            ctx,
            mgmt,
            "bfd",
            bfd_doc,
            lambda: _safe_reconcile(
                ctx,
                "bfd_interfaces",
                mgmt,
                ("NSOBFDInterfaceState",),
                reconcile_bfd,
                device,
                bfd_doc.get("interfaces", []),
            ),
            epoch=dev_id,
            pre_body=lambda: bfd_reconcile_plan(device, bfd_doc.get("interfaces", [])),
        )
        from .models import NSOBFDInterfaceState

        ctx["bfd_states"] = list(
            NSOBFDInterfaceState.objects.filter(management__device=device).select_related("interface")
        )
    # Redistribution runs LAST: its destination is a netbox_routing OSPFInstance /
    # ISISInstance / BGPAddressFamily created by the protocol reconciles above, so
    # those must run first (BGP especially — BGP-dest redistribution needs its AF).
    if mgmt.manage_redistribution:
        redist_doc = client.get_redistribution(dev_id)
        _gated(
            ctx,
            mgmt,
            "redistribution",
            redist_doc,
            lambda: _safe_reconcile(
                ctx,
                "redistribution_states",
                mgmt,
                ("NSORedistributionState",),
                reconcile_redistribution,
                device,
                redist_doc,
            ),
            epoch=dev_id,
            pre_body=lambda: redistribution_reconcile_plan(device, redist_doc),
        )


@_deployment_guarded("reconcile")
def reconcile_device(device, mgmt=None, *, call_class: str = "rq") -> dict:
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
        interface_ip_reconcile_plan,
        interface_reconcile_plan,
        logging_reconcile_plan,
        snmp_reconcile_plan,
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

    # D5: the whole gated run executes under the device-wide read mutex; contention
    # and coordination failures abort BEFORE any fetch (fail closed, rows untouched).
    from .read_gate import SKIPPED_BUSY, SKIPPED_LOCK_UNAVAILABLE, LockUnavailable

    try:
        held = _acquire_reconcile_lease(mgmt, device.pk, call_class)
    except LockUnavailable:
        logger.warning(
            "device %s reconcile: redis coordination unreachable — failing closed (no reads applied)", device.pk
        )
        _mark_all_gated(ctx, _enabled_device_families(mgmt), SKIPPED_LOCK_UNAVAILABLE)
        ctx["_lock_unavailable"] = True
        return ctx
    if held.state == "busy":
        _mark_all_gated(ctx, _enabled_device_families(mgmt), SKIPPED_BUSY)
        return ctx
    if held.state == "deferred":
        ctx["_deferred"] = held.attempts
        return ctx

    from contextlib import nullcontext

    with held.lease if held.lease is not None else nullcontext(), suppress_intent_push():
        if mgmt.manage_interfaces:
            # S4: the object-shaped interfaces-doc (read_state inline); on an S3
            # adapter the client falls back to the legacy list as a key-absent doc.
            interfaces_doc = client.get_interfaces_doc(dev_id)
            fetched_interfaces = interfaces_doc.get("interfaces", [])
            fetched_state = client.get_state(dev_id)
            interface_result = _gated(
                ctx,
                mgmt,
                "interface_attributes",
                interfaces_doc,
                lambda: _safe_reconcile(
                    ctx,
                    "interface_states",
                    mgmt,
                    ("NSOInterfaceState",),
                    _upsert_interface_states,
                    device,
                    fetched_interfaces,
                ),
                epoch=dev_id,
                pre_body=lambda: interface_reconcile_plan(device, fetched_interfaces),
            )
            if interface_result.disposition in ("ran", "legacy"):
                ctx["interfaces"] = fetched_interfaces
                ctx["state"] = fetched_state
            # materialise SVIs/IRBs (virtual interfaces + VLAN link) BEFORE the IP
            # reconcile, which only attaches IPs to interfaces that already exist —
            # otherwise an SVI's IPs are dropped until the next refresh.
            from .svi_reconciler import reconcile_svi

            svi_doc = client.get_svi(dev_id)
            _gated(
                ctx,
                mgmt,
                "svi",
                svi_doc,
                lambda: _safe_reconcile(ctx, "svi_states", mgmt, ("NSOSVIState",), reconcile_svi, device, svi_doc),
                epoch=dev_id,
                pre_body=lambda: _native_vlan_footprint(device, svi_doc, "svi"),
            )
            # materialise dot1q subinterfaces (virtual interface + Interface.parent
            # link) BEFORE the IP reconcile, for the same ordering reason as SVIs.
            from .subinterface_reconciler import reconcile_subinterface, subinterface_reconcile_plan

            sub_doc = client.get_subinterface(dev_id)
            _gated(
                ctx,
                mgmt,
                "subinterface",
                sub_doc,
                lambda: _safe_reconcile(
                    ctx, "subinterface_states", mgmt, ("NSOSubinterfaceState",), reconcile_subinterface, device, sub_doc
                ),
                epoch=dev_id,
                pre_body=lambda: subinterface_reconcile_plan(device, sub_doc),
            )
            # Phase 2b: per-interface MTU read mirror (read-only display).
            from .interface_mtu_reconciler import interface_mtu_reconcile_plan, reconcile_interface_mtu

            mtu_doc = client.get_interface_mtu(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_mtu",
                mtu_doc,
                lambda: _safe_reconcile(
                    ctx,
                    "interface_mtu_states",
                    mgmt,
                    ("NSOInterfaceMtuState",),
                    reconcile_interface_mtu,
                    device,
                    mtu_doc,
                ),
                epoch=dev_id,
                pre_body=lambda: interface_mtu_reconcile_plan(device, mtu_doc),
            )
            # Import interface IP addresses onto their (now first-class, logical-named)
            # NetBox interfaces. Runs AFTER the adapter sync created the interfaces;
            # gated internally by interface_ip_auto_create (off → lands as pending).
            ip_doc = client.get_interface_ips(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_ip",
                ip_doc,
                lambda: _safe_reconcile(
                    ctx, "interface_ips", mgmt, ("NSOInterfaceIPState",), _reconcile_interface_ips, device, ip_doc
                ),
                epoch=dev_id,
                pre_body=lambda: interface_ip_reconcile_plan(device, ip_doc),
            )
            # LACP/LAG bundle + member overlay states (interface-level).
            from .lacp_reconciler import lacp_reconcile_plan, reconcile_lag_config

            lag_doc = client.get_lag_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "lag_config",
                lag_doc,
                lambda: _safe_reconcile(
                    ctx,
                    "lacp_bundle_states",
                    mgmt,
                    ("NSOLACPBundleState", "NSOLACPMemberState"),
                    reconcile_lag_config,
                    device,
                    lag_doc,
                ),
                epoch=dev_id,
                pre_body=lambda: lacp_reconcile_plan(device, lag_doc),
            )
            # VLAN database + L2 switchport (VLAN DB first — switchport links to it).
            from .vlan_reconciler import reconcile_switchport, reconcile_vlan_database

            vlan_doc = client.get_vlan_database(dev_id)
            _gated(
                ctx,
                mgmt,
                "vlan",
                vlan_doc,
                lambda: _safe_reconcile(
                    ctx, "vlan_states", mgmt, ("NSOVLANState",), reconcile_vlan_database, device, vlan_doc
                ),
                epoch=dev_id,
                pre_body=lambda: _native_vlan_footprint(device, vlan_doc, "vlan"),
            )
            sw_doc = client.get_switchport(dev_id)
            sw_slot, sw_plan = _switchport_attempt_slot(device, sw_doc)
            _gated(
                ctx,
                mgmt,
                "switchport",
                sw_doc,
                lambda: _safe_reconcile(
                    ctx,
                    "switchport_states",
                    mgmt,
                    ("NSOSwitchportState",),
                    reconcile_switchport,
                    device,
                    sw_doc,
                    sw_slot[0],
                ),
                epoch=dev_id,
                pre_body=sw_plan,
            )
        if mgmt.manage_snmp:
            snmp_doc = client.get_snmp_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "snmp",
                snmp_doc,
                lambda: _safe_reconcile(
                    ctx,
                    "snmp_data",
                    mgmt,
                    ("NSOSnmpCommunityState", "NSOSnmpV3UserState", "NSOSnmpHostState", "NSOSnmpSystemInfoState"),
                    _reconcile_snmp_config,
                    device,
                    snmp_doc,
                ),
                epoch=dev_id,
                pre_body=lambda: snmp_reconcile_plan(device, snmp_doc),
            )
        if mgmt.manage_logging:
            log_doc = client.get_logging_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "logging",
                log_doc,
                lambda: _safe_reconcile(
                    ctx,
                    "logging_data",
                    mgmt,
                    ("NSOLoggingHostState", "NSOLoggingLevelState"),
                    _reconcile_logging_config,
                    device,
                    log_doc,
                ),
                epoch=dev_id,
                pre_body=lambda: logging_reconcile_plan(device, log_doc),
            )
        if getattr(mgmt, "manage_l2", False):
            # Nokia L2 SAP overlays. Kept in the full reconcile (not just
            # on-expand) so the periodic sync-complete refresh keeps them current —
            # the tab reads these persisted rows without reconciling on expand.
            from .l2_service_reconciler import l2_service_reconcile_plan, reconcile_l2_services

            l2_doc = client.get_l2_services(dev_id)
            _gated(
                ctx,
                mgmt,
                "l2_service",
                l2_doc,
                lambda: _safe_reconcile(
                    ctx, "l2_sap_states", mgmt, ("NSOL2SapState",), reconcile_l2_services, device, l2_doc
                ),
                epoch=dev_id,
                pre_body=lambda: l2_service_reconcile_plan(device, l2_doc),
            )
        _reconcile_routing(device, mgmt, client, ctx)
    return ctx


@_deployment_guarded("reconcile")
def reconcile_category(device, mgmt, key: str) -> dict:  # noqa: C901
    """Reconcile a SINGLE category and return its display context (for lazy expand).

    Runs only the requested category's reconciler(s), suppress-wrapped (no intent
    push). Used by the lazy-load endpoint when an operator expands one category on
    the tab, so the page render itself stays counts-only. Raises AdapterError on
    adapter failure — the caller renders a per-category error.
    """
    from . import adapter_client as client
    from .bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
    from .redistribution_reconciler import reconcile_redistribution, redistribution_reconcile_plan
    from .route_policy_reconciler import reconcile_route_policy, route_policy_reconcile_plan
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
        interface_ip_reconcile_plan,
        interface_reconcile_plan,
        snmp_reconcile_plan,
    )

    ctx = _empty_context()
    if mgmt is None or mgmt.adapter_device_id is None:
        return ctx
    dev_id = mgmt.adapter_device_id

    # D5/R6-1: the whole per-category gated run holds the device mutex; the WEB call
    # class fails fast on contention (the view renders persisted rows + an
    # in-progress chip) and fails CLOSED when redis coordination is unreachable.
    from .read_gate import SKIPPED_BUSY, SKIPPED_LOCK_UNAVAILABLE, LockUnavailable

    try:
        held = _acquire_reconcile_lease(mgmt, device.pk, "web")
    except LockUnavailable:
        logger.warning(
            "device %s category %s: redis coordination unreachable — failing closed (no reads applied)",
            device.pk,
            key,
        )
        _mark_all_gated(ctx, _CATEGORY_RECONCILE_FAMILIES.get(key, ()), SKIPPED_LOCK_UNAVAILABLE)
        ctx["_lock_unavailable"] = True
        return ctx
    if held.state == "busy":
        _mark_all_gated(ctx, _CATEGORY_RECONCILE_FAMILIES.get(key, ()), SKIPPED_BUSY)
        return ctx

    from contextlib import nullcontext

    with held.lease if held.lease is not None else nullcontext(), suppress_intent_push():
        if key == "interface":
            # Merged "Interfaces" card: refresh all four per-interface scalar
            # overlays (enabled/description, IPs, MTU, switchport) so the
            # consolidated row-per-interface table reflects the latest device read.
            from .interface_mtu_reconciler import interface_mtu_reconcile_plan, reconcile_interface_mtu
            from .subinterface_reconciler import reconcile_subinterface, subinterface_reconcile_plan
            from .svi_reconciler import reconcile_svi
            from .vlan_reconciler import reconcile_switchport, reconcile_vlan_database

            interfaces_doc = client.get_interfaces_doc(dev_id)
            fetched_interfaces = interfaces_doc.get("interfaces", [])
            fetched_state = client.get_state(dev_id)
            interface_result = _gated(
                ctx,
                mgmt,
                "interface_attributes",
                interfaces_doc,
                lambda: _upsert_interface_states(device, fetched_interfaces),
                epoch=dev_id,
                ctx_key="interface_states",
                pre_body=lambda: interface_reconcile_plan(device, fetched_interfaces),
            )
            if interface_result.disposition in ("ran", "legacy"):
                ctx["interfaces"] = fetched_interfaces
                ctx["state"] = fetched_state
            svi_doc = client.get_svi(dev_id)  # before IPs
            _gated(
                ctx,
                mgmt,
                "svi",
                svi_doc,
                lambda: reconcile_svi(device, svi_doc),
                epoch=dev_id,
                ctx_key="svi_states",
                pre_body=lambda: _native_vlan_footprint(device, svi_doc, "svi"),
            )
            sub_doc = client.get_subinterface(dev_id)
            _gated(
                ctx,
                mgmt,
                "subinterface",
                sub_doc,
                lambda: reconcile_subinterface(device, sub_doc),
                epoch=dev_id,
                ctx_key="subinterface_states",
                pre_body=lambda: subinterface_reconcile_plan(device, sub_doc),
            )
            ip_doc = client.get_interface_ips(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_ip",
                ip_doc,
                lambda: _reconcile_interface_ips(device, ip_doc),
                epoch=dev_id,
                ctx_key="interface_ips",
                pre_body=lambda: interface_ip_reconcile_plan(device, ip_doc),
            )
            mtu_doc = client.get_interface_mtu(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_mtu",
                mtu_doc,
                lambda: reconcile_interface_mtu(device, mtu_doc),
                epoch=dev_id,
                ctx_key="interface_mtu_states",
                pre_body=lambda: interface_mtu_reconcile_plan(device, mtu_doc),
            )
            # VLAN DB first so switchport vid lookups resolve in the per-device group.
            vlan_doc = client.get_vlan_database(dev_id)
            _gated(
                ctx,
                mgmt,
                "vlan",
                vlan_doc,
                lambda: reconcile_vlan_database(device, vlan_doc),
                epoch=dev_id,
                pre_body=lambda: _native_vlan_footprint(device, vlan_doc, "vlan"),
            )
            sw_doc = client.get_switchport(dev_id)
            sw_slot, sw_plan = _switchport_attempt_slot(device, sw_doc)
            _gated(
                ctx,
                mgmt,
                "switchport",
                sw_doc,
                lambda: reconcile_switchport(device, sw_doc, sw_slot[0]),
                epoch=dev_id,
                ctx_key="switchport_states",
                pre_body=sw_plan,
            )
        elif key == "interfaces":
            from .subinterface_reconciler import reconcile_subinterface, subinterface_reconcile_plan
            from .svi_reconciler import reconcile_svi

            interfaces_doc = client.get_interfaces_doc(dev_id)
            fetched_interfaces = interfaces_doc.get("interfaces", [])
            fetched_state = client.get_state(dev_id)
            interface_result = _gated(
                ctx,
                mgmt,
                "interface_attributes",
                interfaces_doc,
                lambda: _upsert_interface_states(device, fetched_interfaces),
                epoch=dev_id,
                ctx_key="interface_states",
                pre_body=lambda: interface_reconcile_plan(device, fetched_interfaces),
            )
            if interface_result.disposition in ("ran", "legacy"):
                ctx["interfaces"] = fetched_interfaces
                ctx["state"] = fetched_state
            svi_doc = client.get_svi(dev_id)  # before IPs
            _gated(
                ctx,
                mgmt,
                "svi",
                svi_doc,
                lambda: reconcile_svi(device, svi_doc),
                epoch=dev_id,
                ctx_key="svi_states",
                pre_body=lambda: _native_vlan_footprint(device, svi_doc, "svi"),
            )
            sub_doc = client.get_subinterface(dev_id)
            _gated(
                ctx,
                mgmt,
                "subinterface",
                sub_doc,
                lambda: reconcile_subinterface(device, sub_doc),
                epoch=dev_id,
                ctx_key="subinterface_states",
                pre_body=lambda: subinterface_reconcile_plan(device, sub_doc),
            )
            ip_doc = client.get_interface_ips(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_ip",
                ip_doc,
                lambda: _reconcile_interface_ips(device, ip_doc),
                epoch=dev_id,
                ctx_key="interface_ips",
                pre_body=lambda: interface_ip_reconcile_plan(device, ip_doc),
            )
        elif key == "interface_ips":
            from .subinterface_reconciler import reconcile_subinterface, subinterface_reconcile_plan
            from .svi_reconciler import reconcile_svi

            svi_doc = client.get_svi(dev_id)  # SVIs exist before IPs
            _gated(
                ctx,
                mgmt,
                "svi",
                svi_doc,
                lambda: reconcile_svi(device, svi_doc),
                epoch=dev_id,
                ctx_key="svi_states",
                pre_body=lambda: _native_vlan_footprint(device, svi_doc, "svi"),
            )
            sub_doc = client.get_subinterface(dev_id)
            _gated(
                ctx,
                mgmt,
                "subinterface",
                sub_doc,
                lambda: reconcile_subinterface(device, sub_doc),
                epoch=dev_id,
                ctx_key="subinterface_states",
                pre_body=lambda: subinterface_reconcile_plan(device, sub_doc),
            )
            ip_doc = client.get_interface_ips(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_ip",
                ip_doc,
                lambda: _reconcile_interface_ips(device, ip_doc),
                epoch=dev_id,
                ctx_key="interface_ips",
                pre_body=lambda: interface_ip_reconcile_plan(device, ip_doc),
            )
        elif key == "lacp":
            from .lacp_reconciler import lacp_reconcile_plan, reconcile_lag_config

            lag_doc = client.get_lag_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "lag_config",
                lag_doc,
                lambda: reconcile_lag_config(device, lag_doc),
                epoch=dev_id,
                ctx_key="lacp_bundle_states",
                pre_body=lambda: lacp_reconcile_plan(device, lag_doc),
            )
        elif key == "vlan":
            from .vlan_reconciler import reconcile_vlan_database

            vlan_doc = client.get_vlan_database(dev_id)
            _gated(
                ctx,
                mgmt,
                "vlan",
                vlan_doc,
                lambda: reconcile_vlan_database(device, vlan_doc),
                epoch=dev_id,
                ctx_key="vlan_states",
                pre_body=lambda: _native_vlan_footprint(device, vlan_doc, "vlan"),
            )
        elif key == "switchport":
            from .vlan_reconciler import reconcile_switchport, reconcile_vlan_database

            # VLAN DB first so switchport vid lookups resolve in the per-device group.
            vlan_doc = client.get_vlan_database(dev_id)
            _gated(
                ctx,
                mgmt,
                "vlan",
                vlan_doc,
                lambda: reconcile_vlan_database(device, vlan_doc),
                epoch=dev_id,
                pre_body=lambda: _native_vlan_footprint(device, vlan_doc, "vlan"),
            )
            sw_doc = client.get_switchport(dev_id)
            sw_slot, sw_plan = _switchport_attempt_slot(device, sw_doc)
            _gated(
                ctx,
                mgmt,
                "switchport",
                sw_doc,
                lambda: reconcile_switchport(device, sw_doc, sw_slot[0]),
                epoch=dev_id,
                ctx_key="switchport_states",
                pre_body=sw_plan,
            )
        elif key == "svi":
            from .svi_reconciler import reconcile_svi

            svi_doc = client.get_svi(dev_id)
            _gated(
                ctx,
                mgmt,
                "svi",
                svi_doc,
                lambda: reconcile_svi(device, svi_doc),
                epoch=dev_id,
                ctx_key="svi_states",
                pre_body=lambda: _native_vlan_footprint(device, svi_doc, "svi"),
            )
        elif key == "subinterface":
            from .subinterface_reconciler import reconcile_subinterface, subinterface_reconcile_plan

            sub_doc = client.get_subinterface(dev_id)
            _gated(
                ctx,
                mgmt,
                "subinterface",
                sub_doc,
                lambda: reconcile_subinterface(device, sub_doc),
                epoch=dev_id,
                ctx_key="subinterface_states",
                pre_body=lambda: subinterface_reconcile_plan(device, sub_doc),
            )
        elif key == "interface_mtu":
            from .interface_mtu_reconciler import interface_mtu_reconcile_plan, reconcile_interface_mtu

            mtu_doc = client.get_interface_mtu(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_mtu",
                mtu_doc,
                lambda: reconcile_interface_mtu(device, mtu_doc),
                epoch=dev_id,
                ctx_key="interface_mtu_states",
                pre_body=lambda: interface_mtu_reconcile_plan(device, mtu_doc),
            )
        elif key == "snmp":
            snmp_doc = client.get_snmp_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "snmp",
                snmp_doc,
                lambda: _reconcile_snmp_config(device, snmp_doc),
                epoch=dev_id,
                ctx_key="snmp_data",
                pre_body=lambda: snmp_reconcile_plan(device, snmp_doc),
            )
        elif key == "logging":
            from .template_content import logging_reconcile_plan

            log_doc = client.get_logging_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "logging",
                log_doc,
                lambda: _reconcile_logging_config(device, log_doc),
                epoch=dev_id,
                ctx_key="logging_data",
                pre_body=lambda: logging_reconcile_plan(device, log_doc),
            )
        elif key == "static":
            from .template_content import static_route_reconcile_plan

            static_doc = client.get_static_routes(dev_id)
            _gated(
                ctx,
                mgmt,
                "static_route",
                static_doc,
                lambda: _reconcile_static_routes(device, static_doc),
                epoch=dev_id,
                ctx_key="static_routes",
                pre_body=lambda: static_route_reconcile_plan(device, static_doc),
            )
        elif key == "isis":
            # R3-6: ONE document → ONE gate decision → ONE compound body.
            isis_payload = client.get_isis_interfaces(dev_id)

            def _isis_body():
                ctx["isis_interfaces"] = _reconcile_isis_interfaces(device, isis_payload.get("interfaces", []))
                ctx["isis_processes"] = _reconcile_isis_process(device, isis_payload.get("processes", []))

            _gated(ctx, mgmt, "isis", isis_payload, _isis_body, epoch=dev_id)
        elif key == "ospf":
            ospf_doc = client.get_ospf(dev_id)
            _gated(
                ctx,
                mgmt,
                "ospf",
                ospf_doc,
                lambda: _reconcile_ospf(device, ospf_doc),
                epoch=dev_id,
                ctx_key="ospf_data",
            )
        elif key == "bgp":
            from .models import NSOBGPPeerTemplateState

            bgp_doc = client.get_bgp_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "bgp",
                bgp_doc,
                lambda: _reconcile_bgp_config(device, bgp_doc),
                epoch=dev_id,
                ctx_key="bgp_peers",
                pre_body=lambda: bgp_reconcile_plan(device, bgp_doc),
            )
            ctx["bgp_peer_templates"] = list(
                NSOBGPPeerTemplateState.objects.filter(management=mgmt).select_related("template")
            )
        elif key == "bfd":
            from .bfd_reconciler import bfd_reconcile_plan, reconcile_bfd
            from .models import NSOBFDInterfaceState

            bfd_doc = client.get_bfd(dev_id)
            _gated(
                ctx,
                mgmt,
                "bfd",
                bfd_doc,
                lambda: reconcile_bfd(device, bfd_doc.get("interfaces", [])),
                epoch=dev_id,
                ctx_key="bfd_interfaces",
                pre_body=lambda: bfd_reconcile_plan(device, bfd_doc.get("interfaces", [])),
            )
            ctx["bfd_states"] = list(
                NSOBFDInterfaceState.objects.filter(management__device=device).select_related("interface")
            )
        elif key == "route_policy":
            rp_doc = client.get_route_policy(dev_id)
            _gated(
                ctx,
                mgmt,
                "route_policy",
                rp_doc,
                lambda: reconcile_route_policy(device, rp_doc),
                epoch=dev_id,
                ctx_key="route_policy_states",
                pre_body=lambda: route_policy_reconcile_plan(device, rp_doc),
            )
        elif key == "redistribution":
            redist_doc = client.get_redistribution(dev_id)
            _gated(
                ctx,
                mgmt,
                "redistribution",
                redist_doc,
                lambda: reconcile_redistribution(device, redist_doc),
                epoch=dev_id,
                ctx_key="redistribution_states",
                pre_body=lambda: redistribution_reconcile_plan(device, redist_doc),
            )
        elif key == "l2_services":
            # reconcile into native vpn.L2VPN + L2VPNTermination + NSOL2SapState
            # (value-aware drift/accept). The dot1q tag stays per-SAP interface-local encap.
            from .l2_service_reconciler import l2_service_reconcile_plan, reconcile_l2_services

            l2_doc = client.get_l2_services(dev_id)
            _gated(
                ctx,
                mgmt,
                "l2_service",
                l2_doc,
                lambda: reconcile_l2_services(device, l2_doc),
                epoch=dev_id,
                ctx_key="l2_sap_states",
                pre_body=lambda: l2_service_reconcile_plan(device, l2_doc),
            )
    return ctx


# ── Off-request reconcile: RQ job fired by the adapter's sync-complete callback ──

_RECONCILE_QUEUE = "default"


def _counts_for(result: dict | None, scope: str) -> dict:
    """Return the apply's ``<scope>_count_by_outcome`` map, or an empty one.

    ``result`` is an object by contract but each counts map inside it is free-form JSON.
    An empty map reads as "this job says nothing about this scope", which is what a junk
    one should mean: raising here loses the whole device's settle to the caller's blanket
    except, not just this scope.
    """
    counts = (result or {}).get(f"{scope}_count_by_outcome")
    return counts if isinstance(counts, dict) else {}


def _count_of(counts: dict, outcome: str) -> int:
    """Return one outcome's non-negative integer tally, or zero."""
    count = counts.get(outcome)
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0


def _scope_failure_messages(job: dict | None, scope: str) -> str:
    """Join the real per-item error messages for *scope* from a failed apply job.

    The adapter records each failed item under ``job.error.detail.items`` as
    ``{"type": <scope>, "error": <message>, ...}`` (jobs API exposes ``error``).
    Returns a de-duplicated, human-readable string, or "" when none are present.
    """
    if not job:
        return ""
    # ``error`` is an object by contract; ``detail`` and ``items`` inside it are free-form:
    # a scalar items raises on iteration and a string one iterates its characters.
    detail = (job.get("error") or {}).get("detail")
    items = (detail if isinstance(detail, dict) else {}).get("items")
    items = items if isinstance(items, list) else []
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
    from . import status_machine as sm
    from .intent_state import MutationFootprint, SourceRow, mirror_transaction
    from .models import NSORoutePolicyState

    rows = list(
        NSORoutePolicyState.objects.filter(
            management=mgmt,
            content_type__isnull=False,
            object_id__isnull=False,
            status__in=sm.OWNED_STATES,
        )
    )
    footprint = MutationFootprint.for_keys(
        {(mgmt.device_id, "route_policy")},
        overlay_rows=tuple(SourceRow(row._meta.label_lower, row.pk) for row in rows),
    )
    with mirror_transaction(footprint):
        _journal_route_policy_apply_locked(type(mgmt).objects.get(pk=mgmt.pk), job)


def _journal_route_policy_apply_locked(mgmt, job: dict) -> None:
    """Journal one carrier while its device revision and owned policy rows are locked."""
    apply_attempt_id = job.get("apply_attempt_id")
    if apply_attempt_id is not None:
        from django.core.exceptions import ValidationError

        from .models import NSOApplyAttempt, NSOIntentRevision

        try:
            attempt = NSOApplyAttempt.objects.filter(pk=apply_attempt_id, management=mgmt).first()
        except (TypeError, ValueError, ValidationError):
            return
        expected_revision = None if attempt is None else attempt.scope_revisions.get("route_policy")
        current_revision = (
            NSOIntentRevision.objects.filter(device_id=mgmt.device_id, scope="route_policy")
            .values_list("revision", flat=True)
            .first()
        )
        if type(expected_revision) is not int or current_revision != expected_revision:
            return
    job_id = str(job.get("id") or "")
    if not job_id or job_id == (mgmt.last_journaled_apply_job or ""):
        return
    counts = _counts_for(job.get("result"), "route_policy")
    applied = _count_of(counts, "in_sync")
    failed = _count_of(counts, "apply_failed")
    # Mark the job seen FIRST so a failure mid-write can't double-post next reconcile.
    mgmt.last_journaled_apply_job = job_id
    type(mgmt).objects.filter(pk=mgmt.pk).update(last_journaled_apply_job=job_id)
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


def run_device_reconcile(device_id: int, notify_class: bool = False) -> dict:
    """RQ entrypoint: reconcile one device by NetBox device id, off the request path.

    Runs in the rqworker (no HTTP request), so suppress_intent_push() — not the
    GET-render guard — is what keeps the NSO*State writes from pushing intent back.
    Adapter and deployment-gate refusals are returned instead of crashing the worker.
    ``notify_class=True`` marks a unique notify job: its lease acquisition is
    single-attempt + defer-marker (no 90s retry burn on general RQ workers). Under READSEM
    1334 the enqueue plane no longer sets it (all carriers are rq-class), but the param stays
    for any :func:`enqueue_reconcile_carrier`-external caller and for in-flight jobs across a deploy.
    """
    from dcim.models import Device

    from .adapter_client import AdapterError
    from .deployment import DeploymentQuiesced, operation
    from .models import NSODeviceManagement
    from .settlement import settle_static_routes
    from .signals import suppress_intent_push

    try:
        device = Device.objects.get(pk=device_id)
    except Device.DoesNotExist:
        logger.warning("nso reconcile: device %s no longer exists; skipping", device_id)
        return {"device_id": device_id, "skipped": "device_gone"}

    try:
        ctx = reconcile_device(device, call_class="notify" if notify_class else "rq")
    except (AdapterError, DeploymentQuiesced) as exc:
        logger.warning("nso reconcile deferred for device %s: %s", device_id, exc)
        return {"device_id": device_id, "error": str(exc)}

    if ctx.get("_deferred"):
        # R7-1/R8-1: never a zero-work success — the lease owner's release hook (or
        # the cadence backstop) runs the successor; this job's summary says so.
        logger.warning(
            "nso reconcile deferred for device %s after %s attempts (device lease contended)",
            device_id,
            ctx["_deferred"],
        )
        return {"device_id": device_id, "deferred": True, "attempts": ctx["_deferred"]}
    if ctx.get("_lock_unavailable"):
        return {"device_id": device_id, "skipped": "lock_unavailable"}

    route_policy_attempt_ids = ctx.pop(_ROUTE_POLICY_ATTEMPT_IDS, ())
    route_policy_adapter_device_id = ctx.pop(_ROUTE_POLICY_ADAPTER_DEVICE_ID, None)
    route_policy_evidence = None
    if route_policy_adapter_device_id is not None and route_policy_attempt_ids:
        from .apply_settlement import deployment_evidence_attempt_ids, load_deployment_evidence

        evidence_management = NSODeviceManagement.objects.filter(
            device=device,
            adapter_device_id=route_policy_adapter_device_id,
        ).first()
        if evidence_management is not None:
            requested_attempt_ids = tuple(
                sorted(
                    set(route_policy_attempt_ids) | set(deployment_evidence_attempt_ids(evidence_management)),
                    key=str,
                )
            )
            try:
                with operation("reconcile"):
                    route_policy_evidence = load_deployment_evidence(
                        evidence_management,
                        attempt_ids=requested_attempt_ids,
                    )
            except AdapterError as exc:
                logger.warning(
                    "nso reconcile: route-policy evidence failed for device %s: %s",
                    device.pk,
                    exc,
                )
            except DeploymentQuiesced as exc:
                logger.warning("nso reconcile deferred for device %s: %s", device_id, exc)
                return {"device_id": device_id, "error": str(exc)}

    # Step 4: after the post-sync reconcile, walk this device's settlement feed (the
    # production carrier for #1502's consumer), settle any rows left 'deploying' whose
    # scope's last apply reported a failure → apply_failed (no longer stuck), escalate
    # rows a SUCCEEDED apply left 'deploying' past the grace (silent drop, #26), and
    # record the route-policy apply outcome in the netbox-routing journals (idempotent).
    try:
        # Reverse one-to-one: a plain attribute read raises for an unmanaged device.
        mgmt = getattr(device, "nso_management", None)
        if mgmt is not None and mgmt.adapter_device_id is not None:
            static_route_feed_drained = False
            # Drain exact route results before attempt evidence. A settled generation may
            # judge a route only after this walk proves that no precise verdict remains.
            try:
                static_route_feed_drained = settle_static_routes(mgmt).drained
            except Exception as exc:  # noqa: BLE001 — narrow: only the static backstop stands down
                logger.warning(
                    "nso reconcile: static-route settlement failed for device %s: %s — "
                    "the static backstop stands down for this invocation",
                    device_id,
                    exc,
                )
            # settle_static_routes locks the row and consumes the id IT reads, so a link repair
            # committing in that window leaves the state read above about another adapter device.
            # A fresh fetch, not refresh_from_db: an unmanaged device must skip here, not raise.
            mgmt = NSODeviceManagement.objects.filter(pk=mgmt.pk).first()
            if mgmt is not None and mgmt.adapter_device_id is not None:
                # These are mirror writes, not operator intent: without the suppression the
                # first status flip's push-on-save signal refuses (no writer transaction) and
                # takes the rest of Step 4 with it.
                with suppress_intent_push():
                    from .apply_settlement import (
                        latest_route_policy_carrier,
                        settle_apply_attempts,
                        settle_device_apply_attempts,
                    )

                    with operation("reconcile"):
                        if (
                            isinstance(route_policy_evidence, dict)
                            and route_policy_evidence.get("device_id") == mgmt.adapter_device_id
                        ):
                            evidence = route_policy_evidence
                            settle_apply_attempts(
                                mgmt,
                                evidence,
                                static_route_feed_drained=static_route_feed_drained,
                                required_attempt_ids=route_policy_attempt_ids,
                            )
                        else:
                            evidence = settle_device_apply_attempts(
                                mgmt,
                                static_route_feed_drained=static_route_feed_drained,
                            )
                    _journal_route_policy_apply(
                        mgmt,
                        latest_route_policy_carrier(
                            evidence,
                            attempt_ids=route_policy_attempt_ids or None,
                        ),
                    )
    except Exception as exc:  # noqa: BLE001 — settling is best-effort, never crash the worker
        logger.warning("nso reconcile: apply-failure settle skipped for device %s: %s", device_id, exc)

    summary = {"device_id": device_id, "interface_states": len(ctx.get("interface_states") or {})}
    logger.info("nso reconcile complete: %s", summary)
    return summary


def enqueue_device_reconcile(device_id: int):
    """Enqueue a background reconcile for *device_id* via the READSEM 1334 queued-carrier arbiter.

    Every producer — the adapter sync-complete callback (``api/views.py``), the UI "Refresh overlays"
    action (``views.py``), and the lease-release handoff — funnels through
    :func:`~netbox_nso_plugin.read_gate.enqueue_reconcile_carrier`, which single-flights the per-device
    queued carrier: a new edge either suppresses onto the one genuinely-queued carrier (loss-free — it
    starts after this edge's mirror refresh) or enqueues exactly one trailing carrier. Returns the
    (existing or new) RQ job, or None if RQ is absent (run inline).
    """
    try:
        import django_rq
    except ImportError:  # pragma: no cover - RQ ships with NetBox
        logger.warning("django_rq unavailable; running reconcile for device %s inline", device_id)
        run_device_reconcile(device_id)
        return None

    from .read_gate import enqueue_reconcile_carrier

    queue = django_rq.get_queue(_RECONCILE_QUEUE)
    return enqueue_reconcile_carrier(queue.connection, queue, device_id)


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
