# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O O3b: deployment-gate and restore management commands."""

from __future__ import annotations

import io
import threading
import time
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from ._outbox_case import (
    ReceiptAdapter,
    entries,
    make_managed,
    marking_mode,
    own_route,
    own_vlan,
    state_of,
    without_commit_drain,
)
from ._static_route_case import _unassign_and_retire
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestReceiptSelectors(SimpleTestCase):
    def test_deployment_gate_rejects_a_boolean_device_id(self):
        from netbox_nso_plugin.management.commands.nso_intent_deployment_gate import _receipt_for

        document = {"receipts": [{"device_id": True, "section": "vlan"}]}

        with self.assertRaisesRegex(CommandError, "Missing receipt"):
            _receipt_for(document, adapter_device_id=1, section="vlan")

    def test_restore_ignores_a_boolean_device_id(self):
        from netbox_nso_plugin.management.commands.nso_intent_restore import _key_receipt

        document = {"receipts": [{"device_id": True, "section": "vlan"}]}

        self.assertIsNone(_key_receipt(document, adapter_device_id=1, section="vlan"))

    def test_restore_failure_guidance_requires_reconciliation_before_abort(self):
        from netbox_nso_plugin.management.commands.nso_intent_restore import _gate_failure_guidance

        with self.assertRaises(CommandError) as raised:
            with _gate_failure_guidance(created=True):
                raise CommandError("Restore failed closed")

        message = str(raised.exception)
        self.assertLess(message.index("nso_intent_restore"), message.index("nso_intent_deployment_gate --abort"))

    def test_restore_does_not_tell_the_operator_to_abort_a_preexisting_gate(self):
        from netbox_nso_plugin.management.commands.nso_intent_restore import _gate_failure_guidance

        with self.assertRaises(CommandError) as raised:
            with _gate_failure_guidance(created=False):
                raise CommandError("Restore failed closed")

        message = str(raised.exception)
        self.assertIn("owning operation", message)
        self.assertNotIn("nso_intent_deployment_gate --abort", message)

    def test_restore_adapter_failure_preserves_gate_recovery_guidance(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.management.commands.nso_intent_restore import _gate_failure_guidance

        with self.assertRaises(CommandError) as raised:
            with _gate_failure_guidance(created=True):
                raise AdapterError("adapter receipt lookup failed", code="nso_unreachable")

        self.assertIn("adapter receipt lookup failed", str(raised.exception))
        self.assertIn("nso_intent_restore", str(raised.exception))


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

    def test_quiesce_times_out_while_a_shared_operation_is_active(self):
        from netbox_nso_plugin import deployment

        holder_ready = threading.Event()
        release_holder = threading.Event()
        errors = []

        def hold_shared_lock():
            close_old_connections()
            try:
                with deployment.operation("timeout test holder"):
                    holder_ready.set()
                    release_holder.wait(10)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        def request_quiesce():
            close_old_connections()
            try:
                deployment.quiesce()
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_shared_lock)
        waiter = threading.Thread(target=request_quiesce)
        holder.start()
        waiter_finished = False
        try:
            self.assertTrue(holder_ready.wait(5), "the shared lock was not acquired")
            with patch.object(deployment, "_EXCLUSIVE_LOCK_TIMEOUT_MS", 50):
                waiter.start()
                waiter.join(timeout=1)
                waiter_finished = not waiter.is_alive()
        finally:
            release_holder.set()
            holder.join(timeout=10)
            if waiter.ident is not None:
                waiter.join(timeout=10)
            if deployment.is_quiesced():
                deployment.resume()

        self.assertTrue(waiter_finished, "quiesce ignored its advisory-lock timeout")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], deployment.DeploymentTransitionTimeout)
        self.assertIn("timed out", str(errors[0]).lower())

    def test_a_pending_exclusive_transition_refuses_new_shared_work(self):  # noqa: C901
        from netbox_nso_plugin import deployment

        holder_ready = threading.Event()
        release_holder = threading.Event()
        waiter_ready = threading.Event()
        waiter_pid = []
        errors = []

        def hold_shared_lock():
            close_old_connections()
            try:
                with deployment.operation("test holder"):
                    holder_ready.set()
                    release_holder.wait(10)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        def request_exclusive_lock():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    waiter_pid.append(cursor.fetchone()[0])
                waiter_ready.set()
                deployment.quiesce()
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_shared_lock)
        waiter = threading.Thread(target=request_exclusive_lock)
        holder.start()
        queued = False
        outcomes = {}
        late = []

        def attempt_operation():
            close_old_connections()
            try:
                with deployment.operation("late operation"):
                    outcomes["operation"] = "admitted"
            except deployment.DeploymentQuiesced:
                outcomes["operation"] = "refused"
            finally:
                connection.close()

        def attempt_mutation():
            close_old_connections()
            try:
                with transaction.atomic():
                    deployment.lock_mutation()
                outcomes["mutation"] = "admitted"
            except deployment.DeploymentQuiesced:
                outcomes["mutation"] = "refused"
            finally:
                connection.close()

        try:
            self.assertTrue(holder_ready.wait(5), "the shared lock was not acquired")
            waiter.start()
            self.assertTrue(waiter_ready.wait(5), "the exclusive waiter did not start")

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                        [waiter_pid[0]],
                    )
                    queued = cursor.fetchone()[0]
                if queued:
                    break
                time.sleep(0.01)

            late = [threading.Thread(target=attempt_operation), threading.Thread(target=attempt_mutation)]
            for worker in late:
                worker.start()
            for worker in late:
                worker.join(timeout=1)
            finished_before_release = [not worker.is_alive() for worker in late]
        finally:
            release_holder.set()
            holder.join(timeout=10)
            if waiter.ident is not None:
                waiter.join(timeout=10)
            for worker in late:
                worker.join(timeout=10)
            if deployment.is_quiesced():
                deployment.resume()

        self.assertTrue(queued, "the exclusive transition never queued behind the shared lock")
        self.assertEqual(finished_before_release, [True, True], "new shared work blocked behind the transition")
        self.assertEqual(outcomes, {"operation": "refused", "mutation": "refused"})
        self.assertEqual(errors, [])

    def test_verification_receipt_rejects_non_canonical_field_types(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.management.commands.nso_intent_deployment_gate import _verification_receipt

        own_route(self.mgmt, "198.18.39.0/24", "198.18.0.1")
        claim = drain.claim(self.device.pk, "static_route", force=True)
        wire = delivery.wire_body(
            claim.rendered,
            claim.payload,
            deletions=[],
            marking_mode=claim.marking_mode,
        )
        valid = {
            "push_seq": claim.push_seq,
            "request_digest": drain.wire_digest(wire),
            "store_only": False,
            "delete_origin": False,
            "backfill_only": False,
            "status_code": 200,
            "response": {},
        }
        for field, malformed in (
            ("push_seq", float(claim.push_seq)),
            ("status_code", 200.0),
            ("store_only", 0),
            ("delete_origin", 0),
            ("backfill_only", 0),
        ):
            with self.subTest(field=field):
                with self.assertRaises(CommandError):
                    _verification_receipt(claim, {**valid, field: malformed})

    def test_verification_receipt_uses_the_claim_marking_mode(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.management.commands.nso_intent_deployment_gate import _verification_receipt

        own_route(self.mgmt, "198.18.40.0/24", "198.18.0.1")
        claim = drain.claim(self.device.pk, "static_route", force=True)
        expected_wire = delivery.wire_body(
            claim.rendered,
            claim.payload,
            deletions=[],
            marking_mode=claim.marking_mode,
        )
        receipt = {
            "push_seq": claim.push_seq,
            "request_digest": drain.wire_digest(expected_wire),
            "store_only": False,
            "delete_origin": False,
            "backfill_only": False,
            "status_code": 200,
        }
        observed_modes = []
        original_wire_body = delivery.wire_body

        def record_marking_mode(*args, **kwargs):
            observed_modes.append(kwargs.get("marking_mode"))
            return original_wire_body(*args, **kwargs)

        with patch.object(delivery, "wire_body", side_effect=record_marking_mode):
            _verification_receipt(claim, receipt)

        self.assertEqual(observed_modes, [claim.marking_mode])

    def test_pending_transition_probe_is_scoped_to_the_current_database(self):
        from netbox_nso_plugin import deployment

        class ProbeCursor:
            def execute(self, sql, params):
                self.sql = " ".join(sql.lower().split())
                self.params = params

            def fetchone(self):
                return (False,)

        cursor = ProbeCursor()

        self.assertFalse(deployment._exclusive_transition_pending(cursor))
        self.assertEqual(cursor.params, [deployment._LOCK_KEY >> 32, deployment._LOCK_KEY & 0xFFFFFFFF])
        self.assertIn(
            "database = (select oid from pg_database where datname = current_database())",
            cursor.sql,
        )

    def test_quiescence_middleware_only_blocks_nso_plugin_mutations(self):
        from django.conf import settings
        from django.urls import reverse

        from netbox_nso_plugin.deployment import quiesce, resume

        middleware_path = "netbox_nso_plugin.middleware.IntentDeploymentMiddleware"
        self.assertIn(middleware_path, settings.MIDDLEWARE)

        quiesce()
        try:
            plugin_responses = [
                self.client.post(reverse("plugins:netbox_nso_plugin:onboard")),
                self.client.post("/api/plugins/nso/onboard/"),
            ]
            unrelated_response = self.client.post("/login/", {"username": "not-a-user", "password": "wrong"})
        finally:
            resume()

        self.assertEqual([response.status_code for response in plugin_responses], [503, 503])
        self.assertEqual(unrelated_response.status_code, 200)

    def test_quiescence_quotes_the_refused_request_path_in_the_log(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        from netbox_nso_plugin.deployment import quiesce, resume
        from netbox_nso_plugin.middleware import IntentDeploymentMiddleware

        request = RequestFactory().post("/plugins/nso/refused/")
        request.path_info = "/plugins/nso/refused\nforged-record"

        quiesce()
        try:
            with self.assertLogs("netbox_nso_plugin.middleware", level="INFO") as logged:
                response = IntentDeploymentMiddleware(lambda _request: HttpResponse("mutated"))(request)
        finally:
            resume()

        assert response.status_code == 503
        message = logged.output[0].split(":", 2)[-1]
        assert "\n" not in message
        assert repr(request.path_info) in message

    def test_plugin_request_does_not_wrap_network_delivery_in_a_database_transaction(self):
        from django.db import connection
        from django.http import HttpResponse
        from django.test import RequestFactory

        from netbox_nso_plugin.middleware import IntentDeploymentMiddleware

        in_atomic_block = []

        def inspect_transaction(_request):
            in_atomic_block.append(connection.in_atomic_block)
            return HttpResponse("ok")

        response = IntentDeploymentMiddleware(inspect_transaction)(
            RequestFactory().post("/plugins/nso/device-management/1/actions/apply/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(in_atomic_block, [False])

    def test_quiescence_returns_503_and_rolls_back_a_core_intent_mutation(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from extras.models import Tag

        from netbox_nso_plugin.deployment import quiesce, resume
        from netbox_nso_plugin.middleware import IntentDeploymentMiddleware
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOStaticRouteState

        route = own_route(self.mgmt, "198.18.43.0/24", "198.18.0.1")
        NSOIntentOutboxEntry.objects.all().delete()

        def remove_route(_request):
            Tag.objects.create(name="request rollback witness", slug="request-rollback-witness")
            with transaction.atomic():
                _unassign_and_retire(route, self.device)
            return HttpResponse("mutated")

        quiesce()
        try:
            response = IntentDeploymentMiddleware(remove_route)(
                RequestFactory().post(f"/plugins/netbox-routing/static-routes/{route.pk}/edit/")
            )
        finally:
            resume()

        assert response.status_code == 503
        assert route.devices.filter(pk=self.device.pk).exists(), "the refused request committed the route removal"
        assert NSOStaticRouteState.objects.filter(management=self.mgmt, static_route=route).exists()
        assert not NSOIntentOutboxEntry.objects.exists(), "the refused request left partial outbox work"
        assert not Tag.objects.filter(slug="request-rollback-witness").exists(), (
            "the refused request left an earlier autocommitted write"
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
                {"consumed_by_push_seq": 77},
                "a row carrying a push_seq",
            ),
            "unconsumed entry": (
                {},
                {"consumed_by_push_seq": None},
                "an unconsumed entry",
            ),
            "unacknowledged operation": (
                {"push_seq": 91},
                None,
                "an unacknowledged operation",
            ),
            "queued deletions": (
                {"queued_deletions": [{"route_id": 9, "triples": [], "unverified": True}]},
                None,
                "queued deletions",
            ),
            "revoked ids": (
                {"revoked_ids": [11]},
                None,
                "revoked ids",
            ),
            "fence withheld": (
                {"fence_withheld_since": timezone.now()},
                None,
                "the fence is withheld",
            ),
        }
        for label, (state_fields, entry_fields, expected) in cases.items():
            with self.subTest(case=label):
                NSOIntentOutboxEntry.objects.all().delete()
                NSOIntentOutboxState.objects.all().delete()
                NSOIntentOutboxState.objects.create(device=self.device, scope="static_route", **state_fields)
                if entry_fields is not None:
                    NSOIntentOutboxEntry.objects.create(
                        device=self.device,
                        scope="static_route",
                        batch_id=1,
                        **entry_fields,
                    )

                with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep"):
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

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep"):
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

    def test_a_receipt_read_failure_is_a_named_command_failure(self):
        from netbox_nso_plugin import adapter_client, drain
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.deployment import is_quiesced

        own_route(self.mgmt, "198.18.42.0/24", "198.18.0.1")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route") == drain.SUCCEEDED

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep"):
            self._prepare()

        config, session = self.adapter.patches()
        with (
            config,
            session,
            patch.object(
                adapter_client,
                "get_intent_receipts",
                side_effect=AdapterError("receipt read failed", code="nso_unreachable"),
            ),
            self.assertRaisesRegex(CommandError, "Verification failed: receipt read failed"),
        ):
            call_command(
                "nso_intent_deployment_gate",
                verify=True,
                device_id=self.device.pk,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        assert is_quiesced(), "a failed receipt read resumed writes"

    def test_a_reprepare_after_a_failed_verification_keeps_the_gate(self):
        """codex O3b review P1: --prepare must not release a gate it did not create."""
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.deployment import is_quiesced

        own_route(self.mgmt, "198.18.42.0/24", "198.18.0.1")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route") == drain.SUCCEEDED

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep"):
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

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep"):
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
                patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep"),
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
            _unassign_and_retire(route, self.device)
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
        from netbox_nso_plugin.intent_state import content_mutation
        from netbox_nso_plugin.middleware import IntentDeploymentMiddleware
        from netbox_nso_plugin.onboarding import advance_provisioning, onboard_candidate
        from netbox_nso_plugin.reconcile import reconcile_device

        own_route(self.mgmt, "198.18.40.0/24", "198.18.0.1")
        config, session = self.adapter.patches()
        with config, session:
            assert drain.drain_key(self.device.pk, "static_route") == drain.SUCCEEDED
        assert entries(self.device, "static_route") == []

        with patch("netbox_nso_plugin.management.commands.nso_intent_deployment_gate._sleep") as sleeper:
            self._prepare()

        with self.assertRaisesRegex(RuntimeError, "deployment is quiesced"):
            with transaction.atomic():
                outbox.enqueue(self.device.pk, "static_route")
        blocked = IntentDeploymentMiddleware(lambda _request: HttpResponse("mutated"))(
            RequestFactory().post("/api/plugins/nso/onboard/")
        )
        assert blocked.status_code == 503
        for operation in (
            lambda: reconcile_device(None),
            lambda: resync_intent(None, None),
            lambda: onboard_candidate(None, None),
            lambda: advance_provisioning(None),
        ):
            with self.assertRaises(DeploymentQuiesced):
                operation()
        with patch("netbox_nso_plugin.drain._compact_intent_outbox") as compact:
            assert drain.drain_intent_outbox() == (0, 0), "the scheduled drain tick ran while quiesced"
        compact.assert_not_called()

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

        with without_commit_drain(), content_mutation({(self.device.pk, "static_route")}):
            outbox.enqueue(self.device.pk, "static_route")
        assert entries(self.device, "static_route", unconsumed=True), "normal operation did not resume"


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

    def test_the_global_watermark_is_fully_advanced_without_an_outstanding_claim(self):
        from django.db import connection

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.outbox import PUSH_SEQ_ADVANCE_BATCH, PUSH_SEQ_SEQUENCE, allocate_push_seq

        claim, url = self._lost_vlan_response(935)
        assert drain.settle(claim, {"count": 1}) == drain.SUCCEEDED
        accepted = claim.push_seq + PUSH_SEQ_ADVANCE_BATCH + 1
        self.adapter.receipts[url]["push_seq"] = accepted
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT setval('{PUSH_SEQ_SEQUENCE}', %s, true)", [claim.push_seq])

        self._restore()

        assert allocate_push_seq() > accepted

    def test_a_stalled_watermark_advance_names_itself_instead_of_spinning(self):
        """The advance was an unbounded ``while``: a non-increasing return burned the whole
        command instead of failing with a cause an operator can read."""
        from django.db import connection

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.outbox import PUSH_SEQ_ADVANCE_BATCH, PUSH_SEQ_SEQUENCE

        claim, url = self._lost_vlan_response(936)
        assert drain.settle(claim, {"count": 1}) == drain.SUCCEEDED
        accepted = claim.push_seq + PUSH_SEQ_ADVANCE_BATCH + 1
        self.adapter.receipts[url]["push_seq"] = accepted
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT setval('{PUSH_SEQ_SEQUENCE}', %s, true)", [claim.push_seq])

        with (
            patch("netbox_nso_plugin.outbox.advance_push_seq", return_value=claim.push_seq),
            self.assertRaisesRegex(CommandError, "advance stalled"),
        ):
            self._restore()

    def test_an_implausibly_distant_push_sequence_watermark_is_refused_before_advancing(self):
        from netbox_nso_plugin import drain, outbox

        claim, url = self._lost_vlan_response(939)
        before = outbox.issued_push_seq()
        self.adapter.receipts[url]["push_seq"] = claim.push_seq + drain.MAX_RESTORE_GAP + 1

        with (
            patch(
                "netbox_nso_plugin.outbox.advance_push_seq",
                side_effect=AssertionError("an out-of-range watermark reached the sequence"),
            ) as advance,
            self.assertRaisesRegex(CommandError, "push-seq watermark .* ahead"),
        ):
            self._restore()

        advance.assert_not_called()
        assert outbox.issued_push_seq() == before, "the refused watermark burned the push-sequence namespace"

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

    def test_restore_uses_the_claims_persisted_marking_mode(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        route = own_route(self.mgmt, "198.18.44.0/24", "198.18.0.1")
        NSOIntentOutboxEntry.objects.all().delete()

        with marking_mode("static_route", delivery.MARKING_QUERY_FLAG):
            with without_commit_drain():
                _unassign_and_retire(route, self.device)
            claim = drain.claim(self.device.pk, "static_route")
            assert [record["route_id"] for record in claim.deletions] == [route.pk]
            config, session = self.adapter.patches()
            with config, session:
                drain.send_claim(claim)

        self._restore()

        assert {} in self.adapter.receipt_reads
        assert {
            "device_id": self.adapter_device_id,
            "section": "static_route",
        } in self.adapter.receipt_reads
        assert state_of(self.device, "static_route").push_seq is None

    def test_restore_rejects_invalid_persisted_claim_flags(self):
        from netbox_nso_plugin.deployment import is_quiesced

        claim, _url = self._lost_vlan_response(938)
        state = state_of(self.device, "vlan")
        state.claim_flags = {**state.claim_flags, "marking_mode": "corrupt"}
        state.save(update_fields=["claim_flags"])

        with self.assertRaises(CommandError) as raised:
            self._restore()

        self.assertIn(f"{self.device.pk}/vlan", str(raised.exception))
        self.assertIn("invalid claim flags", str(raised.exception))
        self.assertIn("nso_intent_deployment_gate --abort", str(raised.exception))
        assert state_of(self.device, "vlan").push_seq == claim.push_seq
        assert is_quiesced(), "failed restore resumed the deployment gate"

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
        from netbox_nso_plugin import drain, outbox

        # Floor the sequence first: on a worker whose database never allocated, the claim
        # takes seq 1, and the lower receipt would fabricate seq 0, which no adapter serves.
        outbox.advance_push_seq(1)
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

    def test_an_implausibly_distant_route_watermark_is_refused_before_advancing(self):
        from netbox_nso_plugin import drain

        issued = self._next_route_id()
        self.adapter.global_max_route_id = issued + drain.MAX_RESTORE_GAP + 2

        with self.assertRaisesRegex(CommandError, "route-id watermark .* ahead"):
            self._restore()

        assert self._next_route_id() == issued + 2, "the refused watermark burned through the route-id namespace"

    def test_a_stalled_route_sequence_names_itself(self):
        from django.db import connection

        from netbox_nso_plugin.restore import advance_static_route_pk

        class StalledCursor:
            def __init__(self):
                self.row = None
                self.advance_attempts = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params):
                if "pg_get_serial_sequence" in sql:
                    self.row = ("public.netbox_routing_staticroute_id_seq",)
                elif "SELECT nextval" in sql:
                    self.row = (40,)
                else:
                    self.advance_attempts += 1
                    if self.advance_attempts > 1:
                        raise AssertionError("the sequence loop retried after it stopped advancing")
                    self.row = (40,)

            def fetchone(self):
                return self.row

        with (
            patch.object(connection, "cursor", return_value=StalledCursor()),
            self.assertRaisesRegex(CommandError, "route-id advance stalled at 40"),
        ):
            advance_static_route_pk(41)

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

        _device, mgmt = make_managed(self.tag, self.adapter_device_id)
        own_route(mgmt, "198.18.82.0/24", "198.18.0.10")
        own_route(mgmt, "198.18.83.0/24", "198.18.0.11")
        NSOStaticRouteState.objects.filter(management=mgmt).update(
            last_acked_triple={"vrf": "", "prefix": "198.18.0.0/15", "next_hop": "198.18.0.1"}
        )
        assert NSOStaticRouteState.objects.filter(last_acked_triple__isnull=False).count() == 2

        self._restore()

        assert NSOStaticRouteState.objects.filter(last_acked_triple__isnull=False).count() == 0
