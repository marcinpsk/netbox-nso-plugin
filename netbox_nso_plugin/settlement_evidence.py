# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Fence settlement diagnostics to the row state that started each request."""


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


def write_diagnostic(row, fence, error=None, *, cursor_written=False):
    """Write or clear a diagnostic while the caller holds the row lock."""
    from .intent_state import mirror_refresh
    from .signals import suppress_intent_push

    if not matches_request_fence(row, fence) and not (error is None and cursor_written):
        return
    issue = {}
    if error is not None:
        issue = {
            "request_fence": fence,
            "store_incarnation": error.store_incarnation,
            "reason": error.reason,
        }
        for key in ("job_id", "settle_seq"):
            value = getattr(error, key)
            if type(value) is int and value > 0:
                issue[key] = value
    with suppress_intent_push(), mirror_refresh(row, ("settle_evidence_error",)) as locked:
        locked.settle_evidence_error = issue
        locked.save(update_fields=("settle_evidence_error",))
