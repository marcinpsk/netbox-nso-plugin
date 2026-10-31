# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Fence settlement diagnostics to the row state that started each request."""

import re

_CLOSED_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def request_fence(management):
    """Capture the mapping and complete cursor before reading evidence."""
    from .durable_capture import mapping_epoch

    return {
        "mapping_epoch": mapping_epoch(management),
        "cursor_device_id": management.settle_cursor_device_id,
        "cursor_incarnation": management.settle_cursor_incarnation,
        "cursor_seq": management.settle_cursor_seq,
    }


def matches_request_fence(management, fence):
    """Match the full row state that started an evidence request."""
    return request_fence(management) == fence


def invariant_code(error):
    """Keep an invariant's closed code; free text is not a closed reason."""
    text = str(error)
    return text if _CLOSED_CODE.match(text) else "unclassified"


def write_diagnostic(row, fence, error=None, *, cursor_written=False, stall_class=None, code=None):
    """Write or clear a diagnostic while the caller holds the row lock."""
    from .intent_state import mirror_refresh
    from .settlement_issue import retire_open_issues
    from .signals import suppress_intent_push

    if not matches_request_fence(row, fence) and not (error is None and cursor_written):
        return
    issue = {}
    if error is not None:
        issue = {
            "request_fence": fence,
            "store_incarnation": error.store_incarnation,
            "reason": error.reason,
            "class": stall_class,
        }
        if code is not None:
            issue["code"] = code
        for key in ("job_id", "settle_seq"):
            value = getattr(error, key)
            if type(value) is int and value > 0:
                issue[key] = value
        if getattr(error, "evidence_identity", None) is not None:
            issue["identity"] = error.evidence_identity
        if getattr(error, "evidence_digest", None) is not None:
            issue["digest"] = error.evidence_digest
    with suppress_intent_push(), mirror_refresh(row, ("settle_evidence_error",)) as locked:
        stored = locked.settle_evidence_error or {}
        # A published exit belongs to this exact diagnostic, so an unchanged rewrite keeps it.
        if {key: value for key, value in stored.items() if key != "exit"} == issue:
            return
        locked.settle_evidence_error = issue
        locked.save(update_fields=("settle_evidence_error",))
        retire_open_issues(locked)
