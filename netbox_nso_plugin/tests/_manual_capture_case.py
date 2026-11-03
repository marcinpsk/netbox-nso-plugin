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


def admit_captured_attempt(attempt):
    """Use a ready adapter verdict for tests of local lifecycle invariants."""
    from netbox_nso_plugin.durable_capture import promote_snapshots, record_preflight

    from ._adapter_http import make_apply_preflight

    record_preflight(attempt, make_apply_preflight(attempt.selected))
    promote_snapshots(attempt)
    return attempt


def promote_preflighted_intent(*args, **kwargs):
    from netbox_nso_plugin.apply_state import capture_current_intent

    return admit_captured_attempt(capture_current_intent(*args, **kwargs))


def prepare_and_confirm_apply(management):
    from netbox_nso_plugin.models import NSOApplyAttempt
    from netbox_nso_plugin.views import _prepare_apply

    prepared, selected = _prepare_apply(management)
    admit_captured_attempt(NSOApplyAttempt.objects.get(pk=prepared.attempt_id))
    return prepared, selected


def post_apply(client, management, **kwargs):
    """Exercise preparation, preflight and confirmation through their real URLs."""
    from django.urls import reverse

    preview = client.post(
        reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[management.device_id]), **kwargs
    )
    if preview.status_code != 200:
        return preview
    return client.post(
        reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[management.pk, "apply"]),
        {"apply_preview": preview.json()["apply_preview"]},
        **kwargs,
    )
