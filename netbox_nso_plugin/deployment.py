# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Fleet-wide quiescence for an intent-protocol deployment."""

from __future__ import annotations

import contextlib
import contextvars
import functools

from django.db import connection, transaction
from django.utils import timezone

# One PostgreSQL advisory-lock namespace for the deployment switch. Normal operations take
# a shared lock. Activation takes the exclusive lock and waits for every old operation.
_LOCK_KEY = 1_503_003_006
_bypass = contextvars.ContextVar("nso_intent_deployment_bypass", default=False)


class DeploymentQuiesced(RuntimeError):
    """An intent operation was refused while the deployment gate was active."""


def is_quiesced() -> bool:
    """Return whether the durable fleet switch is active."""
    from .models import NSOIntentDeploymentControl

    return NSOIntentDeploymentControl.objects.filter(pk=1).exists()


def _refuse(label: str) -> None:
    raise DeploymentQuiesced(f"{label} is blocked because the intent deployment is quiesced")


def lock_mutation() -> None:
    """Join the current transaction to the gate and refuse a new intent mutation."""
    if _bypass.get():
        return
    if not connection.in_atomic_block:
        raise RuntimeError("an intent mutation must join the deployment gate inside its transaction")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", [_LOCK_KEY])
    if is_quiesced():
        _refuse("intent mutation")


@contextlib.contextmanager
def operation(label: str):
    """Hold a shared session lock for one reconcile, resync, provision, or drain."""
    if _bypass.get():
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock_shared(%s)", [_LOCK_KEY])
    try:
        if is_quiesced():
            _refuse(label)
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock_shared(%s)", [_LOCK_KEY])


def guarded(label: str):
    """Decorate a complete intent operation with the shared deployment lock."""

    def _decorate(fn):
        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            with operation(label):
                return fn(*args, **kwargs)

        return _wrapped

    return _decorate


def _set_active(active: bool) -> bool:
    """Change the switch and return whether activation created it."""
    from .models import NSOIntentDeploymentControl

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [_LOCK_KEY])
    try:
        with transaction.atomic():
            if active:
                _control, created = NSOIntentDeploymentControl.objects.update_or_create(
                    pk=1,
                    defaults={"quiesced_at": timezone.now()},
                )
                return created
            NSOIntentDeploymentControl.objects.filter(pk=1).delete()
            return False
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [_LOCK_KEY])


def quiesce() -> bool:
    """Block new operations and return whether this call activated the gate."""
    return _set_active(True)


def resume() -> None:
    """Restore normal intent operation."""
    _set_active(False)


@contextlib.contextmanager
def gate_bypass():
    """Let the gate command make its one verification push while work stays blocked."""
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)
