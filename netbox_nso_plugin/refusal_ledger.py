# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Durable selection, execution, snapshot and concurrency metadata."""

from django.db import models
from django.db.models.functions import Now

from .refusal_registry import OVERLAYS

_LABELS = tuple(OVERLAYS)
_LABEL_CHOICES = tuple((label, label) for label in _LABELS)
_DIGEST = r"^[0-9a-f]{64}$"
ISSUE_STATES = ("open", "resolved", "superseded", "acknowledged")
STALL_CLASSES = ("invalid_evidence", "evidence_invariant")
ISSUE_POSITIONS = ("next_feed", "historical")
OWNER_EFFECTS = ("unavailable", "superseded", "missing_owner")
# The custom action on NSODeviceManagement that permits acknowledging a settlement issue.
ACKNOWLEDGE_SETTLEMENT_ISSUE = "acknowledge_settlement_issue"


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
            ("acknowledged", "Acknowledged"),
        ),
    )
    invariant_code = models.CharField(max_length=64, blank=True, default="")
    consumed_at = models.DateTimeField(null=True, blank=True)
    settlement_issue = models.ForeignKey(
        "NSOSettlementIssue",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="acknowledged_executions",
    )
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
                condition=models.Q(consumption_state__in=("unconsumed", "unresolved", "acknowledged"))
                | models.Q(terminal_projection__isnull=False, consumed_at__isnull=False),
                name="nso_execution_consumed_terminal",
            ),
            # An acknowledgement resolves one exact obligation and is never a certificate.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        consumption_state="acknowledged",
                        settlement_issue__isnull=False,
                        consumed_at__isnull=True,
                        store_incarnation__isnull=False,
                        generation_id__isnull=False,
                        carrier_job_id__isnull=False,
                        settle_seq__isnull=False,
                        settle_seq__gt=0,
                    )
                    & (models.Q(terminal_projection__isnull=False) | models.Q(commit_acknowledged__isnull=True))
                )
                | (~models.Q(consumption_state="acknowledged") & models.Q(settlement_issue__isnull=True)),
                name="nso_execution_acknowledged",
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
    expected_owner_revision = models.PositiveBigIntegerField(null=True, blank=True)
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
        # The absent-owner proof reads an owner's snapshots across attempts.
        indexes = [
            models.Index(
                fields=["model_label", "overlay_pk", "mapping_epoch", "expected_fence_revision"],
                name="nso_snapshot_owner_epoch",
            )
        ]


class NSOOverlaySettlementFence(models.Model):
    management = models.ForeignKey("NSODeviceManagement", on_delete=models.CASCADE, related_name="settlement_fences")
    mapping_epoch = models.CharField(max_length=64)
    model_label = models.CharField(max_length=100, choices=_LABEL_CHOICES)
    overlay_pk = models.PositiveBigIntegerField()
    bookkeeping_revision = models.PositiveBigIntegerField(default=0)
    owner_revision = models.PositiveBigIntegerField(default=0)
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


class NSOSettlementIssue(models.Model):
    """One immutable revision of an identified settlement stall and its proposed resolution."""

    management = models.ForeignKey("NSODeviceManagement", on_delete=models.CASCADE, related_name="settlement_issues")
    mapping_epoch = models.CharField(max_length=64)
    adapter_device_id = models.PositiveBigIntegerField()
    store_incarnation = models.CharField(max_length=64)
    job_id = models.PositiveBigIntegerField()
    settle_seq = models.PositiveBigIntegerField()
    request_fence = models.JSONField()
    stall_class = models.CharField(max_length=32, choices=tuple((value, value) for value in STALL_CLASSES))
    stall_reason = models.CharField(max_length=128)
    position = models.CharField(max_length=16, choices=tuple((value, value) for value in ISSUE_POSITIONS))
    cursor_before = models.PositiveBigIntegerField()
    cursor_after = models.PositiveBigIntegerField()
    evidence_digest = models.CharField(max_length=64)
    impact_digest = models.CharField(max_length=64)
    contract_fingerprint = models.CharField(max_length=64)
    revision_digest = models.CharField(max_length=64)
    proposal = models.JSONField()
    state = models.CharField(
        max_length=16, default="open", choices=tuple((value, value.capitalize()) for value in ISSUE_STATES)
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    # A plain user reference and label keep the audit identity after the user is deleted.
    actor_id = models.PositiveBigIntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=150, blank=True, default="")
    acknowledgement_reason = models.TextField(blank=True, default="")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    committed_effects = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "management",
                    "mapping_epoch",
                    "adapter_device_id",
                    "store_incarnation",
                    "job_id",
                    "revision_digest",
                ],
                name="nso_issue_revision",
            ),
            # A walk stops at one position, so one revision at most is open.
            models.UniqueConstraint(fields=["management"], condition=models.Q(state="open"), name="nso_issue_open"),
            models.CheckConstraint(condition=models.Q(state__in=ISSUE_STATES), name="nso_issue_state"),
            models.CheckConstraint(condition=models.Q(stall_class__in=STALL_CLASSES), name="nso_issue_stall_class"),
            models.CheckConstraint(
                condition=models.Q(
                    position="next_feed",
                    cursor_after=models.F("settle_seq"),
                    settle_seq__gt=models.F("cursor_before"),
                )
                | models.Q(
                    position="historical",
                    cursor_after=models.F("cursor_before"),
                    settle_seq__lte=models.F("cursor_before"),
                ),
                name="nso_issue_cursor_rule",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    job_id__gt=0,
                    settle_seq__gt=0,
                    evidence_digest__regex=_DIGEST,
                    impact_digest__regex=_DIGEST,
                    contract_fingerprint__regex=_DIGEST,
                    revision_digest__regex=_DIGEST,
                )
                & ~models.Q(store_incarnation="")
                & ~models.Q(stall_reason=""),
                name="nso_issue_identity",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="open", closed_at__isnull=True)
                    | (~models.Q(state="open") & models.Q(closed_at__isnull=False))
                )
                & (
                    (
                        models.Q(
                            state="acknowledged",
                            acknowledged_at__isnull=False,
                            committed_effects__isnull=False,
                        )
                        & models.Q(actor_id__isnull=False)
                        & ~models.Q(actor_label="")
                        & ~models.Q(acknowledgement_reason="")
                    )
                    | models.Q(
                        ~models.Q(state="acknowledged"),
                        actor_id__isnull=True,
                        actor_label="",
                        acknowledgement_reason="",
                        acknowledged_at__isnull=True,
                        committed_effects__isnull=True,
                    )
                ),
                name="nso_issue_audit",
            ),
        ]


class NSOSettlementIssueOwner(models.Model):
    """One owner's frozen contribution subset and its proposed and committed effect."""

    issue = models.ForeignKey(NSOSettlementIssue, on_delete=models.CASCADE, related_name="owners")
    model_label = models.CharField(max_length=100, choices=_LABEL_CHOICES)
    overlay_pk = models.PositiveBigIntegerField()
    # Plain identities keep the audit record when retention prunes the attempt.
    snapshot_ids = models.JSONField(default=list)
    attempt_id = models.UUIDField(null=True, blank=True)
    selection_ids = models.JSONField(default=list)
    contributors = models.JSONField(default=list)
    unknown = models.JSONField(default=list)
    covered = models.JSONField(default=list)
    authority = models.JSONField()
    order_bound = models.PositiveBigIntegerField()
    proposed_effect = models.CharField(max_length=16, choices=tuple((value, value) for value in OWNER_EFFECTS))
    committed_effect = models.CharField(
        max_length=16, null=True, blank=True, choices=tuple((value, value) for value in OWNER_EFFECTS)
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["issue", "model_label", "overlay_pk"], name="nso_issue_owner"),
            models.CheckConstraint(condition=models.Q(model_label__in=_LABELS), name="nso_issue_owner_model"),
            models.CheckConstraint(
                condition=models.Q(proposed_effect__in=OWNER_EFFECTS)
                & (models.Q(committed_effect__isnull=True) | models.Q(committed_effect=models.F("proposed_effect"))),
                name="nso_issue_owner_effect",
            ),
        ]
