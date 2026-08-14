# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O O3b: deployment-gate and restore management commands."""

from __future__ import annotations

import io
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from ._outbox_case import (
    ReceiptAdapter,
    entries,
    make_managed,
    own_route,
    own_vlan,
    state_of,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestDeploymentGate(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O3.16: every dirty-key shape blocks, while the complete healthy sequence passes."""

    tag = "gatecmd"
    adapter_device_id = 7801

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def _prepare(self):
        return call_command(
            "nso_intent_deployment_gate",
            prepare=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    def test_each_dirty_key_shape_refuses_the_gate(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentOutboxState

        cases = {
            "live claimed_at": (
                {"claimed_at": timezone.now()},
                None,
                "a live claim",
            ),
            "claim deletions": (
                {"claim_deletions": [{"route_id": 7, "triples": [], "unverified": True}]},
                None,
                "in-flight deletion authority",
            ),
            "row carrying push_seq": (
                {},
                77,
                "a row carrying a push_seq",
            ),
            "fence withheld": (
                {"fence_withheld_since": timezone.now()},
                None,
                "the fence is withheld",
            ),
        }
        for label, (state_fields, row_seq, expected) in cases.items():
            with self.subTest(case=label):
                NSOIntentOutboxEntry.objects.all().delete()
                NSOIntentOutboxState.objects.all().delete()
                NSOIntentOutboxState.objects.create(device=self.device, scope="static_route", **state_fields)
                if row_seq is not None:
                    NSOIntentOutboxEntry.objects.create(
                        device=self.device,
                        scope="static_route",
                        batch_id=1,
                        consumed_by_push_seq=row_seq,
                    )

                with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.time.sleep"):
                    with self.assertRaisesRegex(CommandError, expected):
                        self._prepare()

    def test_a_failed_verification_keeps_writes_blocked(self):
        """§4.6: the rollback happens with writes still blocked; only success or --abort resumes."""
        from netbox_nso_plugin import drain, outbox
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.deployment import DeploymentQuiesced, is_quiesced

        own_route(self.mgmt, "198.18.41.0/24", "198.18.0.1")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route") == drain.SUCCEEDED

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.time.sleep"):
            self._prepare()

        self.adapter.fail_with = AdapterError("adapter exploded", code="internal_error", status_code=500)
        config, session = self.adapter.patches()
        with config, session:
            with self.assertRaisesRegex(CommandError, "Verification push failed"):
                call_command(
                    "nso_intent_deployment_gate",
                    verify=True,
                    device_id=self.device.pk,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

        assert is_quiesced(), "a failed verification resumed writes"
        with self.assertRaises(DeploymentQuiesced):
            with transaction.atomic():
                outbox.enqueue(self.device.pk, "static_route")

        call_command("nso_intent_deployment_gate", abort=True, stdout=io.StringIO(), stderr=io.StringIO())
        assert not is_quiesced(), "--abort must release the gate"

    def test_a_reprepare_after_a_failed_verification_keeps_the_gate(self):
        """codex O3b review P1: --prepare must not release a gate it did not create."""
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.deployment import is_quiesced

        own_route(self.mgmt, "198.18.42.0/24", "198.18.0.1")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route") == drain.SUCCEEDED

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.time.sleep"):
            self._prepare()
        self.adapter.fail_with = AdapterError("adapter exploded", code="internal_error", status_code=500)
        config, session = self.adapter.patches()
        with config, session:
            with self.assertRaisesRegex(CommandError, "Verification push failed"):
                call_command(
                    "nso_intent_deployment_gate",
                    verify=True,
                    device_id=self.device.pk,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
        assert is_quiesced()

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.time.sleep"):
            with self.assertRaisesRegex(CommandError, "Deployment gate blocked"):
                self._prepare()
        assert is_quiesced(), "a failed re-prepare released a gate it did not create"

    def test_prepare_failure_preserves_a_gate_created_before_its_exclusive_transition(self):
        from netbox_nso_plugin.deployment import is_quiesced, quiesce, resume
        from netbox_nso_plugin.models import NSOIntentOutboxState

        NSOIntentOutboxState.objects.create(
            device=self.device,
            scope="static_route",
            claimed_at=timezone.now(),
        )

        def competing_transition():
            quiesce()
            return quiesce()

        try:
            with (
                patch(
                    "netbox_nso_plugin.management.commands.nso_intent_deployment_gate.quiesce",
                    side_effect=competing_transition,
                ),
                patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.time.sleep"),
            ):
                with self.assertRaisesRegex(CommandError, "Deployment gate blocked"):
                    self._prepare()
            assert is_quiesced(), "prepare released the competing command's gate"
        finally:
            resume()

    def test_verification_abandons_a_claim_that_carries_deletions(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.deployment import quiesce, resume

        route = own_route(self.mgmt, "198.18.43.0/24", "198.18.0.1")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route") == drain.SUCCEEDED
        with without_commit_drain():
            route.devices.remove(self.device)
        claim = drain.claim(self.device.pk, "static_route")
        assert claim is not None and [record["route_id"] for record in claim.deletions] == [route.pk]

        quiesce()
        try:
            with (
                patch(
                    "netbox_nso_plugin.management.commands.nso_intent_deployment_gate.drain.gate_blockers",
                    return_value=[],
                ),
                patch(
                    "netbox_nso_plugin.management.commands.nso_intent_deployment_gate.drain.claim", return_value=claim
                ),
            ):
                with self.assertRaisesRegex(CommandError, "no-deletion static verification push"):
                    call_command(
                        "nso_intent_deployment_gate",
                        verify=True,
                        device_id=self.device.pk,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )
            state = state_of(self.device, "static_route")
            assert state.push_seq is None, "the rejected verification claim remained active"
            assert state.claim_deletions == []
            assert [record["route_id"] for record in state.queued_deletions] == [route.pk]
        finally:
            resume()

    def test_the_full_gate_sequence_quiesces_and_verifies_a_no_deletion_static_push(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        from netbox_nso_plugin import drain, outbox
        from netbox_nso_plugin.deployment import DeploymentQuiesced
        from netbox_nso_plugin.intent_drift import resync_intent
        from netbox_nso_plugin.middleware import IntentDeploymentMiddleware
        from netbox_nso_plugin.onboarding import advance_provisioning, onboard_candidate
        from netbox_nso_plugin.reconcile import reconcile_device

        route = own_route(self.mgmt, "198.18.40.0/24", "198.18.0.1")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route") == drain.SUCCEEDED
        assert entries(self.device, "static_route") == []

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate.time.sleep") as sleeper:
            self._prepare()

        with self.assertRaisesRegex(RuntimeError, "deployment is quiesced"):
            with transaction.atomic():
                outbox.enqueue(self.device.pk, "static_route")
        blocked = IntentDeploymentMiddleware(lambda _request: HttpResponse("mutated"))(RequestFactory().post("/api/"))
        assert blocked.status_code == 503
        for operation in (
            lambda: reconcile_device(None),
            lambda: resync_intent(None, None),
            lambda: onboard_candidate(None, None),
            lambda: advance_provisioning(None),
        ):
            with self.assertRaises(DeploymentQuiesced):
                operation()
        assert drain.drain_intent_outbox() == (0, 0), "the scheduled drain tick ran while quiesced"

        stdout = io.StringIO()
        config, session = self.adapter.patches()
        with config, session:
            call_command(
                "nso_intent_deployment_gate",
                verify=True,
                device_id=self.device.pk,
                stdout=stdout,
                stderr=io.StringIO(),
            )

        sleeper.assert_called_once_with(635)
        verification = self.adapter.requests[-1]
        assert verification["body"]["deleted_routes"] == []
        assert verification["push_seq"] == max(receipt["push_seq"] for receipt in self.adapter.receipts.values())
        assert {tuple(sorted(read.items())) for read in self.adapter.receipt_reads} >= {
            tuple(sorted({"device_id": self.adapter_device_id, "section": "static_route"}.items()))
        }
        assert state_of(self.device, "static_route").push_seq is None
        assert drain.gate_blockers() == []
        assert "Deployment verification passed" in stdout.getvalue()

        with without_commit_drain(), transaction.atomic():
            outbox.enqueue(self.device.pk, "static_route")
        assert entries(self.device, "static_route", unconsumed=True), "normal operation did not resume"
        assert route.pk is not None


class TestIntentRestoreResolvesEveryReceiptCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O3.18: the command drives all four verdicts through the production receipt GET."""

    tag = "restorecmd"
    adapter_device_id = 7802

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)

    def _lost_vlan_response(self, vid):
        from netbox_nso_plugin import drain

        own_vlan(self.mgmt, vid, self.tag)
        claim = drain.claim(self.device.pk, "vlan")
        config, session = self.adapter.patches()
        with config, session:
            drain.send_claim(claim)
        [url] = self.adapter.receipts
        return claim, url

    def _restore(self):
        config, session = self.adapter.patches()
        with config, session:
            return call_command("nso_intent_restore", stdout=io.StringIO(), stderr=io.StringIO())

    def _assert_real_reads(self):
        assert {} in self.adapter.receipt_reads, "the command did not read the fleet-wide maxima"
        assert {
            "device_id": self.adapter_device_id,
            "section": "vlan",
        } in self.adapter.receipt_reads, "the command did not read the key through its registry section"

    def test_same_sequence_and_digest_settles_after_full_ack_validation(self):
        claim, _url = self._lost_vlan_response(930)

        self._restore()

        self._assert_real_reads()
        assert state_of(self.device, "vlan").push_seq is None
        assert entries(self.device, "vlan") == []
        assert claim.push_seq is not None

    def test_higher_receipt_rebases_and_returns_consumed_rows_to_the_fold(self):
        from netbox_nso_plugin.outbox import allocate_push_seq

        claim, url = self._lost_vlan_response(931)
        accepted = claim.push_seq + 50
        self.adapter.receipts[url]["push_seq"] = accepted

        self._restore()

        self._assert_real_reads()
        state = state_of(self.device, "vlan")
        assert state.push_seq is None
        assert [row.consumed_by_push_seq for row in entries(self.device, "vlan")] == [None]
        assert allocate_push_seq() > accepted

    def test_equal_sequence_with_another_digest_fails_closed_and_names_the_key(self):
        from netbox_nso_plugin.deployment import is_quiesced

        claim, url = self._lost_vlan_response(932)
        self.adapter.receipts[url]["digest"] = "f" * 64

        with self.assertRaisesRegex(CommandError, rf"{self.device.pk}/vlan"):
            self._restore()

        self._assert_real_reads()
        assert state_of(self.device, "vlan").push_seq == claim.push_seq
        assert is_quiesced(), "failed-closed restore resumed work against an unresolved key"

    def test_a_same_sequence_receipt_with_another_mode_fails_closed(self):
        """codex O3b review P1: mode is part of the receipt identity, never discarded."""
        from netbox_nso_plugin.deployment import is_quiesced

        claim, url = self._lost_vlan_response(934)
        self.adapter.receipts[url]["params"] = {"store_only": "true"}

        with self.assertRaisesRegex(CommandError, rf"{self.device.pk}/vlan"):
            self._restore()

        self._assert_real_reads()
        assert state_of(self.device, "vlan").push_seq == claim.push_seq
        assert is_quiesced(), "a mode-mismatched receipt settled a restored claim"

    def test_non_boolean_receipt_modes_fail_closed(self):
        from netbox_nso_plugin.management.commands.nso_intent_restore import _normalize

        valid = {
            "push_seq": 1,
            "request_digest": "a" * 64,
            "response": {},
            "store_only": False,
            "delete_origin": False,
            "backfill_only": False,
        }
        for field, malformed in (
            ("store_only", "false"),
            ("delete_origin", 0),
            ("backfill_only", None),
        ):
            with self.subTest(field=field, malformed=malformed):
                receipt = {**valid, field: malformed}
                with self.assertRaises(CommandError):
                    _normalize(receipt)

    def test_restore_preserves_a_gate_created_by_another_invocation(self):
        from netbox_nso_plugin.deployment import is_quiesced, quiesce, resume

        quiesce()
        try:
            self._restore()
            assert is_quiesced(), "restore resumed another invocation's deployment gate"
        finally:
            resume()

    def test_lower_receipt_releases_the_restored_lease_for_normal_replay(self):
        from netbox_nso_plugin import drain

        claim, url = self._lost_vlan_response(933)
        self.adapter.receipts[url]["push_seq"] = claim.push_seq - 1

        self._restore()

        self._assert_real_reads()
        state = state_of(self.device, "vlan")
        assert state.push_seq == claim.push_seq
        assert state.claimed_at is None, "the restored live lease stranded the replay for ten minutes"
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "vlan") == drain.SUCCEEDED


class TestIntentRestoreProtectsTheRouteIdentityNamespace(
    _CascadeFlushMixin,
    IntentPushResetMixin,
    TransactionTestCase,
):
    """O3.21: restore advances real route ids and clears every claimed acknowledgement."""

    tag = "routeids"
    adapter_device_id = 7803

    def setUp(self):
        super().setUp()
        self.adapter = ReceiptAdapter()

    @staticmethod
    def _route_sequence():
        from django.db import connection
        from netbox_routing.models import StaticRoute

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [StaticRoute._meta.db_table])
            return cursor.fetchone()[0]

    def _next_route_id(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval(%s)", [self._route_sequence()])
            return int(cursor.fetchone()[0])

    def _restore(self):
        config, session = self.adapter.patches()
        with config, session:
            call_command("nso_intent_restore", stdout=io.StringIO(), stderr=io.StringIO())

    def test_restore_advances_the_real_static_route_sequence_past_the_adapter_maximum(self):
        from django.db import connection
        from netbox_routing.models import StaticRoute

        route = StaticRoute.objects.create(prefix="198.18.80.0/24", next_hop="198.18.0.8", metric=1)
        watermark = route.pk + 40
        with connection.cursor() as cursor:
            cursor.execute("SELECT setval(%s, %s, true)", [self._route_sequence(), route.pk])
        self.adapter.global_max_route_id = watermark

        self._restore()

        assert self._next_route_id() > watermark
        assert {} in self.adapter.receipt_reads, "production did not read the maximum from the HTTP response"

    def test_a_missing_route_id_maximum_fails_fast_instead_of_defaulting(self):
        self.adapter.include_global_max_route_id = False

        with self.assertRaisesRegex(CommandError, "global_max_route_id"):
            self._restore()

    def test_ordinary_big_auto_field_operation_does_not_advance_to_an_adapter_watermark(self):
        from netbox_routing.models import StaticRoute

        route = StaticRoute.objects.create(prefix="198.18.81.0/24", next_hop="198.18.0.9", metric=1)
        would_be_adapter_maximum = route.pk + 40

        assert self._next_route_id() == route.pk + 1
        assert self._next_route_id() < would_be_adapter_maximum
        assert self.adapter.receipt_reads == []

    def test_restore_clears_last_acked_triple_on_every_overlay(self):
        from netbox_nso_plugin.models import NSOStaticRouteState

        device, mgmt = make_managed(self.tag, self.adapter_device_id)
        first = own_route(mgmt, "198.18.82.0/24", "198.18.0.10")
        second = own_route(mgmt, "198.18.83.0/24", "198.18.0.11")
        NSOStaticRouteState.objects.filter(management=mgmt).update(
            last_acked_triple={"vrf": "", "prefix": "198.18.0.0/15", "next_hop": "198.18.0.1"}
        )
        assert NSOStaticRouteState.objects.filter(last_acked_triple__isnull=False).count() == 2

        self._restore()

        assert NSOStaticRouteState.objects.filter(last_acked_triple__isnull=False).count() == 0
        assert {first.pk, second.pk}
        assert device.pk is not None
