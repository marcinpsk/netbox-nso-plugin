# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the off-request reconcile job and the sync-complete callback endpoint."""

import os
import sys
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import requests
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from utilities.testing import APITestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

from ._adapter_http import make_response
from ._outbox_case import ReceiptAdapter, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestProvisionTombstoneEnqueue(TestCase):
    def test_inline_sweep_failure_is_left_for_the_cadence_retry(self):
        from netbox_nso_plugin.reconcile import enqueue_provision_tombstone_sweep

        attempt_id = uuid4()
        with (
            patch.dict(sys.modules, {"django_rq": None}),
            patch(
                "netbox_nso_plugin.reconcile.run_provision_tombstone_sweep",
                side_effect=RuntimeError("sweep unavailable"),
            ),
        ):
            result = enqueue_provision_tombstone_sweep(attempt_id)

        self.assertIsNone(result)

    def test_queue_failure_is_left_for_the_cadence_retry(self):
        from netbox_nso_plugin.reconcile import enqueue_provision_tombstone_sweep

        attempt_id = uuid4()
        with patch("django_rq.get_queue") as get_queue:
            get_queue.return_value.enqueue.side_effect = RuntimeError("queue unavailable")

            result = enqueue_provision_tombstone_sweep(attempt_id)

        self.assertIsNone(result)


def _make_device(name="rec-dev"):
    mfg = Manufacturer.objects.create(name=f"{name}-mfg", slug=f"{name}-mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{name}-dt", slug=f"{name}-dt")
    role = DeviceRole.objects.create(name=f"{name}-role", slug=f"{name}-role")
    site = Site.objects.create(name=f"{name}-site", slug=f"{name}-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


@contextmanager
def _patch_apply_pushes(answers=None, *, apply_error=None):
    """Run actual preparation claims against one adapter HTTP double."""
    answers = answers or {}

    def respond(body):
        if isinstance(body, dict) and "deleted_routes" in body and "static_route" in answers:
            answer = answers["static_route"]
            if isinstance(answer, dict):
                default = ReceiptAdapter._default_response(body)
                return {**{name: value for name, value in default.items() if name != "count"}, **answer}
            return answer
        return ReceiptAdapter._default_response(body)

    class PreparationAdapter(ReceiptAdapter):
        def _handle(self, method, url, **kwargs):
            if method == "POST" and url.endswith("/actions/apply") and apply_error is not None:
                if apply_error.status_code is None:
                    raise requests.ConnectionError("Apply admission answer was lost.")
                return make_response(
                    apply_error.status_code,
                    {
                        "error": {
                            "code": apply_error.code,
                            "message": "Adapter admission failed.",
                            "detail": apply_error.detail or {},
                        }
                    },
                )
            return super()._handle(method, url, **kwargs)

    adapter = PreparationAdapter(respond=respond)
    config, session = adapter.patches()
    with config, session:
        yield adapter


def _forced_scopes(adapter, device_id):
    """Return the scopes with real acknowledged store-only requests."""
    from netbox_nso_plugin import delivery
    from netbox_nso_plugin.models import NSOAcceptedSelection

    scopes = {entry.section: entry.key for entry in delivery.delivery_keys().values()}
    requests_ = [request for request in adapter.requests if request["params"].get("store_only") == "true"]
    selections = dict(
        NSOAcceptedSelection.objects.filter(
            management__device_id=device_id,
            source_push_seq__in=[request["push_seq"] for request in requests_],
            acknowledged_at__isnull=False,
        ).values_list("source_push_seq", "stream")
    )
    return [scopes[selections[request["push_seq"]]] for request in requests_ if request["push_seq"] in selections]


class _CallbackEvidenceAdapter(ReceiptAdapter):
    """Serve coherent empty execution evidence and one real policy observation."""

    def __init__(self, adapter_device_id, *, evidence_error=None, unknown_attempt_id=None):
        super().__init__()
        self.adapter_device_id = adapter_device_id
        self.evidence_error = evidence_error
        self.unknown_attempt_id = unknown_attempt_id
        self.evidence_requests = []
        self.policy_reads = []
        self.apply_requests = []
        self.feed_reads = 0
        self.on_evidence = None
        self.on_cursor_feed = None

    def _handle(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/api/v1/jobs"):
            self.feed_reads += 1
            if self.feed_reads == 3 and self.on_cursor_feed is not None:
                self.on_cursor_feed()
            if self.evidence_error is not None:
                raise self.evidence_error
            response = make_response(200, [])
            response.headers["X-Store-Incarnation"] = "callback-evidence-store"
            return response
        if method == "GET" and url.endswith(f"/devices/{self.adapter_device_id}/generations"):
            return make_response(200, [])
        if method == "POST" and url.endswith("/deployment-evidence"):
            if self.on_evidence is not None:
                action, self.on_evidence = self.on_evidence, None
                action()
            self.evidence_requests.append(kwargs["json"])
            return make_response(
                200,
                {
                    "device_id": self.adapter_device_id,
                    "head": None,
                    "blocked": False,
                    "write_work_pending": False,
                    "held_jobs": [],
                    "pending_generations": 0,
                    "attempts": [],
                    "unknown_apply_attempt_ids": []
                    if self.unknown_attempt_id is None
                    else [str(self.unknown_attempt_id)],
                },
            )
        if method == "GET" and url.endswith("/route-policy"):
            self.policy_reads.append(url)
            return make_response(200, {"prefix_lists": [{"name": "CALLBACK-PREFIX", "afi": "ipv4", "entries": []}]})
        if method == "POST" and url.endswith("/actions/apply"):
            self.apply_requests.append(kwargs["json"])
            raise AssertionError("Unexpected manual Apply replay")
        return super()._handle(method, url, **kwargs)


class TestRunDeviceReconcile(APITestCase):
    """run_device_reconcile is the rqworker entrypoint (off-request)."""

    def _managed_policy_device(self, name, adapter_device_id):
        device = _make_device(name)
        instance = NSOInstance.objects.create(name=name, adapter_instance_id=name)
        with without_commit_drain():
            management = NSODeviceManagement.objects.create(
                device=device,
                nso_instance=instance,
                nso_device_name=name,
                adapter_device_id=adapter_device_id,
                manage_routing=True,
                manage_route_policy=True,
            )
        return device, management

    def test_route_policy_reconcile_does_not_prefetch_deployment_evidence(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.reconcile import run_device_reconcile

        device, management = self._managed_policy_device("rec-evidence-once", 23)
        adapter = _CallbackEvidenceAdapter(management.adapter_device_id)
        config, session = adapter.patches()
        with config, session:
            result = run_device_reconcile(device.pk)

        self.assertEqual(result, {"device_id": device.pk, "interface_states": 0})
        self.assertEqual(adapter.evidence_requests, [{"apply_attempt_ids": []}])
        self.assertEqual(len(adapter.policy_reads), 1)
        self.assertTrue(
            NSORoutePolicyState.objects.filter(management=management, object_name="CALLBACK-PREFIX").exists()
        )

    def test_route_policy_evidence_programming_error_stops_reconcile(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.reconcile import run_device_reconcile

        device, management = self._managed_policy_device("rec-evidence-error", 23)
        adapter = _CallbackEvidenceAdapter(
            management.adapter_device_id, evidence_error=RuntimeError("evidence parser failed")
        )
        config, session = adapter.patches()
        with config, session, self.assertRaisesRegex(RuntimeError, "evidence parser failed"):
            run_device_reconcile(device.pk)

        self.assertEqual(adapter.policy_reads, [])
        self.assertFalse(NSORoutePolicyState.objects.filter(management=management).exists())

    def test_mapping_change_during_cursor_walk_does_not_reuse_old_evidence(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        _device, management = self._managed_policy_device("rec-evidence-remap", 23)
        adapter = _CallbackEvidenceAdapter(management.adapter_device_id)
        adapter.on_cursor_feed = lambda: NSODeviceManagement.objects.filter(pk=management.pk).update(
            adapter_source_epoch=(management.adapter_source_epoch or 0) + 1
        )
        config, session = adapter.patches()
        with config, session:
            result = consume_static_route_settlements(management)

        self.assertEqual(adapter.feed_reads, 3)
        self.assertIsNone(result.deployment_evidence)
        self.assertIsNone(result.mapping_epoch)

    def _sent_selection(self, management):
        from django.utils import timezone

        from netbox_nso_plugin.durable_capture import mapping_epoch
        from netbox_nso_plugin.models import NSOAcceptedSelection

        return NSOAcceptedSelection.objects.create(
            management=management,
            mapping_epoch=mapping_epoch(management),
            stream="route_policy",
            source_push_seq=1,
            plugin_revision=1,
            request_digest="a" * 64,
            publication_state="sent",
            acknowledged_at=timezone.now(),
        )

    def test_sweeper_fetches_deployment_evidence_once(self):
        from netbox_nso_plugin.settlement import sweep_static_route_settlements

        _device, management = self._managed_policy_device("rec-evidence-sweep", 23)
        self._sent_selection(management)
        adapter = _CallbackEvidenceAdapter(management.adapter_device_id)
        config, session = adapter.patches()
        with config, session:
            result = sweep_static_route_settlements()

        self.assertEqual(result, (1, 0))
        self.assertEqual(adapter.evidence_requests, [{"apply_attempt_ids": []}])

    def test_unmanage_during_evidence_fetch_skips_callback_reads(self):
        from netbox_nso_plugin.reconcile import run_device_reconcile

        device, management = self._managed_policy_device("rec-evidence-unmanage", 23)
        adapter = _CallbackEvidenceAdapter(management.adapter_device_id)
        adapter.on_evidence = lambda: NSODeviceManagement.objects.filter(pk=management.pk).delete()
        config, session = adapter.patches()
        with config, session:
            result = run_device_reconcile(device.pk)

        self.assertEqual(result, {"device_id": device.pk, "interface_states": 0})
        self.assertEqual(adapter.policy_reads, [])
        self.assertFalse(NSODeviceManagement.objects.filter(pk=management.pk).exists())

    def test_unmanage_during_evidence_fetch_does_not_abort_sweeper(self):
        from netbox_nso_plugin.settlement import sweep_static_route_settlements

        _device, management = self._managed_policy_device("rec-evidence-sweep-unmanage", 23)
        self._sent_selection(management)
        adapter = _CallbackEvidenceAdapter(management.adapter_device_id)
        adapter.on_evidence = lambda: NSODeviceManagement.objects.filter(pk=management.pk).delete()
        config, session = adapter.patches()
        with config, session:
            result = sweep_static_route_settlements()

        self.assertEqual(result, (0, 1))
        self.assertFalse(NSODeviceManagement.objects.filter(pk=management.pk).exists())

    def test_unrequested_evidence_uuid_does_not_stop_reconcile(self):
        from netbox_nso_plugin.models import NSOApplyAttempt, NSORoutePolicyState
        from netbox_nso_plugin.reconcile import run_device_reconcile

        device, management = self._managed_policy_device("rec-evidence-extra", 24)
        requested_id, extra_id = uuid4(), uuid4()
        attempt = NSOApplyAttempt.objects.create(
            id=requested_id,
            adapter_apply_attempt_id=requested_id,
            management=management,
            adapter_device_id=management.adapter_device_id,
            selected={"route_policy": 1},
            scope_revisions={"route_policy": 1},
        )
        adapter = _CallbackEvidenceAdapter(management.adapter_device_id, unknown_attempt_id=extra_id)
        config, session = adapter.patches()
        with config, session, self.assertLogs("netbox_nso_plugin.reconcile", level="WARNING") as logs:
            result = run_device_reconcile(device.pk)

        self.assertEqual(result, {"device_id": device.pk, "interface_states": 0})
        self.assertTrue(adapter.evidence_requests)
        self.assertTrue(all(row == {"apply_attempt_ids": [str(requested_id)]} for row in adapter.evidence_requests))
        self.assertEqual(adapter.apply_requests, [])
        self.assertTrue(any("settle skipped" in line for line in logs.output))
        self.assertEqual(len(adapter.policy_reads), 1)
        self.assertTrue(
            NSORoutePolicyState.objects.filter(management=management, object_name="CALLBACK-PREFIX").exists()
        )
        attempt.refresh_from_db()
        self.assertIsNone(attempt.response)

    def test_missing_device_is_skipped(self):
        from netbox_nso_plugin.reconcile import run_device_reconcile

        result = run_device_reconcile(9_999_999)
        self.assertEqual(result.get("skipped"), "device_gone")

    def test_success_returns_summary(self):
        from netbox_nso_plugin import reconcile

        device = _make_device("rec-ok")
        fake_ctx = {"interface_states": {("a", "description"): object(), ("b", "enabled"): object()}}
        with patch.object(reconcile, "reconcile_device", return_value=fake_ctx) as m:
            result = reconcile.run_device_reconcile(device.pk)
        m.assert_called_once()
        self.assertEqual(result, {"device_id": device.pk, "interface_states": 2})

    def test_adapter_error_is_swallowed(self):
        from netbox_nso_plugin import reconcile
        from netbox_nso_plugin.adapter_client import AdapterError

        device = _make_device("rec-err")
        with patch.object(reconcile, "reconcile_device", side_effect=AdapterError("down", code="nso_unreachable")):
            result = reconcile.run_device_reconcile(device.pk)
        self.assertIn("error", result)
        self.assertEqual(result["device_id"], device.pk)

    def test_deployment_quiescence_is_returned_without_failing_the_worker(self):
        from netbox_nso_plugin import reconcile
        from netbox_nso_plugin.deployment import quiesce, resume

        device = _make_device("rec-quiesced")
        activated = quiesce()
        try:
            self.assertTrue(activated)
            result = reconcile.run_device_reconcile(device.pk)
        finally:
            if activated:
                resume()

        self.assertIn("error", result)
        self.assertEqual(result["device_id"], device.pk)
        self.assertIn("quiesced", result["error"])

    def test_an_unmanaged_device_is_not_reported_as_a_settle_failure(self):
        """Step 4 has nothing to settle for a device with no management row, and says nothing.

        ``device.nso_management`` is a reverse one-to-one: reading it on an unmanaged device
        raises, so the guard below it never runs and the step's own handler logs a warning
        that names a failure which did not happen.
        """
        from netbox_nso_plugin import reconcile

        device = _make_device("rec-unmanaged")
        with (
            patch.object(reconcile, "reconcile_device", return_value={}),
            self.assertNoLogs("netbox_nso_plugin.reconcile", level="WARNING"),
        ):
            reconcile.run_device_reconcile(device.pk)


class TestSyncCompleteEndpoint(APITestCase):
    """POST /api/plugins/nso/sync-complete/ — the adapter's sync-done callback."""

    def _url(self):
        return "/api/plugins/nso/sync-complete/"

    def test_missing_ids_returns_400(self):
        response = self.client.post(self._url(), {}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_object_body_returns_400_without_enqueue(self):
        management_count = NSODeviceManagement.objects.count()
        for body in ([], ["x"]):
            with (
                self.subTest(body=body),
                patch("netbox_nso_plugin.reconcile.enqueue_device_reconcile") as enqueue,
            ):
                response = self.client.post(self._url(), body, format="json", **self.header)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data["detail"], "request body must be a JSON object")
                self.assertEqual(NSODeviceManagement.objects.count(), management_count)
                enqueue.assert_not_called()

    def test_netbox_device_id_enqueues_and_returns_202(self):
        with patch("netbox_nso_plugin.reconcile.enqueue_device_reconcile") as m:
            response = self.client.post(self._url(), {"netbox_device_id": 42}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        m.assert_called_once_with(42)
        self.assertEqual(response.data["netbox_device_id"], 42)

    def test_unknown_adapter_device_id_returns_404(self):
        response = self.client.post(self._url(), {"adapter_device_id": 999_999}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_adapter_device_id_resolves_to_device_and_enqueues(self):
        from netbox_nso_plugin.delivery import delivery_keys
        from netbox_nso_plugin.intent_state import content_mutation

        device = _make_device("rec-map")
        inst = NSOInstance.objects.create(name="rec-nso", adapter_instance_id="rec-nso")
        with content_mutation({(device.pk, scope) for scope in delivery_keys()}):
            NSODeviceManagement.objects.bulk_create(
                [
                    NSODeviceManagement(
                        device=device,
                        nso_instance=inst,
                        nso_device_name="rec-map",
                        adapter_device_id=4242,
                        custom_field_data={},
                    )
                ]
            )
        with patch("netbox_nso_plugin.reconcile.enqueue_device_reconcile") as m:
            response = self.client.post(self._url(), {"adapter_device_id": 4242}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        m.assert_called_once_with(device.pk)


class TestProvisionCompletePermission(APITestCase):
    """Only the adapter service principal can record provision evidence."""

    user_permissions = ()

    def test_authenticated_principal_without_tombstone_change_permission_is_denied(self):
        from netbox_nso_plugin.models import NSOProvisionTombstone

        device = _make_device("prov-callback-denied")
        instance = NSOInstance.objects.create(
            name="prov-callback-denied-nso",
            adapter_instance_id="prov-callback-denied-nso",
        )
        tombstone = NSOProvisionTombstone.objects.create(
            netbox_device_id=device.pk,
            nso_instance=instance.adapter_instance_id,
            nso_device_name="prov-callback-denied",
            canonical_request={},
        )
        payload = {
            "provision_attempt_id": str(tombstone.provision_attempt_id),
            "status": "failed",
            "error": {"code": "provision_failed"},
        }

        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            response = self.client.post(
                "/api/plugins/nso/provision-complete/",
                payload,
                format="json",
                **self.header,
            )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "open")
        self.assertIsNone(tombstone.terminal_evidence)
        enqueue.assert_not_called()

        self.add_permissions("netbox_nso_plugin.change_nsoprovisiontombstone")
        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            response = self.client.post(
                "/api/plugins/nso/provision-complete/",
                payload,
                format="json",
                **self.header,
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "terminal")
        enqueue.assert_called_once()


class TestProvisionCompleteEndpoint(APITestCase):
    """POST /api/plugins/nso/provision-complete/ — the adapter's provision-done callback."""

    user_permissions = ("netbox_nso_plugin.change_nsoprovisiontombstone",)

    def _url(self):
        return "/api/plugins/nso/provision-complete/"

    def _tombstone(self, job_id="55"):
        from netbox_nso_plugin.models import NSOProvisionTombstone

        device = _make_device(f"prov-{job_id}")
        inst = NSOInstance.objects.create(name=f"prov-nso-{job_id}", adapter_instance_id=f"prov-nso-{job_id}")
        return NSOProvisionTombstone.objects.create(
            netbox_device_id=device.pk,
            nso_instance=inst.adapter_instance_id,
            nso_device_name=f"prov-{job_id}",
            # A deliberately divergent id: the callback must correlate by the row key, not this.
            canonical_request={"provision_attempt_id": str(uuid4())},
            adapter_job_id=job_id,
        )

    def test_missing_attempt_id_returns_400(self):
        response = self.client.post(self._url(), {}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_object_body_returns_400_without_enqueue(self):
        from netbox_nso_plugin.models import NSOProvisionTombstone

        tombstone_count = NSOProvisionTombstone.objects.count()
        for body in ([], ["x"]):
            with (
                self.subTest(body=body),
                patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue,
            ):
                response = self.client.post(self._url(), body, format="json", **self.header)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data["detail"], "invalid provision completion evidence")
                self.assertEqual(NSOProvisionTombstone.objects.count(), tombstone_count)
                enqueue.assert_not_called()

    def test_unexpected_terminal_write_error_is_not_reported_as_invalid_evidence(self):
        payload = {"provision_attempt_id": str(uuid4()), "status": "failed"}

        with (
            patch(
                "netbox_nso_plugin.provision_lifecycle.mark_provision_terminal",
                side_effect=ValueError("unexpected terminal write failure"),
            ),
            self.assertRaisesRegex(ValueError, "unexpected terminal write failure"),
        ):
            self.client.post(self._url(), payload, format="json", **self.header)

    def test_known_attempt_only_marks_terminal_and_enqueues_the_sweep(self):
        """The callback records evidence but never advances or offboards a device."""
        tombstone = self._tombstone("77")
        payload = {
            "provision_attempt_id": str(tombstone.provision_attempt_id),
            "status": "succeeded",
            "job_id": 77,
            "result": {"ok": True, "device_id": 7077},
        }
        with (
            patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue,
            patch("netbox_nso_plugin.onboarding.advance_provisioning") as advance,
            patch("netbox_nso_plugin.adapter_client.delete_device") as offboard,
        ):
            response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "terminal")
        self.assertEqual(tombstone.terminal_status, "succeeded")
        self.assertEqual(tombstone.terminal_evidence, payload)
        enqueue.assert_called_once_with(tombstone.provision_attempt_id)
        advance.assert_not_called()
        offboard.assert_not_called()
        self.assertTrue(response.data["queued"])

    def test_malformed_success_evidence_does_not_win_the_terminal_cas(self):
        tombstone = self._tombstone("78")

        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            response = self.client.post(
                self._url(),
                {
                    "provision_attempt_id": str(tombstone.provision_attempt_id),
                    "status": "succeeded",
                    "result": "not-an-object",
                },
                format="json",
                **self.header,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "open")
        self.assertIsNone(tombstone.terminal_evidence)
        enqueue.assert_not_called()

    def test_conflicting_job_receipt_returns_400(self):
        tombstone = self._tombstone("79")

        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            response = self.client.post(
                self._url(),
                {
                    "provision_attempt_id": str(tombstone.provision_attempt_id),
                    "status": "failed",
                    "job_id": 80,
                    "error": {"code": "provision_failed"},
                },
                format="json",
                **self.header,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "open")
        enqueue.assert_not_called()

    def test_unknown_attempt_is_acked_without_enqueue(self):
        """An untracked attempt is best-effort callback evidence, not completion authority."""
        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            response = self.client.post(
                self._url(),
                {"provision_attempt_id": str(uuid4()), "status": "failed"},
                format="json",
                **self.header,
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertFalse(response.data["queued"])
        enqueue.assert_not_called()

    def test_the_callback_correlates_by_the_tombstone_key_not_the_request_body(self):
        tombstone = self._tombstone("81")

        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            by_body = self.client.post(
                self._url(),
                {
                    "provision_attempt_id": tombstone.canonical_request["provision_attempt_id"],
                    "status": "failed",
                },
                format="json",
                **self.header,
            )

        self.assertEqual(by_body.status_code, status.HTTP_202_ACCEPTED)
        self.assertFalse(by_body.data["queued"])
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "open")
        enqueue.assert_not_called()

        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            by_key = self.client.post(
                self._url(),
                {"provision_attempt_id": str(tombstone.provision_attempt_id), "status": "failed"},
                format="json",
                **self.header,
            )

        self.assertEqual(by_key.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(by_key.data["queued"])
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "terminal")
        enqueue.assert_called_once_with(tombstone.provision_attempt_id)


class TestStaticRouteApplySettle(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Static routes join the preparation and attempt-addressed settlement flow. Found
    live on rg03: a successfully applied static route stayed 'pending apply' forever —
    _prepare_apply never marked it deploying and never force-pushed its snapshot."""

    def _setup(self, status_="deploying", tag="sr-settle"):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        from ._static_route_case import _assign_without_push

        device = _make_device(tag)
        inst, _ = NSOInstance.objects.get_or_create(name=f"{tag}-inst", defaults={"adapter_instance_id": f"{tag}-inst"})
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name=tag, adapter_device_id=89
        )
        route = StaticRoute.objects.create(prefix="198.18.99.0/24", next_hop="198.18.0.1", name="sr-settle", metric=1)
        # Assign through the suppressed writer footprint. The route is a qualifying native
        # anchor, but the greenfield signal does not create the overlay under test.
        _assign_without_push(route, device)
        row = acquire_overlay(
            NSOStaticRouteState,
            management=mgmt,
            static_route=route,
            nso_prefix="198.18.99.0/24",
            nso_next_hop="198.18.0.1",
            status=status_,
        )
        return mgmt, row

    def test_prepare_apply_marks_accepted_static_route_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        # A stored count, not a bare mock: the promotion gate reads the count.
        with _patch_apply_pushes({"static_route": {"device_id": 89, "count": 1, "routes": []}}) as push:
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        # And the owned snapshot is re-sent under its own forced claim, so a stale adapter
        # intent still applies.
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("static_route"), 1)

    def test_a_push_answer_with_no_stored_count_does_not_promote(self):
        """An acknowledgement is a stored count, not a truthy answer.

        The fleet re-sync already reads it that way. Promoting on anything truthy puts the
        row in 'deploying' against intent the adapter may not be holding, and no apply
        result can then name it.
        """
        from netbox_nso_plugin.views import _prepare_apply

        for tag, answer in (("sr-nondict", "stored"), ("sr-nocount", {"device_id": 89, "routes": []})):
            with self.subTest(answer=answer):
                mgmt, row = self._setup(status_="accepted", tag=tag)
                with _patch_apply_pushes({"static_route": answer}):
                    _prepare_apply(mgmt)
                row.refresh_from_db()
                self.assertEqual(row.status, "accepted")


class TestL2SapApplySettle(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """L2 SAPs join the preparation and attempt-addressed settlement flow.
    Found by the item-12 real-apply scoping on ra1 (Nokia): an accepted SAP would apply
    adapter-side but never read 'deploying' nor flip to apply_failed on a failed scope."""

    def _setup(self, status_="deploying"):
        from netbox_nso_plugin.models import NSOL2SapState

        device = _make_device("sap-settle")
        inst, _ = NSOInstance.objects.get_or_create(
            name="sap-settle-inst", defaults={"adapter_instance_id": "sap-settle-inst"}
        )
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="sap-settle", adapter_device_id=90
        )
        row = acquire_overlay(
            NSOL2SapState,
            management=mgmt,
            service_name="vpls-701",
            service_type="vpls",
            sap_id="1/1/c31/3:702",
            port="1/1/c31/3",
            outer_tag=702,
            status=status_,
        )
        return mgmt, row

    def test_prepare_apply_marks_accepted_l2_sap_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        # And the owned snapshot is re-sent under its own forced claim, so a stale adapter
        # intent still applies.
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("l2_sap"), 1)


class TestRoutePolicyApplySettle(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Route-policy joins the deploying→settle flow: Apply marks accepted→deploying,
    a failed route_policy scope flips the stuck deploying row → apply_failed."""

    def _setup(self, status_="deploying"):
        from netbox_nso_plugin.models import NSORoutePolicyState

        device = _make_device("rp-settle")
        inst, _ = NSOInstance.objects.get_or_create(
            name="rp-settle-inst", defaults={"adapter_instance_id": "rp-settle-inst"}
        )
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="rp-settle", adapter_device_id=88
        )
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import CommunityList

        policy = CommunityList.objects.create(name="CL-X")
        row = acquire_overlay(
            NSORoutePolicyState,
            management=mgmt,
            family="community_list",
            object_name="CL-X",
            content_type=ContentType.objects.get_for_model(CommunityList),
            object_id=policy.pk,
            status=status_,
        )
        return mgmt, row

    def test_prepare_apply_marks_accepted_route_policy_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        with _patch_apply_pushes():
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_prepare_apply_force_pushes_owned_interface_intent(self):
        """Apply re-sends the owned interface snapshot (status-based) under a forced claim, so
        an owned attribute whose adapter intent went stale is actually re-applied instead of
        silently dropped. Ownership is kept durable by the reconciler's owned-guard."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("interface"), 1)

    def test_prepare_apply_force_pushes_owned_route_policy_intent(self):
        """Apply must re-send owned ROUTE-POLICY intent too (like interface/VLAN). An owned
        route-policy object whose adapter intent went stale/empty otherwise applies 0 items and
        the row sticks in 'deploying' forever — observed on rg03, where an owned as-path had NO
        adapter intent row, so Apply pushed nothing and the row never settled."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("route_policy"), 1)

    def test_prepare_apply_force_pushes_deferred_scopes(self):
        """Apply marks SVI/subinterface/BFD/MTU accepted->deploying, so it must also re-send
        their owned snapshots. These are mirrored as adapter intent (reactive push on accept/edit),
        but that mirror can go stale/empty (a failed push, an out-of-band adapter reset). Without a
        forced claim, Apply marks the row 'deploying' but the acknowledged baseline drops the send →
        0 items applied → the row sticks in 'deploying' forever (route-policy's rg03 failure mode)."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        forced = _forced_scopes(push, mgmt.device_id)
        for scope in ("svi", "subinterface", "bfd", "interface_mtu"):
            self.assertEqual(forced.count(scope), 1, scope)


class TestSnmpApplyForcePush(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """SNMP intent is mirrored reactively on accept, and a failed PUT is swallowed — so a
    device whose accept-time push failed has no adapter intent at all. Apply is the only
    recovery, exactly as for logging hosts, and must re-send the owned SNMP snapshot.

    The refresh must be STORE-ONLY: a plain put_snmp_intent enqueues the shrink-removal job
    (and auto-apply on auto_apply devices), and _prepare_apply runs before trigger_apply —
    whose _trigger 409s while any job is active, so the recovery would kill the Apply it serves.

    It runs the real claim, which is why there is no test transaction: the mode has to reach
    the wire as the query flag, and only the transport is doubled.
    """

    def test_prepare_apply_force_pushes_snmp_snapshot(self):
        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.views import _prepare_apply

        with without_commit_drain(), transaction.atomic():
            device = _make_device("snmp-apply")
            inst, _ = NSOInstance.objects.get_or_create(
                name="snmp-apply-inst", defaults={"adapter_instance_id": "snmp-apply-inst"}
            )
            mgmt = NSODeviceManagement.objects.create(
                device=device, nso_instance=inst, nso_device_name="snmp-apply", adapter_device_id=91
            )
            acquire_overlay(
                NSOSnmpHostState,
                management=mgmt,
                address="198.18.0.40",
                version="v2c",
                notify_type="trap",
                community_hash="abcd1234abcd1234",
                status="accepted",
            )
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            _prepare_apply(mgmt)
        pushes = [request for request in adapter.requests if request["url"].endswith("/snmp-intent")]
        self.assertEqual(len(pushes), 1)
        self.assertIn(f"/devices/{mgmt.adapter_device_id}/", pushes[0]["url"])
        self.assertEqual([host["address"] for host in pushes[0]["body"]["hosts"]], ["198.18.0.40"])
        self.assertEqual(pushes[0]["params"].get("store_only"), "true")


class TestApplyRollbackOnAdapterError(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Apply rollback follows whether the POST definitely failed before enqueue."""

    def _setup(self):
        from netbox_nso_plugin.models import NSORoutePolicyState

        device = _make_device("apply-rb")
        inst, _ = NSOInstance.objects.get_or_create(
            name="apply-rb-inst", defaults={"adapter_instance_id": "apply-rb-inst"}
        )
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="apply-rb", adapter_device_id=99
        )
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import CommunityList

        policy = CommunityList.objects.create(name="CL-RB")
        row = acquire_overlay(
            NSORoutePolicyState,
            management=mgmt,
            family="community_list",
            object_name="CL-RB",
            content_type=ContentType.objects.get_for_model(CommunityList),
            object_id=policy.pk,
            status="accepted",
        )
        return mgmt, row

    def _post_with_error(self, mgmt, error):
        from django.contrib.auth import get_user_model

        admin = get_user_model().objects.create_superuser(username="apply-rb-admin", password="pw", email="a@x.y")  # noqa: S106
        self.client.force_login(admin)
        with _patch_apply_pushes(apply_error=error):
            return self.client.post(f"/plugins/nso/device-management/{mgmt.pk}/actions/apply/")

    def test_an_ambiguous_transport_failure_keeps_deploying_for_reconciliation(self):
        from netbox_nso_plugin.adapter_client import AdapterError

        mgmt, row = self._setup()
        resp = self._post_with_error(mgmt, AdapterError("socket closed after send", code="nso_unreachable"))

        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_an_ambiguous_http_server_error_keeps_deploying_for_reconciliation(self):
        from netbox_nso_plugin.adapter_client import AdapterError

        mgmt, row = self._setup()
        resp = self._post_with_error(
            mgmt,
            AdapterError("gateway failed after forwarding Apply", code="bad_gateway", status_code=502),
        )

        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_a_definite_http_client_rejection_rolls_back_deploying(self):
        from netbox_nso_plugin.adapter_client import AdapterError

        mgmt, row = self._setup()
        resp = self._post_with_error(
            mgmt,
            AdapterError("adapter rejected the request shape", code="validation_error", status_code=422),
        )

        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")


class TestApplyPreviewInterfaceScope(APITestCase):
    """The Apply preview lists OWNED-status interface attributes whose NetBox value differs
    from the device — exactly what the matrix shows as 'pending' and the force-push applies.

    Ownership is status-based (status in OWNED_STATES), NOT accepted_at — a stale accepted_at
    on an unowned status never makes the preview claim a push Apply would not perform.
    """

    def test_preview_includes_owned_interface_pending(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.views import _apply_preview_interface_changes

        device = _make_device("ifprev")
        iface = Interface.objects.create(device=device, name="ae2.0", type="virtual", description="UPLINK")
        # Owned status (accepted) and the device description is empty → value differs →
        # genuinely pending, must show + apply. accepted_at=None proves it's status-driven.
        acquire_overlay(NSOInterfaceState, interface=iface, attribute="description", status="accepted", nso_value="")
        changes = _apply_preview_interface_changes(device.pk)
        self.assertEqual([(c["interface"], c["attribute"]) for c in changes], [("ae2.0", "description")])
        self.assertEqual(changes[0]["netbox"], "UPLINK")

    def test_preview_excludes_unowned_interface_despite_stale_accepted_at(self):
        from dcim.models import Interface
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.views import _apply_preview_interface_changes

        device = _make_device("ifprev2")
        iface = Interface.objects.create(device=device, name="ae2.0", type="virtual", description="UPLINK")
        # The device-27 ae2.0 case: an 'imported' (un-owned) attribute carrying a STALE
        # accepted_at, value differs — Apply won't push it, so it must not be previewed.
        NSOInterfaceState.objects.create(
            interface=iface,
            attribute="description",
            status="imported",
            nso_value="",
            accepted_at=timezone.now(),
        )
        self.assertEqual(_apply_preview_interface_changes(device.pk), [])

    def test_preview_excludes_owned_interface_in_sync(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.views import _apply_preview_interface_changes

        device = _make_device("ifprev3")
        iface = Interface.objects.create(device=device, name="ae3.0", type="virtual", description="MATCH")
        # Owned (in_sync) and the device already matches NetBox → not pending → not previewed.
        acquire_overlay(
            NSOInterfaceState, interface=iface, attribute="description", status="in_sync", nso_value="MATCH"
        )
        self.assertEqual(_apply_preview_interface_changes(device.pk), [])


class TestSafeReconcile(APITestCase):
    """A faulty reconciler marks its scope's rows 'error' and never crashes the worker."""

    def _setup(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        device = _make_device("safe")
        inst, _ = NSOInstance.objects.get_or_create(name="safe-inst", defaults={"adapter_instance_id": "safe-inst"})
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="safe", adapter_device_id=88
        )
        vlan = VLAN.objects.create(group=_device_vlan_group(device), vid=10, name="V10")
        imported = NSOVLANState.objects.create(management=mgmt, vlan=vlan, device_name="V10", status="imported")
        vlan2 = VLAN.objects.create(group=_device_vlan_group(device), vid=20, name="V20")
        owned = acquire_overlay(NSOVLANState, management=mgmt, vlan=vlan2, device_name="V20", status="accepted")
        return mgmt, imported, owned

    def test_failure_marks_unowned_error_preserves_owned(self):
        from netbox_nso_plugin.reconcile import _safe_reconcile

        mgmt, imported, owned = self._setup()
        ctx = {"vlan_states": []}

        def boom(*_a):
            raise RuntimeError("malformed payload")

        from netbox_nso_plugin.reconcile import ReconcileScopeError

        with self.assertRaises(ReconcileScopeError):
            _safe_reconcile(ctx, "vlan_states", mgmt, ("NSOVLANState",), boom, object())
        self.assertEqual(ctx["vlan_states"], [])
        imported.refresh_from_db()
        owned.refresh_from_db()
        self.assertEqual(imported.status, "imported")  # marking happens only after the gate rolls back
        self.assertEqual(owned.status, "accepted")  # owned ownership preserved

    def _assert_interface_fault_isolated(self, model, values, owned_values, fault_target, family, context_key):
        from dcim.models import Interface

        from netbox_nso_plugin.reconcile import _LeaseOutcome, reconcile_device

        from .test_gated_reconcile import _rs

        mgmt, _imported, _owned = self._setup()
        mgmt.manage_interfaces = True
        mgmt.manage_description = True
        mgmt.manage_enabled = True
        mgmt.save(update_fields=("manage_interfaces", "manage_description", "manage_enabled"))
        iface = Interface.objects.create(device=mgmt.device, name="Ethernet1", type="1000base-t")
        imported = model.objects.create(interface=iface, status="imported", **values)
        owned = acquire_overlay(model, interface=iface, status="accepted", **owned_values)
        other = _make_device("scope-other")
        other_iface = Interface.objects.create(device=other, name="Ethernet1", type="1000base-t")
        unrelated = model.objects.create(interface=other_iface, status="imported", **values)
        empty = {"interfaces": [], "read_state": _rs()}

        def fail_reconcile(*_args):
            raise RuntimeError("interface read failed")

        with (
            # The shared Redis lease can defer or fail before the injected fault runs.
            patch("netbox_nso_plugin.reconcile._acquire_reconcile_lease", return_value=_LeaseOutcome()),
            patch("netbox_nso_plugin.adapter_client.get_interfaces_doc", return_value=empty),
            patch("netbox_nso_plugin.adapter_client.get_state", return_value={}),
            patch("netbox_nso_plugin.adapter_client.get_svi", return_value={"svis": [], "read_state": _rs()}),
            patch("netbox_nso_plugin.adapter_client.get_subinterface", return_value=empty),
            patch("netbox_nso_plugin.adapter_client.get_interface_mtu", return_value=empty),
            patch("netbox_nso_plugin.adapter_client.get_interface_ips", return_value=empty),
            patch("netbox_nso_plugin.adapter_client.get_lag_config", return_value={"bundles": [], "read_state": _rs()}),
            patch(
                "netbox_nso_plugin.adapter_client.get_vlan_database", return_value={"vlans": [], "read_state": _rs()}
            ),
            patch("netbox_nso_plugin.adapter_client.get_switchport", return_value=empty),
            patch(f"netbox_nso_plugin.template_content.{fault_target}", new=fail_reconcile),
        ):
            ctx = reconcile_device(mgmt.device, mgmt)

        imported.refresh_from_db()
        owned.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(imported.status, "error")
        self.assertEqual(owned.status, "accepted")
        self.assertEqual(unrelated.status, "imported")
        self.assertFalse(ctx[context_key])
        self.assertEqual(ctx["_gate"][family], "skipped_unavailable")
        self.assertEqual(ctx["_gate"]["lag_config"], "ran")

    def test_interface_ip_fault_marks_only_device_unowned_rows_and_continues(self):
        from netbox_nso_plugin.models import NSOInterfaceIPState

        self._assert_interface_fault_isolated(
            NSOInterfaceIPState,
            {"address": "198.18.0.1/24"},
            {"address": "198.18.0.2/24"},
            "_reconcile_interface_ips",
            "interface_ip",
            "interface_ips",
        )

    def test_interface_attribute_fault_marks_only_device_unowned_rows_and_continues(self):
        from netbox_nso_plugin.models import NSOInterfaceState

        self._assert_interface_fault_isolated(
            NSOInterfaceState,
            {"attribute": "description", "nso_value": "observed"},
            {"attribute": "enabled", "nso_value": "true"},
            "_upsert_interface_states",
            "interface_attributes",
            "interface_states",
        )

    def test_adapter_error_propagates(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.reconcile import _safe_reconcile

        mgmt, _imported, _owned = self._setup()

        def down(*_a):
            raise AdapterError("nso down", code="nso_unreachable")

        with self.assertRaises(AdapterError):
            _safe_reconcile({}, "vlan_states", mgmt, ("NSOVLANState",), down, object())

    def test_success_stores_result(self):
        from netbox_nso_plugin.reconcile import _safe_reconcile

        mgmt, _imported, _owned = self._setup()
        ctx = {"vlan_states": []}
        _safe_reconcile(ctx, "vlan_states", mgmt, ("NSOVLANState",), lambda *_a: ["ok"], object())
        self.assertEqual(ctx["vlan_states"], ["ok"])


class TestEnqueueDeviceReconcileWiring(TestCase):
    """READSEM 1334: `enqueue_device_reconcile` funnels every public producer through the
    queued-carrier arbiter. Real RQ Queue/Job on the configured Redis, isolated throwaway queue.
    (The former deterministic-id + orphan-reclaim machinery is retired — unique carrier ids mean a
    dead carrier is just 'not queued', so the next producer enqueues a fresh one. The arbiter's
    internal invariants live in test_read_gate.TestQueuedCarrierArbiter; this class covers the
    public wrapper + django_rq wiring end-to-end.)
    """

    _DEV = 987654  # won't collide with any real nso-reconcile-<id> job hash
    _QUEUE = f"nso-test-reconcile-wiring-{os.environ.get('NETBOX_NSO_REDIS_KEY_NAMESPACE', 'local')}"

    def _queue(self):
        import django_rq
        from rq import Queue

        conn = django_rq.get_queue("default").connection
        return Queue(self._QUEUE, connection=conn)

    def setUp(self):
        super().setUp()
        self._detached_jobs = []
        self._clear()

    def tearDown(self):
        self._clear()
        super().tearDown()

    def _clear(self):
        from netbox_nso_plugin.read_gate import carrier_key, marker_key

        q = self._queue()
        q.delete(delete_jobs=True)
        for job in self._detached_jobs:
            job.delete()
        self._detached_jobs.clear()
        q.connection.delete(carrier_key(self._DEV))
        q.connection.delete(marker_key(self._DEV))

    def _carriers(self, q):
        return [jid for jid in q.get_job_ids() if f"-{self._DEV}-carrier-" in jid]

    def test_enqueues_one_carrier(self):
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            job = enqueue_device_reconcile(self._DEV)
        self.assertEqual(self._carriers(q), [job.id])

    def test_genuinely_queued_carrier_is_not_duplicated(self):
        """A truly-queued carrier absorbs a second edge — no pile-up (it snapshots post-refresh)."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            second = enqueue_device_reconcile(self._DEV)  # genuinely queued → suppress
        self.assertEqual(first.id, second.id)
        self.assertEqual(self._carriers(q), [first.id])

    def test_started_carrier_gets_a_trailing_edge(self):
        """A carrier a worker has popped (off the queue list) is NOT suppressible — the next edge
        gets a distinct trailing carrier (the former S5a unique-notify trailing edge, now unified)."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            self._detached_jobs.append(first)
            q.remove(first.id)  # worker popped it; pointer still references it
            second = enqueue_device_reconcile(self._DEV)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self._carriers(q), [second.id])

    def test_completed_carrier_gets_a_fresh_edge(self):
        """A notify after a carrier finished (terminal hash, off the queue) → a fresh carrier."""
        from rq.job import Job as RqJob

        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            self._detached_jobs.append(first)
            q.remove(first.id)
            RqJob.fetch(first.id, connection=q.connection).set_status("finished")
            second = enqueue_device_reconcile(self._DEV)
        self.assertIsNotNone(second)
        self.assertIn(second.id, q.get_job_ids())

    def test_clear_deletes_queue_registry_and_detached_job(self):
        """Per-run queues and jobs already popped from them must not accumulate in Redis."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            detached = enqueue_device_reconcile(self._DEV)
        q.remove(detached.id)
        self._detached_jobs = [detached]
        self.assertTrue(q.connection.sismember(q.redis_queues_keys, q.key))
        self.assertTrue(q.connection.exists(detached.key))

        self._clear()

        self.assertFalse(q.connection.sismember(q.redis_queues_keys, q.key))
        self.assertFalse(q.connection.exists(detached.key))


class TestNotifyClassLeaseBudget(TestCase):
    """READSEM 1334 keeps the notify-class lease branch + ``run_device_reconcile`` param (in-flight
    jobs serialized with ``notify_class=True`` across a deploy) even though the enqueue plane no
    longer sets it. The trailing-edge is now the arbiter's job (a started carrier is not suppressible
    → a trailing carrier; see TestEnqueueDeviceReconcileWiring / TestQueuedCarrierArbiter); the
    lease-acquisition semantics for the ``notify`` call class are unchanged.
    """

    _DEV = 987655

    def test_notify_class_lease_uses_zero_retry_budget(self):
        """codex R6-4: notify-class jobs must not burn the 90s retry budget on general RQ
        workers — single attempt, defer-marker, one post-marker attempt (shipped shape)."""
        from netbox_nso_plugin.read_gate import Deferred
        from netbox_nso_plugin.reconcile import _acquire_reconcile_lease

        mgmt = type("M", (), {"pk": self._DEV})()
        with patch("netbox_nso_plugin.read_gate.acquire_for_rq") as acq:
            acq.return_value = Deferred(attempts=2, nonce="n")
            out = _acquire_reconcile_lease(mgmt, self._DEV, "notify")

        acq.assert_called_once()
        self.assertEqual(acq.call_args.kwargs.get("retry_budget_s"), 0.0)
        self.assertEqual(out.state, "deferred")
