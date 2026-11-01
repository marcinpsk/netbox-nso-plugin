# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Resolve settlement obligations by type and keep acknowledged unknown contributions from success.

An obligation is one exact (generation, carrier job) execution under the settlement cursor's
epoch. It is certified by a consumed execution, acknowledged by an operator-accepted
non-success disposition, or unresolved. Certificate helpers never return an acknowledgement.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime

CERTIFIED = "certified"
ACKNOWLEDGED = "acknowledged"
UNRESOLVED = "unresolved"
UNKNOWN_EFFECT = "unavailable"
#: Consumption states that resolve an obligation; only the first two are certificates.
RESOLVED_STATES = ("consumed", "superseded", ACKNOWLEDGED)


def instant(value):
    """Normalize an aware timestamp for canonical comparison."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError("settlement decisions require aware timestamps")
    return value.astimezone(UTC).isoformat()


def _canonical(value):
    if value is None or isinstance(value, bool | str):
        return value
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical settlement mappings require string keys")
        return {key: _canonical(item) for key, item in value.items()}
    raise TypeError(f"canonical settlement decisions do not accept {type(value).__name__}")


def canonical_json(value) -> str:
    """Encode JSON types exactly; callers order identity-keyed collections before encoding."""
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def cursor_epoch_executions(management, incarnation):
    """Return every execution row under the cursor epoch: store incarnation and adapter device."""
    from .models import NSOApplyExecution

    return NSOApplyExecution.objects.filter(
        management=management, adapter_device_id=management.adapter_device_id, store_incarnation=incarnation
    )


def acknowledged_obligations(management, incarnation) -> frozenset[tuple[int, int]]:
    """Return the exact obligations that an acknowledged issue released in this cursor epoch."""
    return frozenset(
        cursor_epoch_executions(management, incarnation)
        .filter(consumption_state=ACKNOWLEDGED, settlement_issue__state=ACKNOWLEDGED)
        .values_list("generation_id", "carrier_job_id")
    )


def acknowledged_jobs(management, incarnation) -> frozenset[int]:
    """Return the feed jobs whose every obligation an acknowledged issue released."""
    from .models import NSOSettlementIssue

    return frozenset(
        NSOSettlementIssue.objects.filter(
            management=management,
            adapter_device_id=management.adapter_device_id,
            store_incarnation=incarnation,
            state=ACKNOWLEDGED,
        ).values_list("job_id", flat=True)
    )


def obligation_resolution(management, incarnation, generation_id, carrier_job_id) -> str:
    """Classify one exact obligation as certified, acknowledged or unresolved."""
    states = list(
        cursor_epoch_executions(management, incarnation)
        .filter(generation_id=generation_id, carrier_job_id=carrier_job_id)
        .values_list("consumption_state", "invariant_code", "consumed_at", "settlement_issue__state")
    )
    if any(
        state in {"consumed", "superseded"} and not code and consumed_at is not None
        for state, code, consumed_at, _issue in states
    ):
        return CERTIFIED
    if any(state == ACKNOWLEDGED and issue == ACKNOWLEDGED for state, _code, _consumed, issue in states):
        return ACKNOWLEDGED
    return UNRESOLVED


def contribution_key(stream, row):
    """Return the stored form of one logical contribution identity."""
    return [stream, row["subject"], row["key"]]


def unknown_contributions(row, fence):
    """Return acknowledged unknown contributions that still bind this owner's acceptance."""
    from .models import NSOSettlementIssueOwner

    if fence is None:
        return []
    owners = NSOSettlementIssueOwner.objects.filter(
        issue__management_id=fence.management_id,
        issue__mapping_epoch=fence.mapping_epoch,
        issue__state=ACKNOWLEDGED,
        model_label=row._meta.label_lower,
        overlay_pk=row.pk,
        committed_effect=UNKNOWN_EFFECT,
    ).select_related("issue")
    return _binding_unknowns(row, fence, owners.order_by("pk"))


def _binding_unknowns(row, fence, owners):
    pending = []
    for owner in owners:
        # A new acceptance fences the old advisory into history.
        if (
            owner.authority.get("accepted_at") != instant(getattr(row, "accepted_at", None))
            or owner.authority.get("owner_revision") != fence.owner_revision
        ):
            continue
        remaining = [key for key in owner.unknown if key not in owner.covered]
        if remaining:
            pending.append((owner, remaining))
    return pending


def acknowledged_unknown_jobs(rows) -> dict:
    """Map each owner key to the acknowledged jobs whose unknown contributions still bind the row."""
    from .durable_capture import mapping_epoch
    from .models import NSOOverlaySettlementFence, NSOSettlementIssueOwner

    keyed = {(row._meta.label_lower, row.pk): row for row in rows}
    jobs = dict.fromkeys(keyed, ())
    if not keyed:
        return jobs
    labels, pks = {label for label, _pk in keyed}, {pk for _label, pk in keyed}
    owners = {}
    for owner in (
        NSOSettlementIssueOwner.objects.filter(
            issue__state=ACKNOWLEDGED, committed_effect=UNKNOWN_EFFECT, model_label__in=labels, overlay_pk__in=pks
        )
        .select_related("issue")
        .order_by("pk")
    ):
        owners.setdefault((owner.model_label, owner.overlay_pk), []).append(owner)
    if not owners.keys() & keyed.keys():
        return jobs
    fences = NSOOverlaySettlementFence.objects.filter(
        model_label__in={label for label, _pk in owners}, overlay_pk__in={pk for _label, pk in owners}
    ).select_related("management")
    for fence in fences:
        key = (fence.model_label, fence.overlay_pk)
        # Only the current mapping epoch binds, as in the success guard.
        if key not in keyed or key not in owners or fence.mapping_epoch != mapping_epoch(fence.management):
            continue
        candidates = [
            owner
            for owner in owners[key]
            if owner.issue.management_id == fence.management_id and owner.issue.mapping_epoch == fence.mapping_epoch
        ]
        jobs[key] = tuple(
            sorted({owner.issue.job_id for owner, _rest in _binding_unknowns(keyed[key], fence, candidates)})
        )
    return jobs


def cover_unknown_contributions(row, fence, execution, outcomes):
    """Record contributions that a later certificate decides and return the ones still unknown."""
    decided = {json.dumps(contribution_key(outcome["stream"], outcome["row"])) for outcome in outcomes}
    rest = []
    for owner, remaining in unknown_contributions(row, fence):
        later = (
            type(execution.settle_seq) is int
            and execution.settle_seq > owner.order_bound
            and execution.store_incarnation == owner.issue.store_incarnation
        )
        covered = [key for key in remaining if later and json.dumps(key) in decided]
        if covered:
            owner.covered = [*owner.covered, *covered]
            owner.save(update_fields=("covered",))
        left = [key for key in remaining if key not in covered]
        if left:
            rest.append((owner, left))
    return rest


def acknowledged_since(snapshot, row, fence, management) -> bool:
    """Return whether a contiguous acknowledgement chain is the only change since the snapshot froze."""
    from . import durable_capture as capture
    from .models import NSOSettlementIssueOwner

    if fence is None or row.status != "apply_failed" or fence.bookkeeping_revision <= snapshot.expected_fence_revision:
        return False
    chain = {}
    for owner in NSOSettlementIssueOwner.objects.filter(
        issue__management_id=fence.management_id,
        issue__mapping_epoch=snapshot.mapping_epoch,
        issue__state=ACKNOWLEDGED,
        model_label=row._meta.label_lower,
        overlay_pk=row.pk,
        committed_effect=UNKNOWN_EFFECT,
    ):
        revision = owner.authority["fence"]["bookkeeping_revision"]
        if revision in chain:
            return False
        chain[revision] = owner
    if set(range(snapshot.expected_fence_revision, fence.bookkeeping_revision)) - set(chain):
        return False
    first = chain[snapshot.expected_fence_revision].authority
    expected = {
        "status": snapshot.expected_status,
        "last_apply_at": instant(snapshot.expected_last_apply_at),
        "last_apply_error": canonical_digest(snapshot.expected_last_apply_error),
        "apply_attempt_id": None
        if snapshot.expected_apply_attempt_id is None
        else str(snapshot.expected_apply_attempt_id),
    }
    return all(first.get(name) == value for name, value in expected.items()) and capture._matches(
        snapshot,
        row,
        management,
        {},
        "expected",
        check_revisions=False,
        check_bookkeeping_revision=False,
        check_bookkeeping=False,
    )


def unknown_advisory(job_ids) -> str:
    jobs = sorted(set(job_ids))
    noun = "job" if len(jobs) == 1 else "jobs"
    return (
        f"Outcome unknown for adapter {noun} {', '.join(str(job) for job in jobs)}. "
        "Settlement evidence was acknowledged. This does not certify deployment."
    )


def unknown_bookkeeping(row, job_ids, attempt):
    """Return the non-success bookkeeping that keeps desired content and historical apply time."""
    from .refusal_registry import OVERLAYS

    advisory = unknown_advisory(job_ids)
    error = {"message": advisory} if OVERLAYS[row._meta.label_lower].error_type == "JSONField" else advisory
    return {"status": "apply_failed", "last_apply_error": error, "apply_attempt_id": attempt.pk}


def close_completed_attempt(attempt, now):
    """Close an attempt when no snapshot is unresolved and no acknowledged sibling is outstanding."""
    from .models import NSOApplyExecution

    if attempt.overlay_snapshots.filter(settlement_disposition="unresolved").exists():
        return False
    if attempt.trigger_kind == "manual" and attempt.executions.filter(consumption_state=ACKNOWLEDGED).exists():
        # Generation ids belong to one store, so siblings resolve only inside the attempt's own store.
        stores = set(attempt.executions.values_list("store_incarnation", flat=True))
        if len(stores) != 1:
            return False
        admitted = {
            link.get("generation_id")
            for link in (attempt.response or {}).get("generations") or ()
            if isinstance(link, dict)
        }
        resolved = set(
            NSOApplyExecution.objects.filter(
                management_id=attempt.management_id,
                adapter_device_id=attempt.adapter_device_id,
                store_incarnation=stores.pop(),
                generation_id__in=admitted,
                consumption_state__in=RESOLVED_STATES,
            ).values_list("generation_id", flat=True)
        )
        if admitted - resolved:
            return False
    attempt.lifecycle = "terminal"
    attempt.completed_at = now
    attempt.save(update_fields=("lifecycle", "completed_at"))
    return True
