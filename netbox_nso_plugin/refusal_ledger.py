# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Durable selection, execution, snapshot and concurrency metadata."""

from django.db import models
from django.db.models.functions import Now

from .refusal_registry import OVERLAYS

_LABELS = tuple(OVERLAYS)
_LABEL_CHOICES = tuple((label, label) for label in _LABELS)


class NSOAcceptedSelection(models.Model):
    management = models.ForeignKey("NSODeviceManagement", on_delete=models.CASCADE, related_name="accepted_selections")
    mapping_epoch = models.CharField(max_length=64)
    store_incarnation = models.CharField(max_length=64, null=True, blank=True)
    stream = models.CharField(max_length=32)
    source_push_seq = models.PositiveBigIntegerField(null=True, blank=True)
    selection_revision = models.PositiveBigIntegerField(null=True, blank=True)
    plugin_revision = models.PositiveBigIntegerField()
    request_digest = models.CharField(max_length=64)
    contributor_manifest = models.JSONField(default=list)
    publication_state = models.CharField(
        max_length=16,
        default="recorded",
        choices=(("recorded", "Recorded"), ("sent", "Sent"), ("unresolved", "Unresolved")),
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    binding_state = models.CharField(
        max_length=16,
        default="unbound",
        choices=(("unbound", "Unbound"), ("bound", "Bound"), ("invalidated", "Invalidated")),
    )
    frozen_revision = models.PositiveBigIntegerField(null=True, blank=True)
    acceptance_identity = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(source_push_seq__isnull=False, source_push_seq__gt=0, selection_revision__isnull=True)
                    | models.Q(source_push_seq__isnull=True, selection_revision__isnull=False, selection_revision__gt=0)
                    | models.Q(
                        source_push_seq__isnull=True,
                        selection_revision__isnull=True,
                        stream__in=("lag", "switchport"),
                        binding_state="unbound",
                        acknowledged_at__isnull=True,
                    )
                ),
                name="nso_selection_selector",
            ),
            # Not nulls_distinct=False: PostgreSQL 14 (NetBox 4.6) ignores it.
            models.UniqueConstraint(
                fields=["management", "mapping_epoch", "stream", "source_push_seq"],
                condition=models.Q(source_push_seq__isnull=False, store_incarnation__isnull=True),
                name="nso_selection_push_unbound",
            ),
            models.UniqueConstraint(
                fields=["management", "mapping_epoch", "store_incarnation", "stream", "source_push_seq"],
                condition=models.Q(source_push_seq__isnull=False, store_incarnation__isnull=False),
                name="nso_selection_push_bound",
            ),
            models.UniqueConstraint(
                fields=["management", "mapping_epoch", "stream", "selection_revision"],
                condition=models.Q(selection_revision__isnull=False, store_incarnation__isnull=True),
                name="nso_selection_preparation_unbound",
            ),
            models.UniqueConstraint(
                fields=["management", "mapping_epoch", "store_incarnation", "stream", "selection_revision"],
                condition=models.Q(selection_revision__isnull=False, store_incarnation__isnull=False),
                name="nso_selection_preparation_bound",
            ),
            models.CheckConstraint(
                condition=~models.Q(binding_state="bound")
                | (
                    models.Q(store_incarnation__isnull=False, frozen_revision__isnull=False)
                    & ~models.Q(store_incarnation="")
                ),
                name="nso_selection_bound_identity",
            ),
        ]


class NSOApplyExecution(models.Model):
    attempt = models.ForeignKey("NSOApplyAttempt", on_delete=models.CASCADE, related_name="executions")
    management = models.ForeignKey("NSODeviceManagement", on_delete=models.CASCADE, related_name="apply_executions")
    mapping_epoch = models.CharField(max_length=64)
    adapter_device_id = models.PositiveBigIntegerField()
    store_incarnation = models.CharField(max_length=64, null=True, blank=True)
    generation_id = models.PositiveBigIntegerField(null=True, blank=True)
    generation_seq = models.PositiveBigIntegerField(null=True, blank=True)
    carrier_job_id = models.PositiveBigIntegerField(null=True, blank=True)
    settlement_cohort = models.PositiveBigIntegerField(null=True, blank=True)
    mode = models.CharField(max_length=32, blank=True, default="")
    source_selectors = models.JSONField(default=dict)
    settle_seq = models.PositiveBigIntegerField(null=True, blank=True)
    terminal_projection = models.JSONField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)
    commit_acknowledged = models.BooleanField(null=True, blank=True)
    consumption_state = models.CharField(
        max_length=16,
        default="unconsumed",
        choices=(
            ("unconsumed", "Unconsumed"),
            ("consumed", "Consumed"),
            ("superseded", "Superseded"),
            ("unresolved", "Unresolved"),
        ),
    )
    invariant_code = models.CharField(max_length=64, blank=True, default="")
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["management", "mapping_epoch", "store_incarnation", "generation_id", "carrier_job_id"],
                condition=models.Q(
                    store_incarnation__isnull=False, generation_id__isnull=False, carrier_job_id__isnull=False
                ),
                name="nso_execution_remote_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(terminal_projection__isnull=True)
                | (
                    models.Q(
                        store_incarnation__isnull=False,
                        generation_id__isnull=False,
                        generation_seq__isnull=False,
                        carrier_job_id__isnull=False,
                        terminal_at__isnull=False,
                    )
                    & ~models.Q(store_incarnation="")
                ),
                name="nso_execution_terminal_bound",
            ),
            models.CheckConstraint(
                condition=models.Q(consumption_state__in=("unconsumed", "unresolved"))
                | models.Q(terminal_projection__isnull=False, consumed_at__isnull=False),
                name="nso_execution_consumed_terminal",
            ),
        ]


class NSOApplyOverlaySnapshot(models.Model):
    attempt = models.ForeignKey("NSOApplyAttempt", on_delete=models.CASCADE, related_name="overlay_snapshots")
    selection = models.ForeignKey(
        NSOAcceptedSelection, on_delete=models.RESTRICT, null=True, blank=True, related_name="overlay_snapshots"
    )
    executions = models.ManyToManyField(NSOApplyExecution, related_name="overlay_snapshots", blank=True)
    model_label = models.CharField(max_length=100, choices=_LABEL_CHOICES)
    overlay_pk = models.PositiveBigIntegerField()
    mapping_epoch = models.CharField(max_length=64)
    prior_status = models.CharField(max_length=32)
    prior_last_apply_at = models.DateTimeField(null=True, blank=True)
    prior_last_apply_error = models.JSONField(null=True, blank=True)
    prior_apply_attempt = models.ForeignKey(
        "NSOApplyAttempt", on_delete=models.RESTRICT, null=True, blank=True, related_name="referenced_prior_snapshots"
    )
    expected_status = models.CharField(max_length=32)
    expected_last_apply_at = models.DateTimeField(null=True, blank=True)
    expected_last_apply_error = models.JSONField(null=True, blank=True)
    expected_apply_attempt = models.ForeignKey(
        "NSOApplyAttempt",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="referenced_expected_snapshots",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    scope_revisions = models.JSONField(default=dict)
    content_digest = models.CharField(max_length=64)
    observation_watermark = models.JSONField(default=dict)
    expected_fence_revision = models.PositiveBigIntegerField(default=0)
    contributors = models.JSONField(default=dict)
    static_route_fences = models.JSONField(null=True, blank=True)
    settlement_disposition = models.CharField(
        max_length=16,
        default="unresolved",
        choices=(
            ("unresolved", "Unresolved"),
            ("refused", "Refused"),
            ("not_applied", "Not applied"),
            ("in_sync", "In sync"),
            ("unavailable", "Unavailable"),
            ("superseded", "Superseded"),
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["attempt", "model_label", "overlay_pk"], name="nso_snapshot_owner"),
            models.CheckConstraint(condition=models.Q(model_label__in=_LABELS), name="nso_snapshot_closed_model"),
        ]


class NSOOverlaySettlementFence(models.Model):
    management = models.ForeignKey("NSODeviceManagement", on_delete=models.CASCADE, related_name="settlement_fences")
    mapping_epoch = models.CharField(max_length=64)
    model_label = models.CharField(max_length=100, choices=_LABEL_CHOICES)
    overlay_pk = models.PositiveBigIntegerField()
    bookkeeping_revision = models.PositiveBigIntegerField(default=0)
    observation_watermark = models.JSONField(default=dict)
    current_attempt = models.ForeignKey(
        "NSOApplyAttempt", on_delete=models.RESTRICT, null=True, blank=True, related_name="active_settlement_fences"
    )
    last_execution = models.ForeignKey(
        NSOApplyExecution, on_delete=models.RESTRICT, null=True, blank=True, related_name="settlement_fences"
    )
    last_settle_seq = models.PositiveBigIntegerField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    content_digest = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["management", "mapping_epoch", "model_label", "overlay_pk"], name="nso_fence_owner"
            ),
            models.CheckConstraint(condition=models.Q(model_label__in=_LABELS), name="nso_fence_closed_model"),
        ]
