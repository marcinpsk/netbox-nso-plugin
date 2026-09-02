# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and execute exact renderer-input mutations."""

from __future__ import annotations

import contextlib
import contextvars
import copy
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone

from .intent_state import (
    ABSENT,
    SOURCE_MODEL_RANKS,
    IntentMutationProtocolError,
    MutationFootprint,
    SourceRow,
    _authorize_dml,
    _intent_transaction,
    _normal,
    canonical_fragment,
    deletion_footprint_for_instance,
    footprint_for_instance,
    mirror_transaction,
    renderer_input_specs,
)


class IntentPlanStaleError(IntentMutationProtocolError):
    """A renderer input row changed after its exact plan was frozen."""


@dataclass(frozen=True)
class RendererWrite:
    """One exact operation in a frozen renderer write set."""

    operation: str
    model_label: str
    pk: Any = None
    natural_key: tuple[tuple[str, Any], ...] = ()
    update_fields: tuple[str, ...] | None = None
    values: tuple[tuple[str, Any], ...] = ()
    before_values: tuple[tuple[str, Any], ...] = ()
    selected_pks: tuple[Any, ...] = ()
    cascade: bool = False
    force_insert: bool = False


@dataclass(frozen=True)
class RendererCreationRef:
    """A stable reference to another row created by the same frozen plan."""

    model_label: str
    natural_key: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class RendererSave:
    """A proposed instance save, before it is reduced to an immutable write."""

    instance: Any
    update_fields: tuple[str, ...] | None = None
    force_insert: bool = False
    natural_key_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RendererDelete:
    """A proposed instance delete, before Collector expands its effects."""

    instance: Any


@dataclass(frozen=True)
class RendererSetUpdate:
    """A set-based update whose selected primary keys are already frozen."""

    model_label: str
    selected_pks: tuple[Any, ...]
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class RendererM2MAdd:
    """An exact set of related rows to add to one M2M field."""

    instance: Any
    field_name: str
    related: tuple[Any, ...]


@dataclass(frozen=True)
class RendererM2MSet:
    """An exact final related-row set for one M2M field."""

    instance: Any
    field_name: str
    related: tuple[Any, ...]


def planned_save(instance, *, update_fields=None, force_insert=False, natural_key=()) -> RendererSave:
    """Describe one save for :meth:`RendererMutationPlan.build`."""
    fields = None if update_fields is None else tuple(sorted(set(update_fields)))
    return RendererSave(
        instance=instance,
        update_fields=fields,
        force_insert=bool(force_insert),
        natural_key_fields=tuple(natural_key),
    )


def planned_delete(instance) -> RendererDelete:
    """Describe one delete for :meth:`RendererMutationPlan.build`."""
    return RendererDelete(instance=instance)


def planned_set_update(queryset, **values) -> RendererSetUpdate:
    """Freeze one queryset and its exact set-based update values."""
    model = queryset.model
    normalized = tuple(
        sorted(
            (
                model._meta.get_field(name).attname,
                _normal(value),
            )
            for name, value in values.items()
        )
    )
    return RendererSetUpdate(
        model_label=model._meta.label_lower,
        selected_pks=tuple(queryset.order_by("pk").values_list("pk", flat=True)),
        values=normalized,
    )


def planned_m2m_add(instance, field_name, related) -> RendererM2MAdd:
    """Freeze one M2M add operation before lock acquisition."""
    return RendererM2MAdd(instance=instance, field_name=field_name, related=tuple(related))


def planned_m2m_set(instance, field_name, related) -> RendererM2MSet:
    """Freeze the exact final edge set for one M2M field."""
    return RendererM2MSet(instance=instance, field_name=field_name, related=tuple(related))


@dataclass(frozen=True)
class RendererMutationPlan:
    """An immutable exact write set and its mechanically derived lock footprint."""

    write_set: tuple[RendererWrite, ...]
    lock_footprint: MutationFootprint
    content_keys: tuple[tuple[int, str], ...]
    planned_at: Any

    @property
    def changes_content(self) -> bool:
        return bool(self.content_keys)

    @classmethod
    def build(cls, *, saves=(), deletes=(), set_updates=(), m2m_writes=(), planned_at=None) -> RendererMutationPlan:
        """Freeze proposed writes and derive every lock and revision dependency."""
        planned_at = planned_at or timezone.now()
        saves = tuple(saves)
        creation_refs = _creation_refs(saves)
        support_refs = _referenced_support_refs(saves, creation_refs)
        writes: list[RendererWrite] = []
        footprints: list[MutationFootprint] = []
        content_keys: set[tuple[int, str]] = set()

        for proposed in saves:
            write, footprint, changed_keys = _plan_save(proposed, creation_refs, support_refs)
            writes.append(write)
            footprints.append(footprint)
            content_keys.update(changed_keys)
        for proposed in deletes:
            delete_writes, footprint, changed_keys = _plan_delete(proposed)
            writes.extend(delete_writes)
            footprints.append(footprint)
            content_keys.update(changed_keys)
        for proposed in set_updates:
            write, footprint, changed_keys = _plan_set_update(proposed)
            writes.append(write)
            footprints.append(footprint)
            content_keys.update(changed_keys)
        for proposed in m2m_writes:
            if isinstance(proposed, RendererM2MAdd):
                write, footprint, changed_keys = _plan_m2m_add(proposed, creation_refs)
            elif isinstance(proposed, RendererM2MSet):
                write, footprint, changed_keys = _plan_m2m_set(proposed, creation_refs)
            else:
                raise IntentMutationProtocolError(f"unsupported M2M proposal {type(proposed)!r}")
            writes.append(write)
            footprints.append(footprint)
            content_keys.update(changed_keys)

        lock_footprint = MutationFootprint.merge(*footprints) if footprints else MutationFootprint()
        return cls(
            write_set=tuple(writes),
            lock_footprint=lock_footprint,
            content_keys=tuple(sorted(content_keys)),
            planned_at=planned_at,
        )


def _stored_instance(instance):
    if instance.pk is None or instance._state.adding:
        return None
    return type(instance)._default_manager.filter(pk=instance.pk).first()


def _effective_after(instance, before, update_fields):
    if before is None or update_fields is None:
        return instance
    effective = copy.copy(before)
    for name in update_fields:
        field = instance._meta.get_field(name)
        if field.is_relation and field.many_to_one and field.is_cached(instance):
            setattr(effective, field.name, field.get_cached_value(instance))
        else:
            setattr(effective, field.attname, getattr(instance, field.attname))
    return effective


def _planned_field_value(instance, field, creation_refs):
    if field.is_relation and field.many_to_one and field.is_cached(instance):
        related = field.get_cached_value(instance)
        reference = creation_refs.get(id(related))
        if reference is not None:
            return reference
    return _normal(getattr(instance, field.attname))


def _field_values(instance, update_fields, creation_refs=None):
    creation_refs = creation_refs or {}
    fields = instance._meta.concrete_fields
    if update_fields is not None:
        selected = set(update_fields)
        fields = tuple(field for field in fields if field.name in selected or field.attname in selected)
    else:
        fields = tuple(field for field in fields if not field.primary_key)
    return tuple(sorted((field.attname, _planned_field_value(instance, field, creation_refs)) for field in fields))


def _natural_key(instance, fields, creation_refs=None):
    creation_refs = creation_refs or {}
    return tuple(
        (
            instance._meta.get_field(name).attname,
            _planned_field_value(instance, instance._meta.get_field(name), creation_refs),
        )
        for name in fields
    )


def _creation_refs(saves):
    references = {}
    for proposed in saves:
        instance = proposed.instance
        if _stored_instance(instance) is not None:
            continue
        if not proposed.natural_key_fields:
            continue
        references[id(instance)] = RendererCreationRef(
            model_label=instance._meta.label_lower,
            natural_key=_natural_key(instance, proposed.natural_key_fields, references),
        )
    return references


def _referenced_support_refs(saves, creation_refs):
    references = set()
    specs = renderer_input_specs()
    for proposed in saves:
        if proposed.instance._meta.label_lower not in specs:
            continue
        before = _stored_instance(proposed.instance)
        after = _effective_after(proposed.instance, before, proposed.update_fields)
        values = _field_values(after, proposed.update_fields, creation_refs)
        references.update(value for _attname, value in values if isinstance(value, RendererCreationRef))
    return frozenset(references)


def _dependencies(before, after, spec):
    resolver = spec.dependency_resolver
    if resolver is None:
        return MutationFootprint(), False
    return resolver(before, after, spec)


def _changed_keys(before, after, spec, dependency_changed):
    before_fragment = ABSENT if before is None else canonical_fragment(before, spec)
    after_fragment = ABSENT if after is None else canonical_fragment(after, spec)
    if before_fragment == after_fragment and not dependency_changed:
        return set()
    keys = set()
    for candidate in (before, after):
        if candidate is not None:
            keys.update(spec.resolver(candidate, spec))
    return keys


def _plan_save(proposed: RendererSave, creation_refs, support_refs):
    instance = proposed.instance
    label = instance._meta.label_lower
    spec = renderer_input_specs().get(label)
    before = _stored_instance(instance)
    if spec is None:
        reference = creation_refs.get(id(instance))
        if before is not None or not proposed.force_insert or reference not in support_refs:
            raise IntentMutationProtocolError(f"{label} is not a registered renderer input")
        return (
            RendererWrite(
                operation="save",
                model_label=label,
                natural_key=reference.natural_key,
                update_fields=proposed.update_fields,
                values=_field_values(instance, proposed.update_fields, creation_refs),
                force_insert=True,
            ),
            MutationFootprint(),
            set(),
        )
    after = _effective_after(instance, before, proposed.update_fields)
    if before is None and not proposed.natural_key_fields:
        raise IntentMutationProtocolError(f"a {label} creation requires a stable natural key")
    identity = () if before is not None else _natural_key(after, proposed.natural_key_fields, creation_refs)
    write = RendererWrite(
        operation="save",
        model_label=label,
        pk=None if before is None else before.pk,
        natural_key=identity,
        update_fields=proposed.update_fields,
        values=_field_values(after, proposed.update_fields, creation_refs),
        before_values=() if before is None else _field_values(before, None),
        force_insert=proposed.force_insert,
    )
    base = MutationFootprint.merge(
        *(footprint_for_instance(candidate, spec) for candidate in (before, after) if candidate is not None)
    )
    dependency_footprint, dependency_changed = _dependencies(before, after, spec)
    footprint = MutationFootprint.merge(base, dependency_footprint)
    return write, footprint, _changed_keys(before, after, spec, dependency_changed)


def _materialize_field_update_rows(rows):
    """Return one field-update container's model and stable row tuple."""
    if hasattr(rows, "model"):
        return rows.model, tuple(rows.order_by("pk"))
    materialized = tuple(rows)
    if not materialized:
        return None, ()
    return type(materialized[0]), materialized


def _collector_writes(instance):
    from django.db.models.deletion import Collector

    collector = Collector(using=instance._state.db or "default", origin=instance)
    collector.collect([instance])
    root_identity = (instance._meta.label_lower, instance.pk)
    writes = []
    footprints = []
    changed_keys = set()
    specs = renderer_input_specs()

    def record_change(before, after):
        spec = specs.get(before._meta.label_lower)
        if spec is None:
            return
        dependency_footprint, dependency_changed = _dependencies(before, after, spec)
        footprints.append(footprint_for_instance(before, spec))
        if after is not None:
            footprints.append(footprint_for_instance(after, spec))
        footprints.append(dependency_footprint)
        changed_keys.update(_changed_keys(before, after, spec, dependency_changed))

    for model, rows in collector.data.items():
        for row in sorted(rows, key=lambda instance: instance.pk):
            writes.append(
                RendererWrite(
                    operation="delete",
                    model_label=model._meta.label_lower,
                    pk=row.pk,
                    before_values=_field_values(row, None),
                    cascade=(model._meta.label_lower, row.pk) != root_identity,
                )
            )
            record_change(row, None)
    for queryset in collector.fast_deletes:
        for row in queryset.order_by("pk"):
            writes.append(
                RendererWrite(
                    operation="delete",
                    model_label=queryset.model._meta.label_lower,
                    pk=row.pk,
                    before_values=_field_values(row, None),
                    cascade=True,
                )
            )
            record_change(row, None)
    for (field, value), querysets in collector.field_updates.items():
        for rows in querysets:
            model, materialized = _materialize_field_update_rows(rows)
            if model is None:
                continue
            for row in materialized:
                writes.append(
                    RendererWrite(
                        operation="set_update",
                        model_label=model._meta.label_lower,
                        pk=row.pk,
                        update_fields=(field.name,),
                        values=((field.attname, _normal(value)),),
                        cascade=True,
                    )
                )
                after = copy.copy(row)
                setattr(after, field.attname, _normal(value))
                if field.is_relation and field.is_cached(after):
                    field.delete_cached_value(after)
                record_change(row, after)
    footprint = MutationFootprint.merge(*footprints) if footprints else MutationFootprint()
    return tuple(writes), footprint, changed_keys


def _plan_delete(proposed: RendererDelete):
    instance = proposed.instance
    label = instance._meta.label_lower
    spec = renderer_input_specs().get(label)
    if spec is None:
        raise IntentMutationProtocolError(f"{label} is not a registered renderer input")
    before = _stored_instance(instance)
    if before is None:
        raise IntentMutationProtocolError(f"cannot plan deletion of missing {label} row {instance.pk!r}")
    writes, collector_footprint, changed_keys = _collector_writes(before)
    footprint = MutationFootprint.merge(
        deletion_footprint_for_instance(before),
        collector_footprint,
    )
    return writes, footprint, changed_keys


def _plan_set_update(proposed: RendererSetUpdate):
    model = apps.get_model(proposed.model_label)
    spec = renderer_input_specs().get(proposed.model_label)
    if spec is None:
        raise IntentMutationProtocolError(f"{proposed.model_label} is not a registered renderer input")
    values = dict(proposed.values)
    footprints = []
    changed_keys = set()
    for before in model._default_manager.filter(pk__in=proposed.selected_pks).order_by("pk"):
        after = copy.copy(before)
        for attname, value in values.items():
            setattr(after, attname, value)
        dependency_footprint, dependency_changed = _dependencies(before, after, spec)
        footprints.extend((footprint_for_instance(before, spec), footprint_for_instance(after, spec)))
        footprints.append(dependency_footprint)
        changed_keys.update(_changed_keys(before, after, spec, dependency_changed))
    write = RendererWrite(
        operation="set_update",
        model_label=proposed.model_label,
        update_fields=tuple(sorted(values)),
        values=proposed.values,
        selected_pks=proposed.selected_pks,
    )
    footprint = MutationFootprint.merge(*footprints) if footprints else MutationFootprint()
    return write, footprint, changed_keys


def _creation_identity(instance, creation_refs):
    reference = creation_refs.get(id(instance))
    if reference is not None:
        return reference
    if instance.pk is None or instance._state.adding:
        raise IntentMutationProtocolError("an M2M row creation requires a stable natural key save")
    return instance.pk


def _related_identities(related, creation_refs):
    identities = {_creation_identity(row, creation_refs) for row in related}
    return tuple(
        sorted(
            identities,
            key=lambda identity: (1, repr(identity)) if isinstance(identity, RendererCreationRef) else (0, identity),
        )
    )


def _plan_m2m_add(proposed: RendererM2MAdd, creation_refs):
    instance = proposed.instance
    owner_spec = renderer_input_specs().get(instance._meta.label_lower)
    if owner_spec is None:
        raise IntentMutationProtocolError(f"{instance._meta.label_lower} is not a registered renderer input")
    owner_identity = _creation_identity(instance, creation_refs)
    field = instance._meta.get_field(proposed.field_name)
    through = field.remote_field.through
    related_model = field.remote_field.model
    existing = (
        set(getattr(instance, proposed.field_name).values_list("pk", flat=True))
        if instance.pk is not None and not instance._state.adding
        else set()
    )
    related_identities = _related_identities(proposed.related, creation_refs)
    added_identities = tuple(identity for identity in related_identities if identity not in existing)
    if added_identities != related_identities:
        raise IntentMutationProtocolError("an M2M add plan must contain only absent edges")
    persisted_pks = tuple(identity for identity in added_identities if not isinstance(identity, RendererCreationRef))
    persisted = tuple(related_model._default_manager.filter(pk__in=persisted_pks).order_by("pk"))
    if {row.pk for row in persisted} != set(persisted_pks):
        raise IntentMutationProtocolError("an M2M add plan contains a missing related row")
    owner_footprint = footprint_for_instance(instance, owner_spec)
    try:
        related_footprints = tuple(footprint_for_instance(row) for row in proposed.related)
    except KeyError as exc:
        raise IntentMutationProtocolError(f"unregistered M2M related model {exc.args[0]}") from exc
    row_kind = "source_rows" if through._meta.label_lower in SOURCE_MODEL_RANKS else "overlay_rows"
    through_footprint = MutationFootprint.for_keys(
        (),
        **{row_kind: (SourceRow(through._meta.label_lower, None),)},
    )
    footprint = MutationFootprint.merge(owner_footprint, *related_footprints, through_footprint)
    scopes = set(owner_spec.scopes)
    if instance._meta.label_lower == "dcim.interface" and proposed.field_name == "tagged_vlans":
        scopes = {"switchport"}
    keys = (
        {key for key in owner_spec.resolver(instance, owner_spec) if key[1] in scopes}
        if canonical_fragment(instance, owner_spec) != ABSENT
        else set()
    )
    write = RendererWrite(
        operation="m2m_add",
        model_label=instance._meta.label_lower,
        pk=owner_identity,
        natural_key=(("field_name", proposed.field_name),),
        selected_pks=added_identities,
        values=(("through_model", through._meta.label_lower),),
    )
    return write, footprint, keys


def _plan_m2m_set(proposed: RendererM2MSet, creation_refs):
    instance = proposed.instance
    field = instance._meta.get_field(proposed.field_name)
    related_model = field.remote_field.model
    related_identities = _related_identities(proposed.related, creation_refs)
    before_pks = (
        tuple(sorted(getattr(instance, proposed.field_name).values_list("pk", flat=True)))
        if instance.pk is not None and not instance._state.adding
        else ()
    )
    additions = tuple(row for row in proposed.related if _creation_identity(row, creation_refs) not in before_pks)
    add_write, footprint, keys = _plan_m2m_add(
        RendererM2MAdd(
            instance=instance,
            field_name=proposed.field_name,
            related=additions,
        ),
        creation_refs,
    )
    write = RendererWrite(
        operation="m2m_set",
        model_label=add_write.model_label,
        pk=add_write.pk,
        natural_key=add_write.natural_key,
        selected_pks=related_identities,
        values=(("before_pks", before_pks), *add_write.values),
    )
    previous_related = related_model._default_manager.filter(pk__in=before_pks).order_by("pk")
    try:
        previous_footprints = tuple(footprint_for_instance(row) for row in previous_related)
    except KeyError as exc:
        raise IntentMutationProtocolError(f"unregistered M2M related model {exc.args[0]}") from exc
    footprint = MutationFootprint.merge(
        footprint,
        *previous_footprints,
    )
    if set(before_pks) == set(related_identities):
        keys = set()
    return write, footprint, keys


def _manifest_binding(instance):
    from .ownership_planner import converted_scope_rules

    label = instance._meta.label_lower
    for rule in converted_scope_rules().values():
        native_field = dict(rule.overlay_native_fields).get(label)
        if native_field is None:
            continue
        native = getattr(instance, native_field, None)
        management = getattr(instance, "management", None)
        if native is None or management is None:
            return None
        native_key = {name: _normal(getattr(native, name)) for name in rule.native_key_fields}
        return rule, management.device_id, native._meta.label_lower, native_key
    return None


def _maintain_manifest(instance):
    from . import status_machine as sm
    from .models import NSOOwnershipManifest

    binding = _manifest_binding(instance)
    if binding is None:
        return
    rule, device_id, native_model_label, native_key = binding
    identity = {
        "device_id": device_id,
        "scope": rule.scope,
        "native_model_label": native_model_label,
        "native_key": native_key,
    }
    if sm.is_owned(instance.status):
        NSOOwnershipManifest.objects.update_or_create(
            **identity,
            defaults={
                "ownership_state": "owned",
                "deletion_authority": rule.deletion_authority,
            },
        )
    else:
        NSOOwnershipManifest.objects.filter(**identity, ownership_state="owned").update(ownership_state="detached")


class RendererWriter:
    """Consume one frozen plan through exact ORM operations."""

    def __init__(self, plan: RendererMutationPlan, *, content: bool, permit):
        self.plan = plan
        self.content = content
        self.permit = permit
        self._consumed: set[int] = set()
        self._active_operation: int | None = None

    def _reference_matches(self, reference, related):
        if related is None or related._meta.label_lower != reference.model_label:
            return False
        return all(self._value_matches(related, attname, expected) for attname, expected in reference.natural_key)

    def _value_matches(self, instance, attname, expected):
        if isinstance(expected, RendererCreationRef):
            field = next(field for field in instance._meta.concrete_fields if field.attname == attname)
            return self._reference_matches(expected, getattr(instance, field.name, None))
        return _normal(getattr(instance, attname)) == expected

    def _fields_match(self, expected_values, instance):
        return all(self._value_matches(instance, attname, expected) for attname, expected in expected_values)

    def _identity_matches(self, write, instance):
        if write.pk is not None:
            return instance.pk == write.pk
        return self._fields_match(write.natural_key, instance)

    def _find_save(self, instance, update_fields, force_insert=False):
        normalized_fields = None if update_fields is None else tuple(sorted(set(update_fields)))
        for index, write in enumerate(self.plan.write_set):
            if index in self._consumed:
                continue
            if (
                write.operation == "save"
                and write.model_label == instance._meta.label_lower
                and self._identity_matches(write, instance)
                and write.update_fields == normalized_fields
                and self._fields_match(write.values, instance)
                and write.force_insert is bool(force_insert)
            ):
                if write.pk is not None:
                    current = type(instance)._default_manager.filter(pk=write.pk).first()
                    if current is None or not self._fields_match(write.before_values, current):
                        raise IntentPlanStaleError(f"{write.model_label} row {write.pk!r} changed after planning")
                return index
        raise IntentMutationProtocolError(
            f"save of {instance._meta.label_lower} row {instance.pk!r} is outside the frozen write set"
        )

    def _resolve_reference(self, reference):
        model = apps.get_model(reference.model_label)
        filters = {}
        for attname, expected in reference.natural_key:
            if isinstance(expected, RendererCreationRef):
                related = self._resolve_reference(expected)
                if related is None:
                    return None
                expected = related.pk
            filters[attname] = expected
        return model._default_manager.filter(**filters).first()

    def _resolve_creation(self, write):
        return self._resolve_reference(
            RendererCreationRef(model_label=write.model_label, natural_key=write.natural_key)
        )

    def _creation_matches(self, write, instance):
        spec = renderer_input_specs().get(write.model_label)
        if spec is None:
            return bool(write.natural_key) and self._fields_match(write.natural_key, instance)
        content_attnames = {spec.model._meta.get_field(field_name).attname for field_name in spec.content_fields}
        expected = tuple((attname, value) for attname, value in write.values if attname in content_attnames)
        return self._fields_match(expected, instance)

    def consume_existing_creation(self, instance) -> bool:
        """Consume a planned insert that a concurrent writer materialized first."""
        if instance.pk is None:
            return False
        index = next(
            (
                candidate
                for candidate, write in enumerate(self.plan.write_set)
                if candidate not in self._consumed
                and write.operation == "save"
                and write.force_insert
                and write.model_label == instance._meta.label_lower
                and self._identity_matches(write, instance)
            ),
            None,
        )
        if index is None:
            return False
        write = self.plan.write_set[index]
        if not self._creation_matches(write, instance):
            raise IntentPlanStaleError(f"{write.model_label} creation {write.natural_key!r} changed after planning")
        self._consumed.add(index)
        return True

    def consume_applied_save(self, instance) -> bool:
        """Consume a frozen update that another writer applied exactly."""
        for index, write in enumerate(self.plan.write_set):
            if (
                index in self._consumed
                or write.operation != "save"
                or write.force_insert
                or write.model_label != instance._meta.label_lower
                or not self._identity_matches(write, instance)
            ):
                continue
            current = type(instance)._default_manager.filter(pk=write.pk).first()
            expected = dict(write.before_values)
            expected.update(write.values)
            if current is not None and self._fields_match(tuple(expected.items()), current):
                self._consumed.add(index)
                return True
        return False

    def consume_applied_m2m_set(self, instance, field_name) -> bool:
        """Consume a frozen M2M replacement that another writer applied exactly."""
        identity = (("field_name", field_name),)
        for index, write in enumerate(self.plan.write_set):
            if (
                index in self._consumed
                or write.operation != "m2m_set"
                or write.model_label != instance._meta.label_lower
                or not self._owner_matches(write.pk, instance)
                or write.natural_key != identity
            ):
                continue
            expected = []
            for related_identity in write.selected_pks:
                if isinstance(related_identity, RendererCreationRef):
                    related = self._resolve_reference(related_identity)
                    if related is None:
                        return False
                    expected.append(related.pk)
                else:
                    expected.append(related_identity)
            current = tuple(sorted(getattr(instance, field_name).values_list("pk", flat=True)))
            if current == tuple(sorted(expected)):
                self._consumed.add(index)
                return True
        return False

    @contextlib.contextmanager
    def _operation(self, index):
        previous = self._active_operation
        self._active_operation = index
        try:
            yield
        finally:
            self._active_operation = previous

    def save(self, instance, *, update_fields=None, force_insert=False):
        """Execute one exact planned save."""
        index = self._find_save(instance, update_fields, force_insert)
        with self._operation(index):
            if not force_insert:
                instance.save(update_fields=update_fields)
            else:
                try:
                    with transaction.atomic():
                        instance.save(force_insert=True)
                except IntegrityError:
                    write = self.plan.write_set[index]
                    existing = self._resolve_creation(write) if write.natural_key else None
                    if existing is None or not self._creation_matches(write, existing):
                        raise
                    instance.pk = existing.pk
                    instance._state.adding = False
                    instance._state.db = existing._state.db
        _maintain_manifest(instance)
        self._consumed.add(index)
        return instance

    def delete(self, instance):
        """Execute one planned root delete and consume its Collector closure."""
        index = next(
            (
                candidate
                for candidate, write in enumerate(self.plan.write_set)
                if candidate not in self._consumed
                and write.operation == "delete"
                and not write.cascade
                and write.model_label == instance._meta.label_lower
                and write.pk == instance.pk
            ),
            None,
        )
        if index is None:
            raise IntentMutationProtocolError(
                f"delete of {instance._meta.label_lower} row {instance.pk!r} is outside the frozen write set"
            )
        root_write = self.plan.write_set[index]
        current = type(instance)._default_manager.filter(pk=instance.pk).first()
        if current is None or not self._fields_match(root_write.before_values, current):
            raise IntentPlanStaleError(f"{root_write.model_label} row {root_write.pk!r} changed after planning")
        closure, _footprint, _changed_keys = _collector_writes(current)
        matched = []
        available = [candidate for candidate in range(len(self.plan.write_set)) if candidate not in self._consumed]
        for expected in closure:
            candidate = next(
                (position for position in available if self.plan.write_set[position] == expected),
                None,
            )
            if candidate is None:
                break
            matched.append(candidate)
            available.remove(candidate)
        if len(matched) != len(closure) or index not in matched:
            raise IntentMutationProtocolError("the planned Collector cascade changed before delete")
        from django.db.models.deletion import Collector

        collector = Collector(using=instance._state.db or "default", origin=instance)
        collector.collect([instance])
        for (_field, _value), querysets in collector.field_updates.items():
            for rows in querysets:
                model, _materialized = _materialize_field_update_rows(rows)
                if model is None:
                    continue
                _authorize_dml(self.permit, model._meta.db_table)
        with self._operation(index):
            result = instance.delete()
        self._consumed.update(matched)
        return result

    def _owner_matches(self, expected, instance):
        if isinstance(expected, RendererCreationRef):
            return self._reference_matches(expected, instance)
        return instance.pk == expected

    def _selected_matches(self, expected, related):
        related = tuple(related)
        if len(expected) != len(related):
            return False
        return all(
            any(
                self._reference_matches(identity, row)
                if isinstance(identity, RendererCreationRef)
                else row.pk == identity
                for row in related
            )
            for identity in expected
        )

    def _find_m2m_add(self, instance, field_name, related):
        identity = (("field_name", field_name),)
        for index, write in enumerate(self.plan.write_set):
            if (
                index not in self._consumed
                and write.operation == "m2m_add"
                and write.model_label == instance._meta.label_lower
                and self._owner_matches(write.pk, instance)
                and write.natural_key == identity
                and self._selected_matches(write.selected_pks, related)
            ):
                return index
        raise IntentMutationProtocolError("the M2M add is outside the frozen write set")

    def m2m_add(self, instance, field_name, related):
        """Add exactly the related rows frozen into one planned M2M operation."""
        related = tuple(related)
        index = self._find_m2m_add(instance, field_name, related)
        with self._operation(index):
            getattr(instance, field_name).add(*related)
        _maintain_manifest(instance)
        self._consumed.add(index)

    def _find_m2m_set(self, instance, field_name, related):
        identity = (("field_name", field_name),)
        for index, write in enumerate(self.plan.write_set):
            if (
                index not in self._consumed
                and write.operation == "m2m_set"
                and write.model_label == instance._meta.label_lower
                and self._owner_matches(write.pk, instance)
                and write.natural_key == identity
                and self._selected_matches(write.selected_pks, related)
            ):
                return index
        raise IntentMutationProtocolError("the M2M set is outside the frozen write set")

    def m2m_set(self, instance, field_name, related):
        """Replace one M2M edge set with the exact planned persisted rows."""
        related = tuple(related)
        index = self._find_m2m_set(instance, field_name, related)
        write = self.plan.write_set[index]
        before_pks = dict(write.values)["before_pks"]
        current_pks = tuple(sorted(getattr(instance, field_name).values_list("pk", flat=True)))
        if current_pks != before_pks:
            raise IntentPlanStaleError("the M2M edge set changed after planning")
        with self._operation(index):
            getattr(instance, field_name).set(related)
        _maintain_manifest(instance)
        self._consumed.add(index)

    def set_update(self, model, operation: RendererWrite, **values):
        """Update only the row IDs frozen into one planned set operation."""
        index = next(
            (
                candidate
                for candidate, write in enumerate(self.plan.write_set)
                if candidate not in self._consumed and write is operation
            ),
            None,
        )
        normalized = tuple(
            sorted((model._meta.get_field(name).attname, _normal(value)) for name, value in values.items())
        )
        if (
            index is None
            or operation.operation != "set_update"
            or operation.model_label != model._meta.label_lower
            or operation.values != normalized
        ):
            raise IntentMutationProtocolError("set-based update is outside the frozen write set")
        _authorize_dml(self.permit, model._meta.db_table)
        updated = model._default_manager.filter(pk__in=operation.selected_pks).update(**values)
        if updated != len(operation.selected_pks):
            raise IntentPlanStaleError("the frozen set-based update lost a selected row")
        self._consumed.add(index)
        return updated

    def signal_write_is_authorized(self, instance, *, deleting, update_fields):
        """Validate that a registered model signal came from this writer object."""
        if self._active_operation is None:
            return False
        if deleting:
            return any(
                write.operation == "delete"
                and write.model_label == instance._meta.label_lower
                and write.pk == instance.pk
                for write in self.plan.write_set
            )
        write = self.plan.write_set[self._active_operation]
        normalized_fields = None if update_fields is None else tuple(sorted(set(update_fields)))
        return (
            write.operation == "save"
            and write.model_label == instance._meta.label_lower
            and self._identity_matches(write, instance)
            and write.update_fields == normalized_fields
            and self._fields_match(write.values, instance)
        )

    def signal_m2m_is_authorized(self, instance, action, field_name, pk_set) -> bool:
        """Validate that one M2M signal belongs to the active exact writer operation."""
        if self._active_operation is None or action not in {"pre_add", "pre_remove"}:
            return False
        write = self.plan.write_set[self._active_operation]
        if (
            write.model_label != instance._meta.label_lower
            or not self._owner_matches(write.pk, instance)
            or write.natural_key != (("field_name", field_name),)
        ):
            return False
        field = instance._meta.get_field(field_name)
        changed = field.remote_field.model._default_manager.filter(pk__in=pk_set or ()).order_by("pk")
        if write.operation == "m2m_add":
            return action == "pre_add" and self._selected_matches(write.selected_pks, changed)
        if write.operation != "m2m_set":
            return False
        before_pks = set(dict(write.values)["before_pks"])
        after_pks = set(write.selected_pks)
        expected = before_pks - after_pks if action == "pre_remove" else after_pks - before_pks
        return action in {"pre_remove", "pre_add"} and self._selected_matches(tuple(expected), changed)

    def assert_complete(self):
        remaining = [
            write
            for index, write in enumerate(self.plan.write_set)
            if index not in self._consumed and not write.cascade
        ]
        if remaining:
            raise IntentMutationProtocolError(f"renderer write plan left operations unused: {remaining!r}")


_ACTIVE_WRITER: contextvars.ContextVar[RendererWriter | None] = contextvars.ContextVar(
    "nso_renderer_writer", default=None
)


def active_renderer_writer() -> RendererWriter | None:
    """Return the current explicit writer, if this call entered one."""
    return _ACTIVE_WRITER.get()


def renderer_writer_owns_key(device_id, scope, *, content=False) -> bool:
    """Return whether the active writer owns one lock key and optionally bumped it."""
    writer = active_renderer_writer()
    if writer is None:
        return False
    key = (int(device_id), str(scope))
    if content:
        return writer.content and key in writer.plan.content_keys
    return key in writer.plan.lock_footprint.revision_keys


def require_planned_signal_write(instance, *, deleting=False, update_fields=None) -> None:
    """Refuse a registered save/delete that bypasses the active writer object."""
    writer = active_renderer_writer()
    if writer is not None and not writer.signal_write_is_authorized(
        instance,
        deleting=deleting,
        update_fields=update_fields,
    ):
        raise IntentMutationProtocolError(
            f"{instance._meta.label_lower} row {instance.pk!r} bypassed the active renderer writer"
        )


def require_planned_m2m_signal(instance, action, field_name, pk_set) -> None:
    """Refuse an M2M mutation that bypasses the active exact writer operation."""
    writer = active_renderer_writer()
    if writer is not None and not writer.signal_m2m_is_authorized(instance, action, field_name, pk_set):
        raise IntentMutationProtocolError(
            f"{instance._meta.label_lower} row {instance.pk!r} bypassed the active renderer writer"
        )


def _finalize_fingerprints(plan):
    from . import delivery
    from .models import NSODeviceManagement, NSOIntentRevision

    verified_at = timezone.now()
    for device_id, scope in plan.content_keys:
        adapter_device_id = (
            NSODeviceManagement.objects.filter(device_id=device_id).values_list("adapter_device_id", flat=True).first()
        )
        rendered = delivery.render(scope, device_id, adapter_device_id)
        revision = NSOIntentRevision.objects.get(device_id=device_id, scope=scope)
        revision.verified_revision = revision.revision
        revision.verified_fingerprint = delivery.canonical_fingerprint(rendered.payload)
        revision.verified_at = verified_at
        revision.save(update_fields=["verified_revision", "verified_fingerprint", "verified_at", "updated_at"])


@contextlib.contextmanager
def renderer_writes(plan: RendererMutationPlan):
    """Execute one content plan and atomically finalize every bumped fingerprint."""
    if not plan.content_keys:
        raise IntentMutationProtocolError("renderer_writes requires a content-changing plan")
    if active_renderer_writer() is not None:
        raise IntentMutationProtocolError("renderer writer contexts cannot nest")
    with _intent_transaction(
        plan.lock_footprint,
        bump_keys=plan.content_keys,
        repend_after=True,
    ) as permit:
        writer = RendererWriter(plan, content=True, permit=permit)
        token = _ACTIVE_WRITER.set(writer)
        try:
            yield writer
            writer.assert_complete()
            _finalize_fingerprints(plan)
        finally:
            _ACTIVE_WRITER.reset(token)


@contextlib.contextmanager
def renderer_mirror_writes(plan: RendererMutationPlan):
    """Execute one exact lifecycle/mirror plan without changing trusted fingerprints."""
    if plan.content_keys:
        raise IntentMutationProtocolError("renderer_mirror_writes cannot execute a content-changing plan")
    if active_renderer_writer() is not None:
        raise IntentMutationProtocolError("renderer writer contexts cannot nest")
    with mirror_transaction(plan.lock_footprint) as permit:
        writer = RendererWriter(plan, content=False, permit=permit)
        token = _ACTIVE_WRITER.set(writer)
        try:
            yield writer
            writer.assert_complete()
        finally:
            _ACTIVE_WRITER.reset(token)
