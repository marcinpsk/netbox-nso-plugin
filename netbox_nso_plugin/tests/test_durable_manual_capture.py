# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Manual admission restores durable bookkeeping across request contexts."""

import contextvars
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import requests
from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models.signals import post_save, pre_save
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from netbox_nso_plugin import delivery
from netbox_nso_plugin.intent_state import canonical_fragment
from netbox_nso_plugin.models import (
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSODeviceManagement,
    NSOFamilyReadState,
    NSOIntentRevision,
    NSOInterfaceMtuState,
    NSOInterfaceState,
    NSOOverlaySettlementFence,
)
from netbox_nso_plugin.refusal_registry import OVERLAYS

from ._adapter_http import make_response
from ._outbox_case import (
    ReceiptAdapter,
    content_update,
    in_thread,
    make_managed,
    mirror_update,
    own_vlan,
    without_commit_drain,
)
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class ManualAdmissionAdapter(ReceiptAdapter):
    """Keep real preparation calls and control only the remote admission answer."""

    def __init__(self, answer, *, before_apply=None):
        super().__init__()
        self.answer = answer
        self.before_apply = before_apply
        self.apply_requests = []
        self.rejected = False

    def _handle(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/actions/apply"):
            body = copy.deepcopy(kwargs["json"])
            self.apply_requests.append(body)
            if self.before_apply is not None:
                self.before_apply(body)
            answer = self.answer(body)
            if isinstance(answer, requests.RequestException):
                raise answer
            status, response = answer
            self.rejected = status == 409
            return make_response(status, response)
        if method == "POST" and url.endswith("/deployment-evidence"):
            body = self.apply_requests[0]
            attempt_id = body["apply_attempt_id"]
            attempts = []
            if self.rejected:
                attempts.append(
                    {
                        "apply_attempt_id": attempt_id,
                        "admission_state": "rejected",
                        "http_status": 409,
                        "response": self.rejection(),
                        "generations": [],
                    }
                )
            return make_response(
                200,
                {
                    "device_id": int(url.split("/devices/")[1].split("/")[0]),
                    "head": None,
                    "blocked": False,
                    "write_work_pending": False,
                    "held_jobs": [],
                    "pending_generations": 0,
                    "attempts": attempts,
                    "unknown_apply_attempt_ids": [] if self.rejected else [attempt_id],
                },
            )
        return super()._handle(method, url, **kwargs)

    @staticmethod
    def rejection():
        return {
            "error": {
                "code": "apply_unexecutable",
                "message": "Selected interface intent cannot execute.",
                "detail": {"streams": {"interface": "no_executable_interface"}},
            }
        }


class TestDurableManualCapture(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_superuser(
            username="durablemanualadmin", password=None, email="manual@test.example"
        )
        self.client.force_login(user)
        self.device, self.management = make_managed("durablemanual", 1767)
        with without_commit_drain(), transaction.atomic():
            self.management.manage_description = True
            self.management.save(update_fields=["manage_description"])
            interface = Interface.objects.create(
                device=self.device, name="Port-channel17", type="lag", mtu=9000, description="Desired description"
            )
            self.attribute = acquire_overlay(
                NSOInterfaceState,
                interface=interface,
                attribute="description",
                status="accepted",
                nso_value="Prior description",
            )
            self.mtu = acquire_overlay(
                NSOInterfaceMtuState, management=self.management, interface=interface, l2_mtu=9000, status="accepted"
            )
            self.vlan = own_vlan(self.management, 1767, "durablemanual")
        self.prior_attempt = NSOApplyAttempt.objects.create(
            id=uuid4(),
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            selected={},
            scope_revisions={},
            http_status=409,
            response=ManualAdmissionAdapter.rejection(),
        )
        self.rows = (("interface", self.attribute), ("interface_mtu", self.mtu), ("vlan", self.vlan))
        self.prior = {}
        for index, (scope, row) in enumerate(self.rows):
            error = (
                {"code": "apply_failed", "message": "Prior description Apply failed.", "detail": {"attempt": 1}}
                if row is self.attribute
                else f"Prior {scope} Apply failed."
            )
            row = content_update(row, accepted_at=timezone.now() - timedelta(days=4))
            row = mirror_update(
                row,
                status="apply_failed",
                last_apply_at=timezone.now() - timedelta(days=2, minutes=index),
                last_apply_error=error,
                apply_attempt_id=self.prior_attempt.pk,
            )
            self.prior[(row._meta.label_lower, row.pk)] = {
                "status": row.status,
                "last_apply_at": row.last_apply_at,
                "last_apply_error": copy.deepcopy(row.last_apply_error),
                "apply_attempt_id": row.apply_attempt_id,
                "accepted_at": row.accepted_at,
                "content_digest": delivery.canonical_fingerprint(canonical_fragment(row)),
            }
        self.read_watermarks = {}
        families = {family for _, row in self.rows for family in OVERLAYS[row._meta.label_lower].read_families}
        for index, family in enumerate(sorted(families), start=1):
            NSOFamilyReadState.objects.create(
                management=self.management,
                family=family,
                applied_attempt_id=index,
                applied_incarnation="manual-read-store",
                applied_source_epoch=3,
                applied_payload_revision=index + 10,
                applied_publication_sequence=index + 20,
                observed_attempt_id=index + 100,
                observed_incarnation="newer-observed-store",
                observed_source_epoch=4,
                observed_payload_revision=index + 110,
            )
            self.read_watermarks[family] = {
                "attempt_id": index,
                "incarnation": "manual-read-store",
                "source_epoch": 3,
                "payload_revision": index + 10,
                "publication_sequence": index + 20,
            }

    def _apply(self, adapter):
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.management.pk, "apply"])
        config, session = adapter.patches()
        with config, session:
            return self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def _assert_prior_bookkeeping(self, row):
        row.refresh_from_db()
        prior = self.prior[(row._meta.label_lower, row.pk)]
        for name in ("status", "last_apply_at", "last_apply_error", "apply_attempt_id", "accepted_at"):
            self.assertEqual(getattr(row, name), prior[name], f"{row._meta.label_lower}.{name}")
        self.assertIs(type(row.last_apply_error), type(prior["last_apply_error"]))

    def test_manual_prepare_persists_all_prior_bookkeeping_before_promotion(self):
        observed = []
        promotion_observed = []

        def inspect_before_promotion(sender, instance, **kwargs):
            if (
                promotion_observed
                or instance.status != "deploying"
                or (instance._meta.label_lower, instance.pk) not in self.prior
            ):
                return

            def read():
                try:
                    snapshots = list(NSOApplyOverlaySnapshot.objects.filter(attempt_id=instance.apply_attempt_id))
                    rows = {(row._meta.label_lower, row.pk): type(row).objects.get(pk=row.pk) for _, row in self.rows}
                    return snapshots, rows
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=1) as executor:
                promotion_observed.append(executor.submit(read).result(timeout=30))

        for _, row in self.rows:
            pre_save.connect(inspect_before_promotion, sender=type(row), weak=False)
            self.addCleanup(pre_save.disconnect, inspect_before_promotion, sender=type(row))

        def inspect_committed(body):
            def read():
                try:
                    attempt = NSOApplyAttempt.objects.get(pk=body["apply_attempt_id"])
                    snapshots = list(NSOApplyOverlaySnapshot.objects.filter(attempt=attempt))
                    fences = list(NSOOverlaySettlementFence.objects.filter(management_id=self.management.pk))
                    revisions = dict(
                        NSOIntentRevision.objects.filter(device_id=self.device.pk).values_list("scope", "revision")
                    )
                    rows = {(row._meta.label_lower, row.pk): type(row).objects.get(pk=row.pk) for _, row in self.rows}
                    return attempt, snapshots, fences, revisions, rows
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=1) as executor:
                observed.append(executor.submit(read).result(timeout=30))

        def no_op(body):
            return 200, {
                "device_id": self.management.adapter_device_id,
                "outcome": "no_op",
                "job_id": None,
                "selected": body["selected"],
                "skipped": dict.fromkeys(body["selected"], "already_applied"),
                "skipped_detail": None,
                "generations": [],
            }

        adapter = ManualAdmissionAdapter(no_op, before_apply=inspect_committed)
        response = self._apply(adapter)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(promotion_observed), 1)
        committed_snapshots, prior_rows = promotion_observed[0]
        self.assertEqual({(item.model_label, item.overlay_pk) for item in committed_snapshots}, set(self.prior))
        for key, row in prior_rows.items():
            for name in ("status", "last_apply_at", "last_apply_error", "apply_attempt_id"):
                self.assertEqual(getattr(row, name), self.prior[key][name])
        self.assertEqual(len(observed), 1)
        attempt, snapshots, fences, revisions, rows = observed[0]
        snapshot_by_owner = {(item.model_label, item.overlay_pk): item for item in snapshots}
        self.assertEqual(set(snapshot_by_owner), set(self.prior))
        fence_by_owner = {(item.model_label, item.overlay_pk): item for item in fences}
        self.assertEqual(attempt.selected, adapter.apply_requests[0]["selected"])
        for scope, row in self.rows:
            key = (row._meta.label_lower, row.pk)
            prior = self.prior[key]
            snapshot = snapshot_by_owner[key]
            current = rows[key]
            fence = fence_by_owner.get(key)
            self.assertEqual(snapshot.prior_status, prior["status"])
            self.assertEqual(snapshot.prior_last_apply_at, prior["last_apply_at"])
            self.assertEqual(snapshot.prior_last_apply_error, prior["last_apply_error"])
            self.assertIs(type(snapshot.prior_last_apply_error), type(prior["last_apply_error"]))
            self.assertEqual(snapshot.prior_apply_attempt_id, self.prior_attempt.pk)
            self.assertEqual(snapshot.accepted_at, prior["accepted_at"])
            self.assertEqual(snapshot.content_digest, prior["content_digest"])
            self.assertEqual(snapshot.scope_revisions[scope], revisions[scope])
            self.assertEqual(snapshot.scope_revisions[scope], attempt.scope_revisions[scope])
            fence_revision = (
                fence.bookkeeping_revision if fence is not None and fence.mapping_epoch == snapshot.mapping_epoch else 0
            )
            self.assertEqual(snapshot.expected_fence_revision, fence_revision)
            self.assertEqual(
                snapshot.observation_watermark,
                {family: self.read_watermarks[family] for family in OVERLAYS[row._meta.label_lower].read_families},
            )
            self.assertEqual(snapshot.expected_status, "deploying")
            self.assertEqual(current.status, snapshot.expected_status)
            self.assertEqual(snapshot.expected_last_apply_at, current.last_apply_at)
            self.assertEqual(snapshot.expected_last_apply_error, current.last_apply_error)
            self.assertEqual(snapshot.expected_apply_attempt_id, attempt.pk)
            self.assertEqual(current.apply_attempt_id, attempt.pk)
            self.assertIsNotNone(snapshot.selection_id)
            self.assertTrue(snapshot.contributors["rows"])
        self.assertTrue(any(request["params"].get("store_only") == "true" for request in adapter.requests))
        self.assertTrue(any("source_revision" in request["body"] for request in adapter.requests))
        for _, row in self.rows:
            self._assert_prior_bookkeeping(row)

    def test_manual_rejection_restores_snapshot_after_restart(self):
        adapter = ManualAdmissionAdapter(lambda _body: requests.ConnectionError("Apply admission answer was lost."))
        response = self._apply(adapter)
        self.assertEqual(response.status_code, 502)
        original_request = copy.deepcopy(adapter.apply_requests[0])
        attempt_id = UUID(original_request["apply_attempt_id"])
        attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        self.assertIsNone(attempt.response)
        del attempt
        adapter.answer = lambda _body: (409, adapter.rejection())

        def recover():
            from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

            management = NSODeviceManagement.objects.get(pk=self.management.pk)
            settle_device_apply_attempts(management, static_route_feed_drained=True)

        config, session = adapter.patches()
        with config, session:
            in_thread(lambda: contextvars.Context().run(recover))

        self.assertEqual(adapter.apply_requests, [original_request, original_request])
        attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        self.assertEqual(attempt.http_status, 409)
        self.assertEqual(attempt.response, adapter.rejection())
        for _, row in self.rows:
            self._assert_prior_bookkeeping(row)

    def test_manual_skipped_stream_restores_only_its_snapshot(self):
        def promoted(body):
            selected = body["selected"]
            skipped = {stream: "already_applied" for stream in selected if stream != "interface_mtu"}
            return 202, {
                "device_id": self.management.adapter_device_id,
                "outcome": "promoted",
                "job_id": 1768,
                "selected": selected,
                "skipped": skipped,
                "skipped_detail": None,
                "generations": [
                    {
                        "generation_id": 17,
                        "seq": 17,
                        "job_id": 1768,
                        "mode": "networked",
                        "source_push_seq": {"interface_mtu": selected["interface_mtu"]},
                        "stream_revisions": {"interface_mtu": selected["interface_mtu"]},
                        "digest": "a" * 64,
                    }
                ],
            }

        adapter = ManualAdmissionAdapter(promoted)
        response = self._apply(adapter)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self._assert_prior_bookkeeping(self.vlan)
        self._assert_prior_bookkeeping(self.attribute)
        self.mtu.refresh_from_db()
        attempt_id = UUID(adapter.apply_requests[0]["apply_attempt_id"])
        self.assertEqual(self.mtu.status, "deploying")
        self.assertEqual(self.mtu.apply_attempt_id, attempt_id)
        self.assertNotEqual(
            self.mtu.last_apply_at, self.prior[(self.mtu._meta.label_lower, self.mtu.pk)]["last_apply_at"]
        )
        self.assertEqual(self.mtu.last_apply_error, "")

    def test_manual_empty_selection_refuses_a_mapping_change_before_promotion(self):
        self.device, self.management = make_managed("manualemptymapping", 1867)
        changed = []

        def change_mapping_after_capture(sender, instance, created, **kwargs):
            if not created or instance.trigger_kind != "manual" or instance.management_id != self.management.pk:
                return

            def commit_mapping():
                def change():
                    from netbox_nso_plugin.intent_state import update_mirror_fields

                    management = NSODeviceManagement.objects.get(pk=self.management.pk)
                    update_mirror_fields(management, adapter_device_id=1868)
                    changed.append(management.adapter_device_id)

                in_thread(lambda: contextvars.Context().run(change))

            transaction.on_commit(commit_mapping)

        post_save.connect(change_mapping_after_capture, sender=NSOApplyAttempt, weak=False)
        self.addCleanup(post_save.disconnect, change_mapping_after_capture, sender=NSOApplyAttempt)

        def no_op(body):
            return 200, {
                "device_id": 1867,
                "outcome": "no_op",
                "job_id": None,
                "selected": body["selected"],
                "skipped": dict.fromkeys(body["selected"], "already_applied"),
                "skipped_detail": None,
                "generations": [],
            }

        adapter = ManualAdmissionAdapter(no_op)
        response = self._apply(adapter)

        self.assertEqual(changed, [1868])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(adapter.apply_requests, [])
        attempt = NSOApplyAttempt.objects.get(management=self.management, trigger_kind="manual")
        self.assertIsNone(attempt.adapter_apply_attempt_id)
        self.assertIsNone(attempt.response)
        self.assertFalse(NSOApplyOverlaySnapshot.objects.filter(attempt=attempt).exists())
