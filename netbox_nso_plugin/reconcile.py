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


def _gated(ctx: dict, mgmt, family: str, payload, body, *, epoch, ctx_key: str | None = None):
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
        result = gated_family_run(mgmt, family, read_state, body, epoch=epoch)
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
        rp_doc = client.get_route_policy(dev_id)
        _gated(
            ctx,
            mgmt,
            "route_policy",
            rp_doc,
            lambda: _safe_reconcile(
                ctx, "route_policy_states", mgmt, ("NSORoutePolicyState",), reconcile_route_policy, device, rp_doc
            ),
            epoch=dev_id,
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
            )
            # materialise dot1q subinterfaces (virtual interface + Interface.parent
            # link) BEFORE the IP reconcile, for the same ordering reason as SVIs.
            from .subinterface_reconciler import reconcile_subinterface

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
            )
            # Phase 2b: per-interface MTU read mirror (read-only display).
            from .interface_mtu_reconciler import reconcile_interface_mtu

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
            )
            # LACP/LAG bundle + member overlay states (interface-level).
            from .lacp_reconciler import reconcile_lag_config

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
            )
            sw_doc = client.get_switchport(dev_id)
            _gated(
                ctx,
                mgmt,
                "switchport",
                sw_doc,
                lambda: _safe_reconcile(
                    ctx, "switchport_states", mgmt, ("NSOSwitchportState",), reconcile_switchport, device, sw_doc
                ),
                epoch=dev_id,
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
            )
        if getattr(mgmt, "manage_l2", False):
            # Nokia L2 SAP overlays. Kept in the full reconcile (not just
            # on-expand) so the periodic sync-complete refresh keeps them current —
            # the tab reads these persisted rows without reconciling on expand.
            from .l2_service_reconciler import reconcile_l2_services

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
            from .interface_mtu_reconciler import reconcile_interface_mtu
            from .subinterface_reconciler import reconcile_subinterface
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
            )
            if interface_result.disposition in ("ran", "legacy"):
                ctx["interfaces"] = fetched_interfaces
                ctx["state"] = fetched_state
            svi_doc = client.get_svi(dev_id)  # before IPs
            _gated(
                ctx, mgmt, "svi", svi_doc, lambda: reconcile_svi(device, svi_doc), epoch=dev_id, ctx_key="svi_states"
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
            )
            # VLAN DB first so switchport vid lookups resolve in the per-device group.
            vlan_doc = client.get_vlan_database(dev_id)
            _gated(ctx, mgmt, "vlan", vlan_doc, lambda: reconcile_vlan_database(device, vlan_doc), epoch=dev_id)
            sw_doc = client.get_switchport(dev_id)
            _gated(
                ctx,
                mgmt,
                "switchport",
                sw_doc,
                lambda: reconcile_switchport(device, sw_doc),
                epoch=dev_id,
                ctx_key="switchport_states",
            )
        elif key == "interfaces":
            from .subinterface_reconciler import reconcile_subinterface
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
            )
            if interface_result.disposition in ("ran", "legacy"):
                ctx["interfaces"] = fetched_interfaces
                ctx["state"] = fetched_state
            svi_doc = client.get_svi(dev_id)  # before IPs
            _gated(
                ctx, mgmt, "svi", svi_doc, lambda: reconcile_svi(device, svi_doc), epoch=dev_id, ctx_key="svi_states"
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
            )
        elif key == "interface_ips":
            from .subinterface_reconciler import reconcile_subinterface
            from .svi_reconciler import reconcile_svi

            svi_doc = client.get_svi(dev_id)  # SVIs exist before IPs
            _gated(
                ctx, mgmt, "svi", svi_doc, lambda: reconcile_svi(device, svi_doc), epoch=dev_id, ctx_key="svi_states"
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
            )
        elif key == "lacp":
            from .lacp_reconciler import reconcile_lag_config

            lag_doc = client.get_lag_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "lag_config",
                lag_doc,
                lambda: reconcile_lag_config(device, lag_doc),
                epoch=dev_id,
                ctx_key="lacp_bundle_states",
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
            )
        elif key == "switchport":
            from .vlan_reconciler import reconcile_switchport, reconcile_vlan_database

            # VLAN DB first so switchport vid lookups resolve in the per-device group.
            vlan_doc = client.get_vlan_database(dev_id)
            _gated(ctx, mgmt, "vlan", vlan_doc, lambda: reconcile_vlan_database(device, vlan_doc), epoch=dev_id)
            sw_doc = client.get_switchport(dev_id)
            _gated(
                ctx,
                mgmt,
                "switchport",
                sw_doc,
                lambda: reconcile_switchport(device, sw_doc),
                epoch=dev_id,
                ctx_key="switchport_states",
            )
        elif key == "svi":
            from .svi_reconciler import reconcile_svi

            svi_doc = client.get_svi(dev_id)
            _gated(
                ctx, mgmt, "svi", svi_doc, lambda: reconcile_svi(device, svi_doc), epoch=dev_id, ctx_key="svi_states"
            )
        elif key == "subinterface":
            from .subinterface_reconciler import reconcile_subinterface

            sub_doc = client.get_subinterface(dev_id)
            _gated(
                ctx,
                mgmt,
                "subinterface",
                sub_doc,
                lambda: reconcile_subinterface(device, sub_doc),
                epoch=dev_id,
                ctx_key="subinterface_states",
            )
        elif key == "interface_mtu":
            from .interface_mtu_reconciler import reconcile_interface_mtu

            mtu_doc = client.get_interface_mtu(dev_id)
            _gated(
                ctx,
                mgmt,
                "interface_mtu",
                mtu_doc,
                lambda: reconcile_interface_mtu(device, mtu_doc),
                epoch=dev_id,
                ctx_key="interface_mtu_states",
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
            )
        elif key == "logging":
            log_doc = client.get_logging_config(dev_id)
            _gated(
                ctx,
                mgmt,
                "logging",
                log_doc,
                lambda: _reconcile_logging_config(device, log_doc),
                epoch=dev_id,
                ctx_key="logging_data",
            )
        elif key == "static":
            static_doc = client.get_static_routes(dev_id)
            _gated(
                ctx,
                mgmt,
                "static_route",
                static_doc,
                lambda: _reconcile_static_routes(device, static_doc),
                epoch=dev_id,
                ctx_key="static_routes",
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
            )
            ctx["bgp_peer_templates"] = list(
                NSOBGPPeerTemplateState.objects.filter(management=mgmt).select_related("template")
            )
        elif key == "bfd":
            from .bfd_reconciler import reconcile_bfd
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
            )
        elif key == "l2_services":
            # reconcile into native vpn.L2VPN + L2VPNTermination + NSOL2SapState
            # (value-aware drift/accept). The dot1q tag stays per-SAP interface-local encap.
            from .l2_service_reconciler import reconcile_l2_services

            l2_doc = client.get_l2_services(dev_id)
            _gated(
                ctx,
                mgmt,
                "l2_service",
                l2_doc,
                lambda: reconcile_l2_services(device, l2_doc),
                epoch=dev_id,
                ctx_key="l2_sap_states",
            )
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
    # "static_route" deliberately absent (#1502 Appendix S): a static-route row is settled
    # by the generation-correlated settlement consumer, which knows WHICH intent the result
    # is about. The two coarse channels here do not — a scope counter says the apply
    # succeeded, not that it carried this row's generation — so both were writing verdicts
    # on evidence they did not have. `_escalate_stuck_static_routes` is the backstop half's
    # replacement, and it runs only after the consumer has walked the feed.
    "l2_sap": "NSOL2SapState",
    # Levels only — NSOLoggingHostState still settles via reconcile-matching alone
    # (the pre-existing family behavior). The levels singleton needs the failure leg:
    # a CLOSED adapter write-gate fails the whole logging scope by design (NX-P4a),
    # and without apply_failed surfacing the row would read accepted/green forever.
    "logging": "NSOLoggingLevelState",
}


_GENERIC_APPLY_ERROR = "Apply reported a failure for this scope (see the adapter apply job)."


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
        counts = _counts_for(apply_result, scope)
        if _count_of(counts, "apply_failed") <= 0:
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
    """Parse an adapter job timestamp — canonical UTC isoformat + 'Z', optional fraction."""
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
        counts = _counts_for(result, scope)
        if sum(_count_of(counts, outcome) for outcome in counts) <= 0:
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


_STUCK_STATIC_ROUTE_ERROR = (
    "This route's intent was pushed as generation {generation} and the adapter has still "
    "reported no result naming that generation. The apply either never carried this route "
    "or its result never correlated — re-apply; the value is still safe in NetBox."
)

_UNCLOCKED_STATIC_ROUTE_ERROR = (
    "This route is applying with no generation clock, which is the state an upgrade leaves "
    "a row in that was already applying before generation-correlated settlement existed. No "
    "apply result can ever name its generation, so it can never settle — re-apply; the value "
    "is still safe in NetBox. (Running the static-route intent re-sync arms every such row.)"
)


def _escalate_stuck_static_routes(mgmt, *, adapter_device_id, apply_active: bool | None = None) -> None:
    """Static-route rows left 'deploying' past the grace with no correlated result → apply_failed.

    Static routes left :data:`_APPLY_DEPLOYING_SCOPES`, so :func:`_escalate_stuck_deploying`
    no longer judges them; this is that half's replacement. It is anchored on
    ``generation_started_at`` — the moment the generation this row is waiting for was armed
    — because every other overlay timestamp is rewritten by reconcile and so cannot date a
    generation.

    **Never call this directly.** :func:`~netbox_nso_plugin.settlement.settle_static_routes`
    owns the precondition — a feed walked to its end with nothing stalled — and a row judged
    on an unwalked page is a false red on a device the adapter already reported ``in_sync``.

    One row is excluded even then: a row carrying a ``last_result_advisory``, which means a
    result **did** correlate to this generation and deliberately did not settle it
    (``unproven``, or a fingerprint the row is not waiting for). The clock says nothing there
    that the advisory has not said better, and an edit clears the advisory with the
    generation, so it can never go stale.

    A ``deploying`` row whose ``generation_started_at`` is NULL escalates with a **distinct**
    reason rather than waiting on a clock it has not got. It is an impossible state once the
    rollout backfill has run, and the timestamp comparison is NULL-false — so skipping it is
    how a pre-upgrade row stays ``deploying`` forever, which is exactly what #1502 exists to
    end. Fail loudly on the state the backfill did not reach.

    And nothing is judged at all while an apply is in flight: ``_prepare_apply`` re-marks
    rows ``deploying`` without re-stamping the generation clock, so a route staged long
    before its Apply looks stuck the moment that Apply starts. Failing it there is
    unrecoverable — the apply's own ``in_sync`` cannot lift a row out of ``apply_failed``.
    A caller that already fetched the job state **for this adapter device** passes it as
    *apply_active*; otherwise the lookup is made here, and only when there is something to
    escalate, so a quiet device on the maintenance tick costs no adapter call.
    """
    from django.db.models import Q
    from django.utils import timezone

    from . import models
    from . import status_machine as sm

    cutoff = timezone.now() - _stuck_deploying_grace()
    rows = list(
        models.NSOStaticRouteState.objects.filter(
            Q(generation_started_at__lt=cutoff) | Q(generation_started_at__isnull=True),
            management=mgmt,
            status="deploying",
            last_result_advisory="",
        )
    )
    if not rows:
        return
    if apply_active is None:
        _job, apply_active = _apply_job_state(adapter_device_id)
    if apply_active:
        logger.debug(
            "nso reconcile: static-route escalation stands down for adapter device %s — an apply is in flight",
            adapter_device_id,
        )
        return
    for row in rows:
        new_status = sm.on_apply_result(row.status, ok=False)
        if new_status == row.status:
            continue
        # Compare-and-set through .update(): the status is shared with the operator Apply
        # flow and with the settlement consumer, and a save() here would fire the row's
        # intent push on an RQ worker with a cold cache — a full static-route PUT that, under
        # adapter auto-apply, starts another apply for the row just declared failed.
        unclocked = row.generation_started_at is None
        matched = models.NSOStaticRouteState.objects.filter(pk=row.pk, status=row.status).update(
            status=new_status,
            last_apply_error=(
                _UNCLOCKED_STATIC_ROUTE_ERROR
                if unclocked
                else _STUCK_STATIC_ROUTE_ERROR.format(generation=row.intent_generation)
            ),
        )
        if not matched:
            continue
        if unclocked:
            logger.error(
                "nso reconcile: NSOStaticRouteState %s is deploying with no generation clock — "
                "an upgrade left it uncorrelatable and the rollout backfill did not reach it; "
                "escalated to apply_failed",
                row.pk,
            )
            continue
        logger.warning(
            "nso reconcile: NSOStaticRouteState %s stuck deploying at generation %s past the "
            "grace with no correlated settlement — escalated to apply_failed",
            row.pk,
            row.intent_generation,
        )


_TERMINAL_GENERATION_STATUSES = frozenset({"settled", "failed", "outcome_unknown", "abandoned"})


def _apply_job_state(adapter_device_id) -> tuple[dict | None, bool]:
    """Best-effort: (most recent terminal apply job, may its generation chain be active).

    The jobs surface supplies the last apply result and visible queued or running work.
    The generations surface covers the barrier interval after a head job finishes and
    before its pending successor gets a job.

    A failed or malformed probe answers the second question with **True**, because it did
    not answer it at all. Its only consumer is a fail-closed gate. Standing down costs one
    tick, while escalating a row whose Apply is running is unrecoverable.
    """
    from . import adapter_client as client

    try:
        jobs = client.list_jobs(adapter_device_id)  # most-recent-first
    except Exception as exc:  # noqa: BLE001 — adapter transient; settling is best-effort
        logger.warning(
            "nso reconcile: could not read adapter device %s's jobs (%s) — treating apply activity as unknown",
            adapter_device_id,
            exc,
        )
        return None, True
    last, active = None, False
    for job in jobs or []:
        job_type = job.get("type")
        if job.get("status") in ("queued", "running"):
            if job_type in ("apply", "removal"):
                active = True
        elif job_type == "apply" and last is None and job.get("status") in ("succeeded", "failed"):
            last = job
    try:
        generations = client.list_device_generations(adapter_device_id)
    except Exception as exc:  # noqa: BLE001 (adapter transient; the gate fails closed)
        logger.warning(
            "nso reconcile: could not read adapter device %s's generations (%s), treating apply activity as unknown",
            adapter_device_id,
            exc,
        )
        return last, True
    current_chain = []
    if generations:
        latest = generations[-1]
        cohort = latest.get("settlement_cohort")
        current_chain = (
            [latest]
            if cohort is None
            else [generation for generation in generations if generation.get("settlement_cohort") == cohort]
        )
    for generation in current_chain:
        if generation.get("status") not in _TERMINAL_GENERATION_STATUSES:
            active = True
            break
    return last, active


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
    counts = _counts_for(job.get("result"), "route_policy")
    applied = _count_of(counts, "in_sync")
    failed = _count_of(counts, "apply_failed")
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
    from .deployment import DeploymentQuiesced
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

    # Step 4: after the post-sync reconcile, walk this device's settlement feed (the
    # production carrier for #1502's consumer), settle any rows left 'deploying' whose
    # scope's last apply reported a failure → apply_failed (no longer stuck), escalate
    # rows a SUCCEEDED apply left 'deploying' past the grace (silent drop, #26), and
    # record the route-policy apply outcome in the netbox-routing journals (idempotent).
    try:
        # Reverse one-to-one: a plain attribute read raises for an unmanaged device.
        mgmt = getattr(device, "nso_management", None)
        if mgmt is not None and mgmt.adapter_device_id is not None:
            adapter_device_id = mgmt.adapter_device_id
            job, apply_active = _apply_job_state(adapter_device_id)
            # BEFORE the coarse settle and both backstops, in the same invocation.
            # `_escalate_stuck_deploying`'s own justification is that the settling step "runs
            # right before this"; static routes no longer settle by reconcile, so without
            # this walk the static backstop would fail rows the adapter reported in_sync.
            # `settle_static_routes` owns the escalation and every precondition it needs,
            # including the apply-in-flight check — an error here therefore stands the static
            # backstop down and touches nothing else.
            try:
                # The pair names the device the state was read for; the consumer locks its own.
                settle_static_routes(mgmt, apply_state=(adapter_device_id, apply_active))
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
                job, apply_active = _apply_job_state(mgmt.adapter_device_id)
                # These are mirror writes, not operator intent: without the suppression the
                # first status flip's push-on-save signal refuses (no writer transaction) and
                # takes the rest of Step 4 with it.
                with suppress_intent_push():
                    _settle_apply_failures(mgmt, job.get("result") if job else None, job)
                    if not apply_active:
                        _escalate_stuck_deploying(mgmt, job)
                    _journal_route_policy_apply(mgmt, job)
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
