# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Canonical content and native identity for one BGP router aggregate."""

import hashlib
import ipaddress
import json


class BGPIntentError(ValueError):
    """A BGP intent validation reason that the operator may see."""

    def __init__(self, public_message):
        super().__init__(public_message)
        self.public_message = public_message


def canonical_asn(value):
    from .bgp_reconciler import _parse_asn

    try:
        return str(_parse_asn(str(value)))
    except (TypeError, ValueError) as exc:
        raise BGPIntentError("The BGP router ASN is invalid.") from exc


def router_fragment(asn, router_id, scopes):
    """Normalize the router fields without adding child contributions."""
    if router_id in (None, ""):
        router_id = None
    else:
        try:
            router_id = str(ipaddress.IPv4Address(str(router_id)))
        except ipaddress.AddressValueError as exc:
            raise BGPIntentError("The BGP router-id must be an IPv4 address.") from exc
    normalized = []
    seen = set()
    for scope in scopes:
        vrf = scope["vrf"]
        if not isinstance(vrf, str) or vrf in seen:
            raise BGPIntentError("The BGP router has an invalid or duplicate VRF scope.")
        seen.add(vrf)
        families = [value if isinstance(value, str) else value["af"] for value in scope["address_families"]]
        if any(not isinstance(value, str) or not value for value in families) or len(set(families)) != len(families):
            raise BGPIntentError("The BGP router has an invalid or duplicate address family.")
        normalized.append({"vrf": vrf, "address_families": [{"af": value} for value in sorted(families)]})
    return {"asn": canonical_asn(asn), "router_id": router_id, "scopes": sorted(normalized, key=lambda row: row["vrf"])}


def observed_router_content(router_entry):
    return router_fragment(router_entry["asn"], router_entry.get("router_id"), router_entry["scopes"])


def _native_router(value):
    if value._meta.label_lower == "netbox_nso_plugin.nsobgprouterstate":
        if value.bgp_router_id is None:
            router = value._state.fields_cache.get("bgp_router")
            if value._state.adding and router is not None and router._state.adding:
                return router
            raise BGPIntentError("Import and accept a linked BGP router before sending BGP intent.")
        return value.bgp_router
    return value


def router_content(value):
    """Read desired scalar and scope/AF membership from the native graph."""
    from netbox_routing.models import BGPAddressFamily, BGPScope

    router = _native_router(value)
    if router.asn_id is None:
        raise BGPIntentError("The linked BGP router has no ASN.")
    scopes = list(BGPScope.objects.filter(router=router).select_related("vrf").order_by("pk")) if router.pk else []
    by_scope = {scope.pk: [] for scope in scopes}
    for family in BGPAddressFamily.objects.filter(scope__in=scopes).order_by("pk"):
        by_scope[family.scope_id].append(family.address_family)
    return router_fragment(
        router.asn.asn,
        router.router_id,
        [{"vrf": scope.vrf.name if scope.vrf_id else "", "address_families": by_scope[scope.pk]} for scope in scopes],
    )


def router_content_digest(content):
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def qualified_router(state, device_id=None):
    """Require one matching native identity and a complete unambiguous graph."""
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import BGPRouter

    router = _native_router(state)
    device_id = state.management.device_id if device_id is None else device_id
    if state.management.device_id != device_id:
        raise BGPIntentError("The BGP router belongs to another managed device.")
    canonical = canonical_asn(state.asn_str)
    if state.asn_str != canonical:
        raise BGPIntentError("The BGP router identity must use a canonical decimal ASN.")
    device_type = ContentType.objects.get_for_model(Device)
    if (
        router.assigned_object_type_id != device_type.pk
        or router.assigned_object_id != device_id
        or router.asn_id is None
        or canonical_asn(router.asn.asn) != canonical
    ):
        raise BGPIntentError("Relink the BGP router to the matching device and ASN before accepting it.")
    matches = BGPRouter.objects.filter(
        assigned_object_type=device_type,
        assigned_object_id=device_id,
        asn__asn=int(canonical),
    )
    if list(matches.values_list("pk", flat=True)[:2]) != [router.pk]:
        raise BGPIntentError("Resolve duplicate BGP routers for this device and ASN before accepting it.")
    router_content(router)
    return router
