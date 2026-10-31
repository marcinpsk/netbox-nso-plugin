# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Project catalog evidence without device diagnostics or natural row keys."""

import copy

from . import refusal_contract, refusal_protocol

INVALID = "invalid refusal evidence"
UNAVAILABLE = "Execution unavailable; showing previous state."
UNAPPLIED = "Execution includes unapplied contributions. Review the row states."
PENDING_STATES = frozenset({"pending", "deploying", "apply_failed", "unavailable"})
_STATE_SEVERITY = ("apply_failed", "unavailable", "drift", "pending", "deploying", "unknown", "in_sync")
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
    """Return one catalog phrase per distinct reason and message, separated visibly."""
    return " | ".join(sorted({f"{cause['reason']}: {cause['message']}" for cause in catalog_causes(causes)}))


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
    """Label adapter evidence and validate cached errors before exposing a snapshot."""
    projected = copy.deepcopy(snapshot)
    if isinstance(projected, dict):
        if projected:
            projected["source"] = "adapter-reported"
        interfaces = projected.get("interfaces")
        if isinstance(interfaces, list):
            projected["interfaces"] = [interface for interface in interfaces if _project_snapshot_interface(interface)]
        elif interfaces is not None:
            projected["interfaces"] = []
    return projected


def _project_snapshot_interface(interface):
    """Project one cached interface in place; reject a shape that cannot carry validated errors."""
    if not isinstance(interface, dict):
        return False
    attrs = interface.get("attrs") or {}
    if not isinstance(attrs, dict) or not all(isinstance(attr, dict) for attr in attrs.values()):
        return False
    for attr in attrs.values():
        if "last_apply_error" in attr:
            attr["last_apply_error"] = stored_error_projection(attr["last_apply_error"])
    return True


def _current_snapshot(row):
    from .models import NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS

    if not hasattr(row, "_meta") or row._meta.label_lower not in OVERLAYS or row.pk is None:
        return None
    if hasattr(row, "_refusal_current"):
        return row._refusal_current
    # Fences at other mapping epochs share the owner key; only the current epoch's fence counts.
    fences = (
        NSOOverlaySettlementFence.objects.filter(
            model_label=row._meta.label_lower,
            overlay_pk=row.pk,
            accepted_at=row.accepted_at,
            current_attempt__isnull=False,
            last_execution__consumption_state="consumed",
        )
        .select_related("management", "last_execution")
        .prefetch_related("last_execution__overlay_snapshots")
    )
    return next((current for fence in fences if (current := _snapshot_for_fence(row, fence)) is not None), None)


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
    """Load current execution links and previous refusals once for a display collection."""
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
    _load_previous_refusals(owners)
    from .settlement_resolution import acknowledged_unknown_jobs

    for key, jobs in acknowledged_unknown_jobs(owners.values()).items():
        owners[key]._acknowledged_jobs = jobs
    return rows


def _acknowledged_jobs(row):
    """Return the acknowledged jobs whose unknown contributions still bind this owner."""
    from .refusal_registry import OVERLAYS
    from .settlement_resolution import acknowledged_unknown_jobs

    if not hasattr(row, "_meta") or row._meta.label_lower not in OVERLAYS or row.pk is None:
        return ()
    if not hasattr(row, "_acknowledged_jobs"):
        row._acknowledged_jobs = acknowledged_unknown_jobs([row])[(row._meta.label_lower, row.pk)]
    return row._acknowledged_jobs


def settled_interface_owners(queryset):
    """Select interface owners that row_projection can only classify in_sync, without projecting them."""
    from django.db.models import Exists, F, OuterRef, Q

    from .models import NSOInterfaceState, NSOOverlaySettlementFence

    current = NSOOverlaySettlementFence.objects.filter(
        model_label=NSOInterfaceState._meta.label_lower, overlay_pk=OuterRef("pk"), current_attempt__isnull=False
    )
    # Exact stored values imply matches_device_value; any looser match stays a projection candidate.
    equal = (
        Q(attribute="description", nso_value=F("interface__description"))
        | Q(attribute="enabled", nso_value="True", interface__enabled=True)
        | Q(attribute="enabled", nso_value="False", interface__enabled=False)
    )
    return queryset.filter(
        equal, Q(last_apply_error__isnull=True) | Q(last_apply_error={}), status__in=("imported", "in_sync")
    ).exclude(Exists(current))


def _load_previous_refusals(owners):
    from .models import NSOApplyExecution, NSOApplyOverlaySnapshot

    # Only a text error reads the previous refusal; see row_projection.
    keyed = {
        (row.apply_attempt_id, label, pk): row
        for (label, pk), row in owners.items()
        if getattr(row, "apply_attempt_id", None) is not None
        and isinstance(row.last_apply_error, str)
        and row.last_apply_error
    }
    for row in keyed.values():
        row._refusal_previous = None
    if not keyed:
        return
    snapshots = {
        snapshot.pk: snapshot
        for snapshot in NSOApplyOverlaySnapshot.objects.filter(
            attempt_id__in={attempt_id for attempt_id, _label, _pk in keyed},
            model_label__in={label for _attempt_id, label, _pk in keyed},
            overlay_pk__in={pk for _attempt_id, _label, pk in keyed},
            settlement_disposition="refused",
        ).only("attempt_id", "model_label", "overlay_pk", "contributors")
        if (snapshot.attempt_id, snapshot.model_label, snapshot.overlay_pk) in keyed
    }
    links = list(
        NSOApplyOverlaySnapshot.executions.through.objects.filter(nsoapplyoverlaysnapshot_id__in=snapshots).values_list(
            "nsoapplyoverlaysnapshot_id", "nsoapplyexecution_id"
        )
    )
    executions = NSOApplyExecution.objects.filter(
        pk__in={execution_id for _snapshot_id, execution_id in links}, consumption_state="consumed"
    ).in_bulk()
    consumed = {snapshot_id: [] for snapshot_id in snapshots}
    for snapshot_id, execution_id in links:
        if execution_id in executions:
            consumed[snapshot_id].append(executions[execution_id])
    for snapshot in snapshots.values():
        row = keyed[(snapshot.attempt_id, snapshot.model_label, snapshot.overlay_pk)]
        row._refusal_previous = (snapshot, consumed[snapshot.pk])


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
    if hasattr(row, "_refusal_previous"):
        return None if row._refusal_previous is None else _snapshot_refusal(*row._refusal_previous)
    snapshot = NSOApplyOverlaySnapshot.objects.filter(
        attempt_id=row.apply_attempt_id,
        model_label=row._meta.label_lower,
        overlay_pk=row.pk,
        settlement_disposition="refused",
    ).first()
    if snapshot is None:
        return None
    return _snapshot_refusal(snapshot, snapshot.executions.filter(consumption_state="consumed"))


def row_projection(row, interface=None):
    """Decide owner state, pending membership and safe execution evidence together."""
    from .refusal_registry import OVERLAYS
    from .settlement_resolution import unknown_advisory

    error = getattr(row, "last_apply_error", None)
    acknowledged = _acknowledged_jobs(row)
    historical_refusal = None
    projection = {"refusal": None, "refusal_text": "", "advisory": "", "error": None, "error_history": False}
    if isinstance(error, dict) and "refusals" in error:
        projection["refusal"] = safe_projection(error["refusals"])
    current = _current_snapshot(row)
    projection["execution_disposition"] = current[0].settlement_disposition if current else None
    projection["advisory"] = UNAVAILABLE if projection["execution_disposition"] == "unavailable" else ""
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
    # The acknowledgement stores its advisory as the error text; it shows as the advisory only.
    advisories = (
        {unknown_advisory(acknowledged), *(unknown_advisory([job]) for job in acknowledged)} if acknowledged else set()
    )
    if projection["refusal"] is not None:
        projection["refusal_text"] = projection["refusal"]["text"]
        projection["error"] = projection["refusal_text"]
    elif error and projection["error"] is None:
        message = (error.get("message") or str(error)) if isinstance(error, dict) else error
        projection["error"] = None if message in advisories else message
    if not projection["advisory"] and acknowledged:
        projection["advisory"] = unknown_advisory(acknowledged)
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
    kind, label, owned = _row_classification(row, projection, interface, acknowledged)
    if kind == "in_sync":
        # A current not_applied restoration keeps its prior error as history; older failure text is not shown.
        restored = projection["execution_disposition"] == "not_applied" and projection["error"] is not None
        projection.update(refusal=None, refusal_text="", error_history=restored)
        if not restored:
            projection["error"] = None
    pending = kind in PENDING_STATES
    projection.update(
        state=kind,
        kind=kind,
        label=label,
        owned=owned,
        pending=pending,
        bucket="pending" if pending else "in_sync" if kind == "in_sync" else "drift",
    )
    return projection


def _stored_state(status, owned):
    """Classify lifecycle status before current execution and value evidence."""
    if status == "deploying":
        return "deploying", "deploying"
    if status in {"imported", "in_sync"}:
        return "in_sync", "in sync"
    if owned and status == "apply_failed":
        return "apply_failed", "apply failed"
    if status in {"changed", "conflict", "accepted", "apply_failed"}:
        return ("pending", "pending apply") if owned else ("drift", "drift")
    return "unknown", status or "unknown"


def _row_classification(row, projected, interface, acknowledged=()):
    """Give current execution evidence precedence over stored status and value equality."""
    from .status_machine import OWNED_STATES
    from .summary import _COMPARABLE_IFACE_ATTRS, _netbox_value_for, matches_device_value

    owned = row.status in OWNED_STATES
    if projected["refusal"] is not None and not projected["refusal"]["valid"]:
        return "apply_failed", "apply failed", owned
    if projected["execution_disposition"] == "refused":
        return "apply_failed", "apply failed", owned
    if projected["advisory"] == UNAVAILABLE:
        return "unavailable", "previous " + _stored_state(row.status, owned)[1], owned
    if acknowledged and row.status == "apply_failed":
        # An acknowledged unknown outcome never reads as success, also when the values match.
        return "apply_failed", "outcome unknown", owned
    if interface is None and hasattr(row, "attribute"):
        interface = getattr(row, "interface", None)
    # A current restoration or an ordinary error outranks value equality; an uncorrelated refusal does not.
    retained_failure = row.status == "apply_failed" and (
        projected["execution_disposition"] == "not_applied" or (projected["error"] and projected["refusal"] is None)
    )
    if (
        interface is not None
        and row.status != "deploying"
        and not retained_failure
        and row.attribute in _COMPARABLE_IFACE_ATTRS
    ):
        if matches_device_value(row.attribute, _netbox_value_for(row.attribute, interface), row.nso_value):
            return "in_sync", "in sync", owned
        if row.status == "apply_failed":
            return "apply_failed", "apply failed", owned
        return ("pending", "pending apply", owned) if owned else ("drift", "drift", owned)
    kind, label = _stored_state(row.status, owned)
    return kind, label, owned


def row_state(row):
    """Read the common owner classification."""
    projected = row_projection(row)
    return projected["state"], projected["label"]


def group_projection(projections):
    """Use one projected owner's state and bucket for a grouped grid row."""
    selected = min(projections, key=lambda item: _STATE_SEVERITY.index(item["state"]), default=None)
    if selected is None:
        return {"state": "in_sync", "kind": "in_sync", "label": "in sync", "pending": False, "bucket": "in_sync"}
    return {key: selected[key] for key in ("state", "kind", "label", "pending", "bucket")}


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
    identities = {refusal_contract.logical_key(row) for row in result["row_outcomes"]}
    phrases = set()
    for cause, catalog in zip(attributed, projected["causes"], strict=True):
        logical_rows = cause["logical_rows"]
        foreign = [
            logical["row"] for logical in logical_rows if refusal_contract.logical_key(logical) not in identities
        ]
        if len(foreign) < len(logical_rows):
            phrases.add(f"{catalog['reason']}: {catalog['message']}")
        else:
            foreign = [catalog["row"]]
        # A mixed cause also names each linked row outside this Apply.
        for row in foreign:
            phrases.add(
                f"Blocked by {catalog['family']} {row['subject']} {row['key']} "
                f"(not part of this Apply): {catalog['message']}"
            )
    projected["text"] = " | ".join(sorted(phrases))
    rollback = result["commit_acknowledged"] is False and all(
        cause["sender_phase"] in {"pre_commit_dry_run", "commit"} for cause in attributed
    )
    summary = "Execution evidence. "
    if rollback:
        summary = (
            "Refused; commit not attempted. "
            if all(cause["sender_phase"] == "pre_commit_dry_run" for cause in attributed)
            else "Refused; nothing committed. "
        )
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
    """Summarize current owners and executions with no local owner snapshots."""
    from .durable_capture import mapping_epoch
    from .refusal_registry import OVERLAYS

    if management is None:
        return []
    fences = list(management.settlement_fences.filter(current_attempt__isnull=False, last_execution__isnull=False))
    rows = []
    for label in {fence.model_label for fence in fences}:
        spec = OVERLAYS[label]
        # Classification reads the owner relation, so load it with the rows.
        related = spec.device_lookup.rsplit("__", 1)[0]
        rows.extend(
            spec.model.objects.filter(pk__in=[f.overlay_pk for f in fences if f.model_label == label]).select_related(
                related
            )
        )
    executions = {
        execution.pk: (execution, False)
        for execution in management.apply_executions.filter(
            mapping_epoch=mapping_epoch(management),
            adapter_device_id=management.adapter_device_id,
            overlay_snapshots__isnull=True,
            consumption_state="consumed",
        )
    }
    for row in project_rows(rows):
        current = _current_snapshot(row)
        if current is not None:
            execution = current[1]
            unavailable = row_projection(row)["advisory"] == UNAVAILABLE
            previous = executions.get(execution.pk)
            executions[execution.pk] = (execution, unavailable or bool(previous and previous[1]))
    summaries = []
    for execution, unavailable in sorted(executions.values(), key=lambda item: item[0].terminal_at, reverse=True):
        stored = execution.terminal_projection
        result = stored.get("carrier_job_result") if isinstance(stored, dict) else None
        projected = document_projection(result)
        if unavailable:
            summary = projected["summary"]
            projected["summary"] = UNAVAILABLE + (" " + summary if summary and summary != UNAVAILABLE else "")
        if projected["summary"]:
            summaries.append(projected)
    return summaries


def settlement_stall_projection(management):
    """Show a diagnostic only while its complete request fence is current."""
    from .settlement_evidence import matches_request_fence

    if management is None:
        return None
    issue = management.settle_evidence_error
    if issue and matches_request_fence(management, issue.get("request_fence")):
        return issue
    return None


def validate_interface_errors(interfaces):
    """Reject forged catalog records at the interface HTTP boundary."""
    if not isinstance(interfaces, list):
        return
    for interface in interfaces:
        if not isinstance(interface, dict):
            raise refusal_contract.ContractError("invalid interface evidence")
        attrs = interface.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise refusal_contract.ContractError("invalid interface evidence")
        for attr in attrs.values():
            if not isinstance(attr, dict):
                raise refusal_contract.ContractError("invalid interface evidence")
            error = attr.get("last_apply_error")
            if isinstance(error, dict) and "refusals" in error:
                catalog_causes(error["refusals"])
