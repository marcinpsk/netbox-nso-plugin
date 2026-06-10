# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox template extensions — interface badge (device NSO tab is now a registered tab view)."""

import logging
from datetime import datetime

from django.apps import apps
from django.utils import timezone
from netbox.plugins import PluginTemplateExtension

from . import status_machine as sm

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
    "error": "danger",
}


def _adapter_setting(name: str, default: bool = False) -> bool:
    """Resolve a boolean plugin setting.

    The ``AdapterConnection`` singleton (when ``enabled``) is authoritative — this
    is the UI-editable settings surface. When there is no enabled connection row we
    fall back to the PLUGINS_CONFIG bootstrap (exposed on the app config as
    ``_<name>``), then to *default*.
    """
    try:
        from .models import AdapterConnection

        conn = AdapterConnection.objects.filter(enabled=True).first()
        if conn is not None:
            return bool(getattr(conn, name))
    except Exception:
        pass
    cfg = apps.get_app_config("netbox_nso_plugin")
    return bool(getattr(cfg, f"_{name}", default))


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
            if status == "drifted":  # legacy adapter vocab → unified machine
                status = "changed"
            last_apply_at = None
            if attr_data.get("last_apply_at"):
                try:
                    # The adapter sends UTC ISO-8601 with a trailing "Z"; keep the zone
                    # (don't strip it) so we store a tz-aware datetime, not a naive one.
                    last_apply_at = datetime.fromisoformat(attr_data["last_apply_at"].replace("Z", "+00:00"))
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

    Returns the new status string: 'imported' (materialized), 'conflict', or 'error'.
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
        return "imported"
    except ValidationError:
        return "conflict"
    except Exception as exc:  # pragma: no cover
        logger.warning("nso_ip.create_failed addr=%s: %s", address, repr(exc))
        return "error"


def _unassign_state_ip(state, VRF, IPAddress, transaction) -> None:
    """Unassign the NetBox IPAddress backing *state* (if any) from its interface."""
    vrf_obj = None
    if state.vrf and VRF is not None:
        try:
            vrf_obj = VRF.objects.get(name=state.vrf)
        except VRF.DoesNotExist:
            pass
    ip_obj = IPAddress.objects.filter(address=state.address, vrf=vrf_obj, assigned_object_id=state.interface_id).first()
    if ip_obj is not None:
        ip_obj.assigned_object = None
        with transaction.atomic():
            ip_obj.save(update_fields=["assigned_object_type", "assigned_object_id"])


def _retire_stale_ip_states(device, resolved_keys, VRF, IPAddress, now, transaction, NSOInterfaceIPState) -> None:
    """Reconcile state rows the payload no longer reports under their (iface, addr, vrf) key.

    *resolved_keys* is a set of ``(interface_id, address, vrf)`` built during the
    reconcile loop using the same logical→physical interface resolution applied
    when the state rows were created.  Keying on ``interface_id`` (not the name
    string) avoids spurious 'changed' drift on Nokia, where the payload carries a
    logical router-interface name but the state row is bound to the physical port.

    Two cases for a row whose full key is no longer reported:
    - **VRF re-key** — the same ``(interface, address)`` IS still reported, just
      under a different VRF (the VRF capture was corrected, e.g. ``"" → mgmtVrf``).
      This is the *same* IP, not a removal, so the stale row is **deleted** (and its
      orphaned NetBox IP unassigned) rather than left as a phantom 'changed'
      duplicate alongside the correctly-keyed row.
    - **Genuine removal** — the address is gone entirely → unassign + mark 'changed'.
    """
    existing_states = NSOInterfaceIPState.objects.filter(interface__device=device).select_related("interface")
    # (interface_id, address) reported under *any* VRF this run.
    reported_addr = {(iface_id, addr) for (iface_id, addr, _vrf) in resolved_keys}
    for state in existing_states:
        key = (state.interface_id, state.address, state.vrf)
        if key in resolved_keys:
            continue
        if (state.interface_id, state.address) in reported_addr:
            # VRF re-key: same IP, corrected VRF → drop the stale variant.
            _unassign_state_ip(state, VRF, IPAddress, transaction)
            state.delete()
            continue
        # Drift on genuine removal — but accepted/deploying (pending push, device
        # legitimately doesn't have it yet) is preserved by on_reconcile.
        new_status = sm.on_reconcile(state.status, present=False)
        if new_status == state.status:
            continue
        _unassign_state_ip(state, VRF, IPAddress, transaction)
        state.status = new_status
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
    auto_create = _adapter_setting("interface_ip_auto_create")

    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    payload_set, attr_map, bound_port_map = _build_payload_index(payload)
    now = timezone.now()
    # (interface_id, address, vrf) for every payload entry that resolved to a real
    # interface — the retire pass compares against this, not the logical name set.
    resolved_keys: set[tuple[int, str, str]] = set()

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
        resolved_keys.add((iface.pk, address, vrf_name))

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
                # Device reports the IP and it's correctly assigned (materialized):
                # owned settles to in_sync (device confirms intent), unowned rests imported.
                state.status = sm.on_reconcile(state.status, matches=True)
                if (
                    state.auto_assigned
                    and state.status == "in_sync"
                    and prev_status != "in_sync"
                    and existing_ip.status == "reserved"
                ):
                    _activate_auto_assigned_ip(state, existing_ip, address, iface, IPAddress, logger)
            else:
                # Address already assigned to a different interface → adoption conflict.
                state.status = sm.on_reconcile(state.status, matches=False, conflict=True)
        elif auto_create and state.status != "conflict":
            result = _create_and_link_ip(address, vrf_obj, iface, Prefix, logger, transaction, ValidationError)
            if not sm.is_owned(state.status):
                state.status = result
        elif not sm.is_owned(state.status) and state.status != "conflict":
            state.status = "imported"

        state.save()

    _retire_stale_ip_states(device, resolved_keys, VRF, IPAddress, now, transaction, NSOInterfaceIPState)

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
        state.status = sm.on_reconcile(state.status, matches=None)  # mirror overlay
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
        state.status = sm.on_reconcile(state.status, matches=None)  # mirror overlay
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
        state.status = sm.on_reconcile(state.status, matches=None)  # mirror overlay
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
        system_info_state.status = sm.on_reconcile(system_info_state.status, matches=None)
        system_info_state.save()

    return {
        "communities": list(NSOSnmpCommunityState.objects.filter(management=mgmt)),
        "v3_users": list(NSOSnmpV3UserState.objects.filter(management=mgmt)),
        "hosts": list(NSOSnmpHostState.objects.filter(management=mgmt)),
        "system_info": system_info_state,
        "last_refreshed_at": payload.get("last_refreshed_at"),
        "refresh_source": payload.get("refresh_source", "never"),
    }


def _reconcile_logging_config(device, payload: dict) -> dict:
    """Full-replace import of logging/syslog hosts into NSOLoggingHostState.

    Rows whose address matches the payload are updated; rows absent from the
    payload are deleted; new rows are created with status='imported'. Read-only
    overlay (no write path yet) so status stays imported. Returns {"hosts": [...]}.
    """
    from django.utils import timezone

    from .models import NSODeviceManagement, NSOLoggingHostState

    try:
        mgmt = device.nso_management
    except NSODeviceManagement.DoesNotExist:
        return {"hosts": [], "last_refreshed_at": None, "refresh_source": "never"}

    now = timezone.now()
    payload_hosts = {h.get("address"): h for h in (payload.get("hosts") or []) if h.get("address")}

    # Delete rows no longer reported.
    NSOLoggingHostState.objects.filter(management=mgmt).exclude(address__in=payload_hosts.keys()).delete()

    for addr, h in payload_hosts.items():
        state, _ = NSOLoggingHostState.objects.get_or_create(
            management=mgmt, address=addr, defaults={"status": "unknown"}
        )
        state.port = h.get("port")
        state.severity = h.get("severity") or ""
        state.facility = h.get("facility") or ""
        state.transport = h.get("transport") or ""
        state.vrf = h.get("vrf") or ""
        state.source = h.get("source") or ""
        state.status = sm.on_reconcile(state.status, matches=None)  # mirror overlay
        state.last_sync_at = now
        state.save()

    return {
        "hosts": list(NSOLoggingHostState.objects.filter(management=mgmt)),
        "last_refreshed_at": payload.get("last_refreshed_at"),
        "refresh_source": payload.get("refresh_source", "never"),
    }


def _static_route_metric(entry: dict) -> int:
    """Clamp the NSO metric to StaticRoute's 0..255 PositiveSmallInt constraint.

    Junos route metric/preference can exceed 255; an out-of-range value would fail
    full_clean() and drop the route, so fall back to the model default (1).
    """
    m = entry.get("metric")
    return m if isinstance(m, int) and 0 <= m <= 255 else 1


def _resolve_static_route(entry, StaticRoute, VRF, auto_create, vrf_auto_create, device, logger):
    """Find/create a StaticRoute for one adapter entry. Returns (route, created) or (None, False).

    ``interface_next_hop`` carries interface-, discard/reject- and next-table-style
    next hops (routes with no IP next hop); a route needs at least one of next_hop
    or interface_next_hop or it is skipped.
    """
    vrf_name = entry.get("vrf") or ""
    prefix = entry.get("prefix") or ""
    next_hop = entry.get("next_hop") or None
    iface_nh = entry.get("interface_next_hop") or None

    if not prefix or (not next_hop and not iface_nh):
        return None, False

    vrf_obj = None
    if vrf_name and VRF is not None:
        vrf_obj = VRF.objects.filter(name=vrf_name).first()
        if vrf_obj is None:
            if vrf_auto_create:
                vrf_obj = VRF.objects.create(name=vrf_name)
                logger.info("Auto-created VRF %r for static route %s", vrf_name, prefix)
            else:
                logger.warning("VRF %r not found in NetBox; skipping route %s", vrf_name, prefix)
                return None, False

    # Idempotent lookup. IP next-hop routes key on next_hop; interface/pseudo
    # next-hop routes (discard, reject, next-table) have a null next_hop, so key
    # on interface_next_hop too to avoid duplicating them.
    lookup = {"vrf": vrf_obj, "prefix": prefix, "next_hop": next_hop}
    if next_hop is None:
        lookup["interface_next_hop"] = iface_nh
    existing = StaticRoute.objects.filter(**lookup).first()
    if existing is not None:
        return existing, False

    if not auto_create:
        logger.debug("StaticRoute %s not found and auto_create=False; skipping device %s", prefix, device)
        return None, False

    try:
        route = StaticRoute(
            vrf=vrf_obj,
            prefix=prefix,
            next_hop=next_hop,
            interface_next_hop=iface_nh,
            metric=_static_route_metric(entry),
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

    auto_create = _adapter_setting("static_route_auto_create")
    vrf_auto_create = _adapter_setting("vrf_auto_create")
    now = timezone.now()
    seen_route_ids: set[int] = set()

    for entry in payload.get("routes") or []:
        route, created = _resolve_static_route(entry, StaticRoute, VRF, auto_create, vrf_auto_create, device, logger)
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

        # FK overlay: materialized = the StaticRoute is linked to this device.
        on_device = created or route.devices.filter(pk=device.pk).exists()
        if not on_device and auto_create:
            route.devices.add(device)
            on_device = True
        state.status = sm.on_reconcile(state.status, matches=on_device, conflict=not on_device, settles_owned=False)
        state.save()

    stale_qs = NSOStaticRouteState.objects.filter(management=mgmt).exclude(static_route_id__in=seen_route_ids)
    for stale in stale_qs:
        stale.static_route.devices.remove(device)
        new_status = sm.on_reconcile(stale.status, present=False)
        if new_status != stale.status:
            stale.status = new_status
            stale.save()

    return list(NSOStaticRouteState.objects.filter(management=mgmt).select_related("static_route"))


def _reconcile_isis_settings(obj, settings: dict | None) -> None:
    """Reconcile a netbox_routing ISISSetting EAV bag for *obj* (instance/interface).

    *settings* is the {key: value} dict the adapter mirrored from the device.
    Creates/updates rows for present keys and deletes rows no longer reported,
    constrained to the keys netbox_routing recognises (ISISSettingChoices) so an
    unknown key never raises. No-op when netbox-routing lacks ISISSetting (older
    fork) or *obj* is None.
    """
    if obj is None:
        return
    try:
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.choices import ISISSettingChoices
        from netbox_routing.models import ISISSetting
    except Exception:
        return

    valid = {k for k, _ in ISISSettingChoices.CHOICES}
    wanted = {k: str(v) for k, v in (settings or {}).items() if k in valid and v is not None}

    ct = ContentType.objects.get_for_model(type(obj))
    existing = {s.key: s for s in ISISSetting.objects.filter(assigned_object_type=ct, assigned_object_id=obj.pk)}
    for key, value in wanted.items():
        row = existing.get(key)
        if row is None:
            ISISSetting.objects.create(assigned_object=obj, key=key, value=value)
        elif row.value != value:
            row.value = value
            row.save(update_fields=["value"])
    for key, row in existing.items():
        if key not in wanted:
            row.delete()


def _reconcile_child_levels(model, parent_field, parent, cols, levels) -> None:
    """Full-replace per-level child rows (ISISLevel/ISISInterfaceLevel) for *parent*.

    *levels* is the adapter's list of {level, <snake_case cols>} dicts. Each col
    is set only when present (and the model carries it); levels NSO no longer
    reports are deleted. No-op when *parent* is None.
    """
    if parent is None:
        return
    incoming = {}
    for lvl in levels or []:
        try:
            incoming[int(lvl["level"])] = lvl
        except (KeyError, TypeError, ValueError):
            continue
    existing = {row.level: row for row in model.objects.filter(**{parent_field: parent})}
    for lvl, data in incoming.items():
        row = existing.get(lvl) or model(**{parent_field: parent, "level": lvl})
        changed = row.pk is None
        for col in cols:
            val = data.get(col)
            if val is not None and getattr(row, col, None) != val:
                setattr(row, col, val)
                changed = True
        if changed:
            row.save()
    for lvl, row in existing.items():
        if lvl not in incoming:
            row.delete()


def _reconcile_isis_segment_routing(inst, sr: dict | None) -> None:
    """Upsert the netbox_routing ISISSegmentRouting (1:1) for *inst* from *sr*.

    Deletes the SR row when NSO reports no segment-routing. No-op when the fork
    lacks ISISSegmentRouting or *inst* is None.
    """
    if inst is None:
        return
    try:
        from netbox_routing.models import ISISSegmentRouting
    except Exception:
        return
    if not sr:
        ISISSegmentRouting.objects.filter(instance=inst).delete()
        return
    cols = (
        "enabled",
        "prefix_sid_range",
        "srgb_start",
        "srgb_range",
        "node_sid_index",
        "node_sid_label",
        "node_sid_v6_index",
        "node_sid_v6_label",
        "maximum_sid_depth",
        "tunnel_table_pref",
    )
    row, _ = ISISSegmentRouting.objects.get_or_create(instance=inst)
    fields = []
    for col in cols:
        val = sr.get(col)
        if val is not None and getattr(row, col, None) != val:
            setattr(row, col, val)
            fields.append(col)
    if fields:
        row.save(update_fields=fields)


def _reconcile_isis_flex_algos(inst, flex_algos) -> None:
    """Full-replace ISISFlexAlgo rows for *inst* from the adapter's flex-algo list.

    Each entry is {algo_id, metric_type, priority, admin_group_*}; algos NSO no
    longer reports are deleted. No-op when the fork lacks ISISFlexAlgo or *inst*
    is None.
    """
    if inst is None:
        return
    try:
        from netbox_routing.models import ISISFlexAlgo
    except Exception:
        return
    cols = (
        "metric_type",
        "priority",
        "admin_group_exclude",
        "admin_group_include_any",
        "admin_group_include_all",
    )
    incoming = {}
    for fa in flex_algos or []:
        try:
            incoming[int(fa["algo_id"])] = fa
        except (KeyError, TypeError, ValueError):
            continue
    existing = {row.algo_id: row for row in ISISFlexAlgo.objects.filter(instance=inst)}
    for aid, data in incoming.items():
        row = existing.get(aid) or ISISFlexAlgo(instance=inst, algo_id=aid)
        changed = row.pk is None
        for col in cols:
            val = data.get(col)
            if val is not None and getattr(row, col, None) != val:
                setattr(row, col, val)
                changed = True
        if changed:
            row.save()
    for aid, row in existing.items():
        if aid not in incoming:
            row.delete()


_ISIS_LEVEL_COLS = ("default_metric", "wide_metrics_only", "preference", "labeled_preference", "disabled", "auth_type")
_ISIS_IFACE_LEVEL_COLS = ("metric", "hello_interval", "hello_multiplier", "priority", "passive")


def _link_routing_isis_interface(device, iface, af, state, instances: dict, bfd_enabled=None, entry=None):
    """Create/update the netbox_routing.ISISInterface for this row; return it (or None).

    Returns None when netbox-routing isn't installed. ISISInterface.instance is required,
    so the ISISInstance is ensured here too (this reconcile runs before the process one),
    cached in *instances* keyed by process_tag. Mirrors how BGP/static reconcile into
    netbox_routing; owner is left null (ownership lives in NSO*State.accepted_at).
    """
    try:
        from netbox_routing.models import ISISInstance, ISISInterface
    except Exception:
        return None

    entry = entry or {}
    tag = state.process_tag
    if tag not in instances:
        instances[tag], _ = ISISInstance.objects.get_or_create(device=device, process_tag=tag)
    inst = instances[tag]

    ri, _ = ISISInterface.objects.get_or_create(interface=iface, address_family=af, defaults={"instance": inst})
    fields: list[str] = []
    if ri.instance_id != inst.id:
        ri.instance = inst
        fields.append("instance")
    routing_fields = [
        ("circuit_type", state.circuit_type or None),
        ("network_type", state.network_type or None),
        ("metric", state.metric),
        ("passive", state.passive),
    ]
    # hello_auth_type only exists on ISISInterface once the netbox-routing isis
    # branch lands — guard so the reconcile is a no-op for it until then.
    # ISISInterface.hello_auth_type is a non-null CharField (default "") — write
    # "" (not None) when there is no hello auth.
    if hasattr(ri, "hello_auth_type"):
        routing_fields.append(("hello_auth_type", state.hello_auth_type or ""))
    # bfd_enabled (nullable bool) — guard until the netbox-routing field is present.
    if hasattr(ri, "bfd_enabled"):
        routing_fields.append(("bfd_enabled", bfd_enabled))
    # M33 P1 per-interface scalars — guard each until the fork carries the column.
    if hasattr(ri, "csnp_interval"):
        routing_fields.append(("csnp_interval", entry.get("csnp_interval")))
    if hasattr(ri, "retransmit_interval"):
        routing_fields.append(("retransmit_interval", entry.get("retransmit_interval")))
    if hasattr(ri, "lsp_interval"):
        routing_fields.append(("lsp_interval", entry.get("lsp_interval")))
    if hasattr(ri, "mesh_group"):
        routing_fields.append(("mesh_group", entry.get("mesh_group") or ""))
    for attr, val in routing_fields:
        if getattr(ri, attr) != val:
            setattr(ri, attr, val)
            fields.append(attr)
    if fields:
        ri.save(update_fields=fields)
    _reconcile_isis_settings(ri, entry.get("settings"))
    # M33 P2 per-level interface child rows.
    try:
        from netbox_routing.models import ISISInterfaceLevel

        _reconcile_child_levels(ISISInterfaceLevel, "interface", ri, _ISIS_IFACE_LEVEL_COLS, entry.get("levels"))
    except Exception:
        pass
    return ri


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
    dropped: list[str] = []
    instances: dict[str, object] = {}  # process_tag -> netbox_routing ISISInstance (cache)

    for entry in interfaces or []:
        iface_name = entry.get("interface_name") or ""
        af = entry.get("af") or ""
        if not iface_name or not af:
            continue

        iface = iface_map.get(iface_name)
        if iface is None:
            # Nokia SR OS IS-IS interfaces are logical router-interfaces (e.g.
            # "LAG99:10") whose name does not match a NetBox dcim.Interface (named
            # by port-id). bound_port carries the physical/LAG binding ("lag-99:10")
            # the adapter derived from the device — correlate through it. Mirrors
            # the interface-IP reconcile's bound_port fallback.
            bound_port = entry.get("bound_port")
            if bound_port:
                iface = iface_map.get(bound_port)
        if iface is None:
            # The interface the adapter reports does not exist in NetBox — most
            # often a logical unit (e.g. Junos ae98.100) that is not yet modelled
            # as a dcim.Interface. Record it so the drop is visible rather than
            # silent (see docs/junos-subinterface-modeling-plan.md).
            dropped.append(iface_name)
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
        state.hello_auth_type = entry.get("hello_auth_type") or ""
        state.hello_auth_present = bool(entry.get("hello_auth_present", False))
        state.last_sync_at = now

        state.isis_interface = _link_routing_isis_interface(
            device, iface, af, state, instances, bfd_enabled=entry.get("bfd_enabled"), entry=entry
        )

        # Mirror overlay: linking the netbox-routing ISIS interface is best-effort
        # (an unmodelled link is benign), so an unowned row rests at imported.
        state.status = sm.on_reconcile(state.status, matches=None)
        state.save()
        seen_keys.add((iface.pk, af))

    stale_qs = NSOISISInterfaceState.objects.filter(management=mgmt)
    for stale in stale_qs:
        if (stale.interface_id, stale.af) not in seen_keys:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])

    if dropped:
        logger.warning(
            "IS-IS reconcile for %s: %d interface(s) not found in NetBox, dropped: %s",
            device,
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )

    return list(NSOISISInterfaceState.objects.filter(management=mgmt).select_related("interface", "isis_interface"))


# netbox_routing ISISInstance scalar columns synced from NSO (M33 P1). Each is
# guarded by hasattr so the reconcile no-ops on a fork without the column.
_ISIS_INSTANCE_SCALAR_ATTRS = (
    "spf_initial_wait",
    "spf_max_wait",
    "lsp_initial_wait",
    "lsp_max_wait",
    "lsp_lifetime",
    "lsp_refresh_interval",
    "lsp_mtu",
    "overload_on_startup",
    "overload_timeout",
    "te_enabled",
    "sr_enabled",
    "sr_node_msd",
    "distance",
    "maximum_paths",
    "reference_bandwidth",
)


def _sync_routing_isis_instance(device, tag, state, entry):
    """Create/update the netbox_routing.ISISInstance for *tag*; return it (or None).

    Returns None when netbox-routing isn't installed. Informational/auth string
    fields sync from *state* when NSO reported a non-empty value; overload_bit is
    tri-state (None left alone); the M33 P1 scalar columns sync from *entry* when
    present (guarded per-column). The ISISSetting EAV bag is full-replaced.
    """
    try:
        from netbox_routing.models import ISISInstance
    except Exception:
        return None

    inst, _ = ISISInstance.objects.get_or_create(device=device, process_tag=tag)
    inst_fields: list[str] = []
    for attr, val in (
        ("net", state.net),
        ("is_type", state.is_type),
        ("metric_style", state.metric_style),
        ("area_auth_type", state.area_auth_type),
        ("area_auth_key", state.area_auth_key),
        ("domain_auth_type", state.domain_auth_type),
        ("domain_auth_key", state.domain_auth_key),
    ):
        if val and getattr(inst, attr) != val:
            setattr(inst, attr, val)
            inst_fields.append(attr)
    # overload_bit is a tri-state boolean — sync True/False, but leave None
    # (NSO didn't report it) untouched so we never clobber a manually-set value.
    if state.overload_bit is not None and inst.overload_bit != state.overload_bit:
        inst.overload_bit = state.overload_bit
        inst_fields.append("overload_bit")
    # M33 P1 cross-vendor scalars — only sync columns the fork carries and only
    # when NSO actually reported a value (never clobber an operator value with None).
    for attr in _ISIS_INSTANCE_SCALAR_ATTRS:
        if not hasattr(inst, attr):
            continue
        val = entry.get(attr)
        if val is not None and getattr(inst, attr) != val:
            setattr(inst, attr, val)
            inst_fields.append(attr)
    if inst_fields:
        inst.save(update_fields=inst_fields)
    _reconcile_isis_settings(inst, entry.get("settings"))
    # M33 P2 child tables — guarded import inside the helpers.
    try:
        from netbox_routing.models import ISISLevel

        _reconcile_child_levels(ISISLevel, "instance", inst, _ISIS_LEVEL_COLS, entry.get("levels"))
    except Exception:
        pass
    _reconcile_isis_segment_routing(inst, entry.get("segment_routing"))
    _reconcile_isis_flex_algos(inst, entry.get("flex_algos"))
    return inst


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

    for entry in process_list or []:
        # Junos' default IS-IS instance has an empty process tag — "" is a valid
        # key (NSOISISInstanceState.process_tag defaults to ""). Only skip an
        # entry that genuinely omits the field.
        tag = entry.get("process_tag")
        if tag is None:
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
        state.area_auth_key = entry.get("area_auth_key") or ""
        state.domain_auth_type = entry.get("domain_auth_type") or ""
        state.domain_auth_present = bool(entry.get("domain_auth_present", False))
        state.domain_auth_key = entry.get("domain_auth_key") or ""
        state.last_sync_at = now

        # netbox-routing ISISInstance (optional dep): create/update the real
        # instance keyed by (device, process_tag) and link it from the state row.
        state.isis_instance = _sync_routing_isis_instance(device, tag, state, entry)

        # Mirror overlay: linking the netbox-routing ISIS instance is best-effort.
        state.status = sm.on_reconcile(state.status, matches=None)
        state.save()
        seen_tags.add(tag)

    stale_qs = NSOISISInstanceState.objects.filter(management=mgmt)
    for stale in stale_qs:
        if stale.process_tag not in seen_tags:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])

    return list(NSOISISInstanceState.objects.filter(management=mgmt).select_related("isis_instance"))


def _import_ospf_models():
    """Return (OSPFInstance, OSPFArea, OSPFInterface) or (None, None, None).

    netbox-routing is an optional dependency; treat its absence as "fill disabled".
    """
    try:
        from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

        return OSPFInstance, OSPFArea, OSPFInterface
    except Exception:
        return None, None, None


def _resolve_ospf_vrf(vrf_name: str):
    """Resolve an ipam.VRF by name for an OSPF instance.

    Returns None for the global table or an unknown VRF, unless the
    ``vrf_auto_create`` setting is on — then the VRF is created (mirrors the
    static-route fill so all surfaces share one toggle).
    """
    if not vrf_name:
        return None
    try:
        from ipam.models import VRF

        vrf_obj = VRF.objects.filter(name=vrf_name).first()
        if vrf_obj is None and _adapter_setting("vrf_auto_create"):
            vrf_obj = VRF.objects.create(name=vrf_name)
        return vrf_obj
    except Exception:
        return None


def _get_or_create_ospf_instance(device, pid, entry, OSPFInstance):
    """Create/refresh the netbox-routing OSPFInstance for one process.

    Keyed on (device, process_id). ``router_id`` is required by the model
    (IPAddressField) so an instance NSO reports without one is skipped — the
    overlay still records it. ``name`` is only set on create so an operator
    rename survives later syncs; router_id/vrf are kept faithful to the device.
    """
    if OSPFInstance is None:
        return None
    router_id = entry.get("router_id")
    if not router_id:
        return None
    vrf_obj = _resolve_ospf_vrf(entry.get("vrf") or "")
    obj, created = OSPFInstance.objects.get_or_create(
        device=device,
        process_id=pid,
        defaults={"name": str(pid), "router_id": router_id, "vrf": vrf_obj},
    )
    if not created:
        changed = False
        if str(obj.router_id) != str(router_id):
            obj.router_id = router_id
            changed = True
        if obj.vrf_id != (vrf_obj.id if vrf_obj else None):
            obj.vrf = vrf_obj
            changed = True
        if changed:
            obj.save()
    return obj


_OSPF_AUTH_MAP = {"message-digest": "message-digest", "null": "null"}
_OSPF_NETWORK_TYPES = {"broadcast", "non-broadcast", "point-to-point", "point-to-multipoint"}


def _fill_ospf_interface(entry, iface, inst_by_pid, OSPFArea, OSPFInterface):
    """Create/refresh the netbox-routing OSPFInterface + its OSPFArea.

    OSPFArea is a global object keyed by area_id. OSPFInterface is OneToOne on
    the dcim.Interface. Auth keys are never imported — only the auth *type* (and
    only the values netbox-routing models; plaintext has no equivalent).
    """
    if OSPFInterface is None or OSPFArea is None:
        return
    pid = entry.get("process_id")
    inst = inst_by_pid.get(pid)
    if inst is None:
        return
    area_id = entry.get("area_id") or "0.0.0.0"
    area, _ = OSPFArea.objects.get_or_create(area_id=area_id, defaults={"area_type": "standard"})

    cost = entry.get("cost")
    cost = cost if isinstance(cost, int) and 1 <= cost <= 65535 else None
    nt = entry.get("network_type")
    nt = nt if nt in _OSPF_NETWORK_TYPES else None
    auth = _OSPF_AUTH_MAP.get(entry.get("auth_type") or "")
    fields = {
        "instance": inst,
        "area": area,
        "passive": bool(entry.get("passive", False)),
        "priority": entry.get("priority"),
        "cost": cost,
        "network_type": nt,
        "authentication": auth,
    }
    obj, created = OSPFInterface.objects.get_or_create(interface=iface, defaults=fields)
    if not created:
        for key, val in fields.items():
            setattr(obj, key, val)
        obj.save()


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
    from django.utils import timezone

    from .models import NSODeviceManagement, NSOOSPFInstanceState, NSOOSPFInterfaceState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return {"instances": [], "interfaces": []}

    now = timezone.now()

    OSPFInstance, _OSPFArea, _OSPFInterface = _import_ospf_models()

    # ── Instance reconcile ──
    seen_pids: set[str] = set()
    for entry in payload.get("instances") or []:
        pid = entry.get("process_id")
        if pid is None:
            continue
        # process_id is a string column (IOS-XR/Junos allow named processes);
        # coerce so int payloads and the string DB value compare consistently.
        pid = str(pid)
        state, _ = NSOOSPFInstanceState.objects.get_or_create(
            management=mgmt,
            process_id=pid,
            defaults={"status": "unknown"},
        )
        state.router_id = entry.get("router_id") or ""
        state.vrf = entry.get("vrf") or ""
        state.areas = entry.get("areas") or []
        state.last_sync_at = now
        ospf_inst = _get_or_create_ospf_instance(device, pid, entry, OSPFInstance)
        if ospf_inst is not None:
            state.ospf_instance = ospf_inst
        # Mirror overlay: linking the netbox-routing OSPF instance is best-effort.
        state.status = sm.on_reconcile(state.status, matches=None)
        state.save()
        seen_pids.add(pid)

    for stale in NSOOSPFInstanceState.objects.filter(management=mgmt):
        if stale.process_id not in seen_pids:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])

    _reconcile_ospf_interfaces(device, mgmt, payload, now)

    return {
        "instances": list(NSOOSPFInstanceState.objects.filter(management=mgmt).select_related("ospf_instance")),
        "interfaces": list(NSOOSPFInterfaceState.objects.filter(management=mgmt).select_related("interface")),
    }


def _reconcile_ospf_interfaces(device, mgmt, payload, now) -> None:
    """Reconcile the OSPF interface section of *payload* into NSOOSPFInterfaceState rows.

    Split out of _reconcile_ospf to keep that function under the complexity gate.
    Interfaces the adapter reports but NetBox lacks (usually unmodelled logical
    units — see docs/junos-subinterface-modeling-plan.md) are counted and logged
    rather than silently dropped.
    """
    from dcim.models import Interface

    from .models import NSOOSPFInterfaceState

    OSPFInstance, OSPFArea, OSPFInterface = _import_ospf_models()
    inst_by_pid = {i.process_id: i for i in OSPFInstance.objects.filter(device=device)} if OSPFInstance else {}

    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}
    seen_iface_pks: set[int] = set()
    dropped: list[str] = []

    for entry in payload.get("interfaces") or []:
        iface_name = entry.get("interface_name") or ""
        if not iface_name:
            continue
        iface = iface_map.get(iface_name)
        if iface is None:
            dropped.append(iface_name)
            continue
        state, _ = NSOOSPFInterfaceState.objects.get_or_create(
            management=mgmt,
            interface=iface,
            defaults={"status": "unknown"},
        )
        # Normalise process_id to a string (named processes on IOS-XR/Junos) so the
        # value stored, the inst_by_pid lookup, and the DB column all agree.
        pid_raw = entry.get("process_id")
        entry["process_id"] = str(pid_raw) if pid_raw is not None else None
        state.process_id = entry["process_id"]
        state.area_id = entry.get("area_id") or ""
        state.passive = bool(entry.get("passive", False))
        state.priority = entry.get("priority")
        state.cost = entry.get("cost")
        state.network_type = entry.get("network_type") or ""
        state.auth_type = entry.get("auth_type") or ""
        state.auth_present = bool(entry.get("auth_present", False))
        state.last_sync_at = now
        state.status = sm.on_reconcile(state.status, matches=None)  # mirror overlay
        state.save()
        _fill_ospf_interface(entry, iface, inst_by_pid, OSPFArea, OSPFInterface)
        seen_iface_pks.add(iface.pk)

    for stale in NSOOSPFInterfaceState.objects.filter(management=mgmt):
        if stale.interface_id not in seen_iface_pks:
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save(update_fields=["status"])

    if dropped:
        logger.warning(
            "OSPF reconcile for %s: %d interface(s) not found in NetBox, dropped: %s",
            device,
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )


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
