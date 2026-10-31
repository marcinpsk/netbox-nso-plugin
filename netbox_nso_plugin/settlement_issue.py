# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Publish identified settlement stalls as issue revisions with their complete decision.

Observation and planning are pure reads: they never replay Apply, record invariants or bind
selections. The decision digest binds the request fence, the observed adapter evidence, the
local join inputs, the owner authority, the proposed effects and the contract fingerprint.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import adapter_client
from .settlement_resolution import (
    ACKNOWLEDGED,
    canonical_digest,
    contribution_key,
    instant,
)
from .settlement_schemas import _SCHEMA, canonical_uuid

logger = logging.getLogger(__name__)

EXIT_ACKNOWLEDGEABLE = "acknowledgeable"
EXIT_CODES = frozenset(
    {
        EXIT_ACKNOWLEDGEABLE,
        "unidentified",
        "unordered",
        "ownership_unbounded",
        "resolved_obligation",
        "unbound_evidence",
    }
)
# The adapter observation accepts at most this many distinct attempt identities.
MAX_OBSERVED_ATTEMPTS = 100
_CERTIFIED_STATES = ("consumed", "superseded")
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed"})


def _contract_fingerprint():
    from .execution_contract import METADATA

    return canonical_digest({"settlements": _SCHEMA, "execution": METADATA})


CONTRACT_FINGERPRINT = _contract_fingerprint()


class NotAcknowledgeable(Exception):
    """The stall stays visible without an exit, for one closed reason."""

    def __init__(self, code):
        if code not in EXIT_CODES:
            raise ValueError(f"unknown exit code {code!r}")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Stall:
    """The identified stall that a diagnostic or an issue names."""

    fence: dict
    job_id: int
    store_incarnation: str
    stall_class: str
    reason: str


@dataclass
class Decision:
    """One complete decision: its digests, its stored proposal and its owner plan."""

    target: dict
    proposal: dict
    owners: list
    evidence_digest: str
    impact_digest: str
    revision_digest: str


def _positive(value):
    return type(value) is int and value > 0


def _uuid_text(value):
    return None if value is None else str(value)


def stall_from_diagnostic(management) -> Stall:
    """Accept only a current diagnostic with a trusted job identity and store incarnation."""
    from .refusal_ledger import STALL_CLASSES
    from .settlement_evidence import matches_request_fence

    diagnostic = management.settle_evidence_error or {}
    fence = diagnostic.get("request_fence")
    if not diagnostic or not isinstance(fence, dict) or not matches_request_fence(management, fence):
        raise NotAcknowledgeable("unidentified")
    job_id, incarnation = diagnostic.get("job_id"), diagnostic.get("store_incarnation")
    stall_class = diagnostic.get("class")
    if (
        management.adapter_device_id is None
        or not _positive(job_id)
        or not canonical_uuid(incarnation)
        or stall_class not in STALL_CLASSES
    ):
        raise NotAcknowledgeable("unidentified")
    reason = diagnostic.get("code") or diagnostic.get("reason")
    if not isinstance(reason, str) or not reason:
        raise NotAcknowledgeable("unidentified")
    return Stall(dict(fence), job_id, incarnation, stall_class, reason)


def issue_stall(issue) -> Stall:
    return Stall(
        dict(issue.request_fence), issue.job_id, issue.store_incarnation, issue.stall_class, issue.stall_reason
    )


def _effective_cursor(fence, management, incarnation):
    """Use the same cursor epoch rule as the settlement walk."""
    if fence["cursor_device_id"] == management.adapter_device_id and fence["cursor_incarnation"] == incarnation:
        return fence["cursor_seq"] or 0
    return 0


def _envelope(item):
    """Bind one decoded evidence item by its trusted identity and content digest."""
    from .adapter_client import EvidenceItem

    digest = getattr(item, "evidence_digest", None)
    identity = getattr(item, "evidence_identity", None)
    if digest is None or identity is None:
        raise NotAcknowledgeable("unbound_evidence")
    return {"identity": dict(identity), "digest": digest, "valid": not isinstance(item, EvidenceItem)}


def _generation_order(envelope):
    identity = envelope["identity"]
    return identity["generation_id"], identity["carrier_job_id"] or 0


def _attempt_rows(attempt):
    from .apply_settlement import _attempt_generations

    return _attempt_generations(attempt)


def _remote(observation):
    """Project the decision-relevant adapter evidence, excluding transient work counters."""
    deployment = observation["deployment"]
    absent = observation["absent"]
    return {
        "store_incarnation": observation["store_incarnation"],
        "device": observation["device"],
        "job": None if observation["job"] is None else _envelope(observation["job"]),
        "next_feed": observation["next_feed"],
        "generations": sorted((_envelope(row) for row in observation["generations"]), key=_generation_order),
        "head": None if deployment["head"] is None else _envelope(deployment["head"]),
        "attempts": sorted(
            (
                {
                    **_envelope(attempt),
                    "generations": sorted((_envelope(row) for row in _attempt_rows(attempt)), key=_generation_order),
                }
                for attempt in deployment["attempts"]
            ),
            key=lambda envelope: envelope["identity"]["apply_attempt_id"],
        ),
        "unknown_apply_attempt_ids": sorted(deployment["unknown_apply_attempt_ids"]),
        "receipts": sorted(
            (_envelope(row) for row in observation["receipts"]), key=lambda envelope: envelope["identity"]["section"]
        ),
        "absent": {
            "job_id": absent["job_id"],
            "apply_attempt_ids": sorted(absent["apply_attempt_ids"]),
            "receipt_sections": sorted(absent["receipt_sections"]),
        },
    }


def _observed_generations(observation):
    deployment = observation["deployment"]
    rows = list(observation["generations"])
    if deployment["head"] is not None:
        rows.append(deployment["head"])
    for attempt in deployment["attempts"]:
        rows.extend(_attempt_rows(attempt))
    return rows


def _target(observation, stall, management):
    """Prove the target's identity and its position with the cursor rule table."""
    from .adapter_client import EvidenceItem

    job = observation["job"]
    if job is None or observation["store_incarnation"] != stall.store_incarnation:
        raise NotAcknowledgeable("unordered")
    settle_seq = job.evidence_identity["settle_seq"]
    if not _positive(settle_seq) or job.evidence_identity["device_id"] != management.adapter_device_id:
        raise NotAcknowledgeable("unordered")
    if not isinstance(job, EvidenceItem) and job["status"] not in _TERMINAL_JOB_STATUSES:
        raise NotAcknowledgeable("unordered")
    cursor = _effective_cursor(stall.fence, management, stall.store_incarnation)
    if settle_seq <= cursor:
        return {"position": "historical", "settle_seq": settle_seq, "cursor_before": cursor, "cursor_after": cursor}
    next_feed = observation["next_feed"]
    if next_feed is None or (next_feed["id"], next_feed["settle_seq"]) != (stall.job_id, settle_seq):
        raise NotAcknowledgeable("unordered")
    return {"position": "next_feed", "settle_seq": settle_seq, "cursor_before": cursor, "cursor_after": settle_seq}


def _members(observation, job_id):
    """Return the exact generations carried by the target from trusted identities."""
    from .adapter_client import EvidenceItem

    members = {}
    for row in _observed_generations(observation):
        identity = row.evidence_identity
        valid = not isinstance(row, EvidenceItem)
        if identity["carrier_job_id"] == job_id or valid and row.get("job_id") == job_id:
            if members.setdefault(identity["generation_id"], identity["seq"]) != identity["seq"]:
                raise NotAcknowledgeable("unbound_evidence")
        elif not valid:
            # A failed projection that may belong to the target leaves its membership unbounded.
            raise NotAcknowledgeable("ownership_unbounded")
    job = observation["job"]
    if job is not None and not isinstance(job, EvidenceItem):
        for outcome in job["row_outcomes"] or ():
            for generation_id in (outcome["generation_id"], outcome["carrier"]["generation_id"]):
                if generation_id not in members:
                    raise NotAcknowledgeable("ownership_unbounded")
    return members


def _provenance(management, stall, members, known=None):
    """Read immutable generation provenance from the listing inside one store incarnation."""
    if known is not None:
        provenance = {int(key): value for key, value in known.items()}
        if set(provenance) != set(members):
            raise NotAcknowledgeable("ownership_unbounded")
        return provenance
    if not members:
        return {}
    from .adapter_client import EvidenceItem

    device_id = management.adapter_device_id
    _page, before = adapter_client.get_settlement_feed(device_id, after_settle_seq=0, limit=1)
    listing = adapter_client.list_device_generations(device_id)
    _page, after = adapter_client.get_settlement_feed(device_id, after_settle_seq=0, limit=1)
    if before != stall.store_incarnation or after != stall.store_incarnation:
        raise NotAcknowledgeable("unordered")
    by_id = {row["generation_id"]: row for row in listing}
    provenance = {}
    for generation_id, seq in members.items():
        row = by_id.get(generation_id)
        if row is None or isinstance(row, EvidenceItem) or row["seq"] != seq:
            raise NotAcknowledgeable("ownership_unbounded")
        provenance[generation_id] = {
            "stream_revisions": copy.deepcopy(row["stream_revisions"]),
            "source_push_seq": copy.deepcopy(row["source_push_seq"]),
        }
    return provenance


def _selector(provenance, stream):
    source = provenance["source_push_seq"].get(stream)
    if source is not None:
        return {"source_push_seq": source}
    return {"selection_revision": provenance["stream_revisions"][stream]}


def _attempts_naming(management, job_id, generation_ids):
    """Return every attempt whose stored admission names the job or a generation, with or without its identity."""
    from .models import NSOApplyAttempt

    query = Q(response__generations__contains=[{"job_id": job_id}])
    for generation_id in generation_ids:
        query |= Q(response__generations__contains=[{"generation_id": generation_id}])
    return set(NSOApplyAttempt.objects.filter(management=management).filter(query).values_list("pk", flat=True))


def _candidate_attempts(management, stall, observation, members, provenance):
    """Find every local attempt that a member names, plus lost answers that may name one."""
    attempts = _attempts_naming(management, stall.job_id, members)
    for row in _observed_generations(observation):
        identity = row.evidence_identity
        if identity["generation_id"] in members and identity["apply_attempt_id"] is not None:
            attempts.add(UUID(identity["apply_attempt_id"]))
    from .models import NSOApplyAttempt

    # A lost answer is ambiguous linkage, also for an attempt from before the adapter identity existed.
    for attempt in NSOApplyAttempt.objects.filter(management=management, trigger_kind="manual", response__isnull=True):
        if any(
            attempt.selected.get(stream) == next(iter(_selector(provenance[generation_id], stream).values()))
            for generation_id in members
            for stream in provenance[generation_id]["stream_revisions"]
        ):
            attempts.add(attempt.pk)
    return attempts


def _linked_attempts(management, stall, observation, members, candidates):
    """Keep candidates whose stored admission or observed admission links them to a member."""
    from .models import NSOApplyAttempt

    linked = _attempts_naming(management, stall.job_id, members)
    for row in _observed_generations(observation):
        identity = row.evidence_identity
        if identity["generation_id"] in members and identity["apply_attempt_id"] is not None:
            linked.add(UUID(identity["apply_attempt_id"]))
    for attempt in observation["deployment"]["attempts"]:
        if any(row.evidence_identity["generation_id"] in members for row in _attempt_rows(attempt)):
            linked.add(UUID(attempt.evidence_identity["apply_attempt_id"]))
    observed = {
        UUID(attempt.evidence_identity["apply_attempt_id"]) for attempt in observation["deployment"]["attempts"]
    }
    unknown = {UUID(value) for value in observation["deployment"]["unknown_apply_attempt_ids"]}
    for attempt in NSOApplyAttempt.objects.filter(pk__in=candidates, response__isnull=True):
        if attempt.pk not in observed | unknown:
            raise NotAcknowledgeable("ownership_unbounded")
    return linked & set(candidates)


@dataclass
class _Plan:
    owners: dict = field(default_factory=dict)
    selections: set = field(default_factory=set)
    snapshots: set = field(default_factory=set)
    memberships: dict = field(default_factory=dict)


def _owning_attempts(management, stall, observation, members, linked):
    """Join each obligation to at most one promoted manual admission."""
    from .models import NSOApplyAttempt

    owning = {}
    local = NSOApplyAttempt.objects.in_bulk(linked)
    for generation_id in members:
        candidates = {
            attempt_id
            for attempt_id, attempt in local.items()
            if attempt.trigger_kind == "manual"
            and attempt.adapter_apply_attempt_id == attempt.pk
            and any(
                link.get("generation_id") == generation_id and link.get("job_id") == stall.job_id
                for link in (attempt.response or {}).get("generations") or ()
            )
        }
        for row in _observed_generations(observation):
            identity = row.evidence_identity
            if (
                identity["generation_id"] == generation_id
                and identity["carrier_job_id"] == stall.job_id
                and identity["apply_attempt_id"] is not None
                and UUID(identity["apply_attempt_id"]) in local
            ):
                candidates.add(UUID(identity["apply_attempt_id"]))
        if len(candidates) > 1:
            raise NotAcknowledgeable("ownership_unbounded")
        attempt = local[next(iter(candidates))] if candidates else None
        if attempt is not None and (
            attempt.adapter_device_id != management.adapter_device_id
            or attempt.source_epoch != management.adapter_source_epoch
        ):
            raise NotAcknowledgeable("ownership_unbounded")
        owning[generation_id] = attempt
    return owning


def _admission_provenance(owning, members, provenance):
    """Require stored admission links to agree with the listing's immutable provenance."""
    for generation_id, attempt in owning.items():
        if attempt is None or attempt.response is None:
            continue
        for link in attempt.response.get("generations") or ():
            if link.get("generation_id") != generation_id:
                continue
            if (
                link.get("seq") != members[generation_id]
                or link.get("stream_revisions") != provenance[generation_id]["stream_revisions"]
                or link.get("source_push_seq") != provenance[generation_id]["source_push_seq"]
            ):
                raise NotAcknowledgeable("ownership_unbounded")


def _decided_later(management, stall, owner_key, frozen, contribution, bound):
    """Prove a certified later execution decided this contribution under the same acceptance."""
    from .models import NSOApplyExecution

    executions = NSOApplyExecution.objects.filter(
        management=management,
        adapter_device_id=management.adapter_device_id,
        store_incarnation=stall.store_incarnation,
        consumption_state__in=_CERTIFIED_STATES,
        invariant_code="",
        consumed_at__isnull=False,
        settle_seq__gt=bound,
        overlay_snapshots__model_label=owner_key[0],
        overlay_snapshots__overlay_pk=owner_key[1],
        overlay_snapshots__accepted_at=frozen.accepted_at,
        overlay_snapshots__expected_owner_revision=frozen.expected_owner_revision,
        overlay_snapshots__settlement_disposition__in=("in_sync", "refused", "not_applied", "unavailable"),
    ).distinct()
    for execution in executions:
        outcomes = (execution.terminal_projection or {}).get("row_outcomes") or ()
        if any(contribution_key(outcome["stream"], outcome["row"]) == contribution for outcome in outcomes):
            return True
    return False


def _fence_projection(management, label, fence):
    """Project a missing fence as the fence that the lock protocol creates for it."""
    from .lifecycle_fence import applied_watermark
    from .refusal_registry import OVERLAYS

    if fence is None:
        return {
            "bookkeeping_revision": 0,
            "owner_revision": 0,
            "current_attempt_id": None,
            "last_execution_id": None,
            "last_settle_seq": None,
            "observation_watermark": applied_watermark(management, OVERLAYS[label]),
            "accepted_at": None,
            "content_digest": "",
        }
    return {
        "bookkeeping_revision": fence.bookkeeping_revision,
        "owner_revision": fence.owner_revision,
        "current_attempt_id": _uuid_text(fence.current_attempt_id),
        "last_execution_id": fence.last_execution_id,
        "last_settle_seq": fence.last_settle_seq,
        "observation_watermark": fence.observation_watermark,
        "accepted_at": instant(fence.accepted_at),
        "content_digest": fence.content_digest,
    }


def _authority(management, label, row, fence):
    """Capture the owner authority that the decision binds."""
    from . import durable_capture as capture
    from .lifecycle_fence import owner_content_digest

    fenced = _fence_projection(management, label, fence)
    authority = {"exists": row is not None, "fence": fenced, "owner_revision": fenced["owner_revision"]}
    if row is not None:
        authority.update(
            status=row.status,
            accepted_at=instant(row.accepted_at),
            last_apply_at=instant(row.last_apply_at),
            last_apply_error=canonical_digest(row.last_apply_error),
            apply_attempt_id=_uuid_text(row.apply_attempt_id),
            content_digest=owner_content_digest(row),
            static_route_fences=capture._static_fences(row),
        )
    return authority


def _unique_selection(management, stall, provenance, stream):
    """Return the one eligible selection a stream selector names, with its push snapshots."""
    from . import durable_capture as capture
    from .models import NSOAcceptedSelection
    from .refusal_registry import RegistryError

    eligible = [
        selection
        for selection in NSOAcceptedSelection.objects.filter(
            management=management, stream=stream, **_selector(provenance, stream)
        ).order_by("pk")
        if selection.binding_state != "invalidated" and selection.store_incarnation in (None, stall.store_incarnation)
    ]
    if len(eligible) != 1:
        raise NotAcknowledgeable("ownership_unbounded")
    try:
        snapshots = capture._validate_snapshot_membership(eligible[0])
    except RegistryError:
        raise NotAcknowledgeable("ownership_unbounded") from None
    return eligible[0], {(item.model_label, item.overlay_pk): item for item in snapshots}


def _frozen_snapshot(attempt, selection, pushed, key, epoch):
    """Use the manual snapshot of the owning attempt, else the selection's frozen acceptance."""
    frozen = pushed
    if attempt is not None and selection.mapping_epoch == epoch:
        frozen = attempt.overlay_snapshots.filter(model_label=key[0], overlay_pk=key[1]).first()
    if frozen is None or frozen.expected_owner_revision is None:
        raise NotAcknowledgeable("ownership_unbounded")
    return frozen


def _contributions(management, stall, members, provenance, owning, plan, epoch):
    """Map each affected owner to its exact contributor subset and frozen snapshots."""
    contributions = {}
    for generation_id in sorted(members):
        attempt = owning[generation_id]
        for stream in sorted(provenance[generation_id]["stream_revisions"]):
            selection, pushed = _unique_selection(management, stall, provenance[generation_id], stream)
            plan.selections.add(selection.pk)
            for owner in selection.contributor_manifest:
                key = (owner["model_label"], owner["overlay_pk"])
                rows = [item for item in owner["contributors"] if item["stream"] == stream]
                if not rows:
                    continue
                frozen = _frozen_snapshot(attempt, selection, pushed[key], key, epoch)
                plan.snapshots.add(frozen.pk)
                entry = contributions.setdefault(
                    key, {"rows": {}, "selections": set(), "attempts": set(), "generations": set()}
                )
                entry["selections"].add(selection.pk)
                entry["generations"].add(generation_id)
                entry["attempts"].add(None if attempt is None else attempt.pk)
                for item in rows:
                    entry["rows"][tuple(contribution_key(stream, item["logical_ref"]))] = frozen
    return contributions


def _owner_plan(management, stall, target, key, entry, epoch):
    """Decide one owner's effect per contribution and capture the authority it binds."""
    from .lifecycle_fence import owner_content_digest, owner_deletion_proven
    from .models import NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS
    from .status_machine import is_owned

    label, pk = key
    if len(entry["attempts"]) != 1:
        raise NotAcknowledgeable("ownership_unbounded")
    spec = OVERLAYS[label]
    row = spec.model.objects.filter(pk=pk, **{spec.device_lookup: management.device_id}).first()
    fence = NSOOverlaySettlementFence.objects.filter(
        management=management, mapping_epoch=epoch, model_label=label, overlay_pk=pk
    ).first()
    unknown = []
    for contribution, frozen in sorted(entry["rows"].items()):
        if row is None:
            if not owner_deletion_proven(management, frozen):
                unknown.append(list(contribution))
            continue
        if (
            not is_owned(row.status)
            or frozen.mapping_epoch != epoch
            or row.accepted_at != frozen.accepted_at
            or owner_content_digest(row) != frozen.content_digest
            or (0 if fence is None else fence.owner_revision) != frozen.expected_owner_revision
        ):
            continue
        if _decided_later(management, stall, key, frozen, list(contribution), target["settle_seq"]):
            continue
        unknown.append(list(contribution))
    attempt_id = next(iter(entry["attempts"]))
    return {
        "model_label": label,
        "overlay_pk": pk,
        "snapshot_ids": sorted({frozen.pk for frozen in entry["rows"].values()}),
        "attempt_id": _uuid_text(attempt_id),
        "selection_ids": sorted(entry["selections"]),
        "generation_ids": sorted(entry["generations"]),
        "contributors": [list(contribution) for contribution in sorted(entry["rows"])],
        "unknown": unknown,
        "authority": _authority(management, label, row, fence),
        "order_bound": target["settle_seq"],
        "effect": "superseded" if not unknown else "missing_owner" if row is None else "unavailable",
    }


def _plan(management, stall, target, members, provenance, owning):
    """Select exact snapshots and contributor subsets, and the effect of each owner."""
    from . import durable_capture as capture

    epoch = capture.mapping_epoch(management)
    plan = _Plan()
    for attempt in owning.values():
        if attempt is not None:
            plan.memberships[attempt.pk] = set(attempt.overlay_snapshots.values_list("model_label", "overlay_pk"))
    contributions = _contributions(management, stall, members, provenance, owning, plan, epoch)
    for key, entry in sorted(contributions.items()):
        plan.owners[key] = _owner_plan(management, stall, target, key, entry, epoch)
    return plan


def _placeholder(execution):
    """Apply the narrowed exception: a diagnostic-only execution on an unreferenced cadence parent."""
    from .models import NSOApplyOverlaySnapshot, NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS

    attempt = execution.attempt
    if not (
        execution.invariant_code
        and execution.consumption_state == "unconsumed"
        and execution.terminal_projection is None
        and not execution.overlay_snapshots.exists()
        and attempt.trigger_kind == "cadence"
        and attempt.adapter_apply_attempt_id is None
    ):
        return False
    return not (
        attempt.executions.exclude(pk=execution.pk).exists()
        or NSOApplyOverlaySnapshot.objects.filter(
            Q(attempt=attempt) | Q(prior_apply_attempt=attempt) | Q(expected_apply_attempt=attempt)
        ).exists()
        or NSOOverlaySettlementFence.objects.filter(current_attempt=attempt).exists()
        or attempt.deployment_verifications.exists()
        or any(spec.model.objects.filter(apply_attempt_id=attempt.pk).exists() for spec in OVERLAYS.values())
    )


def _references(management, stall, members, provenance, linked):
    """Run the complete dependency proof: every surface that can bind a member or its attempts."""
    from . import delivery
    from .models import (
        NSOAcceptedSelection,
        NSOApplyExecution,
        NSOApplyOverlaySnapshot,
        NSOIntentOutboxState,
        NSOOverlaySettlementFence,
    )
    from .refusal_registry import OVERLAYS

    references = {}
    placeholders = {}
    selections = set()
    epoch = mapping_epoch(management)
    for generation_id in members:
        for stream in provenance[generation_id]["stream_revisions"]:
            for selection in NSOAcceptedSelection.objects.filter(
                management=management, stream=stream, **_selector(provenance[generation_id], stream)
            ):
                # A null incarnation is a candidate, not absence.
                if selection.store_incarnation in (None, stall.store_incarnation):
                    selections.add(selection.pk)
                    references[("selection", selection.pk)] = _selection_state(selection)
            source = provenance[generation_id]["source_push_seq"].get(stream)
            scopes = [
                key for key, entry in delivery.delivery_keys().items() if entry.in_protocol and entry.section == stream
            ]
            if (
                source is not None
                and NSOIntentOutboxState.objects.filter(
                    device_id=management.device_id, scope__in=scopes, push_seq=source
                ).exists()
            ):
                references[("claim", stream, source)] = {"push_seq": source}
    executions = list(
        NSOApplyExecution.objects.filter(
            management=management, adapter_device_id=management.adapter_device_id, generation_id__in=list(members)
        )
        .filter(Q(store_incarnation=stall.store_incarnation) | Q(store_incarnation__isnull=True))
        .select_related("attempt")
        .order_by("pk")
    )
    obligation_executions = set()
    for execution in executions:
        if execution.carrier_job_id == stall.job_id:
            if execution.consumption_state != "unconsumed" and execution.consumption_state != "unresolved":
                raise NotAcknowledgeable("resolved_obligation")
            obligation_executions.add(execution.pk)
            if execution.mapping_epoch == epoch and _placeholder(execution):
                placeholders[execution.generation_id] = execution.pk
                continue
            linked = linked | {execution.attempt_id}
        references[("execution", execution.pk)] = _execution_state(execution)
    for attempt in _attempt_rows_for(linked):
        references[("attempt", str(attempt.pk))] = _attempt_state(attempt)
    snapshot_query = (
        Q(selection_id__in=selections)
        | Q(attempt_id__in=linked)
        | Q(prior_apply_attempt_id__in=linked)
        | Q(expected_apply_attempt_id__in=linked)
        | Q(executions__in=obligation_executions)
    )
    for snapshot in NSOApplyOverlaySnapshot.objects.filter(snapshot_query).distinct().order_by("pk"):
        references[("snapshot", snapshot.pk)] = _snapshot_state(snapshot)
    for fence in NSOOverlaySettlementFence.objects.filter(management=management).filter(
        Q(current_attempt_id__in=linked) | Q(last_execution_id__in=obligation_executions)
    ):
        references[("fence", fence.pk)] = _fence_state(fence)
    for label, spec in OVERLAYS.items():
        for pk, status, attempt_id in spec.model.objects.filter(apply_attempt_id__in=linked).values_list(
            "pk", "status", "apply_attempt_id"
        ):
            references[("overlay", label, pk)] = {"status": status, "apply_attempt_id": str(attempt_id)}
    return references, placeholders, linked


def _attempt_rows_for(attempt_ids):
    from .models import NSOApplyAttempt

    return NSOApplyAttempt.objects.filter(pk__in=attempt_ids).order_by("pk")


def _selection_state(selection):
    return {
        "mapping_epoch": selection.mapping_epoch,
        "store_incarnation": selection.store_incarnation,
        "stream": selection.stream,
        "source_push_seq": selection.source_push_seq,
        "selection_revision": selection.selection_revision,
        "plugin_revision": selection.plugin_revision,
        "request_digest": selection.request_digest,
        "manifest": canonical_digest(selection.contributor_manifest),
        "publication_state": selection.publication_state,
        "acknowledged_at": instant(selection.acknowledged_at),
        "binding_state": selection.binding_state,
        "frozen_revision": selection.frozen_revision,
    }


def _execution_state(execution):
    return {
        "attempt_id": str(execution.attempt_id),
        "mapping_epoch": execution.mapping_epoch,
        "generation_id": execution.generation_id,
        "carrier_job_id": execution.carrier_job_id,
        "consumption_state": execution.consumption_state,
        "invariant_code": execution.invariant_code,
        "settle_seq": execution.settle_seq,
        "terminal_projection": canonical_digest(execution.terminal_projection),
    }


def _attempt_state(attempt):
    return {
        "trigger_kind": attempt.trigger_kind,
        "lifecycle": attempt.lifecycle,
        "adapter_apply_attempt_id": _uuid_text(attempt.adapter_apply_attempt_id),
        "adapter_device_id": attempt.adapter_device_id,
        "source_epoch": attempt.source_epoch,
        "http_status": attempt.http_status,
        "response": canonical_digest(attempt.response),
        "selected": attempt.selected,
    }


def _snapshot_state(snapshot):
    return {
        "attempt_id": str(snapshot.attempt_id),
        "selection_id": snapshot.selection_id,
        "model_label": snapshot.model_label,
        "overlay_pk": snapshot.overlay_pk,
        "mapping_epoch": snapshot.mapping_epoch,
        "prior_apply_attempt_id": _uuid_text(snapshot.prior_apply_attempt_id),
        "expected_apply_attempt_id": _uuid_text(snapshot.expected_apply_attempt_id),
        "accepted_at": instant(snapshot.accepted_at),
        "content_digest": snapshot.content_digest,
        "expected_fence_revision": snapshot.expected_fence_revision,
        "expected_owner_revision": snapshot.expected_owner_revision,
        "settlement_disposition": snapshot.settlement_disposition,
    }


def _fence_state(fence):
    return {
        "mapping_epoch": fence.mapping_epoch,
        "model_label": fence.model_label,
        "overlay_pk": fence.overlay_pk,
        "bookkeeping_revision": fence.bookkeeping_revision,
        "owner_revision": fence.owner_revision,
        "current_attempt_id": _uuid_text(fence.current_attempt_id),
        "last_execution_id": fence.last_execution_id,
    }


def _resolved_history(management, stall, attempt_id, members):
    """Treat a linked attempt as history only when each member it ran is already resolved."""
    from .models import NSOApplyExecution

    executions = NSOApplyExecution.objects.filter(
        management=management, attempt_id=attempt_id, generation_id__in=list(members)
    ).exclude(carrier_job_id=stall.job_id)
    return (
        executions.exists()
        and not executions.exclude(consumption_state__in=(*_CERTIFIED_STATES, ACKNOWLEDGED)).exists()
    )


def _require_explained(management, stall, references, plan, members):
    """Refuse when any positive reference is outside the planned owners and frozen memberships."""
    owners = set(plan.owners)
    explained_attempts = {str(attempt_id) for attempt_id in plan.memberships}
    explained_attempts.update(
        identity[1]
        for identity in references
        if identity[0] == "attempt" and _resolved_history(management, stall, UUID(identity[1]), members)
    )
    memberships = {str(attempt_id): owners_of for attempt_id, owners_of in plan.memberships.items()}

    def member_of(attempt_id, owner_key):
        if attempt_id is None:
            return False
        if attempt_id not in memberships:
            from .models import NSOApplyOverlaySnapshot

            memberships[attempt_id] = set(
                NSOApplyOverlaySnapshot.objects.filter(attempt_id=attempt_id).values_list("model_label", "overlay_pk")
            )
        return attempt_id in explained_attempts and owner_key in memberships[attempt_id]

    for identity, state in references.items():
        kind = identity[0]
        if kind == "selection":
            explained = identity[1] in plan.selections
        elif kind == "claim":
            explained = False
        elif kind == "attempt":
            explained = identity[1] in explained_attempts
        elif kind == "execution":
            explained = (
                state["carrier_job_id"] != stall.job_id
                and state["consumption_state"] in (*_CERTIFIED_STATES, ACKNOWLEDGED)
                or state["carrier_job_id"] == stall.job_id
                and state["attempt_id"] in explained_attempts
            )
        elif kind == "snapshot":
            owner_key = (state["model_label"], state["overlay_pk"])
            explained = (
                identity[1] in plan.snapshots
                or state["selection_id"] in plan.selections
                or owner_key in owners
                or any(
                    member_of(attempt_id, owner_key)
                    for attempt_id in (
                        state["attempt_id"],
                        state["prior_apply_attempt_id"],
                        state["expected_apply_attempt_id"],
                    )
                )
            )
        elif kind == "fence":
            owner_key = (state["model_label"], state["overlay_pk"])
            explained = owner_key in owners or member_of(state["current_attempt_id"], owner_key)
        else:
            owner_key = (identity[1], identity[2])
            explained = owner_key in owners or member_of(state["apply_attempt_id"], owner_key)
        if not explained:
            raise NotAcknowledgeable("ownership_unbounded")


def _observe(management, stall, cursor, attempts):
    if len(attempts) > MAX_OBSERVED_ATTEMPTS:
        raise NotAcknowledgeable("ownership_unbounded")
    return adapter_client.get_settlement_observation(
        management.adapter_device_id, job_id=stall.job_id, after_settle_seq=cursor, apply_attempt_ids=attempts
    )


def decide(management, stall, *, request=None, provenance=None) -> Decision:
    """Build the complete decision with pure reads; a stored request and provenance are reused."""
    cursor = _effective_cursor(stall.fence, management, stall.store_incarnation)
    attempts = (
        sorted(_attempts_naming(management, stall.job_id, ()), key=str)
        if request is None
        else [UUID(value) for value in request["apply_attempt_ids"]]
    )
    observation = _observe(management, stall, cursor, attempts)
    target = _target(observation, stall, management)
    members = _members(observation, stall.job_id)
    known = _provenance(management, stall, members, provenance)
    candidates = _candidate_attempts(management, stall, observation, members, known)
    if request is None and not candidates <= set(attempts):
        attempts = sorted(candidates | set(attempts), key=str)
        observation = _observe(management, stall, cursor, attempts)
        if _target(observation, stall, management) != target or _members(observation, stall.job_id) != members:
            raise NotAcknowledgeable("unbound_evidence")
        candidates = _candidate_attempts(management, stall, observation, members, known)
    if not candidates <= set(attempts):
        raise NotAcknowledgeable("unbound_evidence")
    linked = _linked_attempts(management, stall, observation, members, candidates)
    references, placeholders, linked = _references(management, stall, members, known, linked)
    plan = _Plan()
    if references:
        owning = _owning_attempts(management, stall, observation, members, linked)
        _admission_provenance(owning, members, known)
        plan = _plan(management, stall, target, members, known, owning)
        _require_explained(management, stall, references, plan, members)
    else:
        owning = dict.fromkeys(members)
    remote = _remote(observation)
    obligations = [
        {
            "generation_id": generation_id,
            "carrier_job_id": stall.job_id,
            "generation_seq": members[generation_id],
            "attempt_id": _uuid_text(None if owning[generation_id] is None else owning[generation_id].pk),
            "placeholder_execution_id": placeholders.get(generation_id),
        }
        for generation_id in sorted(members)
    ]
    owners = [plan.owners[key] for key in sorted(plan.owners)]
    local = {
        "obligations": obligations,
        "provenance": {str(key): value for key, value in sorted(known.items())},
        "references": [
            {"identity": [str(part) for part in identity], "state": state}
            for identity, state in sorted(references.items(), key=lambda item: [str(part) for part in item[0]])
        ],
        "owners": owners,
    }
    target = {
        "adapter_device_id": management.adapter_device_id,
        "store_incarnation": stall.store_incarnation,
        "job_id": stall.job_id,
        **target,
    }
    evidence_digest = canonical_digest(remote)
    impact_digest = canonical_digest(local)
    revision_digest = canonical_digest(
        {
            "fence": stall.fence,
            "stall": {"class": stall.stall_class, "reason": stall.reason},
            "target": target,
            "evidence": evidence_digest,
            "impact": impact_digest,
            "contract": CONTRACT_FINGERPRINT,
        }
    )
    proposal = {
        "observation_request": {"apply_attempt_ids": sorted(str(attempt_id) for attempt_id in attempts)},
        "obligations": obligations,
        "provenance": local["provenance"],
    }
    return Decision(target, proposal, owners, evidence_digest, impact_digest, revision_digest)


def _record_exit(management, stall_fence, diagnostic, code):
    """Expose the exit decision beside the current diagnostic only."""
    from .intent_state import mirror_refresh
    from .models import NSODeviceManagement
    from .settlement_evidence import matches_request_fence
    from .signals import suppress_intent_push

    row = NSODeviceManagement.objects.select_for_update().get(pk=management.pk)
    current = row.settle_evidence_error or {}
    if not current or _base(current) != _base(diagnostic) or not matches_request_fence(row, stall_fence):
        return False
    if current.get("exit") == code:
        return True
    with suppress_intent_push(), mirror_refresh(row, ("settle_evidence_error",)) as locked:
        locked.settle_evidence_error = {**_base(current), "exit": code}
        locked.save(update_fields=("settle_evidence_error",))
    return True


def _base(diagnostic):
    return {key: value for key, value in (diagnostic or {}).items() if key != "exit"}


def publish_stall_issue(management):
    """Publish the current stall as an issue revision, or record why it has no exit."""
    from .models import NSODeviceManagement, NSOSettlementIssue, NSOSettlementIssueOwner

    pk = getattr(management, "pk", management)
    management = NSODeviceManagement.objects.get(pk=pk)
    diagnostic = dict(management.settle_evidence_error or {})
    fence = diagnostic.get("request_fence")
    if not diagnostic:
        return None
    try:
        stall = stall_from_diagnostic(management)
        decision = decide(management, stall)
    except NotAcknowledgeable as exc:
        with transaction.atomic():
            _record_exit(management, fence, diagnostic, exc.code)
        return None
    except adapter_client.AdapterError:
        logger.warning("Settlement issue for management %s is not published: the adapter is unavailable", pk)
        return None
    with transaction.atomic():
        if not _record_exit(management, stall.fence, diagnostic, EXIT_ACKNOWLEDGEABLE):
            return None
        identity = {
            "management_id": pk,
            "mapping_epoch": mapping_epoch(management),
            "adapter_device_id": management.adapter_device_id,
            "store_incarnation": stall.store_incarnation,
            "job_id": stall.job_id,
            "revision_digest": decision.revision_digest,
        }
        existing = NSOSettlementIssue.objects.select_for_update().filter(**identity).first()
        others = NSOSettlementIssue.objects.select_for_update().filter(management_id=pk, state="open")
        if existing is not None:
            others = others.exclude(pk=existing.pk)
        now = timezone.now()
        for other in others:
            other.state = "superseded"
            other.closed_at = now
            other.save(update_fields=("state", "closed_at"))
        if existing is not None:
            if existing.state in {"resolved", "superseded"}:
                existing.state = "open"
                existing.closed_at = None
                existing.save(update_fields=("state", "closed_at"))
            return existing
        issue = NSOSettlementIssue.objects.create(
            **identity,
            settle_seq=decision.target["settle_seq"],
            request_fence=stall.fence,
            stall_class=stall.stall_class,
            stall_reason=stall.reason,
            position=decision.target["position"],
            cursor_before=decision.target["cursor_before"],
            cursor_after=decision.target["cursor_after"],
            evidence_digest=decision.evidence_digest,
            impact_digest=decision.impact_digest,
            contract_fingerprint=CONTRACT_FINGERPRINT,
            proposal=decision.proposal,
        )
        NSOSettlementIssueOwner.objects.bulk_create(
            NSOSettlementIssueOwner(
                issue=issue,
                model_label=owner["model_label"],
                overlay_pk=owner["overlay_pk"],
                snapshot_ids=owner["snapshot_ids"],
                attempt_id=owner["attempt_id"],
                selection_ids=owner["selection_ids"],
                contributors=owner["contributors"],
                unknown=owner["unknown"],
                authority=owner["authority"],
                order_bound=owner["order_bound"],
                proposed_effect=owner["effect"],
            )
            for owner in decision.owners
        )
    return issue


def mapping_epoch(management):
    from .durable_capture import mapping_epoch

    return mapping_epoch(management)


def retire_open_issues(management):
    """Close open revisions whose diagnostic or request fence is no longer current."""
    from .models import NSOSettlementIssue
    from .settlement_evidence import matches_request_fence
    from .settlement_resolution import CERTIFIED, obligation_resolution

    diagnostic = management.settle_evidence_error or {}
    now = timezone.now()
    for issue in NSOSettlementIssue.objects.select_for_update().filter(management=management, state="open"):
        if (
            matches_request_fence(management, issue.request_fence)
            and diagnostic.get("request_fence") == issue.request_fence
            and diagnostic.get("job_id") == issue.job_id
            and diagnostic.get("store_incarnation") == issue.store_incarnation
        ):
            continue
        progressed = (
            management.settle_cursor_incarnation == issue.store_incarnation
            and management.settle_cursor_device_id == issue.adapter_device_id
            and (management.settle_cursor_seq or 0) >= issue.settle_seq
            and issue.position == "next_feed"
        ) or (
            issue.position == "historical"
            and all(
                obligation_resolution(
                    management, issue.store_incarnation, obligation["generation_id"], obligation["carrier_job_id"]
                )
                == CERTIFIED
                for obligation in issue.proposal["obligations"]
            )
        )
        issue.state = "resolved" if progressed else "superseded"
        issue.closed_at = now
        issue.save(update_fields=("state", "closed_at"))
