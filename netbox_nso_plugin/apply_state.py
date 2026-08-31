# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Serialize Apply promotion with the intent transactions it represents."""

import contextlib
import contextvars
import hashlib

from django.db import connection, transaction
from django.utils import timezone


class IntentChangedDuringPreparation(Exception):
    """The current NetBox intent no longer matches a stored Apply receipt."""


APPLY_DEPLOYING_MODEL_NAMES = {
    "vlan": "NSOVLANState",
    "svi": "NSOSVIState",
    "subinterface": "NSOSubinterfaceState",
    "bfd": "NSOBFDInterfaceState",
    "interface_mtu": "NSOInterfaceMtuState",
    "route_policy": "NSORoutePolicyState",
    "static_route": "NSOStaticRouteState",
    "l2_sap": "NSOL2SapState",
    "logging": "NSOLoggingLevelState",
}

_DEVICE_INTENT_LOCK_NAMESPACE = 1_503_003_007
_SHARED_DEPENDENCY_LOCK_NAMESPACE = 1_503_003_008


class LockOrderViolation(RuntimeError):
    """A lock acquisition moved backwards in the declared L2-L8 order."""


_LOCK_ORDER: contextvars.ContextVar[tuple[int, object] | None] = contextvars.ContextVar(
    "nso_intent_lock_order", default=None
)


@contextlib.contextmanager
def lock_order_scope():
    """Enable the development lock-order assertion for one complete footprint."""
    token = _LOCK_ORDER.set((0, None))
    try:
        yield
    finally:
        _LOCK_ORDER.reset(token)


def _enter_level(level: int, key) -> None:
    """Reject descending levels or keys while a footprint trace is active."""
    marker = _LOCK_ORDER.get()
    if marker is None:
        return
    previous_level, previous_key = marker
    if level < previous_level or (level == previous_level and previous_key is not None and key < previous_key):
        raise LockOrderViolation(f"intent lock order moved from L{previous_level} {previous_key!r} to L{level} {key!r}")
    _LOCK_ORDER.set((level, key))


def lock_device_intent_transaction(device_id: int) -> None:
    """Serialize one device's reconciliation bodies with native intent edits."""
    if not connection.in_atomic_block:
        raise RuntimeError("a device intent lock must be held inside a transaction")
    _enter_level(4, int(device_id))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_DEVICE_INTENT_LOCK_NAMESPACE, device_id],
        )


def lock_shared_dependencies(keys) -> None:
    """Lock immutable shared renderer roots in canonical order."""
    if not connection.in_atomic_block:
        raise RuntimeError("shared dependency locks require a transaction")
    with connection.cursor() as cursor:
        for kind, key in sorted(set(keys)):
            canonical = f"{kind}:{key}"
            digest = hashlib.sha256(canonical.encode()).digest()[:4]
            lock_key = int.from_bytes(digest, byteorder="big", signed=True)
            _enter_level(3, (kind, key))
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [_SHARED_DEPENDENCY_LOCK_NAMESPACE, lock_key],
            )


def vlan_ids_for_dependency_lock(items, *field_names: str) -> set[int]:
    """Return only VLAN IDs that are safe to use for pre-validation locks."""
    if not isinstance(items, list):
        return set()
    fields = field_names or ("vlan_id",)
    vlan_ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for field_name in fields:
            values = item.get(field_name)
            if not isinstance(values, list):
                values = [values]
            for value in values:
                try:
                    vlan_ids.add(int(value))
                except (TypeError, ValueError):
                    continue
    return vlan_ids


def vlan_intent_targets(vlan_id, scopes) -> tuple[set[int], dict[str, list]]:
    """Discover the managed devices and overlay rows affected by one VLAN."""
    from django.db.models import Q

    from .models import NSOSVIState, NSOSwitchportState, NSOVLANState

    requested = tuple(scope for scope in ("vlan", "svi", "switchport") if scope in scopes)
    queries = {
        "vlan": NSOVLANState.objects.filter(vlan_id=vlan_id),
        "svi": NSOSVIState.objects.filter(vlan_id=vlan_id),
        "switchport": NSOSwitchportState.objects.filter(
            pk__in=NSOSwitchportState.objects.filter(Q(untagged_vlan_id=vlan_id) | Q(tagged_vlans__id=vlan_id)).values(
                "pk"
            )
        ),
    }
    rows = {scope: list(queries[scope].select_related("management").order_by("pk")) for scope in requested}
    device_ids = {row.management.device_id for states in rows.values() for row in states}
    return device_ids, rows


def interface_intent_targets(interface_id) -> tuple[set[int], set[str]]:
    """Discover the device and renderer scopes whose payload contains an interface."""
    from dcim.models import Interface

    interface = Interface.objects.filter(pk=interface_id).values("device_id").first()
    if interface is None:
        return set(), set()
    from .models import NSODeviceManagement

    if not NSODeviceManagement.objects.filter(device_id=interface["device_id"]).exists():
        return set(), set()
    return {interface["device_id"]}, {
        "interface",
        "ip",
        "svi",
        "subinterface",
        "bfd",
        "interface_mtu",
        "isis",
        "bgp",
        "ospf",
        "lacp",
        "switchport",
    }


def deploying_models() -> dict:
    """Return the delivery scopes whose owned rows carry an Apply-in-flight state."""
    from . import models

    return {scope: getattr(models, name) for scope, name in APPLY_DEPLOYING_MODEL_NAMES.items()}


def lock_intent_revisions(device_id: int, scopes) -> dict[str, int]:
    """Lock and return one device's durable scope revisions in canonical order."""
    from .models import NSOIntentRevision

    ordered_scopes = tuple(sorted(scopes))
    for scope in ordered_scopes:
        _enter_level(7, (int(device_id), scope))
        NSOIntentRevision.objects.get_or_create(device_id=device_id, scope=scope)
    rows = (
        NSOIntentRevision.objects.select_for_update(of=("self",))
        .filter(device_id=device_id, scope__in=ordered_scopes)
        .order_by("scope")
    )
    current = {row.scope: int(row.revision) for row in rows}
    if set(current) != set(ordered_scopes):
        raise IntentChangedDuringPreparation
    return current


def promote_current_intent(
    management,
    registry,
    pushed,
    *,
    apply_attempt_id,
    static_route_stored: bool,
):
    """CAS stored receipt revisions, create the attempt, and stamp its rows."""
    from . import status_machine as sm
    from .intent_state import OVERLAY_MODEL_RANKS, mirror_refresh
    from .models import NSOApplyAttempt, NSODeviceManagement, NSOStaticRouteState
    from .signals import suppress_intent_push

    expected_scopes = {entry.key for entry in registry.values() if entry.in_protocol}
    if set(pushed) != expected_scopes or any(type(snapshot.revision) is not int for snapshot in pushed.values()):
        raise IntentChangedDuringPreparation

    with transaction.atomic(), lock_order_scope():
        lock_device_intent_transaction(management.device_id)
        _enter_level(5, management.pk)
        locked = NSODeviceManagement.objects.select_for_update(of=("self",)).order_by().get(pk=management.pk)
        if locked.adapter_device_id != management.adapter_device_id or locked.source_rekey_pending:
            raise IntentChangedDuringPreparation
        current = lock_intent_revisions(locked.device_id, expected_scopes)
        if any(current[scope] != snapshot.revision for scope, snapshot in pushed.items()):
            raise IntentChangedDuringPreparation

        locked_rows: list[tuple] = []
        model_ranks = {label: rank for rank, label in enumerate(OVERLAY_MODEL_RANKS)}
        for scope, model in sorted(
            deploying_models().items(),
            key=lambda item: model_ranks[item[1]._meta.label_lower],
        ):
            if model is NSOStaticRouteState and not static_route_stored:
                continue
            _enter_level(8, (model_ranks[model._meta.label_lower], 0))
            locked_statuses = list(
                model.objects.select_for_update(of=("self",))
                .filter(management=locked, status__in=(sm.ACCEPTED, sm.APPLY_FAILED))
                .order_by("pk")
            )
            locked_rows.append((scope, model, locked_statuses))

        selected = {
            registry[scope].section: pushed[scope].push_seq
            for scope in sorted(pushed, key=lambda candidate: registry[candidate].section)
        }
        attempt = NSOApplyAttempt.objects.create(
            id=apply_attempt_id,
            management=locked,
            adapter_device_id=locked.adapter_device_id,
            scope_revisions=current,
            selected=selected,
        )

        moved: list[tuple] = []
        now = timezone.now()
        for scope, model, rows in locked_rows:
            section = registry[scope].section
            for previous_status in (sm.ACCEPTED, sm.APPLY_FAILED):
                selected_rows = [row for row in rows if row.status == previous_status]
                if not selected_rows:
                    continue
                pks = []
                for row in selected_rows:
                    fields = {"status", "apply_attempt_id", "last_apply_at", "last_apply_error"}
                    with suppress_intent_push(), mirror_refresh(row, fields) as locked:
                        if locked is None:
                            continue
                        locked.status = sm.advance(previous_status, sm.APPLY)
                        locked.apply_attempt_id = attempt.pk
                        locked.last_apply_at = now
                        locked.last_apply_error = ""
                        locked.save(update_fields=fields)
                        pks.append(locked.pk)
                if pks:
                    moved.append((section, model, pks, previous_status))
    return attempt, moved
