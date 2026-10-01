# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Create owned test fixtures through the real acquisition writer."""

from netbox_nso_plugin.ownership_grants import OwnershipGrant
from netbox_nso_plugin.renderer_writer import (
    RendererMutationPlan,
    planned_save,
    renderer_mirror_writes,
    renderer_writes,
)


def acquire_overlay(model, **values):
    return save_overlay_fixture(model(**values))


def save_overlay_fixture(instance, *, update_fields=None):
    from ._outbox_case import without_commit_drain

    created = instance.pk is None or instance._state.adding
    natural_key = next(iter(instance._meta.unique_together), ())
    if not natural_key:
        natural_key = tuple(
            field.name for field in instance._meta.concrete_fields if field.unique and field.is_relation
        )
    plan = RendererMutationPlan.build(
        saves=(planned_save(instance, update_fields=update_fields, force_insert=created, natural_key=natural_key),),
        grant=OwnershipGrant("create"),
        settles_deploying=False,
    )
    mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
    with without_commit_drain(), mutation(plan) as writer:
        writer.save(instance, update_fields=update_fields, force_insert=created)
    return instance


def update_or_acquire_overlay(model, *, defaults, **lookup):
    import copy

    current = model.objects.filter(**lookup).first()
    if current is None:
        return acquire_overlay(model, **(lookup | defaults)), True
    candidate = copy.copy(current)
    for name, value in defaults.items():
        setattr(candidate, name, value)
    return save_overlay_fixture(candidate, update_fields=tuple(defaults)), False
