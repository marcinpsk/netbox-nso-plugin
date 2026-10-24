# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve native dependencies selected by the current operator."""

from .native_authorization import operator_created, operator_user, refuse_operator_write


def operator_queryset(model):
    """Restrict operator selections without filtering system observations or stored rendering reads."""
    queryset = model._default_manager.all()
    user = operator_user()
    return queryset.restrict(user, "view") if user is not None else queryset


def require_operator_view(row):
    """Require view on a selected existing row; the operator's own new rows retain add authority."""
    if (
        row.pk is not None
        and not row._state.adding
        and operator_user() is not None
        and not operator_created(row)
        and not operator_queryset(type(row)).filter(pk=row.pk).exists()
    ):
        refuse_operator_write(f"You cannot select this {row._meta.verbose_name}.")
    return row


def operator_lookup(model, *, create_if_missing=False, **filters):
    """Find a visible dependency, and refuse a hidden collision before create-if-missing."""
    row = operator_queryset(model).filter(**filters).first()
    if row is not None or operator_user() is None:
        return row
    existing = model._default_manager.filter(**filters).first()
    if existing is not None:
        if operator_created(existing):
            return existing
        if create_if_missing:
            refuse_operator_write(f"You cannot select or create this {model._meta.verbose_name}.")
    return None
