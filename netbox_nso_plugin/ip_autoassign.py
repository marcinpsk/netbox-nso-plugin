# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M13 — IP auto-assignment from purpose Prefix pools.

Phase A: single-ended allocation for loopback and access interfaces.
Phase B (P2P reserve-then-activate) is a follow-on milestone.

Public entry point: :func:`auto_assign_ip`.
"""

from __future__ import annotations

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


def find_pool(classification: str, vrf, site, family: str):
    """Find the best-matching Prefix pool for (classification, vrf, site, family).

    Selection:
    1. Match ``Prefix.role__slug`` → the classification role slug.
    2. Narrow by VRF; fall back to global (vrf=None) if no VRF-specific pool.
    3. Narrow by site scope; skip site filter when no match found.
    4. Return the first pool with available space.

    Returns a ``Prefix`` instance or ``None`` (no pool / all exhausted).
    """
    from django.apps import apps
    from ipam.models import Prefix  # NetBox IPAM model

    cfg = apps.get_app_config("netbox_nso_plugin")
    role_slug_map: dict[str, str] = getattr(cfg, "_ip_pool_role_slugs", _DEFAULT_ROLE_SLUGS)
    role_slug = role_slug_map.get(classification)
    if role_slug is None:
        logger.debug("ip_autoassign: no role slug for classification %r", classification)
        return None

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
                from django.contrib.contenttypes.models import ContentType

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


# ── P2P child-prefix carving ──────────────────────────────────────────────────


def _get_p2p_child_length(pool, family: str) -> int:
    """Resolve the P2P child-prefix length for *pool* and *family*.

    Priority:
    1. Per-pool custom field ``p2p_child_length_v4`` / ``p2p_child_length_v6``.
    2. Plugin-wide config ``p2p_child_length_v4`` / ``p2p_child_length_v6``.
    3. Built-in defaults: 31 (IPv4) / 127 (IPv6).
    """
    from django.apps import apps

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


def carve_p2p_child(pool, family: str):
    """Carve a child prefix from P2P pool *pool* for address-family *family*.

    Returns ``(child_prefix, host_a_str, host_b_str)`` where ``host_a_str``
    and ``host_b_str`` are ``'IP/length'`` strings ready for use as
    ``IPAddress.address``.  Returns ``None`` when the pool has no available
    block large enough for the configured child length.

    The carved ``Prefix`` is written to IPAM with ``status="reserved"``
    so that concurrent allocators cannot re-use the same range.
    For P2P states the ``source_pool`` FK points to the **carved child**
    (not the top-level pool) so that rollback can cleanly delete it.
    """
    from ipam.models import Prefix

    length = _get_p2p_child_length(pool, family)
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
        description="P2P link (M13 auto-assigned)",
    )
    child_prefix.refresh_from_db()  # ensure prefix is netaddr.IPNetwork for get_available_ips()

    hosts = list(child_prefix.get_available_ips())
    if len(hosts) < 2:
        child_prefix.delete()
        return None

    child_len = child_prefix.prefix.prefixlen
    return child_prefix, f"{hosts[0]}/{child_len}", f"{hosts[1]}/{child_len}"


# ── Rollback ──────────────────────────────────────────────────────────────────


def rollback_auto_assigned(state, _cascade: bool = True) -> None:
    """Unassign and delete an auto-assigned IP address from *state*.

    Clears the IPAddress assignment from the interface, deletes the IPAddress
    object, then deletes the ``NSOInterfaceIPState`` row.  Used on
    ``apply_failed`` or explicit operator de-allocation.

    For P2P states (``state.peer_state`` is set), when called as the primary
    end (``_cascade=True``), also rolls back the peer state and deletes the
    shared carved child prefix (``source_pool``).

    Swallows exceptions so callers can call this in a cleanup path.
    """
    from ipam.models import IPAddress

    if not state.auto_assigned:
        return

    # Capture P2P references before any deletions (objects may be nulled).
    peer_state = state.peer_state
    source_pool = state.source_pool

    try:
        vrf_obj = None
        if state.vrf:
            try:
                from ipam.models import VRF

                vrf_obj = VRF.objects.get(name=state.vrf)
            except Exception:
                pass
        ip_obj = IPAddress.objects.filter(
            address=state.address,
            vrf=vrf_obj,
            assigned_object_id=state.interface_id,
        ).first()
        if ip_obj is not None:
            ip_obj.delete()
    except Exception as exc:
        logger.warning("ip_autoassign.rollback: failed to delete IPAddress for %s: %s", state, exc)
    try:
        state.delete()
    except Exception as exc:
        logger.warning("ip_autoassign.rollback: failed to delete NSOInterfaceIPState %s: %s", state, exc)

    if _cascade and peer_state is not None:
        rollback_auto_assigned(peer_state, _cascade=False)
        # Delete the carved child prefix (shared by both ends) only once.
        if source_pool is not None:
            try:
                source_pool.refresh_from_db()
                source_pool.delete()
            except Exception as exc:
                logger.warning(
                    "ip_autoassign.rollback: failed to delete carved child prefix %s: %s",
                    source_pool,
                    exc,
                )


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


def _delete_if_set(*objs):
    """Silently delete each non-None object; used for P2P rollback cleanup."""
    for obj in objs:
        if obj is not None:
            try:
                obj.delete()
            except Exception:
                pass


def _assign_one_p2p_family(interface, peer_iface, mgmt, peer_mgmt, family, site, result):
    """Attempt one address-family allocation for a P2P pair.  Mutates *result*."""
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone
    from ipam.models import IPAddress, Prefix

    from .models import NSOInterfaceIPState
    from .signals import _push_ip_intent_for_device

    _OCCUPIED = ("reserved", "accepted", "deploying", "in_sync")

    # Wrap the occupancy check + carve in a transaction and lock the pool row so
    # concurrent callers for the same pool serialize rather than racing for the
    # same available space.  Without this a TOCTOU window allows two callers to
    # read the same available block and both attempt to create overlapping child
    # prefixes, producing an unhandled IntegrityError from the unique constraint.
    with transaction.atomic():
        pool = find_pool("p2p-core", None, site, family)
        if pool is None:
            result["errors"].append(
                {"interface": str(interface), "family": family, "reason": f"No {family} p2p-core pool found"}
            )
            return

        # Row-level lock on the pool prefix: serializes concurrent carves for
        # the same pool without blocking unrelated allocations.
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

        carved = carve_p2p_child(pool, family)
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
    ip_a = ip_b = None

    try:
        with _suppress_ip_intent_push():
            ip_a = IPAddress(address=host_a_str, status="reserved")
            ip_a.assigned_object = interface
            ip_a.save()
            ip_b = IPAddress(address=host_b_str, status="reserved")
            ip_b.assigned_object = peer_iface
            ip_b.save()
    except Exception as exc:
        _delete_if_set(ip_a, child_prefix)
        result["errors"].append(
            {"interface": str(interface), "family": family, "reason": f"Failed to reserve P2P IPAddresses: {exc}"}
        )
        return

    state_a = state_b = None
    now = timezone.now()
    try:
        state_a, _ = NSOInterfaceIPState.objects.update_or_create(
            interface=interface,
            address=host_a_str,
            vrf=vrf_name,
            defaults={
                "family": family,
                "status": "accepted",
                "auto_assigned": True,
                "source_pool": child_prefix,
                "accepted_at": now,
            },
        )
        state_b, _ = NSOInterfaceIPState.objects.update_or_create(
            interface=peer_iface,
            address=host_b_str,
            vrf=vrf_name,
            defaults={
                "family": family,
                "status": "accepted",
                "auto_assigned": True,
                "source_pool": child_prefix,
                "accepted_at": now,
            },
        )
        state_a.peer_state = state_b
        state_a.save(update_fields=["peer_state"])
        state_b.peer_state = state_a
        state_b.save(update_fields=["peer_state"])
    except Exception as exc:
        _delete_if_set(ip_a, ip_b, child_prefix, state_a)
        result["errors"].append(
            {"interface": str(interface), "family": family, "reason": f"Failed to create P2P state records: {exc}"}
        )
        return

    for dev_id, adapter_id in [
        (mgmt.device_id, mgmt.adapter_device_id),
        (peer_mgmt.device_id, peer_mgmt.adapter_device_id),
    ]:
        try:
            _push_ip_intent_for_device(dev_id, adapter_id)
        except Exception as exc:
            logger.warning("ip_autoassign.p2p: failed to push intent for device %s: %s", dev_id, exc)

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
        _assign_one_p2p_family(interface, peer_iface, mgmt, peer_mgmt, family, site, result)
    return result


# ── Main entry point ──────────────────────────────────────────────────────────


def auto_assign_ip(interface, families: tuple[str, ...] = ("ipv4", "ipv6")) -> dict:
    """Allocate one IP per requested address family for *interface*.

    Phase A: loopback and access links (single-ended, fill-empty-only).
    P2P core (``"p2p-core"`` classification) is deferred to Phase B.

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
    from django.utils import timezone
    from ipam.models import IPAddress

    from .models import NSODeviceManagement, NSOInterfaceIPState
    from .signals import _push_ip_intent_for_device

    result: dict = {"allocated": [], "skipped": [], "errors": []}

    # Gate: device must be managed by NSO.
    try:
        mgmt = NSODeviceManagement.objects.get(device=interface.device)
    except NSODeviceManagement.DoesNotExist:
        result["errors"].append({"interface": str(interface), "reason": "Device is not managed by NSO"})
        return result

    if mgmt.adapter_device_id is None:
        result["errors"].append({"interface": str(interface), "reason": "Device has no adapter_device_id"})
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
        try:
            peer_mgmt = NSODeviceManagement.objects.get(device=peer_iface.device)
        except NSODeviceManagement.DoesNotExist:
            result["errors"].append(
                {
                    "interface": str(interface),
                    "reason": "P2P core link: peer device is not managed by NSO",
                }
            )
            return result
        if peer_mgmt.adapter_device_id is None:
            result["errors"].append(
                {
                    "interface": str(interface),
                    "reason": "P2P core link: peer device has no adapter_device_id",
                }
            )
            return result
        return _auto_assign_p2p(interface, peer_iface, mgmt, peer_mgmt, families, result)

    site = getattr(interface.device, "site", None)
    vrf = None  # Phase A: no per-interface VRF resolution; pools matched globally/by site

    for family in families:
        # Fill-empty guard: skip if a managed IP already exists in this family.
        occupied = NSOInterfaceIPState.objects.filter(
            interface=interface,
            family=family,
            status__in=("reserved", "accepted", "deploying", "in_sync"),
        ).exists()
        if occupied:
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

        available_str = pool.get_first_available_ip()
        if available_str is None:
            result["errors"].append(
                {
                    "interface": str(interface),
                    "family": family,
                    "reason": f"Pool {pool} is exhausted (no available IPs)",
                }
            )
            continue

        # Reserve the IPAddress in IPAM so concurrent allocations don't collide.
        try:
            ip_obj = IPAddress(address=available_str, status="reserved")
            ip_obj.assigned_object = interface
            ip_obj.save()
        except Exception as exc:
            result["errors"].append(
                {
                    "interface": str(interface),
                    "family": family,
                    "reason": f"Failed to create IPAddress: {exc}",
                }
            )
            continue

        vrf_name = pool.vrf.name if pool.vrf else ""

        # Create (or update the signal-created) NSOInterfaceIPState as 'accepted'.
        # The IPAddress save above may have triggered _on_ip_address_save which
        # already created a NSOInterfaceIPState row — update_or_create handles both.
        try:
            state, _ = NSOInterfaceIPState.objects.update_or_create(
                interface=interface,
                address=available_str,
                vrf=vrf_name,
                defaults={
                    "family": family,
                    "status": "accepted",
                    "auto_assigned": True,
                    "source_pool": pool,
                    "accepted_at": timezone.now(),
                },
            )
        except Exception as exc:
            # State creation failed — undo the IPAddress so IPAM stays clean.
            try:
                ip_obj.delete()
            except Exception:
                pass
            result["errors"].append(
                {
                    "interface": str(interface),
                    "family": family,
                    "reason": f"Failed to create NSOInterfaceIPState: {exc}",
                }
            )
            continue

        # Explicitly push the device's IP intent (M12 write pipe) so the new
        # address is sent to NSO.  Signal-driven push will fire on the next
        # IPAddress save, but we push now to be immediate.
        try:
            _push_ip_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)
        except Exception as exc:
            logger.warning(
                "ip_autoassign: failed to push IP intent for device %s after allocating %s: %s",
                mgmt.device_id,
                available_str,
                exc,
            )

        result["allocated"].append(
            {
                "interface": str(interface),
                "family": family,
                "address": available_str,
                "pool": str(pool),
                "state_id": state.pk,
            }
        )
        logger.info(
            "ip_autoassign: allocated %s (%s) from pool %s for %s",
            available_str,
            family,
            pool,
            interface,
        )

    return result
