# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Renderer fingerprint audit and repair at the real database seam."""

from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.db.utils import OperationalError
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext, override_settings

from ._outbox_case import make_managed, mirror_update, own_route, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestRendererAuditRepair(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        set_scope = patch("netbox_nso_plugin.adapter_client.set_scope", return_value={})
        set_scope.start()
        self.addCleanup(set_scope.stop)
        self.device, self.management = make_managed("renderer-audit", 16270)

    def test_unknown_baseline_repairs_once_and_demotes_stale_lifecycle(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        deploying = own_vlan(self.management, 1627, "renderer-audit-deploying")
        in_sync = own_vlan(self.management, 1628, "renderer-audit-in-sync")
        attempt_id = uuid4()
        mirror_update(deploying, status="deploying", apply_attempt_id=attempt_id)
        mirror_update(in_sync, status="in_sync", apply_attempt_id=attempt_id)
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        before_revision = revision.revision
        NSOIntentRevision.objects.filter(pk=revision.pk).update(
            verified_revision=None,
            verified_fingerprint=None,
            verified_at=None,
        )
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()

        result = audit_renderer_scopes(
            self.device.pk,
            ["vlan"],
            trigger="test",
            pre_capture=True,
        )

        deploying.refresh_from_db()
        in_sync.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(result.repaired, ("vlan",))
        self.assertEqual(result.unknown, ())
        self.assertEqual(revision.revision, before_revision + 1)
        self.assertEqual(revision.verified_revision, revision.revision)
        self.assertEqual(
            revision.verified_fingerprint,
            delivery.canonical_fingerprint(
                delivery.render("vlan", self.device.pk, self.management.adapter_device_id).payload
            ),
        )
        self.assertEqual((deploying.status, deploying.apply_attempt_id), ("accepted", None))
        self.assertEqual((in_sync.status, in_sync.apply_attempt_id), ("accepted", None))
        contributions = list(
            NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").values(
                "kind",
                "mark_and",
                "mark_any",
                "transitions",
            )
        )
        self.assertEqual(
            contributions,
            [{"kind": "repair", "mark_and": False, "mark_any": False, "transitions": []}],
        )

    def test_signal_less_owned_creation_is_acquired_before_drift_repair(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import (
            NSOIntentOutboxEntry,
            NSOIntentRevision,
            NSOOwnershipManifest,
            NSOVLANState,
        )
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1629, "renderer-audit-baseline")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        before_revision = revision.revision
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        vlan = VLAN(vid=1630, name="renderer-audit-created")
        VLAN.objects.bulk_create([vlan])
        state = NSOVLANState(
            management=self.management,
            vlan=vlan,
            device_name=vlan.name,
            status="accepted",
        )
        NSOVLANState.objects.bulk_create([state])

        result = audit_renderer_scopes(
            self.device.pk,
            ["vlan"],
            trigger="test",
            pre_capture=True,
        )

        revision.refresh_from_db()
        self.assertEqual(result.repaired, ("vlan",))
        self.assertEqual(revision.revision, before_revision + 1)
        self.assertTrue(
            NSOOwnershipManifest.objects.filter(
                device=self.device,
                scope="vlan",
                native_model_label="ipam.vlan",
                native_key={"group_id": None, "vid": 1630},
                ownership_state="owned",
            ).exists()
        )

    def test_static_route_repair_allocates_a_fresh_intent_generation(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision, NSOStaticRouteState
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        route = own_route(
            self.management,
            "198.18.162.0/24",
            "198.18.0.162",
            device=self.device,
        )
        state = NSOStaticRouteState.objects.get(management=self.management, static_route=route)
        before_generation = state.intent_generation
        revision = NSOIntentRevision.objects.get(device=self.device, scope="static_route")
        NSOIntentRevision.objects.filter(pk=revision.pk).update(
            verified_revision=None,
            verified_fingerprint=None,
            verified_at=None,
        )
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="static_route").delete()

        result = audit_renderer_scopes(
            self.device.pk,
            ["static_route"],
            trigger="test",
            pre_capture=True,
        )

        state.refresh_from_db()
        self.assertEqual(result.repaired, ("static_route",))
        self.assertGreater(state.intent_generation, before_generation)
        self.assertEqual(
            list(
                NSOIntentOutboxEntry.objects.filter(device=self.device, scope="static_route").values_list(
                    "kind",
                    flat=True,
                )
            ),
            ["repair"],
        )

    def test_matching_scope_uses_only_the_optimistic_read_committed_pass(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1631, "renderer-audit-matching")
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()

        with CaptureQueriesContext(connection) as captured:
            result = audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="test",
                pre_capture=True,
            )

        statements = [query["sql"].upper() for query in captured.captured_queries]
        self.assertEqual(result.repaired, ())
        self.assertFalse(any("FOR UPDATE" in statement for statement in statements))
        self.assertFalse(any("REPEATABLE READ" in statement for statement in statements))

    def test_repair_locks_the_concrete_native_renderer_inputs(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1634, "renderer-audit-source-lock")
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(verified_revision=None)

        with CaptureQueriesContext(connection) as captured:
            audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="test",
                pre_capture=True,
            )

        statements = [query["sql"].upper() for query in captured.captured_queries]
        self.assertTrue(any('FROM "IPAM_VLAN"' in statement and "FOR UPDATE" in statement for statement in statements))

    def test_a_concurrent_lifecycle_write_does_not_fail_the_pre_capture_audit(self):
        """A reconciler touching ``last_sync_at`` may not close an operator's Apply.

        The plan's compare-and-set is against the FULL pre-image, and
        ``IntentPlanStaleError`` is not a serialization failure, so
        ``_repair_with_retries`` re-raises it out of a mandatory gate.
        """
        from django.utils import timezone

        from netbox_nso_plugin.intent_state import audit_scope_footprint
        from netbox_nso_plugin.models import NSOIntentRevision, NSOVLANState
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        state = own_vlan(self.management, 1636, "renderer-audit-race")
        mirror_update(state, status="in_sync")
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(verified_revision=None)

        def _touch_then_delegate(device_id, scopes):
            # Runs after the plan is frozen and before the repair takes its locks.
            NSOVLANState.objects.filter(pk=state.pk).update(last_sync_at=timezone.now())
            return audit_scope_footprint(device_id, scopes)

        with patch(
            "netbox_nso_plugin.renderer_audit.audit_scope_footprint",
            side_effect=_touch_then_delegate,
        ):
            result = audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="test",
                pre_capture=True,
            )

        state.refresh_from_db()
        self.assertEqual(result.repaired, ("vlan",))
        self.assertEqual(state.status, "accepted")

    def test_lifecycle_only_foreign_change_does_not_repair(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision, NSOVLANState
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        state = own_vlan(self.management, 1635, "renderer-audit-lifecycle")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        before = (revision.revision, revision.verified_revision, revision.verified_fingerprint)
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        NSOVLANState.objects.filter(pk=state.pk).update(last_apply_error="foreign diagnostic")

        result = audit_renderer_scopes(
            self.device.pk,
            ["vlan"],
            trigger="test",
            pre_capture=True,
        )

        revision.refresh_from_db()
        self.assertEqual(result.repaired, ())
        self.assertEqual(
            (revision.revision, revision.verified_revision, revision.verified_fingerprint),
            before,
        )
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").exists())

    @override_settings(
        PLUGINS_CONFIG={
            "netbox_nso_plugin": {
                "renderer_audit_scope_batch_cap": 1,
                "renderer_audit_tick_budget_seconds": 240,
            }
        }
    )
    def test_cadence_defers_scopes_above_the_batch_cap(self):
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        result = audit_renderer_scopes(
            self.device.pk,
            ["vlan", "interface"],
            trigger="cadence",
        )

        self.assertEqual(result.audited, ("vlan",))
        self.assertEqual(result.deferred, ("interface",))

    @override_settings(
        PLUGINS_CONFIG={
            "netbox_nso_plugin": {
                "renderer_audit_scope_batch_cap": 1,
                "renderer_audit_tick_budget_seconds": 240,
            }
        }
    )
    def test_pre_capture_fails_before_a_partial_batch(self):
        from netbox_nso_plugin.renderer_audit import RendererAuditBudgetExceeded, audit_renderer_scopes

        with (
            patch("netbox_nso_plugin.renderer_audit.delivery.render") as render,
            self.assertRaises(RendererAuditBudgetExceeded),
        ):
            audit_renderer_scopes(
                self.device.pk,
                ["vlan", "interface"],
                trigger="pre-capture",
                pre_capture=True,
            )

        render.assert_not_called()

    @override_settings(
        PLUGINS_CONFIG={
            "netbox_nso_plugin": {
                "renderer_audit_scope_batch_cap": 18,
                "renderer_audit_tick_budget_seconds": 0.5,
            }
        }
    )
    def test_cadence_defers_a_candidate_when_the_repair_budget_expires(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1636, "renderer-audit-budget")
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(verified_revision=None)

        with patch("netbox_nso_plugin.renderer_audit._budget_expired", side_effect=(False, True)):
            result = audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="cadence",
            )

        self.assertEqual(result.repaired, ())
        self.assertEqual(result.deferred, ("vlan",))

    def test_serialization_exhaustion_leaves_the_key_unknown(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1632, "renderer-audit-race")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        NSOIntentRevision.objects.filter(pk=revision.pk).update(verified_revision=None)

        class SerializationFailure(Exception):
            sqlstate = "40001"

        failure = OperationalError("serialization failure")
        failure.__cause__ = SerializationFailure()
        with patch("netbox_nso_plugin.renderer_audit._repair_candidates", side_effect=failure) as repair:
            result = audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="cadence",
            )

        revision.refresh_from_db()
        self.assertEqual(repair.call_count, 3)
        self.assertEqual(result.repaired, ())
        self.assertEqual(result.unknown, ("vlan",))
        self.assertIsNone(revision.verified_revision)
        self.assertIsNone(revision.verified_fingerprint)
        self.assertIsNone(revision.verified_at)

    def test_serialization_exhaustion_fails_a_pre_capture_closed(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import RendererAuditRepairFailed, audit_renderer_scopes

        own_vlan(self.management, 1633, "renderer-audit-race-closed")
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(verified_revision=None)

        class SerializationFailure(Exception):
            sqlstate = "40001"

        failure = OperationalError("serialization failure")
        failure.__cause__ = SerializationFailure()
        with (
            patch("netbox_nso_plugin.renderer_audit._repair_candidates", side_effect=failure),
            self.assertRaises(RendererAuditRepairFailed),
        ):
            audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="pre-capture",
                pre_capture=True,
            )

        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        self.assertIsNone(revision.verified_revision)
        self.assertIsNone(revision.verified_fingerprint)
        self.assertIsNone(revision.verified_at)
