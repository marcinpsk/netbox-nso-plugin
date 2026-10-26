# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Native-write authority for plugin requests.

Each plugin request runs under one authority mode. A read or adapter-action endpoint may not
write native NetBox rows. An operator endpoint writes them only through the NativeWriter: it
checks the user's NetBox permission on the stored row before each write and on every completed
row before commit, and the adapter transport refuses calls until the scope commits. An
allowlisted system entry writes device facts: each native effect names its value source, and
owned intent never reaches a native row through it. Outside a request no mode applies.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import inspect
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from django.db import connections, transaction

_GUARDED_MODES = frozenset({"operator", "system"})
_DML = re.compile(r'\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"?([A-Za-z0-9_]+)"?', re.IGNORECASE)


class NativeWriteRefused(RuntimeError):
    """A native write ran outside the authority that permits it."""


class OwnedIntentRefused(NativeWriteRefused):
    """The system scope refused a native effect that carries owned intent."""


class SharedOperationConflict(RuntimeError):
    """The affected management set changed before the mutation acquired its locks."""

    def __init__(self, public_message):
        super().__init__(public_message)
        self.public_message = public_message


def permissions_violation(message):
    """Return a PermissionsViolation whose ``message`` is the authored operator text."""
    from utilities.exceptions import PermissionsViolation

    exc = PermissionsViolation(message)
    exc.message = message
    return exc


# ── Value sources of a system-scope native effect ──


@dataclass(frozen=True)
class Observed:
    """The values come from the device observation of *scope*."""

    scope: str


@dataclass(frozen=True)
class OwnedIntent:
    """The values come from the overlay row *overlay_pk* of *scope*."""

    scope: str
    overlay_pk: Any


# ── Owned-intent map: the native effects that carry each ownership scope's intent ──


@dataclass(frozen=True)
class Anchored:
    """A native row 1:1 with the scope's anchor: while the scope is owned, its mapped effects are intent."""

    native_model: str
    fields: frozenset[str] | None  # None: every field of the row
    lifecycle: bool  # creating or deleting the row is an intent effect
    owned: Callable[[Any], bool] = field(compare=False)


@dataclass(frozen=True)
class Dependency:
    """A shared native row that other scopes also create from observations: only the origin rule applies."""

    native_model: str


@dataclass(frozen=True)
class NoNativeIntentEffects:
    """An explicit declaration that the system scope writes no native row carrying this scope's intent."""

    reason: str


def _owned_overlay(model_name, **filters):
    from django.apps import apps

    from .status_machine import OWNED_STATES

    model = apps.get_model("netbox_nso_plugin", model_name)
    return model.objects.filter(status__in=OWNED_STATES, **filters).exists()


def _owned_manifest(scope, native_model_label, **filters):
    from .models import NSOOwnershipManifest

    if scope is not None:
        filters["scope"] = scope
    return NSOOwnershipManifest.objects.filter(
        native_model_label=native_model_label, ownership_state="owned", **filters
    ).exists()


def _interface_scope_owned(model_name, scope):
    def owned(interface):
        return interface.pk is not None and (
            _owned_overlay(model_name, interface_id=interface.pk)
            or _owned_manifest(scope, "dcim.interface", native_id=interface.pk)
        )

    return owned


def interface_attribute_owned(interface, attribute):
    """Return whether the overlay or manifest owns this interface attribute."""
    return interface.pk is not None and (
        _owned_overlay("nsointerfacestate", interface_id=interface.pk, attribute=attribute)
        or _owned_manifest("interface", "dcim.interface", native_id=interface.pk, state_key__attribute=attribute)
    )


def _bfd_interface_owned(row):
    return _owned_overlay("nsobfdinterfacestate", interface_id=row.interface_id) or _owned_manifest(
        "bfd", "dcim.interface", native_id=row.interface_id
    )


def _isis_interface_owned(row):
    return _owned_overlay("nsoisisinterfacestate", interface_id=row.interface_id, af=row.address_family) or (
        row.pk is not None and _owned_manifest("isis", "netbox_routing.isisinterface", native_id=row.pk)
    )


def _redistribution_owned(row):
    from .models import NSORedistributionState
    from .ownership_planner import _json_value
    from .redistribution_reconciler import _resolve_redist_destination
    from .status_machine import OWNED_STATES

    if row.pk is not None and _owned_overlay("nsoredistributionstate", redistribution_id=row.pk):
        return True
    key = {
        name: _json_value(getattr(row, name))
        for name in ("destination_type_id", "destination_id", "source_protocol", "source_ref")
    }
    # The manifest scope is the destination protocol, so the native key alone binds the row.
    if _owned_manifest(None, "netbox_routing.redistribution", native_key=key):
        return True
    # An owned overlay whose native row is missing names its destination by protocol and reference.
    candidates = NSORedistributionState.objects.filter(
        redistribution__isnull=True,
        source_protocol=row.source_protocol,
        source_ref=row.source_ref or "",
        status__in=OWNED_STATES,
    ).select_related("management__device")
    for state in candidates:
        destination = _resolve_redist_destination(state.management.device, state.dest_protocol, state.dest_ref)
        if (
            destination is not None
            and destination.pk == row.destination_id
            and _content_type_id(destination) == row.destination_type_id
        ):
            return True
    return False


def _content_type_id(instance):
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(type(instance)).pk


_CENSUS_R3 = "round-3 reconciler census: the system scope skips owned rows of this scope or writes only observed values"
_OVERLAY_ONLY = "overlay-only scope: no native NetBox row carries this intent"

OWNED_NATIVE_EFFECTS: Mapping[str, tuple[Anchored | Dependency, ...] | NoNativeIntentEffects] = MappingProxyType(
    {
        "netbox_nso_plugin.nsobfdinterfacestate": (
            Anchored("netbox_routing.bfdinterface", None, lifecycle=True, owned=_bfd_interface_owned),
            Dependency("netbox_routing.bfdprofile"),
        ),
        "netbox_nso_plugin.nsoisisinterfacestate": (
            Anchored(
                "netbox_routing.isisinterface", frozenset({"instance"}), lifecycle=False, owned=_isis_interface_owned
            ),
            Dependency("netbox_routing.isisinstance"),
        ),
        "netbox_nso_plugin.nsoredistributionstate": (
            Anchored(
                "netbox_routing.redistribution",
                frozenset({"route_map", "metric", "metric_type"}),
                lifecycle=True,
                owned=_redistribution_owned,
            ),
        ),
        "netbox_nso_plugin.nsoswitchportstate": (
            Anchored(
                "dcim.interface",
                frozenset({"mode", "untagged_vlan", "tagged_vlans"}),
                lifecycle=False,
                owned=_interface_scope_owned("nsoswitchportstate", "switchport"),
            ),
        ),
        "netbox_nso_plugin.nsointerfacemtustate": (
            Anchored(
                "dcim.interface",
                frozenset({"mtu"}),
                lifecycle=False,
                owned=_interface_scope_owned("nsointerfacemtustate", "interface_mtu"),
            ),
        ),
        "netbox_nso_plugin.nsointerfacestate": (
            Anchored(
                "dcim.interface",
                frozenset({"description"}),
                lifecycle=False,
                owned=functools.partial(interface_attribute_owned, attribute="description"),
            ),
            Anchored(
                "dcim.interface",
                frozenset({"enabled"}),
                lifecycle=False,
                owned=functools.partial(interface_attribute_owned, attribute="enabled"),
            ),
        ),
        **{
            f"netbox_nso_plugin.{name}": NoNativeIntentEffects(_OVERLAY_ONLY)
            for name in (
                "nsosnmpcommunitystate",
                "nsosnmpv3userstate",
                "nsosnmphoststate",
                "nsosnmpsysteminfostate",
                "nsologginghoststate",
                "nsologginglevelstate",
            )
        },
        **{
            f"netbox_nso_plugin.{name}": NoNativeIntentEffects(_CENSUS_R3)
            for name in (
                "nsointerfaceipstate",
                "nsosvistate",
                "nsosubinterfacestate",
                "nsovlanstate",
                "nsostaticroutestate",
                "nsoisisflexalgostate",
                "nsol2sapstate",
                "nsoisisinstancestate",
                "nsobgprouterstate",
                "nsobgppeerstate",
                "nsobgppeertemplatestate",
                "nsoroutepolicystate",
                "nsoospfinstancestate",
                "nsoospfinterfacestate",
                "nsolacpbundlestate",
                "nsolacpmemberstate",
            )
        },
    }
)


def missing_owned_effect_declarations(labels) -> list[str]:
    """Return the overlay model labels without an owned-intent entry or declaration."""
    return sorted(label for label in labels if label not in OWNED_NATIVE_EFFECTS)


@functools.cache
def _anchored_entries(native_model_label):
    return tuple(
        (scope, entry)
        for scope, entries in OWNED_NATIVE_EFFECTS.items()
        if isinstance(entries, tuple)
        for entry in entries
        if isinstance(entry, Anchored) and entry.native_model == native_model_label
    )


# ── Native tables ──


@functools.cache
def native_tables() -> frozenset[str]:
    """Return every table outside this plugin, except the NetBox bookkeeping a plugin-row write records.

    Bookkeeping: change logs, search and type caches, and user preferences.
    """
    from django.apps import apps

    bookkeeping = {
        apps.get_model(label)._meta.db_table
        for label in (
            "core.ObjectChange",
            "extras.CachedValue",
            "core.ObjectType",
            "contenttypes.ContentType",
            "users.UserConfig",
        )
    }
    tables = set()
    for model in apps.get_models(include_auto_created=True):
        owner = model._meta.auto_created or model
        if owner._meta.app_label != "netbox_nso_plugin" and not model._meta.proxy:
            tables.add(model._meta.db_table)
    return frozenset(tables - bookkeeping)


def is_native(model) -> bool:
    """Return whether *model* stores native NetBox rows."""
    return model._meta.db_table in native_tables()


def _model_tables(model):
    return frozenset(parent._meta.db_table for parent in (model, *model._meta.get_parent_list()))


def _cascade_tables(model):
    """Return the tables a Collector run deleting *model* rows touches directly: the model and its through rows."""
    through = {relation.remote_field.through._meta.db_table for relation in model._meta.many_to_many}
    return _model_tables(model) | through


# ── Scopes ──


@dataclass(frozen=True)
class _Permit:
    """One active native write: the effect the ORM call may make, and the tables it may touch."""

    kind: str
    model: type
    instance: Any = None
    field_name: str | None = None
    pks: frozenset = frozenset()
    updates: frozenset = frozenset()
    tables: frozenset = frozenset()
    origin: Any = None
    # Through tables of the owner's own M2M change while its save runs (between pre_* and post_*).
    window: set = field(default_factory=set, compare=False)


@dataclass(eq=False)
class _Scope:
    mode: str
    user: Any = None
    reason: str = ""
    parent: _Scope | None = None
    permits: list = field(default_factory=list)
    latched: str = ""
    refused: str = ""
    writer: NativeWriter | None = None
    kept: set = field(default_factory=set)
    effects: set = field(default_factory=set)
    shared: list = field(default_factory=list)
    conflict: str = ""

    def top(self):
        return self.permits[-1] if self.permits else None

    @contextlib.contextmanager
    def holding(self, permit):
        self.permits.append(permit)
        try:
            yield
        finally:
            self.permits.pop()

    def chain(self):
        scope = self
        while scope is not None:
            yield scope
            scope = scope.parent


_SCOPE: ContextVar[_Scope | None] = ContextVar("netbox_nso_plugin_authority", default=None)


def _refuse(message, error=NativeWriteRefused):
    """Raise a refusal and latch it on every enclosing scope, so a swallowed refusal still fails."""
    scope = _SCOPE.get()
    for link in scope.chain() if scope is not None else ():
        link.refused = link.refused or message
    raise error(message)


def _backstop(execute, sql, params, many, context):
    scope = _SCOPE.get()
    if scope is not None:
        match = _DML.match(str(sql))
        if match is not None and match.group(1) in native_tables():
            table = match.group(1)
            if scope.mode == "no_write":
                _refuse(f"native write to {table} outside a native-mutation endpoint")
            permit = scope.top()
            if scope.mode in _GUARDED_MODES and (permit is None or table not in permit.tables | permit.window):
                _refuse(f"native write to {table} outside its writer permit")
    return execute(sql, params, many, context)


@contextlib.contextmanager
def _entered(scope):
    scope.parent = _SCOPE.get()
    token = _SCOPE.set(scope)
    try:
        with connections["default"].execute_wrapper(_backstop):
            yield scope
            if scope.refused and scope.mode in _GUARDED_MODES:
                raise NativeWriteRefused(f"{scope.refused} (a helper swallowed this refusal)")
    finally:
        _SCOPE.reset(token)


@contextlib.contextmanager
def _operator_scope(user, target=None):
    scope = _Scope("operator", user=user)
    scope.writer = NativeWriter(scope)
    with transaction.atomic(), _entered(scope):
        keep_authorized_target(target)
        yield scope
        if scope.conflict:
            raise SharedOperationConflict(scope.conflict)
        # A denial latches: a helper that swallowed it still refuses the request.
        if scope.latched:
            raise permissions_violation(scope.latched)
        if not transaction.get_rollback():
            scope.writer.verify()


@contextlib.contextmanager
def authority_scope(effects: str, user=None, *, target=None):
    """Run an endpoint's handler under the authority of its declared effect.

    Read and adapter-action endpoints may not write native rows. A native-mutation endpoint
    writes them through the operator NativeWriter inside one transaction.
    """
    if effects in ("read", "adapter_action"):
        scope = _Scope("no_write", user=user)
        scope.writer = NativeWriter(scope)
        with _entered(scope):
            yield
    elif effects == "native_mutation":
        if user is None:
            raise NativeWriteRefused("a native-mutation endpoint needs its operator")
        with _operator_scope(user, target):
            yield
    else:
        raise NativeWriteRefused(f"unknown endpoint effect {effects!r}")


SYSTEM_ENTRIES: dict[str, str] = {}


def system_entry(reason: str):
    """Mark the function as an allowlisted entry into the system scope (device-fact reconciliation).

    Callers pass only the authorized device or management; the function writes device facts,
    not operator values. Inside a request it runs under the system NativeWriter; outside a
    request it runs unchanged. The census test pins the allowlist.
    """

    def decorate(function):
        SYSTEM_ENTRIES[f"{function.__module__}.{function.__qualname__}"] = reason

        @functools.wraps(function)
        def entered(*args, **kwargs):
            if _SCOPE.get() is None:
                return function(*args, **kwargs)
            scope = _Scope("system", reason=reason)
            scope.writer = NativeWriter(scope)
            with _entered(scope):
                return function(*args, **kwargs)

        return entered

    return decorate


def active_native_writer() -> NativeWriter | None:
    """Return the NativeWriter of the running scope, None outside a request."""
    scope = _SCOPE.get()
    return None if scope is None else scope.writer


def save_native(instance, *, origin, update_fields=None):
    """Save one native row through the active NativeWriter (a direct write outside a renderer plan)."""
    writer = active_native_writer()
    effect = (
        writer.save(instance, update_fields=update_fields, created=instance._state.adding, origin=origin)
        if writer is not None and is_native(type(instance))
        else contextlib.nullcontext()
    )
    with effect:
        instance.save(update_fields=update_fields)
    return instance


def requesting_user():
    """Return the operator of the running request, None outside a request."""
    scope = _SCOPE.get()
    return next((link.user for link in scope.chain() if link.user is not None), None) if scope else None


def operator_user():
    """Return the active operator, with no inherited authority inside a system entry."""
    scope = _SCOPE.get()
    return scope.user if scope is not None and scope.mode == "operator" else None


def operator_created(row):
    """Return whether this operator operation created the exact native row."""
    scope = _SCOPE.get()
    return scope is not None and scope.mode == "operator" and row.pk in scope.writer._created[row._meta.label_lower]


def refuse_operator_write(message):
    """Latch a permission refusal so a helper cannot swallow it and commit."""
    scope = _SCOPE.get()
    if scope is None or scope.mode != "operator":
        raise NativeWriteRefused("a permission refusal needs its operator scope")
    scope.writer._deny(message)


def keep_authorized_target(target):
    """Keep the management or unmanaged device that target authorization checked."""
    scope = _SCOPE.get()
    if scope is None or scope.mode != "operator" or target is None:
        return
    if target.management is not None:
        scope.kept.add(("management", target.management.pk))
    elif target.device is not None:
        scope.kept.add(("device", target.device.pk))


class shared_operation:
    """Authorize one complete management set, then freeze it under the mutation locks."""

    def __init__(self, affected):
        from django.http import Http404

        from .models import NSODeviceManagement

        self.affected = affected
        self.expected = frozenset(affected())
        self.scope = _SCOPE.get()
        self.checked = False
        if self.scope is not None and self.scope.mode == "operator":
            allowed = NSODeviceManagement.objects.restrict(self.scope.user, "change").filter(pk__in=self.expected)
            if allowed.count() != len(self.expected):
                raise Http404
            self.scope.shared.append(self)

    def verify(self):
        """Re-enumerate before the first write, with the existing mutation locks held."""
        if self.checked:
            return
        if frozenset(self.affected()) != self.expected:
            message = "The shared object membership changed. Refresh the page and try again."
            if self.scope is not None and self.scope.mode == "operator":
                self.scope.conflict = self.scope.conflict or message
            raise SharedOperationConflict(message)
        if self.scope is not None and self.scope.mode == "operator":
            self.scope.kept.update(("management", pk) for pk in self.expected)
        self.checked = True

    def __enter__(self):
        self.verify()
        return self

    def __exit__(self, *exc):
        return False


def verify_shared_operations():
    """Check pending shared operations after source locks and before revision writes."""
    scope = _SCOPE.get()
    if scope is not None and scope.mode == "operator":
        for operation in scope.shared:
            operation.verify()


def _record_effect(instance):
    """Record both overlay ownership and direct native device references for phase-end checks."""
    scope = _SCOPE.get()
    if scope is None or scope.mode != "operator":
        return
    import operator

    from django.core.exceptions import ObjectDoesNotExist

    from .models import NSODeviceManagement
    from .request_authorization import AUTHZ_TARGETS

    for model, field_name in _overlay_through_owners(type(instance)):
        _record_rows(model, (getattr(instance, field_name),))
    path = AUTHZ_TARGETS.get(instance._meta.label_lower)
    if path is not None:
        try:
            management = path.management_of(instance)
        except ObjectDoesNotExist:
            management = None
        if management is not None:
            scope.effects.add(("management", management.pk))
        else:
            device = operator.attrgetter(path.device.replace("__", "."))(instance)
            scope.effects.add(("device", device.pk))
        scope.writer.mutated = True
    elif is_native(type(instance)) and any(
        field.name == "device" and field.many_to_one and field.remote_field.model._meta.label_lower == "dcim.device"
        for field in instance._meta.concrete_fields
    ):
        management_id = (
            NSODeviceManagement.objects.filter(device_id=instance.device_id).values_list("pk", flat=True).first()
        )
        if management_id is not None:
            scope.effects.add(("management", management_id))


def _record_rows(model, pks):
    scope = _SCOPE.get()
    if scope is not None and scope.mode == "operator" and _records_effects(model):
        for row in model._default_manager.filter(pk__in=pks):
            _record_effect(row)


@functools.cache
def _overlay_through_owners(model):
    from django.apps import apps

    from .request_authorization import AUTHZ_TARGETS

    return tuple(
        (owner, model._meta.get_field(relation.m2m_field_name()).attname)
        for label in AUTHZ_TARGETS
        for owner in (apps.get_model(label),)
        for relation in owner._meta.many_to_many
        if relation.remote_field.through is model
    )


@functools.cache
def _records_effects(model):
    from .request_authorization import AUTHZ_TARGETS

    return (
        model._meta.label_lower in AUTHZ_TARGETS
        or bool(_overlay_through_owners(model))
        or (
            is_native(model)
            and any(
                field.name == "device"
                and field.many_to_one
                and field.remote_field.model._meta.label_lower == "dcim.device"
                for field in model._meta.concrete_fields
            )
        )
    )


def _record_saved(sender, instance, **kwargs):
    _record_effect(instance)


def require_operator_add(row) -> None:
    """Refuse a row the requesting operator may not add; NetBox checks add constraints on the saved row.

    Outside a request no operator acts (C4), and the check does not apply.
    """
    scope = _SCOPE.get()
    if scope is None:
        return
    user = requesting_user()
    if user is None:
        _refuse(f"an add of {row._meta.label_lower} inside a request needs its operator")
    model = type(row)
    if not (user.has_perm(_permission(model, "add")) and _restricted(model, user, "add").filter(pk=row.pk).exists()):
        message = f"You cannot add this {model._meta.verbose_name}."
        scope.latched = scope.latched or message
        raise permissions_violation(message)


def refuse_adapter_call_in_mutation_phase(method: str) -> None:
    """Refuse an adapter call while an operator scope's native writes are not yet verified and committed.

    The mutation phase starts at the scope's first native effect. Before it, a read is allowed;
    any other call waits for the commit (outbox delivery).
    """
    scope = _SCOPE.get()
    for link in scope.chain() if scope is not None else ():
        if link.mode == "operator" and (method.upper() != "GET" or link.writer.mutated):
            _refuse(f"adapter {method} inside an operator native-mutation phase; deliver after commit")


# ── The NativeWriter ──


def _restricted(model, user, action):
    from utilities.querysets import RestrictedQuerySet

    return RestrictedQuerySet(model=model).restrict(user, action)


def _permission(model, action):
    return f"{model._meta.app_label}.{action}_{model._meta.model_name}"


def _stored(model, pk):
    return model._default_manager.filter(pk=pk).first() if pk is not None else None


class NativeWriter:
    """Authorize each native effect of one authority scope and hold its permit during the ORM call.

    Operator scope: the stored row must be in the user's restriction before a change, and every
    completed row must be in it before commit (C1). System scope: each effect names its value
    source; owned intent and the mapped effects of an owned anchored scope are refused (C14).
    """

    def __init__(self, scope):
        self._scope = scope
        self.mutated = False
        self._created = defaultdict(set)
        self._changed = defaultdict(set)

    # The effects. Each authorizes first, holds its permit for the ORM call, then records it.

    @contextlib.contextmanager
    def save(self, instance, *, update_fields, created, origin):
        """Authorize one native save; *created* is True for an insert."""
        model = type(instance)
        self._authorize("save", model, instance=instance, update_fields=update_fields, created=created, origin=origin)
        permit = _Permit("save", model, instance=instance, tables=_model_tables(model), origin=origin)
        with self._scope.holding(permit):
            yield
        self._record(model, (instance.pk,), "add" if created else "change")

    @contextlib.contextmanager
    def delete(self, root, *, closure, origin):
        """Authorize one Collector run: the root is the origin, its cascades follow it (NetBox semantics)."""
        from django.apps import apps

        model = type(root)
        if is_native(model):
            self._authorize("delete", model, instance=root, origin=origin)
        self.mutated = True
        models = {apps.get_model(label) for label, _pk, _operation in closure}
        tables = frozenset().union(*(_cascade_tables(member) for member in models | {model}))
        deletes = frozenset((label, pk) for label, pk, operation in closure if operation == "delete")
        updates = frozenset((label, pk) for label, pk, operation in closure if operation == "set_update")
        permit = _Permit("delete", model, instance=root, pks=deletes, updates=updates, tables=tables)
        with self._scope.holding(permit):
            yield

    @contextlib.contextmanager
    def m2m(self, owner, field_name, *, origin):
        """Authorize one M2M change; the relation owner carries it."""
        model = type(owner)
        self._authorize("m2m", model, instance=owner, field_name=field_name, origin=origin)
        through = model._meta.get_field(field_name).remote_field.through
        permit = _Permit("m2m", model, instance=owner, field_name=field_name, tables=_model_tables(through))
        with self._scope.holding(permit):
            yield
        self._record(model, (owner.pk,), "change")

    @contextlib.contextmanager
    def set_update(self, model, pks, values, *, origin):
        """Authorize one set-based update of exactly the rows *pks*."""
        pks = frozenset(pks)
        self._authorize("set_update", model, pks=pks, update_fields=tuple(values), values=values, origin=origin)
        with self._scope.holding(_Permit("set_update", model, pks=pks, tables=_model_tables(model))):
            yield
        self._record(model, pks, "change")

    def authorize_m2m(self, model, owner, field_name, *, origin):
        """Authorize an M2M change that the owner's own permitted save makes."""
        self._authorize("m2m", model, instance=owner, field_name=field_name, origin=origin)

    # Operator authorization.

    def _deny(self, message):
        self._scope.latched = self._scope.latched or message
        raise permissions_violation(message)

    def _require_permission(self, model, action):
        if not self._scope.user.has_perm(_permission(model, action)):
            self._deny(f"Native write refused: you cannot {action} {model._meta.verbose_name_plural}.")

    def _require_rows(self, model, action, pks):
        pks = set(pks)
        if _restricted(model, self._scope.user, action).filter(pk__in=pks).count() != len(pks):
            self._deny(f"Native write refused: you cannot {action} this {model._meta.verbose_name}.")

    def _operator_check(self, kind, model, *, instance, pks, created):
        label = model._meta.label_lower
        if kind == "save" and created:
            self._require_permission(model, "add")
            return
        selected = set(pks) if kind == "set_update" else {instance.pk}
        if kind != "delete" and selected <= self._created[label]:
            # A later write of a row this operation created needs only the add check at phase end (C1).
            return
        action = "delete" if kind == "delete" else "change"
        self._require_permission(model, action)
        self._require_rows(model, action, selected - self._created[label] if action == "change" else selected)

    def _record(self, model, pks, action):
        if self._scope.mode != "operator":
            return
        _record_rows(model, pks)
        label = model._meta.label_lower
        for pk in pks:
            if action == "add":
                self._created[label].add(pk)
            elif pk not in self._created[label]:
                self._changed[label].add(pk)

    def verify(self):
        """Require every surviving created or changed native row in the add or change restriction (phase end).

        A row a savepoint rollback restored is verified again; a deleted row has nothing to verify.
        """
        from dcim.models import Device
        from django.apps import apps

        from .models import NSODeviceManagement

        scope = self._scope
        if not scope.effects <= scope.kept:
            self._deny("Write refused: an affected management was not declared by this operation.")
        for kind, model in (("management", NSODeviceManagement), ("device", Device)):
            pks = {pk for effect_kind, pk in scope.effects if effect_kind == kind}
            if pks and _restricted(model, scope.user, "change").filter(pk__in=pks).count() != len(pks):
                self._deny(f"Write refused: you cannot change every affected {model._meta.verbose_name}.")

        for rows, action in ((self._created, "add"), (self._changed, "change")):
            for label, pks in rows.items():
                model = apps.get_model(label)
                surviving = set(model._default_manager.filter(pk__in=pks).values_list("pk", flat=True))
                if surviving:
                    self._require_rows(model, action, surviving)

    # System authorization (C14).

    def _system_check(self, kind, model, *, instance, pks, update_fields, created, field_name, values, origin):
        label = model._meta.label_lower
        if origin is None:
            _refuse(f"the system-scope {kind} of {label} declares no value source")
        if isinstance(origin, OwnedIntent):
            _refuse(
                f"the system scope cannot write the owned intent of {origin.scope} into {label}", OwnedIntentRefused
            )
        if not isinstance(origin, Observed):
            _refuse(f"unknown value source {origin!r}")
        for scope_label, entry in _anchored_entries(label):
            if self._carries_owned_intent(
                entry, kind, model, instance, pks, update_fields, created, field_name, values
            ):
                _refuse(f"{label} row carries the owned intent of {scope_label}", OwnedIntentRefused)

    @staticmethod
    def _carries_owned_intent(entry, kind, model, instance, pks, update_fields, created, field_name, values):
        """Return whether the effect writes the anchored entry's intent while its scope is owned (C14b).

        A mapped change is checked at the stored and at the resulting anchor; any other change only
        when it moves the row into an owned anchor.
        """
        mapped = entry.fields
        if kind == "m2m":
            return (mapped is None or field_name in mapped) and entry.owned(instance)
        if kind == "delete":
            return entry.lifecycle and entry.owned(instance)
        if kind == "save" and created:
            return entry.owned(instance)
        # update_fields and set-update keys may name a field or its attname (instance or instance_id).
        names = {model._meta.get_field(name).name for name in update_fields} if update_fields is not None else None
        columns = [column for column in model._meta.concrete_fields if names is None or column.name in names]
        if kind == "set_update":
            pairs = []
            for stored in model._default_manager.filter(pk__in=pks):
                result = copy.copy(stored)
                for name, value in values.items():
                    setattr(result, model._meta.get_field(name).attname, value)
                pairs.append((stored, result))
        else:
            stored = _stored(model, instance.pk)
            result = instance
            if stored is not None and names is not None:
                # A partial save persists only the written fields: the result is the stored row plus them.
                result = copy.copy(stored)
                for column in columns:
                    setattr(result, column.attname, getattr(instance, column.attname))
            pairs = [(stored, result)] if stored is not None else []
        for stored, result in pairs:
            changed = {
                column.name for column in columns if getattr(stored, column.attname) != getattr(result, column.attname)
            }
            if not changed:
                continue
            if mapped is None or changed & mapped:
                if entry.owned(stored) or entry.owned(result):
                    return True
            elif entry.owned(result) and not entry.owned(stored):
                return True
        return False

    def _authorize(
        self,
        kind,
        model,
        *,
        instance=None,
        pks=frozenset(),
        update_fields=None,
        created=False,
        field_name=None,
        values=None,
        origin,
    ):
        scope = self._scope
        if scope.mode == "no_write":
            _refuse(f"native {kind} of {model._meta.label_lower} outside a native-mutation endpoint")
        self.mutated = True
        if scope.mode == "operator":
            self._operator_check(kind, model, instance=instance, pks=pks, created=created)
        else:
            self._system_check(
                kind,
                model,
                instance=instance,
                pks=pks,
                update_fields=update_fields,
                created=created,
                field_name=field_name,
                values=values,
                origin=origin,
            )


# ── Ambient guards: a native write inside a request needs the exact active permit (C9) ──


def _guarded_scope():
    scope = _SCOPE.get()
    return scope if scope is not None and scope.mode in _GUARDED_MODES else None


def _guard_save(sender, instance, **kwargs):
    if instance.pk is not None:
        _record_rows(sender, (instance.pk,))
    _record_effect(instance)
    scope = _guarded_scope()
    if scope is None or not is_native(sender):
        return
    permit = scope.top()
    if permit is not None and permit.kind == "save" and permit.instance is instance:
        return
    # NetBox clears a deleted row's references by saving the referring row (primary IPs): the closure froze it.
    if permit is not None and permit.kind == "delete" and (sender._meta.label_lower, instance.pk) in permit.updates:
        return
    _refuse(f"native save of {sender._meta.label_lower} without its writer permit")


def _through_owned(model, row, permit):
    """Return whether *row* of an M2M through table belongs to the permit's relation owner."""
    if permit is None or permit.kind not in ("save", "m2m") or not model._meta.auto_created:
        return False
    for relation in type(permit.instance)._meta.many_to_many:
        if relation.remote_field.through is not model:
            continue
        if permit.kind == "m2m" and relation.name != permit.field_name:
            continue
        owner = model._meta.get_field(relation.m2m_field_name()).attname
        return getattr(row, owner) == permit.instance.pk
    return False


def _guard_delete(sender, instance, **kwargs):
    _record_effect(instance)
    scope = _guarded_scope()
    if scope is None or not is_native(sender):
        return
    permit = scope.top()
    if permit is None or permit.kind != "delete" or (sender._meta.label_lower, instance.pk) not in permit.pks:
        _refuse(f"native delete of {sender._meta.label_lower} without its writer permit")


def _guard_m2m(sender, instance, action, reverse, pk_set=None, **kwargs):
    if not action.startswith(("pre_", "post_")):
        return
    if reverse:
        owners = [
            field
            for field in sender._meta.fields
            if field.many_to_one and field.remote_field.model is not type(instance)
        ]
        if len(owners) == 1:
            owner_model = owners[0].remote_field.model
            selected = pk_set
            if selected is None:
                relation_field = next(
                    field
                    for field in sender._meta.fields
                    if field.many_to_one and field.remote_field.model is type(instance)
                )
                selected = sender.objects.filter(**{relation_field.attname: instance.pk}).values_list(
                    owners[0].attname, flat=True
                )
            _record_rows(owner_model, selected)
    else:
        _record_effect(instance)
    scope = _guarded_scope()
    if scope is None or sender._meta.db_table not in native_tables():
        return
    permit = scope.top()
    owned_by_permit = permit is not None and not reverse and permit.instance is instance
    field_name = next(
        (relation.name for relation in type(instance)._meta.many_to_many if relation.remote_field.through is sender),
        None,
    )
    if action.startswith("post_"):
        if owned_by_permit and permit.kind == "save":
            permit.window.discard(sender._meta.db_table)
        return
    if owned_by_permit and permit.kind == "m2m" and permit.field_name == field_name:
        return
    if owned_by_permit and permit.kind == "save":
        # The owner's save changes its own relation (NetBox clears tagged VLANs): C14 applies to that change.
        changes = bool(pk_set) if action != "pre_clear" else getattr(instance, field_name).exists()
        if changes and scope.mode == "system":
            scope.writer.authorize_m2m(type(instance), instance, field_name, origin=permit.origin)
        permit.window.add(sender._meta.db_table)
        return
    _refuse(f"native M2M change of {sender._meta.label_lower} without its writer permit")


def _bookkeeping_update(model, names, permit):
    """Return whether an update writes only NetBox caches that follow the permitted write.

    Counter caches of related rows, and the underscore-named caches (prefix depth and children)
    NetBox recomputes on rows of the model being written.
    """
    from django.core.exceptions import FieldDoesNotExist
    from utilities.fields import CounterCacheField

    if permit is None or not names:
        return False
    try:
        fields = [model._meta.get_field(name) for name in names]
    except FieldDoesNotExist:
        return False
    if all(isinstance(field, CounterCacheField) for field in fields):
        return True
    return model._meta.db_table in permit.tables and all(field.name.startswith("_") for field in fields)


def _guarded_update(original):
    @functools.wraps(original)
    def guarded(self, **kwargs):
        scope = _guarded_scope()
        if scope is None or not is_native(self.model):
            if scope is None or scope.mode != "operator" or not _records_effects(self.model):
                return original(self, **kwargs)
            selected = set(self.values_list("pk", flat=True))
            _record_rows(self.model, selected)
            result = original(self, **kwargs)
            _record_rows(self.model, selected)
            return result
        permit = scope.top()
        if _bookkeeping_update(self.model, kwargs, permit):
            _record_rows(self.model, self.values_list("pk", flat=True))
            with scope.holding(_Permit("bookkeeping", self.model, tables=_model_tables(self.model))):
                return original(self, **kwargs)
        selected = set(self.values_list("pk", flat=True))
        if not selected:
            # An update that selects no row writes nothing (NetBox's signal handlers clear references this way).
            return 0
        label = self.model._meta.label_lower
        exact = permit is not None and permit.kind == "set_update" and permit.model is self.model
        # A Collector run updates the foreign keys its plan froze (SET_NULL and similar).
        collected = (
            permit is not None and permit.kind == "delete" and {(label, pk) for pk in selected} <= permit.updates
        )
        if not (exact and selected == permit.pks) and not collected:
            _refuse(f"native queryset update of {label} without its writer permit")
        _record_rows(self.model, selected)
        result = original(self, **kwargs)
        _record_rows(self.model, selected)
        return result

    guarded._nso_native_guard = True
    return guarded


def _guarded_bulk(bulk):
    signature = inspect.signature(bulk)

    @functools.wraps(bulk)
    def guarded(self, *args, **kwargs):
        scope = _guarded_scope()
        if scope is None or not is_native(self.model):
            if scope is None or scope.mode != "operator" or not _records_effects(self.model):
                return bulk(self, *args, **kwargs)
            call = signature.bind(self, *args, **kwargs)
            call.arguments["objs"] = rows = list(call.arguments["objs"])
            _record_rows(self.model, [row.pk for row in rows if row.pk is not None])
            for row in rows:
                _record_effect(row)
            result = bulk(*call.args, **call.kwargs)
            _record_rows(self.model, [row.pk for row in rows if row.pk is not None])
            return result
        call = signature.bind(self, *args, **kwargs)
        if call.arguments.get("update_conflicts"):
            # An upsert changes the stored conflict row, which the proposed rows do not name.
            _refuse(f"native upsert of {self.model._meta.label_lower} inside a request")
        call.arguments["objs"] = rows = list(call.arguments["objs"])
        permit = scope.top()
        # M2M add writes the owner's own through rows; another owner's rows need their own permit.
        if rows and all(_through_owned(self.model, row, permit) for row in rows):
            return bulk(*call.args, **call.kwargs)
        names = tuple(call.arguments.get("fields", ())) if bulk.__name__ == "bulk_update" else ()
        if not _bookkeeping_update(self.model, names, permit):
            _refuse(f"native bulk write of {self.model._meta.label_lower} without its writer permit")
        _record_rows(self.model, [row.pk for row in rows])
        with scope.holding(_Permit("bookkeeping", self.model, tables=_model_tables(self.model))):
            return bulk(*call.args, **call.kwargs)

    guarded._nso_native_guard = True
    return guarded


def _refuse_foreign_through_rows(model, rows, permit, operation):
    """Refuse a direct through-row write whose rows do not all belong to the permit's owner.

    Django sends no save or delete signals for auto-created M2M through models, so their
    queryset entry points carry the owner check. A Collector run keeps its frozen closure.
    """
    if permit is not None and permit.kind == "delete":
        # A Collector run deletes only the through rows its closure froze; it creates none.
        label = model._meta.label_lower
        if operation == "delete" and all((label, row.pk) in permit.pks for row in rows):
            return
        _refuse(f"native {operation} of {label} rows outside the frozen deletion closure")
    if not all(_through_owned(model, row, permit) for row in rows):
        _refuse(f"native {operation} of {model._meta.label_lower} rows of another owner")


def _guarded_create(original):
    @functools.wraps(original)
    def guarded(self, **kwargs):
        scope = _guarded_scope()
        if scope is not None and self.model._meta.auto_created and is_native(self.model):
            _refuse_foreign_through_rows(self.model, [self.model(**kwargs)], scope.top(), "create")
        if scope is not None and scope.mode == "operator" and _overlay_through_owners(self.model):
            _record_effect(self.model(**kwargs))
        return original(self, **kwargs)

    guarded._nso_native_guard = True
    return guarded


def _guarded_delete(original):
    @functools.wraps(original)
    def guarded(self):
        scope = _guarded_scope()
        if scope is not None and self.model._meta.auto_created and is_native(self.model):
            _refuse_foreign_through_rows(self.model, list(self), scope.top(), "delete")
        if scope is not None and scope.mode == "operator" and _overlay_through_owners(self.model):
            _record_rows(self.model, self.values_list("pk", flat=True))
        return original(self)

    guarded._nso_native_guard = True
    return guarded


def install_guards() -> None:
    """Connect the ambient native-write guards (signals and queryset methods)."""
    from django.db.models import QuerySet, signals

    signals.pre_save.connect(_guard_save, dispatch_uid="nso_native_guard_save")
    signals.post_save.connect(_record_saved, dispatch_uid="nso_overlay_effect_save")
    signals.pre_delete.connect(_guard_delete, dispatch_uid="nso_native_guard_delete")
    signals.m2m_changed.connect(_guard_m2m, dispatch_uid="nso_native_guard_m2m")
    for name, guard in (("update", _guarded_update), ("create", _guarded_create), ("delete", _guarded_delete)):
        method = getattr(QuerySet, name)
        if not getattr(method, "_nso_native_guard", False):
            setattr(QuerySet, name, guard(method))
    for name in ("bulk_create", "bulk_update"):
        method = getattr(QuerySet, name)
        if not getattr(method, "_nso_native_guard", False):
            setattr(QuerySet, name, _guarded_bulk(method))
