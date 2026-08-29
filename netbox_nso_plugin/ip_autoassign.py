# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""IP auto-assignment from purpose Prefix pools.

Two allocation shapes, both fully implemented:

* **Single-ended** (loopback / access): draw one host per family, fill-empty-only.
* **P2P** (core links): reserve-then-activate — carve a child prefix, reserve both
  host halves, link the two ends, push both, roll back on partial failure.

Public entry points:

* :func:`auto_assign_ip` — classify the interface (M13 heuristic) then allocate.
* :func:`assign_ips_for_role` — allocate from an :class:`~netbox_nso_plugin.models.NSOLinkRole`'s
  configured pool + mask (link-role provisioning); reuses the same carve/reserve/
  rollback machinery, parameterized by the role instead of the hardcoded ``p2p-core``.
"""

from __future__ import annotations

import copy
import logging

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Default mapping from classification → Prefix role slug.  Operators may
# override via PLUGINS_CONFIG["netbox_nso_plugin"]["ip_pool_role_slugs"].
_DEFAULT_ROLE_SLUGS: dict[str, str] = {
    "loopback": "loopback",
    "access": "access-lan",
    "p2p-core": "p2p-core",
}

# Interface name prefixes (case-insensitive) that identify loopback interfaces.
_LOOPBACK_PREFIXES: tuple[str, ...] = ("loopback", "lo")

# Device role slugs whose devices qualify as P2P core peers.  Empty by default —
# operators opt-in via PLUGINS_CONFIG["netbox_nso_plugin"]["p2p_core_device_role_slugs"].
_DEFAULT_P2P_CORE_DEVICE_ROLE_SLUGS: frozenset[str] = frozenset()


# ── P2P helpers ───────────────────────────────────────────────────────────────


def _get_p2p_core_device_role_slugs() -> frozenset:
    """Return the set of device-role slugs that classify both ends as p2p-core."""
    from django.apps import apps

    cfg = apps.get_app_config("netbox_nso_plugin")
    return frozenset(getattr(cfg, "_p2p_core_device_role_slugs", _DEFAULT_P2P_CORE_DEVICE_ROLE_SLUGS))


# ── Classification ────────────────────────────────────────────────────────────


def classify_interface(interface) -> str | None:
    """Return the pool classification for *interface*.

    Returns one of ``"loopback"``, ``"p2p-core"``, or ``"access"``.

    Resolution order:
    1. Explicit tag whose slug matches a known classification key.
    2. Name prefix (Loopback / lo → ``"loopback"``).
    3. Cable-peer + device-role heuristic: if ``p2p_core_device_role_slugs``
       is configured and a single cable peer exists whose device (and this
       device) both carry a role slug in that set → ``"p2p-core"``.
    4. Default: ``"access"`` for any other interface.
    """
    # 1. Tag override — look for a tag with slug matching a classification key.
    known = set(_DEFAULT_ROLE_SLUGS.keys())
    for tag in interface.tags.all():
        slug = tag.slug.lower()
        if slug in known:
            return slug
        # Also accept "ip-pool-role:loopback"-style compound slugs.
        if slug.startswith("ip-pool-role:"):
            candidate = slug.split(":", 1)[1]
            if candidate in known:
                return candidate

    # 2. Loopback by name.
    name_lower = interface.name.lower()
    if any(name_lower.startswith(p) for p in _LOOPBACK_PREFIXES):
        return "loopback"

    # 3. P2P core via cable-peer + device-role heuristic.
    core_slugs = _get_p2p_core_device_role_slugs()
    if core_slugs:
        from .derived_intent import find_peer

        peer = find_peer(interface)
        if peer is not None:
            role_a = getattr(getattr(interface, "device", None), "role", None)
            role_b = getattr(getattr(peer, "device", None), "role", None)
            if role_a is not None and role_b is not None and role_a.slug in core_slugs and role_b.slug in core_slugs:
                return "p2p-core"

    # 4. Default: access.
    return "access"


# ── Pool matching ─────────────────────────────────────────────────────────────


def _select_pool_by_role_slug(role_slug: str, vrf, site, family: str):
    """Return the first available Prefix pool with ``role__slug == role_slug``.

    The shared pool-selection core: family + VRF (fall back to global) + site-scope
    narrowing, first pool with a free host. Used both by :func:`find_pool` (after
    mapping a classification to a slug) and by the link-role pool resolver (which
    passes the role's ``ipvX_pool_role`` slug verbatim).
    """
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import Prefix  # NetBox IPAM model

    af = 4 if family == "ipv4" else 6
    base_qs = Prefix.objects.filter(role__slug=role_slug, prefix__family=af)

    def _with_vrf(qs):
        if vrf is not None:
            narrowed = qs.filter(vrf=vrf)
            return narrowed if narrowed.exists() else qs.filter(vrf__isnull=True)
        return qs.filter(vrf__isnull=True)

    def _with_site(qs):
        if site is not None:
            try:
                site_ct = ContentType.objects.get_for_model(site)
                narrowed = qs.filter(scope_type=site_ct, scope_id=site.pk)
                return narrowed if narrowed.exists() else qs
            except Exception:
                pass
        return qs

    qs = _with_site(_with_vrf(base_qs))
    for pool in qs.order_by("prefix"):
        if pool.get_first_available_ip() is not None:
            return pool
    return None


def find_pool(classification: str, vrf, site, family: str):
    """Find the best-matching Prefix pool for (classification, vrf, site, family).

    Maps *classification* → a Prefix role slug (via the configurable role-slug map),
    then delegates to :func:`_select_pool_by_role_slug`.

    Returns a ``Prefix`` instance or ``None`` (no pool / all exhausted).
    """
    from django.apps import apps

    cfg = apps.get_app_config("netbox_nso_plugin")
    role_slug_map: dict[str, str] = getattr(cfg, "_ip_pool_role_slugs", _DEFAULT_ROLE_SLUGS)
    role_slug = role_slug_map.get(classification)
    if role_slug is None:
        logger.debug("ip_autoassign: no role slug for classification %r", classification)
        return None
    return _select_pool_by_role_slug(role_slug, vrf, site, family)


def _prefix_family(prefix) -> int | None:
    """Return 4 or 6 for a Prefix, tolerating string-vs-netaddr prefix values."""
    try:
        return prefix.prefix.version
    except Exception:
        fam = getattr(prefix, "family", None)
        return fam if fam in (4, 6) else None


def _resolve_role_pool(spec, vrf, site):
    """Resolve a :class:`~netbox_nso_plugin.link_role.PoolSpec` to a Prefix pool.

    The explicit ``prefix`` wins (reloaded fresh and verified to match the spec's
    family — an FK-cached Prefix can carry an unconverted ``prefix`` value that
    breaks ``get_first_available_ip``); otherwise the ``role_slug`` is matched via
    :func:`_select_pool_by_role_slug`. Returns ``None`` when neither yields a pool.
    """
    from ipam.models import Prefix

    af = 4 if spec.family == "ipv4" else 6
    if spec.prefix is not None:
        pool = Prefix.objects.filter(pk=spec.prefix.pk).first()
        if pool is None or _prefix_family(pool) != af:
            return None
        return pool
    if spec.role_slug:
        return _select_pool_by_role_slug(spec.role_slug, vrf, site, spec.family)
    return None


# ── P2P child-prefix carving ──────────────────────────────────────────────────


def _get_p2p_child_length(pool, family: str, override: int | None = None) -> int:
    """Resolve the P2P child-prefix length for *pool* and *family*.

    Priority:
    0. An explicit *override* (e.g. an NSOLinkRole's ``ipv4_mask``/``ipv6_mask``).
    1. Per-pool custom field ``p2p_child_length_v4`` / ``p2p_child_length_v6``.
    2. Plugin-wide config ``p2p_child_length_v4`` / ``p2p_child_length_v6``.
    3. Built-in defaults: 31 (IPv4) / 127 (IPv6).
    """
    from django.apps import apps

    if override is not None:
        return int(override)

    family_key = "v4" if family == "ipv4" else "v6"
    defaults = {"v4": 31, "v6": 127}
    cf_key = f"p2p_child_length_{family_key}"

    cf_val = pool.custom_field_data.get(cf_key)
    if cf_val is not None:
        try:
            return int(cf_val)
        except (ValueError, TypeError):
            pass

    cfg = apps.get_app_config("netbox_nso_plugin")
    cfg_lengths = getattr(cfg, "_p2p_child_lengths", {})
    if family_key in cfg_lengths:
        return int(cfg_lengths[family_key])

    return defaults[family_key]


def carve_p2p_child(pool, family: str, override_mask: int | None = None):
    """Carve a child prefix from P2P pool *pool* for address-family *family*.

    Returns ``(child_prefix, host_a_str, host_b_str)`` where ``host_a_str``
    and ``host_b_str`` are ``'IP/length'`` strings ready for use as
    ``IPAddress.address``.  Returns ``None`` when the pool has no available
    block large enough for the configured child length.

    *override_mask* forces the child length (an NSOLinkRole's mask); when ``None``
    the length is resolved from the pool CF / config / default.

    The carved ``Prefix`` is written to IPAM with ``status="reserved"``
    so that concurrent allocators cannot re-use the same range.
    For P2P states the ``source_pool`` FK points to the **carved child**
    (not the top-level pool) so that rollback can cleanly delete it.
    """
    from ipam.models import Prefix

    length = _get_p2p_child_length(pool, family, override_mask)
    pool.refresh_from_db()  # ensure pool.prefix is netaddr.IPNetwork, not a string
    available = pool.get_available_prefixes()

    child_network = None
    for cidr in available.iter_cidrs():
        if cidr.prefixlen <= length:
            child_network = next(cidr.subnet(length))
            break
    if child_network is None:
        return None

    child_prefix = Prefix.objects.create(
        prefix=str(child_network),
        vrf=pool.vrf,
        status="reserved",
        description="P2P link (auto-assigned)",
    )
    child_prefix.refresh_from_db()  # ensure prefix is netaddr.IPNetwork for get_available_ips()

    hosts = list(child_prefix.get_available_ips())
    if len(hosts) < 2:
        child_prefix.delete()
        return None

    child_len = child_prefix.prefix.prefixlen
    return child_prefix, f"{hosts[0]}/{child_len}", f"{hosts[1]}/{child_len}"


# ── Rollback ──────────────────────────────────────────────────────────────────


def rollback_auto_assigned(state) -> None:
    """Unassign and delete an auto-assigned IP address from *state*.

    Clears the IPAddress assignment from the interface, deletes the IPAddress
    object, then deletes the ``NSOInterfaceIPState`` row.  Used on
    ``apply_failed`` or explicit operator de-allocation.

    For P2P states, one immutable footprint covers both ends and the shared
    carved child prefix. A failure rolls the complete cleanup back.
    """
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from django.db import transaction
    from ipam.models import IPAddress

    from .renderer_writer import (
        RendererMutationPlan,
        planned_delete,
        planned_set_update,
        renderer_mirror_writes,
        renderer_writes,
    )
    from .signals import suppress_intent_push

    if not state.auto_assigned:
        return

    states = [state]
    source_pool = state.source_pool
    is_p2p = state.allocation_kind == state.ALLOCATION_KIND_P2P
    if is_p2p:
        peer = type(state).objects.filter(pk=state.peer_state_id).first()
        if peer is not None:
            states.append(peer)
    interface_type = ContentType.objects.get_for_model(Interface)
    ip_addresses = []
    for candidate in states:
        vrf_obj = None
        if candidate.vrf:
            from ipam.models import VRF

            vrf_obj = VRF.objects.filter(name=candidate.vrf).first()
        ip_address = IPAddress.objects.filter(
            address=candidate.address,
            vrf=vrf_obj,
            assigned_object_type=interface_type,
            assigned_object_id=candidate.interface_id,
        ).first()
        if ip_address is not None:
            ip_addresses.append(ip_address)

    with transaction.atomic(), suppress_intent_push():
        if is_p2p and source_pool is not None:
            source_pool = type(source_pool).objects.select_for_update().get(pk=source_pool.pk)
            pool_plan = RendererMutationPlan.build(deletes=(planned_delete(source_pool),))
            pool_mutation = (
                renderer_writes(pool_plan) if pool_plan.changes_content else renderer_mirror_writes(pool_plan)
            )
            with pool_mutation as writer:
                writer.delete(source_pool)
            source_pool = None
            states = list(type(state).objects.filter(pk__in=[candidate.pk for candidate in states]).order_by("pk"))

        if len(states) > 1:
            clear_plan = RendererMutationPlan.build(
                set_updates=(
                    planned_set_update(
                        type(state).objects.filter(pk__in=[candidate.pk for candidate in states]),
                        peer_state=None,
                    ),
                ),
            )
            clear_mutation = (
                renderer_writes(clear_plan) if clear_plan.changes_content else renderer_mirror_writes(clear_plan)
            )
            with clear_mutation as writer:
                clear_write = next(write for write in clear_plan.write_set if write.operation == "set_update")
                writer.set_update(type(state), clear_write, peer_state=None)
            states = list(type(state).objects.filter(pk__in=[candidate.pk for candidate in states]).order_by("pk"))

        plan = RendererMutationPlan.build(
            deletes=(planned_delete(candidate) for candidate in (*ip_addresses, *states)),
        )
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
        with mutation as writer:
            for ip_address in ip_addresses:
                writer.delete(ip_address)
            for candidate in states:
                writer.delete(candidate)


# ── P2P allocation helper ─────────────────────────────────────────────────────


def _suppress_ip_intent_push():
    """Context manager: suppress `_on_ip_address_save` intent pushes.

    Set during P2P IPAddress pair reservation so the signal does not fire
    premature one-sided pushes before ``peer_state`` is linked.
    """
    from contextlib import contextmanager

    from .signals import _p2p_allocation_active

    @contextmanager
    def _ctx():
        prev = getattr(_p2p_allocation_active, "active", False)
        _p2p_allocation_active.active = True
        try:
            yield
        finally:
            _p2p_allocation_active.active = prev

    return _ctx()


def _assign_one_p2p_family(
    interface,
    peer_iface,
    mgmt,
    peer_mgmt,
    family,
    site,
    result,
    *,
    pool_finder,
    no_pool_reason,
    override_mask=None,
    push=True,
):
    """Attempt one address-family allocation for a P2P pair.  Mutates *result*.

    Both entry points parameterize the pool decision the same way: *pool_finder* is
    a ``(family, site) -> Prefix`` callable, *no_pool_reason* a ``family -> str`` for
    the error message, and *override_mask* the child mask. The M13 heuristic path
    (:func:`_auto_assign_p2p`) resolves ``p2p-core`` via :func:`find_pool`; the
    link-role path resolves the role's configured pool — both reuse this one
    carve/reserve/rollback flow.
    """
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone
    from ipam.models import IPAddress, Prefix

    from .models import NSOInterfaceIPState
    from .renderer_writer import RendererMutationPlan, planned_save, renderer_writes
    from .signals import _schedule_intent_push

    _OCCUPIED = ("reserved", "accepted", "deploying", "in_sync")
    now = timezone.now()
    pool = child_prefix = state_a = state_b = None
    host_a_str = host_b_str = None

    # Carve + reserve both IPs + create both state rows in ONE transaction under the pool
    # lock. Any failure rolls the whole allocation back automatically — no orphaned child
    # prefix, reserved IPAddresses, or half-linked peer state — instead of relying on
    # best-effort _delete_if_set cleanup that can itself fail and leave IPAM debris (the
    # standalone M13 path has no outer atomic to save it). The lock also closes the TOCTOU
    # window where two callers carve the same block.
    try:
        with transaction.atomic():
            pool = pool_finder(family, site)
            if pool is None:
                result["errors"].append(
                    {"interface": str(interface), "family": family, "reason": no_pool_reason(family)}
                )
                return
            # Row-level lock on the pool prefix: serializes concurrent carves for the same
            # pool without blocking unrelated allocations.
            pool = Prefix.objects.select_for_update().get(pk=pool.pk)
            if NSOInterfaceIPState.objects.filter(
                Q(interface=interface) | Q(interface=peer_iface),
                family=family,
                status__in=_OCCUPIED,
            ).exists():
                result["skipped"].append(
                    {
                        "interface": str(interface),
                        "family": family,
                        "reason": f"One or both P2P ends already have a managed {family} IP",
                    }
                )
                return
            carved = carve_p2p_child(pool, family, override_mask)
            if carved is None:
                result["errors"].append(
                    {
                        "interface": str(interface),
                        "family": family,
                        "reason": f"P2P pool {pool} has no available space for a child prefix",
                    }
                )
                return
            child_prefix, host_a_str, host_b_str = carved
            vrf_name = pool.vrf.name if pool.vrf else ""
            ip_a = IPAddress(address=host_a_str, vrf=pool.vrf, status="reserved")
            ip_a.assigned_object = interface
            ip_b = IPAddress(address=host_b_str, vrf=pool.vrf, status="reserved")
            ip_b.assigned_object = peer_iface
            state_a = NSOInterfaceIPState(
                interface=interface,
                address=host_a_str,
                vrf=vrf_name,
                family=family,
                status="accepted",
                auto_assigned=True,
                allocation_kind=NSOInterfaceIPState.ALLOCATION_KIND_P2P,
                source_pool=child_prefix,
                accepted_at=now,
            )
            state_b = NSOInterfaceIPState(
                interface=peer_iface,
                address=host_b_str,
                vrf=vrf_name,
                family=family,
                status="accepted",
                auto_assigned=True,
                allocation_kind=NSOInterfaceIPState.ALLOCATION_KIND_P2P,
                source_pool=child_prefix,
                accepted_at=now,
                peer_state=state_a,
            )
            state_a_link = copy.copy(state_a)
            state_a_link.peer_state = state_b
            plan = RendererMutationPlan.build(
                saves=(
                    planned_save(ip_a, force_insert=True, natural_key=("address", "vrf")),
                    planned_save(ip_b, force_insert=True, natural_key=("address", "vrf")),
                    planned_save(
                        state_a,
                        force_insert=True,
                        natural_key=("interface", "address", "vrf"),
                    ),
                    planned_save(
                        state_b,
                        force_insert=True,
                        natural_key=("interface", "address", "vrf"),
                    ),
                    planned_save(
                        state_a_link,
                        update_fields=("peer_state",),
                        natural_key=("interface", "address", "vrf"),
                    ),
                ),
                planned_at=now,
            )
            with renderer_writes(plan) as writer, _suppress_ip_intent_push():
                writer.save(ip_a, force_insert=True)
                writer.save(ip_b, force_insert=True)
                writer.save(state_a, force_insert=True)
                writer.save(state_b, force_insert=True)
                state_a_link.pk = state_a.pk
                state_a_link._state.adding = False
                writer.save(state_a_link, update_fields=("peer_state",))
                if push:
                    # Appended inside the allocation's own transaction: an in-protocol send is
                    # a claimed, sequenced operation, and the drain runs on this commit.
                    for dev_id in (mgmt.device_id, peer_mgmt.device_id):
                        _schedule_intent_push((dev_id, "ip"))
    except Exception as exc:
        result["errors"].append(
            {"interface": str(interface), "family": family, "reason": f"P2P allocation failed: {exc}"}
        )
        return

    result["allocated"].extend(
        [
            {
                "interface": str(interface),
                "family": family,
                "address": host_a_str,
                "pool": str(pool),
                "state_id": state_a.pk,
            },
            {
                "interface": str(peer_iface),
                "family": family,
                "address": host_b_str,
                "pool": str(pool),
                "state_id": state_b.pk,
            },
        ]
    )
    logger.info(
        "ip_autoassign.p2p: allocated %s (A) + %s (B) from %s for %s ↔ %s",
        host_a_str,
        host_b_str,
        pool,
        interface,
        peer_iface,
    )


def _auto_assign_p2p(interface, peer_iface, mgmt, peer_mgmt, families, result):
    """Phase B: reserve-then-activate allocation for both ends of a P2P core link."""
    site = getattr(interface.device, "site", None)
    for family in families:
        _assign_one_p2p_family(
            interface,
            peer_iface,
            mgmt,
            peer_mgmt,
            family,
            site,
            result,
            pool_finder=lambda fam, s: find_pool("p2p-core", None, s, fam),  # noqa: E731
            no_pool_reason=lambda fam: f"No {fam} p2p-core pool found",  # noqa: E731
        )
    return result


# ── Single-ended allocation helpers ─────────────────────────────────────────────


def _single_family_occupied(interface, family: str) -> bool:
    """Fill-empty guard: True if a managed IP already exists for (interface, family)."""
    from .models import NSOInterfaceIPState

    return NSOInterfaceIPState.objects.filter(
        interface=interface,
        family=family,
        status__in=("reserved", "accepted", "deploying", "in_sync"),
    ).exists()


def _single_ip_allocation_footprint(mgmt):
    """Declare the new rows that one single-ended allocation can create."""
    from .intent_state import MutationFootprint, SourceRow

    return MutationFootprint.for_keys(
        {(mgmt.device_id, "ip")},
        source_rows=(SourceRow("ipam.ipaddress", None),),
        overlay_rows=(SourceRow("netbox_nso_plugin.nsointerfaceipstate", None),),
    )


class _AllocationNoOp(Exception):
    """Exit an allocation transaction before recording its non-write result."""

    def __init__(self, result_key, entry):
        super().__init__(entry["reason"])
        self.result_key = result_key
        self.entry = entry


def _reserve_single(interface, mgmt, family: str, pool, result, push=True) -> None:
    """Draw one host from *pool*, reserve it, and create the accepted state. Mutates *result*.

    The caller has already resolved *pool* and passed the fill-empty guard; this is
    the shared reservation, state, and outbox body used by both the M13 classification
    path and the link-role single-ended path. The commit callback drains the scheduled
    outbox entry. *push* False skips that scheduling.
    """
    from django.db import transaction
    from django.utils import timezone
    from ipam.models import IPAddress, Prefix

    from .intent_state import intent_transaction
    from .models import NSOInterfaceIPState
    from .renderer_writer import RendererMutationPlan, planned_save, renderer_writes
    from .signals import _schedule_intent_push, suppress_intent_push

    # Acquire the allocation footprint before the fill-empty check and address selection.
    # The exact writer reuses this permit after its plan is frozen under the lock.
    failed_step = "lock the pool and check the fill-empty guard"
    try:
        with transaction.atomic():
            pool = Prefix.objects.select_for_update().get(pk=pool.pk)
            with intent_transaction(_single_ip_allocation_footprint(mgmt)):
                if _single_family_occupied(interface, family):
                    raise _AllocationNoOp(
                        "skipped",
                        {
                            "interface": str(interface),
                            "family": family,
                            "reason": "Already has a managed IP in this family",
                        },
                    )

                failed_step = "select an available address"
                available_str = pool.get_first_available_ip()
                if available_str is None:
                    raise _AllocationNoOp(
                        "errors",
                        {
                            "interface": str(interface),
                            "family": family,
                            "reason": f"Pool {pool} is exhausted (no available IPs)",
                        },
                    )

                planned_at = timezone.now()
                failed_step = "create IPAddress"
                ip_obj = IPAddress(address=available_str, vrf=pool.vrf, status="reserved")
                ip_obj.assigned_object = interface
                vrf_name = pool.vrf.name if pool.vrf else ""
                current = NSOInterfaceIPState.objects.filter(
                    interface=interface,
                    address=available_str,
                    vrf=vrf_name,
                ).first()
                state = (
                    copy.copy(current)
                    if current is not None
                    else NSOInterfaceIPState(interface=interface, address=available_str, vrf=vrf_name)
                )
                state.family = family
                state.status = "accepted"
                state.auto_assigned = True
                state.allocation_kind = NSOInterfaceIPState.ALLOCATION_KIND_SINGLE
                state.source_pool = pool
                state.accepted_at = planned_at
                state_fields = (
                    None
                    if current is None
                    else ("family", "status", "auto_assigned", "allocation_kind", "source_pool", "accepted_at")
                )
                plan = RendererMutationPlan.build(
                    saves=(
                        planned_save(ip_obj, force_insert=True, natural_key=("address", "vrf")),
                        planned_save(
                            state,
                            update_fields=state_fields,
                            force_insert=current is None,
                            natural_key=("interface", "address", "vrf"),
                        ),
                    ),
                    planned_at=planned_at,
                )
                with renderer_writes(plan) as writer, suppress_intent_push():
                    writer.save(ip_obj, force_insert=True)
                    failed_step = "create NSOInterfaceIPState"
                    writer.save(state, update_fields=state_fields, force_insert=current is None)
                    failed_step = "schedule the IP intent push"
                    if push:
                        _schedule_intent_push((mgmt.device_id, "ip"))
    except _AllocationNoOp as exc:
        result[exc.result_key].append(exc.entry)
        return
    except Exception as exc:
        result["errors"].append(
            {
                "interface": str(interface),
                "family": family,
                "reason": f"Failed to {failed_step}: {exc}",
            }
        )
        return

    result["allocated"].append(
        {
            "interface": str(interface),
            "family": family,
            "address": available_str,
            "pool": str(pool),
            "state_id": state.pk,
        }
    )
    logger.info("ip_autoassign: allocated %s (%s) from pool %s for %s", available_str, family, pool, interface)


# ── Shared managed-device guard ─────────────────────────────────────────────────


def _resolve_managed_mgmt(device, *, subject: str = "Device"):
    """Return ``(NSODeviceManagement, None)`` for an NSO-managed *device*, else ``(None, reason)``.

    The single guard shared by the IP / description / IGP consumers: a device with
    no management row, or one without an ``adapter_device_id``, cannot receive
    intent. *subject* prefixes the reason so a caller can tell the near end from the
    far (peer) end (e.g. ``"P2P role: peer device"``).
    """
    from .models import NSODeviceManagement

    mgmt = NSODeviceManagement.objects.filter(device=device).first()
    if mgmt is None:
        return None, f"{subject} is not managed by NSO"
    if mgmt.adapter_device_id is None:
        return None, f"{subject} has no adapter_device_id"
    return mgmt, None


# ── Main entry point ──────────────────────────────────────────────────────────


def auto_assign_ip(interface, families: tuple[str, ...] = ("ipv4", "ipv6")) -> dict:
    """Allocate one IP per requested address family for *interface*.

    Classifies the interface (M13 heuristic): loopback / access links are
    single-ended (fill-empty-only); ``p2p-core`` links use the P2P
    reserve-then-activate flow for both ends.

    **Fill-empty-only:** an interface that already has an accepted/deploying/
    in_sync/reserved ``NSOInterfaceIPState`` row in the given family is skipped.

    Returns a dict::

        {
            "allocated": [{"interface": ..., "family": ..., "address": ...,
                           "pool": ..., "state_id": ...}, ...],
            "skipped":   [{"interface": ..., "family": ..., "reason": ...}, ...],
            "errors":    [{"interface": ..., "family": ..., "reason": ...}, ...],
        }
    """
    result: dict = {"allocated": [], "skipped": [], "errors": []}

    # Gate: device must be managed by NSO.
    mgmt, reason = _resolve_managed_mgmt(interface.device)
    if mgmt is None:
        result["errors"].append({"interface": str(interface), "reason": reason})
        return result

    classification = classify_interface(interface)
    if classification == "p2p-core":
        # Phase B: P2P reserve-then-activate.
        from .derived_intent import find_peer

        peer_iface = find_peer(interface)
        if peer_iface is None:
            result["errors"].append(
                {
                    "interface": str(interface),
                    "reason": "P2P core link: no cable peer found",
                }
            )
            return result
        peer_mgmt, reason = _resolve_managed_mgmt(peer_iface.device, subject="P2P core link: peer device")
        if peer_mgmt is None:
            result["errors"].append({"interface": str(interface), "reason": reason})
            return result
        return _auto_assign_p2p(interface, peer_iface, mgmt, peer_mgmt, families, result)

    site = getattr(interface.device, "site", None)
    vrf = None  # Phase A: no per-interface VRF resolution; pools matched globally/by site

    for family in families:
        # Fill-empty guard: skip if a managed IP already exists in this family.
        if _single_family_occupied(interface, family):
            result["skipped"].append(
                {
                    "interface": str(interface),
                    "family": family,
                    "reason": "Already has a managed IP in this family",
                }
            )
            continue

        pool = find_pool(classification, vrf, site, family)
        if pool is None:
            result["errors"].append(
                {
                    "interface": str(interface),
                    "family": family,
                    "reason": f"No {family} pool found for classification '{classification}'",
                }
            )
            continue

        _reserve_single(interface, mgmt, family, pool, result)

    return result


# ── Link-role entry point ───────────────────────────────────────────────────────


def assign_ips_for_role(
    interface,
    role,
    other_end=None,
    push=True,
    *,
    mgmt=None,
    peer_mgmt=None,
    resolved_pools=None,
) -> dict:
    """Allocate IPs for *interface* from an ``NSOLinkRole``'s configured pools + mask.

    The link-role counterpart to :func:`auto_assign_ip`: the pool (explicit Prefix
    or role slug) and the p2p child mask come from *role* rather than the M13
    heuristic, but the carve/reserve/rollback/TOCTOU machinery is shared.

    * ``p2p`` role → *other_end* is the peer interface (from
      ``link_role.resolve_role``); both ends are allocated together.
    * ``single`` role → one host per opted-in family for this interface only.

    Fill-empty-only. *push* False skips the immediate adapter push (the orchestrator
    defers pushes to after an atomic commit). Returns the ``{allocated, skipped,
    errors}`` dict. A role that manages no IP family is a no-op (empty result).
    *resolved_pools* supplies pool rows that the orchestrator already locked.
    """
    from .link_role import intent_bundle

    result: dict = {"allocated": [], "skipped": [], "errors": []}

    bundle = intent_bundle(role)
    if not bundle.pools:
        return result  # role does not manage IP → nothing to do

    # Gate: device must be managed by NSO (resolve unless the orchestrator threaded it).
    if mgmt is None:
        mgmt, reason = _resolve_managed_mgmt(interface.device)
        if mgmt is None:
            result["errors"].append({"interface": str(interface), "reason": reason})
            return result

    site = getattr(interface.device, "site", None)

    if role.link_type == "p2p":
        peer_iface = other_end
        if peer_iface is None:
            result["errors"].append({"interface": str(interface), "reason": "P2P role: no cable peer found"})
            return result
        if peer_mgmt is None:
            peer_mgmt, reason = _resolve_managed_mgmt(peer_iface.device, subject="P2P role: peer device")
            if peer_mgmt is None:
                result["errors"].append({"interface": str(interface), "reason": reason})
                return result
        for spec in bundle.pools:
            _assign_one_p2p_family(
                interface,
                peer_iface,
                mgmt,
                peer_mgmt,
                spec.family,
                site,
                result,
                pool_finder=(
                    lambda fam, s, _spec=spec: (
                        resolved_pools.get(_spec.family)
                        if resolved_pools is not None
                        else _resolve_role_pool(_spec, None, s)
                    )
                ),
                no_pool_reason=(lambda fam, _slug=role.slug: f"No {fam} pool found for role '{_slug}'"),
                override_mask=spec.mask,
                push=push,
            )
        return result

    # Single-ended role (loopback / access): one host per opted-in family.
    for spec in bundle.pools:
        if _single_family_occupied(interface, spec.family):
            result["skipped"].append(
                {
                    "interface": str(interface),
                    "family": spec.family,
                    "reason": "Already has a managed IP in this family",
                }
            )
            continue
        pool = resolved_pools.get(spec.family) if resolved_pools is not None else _resolve_role_pool(spec, None, site)
        if pool is None:
            result["errors"].append(
                {
                    "interface": str(interface),
                    "family": spec.family,
                    "reason": f"No {spec.family} pool found for role '{role.slug}'",
                }
            )
            continue
        _reserve_single(interface, mgmt, spec.family, pool, result, push=push)

    return result
