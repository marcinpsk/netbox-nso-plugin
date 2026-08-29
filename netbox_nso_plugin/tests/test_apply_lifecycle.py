# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The durable intent revision and Apply-attempt lifecycle."""

from __future__ import annotations

from uuid import uuid4

from django.db import IntegrityError, models, transaction
from django.test import SimpleTestCase, TestCase

from ._outbox_case import make_managed, mirror_update

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
                self.assertIn(
                    "(OR: (NOT (AND: ('status', 'deploying'))), ('apply_attempt_id__isnull', False))",
                    {str(constraint.condition) for constraint in model._meta.constraints},
                )


class TestDeployingAttemptConstraint(TestCase):
    def test_postgresql_refuses_deploying_without_an_attempt_uuid(self):
        from netbox_nso_plugin.models import NSOLoggingLevelState

        _device, management = make_managed("apply-identity-constraint", 1623)

        with self.assertRaises(IntegrityError), transaction.atomic():
            NSOLoggingLevelState.objects.create(
                management=management,
                console_severity="WARNING",
                status="deploying",
            )


class TestIntentRevisionWrites(TestCase):
    def setUp(self):
        self.device, self.management = make_managed("intent-revision", 1624)

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
        before = NSOIntentRevision.objects.get(device=self.device, scope="logging").revision

        with intent_transaction(footprint_for_instance(row)):
            outbox.enqueue(self.device.pk, "logging")

        revision = NSOIntentRevision.objects.get(device=self.device, scope="logging")
        self.assertEqual(revision.revision, before + 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNone(row.apply_attempt_id)

    def test_savepoint_rollback_does_not_suppress_the_next_revision_bump(self):
        from netbox_nso_plugin import outbox
        from netbox_nso_plugin.intent_state import MutationFootprint, intent_transaction
        from netbox_nso_plugin.models import NSOIntentRevision

        footprint = MutationFootprint.for_keys({(self.device.pk, "logging")})
        before = NSOIntentRevision.objects.get(device=self.device, scope="logging").revision
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
