# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""BGP reconciler for M15 A4.

Reads the adapter's GET /api/v1/devices/{id}/bgp-config response and
creates/updates the netbox-routing BGP object graph in NetBox.

Object creation order (FK prerequisites):
  ipam.ASN → BGPRouter → BGPScope → BGPAddressFamily → BGPPeer → BGPPeerAddressFamily

NSOBGPPeerState rows are kept as a compliance overlay so the operator can
see which peers were imported from NSO and track their write-path status.
"""

import ipaddress
import logging

logger = logging.getLogger(__name__)

_BGP_WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync", "apply_failed"}


def _get_or_create_asn(asn_str: str, ASN):
    """Find or create an ipam.ASN for the given ASN number string.

    When creating a new ASN, a placeholder RIR named 'NSO Auto-Discovered'
    is found or created so that the non-nullable rir field is satisfied.
    """
    from ipam.models import RIR

    try:
        asn_int = int(asn_str)
    except (ValueError, TypeError):
        logger.warning("BGP: invalid ASN value %r, skipping router", asn_str)
        return None

    existing = ASN.objects.filter(asn=asn_int).first()
    if existing is not None:
        return existing

    # Auto-create: need a placeholder RIR (rir field is NOT nullable)
    placeholder_rir, _ = RIR.objects.get_or_create(
        name="NSO Auto-Discovered",
        defaults={"slug": "nso-auto-discovered", "is_private": True},
    )
    obj, created = ASN.objects.get_or_create(
        asn=asn_int,
        defaults={"rir": placeholder_rir},
    )
    if created:
        logger.debug("BGP: auto-created ASN %d (rir=NSO Auto-Discovered)", asn_int)
    return obj


def _get_or_create_router(device, asn_obj, BGPRouter, ContentType, Device):
    """Find or create a BGPRouter for (device, asn).

    Uses str(asn) as the name so that multiple routers on the same device
    (different ASNs) don't collide on the (device, name) unique constraint
    that treats NULL == NULL when nulls_distinct=False.
    """
    ct = ContentType.objects.get_for_model(Device)
    obj, created = BGPRouter.objects.get_or_create(
        assigned_object_type=ct,
        assigned_object_id=device.pk,
        asn=asn_obj,
        defaults={"name": str(asn_obj.asn)},
    )
    if created:
        logger.debug("BGP: auto-created BGPRouter device=%s asn=%s", device, asn_obj)
    return obj


def _get_or_create_scope(router_obj, vrf_obj, BGPScope):
    """Find or create a BGPScope for (router, vrf)."""
    obj, created = BGPScope.objects.get_or_create(
        router=router_obj,
        vrf=vrf_obj,
        defaults={},
    )
    if created:
        logger.debug("BGP: auto-created BGPScope router=%s vrf=%s", router_obj, vrf_obj)
    return obj


def _get_or_create_address_family(scope_obj, af_str: str, BGPAddressFamily):
    """Find or create a BGPAddressFamily for (scope, af)."""
    obj, created = BGPAddressFamily.objects.get_or_create(
        scope=scope_obj,
        address_family=af_str,
        defaults={},
    )
    if created:
        logger.debug("BGP: auto-created BGPAddressFamily scope=%s af=%s", scope_obj, af_str)
    return obj


def _resolve_peer_ip(peer_address_str: str, IPAddress):
    """Return an ipam.IPAddress matching the given host IP, creating one if absent.

    Searches by host part of the address (e.g. finds '10.0.0.1/30' for query
    '10.0.0.1').  If not found, auto-creates a bare /32 or /128 host entry.
    Returns None if the address string is invalid or creation fails.
    """
    try:
        target = ipaddress.ip_address(peer_address_str)
    except ValueError:
        logger.warning("BGP: invalid peer IP address %r", peer_address_str)
        return None

    existing = IPAddress.objects.filter(address__net_host=peer_address_str).first()
    if existing is not None:
        return existing

    mask = 32 if target.version == 4 else 128
    cidr = f"{peer_address_str}/{mask}"
    try:
        ip_obj = IPAddress(address=cidr)
        ip_obj.full_clean()
        ip_obj.save()
        logger.debug("BGP: auto-created stub IP %s for BGP peer", cidr)
        return ip_obj
    except Exception as exc:
        logger.warning("BGP: could not create IP %s: %s", cidr, exc)
        return None


def _get_or_create_peer_group(name: str, BGPPeerTemplate, remote_asn_obj=None):
    """Find or create a BGPPeerTemplate (netbox-routing's peer-group) by name.

    Enriches the template's remote_as from the group's (inherited) value when
    known — peer-group members share the group's remote-AS. Keyed by name (the
    natural peer-group key); remote_as is set/updated, not part of the lookup.
    """
    if not name:
        return None
    obj = BGPPeerTemplate.objects.filter(name=name).first()
    if obj is None:
        obj = BGPPeerTemplate.objects.create(name=name, remote_as=remote_asn_obj)
        logger.debug("BGP: auto-created BGPPeerTemplate %r", name)
    elif remote_asn_obj is not None and obj.remote_as_id != remote_asn_obj.pk:
        obj.remote_as = remote_asn_obj
        obj.save(update_fields=["remote_as"])
    return obj


def _resolve_bgp_source(device, source: str, IPAddress):
    """Resolve a BGP session source to an ipam.IPAddress.

    ``source`` is either an IP (Junos/Nokia local-address) or an interface name
    (IOS update-source, e.g. ``Loopback4``). For an IP, match an existing
    IPAddress; for an interface, take an IP assigned to that device interface.
    Returns None when nothing matches (we don't fabricate IPs).
    """
    if not source:
        return None
    import netaddr

    try:
        netaddr.IPAddress(source)
        return IPAddress.objects.filter(address__net_host=source).first()
    except (netaddr.AddrFormatError, ValueError):
        pass
    try:
        from dcim.models import Interface

        iface = Interface.objects.filter(device=device, name=source).first()
        if iface is not None:
            return iface.ip_addresses.first()
    except Exception:
        pass
    return None


def _get_or_create_peer(
    scope_obj, ip_obj, peer_data: dict, remote_asn_obj, local_asn_obj, peer_group_obj, BGPPeer, source_obj=None
):
    """Find or create a BGPPeer for (scope, peer_ip).

    Updates mutable fields (enabled, remote_as, local_as, peer_group, ttl,
    password) only if the peer was newly created or the fields differ.
    Returns (bgp_peer, conflict) where conflict=True when an existing peer
    exists that was not created by this plugin (detected heuristically by
    the absence of a linked NSOBGPPeerState row — checked by caller).
    """
    _FK_FIELDS = {"remote_as", "local_as", "peer_group", "source"}
    desired = {
        "enabled": peer_data.get("enabled"),
        "remote_as": remote_asn_obj,
        "local_as": local_asn_obj,
        "peer_group": peer_group_obj,
        "source": source_obj,
        "ttl": peer_data.get("ttl"),
        "password": peer_data.get("password"),
    }
    obj, created = BGPPeer.objects.get_or_create(
        scope=scope_obj,
        peer=ip_obj,
        name=None,
        defaults=desired,
    )
    if not created:
        changed = False
        for field, value in desired.items():
            if field in _FK_FIELDS:
                current = getattr(obj, f"{field}_id")
                new = value.pk if value is not None else None
            else:
                current = getattr(obj, field)
                new = value
            if current != new:
                setattr(obj, field, value)
                changed = True
        if changed:
            obj.save()
    return obj, not created


def _resolve_routemap(name):
    """Resolve a netbox_routing.RouteMap by name (created by the route-policy reconciler)."""
    if not name:
        return None
    try:
        from netbox_routing.models import RouteMap

        return RouteMap.objects.filter(name=name).first()
    except Exception:
        return None


def _resolve_prefixlist(name):
    """Resolve a netbox_routing.PrefixList by name."""
    if not name:
        return None
    try:
        from netbox_routing.models import PrefixList

        return PrefixList.objects.filter(name=name).first()
    except Exception:
        return None


def _reconcile_peer_address_families(peer_obj, peer_af_list: list, scope_obj, BGPAddressFamily, BGPPeerAddressFamily):
    """Ensure BGPPeerAddressFamily rows exist for each (peer, af), with per-AF policies.

    Links inbound/outbound route-map + prefix-list references (resolved by name to
    the netbox_routing objects the route-policy reconciler created). Unresolved names
    are left null rather than guessed.
    """
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(peer_obj.__class__)
    for paf_entry in peer_af_list:
        af_str = paf_entry.get("af") or ""
        if not af_str:
            continue
        af_obj = _get_or_create_address_family(scope_obj, af_str, BGPAddressFamily)
        policy = {
            "enabled": paf_entry.get("enabled", True),
            "routemap_in": _resolve_routemap(paf_entry.get("routemap_in")),
            "routemap_out": _resolve_routemap(paf_entry.get("routemap_out")),
            "prefixlist_in": _resolve_prefixlist(paf_entry.get("prefixlist_in")),
            "prefixlist_out": _resolve_prefixlist(paf_entry.get("prefixlist_out")),
        }
        obj, created = BGPPeerAddressFamily.objects.get_or_create(
            assigned_object_type=ct,
            assigned_object_id=peer_obj.pk,
            address_family=af_obj,
            defaults=policy,
        )
        if not created:
            changed = False
            for field, value in policy.items():
                if field == "enabled":
                    if obj.enabled != value:
                        obj.enabled = value
                        changed = True
                else:
                    if getattr(obj, f"{field}_id") != (value.pk if value else None):
                        setattr(obj, field, value)
                        changed = True
            if changed:
                obj.save()


def _resolve_vrf(vrf_name: str, VRF):
    """Look up an ipam.VRF by name; return None for global or if not found."""
    if not vrf_name or VRF is None:
        return None
    try:
        return VRF.objects.get(name=vrf_name)
    except VRF.DoesNotExist:
        logger.debug("BGP: VRF %r not found in NetBox, using global", vrf_name)
        return None


def _update_peer_state(
    mgmt, asn_str, vrf_name, peer_address_str, bgp_peer, remote_as_str, peer_entry, was_existing, now
):
    """Create or update an NSOBGPPeerState overlay row for a single peer."""
    from .models import NSOBGPPeerState

    has_state = NSOBGPPeerState.objects.filter(
        management=mgmt,
        asn_str=asn_str,
        vrf_name=vrf_name,
        peer_address_str=peer_address_str,
    ).exists()
    conflict = was_existing and not has_state

    state, _ = NSOBGPPeerState.objects.get_or_create(
        management=mgmt,
        asn_str=asn_str,
        vrf_name=vrf_name,
        peer_address_str=peer_address_str,
        defaults={"status": "unknown"},
    )
    state.bgp_peer = bgp_peer
    state.remote_as_str = remote_as_str
    state.enabled = peer_entry.get("enabled")
    state.last_sync_at = now
    if conflict and state.status not in _BGP_WRITE_PATH_STATUSES:
        state.status = "conflict"
    elif state.status not in _BGP_WRITE_PATH_STATUSES and state.status != "conflict":
        state.status = "in_sync" if state.bgp_peer_id else "imported"
    state.save()
    return state


def _reconcile_scope(
    mgmt,
    router_obj,
    scope_entry,
    asn_str,
    now,
    seen_keys,
    ASN,
    BGPScope,
    BGPAddressFamily,
    BGPPeer,
    BGPPeerAddressFamily,
    BGPPeerTemplate,
    IPAddress,
    VRF,
):
    """Reconcile a single scope entry: create scope, AFs, peers, and state rows."""
    vrf_name = scope_entry.get("vrf") or ""
    vrf_obj = _resolve_vrf(vrf_name, VRF)
    scope_obj = _get_or_create_scope(router_obj, vrf_obj, BGPScope)

    for af_str in scope_entry.get("address_families") or []:
        if af_str:
            _get_or_create_address_family(scope_obj, af_str, BGPAddressFamily)

    for peer_entry in scope_entry.get("peers") or []:
        peer_address_str = peer_entry.get("peer_address") or ""
        if not peer_address_str:
            continue
        ip_obj = _resolve_peer_ip(peer_address_str, IPAddress)
        if ip_obj is None:
            logger.warning("BGP: could not resolve IP for peer %r; skipping", peer_address_str)
            continue

        remote_as_str = str(peer_entry.get("remote_as") or "")
        remote_asn_obj = _get_or_create_asn(remote_as_str, ASN) if remote_as_str else None
        local_as_str = str(peer_entry.get("local_as") or "")
        local_asn_obj = _get_or_create_asn(local_as_str, ASN) if local_as_str else None
        peer_group_obj = _get_or_create_peer_group(peer_entry.get("peer_group") or "", BGPPeerTemplate, remote_asn_obj)
        source_obj = _resolve_bgp_source(mgmt.device, peer_entry.get("source"), IPAddress)

        bgp_peer, was_existing = _get_or_create_peer(
            scope_obj, ip_obj, peer_entry, remote_asn_obj, local_asn_obj, peer_group_obj, BGPPeer, source_obj
        )
        _update_peer_state(
            mgmt, asn_str, vrf_name, peer_address_str, bgp_peer, remote_as_str, peer_entry, was_existing, now
        )
        seen_keys.add((mgmt.pk, asn_str, vrf_name, peer_address_str))

        _reconcile_peer_address_families(
            bgp_peer, peer_entry.get("address_families") or [], scope_obj, BGPAddressFamily, BGPPeerAddressFamily
        )


def _reconcile_bgp_config(device, payload: dict) -> list:
    """Reconcile BGP config from adapter payload into NetBox netbox-routing BGP models.

    For each router → scope → peer entry reported by the adapter:
    - Ensures ipam.ASN, BGPRouter, BGPScope, BGPAddressFamily, BGPPeer exist.
    - Creates/updates NSOBGPPeerState overlay rows for compliance tracking.
    - Marks stale state rows (no longer reported) as status='changed'.

    Returns a list of NSOBGPPeerState instances for this device.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    try:
        from netbox_routing.models import (
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPPeerTemplate,
            BGPRouter,
            BGPScope,
        )
    except ImportError:
        logger.warning("netbox_routing not installed; skipping BGP reconcile")
        return []

    try:
        from ipam.models import ASN, IPAddress
    except ImportError:
        logger.warning("ipam not available; skipping BGP reconcile")
        return []

    try:
        from ipam.models import VRF
    except ImportError:
        VRF = None

    from dcim.models import Device

    from .models import NSOBGPPeerState, NSODeviceManagement

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    now = timezone.now()
    seen_keys: set[tuple] = set()

    for router_entry in payload.get("routers") or []:
        asn_str = str(router_entry.get("asn") or "")
        if not asn_str:
            continue
        asn_obj = _get_or_create_asn(asn_str, ASN)
        if asn_obj is None:
            continue
        router_obj = _get_or_create_router(device, asn_obj, BGPRouter, ContentType, Device)
        for scope_entry in router_entry.get("scopes") or []:
            _reconcile_scope(
                mgmt,
                router_obj,
                scope_entry,
                asn_str,
                now,
                seen_keys,
                ASN,
                BGPScope,
                BGPAddressFamily,
                BGPPeer,
                BGPPeerAddressFamily,
                BGPPeerTemplate,
                IPAddress,
                VRF,
            )

    # Mark stale state rows (no longer reported by NSO)
    for stale in NSOBGPPeerState.objects.filter(management=mgmt):
        key = (mgmt.pk, stale.asn_str, stale.vrf_name, stale.peer_address_str)
        if key not in seen_keys:
            stale.status = "changed"
            stale.save(update_fields=["status"])

    return list(NSOBGPPeerState.objects.filter(management=mgmt).select_related("bgp_peer"))
