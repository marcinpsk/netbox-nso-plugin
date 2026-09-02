# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Pure acquisition and retirement rules for converted delivery scopes."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from enum import Enum

from django.apps import apps


class OwnershipAction(str, Enum):
    """One state-derived ownership transition."""

    NONE = "none"
    CREATE = "create"
    ACQUIRE = "acquire"
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
        native_model_labels=("dcim.interface",),
        native_key_fields=("device_id", "name"),
        overlay_model_labels=("netbox_nso_plugin.nsobfdinterfacestate",),
        overlay_native_fields=(("netbox_nso_plugin.nsobfdinterfacestate", "interface"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from persisted per-interface BFD state. Save events are not ownership evidence."
        ),
    ),
    "bgp": ScopeOwnershipRule(
        scope="bgp",
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
        native_model_labels=(
            "netbox_routing.prefixlist",
            "netbox_routing.communitylist",
            "netbox_routing.aspath",
            "netbox_routing.routemap",
        ),
        native_key_fields=("name",),
        overlay_model_labels=("netbox_nso_plugin.nsoroutepolicystate",),
        overlay_native_fields=(("netbox_nso_plugin.nsoroutepolicystate", "assigned_object"),),
        foreign_overlay_delete="reown",
        deletion_authority=True,
        intentional_semantic_delta=(
            "Acquire from a persisted named policy root and its linked device overlay. Native root, entry, M2M, "
            "and through-row events are not ownership evidence. Native policy deletes no longer delete per-device "
            "overlays and push reduced snapshots synchronously. Acceptance and contributor cascades use exact "
            "acquisition planning and outbox delivery instead of owning and pushing directly. Entries and references "
            "are graph dependencies."
        ),
    ),
    "isis": ScopeOwnershipRule(
        scope="isis",
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

    if signature.overlay_owned:
        if signature.native_present and signature.native_qualifies:
            return OwnershipAction.RECORD_MANIFEST
        return OwnershipAction.RETRACT
    if not signature.native_present or not signature.native_qualifies:
        return OwnershipAction.NONE
    if signature.overlay_present:
        return OwnershipAction.ACQUIRE
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


def manifest_binding(instance):
    """Return the durable manifest identity for one overlay instance."""
    label = instance._meta.label_lower
    for rule in converted_scope_rules().values():
        native_field = dict(rule.overlay_native_fields).get(label)
        if native_field is None:
            continue
        if native_field == "__self__":
            native = instance
        elif native_field == "__ip_address__":
            from dcim.models import Interface
            from django.contrib.contenttypes.models import ContentType
            from ipam.models import IPAddress

            interface_type = ContentType.objects.get_for_model(Interface)
            vrf_name = getattr(instance, "vrf", "")
            vrf_id = None
            if vrf_name:
                from ipam.models import VRF

                vrf_id = VRF.objects.filter(name=vrf_name).values_list("pk", flat=True).first()
            native = IPAddress.objects.filter(
                address=instance.address,
                vrf_id=vrf_id,
                assigned_object_type=interface_type,
                assigned_object_id=instance.interface_id,
            ).first()
        elif native_field == "__ospf_interface__":
            from netbox_routing.models import OSPFInterface

            native = OSPFInterface.objects.filter(interface_id=instance.interface_id).first()
        else:
            native = getattr(instance, native_field, None)
        management = getattr(instance, "management", None)
        if native is None or management is None:
            return None

        def json_value(value):
            if value is None or isinstance(value, (bool, int, float, str)):
                return value
            if isinstance(value, dict):
                return {str(key): json_value(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_value(item) for item in value]
            return str(value)

        key_fields = dict(rule.native_key_fields_by_model).get(native._meta.label_lower, rule.native_key_fields)
        native_key = {name: json_value(getattr(native, name)) for name in key_fields}
        scope = getattr(instance, rule.manifest_scope_field) if rule.manifest_scope_field else rule.scope
        return rule, scope, management.device_id, native._meta.label_lower, native_key
    return None


def maintain_manifest(instance) -> None:
    """Make one overlay's manifest agree with its persisted ownership state."""
    from . import status_machine as sm
    from .models import NSOOwnershipManifest

    binding = manifest_binding(instance)
    if binding is None:
        return
    rule, scope, device_id, native_model_label, native_key = binding
    identity = {
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
            "ownership_state": "owned",
            "deletion_authority": rule.deletion_authority,
        }
        if lineage is not None:
            defaults["acknowledged_lineage"] = [copy.deepcopy(lineage)]
        manifest, created = NSOOwnershipManifest.objects.get_or_create(
            **identity,
            defaults=defaults,
        )
        if not created:
            NSOOwnershipManifest.objects.filter(pk=manifest.pk).exclude(ownership_state="retired").update(**defaults)
    else:
        NSOOwnershipManifest.objects.filter(**identity, ownership_state="owned").update(ownership_state="detached")


def _manifest_record_actions(device_id, requested):
    """Return owned overlays whose durable manifest evidence is absent."""
    from .intent_state import OVERLAY_MODEL_RANKS, renderer_input_specs
    from .models import NSOOwnershipManifest
    from .status_machine import is_owned

    planned = []
    seen = set()
    manifest_states = {
        (scope, native_model_label, json.dumps(native_key, sort_keys=True)): ownership_state
        for scope, native_model_label, native_key, ownership_state in NSOOwnershipManifest.objects.filter(
            device_id=device_id,
            scope__in=tuple(requested),
        ).values_list("scope", "native_model_label", "native_key", "ownership_state")
    }
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
            binding = manifest_binding(instance)
            if binding is None:
                continue
            rule, scope, bound_device_id, native_model_label, native_key = binding
            if scope not in requested or bound_device_id != device_id:
                continue
            manifest_state = manifest_states.get((scope, native_model_label, json.dumps(native_key, sort_keys=True)))
            action = plan_ownership(
                rule,
                OwnershipSignature(
                    native_present=True,
                    native_qualifies=True,
                    overlay_present=True,
                    overlay_owned=is_owned(instance.status),
                    manifest_state=manifest_state,
                ),
            )
            if action is OwnershipAction.RECORD_MANIFEST:
                planned.append((scope, instance._meta.label_lower, instance.pk))
    return tuple(planned)


def reconcile_scope_ownership(device_id: int, scopes) -> tuple[tuple[str, object], ...]:
    """Run the state-derived ownership planner before a renderer audit."""
    from .intent_state import mirror_transaction, reconcile_family_footprint

    requested = frozenset(str(scope) for scope in scopes)
    if not requested:
        return ()
    planned = _manifest_record_actions(device_id, requested)
    if not planned:
        return ()
    footprint = reconcile_family_footprint(device_id, requested)
    completed = []
    with mirror_transaction(footprint):
        current = set(_manifest_record_actions(device_id, requested))
        for scope, model_label, pk in planned:
            instance = apps.get_model(model_label).objects.filter(pk=pk).first()
            if instance is None:
                continue
            if (scope, model_label, pk) not in current:
                continue
            maintain_manifest(instance)
            completed.append((scope, pk))
    return tuple(completed)
