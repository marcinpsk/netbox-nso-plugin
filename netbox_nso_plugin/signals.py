# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Signal handlers for NSODeviceManagement scope propagation and intent push."""

import logging
import threading

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)

# Thread-local flag set by ip_autoassign._suppress_ip_intent_push() to prevent
# premature intent pushes during P2P IPAddress pair reservation.
_p2p_allocation_active = threading.local()

# Header the nso-adapter sets on every write it makes to NetBox. Such writes are
# imports/applies (adapter-origin), NOT operator intent edits, so the Decision-G
# signal must not promote them to 'accepted' or push them back as intent.
_ADAPTER_IMPORT_HEADER = "X-NSO-Adapter-Import"


def _is_adapter_origin_write() -> bool:
    """Return True if the current request is an adapter-origin write (carries the import header).

    Origin — not content — is the correct discriminator: an import that writes a
    *changed* value and an operator edit to the *same* value leave identical
    interface state; only the request origin distinguishes them. The adapter runs
    in a separate process, so a thread-local can't reach here; the marker rides on
    the HTTP request via NetBox's current_request contextvar.
    """
    try:
        from netbox.context import current_request

        request = current_request.get()
    except Exception:
        return False
    if request is None:
        return False
    return request.headers.get(_ADAPTER_IMPORT_HEADER) is not None


@receiver(post_save, sender="netbox_nso_plugin.NSODeviceManagement")
def sync_scope_to_adapter(sender, instance, created, **kwargs):
    """Push device + scope to the adapter whenever an NSODeviceManagement record is saved.

    After setting scope, calls sync-notify so the adapter starts an immediate sync
    rather than waiting for the next scheduled poll.
    """
    from . import adapter_client as client

    try:
        if created or instance.adapter_device_id is None:
            result = client.onboard_device(
                nso_instance=instance.nso_instance.adapter_instance_id,
                nso_device_name=instance.nso_device_name,
                netbox_device_id=instance.device_id,
            )
            type(instance).objects.filter(pk=instance.pk).update(adapter_device_id=result["id"])
            instance.adapter_device_id = result["id"]
        else:
            client.patch_device(
                adapter_device_id=instance.adapter_device_id,
                nso_instance=instance.nso_instance.adapter_instance_id,
                nso_device_name=instance.nso_device_name,
            )

        client.set_scope(instance.adapter_device_id, instance.managed_attributes, auto_apply=instance.auto_apply)

        notify_result = client.sync_notify(instance.adapter_device_id)
        if notify_result and notify_result.get("job_id"):
            logger.debug(
                "Sync-notify sent for device %s, job_id=%s",
                instance.device_id,
                notify_result["job_id"],
            )
    except Exception as exc:
        logger.warning("Failed to sync scope to adapter for device %s: %s", instance.device_id, exc)


@receiver(post_delete, sender="netbox_nso_plugin.NSODeviceManagement")
def offboard_device_from_adapter(sender, instance, **kwargs):
    """Remove device from adapter when the management record is deleted."""
    if instance.adapter_device_id is None:
        return
    from . import adapter_client as client

    try:
        client.delete_device(instance.adapter_device_id)
    except Exception as exc:
        logger.warning("Failed to offboard device %s from adapter: %s", instance.adapter_device_id, exc)


@receiver(post_save, sender="netbox_nso_plugin.NSOInterfaceState")
def push_intent_on_accept(sender, instance, **kwargs):
    """Push full intent snapshot to adapter when an interface state is accepted."""
    if instance.status != "accepted":
        return

    from . import adapter_client as client
    from .models import NSODeviceManagement, NSOInterfaceState

    device_id = instance.interface.device_id
    try:
        mgmt = NSODeviceManagement.objects.get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    states = NSOInterfaceState.objects.filter(
        interface__device_id=device_id,
        status="accepted",
    ).select_related("interface")

    attributes = []
    for state in states:
        iface = state.interface
        if state.attribute == "description":
            intent_value = iface.description or ""
        elif state.attribute == "enabled":
            intent_value = str(iface.enabled).lower()
        else:
            continue
        attributes.append(
            {
                "interface": iface.name,
                "attribute": state.attribute,
                "intent_value": intent_value,
                "accepted_at": state.accepted_at.isoformat() if state.accepted_at else None,
            }
        )

    try:
        client.put_intent(mgmt.adapter_device_id, attributes)
    except Exception as exc:
        logger.warning("Failed to push intent for device %s: %s", device_id, exc)


def _templates():
    """Return the parsed derived-intent template list from the AppConfig.

    Returns an empty list when the feature is off (config absent or empty).
    """
    from django.apps import apps

    cfg = apps.get_app_config("netbox_nso_plugin")
    return getattr(cfg, "_derived_intent_templates", [])


def _affected_interfaces(cable):
    """Yield all Interface objects on either end of *cable*.

    Uses the Phase-1-confirmed API: cable.a_terminations and
    cable.b_terminations are lists of termination objects (Interface, Circuit,
    etc.) — not querysets, not CableTermination wrappers.
    """
    from dcim.models import Interface as _Interface

    for iface in list(cable.a_terminations) + list(cable.b_terminations):
        if isinstance(iface, _Interface):
            yield iface


def _recompute_one(interface, templates):
    """Recompute the description for *interface* if it is managed by a sentinel.

    Idempotent: no write if the current value already matches.
    """
    from .derived_intent import compute_description, is_managed_description

    match = is_managed_description(interface.description or "", templates)
    if match is None:
        return
    try:
        new_value = compute_description(interface, match)
    except Exception as exc:  # noqa: BLE001 — signal handler must not propagate; compute_description traverses cable topology and can raise arbitrary ORM/template exceptions
        logger.exception(
            "derived_intent.compute_failed field=description_from_cable interface_id=%s exc=%s",
            interface.pk,
            exc,
        )
        return
    if new_value is None:
        return  # skip-logged inside compute_description
    if interface.description == new_value:
        return  # idempotent — terminates signal chain
    interface.description = new_value
    interface.save(update_fields=["description"])


def _recompute_on_cable_change(sender, instance, **kwargs):
    """Recompute descriptions for both ends of a cable after it is saved."""
    templates = _templates()
    if not templates:
        return
    for iface in _affected_interfaces(instance):
        _recompute_one(iface, templates)


def _recompute_on_cable_delete(sender, instance, **kwargs):
    """Recompute descriptions for cable endpoints after the cable is deleted.

    Django populates termination FK values in post_delete, so
    ``_affected_interfaces`` still works here.
    """
    templates = _templates()
    if not templates:
        return
    for iface in _affected_interfaces(instance):
        _recompute_one(iface, templates)


def _recompute_on_interface_save(sender, instance, created, **kwargs):
    """Recompute description when an interface is saved (description may have changed)."""
    if _is_adapter_origin_write():
        return  # adapter import — not an operator edit; don't recompute derived intent
    templates = _templates()
    if not templates:
        return
    _recompute_one(instance, templates)


def _push_intent_on_interface_edit(sender, instance, created, **kwargs):
    """Treat direct edits to description/enabled on managed interfaces as intent.

    If the interface belongs to an NSO-managed device, promote the
    relevant NSOInterfaceState rows to 'accepted' and push the full
    intent snapshot to the adapter.  This is an idempotent no-op if
    the value hasn't actually changed.

    Decision G (activated in Phase 2): editing description/enabled on a managed
    interface IS an intent change — identical to an explicit Accept action.

    Adapter-origin writes (imports/applies) are skipped: importing a value is not
    an operator accept, and re-promoting + pushing it back would both corrupt the
    imported→accepted gate and, during a bulk sync, fire one full-device intent
    push per interface (the device-27 sync wall).
    """
    if created:
        return  # new interface — nothing to accept yet

    if _is_adapter_origin_write():
        return  # import/apply, not an operator intent edit

    from .models import NSODeviceManagement, NSOInterfaceState

    try:
        mgmt = NSODeviceManagement.objects.get(device_id=instance.device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    now = timezone.now()
    updated = False
    for attribute in ("description", "enabled"):
        if attribute not in mgmt.managed_attributes:
            continue
        state = NSOInterfaceState.objects.filter(
            interface=instance,
            attribute=attribute,
        ).first()
        if state is None:
            continue
        if state.status in ("imported", "changed") and state.accepted_at is None:
            state.status = "accepted"
            state.accepted_at = now
            state.save(update_fields=["status", "accepted_at"])
            updated = True

    if not updated:
        return

    from . import adapter_client as client

    states = NSOInterfaceState.objects.filter(
        interface__device_id=instance.device_id,
        status="accepted",
    ).select_related("interface")

    attributes_payload = []
    for state in states:
        iface = state.interface
        if state.attribute == "description":
            intent_value = iface.description or ""
        elif state.attribute == "enabled":
            intent_value = str(iface.enabled).lower()
        else:
            continue
        attributes_payload.append(
            {
                "interface": iface.name,
                "attribute": state.attribute,
                "intent_value": intent_value,
                "accepted_at": state.accepted_at.isoformat() if state.accepted_at else None,
            }
        )

    try:
        client.put_intent(mgmt.adapter_device_id, attributes_payload)
    except Exception as exc:
        logger.warning(
            "G-activated: failed to push intent for device %s: %s",
            instance.device_id,
            exc,
        )


def _push_ip_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IP intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOInterfaceIPState

    ip_states = NSOInterfaceIPState.objects.filter(
        interface__device_id=device_id,
        status="accepted",
    ).select_related("interface")

    addresses = [
        {
            "interface": ip_state.interface.name,
            "address": ip_state.address,
            "family": ip_state.family,
            "secondary": bool(ip_state.secondary),
            "vrf": ip_state.vrf,
            "accepted_at": ip_state.accepted_at.isoformat() if ip_state.accepted_at else None,
        }
        for ip_state in ip_states
    ]

    try:
        client.put_ip_intent(adapter_device_id, addresses)
    except Exception as exc:
        logger.warning("Failed to push IP intent for device %s: %s", device_id, exc)


def _push_snmp_intent_for_device(device_id, adapter_device_id):
    """Build and push the full SNMP intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOSnmpCommunityState, NSOSnmpHostState, NSOSnmpSystemInfoState, NSOSnmpV3UserState

    communities = []
    for row in NSOSnmpCommunityState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("management"):
        if not row.vault_ref:
            continue
        communities.append(
            {
                "label": row.community_hash,  # use hash as stable label
                "vault_ref": row.vault_ref,
                "access": row.access,
                "acl": row.acl or None,
            }
        )

    v3_users = []
    for row in NSOSnmpV3UserState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ):
        if not row.vault_ref:
            continue
        v3_users.append(
            {
                "username": row.username,
                "auth_vault_ref": row.vault_ref,  # single vault_ref used for auth
                "priv_vault_ref": None,
            }
        )

    hosts = []
    for row in NSOSnmpHostState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ):
        hosts.append(
            {
                "address": row.address,
                "version": row.version,
                "notify_type": row.notify_type,
                "community_or_user": row.community_hash or "",  # hash used as community label reference
            }
        )

    system_info = None
    try:
        sysinfo = NSOSnmpSystemInfoState.objects.get(
            management__device_id=device_id,
        )
        if sysinfo.status in ("accepted", "deploying", "in_sync"):
            system_info = {
                "location": sysinfo.location or None,
                "contact": sysinfo.contact or None,
            }
    except NSOSnmpSystemInfoState.DoesNotExist:
        pass

    try:
        client.put_snmp_intent(adapter_device_id, communities, v3_users, hosts, system_info)
    except Exception as exc:
        logger.warning("Failed to push SNMP intent for device %s: %s", device_id, exc)


def _on_snmp_state_save(sender, instance, **kwargs):
    """Push SNMP intent whenever an SNMP state row is saved (accept triggers push)."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_snmp_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _on_ip_address_change(sender, instance, **kwargs):
    """Push IP intent when an IPAddress assigned to a managed interface changes."""
    from dcim.models import Interface as _Interface

    from .models import NSODeviceManagement, NSOInterfaceIPState

    assigned = instance.assigned_object
    if not isinstance(assigned, _Interface):
        return

    device_id = assigned.device_id

    try:
        mgmt = NSODeviceManagement.objects.get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    addr_str = str(instance.address)  # "ip/plen"
    vrf_name = instance.vrf.name if instance.vrf else ""
    family = "ipv6" if ":" in addr_str.split("/")[0] else "ipv4"

    ip_state, created = NSOInterfaceIPState.objects.get_or_create(
        interface=assigned,
        address=addr_str,
        vrf=vrf_name,
        defaults={
            "status": "accepted",
            "accepted_at": timezone.now(),
            "family": family,
            "secondary": False,
        },
    )

    if not created:
        if ip_state.status == "conflict":
            logger.debug(
                "IP %s on interface %s is in conflict state; skipping intent push",
                addr_str,
                assigned.name,
            )
            return
        if ip_state.status != "accepted":
            ip_state.status = "accepted"
            ip_state.accepted_at = timezone.now()
            ip_state.save(update_fields=["status", "accepted_at"])

    if not getattr(_p2p_allocation_active, "active", False):
        _push_ip_intent_for_device(device_id, mgmt.adapter_device_id)


def _on_ip_address_delete(sender, instance, **kwargs):
    """Push IP intent (with the deleted IP removed) when an IPAddress is deleted."""
    from dcim.models import Interface as _Interface

    from .models import NSODeviceManagement, NSOInterfaceIPState

    assigned = instance.assigned_object
    if not isinstance(assigned, _Interface):
        return

    device_id = assigned.device_id

    try:
        mgmt = NSODeviceManagement.objects.get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    addr_str = str(instance.address)
    vrf_name = instance.vrf.name if instance.vrf else ""
    NSOInterfaceIPState.objects.filter(
        interface=assigned,
        address=addr_str,
        vrf=vrf_name,
    ).delete()

    _push_ip_intent_for_device(device_id, mgmt.adapter_device_id)


def _push_static_route_intent_for_device(device_id, adapter_device_id):
    """Build and push the full static route intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOStaticRouteState

    routes = []
    for row in NSOStaticRouteState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("static_route", "static_route__vrf"):
        sr = row.static_route
        if sr.next_hop is None:
            continue  # interface-only next-hop not supported by static-route-reconciler v1
        vrf_name = sr.vrf.name if sr.vrf else ""
        routes.append(
            {
                "vrf": vrf_name,
                "prefix": str(sr.prefix),
                "next_hop": str(sr.next_hop),
                "metric": sr.metric,
                "permanent": sr.permanent or False,
                "tag": sr.tag,
            }
        )

    try:
        client.put_static_route_intent(adapter_device_id, routes)
    except Exception as exc:
        logger.warning("Failed to push static route intent for device %s: %s", device_id, exc)


def _on_static_route_state_save(sender, instance, **kwargs):
    """Push static route intent whenever an NSOStaticRouteState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_static_route_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _push_isis_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IS-IS intent snapshot (interfaces + processes) for a device."""
    from . import adapter_client as client
    from .models import NSOISISInstanceState, NSOISISInterfaceState

    redist_by_proc = _collect_redistribution_by_dest_ref(device_id, "isis")

    interfaces = []
    for row in NSOISISInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("interface"):
        interfaces.append(
            {
                "interface_name": row.interface.name,
                "af": row.af,
                "process_tag": row.process_tag or "",
                "circuit_type": row.circuit_type,
                "network_type": row.network_type,
                "metric": row.metric,
                "passive": row.passive or False,
            }
        )

    processes = []
    for row in NSOISISInstanceState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ):
        proc_entry = {
            "process_tag": row.process_tag or "",
            "net": row.net,
            "is_type": row.is_type,
            "metric_style": row.metric_style,
            "overload_bit": row.overload_bit,
            "area_auth_type": row.area_auth_type,
            "area_auth_key": row.area_auth_key,
            "domain_auth_type": row.domain_auth_type,
            "domain_auth_key": row.domain_auth_key,
        }
        proc_redist = redist_by_proc.get(row.process_tag or "", [])
        if proc_redist:
            proc_entry["redistribution"] = proc_redist
        processes.append(proc_entry)

    try:
        client.put_isis_interface_intent(adapter_device_id, interfaces, processes=processes)
    except Exception as exc:
        logger.warning("Failed to push IS-IS intent for device %s: %s", device_id, exc)


def _on_isis_interface_state_save(sender, instance, **kwargs):
    """Push IS-IS interface intent whenever an NSOISISInterfaceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_isis_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _on_isis_instance_state_save(sender, instance, **kwargs):
    """Push IS-IS intent (interfaces + processes) whenever an NSOISISInstanceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_isis_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _build_bgp_router_list(routers: dict, scope_afs: dict) -> list:
    """Convert the routers dict + scope_afs into the adapter router_list format."""
    router_list = []
    for asn_str, router_data in routers.items():
        scopes_out = []
        for vrf_str, scope_data in router_data["scopes"].items():
            af_map = scope_afs.get((asn_str, vrf_str), {})
            afs_out = [{"af": af_str, "redistribution": redist_entries} for af_str, redist_entries in af_map.items()]
            scope_out = dict(scope_data)
            scope_out["address_families"] = afs_out
            scopes_out.append(scope_out)
        router_list.append({"asn": asn_str, "scopes": scopes_out})
    return router_list


def _push_bgp_intent_for_device(device_id, adapter_device_id):
    """Build and push the full BGP intent snapshot for a device (M16 B3)."""
    from . import adapter_client as client
    from .models import NSOBGPPeerState

    # BGP redistribution: dest_ref = f"{asn}:{vrf}:{af}"
    redist_by_af = _collect_redistribution_by_dest_ref(device_id, "bgp")

    # Build scope-level address_families from redistribution dest_refs
    scope_afs: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for dest_ref, redist_entries in redist_by_af.items():
        parts = dest_ref.split(":", 2)
        if len(parts) != 3:
            continue
        asn_str, vrf_str, af_str = parts
        scope_afs.setdefault((asn_str, vrf_str), {})[af_str] = redist_entries

    routers: dict[str, dict] = {}
    for row in NSOBGPPeerState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("management", "bgp_peer"):
        asn_str = row.asn_str
        vrf_name = row.vrf_name or ""
        if asn_str not in routers:
            routers[asn_str] = {"asn": asn_str, "scopes": {}}
        scopes = routers[asn_str]["scopes"]
        if vrf_name not in scopes:
            scopes[vrf_name] = {"vrf": vrf_name, "address_families": [], "peers": []}

        # Build address-family list from the linked BGPPeer if available.
        peer_afs = []
        if row.bgp_peer is not None:
            for paf in row.bgp_peer.address_families.select_related(
                "address_family",
                "prefixlist_in",
                "prefixlist_out",
                "routemap_in",
                "routemap_out",
            ):
                af_entry = {
                    "af": paf.address_family.address_family,
                    "enabled": paf.enabled if paf.enabled is not None else True,
                }
                if paf.routemap_in:
                    af_entry["routemap_in"] = paf.routemap_in.name
                if paf.routemap_out:
                    af_entry["routemap_out"] = paf.routemap_out.name
                if paf.prefixlist_in:
                    af_entry["prefixlist_in"] = paf.prefixlist_in.name
                if paf.prefixlist_out:
                    af_entry["prefixlist_out"] = paf.prefixlist_out.name
                peer_afs.append(af_entry)

        scopes[vrf_name]["peers"].append(
            {
                "peer_address": row.peer_address_str,
                "enabled": row.enabled if row.enabled is not None else True,
                "remote_as": row.remote_as_str or None,
                "address_families": peer_afs,
            }
        )

    try:
        client.put_bgp_intent(adapter_device_id, _build_bgp_router_list(routers, scope_afs))
    except Exception as exc:
        logger.warning("Failed to push BGP intent for device %s: %s", device_id, exc)


def _on_bgp_peer_state_save(sender, instance, **kwargs):
    """Push BGP intent whenever an NSOBGPPeerState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_bgp_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _on_redistribution_state_save(sender, instance, **kwargs):
    """Push the relevant routing protocol intent when an NSORedistributionState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    dest = instance.dest_protocol
    if dest == "ospf":
        _push_ospf_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)
    elif dest == "isis":
        _push_isis_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)
    elif dest == "bgp":
        _push_bgp_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)
    else:
        logger.warning(
            "NSORedistributionState: unknown dest_protocol %r for device %s — no push triggered",
            dest,
            mgmt.device_id,
        )


def _push_route_policy_intent_for_device(device_id, adapter_device_id):
    """Build and push the full route-policy intent snapshot for a device (M17 B3)."""
    from . import adapter_client as client
    from .models import NSORoutePolicyState

    objects = []
    for row in NSORoutePolicyState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("management"):
        # Build the entries payload from the associated NetBox object via the GFK.
        obj = row.content_object
        if obj is None:
            continue
        entries = _build_route_policy_entries(row.family, obj)
        objects.append(
            {
                "family": row.family,
                "name": row.object_name,
                "entries": entries,
                "accepted": row.status == "accepted",
            }
        )

    try:
        client.put_route_policy_intent(adapter_device_id, objects)
    except Exception as exc:
        logger.warning("Failed to push route-policy intent for device %s: %s", device_id, exc)


def _build_route_policy_entries(family, obj):
    """Serialize a NetBox route-policy object's entries for the adapter intent payload."""
    if family == "prefix_list":
        return [
            {
                "sequence": e.index,
                "action": e.action.lower() if e.action else "permit",
                "prefix": str(e.prefix),
                **({"ge": e.ge} if getattr(e, "ge", None) is not None else {}),
                **({"le": e.le} if getattr(e, "le", None) is not None else {}),
            }
            for e in obj.prefixes.all().order_by("index")
        ]
    if family == "community_list":
        return [
            {"sequence": i + 1, "action": "permit", "community": str(c.value)}
            for i, c in enumerate(obj.communities.all())
        ]
    if family == "as_path":
        return [
            {
                "sequence": i + 1,
                "action": e.action.lower() if e.action else "permit",
                "pattern": e.regex or "",
            }
            for i, e in enumerate(
                getattr(obj, "entries", obj.access_list_entries.all()) if hasattr(obj, "access_list_entries") else []
            )
        ]
    if family == "route_map":
        entries = []
        for e in obj.entries.all().order_by("sequence"):
            entry: dict = {
                "sequence": e.sequence,
                "action": e.action.lower() if e.action else "permit",
                "match": e.match or "{}",
                "set": e.set or "{}",
            }
            entries.append(entry)
        return entries
    return []


def _on_route_policy_state_save(sender, instance, **kwargs):
    """Push route-policy intent whenever an NSORoutePolicyState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_route_policy_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _collect_redistribution_by_dest_ref(device_id: int, dest_protocol: str) -> dict[str, list[dict]]:
    """Return redistribution entries grouped by dest_ref for the given protocol and device.

    When a row has its ``redistribution`` FK set (Track B), the fork object's fields
    are used as the source of truth so operator-authored intent reaches the push payload.
    """
    from .models import NSORedistributionState

    by_ref: dict[str, list[dict]] = {}
    for row in NSORedistributionState.objects.filter(
        management__device_id=device_id,
        dest_protocol=dest_protocol,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("redistribution", "redistribution__route_map"):
        entry: dict = {"source_protocol": row.source_protocol, "source_ref": row.source_ref}
        if row.redistribution_id is not None:
            fork = row.redistribution
            if fork.route_map:
                entry["route_map"] = fork.route_map.name
            if fork.metric is not None:
                entry["metric"] = fork.metric
            if fork.metric_type:
                entry["metric_type"] = fork.metric_type
        else:
            if row.route_map:
                entry["route_map"] = row.route_map
            if row.metric is not None:
                entry["metric"] = row.metric
            if row.metric_type:
                entry["metric_type"] = row.metric_type
        by_ref.setdefault(row.dest_ref, []).append(entry)
    return by_ref


def _push_ospf_intent_for_device(device_id, adapter_device_id):
    """Build and push the full OSPF intent snapshot for a device (M19 B3)."""
    from . import adapter_client as client
    from .models import NSOOSPFInstanceState, NSOOSPFInterfaceState

    redist_by_proc = _collect_redistribution_by_dest_ref(device_id, "ospf")

    instances = []
    for row in NSOOSPFInstanceState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("management"):
        entry = {
            "process_id": row.process_id,
            "vrf": row.vrf or "",
            "areas": row.areas or [],
        }
        if row.router_id:
            entry["router_id"] = row.router_id
        proc_redist = redist_by_proc.get(str(row.process_id), [])
        if proc_redist:
            entry["redistribution"] = proc_redist
        instances.append(entry)

    interfaces = []
    for row in NSOOSPFInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=("accepted", "deploying", "in_sync"),
    ).select_related("management"):
        entry = {
            "interface_name": row.interface_name,
            "passive": row.passive if row.passive is not None else False,
            "auth_present": row.auth_present if row.auth_present is not None else False,
        }
        if row.process_id is not None:
            entry["process_id"] = row.process_id
        if row.area_id is not None:
            entry["area_id"] = row.area_id
        if row.priority is not None:
            entry["priority"] = row.priority
        if row.cost is not None:
            entry["cost"] = row.cost
        if row.network_type is not None:
            entry["network_type"] = row.network_type
        if row.auth_type is not None:
            entry["auth_type"] = row.auth_type
        interfaces.append(entry)

    try:
        client.put_ospf_intent(adapter_device_id, {"instances": instances, "interfaces": interfaces})
    except Exception as exc:
        logger.warning("Failed to push OSPF intent for device %s: %s", device_id, exc)


def _on_ospf_instance_state_save(sender, instance, **kwargs):
    """Push OSPF intent whenever an NSOOSPFInstanceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_ospf_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _on_ospf_interface_state_save(sender, instance, **kwargs):
    """Push OSPF intent whenever an NSOOSPFInterfaceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _push_ospf_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)


def _on_redistribution_fork_save(sender, instance, **kwargs):
    """Push protocol intent when a netbox_routing.Redistribution fork object is saved.

    Finds all NSORedistributionState rows linked to this Redistribution, determines
    their destination protocol, and triggers the appropriate intent push.
    Deduplicates pushes so each (device, dest_protocol) pair is pushed at most once.
    """
    from .models import NSORedistributionState

    seen: set[tuple] = set()
    for state in NSORedistributionState.objects.filter(redistribution=instance).select_related("management"):
        mgmt = state.management
        if mgmt.adapter_device_id is None:
            continue
        key = (mgmt.device_id, state.dest_protocol)
        if key in seen:
            continue
        seen.add(key)
        try:
            dest = state.dest_protocol
            if dest == "ospf":
                _push_ospf_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)
            elif dest == "isis":
                _push_isis_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)
            elif dest == "bgp":
                _push_bgp_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)
            else:
                logger.warning(
                    "Redistribution fork save: unknown dest_protocol %r for device %s — no push",
                    dest,
                    mgmt.device_id,
                )
        except Exception as exc:
            logger.warning(
                "Redistribution fork save: push failed for device %s protocol %s: %s",
                mgmt.device_id,
                state.dest_protocol,
                exc,
            )


def _connect_g_activated():  # pragma: no cover
    """Wire post-app-load signal handlers that require dcim/ipam models.

    Called from AppConfig.ready() after all apps are loaded so that
    dcim.Interface, dcim.Cable, and ipam.IPAddress are importable.
    """
    from dcim.models import Cable, Interface
    from ipam.models import IPAddress

    post_save.connect(
        _push_intent_on_interface_edit,
        sender=Interface,
        dispatch_uid="nso_plugin_iface_g_activated",
    )
    post_save.connect(
        _recompute_on_cable_change,
        sender=Cable,
        dispatch_uid="nso_plugin_cable_post_save",
    )
    post_delete.connect(
        _recompute_on_cable_delete,
        sender=Cable,
        dispatch_uid="nso_plugin_cable_post_delete",
    )
    post_save.connect(
        _recompute_on_interface_save,
        sender=Interface,
        dispatch_uid="nso_plugin_iface_derived_intent",
    )
    post_save.connect(
        _on_ip_address_change,
        sender=IPAddress,
        dispatch_uid="nso_plugin_ipaddress_post_save",
    )
    post_delete.connect(
        _on_ip_address_delete,
        sender=IPAddress,
        dispatch_uid="nso_plugin_ipaddress_post_delete",
    )

    # SNMP state → intent push (M11 B3)
    from .models import NSOSnmpCommunityState, NSOSnmpHostState, NSOSnmpSystemInfoState, NSOSnmpV3UserState

    for snmp_model in (NSOSnmpCommunityState, NSOSnmpV3UserState, NSOSnmpHostState, NSOSnmpSystemInfoState):
        post_save.connect(
            _on_snmp_state_save,
            sender=snmp_model,
            dispatch_uid=f"nso_plugin_snmp_{snmp_model.__name__}_post_save",
        )

    # Static route state → intent push (M10 B3)
    from .models import NSOStaticRouteState

    post_save.connect(
        _on_static_route_state_save,
        sender=NSOStaticRouteState,
        dispatch_uid="nso_plugin_static_route_state_post_save",
    )

    # IS-IS interface state → intent push (M14 B3)
    from .models import NSOISISInstanceState, NSOISISInterfaceState

    post_save.connect(
        _on_isis_interface_state_save,
        sender=NSOISISInterfaceState,
        dispatch_uid="nso_plugin_isis_interface_state_post_save",
    )

    # IS-IS process (instance) state → intent push (M18 B3)
    post_save.connect(
        _on_isis_instance_state_save,
        sender=NSOISISInstanceState,
        dispatch_uid="nso_plugin_isis_instance_state_post_save",
    )

    # BGP peer state → intent push (M16 B3)
    from .models import NSOBGPPeerState

    post_save.connect(
        _on_bgp_peer_state_save,
        sender=NSOBGPPeerState,
        dispatch_uid="nso_plugin_bgp_peer_state_post_save",
    )

    # Route-policy state → intent push (M17 B3)
    from .models import NSORoutePolicyState

    post_save.connect(
        _on_route_policy_state_save,
        sender=NSORoutePolicyState,
        dispatch_uid="nso_plugin_route_policy_state_post_save",
    )

    # OSPF state → intent push (M19 B3)
    from .models import NSOOSPFInstanceState, NSOOSPFInterfaceState

    post_save.connect(
        _on_ospf_instance_state_save,
        sender=NSOOSPFInstanceState,
        dispatch_uid="nso_plugin_ospf_instance_state_post_save",
    )
    post_save.connect(
        _on_ospf_interface_state_save,
        sender=NSOOSPFInterfaceState,
        dispatch_uid="nso_plugin_ospf_interface_state_post_save",
    )

    # Redistribution state → intent push (M20 B3)
    from .models import NSORedistributionState

    post_save.connect(
        _on_redistribution_state_save,
        sender=NSORedistributionState,
        dispatch_uid="nso_plugin_redistribution_state_post_save",
    )

    # netbox_routing.Redistribution fork save → intent push (routing accept path B)
    try:
        from netbox_routing.models import Redistribution

        post_save.connect(
            _on_redistribution_fork_save,
            sender=Redistribution,
            dispatch_uid="nso_plugin_redistribution_fork_post_save",
        )
    except ImportError:
        logger.debug("netbox_routing not installed — redistribution fork signal not registered")
