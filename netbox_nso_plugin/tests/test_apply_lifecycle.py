# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The durable intent revision and Apply-attempt lifecycle."""

from __future__ import annotations

from uuid import uuid4

from django.db import IntegrityError, models, transaction
from django.test import SimpleTestCase, TestCase

from ._outbox_case import make_managed, mirror_update, trust_scope, without_commit_drain

PROMOTED_MODEL_NAMES = (
    "NSOVLANState",
    "NSOSVIState",
    "NSOSubinterfaceState",
    "NSOBFDInterfaceState",
    "NSOInterfaceMtuState",
    "NSORoutePolicyState",
    "NSOStaticRouteState",
    "NSOL2SapState",
    "NSOLoggingLevelState",
)


class TestApplyAttemptSchema(SimpleTestCase):
    def test_identity_models_keep_scope_revisions_and_uuid_attempts(self):
        from netbox_nso_plugin import models as plugin_models

        revision = plugin_models.NSOIntentRevision
        self.assertIn(
            ("device", "scope"),
            {tuple(constraint.fields) for constraint in revision._meta.constraints},
        )
        self.assertIsInstance(revision._meta.get_field("revision"), models.BigIntegerField)

        attempt = plugin_models.NSOApplyAttempt
        self.assertIsInstance(attempt._meta.pk, models.UUIDField)
        self.assertEqual(
            {
                "management",
                "adapter_device_id",
                "scope_revisions",
                "selected",
                "http_status",
                "response",
            },
            {field.name for field in attempt._meta.fields if field.name not in {"id", "created_at", "last_updated"}},
        )

        outbox_state = plugin_models.NSOIntentOutboxState
        claim_revision = outbox_state._meta.get_field("claim_revision")
        self.assertIsInstance(claim_revision, models.BigIntegerField)
        self.assertTrue(claim_revision.null)

    def test_every_promoted_overlay_requires_a_uuid_for_deploying_rows(self):
        from netbox_nso_plugin import models as plugin_models

        for model_name in PROMOTED_MODEL_NAMES:
            with self.subTest(model=model_name):
                model = getattr(plugin_models, model_name)
                field = model._meta.get_field("apply_attempt_id")
                self.assertIsInstance(field, models.UUIDField)
                self.assertTrue(field.null)
                self.assertIn(
                    ("management", "status", "apply_attempt_id"),
                    {tuple(index.fields) for index in model._meta.indexes},
                )
                expected = ~models.Q(status="deploying") | models.Q(apply_attempt_id__isnull=False)
                self.assertTrue(
                    any(constraint.condition == expected for constraint in model._meta.constraints),
                    f"{model_name} must require an attempt UUID while deploying",
                )


class TestDeployingAttemptConstraint(TestCase):
    def test_postgresql_refusal_leaves_no_renderer_transaction_active(self):
        from netbox_nso_plugin.intent_state import _ACTIVE_PERMIT
        from netbox_nso_plugin.models import NSOLoggingLevelState

        _device, management = make_managed("apply-identity-constraint", 1623)

        with self.assertRaises(IntegrityError), transaction.atomic():
            NSOLoggingLevelState.objects.create(
                management=management,
                console_severity="WARNING",
                status="deploying",
            )

        self.assertIsNone(_ACTIVE_PERMIT.get())


class TestIntentRevisionWrites(TestCase):
    def setUp(self):
        self.device, self.management = make_managed("intent-revision", 1624)

    def test_revision_upsert_uses_the_models_table_name(self):
        from unittest.mock import patch

        from django.db import connection

        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.outbox import bump_intent_revision

        real_table = NSOIntentRevision._meta.db_table
        renamed_table = "test_nso_intent_revision"
        before = (
            NSOIntentRevision.objects.filter(device=self.device, scope="vlan")
            .values_list("revision", flat=True)
            .first()
            or 0
        )

        def restore_real_table(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("INSERT"):
                self.assertIn(connection.ops.quote_name(renamed_table), sql)
                sql = sql.replace(connection.ops.quote_name(renamed_table), connection.ops.quote_name(real_table))
            return execute(sql, params, many, context)

        with (
            patch.object(NSOIntentRevision._meta, "db_table", renamed_table),
            connection.execute_wrapper(restore_real_table),
            transaction.atomic(),
        ):
            self.assertEqual(bump_intent_revision(self.device.pk, "vlan"), before + 1)

    def test_enqueue_bumps_the_scope_revision_and_repends_deploying_rows(self):
        from netbox_nso_plugin import outbox
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOIntentRevision, NSOLoggingLevelState

        row = NSOLoggingLevelState.objects.create(
            management=self.management,
            console_severity="WARNING",
            status="accepted",
        )
        mirror_update(
            row,
            status="deploying",
            apply_attempt_id=uuid4(),
        )
        revision, _created = NSOIntentRevision.objects.get_or_create(device=self.device, scope="logging")
        before = revision.revision

        with intent_transaction(footprint_for_instance(row)):
            outbox.enqueue(self.device.pk, "logging")

        revision = NSOIntentRevision.objects.get(device=self.device, scope="logging")
        self.assertEqual(revision.revision, before + 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNone(row.apply_attempt_id)

    def test_a_foreign_logging_host_edit_repends_on_the_next_audit(self):
        from netbox_nso_plugin.models import NSOLoggingHostState, NSOLoggingLevelState
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        with without_commit_drain(), transaction.atomic():
            host = NSOLoggingHostState.objects.create(
                management=self.management,
                address="198.18.0.10",
                status="accepted",
            )
            level = NSOLoggingLevelState.objects.create(
                management=self.management,
                console_severity="WARNING",
                status="accepted",
            )
        mirror_update(level, status="deploying", apply_attempt_id=uuid4())
        trust_scope(self.device, self.management, "logging")

        with without_commit_drain(), transaction.atomic():
            host.port = 5514
            host.save(update_fields=["port"])

        level.refresh_from_db()
        self.assertEqual(level.status, "deploying")

        audit_renderer_scopes(self.device.pk, ("logging",), trigger="test", pre_capture=True)

        level.refresh_from_db()
        self.assertEqual(level.status, "accepted")
        self.assertIsNone(level.apply_attempt_id)

    def test_savepoint_rollback_does_not_suppress_the_next_revision_bump(self):
        from netbox_nso_plugin import outbox
        from netbox_nso_plugin.intent_state import MutationFootprint, intent_transaction
        from netbox_nso_plugin.models import NSOIntentRevision

        footprint = MutationFootprint.for_keys({(self.device.pk, "logging")})
        revision, _created = NSOIntentRevision.objects.get_or_create(device=self.device, scope="logging")
        before = revision.revision
        with transaction.atomic():
            try:
                with transaction.atomic(), intent_transaction(footprint):
                    outbox.enqueue(self.device.pk, "logging")
                    raise RuntimeError("roll back the savepoint")
            except RuntimeError:
                pass
            with intent_transaction(footprint):
                outbox.enqueue(self.device.pk, "logging")

        revision = NSOIntentRevision.objects.get(device=self.device, scope="logging")
        self.assertEqual(revision.revision, before + 1)
