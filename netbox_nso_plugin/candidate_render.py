# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Bind unsaved overlay contributions to one read-only render."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from operator import attrgetter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import Model

    from .renderer_writer import RendererM2MSet


@dataclass(frozen=True)
class CandidateBatch:
    """Unsaved overlays and final M2M sets shared with the exact writer."""

    overlays: tuple[Model, ...] = ()
    m2m: tuple[RendererM2MSet, ...] = ()


_CANDIDATES: ContextVar[tuple[CandidateBatch, int] | None] = ContextVar("nso_render_candidates", default=None)


def overlay_identity(row):
    """Use the model's stable device/native/state uniqueness, without an overlay PK."""
    from .refusal_registry import OVERLAYS

    spec = OVERLAYS[row._meta.label_lower]
    device_id = attrgetter(spec.device_lookup.replace("__", "."))(row)
    fields = row._meta.unique_together[0]
    values = tuple(getattr(row, row._meta.get_field(name).attname) for name in fields)
    if device_id is None or any(value is None for value in values):
        raise ValueError("candidate identity requires persisted device and native anchors")
    return row._meta.label_lower, device_id, values


def _validate_batch(batch):
    identities = [overlay_identity(row) for row in batch.overlays]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate candidate identity")
    relations = set()
    for relation in batch.m2m:
        identity = overlay_identity(relation.instance)
        key = identity, relation.field_name
        if identity not in identities or key in relations:
            raise ValueError("planned relation requires one candidate and one final related set")
        field = relation.instance._meta.get_field(relation.field_name)
        if not field.many_to_many or any(
            not isinstance(row, field.related_model) or row.pk is None for row in relation.related
        ):
            raise ValueError("planned relation requires persisted related rows of the declared model")
        relations.add(key)


@contextmanager
def bind_candidates(batch, device_id):
    """Bind one validated batch and restore the enclosing render context."""
    if batch is not None:
        _validate_batch(batch)
    token = _CANDIDATES.set(None if batch is None else (batch, device_id))
    try:
        yield
    finally:
        _CANDIDATES.reset(token)


def _text_value_ranks(queryset, field, rows):
    """Rank candidate text with the real column's collation in the same database."""
    from django.db import connections

    connection = connections[queryset.db]
    column = connection.ops.quote_name(field.column)
    table = connection.ops.quote_name(queryset.model._meta.db_table)
    values = list(dict.fromkeys(getattr(row, field.attname) for row in rows))
    with connection.cursor() as cursor:
        # The empty column branch carries its collation into the candidate values.
        cursor.execute(
            f"SELECT value FROM (SELECT {column} AS value FROM {table} WHERE FALSE "
            "UNION ALL SELECT unnest(%s::text[])) AS candidate_values ORDER BY value",
            [values],
        )
        return {value: rank for rank, (value,) in enumerate(cursor.fetchall())}


def _ordered_rows(queryset, rows, persisted):
    """Keep native ordering and batch order for new PK-ordered rows."""
    ordering = queryset.query.order_by or queryset.model._meta.ordering
    for name in reversed(ordering):
        descending = name.startswith("-")
        field_name = name.lstrip("-")
        field = queryset.model._meta.pk if field_name == "pk" else queryset.model._meta.get_field(field_name)
        if field.is_relation:
            ids = {getattr(row, field.attname) for row in rows}
            ranks = {
                pk: rank
                for rank, pk in enumerate(
                    field.related_model.objects.using(queryset.db).filter(pk__in=ids).values_list("pk", flat=True)
                )
            }
            rows.sort(key=lambda row: ranks[getattr(row, field.attname)], reverse=descending)
        elif field.primary_key:
            rows.sort(key=lambda row: persisted.get(overlay_identity(row), row).pk or float("inf"), reverse=descending)
        elif field.get_internal_type() in {"CharField", "TextField"}:
            ranks = _text_value_ranks(queryset, field, rows)
            rows.sort(key=lambda row: ranks[getattr(row, field.attname)], reverse=descending)
        else:
            rows.sort(
                key=lambda row: (getattr(row, field.attname) is None, getattr(row, field.attname)), reverse=descending
            )
    return tuple(rows)


def render_rows(queryset, *, scope):
    """Merge device-scoped rows before ownership and caller qualification."""
    from .refusal_registry import OVERLAYS
    from .status_machine import OWNED_STATES

    context = _CANDIDATES.get()
    candidates = ()
    if context is not None:
        batch, device_id = context
        candidates = tuple(
            row
            for row in batch.overlays
            if row._meta.label_lower == queryset.model._meta.label_lower
            and overlay_identity(row)[1] == device_id
            and scope in OVERLAYS[row._meta.label_lower].delivery_keys
            and getattr(row, "dest_protocol", scope) == scope
        )
    if not candidates:
        return tuple(queryset.filter(status__in=OWNED_STATES))
    persisted = {overlay_identity(row): row for row in queryset}
    merged = dict(persisted)
    merged.update((overlay_identity(row), row) for row in candidates)
    rows = [row for row in merged.values() if row.status in OWNED_STATES]
    return _ordered_rows(queryset, rows, persisted)


def render_related(row, field_name):
    """Read a planned final M2M set without opening an unsaved row's manager."""
    context = _CANDIDATES.get()
    if context is not None:
        identity = overlay_identity(row)
        for relation in context[0].m2m:
            if (overlay_identity(relation.instance), relation.field_name) == (identity, field_name):
                return tuple(relation.related)
    if row.pk is None:
        identity = overlay_identity(row)
        fields = row._meta.unique_together[0]
        row = type(row).objects.filter(**dict(zip(fields, identity[2], strict=True))).first()
        if row is None:
            return ()
    return tuple(getattr(row, field_name).all())
