# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Freeze safe logical contributions from the selected renderer payload."""

from dataclasses import dataclass
from operator import attrgetter

from .apply_state import _enter_level
from .delivery import canonical_fingerprint, delivery_keys
from .intent_state import ABSENT, OVERLAY_MODEL_RANKS, canonical_fragment
from .refusal_protocol import SUBJECTS, RefusalProtocolError, row_ref
from .refusal_registry import OVERLAYS, RegistryError, owner_spec
from .status_machine import OWNED_STATES

_IDENTITY_FIELDS = {
    "nsointerfacestate": ("interface.name", "attribute"),
    "nsointerfaceipstate": ("interface.name", "family", "address"),
    "nsosnmpcommunitystate": ("community_hash",),
    "nsosnmpv3userstate": ("username",),
    "nsosnmphoststate": ("address",),
    "nsosnmpsysteminfostate": (),
    "nsologginghoststate": ("address",),
    "nsologginglevelstate": (),
    "nsosvistate": ("interface.name",),
    "nsosubinterfacestate": ("interface.name",),
    "nsointerfacemtustate": ("interface.name",),
    "nsovlanstate": ("vlan.vid",),
    "nsobfdinterfacestate": ("interface.name",),
    "nsostaticroutestate": ("static_route_id",),
    "nsol2sapstate": ("service_name", "sap_id"),
    "nsoisisinstancestate": ("process_tag",),
    "nsoisisinterfacestate": ("interface.name", "af"),
    "nsoisisflexalgostate": ("process_tag", "algo_id"),
    "nsobgprouterstate": ("asn_str",),
    "nsobgppeerstate": ("asn_str", "vrf_name", "peer_address_str"),
    "nsoredistributionstate": ("dest_protocol", "dest_ref", "source_protocol", "source_ref"),
    "nsoospfinstancestate": ("process_id",),
    "nsoospfinterfacestate": ("interface.name",),
    "nsoroutepolicystate": ("family", "object_name"),
    "nsolacpbundlestate": ("interface.name",),
    "nsolacpmemberstate": ("interface.lag.name", "interface.name"),
    "nsoswitchportstate": ("interface.name",),
}
_FLAT_SCOPES = {
    "svi": ("svi.interface", "interface", "interface_name"),
    "subinterface": ("subinterface.interface", "interface", "interface_name"),
    "interface_mtu": ("mtu.interface", "interface", "interface_name"),
    "vlan": ("vlan.vlan", "vlan", "vlan_id"),
    "bfd": ("bfd.interface", "interface", "interface_name"),
    "switchport": ("switchport.interface", "interface", "interface_name"),
}
_POLICY_SUBJECTS = {
    "prefix_list": "route-policy.prefix-list",
    "community_list": "route-policy.community-list",
    "as_path": "route-policy.as-path",
    "route_map": "route-policy.route-map",
}
if {f"netbox_nso_plugin.{name}" for name in _IDENTITY_FIELDS} != set(OVERLAYS):
    raise RegistryError("selection owner identity inventory differs from the registry")


@dataclass(frozen=True)
class _Emission:
    subject: str
    path: tuple
    identity: tuple
    content: object
    carrier: tuple | None = None
    emitted_refs: tuple = ()


def current_content_digest(row):
    """Hash the same owner fragment used by the revision-bearing writer."""
    return canonical_fingerprint(canonical_fragment(row))


def lock_manifest_rows(management, scopes):
    """Lock applicable owners after the caller locks device, management and revisions."""
    scopes = frozenset(scopes)
    if not scopes <= delivery_keys().keys():
        raise RegistryError("selection contains an unknown delivery scope")
    locked = {}
    for rank, label in enumerate(OVERLAY_MODEL_RANKS):
        spec = OVERLAYS.get(label)
        if spec is None or not scopes.intersection(spec.delivery_keys):
            continue
        _enter_level(8, (rank, 0))
        queryset = spec.model.objects.select_for_update(of=("self",)).filter(
            **{spec.device_lookup: management.device_id}, status__in=OWNED_STATES
        )
        if spec.model._meta.model_name == "nsoredistributionstate":
            queryset = queryset.filter(dest_protocol__in=scopes.intersection({"bgp", "ospf", "isis"}))
        for row in queryset.order_by("pk"):
            locked[(label, row.pk)] = row
    return locked


def _owner_identity(row):
    fields = _IDENTITY_FIELDS[row._meta.model_name]
    identity = tuple(attrgetter(name)(row) for name in fields)
    if row._meta.model_name in {"nsoisisinstancestate", "nsoisisflexalgostate"}:
        identity = (identity[0] or "", *identity[1:])
    if row._meta.model_name == "nsobgppeerstate":
        identity = (identity[0], identity[1] or "", identity[2])
    if row._meta.model_name == "nsoospfinstancestate":
        identity = (str(identity[0]),)
    return identity


def _ref(subject, path):
    try:
        family = SUBJECTS[subject].family
        ref = row_ref(family, subject, path)
    except (KeyError, RefusalProtocolError):
        raise RegistryError("selected contribution has an invalid codec identity") from None
    return {"family": family, "subject": ref.subject, "key": ref.key}


def _source_ref(item):
    value = item["source_ref"]
    if not isinstance(value, str):
        raise RegistryError("redistribution source reference must be text")
    if item["source_protocol"] != "bgp" or value == "":
        return value
    parts = value.split(".")
    if len(parts) not in (1, 2) or any(
        not part.isascii() or not part.isdecimal() or (part != "0" and part.startswith("0")) for part in parts
    ):
        raise RegistryError("BGP redistribution source reference has invalid AS notation")
    limit = 4294967295 if len(parts) == 1 else 65535
    if any(len(part) > 10 for part in parts):
        raise RegistryError("BGP redistribution source reference exceeds its AS range")
    values = tuple(int(part) for part in parts)
    if any(value > limit for value in values):
        raise RegistryError("BGP redistribution source reference exceeds its AS range")
    return str(values[0] if len(values) == 1 else values[0] * 65536 + values[1])


def _redistribution(protocol, destination, path, item):
    subject = (
        f"{protocol}.process-config.redistribute"
        if protocol != "bgp"
        else "bgp.router.scope.address-family.redistribute"
    )
    natural_path = (*path, ("redistribute", (item["source_protocol"], _source_ref(item))))
    identity = (protocol, destination, item["source_protocol"], item["source_ref"])
    return _Emission(subject, natural_path, identity, item)


def _interface_emissions(payload):
    for item in payload:
        subject = "interface." + item["attribute"]
        yield _Emission(subject, (("interface", (item["interface"],)),), (item["interface"], item["attribute"]), item)


def _ip_emissions(payload):
    for item in payload:
        address, separator, _prefix = item["address"].rpartition("/")
        if not separator:
            raise RegistryError("interface IP contribution lacks a prefix length")
        family = item["family"]
        subject = "interface." + family + "-address"
        path = (("interface", (item["interface"],)), (family + "-address", (address,)))
        yield _Emission(subject, path, (item["interface"], family, item["address"]), item)


def _snmp_emissions(payload):
    communities, users, hosts, info = payload
    for entries, subject, name, field in (
        (communities, "snmp.community", "community", "label"),
        (users, "snmp.v3-user", "v3-user", "username"),
        (hosts, "snmp.host", "host", "address"),
    ):
        for item in entries:
            yield _Emission(subject, ((name, (item[field],)),), (item[field],), item)
    if info is not None and (info["location"] is not None or info["contact"] is not None):
        yield _Emission("snmp.system-info", (), (), info)


def _logging_emissions(payload):
    hosts, levels = payload
    for item in hosts:
        yield _Emission("logging.host", (("host", (item["address"],)),), (item["address"],), item)
    if levels:
        yield _Emission("logging.local-levels", (), (), levels)


def _static_route_emissions(payload):
    for item in payload:
        path = (("route", (item["vrf"], item["prefix"], item["next_hop"])),)
        yield _Emission("static-route.route", path, (item["route_id"],), item)


def _l2_sap_emissions(payload):
    for item in payload:
        identity = (item["service_name"], item["sap_id"])
        yield _Emission("l2-sap.sap", (("sap", identity),), identity, item)


def _flex_algo_emissions(payload):
    for item in payload:
        tag, algo = item["process_tag"], item["algo_id"]
        path = (("process-config", (tag,)), ("flex-algo", (algo,)))
        yield _Emission("isis.process-config.flex-algo", path, (tag, algo), item)


def _isis_emissions(payload):
    interfaces, processes = payload
    for item in interfaces:
        identity = (item["interface_name"], item["af"])
        yield _Emission("isis.interface-config", (("interface-config", identity),), identity, item)
    for item in processes:
        tag = item["process_tag"]
        path = (("process-config", (tag,)),)
        content = {key: value for key, value in item.items() if key not in {"levels", "redistribution"}}
        yield _Emission("isis.process-config", path, (tag,), content)
        for level in item.get("levels", ()):
            yield _Emission("isis.process-config.level", (*path, ("level", (level["level"],))), (tag,), level)
        for redistribution in item.get("redistribution", ()):
            yield _redistribution("isis", tag, path, redistribution)


def _bgp_emissions(payload):
    for router in payload:
        asn = router["asn"]
        router_path = (("router", (asn,)),)
        router_subject = "bgp.router"
        router_content = {
            "asn": asn,
            "router_id": router.get("router_id"),
            "scopes": [
                {
                    "vrf": scope["vrf"],
                    "address_families": [{"af": family["af"]} for family in scope["address_families"]],
                }
                for scope in router["scopes"]
            ],
        }
        emitted_refs = []
        for scope in router["scopes"]:
            scope_path = (*router_path, ("scope", (scope["vrf"],)))
            emitted_refs.append(("bgp.router.scope", scope_path))
            for family in scope["address_families"]:
                emitted_refs.append(
                    ("bgp.router.scope.address-family", (*scope_path, ("address-family", (family["af"],))))
                )
        yield _Emission(router_subject, router_path, (asn,), router_content, emitted_refs=tuple(emitted_refs))
        for scope in router["scopes"]:
            vrf = scope["vrf"]
            scope_path = (*router_path, ("scope", (vrf,)))
            for peer in scope["peers"]:
                peer_path = (*scope_path, ("peer", (peer["peer_address"],)))
                peer_refs = tuple(
                    (
                        "bgp.router.scope.peer.peer-address-family",
                        (*peer_path, ("peer-address-family", (family["af"],))),
                    )
                    for family in peer["address_families"]
                )
                yield _Emission(
                    "bgp.router.scope.peer",
                    peer_path,
                    (asn, vrf, peer["peer_address"]),
                    peer,
                    carrier=(router_subject, router_path),
                    emitted_refs=peer_refs,
                )
            for family in scope["address_families"]:
                family_path = (*scope_path, ("address-family", (family["af"],)))
                for redistribution in family["redistribution"]:
                    yield _redistribution("bgp", f"{asn}/{vrf}/{family['af']}", family_path, redistribution)


def _policy_emissions(payload):
    for item in payload:
        subject = _POLICY_SUBJECTS.get(item["family"])
        if subject is None:
            raise RegistryError("selected policy object has an unknown family")
        path = ((subject.removeprefix("route-policy."), (item["name"],)),)
        yield _Emission(subject, path, (item["family"], item["name"]), item)


def _ospf_emissions(payload):
    for item in payload["instances"]:
        process_id = str(item["process_id"])
        path = (("process-config", (process_id,)),)
        content = {key: value for key, value in item.items() if key != "redistribution"}
        yield _Emission("ospf.process-config", path, (process_id,), content)
        for redistribution in item.get("redistribution", ()):
            yield _redistribution("ospf", process_id, path, redistribution)
    for item in payload["interfaces"]:
        identity = (item["interface_name"],)
        yield _Emission("ospf.interface-config", (("interface-config", identity),), identity, item)


def _lacp_emissions(payload):
    for item in payload:
        path = (("bundle", (item["name"],)),)
        content = {key: value for key, value in item.items() if key != "members"}
        yield _Emission("lag.bundle", path, (item["name"],), content)
        for member in item["members"]:
            identity = (item["name"], member["interface_name"])
            yield _Emission(
                "lag.bundle.member",
                (*path, ("member", (member["interface_name"],))),
                identity,
                member,
                carrier=("lag.bundle", path),
            )


_EMITTERS = {
    "interface": _interface_emissions,
    "ip": _ip_emissions,
    "snmp": _snmp_emissions,
    "logging": _logging_emissions,
    "static_route": _static_route_emissions,
    "l2_sap": _l2_sap_emissions,
    "isis_flex_algo": _flex_algo_emissions,
    "isis": _isis_emissions,
    "bgp": _bgp_emissions,
    "route_policy": _policy_emissions,
    "ospf": _ospf_emissions,
    "lacp": _lacp_emissions,
}
if set(_EMITTERS) | set(_FLAT_SCOPES) != set(delivery_keys()):
    raise RegistryError("selection emission inventory differs from the delivery registry")


def _emissions(scope, payload):
    if isinstance(payload, dict) and payload.get("blocked"):
        raise RegistryError("blocked renderer output cannot form a selected manifest")
    if scope in _FLAT_SCOPES:
        subject, name, field = _FLAT_SCOPES[scope]
        for item in payload:
            identity = (item[field],)
            yield _Emission(subject, ((name, identity),), identity, item)
    else:
        emitter = _EMITTERS.get(scope)
        if emitter is None:
            raise RegistryError("selection contains an unknown delivery scope")
        yield from emitter(payload)


def _owner_index(management, scope, locked_rows):
    index = {}
    digests = {}
    for key, row in locked_rows.items():
        spec = OVERLAYS.get(key[0])
        if spec is None or key != (row._meta.label_lower, row.pk):
            raise RegistryError("locked selection contains an unknown owner")
        device_id = attrgetter(spec.device_lookup.replace("__", "."))(row)
        if device_id != management.device_id or row.status not in OWNED_STATES:
            raise RegistryError("locked selection contains an owner outside the selected device")
        if scope not in spec.delivery_keys:
            continue
        fragment = canonical_fragment(row)
        if fragment == ABSENT:
            continue
        identity = _owner_identity(row)
        index.setdefault((spec.model_label, identity), []).append(key)
        digests[key] = canonical_fingerprint(fragment)
    return index, digests


def frozen_manifest(management, scope, payload, locked_rows=None):
    """Join every emitted logical contribution to exactly one locked local owner."""
    if scope not in delivery_keys():
        raise RegistryError("selection contains an unknown delivery scope")
    if locked_rows is None:
        locked_rows = lock_manifest_rows(management, (scope,))
    index, digests = _owner_index(management, scope, locked_rows)
    stream = delivery_keys()[scope].section
    manifest = {}
    selected_rows = {}
    seen = set()
    for emission in _emissions(scope, payload):
        spec = owner_spec(emission.subject)
        owners = index.get((spec.model_label, emission.identity), ())
        if len(owners) != 1:
            raise RegistryError("selected logical contribution has no unique local owner")
        key = owners[0]
        logical = _ref(emission.subject, emission.path)
        logical_key = (logical["family"], logical["subject"], logical["key"])
        if logical_key in seen:
            raise RegistryError("selected payload repeats a logical contribution")
        seen.add(logical_key)
        carrier = _ref(*emission.carrier) if emission.carrier is not None else logical
        refs = [logical, *(_ref(*extra) for extra in emission.emitted_refs)]
        if len({(ref["family"], ref["subject"], ref["key"]) for ref in refs}) != len(refs):
            raise RegistryError("selected contribution repeats an emitted identity")
        contributor = {
            "logical_ref": logical,
            "carrier_ref": carrier,
            "stream": stream,
            "contribution_digest": canonical_fingerprint(emission.content),
            "remote_acceptance_identity": None,
            "emitted_refs": refs,
        }
        owner = manifest.setdefault(
            key,
            {"model_label": key[0], "overlay_pk": key[1], "content_digest": digests[key], "contributors": []},
        )
        owner["contributors"].append(contributor)
        selected_rows[key] = locked_rows[key]
    for owner in manifest.values():
        for contributor in owner["contributors"]:
            carrier = contributor["carrier_ref"]
            if (carrier["family"], carrier["subject"], carrier["key"]) not in seen:
                raise RegistryError("selected contribution has no emitted carrier")
    ranks = {label: rank for rank, label in enumerate(OVERLAY_MODEL_RANKS)}
    return [manifest[key] for key in sorted(manifest, key=lambda key: (ranks[key[0]], key[1]))], selected_rows
