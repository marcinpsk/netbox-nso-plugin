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
