# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Detect and repair rendered intent that bypassed the explicit writer."""

from __future__ import annotations

import copy
import dataclasses
import logging
import time

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone

from . import delivery, outbox
from .intent_state import (
    OVERLAY_MODEL_RANKS,
    MutationFootprint,
    audit_scope_footprint,
    mirror_transaction,
    renderer_input_specs,
)
from .renderer_writer import RendererMutationPlan, consume_renderer_plan, planned_save

logger = logging.getLogger(__name__)

_DEFAULT_SCOPE_BATCH_CAP = 18
_DEFAULT_TICK_BUDGET_SECONDS = 240.0
_REPAIR_ATTEMPTS = 3
_SERIALIZATION_FAILURE = "40001"


class RendererAuditBudgetExceeded(RuntimeError):
    """A pre-capture audit could not verify its complete requested scope set."""


class RendererAuditRepairFailed(RuntimeError):
    """A pre-capture audit could not establish a trusted renderer baseline."""


@dataclasses.dataclass(frozen=True)
class RendererAuditResult:
    """The complete outcome of one bounded renderer audit."""

    audited: tuple[str, ...]
    repaired: tuple[str, ...]
    deferred: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class RendererFleetAuditResult:
    """The bounded outcome of one periodic fleet pass."""

    devices: int
    repaired: int
    deferred: int
    unknown: int
    failed: int


def _setting(name, default, cast):
    value = settings.PLUGINS_CONFIG.get("netbox_nso_plugin", {}).get(name, default)
    value = cast(value)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _device_filter(model, device_id):
    fields = {field.name for field in model._meta.concrete_fields}
    if "management" in fields:
        return {"management__device_id": device_id}
    if "interface" in fields:
        return {"interface__device_id": device_id}
    return None


def _repair_plan(device_id: int, scope: str) -> RendererMutationPlan:
    """Freeze lifecycle corrections for one optimistic repair candidate."""
    saves = []
    seen = set()
    for spec in renderer_input_specs().values():
        if scope not in spec.scopes or spec.model_label not in OVERLAY_MODEL_RANKS:
            continue
        model = spec.model
        if not any(field.name == "status" for field in model._meta.concrete_fields):
            continue
        if scope == "static_route" and spec.model_label == "netbox_nso_plugin.nsostaticroutestate":
            continue
        filters = _device_filter(model, device_id)
        if filters is None:
            continue
        for row in model.objects.filter(**filters, status__in=("deploying", "in_sync")).order_by("pk"):
            identity = (row._meta.label_lower, row.pk)
            if identity in seen:
                continue
            seen.add(identity)
            candidate = copy.copy(row)
            candidate.status = "accepted"
            fields = ["status"]
            if hasattr(candidate, "apply_attempt_id"):
                candidate.apply_attempt_id = None
                fields.append("apply_attempt_id")
            saves.append(planned_save(candidate, update_fields=fields))
    if scope == "static_route":
        from .models import NSOStaticRouteState
        from .signals import (
            _STATIC_ROUTE_ARMED_FIELDS,
            PUSHED_STATIC_ROUTE_FILTER,
            _arm_static_route_generation,
        )

        rows = NSOStaticRouteState.objects.filter(
            PUSHED_STATIC_ROUTE_FILTER,
            management__device_id=device_id,
        ).order_by("pk")
        for row in rows:
            candidate = copy.copy(row)
            fields = list(_STATIC_ROUTE_ARMED_FIELDS)
            if candidate.status in {"deploying", "in_sync"}:
                candidate.status = "accepted"
                candidate.apply_attempt_id = None
                fields.extend(("status", "apply_attempt_id"))
            _arm_static_route_generation(candidate)
            saves.append(planned_save(candidate, update_fields=fields))
    return RendererMutationPlan.build(saves=saves)


def _post_repair_fingerprint(scope, payload, plan) -> str:
    """Fingerprint the one under-lock render after deterministic repair-only fields."""
    if scope != "static_route":
        return delivery.canonical_fingerprint(payload)
    from .models import NSOStaticRouteState

    generations = {
        NSOStaticRouteState.objects.filter(pk=write.pk).values_list("static_route_id", flat=True).get(): dict(
            write.values
        )["intent_generation"]
        for write in plan.write_set
        if write.model_label == NSOStaticRouteState._meta.label_lower
    }
    repaired_payload = copy.deepcopy(payload)
    for route in repaired_payload:
        route_id = route.get("route_id") if isinstance(route, dict) else None
        if route_id in generations:
            route["generation"] = generations[route_id]
    return delivery.canonical_fingerprint(repaired_payload)


def _trusted(revision) -> bool:
    return bool(
        revision is not None and revision.verified_revision == revision.revision and revision.verified_fingerprint
    )


def _budget_expired(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _bounded_scopes(scopes, *, pre_capture):
    registry = delivery.delivery_keys()
    requested = tuple(dict.fromkeys(str(scope) for scope in scopes))
    unknown = set(requested) - set(registry)
    if unknown:
        raise ValueError(f"unknown renderer scopes {sorted(unknown)!r}")
    cap = _setting("renderer_audit_scope_batch_cap", _DEFAULT_SCOPE_BATCH_CAP, int)
    if len(requested) <= cap:
        return requested, ()
    if pre_capture:
        raise RendererAuditBudgetExceeded(
            f"pre-capture audit requested {len(requested)} scopes, above the configured cap {cap}"
        )
    return requested[:cap], requested[cap:]


def _optimistic_candidates(device_id, selected, management, deadline, *, pre_capture):
    """Return unlocked fingerprint mismatches and any scopes deferred by time."""
    from .models import NSOIntentRevision

    revisions = {row.scope: row for row in NSOIntentRevision.objects.filter(device_id=device_id, scope__in=selected)}
    candidates = []
    for index, scope in enumerate(selected):
        if _budget_expired(deadline):
            if pre_capture:
                raise RendererAuditBudgetExceeded("pre-capture audit exhausted its time budget")
            return tuple(candidates), selected[index:]
        rendered = delivery.render(scope, device_id, management.adapter_device_id)
        fingerprint = delivery.canonical_fingerprint(rendered.payload)
        revision = revisions.get(scope)
        if not _trusted(revision) or revision.verified_fingerprint != fingerprint:
            candidates.append(scope)
    return tuple(candidates), ()


def _repair_candidates(device_id, candidates, management):
    """Repair one optimistic candidate batch in a repeatable-read transaction."""
    from .models import NSOIntentRevision

    # Planned twice on purpose: this pass only derives the lock footprint, because a
    # plan's dependencies are not knowable before it exists. The plan that is CONSUMED
    # is rebuilt below, under the locks, so its full pre-image compare-and-set cannot
    # lose to a foreign lifecycle write and fail a mandatory gate closed.
    footprint = MutationFootprint.merge(
        audit_scope_footprint(device_id, candidates),
        *(_repair_plan(device_id, scope).lock_footprint for scope in candidates),
    )
    with mirror_transaction(footprint, repeatable_read=True) as permit:
        locked_revisions = {
            row.scope: row for row in NSOIntentRevision.objects.filter(device_id=device_id, scope__in=candidates)
        }
        locked_payloads = {}
        repaired = []
        for scope in candidates:
            rendered = delivery.render(scope, device_id, management.adapter_device_id)
            fingerprint = delivery.canonical_fingerprint(rendered.payload)
            locked_payloads[scope] = rendered.payload
            revision = locked_revisions.get(scope)
            if not _trusted(revision) or revision.verified_fingerprint != fingerprint:
                repaired.append(scope)

        for scope in repaired:
            outbox.bump_intent_revision(device_id, scope)

        from .signals import suppress_intent_push

        plans = {scope: _repair_plan(device_id, scope) for scope in repaired}
        with suppress_intent_push():
            for scope in repaired:
                plan = plans[scope]
                with consume_renderer_plan(plan, permit, content=True) as writer:
                    for write in plan.write_set:
                        model = apps.get_model(write.model_label)
                        candidate = model.objects.get(pk=write.pk)
                        for attname, value in write.values:
                            setattr(candidate, attname, value)
                        writer.save(candidate, update_fields=write.update_fields)

        verified_at = timezone.now()
        for scope in repaired:
            outbox.enqueue(device_id, scope, kind=outbox.CONTRIBUTION_KIND_REPAIR)
            revision = NSOIntentRevision.objects.get(device_id=device_id, scope=scope)
            revision.verified_revision = revision.revision
            revision.verified_fingerprint = _post_repair_fingerprint(scope, locked_payloads[scope], plans[scope])
            revision.verified_at = verified_at
            revision.save(
                update_fields=[
                    "verified_revision",
                    "verified_fingerprint",
                    "verified_at",
                    "updated_at",
                ]
            )
    return tuple(repaired)


def _serialization_failure(exc) -> bool:
    return getattr(exc.__cause__, "sqlstate", None) == _SERIALIZATION_FAILURE


def _leave_unknown(device_id, scopes) -> None:
    """Invalidate stale proof after every bounded repair attempt serialized out."""
    from .models import NSOIntentRevision

    with transaction.atomic():
        rows = list(
            NSOIntentRevision.objects.select_for_update(of=("self",))
            .filter(device_id=device_id, scope__in=scopes)
            .order_by("scope")
        )
        for revision in rows:
            revision.verified_revision = None
            revision.verified_fingerprint = None
            revision.verified_at = None
            revision.save(
                update_fields=[
                    "verified_revision",
                    "verified_fingerprint",
                    "verified_at",
                    "updated_at",
                ]
            )


def _repair_with_retries(device_id, candidates, management, deadline):
    """Retry a serialization failure without asserting a false fingerprint."""
    for attempt in range(_REPAIR_ATTEMPTS):
        if _budget_expired(deadline):
            raise RendererAuditBudgetExceeded("renderer repair exhausted its time budget")
        try:
            return _repair_candidates(device_id, candidates, management)
        except OperationalError as exc:
            if not _serialization_failure(exc):
                raise
            logger.warning(
                "renderer audit serialization retry device=%s scopes=%s attempt=%s",
                device_id,
                candidates,
                attempt + 1,
            )
    _leave_unknown(device_id, candidates)
    return None


def audit_renderer_scopes(
    device_id,
    scopes,
    trigger,
    *,
    pre_capture=False,
    deadline: float | None = None,
) -> RendererAuditResult:
    """Audit a bounded scope set and repair every mismatch under the intent locks."""
    from .models import NSODeviceManagement

    device_id = int(device_id)
    selected, deferred = _bounded_scopes(scopes, pre_capture=pre_capture)
    if not selected:
        return RendererAuditResult((), (), deferred)
    if deadline is None:
        budget = _setting(
            "renderer_audit_tick_budget_seconds",
            _DEFAULT_TICK_BUDGET_SECONDS,
            float,
        )
        deadline = time.monotonic() + budget
    management = NSODeviceManagement.objects.filter(device_id=device_id).first()
    if management is None:
        return RendererAuditResult(selected, (), deferred)

    if trigger == "cadence":
        from .management_lifecycle import reconcile_management_control

        reconcile_management_control(device_id)

    from .ownership_planner import reconcile_scope_ownership

    reconcile_scope_ownership(device_id, selected)

    candidates, timed_out = _optimistic_candidates(
        device_id,
        selected,
        management,
        deadline,
        pre_capture=pre_capture,
    )
    deferred = (*timed_out, *deferred)
    if not candidates:
        return RendererAuditResult(selected, (), deferred)
    try:
        repaired = _repair_with_retries(device_id, candidates, management, deadline)
    except RendererAuditBudgetExceeded:
        if pre_capture:
            raise
        logger.info(
            "renderer audit deferred repair device=%s scopes=%s trigger=%s",
            device_id,
            candidates,
            trigger,
        )
        return RendererAuditResult(selected, (), (*candidates, *deferred))
    if repaired is None:
        logger.error(
            "renderer audit left unknown device=%s scopes=%s trigger=%s",
            device_id,
            candidates,
            trigger,
        )
        if pre_capture:
            raise RendererAuditRepairFailed("pre-capture audit could not establish a trusted baseline")
        return RendererAuditResult(selected, (), deferred, candidates)
    if repaired:
        logger.warning(
            "renderer audit repaired device=%s scopes=%s trigger=%s",
            device_id,
            repaired,
            trigger,
        )
    return RendererAuditResult(selected, tuple(repaired), deferred)


def audit_renderer_fleet() -> RendererFleetAuditResult:
    """Audit managed devices until the one shared cadence deadline expires."""
    from .models import NSODeviceManagement

    budget = _setting(
        "renderer_audit_tick_budget_seconds",
        _DEFAULT_TICK_BUDGET_SECONDS,
        float,
    )
    deadline = time.monotonic() + budget
    scopes = tuple(delivery.delivery_keys())
    device_ids = tuple(NSODeviceManagement.objects.order_by("device_id").values_list("device_id", flat=True))
    audited_devices = repaired = deferred = unknown = failed = 0
    for index, device_id in enumerate(device_ids):
        if _budget_expired(deadline):
            deferred += (len(device_ids) - index) * len(scopes)
            break
        try:
            result = audit_renderer_scopes(
                device_id,
                scopes,
                trigger="cadence",
                deadline=deadline,
            )
        except Exception:  # noqa: BLE001 (the next cadence retries this device)
            failed += 1
            logger.exception("renderer cadence audit failed device=%s", device_id)
            continue
        audited_devices += 1
        repaired += len(result.repaired)
        deferred += len(result.deferred)
        unknown += len(result.unknown)
    return RendererFleetAuditResult(
        devices=audited_devices,
        repaired=repaired,
        deferred=deferred,
        unknown=unknown,
        failed=failed,
    )
