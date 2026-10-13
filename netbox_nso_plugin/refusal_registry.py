# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Closed settlement-owner inventory, independent of runtime dispatch and lock ranks."""

from dataclasses import dataclass
from types import MappingProxyType

from .delivery import delivery_keys
from .refusal_protocol import SUBJECTS, RowRef, row_ref


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class OverlaySpec:
    model_label: str
    delivery_keys: tuple[str, ...]
    subjects: tuple[str, ...]
    read_families: tuple[str, ...]
    counter_keys: tuple[str, ...]
    device_lookup: str = "management__device_id"
    error_type: str = "TextField"
    disposition: str = "owned"

    @property
    def model(self):
        from django.apps import apps

        return apps.get_model(self.model_label)

    def logical_ref(self, subject: str, natural_path: tuple) -> RowRef:
        if subject not in self.subjects:
            raise RegistryError("subject is not owned by this overlay")
        return row_ref(SUBJECTS[subject].family, subject, natural_path)

    def contributor_refs(self, emitted_paths: tuple) -> tuple[RowRef, ...]:
        refs = tuple(self.logical_ref(subject, path) for subject, path in emitted_paths)
        if not refs or len(set(refs)) != len(refs):
            raise RegistryError("invalid emitted contributor set")
        return refs


def _spec(model, keys, subjects, reads, counters, *, interface=False):
    return OverlaySpec(
        model_label=f"netbox_nso_plugin.{model}",
        delivery_keys=keys,
        subjects=subjects,
        read_families=reads,
        counter_keys=tuple(f"{key}_count_by_outcome" for key in counters),
        device_lookup="interface__device_id" if interface else "management__device_id",
        error_type="JSONField" if interface else "TextField",
    )


_SPECS = (
    _spec(
        "nsointerfacestate",
        ("interface",),
        ("interface.description", "interface.enabled"),
        ("interface_attributes",),
        ("attribute",),
        interface=True,
    ),
    _spec(
        "nsointerfaceipstate",
        ("ip",),
        ("interface.ipv4-address", "interface.ipv6-address"),
        ("interface_ip",),
        ("ip",),
        interface=True,
    ),
    _spec("nsosnmpcommunitystate", ("snmp",), ("snmp.community",), ("snmp",), ("snmp",)),
    _spec("nsosnmpv3userstate", ("snmp",), ("snmp.v3-user",), ("snmp",), ("snmp",)),
    _spec("nsosnmphoststate", ("snmp",), ("snmp.host",), ("snmp",), ("snmp",)),
    _spec("nsosnmpsysteminfostate", ("snmp",), ("snmp.system-info",), ("snmp",), ("snmp",)),
    _spec("nsologginghoststate", ("logging",), ("logging.host",), ("logging",), ("logging",)),
    _spec("nsologginglevelstate", ("logging",), ("logging.local-levels",), ("logging",), ("logging",)),
    _spec("nsosvistate", ("svi",), ("svi.interface",), ("svi",), ("svi",)),
    _spec("nsosubinterfacestate", ("subinterface",), ("subinterface.interface",), ("subinterface",), ("subinterface",)),
    _spec("nsointerfacemtustate", ("interface_mtu",), ("mtu.interface",), ("interface_mtu",), ("interface_mtu",)),
    _spec("nsovlanstate", ("vlan",), ("vlan.vlan",), ("vlan",), ("vlan",)),
    _spec("nsobfdinterfacestate", ("bfd",), ("bfd.interface",), ("bfd",), ("bfd",)),
    _spec("nsostaticroutestate", ("static_route",), ("static-route.route",), ("static_route",), ("static_route",)),
    _spec("nsol2sapstate", ("l2_sap",), ("l2-sap.sap",), ("l2_service",), ("l2_sap",)),
    _spec(
        "nsoisisinstancestate", ("isis",), ("isis.process-config", "isis.process-config.level"), ("isis",), ("isis",)
    ),
    _spec("nsoisisinterfacestate", ("isis",), ("isis.interface-config",), ("isis",), ("isis",)),
    _spec("nsoisisflexalgostate", ("isis_flex_algo",), ("isis.process-config.flex-algo",), ("isis",), ("isis",)),
    _spec("nsobgprouterstate", ("bgp",), ("bgp.router",), ("bgp",), ("bgp",)),
    _spec("nsobgppeerstate", ("bgp",), ("bgp.router.scope.peer",), ("bgp",), ("bgp",)),
    _spec(
        "nsoredistributionstate",
        ("bgp", "ospf", "isis"),
        (
            "bgp.router.scope.address-family.redistribute",
            "ospf.process-config.redistribute",
            "isis.process-config.redistribute",
        ),
        ("redistribution", "bgp", "ospf", "isis"),
        ("bgp", "ospf", "isis"),
    ),
    _spec("nsoospfinstancestate", ("ospf",), ("ospf.process-config",), ("ospf",), ("ospf",)),
    _spec("nsoospfinterfacestate", ("ospf",), ("ospf.interface-config",), ("ospf",), ("ospf",)),
    _spec(
        "nsoroutepolicystate",
        ("route_policy",),
        ("route-policy.prefix-list", "route-policy.community-list", "route-policy.as-path", "route-policy.route-map"),
        ("route_policy",),
        ("route_policy",),
    ),
    _spec("nsolacpbundlestate", ("lacp",), ("lag.bundle",), ("lag", "lag_config"), ("lag",)),
    _spec("nsolacpmemberstate", ("lacp",), ("lag.bundle.member",), ("lag", "lag_config"), ("lag",)),
    _spec("nsoswitchportstate", ("switchport",), ("switchport.interface",), ("switchport",), ("switchport",)),
)
OVERLAYS = MappingProxyType({spec.model_label: spec for spec in _SPECS})
EXCLUDED_MODELS = MappingProxyType({"netbox_nso_plugin.nsobgppeertemplatestate": "read_only_not_pushed"})
_SUBJECT_OWNERS = {subject: spec for spec in _SPECS for subject in spec.subjects}
if len(_SUBJECT_OWNERS) != sum(len(spec.subjects) for spec in _SPECS):
    raise RegistryError("duplicate registry subject owner")
# The adapter document carries IP rows in the interface section and flex-algo rows in the IS-IS section.
_SECTION_OWNER = {"ip": "interface", "isis_flex_algo": "isis"}
_INTERFACE_SECTION = delivery_keys()["interface"].section


def _document_section(key: str) -> str:
    return delivery_keys()[_SECTION_OWNER.get(key, key)].section


SECTION_COUNTER_KEYS = MappingProxyType(
    {
        section: tuple(
            sorted(
                {
                    counter
                    for spec in _SPECS
                    for counter in spec.counter_keys
                    if counter == f"{section}_count_by_outcome"
                    or (
                        section == _INTERFACE_SECTION
                        and counter in {"attribute_count_by_outcome", "ip_count_by_outcome"}
                    )
                }
            )
        )
        for section in {_document_section(key) for spec in _SPECS for key in spec.delivery_keys}
    }
)


def model_spec(label: str) -> OverlaySpec:
    try:
        return OVERLAYS[label]
    except (KeyError, TypeError):
        raise RegistryError("model is excluded or unknown") from None


def owner_spec(subject: str) -> OverlaySpec:
    try:
        return _SUBJECT_OWNERS[subject]
    except (KeyError, TypeError):
        raise RegistryError("logical outcome has no pushed owner") from None
