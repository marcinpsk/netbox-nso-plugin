# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Project catalog evidence without device diagnostics or natural row keys."""

import copy

from . import refusal_contract, refusal_protocol

INVALID = "invalid refusal evidence"
UNAVAILABLE = "Execution unavailable; showing previous state."
UNAPPLIED = "Execution includes unapplied contributions. Review the row states."
_CAUSE_FIELDS = refusal_contract._CAUSE_ORDER


def catalog_causes(causes):
    """Revalidate each complete record and copy text from the installed catalog."""
    if not isinstance(causes, list) or not causes:
        raise refusal_contract.ContractError("invalid catalog refusal")
    projected = []
    for cause in causes:
        refusal_contract._cause(cause)
        entry = refusal_protocol.MESSAGES[cause["message_id"]]
        projected.append({**copy.deepcopy(cause), "reason": entry.reason, "message": entry.text})
    return projected


def format_causes(causes):
    """Return one catalog phrase per distinct reason and message."""
    return " ".join(sorted({f"{cause['reason']}: {cause['message']}" for cause in catalog_causes(causes)}))


def safe_projection(causes):
    """Reject the complete cause set when any stored record is invalid."""
    try:
        projected = catalog_causes(causes)
    except (refusal_contract.ContractError, TypeError, ValueError, KeyError):
        return {"valid": False, "causes": [], "text": INVALID}
    return {"valid": True, "causes": projected, "text": format_causes(projected)}


def attributed_projection(causes):
    """Strip attribution metadata only after validating the complete records."""
    try:
        refusal_contract.validate_refusals(causes)
        return safe_projection([{key: cause[key] for key in _CAUSE_FIELDS} for cause in causes])
    except (refusal_contract.ContractError, TypeError, ValueError, KeyError):
        return safe_projection(None)


def stored_error_projection(error):
    """Replace structured diagnostics with validated catalog evidence."""
    if isinstance(error, dict) and "refusals" in error:
        projected = safe_projection(error["refusals"])
        return {"refusals": projected["causes"], "message": projected["text"]}
    return error


def snapshot_projection(snapshot):
    """Validate cached interface errors before exposing a management snapshot."""
    projected = copy.deepcopy(snapshot)
    if isinstance(projected, dict):
        for interface in projected.get("interfaces") or []:
            for attr in (interface.get("attrs") or {}).values():
                if "last_apply_error" in attr:
                    attr["last_apply_error"] = stored_error_projection(attr["last_apply_error"])
    return projected


def _current_snapshot(row):
    from .models import NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS

    if not hasattr(row, "_meta") or row._meta.label_lower not in OVERLAYS or row.pk is None:
        return None
    if hasattr(row, "_refusal_current"):
        return row._refusal_current
    fence = (
        NSOOverlaySettlementFence.objects.filter(
            model_label=row._meta.label_lower,
            overlay_pk=row.pk,
            accepted_at=row.accepted_at,
            current_attempt__isnull=False,
            last_execution__consumption_state="consumed",
        )
        .select_related("management", "last_execution")
        .prefetch_related("last_execution__overlay_snapshots")
        .first()
    )
    return _snapshot_for_fence(row, fence) if fence is not None else None


def _snapshot_for_fence(row, fence):
    from .durable_capture import mapping_epoch

    if fence.mapping_epoch != mapping_epoch(fence.management) or fence.accepted_at != row.accepted_at:
        return None
    for snapshot in fence.last_execution.overlay_snapshots.all():
        if (
            snapshot.attempt_id == fence.current_attempt_id
            and snapshot.model_label == row._meta.label_lower
            and snapshot.overlay_pk == row.pk
            and snapshot.accepted_at == row.accepted_at
            and snapshot.expected_owner_revision == fence.owner_revision
        ):
            return snapshot, fence.last_execution
    return None


def project_rows(rows):
    """Load current execution links once for a display collection."""
    from .models import NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS

    rows = list(rows)
    owners = {
        (row._meta.label_lower, row.pk): row
        for row in rows
        if hasattr(row, "_meta") and row._meta.label_lower in OVERLAYS
    }
    for row in owners.values():
        row._refusal_current = None
    if owners:
        fences = (
            NSOOverlaySettlementFence.objects.filter(
                model_label__in={label for label, _pk in owners},
                overlay_pk__in={pk for _label, pk in owners},
                current_attempt__isnull=False,
                last_execution__consumption_state="consumed",
            )
            .select_related("management", "last_execution")
            .prefetch_related("last_execution__overlay_snapshots")
        )
        executions = {}
        for fence in fences:
            fence.last_execution = executions.setdefault(fence.last_execution_id, fence.last_execution)
            row = owners.get((fence.model_label, fence.overlay_pk))
            if row is not None:
                current = _snapshot_for_fence(row, fence)
                if current is not None:
                    row._refusal_current = current
    return rows


def _snapshot_refusal(snapshot, executions):
    try:
        refs = {
            (item["stream"], item["logical_ref"]["subject"], item["logical_ref"]["key"])
            for item in snapshot.contributors["rows"]
        }
        causes = []
        for execution in executions:
            if not hasattr(execution, "_display_causes_by_row"):
                generation = execution.terminal_projection
                refusal_contract.validate_generation(generation)
                by_row = {}
                for outcome in generation["row_outcomes"]:
                    key = (outcome["stream"], outcome["row"]["subject"], outcome["row"]["key"])
                    by_row.setdefault(key, []).extend(outcome["refusals"])
                execution._display_causes_by_row = by_row
            for ref in refs:
                causes.extend(execution._display_causes_by_row.get(ref, ()))
        return safe_projection(causes)
    except (refusal_contract.ContractError, TypeError, ValueError, KeyError):
        return safe_projection(None)


def _previous_refusal(row):
    from .models import NSOApplyOverlaySnapshot

    if not hasattr(row, "_meta") or getattr(row, "apply_attempt_id", None) is None:
        return None
    snapshot = NSOApplyOverlaySnapshot.objects.filter(
        attempt_id=row.apply_attempt_id,
        model_label=row._meta.label_lower,
        overlay_pk=row.pk,
        settlement_disposition="refused",
    ).first()
    if snapshot is None:
        return None
    return _snapshot_refusal(snapshot, snapshot.executions.filter(consumption_state="consumed"))


def row_projection(row):
    """Keep restored history separate from the current execution's refusal."""
    from .refusal_registry import OVERLAYS

    error = getattr(row, "last_apply_error", None)
    historical_refusal = None
    projection = {"refusal": None, "refusal_text": "", "advisory": "", "error": None}
    if isinstance(error, dict) and "refusals" in error:
        projection["refusal"] = safe_projection(error["refusals"])
    current = _current_snapshot(row)
    projection["execution_disposition"] = current[0].settlement_disposition if current else None
    if isinstance(error, str) and error and projection["execution_disposition"] != "refused":
        projection["refusal"] = _previous_refusal(row)
    if current is not None and (projection["refusal"] is None or projection["refusal"]["valid"]):
        snapshot, execution = current
        if snapshot.settlement_disposition == "refused":
            projection["refusal"] = _snapshot_refusal(snapshot, [execution])
        elif snapshot.settlement_disposition == "unavailable":
            if projection["refusal"] is not None:
                historical_refusal = projection["refusal"]
                projection["error"] = projection["refusal"]["text"]
            projection["refusal"] = None
            projection["advisory"] = UNAVAILABLE
    if projection["refusal"] is not None:
        projection["refusal_text"] = projection["refusal"]["text"]
        projection["error"] = projection["refusal_text"]
    elif error and projection["error"] is None:
        projection["error"] = (error.get("message") or str(error)) if isinstance(error, dict) else error
    if not projection["advisory"]:
        projection["advisory"] = getattr(row, "last_result_advisory", "") or ""
    spec = OVERLAYS.get(row._meta.label_lower) if hasattr(row, "_meta") else None
    if spec is not None and spec.error_type == "JSONField":
        error_refusal = projection["refusal"] if projection["refusal"] is not None else historical_refusal
        projection["error_value"] = (
            {"refusals": error_refusal["causes"], "message": error_refusal["text"]}
            if error_refusal is not None
            else copy.deepcopy(error)
        )
    else:
        projection["error_value"] = projection["error"] if projection["error"] is not None else error
    return projection


def row_state(row):
    """Show qualified prior state when the latest execution is unavailable."""
    from .status_machine import OWNED_STATES
    from .summary import display_state

    projected = row_projection(row)
    owned = row.status in OWNED_STATES
    if projected["refusal"] is not None and not projected["refusal"]["valid"]:
        return "apply_failed", INVALID
    if projected["execution_disposition"] == "refused":
        return "apply_failed", "apply failed"
    if projected["advisory"] == UNAVAILABLE:
        return "unavailable", "previous " + display_state(row.status, owned, distinguish_failed=True)[1]
    return display_state(row.status, owned, distinguish_failed=True)


def document_projection(result):
    """Claim rollback only from a complete precommit refusal certificate."""
    try:
        refusal_contract.validate_result(result)
    except (refusal_contract.ContractError, TypeError, ValueError, KeyError):
        return {"refusal": safe_projection(None), "refusal_text": INVALID, "summary": INVALID}
    attributed = result["refusals"]
    if not attributed:
        unavailable = any(row["disposition"] == "unavailable" for row in result["row_outcomes"])
        unapplied = any(row["disposition"] == "not_applied" for row in result["row_outcomes"])
        summary = UNAVAILABLE if unavailable else UNAPPLIED if unapplied else ""
        return {"refusal": None, "refusal_text": "", "summary": summary}
    projected = attributed_projection(attributed)
    rollback = (
        result["commit_acknowledged"] is False
        and all(row["disposition"] in {"refused", "not_applied"} for row in result["row_outcomes"])
        and any(row["disposition"] == "refused" for row in result["row_outcomes"])
        and all(
            cause["provenance"] == "current" and cause["sender_phase"] in {"pre_commit_dry_run", "commit"}
            for cause in attributed
        )
    )
    summary = "Refused; nothing committed. " if rollback else "Execution evidence. "
    return {"refusal": projected, "refusal_text": projected["text"], "summary": summary + projected["text"]}


def job_projection(job):
    """Keep structured refusals outside the ordinary free-form error path."""
    result = job.get("result")
    if not isinstance(result, dict) or "row_outcomes" not in result:
        return job
    projected = document_projection(result)
    safe = {**job, **projected, "refusal_summary": projected["summary"]}
    if projected["summary"]:
        safe["error"] = None
        safe["result"] = {
            key: value
            for key, value in result.items()
            if key in {"row_outcomes", "refusals", "settlement_owners", "commit_acknowledged"}
            or key.endswith("_count_by_outcome")
        }
    return safe


def device_execution_summaries(management):
    """Read only executions still named by a current owner fence."""
    from .durable_capture import mapping_epoch
    from .models import NSOApplyExecution

    if management is None:
        return []
    executions = (
        NSOApplyExecution.objects.filter(
            settlement_fences__management=management,
            settlement_fences__mapping_epoch=mapping_epoch(management),
            settlement_fences__current_attempt__isnull=False,
            consumption_state="consumed",
        )
        .distinct()
        .order_by("-terminal_at")
    )
    summaries = []
    for execution in executions:
        stored = execution.terminal_projection
        result = stored.get("carrier_job_result") if isinstance(stored, dict) else None
        projected = document_projection(result)
        if execution.overlay_snapshots.filter(settlement_disposition="unavailable").exists():
            summary = projected["summary"]
            projected["summary"] = UNAVAILABLE + (" " + summary if summary and summary != UNAVAILABLE else "")
        if projected["summary"]:
            summaries.append(projected)
    return summaries


def validate_interface_errors(interfaces):
    """Reject forged catalog records at the interface HTTP boundary."""
    if not isinstance(interfaces, list):
        return
    for interface in interfaces:
        if not isinstance(interface, dict):
            raise refusal_contract.ContractError("invalid interface evidence")
        for attr in (interface.get("attrs") or {}).values():
            if not isinstance(attr, dict):
                raise refusal_contract.ContractError("invalid interface evidence")
            error = attr.get("last_apply_error")
            if isinstance(error, dict) and "refusals" in error:
                catalog_causes(error["refusals"])
