# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Renderer fingerprint audit and repair at the real database seam."""

from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.db.utils import OperationalError
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext, override_settings

from ._adapter_http import patch_matching_control_state
from ._outbox_case import (
    ReceiptAdapter,
    in_thread,
    make_managed,
    mirror_update,
    own_route,
    own_vlan,
    reset_renderer_audit_rotation,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def own_redistribution(management, dest_protocol, source_protocol):
    """One owned redistribution overlay, whose delivery scope is its ``dest_protocol``."""
    from netbox_nso_plugin.models import NSORedistributionState
    from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

    state = NSORedistributionState(
        management=management,
        dest_protocol=dest_protocol,
        dest_ref="65100::ipv4-unicast",
        source_protocol=source_protocol,
        source_ref="",
        status="accepted",
    )
    plan = RendererMutationPlan.build(
        saves=(
            planned_save(
                state,
                force_insert=True,
                natural_key=("management", "dest_protocol", "dest_ref", "source_protocol", "source_ref"),
            ),
        )
    )
    with renderer_writes(plan) as writer:
        writer.save(state, force_insert=True)
    return state


class TestRendererAuditRepair(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        reset_renderer_audit_rotation(self)
        patch_matching_control_state(self)
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

    def test_an_unlinked_management_row_is_not_audited(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        NSODeviceManagement.objects.filter(pk=self.management.pk).update(adapter_device_id=None)
        with (
            patch("netbox_nso_plugin.renderer_audit.delivery.render") as render,
            patch("netbox_nso_plugin.ownership_planner.reconcile_scope_ownership") as reconcile_ownership,
        ):
            result = audit_renderer_scopes(self.device.pk, ("vlan",), trigger="test", pre_capture=True)

        self.assertEqual(result.repaired, ())
        render.assert_not_called()
        reconcile_ownership.assert_not_called()

    def test_delivery_wraps_a_route_policy_family_target_mismatch(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.renderer_audit import RendererAuditRepairFailed

        route_map = RouteMap.objects.create(name="renderer-audit-wrong-family")
        state = NSORoutePolicyState(
            management=self.management,
            content_type=ContentType.objects.get_for_model(RouteMap),
            object_id=route_map.pk,
            family="prefix_list",
            object_name=route_map.name,
            status="accepted",
        )
        NSORoutePolicyState.objects.bulk_create([state])

        with self.assertRaises(AdapterError) as error:
            deliver("route_policy", self.device.pk, self.management.adapter_device_id)

        self.assertEqual(error.exception.code, "renderer_audit_failed")
        self.assertIsInstance(error.exception.__cause__, RendererAuditRepairFailed)

    def test_bgp_repair_demotes_the_lifecycle_only_peer_template(self):
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState
        from netbox_nso_plugin.renderer_audit import _repair_plan

        state = NSOBGPPeerTemplateState.objects.create(
            management=self.management,
            template_name="AUDIT-PEERS",
            status="deploying",
        )

        plan = _repair_plan(self.device.pk, "bgp")

        write = next(write for write in plan.write_set if write.model_label == state._meta.label_lower)
        self.assertEqual(dict(write.values)["status"], "accepted")
        self.assertEqual(plan.content_keys, ())

    def test_a_repair_schedules_its_contribution_for_immediate_drain(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1641, "renderer-audit-drain")
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(verified_revision=None)
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        adapter = ReceiptAdapter()
        config, session = adapter.patches()

        with config, session:
            result = audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="cadence",
            )

        self.assertEqual(result.repaired, ("vlan",))
        self.assertEqual(len(adapter.requests), 1)
        self.assertFalse(
            NSOIntentOutboxEntry.objects.filter(
                device=self.device,
                scope="vlan",
                consumed_by_push_seq__isnull=True,
            ).exists()
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

        baseline = own_vlan(self.management, 1629, "renderer-audit-baseline")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        before_revision = revision.revision
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        vlan = VLAN(group=baseline.vlan.group, vid=1630, name="renderer-audit-created")
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
                device_id=self.device.pk,
                scope="vlan",
                native_model_label="ipam.vlan",
                native_key={"group_id": baseline.vlan.group_id, "vid": 1630},
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

    def test_repair_verifies_the_render_that_follows_its_own_write(self):
        """The stored proof is the POST-write render, so the next audit can match it.

        The wrapper stands in for a renderer that emits a repair-visible field: the audit's
        own lifecycle write moves it, so a proof taken from the pre-write render can never
        match the next render and the scope repairs forever.
        """
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision, NSOVLANState
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        state = own_vlan(self.management, 1637, "renderer-audit-post-write")
        mirror_update(state, status="deploying", apply_attempt_id=uuid4())
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(
            verified_revision=None,
            verified_fingerprint=None,
            verified_at=None,
        )
        real_render = delivery.render

        def render_with_status(key, device_id, adapter_device_id):
            rendered = real_render(key, device_id, adapter_device_id)
            rendered.payload = {
                "payload": rendered.payload,
                "statuses": list(
                    NSOVLANState.objects.filter(management__device_id=device_id)
                    .order_by("pk")
                    .values_list("status", flat=True)
                ),
            }
            return rendered

        with patch("netbox_nso_plugin.delivery.render", side_effect=render_with_status):
            result = audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="test",
                pre_capture=True,
            )
            after = delivery.render("vlan", self.device.pk, self.management.adapter_device_id)

        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        self.assertEqual(result.repaired, ("vlan",))
        self.assertEqual(after.payload["statuses"], ["accepted"])
        self.assertEqual(revision.verified_fingerprint, delivery.canonical_fingerprint(after.payload))

    def test_static_route_repair_demotes_a_deploying_row_the_push_filter_omits(self):
        """Apply deploys every accepted row; the repair must be able to demote every one.

        ``promote_current_intent`` moves accepted/apply_failed rows to ``deploying`` with no
        next-hop condition, so a repair that only considers the rows the renderer pushes
        strands an interface-next-hop row in ``deploying`` forever.
        """
        import time

        from netbox_nso_plugin.models import NSOIntentRevision, NSOStaticRouteState
        from netbox_nso_plugin.renderer_audit import _repair_with_retries

        route = own_route(self.management, "198.18.164.0/24", None, device=self.device)
        state = NSOStaticRouteState.objects.get(management=self.management, static_route=route)
        mirror_update(state, status="deploying", apply_attempt_id=uuid4())
        NSOIntentRevision.objects.filter(device=self.device, scope="static_route").update(verified_revision=None)

        # The repair itself, not the whole audit: `reconcile_scope_ownership` retracts this
        # route's manifest before the repair can run (reported separately, not this pin).
        repaired = _repair_with_retries(
            self.device.pk,
            ("static_route",),
            self.management,
            time.monotonic() + 120,
        )

        state.refresh_from_db()
        self.assertEqual(repaired, ("static_route",))
        self.assertEqual((state.status, state.apply_attempt_id), ("accepted", None))

    def test_a_repair_demotes_only_the_redistribution_rows_of_the_repaired_scope(self):
        """One spec covers bgp/isis/ospf; the row's ``dest_protocol`` is its scope."""
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        bgp = own_redistribution(self.management, "bgp", "connected")
        isis = own_redistribution(self.management, "isis", "connected")
        mirror_update(bgp, status="deploying")
        mirror_update(isis, status="deploying")
        NSOIntentRevision.objects.filter(device=self.device, scope="bgp").update(verified_revision=None)

        result = audit_renderer_scopes(
            self.device.pk,
            ["bgp"],
            trigger="test",
            pre_capture=True,
        )

        bgp.refresh_from_db()
        isis.refresh_from_db()
        self.assertEqual(result.repaired, ("bgp",))
        self.assertEqual(bgp.status, "accepted")
        self.assertEqual(isis.status, "deploying")

    def test_leaving_a_key_unknown_takes_the_revision_lock_at_its_declared_level(self):
        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.renderer_audit import _leave_unknown

        own_vlan(self.management, 1638, "renderer-audit-unknown-order")
        real_enter_level = apply_state._enter_level
        entered = []

        def record(level, key):
            entered.append((level, key))
            return real_enter_level(level, key)

        with patch("netbox_nso_plugin.apply_state._enter_level", side_effect=record):
            _leave_unknown(self.device.pk, ("vlan",))

        self.assertEqual(entered, [(7, (self.device.pk, "vlan"))])

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
            in_thread(lambda: NSOVLANState.objects.filter(pk=state.pk).update(last_sync_at=timezone.now()))
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
                "renderer_audit_scope_batch_cap": 1,
                "renderer_audit_tick_budget_seconds": 240,
            }
        }
    )
    def test_cadence_rotates_over_the_scopes_the_previous_tick_deferred(self):
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        first = audit_renderer_scopes(self.device.pk, ["vlan", "interface"], trigger="cadence")
        second = audit_renderer_scopes(self.device.pk, ["vlan", "interface"], trigger="cadence")

        self.assertEqual((first.audited, first.deferred), (("vlan",), ("interface",)))
        self.assertEqual((second.audited, second.deferred), (("interface",), ("vlan",)))

    def test_the_ownership_reconcile_is_not_started_without_a_tick_budget(self):
        """The planner's pass is outside the render budget, so it is not entered without one."""
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1640, "renderer-audit-ownership-budget")
        reconciled = []

        with (
            patch("netbox_nso_plugin.renderer_audit._budget_expired", return_value=True),
            patch(
                "netbox_nso_plugin.ownership_planner.reconcile_scope_ownership",
                side_effect=lambda *args: reconciled.append(args),
            ),
        ):
            result = audit_renderer_scopes(self.device.pk, ["vlan"], trigger="cadence")

        self.assertEqual(reconciled, [])
        self.assertEqual((result.repaired, result.deferred), ((), ("vlan",)))

    @override_settings(PLUGINS_CONFIG={"netbox_nso_plugin": {}})
    def test_the_default_batch_cap_admits_a_registry_that_grows_by_one_key(self):
        """The default is derived from the registry, so a new delivery key cannot exceed it."""
        import dataclasses

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.renderer_audit import _bounded_scopes

        registry = delivery.delivery_keys()
        grown = {**registry, "fake_scope": dataclasses.replace(registry["vlan"], key="fake_scope")}

        with patch("netbox_nso_plugin.delivery.delivery_keys", return_value=grown):
            selected, deferred = _bounded_scopes(tuple(grown), pre_capture=True)

        self.assertEqual(selected, tuple(grown))
        self.assertEqual(deferred, ())

    @override_settings(
        PLUGINS_CONFIG={
            "netbox_nso_plugin": {
                "renderer_audit_scope_batch_cap": 18,
                "renderer_audit_tick_budget_seconds": 0.5,
            }
        }
    )
    def test_cadence_defers_a_candidate_when_the_repair_budget_expires(self):
        import itertools

        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_scopes

        own_vlan(self.management, 1636, "renderer-audit-budget")
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(verified_revision=None)
        # A real clock, 0.4s per reading against the configured 0.5s budget: the optimistic
        # pass still fits, the repair that follows it does not.
        clock = itertools.count(0.0, 0.4)

        with patch("netbox_nso_plugin.renderer_audit._monotonic", side_effect=lambda: next(clock)):
            result = audit_renderer_scopes(
                self.device.pk,
                ["vlan"],
                trigger="cadence",
            )

        self.assertEqual(result.repaired, ())
        self.assertEqual(result.deferred, ("vlan",))

    def test_a_real_concurrent_update_serializes_the_repair_out_and_retries_it(self):
        """A genuine 40001, not a fabricated one: a foreign committed write races the locks.

        The competing write lands between the repeatable-read snapshot and the row locks,
        which is exactly where PostgreSQL answers ``SELECT ... FOR UPDATE`` with "could not
        serialize access". Only the first attempt races, so the retry has to recover.
        """
        import time

        from django.utils import timezone

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.models import NSOIntentRevision, NSOVLANState
        from netbox_nso_plugin.renderer_audit import _repair_with_retries

        state = own_vlan(self.management, 1639, "renderer-audit-serialization")
        NSOIntentRevision.objects.filter(device=self.device, scope="vlan").update(verified_revision=None)
        real_lock_shared = apply_state.lock_shared_dependencies
        collisions = []

        def collide(keys):
            if not collisions:
                collisions.append(True)
                in_thread(lambda: NSOVLANState.objects.filter(pk=state.pk).update(last_sync_at=timezone.now()))
            return real_lock_shared(keys)

        with patch("netbox_nso_plugin.apply_state.lock_shared_dependencies", side_effect=collide):
            repaired = _repair_with_retries(
                self.device.pk,
                ("vlan",),
                self.management,
                time.monotonic() + 120,
            )

        self.assertEqual(collisions, [True])
        self.assertEqual(repaired, ("vlan",))

    def test_a_serialization_failure_is_recognised_through_either_driver_attribute(self):
        from netbox_nso_plugin.renderer_audit import _serialization_failure

        class Psycopg2Failure(Exception):
            pgcode = "40001"

        class Psycopg3Failure(Exception):
            sqlstate = "40001"

        class OtherFailure(Exception):
            sqlstate = "23505"

        for cause, expected in ((Psycopg2Failure(), True), (Psycopg3Failure(), True), (OtherFailure(), False)):
            with self.subTest(cause=type(cause).__name__):
                failure = OperationalError("driver failure")
                failure.__cause__ = cause
                self.assertIs(_serialization_failure(failure), expected)

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
