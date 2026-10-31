# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Acknowledge one exact settlement issue revision as outcome unknown.

The operation takes the existing mutation locks, revalidates the complete decision against one
fresh adapter observation, and commits the audit record, the exact obligations, the owner
effects and the cursor rule in one transaction. Any change refuses it with no writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.utils import timezone

from . import adapter_client
from .settlement_issue import NotAcknowledgeable, decide, issue_stall, mapping_epoch
from .settlement_resolution import ACKNOWLEDGED, close_completed_attempt, unknown_bookkeeping

REFUSAL_CODES = frozenset(
    {"invalid_actor", "invalid_reason", "revision_mismatch", "not_open", "stale", "observation_unavailable"}
)
MAX_REASON_LENGTH = 2000


class AcknowledgementRefused(Exception):
    """The acknowledgement changed nothing, for one closed reason."""

    def __init__(self, code):
        if code not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code {code!r}")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AcknowledgementReceipt:
    issue: object
    replayed: bool


def _reason(reason):
    if not isinstance(reason, str):
        raise AcknowledgementRefused("invalid_reason")
    reason = reason.strip()
    if not reason or len(reason) > MAX_REASON_LENGTH or "\x00" in reason:
        raise AcknowledgementRefused("invalid_reason")
    return reason


def _actor(actor):
    label = actor.get_username() if getattr(actor, "pk", None) is not None else ""
    if not getattr(actor, "is_authenticated", False) or not label:
        raise AcknowledgementRefused("invalid_actor")
    return label


def _footprint(issue):
    """Build the lock footprint from the stored proposal, including absent owners."""
    from .intent_state import MutationFootprint, SourceRow, footprint_for_instance
    from .refusal_registry import OVERLAYS

    management = issue.management
    owners = list(issue.owners.order_by("model_label", "overlay_pk"))
    keys = {(management.device_id, scope) for owner in owners for scope in OVERLAYS[owner.model_label].delivery_keys}
    footprints = [
        MutationFootprint(device_ids=(management.device_id,)),
        MutationFootprint.for_keys(
            keys, overlay_rows=(SourceRow(owner.model_label, owner.overlay_pk) for owner in owners)
        ),
    ]
    for owner in owners:
        row = OVERLAYS[owner.model_label].model.objects.filter(pk=owner.overlay_pk).first()
        if row is not None:
            footprints.append(footprint_for_instance(row))
    return MutationFootprint.merge(*footprints)


def acknowledge(issue, revision_digest, actor, reason) -> AcknowledgementReceipt:
    """Acknowledge one exact issue revision as outcome unknown, or change nothing."""
    from .intent_state import mirror_transaction
    from .lifecycle_fence import authoritative_bookkeeping
    from .models import NSODeviceManagement, NSOSettlementIssue
    from .settlement_evidence import request_fence
    from .signals import suppress_intent_push

    reason = _reason(reason)
    label = _actor(actor)
    pk = getattr(issue, "pk", issue)
    stored = NSOSettlementIssue.objects.select_related("management").get(pk=pk)
    with suppress_intent_push(), authoritative_bookkeeping(), mirror_transaction(_footprint(stored)):
        locked = NSOSettlementIssue.objects.select_for_update().get(pk=pk)
        if locked.revision_digest != revision_digest:
            raise AcknowledgementRefused("revision_mismatch")
        if locked.state == ACKNOWLEDGED:
            return AcknowledgementReceipt(locked, True)
        if locked.state != "open":
            raise AcknowledgementRefused("not_open")
        management = NSODeviceManagement.objects.select_for_update().get(pk=locked.management_id)
        if (
            request_fence(management) != locked.request_fence
            or mapping_epoch(management) != locked.mapping_epoch
            or management.adapter_device_id != locked.adapter_device_id
        ):
            raise AcknowledgementRefused("stale")
        try:
            decision = decide(
                management,
                issue_stall(locked),
                request=locked.proposal["observation_request"],
                provenance=locked.proposal["provenance"],
            )
        except NotAcknowledgeable:
            raise AcknowledgementRefused("stale") from None
        except adapter_client.AdapterError:
            raise AcknowledgementRefused("observation_unavailable") from None
        if decision.revision_digest != locked.revision_digest:
            raise AcknowledgementRefused("stale")
        _commit(locked, management, decision, actor, label, reason)
    return AcknowledgementReceipt(locked, False)


def _commit(issue, management, decision, actor, label, reason):
    """Write the ledger, owner effects, cursor and diagnostic of one verified decision."""
    from .models import NSOApplyAttempt, NSOApplyExecution, NSOApplyOverlaySnapshot

    now = timezone.now()
    attempts = {
        attempt.pk: attempt
        for attempt in NSOApplyAttempt.objects.select_for_update().filter(
            pk__in=[UUID(row["attempt_id"]) for row in decision.proposal["obligations"] if row["attempt_id"]]
        )
    }
    executions = {}
    released = None
    for obligation in decision.proposal["obligations"]:
        execution = (
            NSOApplyExecution.objects.select_for_update()
            .filter(
                management=management,
                mapping_epoch=issue.mapping_epoch,
                store_incarnation=issue.store_incarnation,
                generation_id=obligation["generation_id"],
                carrier_job_id=issue.job_id,
            )
            .first()
        )
        owning = attempts.get(UUID(obligation["attempt_id"])) if obligation["attempt_id"] else None
        if execution is not None:
            placeholder = execution.pk == obligation["placeholder_execution_id"]
            if (
                not placeholder
                and str(execution.attempt_id) != obligation["attempt_id"]
                or execution.consumption_state not in {"unconsumed", "unresolved"}
                or execution.settle_seq not in (None, issue.settle_seq)
                or execution.generation_seq not in (None, obligation["generation_seq"])
            ):
                raise AcknowledgementRefused("stale")
            if placeholder and owning is not None:
                # A diagnostic placeholder does not establish manual execution ownership.
                execution.attempt = owning
        else:
            attempt = owning
            if attempt is None:
                if released is None:
                    released = NSOApplyAttempt.objects.create(
                        management=management,
                        adapter_device_id=management.adapter_device_id,
                        source_epoch=management.adapter_source_epoch,
                        trigger_kind="cadence",
                    )
                attempt = released
            provenance = decision.proposal["provenance"][str(obligation["generation_id"])]
            execution = NSOApplyExecution(
                attempt=attempt,
                management=management,
                mapping_epoch=issue.mapping_epoch,
                adapter_device_id=management.adapter_device_id,
                store_incarnation=issue.store_incarnation,
                generation_id=obligation["generation_id"],
                carrier_job_id=issue.job_id,
                source_selectors=provenance,
            )
        execution.generation_seq = obligation["generation_seq"]
        execution.settle_seq = issue.settle_seq
        execution.consumption_state = ACKNOWLEDGED
        execution.settlement_issue = issue
        execution.save()
        executions[obligation["generation_id"]] = execution
    effects = []
    for owner in issue.owners.order_by("model_label", "overlay_pk"):
        plan = next(
            item
            for item in decision.owners
            if (item["model_label"], item["overlay_pk"]) == (owner.model_label, owner.overlay_pk)
        )
        execution = executions[plan["generation_ids"][0]]
        attempt = execution.attempt
        _apply_owner_effect(owner, management, execution, attempt, issue)
        snapshots = NSOApplyOverlaySnapshot.objects.select_for_update().filter(
            pk__in=owner.snapshot_ids, attempt__trigger_kind="manual"
        )
        for snapshot in snapshots:
            execution.overlay_snapshots.add(snapshot)
            if snapshot.settlement_disposition == "unresolved":
                snapshot.settlement_disposition = (
                    "superseded" if owner.proposed_effect == "superseded" else "unavailable"
                )
                snapshot.save(update_fields=("settlement_disposition",))
        owner.committed_effect = owner.proposed_effect
        owner.save(update_fields=("committed_effect",))
        effects.append(
            {"model_label": owner.model_label, "overlay_pk": owner.overlay_pk, "effect": owner.committed_effect}
        )
    for execution in executions.values():
        close_completed_attempt(execution.attempt, now)
    _apply_cursor(issue, management)
    issue.state = ACKNOWLEDGED
    issue.closed_at = now
    issue.actor_id = actor.pk
    issue.actor_label = label
    issue.acknowledgement_reason = reason
    issue.acknowledged_at = now
    issue.committed_effects = effects
    issue.save(
        update_fields=(
            "state",
            "closed_at",
            "actor_id",
            "actor_label",
            "acknowledgement_reason",
            "acknowledged_at",
            "committed_effects",
        )
    )


def _apply_owner_effect(owner, management, execution, attempt, issue):
    """Write only the unavailable effect; absent and superseded owners keep their rows."""
    from .intent_state import mirror_refresh
    from .lifecycle_fence import _UNSET, advance_fence, fence_for
    from .refusal_registry import OVERLAYS

    if owner.proposed_effect != "unavailable":
        return
    spec = OVERLAYS[owner.model_label]
    row = (
        spec.model.objects.select_for_update(of=("self",))
        .filter(pk=owner.overlay_pk, **{spec.device_lookup: management.device_id})
        .first()
    )
    if row is None:
        raise AcknowledgementRefused("stale")
    values = unknown_bookkeeping(row, [issue.job_id], attempt)
    with mirror_refresh(row, values) as locked:
        if locked is None:
            raise AcknowledgementRefused("stale")
        for name, value in values.items():
            setattr(locked, name, value)
        locked.save(update_fields=values)
        row = locked
    fence = fence_for(row)
    prior = fence.last_execution if fence is not None else None
    newer = (
        prior is None
        or prior.store_incarnation != execution.store_incarnation
        or prior.settle_seq is not None
        and prior.settle_seq < execution.settle_seq
    )
    advance_fence(
        row,
        current_attempt=attempt,
        last_execution=execution if newer else _UNSET,
        last_settle_seq=execution.settle_seq if newer else _UNSET,
    )


def _apply_cursor(issue, management):
    """Advance only past the actual next feed job; a historical carrier keeps the cursor."""
    from .intent_state import mirror_refresh

    diagnostic = management.settle_evidence_error or {}
    values = {}
    if (
        diagnostic.get("job_id") == issue.job_id
        and diagnostic.get("store_incarnation") == issue.store_incarnation
        and diagnostic.get("request_fence") == issue.request_fence
    ):
        values["settle_evidence_error"] = {}
    if issue.position == "next_feed":
        values.update(
            settle_cursor_seq=issue.cursor_after,
            settle_cursor_incarnation=issue.store_incarnation,
            settle_cursor_device_id=issue.adapter_device_id,
            settle_stall_seq=None,
            settle_stall_attempts=0,
            settle_stall_first_seen_at=None,
        )
    if not values:
        return
    with mirror_refresh(management, values) as locked:
        for name, value in values.items():
            setattr(locked, name, value)
        locked.save(update_fields=values)
        management.refresh_from_db()
