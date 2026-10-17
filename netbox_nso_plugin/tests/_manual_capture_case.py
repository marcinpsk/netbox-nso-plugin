# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Prepare real durable selections for focused manual promotion tests."""

from netbox_nso_plugin import delivery, drain

from ._outbox_case import ReceiptAdapter


def store_apply_selection(management, *, adapter=None):
    """Store every protocol receipt and both exact switching preparations."""
    registry = delivery.delivery_keys()
    if adapter is None:
        adapter = ReceiptAdapter()
    direct_selected = {}
    direct_source_revisions = {}
    config, session = adapter.patches()
    with config, session, drain.capture_successful_pushes() as pushed:
        for scope, entry in registry.items():
            if entry.in_protocol:
                outcome = drain.drain_key(management.device_id, scope, mode=delivery.MODE_STORE_ONLY, force=True)
                assert outcome == drain.SUCCEEDED, (scope, outcome)
            else:
                answer = drain.push_now(management.device_id, scope, mode=delivery.MODE_NORMAL, force=True)
                assert answer is not None, scope
                direct_selected[entry.section] = answer["selection_revision"]
                direct_source_revisions[scope] = answer["source_revision"]
    return (
        registry,
        dict(pushed),
        {
            "direct_selected": direct_selected,
            "direct_source_revisions": direct_source_revisions,
        },
    )
