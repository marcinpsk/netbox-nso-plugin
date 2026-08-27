# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Pure acquisition and retirement rules for converted delivery scopes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
    """Retire each owned manifest identity for a device teardown."""
    from .models import NSOOwnershipManifest

    manifests = NSOOwnershipManifest.objects.filter(
        device_id=device_id,
        ownership_state="owned",
    ).only("scope", "native_model_label", "native_key")
    for manifest in manifests.iterator():
        retire_manifest_identity(
            device_ids=(device_id,),
            scope=manifest.scope,
            native_model_label=manifest.native_model_label,
            native_key=manifest.native_key,
        )
