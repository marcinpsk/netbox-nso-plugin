# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Pure acquisition and retirement rules for converted delivery scopes."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace

from django.apps import apps
from django.utils import timezone

ROUTE_POLICY_NATIVE_MODEL_LABELS = MappingProxyType(
    {
        "prefix_list": "netbox_routing.prefixlist",
        "community_list": "netbox_routing.communitylist",
        "as_path": "netbox_routing.aspath",
        "route_map": "netbox_routing.routemap",
    }
)


class OwnershipAction(str, Enum):
    """One state-derived ownership transition."""

    NONE = "none"
    CREATE = "create"
    RECORD_MANIFEST = "record_manifest"
    REOWN = "reown"
    RETRACT = "retract"
    DETACH = "detach"
    RETIRE = "retire"


@dataclass(frozen=True)
class OwnershipSignature:
    """Persisted native, overlay, and manifest facts for one logical object."""

    native_present: bool
    native_qualifies: bool
    overlay_present: bool = False
    overlay_owned: bool = False
    manifest_state: str | None = None

    @property
    def manifest_owned(self) -> bool:
        return self.manifest_state == "owned"


@dataclass(frozen=True)
class ScopeOwnershipRule:
    """Reviewed ownership policy and native identity for one converted scope."""

    scope: str
    acquisition_strategy: str
    native_model_labels: tuple[str, ...]
    native_key_fields: tuple[str, ...]
    overlay_model_labels: tuple[str, ...]
    overlay_native_fields: tuple[tuple[str, str], ...]
    foreign_overlay_delete: str
    deletion_authority: bool
    intentional_semantic_delta: str
    acknowledged_lineage_field: str | None = None
    manifest_scope_field: str | None = None
    native_key_fields_by_model: tuple[tuple[str, tuple[str, ...]], ...] = ()


_CONVERTED_SCOPE_RULES = {
    "lacp": ScopeOwnershipRule(
        scope="lacp",
        acquisition_strategy="native",
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=(
            "netbox_nso_plugin.nsolacpbundlestate",
            "netbox_nso_plugin.nsolacpmemberstate",
        ),
        overlay_native_fields=(
            ("netbox_nso_plugin.nsolacpbundlestate", "interface"),
            ("netbox_nso_plugin.nsolacpmemberstate", "interface"),
        ),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from persisted bundle and member topology instead of save-event provenance."
        ),
    ),
    "vlan": ScopeOwnershipRule(
        scope="vlan",
        acquisition_strategy="native",
        native_model_labels=("ipam.vlan",),
        native_key_fields=("group_id", "vid"),
        overlay_model_labels=("netbox_nso_plugin.nsovlanstate",),
        overlay_native_fields=(("netbox_nso_plugin.nsovlanstate", "vlan"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=("Acquire from persisted VLAN attachment state. Canceling edits need not acquire."),
    ),
    "svi": ScopeOwnershipRule(
        scope="svi",
        acquisition_strategy="native",
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsosvistate",),
        overlay_native_fields=(("netbox_nso_plugin.nsosvistate", "interface"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=("Acquire from persisted SVI interface state instead of save-event provenance."),
    ),
    "switchport": ScopeOwnershipRule(
        scope="switchport",
        acquisition_strategy="native",
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsoswitchportstate",),
        overlay_native_fields=(("netbox_nso_plugin.nsoswitchportstate", "interface"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=("Acquire from current L2 state. M2M edit events are not ownership evidence."),
    ),
    "interface_mtu": ScopeOwnershipRule(
        scope="interface_mtu",
        acquisition_strategy="native",
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsointerfacemtustate",),
        overlay_native_fields=(("netbox_nso_plugin.nsointerfacemtustate", "interface"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from persisted per-interface MTU state. A save event is not ownership evidence."
        ),
    ),
    "subinterface": ScopeOwnershipRule(
        scope="subinterface",
        acquisition_strategy="native",
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsosubinterfacestate",),
        overlay_native_fields=(("netbox_nso_plugin.nsosubinterfacestate", "interface"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from persisted parent and dot1q state. Native save events are not ownership evidence."
        ),
    ),
    "bfd": ScopeOwnershipRule(
        scope="bfd",
        acquisition_strategy="existing_overlay",
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsobfdinterfacestate",),
        overlay_native_fields=(("netbox_nso_plugin.nsobfdinterfacestate", "interface"),),
        foreign_overlay_delete="retire",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from persisted per-interface BFD state. Save events are not ownership evidence. "
            "The BFD timers live only on the overlay, so a foreign overlay delete retires the identity "
            "instead of re-owning it from a native row that carries no BFD content."
        ),
    ),
    "bgp": ScopeOwnershipRule(
        scope="bgp",
        acquisition_strategy="native",
        native_model_labels=("netbox_routing.bgppeer",),
        native_key_fields=("scope_id", "peer_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsobgppeerstate",),
        overlay_native_fields=(("netbox_nso_plugin.nsobgppeerstate", "bgp_peer"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted BGP peer and linked overlay. Native and overlay save events are not ownership "
            "evidence. Foreign native peer deletes no longer delete linked overlays and push a reduced snapshot "
            "synchronously. Greenfield acceptance uses exact acquisition planning and outbox delivery instead of "
            "accepting and pushing directly. Routers, scopes, address families, peer templates, ASNs, and peer IPs "
            "are graph dependencies. BGP reconciliation no longer suppresses missing netbox-routing or IPAM imports; "
            "missing graph dependencies fail fast. BGP foreign-key merge identities use natural graph identities. "
            "Legacy PK-shaped peer and template merge bases are reset before reconciliation."
        ),
    ),
    "interface": ScopeOwnershipRule(
        scope="interface",
        acquisition_strategy="native",
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsointerfacestate",),
        overlay_native_fields=(("netbox_nso_plugin.nsointerfacestate", "interface"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire description and enabled intent from explicit persisted state changes. "
            "Native interface and cable events are not ownership evidence and do not recompute derived values."
        ),
    ),
    "ip": ScopeOwnershipRule(
        scope="ip",
        acquisition_strategy="native",
        native_model_labels=("ipam.ipaddress",),
        native_key_fields=("address", "vrf_id", "assigned_object_type_id", "assigned_object_id"),
        overlay_model_labels=("netbox_nso_plugin.nsointerfaceipstate",),
        overlay_native_fields=(("netbox_nso_plugin.nsointerfaceipstate", "__ip_address__"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from an exact persisted IPAddress and interface-IP state pair. "
            "Native IP save and delete events are not ownership evidence. Reconcile activation and unassignment are atomic."
        ),
    ),
    "l2_sap": ScopeOwnershipRule(
        scope="l2_sap",
        acquisition_strategy="existing_overlay",
        native_model_labels=("netbox_nso_plugin.nsol2sapstate",),
        native_key_fields=("management_id", "service_name", "sap_id"),
        overlay_model_labels=("netbox_nso_plugin.nsol2sapstate",),
        overlay_native_fields=(("netbox_nso_plugin.nsol2sapstate", "__self__"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted SAP overlay because the rendered SAP values live on that row. "
            "VPN and termination mirrors do not establish ownership, and save events are not ownership evidence."
        ),
    ),
    "logging": ScopeOwnershipRule(
        scope="logging",
        acquisition_strategy="existing_overlay",
        native_model_labels=(
            "netbox_nso_plugin.nsologginghoststate",
            "netbox_nso_plugin.nsologginglevelstate",
        ),
        native_key_fields=("management_id", "pk"),
        overlay_model_labels=(
            "netbox_nso_plugin.nsologginghoststate",
            "netbox_nso_plugin.nsologginglevelstate",
        ),
        overlay_native_fields=(
            ("netbox_nso_plugin.nsologginghoststate", "__self__"),
            ("netbox_nso_plugin.nsologginglevelstate", "__self__"),
        ),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=("Acquire from persisted logging rows. Save events are not ownership evidence."),
    ),
    "snmp": ScopeOwnershipRule(
        scope="snmp",
        acquisition_strategy="existing_overlay",
        native_model_labels=(
            "netbox_nso_plugin.nsosnmpcommunitystate",
            "netbox_nso_plugin.nsosnmpv3userstate",
            "netbox_nso_plugin.nsosnmphoststate",
            "netbox_nso_plugin.nsosnmpsysteminfostate",
        ),
        native_key_fields=("management_id", "pk"),
        overlay_model_labels=(
            "netbox_nso_plugin.nsosnmpcommunitystate",
            "netbox_nso_plugin.nsosnmpv3userstate",
            "netbox_nso_plugin.nsosnmphoststate",
            "netbox_nso_plugin.nsosnmpsysteminfostate",
        ),
        overlay_native_fields=(
            ("netbox_nso_plugin.nsosnmpcommunitystate", "__self__"),
            ("netbox_nso_plugin.nsosnmpv3userstate", "__self__"),
            ("netbox_nso_plugin.nsosnmphoststate", "__self__"),
            ("netbox_nso_plugin.nsosnmpsysteminfostate", "__self__"),
        ),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=("Acquire from persisted SNMP rows. Save events are not ownership evidence."),
    ),
    "static_route": ScopeOwnershipRule(
        scope="static_route",
        acquisition_strategy="native",
        native_model_labels=("netbox_routing.staticroute",),
        native_key_fields=("vrf_id", "prefix", "next_hop", "interface_next_hop"),
        overlay_model_labels=("netbox_nso_plugin.nsostaticroutestate",),
        overlay_native_fields=(("netbox_nso_plugin.nsostaticroutestate", "static_route"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted route assignment and overlay. Native route and assignment events are not "
            "ownership evidence. Deletion authority carries only the adapter-acknowledged route triple."
        ),
        acknowledged_lineage_field="last_acked_triple",
    ),
    "isis_flex_algo": ScopeOwnershipRule(
        scope="isis_flex_algo",
        acquisition_strategy="native",
        native_model_labels=("netbox_routing.isisflexalgo",),
        native_key_fields=("instance_id", "algo_id"),
        overlay_model_labels=("netbox_nso_plugin.nsoisisflexalgostate",),
        overlay_native_fields=(("netbox_nso_plugin.nsoisisflexalgostate", "isis_flex_algo"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted Flex-Algo and linked overlay. Native Flex-Algo save and delete events are not "
            "ownership evidence."
        ),
    ),
    "redistribution": ScopeOwnershipRule(
        scope="redistribution",
        acquisition_strategy="native",
        native_model_labels=("netbox_routing.redistribution",),
        native_key_fields=(
            "destination_type_id",
            "destination_id",
            "source_protocol",
            "source_ref",
        ),
        overlay_model_labels=("netbox_nso_plugin.nsoredistributionstate",),
        overlay_native_fields=(("netbox_nso_plugin.nsoredistributionstate", "redistribution"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted destination-specific redistribution and linked overlay. Native and overlay "
            "save events are not ownership evidence. The manifest delivery scope comes from the destination protocol."
        ),
        manifest_scope_field="dest_protocol",
    ),
    "route_policy": ScopeOwnershipRule(
        scope="route_policy",
        acquisition_strategy="existing_overlay",
        native_model_labels=(
            "netbox_routing.prefixlist",
            "netbox_routing.communitylist",
            "netbox_routing.aspath",
            "netbox_routing.routemap",
        ),
        native_key_fields=("name",),
        overlay_model_labels=("netbox_nso_plugin.nsoroutepolicystate",),
        overlay_native_fields=(("netbox_nso_plugin.nsoroutepolicystate", "assigned_object"),),
        foreign_overlay_delete="retire",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted named policy root and its linked device overlay. Native root, entry, M2M, "
            "and through-row events are not ownership evidence. Native policy deletes no longer delete per-device "
            "overlays and push reduced snapshots synchronously. Acceptance and contributor cascades use exact "
            "acquisition planning and outbox delivery instead of owning and pushing directly. Entries and references "
            "are graph dependencies. The per-device content hash lives only on the overlay, so a foreign overlay "
            "delete retires the identity instead of re-owning it from the shared policy root."
        ),
    ),
    "isis": ScopeOwnershipRule(
        scope="isis",
        acquisition_strategy="native",
        native_model_labels=(
            "netbox_routing.isisinstance",
            "netbox_routing.isisinterface",
        ),
        native_key_fields=(),
        native_key_fields_by_model=(
            ("netbox_routing.isisinstance", ("device_id", "process_tag")),
            ("netbox_routing.isisinterface", ("interface_id", "address_family")),
        ),
        overlay_model_labels=(
            "netbox_nso_plugin.nsoisisinstancestate",
            "netbox_nso_plugin.nsoisisinterfacestate",
        ),
        overlay_native_fields=(
            ("netbox_nso_plugin.nsoisisinstancestate", "isis_instance"),
            ("netbox_nso_plugin.nsoisisinterfacestate", "isis_interface"),
        ),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted native process or interface and its linked overlay. Native and overlay save "
            "events are not ownership evidence. Native interface edits no longer refresh owned overlays. Native "
            "interface deletes no longer delete overlays and push retirement synchronously. ISISLevel edits and "
            "deletes no longer re-push immediately. Reconciliation and ownership audits handle these changes. "
            "Settings, levels, Segment Routing, Flex-Algo, Prefix-SID, and SRv6 locator rows are graph dependencies, "
            "not independently owned device objects."
        ),
    ),
    "ospf": ScopeOwnershipRule(
        scope="ospf",
        acquisition_strategy="native",
        native_model_labels=(
            "netbox_routing.ospfinstance",
            "netbox_routing.ospfinterface",
        ),
        native_key_fields=(),
        native_key_fields_by_model=(
            ("netbox_routing.ospfinstance", ("device_id", "process_id")),
            ("netbox_routing.ospfinterface", ("interface_id",)),
        ),
        overlay_model_labels=(
            "netbox_nso_plugin.nsoospfinstancestate",
            "netbox_nso_plugin.nsoospfinterfacestate",
        ),
        overlay_native_fields=(
            ("netbox_nso_plugin.nsoospfinstancestate", "ospf_instance"),
            ("netbox_nso_plugin.nsoospfinterfacestate", "__ospf_interface__"),
        ),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted native process or interface and its overlay. Native and overlay save events "
            "are not ownership evidence and no longer create or refresh owned overlays. Native process and interface "
            "deletes no longer delete overlays and push retirement synchronously. Reconciliation and ownership "
            "audits handle these changes. A shared OSPF area is a dependency, not a device-owned object."
        ),
    ),
}


def converted_scope_rules() -> dict[str, ScopeOwnershipRule]:
    """Return a copy of the reviewed first-tranche rule table."""
    return dict(_CONVERTED_SCOPE_RULES)


def plan_ownership(rule: ScopeOwnershipRule, signature: OwnershipSignature) -> OwnershipAction:
    """Return the pure ownership action for one persisted state signature."""
    if signature.manifest_owned:
        if not signature.native_present or not signature.native_qualifies:
            return OwnershipAction.RETRACT
        if not signature.overlay_present:
            return OwnershipAction(rule.foreign_overlay_delete)
        if not signature.overlay_owned:
            return OwnershipAction.DETACH
        return OwnershipAction.NONE

    if signature.manifest_state in {"detached", "retired"}:
        return OwnershipAction.NONE

    if signature.overlay_owned:
        if signature.native_present and signature.native_qualifies:
            return OwnershipAction.RECORD_MANIFEST
        return OwnershipAction.RETRACT
    if not signature.native_present or not signature.native_qualifies:
        return OwnershipAction.NONE
    # An unowned overlay is the device read the operator has not accepted yet. Only the
    # operator Accept enters ownership, so a qualifying native anchor never promotes it.
    if signature.overlay_present:
        return OwnershipAction.NONE
    return OwnershipAction.CREATE


def retire_manifest_identity(*, device_ids, scope, native_model_label, native_key) -> None:
    """Retire owned manifest entries after an own authoritative native replacement."""
    from .models import NSOOwnershipManifest

    NSOOwnershipManifest.objects.filter(
        device_id__in=set(device_ids),
        scope=scope,
        native_model_label=native_model_label,
        native_key=native_key,
        ownership_state="owned",
    ).update(ownership_state="retired")


def detach_device_manifests(device_id: int) -> None:
    """Detach each owned manifest identity for a direct management teardown."""
    from .models import NSOOwnershipManifest

    NSOOwnershipManifest.objects.filter(
        device_id=device_id,
        ownership_state="owned",
    ).update(ownership_state="detached")


def retire_device_manifests(device_id: int) -> None:
    """Retire all owned manifest identities for a device teardown."""
    from .models import NSOOwnershipManifest

    NSOOwnershipManifest.objects.filter(
        device_id=device_id,
        ownership_state="owned",
    ).update(ownership_state="retired")


def _validated_native_key_fields(native, rule):
    """Return validated native identity fields for one ownership rule."""
    native_label = native._meta.label_lower
    if native_label not in rule.native_model_labels:
        return None
    key_fields = dict(rule.native_key_fields_by_model).get(native_label, rule.native_key_fields)
    for name in key_fields:
        native._meta.get_field(native._meta.pk.name if name == "pk" else name)
    return native_label, key_fields


def manifest_binding(instance):
    """Return the durable manifest identity for one overlay instance."""
    label = instance._meta.label_lower
    for rule in converted_scope_rules().values():
        native_field = dict(rule.overlay_native_fields).get(label)
        if native_field is None:
            continue
        native = _manifest_native(instance, native_field)
        management = _manifest_management(instance)
        if native is None or management is None:
            return None
        key_fields = dict(rule.native_key_fields_by_model).get(native._meta.label_lower, rule.native_key_fields)
        native_key = {name: _json_value(getattr(native, name)) for name in key_fields}
        state_key = _manifest_state_key(instance, native_field)
        scope = getattr(instance, rule.manifest_scope_field) if rule.manifest_scope_field else rule.scope
        return (
            rule,
            scope,
            management.device_id,
            native._meta.label_lower,
            native.pk,
            native_key,
            instance._meta.label_lower,
            state_key,
        )
    return None


def _manifest_native(instance, native_field):
    if native_field == "__self__":
        return instance
    if native_field == "__ip_address__":
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VRF, IPAddress

        interface_type = ContentType.objects.get_for_model(Interface)
        vrf_name = getattr(instance, "vrf", "")
        vrf_id = VRF.objects.filter(name=vrf_name).values_list("pk", flat=True).first() if vrf_name else None
        if vrf_name and vrf_id is None:
            return None
        return IPAddress.objects.filter(
            address=instance.address,
            vrf_id=vrf_id,
            assigned_object_type=interface_type,
            assigned_object_id=instance.interface_id,
        ).first()
    if native_field == "__ospf_interface__":
        from netbox_routing.models import OSPFInterface

        return OSPFInterface.objects.filter(interface_id=instance.interface_id).first()
    return getattr(instance, native_field, None)


def _manifest_management(instance):
    management = getattr(instance, "management", None)
    if management is not None:
        return management
    interface = getattr(instance, "interface", None)
    if interface is None:
        return None
    from .models import NSODeviceManagement

    return NSODeviceManagement.objects.filter(device_id=interface.device_id).first()


def _manifest_state_key(instance, native_field):
    unique_fields = next(
        (
            tuple(fields)
            for fields in instance._meta.unique_together
            if "management" in fields or native_field in fields
        ),
        (),
    )
    excluded = {
        "__ip_address__": {"interface", "address", "vrf"},
        "__ospf_interface__": {"interface"},
    }.get(native_field, set()) | {"management", native_field}
    state_key = {}
    for name in unique_fields:
        if name in excluded:
            continue
        field = instance._meta.get_field(name)
        key_name = field.attname if field.is_relation else name
        state_key[key_name] = _json_value(getattr(instance, key_name))
    return state_key


def maintain_manifest(instance) -> None:
    """Make one overlay's manifest agree with its persisted ownership state."""
    from . import status_machine as sm
    from .models import NSOOwnershipManifest

    binding = manifest_binding(instance)
    if binding is None:
        return
    rule, scope, device_id, native_model_label, native_id, native_key, state_model_label, state_key = binding
    identity = {
        "device_id": device_id,
        "scope": scope,
        "native_model_label": native_model_label,
        "native_key": native_key,
        "state_model_label": state_model_label,
        "state_key": state_key,
    }
    base_identity = {
        "device_id": device_id,
        "scope": scope,
        "native_model_label": native_model_label,
        "native_key": native_key,
    }
    if sm.is_owned(instance.status):
        lineage = (
            getattr(instance, rule.acknowledged_lineage_field, None)
            if rule.acknowledged_lineage_field is not None
            else None
        )
        defaults = {
            "native_id": native_id,
            "ownership_state": "owned",
            "deletion_authority": rule.deletion_authority,
        }
        if lineage is not None:
            defaults["acknowledged_lineage"] = [copy.deepcopy(lineage)]
        incarnation = {
            "device_id": device_id,
            "scope": scope,
            "native_model_label": native_model_label,
            "native_id": native_id,
            "state_model_label": state_model_label,
            "state_key": state_key,
        }
        NSOOwnershipManifest.objects.filter(
            **incarnation,
            ownership_state="owned",
        ).exclude(native_key=native_key).update(ownership_state="retired")
        exact = NSOOwnershipManifest.objects.filter(**identity).first()
        if exact is not None:
            NSOOwnershipManifest.objects.filter(pk=exact.pk).exclude(ownership_state="retired").update(**defaults)
            return
        previous = (
            NSOOwnershipManifest.objects.filter(
                **incarnation,
                ownership_state="retired",
            )
            .order_by("-pk")
            .first()
        )
        if previous is not None:
            NSOOwnershipManifest.objects.filter(pk=previous.pk).update(
                native_key=native_key,
                **defaults,
            )
            return
        legacy = NSOOwnershipManifest.objects.filter(
            **base_identity,
            state_model_label="",
            state_key={},
        ).exclude(ownership_state="retired").first()
        if legacy is not None:
            NSOOwnershipManifest.objects.filter(pk=legacy.pk).update(
                state_model_label=state_model_label,
                state_key=state_key,
                **defaults,
            )
            return
        NSOOwnershipManifest.objects.create(**identity, **defaults)
    else:
        NSOOwnershipManifest.objects.filter(**identity, ownership_state="owned").update(ownership_state="detached")


def retire_overlay_manifest(instance) -> None:
    """Retire the durable identity after an exact writer-owned overlay delete."""
    from .models import NSOOwnershipManifest

    binding = manifest_binding(instance)
    if binding is None:
        return
    _rule, scope, device_id, native_model_label, _native_id, native_key, state_model_label, state_key = binding
    NSOOwnershipManifest.objects.filter(
        device_id=device_id,
        scope=scope,
        native_model_label=native_model_label,
        native_key=native_key,
        state_model_label=state_model_label,
        state_key=state_key,
        ownership_state="owned",
    ).update(ownership_state="retired")


def _manifest_state_key(scope, native_model_label, native_key, state_model_label, state_key):
    return (
        scope,
        native_model_label,
        json.dumps(native_key, sort_keys=True),
        state_model_label,
        json.dumps(state_key, sort_keys=True),
    )


def _manifest_states(device_id, requested):
    from .models import NSOOwnershipManifest

    return {
        _manifest_state_key(scope, native_model_label, native_key, state_model_label, state_key): ownership_state
        for scope, native_model_label, native_key, state_model_label, state_key, ownership_state in (
            NSOOwnershipManifest.objects.filter(
                device_id=device_id,
                scope__in=tuple(requested),
            ).values_list(
                "scope",
                "native_model_label",
                "native_key",
                "state_model_label",
                "state_key",
                "ownership_state",
            )
        )
    }


def _record_action_for(instance, device_id, requested, qualifying, manifest_states):
    """Return one overlay's planned record action, or ``None`` when it needs no work."""
    from .status_machine import is_owned

    binding = manifest_binding(instance)
    if binding is None:
        return None
    (
        rule,
        scope,
        bound_device_id,
        native_model_label,
        native_id,
        native_key,
        state_model_label,
        state_key,
    ) = binding
    if scope not in requested or bound_device_id != device_id:
        return None
    manifest_state = manifest_states.get(
        _manifest_state_key(scope, native_model_label, native_key, state_model_label, state_key)
    )
    action = plan_ownership(
        rule,
        OwnershipSignature(
            native_present=True,
            native_qualifies=(
                is_owned(instance.status)
                if rule.acquisition_strategy == "existing_overlay"
                else (
                    scope,
                    native_model_label,
                    native_id,
                    state_model_label,
                    json.dumps(state_key, sort_keys=True),
                )
                in qualifying
            ),
            overlay_present=True,
            overlay_owned=is_owned(instance.status),
            manifest_state=manifest_state,
        ),
    )
    if action is not OwnershipAction.RECORD_MANIFEST:
        return None
    return (scope, instance._meta.label_lower, instance.pk)


def _device_overlays(device_id, requested):
    """Yield each of the device's status-carrying overlay rows exactly once."""
    from .intent_state import OVERLAY_MODEL_RANKS, renderer_input_specs

    seen = set()
    for spec in renderer_input_specs().values():
        if requested.isdisjoint(spec.scopes) or spec.model_label not in OVERLAY_MODEL_RANKS:
            continue
        model = apps.get_model(spec.model_label)
        fields = {field.name for field in model._meta.concrete_fields}
        if "status" not in fields:
            continue
        if "management" in fields:
            rows = model.objects.filter(management__device_id=device_id)
        elif "interface" in fields:
            rows = model.objects.filter(interface__device_id=device_id)
        else:
            continue
        for instance in rows.order_by("pk"):
            identity = (instance._meta.label_lower, instance.pk)
            if identity in seen:
                continue
            seen.add(identity)
            yield instance


def _manifest_record_actions(device_id, requested):
    """Return owned overlays whose durable manifest evidence is absent."""
    qualifying = _qualifying_overlay_signatures(device_id, requested)
    manifest_states = _manifest_states(device_id, requested)
    planned = []
    for instance in _device_overlays(device_id, requested):
        action = _record_action_for(instance, device_id, requested, qualifying, manifest_states)
        if action is not None:
            planned.append(action)
    return tuple(planned)


def _rule_for_manifest(manifest):
    """Resolve one manifest to its reviewed scope rule."""
    for rule in converted_scope_rules().values():
        if manifest.native_model_label not in rule.native_model_labels:
            continue
        if rule.manifest_scope_field is None and rule.scope != manifest.scope:
            continue
        return rule
    return None


def _native_for_manifest(manifest, rule):
    """Resolve a surviving native row without relying on a cascading relation."""
    model = apps.get_model(manifest.native_model_label)
    if manifest.native_id is not None:
        native = model.objects.filter(pk=manifest.native_id).first()
    else:
        key_fields = dict(rule.native_key_fields_by_model).get(
            manifest.native_model_label,
            rule.native_key_fields,
        )
        filters = {name: manifest.native_key.get(name) for name in key_fields}
        native = model.objects.filter(**filters).first() if filters else None
    if native is None:
        return None
    key_fields = dict(rule.native_key_fields_by_model).get(
        manifest.native_model_label,
        rule.native_key_fields,
    )
    current_key = {name: _json_value(getattr(native, name)) for name in key_fields}
    return native if current_key == manifest.native_key else None


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _manifest_identity_is_complete(manifest) -> bool:
    """Return whether one manifest can still name its native row and its overlay."""
    return manifest.native_id is not None and bool(manifest.state_model_label)


def _state_filters(manifest, rule, native, management):
    model = apps.get_model(manifest.state_model_label)
    native_field = dict(rule.overlay_native_fields)[model._meta.label_lower]
    fields = {field.name for field in model._meta.concrete_fields}
    filters = dict(manifest.state_key)
    if "management" in fields:
        filters["management"] = management
    if native_field == "__self__":
        filters["pk"] = native.pk
    elif native_field == "__ip_address__":
        filters.update(
            interface_id=native.assigned_object_id,
            address=str(native.address),
            vrf=native.vrf.name if native.vrf_id else "",
        )
    elif native_field == "__ospf_interface__":
        filters["interface_id"] = native.interface_id
    elif native_field == "assigned_object":
        from django.contrib.contenttypes.models import ContentType

        filters.update(
            content_type=ContentType.objects.get_for_model(native),
            object_id=native.pk,
        )
    else:
        filters[native_field] = native
    return model, filters


def _seed_reowned_state(candidate, native, native_field, manifest):
    """Populate fields whose desired value lives on the surviving native row."""
    candidate.status = "accepted"
    if hasattr(candidate, "accepted_at"):
        candidate.accepted_at = timezone.now()
    for field in candidate._meta.concrete_fields:
        if field.primary_key or field.name in {
            "management",
            "status",
            "accepted_at",
            native_field,
        }:
            continue
        if field.name in manifest.state_key:
            setattr(candidate, field.name, manifest.state_key[field.name])
            continue
        if hasattr(native, field.name) and not field.many_to_many:
            value = getattr(native, field.name)
            if not field.is_relation or value is None or isinstance(value, field.related_model):
                if value is None and not field.null:
                    if not field.empty_strings_allowed:
                        continue
                    value = ""
                setattr(candidate, field.name, value)
    seeder = _STATE_SEEDERS.get(candidate._meta.label_lower)
    if seeder is not None:
        seeder(candidate, native, manifest)


def _seed_vlan(candidate, native, _manifest):
    candidate.device_name = native.name


def _seed_svi(candidate, native, _manifest):
    definition = _svi_definition(native)
    if definition is not None:
        candidate.svi_type, vlan_id = definition
        candidate.vlan = _device_vlan(native.device_id, vlan_id, preferred=native.untagged_vlan)
    candidate.vrf = native.vrf.name if native.vrf_id else ""


def _seed_switchport(candidate, native, _manifest):
    candidate.mode = native.mode or ""
    candidate.untagged_vlan = native.untagged_vlan


def _seed_static_route(candidate, native, manifest):
    candidate.nso_vrf = native.vrf.name if native.vrf_id else ""
    candidate.nso_prefix = str(native.prefix or "")
    candidate.nso_next_hop = str(native.next_hop or "")
    candidate.last_acked_triple = (
        copy.deepcopy(manifest.acknowledged_lineage[-1]) if manifest.acknowledged_lineage else None
    )
    from .signals import _arm_static_route_generation

    _arm_static_route_generation(candidate)


def _seed_interface_attribute(candidate, native, _manifest):
    value = getattr(native, candidate.attribute)
    candidate.nso_value = str(value).lower() if isinstance(value, bool) else str(value)


def _seed_interface_ip(candidate, native, _manifest):
    candidate.family = f"ipv{native.address.version}"
    candidate.vrf = native.vrf.name if native.vrf_id else ""


def _seed_subinterface(candidate, native, _manifest):
    candidate.parent_interface = native.parent
    suffix = native.name.rsplit(".", 1)[-1]
    candidate.dot1q_vlan = int(suffix) if suffix.isdigit() else None
    candidate.vrf = native.vrf.name if native.vrf_id else ""


def _seed_interface_mtu(candidate, native, _manifest):
    candidate.l2_mtu = native.mtu


def _seed_lacp_member(candidate, native, _manifest):
    candidate.lag_bundle = native.lag
    candidate.mode = ""


def _seed_lacp_bundle(candidate, native, _manifest):
    suffix = "".join(character for character in native.name if character.isdigit())
    candidate.lag_id = int(suffix) if suffix else None


def _seed_flex_algo(candidate, native, _manifest):
    candidate.process_tag = native.instance.process_tag


def _seed_bgp_peer(candidate, native, _manifest):
    candidate.asn_str = str(native.scope.router.asn.asn)
    candidate.vrf_name = native.scope.vrf.name if native.scope.vrf_id else ""
    candidate.peer_address_str = str(native.peer.address.ip)
    candidate.remote_as_str = str(native.remote_as.asn) if native.remote_as_id else ""
    candidate.enabled = native.enabled


def _seed_isis_instance(candidate, native, _manifest):
    candidate.area_auth_present = bool(native.area_auth_key)
    candidate.domain_auth_present = bool(native.domain_auth_key)


def _seed_isis_interface(candidate, native, _manifest):
    candidate.af = native.address_family
    candidate.process_tag = native.instance.process_tag
    candidate.hello_auth_present = bool(native.hello_auth_key)


def _seed_ospf_instance(candidate, native, _manifest):
    candidate.router_id = str(native.router_id or "")
    candidate.vrf = native.vrf.name if native.vrf_id else ""
    candidate.areas = []
    candidate.enabled = True


def _seed_ospf_interface(candidate, native, _manifest):
    candidate.process_id = str(native.instance.process_id)
    candidate.area_id = str(native.area.area_id)
    candidate.auth_type = native.authentication or ""
    candidate.auth_present = bool(native.passphrase)


def _seed_redistribution(candidate, native, _manifest):
    destination = _redistribution_destination_identity(native)
    if destination is not None:
        candidate.dest_protocol, candidate.dest_ref, _device_id = destination
    candidate.source_protocol = native.source_protocol
    candidate.source_ref = native.source_ref or ""
    candidate.route_map = native.route_map.name if native.route_map_id else ""


_STATE_SEEDERS = {
    "netbox_nso_plugin.nsobgppeerstate": _seed_bgp_peer,
    "netbox_nso_plugin.nsointerfaceipstate": _seed_interface_ip,
    "netbox_nso_plugin.nsointerfacemtustate": _seed_interface_mtu,
    "netbox_nso_plugin.nsointerfacestate": _seed_interface_attribute,
    "netbox_nso_plugin.nsoisisflexalgostate": _seed_flex_algo,
    "netbox_nso_plugin.nsoisisinstancestate": _seed_isis_instance,
    "netbox_nso_plugin.nsoisisinterfacestate": _seed_isis_interface,
    "netbox_nso_plugin.nsolacpbundlestate": _seed_lacp_bundle,
    "netbox_nso_plugin.nsolacpmemberstate": _seed_lacp_member,
    "netbox_nso_plugin.nsoospfinstancestate": _seed_ospf_instance,
    "netbox_nso_plugin.nsoospfinterfacestate": _seed_ospf_interface,
    "netbox_nso_plugin.nsoredistributionstate": _seed_redistribution,
    "netbox_nso_plugin.nsostaticroutestate": _seed_static_route,
    "netbox_nso_plugin.nsosubinterfacestate": _seed_subinterface,
    "netbox_nso_plugin.nsosvistate": _seed_svi,
    "netbox_nso_plugin.nsoswitchportstate": _seed_switchport,
    "netbox_nso_plugin.nsovlanstate": _seed_vlan,
}


def _reown_manifest(manifest, rule, native, *, revoke=True):
    from .models import NSODeviceManagement
    from .renderer_writer import RendererMutationPlan, planned_m2m_set, planned_save, renderer_writes

    management = NSODeviceManagement.objects.filter(device_id=manifest.device_id).first()
    if management is None:
        return None
    model, filters = _state_filters(manifest, rule, native, management)
    if model.objects.filter(**filters).exists():
        return None
    candidate = model(**filters)
    native_field = dict(rule.overlay_native_fields)[model._meta.label_lower]
    _seed_reowned_state(candidate, native, native_field, manifest)
    natural_key = next((tuple(fields) for fields in model._meta.unique_together), ())
    m2m_writes = ()
    if candidate._meta.label_lower == "netbox_nso_plugin.nsoswitchportstate":
        m2m_writes = (planned_m2m_set(candidate, "tagged_vlans", tuple(native.tagged_vlans.all())),)
    plan = RendererMutationPlan.build(
        saves=(planned_save(candidate, force_insert=True, natural_key=natural_key),),
        m2m_writes=m2m_writes,
        planned_at=getattr(candidate, "accepted_at", None),
    )
    with renderer_writes(plan) as writer:
        writer.save(candidate, force_insert=True)
        if m2m_writes:
            writer.m2m_set(candidate, "tagged_vlans", tuple(native.tagged_vlans.all()))
        if revoke and manifest.scope == "static_route" and manifest.native_id is not None:
            from . import outbox

            carried = manifest.acknowledged_lineage[-1] if manifest.acknowledged_lineage else None
            outbox.enqueue(
                manifest.device_id,
                manifest.scope,
                transitions=[outbox.revoke_transition(manifest.native_id, carried_triple=carried)],
            )
    return candidate


def _native_identity(rule, native):
    key_fields = dict(rule.native_key_fields_by_model).get(
        native._meta.label_lower,
        rule.native_key_fields,
    )
    return {name: _json_value(getattr(native, name)) for name in key_fields}


def _device_vlan(device_id, vid, *, preferred=None):
    """Resolve the VLAN carried by one native SVI interface."""
    if preferred is not None and preferred.vid == vid:
        return preferred
    from ipam.models import VLAN

    return VLAN.objects.filter(group__slug=f"nso-{device_id}", vid=vid).first()


def _svi_definition(interface):
    """Return ``(type, vid)`` for a native SVI or IRB interface."""
    name = (interface.name or "").lower()
    if name.startswith("vlan") and name[4:].isdigit():
        return "svi", int(name[4:])
    if name.startswith("irb.") and name[4:].isdigit():
        return "irb", int(name[4:])
    return None


def _native_binding(scope, native, state_model_label, state_key=None):
    return scope, native, state_model_label, state_key or {}


def _interface_attribute_bindings(management):
    from dcim.models import Interface

    attributes = tuple(management.managed_attributes)
    return tuple(
        _native_binding(
            "interface",
            interface,
            "netbox_nso_plugin.nsointerfacestate",
            {"attribute": attribute},
        )
        for interface in Interface.objects.filter(device_id=management.device_id).order_by("pk")
        for attribute in attributes
    )


def _lacp_bindings(management):
    from dcim.models import Interface

    bundles = (
        _native_binding("lacp", row, "netbox_nso_plugin.nsolacpbundlestate")
        for row in Interface.objects.filter(device_id=management.device_id, type="lag").order_by("pk")
    )
    members = (
        _native_binding("lacp", row, "netbox_nso_plugin.nsolacpmemberstate")
        for row in Interface.objects.filter(device_id=management.device_id, lag_id__isnull=False).order_by("pk")
    )
    return (*bundles, *members)


def _vlan_bindings(management):
    from ipam.models import VLAN

    return tuple(
        _native_binding("vlan", row, "netbox_nso_plugin.nsovlanstate")
        for row in VLAN.objects.filter(group__slug=f"nso-{management.device_id}").order_by("pk")
    )


def _svi_bindings(management):
    from dcim.models import Interface

    bindings = []
    for interface in Interface.objects.filter(device_id=management.device_id, type="virtual").order_by("pk"):
        definition = _svi_definition(interface)
        if definition is None:
            continue
        _svi_type, vid = definition
        if _device_vlan(management.device_id, vid, preferred=interface.untagged_vlan) is not None:
            bindings.append(_native_binding("svi", interface, "netbox_nso_plugin.nsosvistate"))
    return tuple(bindings)


def _switchport_bindings(management):
    from dcim.models import Interface
    from django.db.models import Q

    interfaces = (
        Interface.objects.filter(device_id=management.device_id)
        .filter(Q(mode__gt="") | Q(untagged_vlan_id__isnull=False) | Q(tagged_vlans__isnull=False))
        .distinct()
        .order_by("pk")
    )
    return tuple(_native_binding("switchport", row, "netbox_nso_plugin.nsoswitchportstate") for row in interfaces)


def _interface_mtu_bindings(management):
    from dcim.models import Interface

    return tuple(
        _native_binding("interface_mtu", row, "netbox_nso_plugin.nsointerfacemtustate")
        for row in Interface.objects.filter(device_id=management.device_id, mtu__isnull=False).order_by("pk")
    )


def _subinterface_bindings(management):
    from dcim.models import Interface

    return tuple(
        _native_binding("subinterface", row, "netbox_nso_plugin.nsosubinterfacestate")
        for row in Interface.objects.filter(device_id=management.device_id, parent_id__isnull=False).order_by("pk")
        if "." in row.name and row.name.rsplit(".", 1)[-1].isdigit()
    )


def _ip_bindings(management):
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import IPAddress

    interface_type = ContentType.objects.get_for_model(Interface)
    rows = IPAddress.objects.filter(
        assigned_object_type=interface_type,
        assigned_object_id__in=Interface.objects.filter(device_id=management.device_id).values("pk"),
    ).order_by("pk")
    return tuple(_native_binding("ip", row, "netbox_nso_plugin.nsointerfaceipstate") for row in rows)


def _static_route_bindings(management):
    from netbox_routing.models import StaticRoute

    rows = (
        StaticRoute.objects.filter(devices__id=management.device_id, next_hop__isnull=False).distinct().order_by("pk")
    )
    return tuple(_native_binding("static_route", row, "netbox_nso_plugin.nsostaticroutestate") for row in rows)


def _flex_algo_bindings(management):
    from netbox_routing.models import ISISFlexAlgo

    return tuple(
        _native_binding(
            "isis_flex_algo",
            row,
            "netbox_nso_plugin.nsoisisflexalgostate",
            {"process_tag": row.instance.process_tag, "algo_id": row.algo_id},
        )
        for row in ISISFlexAlgo.objects.filter(instance__device_id=management.device_id)
        .select_related("instance")
        .order_by("pk")
    )


def _bgp_bindings(management):
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import BGPPeer

    device_type = ContentType.objects.get_for_model(Device)
    rows = BGPPeer.objects.filter(
        scope__router__assigned_object_type=device_type,
        scope__router__assigned_object_id=management.device_id,
    ).select_related("scope__router__asn", "scope__vrf", "peer")
    return tuple(
        _native_binding(
            "bgp",
            row,
            "netbox_nso_plugin.nsobgppeerstate",
            {
                "asn_str": str(row.scope.router.asn.asn),
                "vrf_name": row.scope.vrf.name if row.scope.vrf_id else "",
                "peer_address_str": str(row.peer.address.ip),
            },
        )
        for row in rows.order_by("pk")
    )


def _isis_bindings(management):
    from netbox_routing.models import ISISInstance, ISISInterface

    instances = (
        _native_binding(
            "isis",
            row,
            "netbox_nso_plugin.nsoisisinstancestate",
            {"process_tag": row.process_tag},
        )
        for row in ISISInstance.objects.filter(device_id=management.device_id).order_by("pk")
    )
    interfaces = (
        _native_binding(
            "isis",
            row,
            "netbox_nso_plugin.nsoisisinterfacestate",
            {"interface_id": row.interface_id, "af": row.address_family},
        )
        for row in ISISInterface.objects.filter(interface__device_id=management.device_id)
        .select_related("instance", "interface")
        .order_by("pk")
    )
    return (*instances, *interfaces)


def _ospf_bindings(management):
    from netbox_routing.models import OSPFInstance, OSPFInterface

    instances = (
        _native_binding(
            "ospf",
            row,
            "netbox_nso_plugin.nsoospfinstancestate",
            {"process_id": str(row.process_id)},
        )
        for row in OSPFInstance.objects.filter(device_id=management.device_id).order_by("pk")
    )
    interfaces = (
        _native_binding("ospf", row, "netbox_nso_plugin.nsoospfinterfacestate")
        for row in OSPFInterface.objects.filter(interface__device_id=management.device_id)
        .select_related("instance", "area", "interface")
        .order_by("pk")
    )
    return (*instances, *interfaces)


def _destination_reference(destination):
    """Return ``(scope, reference)`` for one redistribution destination root."""
    label = destination._meta.label_lower
    if label == "netbox_routing.ospfinstance":
        return "ospf", str(destination.process_id)
    if label == "netbox_routing.isisinstance":
        return "isis", destination.process_tag or ""
    if label == "netbox_routing.bgpaddressfamily":
        router = destination.scope.router
        vrf = destination.scope.vrf.name if destination.scope.vrf_id else ""
        return "bgp", f"{router.asn.asn}/{vrf}/{destination.address_family}"
    return None


def _redistribution_destination_identity(redistribution):
    destination = redistribution.destination
    reference = _destination_reference(destination)
    if reference is None:
        return None
    if destination._meta.label_lower == "netbox_routing.bgpaddressfamily":
        return (*reference, destination.scope.router.assigned_object_id)
    return (*reference, destination.device_id)


def _redistribution_destinations(management, requested):
    """Map this device's redistribution destination roots to their scope and reference."""
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import BGPAddressFamily, ISISInstance, OSPFInstance

    destinations = {}
    for scope, model, roots in (
        ("ospf", OSPFInstance, OSPFInstance.objects.filter(device_id=management.device_id)),
        ("isis", ISISInstance, ISISInstance.objects.filter(device_id=management.device_id)),
        (
            "bgp",
            BGPAddressFamily,
            BGPAddressFamily.objects.filter(
                scope__router__assigned_object_type=ContentType.objects.get_for_model(Device),
                scope__router__assigned_object_id=management.device_id,
            ).select_related("scope__router__asn", "scope__vrf"),
        ),
    ):
        if scope not in requested:
            continue
        content_type_id = ContentType.objects.get_for_model(model).pk
        for root in roots:
            destinations[(content_type_id, root.pk)] = _destination_reference(root)
    return destinations


def _redistribution_bindings(management, requested):
    from django.db.models import Q
    from netbox_routing.models import Redistribution

    destinations = _redistribution_destinations(management, requested)
    if not destinations:
        return ()
    predicate = Q()
    for content_type_id, object_id in destinations:
        predicate |= Q(destination_type_id=content_type_id, destination_id=object_id)
    bindings = []
    for row in Redistribution.objects.filter(predicate).select_related("route_map").order_by("pk"):
        scope, destination_ref = destinations[(row.destination_type_id, row.destination_id)]
        state_key = {
            "dest_protocol": scope,
            "dest_ref": destination_ref,
            "source_protocol": row.source_protocol,
            "source_ref": row.source_ref or "",
        }
        bindings.append(_native_binding(scope, row, "netbox_nso_plugin.nsoredistributionstate", state_key))
    return tuple(bindings)


_NATIVE_BINDING_BUILDERS = {
    "bgp": _bgp_bindings,
    "interface": _interface_attribute_bindings,
    "interface_mtu": _interface_mtu_bindings,
    "ip": _ip_bindings,
    "isis": _isis_bindings,
    "isis_flex_algo": _flex_algo_bindings,
    "lacp": _lacp_bindings,
    "ospf": _ospf_bindings,
    "static_route": _static_route_bindings,
    "subinterface": _subinterface_bindings,
    "svi": _svi_bindings,
    "switchport": _switchport_bindings,
    "vlan": _vlan_bindings,
}


def _native_bindings(management, requested):
    bindings = []
    for scope, builder in _NATIVE_BINDING_BUILDERS.items():
        if scope in requested:
            bindings.extend(builder(management))
    if not requested.isdisjoint({"bgp", "isis", "ospf"}):
        bindings.extend(_redistribution_bindings(management, requested))
    return tuple(bindings)


def _qualifying_overlay_signatures(device_id, requested):
    """Return the exact native bindings that qualify for ownership."""
    from .models import NSODeviceManagement

    management = NSODeviceManagement.objects.filter(device_id=device_id).first()
    if management is None:
        return frozenset()
    return frozenset(
        (
            scope,
            native._meta.label_lower,
            native.pk,
            state_model_label,
            json.dumps(state_key, sort_keys=True),
        )
        for scope, native, state_model_label, state_key in _native_bindings(management, requested)
    )


def _native_create_actions(device_id, requested):
    """Return native-only objects whose reviewed rule can construct an overlay."""
    from .models import NSODeviceManagement, NSOOwnershipManifest

    management = NSODeviceManagement.objects.filter(device_id=device_id).first()
    if management is None:
        return ()
    planned = []
    for scope, native, state_model_label, state_key in _native_bindings(management, requested):
        rule_key = "redistribution" if native._meta.label_lower == "netbox_routing.redistribution" else scope
        rule = converted_scope_rules()[rule_key]
        native_key = _native_identity(rule, native)
        identity = {
            "device_id": device_id,
            "scope": scope,
            "native_model_label": native._meta.label_lower,
            "native_key": native_key,
            "state_model_label": state_model_label,
            "state_key": state_key,
        }
        manifest = NSOOwnershipManifest.objects.filter(**identity).first()
        signature = SimpleNamespace(
            **identity,
            native_id=native.pk,
            acknowledged_lineage=[],
        )
        model, filters = _state_filters(signature, rule, native, management)
        overlay_present = model.objects.filter(**filters).exists()
        action = plan_ownership(
            rule,
            OwnershipSignature(
                native_present=True,
                native_qualifies=True,
                overlay_present=overlay_present,
                manifest_state=manifest.ownership_state if manifest is not None else None,
            ),
        )
        if action is OwnershipAction.CREATE:
            planned.append((signature, rule, native))
    return tuple(planned)


def _retract_manifest(manifest, overlay=None) -> bool:
    """Retire one deleted native identity through its scope's authority protocol."""
    from . import outbox
    from .intent_state import (
        IntentMutationProtocolError,
        MutationFootprint,
        intent_transaction,
        reconcile_family_footprint,
    )
    from .models import NSOOwnershipManifest
    from .renderer_writer import RendererMutationPlan, consume_renderer_plan, planned_save
    from .signals import _is_intent_push_suppressed, _is_render_request

    # outbox.enqueue writes nothing while pushes are suppressed. Retiring the manifest anyway
    # would drop this identity's deletion authority with no error, and plan_ownership never
    # revisits a retired row, so the retract must refuse instead.
    if _is_intent_push_suppressed() or _is_render_request():
        raise IntentMutationProtocolError(
            f"the {manifest.scope} retract cannot record its deletion authority while intent pushes are suppressed"
        )

    footprint = reconcile_family_footprint(manifest.device_id, [manifest.scope])
    # An anchor that merely stopped qualifying leaves the overlay behind, and the renderer
    # still reads it: without this the contribution authorises a deletion the re-rendered
    # document never asks for. Demoted, not deleted, so operator content survives.
    plan = None
    if overlay is not None:
        candidate = copy.copy(overlay)
        candidate.status = "imported"
        update_fields = ["status"]
        if hasattr(candidate, "accepted_at"):
            candidate.accepted_at = None
            update_fields.append("accepted_at")
        plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=update_fields),))
        footprint = MutationFootprint.merge(footprint, plan.lock_footprint)
    with intent_transaction(footprint) as permit:
        updated = NSOOwnershipManifest.objects.filter(
            pk=manifest.pk,
            ownership_state="owned",
        ).update(ownership_state="retired")
        if not updated:
            return False
        if plan is not None:
            with consume_renderer_plan(plan, permit, content=True) as writer:
                writer.save(candidate, update_fields=update_fields)
        transitions = ()
        delete_origin = True
        if manifest.scope == "static_route":
            if manifest.native_id is None:
                raise RuntimeError("a static-route manifest cannot retract without its native id")
            acknowledged = manifest.acknowledged_lineage[-1] if manifest.acknowledged_lineage else None
            transitions = (
                outbox.delete_transition(
                    manifest.native_id,
                    last_acked=acknowledged,
                    current=acknowledged,
                ),
            )
            delete_origin = False
        outbox.enqueue(
            manifest.device_id,
            manifest.scope,
            transitions=transitions,
            delete_origin=delete_origin,
        )
    return True


def _detach_manifest(manifest) -> bool:
    """Clear durable ownership without granting device deletion authority."""
    from .models import NSOOwnershipManifest

    return bool(
        NSOOwnershipManifest.objects.filter(
            pk=manifest.pk,
            ownership_state="owned",
        ).update(ownership_state="detached")
    )


def _retire_manifest(manifest) -> bool:
    """Close a durable identity whose only content vanished with its overlay."""
    from .models import NSOOwnershipManifest

    return bool(
        NSOOwnershipManifest.objects.filter(
            pk=manifest.pk,
            ownership_state="owned",
        ).update(ownership_state="retired")
    )


def _manifest_lifecycle_actions(device_id, requested):
    from .models import NSODeviceManagement, NSOOwnershipManifest
    from .status_machine import is_owned

    management = NSODeviceManagement.objects.filter(device_id=device_id).first()
    if management is None:
        return ()
    planned = []
    qualifying = _qualifying_overlay_signatures(device_id, requested)
    manifests = NSOOwnershipManifest.objects.filter(
        device_id=device_id,
        scope__in=requested,
        ownership_state="owned",
    ).order_by("pk")
    for manifest in manifests:
        # 0026/0027 added the native id and state-model columns with no backfill. A row that
        # carries neither cannot name what it owns, so it is no longer usable evidence: retire
        # it and let this same audit rebuild the identity from the surviving state.
        if not _manifest_identity_is_complete(manifest):
            planned.append((manifest, None, None, None, OwnershipAction.RETIRE))
            continue
        rule = _rule_for_manifest(manifest)
        if rule is None:
            continue
        native = _native_for_manifest(manifest, rule)
        overlay = None
        if native is not None:
            model, filters = _state_filters(manifest, rule, native, management)
            overlay = model.objects.filter(**filters).first()
        native_qualifies = native is not None and (
            rule.acquisition_strategy == "existing_overlay"
            or (
                manifest.scope,
                manifest.native_model_label,
                native.pk,
                manifest.state_model_label,
                json.dumps(manifest.state_key, sort_keys=True),
            )
            in qualifying
        )
        action = plan_ownership(
            rule,
            OwnershipSignature(
                native_present=native is not None,
                native_qualifies=native_qualifies,
                overlay_present=overlay is not None,
                overlay_owned=overlay is not None and is_owned(overlay.status),
                manifest_state=manifest.ownership_state,
            ),
        )
        planned.append((manifest, rule, native, overlay, action))
    return tuple(planned)


def _record_missing_manifests(device_id, requested):
    from .intent_state import mirror_transaction, reconcile_family_footprint

    completed = []
    planned = _manifest_record_actions(device_id, requested)
    if not planned:
        return completed
    footprint = reconcile_family_footprint(device_id, requested)
    with mirror_transaction(footprint):
        # One device scan under the lock, then an O(1) re-check of each planned identity.
        qualifying = _qualifying_overlay_signatures(device_id, requested)
        manifest_states = _manifest_states(device_id, requested)
        for scope, model_label, pk in planned:
            instance = apps.get_model(model_label).objects.filter(pk=pk).first()
            if instance is None:
                continue
            if _record_action_for(instance, device_id, requested, qualifying, manifest_states) != (
                scope,
                model_label,
                pk,
            ):
                continue
            maintain_manifest(instance)
            completed.append((scope, pk))
    return completed


def _execute_manifest_lifecycle(device_id, requested):
    completed = []
    for manifest, rule, native, overlay, action in _manifest_lifecycle_actions(device_id, requested):
        if action is OwnershipAction.REOWN and native is not None:
            replacement = _reown_manifest(manifest, rule, native)
            if replacement is not None:
                completed.append((manifest.scope, replacement.pk))
        elif action is OwnershipAction.RETRACT:
            if _retract_manifest(manifest, overlay):
                completed.append((manifest.scope, manifest.pk))
        elif action is OwnershipAction.DETACH:
            if _detach_manifest(manifest):
                completed.append((manifest.scope, manifest.pk))
        elif action is OwnershipAction.RETIRE:
            if _retire_manifest(manifest):
                completed.append((manifest.scope, manifest.pk))
    return completed


def _execute_native_creates(device_id, requested):
    completed = []
    for signature, rule, native in _native_create_actions(device_id, requested):
        created = _reown_manifest(signature, rule, native, revoke=False)
        if created is not None:
            completed.append((signature.scope, created.pk))
    return completed


def reconcile_scope_ownership(device_id: int, scopes) -> tuple[tuple[str, object], ...]:
    """Run the state-derived ownership planner before a renderer audit."""
    requested = frozenset(str(scope) for scope in scopes)
    if not requested:
        return ()
    completed = _record_missing_manifests(device_id, requested)
    completed.extend(_execute_manifest_lifecycle(device_id, requested))
    completed.extend(_execute_native_creates(device_id, requested))
    return tuple(completed)
