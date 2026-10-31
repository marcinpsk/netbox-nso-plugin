# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep issue revisions immutable and acknowledged executions outside every certificate."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone as django_timezone

from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.models import (
    NSOApplyAttempt,
    NSOApplyExecution,
    NSOSettlementIssue,
    NSOSettlementIssueOwner,
)
from netbox_nso_plugin.settlement_resolution import canonical_digest, canonical_json, instant

from ._outbox_case import make_managed
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

INCARNATION = "00000000-0000-4000-8000-000000000004"


class TestCanonicalDecision(SimpleTestCase):
    def test_json_types_and_missing_fields_stay_distinct(self):
        values = [{"a": 1}, {"a": "1"}, {"a": True}, {"a": None}, {}, {"a": 1.0}, {"a": [1, 2]}, {"a": [2, 1]}]
        assert len({canonical_digest(value) for value in values}) == len(values)

    def test_mapping_order_is_irrelevant_and_list_order_is_kept(self):
        assert canonical_json({"b": 1, "a": [3, 1]}) == canonical_json({"a": [3, 1], "b": 1})
        assert canonical_json([1, 3]) != canonical_json([3, 1])

    def test_aware_instants_are_normalized_and_naive_values_fail_closed(self):
        local = datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))
        assert instant(local) == instant(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
        with pytest.raises(TypeError):
            instant(datetime(2026, 1, 2, 3, 4, 5))  # noqa: DTZ001
        with pytest.raises(TypeError):
            canonical_json({"at": local})
        with pytest.raises(TypeError):
            canonical_json({1: "integer key"})
        with pytest.raises(TypeError):
            canonical_json({"value": float("nan")})


class TestSettlementIssueConstraints(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("issue-model", 1561)
        self.attempt = NSOApplyAttempt.objects.create(
            management=self.management, adapter_device_id=1561, trigger_kind="cadence"
        )

    def issue(self, digest="a", **values):
        fields = {
            "management": self.management,
            "mapping_epoch": mapping_epoch(self.management),
            "adapter_device_id": 1561,
            "store_incarnation": INCARNATION,
            "job_id": 501,
            "settle_seq": 1,
            "request_fence": {"cursor_seq": None},
            "stall_class": "invalid_evidence",
            "stall_reason": "invalid job envelope",
            "position": "next_feed",
            "cursor_before": 0,
            "cursor_after": 1,
            "evidence_digest": "b" * 64,
            "impact_digest": "c" * 64,
            "contract_fingerprint": "d" * 64,
            "revision_digest": digest * 64,
            "proposal": {"obligations": []},
        }
        fields.update(values)
        return NSOSettlementIssue.objects.create(**fields)

    def execution(self, **values):
        fields = {
            "attempt": self.attempt,
            "management": self.management,
            "mapping_epoch": mapping_epoch(self.management),
            "adapter_device_id": 1561,
            "store_incarnation": INCARNATION,
            "generation_id": 81,
            "carrier_job_id": 501,
            "settle_seq": 1,
        }
        fields.update(values)
        return NSOApplyExecution.objects.create(**fields)

    def acknowledged(self, issue):
        operator = get_user_model().objects.create_user(username=f"placeholder-operator-{issue.pk}")
        now = django_timezone.now()
        NSOSettlementIssue.objects.filter(pk=issue.pk).update(
            state="acknowledged",
            closed_at=now,
            actor_id=operator.pk,
            actor_label=operator.username,
            acknowledgement_reason="placeholder reason",
            acknowledged_at=now,
            committed_effects=[],
        )
        issue.refresh_from_db()
        return issue

    def assert_rejected(self, create, error=IntegrityError):
        with pytest.raises(error), transaction.atomic():
            create()

    def test_revisions_of_one_job_coexist_and_one_digest_is_one_revision(self):
        first = self.issue("a", state="superseded", closed_at=django_timezone.now())
        second = self.issue("e")
        assert (first.job_id, second.job_id) == (501, 501)
        self.assert_rejected(lambda: self.issue("e", state="superseded", closed_at=django_timezone.now()))

    def test_one_management_has_at_most_one_open_revision(self):
        self.issue("a")
        self.assert_rejected(lambda: self.issue("e"))

    def test_the_cursor_rule_and_identity_are_closed(self):
        for values in (
            {"cursor_after": 2},
            {"position": "historical", "cursor_before": 0, "cursor_after": 0},
            {"position": "historical", "cursor_before": 5, "cursor_after": 6},
            {"revision_digest": "not-a-digest"},
            {"store_incarnation": ""},
            {"stall_class": "unknown"},
            {"state": "open", "closed_at": django_timezone.now()},
            {"state": "resolved"},
        ):
            with self.subTest(values=values):
                self.assert_rejected(lambda values=values: self.issue("a", **values))

    def test_an_acknowledged_issue_requires_its_complete_audit(self):
        issue = self.issue("a")
        for missing in ("actor_id", "actor_label", "acknowledgement_reason", "acknowledged_at", "committed_effects"):
            values = {
                "state": "acknowledged",
                "closed_at": django_timezone.now(),
                "actor_id": 1,
                "actor_label": "placeholder-operator",
                "acknowledgement_reason": "placeholder reason",
                "acknowledged_at": django_timezone.now(),
                "committed_effects": [],
            }
            values[missing] = "" if missing in {"actor_label", "acknowledgement_reason"} else None
            with self.subTest(missing=missing):
                self.assert_rejected(
                    lambda values=values: NSOSettlementIssue.objects.filter(pk=issue.pk).update(**values)
                )

    def test_revision_content_is_immutable_and_an_acknowledgement_is_frozen(self):
        issue = self.issue("a")
        for values in ({"job_id": 502}, {"proposal": {"obligations": [1]}}, {"request_fence": {}}):
            with self.subTest(values=values):
                self.assert_rejected(
                    lambda values=values: NSOSettlementIssue.objects.filter(pk=issue.pk).update(**values),
                    DatabaseError,
                )
        NSOSettlementIssue.objects.filter(pk=issue.pk).update(state="superseded", closed_at=django_timezone.now())
        NSOSettlementIssue.objects.filter(pk=issue.pk).update(state="open", closed_at=None)
        issue = self.acknowledged(issue)
        for values in ({"acknowledgement_reason": "rewritten"}, {"state": "superseded"}, {"actor_id": None}):
            with self.subTest(values=values):
                self.assert_rejected(
                    lambda values=values: NSOSettlementIssue.objects.filter(pk=issue.pk).update(**values),
                    DatabaseError,
                )
        actor_id = issue.actor_id
        get_user_model().objects.filter(pk=actor_id).delete()
        issue.refresh_from_db()
        assert issue.actor_id == actor_id
        assert issue.actor_label.startswith("placeholder-operator")

    def test_owner_effects_are_frozen_and_coverage_only_grows(self):
        issue = self.issue("a")
        owner = NSOSettlementIssueOwner.objects.create(
            issue=issue,
            model_label="netbox_nso_plugin.nsobgprouterstate",
            overlay_pk=1,
            contributors=[["bgp", "bgp.router", "a" * 64]],
            unknown=[["bgp", "bgp.router", "a" * 64]],
            authority={"owner_revision": 0},
            order_bound=1,
            proposed_effect="unavailable",
        )
        self.assert_rejected(
            lambda: NSOSettlementIssueOwner.objects.filter(pk=owner.pk).update(committed_effect="superseded")
        )
        NSOSettlementIssueOwner.objects.filter(pk=owner.pk).update(committed_effect="unavailable")
        for values in ({"committed_effect": None}, {"unknown": []}, {"order_bound": 2}):
            with self.subTest(values=values):
                self.assert_rejected(
                    lambda values=values: NSOSettlementIssueOwner.objects.filter(pk=owner.pk).update(**values),
                    DatabaseError,
                )
        NSOSettlementIssueOwner.objects.filter(pk=owner.pk).update(covered=[["bgp", "bgp.router", "a" * 64]])
        self.assert_rejected(
            lambda: NSOSettlementIssueOwner.objects.filter(pk=owner.pk).update(covered=[]), DatabaseError
        )

    def test_an_acknowledged_execution_is_bound_to_its_issue_and_is_never_a_certificate(self):
        issue = self.issue("a")
        now = django_timezone.now()
        for values in (
            {"consumption_state": "acknowledged"},
            {"consumption_state": "unconsumed", "settlement_issue": issue},
            {"consumption_state": "acknowledged", "settlement_issue": issue, "consumed_at": now},
            {"consumption_state": "acknowledged", "settlement_issue": issue, "commit_acknowledged": False},
            {"consumption_state": "acknowledged", "settlement_issue": issue, "settle_seq": None},
            {"consumption_state": "acknowledged", "settlement_issue": issue, "store_incarnation": None},
        ):
            with self.subTest(values=values):
                self.assert_rejected(lambda values=values: self.execution(**values))
        execution = self.execution(consumption_state="acknowledged", settlement_issue=issue)
        assert execution.terminal_projection is None and execution.commit_acknowledged is None

    def test_certificate_helpers_never_return_an_acknowledged_execution(self):
        from netbox_nso_plugin.apply_settlement import (
            _cursor_epoch_executions,
            _historical_certificate,
            certified_projection,
            generation_certificate_candidates,
            generation_certificates,
        )
        from netbox_nso_plugin.settlement_resolution import (
            ACKNOWLEDGED,
            UNRESOLVED,
            acknowledged_obligations,
            obligation_resolution,
        )

        issue = self.acknowledged(self.issue("a"))
        self.execution(consumption_state="acknowledged", settlement_issue=issue)
        listed = {"generation_id": 81, "carrier_job_id": 501, "status": "failed"}
        assert not _cursor_epoch_executions(self.management, INCARNATION).exists()
        assert _historical_certificate(self.management, INCARNATION, 81, 501) is None
        assert not generation_certificates(self.management, INCARNATION, listed).exists()
        assert generation_certificate_candidates(self.management, INCARNATION, [listed]) == {}
        assert certified_projection(listed, []) is None
        assert obligation_resolution(self.management, INCARNATION, 81, 501) == ACKNOWLEDGED
        assert obligation_resolution(self.management, INCARNATION, 82, 501) == UNRESOLVED
        assert acknowledged_obligations(self.management, INCARNATION) == {(81, 501)}
