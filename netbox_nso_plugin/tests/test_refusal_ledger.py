# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import RestrictedError


@pytest.fixture
def ledger(request):
    from netbox_nso_plugin import models

    types = tuple(
        getattr(models, name)
        for name in (
            "NSOAcceptedSelection",
            "NSOApplyExecution",
            "NSOApplyOverlaySnapshot",
            "NSOOverlaySettlementFence",
        )
    )
    request.getfixturevalue("db")
    from ._outbox_case import make_device

    device = make_device("refusal-ledger")
    instance = models.NSOInstance.objects.create(name="placeholder-ledger", adapter_instance_id="placeholder-ledger")
    management = models.NSODeviceManagement.objects.create(
        device=device,
        nso_instance=instance,
        nso_device_name="placeholder-device",
        adapter_device_id=17,
    )
    attempt = models.NSOApplyAttempt.objects.create(management=management, trigger_kind="push", source_epoch=3)
    return models, management, attempt, types


def execution_values(attempt, incarnation="placeholder-store"):
    return dict(
        attempt=attempt,
        management=attempt.management,
        mapping_epoch="a" * 64,
        adapter_device_id=17,
        store_incarnation=incarnation,
        generation_id=17,
        generation_seq=9,
        carrier_job_id=23,
        settlement_cohort=9,
        mode="manual",
        source_selectors={"bgp": {"source_push_seq": 41, "frozen_revision": 7}},
    )


def selection_values(management, incarnation=None):
    return dict(
        management=management,
        mapping_epoch="a" * 64,
        store_incarnation=incarnation,
        stream="bgp",
        source_push_seq=41,
        plugin_revision=7,
        request_digest="b" * 64,
        contributor_manifest=[
            {
                "model_label": "netbox_nso_plugin.nsobgppeerstate",
                "overlay_pk": 1,
                "row": {"subject": "bgp.router.scope.peer", "key": "c" * 64},
            }
        ],
    )


def test_apply_snapshot_round_trips_json_and_text_errors(ledger):
    models, management, attempt, (Selection, Execution, Snapshot, Fence) = ledger
    prior = models.NSOApplyAttempt.objects.create(management=management)
    selection = Selection.objects.create(**selection_values(management))
    execution = Execution.objects.create(**execution_values(attempt))
    stamp = datetime(2025, 12, 31, 1, 2, 3, tzinfo=UTC)
    errors = (None, {"code": "placeholder", "message": "placeholder prior error"}, "placeholder prior error", "")
    for pk, error in enumerate(errors, 1):
        snapshot = Snapshot.objects.create(
            attempt=attempt,
            selection=selection,
            model_label="netbox_nso_plugin.nsointerfacestate" if pk < 3 else "netbox_nso_plugin.nsobgppeerstate",
            overlay_pk=pk,
            prior_status="in_sync",
            prior_last_apply_at=stamp,
            prior_last_apply_error=error,
            prior_apply_attempt=prior,
            expected_status="deploying",
            expected_last_apply_at=stamp,
            expected_last_apply_error=None if pk < 3 else "",
            expected_apply_attempt=attempt,
            accepted_at=stamp,
            scope_revisions={"bgp": 7},
            content_digest="d" * 64,
            mapping_epoch="a" * 64,
            observation_watermark={
                "bgp": {
                    "incarnation": "placeholder-store",
                    "source_epoch": 3,
                    "payload_revision": 4,
                    "digest": "e" * 64,
                }
            },
            expected_fence_revision=5,
            contributors={str(execution.pk): [{"selection_id": selection.pk, "row_key": "c" * 64}]},
        )
        snapshot.executions.add(execution)
        loaded = Snapshot.objects.get(pk=snapshot.pk)
        assert loaded.prior_last_apply_error == error
        assert type(loaded.prior_last_apply_error) is type(error)
        assert loaded.prior_last_apply_at == stamp
        assert loaded.prior_apply_attempt_id == prior.pk
        assert loaded.expected_apply_attempt_id == attempt.pk
        assert loaded.selection_id == selection.pk
        assert list(loaded.executions.values_list("pk", flat=True)) == [execution.pk]
        assert loaded.contributors == snapshot.contributors
    fence = Fence.objects.create(
        management=management,
        mapping_epoch="a" * 64,
        model_label="netbox_nso_plugin.nsobgppeerstate",
        overlay_pk=3,
        bookkeeping_revision=5,
        current_attempt=attempt,
        last_execution=execution,
        observation_watermark=snapshot.observation_watermark,
        accepted_at=stamp,
        content_digest="d" * 64,
        last_settle_seq=11,
    )
    fence.refresh_from_db()
    assert fence.last_execution.store_incarnation == "placeholder-store"
    assert fence.last_settle_seq == 11
    with pytest.raises(RestrictedError):
        prior.delete()
    with pytest.raises(RestrictedError):
        selection.delete()
    with pytest.raises(RestrictedError):
        execution.delete()
    management.delete()
    assert not any(model.objects.exists() for model in (Selection, Execution, Snapshot, Fence))


def test_execution_identity_survives_carrier_pruning(ledger):
    _, _, attempt, (_, Execution, _, _) = ledger
    values = execution_values(attempt)
    execution = Execution.objects.create(
        **values,
        terminal_projection={"row_outcomes": [], "refusals": []},
        commit_acknowledged=False,
        terminal_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    del values, execution
    loaded = Execution.objects.get(attempt=attempt)
    assert (loaded.generation_id, loaded.carrier_job_id, loaded.generation_seq) == (17, 23, 9)
    assert loaded.terminal_projection == {"row_outcomes": [], "refusals": []}
    assert loaded.commit_acknowledged is False
    assert loaded.consumed_at is None


def test_execution_uniqueness_includes_store_incarnation(ledger):
    _, _, attempt, (_, Execution, _, _) = ledger
    one = Execution.objects.create(**execution_values(attempt))
    two = Execution.objects.create(**execution_values(attempt, "placeholder-replacement-store"))
    assert one.pk != two.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        Execution.objects.create(**execution_values(attempt))


def test_snapshot_and_execution_uniqueness(ledger):
    _, management, attempt, (Selection, Execution, Snapshot, Fence) = ledger
    values = dict(
        attempt=attempt,
        model_label="netbox_nso_plugin.nsobgppeerstate",
        overlay_pk=1,
        prior_status="accepted",
        expected_status="deploying",
        mapping_epoch="a" * 64,
        content_digest="d" * 64,
    )
    snapshot = Snapshot.objects.create(**values)
    selection = Selection.objects.create(**selection_values(management))
    first = Execution.objects.create(**execution_values(attempt))
    retry_values = execution_values(attempt)
    retry_values["carrier_job_id"] = 24
    retry = Execution.objects.create(**retry_values)
    snapshot.selection = selection
    snapshot.save(update_fields=["selection"])
    snapshot.executions.add(first, retry)
    assert snapshot.executions.count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        Snapshot.objects.create(**values)
    fence_values = dict(management=management, mapping_epoch="a" * 64, model_label=values["model_label"], overlay_pk=1)
    Fence.objects.create(**fence_values)
    with pytest.raises(IntegrityError), transaction.atomic():
        Fence.objects.create(**fence_values)
    with pytest.raises(IntegrityError), transaction.atomic():
        Selection.objects.create(**selection_values(management))


def test_automatic_attempt_cannot_hold_manual_replay_identity(ledger):
    models, management, attempt, _ = ledger
    with pytest.raises(IntegrityError), transaction.atomic():
        models.NSOApplyAttempt.objects.create(
            management=management, trigger_kind="cadence", adapter_apply_attempt_id=attempt.pk
        )


def test_unbound_execution_has_no_terminal_authority(ledger):
    _, management, attempt, (_, Execution, _, _) = ledger
    for _ in range(2):
        Execution.objects.create(attempt=attempt, management=management, mapping_epoch="a" * 64, adapter_device_id=17)
    assert Execution.objects.filter(attempt=attempt).count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        Execution.objects.create(
            attempt=attempt,
            management=management,
            mapping_epoch="a" * 64,
            adapter_device_id=17,
            terminal_projection={"row_outcomes": [], "refusals": []},
        )


def test_selection_requires_exactly_one_source_selector(ledger):
    _, management, _, (Selection, _, _, _) = ledger
    invalid = selection_values(management)
    invalid["selection_revision"] = 7
    with pytest.raises(IntegrityError), transaction.atomic():
        Selection.objects.create(**invalid)
    invalid["source_push_seq"] = None
    prepared = Selection.objects.create(**invalid)
    assert prepared.selection_revision == 7
