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
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import sqlparse
from django.apps import apps
from django.db import connections, transaction
from django.db.backends.signals import connection_created
from django.db.models.signals import (
    m2m_changed,
    post_delete,
    post_migrate,
    post_save,
    pre_delete,
    pre_migrate,
    pre_save,
)
from sqlparse.sql import Comparison, Identifier, IdentifierList
from sqlparse.tokens import Comment, Keyword, Literal

logger = logging.getLogger(__name__)

ABSENT = ("ABSENT",)

SOURCE_MODEL_RANKS = (
    "ipam.vlan",
    "ipam.vlangroup",
    "ipam.vrf",
    "ipam.asn",
    "dcim.device",
    "dcim.interface",
    "dcim.interface_tagged_vlans",
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
    "netbox_routing.ospfinstance",
    "netbox_routing.isisinstance",
    "netbox_routing.isislevel",
    "netbox_routing.bgprouter",
    "netbox_routing.bgpscope",
    "netbox_routing.bgppeer",
    "netbox_routing.bgppeertemplate",
    "netbox_routing.bgpaddressfamily",
    "netbox_routing.bgppeeraddressfamily",
    "netbox_routing.redistribution",
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

# Django's collector clears this FK with one lazy SET_NULL update on every ipam.Prefix delete.
# The new value is a literal or a bound parameter, depending on the Django version.
_SOURCE_POOL_CASCADE = re.compile(
    r'SET\s+"source_pool_id"\s*=\s*(?P<value>NULL|%s)'
    r'\s+WHERE\s+(?:"[A-Za-z0-9_]+"\.)?"source_pool_id"\s*(?:IN\s*\(|=)',
    re.IGNORECASE,
)
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


class IntentTransactionNoOp(Exception):
    """Unwind an intent transaction when a locked precondition rejects the mutation."""

    def __init__(self, result=None):
        super().__init__()
        self.result = result


class RendererTargetsChanged(IntentMutationProtocolError):
    """A source row changed the devices that render it during acquisition."""


@dataclass(frozen=True)
class _DMLTarget:
    operation: str
    table: str


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
    bump_revisions: bool = True
    join_deployment_gate: bool = True
    mirror_table: str | None = None
    mirror_pk: Any = None
    mirror_before: Any = None
    mirror_instance: Any = None
    mirror_update_fields: frozenset[str] | None = None
    authorized_dml: dict[str, int] = field(default_factory=dict)
    tokens: list = field(default_factory=list)
    implicit: bool = False
    deferred_update: dict[str, Any] = field(default_factory=dict)
    atomic_block_id: int | None = None
    detect_reconcile_content: bool = False
    initial_deploying_rows: tuple[SourceRow, ...] = ()
    deferred_repend_rows: tuple[SourceRow, ...] = ()


_REGISTRY: dict[str, RendererInputSpec] = {}
_TABLE_REGISTRY: dict[str, RendererInputSpec] = {}
_ACTIVE_PERMIT: contextvars.ContextVar[_Permit | None] = contextvars.ContextVar(
    "nso_intent_mutation_permit", default=None
)
_IMPLICIT_PERMITS: contextvars.ContextVar[dict[object, tuple]] = contextvars.ContextVar(
    "nso_intent_implicit_permits", default={}
)
_MIGRATIONS_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar("nso_intent_migrations_active", default=False)
# (alias, atomic block, prefix pk) per pool delete in flight; the block object is held so a
# rolled-back delete can never be mistaken for an open one.
_DELETING_POOLS: contextvars.ContextVar[frozenset[tuple]] = contextvars.ContextVar(
    "nso_intent_deleting_pools", default=frozenset()
)
_RECONCILER_ACTIVE: contextvars.ContextVar[int] = contextvars.ContextVar("nso_intent_reconciler_active", default=0)
_DML_PARSE_SKIP_KEYWORDS = frozenset(
    {"SELECT", "SET", "SAVEPOINT", "RELEASE", "SHOW", "BEGIN", "COMMIT", "ROLLBACK", "DECLARE", "FETCH", "CLOSE"}
)
_FIRST_SQL_KEYWORD = re.compile(
    r"\A(?:\s+|--[^\r\n]*(?:\r\n?|\n|\Z)|/\*.*?\*/)*([A-Za-z]+)",
    re.DOTALL,
)


def _discard_rolled_back_implicit_permit() -> None:
    """Drop an implicit permit whose acquisition savepoint no longer exists."""
    permit = _ACTIVE_PERMIT.get()
    if permit is None or not permit.implicit or permit.atomic_block_id is None:
        return
    active_blocks = transaction.get_connection().atomic_blocks
    if any(id(block) == permit.atomic_block_id for block in active_blocks):
        return
    _ACTIVE_PERMIT.set(None)
    _IMPLICIT_PERMITS.set({})


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
    return _normal(lacp_member_intent_item(instance))


def _switchport_fragment(instance):
    from .signals import switchport_intent_item

    if instance.status not in ("accepted", "deploying", "in_sync"):
        return ABSENT
    tagged = (vlan.vid for vlan in instance.tagged_vlans.all()) if instance.pk else ()
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


def _effective_after_fragment(instance, spec, update_fields):
    """Serialize only values this save will persist, not unrelated stale attributes."""
    if instance.pk is None or instance._state.adding or update_fields is None:
        return canonical_fragment(instance, spec)
    current = type(instance).objects.filter(pk=instance.pk).first()
    if current is None:
        return canonical_fragment(instance, spec)
    effective = copy.copy(current)
    for field_name in update_fields:
        field = instance._meta.get_field(field_name)
        setattr(effective, field.attname, getattr(instance, field.attname))
    return canonical_fragment(effective, spec)


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
        return _management_keys(device_ids, spec.scopes)
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
    if instance.pk is not None:
        current = type(instance).objects.filter(pk=instance.pk).first()
        if current is not None:
            groups.update(_route_policy_groups(current))
    return route_policy_footprint(groups) if groups else _regular_instance_footprint(instance, spec)


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
        if label not in _REGISTRY or label == "netbox_nso_plugin.nsodevicemanagement":
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
        resolved_devices = {device_id for device_id, _scope in spec.resolver(instance, spec)}
        if instance is not None and not resolved_devices <= expected_devices:
            raise RendererTargetsChanged(
                f"{row.model_label} row {row.pk!r} changed its renderer targets during acquisition"
            )


def _deploying_scope_rows(footprint: MutationFootprint) -> tuple[SourceRow, ...]:
    """Resolve the complete Apply-in-flight set after its revision locks are held."""
    from .apply_state import deploying_models

    models_by_scope = deploying_models()
    rows = []
    for device_id, scope in footprint.revision_keys:
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


def _bump_and_lock_deploying(footprint: MutationFootprint) -> tuple[SourceRow, ...]:
    """Advance locked revisions and lock the complete promoted scope."""
    from .outbox import bump_intent_revision

    for device_id, scope in footprint.revision_keys:
        bump_intent_revision(device_id, scope)
    deploying_rows = _deploying_scope_rows(footprint)
    locked_overlay_rows = tuple(set(footprint.overlay_rows) | set(deploying_rows))
    _lock_rows(locked_overlay_rows, level=8, ranks=OVERLAY_MODEL_RANKS)
    return deploying_rows


def _repend_locked_rows(rows: tuple[SourceRow, ...]) -> None:
    """Force rows captured as deploying back to pending Apply state."""
    from .signals import suppress_intent_push

    with suppress_intent_push():
        for row_ref in rows:
            if row_ref.pk is None:
                continue
            row = apps.get_model(row_ref.model_label).objects.get(pk=row_ref.pk)
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
        deploying_rows = _bump_and_lock_deploying(footprint)
        if not defer_repend:
            _repend_locked_rows(deploying_rows)
        return deploying_rows
    _lock_rows(footprint.overlay_rows, level=8, ranks=OVERLAY_MODEL_RANKS)
    return ()


def _upgrade_detected_reconcile(permit: _Permit, requested: MutationFootprint) -> None:
    """Upgrade a locked read transaction when its body proves a content delta."""
    if not permit.detect_reconcile_content:
        raise IntentMutationProtocolError("read-side content mutation requires a predicted reconcile plan")
    prelocked_rows = set(permit.footprint.overlay_rows)
    missing_rows = {row for row in requested.overlay_rows if row.pk is not None and row not in prelocked_rows}
    if missing_rows:
        details = sorted((row.model_label, repr(row.pk)) for row in missing_rows)
        raise IntentMutationProtocolError(f"detected reconcile content rows were not prelocked: {details!r}")
    from .outbox import bump_intent_revision

    for device_id, scope in permit.footprint.revision_keys:
        bump_intent_revision(device_id, scope)
    permit.deferred_repend_rows = permit.initial_deploying_rows
    permit.dml_kind = "content"
    permit.bump_revisions = True
    permit.detect_reconcile_content = False


@contextlib.contextmanager
def _intent_transaction(footprint: MutationFootprint, *, defer_repend: bool = False):
    """Acquire one content permit, optionally forcing its re-pend at body exit."""
    _discard_rolled_back_implicit_permit()
    active = _ACTIVE_PERMIT.get()
    if active is not None:
        if not active.footprint.covers(footprint):
            raise IntentMutationProtocolError("an active mutation footprint cannot expand")
        yield active
        return
    from .apply_state import lock_order_scope

    with transaction.atomic(), lock_order_scope():
        permit = _Permit(footprint=footprint, dml_kind="content")
        token = _ACTIVE_PERMIT.set(permit)
        try:
            deploying_rows = _acquire(footprint, defer_repend=defer_repend)
            yield permit
            if defer_repend and deploying_rows:
                _repend_locked_rows(deploying_rows)
        finally:
            _ACTIVE_PERMIT.reset(token)


@contextlib.contextmanager
def intent_transaction(footprint: MutationFootprint):
    """Acquire L2-L8, bump at L7, then grant the immutable L9 write permit."""
    with _intent_transaction(footprint) as permit:
        yield permit


@contextlib.contextmanager
def mirror_transaction(footprint: MutationFootprint, *, detect_content_changes: bool = False):
    """Acquire a complete read-side footprint without advancing intent identity."""
    _discard_rolled_back_implicit_permit()
    active = _ACTIVE_PERMIT.get()
    if active is not None:
        if not active.footprint.covers(footprint):
            raise IntentMutationProtocolError("an active mutation footprint cannot expand")
        yield active
        return
    from .apply_state import lock_order_scope

    with transaction.atomic(), lock_order_scope():
        permit = _Permit(
            footprint=footprint,
            dml_kind="reconcile",
            detect_reconcile_content=detect_content_changes,
        )
        token = _ACTIVE_PERMIT.set(permit)
        try:
            _acquire(footprint, bump=False)
            if detect_content_changes:
                permit.initial_deploying_rows = _deploying_scope_rows(footprint)
            yield permit
            if permit.deferred_repend_rows:
                _repend_locked_rows(permit.deferred_repend_rows)
        finally:
            _ACTIVE_PERMIT.reset(token)


@contextlib.contextmanager
def reconcile_transaction(plan: ReconcileMutationPlan):
    """Acquire a read plan with the permit required by its canonical fragment delta."""
    if plan.changes_content:
        mutation = _intent_transaction(plan.footprint, defer_repend=True)
    else:
        mutation = mirror_transaction(
            plan.footprint,
            detect_content_changes=plan.detect_content_changes,
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
        after = ABSENT if locked is None else canonical_fragment(locked, spec)
        if after != before:
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
    _discard_rolled_back_implicit_permit()
    permit = _ACTIVE_PERMIT.get()
    return permit is not None and (int(device_id), str(scope)) in permit.footprint.revision_keys


def mirror_refresh_is_active() -> bool:
    """Return whether the current registered write preserves its canonical fragment."""
    permit = _ACTIVE_PERMIT.get()
    return permit is not None and permit.dml_kind == "mirror"


def _authorize_dml(permit: _Permit, table: str) -> None:
    permit.authorized_dml[table] = permit.authorized_dml.get(table, 0) + 1


@contextlib.contextmanager
def reconcile_cascade_dml(model):
    """Authorize one framework cascade statement inside a covered read transaction."""
    permit = _ACTIVE_PERMIT.get()
    if permit is None or permit.dml_kind != "reconcile":
        yield
        return
    label = model._meta.label_lower
    footprint_rows = (*permit.footprint.source_rows, *permit.footprint.overlay_rows)
    if not any(row.model_label == label for row in footprint_rows):
        raise IntentMutationProtocolError(f"reconcile cascade table {label!r} is outside the active footprint")
    table = model._meta.db_table
    previous = permit.authorized_dml.get(table, 0)
    _authorize_dml(permit, table)
    try:
        yield
    finally:
        if previous:
            permit.authorized_dml[table] = previous
        else:
            permit.authorized_dml.pop(table, None)


def _content_permit_covers(instance, spec: RendererInputSpec, permit: _Permit) -> bool:
    row = SourceRow(instance._meta.label_lower, instance.pk)
    future = SourceRow(instance._meta.label_lower, None)
    if row in (*permit.footprint.source_rows, *permit.footprint.overlay_rows) or future in (
        *permit.footprint.source_rows,
        *permit.footprint.overlay_rows,
    ):
        return True
    resolved = spec.resolver(instance, spec)
    return bool(resolved) and resolved <= set(permit.footprint.revision_keys)


def _footprint_covers_row(instance, permit: _Permit) -> bool:
    rows = (*permit.footprint.source_rows, *permit.footprint.overlay_rows)
    return (
        SourceRow(instance._meta.label_lower, instance.pk) in rows
        or SourceRow(instance._meta.label_lower, None) in rows
    )


def _implicit_key(instance):
    """Identify one persisted row across Collector's distinct Python objects."""
    existing = getattr(instance, "_renderer_mutation_key", None)
    if existing is not None:
        return existing
    identity = instance.pk if instance.pk is not None else id(instance)
    key = (instance._meta.label_lower, identity)
    instance._renderer_mutation_key = key
    return key


def _raw_content_values(instance, spec: RendererInputSpec):
    """Return declared content fields without the overlay ownership gate."""
    return tuple(
        (name, _normal(getattr(instance, instance._meta.get_field(name).attname)))
        for name in sorted(spec.content_fields)
    )


def _normalize_overlay_lifecycle(instance, spec, update_fields):
    """Derive an overlay edit from the current database row, never stale lifecycle state."""
    if instance._meta.label_lower not in OVERLAY_MODEL_RANKS or instance.pk is None:
        return {}
    current = type(instance).objects.filter(pk=instance.pk).first()
    if current is None:
        return {}
    changed = _raw_content_values(current, spec) != _raw_content_values(instance, spec)
    requested = None if update_fields is None else frozenset(update_fields)
    explicit_status = (requested is not None and "status" in requested) or getattr(
        instance,
        "_nso_explicit_status_update",
        False,
    )
    if instance.status != current.status and explicit_status:
        if (
            hasattr(instance, "apply_attempt_id")
            and instance.status != "deploying"
            and instance.apply_attempt_id == current.apply_attempt_id
        ):
            instance.apply_attempt_id = None
            return {} if "apply_attempt_id" in requested else {"apply_attempt_id": None}
        return {}
    from .status_machine import is_owned

    desired_status = "accepted" if changed and is_owned(current.status) else current.status
    if changed and not is_owned(current.status):
        desired_status = "changed"
    desired_attempt_id = None if changed else getattr(current, "apply_attempt_id", None)
    corrections = {"status": desired_status}
    if hasattr(instance, "apply_attempt_id"):
        corrections["apply_attempt_id"] = desired_attempt_id
    deferred = {}
    for field_name, value in corrections.items():
        setattr(instance, field_name, value)
        if requested is not None and field_name not in requested:
            deferred[field_name] = value
    return deferred


def _secondary_dml_footprint(instance, spec):
    """Cover registered rows touched by framework-maintained secondary DML."""
    if instance._meta.label_lower == "dcim.interface" and instance.device_id is not None:
        return footprint_for_instance(instance, spec)
    if instance._meta.label_lower != "dcim.device" or instance.pk is None:
        return None
    return footprint_for_instance(instance, spec)


def _benign_unrendered_insert(instance, before, after) -> bool:
    """Identify native inserts that cannot create renderer intent."""
    if not instance._state.adding or before != ABSENT or after != ABSENT:
        return False
    if instance._meta.label_lower == "ipam.ipaddress":
        return instance.assigned_object_type_id is None and instance.assigned_object_id is None
    if instance._meta.label_lower != "dcim.interface":
        return False
    name = instance.name or ""
    suffix = name.rsplit(".", 1)[-1]
    return instance.parent_id is None or "." not in name or not suffix.isdigit()


def _suppressed_permit(instance, spec, before, after, update_fields, footprint_override=None):
    declared_mirror = update_fields is not None and frozenset(update_fields) <= spec.lifecycle_fields
    if declared_mirror:
        if not transaction.get_connection().in_atomic_block:
            raise IntentMutationProtocolError("suppressed mirror writes require transaction.atomic()")
        locked = type(instance).objects.select_for_update(of=("self",)).filter(pk=instance.pk).first()
        before = ABSENT if locked is None else canonical_fragment(locked, spec)
    if not _RECONCILER_ACTIVE.get():
        if before != after:
            raise IntentMutationProtocolError(f"suppressed {instance._meta.label_lower} write changes rendered content")
        if not declared_mirror:
            raise IntentMutationProtocolError(
                f"suppressed {instance._meta.label_lower} write requires content_mutation or mirror_refresh"
            )
    if before != after:
        return _Permit(
            footprint=footprint_override or footprint_for_instance(instance, spec),
            dml_kind="content",
            implicit=True,
        )
    if not declared_mirror:
        return _Permit(
            footprint=footprint_override or footprint_for_instance(instance, spec),
            dml_kind="content",
            bump_revisions=False,
            implicit=True,
        )
    if footprint := _secondary_dml_footprint(instance, spec):
        return _Permit(
            footprint=footprint,
            dml_kind="content",
            bump_revisions=False,
            implicit=True,
        )
    return _Permit(
        footprint=MutationFootprint(),
        dml_kind="mirror",
        mirror_table=spec.table,
        mirror_pk=instance.pk,
        mirror_before=before,
        mirror_instance=instance,
        mirror_update_fields=frozenset(update_fields),
        implicit=True,
    )


def _authorize_active_write(active, sender, instance, spec, *, deleting, update_fields):
    """Validate one nested registered write against the current immutable permit."""
    if active.dml_kind == "mirror":
        requested = frozenset(update_fields or ())
        if (
            spec.table != active.mirror_table
            or instance.pk != active.mirror_pk
            or not requested
            or not requested <= (active.mirror_update_fields or frozenset())
        ):
            raise IntentMutationProtocolError("the write is outside the active mirror_refresh permit")
    elif active.dml_kind == "reconcile":
        if not _footprint_covers_row(instance, active):
            raise IntentMutationProtocolError(
                f"{sender._meta.label_lower} row {instance.pk!r} is outside the active mirror footprint"
            )
        before = canonical_fragment(instance, spec) if deleting else _database_fragment(instance, spec)
        after = ABSENT if deleting else _effective_after_fragment(instance, spec, update_fields)
        if before != after:
            if active.detect_reconcile_content and sender._meta.label_lower in OVERLAY_MODEL_RANKS:
                requested = MutationFootprint.for_keys(
                    active.footprint.revision_keys,
                    overlay_rows=(SourceRow(sender._meta.label_lower, instance.pk),),
                )
                _upgrade_detected_reconcile(active, requested)
            else:
                raise IntentMutationProtocolError(
                    f"read-side {sender._meta.label_lower} write changes rendered content"
                )
    elif not _content_permit_covers(instance, spec, active):
        before = canonical_fragment(instance, spec) if deleting else _database_fragment(instance, spec)
        after = ABSENT if deleting else _effective_after_fragment(instance, spec, update_fields)
        if before != after:
            raise IntentMutationProtocolError(
                f"{sender._meta.label_lower} row {instance.pk!r} is outside the active mutation footprint"
            )
    _authorize_dml(active, spec.table)
    if instance._meta.label_lower == "dcim.interface" and instance.device_id is not None:
        device_row = SourceRow("dcim.device", instance.device_id)
        if device_row in active.footprint.source_rows:
            _authorize_dml(active, apps.get_model("dcim.device")._meta.db_table)


def _begin_implicit(
    sender,
    instance,
    *,
    deleting=False,
    update_fields=None,
    footprint_override=None,
    **kwargs,
):
    _discard_rolled_back_implicit_permit()
    spec = _REGISTRY.get(sender._meta.label_lower)
    if spec is None:
        return
    active = _ACTIVE_PERMIT.get()
    if active is not None:
        _authorize_active_write(
            active,
            sender,
            instance,
            spec,
            deleting=deleting,
            update_fields=update_fields,
        )
        return
    from .signals import _is_intent_push_suppressed

    deferred = {}
    if not deleting and not _is_intent_push_suppressed():
        deferred = _normalize_overlay_lifecycle(instance, spec, update_fields)
    before = canonical_fragment(instance, spec) if deleting else _database_fragment(instance, spec)
    after = ABSENT if deleting else _effective_after_fragment(instance, spec, update_fields)
    if _is_intent_push_suppressed():
        permit = _suppressed_permit(
            instance,
            spec,
            before,
            after,
            update_fields,
            footprint_override,
        )
    else:
        proposed_footprint = footprint_override or footprint_for_instance(instance, spec)
        benign_insert = _benign_unrendered_insert(instance, before, after)
    if (
        not _is_intent_push_suppressed()
        and before == after
        and not (instance._state.adding and proposed_footprint.overlay_rows and not benign_insert)
    ):
        benign_footprint = MutationFootprint() if benign_insert else proposed_footprint
        secondary_footprint = (
            None if benign_insert or footprint_override is not None else _secondary_dml_footprint(instance, spec)
        )
        if secondary_footprint is not None or benign_footprint.shared_keys or benign_footprint.overlay_rows:
            permit = _Permit(
                footprint=secondary_footprint or benign_footprint,
                dml_kind="content",
                bump_revisions=False,
                join_deployment_gate=not (
                    before == ABSENT
                    and after == ABSENT
                    and bool(benign_footprint.shared_keys)
                    and not benign_footprint.revision_keys
                    and not benign_footprint.overlay_rows
                ),
                implicit=True,
            )
        else:
            permit = _Permit(
                footprint=MutationFootprint(),
                dml_kind="mirror",
                mirror_table=spec.table,
                mirror_pk=instance.pk,
                mirror_before=before,
                mirror_instance=instance,
                mirror_update_fields=frozenset(update_fields or (spec.content_fields | spec.lifecycle_fields)),
                implicit=True,
            )
    elif not _is_intent_push_suppressed():
        permit = _Permit(footprint=proposed_footprint, dml_kind="content", implicit=True)
    token = _ACTIVE_PERMIT.set(permit)
    if transaction.get_connection().atomic_blocks:
        permit.atomic_block_id = id(transaction.get_connection().atomic_blocks[-1])
    permit.tokens.append(token)
    permit.deferred_update.update(deferred)
    if permit.mirror_update_fields is not None:
        permit.mirror_update_fields = permit.mirror_update_fields | frozenset(deferred)
    if permit.dml_kind == "content":
        try:
            if (
                permit.footprint.shared_keys
                or permit.footprint.revision_keys
                or permit.footprint.source_rows
                or permit.footprint.overlay_rows
            ):
                _acquire(
                    permit.footprint,
                    bump=permit.bump_revisions,
                    join_deployment_gate=permit.join_deployment_gate,
                )
        except Exception:
            _ACTIVE_PERMIT.reset(token)
            raise
    _authorize_dml(permit, spec.table)
    permits = dict(_IMPLICIT_PERMITS.get())
    permits[_implicit_key(instance)] = token
    _IMPLICIT_PERMITS.set(permits)


def _begin_delete_implicit(sender, instance, origin=None, **kwargs):
    """Keep a cascade permit alive until the registered root's post-delete."""
    origin_label = getattr(getattr(origin, "_meta", None), "label_lower", None)
    if origin_label in _REGISTRY:
        target = origin
        footprint = deletion_footprint_for_instance(target) if _ACTIVE_PERMIT.get() is None else None
    else:
        origin_model = getattr(origin, "model", None)
        origin_label = getattr(getattr(origin_model, "_meta", None), "label_lower", None)
        roots = list(origin.order_by("pk")) if origin_label in _REGISTRY and _ACTIVE_PERMIT.get() is None else []
        target = roots[0] if roots else instance
        footprint = (
            MutationFootprint.merge(*(deletion_footprint_for_instance(root) for root in roots)) if roots else None
        )
    if footprint is None and _ACTIVE_PERMIT.get() is None:
        footprint = deletion_footprint_for_instance(target)
    _begin_implicit(
        type(target),
        target,
        deleting=True,
        origin=origin,
        footprint_override=footprint,
        **kwargs,
    )


def _end_implicit(sender, instance, **kwargs):
    permits = dict(_IMPLICIT_PERMITS.get())
    token = permits.pop(_implicit_key(instance), None)
    instance.__dict__.pop("_renderer_mutation_key", None)
    _IMPLICIT_PERMITS.set(permits)
    if token is not None:
        permit = _ACTIVE_PERMIT.get()
        try:
            if permit is not None and permit.deferred_update:
                from .signals import suppress_intent_push

                fields = set(permit.deferred_update)
                for field_name, value in permit.deferred_update.items():
                    setattr(instance, field_name, value)
                permit.deferred_update.clear()
                with suppress_intent_push():
                    instance.save(update_fields=fields)
        finally:
            _ACTIVE_PERMIT.reset(token)


def _static_route_devices_footprint(instance, action, pk_set, reverse):
    """Resolve the exact device assignments before the static-route M2M write."""
    StaticRoute = apps.get_model("netbox_routing.staticroute")
    StaticRouteState = apps.get_model("netbox_nso_plugin.nsostaticroutestate")
    if reverse:
        device_ids = {instance.pk}
        route_ids = set(pk_set or ())
        if action == "pre_clear":
            route_ids = set(StaticRoute.objects.filter(devices=instance).values_list("pk", flat=True))
    else:
        route_ids = {instance.pk}
        device_ids = set(pk_set or ())
        if action == "pre_clear":
            device_ids = set(instance.devices.values_list("pk", flat=True))
    keys = _management_keys(device_ids, ("static_route",))
    overlays = StaticRouteState.objects.filter(
        management__device_id__in=device_ids,
        static_route_id__in=route_ids,
    )
    return MutationFootprint.for_keys(
        keys,
        source_rows=(
            SourceRow("netbox_routing.staticroute_devices", None),
            *(SourceRow("netbox_routing.staticroute", route_id) for route_id in route_ids),
        ),
        overlay_rows=(
            SourceRow("netbox_nso_plugin.nsostaticroutestate", None),
            *(SourceRow("netbox_nso_plugin.nsostaticroutestate", pk) for pk in overlays.values_list("pk", flat=True)),
        ),
    )


def _begin_m2m_implicit(sender, instance, action, **kwargs):
    if not action.startswith("pre_"):
        return
    _discard_rolled_back_implicit_permit()
    label = sender._meta.label_lower
    static_route_assignment = label == "netbox_routing.staticroute_devices"
    spec = None if static_route_assignment else _REGISTRY[label]
    token_key = (id(instance), label)
    active = _ACTIVE_PERMIT.get()
    if static_route_assignment:
        footprint = _static_route_devices_footprint(
            instance,
            action,
            kwargs.get("pk_set"),
            kwargs.get("reverse", False),
        )
        keys = set(footprint.revision_keys)
    else:
        owner_spec = _REGISTRY.get(instance._meta.label_lower)
        keys = set() if owner_spec is None else owner_spec.resolver(instance, owner_spec)
        keys = {(device_id, scope) for device_id, scope in keys if scope in spec.scopes}
        row = SourceRow(instance._meta.label_lower, instance.pk)
        row_fields = (
            {"overlay_rows": (row,)} if instance._meta.label_lower in OVERLAY_MODEL_RANKS else {"source_rows": (row,)}
        )
        footprint = MutationFootprint.for_keys(keys, **row_fields)
    if active is not None:
        if not active.footprint.covers(footprint):
            raise IntentMutationProtocolError("the M2M write is outside the active mutation footprint")
        if spec is not None:
            _authorize_dml(active, spec.table)
        return
    from .signals import _is_intent_push_suppressed

    if _is_intent_push_suppressed() and keys:
        raise IntentMutationProtocolError("a suppressed renderer M2M write requires content_mutation")
    permit = _Permit(footprint=footprint, dml_kind="content", implicit=True)
    token = _ACTIVE_PERMIT.set(permit)
    if transaction.get_connection().atomic_blocks:
        permit.atomic_block_id = id(transaction.get_connection().atomic_blocks[-1])
    permit.tokens.append(token)
    try:
        if keys:
            _acquire(footprint)
    except Exception:
        _ACTIVE_PERMIT.reset(token)
        raise
    if spec is not None:
        _authorize_dml(permit, spec.table)
    permits = dict(_IMPLICIT_PERMITS.get())
    permits[token_key] = token
    _IMPLICIT_PERMITS.set(permits)


def _close_m2m_implicit(sender, instance):
    """Close the implicit permit for one M2M mutation."""
    token_key = (id(instance), sender._meta.label_lower)
    permits = dict(_IMPLICIT_PERMITS.get())
    token = permits.pop(token_key, None)
    _IMPLICIT_PERMITS.set(permits)
    if token is not None:
        _ACTIVE_PERMIT.reset(token)


def _abort_m2m_implicit(sender, instance):
    """Close an M2M permit when a pre-action behavior handler fails."""
    _close_m2m_implicit(sender, instance)


def _end_m2m_implicit(sender, instance, action, **kwargs):
    if action.startswith("post_"):
        _close_m2m_implicit(sender, instance)


def _pool_delete_scope(connection):
    """Identify the connection and the still-open atomic block a pool delete runs in."""
    blocks = getattr(connection, "atomic_blocks", None)
    if not blocks:
        return None
    return (connection.alias, blocks[-1])


def _begin_prefix_delete(sender, instance, using=None, **kwargs):
    """Mark the pool whose delete makes Django clear the overlay's audit-trail FK."""
    scope = _pool_delete_scope(transaction.get_connection(using))
    if instance.pk is None or scope is None:
        return
    _DELETING_POOLS.set(_DELETING_POOLS.get() | {(*scope, instance.pk)})


def _end_prefix_delete(sender, instance, using=None, **kwargs):
    """Drop the marker once the pool row is gone."""
    _DELETING_POOLS.set(
        frozenset(entry for entry in _DELETING_POOLS.get() if entry[2] != instance.pk or entry[0] != using)
    )


def _live_pool_markers(connection) -> frozenset[tuple]:
    """Purge markers whose delete transaction is over: a rollback leaves the block popped."""
    entries = _DELETING_POOLS.get()
    blocks = getattr(connection, "atomic_blocks", ())
    live = frozenset(
        entry for entry in entries if entry[0] != connection.alias or any(entry[1] is block for block in blocks)
    )
    if live != entries:
        _DELETING_POOLS.set(live)
    return live


def _is_pool_delete_cascade(statement, params, connection) -> bool:
    """Report whether this is the collector's SET_NULL update for a pool being deleted."""
    entries = _live_pool_markers(connection)
    match = _SOURCE_POOL_CASCADE.search(statement)
    if not entries or match is None:
        return False
    values = list(params or ())
    if match.group("value").upper() != "NULL":
        # The bound new value must be the NULL the collector writes.
        if not values or values[0] is not None:
            return False
        values = values[1:]
    try:
        targets = {int(value) for value in values}
    except (TypeError, ValueError):
        return False
    # The marker stays until post_delete, so a same-shape peer update cannot starve the collector.
    marked = {entry[2] for entry in entries if entry[0] == connection.alias}
    return bool(targets) and targets <= marked


def _execute_with_permit_cleanup(execute, sql, params, many, context, permit):
    """Execute SQL and retire an implicit permit when the database rejects it."""
    try:
        return execute(sql, params, many, context)
    except Exception:
        _clear_failed_implicit_permit(permit)
        raise


def _dml_guard(execute, sql, params, many, context):
    statement = str(sql)
    if _MIGRATIONS_ACTIVE.get():
        return execute(sql, params, many, context)
    first_keyword = _FIRST_SQL_KEYWORD.match(statement)
    if first_keyword is not None and first_keyword.group(1).upper() in _DML_PARSE_SKIP_KEYWORDS:
        return execute(sql, params, many, context)
    target, unparseable = _parse_dml_target(statement)
    if target is None:
        mentioned = _mentioned_registered_tables(statement)
        if unparseable and mentioned:
            raise IntentMutationProtocolError(f"unparseable SQL mentions renderer input tables {sorted(mentioned)!r}")
        return execute(sql, params, many, context)
    if target.table not in _TABLE_REGISTRY:
        return execute(sql, params, many, context)
    touched_columns = _dml_columns(statement, target.operation)
    permit = _ACTIVE_PERMIT.get()
    if target.operation == "INSERT INTO" and touched_columns == frozenset():
        if permit is None:
            # drift signal: this creation skips the pre_save bookkeeping (revision bump, re-pend)
            logger.warning("unpermitted creation on renderer input %s proceeded without bookkeeping", target.table)
        return _execute_with_permit_cleanup(execute, sql, params, many, context, permit)
    table = target.table
    spec = _TABLE_REGISTRY[table]
    guarded_fields = spec.content_fields | _FRAGMENT_GATE_FIELDS.get(spec.model_label, set())
    content_columns = {spec.model._meta.get_field(field_name).column for field_name in guarded_fields}
    if touched_columns and touched_columns.isdisjoint(content_columns):
        return execute(sql, params, many, context)
    if permit is not None and permit.dml_kind == "offline":
        return execute(sql, params, many, context)
    remaining = 0 if permit is None else permit.authorized_dml.get(table, 0)
    footprint_tables = set()
    if permit is not None and permit.dml_kind == "content":
        footprint_tables = {
            apps.get_model(row.model_label)._meta.db_table
            for row in (*permit.footprint.source_rows, *permit.footprint.overlay_rows)
        }
        if permit.footprint.device_ids:
            footprint_tables.add(apps.get_model("netbox_nso_plugin.nsodevicemanagement")._meta.db_table)
    if remaining < 1 and table not in footprint_tables:
        column_detail = "unknown" if touched_columns is None else sorted(touched_columns)
        _clear_failed_implicit_permit(permit)
        raise IntentMutationProtocolError(
            f"bulk/raw DML on renderer input {table} requires an exact content_mutation permit; "
            f"active={getattr(permit, 'dml_kind', None)!r}, columns={column_detail!r}, "
            f"tables={sorted(footprint_tables)!r}"
        )
    if remaining:
        permit.authorized_dml[table] = remaining - 1
    return _execute_with_permit_cleanup(execute, sql, params, many, context, permit)


def _clear_failed_implicit_permit(permit) -> None:
    """Retire an implicit permit when its guarded SQL cannot complete."""
    if permit is None or not permit.implicit or not permit.tokens:
        return
    token = permit.tokens.pop()
    implicit_permits = {key: candidate for key, candidate in _IMPLICIT_PERMITS.get().items() if candidate is not token}
    _IMPLICIT_PERMITS.set(implicit_permits)
    _ACTIVE_PERMIT.reset(token)


@functools.lru_cache(maxsize=512)
def _parse_dml_target(statement: str) -> tuple[_DMLTarget | None, bool]:
    """Return one parsed mutation target and whether classification failed."""
    parsed = sqlparse.parse(statement)
    if len(parsed) != 1:
        classified = [_parse_dml_target(str(candidate)) for candidate in parsed]
        return None, any(target is not None or unparseable for target, unparseable in classified)
    parsed_statement = parsed[0]
    statement_type = parsed_statement.get_type().upper()
    operations = {
        "UPDATE": ("UPDATE", "UPDATE"),
        "INSERT": ("INSERT INTO", "INTO"),
        "DELETE": ("DELETE FROM", "FROM"),
    }
    operation = operations.get(statement_type)
    if operation is None:
        flattened = tuple(
            token for token in parsed_statement.flatten() if not token.is_whitespace and token.ttype not in Comment
        )
        mutation_tokens = tuple(
            (index, token)
            for index, token in enumerate(flattened)
            if token.ttype in sqlparse.tokens.DML and token.normalized in {"UPDATE", "INSERT", "DELETE", "MERGE"}
        )
        if statement_type == "SELECT":
            lock_prefixes = (("FOR",), ("FOR", "NO", "KEY"))

            def is_locking_update(index, token) -> bool:
                if token.normalized != "UPDATE":
                    return False
                return any(
                    tuple(candidate.normalized for candidate in flattened[index - len(prefix) : index]) == prefix
                    for prefix in lock_prefixes
                    if index >= len(prefix)
                )

            return None, any(not is_locking_update(index, token) for index, token in mutation_tokens)
        return None, bool(mutation_tokens)
    operation_name, anchor = operation
    tokens = _sql_tokens(parsed_statement)
    anchor_index = next(
        (
            index
            for index, token in enumerate(tokens)
            if (anchor == "UPDATE" and token.ttype in sqlparse.tokens.DML and token.normalized == anchor)
            or (anchor != "UPDATE" and token.ttype in Keyword and token.normalized == anchor)
        ),
        None,
    )
    if anchor_index is None:
        return None, True
    target_token = next(
        (token for token in tokens[anchor_index + 1 :] if not (token.ttype in Keyword and token.normalized == "ONLY")),
        None,
    )
    if not isinstance(target_token, Identifier) or not target_token.get_real_name():
        return None, True
    return _DMLTarget(operation_name, _postgres_identifier_name(target_token)), False


def _postgres_identifier_name(identifier: Identifier) -> str:
    """Return one identifier with PostgreSQL's unquoted case folding applied."""
    real_name = identifier.get_real_name()
    for token in identifier.tokens:
        if token.ttype == Literal.String.Symbol:
            quoted = token.value[1:-1].replace('""', '"')
            if quoted == real_name:
                return quoted
    return real_name.lower()


def _mentioned_registered_tables(statement: str) -> set[str]:
    """Return registered table names present in otherwise unclassified SQL."""
    return {
        table
        for table in _TABLE_REGISTRY
        if re.search(rf'(?<![A-Za-z0-9_])"?{re.escape(table)}"?(?![A-Za-z0-9_])', statement, re.IGNORECASE)
    }


@functools.lru_cache(maxsize=512)
def _dml_columns(statement: str, operation: str) -> frozenset[str] | None:
    """Return columns that can mutate existing rows, or None when they are unknown."""
    if operation == "DELETE FROM":
        return None
    parsed = sqlparse.parse(statement)
    if len(parsed) != 1:
        return None
    tokens = _sql_tokens(parsed[0])
    if operation == "UPDATE":
        set_index = next(
            (index for index, token in enumerate(tokens) if token.ttype in Keyword and token.normalized == "SET"),
            None,
        )
        if set_index is None or set_index + 1 >= len(tokens):
            return None
        assignments = tokens[set_index + 1]
        if isinstance(assignments, Comparison):
            comparisons = (assignments,)
        elif isinstance(assignments, IdentifierList):
            comparisons = tuple(assignments.get_identifiers())
        else:
            return None
        columns = tuple(_comparison_column(comparison) for comparison in comparisons)
        return None if not columns or any(column is None for column in columns) else frozenset(columns)
    if operation == "INSERT INTO":
        return _insert_update_columns(parsed[0])
    return None


def _insert_update_columns(statement) -> frozenset[str] | None:
    """Return ON CONFLICT DO UPDATE columns, with an empty set for creation."""
    tokens = tuple(token for token in statement.flatten() if not token.is_whitespace and token.ttype not in Comment)
    conflict_index = next(
        (
            index
            for index in range(len(tokens) - 1)
            if tokens[index].normalized == "ON" and tokens[index + 1].normalized == "CONFLICT"
        ),
        None,
    )
    if conflict_index is None:
        return frozenset()
    do_index = next(
        (index for index in range(conflict_index + 2, len(tokens)) if tokens[index].normalized == "DO"),
        None,
    )
    if do_index is None or do_index + 1 >= len(tokens):
        return None
    if tokens[do_index + 1].normalized == "NOTHING":
        return frozenset()
    if (
        tokens[do_index + 1].normalized != "UPDATE"
        or do_index + 2 >= len(tokens)
        or tokens[do_index + 2].normalized != "SET"
    ):
        return None
    return _flattened_assignment_columns(tokens[do_index + 3 :])


def _flattened_assignment_columns(tokens) -> frozenset[str] | None:
    """Return columns from a flattened PostgreSQL assignment list."""
    columns = []
    index = 0
    while index < len(tokens):
        column = _postgres_column_token_name(tokens[index])
        if column is None or index + 1 >= len(tokens) or tokens[index + 1].value != "=":
            return None
        columns.append(column)
        index += 2
        expression_started = False
        depth = 0
        while index < len(tokens):
            token = tokens[index]
            if depth == 0 and token.ttype in Keyword and token.normalized in {"WHERE", "RETURNING"}:
                return frozenset(columns) if expression_started else None
            if depth == 0 and token.value == ",":
                if not expression_started:
                    return None
                index += 1
                break
            if token.value in {"(", "[", "{"}:
                depth += 1
            elif token.value in {")", "]", "}"}:
                depth -= 1
                if depth < 0:
                    return None
            expression_started = True
            index += 1
        else:
            return frozenset(columns) if expression_started and depth == 0 else None
    return None


def _postgres_column_token_name(token) -> str | None:
    """Return one PostgreSQL assignment column with case folding applied."""
    if token.ttype == Literal.String.Symbol:
        return token.value[1:-1].replace('""', '"')
    if token.ttype in sqlparse.tokens.Name:
        return token.value.lower()
    return None


def _comparison_column(comparison) -> str | None:
    """Return the column assigned by one parsed UPDATE comparison."""
    if not isinstance(comparison, Comparison):
        return None
    tokens = _sql_tokens(comparison)
    if len(tokens) < 3 or tokens[1].value != "=" or not isinstance(tokens[0], Identifier):
        return None
    return _postgres_identifier_name(tokens[0])


def _sql_tokens(group) -> tuple:
    """Return significant direct children from one parsed SQL token group."""
    return tuple(token for token in group.tokens if not token.is_whitespace and token.ttype not in Comment)


def _install_guard(connection, **kwargs):
    if _dml_guard not in connection.execute_wrappers:
        connection.execute_wrappers.append(_dml_guard)


def _start_migrations(**kwargs):
    _MIGRATIONS_ACTIVE.set(True)


def _finish_migrations(**kwargs):
    _MIGRATIONS_ACTIVE.set(False)


def ensure_delete_signal_origin() -> None:
    """Make a model deletion identify its root to every cascade signal."""
    from netbox.models.deletion import CustomCollector

    collect = CustomCollector.collect
    if getattr(collect, "_nso_preserves_delete_origin", False):
        return

    @functools.wraps(collect)
    def collect_with_origin(self, objs, *args, **kwargs):
        source = kwargs.get("source", args[0] if args else None)
        if self.origin is None and source is None:
            roots = tuple(objs)
            if len(roots) == 1:
                self.origin = roots[0]
            objs = roots
        return collect(self, objs, *args, **kwargs)

    collect_with_origin._nso_preserves_delete_origin = True
    CustomCollector.collect = collect_with_origin


def register_renderer_input(spec: RendererInputSpec, *, connect_ends: bool = True) -> None:
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
    )
    _REGISTRY[label] = normalized
    _TABLE_REGISTRY[model._meta.db_table] = normalized
    uid = f"nso_intent_guard_{label}"
    pre_save.connect(_begin_implicit, sender=model, dispatch_uid=f"{uid}_pre_save", weak=False)
    pre_delete.connect(_begin_delete_implicit, sender=model, dispatch_uid=f"{uid}_pre_delete", weak=False)
    if connect_ends:
        _connect_renderer_input_ends(model)
    if model._meta.auto_created:
        m2m_changed.connect(_begin_m2m_implicit, sender=model, dispatch_uid=f"{uid}_m2m_begin", weak=False)


def _connect_renderer_input_ends(model) -> None:
    uid = f"nso_intent_guard_{model._meta.label_lower}"
    post_save.connect(_end_implicit, sender=model, dispatch_uid=f"{uid}_post_save", weak=False)
    post_delete.connect(_end_implicit, sender=model, dispatch_uid=f"{uid}_post_delete", weak=False)
    if model._meta.auto_created:
        m2m_changed.connect(_end_m2m_implicit, sender=model, dispatch_uid=f"{uid}_m2m_end", weak=False)


def connect_renderer_input_end_handlers() -> None:
    """Close implicit permits after every renderer behavior handler has run."""
    for label in _REGISTRY:
        _connect_renderer_input_ends(apps.get_model(label))
    StaticRoute = apps.get_model("netbox_routing.staticroute")
    m2m_changed.connect(
        _end_m2m_implicit,
        sender=StaticRoute.devices.through,
        dispatch_uid="nso_intent_guard_static_route_devices_m2m_end",
        weak=False,
    )


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
    connect_ends=True,
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
        connect_ends=connect_ends,
    )


def _register_auto_through(model, scopes, *, shared_kind=None, field_names=None, connect_ends=True):
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
                connect_ends=connect_ends,
            )


def register_builtin_renderer_inputs(*, connect_ends: bool = True) -> None:
    """Install the declared renderer-input registry and every runtime sentinel."""
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
            connect_ends=connect_ends,
        )
    _register_auto_through(
        apps.get_model("netbox_nso_plugin.nsoswitchportstate"),
        ("switchport",),
        field_names={"tagged_vlans"},
        connect_ends=connect_ends,
    )
    vlan_model = apps.get_model("ipam.vlan")
    _register(
        "ipam.vlan",
        ("vlan", "svi", "switchport"),
        shared_kind="vlan",
        model=vlan_model,
        content_fields={"vid", "name"},
        connect_ends=connect_ends,
    )
    _REGISTRY["netbox_nso_plugin.nsovlanstate"] = replace(
        _REGISTRY["netbox_nso_plugin.nsovlanstate"],
        fragment=_vlan_state_fragment,
    )
    _TABLE_REGISTRY[_REGISTRY["netbox_nso_plugin.nsovlanstate"].table] = _REGISTRY["netbox_nso_plugin.nsovlanstate"]
    _REGISTRY["ipam.vlan"] = replace(
        _REGISTRY["ipam.vlan"],
        fragment=_native_vlan_fragment,
    )
    _TABLE_REGISTRY[_REGISTRY["ipam.vlan"].table] = _REGISTRY["ipam.vlan"]
    _REGISTRY["netbox_nso_plugin.nsolacpbundlestate"] = replace(
        _REGISTRY["netbox_nso_plugin.nsolacpbundlestate"],
        fragment=_lacp_bundle_fragment,
    )
    _TABLE_REGISTRY[_REGISTRY["netbox_nso_plugin.nsolacpbundlestate"].table] = _REGISTRY[
        "netbox_nso_plugin.nsolacpbundlestate"
    ]
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
    for label, fragment in exact_direct_fragments.items():
        _REGISTRY[label] = replace(_REGISTRY[label], fragment=fragment)
        _TABLE_REGISTRY[_REGISTRY[label].table] = _REGISTRY[label]
    _REGISTRY["netbox_nso_plugin.nsointerfacestate"] = replace(
        _REGISTRY["netbox_nso_plugin.nsointerfacestate"],
        fragment=_interface_state_fragment,
    )
    _TABLE_REGISTRY[_REGISTRY["netbox_nso_plugin.nsointerfacestate"].table] = _REGISTRY[
        "netbox_nso_plugin.nsointerfacestate"
    ]
    snmp_fragments = {
        "netbox_nso_plugin.nsosnmpcommunitystate": _snmp_community_fragment,
        "netbox_nso_plugin.nsosnmpv3userstate": _snmp_v3_user_fragment,
        "netbox_nso_plugin.nsosnmphoststate": _snmp_host_fragment,
    }
    for label, fragment in snmp_fragments.items():
        _REGISTRY[label] = replace(_REGISTRY[label], fragment=fragment)
        _TABLE_REGISTRY[_REGISTRY[label].table] = _REGISTRY[label]

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
        _register(label, scopes, model=model, content_fields=content_fields, connect_ends=connect_ends)

    if "netbox_routing.staticroute" in _REGISTRY:
        _REGISTRY["netbox_routing.staticroute"] = replace(
            _REGISTRY["netbox_routing.staticroute"],
            fragment=_static_route_fragment,
        )
        _TABLE_REGISTRY[_REGISTRY["netbox_routing.staticroute"].table] = _REGISTRY["netbox_routing.staticroute"]
        StaticRoute = apps.get_model("netbox_routing.staticroute")
        m2m_changed.connect(
            _begin_m2m_implicit,
            sender=StaticRoute.devices.through,
            dispatch_uid="nso_intent_guard_static_route_devices_m2m_begin",
            weak=False,
        )
        if connect_ends:
            m2m_changed.connect(
                _end_m2m_implicit,
                sender=StaticRoute.devices.through,
                dispatch_uid="nso_intent_guard_static_route_devices_m2m_end",
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
            connect_ends=connect_ends,
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
            connect_ends=connect_ends,
        )

    for connection in connections.all():
        _install_guard(connection)
    connection_created.connect(_install_guard, dispatch_uid="nso_intent_dml_guard", weak=False)
    prefix_model = apps.get_model("ipam.prefix")
    pre_delete.connect(
        _begin_prefix_delete, sender=prefix_model, dispatch_uid="nso_intent_prefix_pre_delete", weak=False
    )
    post_delete.connect(
        _end_prefix_delete, sender=prefix_model, dispatch_uid="nso_intent_prefix_post_delete", weak=False
    )
    pre_migrate.connect(_start_migrations, dispatch_uid="nso_intent_pre_migrate", weak=False)
    post_migrate.connect(_finish_migrations, dispatch_uid="nso_intent_post_migrate", weak=False)
