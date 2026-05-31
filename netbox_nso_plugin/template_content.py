# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox template extensions — interface badge (device NSO tab is now a registered tab view)."""

import logging
from datetime import datetime

from django.apps import apps
from django.utils import timezone
from netbox.plugins import PluginTemplateExtension

logger = logging.getLogger(__name__)

# Status → Bootstrap badge colour
_STATUS_BADGE = {
    "unknown": "secondary",
    "imported": "success",
    "changed": "warning",
    "accepted": "info",
    "deploying": "primary",
    "in_sync": "success",
    "apply_failed": "danger",
    "drifted": "warning",
    "error": "danger",
}


def _upsert_interface_states(device, interfaces: list) -> dict:
    """Sync NSOInterfaceState rows from adapter interface data.

    Returns a dict keyed by (interface_name, attribute) → NSOInterfaceState instance.
    Only updates fields that come from the adapter; never overwrites accepted_at.
    """
    from dcim.models import Interface

    from .models import NSOInterfaceState

    # Build name → Interface map for this device's interfaces in the DB
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}

    result: dict = {}
    for iface_data in interfaces or []:
        iface = iface_map.get(iface_data["name"])
        if iface is None:
            continue
        for attr_name, attr_data in (iface_data.get("attrs") or {}).items():
            nso_value = attr_data.get("nso_value") or ""
            status = attr_data.get("status") or "unknown"
            last_apply_at = None
            if attr_data.get("last_apply_at"):
                try:
                    last_apply_at = datetime.fromisoformat(attr_data["last_apply_at"].rstrip("Z"))
                except ValueError:
                    pass

            state, _ = NSOInterfaceState.objects.get_or_create(
                interface=iface,
                attribute=attr_name,
                defaults={"status": status, "nso_value": nso_value},
            )
            # Update fields that come from adapter; never touch accepted_at
            update_fields = []
            if state.status != status:
                state.status = status
                update_fields.append("status")
            if state.nso_value != nso_value:
                state.nso_value = nso_value
                update_fields.append("nso_value")
            if last_apply_at and state.last_apply_at != last_apply_at:
                state.last_apply_at = last_apply_at
                update_fields.append("last_apply_at")
            last_apply_error = attr_data.get("last_apply_error")
            if state.last_apply_error != last_apply_error:
                state.last_apply_error = last_apply_error
                update_fields.append("last_apply_error")
            # Stamp last_sync_at
            state.last_sync_at = timezone.now()
            update_fields.append("last_sync_at")
            if update_fields:
                state.save(update_fields=update_fields)

            result[(iface_data["name"], attr_name)] = state

    return result


def _reconcile_lag_topology(device, lag_data: dict) -> dict:
    """Reconcile adapter LAG topology against NetBox interfaces.

    Besides building the device-tab display structure, this writes NetBox's
    native LAG model: each existing bundle interface is set to ``type='lag'``
    and each existing member's ``lag`` FK is pointed at it; members no longer
    reported in a bundle are unlinked (drift). Interfaces missing from NetBox
    are not created here — they come from device sync — and are reported as
    ``netbox_interface=None``.
    """
    from dcim.models import Interface

    iface_map = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    reconciled_lags = []

    for lag in lag_data.get("lags") or []:
        bundle = iface_map.get(lag.get("name"))
        if bundle is not None and bundle.type != "lag":
            bundle.type = "lag"
            bundle.save(update_fields=["type"])

        reconciled_members = []
        member_names: set[str] = set()
        for member in lag.get("members") or []:
            member_name = member.get("interface")
            member_names.add(member_name)
            member_iface = iface_map.get(member_name)
            if bundle is not None and member_iface is not None and member_iface.lag_id != bundle.id:
                member_iface.lag = bundle
                member_iface.save(update_fields=["lag"])
            reconciled_members.append({**member, "netbox_interface": member_iface})

        # Drift: unlink NetBox members that NSO no longer reports in this bundle.
        if bundle is not None:
            for stale in Interface.objects.filter(device=device, lag=bundle).exclude(name__in=member_names):
                stale.lag = None
                stale.save(update_fields=["lag"])

        reconciled_lags.append(
            {
                **lag,
                "netbox_interface": bundle,
                "members": reconciled_members,
            }
        )

    return {
        "refresh_source": lag_data.get("refresh_source"),
        "last_refreshed_at": lag_data.get("last_refreshed_at"),
        "lags": reconciled_lags,
    }


def _build_payload_index(payload: dict) -> tuple[set, dict, dict]:
    """Parse the adapter payload into a lookup set, attribute map, and bound-port map.

    Returns:
        payload_set: set of (iface_name, address, vrf_name)
        attr_map: {(iface_name, address, vrf_name): {"family": ..., "secondary": ...}}
        bound_port_map: {iface_name: bound_port_str} — only populated for Nokia devices;
            maps the logical router-interface name to its physical port binding.

    """
    payload_set: set[tuple[str, str, str]] = set()
    attr_map: dict = {}
    bound_port_map: dict[str, str] = {}
    for iface_entry in payload.get("interfaces") or []:
        iface_name = iface_entry.get("interface", "")
        bound_port = iface_entry.get("bound_port")
        if bound_port:
            bound_port_map[iface_name] = bound_port
        for addr_entry in iface_entry.get("addresses") or []:
            addr = addr_entry.get("address", "")
            vrf = addr_entry.get("vrf") or ""
            if addr:
                key = (iface_name, addr, vrf)
                payload_set.add(key)
                attr_map[key] = {
                    "family": addr_entry.get("family", "ipv4"),
                    "secondary": addr_entry.get("secondary", False),
                }
    return payload_set, attr_map, bound_port_map


def _create_and_link_ip(address, vrf_obj, iface, Prefix, logger, transaction, ValidationError) -> str:
    """Create an IPAddress in IPAM, assign it to *iface*, and link a containing Prefix.

    Returns the new status string: 'in_sync', 'conflict', or 'error'.
    """
    from ipam.models import IPAddress

    try:
        ip_obj = IPAddress(address=address, vrf=vrf_obj)
        ip_obj.assigned_object = iface
        ip_obj.full_clean()
        with transaction.atomic():
            ip_obj.save()
        try:
            containing = Prefix.objects.filter(
                prefix__net_contains=address.split("/")[0],
                vrf=vrf_obj,
            ).first()
            if containing is None:
                with transaction.atomic():
                    Prefix(prefix=address, vrf=vrf_obj).save()
        except Exception as prefix_exc:  # pragma: no cover
            logger.warning("nso_ip.prefix_link_failed addr=%s: %s", address, repr(prefix_exc))
        return "in_sync"
    except ValidationError:
        return "conflict"
    except Exception as exc:  # pragma: no cover
        logger.warning("nso_ip.create_failed addr=%s: %s", address, repr(exc))
        return "error"


def _retire_stale_ip_states(device, payload_set, VRF, IPAddress, now, transaction, NSOInterfaceIPState) -> None:
    """Mark state rows no longer in *payload_set* as 'changed' and unassign their IPs."""
    existing_states = NSOInterfaceIPState.objects.filter(interface__device=device).select_related("interface")
    for state in existing_states:
        key = (state.interface.name, state.address, state.vrf)
        if key not in payload_set and state.status not in ("changed",):
            vrf_obj = None
            if state.vrf and VRF is not None:
                try:
                    vrf_obj = VRF.objects.get(name=state.vrf)
                except VRF.DoesNotExist:
                    pass
            ip_obj = IPAddress.objects.filter(
                address=state.address, vrf=vrf_obj, assigned_object_id=state.interface_id
            ).first()
            if ip_obj is not None:
                ip_obj.assigned_object = None
                with transaction.atomic():
                    ip_obj.save(update_fields=["assigned_object_type", "assigned_object_id"])
            state.status = "changed"
            state.last_sync_at = now
            state.save(update_fields=["status", "last_sync_at"])


def _activate_auto_assigned_ip(state, existing_ip, address, iface, IPAddress, logger):
    """Promote *existing_ip* (and its P2P peer if applicable) from reserved → active.

    Called when an auto-assigned IP reaches ``in_sync`` for the first time.
    For P2P pairs, the second end to arrive in_sync also activates the peer's IP.
    """
    peer_state = state.peer_state
    if peer_state is not None and peer_state.status != "in_sync":
        return  # first P2P end — wait for peer

    try:
        existing_ip.status = "active"
        existing_ip.save(update_fields=["status"])
    except Exception as _exc:
        logger.warning("nso_ip.activate_failed addr=%s iface=%s: %s", address, iface, repr(_exc))

    if peer_state is not None:
        try:
            peer_ip = IPAddress.objects.filter(
                address=peer_state.address,
                assigned_object_id=peer_state.interface_id,
                status="reserved",
            ).first()
            if peer_ip is not None:
                peer_ip.status = "active"
                peer_ip.save(update_fields=["status"])
        except Exception as _exc:
            logger.warning("nso_ip.activate_peer_failed peer_addr=%s: %s", peer_state.address, repr(_exc))


def _reconcile_interface_ips(device, payload: dict) -> list:
    """Reconcile IP addresses from the adapter payload into NetBox IPAM.

    For each address reported by NSO:
    - If an ipam.IPAddress with matching address+VRF is already assigned to
      this interface → update NSOInterfaceIPState to reflect current status.
    - If it exists but is assigned elsewhere → set status='conflict'; do NOT
      reassign without an explicit operator override.
    - If it does not exist and INTERFACE_IP_AUTO_CREATE is True → create it,
      assign to interface, link containing Prefix if found.
    - If it does not exist and INTERFACE_IP_AUTO_CREATE is False → land as
      status='imported' (awaiting operator accept before IPAM write).

    Addresses previously tracked (NSOInterfaceIPState rows) but no longer
    reported by NSO are unassigned from the interface and set to
    status='changed' for operator review.

    Returns a list of NSOInterfaceIPState instances (all current rows for device).
    """
    import logging

    from dcim.models import Interface
    from django.apps import apps
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from django.utils import timezone
    from ipam.models import IPAddress, Prefix

    try:
        from ipam.models import VRF
    except ImportError:
        VRF = None

    from .models import NSOInterfaceIPState

    logger = logging.getLogger(__name__)
    cfg = apps.get_app_config("netbox_nso_plugin")
    auto_create = getattr(cfg, "_interface_ip_auto_create", False)

    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    payload_set, attr_map, bound_port_map = _build_payload_index(payload)
    now = timezone.now()

    for iface_name, address, vrf_name in payload_set:
        # For Nokia devices: logical router-interface → physical port lookup.
        # bound_port_map[iface_name] = physical port ID (e.g. "1/1/c11/1" or "lag-99:10").
        # If the logical interface isn't in iface_map (Nokia logical names differ from
        # dcim.Interface names), fall back to the bound_port as the interface name.
        iface = iface_map.get(iface_name)
        if iface is None and iface_name in bound_port_map:
            bound_port = bound_port_map[iface_name]
            iface = iface_map.get(bound_port)
        if iface is None:
            continue

        vrf_obj = None
        if vrf_name and VRF is not None:
            try:
                vrf_obj = VRF.objects.get(name=vrf_name)
            except VRF.DoesNotExist:
                pass

        attrs = attr_map.get((iface_name, address, vrf_name), {})
        family = attrs.get("family", "ipv4")
        secondary = attrs.get("secondary", False)

        state, _ = NSOInterfaceIPState.objects.get_or_create(
            interface=iface,
            address=address,
            vrf=vrf_name,
            defaults={"status": "unknown", "nso_value": address, "family": family, "secondary": secondary},
        )
        state.nso_value = address
        state.family = family
        state.secondary = secondary
        state.last_sync_at = now

        prev_status = state.status
        existing_ip = IPAddress.objects.filter(address=address, vrf=vrf_obj).first()
        if existing_ip is not None:
            if existing_ip.assigned_object == iface:
                if state.status not in ("deploying",):
                    state.status = "in_sync"
                if (
                    state.auto_assigned
                    and state.status == "in_sync"
                    and prev_status != "in_sync"
                    and existing_ip.status == "reserved"
                ):
                    _activate_auto_assigned_ip(state, existing_ip, address, iface, IPAddress, logger)
            else:
                state.status = "conflict"
        elif auto_create and state.status not in ("conflict",):
            state.status = _create_and_link_ip(address, vrf_obj, iface, Prefix, logger, transaction, ValidationError)
        elif state.status not in ("accepted", "deploying", "in_sync", "conflict"):
            state.status = "imported"

        state.save()

    _retire_stale_ip_states(device, payload_set, VRF, IPAddress, now, transaction, NSOInterfaceIPState)

    return list(NSOInterfaceIPState.objects.filter(interface__device=device).select_related("interface"))


def _reconcile_snmp_config(device, payload: dict) -> dict:
    """Full-replace import of SNMP config from adapter into plugin SNMP state models.

    Applies import semantics: existing rows whose key matches the payload are
    updated; rows absent from the payload are deleted; new rows are created with
    status=``imported``.  Rows already in ``accepted``/``deploying``/``in_sync``
    retain their status (write path progress must not be clobbered by a read
    refresh).

    Returns a dict with keys ``communities``, ``v3_users``, ``hosts``,
    ``system_info``, ``last_refreshed_at``, ``refresh_source`` for the template.
    """
    from django.utils import timezone

    from .models import (
        NSODeviceManagement,
        NSOSnmpCommunityState,
        NSOSnmpHostState,
        NSOSnmpSystemInfoState,
        NSOSnmpV3UserState,
    )

    _WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync"}
    now = timezone.now()

    try:
        mgmt = device.nso_management
    except NSODeviceManagement.DoesNotExist:
        return {"communities": [], "v3_users": [], "hosts": [], "system_info": None}

    # ── Communities ────────────────────────────────────────────────────────────
    incoming_community_hashes = set()
    for entry in payload.get("communities") or []:
        h = entry.get("community_hash") or ""
        if not h:
            continue
        incoming_community_hashes.add(h)
        state, _ = NSOSnmpCommunityState.objects.get_or_create(management=mgmt, community_hash=h)
        state.access = entry.get("access") or "RO"
        state.acl = entry.get("acl") or ""
        state.has_secret = bool(entry.get("has_secret", True))
        state.last_sync_at = now
        if state.status not in _WRITE_PATH_STATUSES:
            state.status = "imported"
        state.save()
    NSOSnmpCommunityState.objects.filter(management=mgmt).exclude(community_hash__in=incoming_community_hashes).delete()

    # ── V3 users ───────────────────────────────────────────────────────────────
    incoming_usernames = set()
    for entry in payload.get("v3_users") or []:
        username = entry.get("username") or ""
        if not username:
            continue
        incoming_usernames.add(username)
        state, _ = NSOSnmpV3UserState.objects.get_or_create(management=mgmt, username=username)
        state.has_auth_secret = bool(entry.get("has_auth_secret", False))
        state.has_priv_secret = bool(entry.get("has_priv_secret", False))
        state.last_sync_at = now
        if state.status not in _WRITE_PATH_STATUSES:
            state.status = "imported"
        state.save()
    NSOSnmpV3UserState.objects.filter(management=mgmt).exclude(username__in=incoming_usernames).delete()

    # ── Hosts ──────────────────────────────────────────────────────────────────
    incoming_addresses = set()
    for entry in payload.get("hosts") or []:
        address = entry.get("address") or ""
        if not address:
            continue
        incoming_addresses.add(address)
        state, _ = NSOSnmpHostState.objects.get_or_create(management=mgmt, address=address)
        state.version = entry.get("version") or "v2c"
        state.notify_type = entry.get("notify_type") or "trap"
        state.port = entry.get("port")
        state.community_hash = entry.get("community_hash") or ""
        state.last_sync_at = now
        if state.status not in _WRITE_PATH_STATUSES:
            state.status = "imported"
        state.save()
    NSOSnmpHostState.objects.filter(management=mgmt).exclude(address__in=incoming_addresses).delete()

    # ── System info ────────────────────────────────────────────────────────────
    sys_data = payload.get("system_info") or {}
    system_info_state = None
    if sys_data:
        system_info_state, _ = NSOSnmpSystemInfoState.objects.get_or_create(management=mgmt)
        system_info_state.location = sys_data.get("location") or ""
        system_info_state.contact = sys_data.get("contact") or ""
        system_info_state.last_sync_at = now
        if system_info_state.status not in _WRITE_PATH_STATUSES:
            system_info_state.status = "imported"
        system_info_state.save()

    return {
        "communities": list(NSOSnmpCommunityState.objects.filter(management=mgmt)),
        "v3_users": list(NSOSnmpV3UserState.objects.filter(management=mgmt)),
        "hosts": list(NSOSnmpHostState.objects.filter(management=mgmt)),
        "system_info": system_info_state,
        "last_refreshed_at": payload.get("last_refreshed_at"),
        "refresh_source": payload.get("refresh_source", "never"),
    }


_STATIC_ROUTE_WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync"}


def _resolve_static_route(entry: dict, StaticRoute, VRF, auto_create: bool, device, logger):
    """Find/create a StaticRoute for one adapter entry. Returns (route, created) or (None, False)."""
    vrf_name = entry.get("vrf") or ""
    prefix = entry.get("prefix") or ""
    next_hop = entry.get("next_hop") or None
    iface_nh = entry.get("interface_next_hop") or None

    if not prefix or (not next_hop and not iface_nh):
        return None, False

    vrf_obj = None
    if vrf_name and VRF is not None:
        try:
            vrf_obj = VRF.objects.get(name=vrf_name)
        except VRF.DoesNotExist:
            logger.warning("VRF %r not found in NetBox; skipping route %s", vrf_name, prefix)
            return None, False

    try:
        return StaticRoute.objects.get(vrf=vrf_obj, prefix=prefix, next_hop=next_hop), False
    except StaticRoute.DoesNotExist:
        pass

    if not auto_create:
        logger.debug("StaticRoute %s not found and auto_create=False; skipping device %s", prefix, device)
        return None, False

    try:
        route = StaticRoute(
            vrf=vrf_obj,
            prefix=prefix,
            next_hop=next_hop,
            interface_next_hop=iface_nh,
            metric=entry.get("metric") or 1,
            permanent=bool(entry.get("permanent", False)),
            tag=entry.get("tag"),
            name=entry.get("name") or "",
        )
        route.full_clean()
        route.save()
        route.devices.add(device)
        return route, True
    except Exception as exc:
        logger.warning("Could not create StaticRoute %s: %s", prefix, exc)
        return None, False


def _reconcile_static_routes(device, payload: dict) -> list:
    """Reconcile static routes from the adapter payload into NetBox routing.

    For each route reported by NSO:
    - Resolve VRF name → ipam.VRF FK (None = global routing table).
    - Find or create StaticRoute by (vrf, prefix, next_hop).
    - If this device is already in the route's M2M → update status to imported.
    - If auto_create=True and device not in M2M → add device to M2M.
    - If auto_create=False and device not in M2M → status='conflict'.

    Stale state rows (no longer reported): remove device from M2M, status='changed'.

    Returns a list of NSOStaticRouteState instances for this device.
    """
    from django.utils import timezone

    try:
        from netbox_routing.models import StaticRoute
    except ImportError:
        logger.warning("netbox_routing not installed; skipping static route reconcile")
        return []

    try:
        from ipam.models import VRF
    except ImportError:
        VRF = None

    from .models import NSODeviceManagement, NSOStaticRouteState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    cfg = apps.get_app_config("netbox_nso_plugin")
    auto_create = getattr(cfg, "_static_route_auto_create", False)
    now = timezone.now()
    seen_route_ids: set[int] = set()

    for entry in payload.get("routes") or []:
        route, created = _resolve_static_route(entry, StaticRoute, VRF, auto_create, device, logger)
        if route is None:
            continue

        vrf_name = entry.get("vrf") or ""
        prefix = entry.get("prefix") or ""
        next_hop = entry.get("next_hop") or None

        state, _ = NSOStaticRouteState.objects.get_or_create(
            management=mgmt,
            static_route=route,
            defaults={"status": "unknown"},
        )
        state.nso_vrf = vrf_name
        state.nso_prefix = prefix
        state.nso_next_hop = next_hop or ""
        state.last_sync_at = now
        seen_route_ids.add(route.pk)

        if created:
            state.status = "in_sync"  # static_route FK always set → in_sync
        elif route.devices.filter(pk=device.pk).exists():
            if state.status not in _STATIC_ROUTE_WRITE_PATH_STATUSES:
                state.status = "in_sync"
        elif auto_create:
            route.devices.add(device)
            if state.status not in _STATIC_ROUTE_WRITE_PATH_STATUSES:
                state.status = "in_sync"
        else:
            state.status = "conflict"

        state.save()

    stale_qs = NSOStaticRouteState.objects.filter(management=mgmt).exclude(static_route_id__in=seen_route_ids)
    for stale in stale_qs:
        stale.static_route.devices.remove(device)
        stale.status = "changed"
        stale.save()

    return list(NSOStaticRouteState.objects.filter(management=mgmt).select_related("static_route"))


_ISIS_WRITE_PATH_STATUSES = {"accepted", "deploying", "in_sync"}


def _reconcile_isis_interfaces(device, interfaces: list) -> list:
    """Reconcile IS-IS interface data from the adapter into NSOISISInterfaceState rows.

    For each (interface, af) entry reported by NSO:
    - Find or create an NSOISISInterfaceState keyed by (management, interface, af).
    - Update all NSO-reported fields (process_tag, circuit_type, network_type, metric, passive).
    - Set status='imported' if not already in a write-path status.

    Stale rows (no longer reported by NSO): set status='changed'.

    Returns a list of NSOISISInterfaceState instances for this device.
    """
    from dcim.models import Interface
    from django.utils import timezone

    from .models import NSODeviceManagement, NSOISISInterfaceState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    now = timezone.now()
    seen_keys: set[tuple] = set()

    for entry in interfaces or []:
        iface_name = entry.get("interface_name") or ""
        af = entry.get("af") or ""
        if not iface_name or not af:
            continue

        iface = iface_map.get(iface_name)
        if iface is None:
            logger.debug("Interface %r not found in NetBox; skipping IS-IS entry", iface_name)
            continue

        state, _ = NSOISISInterfaceState.objects.get_or_create(
            management=mgmt,
            interface=iface,
            af=af,
            defaults={"status": "unknown"},
        )
        state.process_tag = entry.get("process_tag") or ""
        state.circuit_type = entry.get("circuit_type") or ""
        state.network_type = entry.get("network_type") or ""
        state.metric = entry.get("metric")
        state.passive = bool(entry.get("passive", False))
        state.last_sync_at = now
        if state.status not in _ISIS_WRITE_PATH_STATUSES:
            state.status = "imported"  # NSOISISInterfaceState has no FK yet → always imported
        state.save()
        seen_keys.add((iface.pk, af))

    stale_qs = NSOISISInterfaceState.objects.filter(management=mgmt)
    for stale in stale_qs:
        if (stale.interface_id, stale.af) not in seen_keys:
            stale.status = "changed"
            stale.save(update_fields=["status"])

    return list(NSOISISInterfaceState.objects.filter(management=mgmt).select_related("interface"))


def _reconcile_isis_process(device, process_list: list) -> list:
    """Reconcile IS-IS process data from the adapter into NSOISISInstanceState rows.

    For each process entry reported by NSO (keyed by process_tag):
    - Find or create NSOISISInstanceState keyed by (management, process_tag).
    - Update all NSO-reported fields.
    - Set status='imported' if not already in a write-path status.
    - Try to link to an existing ISISInstance in netbox-routing.

    Stale rows (no longer reported by NSO): set status='changed'.

    Returns a list of NSOISISInstanceState instances for this device.
    """
    from django.utils import timezone

    from .models import NSODeviceManagement, NSOISISInstanceState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return []

    now = timezone.now()
    seen_tags: set[str] = set()

    # Try to import ISISInstance from netbox-routing (may not be installed)
    try:
        from netbox_routing.models import ISISInstance

        isis_instances = {inst.process_id: inst for inst in ISISInstance.objects.filter(device=device)}
    except Exception:
        isis_instances = {}

    for entry in process_list or []:
        tag = entry.get("process_tag") or ""
        if not tag:
            continue

        state, _ = NSOISISInstanceState.objects.get_or_create(
            management=mgmt,
            process_tag=tag,
            defaults={"status": "unknown"},
        )
        state.net = entry.get("net") or ""
        state.is_type = entry.get("is_type") or ""
        state.metric_style = entry.get("metric_style") or ""
        state.overload_bit = entry.get("overload_bit")
        state.area_auth_type = entry.get("area_auth_type") or ""
        state.area_auth_present = bool(entry.get("area_auth_present", False))
        state.domain_auth_type = entry.get("domain_auth_type") or ""
        state.domain_auth_present = bool(entry.get("domain_auth_present", False))
        state.last_sync_at = now

        # Try to link to netbox-routing ISISInstance by process_tag == process_id
        if tag in isis_instances:
            state.isis_instance = isis_instances[tag]

        if state.status not in _ISIS_WRITE_PATH_STATUSES:
            state.status = "in_sync" if state.isis_instance_id else "imported"
        state.save()
        seen_tags.add(tag)

    stale_qs = NSOISISInstanceState.objects.filter(management=mgmt)
    for stale in stale_qs:
        if stale.process_tag not in seen_tags:
            stale.status = "changed"
            stale.save(update_fields=["status"])

    return list(NSOISISInstanceState.objects.filter(management=mgmt).select_related("isis_instance"))


def _reconcile_ospf(device, payload: dict) -> dict:
    """Reconcile OSPF data from the adapter into NSOOSPFInstanceState and NSOOSPFInterfaceState rows.

    ``payload`` is the response body from GET /api/v1/devices/{id}/ospf.

    For each instance reported by NSO:
    - Find or create NSOOSPFInstanceState keyed by (management, process_id).
    - Update fields; set status='imported' if not in write-path status.
    - Try to link to an existing OSPFInstance in netbox-routing.

    For each interface reported by NSO:
    - Find or create NSOOSPFInterfaceState keyed by (management, interface).
    - Update fields; set status='imported' if not in write-path status.

    Stale rows (no longer reported by NSO): set status='changed'.

    Returns {"instances": [...], "interfaces": [...]}.
    """
    from dcim.models import Interface
    from django.utils import timezone

    from .models import NSODeviceManagement, NSOOSPFInstanceState, NSOOSPFInterfaceState

    _OSPF_WRITE_PATH = {"accepted", "deploying", "in_sync", "apply_failed"}

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return {"instances": [], "interfaces": []}

    now = timezone.now()

    # Try to import OSPFInstance from netbox-routing (may not be installed)
    try:
        from netbox_routing.models import OSPFInstance

        ospf_instances = {inst.process_id: inst for inst in OSPFInstance.objects.filter(device=device)}
    except Exception:
        ospf_instances = {}

    # ── Instance reconcile ──
    seen_pids: set[int] = set()
    for entry in payload.get("instances") or []:
        pid = entry.get("process_id")
        if pid is None:
            continue
        state, _ = NSOOSPFInstanceState.objects.get_or_create(
            management=mgmt,
            process_id=pid,
            defaults={"status": "unknown"},
        )
        state.router_id = entry.get("router_id") or ""
        state.vrf = entry.get("vrf") or ""
        state.areas = entry.get("areas") or []
        state.last_sync_at = now
        if pid in ospf_instances:
            state.ospf_instance = ospf_instances[pid]
        if state.status not in _OSPF_WRITE_PATH:
            state.status = "in_sync" if state.ospf_instance_id else "imported"
        state.save()
        seen_pids.add(pid)

    for stale in NSOOSPFInstanceState.objects.filter(management=mgmt):
        if stale.process_id not in seen_pids:
            stale.status = "changed"
            stale.save(update_fields=["status"])

    # ── Interface reconcile ──
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    seen_iface_pks: set[int] = set()

    for entry in payload.get("interfaces") or []:
        iface_name = entry.get("interface_name") or ""
        if not iface_name:
            continue
        iface = iface_map.get(iface_name)
        if iface is None:
            logger.debug("Interface %r not found in NetBox; skipping OSPF entry", iface_name)
            continue
        state, _ = NSOOSPFInterfaceState.objects.get_or_create(
            management=mgmt,
            interface=iface,
            defaults={"status": "unknown"},
        )
        state.process_id = entry.get("process_id")
        state.area_id = entry.get("area_id") or ""
        state.passive = bool(entry.get("passive", False))
        state.priority = entry.get("priority")
        state.cost = entry.get("cost")
        state.network_type = entry.get("network_type") or ""
        state.auth_type = entry.get("auth_type") or ""
        state.auth_present = bool(entry.get("auth_present", False))
        state.last_sync_at = now
        if state.status not in _OSPF_WRITE_PATH:
            state.status = "imported"
        state.save()
        seen_iface_pks.add(iface.pk)

    for stale in NSOOSPFInterfaceState.objects.filter(management=mgmt):
        if stale.interface_id not in seen_iface_pks:
            stale.status = "changed"
            stale.save(update_fields=["status"])

    return {
        "instances": list(NSOOSPFInstanceState.objects.filter(management=mgmt).select_related("ospf_instance")),
        "interfaces": list(NSOOSPFInterfaceState.objects.filter(management=mgmt).select_related("interface")),
    }


def _reconcile_redistribution(device, payload: dict) -> list:
    """Compatibility shim — delegates to redistribution_reconciler.reconcile_redistribution."""
    from .redistribution_reconciler import reconcile_redistribution

    return reconcile_redistribution(device, payload)


class InterfaceNSOBadge(PluginTemplateExtension):
    """Adds an NSO compliance badge to the Interface detail page."""

    models = ["dcim.interface"]

    def right_page(self):
        """Render NSO attribute status badges on the interface page."""
        from .derived_intent import is_managed_description
        from .models import NSOInterfaceState

        interface = self.context["object"]
        states = {s.attribute: s for s in NSOInterfaceState.objects.filter(interface=interface)}
        cfg = apps.get_app_config("netbox_nso_plugin")
        templates = getattr(cfg, "_derived_intent_templates", [])
        match = is_managed_description(interface.description or "", templates)
        return self.render(
            "netbox_nso_plugin/interface_nso_badge.html",
            extra_context={
                "nso_states": states,
                "status_badge": _STATUS_BADGE,
                "derived_intent_match": match,
            },
        )


template_extensions = [InterfaceNSOBadge]
