# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Declare renderer inputs and enforce revision-bearing intent mutations."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import functools
import logging
import operator
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from django.apps import apps
from django.db import connections, transaction
from django.db.models.signals import m2m_changed, pre_delete, pre_save

logger = logging.getLogger(__name__)

ABSENT = ("ABSENT",)

SOURCE_MODEL_RANKS = (
    "ipam.rir",
    "ipam.vlan",
    "ipam.vlangroup",
    "ipam.vrf",
    "ipam.asn",
    "dcim.device",
    "dcim.interface",
    "dcim.interface_tagged_vlans",
    "vpn.l2vpn",
    "vpn.l2vpntermination",
    "ipam.ipaddress",
    "netbox_routing.prefixlist",
    "netbox_routing.customprefix",
    "netbox_routing.prefixlistentry",
    "netbox_routing.communitylist",
    "netbox_routing.community",
    "netbox_routing.communitylistentry",
    "netbox_routing.aspath",
    "netbox_routing.aspathentry",
    "netbox_routing.routemap",
    "netbox_routing.routemapentry",
    "netbox_routing.routemapentrysetcommunity",
    "netbox_routing.routemapentrysetcommunity_communities",
    "netbox_routing.routemapentry_match_aspath",
    "netbox_routing.routemapentry_match_community_list",
    "netbox_routing.routemapentry_match_prefix_list",
    "netbox_routing.staticroute",
    "netbox_routing.staticroute_devices",
    "netbox_routing.isisinstance",
    "netbox_routing.isissetting",
    "netbox_routing.isislevel",
    "netbox_routing.isissegmentrouting",
    "netbox_routing.isisflexalgo",
    "netbox_routing.isissrv6locator",
    "netbox_routing.isisinterface",
    "netbox_routing.isisinterfacelevel",
    "netbox_routing.isisprefixsid",
    "netbox_routing.ospfarea",
    "netbox_routing.ospfinterface",
    "netbox_routing.bgprouter",
    "netbox_routing.bgpscope",
    "netbox_routing.bgppeer",
    "netbox_routing.bgppeertemplate",
    "netbox_routing.bgpaddressfamily",
    "netbox_routing.bgppeeraddressfamily",
    "netbox_routing.redistribution",
    "netbox_routing.bfdprofile",
    "netbox_routing.bfdinterface",
    "netbox_nso_plugin.nsodevicemanagement",
    "netbox_nso_plugin.nsoinstance",
    "netbox_nso_plugin.nsoroutepolicyobjectclass",
    "netbox_nso_plugin.nsoplatformnedmapping",
    "netbox_nso_plugin.nsoswitchportstate_tagged_vlans",
)
OVERLAY_MODEL_RANKS = (
    "netbox_nso_plugin.nsointerfacestate",
    "netbox_nso_plugin.nsointerfaceipstate",
    "netbox_nso_plugin.nsosnmpcommunitystate",
    "netbox_nso_plugin.nsosnmpv3userstate",
    "netbox_nso_plugin.nsosnmphoststate",
    "netbox_nso_plugin.nsosnmpsysteminfostate",
    "netbox_nso_plugin.nsologginghoststate",
    "netbox_nso_plugin.nsologginglevelstate",
    "netbox_nso_plugin.nsosvistate",
    "netbox_nso_plugin.nsosubinterfacestate",
    "netbox_nso_plugin.nsointerfacemtustate",
    "netbox_nso_plugin.nsovlanstate",
    "netbox_nso_plugin.nsoswitchportstate",
    "netbox_nso_plugin.nsobfdinterfacestate",
    "netbox_nso_plugin.nsostaticroutestate",
    "netbox_nso_plugin.nsoisisflexalgostate",
    "netbox_nso_plugin.nsol2sapstate",
    "netbox_nso_plugin.nsoisisinstancestate",
    "netbox_nso_plugin.nsoisisinterfacestate",
    "netbox_nso_plugin.nsobgppeerstate",
    "netbox_nso_plugin.nsobgppeertemplatestate",
    "netbox_nso_plugin.nsoroutepolicystate",
    "netbox_nso_plugin.nsoospfinstancestate",
    "netbox_nso_plugin.nsoospfinterfacestate",
    "netbox_nso_plugin.nsoredistributionstate",
    "netbox_nso_plugin.nsolacpbundlestate",
    "netbox_nso_plugin.nsolacpmemberstate",
)
_PROMOTED_CONTENT_FIELDS = {
    "netbox_nso_plugin.nsointerfacestate": {"interface", "attribute"},
    "netbox_nso_plugin.nsodevicemanagement": {
        "device",
        "nso_instance",
        "nso_device_name",
        "manage_description",
        "manage_enabled",
        "manage_interfaces",
        "manage_routing",
        "manage_static",
        "manage_isis",
        "manage_ospf",
        "manage_bgp",
        "manage_route_policy",
        "manage_redistribution",
        "manage_snmp",
        "manage_logging",
        "manage_l2",
    },
    "netbox_nso_plugin.nsosnmpcommunitystate": {
        "management",
        "community_hash",
        "vault_ref",
        "access",
        "acl",
    },
    "ipam.ipaddress": {
        "address",
        "vrf",
        "assigned_object_type",
        "assigned_object_id",
    },
    "netbox_nso_plugin.nsosnmpv3userstate": {
        "management",
        "username",
        "group_name",
        "auth_protocol",
        "priv_protocol",
        "vault_ref",
    },
    "netbox_nso_plugin.nsosnmphoststate": {
        "management",
        "address",
        "version",
        "notify_type",
        "port",
        "community_hash",
        "username",
    },
    "netbox_nso_plugin.nsosnmpsysteminfostate": {"management", "location", "contact"},
    "netbox_nso_plugin.nsologginghoststate": {
        "management",
        "address",
        "port",
        "severity",
        "facility",
        "transport",
        "vrf",
        "source",
    },
    "netbox_nso_plugin.nsovlanstate": {"management", "vlan"},
    "netbox_nso_plugin.nsoswitchportstate": {
        "management",
        "interface",
        "mode",
        "untagged_vlan",
    },
    "netbox_nso_plugin.nsosvistate": {"management", "interface", "vlan", "svi_type", "vrf"},
    "netbox_nso_plugin.nsosubinterfacestate": {
        "management",
        "interface",
        "parent_interface",
        "dot1q_vlan",
        "vrf",
    },
    "netbox_nso_plugin.nsobfdinterfacestate": {
        "management",
        "interface",
        "min_tx",
        "min_rx",
        "multiplier",
        "micro_bfd",
    },
    "netbox_nso_plugin.nsointerfacemtustate": {
        "management",
        "interface",
        "l2_mtu",
        "ip_mtu",
        "mpls_mtu",
    },
    "netbox_nso_plugin.nsoisisflexalgostate": {
        "management",
        "process_tag",
        "algo_id",
        "metric_type",
        "priority",
        "admin_group_exclude",
        "admin_group_include_any",
        "admin_group_include_all",
    },
    "netbox_nso_plugin.nsoisisinterfacestate": {
        "management",
        "interface",
        "af",
        "process_tag",
        "circuit_type",
        "network_type",
        "metric",
        "passive",
        "bfd_enabled",
        "frr_enabled",
        "frr_protection",
    },
    "netbox_nso_plugin.nsoroutepolicystate": {
        "management",
        "content_type",
        "object_id",
        "family",
        "object_name",
    },
    "netbox_nso_plugin.nsostaticroutestate": {"management", "static_route", "intent_generation"},
    "netbox_nso_plugin.nsol2sapstate": {
        "management",
        "service_name",
        "service_type",
        "sap_id",
        "port",
        "outer_tag",
        "inner_tag",
    },
    "netbox_nso_plugin.nsologginglevelstate": {
        "management",
        "console_severity",
        "monitor_severity",
        "module_severity",
    },
    "netbox_nso_plugin.nsolacpbundlestate": {
        "management",
        "interface",
        "lag_id",
        "min_links",
        "system_priority",
        "system_id",
        "timer",
        "admin_key",
    },
    "netbox_nso_plugin.nsolacpmemberstate": {
        "management",
        "interface",
        "lag_bundle",
        "mode",
        "port_priority",
    },
}

_ROUTE_POLICY_CONTENT_FIELDS = {
    "netbox_routing.customprefix": {"prefix"},
    "netbox_routing.prefixlist": {"name"},
    "netbox_routing.prefixlistentry": {
        "prefix_list",
        "assigned_prefix_type",
        "assigned_prefix_id",
        "sequence",
        "action",
        "ge",
        "le",
    },
    "netbox_routing.community": {"community"},
    "netbox_routing.communitylist": {"name", "invert_match"},
    "netbox_routing.communitylistentry": {"community_list", "action", "community"},
    "netbox_routing.aspath": {"name"},
    "netbox_routing.aspathentry": {"aspath", "sequence", "action", "pattern"},
    "netbox_routing.routemap": {"name"},
    "netbox_routing.routemapentry": {
        "route_map",
        "action",
        "sequence",
        "flow_control",
        "match_afi",
        "call_policy",
        "match",
        "set",
        "vendor_ext",
        "apply_policy",
    },
    "netbox_routing.routemapentrysetcommunity": {
        "route_map_entry",
        "operation",
        "community_list",
    },
}

_FRAGMENT_GATE_FIELDS = {
    "netbox_nso_plugin.nsobfdinterfacestate": {"status"},
    "netbox_nso_plugin.nsobgppeerstate": {"status"},
    "netbox_nso_plugin.nsointerfaceipstate": {"accepted_at", "status"},
    "netbox_nso_plugin.nsointerfacemtustate": {"status"},
    "netbox_nso_plugin.nsointerfacestate": {"accepted_at", "status"},
    "netbox_nso_plugin.nsoisisflexalgostate": {"status"},
    "netbox_nso_plugin.nsoisisinstancestate": {"status"},
    "netbox_nso_plugin.nsoisisinterfacestate": {"status"},
    "netbox_nso_plugin.nsol2sapstate": {"status"},
    "netbox_nso_plugin.nsolacpbundlestate": {"status", "vpc_sensitive"},
    "netbox_nso_plugin.nsolacpmemberstate": {"status"},
    "netbox_nso_plugin.nsologginghoststate": {"status"},
    "netbox_nso_plugin.nsologginglevelstate": {"status"},
    "netbox_nso_plugin.nsoospfinstancestate": {"status"},
    "netbox_nso_plugin.nsoospfinterfacestate": {"status"},
    "netbox_nso_plugin.nsoredistributionstate": {"status"},
    "netbox_nso_plugin.nsoroutepolicystate": {"status"},
    "netbox_nso_plugin.nsosnmpcommunitystate": {"status"},
    "netbox_nso_plugin.nsosnmphoststate": {"status"},
    "netbox_nso_plugin.nsosnmpsysteminfostate": {"status"},
    "netbox_nso_plugin.nsosnmpv3userstate": {
        "status",
        "has_auth_secret",
        "has_priv_secret",
    },
    "netbox_nso_plugin.nsostaticroutestate": {"status"},
    "netbox_nso_plugin.nsosubinterfacestate": {"status"},
    "netbox_nso_plugin.nsosvistate": {"status"},
    "netbox_nso_plugin.nsoswitchportstate": {"status"},
    "netbox_nso_plugin.nsovlanstate": {"status", "device_name"},
}

_GLOBAL_LIFECYCLE_FIELDS = frozenset(
    {
        "created",
        "last_updated",
        "custom_field_data",
        "status",
        "accepted_at",
        "last_sync_at",
        "last_apply_at",
        "last_apply_error",
        "apply_attempt_id",
        "allocation_kind",
        "generation_started_at",
        "last_result_advisory",
        "device_present",
        "content_hash",
        "device_base_hash",
        "captured",
        "is_materialized",
        "adapter_device_id",
        "adapter_source_epoch",
        "source_epoch_aware",
        "source_rekey_pending",
        "reset_pending_source_epoch",
        "adapter_incarnation",
        "adapter_link_error",
        "onboard_status",
        "onboard_error",
        "last_reconciled",
        "intent_push_attempts",
        "intent_push_errors",
        "intent_generation",
        "expected_generation",
        "expected_fingerprint",
        "last_acked_triple",
        "state_snapshot",
    }
)


class IntentMutationProtocolError(RuntimeError):
    """A renderer input write did not hold its complete mutation footprint."""


@dataclass(frozen=True, order=True)
class SourceRow:
    """One source row that must be locked before revision rows."""

    model_label: str
    pk: Any


@dataclass(frozen=True)
class MutationFootprint:
    """The immutable L3-L8 lock and revision set for one content mutation."""

    shared_keys: tuple[tuple[str, str], ...] = ()
    device_ids: tuple[int, ...] = ()
    revision_keys: tuple[tuple[int, str], ...] = ()
    source_rows: tuple[SourceRow, ...] = ()
    overlay_rows: tuple[SourceRow, ...] = ()

    def covers(self, other: MutationFootprint) -> bool:
        """Return whether *other* is a strict subset of this frozen footprint."""

        def rows_cover(held, requested):
            held = set(held)
            return all(row in held or SourceRow(row.model_label, None) in held for row in requested)

        return (
            set(other.shared_keys) <= set(self.shared_keys)
            and set(other.device_ids) <= set(self.device_ids)
            and set(other.revision_keys) <= set(self.revision_keys)
            and rows_cover(self.source_rows, other.source_rows)
            and rows_cover(self.overlay_rows, other.overlay_rows)
        )

    @classmethod
    def for_keys(
        cls,
        keys,
        *,
        shared_keys=(),
        source_rows=(),
        overlay_rows=(),
    ) -> MutationFootprint:
        revisions = tuple(sorted({(int(device_id), str(scope)) for device_id, scope in keys}))

        def row_key(row):
            return (row.model_label, row.pk is None, repr(row.pk))

        return cls(
            shared_keys=tuple(sorted({(str(kind), str(key)) for kind, key in shared_keys})),
            device_ids=tuple(sorted({device_id for device_id, _scope in revisions})),
            revision_keys=revisions,
            source_rows=tuple(sorted(set(source_rows), key=row_key)),
            overlay_rows=tuple(sorted(set(overlay_rows), key=row_key)),
        )

    @classmethod
    def merge(cls, *footprints: MutationFootprint) -> MutationFootprint:
        """Combine footprints during discovery, before any lock is acquired."""
        return cls.for_keys(
            (key for footprint in footprints for key in footprint.revision_keys),
            shared_keys=(key for footprint in footprints for key in footprint.shared_keys),
            source_rows=(row for footprint in footprints for row in footprint.source_rows),
            overlay_rows=(row for footprint in footprints for row in footprint.overlay_rows),
        )


@dataclass(frozen=True)
class ReconcileMutationPlan:
    """A read-side footprint and whether its predicted fragment changes require a bump."""

    footprint: MutationFootprint
    changes_content: bool = False
    # A content-bearing read re-pends the deploying rows in its scope; families opt out per plan.
    settles_deploying: bool = True
    validate_after_acquire: Callable[[], None] | None = field(default=None, compare=False, repr=False)
    detect_content_changes: bool = False


@dataclass(frozen=True)
class RendererInputSpec:
    """One concrete renderer-input table and its declared mutation semantics."""

    model_label: str
    scopes: tuple[str, ...]
    content_fields: frozenset[str]
    lifecycle_fields: frozenset[str]
    resolver: Any
    required_trace_fixtures: tuple[str, ...]
    fragment: Any
    shared_kind: str | None = None
    dependency_resolver: Any = None
    prospective_visibility: Any = None

    @property
    def model(self):
        return apps.get_model(self.model_label)

    @property
    def table(self) -> str:
        return self.model._meta.db_table


@dataclass
class _Permit:
    footprint: MutationFootprint
    dml_kind: str
    mirror_table: str | None = None
    mirror_pk: Any = None
    mirror_before: Any = None
    mirror_instance: Any = None
    mirror_update_fields: frozenset[str] | None = None
    detect_reconcile_content: bool = False
    settles_deploying: bool = True
    initial_deploying_rows: tuple[SourceRow, ...] = ()
    deferred_repend_rows: tuple[SourceRow, ...] = ()


_REGISTRY: dict[str, RendererInputSpec] = {}
_ACTIVE_PERMIT: contextvars.ContextVar[_Permit | None] = contextvars.ContextVar(
    "nso_intent_mutation_permit", default=None
)
_RECONCILER_ACTIVE: contextvars.ContextVar[int] = contextvars.ContextVar("nso_intent_reconciler_active", default=0)


def renderer_input_specs() -> dict[str, RendererInputSpec]:
    """Return the declared registry keyed by lower-case Django model label."""
    return _REGISTRY


def reconcile_family_footprint(device_id: int, scopes) -> MutationFootprint:
    """Cover every registered model that a family reconciler may refresh."""
    requested = frozenset(str(scope) for scope in scopes)
    source_labels = set()
    overlay_labels = set()
    overlay_rows = set()

    def add_model(model) -> None:
        label = model._meta.label_lower
        if label in OVERLAY_MODEL_RANKS:
            field_names = {field.name for field in model._meta.concrete_fields}
            if "management" in field_names:
                device_lookup = "management__device_id"
            elif "interface" in field_names:
                device_lookup = "interface__device_id"
            else:
                raise IntentMutationProtocolError(f"overlay model {label} has no device ownership path")
            overlay_labels.add(label)
            overlay_rows.update(
                SourceRow(label, pk)
                for pk in model.objects.filter(**{device_lookup: device_id}).values_list("pk", flat=True)
            )
        elif label in SOURCE_MODEL_RANKS:
            source_labels.add(label)
        elif label in _REGISTRY and label != "netbox_nso_plugin.nsodevicemanagement":
            raise IntentMutationProtocolError(f"renderer-input model {label} has no declared lock rank")

    for spec in _REGISTRY.values():
        if requested.isdisjoint(spec.scopes):
            continue
        add_model(spec.model)
        for model_field in spec.model._meta.many_to_many:
            add_model(model_field.remote_field.through)

    return MutationFootprint.for_keys(
        {(int(device_id), scope) for scope in requested},
        source_rows=(SourceRow(label, None) for label in source_labels),
        overlay_rows=(*overlay_rows, *(SourceRow(label, None) for label in overlay_labels)),
    )


def audit_scope_footprint(device_id: int, scopes) -> MutationFootprint:
    """Freeze the concrete rows that contribute to one repair candidate set."""
    device_id = int(device_id)
    requested = frozenset(str(scope) for scope in scopes)
    requested_keys = {(device_id, scope) for scope in requested}
    footprints = [reconcile_family_footprint(device_id, requested)]
    for spec in _REGISTRY.values():
        if requested.isdisjoint(spec.scopes):
            continue
        for instance in spec.model._default_manager.all().order_by("pk").iterator():
            footprint = footprint_for_instance(instance, spec)
            if requested_keys.intersection(footprint.revision_keys):
                footprints.append(footprint)
    return MutationFootprint.merge(*footprints)


@contextlib.contextmanager
def renderer_query_trace():
    """Collect concrete Django model tables read by renderer queries."""
    ignored = {
        "contenttypes.contenttype",
        "dcim.site",
        "ipam.vlangroup",
    }
    table_labels = {
        model._meta.db_table: model._meta.label_lower
        for model in apps.get_models(include_auto_created=True)
        if model._meta.label_lower not in ignored
    }
    observed: set[str] = set()

    def trace(execute, sql, params, many, context):
        statement = str(sql)
        if statement.lstrip().upper().startswith("SELECT"):
            for table, label in table_labels.items():
                if f'"{table}"' in statement:
                    observed.add(label)
        return execute(sql, params, many, context)

    with connections["default"].execute_wrapper(trace):
        yield observed


def _normal(value):
    if hasattr(value, "pk"):
        return value.pk
    if isinstance(value, (list, tuple)):
        return tuple(_normal(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(key), _normal(item)) for key, item in value.items()))
    return value


def _declared_fields_fragment(instance):
    """Serialize the exact declared fields after renderer membership gates."""
    spec = _REGISTRY[instance._meta.label_lower]
    if instance._meta.label_lower == "dcim.interface" and instance.pk is None:
        return ABSENT
    if instance._meta.app_label in {"dcim", "ipam", "netbox_routing"}:
        if not _native_source_is_rendered(instance):
            return ABSENT
    if instance._meta.label_lower in OVERLAY_MODEL_RANKS and hasattr(instance, "status"):
        from .status_machine import is_owned

        if not is_owned(instance.status):
            return ABSENT
    return tuple(
        (name, _normal(getattr(instance, instance._meta.get_field(name).attname)))
        for name in sorted(spec.content_fields)
    )


def canonical_fragment(instance, spec: RendererInputSpec | None = None):
    """Return this row's exact declared renderer contribution or ``ABSENT``."""
    spec = spec or _REGISTRY[instance._meta.label_lower]
    return spec.fragment(instance)


def _lacp_bundle_fragment(instance):
    """Use the same pure helper as the nested LACP delivery renderer."""
    from .signals import lacp_bundle_intent_item, lacp_member_intent_item

    owned = ("accepted", "deploying", "in_sync")
    if instance.status not in owned or instance.vpc_sensitive:
        return ABSENT
    Member = apps.get_model("netbox_nso_plugin.nsolacpmemberstate")
    members = (
        lacp_member_intent_item(member)
        for member in Member.objects.filter(
            management_id=instance.management_id,
            lag_bundle_id=instance.interface_id,
            status__in=owned,
        ).select_related("interface")
    )
    return _normal(lacp_bundle_intent_item(instance, members))


def _lacp_member_fragment(instance):
    from .signals import lacp_member_intent_item

    if instance.status not in ("accepted", "deploying", "in_sync"):
        return ABSENT
    Bundle = apps.get_model("netbox_nso_plugin.nsolacpbundlestate")
    if not Bundle.objects.filter(
        management_id=instance.management_id,
        interface_id=instance.lag_bundle_id,
        status__in=("accepted", "deploying", "in_sync"),
        vpc_sensitive=False,
    ).exists():
        return ABSENT
    return _normal(lacp_member_intent_item(instance))


def _lacp_bundle_dependencies(before, after, spec):
    """Resolve the member rows nested below a proposed LACP bundle write."""
    Member = apps.get_model("netbox_nso_plugin.nsolacpmemberstate")
    candidates = tuple(candidate for candidate in (before, after) if candidate is not None)
    pairs = {(row.management_id, row.interface_id) for row in candidates}
    member_rows = tuple(
        member
        for management_id, interface_id in sorted(pairs)
        for member in Member.objects.filter(
            management_id=management_id,
            lag_bundle_id=interface_id,
        ).order_by("pk")
    )
    interface_ids = {
        interface_id for row in candidates for interface_id in (row.interface_id,) if interface_id is not None
    }
    interface_ids.update(member.interface_id for member in member_rows)
    return MutationFootprint.for_keys(
        (key for row in candidates for key in spec.resolver(row, spec)),
        source_rows=(SourceRow("dcim.interface", interface_id) for interface_id in interface_ids),
        overlay_rows=(SourceRow(member._meta.label_lower, member.pk) for member in member_rows),
    ), False


def _lacp_member_dependencies(before, after, spec):
    """Resolve both containing bundles for a proposed LACP member write."""
    Bundle = apps.get_model("netbox_nso_plugin.nsolacpbundlestate")
    candidates = tuple(candidate for candidate in (before, after) if candidate is not None)
    management_ids = {row.management_id for row in candidates}
    interface_ids = {
        interface_id
        for row in candidates
        for interface_id in (row.interface_id, row.lag_bundle_id)
        if interface_id is not None
    }
    bundles = tuple(
        Bundle.objects.filter(
            management_id__in=management_ids,
            interface_id__in=interface_ids,
        ).order_by("pk")
    )
    before_fragment = ABSENT if before is None else _lacp_member_fragment(before)
    after_fragment = ABSENT if after is None else _lacp_member_fragment(after)
    placement_changed = (None if before is None else (before.management_id, before.lag_bundle_id)) != (
        None if after is None else (after.management_id, after.lag_bundle_id)
    )
    return MutationFootprint.for_keys(
        (key for row in candidates for key in spec.resolver(row, spec)),
        source_rows=(SourceRow("dcim.interface", interface_id) for interface_id in interface_ids),
        overlay_rows=(SourceRow(bundle._meta.label_lower, bundle.pk) for bundle in bundles),
    ), placement_changed and (before_fragment != ABSENT or after_fragment != ABSENT)


def _vlan_state_dependencies(before, after, spec):
    """Lock each native VLAN anchor referenced by a VLAN overlay write."""
    candidates = tuple(candidate for candidate in (before, after) if candidate is not None)
    vlan_ids = {row.vlan_id for row in candidates if row.vlan_id is not None}
    return MutationFootprint.for_keys(
        (
            *(key for row in candidates for key in spec.resolver(row, spec)),
            *_vlan_anchor_keys(vlan_ids, spec.scopes),
        ),
        shared_keys=(("vlan", str(vlan_id)) for vlan_id in vlan_ids),
        source_rows=(SourceRow("ipam.vlan", vlan_id) for vlan_id in vlan_ids),
    ), False


def _vlan_anchor_keys(vlan_ids, scopes):
    """Resolve the devices that already render a shared native VLAN anchor."""
    Vlan = apps.get_model("ipam.vlan")
    native_spec = _REGISTRY["ipam.vlan"]
    device_ids = {
        device_id
        for vlan in Vlan.objects.filter(pk__in=vlan_ids).order_by("pk")
        for device_id, _scope in native_spec.resolver(vlan, native_spec)
    }
    return {(device_id, scope) for device_id in device_ids for scope in scopes}


def _svi_dependencies(before, after, spec):
    """Lock the native interface and VLAN anchors of an SVI overlay write."""
    candidates = tuple(candidate for candidate in (before, after) if candidate is not None)
    interface_ids = {row.interface_id for row in candidates if row.interface_id is not None}
    vlan_ids = {row.vlan_id for row in candidates if row.vlan_id is not None}
    return MutationFootprint.for_keys(
        (
            *(key for row in candidates for key in spec.resolver(row, spec)),
            *_vlan_anchor_keys(vlan_ids, spec.scopes),
        ),
        shared_keys=(("vlan", str(vlan_id)) for vlan_id in vlan_ids),
        source_rows=(
            *(SourceRow("dcim.interface", interface_id) for interface_id in interface_ids),
            *(SourceRow("ipam.vlan", vlan_id) for vlan_id in vlan_ids),
        ),
    ), False


def _switchport_dependencies(before, after, spec):
    """Lock every native interface and VLAN read by a switchport write."""
    candidates = tuple(candidate for candidate in (before, after) if candidate is not None)
    interface_ids = {row.interface_id for row in candidates if row.interface_id is not None}
    vlan_ids = {row.untagged_vlan_id for row in candidates if row.untagged_vlan_id is not None}
    for row in candidates:
        if row.pk is not None and not row._state.adding:
            vlan_ids.update(row.tagged_vlans.values_list("pk", flat=True))
    return MutationFootprint.for_keys(
        (
            *(key for row in candidates for key in spec.resolver(row, spec)),
            *_vlan_anchor_keys(vlan_ids, spec.scopes),
        ),
        shared_keys=(("vlan", str(vlan_id)) for vlan_id in vlan_ids),
        source_rows=(
            *(SourceRow("dcim.interface", interface_id) for interface_id in interface_ids),
            *(SourceRow("ipam.vlan", vlan_id) for vlan_id in vlan_ids),
            SourceRow("netbox_nso_plugin.nsoswitchportstate_tagged_vlans", None),
        ),
    ), False


def _interface_dependencies(before, after, spec):
    """Lock VLAN anchors read by an exact native interface write."""
    candidates = tuple(candidate for candidate in (before, after) if candidate is not None)
    vlan_ids = {row.untagged_vlan_id for row in candidates if row.untagged_vlan_id is not None}
    for row in candidates:
        if row.pk is not None and not row._state.adding:
            vlan_ids.update(row.tagged_vlans.values_list("pk", flat=True))
    return MutationFootprint.for_keys(
        _vlan_anchor_keys(vlan_ids, spec.scopes),
        shared_keys=(("vlan", str(vlan_id)) for vlan_id in vlan_ids),
        source_rows=(SourceRow("ipam.vlan", vlan_id) for vlan_id in vlan_ids),
    ), False


def _switchport_fragment(instance):
    from .signals import switchport_intent_item

    if instance.status not in ("accepted", "deploying", "in_sync"):
        return ABSENT
    tagged = instance.tagged_vlans.values_list("vid", flat=True) if instance.pk else ()
    return _normal(switchport_intent_item(instance, tagged))


def _interface_state_fragment(instance):
    """Use the same pure helper as the interface delivery renderer."""
    from .signals import interface_intent_item
    from .status_machine import is_owned

    if not is_owned(instance.status):
        return ABSENT
    item = interface_intent_item(instance)
    return ABSENT if item is None else _normal(item)


def _owned_wire_fragment(instance, builder, *, excluded=False):
    """Serialize one exact renderer item through its shared pure helper."""
    from .status_machine import is_owned

    if excluded or not is_owned(instance.status):
        return ABSENT
    item = builder(instance)
    return ABSENT if item is ABSENT or item is None else _normal(item)


def _snmp_community_fragment(instance):
    from .signals import snmp_community_intent_item

    return _owned_wire_fragment(
        instance,
        snmp_community_intent_item,
        excluded=not bool(instance.vault_ref),
    )


def _snmp_v3_user_fragment(instance):
    from .signals import snmp_v3_user_intent_item, snmp_v3_user_push_blocker

    return _owned_wire_fragment(
        instance,
        snmp_v3_user_intent_item,
        excluded=not bool(instance.vault_ref) or bool(snmp_v3_user_push_blocker(instance)),
    )


def _snmp_host_fragment(instance):
    from .signals import _ned_id_for_device, snmp_host_intent_item, snmp_host_push_blocker

    return _owned_wire_fragment(
        instance,
        lambda row: snmp_host_intent_item(row, _ned_id_for_device(row.management.device_id)),
        excluded=bool(snmp_host_push_blocker(instance)),
    )


def _direct_overlay_fragment(instance):
    """Dispatch direct overlay rows to the pure helper their renderer calls."""
    from . import signals

    helpers = {
        "netbox_nso_plugin.nsointerfaceipstate": signals.interface_ip_intent_item,
        "netbox_nso_plugin.nsosnmpsysteminfostate": signals.snmp_system_info_intent_item,
        "netbox_nso_plugin.nsologginghoststate": lambda row: signals.logging_host_intent_item(
            row,
            signals._ned_id_for_device(row.management.device_id),
        ),
        "netbox_nso_plugin.nsologginglevelstate": signals.logging_levels_intent_item,
        "netbox_nso_plugin.nsosvistate": signals.svi_intent_item,
        "netbox_nso_plugin.nsosubinterfacestate": signals.subinterface_intent_item,
        "netbox_nso_plugin.nsointerfacemtustate": signals.interface_mtu_intent_item,
        "netbox_nso_plugin.nsobfdinterfacestate": signals.bfd_intent_item,
        "netbox_nso_plugin.nsoisisflexalgostate": signals.isis_flex_algo_intent_item,
        "netbox_nso_plugin.nsol2sapstate": signals.l2_sap_intent_item,
        "netbox_nso_plugin.nsoroutepolicystate": signals.route_policy_intent_item,
        "netbox_nso_plugin.nsoospfinterfacestate": signals.ospf_interface_intent_item,
        "netbox_nso_plugin.nsoredistributionstate": signals.redistribution_intent_item,
    }
    label = instance._meta.label_lower
    if label == "netbox_nso_plugin.nsostaticroutestate":
        return _owned_wire_fragment(
            instance,
            signals.static_route_intent_item,
            excluded=instance.static_route.next_hop is None,
        )
    if label == "netbox_nso_plugin.nsoisisinterfacestate":
        return _owned_wire_fragment(instance, signals.isis_interface_intent_item)
    if label == "netbox_nso_plugin.nsoisisinstancestate":
        from .status_machine import is_owned

        if not is_owned(instance.status):
            return ABSENT
        redist = signals._collect_redistribution_by_dest_ref(
            instance.management.device_id,
            "isis",
        ).get(instance.process_tag or "", [])
        levels = signals._isis_levels_for_state(instance)
        return _owned_wire_fragment(
            instance,
            lambda row: signals.isis_instance_intent_item(
                row,
                redistribution=redist,
                levels=levels,
            ),
        )
    if label == "netbox_nso_plugin.nsobgppeerstate":
        address_families = []
        if instance.bgp_peer is not None:
            address_families = [
                signals.bgp_peer_address_family_intent_item(row)
                for row in instance.bgp_peer.address_families.select_related(
                    "address_family",
                    "prefixlist_in",
                    "prefixlist_out",
                    "routemap_in",
                    "routemap_out",
                )
            ]
        return _owned_wire_fragment(
            instance,
            lambda row: signals.bgp_peer_intent_item(row, address_families),
        )
    if label == "netbox_nso_plugin.nsoospfinstancestate":
        redist = signals._collect_redistribution_by_dest_ref(
            instance.management.device_id,
            "ospf",
        ).get(str(instance.process_id), [])
        return _owned_wire_fragment(
            instance,
            lambda row: signals.ospf_instance_intent_item(row, redist),
        )
    return _owned_wire_fragment(instance, helpers[label])


def _static_route_fragment(instance):
    """Match the static-route renderer, including its interface-only refusal."""
    if instance.next_hop is None or not _native_source_is_rendered(instance):
        return ABSENT
    return (
        ("route_id", instance.pk),
        ("vrf", instance.vrf.name if instance.vrf else ""),
        ("prefix", str(instance.prefix)),
        ("next_hop", str(instance.next_hop)),
        ("permanent", bool(instance.permanent)),
        ("tag", instance.tag),
        ("metric", instance.metric),
    )


def _direct_overlay_rows(instance) -> tuple[SourceRow, ...]:
    """Return every overlay row that directly points at this native row."""
    from django.db.models import Q

    rows = []
    for label in OVERLAY_MODEL_RANKS:
        model = apps.get_model(label)
        foreign_keys = [
            field
            for field in model._meta.concrete_fields
            if field.many_to_one and field.remote_field.model is type(instance)
        ]
        if not foreign_keys:
            continue
        targets = functools.reduce(operator.or_, (Q(**{field.attname: instance.pk}) for field in foreign_keys))
        rows.extend(SourceRow(label, pk) for pk in model.objects.filter(targets).values_list("pk", flat=True))
    return tuple(rows)


def _direct_owned_overlay_exists(instance) -> bool:
    """Return whether an owned overlay directly points at this native row."""
    from .status_machine import OWNED_STATES

    return any(
        apps.get_model(row.model_label).objects.filter(pk=row.pk, status__in=OWNED_STATES).exists()
        for row in _direct_overlay_rows(instance)
    )


def _cable_interfaces(instance):
    """Return the concrete interface terminations known before a cable write."""
    Interface = apps.get_model("dcim.interface")
    interfaces = {}
    for side in ("a_terminations", "b_terminations"):
        for termination in getattr(instance, side, ()):
            if isinstance(termination, Interface):
                interfaces[termination.pk] = termination
    return tuple(interfaces[pk] for pk in sorted(interfaces))


def _route_policy_groups(instance) -> tuple[tuple[str, str], ...]:
    """Return every shared policy group whose wire fragment reads this row."""
    label = instance._meta.label_lower
    declarations = {
        "netbox_routing.prefixlist": ("prefix_list", None),
        "netbox_routing.prefixlistentry": ("prefix_list", "prefix_list"),
        "netbox_routing.communitylist": ("community_list", None),
        "netbox_routing.communitylistentry": ("community_list", "community_list"),
        "netbox_routing.aspath": ("as_path", None),
        "netbox_routing.aspathentry": ("as_path", "aspath"),
        "netbox_routing.routemap": ("route_map", None),
        "netbox_routing.routemapentry": ("route_map", "route_map"),
    }
    declaration = declarations.get(label)
    groups = set()
    parent = None
    if declaration is not None:
        family, parent_field = declaration
        parent = instance if parent_field is None else getattr(instance, parent_field, None)
        if name := getattr(parent, "name", None):
            groups.add((family, str(name)))

    groups.update(_indirect_route_policy_groups(instance, label))

    contributor = parent
    if contributor is not None and label != "netbox_routing.routemapentry":
        groups.update(_referencing_route_map_groups(contributor))
    if label == "netbox_routing.routemap" and instance.pk is not None:
        from django.db.models import Q

        RouteMapEntry = apps.get_model("netbox_routing.routemapentry")
        groups.update(
            ("route_map", name)
            for name in RouteMapEntry.objects.filter(Q(call_policy=instance) | Q(apply_policy=instance)).values_list(
                "route_map__name", flat=True
            )
        )
    return tuple(sorted(groups, key=lambda group: (group[0], group[1].casefold())))


def _route_policy_prospective_visibility(effective_saves):
    """Return keys exposed by route-policy ownership acquired in one plan."""
    from . import status_machine as sm

    state_label = "netbox_nso_plugin.nsoroutepolicystate"
    acquired_groups = {
        (after.family, after.object_name.casefold())
        for before, after in effective_saves
        if after._meta.label_lower == state_label
        and (before is None or not sm.is_owned(before.status))
        and sm.is_owned(after.status)
    }
    if not acquired_groups:
        return set()

    keys = set()
    for before, after in effective_saves:
        spec = _REGISTRY.get(after._meta.label_lower)
        if spec is None or spec.shared_kind != "route_policy" or after._meta.label_lower == state_label:
            continue
        groups = {
            (family, name.casefold())
            for candidate in (before, after)
            if candidate is not None
            for family, name in _route_policy_groups(candidate)
        }
        if groups & acquired_groups:
            keys.update(spec.resolver(after, spec))
    return keys


def _route_map_consumer_rows(instance):
    """Return owned overlays whose rendered body reads one route-map name."""
    if instance._meta.label_lower != "netbox_routing.routemap" or instance.pk is None:
        return ()

    from django.db.models import Q

    from .models import NSOBGPPeerState, NSORedistributionState
    from .status_machine import OWNED_STATES

    bgp_states = NSOBGPPeerState.objects.filter(
        Q(bgp_peer__address_families__routemap_in_id=instance.pk)
        | Q(bgp_peer__address_families__routemap_out_id=instance.pk),
        status__in=OWNED_STATES,
    ).select_related("management")
    redistribution_states = NSORedistributionState.objects.filter(
        Q(redistribution__route_map_id=instance.pk) | Q(redistribution__isnull=True, route_map__iexact=instance.name),
        status__in=OWNED_STATES,
    ).select_related("management")
    return (*bgp_states, *redistribution_states)


def _route_map_consumer_keys(instance) -> set[tuple[int, str]]:
    """Resolve route-map consumers to their actual delivery scopes."""
    keys = set()
    supported_scopes = set(_REGISTRY[instance._meta.label_lower].scopes)
    for row in _route_map_consumer_rows(instance):
        if row._meta.label_lower == "netbox_nso_plugin.nsobgppeerstate":
            if "bgp" in supported_scopes:
                keys.add((row.management.device_id, "bgp"))
        elif row.dest_protocol in supported_scopes:
            keys.add((row.management.device_id, row.dest_protocol))
    return keys


def _indirect_route_policy_groups(instance, label) -> set[tuple[str, str]]:
    """Resolve policy groups for leaf objects and auto-created through rows."""
    groups = set()
    if label == "netbox_routing.routemapentrysetcommunity":
        entry = getattr(instance, "route_map_entry", None)
        if entry is not None:
            groups.add(("route_map", entry.route_map.name))
        return groups
    if instance.pk is None:
        return groups
    if label == "netbox_routing.customprefix":
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get_for_model(type(instance))
        parents = apps.get_model("netbox_routing.prefixlist").objects.filter(
            prefix_list_entries__assigned_prefix_type=content_type,
            prefix_list_entries__assigned_prefix_id=instance.pk,
        )
        for prefix_list in parents:
            groups.add(("prefix_list", prefix_list.name))
            groups.update(_referencing_route_map_groups(prefix_list))
    elif label == "netbox_routing.community":
        CommunityList = apps.get_model("netbox_routing.communitylist")
        for community_list in CommunityList.objects.filter(communitylistentries__community_id=instance.pk):
            groups.add(("community_list", community_list.name))
            groups.update(_referencing_route_map_groups(community_list))
        SetCommunity = apps.get_model("netbox_routing.routemapentrysetcommunity")
        for row in SetCommunity.objects.filter(communities=instance).select_related("route_map_entry__route_map"):
            groups.add(("route_map", row.route_map_entry.route_map.name))
    elif instance._meta.auto_created:
        groups.update(_through_route_policy_groups(instance))
    return groups


def _through_route_policy_groups(instance) -> set[tuple[str, str]]:
    """Resolve the route-map owner of one registered M2M through row."""
    groups = set()
    for model_field in instance._meta.concrete_fields:
        related = model_field.remote_field.model if model_field.many_to_one else None
        related_label = getattr(getattr(related, "_meta", None), "label_lower", None)
        if related_label not in {
            "netbox_routing.routemapentry",
            "netbox_routing.routemapentrysetcommunity",
        }:
            continue
        owner = getattr(instance, model_field.name, None)
        entry = owner.route_map_entry if related_label.endswith("setcommunity") else owner
        if entry is not None:
            groups.add(("route_map", entry.route_map.name))
    return groups


def _referencing_route_map_groups(obj) -> set[tuple[str, str]]:
    """Return route maps that serialize the name of one contributor object."""
    if obj.pk is None:
        return set()
    label = obj._meta.label_lower
    lookups = {
        "netbox_routing.prefixlist": "match_prefix_list",
        "netbox_routing.communitylist": "match_community_list",
        "netbox_routing.aspath": "match_aspath",
    }
    lookup = lookups.get(label)
    if lookup is None:
        return set()
    RouteMapEntry = apps.get_model("netbox_routing.routemapentry")
    groups = {
        ("route_map", name)
        for name in RouteMapEntry.objects.filter(**{lookup: obj}).values_list("route_map__name", flat=True)
    }
    if label == "netbox_routing.communitylist":
        SetCommunity = apps.get_model("netbox_routing.routemapentrysetcommunity")
        groups.update(
            ("route_map", name)
            for name in SetCommunity.objects.filter(community_list=obj).values_list(
                "route_map_entry__route_map__name", flat=True
            )
        )
    return groups


def _protocol_native_source_is_rendered(instance) -> bool | None:
    """Resolve indirect BFD, OSPF, and IS-IS ownership, if applicable."""
    from .models import (
        NSOBFDInterfaceState,
        NSOISISInstanceState,
        NSOOSPFInterfaceState,
    )
    from .status_machine import OWNED_STATES

    label = instance._meta.label_lower
    if label == "netbox_routing.bfdinterface":
        return NSOBFDInterfaceState.objects.filter(
            interface_id=instance.interface_id,
            status__in=OWNED_STATES,
        ).exists()
    if label == "netbox_routing.bfdprofile":
        BFDInterface = apps.get_model("netbox_routing.bfdinterface")
        return NSOBFDInterfaceState.objects.filter(
            interface_id__in=BFDInterface.objects.filter(bfd_profile_id=instance.pk).values("interface_id"),
            status__in=OWNED_STATES,
        ).exists()
    if label == "netbox_routing.ospfinterface":
        return NSOOSPFInterfaceState.objects.filter(
            interface_id=instance.interface_id,
            status__in=OWNED_STATES,
        ).exists()
    if label == "netbox_routing.ospfarea":
        OSPFInterface = apps.get_model("netbox_routing.ospfinterface")
        return NSOOSPFInterfaceState.objects.filter(
            interface_id__in=OSPFInterface.objects.filter(area_id=instance.pk).values("interface_id"),
            status__in=OWNED_STATES,
        ).exists()
    if label == "netbox_routing.isislevel":
        return NSOISISInstanceState.objects.filter(
            isis_instance_id=instance.instance_id,
            status__in=OWNED_STATES,
        ).exists()
    return None


def _bgp_native_source_is_rendered(instance) -> bool | None:
    """Resolve indirect BGP ownership, if the row belongs to the BGP graph."""
    from .models import NSOBGPPeerState, NSOBGPPeerTemplateState
    from .status_machine import OWNED_STATES

    label = instance._meta.label_lower
    if label == "netbox_routing.bgppeeraddressfamily":
        owner_model = instance.assigned_object_type.model_class()
        if owner_model is None:
            return False
        if owner_model._meta.label_lower == "netbox_routing.bgppeer":
            return NSOBGPPeerState.objects.filter(
                bgp_peer_id=instance.assigned_object_id,
                status__in=OWNED_STATES,
            ).exists()
        if owner_model._meta.label_lower == "netbox_routing.bgppeertemplate":
            return NSOBGPPeerTemplateState.objects.filter(
                template_id=instance.assigned_object_id,
                status__in=OWNED_STATES,
            ).exists()
        return False
    if label == "netbox_routing.bgpaddressfamily":
        PeerAddressFamily = apps.get_model("netbox_routing.bgppeeraddressfamily")
        return any(
            _native_source_is_rendered(row) for row in PeerAddressFamily.objects.filter(address_family_id=instance.pk)
        )
    if label in {"netbox_routing.bgpscope", "netbox_routing.bgprouter"}:
        BGPPeer = apps.get_model("netbox_routing.bgppeer")
        peers = BGPPeer.objects.all()
        if label == "netbox_routing.bgpscope":
            peers = peers.filter(scope_id=instance.pk)
        else:
            peers = peers.filter(scope__router_id=instance.pk)
        return NSOBGPPeerState.objects.filter(
            bgp_peer_id__in=peers.values("pk"),
            status__in=OWNED_STATES,
        ).exists()
    return None


def _native_source_is_rendered(instance) -> bool:
    """Return whether an owned intent currently consumes this native source row."""
    if instance.pk is None:
        return False
    if _direct_owned_overlay_exists(instance):
        return True

    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Q

    from .models import NSOBGPPeerState, NSORoutePolicyState
    from .status_machine import OWNED_STATES

    label = instance._meta.label_lower
    if label == "dcim.cable":
        return any(_native_source_is_rendered(interface) for interface in _cable_interfaces(instance))
    if label == "dcim.device":
        from .models import NSODeviceManagement

        return NSODeviceManagement.objects.filter(device_id=instance.pk).exists()
    if label == "dcim.interface":
        return any(
            apps.get_model(row.model_label).objects.filter(pk=row.pk, status__in=OWNED_STATES).exists()
            for row in _interface_overlay_rows(instance.pk)
        )
    if label == "ipam.ipaddress":
        BGPPeer = apps.get_model("netbox_routing.bgppeer")
        peers = BGPPeer.objects.filter(Q(peer_id=instance.pk) | Q(source_id=instance.pk))
        return NSOBGPPeerState.objects.filter(
            bgp_peer_id__in=peers.values("pk"),
            status__in=OWNED_STATES,
        ).exists()
    if label == "ipam.vrf":
        StaticRouteState = apps.get_model("netbox_nso_plugin.nsostaticroutestate")
        return StaticRouteState.objects.filter(
            static_route__vrf_id=instance.pk,
            status__in=OWNED_STATES,
        ).exists()
    if label == "ipam.asn":
        return NSOBGPPeerState.objects.filter(
            Q(bgp_peer__local_as_id=instance.pk) | Q(bgp_peer__scope__router__asn_id=instance.pk),
            status__in=OWNED_STATES,
        ).exists()
    if label == "netbox_routing.bgppeertemplate":
        return NSOBGPPeerState.objects.filter(
            bgp_peer__peer_group_id=instance.pk,
            status__in=OWNED_STATES,
        ).exists()
    if (rendered := _protocol_native_source_is_rendered(instance)) is not None:
        return rendered
    if (rendered := _bgp_native_source_is_rendered(instance)) is not None:
        return rendered
    if groups := _route_policy_groups(instance):
        predicate = functools.reduce(
            operator.or_,
            (Q(family=family, object_name__iexact=name) for family, name in groups),
        )
        return NSORoutePolicyState.objects.filter(predicate, status__in=OWNED_STATES).exists()
    if label.startswith("netbox_routing."):
        content_type = ContentType.objects.get_for_model(type(instance))
        return NSORoutePolicyState.objects.filter(
            content_type=content_type,
            object_id=instance.pk,
            status__in=OWNED_STATES,
        ).exists()
    return False


def _vlan_state_fragment(instance):
    from .signals import vlan_intent_item
    from .status_machine import is_owned

    if not is_owned(instance.status):
        return ABSENT
    item = vlan_intent_item(instance)
    return ABSENT if item is None else _normal(item)


def _native_vlan_fragment(instance):
    """Return the native VLAN fragment only while owned intent renders it."""
    if not _native_source_is_rendered(instance):
        return ABSENT
    return (("name", instance.name), ("vid", instance.vid))


def _database_fragment(instance, spec):
    if instance.pk is None or instance._state.adding:
        return ABSENT
    current = type(instance).objects.filter(pk=instance.pk).first()
    return ABSENT if current is None else canonical_fragment(current, spec)


def _effective_after(instance, before, update_fields):
    """Apply the fields saved by this write to the stored instance shape."""
    if before is None or update_fields is None:
        return instance
    effective = copy.copy(before)
    for field_name in update_fields:
        field = instance._meta.get_field(field_name)
        setattr(effective, field.attname, getattr(instance, field.attname))
        if field.is_relation:
            if field.is_cached(instance):
                field.set_cached_value(effective, field.get_cached_value(instance))
            elif field.is_cached(effective):
                field.delete_cached_value(effective)
    return effective


def _effective_after_fragment(instance, spec, update_fields):
    """Serialize only values this save will persist, not unrelated stale attributes."""
    current = (
        None if instance.pk is None or instance._state.adding else type(instance).objects.filter(pk=instance.pk).first()
    )
    return canonical_fragment(_effective_after(instance, current, update_fields), spec)


def _management_keys(device_ids, scopes):
    from .models import NSODeviceManagement

    managed = set(NSODeviceManagement.objects.filter(device_id__in=device_ids).values_list("device_id", flat=True))
    return {(device_id, scope) for device_id in managed for scope in scopes}


def _native_device_ids(instance) -> set[int]:
    def assigned_device_id(assigned):
        if assigned is None:
            return None
        if assigned._meta.label_lower == "dcim.device":
            return assigned.pk
        return getattr(assigned, "device_id", None)

    device_id = getattr(instance, "device_id", None)
    if device_id is not None:
        return {device_id}
    for attribute in ("interface", "instance", "ospf_instance", "isis_instance", "bgp_router", "router"):
        related_device_id = getattr(getattr(instance, attribute, None), "device_id", None)
        if related_device_id is not None:
            return {related_device_id}
    assigned_id = assigned_device_id(getattr(instance, "assigned_object", None))
    if assigned_id is not None:
        return {assigned_id}
    label = instance._meta.label_lower
    scope = getattr(instance, "scope", None)
    if label == "netbox_routing.bgpaddressfamily":
        scope = getattr(instance, "scope", None)
    elif label == "netbox_routing.bgppeeraddressfamily":
        scope = getattr(getattr(instance, "address_family", None), "scope", None)
    elif label == "netbox_routing.bgpscope":
        scope = instance
    if scope is not None:
        assigned_id = assigned_device_id(getattr(getattr(scope, "router", None), "assigned_object", None))
        if assigned_id is not None:
            return {assigned_id}
    if instance.pk is not None and hasattr(instance, "devices"):
        try:
            return set(instance.devices.values_list("pk", flat=True))
        except (AttributeError, ValueError):
            pass
    return set()


def _specialized_generic_keys(instance, spec: RendererInputSpec) -> set[tuple[int, str]] | None:
    """Resolve model-specific keys whose delivery scope is stored outside the native row."""
    from .models import NSODeviceManagement

    management_id = getattr(instance, "management_id", None)
    if management_id is not None:
        device_id = NSODeviceManagement.objects.filter(pk=management_id).values_list("device_id", flat=True).first()
        if instance._meta.label_lower == "netbox_nso_plugin.nsoredistributionstate":
            destination = instance.dest_protocol
            return set() if device_id is None or destination not in spec.scopes else {(device_id, destination)}
        return set() if device_id is None else {(device_id, scope) for scope in spec.scopes}
    if instance._meta.label_lower == "netbox_routing.redistribution" and instance.pk is not None:
        RedistributionState = apps.get_model("netbox_nso_plugin.nsoredistributionstate")
        return {
            (device_id, destination)
            for device_id, destination in RedistributionState.objects.filter(
                redistribution_id=instance.pk,
                dest_protocol__in=spec.scopes,
            ).values_list("management__device_id", "dest_protocol")
        }
    if instance._meta.label_lower == "ipam.ipaddress":
        from dcim.models import Interface
        from django.db.models import Q

        from .models import NSOBGPPeerState
        from .status_machine import OWNED_STATES

        keys = set()
        assigned = getattr(instance, "assigned_object", None)
        if isinstance(assigned, Interface):
            keys.update(_management_keys({assigned.device_id}, ("ip",)))
        if instance.pk is not None:
            keys.update(
                (device_id, "bgp")
                for device_id in NSOBGPPeerState.objects.filter(
                    Q(bgp_peer__peer_id=instance.pk) | Q(bgp_peer__source_id=instance.pk),
                    status__in=OWNED_STATES,
                ).values_list("management__device_id", flat=True)
            )
        return keys
    if instance._meta.label_lower == "ipam.vrf":
        StaticRouteState = apps.get_model("netbox_nso_plugin.nsostaticroutestate")
        device_ids = StaticRouteState.objects.filter(
            static_route__vrf_id=instance.pk,
        ).values_list("management__device_id", flat=True)
        return _management_keys(set(device_ids), spec.scopes)
    if instance._meta.label_lower == "ipam.asn":
        from django.db.models import Q

        PeerState = apps.get_model("netbox_nso_plugin.nsobgppeerstate")
        device_ids = PeerState.objects.filter(
            Q(bgp_peer__local_as_id=instance.pk) | Q(bgp_peer__scope__router__asn_id=instance.pk)
        ).values_list("management__device_id", flat=True)
        return _management_keys(set(device_ids), spec.scopes)
    if instance._meta.label_lower == "netbox_routing.bgppeertemplate":
        PeerState = apps.get_model("netbox_nso_plugin.nsobgppeerstate")
        device_ids = PeerState.objects.filter(
            bgp_peer__peer_group_id=instance.pk,
        ).values_list("management__device_id", flat=True)
        return _management_keys(set(device_ids), spec.scopes)
    return None


def _generic_keys(instance, spec: RendererInputSpec) -> set[tuple[int, str]]:
    from dcim.models import Device, Interface

    from .models import NSODeviceManagement, NSORoutePolicyState, NSOSVIState, NSOVLANState

    if instance is None:
        return set()
    if isinstance(instance, NSODeviceManagement):
        return {(instance.device_id, scope) for scope in spec.scopes}
    if isinstance(instance, Device):
        return _management_keys({instance.pk}, spec.scopes)
    if instance._meta.label_lower == "dcim.cable":
        return _management_keys(
            {interface.device_id for interface in _cable_interfaces(instance)},
            spec.scopes,
        )
    specialized = _specialized_generic_keys(instance, spec)
    if specialized is not None:
        return specialized
    if isinstance(instance, Interface):
        return _management_keys({instance.device_id}, spec.scopes)
    if instance._meta.label_lower == "ipam.vlan":
        device_ids = set(
            NSOVLANState.objects.filter(vlan_id=instance.pk).values_list("management__device_id", flat=True)
        )
        device_ids.update(
            NSOSVIState.objects.filter(vlan_id=instance.pk).values_list("management__device_id", flat=True)
        )
        return _management_keys(device_ids, spec.scopes)
    assigned = getattr(instance, "assigned_object", None)
    if isinstance(assigned, Interface):
        return _management_keys({assigned.device_id}, spec.scopes)
    if device_ids := _native_device_ids(instance):
        return _management_keys(device_ids, spec.scopes)
    if spec.shared_kind == "route_policy":
        family = getattr(instance, "family", None)
        if family is None:
            family = {
                "netbox_routing.prefixlist": "prefix_list",
                "netbox_routing.prefixlistentry": "prefix_list",
                "netbox_routing.communitylist": "community_list",
                "netbox_routing.communitylistentry": "community_list",
                "netbox_routing.aspath": "as_path",
                "netbox_routing.aspathentry": "as_path",
                "netbox_routing.routemap": "route_map",
                "netbox_routing.routemapentry": "route_map",
            }.get(instance._meta.label_lower)
        parent = (
            getattr(instance, "prefix_list", None)
            or getattr(instance, "community_list", None)
            or getattr(instance, "aspath", None)
            or getattr(instance, "route_map", None)
        )
        name = (
            getattr(parent, "name", None) or getattr(instance, "object_name", None) or getattr(instance, "name", None)
        )
        rows = NSORoutePolicyState.objects.all()
        if family and name:
            rows = rows.filter(family=family, object_name__iexact=name)
        device_ids = set(rows.values_list("management__device_id", flat=True))
        return _management_keys(device_ids, spec.scopes) | _route_map_consumer_keys(instance)
    if instance._meta.label_lower in {
        "netbox_nso_plugin.nsoinstance",
        "netbox_nso_plugin.nsoplatformnedmapping",
    }:
        return {
            (device_id, scope)
            for device_id in NSODeviceManagement.objects.values_list("device_id", flat=True)
            for scope in spec.scopes
        }
    return set()


def _interface_overlay_querysets(interface_id):
    """Return each overlay query whose renderer reads this interface's name."""
    from django.db.models import Q

    from .models import (
        NSOBFDInterfaceState,
        NSOBGPPeerState,
        NSOInterfaceIPState,
        NSOInterfaceMtuState,
        NSOInterfaceState,
        NSOISISInterfaceState,
        NSOLACPBundleState,
        NSOLACPMemberState,
        NSOOSPFInterfaceState,
        NSOSubinterfaceState,
        NSOSVIState,
        NSOSwitchportState,
    )

    return (
        NSOInterfaceState.objects.filter(interface_id=interface_id),
        NSOInterfaceIPState.objects.filter(Q(interface_id=interface_id) | Q(interface__parent_id=interface_id)),
        NSOSVIState.objects.filter(interface_id=interface_id),
        NSOSubinterfaceState.objects.filter(Q(interface_id=interface_id) | Q(parent_interface_id=interface_id)),
        NSOBFDInterfaceState.objects.filter(interface_id=interface_id),
        NSOInterfaceMtuState.objects.filter(interface_id=interface_id),
        NSOISISInterfaceState.objects.filter(interface_id=interface_id),
        NSOBGPPeerState.objects.filter(bgp_peer__update_source_id=interface_id),
        NSOOSPFInterfaceState.objects.filter(interface_id=interface_id),
        NSOLACPBundleState.objects.filter(interface_id=interface_id),
        NSOLACPMemberState.objects.filter(Q(interface_id=interface_id) | Q(lag_bundle_id=interface_id)),
        NSOSwitchportState.objects.filter(interface_id=interface_id),
    )


def interface_name_intent_rows(interface_id) -> tuple:
    """Return the exact overlay rows whose renderer reads this interface's name."""
    return tuple(row for queryset in _interface_overlay_querysets(interface_id) for row in queryset.order_by("pk"))


def _interface_overlay_rows(interface_id) -> tuple[SourceRow, ...]:
    """Collect each overlay row whose canonical fragment renders this interface."""
    queries = _interface_overlay_querysets(interface_id)
    return (
        *(SourceRow(queryset.model._meta.label_lower, None) for queryset in queries),
        *(
            SourceRow(queryset.model._meta.label_lower, pk)
            for queryset in queries
            for pk in queryset.order_by("pk").values_list("pk", flat=True)
        ),
    )


def _interface_ip_source_rows(interface_ids) -> tuple[SourceRow, ...]:
    """Return native IP rows whose generic assignment targets these interfaces."""
    from django.contrib.contenttypes.models import ContentType

    Interface = apps.get_model("dcim.interface")
    IPAddress = apps.get_model("ipam.ipaddress")
    content_type = ContentType.objects.get_for_model(Interface)
    return tuple(
        SourceRow(IPAddress._meta.label_lower, pk)
        for pk in IPAddress.objects.filter(
            assigned_object_type=content_type,
            assigned_object_id__in=tuple(interface_ids),
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def footprint_for_instance(instance, spec: RendererInputSpec | None = None) -> MutationFootprint:
    """Resolve one instance to its immutable pre-write footprint."""
    spec = spec or _REGISTRY[instance._meta.label_lower]
    if spec.shared_kind == "route_policy":
        return _route_policy_instance_footprint(instance, spec)
    return _regular_instance_footprint(instance, spec)


def _route_policy_instance_footprint(instance, spec) -> MutationFootprint:
    """Include current and stored groups so a rename locks both identities."""
    groups = set(_route_policy_groups(instance))
    if instance._meta.label_lower == "netbox_nso_plugin.nsoroutepolicystate":
        family = getattr(instance, "family", "")
        name = getattr(instance, "object_name", "")
        management = getattr(instance, "management", None)
        if family and name and management is not None:
            return route_policy_footprint(
                {(family, name)},
                device_ids=(management.device_id,),
            )
    candidates = [instance]
    if instance.pk is not None:
        current = type(instance).objects.filter(pk=instance.pk).first()
        if current is not None:
            groups.update(_route_policy_groups(current))
            candidates.append(current)
    base = route_policy_footprint(groups) if groups else _regular_instance_footprint(instance, spec)
    consumers = {
        (row._meta.label_lower, row.pk): row for candidate in candidates for row in _route_map_consumer_rows(candidate)
    }
    return MutationFootprint.merge(
        base,
        *(_regular_instance_footprint(row, _REGISTRY[row._meta.label_lower]) for row in consumers.values()),
    )


def _regular_instance_footprint(instance, spec) -> MutationFootprint:
    """Resolve non-policy rows and unattached route-policy leaf rows."""
    keys = spec.resolver(instance, spec)
    prior_ip_keys, prior_ip_overlays = _previous_ip_address_targets(instance, spec)
    keys.update(prior_ip_keys)
    shared_keys = ()
    if spec.shared_kind == "vlan":
        shared_keys = [("vlan-slot", f"{getattr(instance, 'group_id', None)}:{getattr(instance, 'vid', None)}")]
        if instance.pk is not None:
            shared_keys.append(("vlan", str(instance.pk)))
            previous_slot = type(instance).objects.filter(pk=instance.pk).values_list("group_id", "vid").first()
            if previous_slot is not None:
                shared_keys.append(("vlan-slot", f"{previous_slot[0]}:{previous_slot[1]}"))
        shared_keys = tuple(shared_keys)
    elif spec.shared_kind == "route_policy":
        family = getattr(instance, "family", instance._meta.label_lower)
        name = getattr(instance, "object_name", getattr(instance, "name", ""))
        if name:
            shared_keys = (("route-policy", f"{family}:{str(name).casefold()}"),)
    row = (SourceRow(instance._meta.label_lower, instance.pk),)
    if instance._meta.label_lower == "ipam.ipaddress":
        assigned = getattr(instance, "assigned_object", None)
        current = type(instance).objects.filter(pk=instance.pk).first() if instance.pk is not None else None
        current_assigned = getattr(current, "assigned_object", None)
        row = (
            *row,
            *(
                SourceRow("dcim.interface", interface.pk)
                for interface in (assigned, current_assigned)
                if getattr(getattr(interface, "_meta", None), "label_lower", None) == "dcim.interface"
            ),
        )
    future_overlays = {
        "netbox_routing.bgppeer": (SourceRow("netbox_nso_plugin.nsobgppeerstate", None),),
        "netbox_routing.ospfinstance": (SourceRow("netbox_nso_plugin.nsoospfinstancestate", None),),
        "netbox_routing.ospfinterface": (SourceRow("netbox_nso_plugin.nsoospfinterfacestate", None),),
        "netbox_routing.redistribution": (SourceRow("netbox_nso_plugin.nsoredistributionstate", None),),
        "ipam.ipaddress": (SourceRow("netbox_nso_plugin.nsointerfaceipstate", None),),
    }.get(instance._meta.label_lower, ())
    future_overlays = (*future_overlays, *prior_ip_overlays)
    if instance._meta.label_lower == "dcim.interface" and instance.pk is not None:
        future_overlays = (*future_overlays, *_interface_overlay_rows(instance.pk))
    elif instance._meta.app_label in {"dcim", "ipam", "netbox_routing"} and instance.pk is not None:
        future_overlays = (*future_overlays, *_direct_overlay_rows(instance))
    if instance._meta.label_lower == "dcim.interface" and instance.device_id is not None:
        row = (
            SourceRow("dcim.device", instance.device_id),
            *row,
            *_interface_ip_source_rows((instance.pk,)),
        )
    if instance._meta.label_lower == "dcim.cable":
        interfaces = _cable_interfaces(instance)
        row = (
            *row,
            SourceRow("dcim.interface", None),
            *(SourceRow("dcim.interface", interface.pk) for interface in interfaces),
            *_interface_ip_source_rows(interface.pk for interface in interfaces),
        )
        future_overlays = (
            *future_overlays,
            *(overlay for interface in interfaces for overlay in _interface_overlay_rows(interface.pk)),
        )
    if instance._meta.label_lower == "dcim.device" and instance.pk is not None:
        Interface = apps.get_model("dcim.interface")
        interfaces = tuple(Interface.objects.filter(device_id=instance.pk).order_by("pk"))
        row = (
            *row,
            SourceRow("dcim.interface", None),
            *(SourceRow("dcim.interface", interface.pk) for interface in interfaces),
        )
        future_overlays = (
            *future_overlays,
            *(overlay for interface in interfaces for overlay in _interface_overlay_rows(interface.pk)),
        )
    if instance._meta.label_lower == "netbox_nso_plugin.nsoredistributionstate":
        current_redistribution_id = (
            type(instance).objects.filter(pk=instance.pk).values_list("redistribution_id", flat=True).first()
            if instance.pk is not None
            else None
        )
        redistribution_ids = {
            redistribution_id
            for redistribution_id in (instance.redistribution_id, current_redistribution_id)
            if redistribution_id is not None
        }
        dependent_states = tuple(
            type(instance)
            .objects.filter(redistribution_id__in=redistribution_ids)
            .select_related("management")
            .order_by("pk")
        )
        keys.update(
            (state.management.device_id, state.dest_protocol)
            for state in dependent_states
            if state.dest_protocol in spec.scopes
        )
        return MutationFootprint.for_keys(
            keys,
            shared_keys=(("redistribution", str(redistribution_id)) for redistribution_id in redistribution_ids),
            source_rows=(
                SourceRow("netbox_routing.redistribution", redistribution_id)
                for redistribution_id in redistribution_ids
            ),
            overlay_rows=(
                *row,
                *(SourceRow(state._meta.label_lower, state.pk) for state in dependent_states),
            ),
        )
    if instance._meta.label_lower in OVERLAY_MODEL_RANKS:
        return MutationFootprint.for_keys(keys, shared_keys=shared_keys, overlay_rows=row)
    if instance._meta.label_lower == "netbox_nso_plugin.nsodevicemanagement":
        Interface = apps.get_model("dcim.interface")
        interface_ids = tuple(
            Interface.objects.filter(device_id=instance.device_id).order_by("pk").values_list("pk", flat=True)
        )
        return MutationFootprint.for_keys(
            keys,
            shared_keys=shared_keys,
            source_rows=(
                *row,
                SourceRow("dcim.device", instance.device_id),
                SourceRow("dcim.interface", None),
                *(SourceRow("dcim.interface", interface_id) for interface_id in interface_ids),
            ),
        )
    return MutationFootprint.for_keys(
        keys,
        shared_keys=shared_keys,
        source_rows=row,
        overlay_rows=future_overlays,
    )


def _previous_ip_address_targets(instance, spec):
    """Resolve the old device and overlay before a GenericForeignKey reassignment."""
    if instance._meta.label_lower != "ipam.ipaddress" or instance.pk is None:
        return set(), ()
    current = type(instance).objects.filter(pk=instance.pk).first()
    if current is None:
        return set(), ()
    keys = spec.resolver(current, spec)
    assigned = current.assigned_object
    if getattr(getattr(assigned, "_meta", None), "label_lower", None) != "dcim.interface":
        return keys, ()
    vrf_name = current.vrf.name if current.vrf else ""
    IPState = apps.get_model("netbox_nso_plugin.nsointerfaceipstate")
    overlays = tuple(
        SourceRow(IPState._meta.label_lower, pk)
        for pk in IPState.objects.filter(
            interface=assigned,
            address=str(current.address),
            vrf=vrf_name,
        ).values_list("pk", flat=True)
    )
    return keys, overlays


def deletion_footprint_for_instance(instance) -> MutationFootprint:
    """Add Django's exact registered cascade closure to a row's footprint."""
    from django.db.models.deletion import Collector

    base = footprint_for_instance(instance)
    collector = Collector(using=instance._state.db or "default", origin=instance)
    collector.collect([instance])
    source_rows = []
    overlay_rows = []

    def add(model, pks, *, future=False):
        label = model._meta.label_lower
        if label not in _REGISTRY:
            return
        target = overlay_rows if label in OVERLAY_MODEL_RANKS else source_rows
        if future:
            target.append(SourceRow(label, None))
        target.extend(SourceRow(label, pk) for pk in pks)

    for model, instances in collector.data.items():
        add(model, (row.pk for row in instances))
    for querysets in collector.field_updates.values():
        for rows in querysets:
            if hasattr(rows, "model"):
                add(rows.model, rows.values_list("pk", flat=True), future=True)
            elif rows:
                model = next(iter(rows)).__class__
                add(model, (row.pk for row in rows), future=True)
    for rows in collector.fast_deletes:
        add(rows.model, rows.values_list("pk", flat=True), future=True)

    cascade = MutationFootprint.for_keys(
        base.revision_keys,
        shared_keys=base.shared_keys,
        source_rows=source_rows,
        overlay_rows=overlay_rows,
    )
    return MutationFootprint.merge(base, cascade)


def route_policy_footprint(groups, *, device_ids=()) -> MutationFootprint:
    """Resolve shared route-policy groups to every affected device and locked row."""
    from django.db.models import Q

    from .models import NSORoutePolicyObjectClass, NSORoutePolicyState

    normalized = tuple(sorted({(str(family), str(name).casefold()) for family, name in groups if name}))
    if not normalized:
        return MutationFootprint.for_keys(
            {(int(device_id), scope) for device_id in device_ids for scope in ("route_policy", "bgp", "isis", "ospf")}
        )
    predicate = functools.reduce(
        operator.or_,
        (Q(family=family, object_name__iexact=name) for family, name in normalized),
    )
    states = list(NSORoutePolicyState.objects.filter(predicate).select_related("management", "content_type"))
    affected_devices = {int(device_id) for device_id in device_ids}
    affected_devices.update(state.management.device_id for state in states)
    source_rows = []
    for state in states:
        if state.content_type_id is None or state.object_id is None:
            continue
        model = state.content_type.model_class()
        if model is not None:
            source_rows.append(SourceRow(model._meta.label_lower, state.object_id))
    classes = list(NSORoutePolicyObjectClass.objects.filter(predicate))
    source_rows.extend(SourceRow(row._meta.label_lower, row.pk) for row in classes)
    source_rows.append(SourceRow("netbox_nso_plugin.nsoroutepolicyobjectclass", None))
    family_models = {
        "prefix_list": (
            "netbox_routing.prefixlist",
            "netbox_routing.customprefix",
            "netbox_routing.prefixlistentry",
        ),
        "community_list": (
            "netbox_routing.communitylist",
            "netbox_routing.community",
            "netbox_routing.communitylistentry",
        ),
        "as_path": ("netbox_routing.aspath", "netbox_routing.aspathentry"),
        "route_map": (
            "netbox_routing.routemap",
            "netbox_routing.routemapentry",
            "netbox_routing.routemapentrysetcommunity",
            "netbox_routing.routemapentrysetcommunity_communities",
            "netbox_routing.routemapentry_match_aspath",
            "netbox_routing.routemapentry_match_community_list",
            "netbox_routing.routemapentry_match_prefix_list",
            "netbox_routing.redistribution",
        ),
    }
    source_rows.extend(
        SourceRow(model_label, None) for family, _name in normalized for model_label in family_models.get(family, ())
    )
    return MutationFootprint.for_keys(
        {(device_id, scope) for device_id in affected_devices for scope in ("route_policy", "bgp", "isis", "ospf")},
        shared_keys=(("route-policy", f"{family}:{name}") for family, name in normalized),
        source_rows=source_rows,
        overlay_rows=(
            *(SourceRow(state._meta.label_lower, state.pk) for state in states),
            SourceRow("netbox_nso_plugin.nsoroutepolicystate", None),
        ),
    )


def vlan_footprint(vlan_id, scopes, *, extra_device_ids=(), shared_keys=()) -> MutationFootprint:
    """Resolve one shared VLAN to its devices, scopes, and exact locked rows."""
    from .apply_state import vlan_intent_targets

    scopes = tuple(sorted(set(scopes)))
    device_ids, rows = vlan_intent_targets(vlan_id, scopes)
    device_ids.update(int(device_id) for device_id in extra_device_ids)
    overlay_rows = [SourceRow(row._meta.label_lower, row.pk) for states in rows.values() for row in states]
    return MutationFootprint.for_keys(
        {(device_id, scope) for device_id in device_ids for scope in scopes},
        shared_keys=(("vlan", str(vlan_id)), *shared_keys),
        source_rows=(SourceRow("ipam.vlan", vlan_id),),
        overlay_rows=overlay_rows,
    )


def _lock_rows(rows: tuple[SourceRow, ...], *, level: int, ranks: tuple[str, ...]) -> None:
    from .apply_state import _enter_level

    rank_by_label = {label: rank for rank, label in enumerate(ranks)}
    unknown = {row.model_label for row in rows} - set(rank_by_label)
    if unknown:
        raise IntentMutationProtocolError(f"renderer-input model ranks are missing {sorted(unknown)!r}")

    def sort_key(candidate):
        return (
            rank_by_label[candidate.model_label],
            candidate.pk is None,
            repr(candidate.pk),
        )

    for row in sorted(rows, key=sort_key):
        if row.pk is None:
            continue
        _enter_level(level, sort_key(row))
        model = apps.get_model(row.model_label)
        list(model.objects.select_for_update(of=("self",)).filter(pk=row.pk).order_by("pk"))


def _revalidate_sources(footprint: MutationFootprint) -> None:
    expected_devices = set(footprint.device_ids)
    static_route_assignment = SourceRow("netbox_routing.staticroute_devices", None) in footprint.source_rows
    for row in footprint.source_rows:
        if row.pk is None:
            continue
        if row.model_label == "netbox_routing.staticroute" and static_route_assignment:
            continue
        spec = _REGISTRY.get(row.model_label)
        if spec is None:
            continue
        instance = apps.get_model(row.model_label).objects.filter(pk=row.pk).first()
        if instance is None:
            raise RendererTargetsChanged(f"{row.model_label} row {row.pk!r} disappeared during acquisition")
        resolved_devices = {device_id for device_id, _scope in spec.resolver(instance, spec)}
        if not resolved_devices <= expected_devices:
            raise IntentMutationProtocolError(
                f"{row.model_label} row {row.pk!r} changed its renderer targets during acquisition"
            )


def _deploying_scope_rows(footprint: MutationFootprint, revision_keys=None) -> tuple[SourceRow, ...]:
    """Discover candidate Apply-in-flight rows after their revision locks are held."""
    from .apply_state import deploying_models

    models_by_scope = deploying_models()
    rows = []
    for device_id, scope in revision_keys if revision_keys is not None else footprint.revision_keys:
        model = models_by_scope.get(scope)
        if model is None:
            continue
        rows.extend(
            SourceRow(model._meta.label_lower, pk)
            for pk in model.objects.filter(
                management__device_id=device_id,
                status="deploying",
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    return tuple(rows)


def _still_deploying_rows(rows: tuple[SourceRow, ...]) -> tuple[SourceRow, ...]:
    """Keep candidates whose locked row version is still deploying."""
    current = set()
    labels = {row.model_label for row in rows}
    for label in OVERLAY_MODEL_RANKS:
        if label not in labels:
            continue
        pks = {row.pk for row in rows if row.model_label == label and row.pk is not None}
        current.update(
            SourceRow(label, pk)
            for pk in apps.get_model(label).objects.filter(pk__in=pks, status="deploying").values_list("pk", flat=True)
        )
    return tuple(row for row in rows if row in current)


def _bump_and_lock_deploying(footprint: MutationFootprint, revision_keys=None) -> tuple[SourceRow, ...]:
    """Advance locked revisions and lock the complete promoted scope."""
    from .outbox import bump_intent_revision

    revision_keys = tuple(footprint.revision_keys if revision_keys is None else revision_keys)
    if not set(revision_keys) <= set(footprint.revision_keys):
        raise IntentMutationProtocolError("a bump key was not locked by this footprint")
    for device_id, scope in revision_keys:
        bump_intent_revision(device_id, scope)
    deploying_rows = _deploying_scope_rows(footprint, revision_keys)
    locked_overlay_rows = tuple(set(footprint.overlay_rows) | set(deploying_rows))
    _lock_rows(locked_overlay_rows, level=8, ranks=OVERLAY_MODEL_RANKS)
    return _still_deploying_rows(deploying_rows)


def _repend_locked_rows(rows: tuple[SourceRow, ...]) -> None:
    """Force rows captured as deploying back to pending Apply state."""
    from .signals import suppress_intent_push

    with suppress_intent_push():
        for row_ref in rows:
            if row_ref.pk is None:
                continue
            row = apps.get_model(row_ref.model_label).objects.filter(pk=row_ref.pk).first()
            # A complete planned delete can consume a row from the initially deploying
            # set. Only surviving rows need to return to operator-pending state.
            if row is None:
                continue
            row.status = "accepted"
            update_fields = ["status"]
            if hasattr(row, "apply_attempt_id"):
                row.apply_attempt_id = None
                update_fields.append("apply_attempt_id")
            row.save(update_fields=update_fields)


def _acquire(
    footprint: MutationFootprint,
    *,
    bump: bool = True,
    join_deployment_gate: bool = True,
    defer_repend: bool = False,
    capture_deploying: bool = False,
    bump_keys=None,
) -> tuple[SourceRow, ...]:
    from .apply_state import (
        _enter_level,
        lock_device_intent_transaction,
        lock_intent_revisions,
        lock_shared_dependencies,
    )
    from .deployment import lock_mutation
    from .models import NSODeviceManagement, NSOFamilyReadState

    if not transaction.get_connection().in_atomic_block:
        raise IntentMutationProtocolError("intent_transaction requires transaction.atomic()")
    if join_deployment_gate:
        lock_mutation()
    lock_shared_dependencies(footprint.shared_keys)
    for device_id in footprint.device_ids:
        lock_device_intent_transaction(device_id)
    management_ids = list(
        NSODeviceManagement.objects.filter(device_id__in=footprint.device_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for management_id in management_ids:
        _enter_level(5, (0, management_id))
    management_rows = list(
        NSODeviceManagement.objects.select_for_update(of=("self",)).filter(pk__in=management_ids).order_by("pk")
    )
    read_state_keys = list(
        NSOFamilyReadState.objects.filter(management__in=management_rows)
        .order_by("management_id", "family")
        .values_list("management_id", "family")
    )
    for management_id, family in read_state_keys:
        _enter_level(5, (1, management_id, family))
    list(
        NSOFamilyReadState.objects.select_for_update(of=("self",))
        .filter(management_id__in=management_ids)
        .order_by("management_id", "family")
    )
    _lock_rows(footprint.source_rows, level=6, ranks=SOURCE_MODEL_RANKS)
    _revalidate_sources(footprint)
    scopes_by_device: dict[int, list[str]] = {}
    for device_id, scope in footprint.revision_keys:
        scopes_by_device.setdefault(device_id, []).append(scope)
    for device_id, scopes in sorted(scopes_by_device.items()):
        lock_intent_revisions(device_id, scopes)
    if bump:
        deploying_rows = _bump_and_lock_deploying(footprint, bump_keys)
        if not defer_repend:
            _repend_locked_rows(deploying_rows)
        return deploying_rows
    deploying_rows = _deploying_scope_rows(footprint) if capture_deploying else ()
    _lock_rows(tuple(set(footprint.overlay_rows) | set(deploying_rows)), level=8, ranks=OVERLAY_MODEL_RANKS)
    return _still_deploying_rows(deploying_rows)


def _upgrade_detected_reconcile(
    permit: _Permit,
    requested: MutationFootprint,
    *,
    bump_keys=None,
) -> None:
    """Upgrade a locked read transaction when its body proves a content delta."""
    if not permit.detect_reconcile_content:
        raise IntentMutationProtocolError("read-side content mutation requires a predicted reconcile plan")
    prelocked_rows = set(permit.footprint.overlay_rows)
    missing_rows = {row for row in requested.overlay_rows if row.pk is not None and row not in prelocked_rows}
    if missing_rows:
        details = sorted((row.model_label, repr(row.pk)) for row in missing_rows)
        raise IntentMutationProtocolError(f"detected reconcile content rows were not prelocked: {details!r}")
    revision_keys = tuple(permit.footprint.revision_keys if bump_keys is None else bump_keys)
    if not set(revision_keys) <= set(permit.footprint.revision_keys):
        raise IntentMutationProtocolError("detected reconcile content keys were not prelocked")

    from .outbox import bump_intent_revision

    for device_id, scope in revision_keys:
        bump_intent_revision(device_id, scope)
    permit.deferred_repend_rows = permit.initial_deploying_rows
    permit.dml_kind = "content"
    permit.bump_revisions = True
    permit.detect_reconcile_content = False


def _join_active_permit(
    footprint: MutationFootprint,
    *,
    settles_deploying: bool,
) -> _Permit | None:
    """Join a covering permit and retain the strictest reconcile settlement rule."""
    active = _ACTIVE_PERMIT.get()
    if active is None:
        return None
    if not active.footprint.covers(footprint):
        raise IntentMutationProtocolError("an active mutation footprint cannot expand")
    active.settles_deploying = active.settles_deploying and settles_deploying
    return active


@contextlib.contextmanager
def _intent_transaction(
    footprint: MutationFootprint,
    *,
    defer_repend: bool = False,
    repend_after: bool = False,
    settles_deploying: bool = True,
    bump_keys=None,
):
    """Acquire one content permit and apply the requested re-pend timing."""
    active = _ACTIVE_PERMIT.get()
    if active is not None:
        yield active
        return
    from .apply_state import lock_order_scope

    with transaction.atomic(), lock_order_scope():
        permit = _Permit(
            footprint=footprint,
            dml_kind="content",
            settles_deploying=settles_deploying,
        )
        token = _ACTIVE_PERMIT.set(permit)
        try:
            deploying_rows = _acquire(
                footprint,
                defer_repend=defer_repend or repend_after,
                bump_keys=bump_keys,
            )
            yield permit
            if (defer_repend or repend_after) and permit.settles_deploying and deploying_rows:
                _repend_locked_rows(deploying_rows)
        finally:
            _ACTIVE_PERMIT.reset(token)


@contextlib.contextmanager
def intent_transaction(footprint: MutationFootprint):
    """Acquire L2-L8, bump at L7, then grant the immutable L9 write permit."""
    with _intent_transaction(footprint) as permit:
        yield permit


@contextlib.contextmanager
def mirror_transaction(
    footprint: MutationFootprint,
    *,
    detect_content_changes: bool = False,
    repeatable_read: bool = False,
):
    """Acquire a complete read-side footprint without advancing intent identity."""
    active = _ACTIVE_PERMIT.get()
    if active is not None:
        yield active
        return
    from .apply_state import lock_order_scope

    with transaction.atomic(), lock_order_scope():
        # Django TestCase opens one outer transaction before fixture setup and marks its
        # Atomic block. PostgreSQL cannot change that transaction's isolation after the
        # fixture queries. Dedicated TransactionTestCase coverage exercises the real
        # REPEATABLE READ repair boundary; production has no marked TestCase block.
        django_test_wrapper = any(
            getattr(block, "_from_testcase", False) for block in connections["default"].atomic_blocks
        )
        if repeatable_read and not django_test_wrapper:
            with connections["default"].cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        permit = _Permit(
            footprint=footprint,
            dml_kind="reconcile",
            detect_reconcile_content=detect_content_changes,
            settles_deploying=settles_deploying,
        )
        token = _ACTIVE_PERMIT.set(permit)
        try:
            permit.initial_deploying_rows = _acquire(
                footprint,
                bump=False,
                capture_deploying=detect_content_changes,
            )
            yield permit
            if permit.settles_deploying and permit.deferred_repend_rows:
                _repend_locked_rows(permit.deferred_repend_rows)
        finally:
            _ACTIVE_PERMIT.reset(token)


@contextlib.contextmanager
def reconcile_transaction(plan: ReconcileMutationPlan):
    """Acquire a read plan with the permit required by its canonical fragment delta."""
    if plan.changes_content:
        mutation = _intent_transaction(
            plan.footprint,
            defer_repend=True,
            settles_deploying=plan.settles_deploying,
        )
    else:
        mutation = mirror_transaction(
            plan.footprint,
            detect_content_changes=plan.detect_content_changes,
            settles_deploying=plan.settles_deploying,
        )
    with mutation as permit:
        if plan.validate_after_acquire is not None:
            plan.validate_after_acquire()
        yield permit


@contextlib.contextmanager
def content_mutation(keys, **footprint_fields):
    """Acquire a revision-bearing content permit for the named keys."""
    footprint = MutationFootprint.for_keys(keys, **footprint_fields)
    with intent_transaction(footprint) as permit:
        yield permit


@contextlib.contextmanager
def offline_mutation():
    """Permit migration DML while the exclusive deployment procedure owns Apply."""
    if not transaction.get_connection().in_atomic_block:
        raise IntentMutationProtocolError("offline mutation requires transaction.atomic()")
    permit = _Permit(footprint=MutationFootprint(), dml_kind="offline")
    token = _ACTIVE_PERMIT.set(permit)
    try:
        yield permit
    finally:
        _ACTIVE_PERMIT.reset(token)


@contextlib.contextmanager
def mirror_refresh(instance, update_fields):
    """Permit one per-instance lifecycle save only when its fragment stays equal."""
    spec = _REGISTRY.get(instance._meta.label_lower)
    if spec is None:
        yield
        return
    update_fields = frozenset(update_fields or ())
    if not update_fields or not update_fields <= spec.lifecycle_fields:
        raise IntentMutationProtocolError("mirror_refresh requires declared lifecycle-only update_fields")
    if not transaction.get_connection().in_atomic_block:
        raise IntentMutationProtocolError("mirror_refresh requires transaction.atomic()")
    locked = type(instance).objects.select_for_update(of=("self",)).filter(pk=instance.pk).first()
    before = ABSENT if locked is None else canonical_fragment(locked, spec)
    permit = _Permit(
        footprint=MutationFootprint(),
        dml_kind="mirror",
        mirror_table=spec.table,
        mirror_pk=instance.pk,
        mirror_before=before,
        mirror_instance=instance,
        mirror_update_fields=update_fields,
    )
    token = _ACTIVE_PERMIT.set(permit)
    try:
        yield locked
        if canonical_fragment(locked, spec) != before:
            raise IntentMutationProtocolError("mirror_refresh changed a canonical renderer fragment")
    finally:
        _ACTIVE_PERMIT.reset(token)


@contextlib.contextmanager
def locked_mirror_refresh(instance, update_fields):
    """Yield the locked save target without shadowing an active exact mutation permit."""
    _discard_rolled_back_implicit_permit()
    if _ACTIVE_PERMIT.get() is not None:
        yield instance
        return
    with mirror_refresh(instance, update_fields) as locked:
        yield locked


def update_mirror_fields(instance, **values):
    """Lock and save lifecycle fields through the instance yielded by ``mirror_refresh``."""
    from .signals import suppress_intent_push

    fields = frozenset(values)
    with transaction.atomic(), suppress_intent_push(), mirror_refresh(instance, fields) as locked:
        if locked is None:
            return None
        for field_name, value in values.items():
            setattr(locked, field_name, value)
        locked.save(update_fields=fields)
    for field_name, value in values.items():
        setattr(instance, field_name, value)
    return locked


def mirror_reconciler(function):
    """Run one read-side reconciler inside the sanctioned mutation boundary."""

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        from .signals import suppress_intent_push

        token = _RECONCILER_ACTIVE.set(_RECONCILER_ACTIVE.get() + 1)
        try:
            with transaction.atomic(), suppress_intent_push():
                return function(*args, **kwargs)
        finally:
            _RECONCILER_ACTIVE.reset(token)

    return wrapped


def revision_was_acquired(device_id: int, scope: str) -> bool:
    """Return whether this transaction acquired and bumped one revision key."""
    permit = _ACTIVE_PERMIT.get()
    return permit is not None and (int(device_id), str(scope)) in permit.footprint.revision_keys


def mirror_refresh_is_active() -> bool:
    """Return whether the current registered write preserves its canonical fragment."""
    permit = _ACTIVE_PERMIT.get()
    return permit is not None and permit.dml_kind == "mirror"


def _validate_explicit_write(sender, instance, update_fields=None, **kwargs):
    """Validate a registered save only while an explicit writer is active."""
    from .renderer_writer import active_renderer_writer, require_planned_signal_write

    if active_renderer_writer() is not None:
        require_planned_signal_write(instance, update_fields=update_fields)


def _validate_explicit_delete(sender, instance, **kwargs):
    """Validate a registered delete only while an explicit writer is active."""
    from .renderer_writer import active_renderer_writer, require_planned_signal_write

    if active_renderer_writer() is not None:
        require_planned_signal_write(instance, deleting=True)


def _validate_explicit_m2m(sender, instance, action, reverse=False, pk_set=None, **kwargs):
    """Validate a registered M2M operation only while an explicit writer is active."""
    if reverse or not action.startswith("pre_"):
        return
    from .renderer_writer import active_renderer_writer, require_planned_m2m_signal

    if active_renderer_writer() is None:
        return
    field_name = next(
        (field.name for field in instance._meta.many_to_many if field.remote_field.through is sender),
        None,
    )
    if field_name is None:
        raise IntentMutationProtocolError("the active writer cannot resolve the M2M field")
    require_planned_m2m_signal(instance, action, field_name, pk_set)


def register_renderer_input(spec: RendererInputSpec) -> None:
    """Register one concrete or auto-created-through renderer input."""
    label = spec.model_label.lower()
    model = apps.get_model(label)
    normalized = RendererInputSpec(
        model_label=label,
        scopes=tuple(spec.scopes),
        content_fields=frozenset(spec.content_fields),
        lifecycle_fields=frozenset(spec.lifecycle_fields),
        resolver=spec.resolver,
        required_trace_fixtures=tuple(spec.required_trace_fixtures),
        fragment=spec.fragment,
        shared_kind=spec.shared_kind,
        dependency_resolver=spec.dependency_resolver,
        prospective_visibility=spec.prospective_visibility,
    )
    _REGISTRY[label] = normalized
    uid = f"nso_renderer_writer_{label}"
    pre_save.connect(_validate_explicit_write, sender=model, dispatch_uid=f"{uid}_pre_save", weak=False)
    pre_delete.connect(_validate_explicit_delete, sender=model, dispatch_uid=f"{uid}_pre_delete", weak=False)
    if model._meta.auto_created:
        m2m_changed.connect(_validate_explicit_m2m, sender=model, dispatch_uid=f"{uid}_m2m", weak=False)


def _field_sets(model):
    concrete = {field.name for field in model._meta.concrete_fields}
    lifecycle = concrete & _GLOBAL_LIFECYCLE_FIELDS
    return frozenset(concrete - lifecycle), frozenset(lifecycle)


def _register(
    label,
    scopes,
    *,
    shared_kind=None,
    fixtures=None,
    model=None,
    content_fields=None,
):
    model = model or apps.get_model(label)
    content, lifecycle = _field_sets(model)
    if content_fields is not None:
        content = frozenset(content_fields)
        lifecycle = frozenset({field.name for field in model._meta.concrete_fields} - set(content))
    register_renderer_input(
        RendererInputSpec(
            model_label=model._meta.label_lower,
            scopes=tuple(scopes),
            content_fields=content,
            lifecycle_fields=lifecycle,
            resolver=_generic_keys,
            required_trace_fixtures=tuple(fixtures or scopes),
            fragment=_declared_fields_fragment,
            shared_kind=shared_kind,
        ),
    )


def _register_auto_through(model, scopes, *, shared_kind=None, field_names=None):
    for m2m_field in model._meta.many_to_many:
        if field_names is not None and m2m_field.name not in field_names:
            continue
        if m2m_field.name == "tags":
            continue
        through = m2m_field.remote_field.through
        if through._meta.auto_created and through._meta.label_lower not in _REGISTRY:
            content_fields = {field.name for field in through._meta.concrete_fields if not field.primary_key}
            _register(
                through._meta.label_lower,
                scopes,
                shared_kind=shared_kind,
                model=through,
                content_fields=content_fields,
            )


def register_builtin_renderer_inputs() -> None:
    """Install the declared renderer-input registry and writer validators."""
    if _REGISTRY:
        return
    all_scopes = (
        "interface",
        "ip",
        "snmp",
        "logging",
        "svi",
        "subinterface",
        "interface_mtu",
        "vlan",
        "bfd",
        "static_route",
        "isis_flex_algo",
        "l2_sap",
        "isis",
        "bgp",
        "route_policy",
        "ospf",
        "lacp",
        "switchport",
    )
    declarations = {
        "netbox_nso_plugin.nsointerfacestate": ("interface",),
        "netbox_nso_plugin.nsointerfaceipstate": ("ip",),
        "netbox_nso_plugin.nsosnmpcommunitystate": ("snmp",),
        "netbox_nso_plugin.nsosnmpv3userstate": ("snmp",),
        "netbox_nso_plugin.nsosnmphoststate": ("snmp",),
        "netbox_nso_plugin.nsosnmpsysteminfostate": ("snmp",),
        "netbox_nso_plugin.nsologginghoststate": ("logging",),
        "netbox_nso_plugin.nsologginglevelstate": ("logging",),
        "netbox_nso_plugin.nsosvistate": ("svi",),
        "netbox_nso_plugin.nsosubinterfacestate": ("subinterface",),
        "netbox_nso_plugin.nsointerfacemtustate": ("interface_mtu",),
        "netbox_nso_plugin.nsovlanstate": ("vlan",),
        "netbox_nso_plugin.nsobfdinterfacestate": ("bfd",),
        "netbox_nso_plugin.nsostaticroutestate": ("static_route",),
        "netbox_nso_plugin.nsoisisflexalgostate": ("isis_flex_algo",),
        "netbox_nso_plugin.nsol2sapstate": ("l2_sap",),
        "netbox_nso_plugin.nsoisisinstancestate": ("isis",),
        "netbox_nso_plugin.nsoisisinterfacestate": ("isis",),
        "netbox_nso_plugin.nsobgppeerstate": ("bgp",),
        "netbox_nso_plugin.nsoredistributionstate": ("bgp", "isis", "ospf"),
        "netbox_nso_plugin.nsoroutepolicystate": ("route_policy",),
        "netbox_nso_plugin.nsoospfinstancestate": ("ospf",),
        "netbox_nso_plugin.nsoospfinterfacestate": ("ospf",),
        "netbox_nso_plugin.nsolacpbundlestate": ("lacp",),
        "netbox_nso_plugin.nsolacpmemberstate": ("lacp",),
        "netbox_nso_plugin.nsoswitchportstate": ("switchport",),
        "netbox_nso_plugin.nsodevicemanagement": all_scopes,
        "netbox_nso_plugin.nsoplatformnedmapping": all_scopes,
        "dcim.device": all_scopes,
        "dcim.interface": (
            "interface",
            "ip",
            "svi",
            "subinterface",
            "interface_mtu",
            "bfd",
            "isis",
            "bgp",
            "ospf",
            "lacp",
            "switchport",
        ),
        "ipam.ipaddress": ("ip", "bgp"),
        "ipam.vrf": ("static_route",),
        "ipam.asn": ("bgp",),
        "netbox_routing.bgppeertemplate": ("bgp",),
    }
    for label, scopes in declarations.items():
        model = apps.get_model(label)
        if label == "dcim.device":
            content_fields = {"platform"}
        elif label == "dcim.interface":
            content_fields = {"device", "name", "description", "enabled", "parent"}
        else:
            content_fields = _PROMOTED_CONTENT_FIELDS.get(label)
        _register(
            label,
            scopes,
            model=model,
            content_fields=content_fields,
        )
    _register_auto_through(
        apps.get_model("netbox_nso_plugin.nsoswitchportstate"),
        ("switchport",),
        field_names={"tagged_vlans"},
    )
    vlan_model = apps.get_model("ipam.vlan")
    _register(
        "ipam.vlan",
        ("vlan", "svi", "switchport"),
        shared_kind="vlan",
        model=vlan_model,
        content_fields={"vid", "name"},
    )
    _REGISTRY["netbox_nso_plugin.nsovlanstate"] = replace(
        _REGISTRY["netbox_nso_plugin.nsovlanstate"],
        fragment=_vlan_state_fragment,
    )
    _REGISTRY["ipam.vlan"] = replace(
        _REGISTRY["ipam.vlan"],
        fragment=_native_vlan_fragment,
    )
    _REGISTRY["netbox_nso_plugin.nsolacpbundlestate"] = replace(
        _REGISTRY["netbox_nso_plugin.nsolacpbundlestate"],
        fragment=_lacp_bundle_fragment,
        dependency_resolver=_lacp_bundle_dependencies,
    )
    exact_direct_fragments = {
        "netbox_nso_plugin.nsobfdinterfacestate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsobgppeerstate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsointerfaceipstate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsointerfacemtustate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoisisflexalgostate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoisisinstancestate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoisisinterfacestate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsol2sapstate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsolacpmemberstate": _lacp_member_fragment,
        "netbox_nso_plugin.nsologginghoststate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsologginglevelstate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoospfinstancestate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoospfinterfacestate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoredistributionstate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoroutepolicystate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsosnmpsysteminfostate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsostaticroutestate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsosubinterfacestate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsosvistate": _direct_overlay_fragment,
        "netbox_nso_plugin.nsoswitchportstate": _switchport_fragment,
    }
    dependency_resolvers = {
        "netbox_nso_plugin.nsolacpmemberstate": _lacp_member_dependencies,
        "netbox_nso_plugin.nsosvistate": _svi_dependencies,
        "netbox_nso_plugin.nsoswitchportstate": _switchport_dependencies,
    }
    for label, fragment in exact_direct_fragments.items():
        _REGISTRY[label] = replace(
            _REGISTRY[label],
            fragment=fragment,
            dependency_resolver=dependency_resolvers.get(label),
            shared_kind="route_policy" if label == "netbox_nso_plugin.nsoroutepolicystate" else None,
            prospective_visibility=(
                _route_policy_prospective_visibility if label == "netbox_nso_plugin.nsoroutepolicystate" else None
            ),
        )
    _REGISTRY["netbox_nso_plugin.nsovlanstate"] = replace(
        _REGISTRY["netbox_nso_plugin.nsovlanstate"],
        dependency_resolver=_vlan_state_dependencies,
    )
    _REGISTRY["dcim.interface"] = replace(
        _REGISTRY["dcim.interface"],
        dependency_resolver=_interface_dependencies,
    )
    _REGISTRY["netbox_nso_plugin.nsointerfacestate"] = replace(
        _REGISTRY["netbox_nso_plugin.nsointerfacestate"],
        fragment=_interface_state_fragment,
    )
    snmp_fragments = {
        "netbox_nso_plugin.nsosnmpcommunitystate": _snmp_community_fragment,
        "netbox_nso_plugin.nsosnmpv3userstate": _snmp_v3_user_fragment,
        "netbox_nso_plugin.nsosnmphoststate": _snmp_host_fragment,
    }
    for label, fragment in snmp_fragments.items():
        _REGISTRY[label] = replace(_REGISTRY[label], fragment=fragment)

    native_declarations = {
        "netbox_routing.staticroute": ("static_route",),
        "netbox_routing.isisinstance": ("isis",),
        "netbox_routing.isislevel": ("isis",),
        "netbox_routing.bgprouter": ("bgp",),
        "netbox_routing.bgpscope": ("bgp",),
        "netbox_routing.bgppeer": ("bgp",),
        "netbox_routing.bgpaddressfamily": ("bgp",),
        "netbox_routing.bgppeeraddressfamily": ("bgp",),
        "netbox_routing.redistribution": ("bgp", "isis", "ospf"),
    }
    for label, scopes in native_declarations.items():
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        content_fields = None
        if label == "netbox_routing.staticroute":
            content_fields = {"vrf", "prefix", "next_hop", "permanent", "tag", "metric"}
        _register(label, scopes, model=model, content_fields=content_fields)

    if "netbox_routing.staticroute" in _REGISTRY:
        _REGISTRY["netbox_routing.staticroute"] = replace(
            _REGISTRY["netbox_routing.staticroute"],
            fragment=_static_route_fragment,
        )
        StaticRoute = apps.get_model("netbox_routing.staticroute")
        m2m_changed.connect(
            _validate_explicit_m2m,
            sender=StaticRoute.devices.through,
            dispatch_uid="nso_renderer_writer_static_route_devices_m2m",
            weak=False,
        )

    from .shared_object_ownership import registered_specs

    route_models = tuple(
        dict.fromkeys(label for shared_spec in registered_specs().values() for label in shared_spec.renderer_models)
    )
    for label in route_models:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        _register(
            label,
            ("route_policy", "bgp", "isis", "ospf"),
            shared_kind="route_policy",
            model=model,
            content_fields=_ROUTE_POLICY_CONTENT_FIELDS[label],
        )
        route_m2m_fields = {
            "netbox_routing.routemapentry": {
                "match_aspath",
                "match_community_list",
                "match_prefix_list",
            },
            "netbox_routing.routemapentrysetcommunity": {"communities"},
        }.get(label, set())
        _register_auto_through(
            model,
            ("route_policy", "bgp", "isis", "ospf"),
            shared_kind="route_policy",
            field_names=route_m2m_fields,
        )
