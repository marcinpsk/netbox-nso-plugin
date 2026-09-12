# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Execute production management-row lifecycle mutations through the exact writer."""

from __future__ import annotations

from .renderer_writer import (
    RendererMutationPlan,
    planned_delete,
    planned_save,
    renderer_mirror_writes,
    renderer_writes,
)


def save_management(instance, *, update_fields=None, force_insert=False):
    """Save one management row with an exact precomputed mutation plan."""
    update_fields = _prepare_source_fence(instance, update_fields)
    if update_fields is None and instance.pk is not None and not instance._state.adding:
        update_fields = full_save_fields(instance)
    natural_key = ("device",) if instance.pk is None or instance._state.adding else ()
    plan = RendererMutationPlan.build(
        saves=(
            planned_save(
                instance,
                update_fields=update_fields,
                force_insert=force_insert,
                natural_key=natural_key,
            ),
        )
    )
    context = renderer_writes if plan.changes_content else renderer_mirror_writes
    with context(plan) as writer:
        writer.save(instance, update_fields=update_fields, force_insert=force_insert)
    return instance


def full_save_fields(instance) -> tuple[str, ...]:
    """Return full-save fields that exclude primary keys and monotone records."""
    protected = type(instance)._STALE_SAVE_PROTECTED_FIELDS
    return tuple(
        sorted(
            field.name
            for field in instance._meta.concrete_fields
            if not field.primary_key and field.name not in protected
        )
    )


def _prepare_source_fence(instance, update_fields):
    """Put a source-rekey fence in the frozen write that changes its source tuple."""
    if instance.pk is None or instance._state.adding:
        return update_fields
    selected = None if update_fields is None else set(update_fields)
    source_fields = {"nso_instance", "nso_instance_id", "nso_device_name"}
    if selected is not None and not selected & source_fields:
        return update_fields
    previous = (
        type(instance)
        .objects.filter(pk=instance.pk)
        .values_list("nso_instance_id", "nso_device_name", "source_rekey_pending")
        .first()
    )
    if previous is None:
        return update_fields
    if selected is None and previous[2]:
        instance.source_rekey_pending = True
    if previous[:2] == (instance.nso_instance_id, instance.nso_device_name):
        return update_fields
    instance.source_rekey_pending = True
    if selected is None:
        selected = set(full_save_fields(instance))
    selected.add("source_rekey_pending")
    return tuple(sorted(selected))


def delete_management(instance):
    """Delete one management row and its exact Collector closure through the writer."""
    plan = RendererMutationPlan.build(deletes=(planned_delete(instance),))
    context = renderer_writes if plan.changes_content else renderer_mirror_writes
    with context(plan) as writer:
        return writer.delete(instance)
