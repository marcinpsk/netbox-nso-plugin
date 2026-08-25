# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Serialize Apply promotion with the intent transactions it represents."""

from django.db import connection, transaction
from django.utils import timezone


class IntentChangedDuringPreparation(Exception):
    """The current NetBox intent no longer matches a stored Apply receipt."""


class InterfaceIntentLockStale(RuntimeError):
    """The interface moved after its device-scoped dependency locks were chosen."""


class DeferredIntentFieldStale(RuntimeError):
    """A field loaded without its baseline changed before the row was locked."""


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

APPLY_INTENT_FIELD_NAMES = {
    "vlan": frozenset({"vlan"}),
    "svi": frozenset({"interface", "vlan", "svi_type", "vrf"}),
    "subinterface": frozenset({"interface", "parent_interface", "dot1q_vlan", "vrf"}),
    "bfd": frozenset({"interface", "min_tx", "min_rx", "multiplier", "micro_bfd"}),
    "interface_mtu": frozenset({"interface", "l2_mtu", "ip_mtu", "mpls_mtu"}),
    "route_policy": frozenset({"content_type", "object_id", "family", "object_name"}),
    "static_route": frozenset({"static_route", "intent_generation"}),
    "l2_sap": frozenset({"service_name", "service_type", "sap_id", "port", "outer_tag", "inner_tag"}),
    "logging": frozenset({"console_severity", "monitor_severity", "module_severity"}),
}

_DEVICE_INTENT_LOCK_NAMESPACE = 1_503_003_007
_VLAN_INTENT_LOCK_NAMESPACE = 1_503_003_008
_DEVICE_VLAN_MEMBERSHIP_LOCK_NAMESPACE = 1_503_003_009
_VLAN_MEMBERSHIP_LOCK_NAMESPACE = 1_503_003_010
_VLAN_RESCOPE_LOCK_NAMESPACE = 1_503_003_011


def lock_device_intent_transaction(device_id: int) -> None:
    """Serialize one device's reconciliation bodies with native intent edits."""
    if not connection.in_atomic_block:
        raise RuntimeError("a device intent lock must be held inside a transaction")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_DEVICE_INTENT_LOCK_NAMESPACE, device_id],
        )


def lock_vlan_intent_transaction(vlan_id: int) -> None:
    """Serialize one shared VLAN with attachment and reconciliation transactions."""
    if not connection.in_atomic_block:
        raise RuntimeError("a VLAN intent lock must be held inside a transaction")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_VLAN_INTENT_LOCK_NAMESPACE, vlan_id],
        )


def lock_device_vlan_membership_transaction(device_id: int) -> None:
    """Keep a device's VLAN attachments stable while dependency locks are chosen."""
    if not connection.in_atomic_block:
        raise RuntimeError("a device VLAN membership lock must be held inside a transaction")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_DEVICE_VLAN_MEMBERSHIP_LOCK_NAMESPACE, device_id],
        )


def lock_vlan_membership_transaction(vlan_id: int) -> None:
    """Serialize attachment-set changes for one native VLAN."""
    if not connection.in_atomic_block:
        raise RuntimeError("a VLAN membership lock must be held inside a transaction")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_VLAN_MEMBERSHIP_LOCK_NAMESPACE, vlan_id],
        )


def lock_vlan_rescope_transaction(state_id: int) -> None:
    """Serialize supported rescope requests for one VLAN overlay."""
    if not connection.in_atomic_block:
        raise RuntimeError("a VLAN rescope lock must be held inside a transaction")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_VLAN_RESCOPE_LOCK_NAMESPACE, state_id],
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


def lock_native_vlan_dependency_rows(device_id: int, collect_vlan_ids) -> None:
    """Fence membership, discover dependencies, then lock their intent and rows."""
    from ipam.models import VLAN

    lock_device_vlan_membership_transaction(device_id)
    ordered_vlan_ids = sorted(set(collect_vlan_ids()))
    for vlan_id in ordered_vlan_ids:
        lock_vlan_intent_transaction(vlan_id)
    list(VLAN.objects.select_for_update(of=("self",)).filter(pk__in=ordered_vlan_ids).order_by("pk"))


def mark_explicit_accept(instance) -> None:
    """Mark one save as an intentional transition into owned pending state."""
    instance._nso_explicit_accept = True


def remember_loaded_status(instance) -> None:
    """Record the status represented by this in-memory instance."""
    if "status" in instance.__dict__:
        instance._nso_loaded_status = instance.__dict__["status"]


def deploying_models() -> dict:
    """Return the delivery scopes whose owned rows carry an Apply-in-flight state."""
    from . import models

    return {scope: getattr(models, name) for scope, name in APPLY_DEPLOYING_MODEL_NAMES.items()}


def capture_intent_field_change(instance, scope, *, update_fields=None) -> None:
    """Remember whether this save changes fields rendered for one Apply scope."""
    instance._nso_intent_previous_status = None
    instance._nso_intent_forced_status = None
    explicit_accept = bool(getattr(instance, "_nso_explicit_accept", False)) and instance.status == "accepted"
    instance._nso_explicit_accept = False
    model = deploying_models().get(scope)
    if model is None or not isinstance(instance, model) or instance._state.adding or instance.pk is None:
        return

    field_names = APPLY_INTENT_FIELD_NAMES[scope]
    if update_fields is not None:
        field_names = field_names.intersection(update_fields)
    status_needs_baseline = not hasattr(instance, "_nso_loaded_status") and (
        update_fields is None or "status" in update_fields
    )
    if not field_names and not status_needs_baseline:
        return
    if not connection.in_atomic_block:
        raise RuntimeError("an intent field edit must be saved inside a transaction")

    fields = [model._meta.get_field(name) for name in field_names]
    attnames = [field.attname for field in fields]
    current = model.objects.select_for_update(of=("self",)).filter(pk=instance.pk).values("status", *attnames).first()
    if current is not None:
        from .status_machine import is_owned

        intent_changed = any(current[field.attname] != getattr(instance, field.attname) for field in fields)
        current_status = current["status"]
        loaded_status = getattr(instance, "_nso_loaded_status", None)
        if loaded_status is None:
            deferred_status = instance.__dict__.get("status", current_status)
            if not explicit_accept and deferred_status != current_status:
                raise DeferredIntentFieldStale("deferred intent status changed before save")
            loaded_status = current_status
            if "status" not in instance.__dict__:
                instance.status = current_status
        stale_status = loaded_status != current_status
        status_edited = instance.status != loaded_status
        if intent_changed:
            instance._nso_intent_previous_status = current_status
            if not explicit_accept and stale_status and not status_edited:
                instance.status = current_status if is_owned(current_status) else "changed"
                if instance.status == "changed":
                    instance._nso_intent_forced_status = "changed"
        elif not explicit_accept and stale_status and not status_edited:
            instance.status = current_status


def repend_changed_row(instance, scope) -> None:
    """Re-pend only the owned row changed by the current intent transaction."""
    from .status_machine import is_owned

    model = deploying_models().get(scope)
    if model is None or not isinstance(instance, model) or instance.pk is None:
        return
    forced_status = getattr(instance, "_nso_intent_forced_status", None)
    if forced_status is not None:
        model.objects.filter(pk=instance.pk).exclude(status=forced_status).update(status=forced_status)
        instance.status = forced_status
    elif is_owned(getattr(instance, "_nso_intent_previous_status", None)) and is_owned(instance.status):
        model.objects.filter(pk=instance.pk).exclude(status="accepted").update(status="accepted")
        instance.status = "accepted"


def lock_vlan_intent_rows(vlan_id, scopes) -> tuple[object | None, dict[str, list]]:
    """Lock shared VLAN intent in the same order as Apply promotion."""
    from django.db.models import Q
    from ipam.models import VLAN

    from .deployment import lock_mutation
    from .models import NSODeviceManagement, NSOSVIState, NSOSwitchportState, NSOVLANState

    requested = tuple(scope for scope in ("vlan", "svi", "switchport") if scope in scopes)
    state_queries = {
        "vlan": NSOVLANState.objects.filter(vlan_id=vlan_id),
        "svi": NSOSVIState.objects.filter(vlan_id=vlan_id),
        "switchport": NSOSwitchportState.objects.filter(
            pk__in=NSOSwitchportState.objects.filter(Q(untagged_vlan_id=vlan_id) | Q(tagged_vlans__id=vlan_id)).values(
                "pk"
            )
        ),
    }
    candidates = {scope: state_queries[scope].order_by("pk") for scope in requested}
    empty = {scope: [] for scope in requested}
    if not any(queryset.exists() for queryset in candidates.values()):
        return None, empty
    if not connection.in_atomic_block:
        raise RuntimeError("VLAN intent edit must be saved inside a transaction")

    lock_mutation()
    lock_vlan_intent_transaction(vlan_id)
    vlan = VLAN.objects.select_for_update(of=("self",)).filter(pk=vlan_id).first()
    if vlan is None:
        return None, empty
    management_devices = dict(
        NSODeviceManagement.objects.filter(
            pk__in={
                management_id
                for queryset in candidates.values()
                for management_id in queryset.values_list("management_id", flat=True)
            }
        ).values_list("pk", "device_id")
    )
    management_ids = sorted(management_devices)
    if not management_ids:
        return vlan, empty

    for device_id in sorted(management_devices.values()):
        lock_device_intent_transaction(device_id)
    list(NSODeviceManagement.objects.select_for_update(of=("self",)).filter(pk__in=management_ids).order_by("pk"))
    rows = {
        scope: list(queryset.select_for_update(of=("self",)).select_related("management"))
        for scope, queryset in candidates.items()
    }
    return vlan, rows


def lock_interface_intent_rows(interface_id) -> tuple[object | None, object | None, dict[str, list]]:
    """Lock a native interface before every intent row whose payload renders its name."""
    from dcim.models import Interface
    from django.db.models import Q

    from .deployment import lock_mutation
    from .models import (
        NSOBFDInterfaceState,
        NSOBGPPeerState,
        NSODeviceManagement,
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

    candidates = {
        "interface": [NSOInterfaceState.objects.filter(interface_id=interface_id).order_by("pk")],
        "ip": [
            NSOInterfaceIPState.objects.filter(
                Q(interface_id=interface_id) | Q(interface__parent_id=interface_id)
            ).order_by("pk")
        ],
        "svi": [NSOSVIState.objects.filter(interface_id=interface_id).order_by("pk")],
        "subinterface": [
            NSOSubinterfaceState.objects.filter(
                Q(interface_id=interface_id) | Q(parent_interface_id=interface_id)
            ).order_by("pk")
        ],
        "bfd": [NSOBFDInterfaceState.objects.filter(interface_id=interface_id).order_by("pk")],
        "interface_mtu": [NSOInterfaceMtuState.objects.filter(interface_id=interface_id).order_by("pk")],
        "isis": [NSOISISInterfaceState.objects.filter(interface_id=interface_id).order_by("pk")],
        "ospf": [NSOOSPFInterfaceState.objects.filter(interface_id=interface_id).order_by("pk")],
        "lacp": [
            NSOLACPBundleState.objects.filter(interface_id=interface_id).order_by("pk"),
            NSOLACPMemberState.objects.filter(Q(interface_id=interface_id) | Q(lag_bundle_id=interface_id)).order_by(
                "pk"
            ),
        ],
        "switchport": [NSOSwitchportState.objects.filter(interface_id=interface_id).order_by("pk")],
    }
    empty = {scope: [] for scope in candidates}
    current = Interface.objects.filter(pk=interface_id).values("device_id").first()
    if current is None or not NSODeviceManagement.objects.filter(device_id=current["device_id"]).exists():
        return None, None, empty
    if not connection.in_atomic_block:
        raise RuntimeError("an interface intent edit must be saved inside a transaction")

    lock_mutation()
    lock_device_intent_transaction(current["device_id"])
    interface = Interface.objects.select_for_update(of=("self",)).filter(pk=interface_id).first()
    if interface is None:
        return None, None, empty
    if interface.device_id != current["device_id"]:
        raise InterfaceIntentLockStale("interface changed devices while acquiring intent locks")
    list(Interface.objects.select_for_update(of=("self",)).filter(parent_id=interface_id).order_by("pk"))
    management = (
        NSODeviceManagement.objects.select_for_update(of=("self",))
        .filter(device_id=interface.device_id)
        .order_by("pk")
        .first()
    )
    if management is None:
        return interface, None, empty
    locked_bgp_states = list(
        NSOBGPPeerState.objects.select_for_update(of=("self",))
        .filter(management=management)
        .select_related("bgp_peer")
        .order_by("pk")
    )
    rows = {
        scope: [state for queryset in querysets for state in queryset.select_for_update(of=("self",))]
        for scope, querysets in candidates.items()
    }
    rows["bgp"] = [
        state
        for state in locked_bgp_states
        if state.bgp_peer is not None and state.bgp_peer.update_source_id == interface_id
    ]
    return interface, management, rows


def lock_intent_revisions(device_id: int, scopes) -> dict[str, int]:
    """Lock and return one device's durable scope revisions in canonical order."""
    from .models import NSOIntentRevision

    ordered_scopes = tuple(sorted(scopes))
    for scope in ordered_scopes:
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
    from .models import NSOApplyAttempt, NSODeviceManagement, NSOStaticRouteState

    expected_scopes = {entry.key for entry in registry.values() if entry.in_protocol}
    if set(pushed) != expected_scopes or any(type(snapshot.revision) is not int for snapshot in pushed.values()):
        raise IntentChangedDuringPreparation

    with transaction.atomic():
        lock_device_intent_transaction(management.device_id)
        locked = NSODeviceManagement.objects.select_for_update(of=("self",)).order_by().get(pk=management.pk)
        if locked.adapter_device_id != management.adapter_device_id or locked.source_rekey_pending:
            raise IntentChangedDuringPreparation
        current = lock_intent_revisions(locked.device_id, expected_scopes)
        if any(current[scope] != snapshot.revision for scope, snapshot in pushed.items()):
            raise IntentChangedDuringPreparation

        locked_rows: list[tuple] = []
        for scope, model in deploying_models().items():
            if model is NSOStaticRouteState and not static_route_stored:
                continue
            locked_statuses = list(
                model.objects.select_for_update(of=("self",))
                .filter(management=locked, status__in=(sm.ACCEPTED, sm.APPLY_FAILED))
                .order_by("pk")
                .values_list("pk", "status")
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
                pks = [pk for pk, status in rows if status == previous_status]
                if not pks:
                    continue
                model.objects.filter(pk__in=pks, status=previous_status).update(
                    status=sm.advance(previous_status, sm.APPLY),
                    apply_attempt_id=attempt.pk,
                    last_apply_at=now,
                    last_apply_error="",
                )
                moved.append((section, model, pks, previous_status))
    return attempt, moved
