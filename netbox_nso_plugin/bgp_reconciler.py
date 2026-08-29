# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""BGP reconciler for A4.

Reads the adapter's GET /api/v1/devices/{id}/bgp-config response and
creates/updates the netbox-routing BGP object graph in NetBox.

Object creation order (FK prerequisites):
  ipam.ASN → BGPRouter → BGPScope → BGPAddressFamily → BGPPeer → BGPPeerAddressFamily

NSOBGPPeerState rows are kept as a compliance overlay so the operator can
see which peers were imported from NSO and track their write-path status.
"""

import hashlib
import ipaddress
import json
import logging

from .intent_state import mirror_reconciler

logger = logging.getLogger(__name__)


def _pk(obj):
    """FK target → pk (None-safe), for canonical content hashing."""
    return obj.pk if obj is not None else None


def _content_hash(content: dict) -> str:
    """Stable hash of a canonical content dict (for 3-way merge base comparison)."""
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


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


def _apply_router_id(router_obj, value) -> None:
    """Import the device's global BGP router-id onto BGPRouter on first read (when empty).

    router-id has no per-field overlay, so once it is set — whether by this import or by
    a later operator edit — the reconciler leaves it untouched. That keeps a pending
    operator edit from being clobbered before it can be accepted and pushed, while still
    mirroring the brownfield value on the initial import.
    """
    if not value or router_obj.router_id:
        return
    router_obj.router_id = value
    router_obj.save(update_fields=["router_id"])
    logger.debug("BGP: imported router-id %s on %s", value, router_obj)


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
    """Resolve a BGP session source to ``(ip_address, interface)``.

    ``source`` is either an IP (Junos/Nokia local-address) or an interface name
    (IOS/IOS-XR update-source, e.g. ``Loopback4``). Returns a 2-tuple in which at
    most one element is set: the matching ipam.IPAddress for an IP, or the
    device's dcim.Interface for an interface name. The interface is kept as itself
    (not collapsed to one of its IPs) so ``update-source Loopback0`` round-trips
    losslessly back to the IOS/IOS-XR writer. Both None when nothing matches (we
    don't fabricate objects).
    """
    if not source:
        return None, None
    import netaddr

    try:
        netaddr.IPAddress(source)
    except (netaddr.AddrFormatError, ValueError):
        pass
    else:
        # An IP local-address (Junos/Nokia). Reuse the stub-creating resolver used
        # for the peer neighbor address so a local-address not yet modeled in IPAM
        # still round-trips (the source is preserved and re-pushable) instead of
        # being silently dropped when no matching IPAddress exists.
        return _resolve_peer_ip(source, IPAddress), None
    try:
        from dcim.models import Interface

        iface = Interface.objects.filter(device=device, name=source).first()
        if iface is not None:
            return None, iface
    except Exception:
        pass
    return None, None


_PEER_FIELDS = (
    "enabled",
    "remote_as",
    "local_as",
    "peer_group",
    "source",
    "update_source",
    "ttl",
    "password",
    "bfd_enabled",
)
_PEER_FK_FIELDS = {"remote_as", "local_as", "peer_group", "source", "update_source"}


def _peer_desired(peer_data, remote_asn_obj, local_asn_obj, peer_group_obj, source_obj, update_source_obj):
    """Return the device-desired BGPPeer field values (objects for FKs)."""
    return {
        "enabled": peer_data.get("enabled"),
        "remote_as": remote_asn_obj,
        "local_as": local_asn_obj,
        "peer_group": peer_group_obj,
        "source": source_obj,
        "update_source": update_source_obj,
        "ttl": peer_data.get("ttl"),
        "password": peer_data.get("password"),
        "bfd_enabled": peer_data.get("bfd_enabled"),
    }


def _af_device_content(af_list: list) -> list:
    """Canonical per-AF policy content from the device payload (FKs resolved to pks).

    Shared by the BGP peer and the peer-group TEMPLATE 3-way merges — both carry the
    same per-AF route-map / prefix-list policy shape.
    """
    afs = []
    for paf in af_list or []:
        af_str = paf.get("af") or ""
        if not af_str:
            continue
        afs.append(
            {
                "af": af_str,
                "enabled": bool(paf.get("enabled", True)),
                "routemap_in": _pk(_resolve_routemap(paf.get("routemap_in"))),
                "routemap_out": _pk(_resolve_routemap(paf.get("routemap_out"))),
                "prefixlist_in": _pk(_resolve_prefixlist(paf.get("prefixlist_in"))),
                "prefixlist_out": _pk(_resolve_prefixlist(paf.get("prefixlist_out"))),
            }
        )
    return sorted(afs, key=lambda a: a["af"])


def _af_object_content(owner_obj) -> list:
    """Canonical per-AF policy content read back from a BGPPeer / BGPPeerTemplate object."""
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import BGPPeerAddressFamily

    ct = ContentType.objects.get_for_model(owner_obj.__class__)
    afs = []
    for paf in BGPPeerAddressFamily.objects.filter(
        assigned_object_type=ct, assigned_object_id=owner_obj.pk
    ).select_related("address_family"):
        afs.append(
            {
                "af": paf.address_family.address_family,
                "enabled": bool(paf.enabled),
                "routemap_in": paf.routemap_in_id,
                "routemap_out": paf.routemap_out_id,
                "prefixlist_in": paf.prefixlist_in_id,
                "prefixlist_out": paf.prefixlist_out_id,
            }
        )
    return sorted(afs, key=lambda a: a["af"])


def _drop_unset_update_source(content: dict) -> None:
    """Keep pre-``update_source`` base hashes valid across the migration.

    ``update_source`` (the IOS/IOS-XR update-source interface) is a new content
    key. Existing ``device_base_hash`` values were computed without it, so we omit
    it whenever it is unset: Junos/Nokia and cisco-without-update-source peers then
    keep their old hash (no phantom drift), while a cisco peer that DOES carry an
    update-source interface differs only on that key — so the 3-way merge
    auto-mirrors (adopts) it instead of flagging a conflict, migrating the peer off
    the old lossy source-IP onto the interface at the same time.
    """
    if content.get("update_source") is None:
        content.pop("update_source", None)


def _peer_device_content(desired: dict, af_list: list) -> dict:
    """Build canonical device-desired content (peer fields + AF policies), FKs as pks."""
    content = {f: (_pk(desired[f]) if f in _PEER_FK_FIELDS else desired[f]) for f in _PEER_FIELDS}
    _drop_unset_update_source(content)
    content["afs"] = _af_device_content(af_list)
    return content


def _peer_object_content(bgp_peer) -> dict:
    """Build canonical content read back from the netbox-routing BGPPeer object + its AFs."""
    content = {
        f: (getattr(bgp_peer, f"{f}_id") if f in _PEER_FK_FIELDS else getattr(bgp_peer, f)) for f in _PEER_FIELDS
    }
    _drop_unset_update_source(content)
    content["afs"] = _af_object_content(bgp_peer)
    return content


def _template_device_content(remote_asn_obj, af_list: list) -> dict:
    """Canonical device-desired content for a peer-group template (remote-AS + AF policies)."""
    return {"remote_as": _pk(remote_asn_obj), "afs": _af_device_content(af_list)}


def _template_object_content(template_obj) -> dict:
    """Canonical content read back from a netbox-routing BGPPeerTemplate object + its AFs."""
    return {"remote_as": template_obj.remote_as_id, "afs": _af_object_content(template_obj)}


def _write_template_fields(template_obj, remote_asn_obj) -> None:
    """Force the device-desired remote-AS onto the peer-group template (seed/auto-mirror)."""
    if template_obj.remote_as_id != _pk(remote_asn_obj):
        template_obj.remote_as = remote_asn_obj
        template_obj.save(update_fields=["remote_as"])


def _write_peer_fields(bgp_peer, desired: dict) -> None:
    """Force-write the device-desired field values onto the BGPPeer (seed/auto-mirror)."""
    for field, value in desired.items():
        setattr(bgp_peer, field, value)
    bgp_peer.save()


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


def _write_peer_afs(peer_obj, peer_af_list: list, scope_obj, BGPAddressFamily, BGPPeerAddressFamily) -> None:
    """Force-mirror a peer's/template's BGPPeerAddressFamily rows from the device.

    Create/update each device AF and prune AF rows the device dropped (mirror = match
    the device exactly). Called only on the 3-way *seed* / *auto-mirror* paths — both
    the BGP peer and the peer-group TEMPLATE merge gate this behind a base comparison,
    so an operator edit (object moved, device unchanged) is frozen and never reaches
    here, while a device-side change is mirrored in.
    """
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(peer_obj.__class__)
    seen_af_ids = set()
    for paf_entry in peer_af_list or []:
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
            for field, value in policy.items():
                setattr(obj, field, value)
            obj.save()
        seen_af_ids.add(af_obj.pk)
    # Prune AF rows the device dropped (mirror = match device exactly).
    BGPPeerAddressFamily.objects.filter(assigned_object_type=ct, assigned_object_id=peer_obj.pk).exclude(
        address_family_id__in=seen_af_ids
    ).delete()


def _resolve_vrf(vrf_name: str, VRF):
    """Look up an ipam.VRF by name; return None for global or if not found."""
    if not vrf_name or VRF is None:
        return None
    try:
        return VRF.objects.get(name=vrf_name)
    except VRF.DoesNotExist:
        logger.debug("BGP: VRF %r not found in NetBox, using global", vrf_name)
        return None


def _reconcile_one_peer(
    mgmt, scope_obj, peer_entry, asn_str, vrf_name, peer_address_str, ip_obj, resolved, now, models
):
    """3-way reconcile of one BGP peer: NetBox object vs device vs stored base.

    Distinguishes an operator edit (object moved, device == base → freeze + 'changed')
    from a device-side change (device moved, object == base → auto-mirror into NetBox),
    and flags both-moved as 'conflict'. ``resolved`` carries the device-resolved FK
    objects; ``models`` carries the netbox-routing classes.
    """
    from . import status_machine as sm
    from .models import NSOBGPPeerState

    BGPPeer = models["BGPPeer"]
    desired = _peer_desired(
        peer_entry,
        resolved["remote_as"],
        resolved["local_as"],
        resolved["peer_group"],
        resolved["source"],
        resolved["update_source"],
    )
    af_list = peer_entry.get("address_families") or []

    bgp_peer, created_peer = BGPPeer.objects.get_or_create(scope=scope_obj, peer=ip_obj, name=None, defaults=desired)
    state, state_created = NSOBGPPeerState.objects.get_or_create(
        management=mgmt,
        asn_str=asn_str,
        vrf_name=vrf_name,
        peer_address_str=peer_address_str,
        defaults={"status": "unknown"},
    )
    state.bgp_peer = bgp_peer
    state.remote_as_str = str(peer_entry.get("remote_as") or "")
    state.enabled = peer_entry.get("enabled")
    state.last_sync_at = now

    dev_hash = _content_hash(_peer_device_content(desired, af_list))
    obj_hash = _content_hash(_peer_object_content(bgp_peer))
    base = state.device_base_hash

    def _mirror():
        _write_peer_fields(bgp_peer, desired)
        _write_peer_afs(bgp_peer, af_list, scope_obj, models["BGPAddressFamily"], models["BGPPeerAddressFamily"])

    matches, conflict = True, False
    if (not created_peer) and state_created:
        # Adoption: a BGPPeer pre-existed that we never tracked — surface, never clobber.
        matches, conflict = False, True
    elif created_peer:
        _mirror()  # seed the new peer's AFs (peer fields already seeded by get_or_create)
        state.device_base_hash = dev_hash
    elif not base:
        # Bootstrap the base without clobbering (existing rows after the migration).
        state.device_base_hash = dev_hash
        matches = obj_hash == dev_hash
    elif obj_hash == dev_hash:
        state.device_base_hash = dev_hash  # in sync; advance base
    elif obj_hash == base and dev_hash != base:
        _mirror()  # device moved, NetBox untouched → auto-mirror
        state.device_base_hash = dev_hash
    elif dev_hash == base and obj_hash != base:
        matches = False  # NetBox edited, device unchanged → freeze + drift
    else:
        conflict = True  # both moved since base → conflict

    state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
    state.save()
    return state


def _reconcile_one_template(mgmt, scope_obj, template_obj, pg_entry, remote_asn_obj, now, models):
    """3-way reconcile of one BGP peer-group template: NetBox object vs device vs base.

    Same clobber-safe contract as :func:`_reconcile_one_peer` (operator edit frozen +
    flagged 'changed'; device-side change auto-mirrored; both-moved → 'conflict') applied
    to the template's remote-AS + per-AF policies, replacing the old seed-once behaviour.
    Templates have no apply path, so an accepted edit rests at ``accepted`` until the
    device adopts the same value (then the next reconcile settles it to ``in_sync``).
    """
    from . import status_machine as sm
    from .models import NSOBGPPeerTemplateState

    af_list = pg_entry.get("address_families") or []

    state, state_created = NSOBGPPeerTemplateState.objects.get_or_create(
        management=mgmt, template_name=template_obj.name, defaults={"status": "unknown"}
    )
    state.template = template_obj
    state.remote_as_str = str(pg_entry.get("remote_as") or "")
    state.last_sync_at = now

    dev_hash = _content_hash(_template_device_content(remote_asn_obj, af_list))
    obj_hash = _content_hash(_template_object_content(template_obj))
    base = state.device_base_hash

    def _mirror():
        _write_template_fields(template_obj, remote_asn_obj)
        _write_peer_afs(template_obj, af_list, scope_obj, models["BGPAddressFamily"], models["BGPPeerAddressFamily"])

    matches, conflict = True, False
    if state_created:
        _mirror()  # first time tracked: seed the template's AF policies from the device
        state.device_base_hash = dev_hash
    elif not base:
        # Bootstrap the base without clobbering (rows pre-dating the overlay/migration).
        state.device_base_hash = dev_hash
        matches = obj_hash == dev_hash
    elif obj_hash == dev_hash:
        state.device_base_hash = dev_hash  # in sync; advance base
    elif obj_hash == base and dev_hash != base:
        _mirror()  # device moved, NetBox untouched → auto-mirror
        state.device_base_hash = dev_hash
    elif dev_hash == base and obj_hash != base:
        matches = False  # operator edited the template, device unchanged → freeze + drift
    else:
        conflict = True  # both moved since base → conflict

    state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
    state.save()
    return state


def _reconcile_scope(
    mgmt,
    router_obj,
    scope_entry,
    asn_str,
    now,
    seen_keys,
    seen_template_names,
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

    for af_str in sorted(scope_entry.get("address_families") or []):
        if af_str:
            _get_or_create_address_family(scope_obj, af_str, BGPAddressFamily)

    for peer_entry in sorted(scope_entry.get("peers") or [], key=lambda row: row.get("peer_address") or ""):
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
        source_obj, update_source_obj = _resolve_bgp_source(mgmt.device, peer_entry.get("source"), IPAddress)

        _reconcile_one_peer(
            mgmt,
            scope_obj,
            peer_entry,
            asn_str,
            vrf_name,
            peer_address_str,
            ip_obj,
            {
                "remote_as": remote_asn_obj,
                "local_as": local_asn_obj,
                "peer_group": peer_group_obj,
                "source": source_obj,
                "update_source": update_source_obj,
            },
            now,
            {"BGPPeer": BGPPeer, "BGPAddressFamily": BGPAddressFamily, "BGPPeerAddressFamily": BGPPeerAddressFamily},
        )
        seen_keys.add((mgmt.pk, asn_str, vrf_name, peer_address_str))

    # Peer-group / template OBJECTS: model each as a BGPPeerTemplate carrying its
    # own per-AF route-map / prefix-list policies (not just inlined onto members).
    for pg_entry in sorted(scope_entry.get("peer_groups") or [], key=lambda row: (row.get("name") or "").casefold()):
        pg_name = pg_entry.get("name") or ""
        if not pg_name:
            continue
        pg_remote_as = str(pg_entry.get("remote_as") or "")
        pg_remote_asn_obj = _get_or_create_asn(pg_remote_as, ASN) if pg_remote_as else None
        template_obj = _get_or_create_peer_group(pg_name, BGPPeerTemplate, pg_remote_asn_obj)
        if template_obj is None:
            continue
        # Peer-group TEMPLATE: full 3-way merge of its remote-AS + per-AF policies via a
        # NSOBGPPeerTemplateState overlay (operator edits frozen, device changes mirrored,
        # both-moved → conflict) — replaces the old seed-once-never-touch behaviour.
        _reconcile_one_template(
            mgmt,
            scope_obj,
            template_obj,
            pg_entry,
            pg_remote_asn_obj,
            now,
            {"BGPAddressFamily": BGPAddressFamily, "BGPPeerAddressFamily": BGPPeerAddressFamily},
        )
        seen_template_names.add((mgmt.pk, pg_name))


@mirror_reconciler
def _reconcile_bgp_config(device, payload: dict) -> list:
    """Reconcile BGP config from adapter payload into NetBox netbox-routing BGP models.

    For each router → scope → peer entry reported by the adapter:
    - Ensures ipam.ASN, BGPRouter, BGPScope, BGPAddressFamily, BGPPeer exist.
    - Creates/updates NSOBGPPeerState overlay rows for compliance tracking.
    - Marks stale state rows (no longer reported) as status='changed'.

    Runs under ``suppress_intent_push()``: unlike the OSPF reconciler (which only links
    to existing native objects), this one MATERIALIZES ``BGPPeer`` rows, and a materialized
    peer now carries a greenfield-ownership ``post_save`` signal. Suppression stops that
    signal from force-owning + pushing brownfield adoption back to the adapter. Reentrant,
    so nesting under the sync path's own suppression (reconcile.py) is a harmless no-op.

    Returns a list of NSOBGPPeerState instances for this device.
    """
    from .signals import suppress_intent_push

    with suppress_intent_push():
        return _reconcile_bgp_config_impl(device, payload)


def _precreate_payload_asns(routers: list[dict], asn_model) -> None:
    """Create shared ASN rows in a stable order before nested BGP writes."""
    values = {
        str(value)
        for router in routers
        for value in (
            [router.get("asn")]
            + [
                peer.get(key)
                for scope in router.get("scopes") or []
                for peer in scope.get("peers") or []
                for key in ("remote_as", "local_as")
            ]
            + [
                group.get("remote_as")
                for scope in router.get("scopes") or []
                for group in scope.get("peer_groups") or []
            ]
        )
        if value not in (None, "")
    }
    for value in sorted(values, key=lambda item: (len(item), item)):
        _get_or_create_asn(value, asn_model)


def _reconcile_bgp_config_impl(device, payload: dict) -> list:
    """Body of :func:`_reconcile_bgp_config` (see it for the contract)."""
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
    seen_template_names: set[tuple] = set()

    routers = sorted(payload.get("routers") or [], key=lambda row: str(row.get("asn") or ""))
    _precreate_payload_asns(routers, ASN)

    for router_entry in routers:
        asn_str = str(router_entry.get("asn") or "")
        if not asn_str:
            continue
        asn_obj = _get_or_create_asn(asn_str, ASN)
        if asn_obj is None:
            continue
        router_obj = _get_or_create_router(device, asn_obj, BGPRouter, ContentType, Device)
        _apply_router_id(router_obj, router_entry.get("router_id"))
        for scope_entry in sorted(router_entry.get("scopes") or [], key=lambda row: row.get("vrf") or ""):
            _reconcile_scope(
                mgmt,
                router_obj,
                scope_entry,
                asn_str,
                now,
                seen_keys,
                seen_template_names,
                ASN,
                BGPScope,
                BGPAddressFamily,
                BGPPeer,
                BGPPeerAddressFamily,
                BGPPeerTemplate,
                IPAddress,
                VRF,
            )

    # Mark stale state rows (accepted/deploying intent preserved by on_reconcile)
    from . import status_machine as sm
    from .models import NSOBGPPeerTemplateState

    for stale in NSOBGPPeerState.objects.filter(management=mgmt):
        key = (mgmt.pk, stale.asn_str, stale.vrf_name, stale.peer_address_str)
        if key not in seen_keys:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])

    for stale_t in NSOBGPPeerTemplateState.objects.filter(management=mgmt):
        if (mgmt.pk, stale_t.template_name) not in seen_template_names:
            new_status = sm.on_reconcile(stale_t.status, present=False)
            if new_status != stale_t.status:
                stale_t.status = new_status
                stale_t.save(update_fields=["status"])

    return list(NSOBGPPeerState.objects.filter(management=mgmt).select_related("bgp_peer"))
