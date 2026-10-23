# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Fleet-wide quiescence for an intent-protocol deployment."""

from __future__ import annotations

import contextlib
import contextvars
import functools
from datetime import datetime

from django.db import OperationalError, connection, transaction
from django.utils import timezone

# One PostgreSQL advisory-lock namespace for the deployment switch. Normal operations take
# a shared lock. Activation takes the exclusive lock and waits for every old operation.
_LOCK_KEY = 1_503_003_006
_EXCLUSIVE_LOCK_TIMEOUT_MS = 60_000
_bypass = contextvars.ContextVar("nso_intent_deployment_bypass", default=False)


class DeploymentQuiesced(RuntimeError):
    """An intent operation was refused while the deployment gate was active."""


class DeploymentTransitionTimeout(RuntimeError):
    """The fleet switch could not acquire its exclusive lock before the deadline."""


def report_replaced_gate(stream) -> None:
    """Tell the operator that the gate this invocation created was replaced and stays quiesced."""
    if is_quiesced():
        stream.write(
            "The deployment gate stays quiesced: another invocation owns the gate now. Its owning operation must release it."
        )


def gate_recovery_guidance(rerun: str, *, created: bool) -> str:
    """Return recovery guidance that respects gate ownership."""
    if created:
        return f"Fix the cause, rerun {rerun}, then run nso_intent_deployment_gate --abort after it succeeds."
    return f"Fix the cause and rerun {rerun}. The gate's owning operation must release it."


def is_quiesced() -> bool:
    """Return whether the durable fleet switch is active."""
    from .models import NSOIntentDeploymentControl

    return NSOIntentDeploymentControl.objects.filter(pk=1).exists()


def is_paired_activation() -> bool:
    """Return whether the active gate guards a paired code activation."""
    from .models import NSOIntentDeploymentControl

    return NSOIntentDeploymentControl.objects.filter(pk=1, paired_activation=True).exists()


def _refuse(label: str) -> None:
    raise DeploymentQuiesced(f"{label} is blocked because the intent deployment is quiesced")


def _exclusive_transition_pending(cursor) -> bool:
    """Return whether activation is waiting for this advisory-lock key."""
    cursor.execute(
        "SELECT EXISTS ("
        "SELECT 1 FROM pg_locks "
        "WHERE locktype = 'advisory' AND classid = %s AND objid = %s "
        "AND objsubid = 1 AND mode = 'ExclusiveLock' AND NOT granted "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ")",
        [_LOCK_KEY >> 32, _LOCK_KEY & 0xFFFFFFFF],
    )
    return cursor.fetchone()[0]


def lock_mutation() -> None:
    """Join the current transaction to the gate and refuse a new intent mutation."""
    if _bypass.get():
        return
    if not connection.in_atomic_block:
        raise RuntimeError("an intent mutation must join the deployment gate inside its transaction")
    with connection.cursor() as cursor:
        if _exclusive_transition_pending(cursor):
            _refuse("intent mutation")
        cursor.execute("SELECT pg_try_advisory_xact_lock_shared(%s)", [_LOCK_KEY])
        acquired = cursor.fetchone()[0]
        transition_pending = acquired and _exclusive_transition_pending(cursor)
    if not acquired:
        _refuse("intent mutation")
    if transition_pending:
        transaction.set_rollback(True)
        _refuse("intent mutation")
    if is_quiesced():
        _refuse("intent mutation")


@contextlib.contextmanager
def operation(label: str):
    """Hold a shared session lock for one reconcile, resync, provision, or drain."""
    if _bypass.get():
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock_shared(%s)", [_LOCK_KEY])
        acquired = cursor.fetchone()[0]
        transition_pending = acquired and _exclusive_transition_pending(cursor)
        if transition_pending:
            cursor.execute("SELECT pg_advisory_unlock_shared(%s)", [_LOCK_KEY])
    if not acquired:
        _refuse(label)
    if transition_pending:
        _refuse(label)
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


@contextlib.contextmanager
def exclusive_transition():
    """Hold the exclusive switch lock in this session; nested holds in the same session are reentrant."""
    acquired = False
    try:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    [f"{_EXCLUSIVE_LOCK_TIMEOUT_MS}ms"],
                )
                cursor.execute("SELECT pg_advisory_lock(%s)", [_LOCK_KEY])
                acquired = True
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) == "55P03":
                raise DeploymentTransitionTimeout("Timed out waiting for active intent operations to finish") from None
            raise
        yield
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [_LOCK_KEY])


def quiesce(*, paired_activation: bool = False) -> datetime | None:
    """Block new operations; return the new gate's quiesced_at, or None when a gate already existed.

    An existing gate keeps its quiesced_at, and no call removes the paired mark.
    """
    from .models import NSOIntentDeploymentControl

    with exclusive_transition(), transaction.atomic():
        control, created = NSOIntentDeploymentControl.objects.get_or_create(
            pk=1,
            defaults={"quiesced_at": timezone.now()},
        )
        if paired_activation:
            NSOIntentDeploymentControl.objects.filter(pk=1).update(paired_activation=True)
        return control.quiesced_at if created else None


def resume(*, paired_verified: bool = False, created_at: datetime | None = None) -> None:
    """Restore normal intent operation.

    A paired-activation gate needs ``paired_verified``. With ``created_at``, release only that
    ordinary gate instance and leave a replaced or promoted gate in place.
    """
    from .models import NSOIntentDeploymentControl

    with exclusive_transition(), transaction.atomic():
        control = NSOIntentDeploymentControl.objects.select_for_update().filter(pk=1).first()
        if control is None:
            return
        if created_at is not None and (control.quiesced_at != created_at or control.paired_activation):
            return
        if control.verification_attempt_id is not None:
            raise DeploymentQuiesced("Revoke verification probe authority before resume")
        if control.paired_activation and not paired_verified:
            raise DeploymentQuiesced("A paired-activation gate resumes only after paired verification")
        control.delete()


@contextlib.contextmanager
def gate_bypass():
    """Let the gate command make its one verification push while work stays blocked."""
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)
