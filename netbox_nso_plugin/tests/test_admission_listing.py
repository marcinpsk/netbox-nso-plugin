# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Reconcile all held admission links before using or persisting them."""

import copy
from uuid import uuid4

from django.core.management.base import CommandError

from netbox_nso_plugin import deployment
from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence
from netbox_nso_plugin.models import NSOAcceptedSelection, NSOApplyAttempt, NSOApplyExecution

from ._adapter_http import make_response
from .test_apply_selector import _promoted
from .test_deployment_probe import DeploymentCase


class TestAdmissionListing(DeploymentCase):
    def prepare(self):
        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        self.adapter.apply_response = lambda selected: (
            202,
            {
                **_promoted(selected),
                "generations": _promoted(selected)["generations"][:1],
            },
        )
        attempt_id = uuid4()
        self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        self.attempt = NSOApplyAttempt.objects.get(pk=attempt_id)

    def assert_admission_links_reconciled(self, kind):
        self.prepare()
        response = copy.deepcopy(self.attempt.response)
        generation = self.terminal()
        self.serve_terminal_evidence(generation, manual=kind == "known")
        original = self.adapter._handle
        changed = None

        def handle(method, url, **kwargs):
            answer = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = answer.json()
                if kind == "known":
                    document["attempts"][0]["projection"]["response"] = changed
                if kind == "replayed":
                    document["unknown_apply_attempt_ids"] = [str(self.attempt.pk)]
                return make_response(200, document)
            return answer

        self.adapter._handle = handle
        self.adapter.apply_response = lambda selected: (202, changed)
        for key, value in (
            ("seq", 5),
            ("digest", "b" * 64),
            ("mode", "detach"),
            ("generation_id", 999),
            ("stream_revisions", {"bgp": 8}),
            ("source_push_seq", {"bgp": 999}),
        ):
            with self.subTest(kind=kind, key=key):
                changed = copy.deepcopy(response)
                changed["generations"][0][key] = value
                NSOApplyAttempt.objects.filter(pk=self.attempt.pk).update(
                    response=changed if kind == "retained" else None,
                    http_status=202 if kind == "retained" else None,
                )
                before_attempts = list(NSOApplyAttempt.objects.order_by("pk").values())
                before_selections = list(NSOAcceptedSelection.objects.order_by("pk").values())
                before_snapshots = list(self.attempt.overlay_snapshots.order_by("pk").values())
                with self.assertRaises(EvidenceInvariantError):
                    load_deployment_evidence(self.management)
                self.assertEqual(list(NSOApplyAttempt.objects.order_by("pk").values()), before_attempts)
                self.assertEqual(list(NSOAcceptedSelection.objects.order_by("pk").values()), before_selections)
                self.assertEqual(list(self.attempt.overlay_snapshots.order_by("pk").values()), before_snapshots)
                self.assertFalse(NSOApplyExecution.objects.exists())
                self.management.refresh_from_db()
                self.assertIsNone(self.management.settle_cursor_seq)

    def test_known_admission_links_match_the_listing_before_persistence(self):
        self.assert_admission_links_reconciled("known")

    def test_replayed_admission_links_match_the_listing_before_persistence(self):
        self.assert_admission_links_reconciled("replayed")

    def test_non_object_replay_response_is_invalid_before_persistence(self):
        from netbox_nso_plugin.adapter_client import InvalidJobEvidence

        self.prepare()
        self.serve_terminal_evidence(self.terminal())
        NSOApplyAttempt.objects.filter(pk=self.attempt.pk).update(response=None, http_status=None)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                document["unknown_apply_attempt_ids"] = [str(self.attempt.pk)]
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        before_attempts = list(NSOApplyAttempt.objects.values())
        before_selections = list(NSOAcceptedSelection.objects.values())
        for result in ("invalid", ["invalid"], 42, False):
            with self.subTest(result=result):
                self.adapter.apply_response = lambda selected: (202, result)
                with self.assertRaises(InvalidJobEvidence) as raised:
                    load_deployment_evidence(self.management)
                self.assertEqual(raised.exception.reason, "invalid settlement evidence")
                self.assertEqual(list(NSOApplyAttempt.objects.values()), before_attempts)
                self.assertEqual(list(NSOAcceptedSelection.objects.values()), before_selections)
                self.assertFalse(NSOApplyExecution.objects.exists())

    def test_retained_full_promotion_links_match_the_listing_before_use(self):
        self.assert_admission_links_reconciled("retained")

    def test_head_retained_admission_is_checked_without_a_requested_attempt(self):
        self.prepare()
        self.serve_terminal_evidence(self.terminal())
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                document["head"]["projection"]["apply_attempt_id"] = str(self.attempt.pk)
                document["head"]["identity"]["apply_attempt_id"] = str(self.attempt.pk)
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        response = copy.deepcopy(self.attempt.response)
        response["generations"][0]["digest"] = "b" * 64
        NSOApplyAttempt.objects.filter(pk=self.attempt.pk).update(response=response)
        before = list(NSOAcceptedSelection.objects.order_by("pk").values())
        with self.assertRaisesRegex(EvidenceInvariantError, "admission_generation_mismatch"):
            load_deployment_evidence(self.management, attempt_ids=())
        self.assertEqual(list(NSOAcceptedSelection.objects.order_by("pk").values()), before)
        self.assertFalse(NSOApplyExecution.objects.exists())

    def test_null_evidence_response_is_an_invalid_stored_response_before_persistence(self):
        from netbox_nso_plugin.adapter_client import InvalidJobEvidence

        self.prepare()
        self.serve_terminal_evidence(self.terminal(), manual=True)
        NSOApplyAttempt.objects.filter(pk=self.attempt.pk).update(response=None, http_status=None)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                document["attempts"][0]["projection"]["response"] = None
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        before = list(NSOAcceptedSelection.objects.order_by("pk").values())
        with self.assertRaises(InvalidJobEvidence) as raised:
            load_deployment_evidence(self.management)
        self.assertEqual(raised.exception.reason, "invalid settlement evidence")
        self.assertEqual(list(NSOAcceptedSelection.objects.order_by("pk").values()), before)
        self.assertFalse(NSOApplyExecution.objects.exists())

    def test_held_rejection_with_null_generations_has_no_admission_links(self):
        self.prepare()
        self.serve_terminal_evidence(self.terminal())
        rejected = NSOApplyAttempt.objects.create(
            management=self.management,
            adapter_device_id=self.management.adapter_device_id,
            selected={"bgp": 1},
            http_status=409,
            response={
                "error": {"code": "apply_unexecutable", "message": "Rejected.", "detail": {}},
                "generations": None,
            },
        )
        NSOApplyAttempt.objects.filter(pk=rejected.pk).update(adapter_apply_attempt_id=rejected.pk)
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                document["head"]["projection"]["apply_attempt_id"] = str(rejected.pk)
                document["head"]["identity"]["apply_attempt_id"] = str(rejected.pk)
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        with deployment.gate_bypass():
            load_deployment_evidence(self.management, attempt_ids=())

    def test_retry_carrier_does_not_change_immutable_admission_links(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        self.prepare()
        generation = self.terminal()
        generation["carrier_job_id"] = 502
        self.serve_terminal_evidence(generation, manual=True)
        with deployment.gate_bypass():
            settle_device_apply_attempts(self.management)
        self.assertEqual(self.attempt.response["generations"][0]["job_id"], 501)
        self.assertEqual(NSOApplyExecution.objects.get().carrier_job_id, 502)


class TestProbeAdmissionListing(DeploymentCase):
    def prepare_probe(self, *, carrier_job_id=501):
        self._prepare_bgp_observation_capture()
        deployment.quiesce(paired_activation=True)
        self.command(prepare_store=True, device_id=self.device.pk, scope="bgp")
        self.adapter.apply_response = lambda selected: (
            202,
            {
                **_promoted(selected),
                "generations": _promoted(selected)["generations"][:1],
            },
        )
        attempt_id = uuid4()
        self.command(start_probe=str(attempt_id), device_id=self.device.pk)
        self.attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        generation = self.terminal()
        generation["carrier_job_id"] = carrier_job_id
        self.serve_terminal_evidence(generation, manual=True)

    def test_probe_links_match_the_listing_before_binding(self):
        self.prepare_probe()
        attempt_id = self.attempt.pk
        original = self.adapter._handle
        change = {}

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                rows = response.json()
                rows[0]["projection"].update(change)
                rows[0]["identity"].update({key: value for key, value in change.items() if key in rows[0]["identity"]})
                return make_response(200, rows)
            return response

        self.adapter._handle = handle
        before_selections = list(NSOAcceptedSelection.objects.order_by("pk").values())
        for change in ({"seq": 5}, {"digest": "b" * 64}, {"mode": "detach"}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(CommandError, "admission_generation_mismatch"):
                    self.command(register_probe=str(attempt_id))
                self.assertFalse(NSOApplyExecution.objects.exists())
                self.assertEqual(list(NSOAcceptedSelection.objects.order_by("pk").values()), before_selections)

    def test_probe_retry_keeps_the_original_admission_job_link(self):
        self.prepare_probe(carrier_job_id=502)
        self.command(register_probe=str(self.attempt.pk))
        self.assertEqual(self.attempt.response["generations"][0]["job_id"], 501)
        self.assertEqual(NSOApplyExecution.objects.get().carrier_job_id, 502)
