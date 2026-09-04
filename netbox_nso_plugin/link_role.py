# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Link-role resolver — map an interface to its governing NSOLinkRole + peer end.

``resolve_role(interface)`` is the single entry point the provisioner uses to
answer "what role governs this interface, and (for p2p) what is the other end?".
It reuses M8's ``find_peer`` (derived_intent) for cable traversal and, as an
opt-in back-compat fallback, M13's ``classify_interface`` heuristic (ip_autoassign).

``intent_bundle(role)`` flattens a role's three outputs (IP pools, description
template, IGP) into a small structure the Phase 3-5 consumers read from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolSpec:
    """Resolved IP pool reference for one address family.

    ``prefix`` (an ``ipam.Prefix``) wins over ``role_slug`` when both are set;
    ``mask`` is the p2p child length (``None`` → reuse the M13 per-pool/default).
    """

    family: str  # "ipv4" | "ipv6"
    prefix: object | None
    role_slug: str
    mask: int | None


@dataclass(frozen=True)
class RoleIntent:
    """Flattened view of an ``NSOLinkRole``'s outputs, for the consumers."""

    role: object
    link_type: str
    pools: tuple[PoolSpec, ...]
    description_template: str
    igp: str


def intent_bundle(role) -> RoleIntent:
    """Return the flattened :class:`RoleIntent` for *role* (opted-in families only)."""
    pools: list[PoolSpec] = []
    if role.assign_ipv4:
        pools.append(PoolSpec("ipv4", role.ipv4_pool_prefix, role.ipv4_pool_role, role.ipv4_mask))
    if role.assign_ipv6:
        pools.append(PoolSpec("ipv6", role.ipv6_pool_prefix, role.ipv6_pool_role, role.ipv6_mask))
    return RoleIntent(
        role=role,
        link_type=role.link_type,
        pools=tuple(pools),
        description_template=role.description_template,
        igp=role.igp,
    )


def _derived_fallback_enabled() -> bool:
    """Whether the config-gated classify_interface fallback is on (default off)."""
    from django.apps import apps

    cfg = apps.get_app_config("netbox_nso_plugin")
    return bool(getattr(cfg, "_link_role_derived_fallback", False))


def resolve_role(interface):
    """Return ``(NSOLinkRole | None, other_end | None)`` for *interface*.

    Resolution order (first match wins):

    1. A **direct interface** assignment (single-ended) → ``(role, None)``.
    2. The interface's **cable** carries an assignment (p2p) → ``(role, peer)``
       where ``peer`` is the far-end interface via ``find_peer`` (``None`` when the
       cable has no single interface peer — a clean skip, not an error).
    3. Optional, config-gated **derived fallback**: ``classify_interface`` →
       an *enabled* role whose slug equals the classification. Off by default.

    Returns ``(None, None)`` when no role governs the interface. Assigned roles are
    returned regardless of ``enabled`` (the provisioner enforces it, so diagnostics
    can distinguish "assigned but disabled" from "unassigned"); the derived
    fallback only ever matches enabled roles.
    """
    from .models import NSOLinkRole, NSOLinkRoleAssignment

    # 1. Direct interface assignment (single-ended) — takes precedence over cable.
    direct = NSOLinkRoleAssignment.objects.filter(interface=interface).select_related("role").first()
    if direct is not None:
        return direct.role, None

    # 2. Cable assignment (p2p) — both ends share the one assignment on the cable.
    if interface.cable_id is not None:
        cable_asgn = NSOLinkRoleAssignment.objects.filter(cable_id=interface.cable_id).select_related("role").first()
        if cable_asgn is not None:
            from .derived_intent import find_peer

            return cable_asgn.role, find_peer(interface)

    # 3. Derived fallback (opt-in) — back-compat with M13's heuristic classification.
    if _derived_fallback_enabled():
        from .ip_autoassign import classify_interface

        classification = classify_interface(interface)
        if classification:
            role = NSOLinkRole.objects.filter(slug=classification, enabled=True).first()
            if role is not None:
                other = None
                if role.link_type == "p2p":
                    from .derived_intent import find_peer

                    other = find_peer(interface)
                return role, other

    return None, None


# ── Description consumer (Phase 4) ──────────────────────────────────────────────


def apply_description_for_role(interface, role, other_end=None, push=True, *, mgmt=None) -> dict:
    """Render + own the interface description from *role*'s template. Mutates state.

    Sets ``dcim.Interface.description`` to the rendered template and marks an
    ``NSOInterfaceState`` (attribute ``description``) as ``accepted`` so the change
    is owned and pushed via the existing interface-intent pipe (reuses the M8
    description contract). ``p2p`` roles use *other_end* for the ``peer_*``
    placeholders; ``single`` roles render with those blank. A role with no template
    is a no-op. *push* False skips the immediate adapter push (the orchestrator
    defers pushes to after an atomic commit). Returns ``{interface, changed,
    description, skipped, error}``.
    """
    from django.utils import timezone

    from .derived_intent import render_template
    from .intent_state import MutationFootprint, SourceRow, intent_transaction
    from .ip_autoassign import _resolve_managed_mgmt
    from .models import NSOInterfaceState
    from .signals import _schedule_intent_push, suppress_intent_push

    result = {"interface": str(interface), "changed": False, "description": None, "skipped": None, "error": None}

    if not role.description_template:
        result["skipped"] = "role does not manage the description"
        return result

    if mgmt is None:
        mgmt, reason = _resolve_managed_mgmt(interface.device)
        if mgmt is None:
            result["error"] = reason
            return result

    new_value = render_template(role.description_template, self_iface=interface, peer_iface=other_end)
    changed = interface.description != new_value

    footprint = MutationFootprint.for_keys(
        {(mgmt.device_id, "interface")},
        source_rows=(SourceRow("dcim.interface", interface.pk),),
        overlay_rows=(SourceRow(NSOInterfaceState._meta.label_lower, None),),
    )
    with intent_transaction(footprint):
        with suppress_intent_push():
            if changed:
                interface.description = new_value
                interface.save(update_fields=["description"])
            NSOInterfaceState.objects.update_or_create(
                interface=interface,
                attribute="description",
                defaults={"status": "accepted", "accepted_at": timezone.now()},
            )
        if push:
            _schedule_intent_push((mgmt.device_id, "interface"))

    result["changed"] = changed
    result["description"] = new_value
    return result


# ── IGP consumer (Phase 5) ──────────────────────────────────────────────────────


def enable_igp_for_role(interface, role, push=True, *, mgmt=None) -> dict:
    """Enable *interface* for the role's IGP (IS-IS or OSPF). Mutates overlay state.

    Creates/updates the matching interface overlay as ``accepted`` — an
    ``NSOISISInterfaceState`` (af ``ipv4``) with the role's circuit-type / metric /
    passive / process-tag, or an ``NSOOSPFInterfaceState`` with the role's area /
    network-type / cost / passive / process-id — then pushes via the existing IGP
    intent pipe. ``igp=none`` is a no-op. *push* False skips the immediate adapter
    push (the orchestrator defers pushes to after an atomic commit). Returns
    ``{interface, igp, enabled, skipped, error}``. One end only; the orchestrator
    runs it on both.
    """
    from django.utils import timezone

    from .intent_state import MutationFootprint, SourceRow, intent_transaction
    from .ip_autoassign import _resolve_managed_mgmt
    from .models import NSOISISInterfaceState, NSOOSPFInterfaceState
    from .signals import _schedule_intent_push, suppress_intent_push

    result = {"interface": str(interface), "igp": role.igp, "enabled": False, "skipped": None, "error": None}

    if role.igp == "none":
        result["skipped"] = "role does not manage an IGP"
        return result

    if mgmt is None:
        mgmt, reason = _resolve_managed_mgmt(interface.device)
        if mgmt is None:
            result["error"] = reason
            return result

    now = timezone.now()
    if role.igp == "isis":
        scope = "isis"
        state_model = NSOISISInterfaceState
    else:
        scope = "ospf"
        state_model = NSOOSPFInterfaceState
    footprint = MutationFootprint.for_keys(
        {(mgmt.device_id, scope)},
        overlay_rows=(SourceRow(state_model._meta.label_lower, None),),
    )
    with intent_transaction(footprint):
        if scope == "isis":
            with suppress_intent_push():
                NSOISISInterfaceState.objects.update_or_create(
                    management=mgmt,
                    interface=interface,
                    af="ipv4",
                    defaults={
                        "process_tag": role.isis_process_tag,
                        "circuit_type": role.isis_circuit_type,
                        "metric": role.isis_metric,
                        "passive": role.isis_passive,
                        "status": "accepted",
                        "accepted_at": now,
                    },
                )
        else:  # ospf
            with suppress_intent_push():
                NSOOSPFInterfaceState.objects.update_or_create(
                    management=mgmt,
                    interface=interface,
                    defaults={
                        "process_id": role.ospf_process_id or None,
                        "area_id": role.ospf_area,
                        "network_type": role.ospf_network_type,
                        "passive": role.ospf_passive,
                        "cost": role.ospf_cost,
                        "status": "accepted",
                        "accepted_at": now,
                    },
                )
        if push:
            # Appended, never pushed around the outbox: an in-protocol send is a claimed,
            # sequenced operation, and the drain runs on this transaction's commit.
            _schedule_intent_push((mgmt.device_id, scope))

    result["enabled"] = True
    return result


# ── Link orchestrator (Phase 6) ─────────────────────────────────────────────────


class _ProvisionRollback(Exception):
    """Internal signal to roll back the provisioning transaction on any consumer error."""


def _provisioned_scopes(role) -> list[str]:
    """Return the delivery scopes one role provisioning changes."""
    scopes = []
    if role.assign_ipv4 or role.assign_ipv6:
        scopes.append("ip")
    if role.description_template:
        scopes.append("interface")
    if role.igp == "isis":
        scopes.append("isis")
    elif role.igp == "ospf":
        scopes.append("ospf")
    return scopes


def _push_provisioned(role, device_ids) -> None:
    """After a successful commit, push each affected (device, category) intent once."""
    from . import drain
    from .models import NSODeviceManagement

    for device_id in device_ids:
        mgmt = NSODeviceManagement.objects.filter(device_id=device_id).first()
        if mgmt is None or mgmt.adapter_device_id is None:
            continue
        for scope in _provisioned_scopes(role):
            try:
                # A forced claim: provisioning must always land its computed intent, and an
                # acknowledged baseline matching this snapshot would otherwise drop the
                # re-provision silently (intent-integrity: no silent drop).
                drain.push_now(device_id, scope, force=True)
            except Exception as exc:  # noqa: BLE001 — adapter may be down; state already owned
                logger.warning("provision_link_role: push failed for device %s: %s", device_id, exc)


def _enqueue_provisioned(role, device_ids) -> None:
    """Record each affected delivery scope before the provisioning commit."""
    from . import outbox

    for device_id in device_ids:
        for scope in _provisioned_scopes(role):
            outbox.enqueue(device_id, scope)


def _provision_link_footprint(role, pairs, device_ids):
    """Return the complete renderer footprint for one link-role provision."""
    from .intent_state import MutationFootprint, SourceRow

    source_rows = [SourceRow("dcim.interface", end.pk) for end, _peer in pairs]
    overlay_rows = []
    if role.assign_ipv4 or role.assign_ipv6:
        source_rows.append(SourceRow("ipam.ipaddress", None))
        overlay_rows.append(SourceRow("netbox_nso_plugin.nsointerfaceipstate", None))
    if role.description_template:
        overlay_rows.append(SourceRow("netbox_nso_plugin.nsointerfacestate", None))
    if role.igp == "isis":
        overlay_rows.append(SourceRow("netbox_nso_plugin.nsoisisinterfacestate", None))
    elif role.igp == "ospf":
        overlay_rows.append(SourceRow("netbox_nso_plugin.nsoospfinterfacestate", None))
    return MutationFootprint.for_keys(
        {(device_id, scope) for device_id in device_ids for scope in _provisioned_scopes(role)},
        source_rows=source_rows,
        overlay_rows=overlay_rows,
    )


def provision_link_role(interface) -> dict:
    """Provision a whole link/interface from its resolved ``NSOLinkRole``.

    Resolves the role (and, for p2p, the peer via the cable), then runs the IP +
    description + IGP consumers on both ends **inside one atomic envelope with
    adapter pushes deferred**. On any consumer error the whole transaction is rolled
    back (no partial device state, no adapter push); on success each affected
    (device, category) intent is pushed once. Returns a summary::

        {role, provisioned, rolled_back, skipped, ip, descriptions, igp, errors}

    A missing/disabled role, a p2p role with no cable peer, or an end whose device
    is not NSO-managed is a clean **skip** (nothing written), distinct from a
    partial-failure **rollback**.
    """
    from django.db import transaction
    from ipam.models import Prefix

    from .intent_state import intent_transaction
    from .ip_autoassign import _resolve_role_pool, assign_ips_for_role
    from .signals import suppress_intent_push

    role, other_end = resolve_role(interface)
    summary = {
        "role": role.slug if role else None,
        "provisioned": False,
        "rolled_back": False,
        "skipped": None,
        "ip": None,
        "descriptions": [],
        "igp": [],
        "errors": [],
        # Interface pks this call governs (both ends for p2p). Callers processing a
        # batch use it to dedup: a link selected from both ends must be provisioned
        # once, not once per end.
        "ends": [interface.pk],
    }
    if role is None:
        summary["skipped"] = "no link role assigned"
        return summary
    if not role.enabled:
        summary["skipped"] = "link role is disabled"
        return summary

    if role.link_type == "p2p":
        if other_end is None:
            summary["skipped"] = "p2p role but no cable peer"
            return summary
        pairs = [(interface, other_end), (other_end, interface)]
    else:
        pairs = [(interface, None)]
    summary["ends"] = sorted({end.pk for end, _peer in pairs})
    device_ids = {end.device_id for end, _peer in pairs}

    # Pre-flight: every end must be NSO-managed — resolve each device's management
    # row once and thread it to the consumers (they'd otherwise re-query it ~10× per
    # link). Skip rather than half-provision.
    from .ip_autoassign import _resolve_managed_mgmt

    mgmt_by_device: dict[int, object] = {}
    for end, _peer in pairs:
        if end.device_id in mgmt_by_device:
            continue
        end_mgmt, _reason = _resolve_managed_mgmt(end.device)
        if end_mgmt is None:
            summary["skipped"] = f"{end} is not NSO-managed"
            return summary
        mgmt_by_device[end.device_id] = end_mgmt

    site = getattr(interface.device, "site", None)
    resolved_pool_ids = {}
    for spec in intent_bundle(role).pools:
        pool = _resolve_role_pool(spec, None, site)
        resolved_pool_ids[spec.family] = pool.pk if pool is not None else None

    def _collect(res, kind):
        if isinstance(res.get("errors"), list):
            summary["errors"].extend({"kind": kind, **e} for e in res["errors"])
        elif res.get("error"):
            summary["errors"].append({"kind": kind, "reason": res["error"], "interface": res.get("interface")})

    try:
        with transaction.atomic():
            locked_pools = {
                pool.pk: pool
                for pool in Prefix.objects.select_for_update(of=("self",))
                .filter(pk__in={pk for pk in resolved_pool_ids.values() if pk is not None})
                .order_by("pk")
            }
            resolved_pools = {family: locked_pools.get(pool_id) for family, pool_id in resolved_pool_ids.items()}
            with intent_transaction(_provision_link_footprint(role, pairs, device_ids)):
                with suppress_intent_push():
                    ip_res = assign_ips_for_role(
                        interface,
                        role,
                        other_end,
                        push=False,
                        mgmt=mgmt_by_device[interface.device_id],
                        peer_mgmt=(mgmt_by_device.get(other_end.device_id) if other_end is not None else None),
                        resolved_pools=resolved_pools,
                    )
                    summary["ip"] = ip_res
                    _collect(ip_res, "ip")
                    for end, peer in pairs:
                        end_mgmt = mgmt_by_device[end.device_id]
                        d = apply_description_for_role(end, role, peer, push=False, mgmt=end_mgmt)
                        summary["descriptions"].append(d)
                        _collect(d, "description")
                        g = enable_igp_for_role(end, role, push=False, mgmt=end_mgmt)
                        summary["igp"].append(g)
                        _collect(g, "igp")
                if summary["errors"]:
                    raise _ProvisionRollback()
                _enqueue_provisioned(role, device_ids)
    except _ProvisionRollback:
        summary["rolled_back"] = True
        return summary

    transaction.on_commit(lambda role=role, device_ids=tuple(device_ids): _push_provisioned(role, device_ids))
    summary["provisioned"] = True
    return summary
