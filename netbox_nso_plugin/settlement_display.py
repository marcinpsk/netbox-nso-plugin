# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Project settlement stalls, their exits and their acknowledgements for the device NSO tab.

The projections read stored rows only. The banner offers an issue only while the stall
diagnostic is current under its request fence and the open issue was published for it.
"""

from __future__ import annotations

from collections import Counter

from .refusal_ledger import OWNER_EFFECTS
from .settlement_acknowledgement import MAX_REASON_LENGTH
from .settlement_issue import EXIT_ACKNOWLEDGEABLE

STALL_CLASSES = {"invalid_evidence": "invalid evidence", "evidence_invariant": "evidence invariant"}
EXIT_TEXT = {
    "unidentified": "Evidence unavailable: the job or its evidence store cannot be identified.",
    "unordered": "The position of the job in the settlement feed cannot be proved.",
    "ownership_unbounded": "The rows that this job can affect cannot be bounded.",
    "resolved_obligation": "This job already has a recorded settlement outcome.",
    "unbound_evidence": "The evidence is not bound to a stored identity.",
}
UNEVALUATED = "The exit for this stall is not evaluated yet."
EFFECT_TEXT = {"unavailable": "outcome unknown", "superseded": "kept, newer state", "missing_owner": "missing row"}
EFFECT_BADGE = {
    "unavailable": "text-bg-danger",
    "superseded": "text-bg-info",
    "missing_owner": "text-bg-warning text-dark",
}
STALE = "Acknowledgement refused because the evidence or settlement position changed. Review the current issue."
REFUSAL_TEXT = {
    "stale": STALE,
    "revision_mismatch": STALE,
    "not_open": STALE,
    "observation_unavailable": (
        "Acknowledgement refused because the adapter evidence could not be read. Nothing changed."
    ),
    "invalid_reason": f"Acknowledgement refused: enter a reason of at most {MAX_REASON_LENGTH} characters.",
    "invalid_actor": "Acknowledgement refused: the operator identity is not valid.",
}
HISTORY_LIMIT = 20


def acknowledged_text(issue, replayed):
    """Return the operator message for one acknowledgement receipt."""
    if replayed:
        return f"Adapter job {issue.job_id} was already acknowledged as outcome unknown. Nothing changed."
    return f"Acknowledged adapter job {issue.job_id} as outcome unknown. Settlement resumes."


def _row_names(keys):
    """Name each owner row; a row that no longer exists keeps its model and key."""
    from .refusal_registry import OVERLAYS

    names = {}
    by_label = {}
    for label, pk in keys:
        by_label.setdefault(label, set()).add(pk)
    for label, pks in by_label.items():
        model = OVERLAYS[label].model
        rows = model.objects.in_bulk(pks)
        for pk in pks:
            row = rows.get(pk)
            names[(label, pk)] = (
                f"{model._meta.verbose_name}: {row}"
                if row is not None
                else f"{model._meta.verbose_name} #{pk} (row deleted)"
            )
    return names


def _issue_projection(issue):
    owners = list(issue.owners.order_by("model_label", "overlay_pk"))
    names = _row_names((owner.model_label, owner.overlay_pk) for owner in owners)
    counts = Counter(owner.proposed_effect for owner in owners)
    return {
        "pk": issue.pk,
        "revision_digest": issue.revision_digest,
        "job_id": issue.job_id,
        "settle_seq": issue.settle_seq,
        "historical": issue.position == "historical",
        "cursor_before": issue.cursor_before,
        "cursor_after": issue.cursor_after,
        "generation_ids": [obligation["generation_id"] for obligation in issue.proposal["obligations"]],
        "counts": {effect: counts[effect] for effect in OWNER_EFFECTS},
        "rows": [
            {
                "key": f"{owner.model_label}:{owner.overlay_pk}",
                "name": names[(owner.model_label, owner.overlay_pk)],
                "effect": EFFECT_TEXT[owner.proposed_effect],
                "badge": EFFECT_BADGE[owner.proposed_effect],
                "unknown": len(owner.unknown),
                "contributors": len(owner.contributors),
            }
            for owner in owners
        ],
    }


def stall_exit(management, stall):
    """Project the exit of the current stall: its open issue, or the closed reason it has none."""
    from .models import NSOSettlementIssue

    if stall is None:
        return None
    projection = {
        "stall_class": STALL_CLASSES.get(stall.get("class"), ""),
        "code": stall.get("code", ""),
        "issue": None,
        "text": "",
        "max_reason": MAX_REASON_LENGTH,
    }
    code = stall.get("exit")
    if code == EXIT_ACKNOWLEDGEABLE:
        issue = NSOSettlementIssue.objects.filter(
            management=management,
            state="open",
            job_id=stall.get("job_id"),
            store_incarnation=stall.get("store_incarnation"),
        ).first()
        # The banner is fenced already; the issue must also belong to the same request fence.
        if issue is not None and issue.request_fence == stall.get("request_fence"):
            projection["issue"] = _issue_projection(issue)
            return projection
        code = None
    projection["text"] = EXIT_TEXT.get(code, UNEVALUATED)
    return projection


def acknowledgement_history(management):
    """Return the most recent acknowledgements of *management*, newest first, with their effects."""
    if management is None:
        return {"entries": [], "truncated": False}
    issues = list(
        management.settlement_issues.filter(state="acknowledged").order_by("-acknowledged_at", "-pk")[
            : HISTORY_LIMIT + 1
        ]
    )
    shown = issues[:HISTORY_LIMIT]
    names = _row_names(
        (effect["model_label"], effect["overlay_pk"]) for issue in shown for effect in issue.committed_effects
    )
    entries = [
        {
            "at": issue.acknowledged_at,
            "actor": issue.actor_label,
            "reason": issue.acknowledgement_reason,
            "job_id": issue.job_id,
            "settle_seq": issue.settle_seq,
            "generation_ids": [obligation["generation_id"] for obligation in issue.proposal["obligations"]],
            "historical": issue.position == "historical",
            "cursor_before": issue.cursor_before,
            "cursor_after": issue.cursor_after,
            "effects": [
                {
                    "key": f"{effect['model_label']}:{effect['overlay_pk']}:{effect['effect']}",
                    "name": names[(effect["model_label"], effect["overlay_pk"])],
                    "text": EFFECT_TEXT[effect["effect"]],
                    "badge": EFFECT_BADGE[effect["effect"]],
                }
                for effect in sorted(
                    issue.committed_effects, key=lambda item: (item["model_label"], item["overlay_pk"])
                )
            ],
        }
        for issue in shown
    ]
    return {"entries": entries, "truncated": len(issues) > HISTORY_LIMIT}
