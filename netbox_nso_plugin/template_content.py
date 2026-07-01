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


def _resolve_interface_attr_status(state, *, created, attr_name, iface, nso_value, adapter_status, derived_templates):
    """Resolve the next status for one interface-attr row (ownership-aware).

    Returns ``(new_status, promote)``:

    - An EXISTING operator-owned row settles by device value and is NEVER clobbered to the
      adapter's unowned status (the owned-guard every other reconciler has).
    - A derived (topology-computed) description is NetBox intent BY DEFINITION, so it is
      owned even when the adapter reports ``imported`` (``promote=True`` → caller stamps
      ``accepted_at``) — matches device → ``in_sync``, differs → ``accepted`` (pending).
    - Otherwise the adapter's status is mirrored verbatim (unowned mirror / fresh import).
    """
    from . import status_machine as sm
    from .derived_intent import is_managed_description
    from .summary import _COMPARABLE_IFACE_ATTRS, _netbox_value_for, matches_device_value

    if not created and sm.is_owned(state.status):
        matches = (
            matches_device_value(attr_name, _netbox_value_for(attr_name, iface), nso_value)
            if attr_name in _COMPARABLE_IFACE_ATTRS
            else None
        )
        return sm.on_reconcile(state.status, matches=matches), False
    if attr_name == "description" and is_managed_description(iface.description or "", derived_templates):
        matches = matches_device_value("description", iface.description, nso_value)
        return ("in_sync" if matches else "accepted"), True
    return adapter_status, False


def _upsert_interface_states(device, interfaces: list) -> dict:
    """Sync NSOInterfaceState rows from adapter interface data.

    Returns a dict keyed by (interface_name, attribute) → NSOInterfaceState instance.
    Only updates fields that come from the adapter; never overwrites accepted_at.

    Owned rows (status in OWNED_STATES, set by the operator's accept/edit) are NOT
    clobbered back to the adapter's unowned status — the same owned-guard every other
    overlay reconciler uses (see ``interface_mtu_reconciler``). Without it, an adapter
    sync that reports ``imported`` for an attribute the operator owns would silently
    drop ownership, and the now status-based intent push would stop re-applying it. The
    owned row instead settles by device-vs-NetBox value (``deploying``/``accepted`` →
    ``in_sync`` once the device reflects the operator's value). A freshly imported row
    (created this sync) or an unowned row tracks the adapter status verbatim.
    """
    from dcim.models import Interface

    from .models import NSOInterfaceState

    # Derived-intent templates (e.g. description-from-cable). A description whose NetBox
    # value matches one is NetBox intent BY DEFINITION (the plugin computes it from
    # topology), so it must be owned even if the adapter reads it as imported — see
    # _resolve_interface_attr_status.
    derived_templates = getattr(apps.get_app_config("netbox_nso_plugin"), "_derived_intent_templates", [])

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

            state, created = NSOInterfaceState.objects.get_or_create(
                interface=iface,
                attribute=attr_name,
                defaults={"status": status, "nso_value": nso_value},
            )
            # Resolve the next status BEFORE overwriting it (ownership-aware — see helper).
            update_fields = []
            new_status, promote = _resolve_interface_attr_status(
                state,
                created=created,
                attr_name=attr_name,
                iface=iface,
                nso_value=nso_value,
                adapter_status=status,
                derived_templates=derived_templates,
            )
            if state.status != new_status:
                state.status = new_status
                update_fields.append("status")
            if promote and state.accepted_at is None:
                state.accepted_at = timezone.now()
                update_fields.append("accepted_at")
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
        # Brownfield adoption (reconcile-created route) — not operator intent; suppress
        # so the greenfield static-route signal doesn't auto-Accept it.
        from .signals import suppress_intent_push

        with suppress_intent_push():
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

    from .signals import suppress_intent_push

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
            # Brownfield adoption: this M2M change is not operator intent — suppress so the
            # greenfield static-route signal doesn't mistake it for an Accept.
            with suppress_intent_push():
                route.devices.add(device)
            on_device = True
        state.status = sm.on_reconcile(state.status, matches=on_device, conflict=not on_device, settles_owned=False)
        state.save()

    stale_qs = NSOStaticRouteState.objects.filter(management=mgmt).exclude(static_route_id__in=seen_route_ids)
    for stale in stale_qs:
        if sm.is_owned(stale.status):
            # Operator-owned (greenfield) route the device stopped reporting → genuine
            # removal drift. KEEP the device↔route association + overlay so the operator
            # can resolve it; removing it from the M2M would silently discard their intent.
            new_status = sm.on_reconcile(stale.status, present=False)
            if new_status != stale.status:
                stale.status = new_status
                stale.save()
        else:
            # Brownfield mirror: the route is gone from the device → un-materialise it.
            # Drop the device↔route association AND the overlay. (The old code removed the
            # M2M but then re-saved the overlay as 'changed', resurrecting a dangling
            # overlay orphaned from the M2M — a route shown as drift on a device its
            # devices-list no longer includes.)
            with suppress_intent_push():
                stale.static_route.devices.remove(device)
            stale.delete()

    return list(NSOStaticRouteState.objects.filter(management=mgmt).select_related("static_route"))


def _reconcile_isis_settings(obj, settings: dict | None, *, write: bool = True) -> bool:
    """Reconcile a netbox_routing ISISSetting EAV bag for *obj* (instance/interface).

    *settings* is the {key: value} dict the adapter mirrored from the device.
    Clobber-safe: when ``write`` (the overlay row is being seeded on first import) it
    creates/updates/deletes to mirror the device; when ``write`` is False it touches
    nothing (operator edits survive). Returns ``matches`` = the bag already equals the
    device. No-op (matches=True) when netbox-routing lacks ISISSetting or *obj* is None.
    """
    if obj is None:
        return True
    try:
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.choices import ISISSettingChoices
        from netbox_routing.models import ISISSetting
    except Exception:
        return True

    valid = {k for k, _ in ISISSettingChoices.CHOICES}
    wanted = {k: str(v) for k, v in (settings or {}).items() if k in valid and v is not None}

    ct = ContentType.objects.get_for_model(type(obj))
    existing = {s.key: s for s in ISISSetting.objects.filter(assigned_object_type=ct, assigned_object_id=obj.pk)}
    matches = True
    for key, value in wanted.items():
        row = existing.get(key)
        if row is None:
            matches = False
            if write:
                ISISSetting.objects.create(assigned_object=obj, key=key, value=value)
        elif row.value != value:
            matches = False
            if write:
                row.value = value
                row.save(update_fields=["value"])
    for key, row in existing.items():
        if key not in wanted:
            matches = False
            if write:
                row.delete()
    return matches


def _reconcile_child_levels(model, parent_field, parent, cols, levels, *, write: bool = True) -> bool:
    """Per-level child rows (ISISLevel/ISISInterfaceLevel) for *parent*.

    Clobber-safe: when ``write`` (seeding on first import) it mirrors the device
    (create/update present levels, delete dropped ones); when ``write`` is False it
    touches nothing. Returns ``matches`` = the levels already equal the device.
    No-op (matches=True) when *parent* is None.
    """
    if parent is None:
        return True
    incoming = {}
    for lvl in levels or []:
        try:
            incoming[int(lvl["level"])] = lvl
        except (KeyError, TypeError, ValueError):
            continue
    existing = {row.level: row for row in model.objects.filter(**{parent_field: parent})}
    matches = True
    for lvl, data in incoming.items():
        row = existing.get(lvl) or model(**{parent_field: parent, "level": lvl})
        changed = row.pk is None
        for col in cols:
            val = data.get(col)
            if val is not None and getattr(row, col, None) != val:
                setattr(row, col, val)
                changed = True
        if changed:
            matches = False
            if write:
                row.save()
    for lvl, row in existing.items():
        if lvl not in incoming:
            matches = False
            if write:
                row.delete()
    return matches


def _reconcile_isis_segment_routing(inst, sr: dict | None, *, write: bool = True) -> bool:
    """Upsert the netbox_routing ISISSegmentRouting (1:1) for *inst* from *sr*.

    Clobber-safe: mirrors the device only when ``write`` (seeding); otherwise touches
    nothing. Returns ``matches`` = the SR row already equals the device. No-op
    (matches=True) when the fork lacks ISISSegmentRouting or *inst* is None.
    """
    if inst is None:
        return True
    try:
        from netbox_routing.models import ISISSegmentRouting
    except Exception:
        return True
    if not sr:
        exists = ISISSegmentRouting.objects.filter(instance=inst).exists()
        if exists and write:
            ISISSegmentRouting.objects.filter(instance=inst).delete()
        return not exists
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
    row, created = ISISSegmentRouting.objects.get_or_create(instance=inst) if write else (None, False)
    if row is None:
        row = ISISSegmentRouting.objects.filter(instance=inst).first()
    matches = not created and row is not None
    fields = []
    for col in cols:
        val = sr.get(col)
        if val is not None and (row is None or getattr(row, col, None) != val):
            matches = False
            if write and row is not None:
                setattr(row, col, val)
                fields.append(col)
    if fields:
        row.save(update_fields=fields)
    return matches


def _reconcile_isis_flex_algos(inst, flex_algos, *, write: bool = True) -> bool:
    """ISISFlexAlgo rows for *inst* from the adapter's flex-algo list.

    Clobber-safe: mirrors the device only when ``write`` (seeding); otherwise touches
    nothing. Returns ``matches`` = the flex-algo set already equals the device. No-op
    (matches=True) when the fork lacks ISISFlexAlgo or *inst* is None.
    """
    if inst is None:
        return True
    try:
        from netbox_routing.models import ISISFlexAlgo
    except Exception:
        return True
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
    matches = True
    # Brownfield mirror: writing ISISFlexAlgo must not trip the greenfield
    # accept→push signal (that handler is _skip_on_render-gated → suppressed here).
    from .signals import suppress_intent_push

    with suppress_intent_push():
        for aid, data in incoming.items():
            row = existing.get(aid) or ISISFlexAlgo(instance=inst, algo_id=aid)
            changed = row.pk is None
            for col in cols:
                val = data.get(col)
                if val is not None and getattr(row, col, None) != val:
                    setattr(row, col, val)
                    changed = True
            if changed:
                matches = False
                if write:
                    row.save()
        for aid, row in existing.items():
            if aid not in incoming:
                matches = False
                if write:
                    row.delete()
    return matches


_ISIS_LEVEL_COLS = ("default_metric", "wide_metrics_only", "preference", "labeled_preference", "disabled", "auth_type")
_ISIS_IFACE_LEVEL_COLS = ("metric", "hello_interval", "hello_multiplier", "priority", "passive")
_ISIS_SR_COLS = (
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
_ISIS_FLEX_COLS = (
    "metric_type",
    "priority",
    "admin_group_exclude",
    "admin_group_include_any",
    "admin_group_include_all",
)
_ISIS_INSTANCE_SCALAR_COLS = (
    "net",
    "is_type",
    "metric_style",
    "area_auth_type",
    "area_auth_key",
    "domain_auth_type",
    "domain_auth_key",
    "overload_bit",
)


_ISIS_IFACE_SCALAR_ATTRS = (
    "circuit_type",
    "network_type",
    "metric",
    "passive",
    "hello_auth_type",
    "bfd_enabled",
    "csnp_interval",
    "retransmit_interval",
    "lsp_interval",
    "mesh_group",
)


def _isis_interface_routing_fields(state, entry, ri, bfd_enabled):
    """Build the (attr, device-value) list for the ISISInterface, guarded by the fork's columns."""
    rf = [
        # circuit_type/network_type are NOT NULL (blank=True, default='') on the
        # netbox-routing ISISInterface, so an unset value must mirror as '' not None.
        ("circuit_type", state.circuit_type or ""),
        ("network_type", state.network_type or ""),
        ("metric", state.metric),
        ("passive", state.passive),
    ]
    if hasattr(ri, "hello_auth_type"):
        rf.append(("hello_auth_type", state.hello_auth_type or ""))
    if hasattr(ri, "bfd_enabled"):
        rf.append(("bfd_enabled", bfd_enabled))
    if hasattr(ri, "csnp_interval"):
        rf.append(("csnp_interval", entry.get("csnp_interval")))
    if hasattr(ri, "retransmit_interval"):
        rf.append(("retransmit_interval", entry.get("retransmit_interval")))
    if hasattr(ri, "lsp_interval"):
        rf.append(("lsp_interval", entry.get("lsp_interval")))
    if hasattr(ri, "mesh_group"):
        rf.append(("mesh_group", entry.get("mesh_group") or ""))
    return rf


def _isis_device_matches_intent(entry, state) -> bool:
    """Return True when the device (adapter *entry*) has caught up to the owned overlay intent.

    Used for owned IS-IS rows where the clobber guard keeps the overlay == netbox-routing
    object, so the normal overlay-vs-object match can't tell whether the *device* has the
    change yet. Mirrors the OSPF device-vs-netbox status semantics.
    """
    return (
        entry.get("metric") == state.metric
        and (entry.get("network_type") or "") == (state.network_type or "")
        and (entry.get("circuit_type") or "") == (state.circuit_type or "")
        and bool(entry.get("passive", False)) == bool(state.passive)
    )


def _isis_interface_pass(state, entry, ri, bfd_enabled, *, write: bool) -> bool:
    """Compare (and, when ``write``, mirror) the device ISIS-interface graph onto *ri*."""
    fields: list[str] = []
    scalar_matches = True
    for attr, val in _isis_interface_routing_fields(state, entry, ri, bfd_enabled):
        if getattr(ri, attr) != val:
            scalar_matches = False
            if write:
                setattr(ri, attr, val)
                fields.append(attr)
    if fields:
        ri.save(update_fields=fields)
    settings_matches = _reconcile_isis_settings(ri, entry.get("settings"), write=write)
    try:
        from netbox_routing.models import ISISInterfaceLevel

        levels_matches = _reconcile_child_levels(
            ISISInterfaceLevel, "interface", ri, _ISIS_IFACE_LEVEL_COLS, entry.get("levels"), write=write
        )
    except Exception:
        levels_matches = True
    return scalar_matches and settings_matches and levels_matches


def _isis_interface_object_hash(ri) -> str:
    """Hash the netbox-routing ISISInterface's content (scalars + settings + levels)."""
    from django.contrib.contenttypes.models import ContentType

    from . import merge_util

    content: dict = {a: getattr(ri, a) for a in _ISIS_IFACE_SCALAR_ATTRS if hasattr(ri, a)}
    try:
        from netbox_routing.models import ISISSetting

        ct = ContentType.objects.get_for_model(type(ri))
        content["settings"] = {
            s.key: s.value for s in ISISSetting.objects.filter(assigned_object_type=ct, assigned_object_id=ri.pk)
        }
    except Exception:
        pass
    try:
        from netbox_routing.models import ISISInterfaceLevel

        content["levels"] = sorted(
            (
                {"level": r.level, **{c: getattr(r, c, None) for c in _ISIS_IFACE_LEVEL_COLS}}
                for r in ISISInterfaceLevel.objects.filter(interface=ri)
            ),
            key=lambda x: x["level"],
        )
    except Exception:
        pass
    return merge_util.content_hash(content)


def _link_routing_isis_interface(device, iface, af, state, instances: dict, bfd_enabled=None, entry=None, base=""):
    """3-way reconcile the netbox_routing.ISISInterface graph for this row.

    Object-content-hash 3-way: device changes auto-mirror when the object is untouched;
    operator edits survive + surface as 'changed'. The structural ``instance`` FK is
    always kept correct. Returns ``(ri, matches, base)``; ``(None, True, base)`` when
    netbox-routing isn't installed.
    """
    try:
        from netbox_routing.models import ISISInstance, ISISInterface
    except Exception:
        return None, True, base

    entry = entry or {}
    tag = state.process_tag
    if tag not in instances:
        instances[tag], _ = ISISInstance.objects.get_or_create(device=device, process_tag=tag)
    inst = instances[tag]

    ri, _ = ISISInterface.objects.get_or_create(interface=iface, address_family=af, defaults={"instance": inst})
    if ri.instance_id != inst.id:  # structural FK — always keep correct
        ri.instance = inst
        ri.save(update_fields=["instance"])

    matches = _isis_interface_pass(state, entry, ri, bfd_enabled, write=False)
    if matches:
        return ri, True, _isis_interface_object_hash(ri)
    if (not base) or _isis_interface_object_hash(ri) == base:
        _isis_interface_pass(state, entry, ri, bfd_enabled, write=True)
        return ri, True, _isis_interface_object_hash(ri)
    return ri, False, base


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
        # Owned (operator-claimed) rows hold the intent we push — set by
        # _accept_isis_interface and refreshed on every ISISInterface edit. A reconcile
        # must NOT clobber them with the device's current values (a greenfield owned
        # change isn't on the device yet, so the adapter reports metric/network-type as
        # None and would wipe the intent). Mirror device values only into unowned rows.
        if not sm.is_owned(state.status):
            state.process_tag = entry.get("process_tag") or ""
            state.circuit_type = entry.get("circuit_type") or ""
            state.network_type = entry.get("network_type") or ""
            state.metric = entry.get("metric")
            state.passive = bool(entry.get("passive", False))
            state.hello_auth_type = entry.get("hello_auth_type") or ""
            state.hello_auth_present = bool(entry.get("hello_auth_present", False))
        state.last_sync_at = now

        # 3-way merge: device changes auto-mirror when the ISISInterface object is
        # untouched (object_hash == base); operator edits survive + surface as 'changed'.
        state.isis_interface, iface_matches, new_base = _link_routing_isis_interface(
            device,
            iface,
            af,
            state,
            instances,
            bfd_enabled=entry.get("bfd_enabled"),
            entry=entry,
            base=state.device_base_hash,
        )
        state.device_base_hash = new_base
        if sm.is_owned(state.status):
            # Owned rows hold the intent we push; the clobber guard keeps the overlay
            # equal to the netbox-routing object, so _isis_interface_pass would always
            # "match" and prematurely settle in_sync before the change reaches the
            # device (which would also drop the row from the Apply preview). Instead,
            # gauge whether the DEVICE (entry) has caught up to the pushed intent —
            # mirrors the OSPF device-vs-netbox semantics.
            iface_matches = _isis_device_matches_intent(entry, state)
        state.status = sm.on_reconcile(state.status, matches=iface_matches)
        state.save()
        seen_keys.add((iface.pk, af))

    for stale in NSOISISInterfaceState.objects.filter(management=mgmt):
        if (stale.interface_id, stale.af) not in seen_keys:
            # vestigial = status-only ghost (no linked netbox-routing ISISInterface)
            sm.finalise_stale_overlay(stale, vestigial=stale.isis_interface_id is None)

    if dropped:
        logger.warning(
            "IS-IS reconcile for %s: %d interface(s) not found in NetBox, dropped: %s",
            device,
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )

    return list(NSOISISInterfaceState.objects.filter(management=mgmt).select_related("interface", "isis_interface"))


# netbox_routing ISISInstance scalar columns synced from NSO. Each is
# guarded by hasattr so the reconcile no-ops on a fork without the column.
# NOTE: segment-routing state (the adapter's top-level ``sr_enabled`` /
# ``sr_node_msd``) is NOT an ISISInstance scalar — netbox-routing moved it to the
# dedicated 1:1 ``ISISSegmentRouting`` child, reconciled via the ``segment_routing``
# bag in :func:`_reconcile_isis_segment_routing` (so it is not duplicated here).
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
    "distance",
    "maximum_paths",
    "reference_bandwidth",
)


def _isis_instance_pass(state, entry, inst, *, write: bool) -> bool:
    """Compare/mirror the whole device ISIS-instance graph onto *inst*.

    When ``write`` is set, mirror device → object; always return ``matches`` (the
    object already equals the device).
    """
    inst_fields: list[str] = []
    scalar_matches = True
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
            scalar_matches = False
            if write:
                setattr(inst, attr, val)
                inst_fields.append(attr)
    if state.overload_bit is not None and inst.overload_bit != state.overload_bit:
        scalar_matches = False
        if write:
            inst.overload_bit = state.overload_bit
            inst_fields.append("overload_bit")
    for attr in _ISIS_INSTANCE_SCALAR_ATTRS:
        if not hasattr(inst, attr):
            continue
        val = entry.get(attr)
        if val is not None and getattr(inst, attr) != val:
            scalar_matches = False
            if write:
                setattr(inst, attr, val)
                inst_fields.append(attr)
    if inst_fields:
        inst.save(update_fields=inst_fields)

    settings_matches = _reconcile_isis_settings(inst, entry.get("settings"), write=write)
    try:
        from netbox_routing.models import ISISLevel

        levels_matches = _reconcile_child_levels(
            ISISLevel, "instance", inst, _ISIS_LEVEL_COLS, entry.get("levels"), write=write
        )
    except Exception:
        levels_matches = True
    sr_matches = _reconcile_isis_segment_routing(inst, entry.get("segment_routing"), write=write)
    flex_matches = _reconcile_isis_flex_algos(inst, entry.get("flex_algos"), write=write)
    return scalar_matches and settings_matches and levels_matches and sr_matches and flex_matches


def _isis_instance_object_hash(inst) -> str:
    """Hash the netbox-routing ISISInstance's full content (cols + settings + levels + SR + flex).

    Self-consistent (compared only against itself / the stored base), so it need not
    match a separate device serializer.
    """
    from django.contrib.contenttypes.models import ContentType

    from . import merge_util

    content: dict = {a: getattr(inst, a, None) for a in _ISIS_INSTANCE_SCALAR_COLS}
    for a in _ISIS_INSTANCE_SCALAR_ATTRS:
        if hasattr(inst, a):
            content[a] = getattr(inst, a)
    try:
        from netbox_routing.models import ISISSetting

        ct = ContentType.objects.get_for_model(type(inst))
        content["settings"] = {
            s.key: s.value for s in ISISSetting.objects.filter(assigned_object_type=ct, assigned_object_id=inst.pk)
        }
    except Exception:
        pass
    try:
        from netbox_routing.models import ISISLevel

        content["levels"] = sorted(
            (
                {"level": r.level, **{c: getattr(r, c, None) for c in _ISIS_LEVEL_COLS}}
                for r in ISISLevel.objects.filter(instance=inst)
            ),
            key=lambda x: x["level"],
        )
    except Exception:
        pass
    try:
        from netbox_routing.models import ISISSegmentRouting

        sr = ISISSegmentRouting.objects.filter(instance=inst).first()
        content["sr"] = {c: getattr(sr, c, None) for c in _ISIS_SR_COLS} if sr else None
    except Exception:
        pass
    try:
        from netbox_routing.models import ISISFlexAlgo

        content["flex"] = sorted(
            (
                {"algo_id": r.algo_id, **{c: getattr(r, c, None) for c in _ISIS_FLEX_COLS}}
                for r in ISISFlexAlgo.objects.filter(instance=inst)
            ),
            key=lambda x: x["algo_id"],
        )
    except Exception:
        pass
    return merge_util.content_hash(content)


def _sync_routing_isis_instance(device, tag, state, entry, base):
    """3-way reconcile the netbox_routing.ISISInstance graph for *tag*.

    Uses an object-content hash + the object-vs-device compare: device-side changes
    auto-mirror when the object is untouched (object_hash == base); operator edits
    survive and surface as 'changed'. Returns ``(inst, matches, base)`` (returned base
    advanced on seed/mirror/insync). ``(None, True, base)`` when netbox-routing absent.
    (ISIS folds both-moved into 'changed' — the edit is always preserved either way.)
    """
    try:
        from netbox_routing.models import ISISInstance
    except Exception:
        return None, True, base

    inst, _ = ISISInstance.objects.get_or_create(device=device, process_tag=tag)
    matches = _isis_instance_pass(state, entry, inst, write=False)
    if matches:
        return inst, True, _isis_instance_object_hash(inst)
    if (not base) or _isis_instance_object_hash(inst) == base:
        # first import (seed) OR device moved while the object was untouched (mirror)
        _isis_instance_pass(state, entry, inst, write=True)
        return inst, True, _isis_instance_object_hash(inst)
    return inst, False, base  # operator edited → changed (edit preserved)


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

        # 3-way merge over the whole ISIS graph: device changes auto-mirror when the
        # object is untouched (object_hash == base); operator edits survive + surface
        # as 'changed'. device_base_hash persists the agreed object snapshot.
        state.isis_instance, inst_matches, new_base = _sync_routing_isis_instance(
            device, tag, state, entry, state.device_base_hash
        )
        state.device_base_hash = new_base
        state.status = sm.on_reconcile(state.status, matches=inst_matches)
        state.save()
        seen_tags.add(tag)

    for stale in NSOISISInstanceState.objects.filter(management=mgmt):
        if stale.process_tag not in seen_tags:
            # vestigial = status-only ghost (no linked netbox-routing ISISInstance)
            sm.finalise_stale_overlay(stale, vestigial=stale.isis_instance_id is None)

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


def _clean_router_id(value) -> str:
    """Normalise an exported router-id, treating the literal ``"None"`` as absent.

    A process the device runs without an explicit router-id can stringify to ``"None"``
    upstream — truthy, but not a valid IP. Returns ``""`` for that (and any falsy value) so
    it never reaches netbox-routing's router_id IPAddressField (which 500s on ``"None"``).
    """
    if not value or str(value).strip().lower() == "none":
        return ""
    return value


def _get_or_create_ospf_instance(device, pid, entry, OSPFInstance, base):
    """3-way reconcile the netbox-routing OSPFInstance for one process.

    Keyed on (device, process_id). ``router_id`` is required by the model so an
    instance NSO reports without one is skipped. 3-way merge against *base*: device
    changes auto-mirror when the object is untouched; operator edits survive and
    surface as drift; both-moved → conflict. Returns ``(obj, matches, conflict, base)``
    where the returned base is the new device hash (advanced on seed/mirror/insync).
    """
    from . import merge_util

    if OSPFInstance is None:
        return None, True, False, base
    router_id = _clean_router_id(entry.get("router_id"))
    if not router_id:  # no router-id (or the literal "None") → can't build the instance; skip
        return None, True, False, base
    vrf_obj = _resolve_ospf_vrf(entry.get("vrf") or "")
    obj, created = OSPFInstance.objects.get_or_create(
        device=device,
        process_id=pid,
        defaults={"name": str(pid), "router_id": router_id, "vrf": vrf_obj},
    )
    dev = {"router_id": str(router_id), "vrf": merge_util.pk(vrf_obj)}
    objc = {"router_id": str(obj.router_id), "vrf": obj.vrf_id}
    dev_hash = merge_util.content_hash(dev)
    action = merge_util.three_way(created=created, base=base, obj_hash=merge_util.content_hash(objc), dev_hash=dev_hash)
    if action in ("seed", "mirror", "insync"):
        if action == "mirror":
            obj.router_id = router_id
            obj.vrf = vrf_obj
            obj.save(update_fields=["router_id", "vrf"])
        return obj, True, False, dev_hash
    if action == "freeze":
        return obj, False, False, base
    return obj, False, True, base  # conflict


_OSPF_AUTH_MAP = {"message-digest": "message-digest", "null": "null"}
_OSPF_NETWORK_TYPES = {"broadcast", "non-broadcast", "point-to-point", "point-to-multipoint"}


def _canonical_area_id(area_id) -> str:
    """Canonicalise an OSPF area-id to dotted-quad (``0`` → ``0.0.0.0``).

    OSPF area-ids are canonically IPv4, so a bare integer and its dotted form name the same
    area. Already-dotted and non-numeric values pass through unchanged.
    """
    s = str(area_id)
    if "." in s:
        return s
    try:
        n = int(s)
    except (TypeError, ValueError):
        return s
    return f"{(n >> 24) & 255}.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


def _resolve_ospf_area(OSPFArea, area_id):
    """Get/create the OSPFArea, matching equivalent area-id forms (``0`` ≡ ``0.0.0.0``).

    The device reports the dotted form; an operator may have created the area as a bare
    integer (or vice-versa). Match either existing form so we don't spawn a duplicate area.
    Comparison still goes by canonical *value* (see ``_fill_ospf_interface``), so even a
    pre-existing duplicate doesn't prevent the owned interface from settling.
    """
    canon = _canonical_area_id(area_id)
    candidates = {str(area_id), canon}
    try:  # also the bare-integer form of the canonical address
        p = [int(x) for x in canon.split(".")]
        candidates.add(str((p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]))
    except Exception:
        pass
    existing = OSPFArea.objects.filter(area_id__in=candidates).first()
    if existing is not None:
        return existing
    return OSPFArea.objects.get_or_create(area_id=canon, defaults={"area_type": "standard"})[0]


def _fill_ospf_interface(entry, iface, inst_by_pid, OSPFArea, OSPFInterface, base):
    """3-way reconcile the netbox-routing OSPFInterface + its OSPFArea.

    OSPFArea is a global object keyed by area_id. OSPFInterface is OneToOne on the
    dcim.Interface. Auth keys are never imported — only the auth *type*. 3-way merge
    against *base*: device changes auto-mirror when the object is untouched; operator
    edits survive and surface as drift; both-moved → conflict. Returns
    ``(matches, conflict, base)`` (returned base advanced on seed/mirror/insync).
    """
    from . import merge_util

    if OSPFInterface is None or OSPFArea is None:
        return True, False, base
    pid = entry.get("process_id")
    inst = inst_by_pid.get(pid)
    if inst is None:
        return True, False, base
    area = _resolve_ospf_area(OSPFArea, entry.get("area_id") or "0.0.0.0")

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
    if not created and obj.passive is None:
        # An operator-created OSPF interface leaves passive unset (None) → the routing UI
        # renders "—". The device value is a concrete bool, so normalise None → that value
        # (benign: None and False already compare equal in the 3-way below).
        obj.passive = fields["passive"]
        obj.save(update_fields=["passive"])

    def _content(src_is_obj):
        out = {}
        for key, val in fields.items():
            if key == "instance":
                out[key] = obj.instance_id if src_is_obj else merge_util.pk(val)
            elif key == "area":
                # Compare areas by canonical value (0 ≡ 0.0.0.0), not pk — the device's
                # dotted area must match an operator's bare-integer area (and survive any
                # pre-existing duplicate area rows) so the owned interface settles in_sync.
                a = obj.area if src_is_obj else val
                out[key] = _canonical_area_id(a.area_id) if a is not None else None
            elif key == "passive":
                # passive is a nullable boolean: an operator who never set it leaves None,
                # while the device-derived value is bool(...) → False. Treat None ≡ False so
                # the owned interface settles instead of perpetually mismatching.
                out[key] = bool(getattr(obj, key) if src_is_obj else val)
            else:
                out[key] = getattr(obj, key) if src_is_obj else val
        return out

    dev_hash = merge_util.content_hash(_content(False))
    action = merge_util.three_way(
        created=created, base=base, obj_hash=merge_util.content_hash(_content(True)), dev_hash=dev_hash
    )
    if action in ("seed", "mirror", "insync"):
        if action == "mirror":
            for key, val in fields.items():
                setattr(obj, key, val)
            obj.save()
        return True, False, dev_hash
    if action == "freeze":
        return False, False, base
    return False, True, base  # conflict


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
        state.router_id = _clean_router_id(entry.get("router_id"))
        state.vrf = entry.get("vrf") or ""
        state.areas = entry.get("areas") or []
        # Admin-state (Nokia 'admin-state enable'): mirror the device value into unowned
        # rows; owned rows keep the operator intent (set by _accept_ospf_instance) so a
        # reconcile before the enable reaches the device doesn't wipe it.
        if not sm.is_owned(state.status):
            state.enabled = entry.get("enabled")
        state.last_sync_at = now
        # 3-way merge: device router_id/vrf change auto-mirrors when the object is
        # untouched; an operator edit surfaces as 'changed' and survives; both → conflict.
        ospf_inst, inst_matches, inst_conflict, new_base = _get_or_create_ospf_instance(
            device, pid, entry, OSPFInstance, state.device_base_hash
        )
        if ospf_inst is not None:
            state.ospf_instance = ospf_inst
        state.device_base_hash = new_base
        state.status = sm.on_reconcile(state.status, matches=inst_matches, conflict=inst_conflict)
        state.save()
        seen_pids.add(pid)

    for stale in NSOOSPFInstanceState.objects.filter(management=mgmt):
        if stale.process_id not in seen_pids:
            # vestigial = status-only ghost (no linked netbox-routing OSPFInstance)
            sm.finalise_stale_overlay(stale, vestigial=stale.ospf_instance_id is None)

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
        # For owned (operator-claimed) rows the overlay columns hold the intent we
        # push — set by _accept_ospf_interface and refreshed on every OSPFInterface
        # edit. A reconcile must NOT clobber them with the device's current values:
        # a greenfield owned change isn't on the device yet, so the adapter reports
        # cost/network-type as None and would wipe the intent (then the next re-push
        # would drop it from the adapter too). Mirror device values only into unowned
        # (brownfield / imported) rows; owned rows keep the operator intent.
        if not sm.is_owned(state.status):
            state.process_id = entry["process_id"]
            state.area_id = entry.get("area_id") or ""
            state.passive = bool(entry.get("passive", False))
            state.priority = entry.get("priority")
            state.cost = entry.get("cost")
            state.network_type = entry.get("network_type") or ""
            state.auth_type = entry.get("auth_type") or ""
            state.auth_present = bool(entry.get("auth_present", False))
        state.last_sync_at = now
        # 3-way merge: device change auto-mirrors when the OSPFInterface is untouched;
        # an operator edit surfaces as 'changed' and survives; both moved → conflict.
        iface_matches, iface_conflict, new_base = _fill_ospf_interface(
            entry, iface, inst_by_pid, OSPFArea, OSPFInterface, state.device_base_hash
        )
        state.device_base_hash = new_base
        state.status = sm.on_reconcile(state.status, matches=iface_matches, conflict=iface_conflict)
        state.save()
        seen_iface_pks.add(iface.pk)

    for stale in NSOOSPFInterfaceState.objects.filter(management=mgmt):
        if stale.interface_id not in seen_iface_pks:
            # vestigial = status-only ghost (no durable netbox-routing OSPFInterface row)
            vestigial = (
                OSPFInterface is None or not OSPFInterface.objects.filter(interface_id=stale.interface_id).exists()
            )
            sm.finalise_stale_overlay(stale, vestigial=vestigial)

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


class RoutePolicyNSODevices(PluginTemplateExtension):
    """Adds an "NSO — applied to devices" panel to the route-policy object pages.

    Targets the netbox-routing community-list / route-map / prefix-list / as-path
    detail pages. The relationship is a GenericForeignKey from our overlay
    (``NSORoutePolicyState``)
    *into* netbox-routing, so NetBox's built-in "Related Objects" card cannot surface
    it — we render our own panel here, without modifying netbox-routing. One row per
    managing device, with that device's per-object status and last apply time.
    """

    models = [
        "netbox_routing.communitylist",
        "netbox_routing.routemap",
        "netbox_routing.prefixlist",
        "netbox_routing.aspath",
    ]

    def full_width_page(self):
        """Render the per-device status table for the route-policy object on display.

        Each row also carries a *capability* verdict for that device — a cache-only
        pre-flight (no live probe) so the operator sees, at a glance, whether the whole
        object applies on that box or some parts are silently dropped.
        """
        from django.contrib.contenttypes.models import ContentType

        from .models import NSORoutePolicyState
        from .status_machine import OWNED_STATES

        obj = self.context["object"]
        ct = ContentType.objects.get_for_model(obj)
        states = list(
            NSORoutePolicyState.objects.filter(content_type=ct, object_id=obj.pk).select_related(
                "management", "management__device"
            )
        )
        self._annotate_capability(obj, states)
        # Edit-propagation blast radius: this object is shared by name across devices, so an
        # operator edit here re-asserts ownership + re-pushes to every device that *owns* it
        # (accepted / deploying / in_sync / apply_failed). Brownfield/un-owned overlays
        # (imported / changed / conflict) are NOT auto-pushed — they surface via reconcile.
        # Surfacing the owned set lets the operator see exactly which boxes an edit touches.
        propagation_devices = [s.management.device for s in states if s.status in OWNED_STATES]
        return self.render(
            "netbox_nso_plugin/route_policy_nso_devices.html",
            extra_context={"nso_states": states, "propagation_devices": propagation_devices},
        )

    @staticmethod
    def _annotate_capability(obj, states):
        """Attach a ``capability`` dict to each state row (cache-only, best-effort).

        ``{state: supported|partial|unknown, unsupported: [...]}``. Computed from the
        adapter's persisted matrix without probing NSO, so the panel stays cheap; an
        unreachable adapter or never-probed device degrades to ``unknown``.
        """
        from . import adapter_client as client
        from .signals import _preflight_constructs

        for state in states:
            community_members, set_keys, match_keys, aspath_names = _preflight_constructs(state.family, obj)
            if not (community_members or set_keys or match_keys or aspath_names):
                # prefix-list carries nothing flaggable — universally representable.
                state.capability = {"state": "supported", "unsupported": []}
                continue
            adapter_id = getattr(state.management, "adapter_device_id", None)
            if not adapter_id:
                state.capability = {"state": "unknown", "unsupported": []}
                continue
            verdict = client.preflight_route_policy(
                adapter_id, community_members, set_keys, match_keys, aspath_names, refresh=False
            )
            if not verdict.get("known"):
                state.capability = {"state": "unknown", "unsupported": []}
            elif verdict.get("coverage_unknown"):
                # probed, but this NED's route-policy isn't classified yet (Junos/Nokia) —
                # honest "not assessed" instead of a green "supported".
                state.capability = {"state": "unassessed", "unsupported": []}
            elif verdict.get("fully_supported"):
                state.capability = {"state": "supported", "unsupported": []}
            else:
                state.capability = {"state": "partial", "unsupported": verdict.get("unsupported", [])}


template_extensions = [InterfaceNSOBadge, RoutePolicyNSODevices]
