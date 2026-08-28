# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile the complete BGP graph through one exact mutation plan.

Reads the adapter's GET /api/v1/devices/{id}/bgp-config response and
creates/updates the netbox-routing BGP object graph in NetBox.

Object creation order (FK prerequisites):
  ipam.ASN → BGPRouter → BGPScope → BGPAddressFamily → BGPPeer → BGPPeerAddressFamily

NSOBGPPeerState rows are kept as a compliance overlay so the operator can
see which peers were imported from NSO and track their write-path status.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import ipaddress
import json
import logging
from dataclasses import replace

logger = logging.getLogger(__name__)


class _Operations:
    """Collect proposed writes and their deterministic replay data."""

    def __init__(self):
        self.saves = []
        self.deletes = []
        self.operations = []
        self.policy_footprint = None

    def save(
        self,
        instance,
        *,
        update_fields=None,
        force_insert=False,
        natural_key=(),
        references=(),
    ):
        from .renderer_writer import planned_save

        references = tuple(references)
        self.saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
                references=references,
            )
        )
        self.operations.append(("save", instance, update_fields, force_insert, references))

    def delete(self, instance):
        from .renderer_writer import planned_delete

        self.deletes.append(planned_delete(instance))
        self.operations.append(("delete", instance, None, False, ()))


def bgp_reconcile_plan(device, payload):
    """Freeze every shared, native, child, and overlay BGP write."""
    from django.utils import timezone

    from .intent_state import MutationFootprint
    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    try:
        operations = _bgp_reconcile_operations(device, payload, planned_at)
    except ImportError:
        return RendererMutationPlan.build(planned_at=planned_at)
    plan = RendererMutationPlan.build(
        saves=operations.saves,
        deletes=operations.deletes,
        planned_at=planned_at,
    )
    policy_footprint = operations.policy_footprint
    if policy_footprint is None:
        return plan
    policy_dependencies = MutationFootprint.for_keys(
        (),
        shared_keys=policy_footprint.shared_keys,
        source_rows=policy_footprint.source_rows,
        overlay_rows=policy_footprint.overlay_rows,
    )
    lock_footprint = MutationFootprint.merge(plan.lock_footprint, policy_dependencies)
    lock_footprint = replace(
        lock_footprint,
        device_ids=tuple(sorted({*lock_footprint.device_ids, *policy_footprint.device_ids})),
    )
    return replace(plan, lock_footprint=lock_footprint)


def _bgp_fk_identity(obj):
    """Return a stable BGP merge identity before or after a graph row is saved."""
    if obj is None:
        return None
    label = obj._meta.label_lower
    if label == "ipam.asn":
        return ("asn", int(obj.asn))
    if label == "ipam.ipaddress":
        return ("ip", str(obj.address))
    if label == "netbox_routing.bgppeertemplate":
        return ("template", obj.name)
    if label in ("netbox_routing.prefixlist", "netbox_routing.routemap"):
        return (label, obj.name)
    if label == "dcim.interface":
        return ("interface", obj.device_id, obj.name)
    return (label, obj.pk)


def _content_hash(content: dict) -> str:
    """Stable hash of a canonical content dict (for 3-way merge base comparison)."""
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


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


def _af_device_content(
    af_list: list,
    *,
    route_maps_by_name=None,
    prefix_lists_by_name=None,
) -> list:
    """Canonical per-AF policy content from the device payload.

    Shared by the BGP peer and the peer-group TEMPLATE 3-way merges — both carry the
    same per-AF route-map / prefix-list policy shape.
    """
    afs = []

    def resolve(name, objects_by_name, resolver):
        if not name:
            return None
        if objects_by_name is not None:
            return objects_by_name.get(name)
        return resolver(name)

    for paf in af_list or []:
        af_str = paf.get("af") or ""
        if not af_str:
            continue
        afs.append(
            {
                "af": af_str,
                "enabled": bool(paf.get("enabled", True)),
                "routemap_in": _bgp_fk_identity(
                    resolve(paf.get("routemap_in"), route_maps_by_name, _resolve_routemap)
                ),
                "routemap_out": _bgp_fk_identity(
                    resolve(paf.get("routemap_out"), route_maps_by_name, _resolve_routemap)
                ),
                "prefixlist_in": _bgp_fk_identity(
                    resolve(paf.get("prefixlist_in"), prefix_lists_by_name, _resolve_prefixlist)
                ),
                "prefixlist_out": _bgp_fk_identity(
                    resolve(paf.get("prefixlist_out"), prefix_lists_by_name, _resolve_prefixlist)
                ),
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
    ).select_related(
        "address_family",
        "routemap_in",
        "routemap_out",
        "prefixlist_in",
        "prefixlist_out",
    ):
        afs.append(
            {
                "af": paf.address_family.address_family,
                "enabled": bool(paf.enabled),
                "routemap_in": _bgp_fk_identity(paf.routemap_in),
                "routemap_out": _bgp_fk_identity(paf.routemap_out),
                "prefixlist_in": _bgp_fk_identity(paf.prefixlist_in),
                "prefixlist_out": _bgp_fk_identity(paf.prefixlist_out),
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


def _peer_device_content(
    desired: dict,
    af_list: list,
    *,
    route_maps_by_name=None,
    prefix_lists_by_name=None,
) -> dict:
    """Build canonical device content with stable natural FK identities."""
    content = {f: (_bgp_fk_identity(desired[f]) if f in _PEER_FK_FIELDS else desired[f]) for f in _PEER_FIELDS}
    _drop_unset_update_source(content)
    content["afs"] = _af_device_content(
        af_list,
        route_maps_by_name=route_maps_by_name,
        prefix_lists_by_name=prefix_lists_by_name,
    )
    return content


def _peer_object_content(bgp_peer) -> dict:
    """Build canonical content read back from the netbox-routing BGPPeer object + its AFs."""
    content = {
        f: (_bgp_fk_identity(getattr(bgp_peer, f)) if f in _PEER_FK_FIELDS else getattr(bgp_peer, f))
        for f in _PEER_FIELDS
    }
    _drop_unset_update_source(content)
    content["afs"] = _af_object_content(bgp_peer)
    return content


def _template_device_content(
    remote_asn_obj,
    af_list: list,
    *,
    route_maps_by_name=None,
    prefix_lists_by_name=None,
) -> dict:
    """Canonical device-desired content for a peer-group template (remote-AS + AF policies)."""
    return {
        "remote_as": _bgp_fk_identity(remote_asn_obj),
        "afs": _af_device_content(
            af_list,
            route_maps_by_name=route_maps_by_name,
            prefix_lists_by_name=prefix_lists_by_name,
        ),
    }


def _template_object_content(template_obj) -> dict:
    """Canonical content read back from a netbox-routing BGPPeerTemplate object + its AFs."""
    return {"remote_as": _bgp_fk_identity(template_obj.remote_as), "afs": _af_object_content(template_obj)}


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


class _BGPGraphPlanner:  # noqa: PLR0904
    """Build the deterministic writes for one BGP payload."""

    def __init__(self, device, payload, planned_at):  # noqa: PLR0915
        from dcim.models import Device
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, VRF, IPAddress
        from netbox_routing.models import (
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPPeerTemplate,
            BGPRouter,
            BGPScope,
            PrefixList,
            RouteMap,
        )

        from .intent_state import route_policy_footprint
        from .models import NSOBGPPeerState, NSOBGPPeerTemplateState, NSODeviceManagement

        self.device = device
        self.payload = {"routers": payload} if isinstance(payload, list) else payload
        self.planned_at = planned_at
        self.operations = _Operations()
        self.management = NSODeviceManagement.objects.filter(device=device).first()
        self.ASN = ASN
        self.RIR = RIR
        self.IPAddress = IPAddress
        self.VRF = VRF
        self.BGPAddressFamily = BGPAddressFamily
        self.BGPPeer = BGPPeer
        self.BGPPeerAddressFamily = BGPPeerAddressFamily
        self.BGPPeerTemplate = BGPPeerTemplate
        self.BGPRouter = BGPRouter
        self.BGPScope = BGPScope
        self.NSOBGPPeerState = NSOBGPPeerState
        self.NSOBGPPeerTemplateState = NSOBGPPeerTemplateState
        self.device_content_type = ContentType.objects.get_for_model(Device)
        self.peer_content_type = ContentType.objects.get_for_model(BGPPeer)
        self.template_content_type = ContentType.objects.get_for_model(BGPPeerTemplate)

        routers = self.payload.get("routers") or []
        reported_asns = {
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
        asn_values = set()
        for value in reported_asns:
            try:
                asn_values.add(int(value))
            except (TypeError, ValueError):
                continue
        self.reported_asns = reported_asns
        vrf_names = {scope.get("vrf") for router in routers for scope in router.get("scopes") or [] if scope.get("vrf")}
        template_names = {
            name
            for router in routers
            for scope in router.get("scopes") or []
            for name in (
                [peer.get("peer_group") for peer in scope.get("peers") or []]
                + [group.get("name") for group in scope.get("peer_groups") or []]
            )
            if name
        }
        self.asns = {
            row.asn: row for row in ASN.objects.filter(asn__in=asn_values).select_related("rir").order_by("pk")
        }
        self.rir = RIR.objects.filter(name="NSO Auto-Discovered").first()
        self.ips = {}
        self.vrfs = {row.name: row for row in VRF.objects.filter(name__in=vrf_names).order_by("pk")}
        self.routers = {
            int(row.asn.asn): row
            for row in BGPRouter.objects.filter(
                assigned_object_type=self.device_content_type,
                assigned_object_id=device.pk,
            )
            .select_related("asn")
            .order_by("pk")
            if row.asn_id is not None
        }
        self.scopes = {}
        for row in (
            BGPScope.objects.filter(router__in=self.routers.values())
            .select_related("router__asn", "vrf")
            .order_by("pk")
        ):
            self.scopes[(row.router_id, row.vrf_id)] = row
        self.address_families = {}
        for row in (
            BGPAddressFamily.objects.filter(scope__in=self.scopes.values())
            .select_related("scope__router__asn", "scope__vrf")
            .order_by("pk")
        ):
            scope_key = (row.scope.router_id, row.scope.vrf_id)
            self.address_families[(*scope_key, row.address_family)] = row
        self.peers = {}
        for row in (
            BGPPeer.objects.filter(scope__in=self.scopes.values(), name__isnull=True)
            .select_related("scope__router__asn", "scope__vrf", "peer")
            .order_by("pk")
        ):
            scope_key = (row.scope.router_id, row.scope.vrf_id)
            self.peers[(scope_key, row.peer_id)] = row
        self.templates = {
            row.name: row
            for row in BGPPeerTemplate.objects.filter(name__in=template_names)
            .select_related("remote_as")
            .order_by("pk")
        }
        self.template_saved = set()
        self.peer_states = (
            {
                (row.asn_str, row.vrf_name, row.peer_address_str): row
                for row in NSOBGPPeerState.objects.filter(management=self.management)
                .select_related("bgp_peer")
                .order_by("pk")
            }
            if self.management is not None
            else {}
        )
        self.template_states = (
            {
                row.template_name: row
                for row in NSOBGPPeerTemplateState.objects.filter(management=self.management)
                .select_related("template")
                .order_by("pk")
            }
            if self.management is not None
            else {}
        )
        self.seen_peers = set()
        self.seen_templates = set()
        policy_entries = tuple(
            address_family
            for router in payload.get("routers", []) or []
            if isinstance(router, dict)
            for scope in router.get("scopes", []) or []
            if isinstance(scope, dict)
            for owner_key in ("peers", "peer_groups")
            for owner in scope.get(owner_key, []) or []
            if isinstance(owner, dict)
            for address_family in owner.get("address_families", []) or []
            if isinstance(address_family, dict)
        )
        policy_groups = {
            (family, address_family.get(field))
            for address_family in policy_entries
            for family, fields in (
                ("route_map", ("routemap_in", "routemap_out")),
                ("prefix_list", ("prefixlist_in", "prefixlist_out")),
            )
            for field in fields
            if address_family.get(field)
        }
        route_map_names = {name for family, name in policy_groups if family == "route_map"}
        prefix_list_names = {name for family, name in policy_groups if family == "prefix_list"}
        self.route_maps_by_name = {row.name: row for row in RouteMap.objects.filter(name__in=route_map_names)}
        self.prefix_lists_by_name = {
            row.name: row for row in PrefixList.objects.filter(name__in=prefix_list_names)
        }
        self.operations.policy_footprint = route_policy_footprint(policy_groups)

    def save(self, *args, **kwargs):
        self.operations.save(*args, **kwargs)

    @staticmethod
    def _router_key(router):
        if router.pk is not None:
            return router.pk
        return ("planned-router", int(router.asn.asn))

    def _scope_key(self, scope):
        return (self._router_key(scope.router), scope.vrf_id)

    @staticmethod
    def _peer_ip_key(peer_ip):
        if peer_ip.pk is not None:
            return peer_ip.pk
        return ("planned-ip", str(peer_ip.address))

    def _ensure_rir(self):
        if self.rir is None:
            self.rir = self.RIR(
                name="NSO Auto-Discovered",
                slug="nso-auto-discovered",
                is_private=True,
            )
            self.save(
                self.rir,
                force_insert=True,
                natural_key=("name",),
            )
        return self.rir

    def asn(self, value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.warning("BGP: invalid ASN value %r, skipping router", value)
            return None
        current = self.asns.get(number)
        if current is not None:
            return current
        current = self.ASN(asn=number, rir=self._ensure_rir())
        self.asns[number] = current
        self.save(current, force_insert=True, natural_key=("asn",))
        return current

    def peer_ip(self, value):
        from django.core.exceptions import ValidationError

        try:
            target = ipaddress.ip_address(value)
        except ValueError:
            logger.warning("BGP: invalid peer IP address %r", value)
            return None
        address = str(target)
        if address in self.ips:
            return self.ips[address]
        try:
            current = self.IPAddress.objects.filter(address__net_host=address).first()
            if current is None:
                mask = 32 if target.version == 4 else 128
                current = self.IPAddress(address=f"{address}/{mask}")
                current.full_clean()
                self.save(
                    current,
                    force_insert=True,
                    natural_key=("address", "vrf"),
                )
        except ValidationError as exc:
            logger.warning("BGP: peer IP address %r rejected by NetBox: %s", value, exc)
            return None
        self.ips[address] = current
        return current

    def source(self, value):
        if not value:
            return None, None
        import netaddr

        try:
            netaddr.IPAddress(value)
        except (netaddr.AddrFormatError, ValueError):
            from dcim.models import Interface

            return None, Interface.objects.filter(device=self.device, name=value).first()
        return self.peer_ip(value), None

    def router(self, asn, router_id):
        number = int(asn.asn)
        current = self.routers.get(number)
        if current is None:
            current = self.BGPRouter(
                assigned_object_type=self.device_content_type,
                assigned_object_id=self.device.pk,
                asn=asn,
                name=str(number),
                router_id=router_id or None,
            )
            self.routers[number] = current
            self.save(
                current,
                force_insert=True,
                natural_key=("assigned_object_type", "assigned_object_id", "asn"),
            )
        elif router_id and not current.router_id:
            current = copy.copy(current)
            current.router_id = router_id
            self.routers[number] = current
            self.save(current, update_fields=("router_id",))
        return current

    def scope(self, router, vrf_name):
        vrf = self.vrfs.get(vrf_name) if vrf_name else None
        key = (self._router_key(router), vrf.pk if vrf is not None else None)
        current = self.scopes.get(key)
        if current is None:
            current = self.BGPScope(router=router, vrf=vrf)
            self.scopes[key] = current
            self.save(
                current,
                force_insert=True,
                natural_key=("router", "vrf"),
            )
        return current

    def address_family(self, scope, value):
        scope_key = self._scope_key(scope)
        key = (*scope_key, value)
        current = self.address_families.get(key)
        if current is None:
            current = self.BGPAddressFamily(scope=scope, address_family=value)
            self.address_families[key] = current
            self.save(
                current,
                force_insert=True,
                natural_key=("scope", "address_family"),
            )
        return current

    def template(self, name, remote_as):
        if not name:
            return None
        current = self.templates.get(name)
        created = current is None
        if created:
            current = self.BGPPeerTemplate(name=name, remote_as=remote_as)
            self.templates[name] = current
        elif remote_as is not None and _bgp_fk_identity(current.remote_as) != _bgp_fk_identity(remote_as):
            if name not in self.template_saved:
                current = copy.copy(current)
            current.remote_as = remote_as
            self.templates[name] = current
        else:
            return current
        if name not in self.template_saved:
            self.template_saved.add(name)
            self.save(
                current,
                update_fields=None if created else ("remote_as",),
                force_insert=created,
                natural_key=("name",) if created else (),
            )
        return current

    def _policy(self, entry):
        return {
            "enabled": entry.get("enabled", True),
            "routemap_in": self.route_maps_by_name.get(entry.get("routemap_in")),
            "routemap_out": self.route_maps_by_name.get(entry.get("routemap_out")),
            "prefixlist_in": self.prefix_lists_by_name.get(entry.get("prefixlist_in")),
            "prefixlist_out": self.prefix_lists_by_name.get(entry.get("prefixlist_out")),
        }

    def plan_address_family_rows(self, owner, entries, scope):
        content_type = self.peer_content_type if isinstance(owner, self.BGPPeer) else self.template_content_type
        existing = (
            {
                row.address_family.address_family: row
                for row in self.BGPPeerAddressFamily.objects.filter(
                    assigned_object_type=content_type,
                    assigned_object_id=owner.pk,
                )
                .select_related("address_family")
                .order_by("pk")
            }
            if owner.pk is not None
            else {}
        )
        seen = set()
        for entry in entries or []:
            value = entry.get("af") or ""
            if not value:
                continue
            seen.add(value)
            address_family = self.address_family(scope, value)
            current = existing.get(value)
            created = current is None
            policy = self._policy(entry)
            row = (
                self.BGPPeerAddressFamily(
                    assigned_object_type=content_type,
                    assigned_object_id=owner.pk,
                    address_family=address_family,
                    **policy,
                )
                if created
                else copy.copy(current)
            )
            if not created:
                for field, field_value in policy.items():
                    setattr(row, field, field_value)
            references = (("assigned_object_id", owner),) if owner.pk is None else ()
            self.save(
                row,
                force_insert=created,
                natural_key=("assigned_object_type", "assigned_object_id", "address_family") if created else (),
                references=references,
            )
        for value, row in existing.items():
            if value not in seen:
                self.operations.delete(row)

    def reconcile_peer(self, scope, entry, asn_str, vrf_name):  # noqa: PLR0915
        from . import status_machine as sm

        address = entry.get("peer_address") or ""
        if not address:
            return
        peer_ip = self.peer_ip(address)
        if peer_ip is None:
            logger.warning("BGP: could not resolve IP for peer %r; skipping", address)
            return
        remote_as = self.asn(entry.get("remote_as")) if entry.get("remote_as") not in (None, "") else None
        local_as = self.asn(entry.get("local_as")) if entry.get("local_as") not in (None, "") else None
        peer_group = self.template(entry.get("peer_group") or "", remote_as)
        source, update_source = self.source(entry.get("source"))
        desired = _peer_desired(entry, remote_as, local_as, peer_group, source, update_source)
        af_entries = entry.get("address_families") or []
        peer_key = (self._scope_key(scope), self._peer_ip_key(peer_ip))
        current_peer = self.peers.get(peer_key)
        created_peer = current_peer is None
        peer = (
            self.BGPPeer(scope=scope, peer=peer_ip, name=None, **desired) if created_peer else copy.copy(current_peer)
        )
        if created_peer:
            self.peers[peer_key] = peer
            self.save(
                peer,
                force_insert=True,
                natural_key=("scope", "peer", "name"),
            )

        state_key = (asn_str, vrf_name, address)
        current_state = self.peer_states.get(state_key)
        state_created = current_state is None
        state = (
            self.NSOBGPPeerState(
                management=self.management, asn_str=asn_str, vrf_name=vrf_name, peer_address_str=address
            )
            if state_created
            else copy.copy(current_state)
        )
        state.bgp_peer = peer
        state.remote_as_str = str(entry.get("remote_as") or "")
        state.enabled = entry.get("enabled")
        state.last_sync_at = self.planned_at
        device_hash = _content_hash(
            _peer_device_content(
                desired,
                af_entries,
                route_maps_by_name=self.route_maps_by_name,
                prefix_lists_by_name=self.prefix_lists_by_name,
            )
        )
        object_hash = device_hash if created_peer else _content_hash(_peer_object_content(current_peer))
        base = state.device_base_hash
        matches = True
        conflict = False
        mirror = False
        if not created_peer and state_created:
            matches = False
            conflict = True
        elif created_peer:
            mirror = True
            state.device_base_hash = device_hash
        elif not base:
            state.device_base_hash = device_hash
            matches = object_hash == device_hash
        elif object_hash == device_hash:
            state.device_base_hash = device_hash
        elif object_hash == base and device_hash != base:
            mirror = True
            state.device_base_hash = device_hash
        elif device_hash == base and object_hash != base:
            matches = False
        else:
            conflict = True
        if mirror:
            if not created_peer:
                for field, value in desired.items():
                    setattr(peer, field, value)
                self.save(peer)
            self.plan_address_family_rows(peer, af_entries, scope)
        state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
        self.peer_states[state_key] = state
        self.seen_peers.add(state_key)
        self.save(
            state,
            force_insert=state_created,
            natural_key=("management", "asn_str", "vrf_name", "peer_address_str") if state_created else (),
        )

    def reconcile_template(self, scope, entry):
        from . import status_machine as sm

        name = entry.get("name") or ""
        if not name:
            return
        remote_as = self.asn(entry.get("remote_as")) if entry.get("remote_as") not in (None, "") else None
        template = self.template(name, remote_as)
        current_state = self.template_states.get(name)
        state_created = current_state is None
        state = (
            self.NSOBGPPeerTemplateState(management=self.management, template_name=name)
            if state_created
            else copy.copy(current_state)
        )
        state.template = template
        state.remote_as_str = str(entry.get("remote_as") or "")
        state.last_sync_at = self.planned_at
        af_entries = entry.get("address_families") or []
        device_hash = _content_hash(
            _template_device_content(
                remote_as,
                af_entries,
                route_maps_by_name=self.route_maps_by_name,
                prefix_lists_by_name=self.prefix_lists_by_name,
            )
        )
        object_hash = _content_hash(_template_object_content(template))
        base = state.device_base_hash
        matches = True
        conflict = False
        mirror = False
        if state_created:
            mirror = True
            state.device_base_hash = device_hash
        elif not base:
            state.device_base_hash = device_hash
            matches = object_hash == device_hash
        elif object_hash == device_hash:
            state.device_base_hash = device_hash
        elif object_hash == base and device_hash != base:
            mirror = True
            state.device_base_hash = device_hash
        elif device_hash == base and object_hash != base:
            matches = False
        else:
            conflict = True
        if mirror:
            self.plan_address_family_rows(template, af_entries, scope)
        state.status = sm.on_reconcile(state.status, matches=matches, conflict=conflict)
        self.template_states[name] = state
        self.seen_templates.add(name)
        self.save(
            state,
            force_insert=state_created,
            natural_key=("management", "template_name") if state_created else (),
        )

    def build(self):
        if self.management is None:
            return self.operations
        routers = sorted(self.payload.get("routers") or [], key=lambda row: str(row.get("asn") or ""))
        for value in sorted(self.reported_asns, key=lambda item: (len(item), item)):
            self.asn(value)
        for router_entry in routers:
            asn_str = str(router_entry.get("asn") or "")
            asn = self.asn(asn_str) if asn_str else None
            if asn is None:
                continue
            router = self.router(asn, router_entry.get("router_id"))
            for scope_entry in sorted(router_entry.get("scopes") or [], key=lambda row: row.get("vrf") or ""):
                vrf_name = scope_entry.get("vrf") or ""
                scope = self.scope(router, vrf_name)
                for value in sorted(scope_entry.get("address_families") or []):
                    if value:
                        self.address_family(scope, value)
                for entry in sorted(scope_entry.get("peers") or [], key=lambda row: row.get("peer_address") or ""):
                    self.reconcile_peer(scope, entry, asn_str, vrf_name)
                for entry in sorted(
                    scope_entry.get("peer_groups") or [],
                    key=lambda row: (row.get("name") or "").casefold(),
                ):
                    self.reconcile_template(scope, entry)
        self.plan_stale_states()
        return self.operations

    def plan_stale_states(self):
        from . import status_machine as sm

        for key, current in self.peer_states.items():
            if key in self.seen_peers:
                continue
            status = sm.on_reconcile(current.status, present=False)
            if status != current.status:
                state = copy.copy(current)
                state.status = status
                self.save(state, update_fields=("status",))
        for name, current in self.template_states.items():
            if name in self.seen_templates:
                continue
            status = sm.on_reconcile(current.status, present=False)
            if status != current.status:
                state = copy.copy(current)
                state.status = status
                self.save(state, update_fields=("status",))


def _bgp_reconcile_operations(device, payload, planned_at):
    return _BGPGraphPlanner(device, payload, planned_at).build()


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
    from .models import NSOBGPPeerState, NSODeviceManagement
    from .renderer_writer import (
        active_renderer_writer,
        renderer_mirror_writes,
        renderer_writes,
        replay_creation_references,
    )
    from .signals import suppress_intent_push

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return []
    active = active_renderer_writer()
    plan = active.plan if active is not None else bgp_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        operations = _bgp_reconcile_operations(device, payload, plan.planned_at)
        for operation, instance, update_fields, force_insert, references in operations.operations:
            if operation == "delete":
                writer.delete(instance)
                continue
            replay_creation_references(instance, references)
            writer.save(instance, update_fields=update_fields, force_insert=force_insert)
    return list(NSOBGPPeerState.objects.filter(management=management).select_related("bgp_peer"))
