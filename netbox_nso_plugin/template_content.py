# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox template extensions — interface badge (device NSO tab is now a registered tab view)."""

import contextlib
import copy
import logging
from datetime import datetime

from django.apps import apps
from django.utils import timezone
from netbox.plugins import PluginTemplateExtension

from . import status_machine as sm
from .intent_state import locked_mirror_refresh, mirror_reconciler
from .snmp_versions import canonical_snmp_version

logger = logging.getLogger(__name__)


def _cas_mirror_update(queryset, **values):
    """Use ``mirror_refresh`` to authorize a save inside a push-suppressing ``mirror_reconciler``."""
    row = queryset.select_for_update(of=("self",)).first()
    if row is None:
        return None
    with locked_mirror_refresh(row, values) as locked:
        if locked is None:
            return None
        for field_name, value in values.items():
            setattr(locked, field_name, value)
        locked.save(update_fields=set(values))
    return locked


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


def _interface_reconcile_operations(device, interfaces, planned_at):
    """Build the exact interface-attribute writes used by preflight and apply.

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

    from .derived_intent import get_sentinel_templates
    from .models import NSOInterfaceState
    from .renderer_writer import planned_save

    # Derived-intent templates (e.g. description-from-cable). A description whose NetBox
    # value matches one is NetBox intent BY DEFINITION (the plugin computes it from
    # topology), so it must be owned even if the adapter reads it as imported — see
    # _resolve_interface_attr_status.
    derived_templates = get_sentinel_templates()

    # Build name → Interface map for this device's interfaces in the DB
    iface_map = {i.name: i for i in Interface.objects.filter(device=device)}

    current_by_key = {
        (row.interface_id, row.attribute): row
        for row in NSOInterfaceState.objects.filter(interface__device=device).order_by("pk")
    }
    saves = []
    operations = []
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

            current = current_by_key.get((iface.pk, attr_name))
            created = current is None
            state = (
                NSOInterfaceState(interface=iface, attribute=attr_name, status=status, nso_value=nso_value)
                if created
                else copy.copy(current)
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
                state.accepted_at = planned_at
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
            state.last_sync_at = planned_at
            update_fields.append("last_sync_at")
            fields = None if created else tuple(update_fields)
            saves.append(
                planned_save(
                    state,
                    update_fields=fields,
                    force_insert=created,
                    natural_key=("interface", "attribute"),
                )
            )
            operations.append((state, fields, created))

            result[(iface_data["name"], attr_name)] = state

    return saves, operations, result


def _interface_plan_and_operations(device, interfaces):
    """Freeze one interface-attribute reconciliation before lock acquisition."""
    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, operations, result = _interface_reconcile_operations(device, interfaces, planned_at)
    return RendererMutationPlan.build(saves=saves, planned_at=planned_at), operations, result


def interface_reconcile_plan(device, interfaces: list):
    """Return the exact renderer mutation plan for interface attributes."""
    plan, _operations, _result = _interface_plan_and_operations(device, interfaces)
    return plan


def _upsert_interface_states(device, interfaces: list) -> dict:
    """Apply one frozen interface-attribute reconciliation."""
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    if active is None:
        plan, operations, result = _interface_plan_and_operations(device, interfaces)
    else:
        plan = active.plan
        _saves, operations, result = _interface_reconcile_operations(device, interfaces, plan.planned_at)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        for state, update_fields, force_insert in operations:
            writer.save(state, update_fields=update_fields, force_insert=force_insert)
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


def _interface_ip_vrf(VRF, name):
    """Return the named VRF, or the global table when it is absent or unknown."""
    if not name or VRF is None:
        return None
    return VRF.objects.filter(name=name).first()


def _interface_ip_native(state, vrf_obj, IPAddress, interface_type):
    """Return the native IP assigned to the state interface under the exact GFK type."""
    return IPAddress.objects.filter(
        address=state.address,
        vrf=vrf_obj,
        assigned_object_type=interface_type,
        assigned_object_id=state.interface_id,
    ).first()


def _interface_ip_reconcile_operations(device, payload, planned_at):  # noqa: C901, PLR0915
    """Build the exact native and overlay writes for one interface-IP read."""
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from django.core.exceptions import ValidationError
    from ipam.models import IPAddress

    try:
        from ipam.models import VRF
    except ImportError:
        VRF = None

    from .models import NSOInterfaceIPState
    from .renderer_writer import planned_delete, planned_save

    auto_create = _adapter_setting("interface_ip_auto_create")
    interface_type = ContentType.objects.get_for_model(Interface)
    iface_map = {row.name: row for row in Interface.objects.filter(device=device)}
    states = {
        (row.interface_id, row.address, row.vrf): row
        for row in NSOInterfaceIPState.objects.filter(interface__device=device)
        .select_related("interface", "peer_state")
        .order_by("pk")
    }
    payload_set, attr_map, bound_port_map = _build_payload_index(payload)
    resolved_keys = set()
    saves = []
    deletes = []
    operations = []
    prefixes = []

    def save(instance, *, update_fields=None, force_insert=False, natural_key=()):
        saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            )
        )
        operations.append(("save", instance, update_fields, force_insert))

    def delete(instance):
        deletes.append(planned_delete(instance))
        operations.append(("delete", instance, None, False))

    for iface_name, address, vrf_name in sorted(payload_set):
        iface = iface_map.get(iface_name)
        if iface is None and iface_name in bound_port_map:
            iface = iface_map.get(bound_port_map[iface_name])
        if iface is None:
            continue
        key = (iface.pk, address, vrf_name)
        resolved_keys.add(key)
        current = states.get(key)
        attrs = attr_map.get((iface_name, address, vrf_name), {})
        state = (
            copy.copy(current)
            if current is not None
            else NSOInterfaceIPState(
                interface=iface,
                address=address,
                vrf=vrf_name,
                status="unknown",
            )
        )
        state.nso_value = address
        state.family = attrs.get("family", "ipv4")
        state.secondary = attrs.get("secondary", False)
        state.last_sync_at = planned_at

        vrf_obj = _interface_ip_vrf(VRF, vrf_name)
        existing_ip = IPAddress.objects.filter(address=address, vrf=vrf_obj).first()
        previous_status = state.status
        if existing_ip is not None and existing_ip.assigned_object == iface:
            state.status = sm.on_reconcile(state.status, matches=True)
            if (
                state.auto_assigned
                and state.status == "in_sync"
                and previous_status != "in_sync"
                and existing_ip.status == "reserved"
                and (state.peer_state is None or state.peer_state.status == "in_sync")
            ):
                native = copy.copy(existing_ip)
                native.status = "active"
                save(native, update_fields=("status",))
                if state.peer_state is not None:
                    peer_vrf = _interface_ip_vrf(VRF, state.peer_state.vrf)
                    peer_ip = _interface_ip_native(state.peer_state, peer_vrf, IPAddress, interface_type)
                    if peer_ip is not None and peer_ip.status == "reserved":
                        peer_candidate = copy.copy(peer_ip)
                        peer_candidate.status = "active"
                        save(peer_candidate, update_fields=("status",))
        elif existing_ip is not None and existing_ip.assigned_object is None:
            if auto_create:
                native = copy.copy(existing_ip)
                native.assigned_object = iface
                save(native, update_fields=("assigned_object_type", "assigned_object_id"))
                state.status = sm.on_reconcile(state.status, matches=True)
            elif not sm.is_owned(state.status):
                state.status = sm.on_reconcile(state.status, matches=True)
        elif existing_ip is not None:
            state.status = sm.on_reconcile(state.status, matches=False, conflict=True)
        elif auto_create and state.status != "conflict":
            native = IPAddress(address=address, vrf=vrf_obj)
            native.assigned_object = iface
            try:
                native.full_clean()
            except ValidationError:
                if not sm.is_owned(state.status):
                    state.status = "conflict"
            else:
                save(native, force_insert=True, natural_key=("address", "vrf"))
                prefixes.append((address, vrf_obj))
                if not sm.is_owned(state.status):
                    state.status = "imported"
        elif not sm.is_owned(state.status) and state.status != "conflict":
            state.status = "imported"

        save(
            state,
            force_insert=current is None,
            natural_key=("interface", "address", "vrf"),
        )

    reported_addresses = {(interface_id, address) for interface_id, address, _vrf in resolved_keys}
    for stale in states.values():
        key = (stale.interface_id, stale.address, stale.vrf)
        if key in resolved_keys:
            continue
        stale_vrf = _interface_ip_vrf(VRF, stale.vrf)
        native = _interface_ip_native(stale, stale_vrf, IPAddress, interface_type)
        if (stale.interface_id, stale.address) in reported_addresses:
            if native is not None:
                native_candidate = copy.copy(native)
                native_candidate.assigned_object = None
                save(native_candidate, update_fields=("assigned_object_type", "assigned_object_id"))
            delete(stale)
            continue
        next_status = sm.on_reconcile(stale.status, present=False)
        if next_status == stale.status:
            continue
        if native is not None:
            native_candidate = copy.copy(native)
            native_candidate.assigned_object = None
            save(native_candidate, update_fields=("assigned_object_type", "assigned_object_id"))
        candidate = copy.copy(stale)
        candidate.status = next_status
        candidate.last_sync_at = planned_at
        save(candidate, update_fields=("status", "last_sync_at"))

    return saves, deletes, operations, prefixes


def _interface_ip_plan_and_operations(device, payload, planned_at=None):
    """Freeze one interface-IP reconciliation before lock acquisition."""
    from .renderer_writer import RendererMutationPlan

    planned_at = planned_at or timezone.now()
    saves, deletes, operations, prefixes = _interface_ip_reconcile_operations(device, payload, planned_at)
    plan = RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)
    return plan, operations, prefixes


def interface_ip_reconcile_plan(device, payload):
    """Return the exact native and overlay mutation plan for interface IPs."""
    plan, _operations, _prefixes = _interface_ip_plan_and_operations(device, payload)
    return plan


def _ensure_interface_ip_prefixes(prefixes):
    """Create missing informational prefixes after their planned IP writes succeed."""
    from django.db import transaction
    from ipam.models import Prefix

    for address, vrf_obj in prefixes:
        try:
            containing = Prefix.objects.filter(prefix__net_contains=address.split("/")[0], vrf=vrf_obj).first()
            if containing is None:
                with transaction.atomic():
                    Prefix(prefix=address, vrf=vrf_obj).save()
        except Exception as exc:  # pragma: no cover
            logger.warning("nso_ip.prefix_link_failed addr=%s: %s", address, repr(exc))


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
    from .models import NSOInterfaceIPState
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    active = active_renderer_writer()
    if active is None:
        plan, operations, prefixes = _interface_ip_plan_and_operations(device, payload)
    else:
        plan = active.plan
        _saves, _deletes, operations, prefixes = _interface_ip_reconcile_operations(
            device,
            payload,
            plan.planned_at,
        )
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        for operation, instance, update_fields, force_insert in operations:
            if operation == "delete":
                writer.delete(instance)
            else:
                writer.save(instance, update_fields=update_fields, force_insert=force_insert)
        _ensure_interface_ip_prefixes(prefixes)

    return list(NSOInterfaceIPState.objects.filter(interface__device=device).select_related("interface"))


def _snmp_reconcile_operations(device, payload, planned_at):  # noqa: C901
    """Build the deterministic SNMP write sequence for preflight and apply."""
    from .models import (
        NSODeviceManagement,
        NSOSnmpCommunityState,
        NSOSnmpHostState,
        NSOSnmpSystemInfoState,
        NSOSnmpV3UserState,
    )
    from .renderer_writer import planned_delete, planned_save
    from .signals import snmp_host_push_blocker, snmp_v3_user_push_blocker

    try:
        management = device.nso_management
    except NSODeviceManagement.DoesNotExist:
        return [], [], [], None

    saves = []
    deletes = []
    operations = []

    def save(instance, *, update_fields=None, force_insert=False, natural_key=()):
        saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            )
        )
        operations.append(("save", instance, update_fields, force_insert))

    def delete(instance):
        deletes.append(planned_delete(instance))
        operations.append(("delete", instance, None, False))

    def retire_absent(rows, incoming):
        for key, row in rows.items():
            if key in incoming:
                continue
            if not sm.is_owned(row.status):
                delete(row)
                continue
            new_status = sm.on_reconcile(row.status, present=False)
            if new_status != row.status:
                candidate = copy.copy(row)
                candidate.status = new_status
                save(candidate, update_fields=("status",))

    communities = {
        row.community_hash: row for row in NSOSnmpCommunityState.objects.filter(management=management).order_by("pk")
    }
    incoming_communities = {}
    for entry in payload.get("communities") or []:
        if isinstance(entry, dict) and (community_hash := entry.get("community_hash") or ""):
            incoming_communities[community_hash] = entry
    value_compare = _snmp_value_compare_supported(device)
    for community_hash, entry in incoming_communities.items():
        current = communities.get(community_hash)
        candidate = (
            copy.copy(current)
            if current is not None
            else NSOSnmpCommunityState(management=management, community_hash=community_hash)
        )
        access = entry.get("access") or "RO"
        acl = entry.get("acl") or ""
        has_secret = bool(entry.get("has_secret", True))
        owned = current is not None and sm.is_owned(current.status)
        if owned:
            if not NSOSnmpCommunityState.objects.filter(
                pk=current.pk,
                community_hash=community_hash,
                status=current.status,
                access=current.access,
                acl=current.acl,
                vault_secret_hash=current.vault_secret_hash,
            ).exists():
                continue
            matches = None
            if value_compare and candidate.vault_secret_hash:
                matches = (
                    candidate.vault_secret_hash == community_hash
                    and candidate.access == access
                    and candidate.acl == acl
                )
            candidate.status = sm.on_reconcile(
                candidate.status,
                matches=matches,
                settles_deploying=False,
            )
            candidate.has_secret = has_secret
            candidate.last_sync_at = planned_at
            fields = ("status", "has_secret", "last_sync_at")
        else:
            candidate.access = access
            candidate.acl = acl
            candidate.has_secret = has_secret
            candidate.status = sm.on_reconcile(candidate.status)
            candidate.last_sync_at = planned_at
            fields = None if current is None else ("access", "acl", "has_secret", "status", "last_sync_at")
        save(
            candidate,
            update_fields=fields,
            force_insert=current is None,
            natural_key=("management", "community_hash"),
        )
    retire_absent(communities, incoming_communities)

    users = {row.username: row for row in NSOSnmpV3UserState.objects.filter(management=management).order_by("pk")}
    incoming_users = {}
    for entry in payload.get("v3_users") or []:
        if isinstance(entry, dict) and (username := entry.get("username") or ""):
            incoming_users[username] = entry
    for username, entry in incoming_users.items():
        current = users.get(username)
        candidate = (
            copy.copy(current) if current is not None else NSOSnmpV3UserState(management=management, username=username)
        )
        candidate.has_auth_secret = bool(entry.get("has_auth_secret", False))
        candidate.has_priv_secret = bool(entry.get("has_priv_secret", False))
        candidate.status = sm.on_reconcile(candidate.status, settles_deploying=False)
        candidate.last_sync_at = planned_at
        if sm.is_owned(candidate.status) and (reason := snmp_v3_user_push_blocker(candidate)):
            logger.warning("SNMP reconcile: %s cannot be rendered: %s", candidate, reason)
            candidate.status = sm.ERROR
        fields = None if current is None else ("has_auth_secret", "has_priv_secret", "status", "last_sync_at")
        save(
            candidate,
            update_fields=fields,
            force_insert=current is None,
            natural_key=("management", "username"),
        )
    retire_absent(users, incoming_users)

    hosts = {row.address: row for row in NSOSnmpHostState.objects.filter(management=management).order_by("pk")}
    incoming_hosts = {}
    for entry in payload.get("hosts") or []:
        if isinstance(entry, dict) and (address := entry.get("address") or ""):
            incoming_hosts[address] = entry
    suppress_default_port = _device_ned_id(device).startswith(("timos", "arcos-", "cisco-ios-cli", "cisco-iosxe-cli"))
    host_fields = ("version", "notify_type", "port", "community_hash", "username")
    for address, entry in incoming_hosts.items():
        current = hosts.get(address)
        candidate = (
            copy.copy(current) if current is not None else NSOSnmpHostState(management=management, address=address)
        )
        device_values = {
            "version": entry.get("version") or "v2c",
            "notify_type": entry.get("notify_type") or "trap",
            "port": entry.get("port"),
            "community_hash": entry.get("community_hash") or "",
            "username": entry.get("username") or "",
        }
        owned = current is not None and sm.is_owned(current.status)
        if owned:
            if not NSOSnmpHostState.objects.filter(
                pk=current.pk,
                address=address,
                status=current.status,
                **{field: getattr(current, field) for field in host_fields},
            ).exists():
                continue
            matches = all(
                (
                    suppress_default_port
                    and field == "port"
                    and value is None
                    and getattr(candidate, field) in (None, 162)
                )
                or (
                    field == "version"
                    and canonical_snmp_version(getattr(candidate, field)) == canonical_snmp_version(value)
                )
                or getattr(candidate, field) == value
                for field, value in device_values.items()
            )
            candidate.status = sm.on_reconcile(
                candidate.status,
                matches=matches,
                settles_deploying=False,
            )
            fields = ("status", "last_sync_at")
        else:
            for field, value in device_values.items():
                setattr(candidate, field, value)
            candidate.status = sm.on_reconcile(candidate.status)
            fields = None if current is None else (*host_fields, "status", "last_sync_at")
        candidate.last_sync_at = planned_at
        if sm.is_owned(candidate.status) and (reason := snmp_host_push_blocker(candidate)):
            logger.warning("SNMP reconcile: %s cannot be rendered: %s", candidate, reason)
            candidate.status = sm.ERROR
        save(
            candidate,
            update_fields=fields,
            force_insert=current is None,
            natural_key=("management", "address"),
        )
    retire_absent(hosts, incoming_hosts)

    system_data = payload.get("system_info") or {}
    current_system = NSOSnmpSystemInfoState.objects.filter(management=management).first()
    system_result = None
    if not system_data:
        if current_system is not None and sm.is_owned(current_system.status):
            system_result = copy.copy(current_system)
            new_status = sm.on_reconcile(system_result.status, present=False)
            if new_status != system_result.status:
                system_result.status = new_status
                save(system_result, update_fields=("status",))
    else:
        system_result = (
            copy.copy(current_system) if current_system is not None else NSOSnmpSystemInfoState(management=management)
        )
        location = system_data.get("location") or ""
        contact = system_data.get("contact") or ""
        owned = current_system is not None and sm.is_owned(current_system.status)
        if owned:
            if NSOSnmpSystemInfoState.objects.filter(
                pk=current_system.pk,
                status=current_system.status,
                location=current_system.location,
                contact=current_system.contact,
            ).exists():
                matches = system_result.location == location and system_result.contact == contact
                system_result.status = sm.on_reconcile(
                    system_result.status,
                    matches=matches,
                    settles_deploying=False,
                )
                system_result.last_sync_at = planned_at
                save(system_result, update_fields=("status", "last_sync_at"))
            else:
                system_result = None
        else:
            system_result.location = location
            system_result.contact = contact
            system_result.status = sm.on_reconcile(system_result.status)
            system_result.last_sync_at = planned_at
            fields = None if current_system is None else ("location", "contact", "status", "last_sync_at")
            save(
                system_result,
                update_fields=fields,
                force_insert=current_system is None,
                natural_key=("management",),
            )

    return saves, deletes, operations, system_result


def _snmp_plan_and_operations(device, payload):
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, deletes, operations, system_result = _snmp_reconcile_operations(device, payload, planned_at)
    plan = RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)
    return plan, operations, system_result


def snmp_reconcile_plan(device, payload):
    """Freeze every SNMP overlay write before reconciliation."""
    plan, _operations, _system_result = _snmp_plan_and_operations(device, payload)
    return plan


def _reconcile_snmp_config(device, payload: dict) -> dict:
    """Apply one frozen SNMP reconciliation through the renderer writer."""
    from .models import (
        NSODeviceManagement,
        NSOSnmpCommunityState,
        NSOSnmpHostState,
        NSOSnmpV3UserState,
    )
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    try:
        management = device.nso_management
    except NSODeviceManagement.DoesNotExist:
        return {"communities": [], "v3_users": [], "hosts": [], "system_info": None}

    active = active_renderer_writer()
    if active is None:
        plan, operations, system_result = _snmp_plan_and_operations(device, payload)
    else:
        plan = active.plan
        _saves, _deletes, operations, system_result = _snmp_reconcile_operations(device, payload, plan.planned_at)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        for operation, instance, update_fields, force_insert in operations:
            if operation == "delete":
                writer.delete(instance)
            else:
                writer.save(instance, update_fields=update_fields, force_insert=force_insert)

    return {
        "communities": list(NSOSnmpCommunityState.objects.filter(management=management)),
        "v3_users": list(NSOSnmpV3UserState.objects.filter(management=management)),
        "hosts": list(NSOSnmpHostState.objects.filter(management=management)),
        "system_info": system_result,
        "last_refreshed_at": payload.get("last_refreshed_at"),
        "refresh_source": payload.get("refresh_source", "never"),
        "snmp_value_compare": _snmp_value_compare_supported(device),
    }


def _snmp_value_compare_supported(device) -> bool:
    """Whether vault-vs-device community value comparison is meaningful here.

    Nokia SR OS stores communities hash2-obfuscated on-device (live-confirmed),
    so the read mirror's hash fingerprints the BLOB, never the plaintext —
    comparison (and harvest) are impossible in principle on timos.
    """
    from .models import NSOPlatformNedMapping

    try:
        if device.platform_id:
            mapping = NSOPlatformNedMapping.objects.filter(platform=device.platform).first()
            if mapping and str(mapping.ned_id).startswith("timos"):
                return False
    except Exception:  # noqa: BLE001 — a mapping problem must never break the tab
        pass
    return True


_TIMOS_LOGGING_SEVERITY = {
    "emergencies": "emergency",
    "alerts": "alert",
    "errors": "error",
    "warnings": "warning",
    "notifications": "notice",
    "informational": "info",
    "debugging": "debug",
    "any": "debug",
}

_JUNOS_LOGGING_SEVERITY = {
    "emergencies": "emergency",
    "alerts": "alert",
    "errors": "error",
    "warnings": "warning",
    "notifications": "notice",
    "informational": "info",
    "debugging": "any",
}

_ARCOS_LOGGING_SEVERITY = {
    "emergencies": "EMERGENCY",
    "alerts": "ALERT",
    "errors": "ERROR",
    "warnings": "WARNING",
    "notifications": "NOTICE",
    "informational": "INFORMATIONAL",
    "debugging": "DEBUG",
    "info": "INFORMATIONAL",
    "any": "DEBUG",
}


def _canonical_logging_field(ned_id: str, field: str, value):
    """Canonicalize operator logging tokens to the value the NED reader returns."""
    if value in (None, ""):
        return value
    token = str(value).lower()
    if field == "severity":
        if ned_id.startswith("timos"):
            return _TIMOS_LOGGING_SEVERITY.get(token, token)
        if ned_id.startswith("juniper-junos-nc"):
            return _JUNOS_LOGGING_SEVERITY.get(token, token)
        if ned_id.startswith("arcos-"):
            return _ARCOS_LOGGING_SEVERITY.get(token, token.upper())
        return token
    if field == "facility":
        if ned_id.startswith("arcos-"):
            return "ALL" if token == "any" else token.upper()
        if ned_id.startswith("cisco-nx-cli") and token == "local7":
            # NX accepts explicit local7 but normalizes it out of running config.
            # Treat the operator token and the reader's omission as one value so
            # apply settles and the next push does not materialize permanent drift.
            return ""
        return token
    return value


def _canonical_logging_intent_field(ned_id: str, field: str, value):
    """Canonicalize a logging token without erasing explicit write semantics."""
    token = str(value).lower() if value not in (None, "") else value
    if field == "facility" and ned_id.startswith("cisco-nx-cli") and token == "local7":
        # The reader omits NX's local7 device default, but the writer needs the
        # explicit token to retract an adopted non-default facility.
        return token
    return _canonical_logging_field(ned_id, field, value)


def _logging_reconcile_operations(device, payload, planned_at):  # noqa: C901
    """Build the deterministic logging write sequence for preflight and apply."""
    from .models import NSODeviceManagement, NSOLoggingHostState, NSOLoggingLevelState
    from .renderer_writer import planned_delete, planned_save

    try:
        management = device.nso_management
    except NSODeviceManagement.DoesNotExist:
        return [], [], [], None

    saves = []
    deletes = []
    operations = []

    def save(instance, *, update_fields=None, force_insert=False, natural_key=()):
        saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            )
        )
        operations.append(("save", instance, update_fields, force_insert))

    def delete(instance):
        deletes.append(planned_delete(instance))
        operations.append(("delete", instance, None, False))

    current_hosts = {
        row.address: row for row in NSOLoggingHostState.objects.filter(management=management).order_by("pk")
    }
    payload_hosts = {
        item.get("address"): item
        for item in (payload.get("hosts") or [])
        if isinstance(item, dict) and item.get("address")
    }
    for address, stale in current_hosts.items():
        if address in payload_hosts:
            continue
        if not sm.is_owned(stale.status):
            delete(stale)
            continue
        candidate = copy.copy(stale)
        candidate.status = sm.on_reconcile(candidate.status, present=False)
        candidate.last_sync_at = planned_at
        save(candidate, update_fields=("status", "last_sync_at"))

    ned_id = _device_ned_id(device)
    suppress_default_port = ned_id.startswith(("timos", "arcos-"))
    host_fields = ("port", "severity", "facility", "transport", "vrf", "source")
    for address, item in payload_hosts.items():
        current = current_hosts.get(address)
        candidate = (
            copy.copy(current) if current is not None else NSOLoggingHostState(management=management, address=address)
        )
        device_values = {
            "port": item.get("port"),
            "severity": item.get("severity") or "",
            "facility": item.get("facility") or "",
            "transport": item.get("transport") or "",
            "vrf": item.get("vrf") or "",
            "source": item.get("source") or "",
        }
        owned = current is not None and sm.is_owned(current.status)
        if owned:
            if not NSOLoggingHostState.objects.filter(
                pk=current.pk,
                address=address,
                status=current.status,
                **{field: getattr(current, field) for field in host_fields},
            ).exists():
                continue
            matches = all(
                (
                    suppress_default_port
                    and field == "port"
                    and value is None
                    and getattr(candidate, field) in (None, 514)
                )
                or _canonical_logging_field(ned_id, field, getattr(candidate, field)) == value
                for field, value in device_values.items()
            )
            candidate.status = sm.on_reconcile(
                candidate.status,
                matches=matches,
                settles_deploying=False,
            )
            fields = ("status", "last_sync_at")
        else:
            for field, value in device_values.items():
                setattr(candidate, field, value)
            candidate.status = sm.on_reconcile(candidate.status)
            fields = None if current is None else (*host_fields, "status", "last_sync_at")
        candidate.last_sync_at = planned_at
        save(
            candidate,
            update_fields=fields,
            force_insert=current is None,
            natural_key=("management", "address"),
        )

    levels_data = payload.get("local_levels") or {}
    current_level = NSOLoggingLevelState.objects.filter(management=management).first()
    level_result = None
    if not levels_data:
        if current_level is not None and sm.is_owned(current_level.status):
            level_result = copy.copy(current_level)
            new_status = sm.on_reconcile(level_result.status, present=False)
            if new_status != level_result.status:
                level_result.status = new_status
                save(level_result, update_fields=("status",))
    else:
        level_result = (
            copy.copy(current_level) if current_level is not None else NSOLoggingLevelState(management=management)
        )
        device_levels = {field: levels_data.get(field) or "" for field in NSOLoggingLevelState.SEVERITY_FIELDS}
        owned = current_level is not None and sm.is_owned(current_level.status)
        if owned:
            if NSOLoggingLevelState.objects.filter(
                pk=current_level.pk,
                status=current_level.status,
                **{field: getattr(current_level, field) for field in device_levels},
            ).exists():
                matches = all(getattr(level_result, field) == value for field, value in device_levels.items())
                level_result.status = sm.on_reconcile(
                    level_result.status,
                    matches=matches,
                    settles_deploying=False,
                )
                level_result.last_sync_at = planned_at
                save(level_result, update_fields=("status", "last_sync_at"))
            else:
                level_result = None
        else:
            for field, value in device_levels.items():
                setattr(level_result, field, value)
            level_result.status = sm.on_reconcile(level_result.status)
            level_result.last_sync_at = planned_at
            fields = (
                None if current_level is None else (*NSOLoggingLevelState.SEVERITY_FIELDS, "status", "last_sync_at")
            )
            save(
                level_result,
                update_fields=fields,
                force_insert=current_level is None,
                natural_key=("management",),
            )

    return saves, deletes, operations, level_result


def _logging_plan_and_operations(device, payload):
    from django.utils import timezone

    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    saves, deletes, operations, level_result = _logging_reconcile_operations(device, payload, planned_at)
    plan = RendererMutationPlan.build(saves=saves, deletes=deletes, planned_at=planned_at)
    return plan, operations, level_result


def logging_reconcile_plan(device, payload):
    """Freeze every logging overlay write before reconciliation."""
    plan, _operations, _level_result = _logging_plan_and_operations(device, payload)
    return plan


def _reconcile_logging_config(device, payload: dict) -> dict:
    """Apply one frozen logging reconciliation through the renderer writer."""
    from .models import NSODeviceManagement, NSOLoggingHostState
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    try:
        management = device.nso_management
    except NSODeviceManagement.DoesNotExist:
        return {"hosts": [], "local_levels": None, "last_refreshed_at": None, "refresh_source": "never"}

    active = active_renderer_writer()
    if active is None:
        plan, operations, level_result = _logging_plan_and_operations(device, payload)
    else:
        plan = active.plan
        _saves, _deletes, operations, level_result = _logging_reconcile_operations(device, payload, plan.planned_at)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        for operation, instance, update_fields, force_insert in operations:
            if operation == "delete":
                writer.delete(instance)
            else:
                writer.save(instance, update_fields=update_fields, force_insert=force_insert)

    return {
        "hosts": list(NSOLoggingHostState.objects.filter(management=management)),
        "local_levels": level_result,
        "last_refreshed_at": payload.get("last_refreshed_at"),
        "refresh_source": payload.get("refresh_source", "never"),
    }


def _static_route_metric(entry: dict, device=None) -> int:
    """Clamp the NSO metric to StaticRoute's 0..255 PositiveSmallInt constraint.

    Junos route metric/preference can exceed 255; an out-of-range value would fail
    full_clean() and drop the route, so fall back to the model default (1).
    """
    if "metric" not in entry and device is not None and _device_uses_timos_ned(device):
        return 5
    m = entry.get("metric")
    return m if isinstance(m, int) and 0 <= m <= 255 else 1


_STATIC_ROUTE_MIRROR_FIELDS = ("nso_vrf", "nso_prefix", "nso_next_hop", "last_sync_at")


def static_route_reconcile_plan(device, payload: dict):
    """Freeze every native route, assignment, and overlay write for one mirror pass."""
    from .renderer_writer import RendererMutationPlan

    planned_at = timezone.now()
    try:
        saves, deletes, m2m_writes, _operations = _static_route_reconcile_operations(device, payload, planned_at)
    except ImportError:
        return RendererMutationPlan.build(planned_at=planned_at)
    return RendererMutationPlan.build(
        saves=saves,
        deletes=deletes,
        m2m_writes=m2m_writes,
        planned_at=planned_at,
    )


def _static_route_reconcile_operations(device, payload, planned_at):  # noqa: C901
    """Build the deterministic static-route write sequence for preflight and apply."""
    from ipam.models import VRF
    from netbox_routing.models import StaticRoute

    from .models import NSODeviceManagement, NSOStaticRouteState
    from .renderer_writer import planned_delete, planned_m2m_add, planned_m2m_set, planned_save

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return [], [], [], []

    auto_create = _adapter_setting("static_route_auto_create")
    vrf_auto_create = _adapter_setting("vrf_auto_create")
    vrfs = {row.name: row for row in VRF.objects.order_by("pk")}
    planned_routes = {}
    states = {
        row.static_route_id: row
        for row in NSOStaticRouteState.objects.filter(management=management)
        .select_related("static_route", "static_route__vrf")
        .prefetch_related("static_route__devices")
        .order_by("pk")
    }
    saves = []
    deletes = []
    m2m_writes = []
    operations = []
    seen_state_pks = set()

    def save(instance, *, update_fields=None, force_insert=False, natural_key=()):
        saves.append(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            )
        )
        operations.append(("save", instance, update_fields, force_insert, None))

    def route_identity(vrf_name, prefix, next_hop, interface_next_hop):
        return vrf_name, prefix, next_hop, interface_next_hop if next_hop is None else None

    routes = payload.get("routes", []) if isinstance(payload, dict) else []
    routes = routes if isinstance(routes, list) else []
    for entry in routes:
        if not isinstance(entry, dict):
            continue
        vrf_name = entry.get("vrf") or ""
        prefix = entry.get("prefix") or ""
        next_hop = entry.get("next_hop") or None
        interface_next_hop = entry.get("interface_next_hop") or None
        if not prefix or (not next_hop and not interface_next_hop):
            continue
        vrf = None
        if vrf_name:
            vrf = vrfs.get(vrf_name)
            if vrf is None and not vrf_auto_create:
                logger.warning("VRF %r not found in NetBox; skipping route %s", vrf_name, prefix)
                continue
            if vrf is None:
                vrf = VRF(name=vrf_name)
                vrf.full_clean()
                save(vrf, force_insert=True, natural_key=("name",))
                vrfs[vrf_name] = vrf

        identity = route_identity(vrf_name, prefix, next_hop, interface_next_hop)
        route = planned_routes.get(identity)
        if route is None:
            lookup = {"vrf": vrf, "prefix": prefix, "next_hop": next_hop}
            if next_hop is None:
                lookup["interface_next_hop"] = interface_next_hop
            route = None if vrf is not None and vrf.pk is None else StaticRoute.objects.filter(**lookup).first()
        created = route is None
        if created and not auto_create:
            logger.debug("StaticRoute %s not found and auto_create=False; skipping device %s", prefix, device)
            continue
        if created:
            route = StaticRoute(
                vrf=vrf,
                prefix=prefix,
                next_hop=next_hop,
                interface_next_hop=interface_next_hop,
                metric=_static_route_metric(entry, device),
                permanent=bool(entry.get("permanent", False)),
                tag=entry.get("tag"),
                name=entry.get("name") or "",
            )
            try:
                route.full_clean(exclude=("vrf",) if vrf is not None and vrf.pk is None else ())
            except Exception as exc:
                logger.warning("Could not create StaticRoute %s: %s", prefix, exc)
                continue
            save(
                route,
                force_insert=True,
                natural_key=("vrf", "prefix", "next_hop", "interface_next_hop"),
            )
        planned_routes[identity] = route

        current_state = None if created else states.get(route.pk)
        state = (
            NSOStaticRouteState(management=management, static_route=route, status="unknown")
            if current_state is None
            else copy.copy(current_state)
        )
        state.nso_vrf = vrf_name
        state.nso_prefix = prefix
        state.nso_next_hop = next_hop or ""
        state.last_sync_at = planned_at

        assigned = () if created else tuple(route.devices.order_by("pk"))
        on_device = created or any(row.pk == device.pk for row in assigned)
        if not on_device and auto_create:
            m2m_writes.append(planned_m2m_add(route, "devices", (device,)))
            operations.append(("m2m_add", route, None, False, (device,)))
            on_device = True
        elif created:
            m2m_writes.append(planned_m2m_add(route, "devices", (device,)))
            operations.append(("m2m_add", route, None, False, (device,)))

        state.status = sm.on_reconcile(
            state.status,
            matches=(
                on_device and route.metric == _static_route_metric(entry, device) and route.tag == entry.get("tag")
            ),
            conflict=not on_device,
            settles_owned=False,
            settles_deploying=False,
        )
        state_created = current_state is None
        save(
            state,
            update_fields=None if state_created else (*_STATIC_ROUTE_MIRROR_FIELDS, "status"),
            force_insert=state_created,
            natural_key=("management", "static_route"),
        )
        if current_state is not None:
            seen_state_pks.add(current_state.pk)

    for current in states.values():
        if current.pk in seen_state_pks:
            continue
        if sm.is_owned(current.status):
            new_status = sm.on_reconcile(current.status, present=False)
            if new_status != current.status:
                candidate = copy.copy(current)
                candidate.status = new_status
                save(candidate, update_fields=("status",))
            continue
        remaining = tuple(row for row in current.static_route.devices.order_by("pk") if row.pk != device.pk)
        if current.static_route.devices.filter(pk=device.pk).exists():
            m2m_writes.append(planned_m2m_set(current.static_route, "devices", remaining))
            operations.append(("m2m_set", current.static_route, None, False, remaining))
        deletes.append(planned_delete(current))
        operations.append(("delete", current, None, False, None))

    return saves, deletes, m2m_writes, operations


@mirror_reconciler
def _reconcile_static_routes(device, payload: dict) -> list:
    """Apply one frozen static-route reconciliation through the renderer writer."""
    from .models import NSODeviceManagement, NSOStaticRouteState
    from .renderer_writer import active_renderer_writer, renderer_mirror_writes, renderer_writes
    from .signals import suppress_intent_push

    management = NSODeviceManagement.objects.filter(device=device).first()
    if management is None:
        return []
    active = active_renderer_writer()
    plan = active.plan if active is not None else static_route_reconcile_plan(device, payload)
    mutation = contextlib.nullcontext(active)
    if active is None:
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer, suppress_intent_push():
        _saves, _deletes, _m2m_writes, operations = _static_route_reconcile_operations(
            device,
            payload,
            plan.planned_at,
        )
        for operation, instance, update_fields, force_insert, related in operations:
            if operation == "save":
                writer.save(instance, update_fields=update_fields, force_insert=force_insert)
            elif operation == "m2m_add":
                writer.m2m_add(instance, "devices", related)
            elif operation == "m2m_set":
                writer.m2m_set(instance, "devices", related)
            else:
                writer.delete(instance)

    return list(
        NSOStaticRouteState.objects.filter(management=management).select_related("static_route", "static_route__vrf")
    )


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


def _reconcile_child_levels(
    model,
    parent_field,
    parent,
    cols,
    levels,
    *,
    write: bool = True,
) -> bool:
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
            if col not in data:
                if row.pk is not None and getattr(row, col, None) is not None:
                    matches = False
                    if write:
                        setattr(row, col, _model_absent_value(row, col))
                        changed = True
                continue
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


# ISISPrefixSID columns mirrored per (interface, algorithm). Unlike the levels
# reconcile these are mirrored EXACTLY (absent -> None): the export emits an
# index XOR an absolute label (never both) plus only the flags a device sets, so
# clearing the counterpart keeps the sid_index/sid_label mutual-exclusion invariant.
_ISIS_PREFIX_SID_COLS = ("sid_index", "sid_label", "n_flag", "no_php", "explicit_null", "readvertise")


def _reconcile_isis_prefix_sids(ri, prefix_sids, *, write: bool = True) -> bool:
    """Per-loopback ISISPrefixSID rows for the ISISInterface *ri*, keyed by algorithm.

    Clobber-safe brownfield mirror (create/update present, delete dropped); no-op
    (matches=True) when the fork lacks ISISPrefixSID or *ri* is None. Runs under
    suppress_intent_push so seeding never trips the accept->push signal.
    """
    if ri is None:
        return True
    try:
        from netbox_routing.models import ISISPrefixSID
    except Exception:
        return True
    incoming = {}
    for ps in prefix_sids or []:
        try:
            incoming[int(ps["algorithm"])] = ps
        except (KeyError, TypeError, ValueError):
            continue
    existing = {row.algorithm: row for row in ISISPrefixSID.objects.filter(interface=ri)}
    matches = True
    from .signals import suppress_intent_push

    with suppress_intent_push():
        for algo, data in incoming.items():
            row = existing.get(algo) or ISISPrefixSID(interface=ri, algorithm=algo)
            changed = row.pk is None
            for col in _ISIS_PREFIX_SID_COLS:
                val = data.get(col)
                if getattr(row, col, None) != val:
                    setattr(row, col, val)
                    changed = True
            if changed:
                matches = False
                if write:
                    row.save()
        for algo, row in existing.items():
            if algo not in incoming:
                matches = False
                if write:
                    row.delete()
    return matches


# Instance-level ISISSegmentRouting columns mirrored from the device SR bag.
# ``node_sid_index``/``node_sid_label`` (+ their v6 twins) were refactored OUT of
# ISISSegmentRouting into the per-loopback ``ISISPrefixSID`` child on the
# netbox-routing side. The network-state export is still instance-level, so the
# payload may keep sending them — they are ignored here (their per-loopback home
# is populated once the readers emit per-interface prefix-SIDs). ``srlb_*`` and
# ``srv6_enabled`` are the newer surviving columns. Filtering against the
# installed model's real fields tolerates fork drift in either direction, so
# ``save(update_fields=...)`` never names a column the model lacks — a stale
# name there raises ValueError → HTTP 500 on accept.
_SR_INSTANCE_COLS = (
    "enabled",
    "srv6_enabled",
    "prefix_sid_range",
    "srgb_start",
    "srgb_range",
    "srlb_start",
    "srlb_range",
    "maximum_sid_depth",
    "tunnel_table_pref",
)


def _sr_instance_cols(model) -> tuple[str, ...]:
    """``_SR_INSTANCE_COLS`` restricted to the concrete fields *model* actually has."""
    names = {f.name for f in model._meta.get_fields()}
    return tuple(c for c in _SR_INSTANCE_COLS if c in names)


def _model_absent_value(obj, field_name):
    """Return the concrete model's representation for an omitted device value."""
    field = obj._meta.get_field(field_name)
    if field.null:
        return None
    if field.has_default():
        return field.get_default()
    raise ValueError(f"{type(obj).__name__}.{field_name} cannot represent an omitted device value")


def _sync_isis_segment_routing_values(row, cols, values, *, write: bool) -> bool:
    """Compare/mirror one existing SR row, treating omitted values as absent."""
    matches = True
    fields = []
    for col in cols:
        val = values.get(col)
        if val is None:
            val = _model_absent_value(row, col)
        if getattr(row, col, None) != val:
            matches = False
            if write:
                setattr(row, col, val)
                fields.append(col)
    if fields:
        row.save(update_fields=fields)
    return matches


def _reconcile_isis_segment_routing(
    inst,
    sr: dict | None,
    *,
    reported: bool | None = None,
    configured: bool | None = None,
    write: bool = True,
) -> bool:
    """Upsert the netbox_routing ISISSegmentRouting (1:1) for *inst* from *sr*.

    Clobber-safe: mirrors the device only when ``write`` (seeding); otherwise touches
    nothing. Returns ``matches`` = the SR row already equals the device. No-op
    (matches=True) when the fork lacks ISISSegmentRouting or *inst* is None.
    """
    if inst is None:
        return True
    try:
        from netbox_routing.models import ISISSegmentRouting
    except (ImportError, AttributeError):
        return True
    if reported is True and configured is False:
        exists = ISISSegmentRouting.objects.filter(instance=inst).exists()
        if exists and write:
            ISISSegmentRouting.objects.filter(instance=inst).delete()
        return not exists
    if sr is None and not (reported is True and configured is True):
        # Legacy adapters did not report SR provenance. Preserve any child and do
        # not let unknown payload shape block the rest of the process reconcile.
        return True
    if not sr:
        # A configured presence container may have no modeled child values.
        row = ISISSegmentRouting.objects.filter(instance=inst).first()
        if row is None:
            if write:
                ISISSegmentRouting.objects.create(instance=inst)
            return False
        return _sync_isis_segment_routing_values(
            row,
            _sr_instance_cols(ISISSegmentRouting),
            {},
            write=write,
        )
    cols = _sr_instance_cols(ISISSegmentRouting)
    row, created = ISISSegmentRouting.objects.get_or_create(instance=inst) if write else (None, False)
    if row is None:
        row = ISISSegmentRouting.objects.filter(instance=inst).first()
    if row is None:
        return False
    values_match = _sync_isis_segment_routing_values(row, cols, sr, write=write)
    return not created and values_match


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


# ISISSRv6Locator columns mirrored per (instance, name). ``prefix`` is required on
# the model (IPNetworkField, NOT NULL), so a locator without a prefix is skipped; it
# reads back as an IPNetwork, so it is compared stringified to avoid phantom drift
# against the incoming CIDR string.
_ISIS_SRV6_LOCATOR_COLS = (
    "prefix",
    "algorithm",
    "is_anycast",
    "is_micro_segment",
    "flavor",
    "block_length",
    "node_length",
    "function_length",
    "argument_length",
    "isis_level",
    "enabled",
)


def _isis_srv6_locator_value(data, col, omitted_defaults):
    value = data.get(col)
    return omitted_defaults.get(col) if value is None and col in omitted_defaults else value


def _sync_isis_srv6_locator_row(row, data, omitted_defaults) -> tuple[bool, bool]:
    """Return ``(changed, matches)`` while applying reported locator columns."""
    changed = row.pk is None
    matches = True
    for col in _ISIS_SRV6_LOCATOR_COLS:
        val = _isis_srv6_locator_value(data, col, omitted_defaults)
        if val is None:
            if row.pk is not None and getattr(row, col, None) not in (None, ""):
                matches = False
                setattr(row, col, _model_absent_value(row, col))
                changed = True
            continue
        cur = getattr(row, col, None)
        differs = (str(cur) != str(val)) if col == "prefix" else (cur != val)
        if differs:
            setattr(row, col, val)
            changed = True
    return changed, matches


def _reconcile_isis_srv6_locators(inst, srv6_locators, *, write: bool = True) -> bool:
    """ISISSRv6Locator rows for *inst* from the adapter's srv6-locator list (keyed by name).

    Clobber-safe brownfield mirror (create/update present, delete dropped); no-op
    (matches=True) when the fork lacks ISISSRv6Locator or *inst* is None. A locator
    with no resolvable prefix is skipped (prefix is required on the model). Runs under
    suppress_intent_push so seeding never trips the accept->push signal.
    """
    if inst is None:
        return True
    try:
        from netbox_routing.models import ISISSRv6Locator
    except Exception:
        return True
    incoming = {}
    for loc in srv6_locators or []:
        try:
            name = str(loc["name"])
        except (KeyError, TypeError):
            continue
        if name and loc.get("prefix"):  # prefix is required (IPNetworkField, NOT NULL)
            incoming[name] = loc
    existing = {row.name: row for row in ISISSRv6Locator.objects.filter(instance=inst)}
    omitted_defaults = _isis_srv6_locator_omitted_defaults(inst.device)
    matches = True
    from .signals import suppress_intent_push

    with suppress_intent_push():
        for name, data in incoming.items():
            row = existing.get(name) or ISISSRv6Locator(instance=inst, name=name)
            changed, row_matches = _sync_isis_srv6_locator_row(row, data, omitted_defaults)
            matches = matches and row_matches
            if changed:
                matches = False
                if write:
                    row.save()
        for name, row in existing.items():
            if name not in incoming:
                matches = False
                if write:
                    row.delete()
    return matches


_ISIS_LEVEL_COLS = ("default_metric", "wide_metrics_only", "preference", "labeled_preference", "disabled", "auth_type")
_ISIS_IFACE_LEVEL_COLS = ("metric", "hello_interval", "hello_multiplier", "priority", "passive")
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
    "frr_enabled",
    "frr_protection",
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
    # FRR (#83), read-only mirror: frr_enabled is tri-state (None = unconfigured,
    # False = explicit device-side disable/exclude — a real value, mirrored verbatim);
    # frr_protection is a NOT NULL CharField, so absent mirrors as ''.
    if hasattr(ri, "frr_enabled"):
        rf.append(("frr_enabled", entry.get("frr_enabled")))
    if hasattr(ri, "frr_protection"):
        rf.append(("frr_protection", entry.get("frr_protection") or ""))
    if hasattr(ri, "csnp_interval") and "csnp_interval" in entry:
        rf.append(("csnp_interval", entry.get("csnp_interval")))
    if hasattr(ri, "retransmit_interval") and "retransmit_interval" in entry:
        rf.append(("retransmit_interval", entry.get("retransmit_interval")))
    if hasattr(ri, "lsp_interval") and "lsp_interval" in entry:
        rf.append(("lsp_interval", entry.get("lsp_interval")))
    if hasattr(ri, "mesh_group"):
        rf.append(("mesh_group", entry.get("mesh_group") or ""))
    return rf


def _isis_device_matches_intent(entry, state, ri=None, device=None) -> bool:
    """Return True when the device (adapter *entry*) has caught up to the owned overlay intent.

    Used for owned IS-IS rows where the clobber guard keeps the overlay == netbox-routing
    object, so the normal overlay-vs-object match can't tell whether the *device* has the
    change yet. Mirrors the OSPF device-vs-netbox status semantics.
    """
    circuit_matches = (entry.get("circuit_type") or "") == (state.circuit_type or "")
    long_scalar_fields = ("csnp_interval", "retransmit_interval", "lsp_interval")
    long_scalars_match = all(
        entry.get(field) == getattr(ri, field)
        if field in entry
        else (getattr(ri, field) is None if device is not None and _device_uses_timos_ned(device) else True)
        for field in long_scalar_fields
        if ri is not None and hasattr(ri, field)
    )
    return (
        entry.get("metric") == state.metric
        and (entry.get("network_type") or "") == (state.network_type or "")
        and circuit_matches
        and long_scalars_match
        and bool(entry.get("passive", False)) == bool(state.passive)
        # tri-state: a None intent expresses no opinion, so it never blocks in_sync
        # (we don't own the device's BFD); True/False must match the device verbatim.
        and (state.bfd_enabled is None or bool(entry.get("bfd_enabled")) == bool(state.bfd_enabled))
        # FRR (#83): the same tri-state contract as BFD; the protection kind only
        # blocks in_sync when the intent asserts one.
        and (state.frr_enabled is None or bool(entry.get("frr_enabled")) == bool(state.frr_enabled))
        and (not state.frr_protection or (entry.get("frr_protection") or "") == state.frr_protection)
    )


def _device_uses_timos_ned(device) -> bool:
    return _device_ned_id(device).startswith("timos")


def _device_ned_id(device) -> str:
    from .models import NSOPlatformNedMapping

    if not device.platform_id:
        return ""
    mapping = NSOPlatformNedMapping.objects.filter(platform_id=device.platform_id).first()
    return str(mapping.ned_id) if mapping else ""


def _isis_instance_omitted_defaults(device) -> dict:
    ned_id = _device_ned_id(device)
    if ned_id.startswith("timos"):
        return {
            "ignore_attached_bit": False,
            "suppress_attached_bit": False,
        }
    if ned_id.startswith("arcos-"):
        return {
            "overload_bit": False,
            "ignore_attached_bit": False,
            "suppress_attached_bit": False,
        }
    return {}


def _isis_srv6_locator_omitted_defaults(device) -> dict:
    if _device_ned_id(device).startswith("arcos-"):
        return {
            "is_micro_segment": False,
        }
    return {}


def _isis_process_device_matches_intent(entry, state, device=None, inst=None) -> bool:
    """Return whether device-reported process scalars match an owned overlay."""
    for field in (
        "net",
        "is_type",
        "metric_style",
        "area_auth_type",
        "domain_auth_type",
        "fast_reroute",
    ):
        if (entry.get(field) or "") != getattr(state, field):
            return False
    for prefix in ("area", "domain"):
        reported = bool(entry.get(f"{prefix}_auth_present", False))
        intended = bool(getattr(state, f"{prefix}_auth_present") or getattr(state, f"{prefix}_auth_key"))
        if reported != intended:
            return False
    for field in ("overload_bit", "microloop_avoidance"):
        intended = getattr(state, field)
        if intended is not None and bool(entry.get(field, False)) != intended:
            return False
    if inst is not None:
        omitted_defaults = _isis_instance_omitted_defaults(device)
        for field in _ISIS_INSTANCE_SCALAR_ATTRS:
            if field in {"fast_reroute", "microloop_avoidance"}:
                # These are already compared above. In particular, an omitted
                # true-only microloop key confirms an owned False value.
                continue
            if not hasattr(inst, field):
                continue
            if field in entry:
                reported = entry.get(field)
                if reported is None:
                    reported = _model_absent_value(inst, field)
            elif field in omitted_defaults:
                reported = omitted_defaults[field]
            else:
                reported = _model_absent_value(inst, field)
            if getattr(inst, field) != reported:
                return False
    return True


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
    children_match = _isis_interface_children_match(entry, ri, write=write)
    if write:
        return True
    return scalar_matches and children_match


def _isis_interface_children_match(entry, ri, *, write: bool) -> bool:
    """Compare/mirror settings, levels, and prefix-SIDs without top-level defaults."""
    settings_matches = _reconcile_isis_settings(ri, entry.get("settings"), write=write)
    try:
        from netbox_routing.models import ISISInterfaceLevel
    except (ImportError, AttributeError):
        levels_matches = True
    else:
        levels_matches = _reconcile_child_levels(
            ISISInterfaceLevel,
            "interface",
            ri,
            _ISIS_IFACE_LEVEL_COLS,
            entry.get("levels"),
            write=write,
        )
    prefix_sid_matches = _reconcile_isis_prefix_sids(ri, entry.get("prefix_sids"), write=write)
    return settings_matches and levels_matches and prefix_sid_matches


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
    try:
        from netbox_routing.models import ISISPrefixSID

        content["prefix_sids"] = sorted(
            (
                {"algorithm": r.algorithm, **{c: getattr(r, c, None) for c in _ISIS_PREFIX_SID_COLS}}
                for r in ISISPrefixSID.objects.filter(interface=ri)
            ),
            key=lambda x: x["algorithm"],
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
    if sm.is_owned(state.status):
        return ri, False, base
    if (not base) or _isis_interface_object_hash(ri) == base:
        mirrored = _isis_interface_pass(state, entry, ri, bfd_enabled, write=True)
        if mirrored:
            return ri, True, _isis_interface_object_hash(ri)
    return ri, False, base


@mirror_reconciler
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
            # tri-state, mirror the device verbatim (None when the NED reports no BFD)
            state.bfd_enabled = entry.get("bfd_enabled")
            # FRR (#83): same tri-state mirror; protection kind '' when unreported.
            state.frr_enabled = entry.get("frr_enabled")
            state.frr_protection = entry.get("frr_protection") or ""
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
            iface_matches = _isis_device_matches_intent(
                entry,
                state,
                state.isis_interface,
                device,
            ) and _isis_interface_children_match(entry, state.isis_interface, write=False)
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
    "suppress_attached_bit",
    "ignore_attached_bit",
    "fast_reroute",
    "microloop_avoidance",
    "distance",
    "maximum_paths",
    "reference_bandwidth",
)


def _sync_isis_instance_long_scalars(inst, entry, omitted_defaults, *, write: bool) -> tuple[bool, list[str]]:
    matches = True
    fields = []
    for attr in _ISIS_INSTANCE_SCALAR_ATTRS:
        if not hasattr(inst, attr):
            continue
        if attr not in entry:
            if attr not in omitted_defaults:
                # Several process fields are emitted only when the NED reports
                # them. Their absence is unknown, not an instruction to erase a
                # previously mirrored value.
                continue
            val = omitted_defaults[attr]
        else:
            val = entry.get(attr)
        if val is None:
            absent = _model_absent_value(inst, attr)
            if getattr(inst, attr) != absent:
                matches = False
                if write:
                    setattr(inst, attr, absent)
                    fields.append(attr)
            continue
        if getattr(inst, attr) != val:
            matches = False
            if write:
                setattr(inst, attr, val)
                fields.append(attr)
    return matches, fields


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
        # is_type is provenance-explicit and corrected readers omit an unset
        # schema default. On an unowned mirror, blank must therefore migrate an
        # old reader's fabricated level-1-2 value out of the linked object.
        # The remaining scalars keep their existing absence/no-op semantics.
        should_compare = bool(val) or attr == "is_type"
        if should_compare and getattr(inst, attr) != val:
            scalar_matches = False
            if write:
                setattr(inst, attr, val)
                inst_fields.append(attr)
    if state.overload_bit is not None and inst.overload_bit != state.overload_bit:
        scalar_matches = False
        if write:
            inst.overload_bit = state.overload_bit
            inst_fields.append("overload_bit")
    omitted_defaults = _isis_instance_omitted_defaults(inst.device)
    long_matches, long_fields = _sync_isis_instance_long_scalars(
        inst,
        entry,
        omitted_defaults,
        write=write,
    )
    scalar_matches = scalar_matches and long_matches
    inst_fields.extend(long_fields)
    if inst_fields:
        inst.save(update_fields=inst_fields)

    settings_matches = _reconcile_isis_settings(inst, entry.get("settings"), write=write)
    try:
        from netbox_routing.models import ISISLevel
    except (ImportError, AttributeError):
        levels_matches = True
    else:
        levels_matches = _reconcile_child_levels(
            ISISLevel,
            "instance",
            inst,
            _ISIS_LEVEL_COLS,
            entry.get("levels"),
            write=write,
        )
    sr_matches = _reconcile_isis_segment_routing(
        inst,
        entry.get("segment_routing"),
        reported=entry.get("segment_routing_reported"),
        configured=entry.get("segment_routing_configured"),
        write=write,
    )
    flex_matches = _reconcile_isis_flex_algos(inst, entry.get("flex_algos"), write=write)
    srv6_matches = _reconcile_isis_srv6_locators(inst, entry.get("srv6_locators"), write=write)
    if write:
        return True
    return scalar_matches and settings_matches and levels_matches and sr_matches and flex_matches and srv6_matches


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
        content["sr"] = {c: getattr(sr, c, None) for c in _sr_instance_cols(ISISSegmentRouting)} if sr else None
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
    try:
        from netbox_routing.models import ISISSRv6Locator

        # prefix is an IPNetwork; stringify so the hash is stable vs the device CIDR string.
        content["srv6"] = sorted(
            (
                {
                    "name": r.name,
                    **{
                        c: (str(getattr(r, c)) if c == "prefix" else getattr(r, c, None))
                        for c in _ISIS_SRV6_LOCATOR_COLS
                    },
                }
                for r in ISISSRv6Locator.objects.filter(instance=inst)
            ),
            key=lambda x: x["name"],
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
    if sm.is_owned(state.status):
        return inst, False, base
    if (not base) or _isis_instance_object_hash(inst) == base:
        # first import (seed) OR device moved while the object was untouched (mirror)
        mirrored = _isis_instance_pass(state, entry, inst, write=True)
        if mirrored:
            return inst, True, _isis_instance_object_hash(inst)
    return inst, False, base  # operator edited → changed (edit preserved)


@mirror_reconciler
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
        # Owned rows hold the intent pushed back to NSO. An omitted configured-only
        # field (for example a default is-type) is device provenance, not permission
        # to erase accepted intent from the overlay and the next push snapshot.
        if not sm.is_owned(state.status):
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
            # FRR (#83): flavor '' when unreported; microloop tri-state verbatim.
            state.fast_reroute = entry.get("fast_reroute") or ""
            state.microloop_avoidance = entry.get("microloop_avoidance")
        state.last_sync_at = now

        # 3-way merge over the whole ISIS graph: device changes auto-mirror when the
        # object is untouched (object_hash == base); operator edits survive + surface
        # as 'changed'. device_base_hash persists the agreed object snapshot.
        state.isis_instance, inst_matches, new_base = _sync_routing_isis_instance(
            device, tag, state, entry, state.device_base_hash
        )
        state.device_base_hash = new_base
        if sm.is_owned(state.status):
            # The object merge compares NetBox intent with its linked object. Owned
            # status must additionally be gated by the device report; otherwise an
            # omitted configured-only default falsely settles accepted intent in_sync.
            inst_matches = inst_matches and _isis_process_device_matches_intent(
                entry,
                state,
                device,
                state.isis_instance,
            )
        state.status = sm.on_reconcile(state.status, matches=inst_matches)
        state.save()
        seen_tags.add(tag)

    for stale in NSOISISInstanceState.objects.filter(management=mgmt):
        if stale.process_tag not in seen_tags:
            # vestigial = status-only ghost (no linked netbox-routing ISISInstance)
            sm.finalise_stale_overlay(stale, vestigial=stale.isis_instance_id is None)

    return list(NSOISISInstanceState.objects.filter(management=mgmt).select_related("isis_instance"))


def _clean_router_id(value) -> str:
    """Normalise an exported router-id, treating the literal ``"None"`` as absent.

    A process the device runs without an explicit router-id can stringify to ``"None"``
    upstream — truthy, but not a valid IP. Returns ``""`` for that (and any falsy value) so
    it never reaches netbox-routing's router_id IPAddressField (which 500s on ``"None"``).
    """
    if not value or str(value).strip().lower() == "none":
        return ""
    return value


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


@mirror_reconciler
def _reconcile_ospf(device, payload: dict) -> dict:
    """Apply the exact OSPF mutation plan."""
    from .ospf_reconciler import reconcile_ospf

    return reconcile_ospf(device, payload)


def ospf_reconcile_plan(device, payload):
    """Expose OSPF preflight planning beside the compatibility reconcile entry point."""
    from .ospf_reconciler import ospf_reconcile_plan as build_plan

    return build_plan(device, payload)


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
        from .derived_intent import get_sentinel_templates

        templates = get_sentinel_templates()
        match = is_managed_description(interface.description or "", templates)
        return self.render(
            "netbox_nso_plugin/interface_nso_badge.html",
            extra_context={
                "nso_states": states,
                "status_badge": _STATUS_BADGE,
                "derived_intent_match": match,
            },
        )


class ISISWritePolicyPanel(PluginTemplateExtension):
    """Read-only-mirror warning on the netbox-routing IS-IS object pages (#78).

    The reconciler mirrors far more IS-IS config into netbox-routing than the intent
    push carries; editing a mirror-only field there and clicking Accept silently
    re-pushes the same snapshot (the change never reaches the device). This panel —
    rendered exactly where those edits happen — names which fields are read-only and
    which genuinely push, from the ``isis_write_policy`` registry (whose truthfulness
    an integrity test proves against a captured payload).
    """

    models = ["netbox_routing.isisinstance", "netbox_routing.isisinterface"]

    def full_width_page(self):
        """Render the write-path coverage card when the object's device is NSO-managed.

        full_width_page, not right_page — the netbox-routing object pages (panel-based
        UI) render plugin full-width content but no right-column plugin block (same
        reason RoutePolicyNSODevices is full-width).
        """
        from .isis_write_policy import ISIS_CHILD_NOTES, ISIS_PUSHED_FIELDS, ISIS_READ_ONLY_FIELDS
        from .models import NSODeviceManagement

        obj = self.context["object"]
        kind = "isis_instance" if obj._meta.model_name == "isisinstance" else "isis_interface"
        device = getattr(obj, "device", None) or getattr(getattr(obj, "interface", None), "device", None)
        if device is None or not NSODeviceManagement.objects.filter(device=device).exists():
            return ""
        return self.render(
            "netbox_nso_plugin/isis_write_policy.html",
            extra_context={
                "read_only_fields": ISIS_READ_ONLY_FIELDS[kind],
                "writable_fields": ISIS_PUSHED_FIELDS[kind],
                "child_notes": ISIS_CHILD_NOTES[kind],
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

        adapter_down = False  # circuit breaker: the FIRST adapter failure short-circuits the rest
        for state in states:
            community_members, set_keys, match_keys, aspath_names = _preflight_constructs(state.family, obj)
            if not (community_members or set_keys or match_keys or aspath_names):
                # prefix-list carries nothing flaggable — universally representable.
                state.capability = {"state": "supported", "unsupported": []}
                continue
            adapter_id = getattr(state.management, "adapter_device_id", None)
            if not adapter_id or adapter_down:
                state.capability = {"state": "unknown", "unsupported": []}
                continue
            try:
                # raise_on_error so a slow/unreachable adapter costs ONE timeout for the whole
                # panel, not one per device row (this runs on every route-policy detail render).
                verdict = client.preflight_route_policy(
                    adapter_id,
                    community_members,
                    set_keys,
                    match_keys,
                    aspath_names,
                    refresh=False,
                    raise_on_error=True,
                )
            except client.AdapterError:
                adapter_down = True
                state.capability = {"state": "unknown", "unsupported": []}
                continue
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


template_extensions = [InterfaceNSOBadge, RoutePolicyNSODevices, ISISWritePolicyPanel]
