# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Resolve acknowledged obligations by type in every settlement consumer."""

import copy

from django.utils import timezone

from netbox_nso_plugin import durable_capture as capture
from netbox_nso_plugin.apply_settlement import deployment_evidence_attempt_ids
from netbox_nso_plugin.deployment_probe import ProbeBlocked, authority
from netbox_nso_plugin.lifecycle_fence import fence_for
from netbox_nso_plugin.models import (
    NSOApplyExecution,
    NSOSettlementIssue,
    NSOSettlementIssueOwner,
)
from netbox_nso_plugin.settlement import consume_static_route_settlements
from netbox_nso_plugin.settlement_acknowledgement import acknowledge
from netbox_nso_plugin.settlement_resolution import instant

from ._adapter_http import make_generation_listing, make_response
from ._bgp_settlement import BGPSettlementHelpers
from ._outbox_case import content_update, mirror_update
from ._refusal_display import RefusalDisplayHelpers
from ._settlement_observation import INCARNATION, failed_item, serve_observation
from ._settlement_wire import generation_wire
from .test_durable_bgp_capture import BGPCaptureCase
from .test_settlement_issue import ADVISORY, HistoricalStore, SettlementIssueHelpers


class TestAcknowledgedObligationConsumers(
    SettlementIssueHelpers, RefusalDisplayHelpers, BGPSettlementHelpers, BGPCaptureCase
):
    def acknowledged_carrier(self):
        issue = self.stall_invalid_manual_carrier()
        acknowledge(issue, issue.revision_digest, self.operator(), "carrier result unreadable")
        issue.refresh_from_db()
        return issue

    def serve_retry(self, *, dispositions=None):
        """Retry generation 81 under carrier 503, joined to the manual attempt through the head."""
        retry = self.terminal(refused=None)
        retry["carrier_job_id"] = 503
        if dispositions is not None:
            for outcome in retry["row_outcomes"]:
                outcome["disposition"] = dispositions
        self.serve_terminal_evidence(retry, settle_seq=2)
        original = self.adapter._handle
        attempt_id = str(self.attempt.pk)

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence") and response.json()["head"] is not None:
                document = response.json()
                document["head"]["identity"]["apply_attempt_id"] = attempt_id
                document["head"]["projection"]["apply_attempt_id"] = attempt_id
                return make_response(200, document)
            return response

        self.adapter._handle = handle
        return retry

    def unknown_owner(self, row, contribution, job_id):
        """Record one more acknowledged unknown contribution under the owner's current acceptance."""
        fence = fence_for_row(row)
        now = timezone.now()
        issue = NSOSettlementIssue.objects.create(
            management=self.management,
            mapping_epoch=capture.mapping_epoch(self.management),
            adapter_device_id=self.management.adapter_device_id,
            store_incarnation=INCARNATION,
            job_id=job_id,
            settle_seq=1,
            request_fence={},
            stall_class="invalid_evidence",
            stall_reason="invalid job envelope",
            position="historical",
            cursor_before=1,
            cursor_after=1,
            evidence_digest="b" * 64,
            impact_digest="c" * 64,
            contract_fingerprint="d" * 64,
            revision_digest="e" * 64,
            proposal={"obligations": []},
            state="acknowledged",
            closed_at=now,
            actor_id=1,
            actor_label="placeholder-operator",
            acknowledgement_reason="placeholder reason",
            acknowledged_at=now,
            committed_effects=[],
        )
        NSOSettlementIssueOwner.objects.create(
            issue=issue,
            model_label=row._meta.label_lower,
            overlay_pk=row.pk,
            contributors=[contribution],
            unknown=[contribution],
            authority={"accepted_at": instant(row.accepted_at), "owner_revision": fence.owner_revision},
            order_bound=1,
            proposed_effect="unavailable",
            committed_effect="unavailable",
        )

    def test_a_later_retry_certificate_covers_the_unknown_contribution(self):
        self.acknowledged_carrier()
        self.serve_retry()
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.consumed, result.cursor, result.stalled), (1, 2, False))
        retry = NSOApplyExecution.objects.get(carrier_job_id=503)
        self.assertEqual(retry.consumption_state, "consumed")
        self.assertEqual(retry.attempt.trigger_kind, "explicit_retry")
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")
        covered = NSOSettlementIssueOwner.objects.filter(issue__job_id=501)
        self.assertTrue(all(owner.covered == owner.unknown for owner in covered))

    def test_an_unknown_contribution_that_no_later_certificate_decides_keeps_the_owner_unknown(self):
        self.acknowledged_carrier()
        router = self.owners[0]
        router.refresh_from_db()
        self.unknown_owner(router, ["bgp", "bgp.router", "f" * 64], 499)
        self.serve_retry()
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.cursor, result.stalled), (2, False))
        router.refresh_from_db()
        self.assertEqual(router.status, "apply_failed")
        self.assertEqual(router.last_apply_error, ADVISORY.format(499))
        for owner in self.owners[1:]:
            owner.refresh_from_db()
            self.assertEqual(owner.status, "in_sync")

    def test_a_later_unavailable_settlement_keeps_the_acknowledged_advisory_on_the_row(self):
        from django.urls import reverse

        from netbox_nso_plugin.refusal_display import UNAVAILABLE

        from .test_refusal_display_parity import OwnerHTML, owner_projections

        self.acknowledged_carrier()
        router = self.owners[0]
        router.refresh_from_db()
        self.unknown_owner(router, ["bgp", "bgp.router", "f" * 64], 499)
        self.serve_retry()
        self.assertFalse(consume_static_route_settlements(self.management).stalled)
        expected = {
            "state": "apply_failed",
            "label": "outcome unknown",
            "pending": True,
            "advisory": ADVISORY.format(499),
            "error": None,
        }
        parsed = OwnerHTML()
        parsed.feed(self.category("bgp").content.decode())
        payload = dict(owner_projections(self.category("bgp", format="json").json()))
        key = (router._meta.model_name, router.pk)
        for surface in (parsed.owners[key], payload[key]):
            self.assertEqual({name: surface[name] for name in expected}, expected)
        tab = self.client.get(reverse("dcim:device_nso", args=(self.device.pk,)))
        self.assertContains(tab, UNAVAILABLE)

    def test_the_not_applied_restore_cannot_restore_success_over_an_unknown_contribution(self):
        self.prepare()
        peer = self.owners[2]
        peer.refresh_from_db()
        self.assertEqual(peer.status, "in_sync")
        snapshots = self.attempt.overlay_snapshots.filter(model_label=peer._meta.label_lower, overlay_pk=peer.pk)
        ref = snapshots.get().contributors["rows"][0]["logical_ref"]
        self.unknown_owner(peer, ["bgp", ref["subject"], ref["key"]], 499)
        capture.restore_attempt(self.attempt.pk, keep_streams=())
        peer.refresh_from_db()
        self.assertEqual(peer.status, "apply_failed")
        self.assertEqual(peer.last_apply_error, ADVISORY.format(499))
        self.assertEqual(snapshots.get().settlement_disposition, "unavailable")
        router = self.owners[0]
        router.refresh_from_db()
        self.assertEqual(router.status, "accepted")

    def test_a_new_acceptance_fences_the_unknown_contribution_into_history(self):
        from netbox_nso_plugin.settlement_resolution import unknown_contributions

        self.acknowledged_carrier()
        router, peer = self.owners[0], self.owners[1]
        router.refresh_from_db()
        peer.refresh_from_db()
        self.assertTrue(unknown_contributions(router, fence_for_row(router)))
        content_update(router, accepted_at=timezone.now())
        router.refresh_from_db()
        self.assertEqual(unknown_contributions(router, fence_for_row(router)), [])
        self.assertTrue(unknown_contributions(peer, fence_for_row(peer)))

    def test_a_rewalk_of_the_same_store_passes_the_acknowledged_job_by_its_identity(self):
        self.acknowledged_carrier()
        mirror_update(self.management, settle_cursor_device_id=self.management.adapter_device_id + 1)
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.epoch_reset)
        self.assertEqual((result.cursor, result.stalled), (1, False))
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error, {})

    def test_probe_authority_and_policy_journals_accept_only_certificates(self):
        from extras.models import JournalEntry

        from netbox_nso_plugin.models import NSOApplyAttempt
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        self.acknowledged_carrier()
        NSOApplyAttempt.objects.filter(pk=self.attempt.pk).update(selected={"bgp": self.attempt.selected["bgp"]})
        with self.assertRaisesRegex(ProbeBlocked, "no captured terminal evidence"):
            authority(self.attempt.pk, completed=True)
        _journal_route_policy_apply(self.management, {"id": 501, "generation_id": 81, "store_incarnation": INCARNATION})
        self.assertFalse(JournalEntry.objects.exists())

    def test_an_attempt_keeps_outstanding_sibling_generations_discoverable(self):
        self.assert_outstanding_sibling_keeps_the_attempt_open(other_store_certificate=False)

    def test_a_sibling_certificate_from_another_store_does_not_complete_the_attempt(self):
        self.assert_outstanding_sibling_keeps_the_attempt_open(other_store_certificate=True)

    def assert_outstanding_sibling_keeps_the_attempt_open(self, *, other_store_certificate):
        from uuid import UUID

        from django.urls import reverse

        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._adapter_http import make_generation_listing
        from ._settlement_wire import generation_wire
        from .test_apply_selector import _promoted

        def admission(selected):
            response = _promoted(selected)
            for link in response["generations"]:
                for key in ("stream_revisions", "source_push_seq"):
                    link[key] = {stream: value for stream, value in link[key].items() if stream == "bgp"}
            response["skipped"] = {stream: "no_receipt" for stream in selected if stream != "bgp"}
            return 202, response

        self.adapter.apply_response = admission
        scope = self.scope()
        self.owners = [self.router_owner(), self.peer(scope, "198.18.0.2"), self.peer(scope, "198.18.0.3")]
        answer = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(answer.status_code, 200, answer.content)
        self.attempt = NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))
        link = self.attempt.response["generations"][1]
        generation = self.terminal(refused=None)
        pending = copy.deepcopy(generation)
        pending.update(
            generation_id=82,
            seq=5,
            status="pending",
            carrier_job_id=None,
            carrier_job_status=None,
            carrier_job_result=None,
            row_outcomes=None,
            refusals=None,
        )
        listed = make_generation_listing(
            generation_id=82,
            seq=5,
            status="pending",
            job_id=None,
            carrier_job_id=None,
            carrier_job_status=None,
            mode="detach",
            settlement_cohort=None,
            digest=link["digest"],
            stream_revisions=link["stream_revisions"],
            source_push_seq=link["source_push_seq"],
        )
        self.serve_terminal_evidence(generation, manual=True)
        original = self.adapter._handle

        def with_pending(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "GET" and url.endswith("/generations"):
                return make_response(
                    200, [*response.json(), generation_wire(listed, self.management.adapter_device_id)]
                )
            if method == "POST" and url.endswith("/deployment-evidence"):
                document = response.json()
                for attempt in document["attempts"]:
                    attempt["generations"].append(generation_wire(pending, self.management.adapter_device_id))
                    attempt["projection"]["generation_ids"] = [81, 82]
                return make_response(200, document)
            return response

        self.adapter._handle = with_pending
        self.invalid_feed_job()
        serve_observation(self)
        if other_store_certificate:
            _other_store_certificate(self.management, 82)
        consume_static_route_settlements(self.management)
        issue = NSOSettlementIssue.objects.get(state="open")
        acknowledge(issue, issue.revision_digest, self.operator(), "carrier result unreadable")
        self.attempt.refresh_from_db()
        self.assertFalse(self.attempt.overlay_snapshots.filter(settlement_disposition="unresolved").exists())
        self.assertEqual(self.attempt.lifecycle, "open")
        self.assertIn(self.attempt.pk, deployment_evidence_attempt_ids(self.management))
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.lifecycle, "open")


def _other_store_certificate(management, generation_id):
    """Record a consumed certificate whose generation id belongs to an earlier store."""
    from netbox_nso_plugin.models import NSOApplyAttempt

    attempt = NSOApplyAttempt.objects.create(
        management=management,
        adapter_device_id=management.adapter_device_id,
        trigger_kind="cadence",
        lifecycle="terminal",
        completed_at=timezone.now(),
    )
    return NSOApplyExecution.objects.create(
        attempt=attempt,
        management=management,
        mapping_epoch=capture.mapping_epoch(management),
        adapter_device_id=management.adapter_device_id,
        store_incarnation="00000000-0000-4000-8000-000000000009",
        generation_id=generation_id,
        generation_seq=1,
        carrier_job_id=900,
        settle_seq=1,
        terminal_projection={"generation_id": generation_id},
        terminal_at=timezone.now(),
        consumption_state="consumed",
        consumed_at=timezone.now(),
    )


def fence_for_row(row):
    from django.db import transaction

    with transaction.atomic():
        return fence_for(row)


class TestAcknowledgedHistoryConsumers(SettlementIssueHelpers, HistoricalStore, BGPCaptureCase):
    def acknowledged_history(self):
        self.serve_history()
        consume_static_route_settlements(self.management)
        issue = NSOSettlementIssue.objects.get(state="open")
        acknowledge(issue, issue.revision_digest, self.operator(), "pre-ledger generation")
        return issue

    def test_an_acknowledgement_from_an_earlier_mapping_epoch_still_releases_the_obligation(self):
        self.acknowledged_history()
        before = capture.mapping_epoch(self.management)
        mirror_update(self.management, adapter_source_epoch=(self.management.adapter_source_epoch or 0) + 1)
        self.management.refresh_from_db()
        self.assertNotEqual(capture.mapping_epoch(self.management), before)
        result = consume_static_route_settlements(self.management)
        self.assertFalse(result.stalled)
        self.assertTrue(result.drained)

    def acknowledged_readiness_store(self):
        """Serve a store whose only obligation an operator acknowledged after readiness refused it."""
        from django.db import transaction

        from netbox_nso_plugin.deployment_checks import check_active_streams

        from ._outbox_case import without_commit_drain

        with without_commit_drain(), transaction.atomic():
            self.router.delete()
        self.prior_attempt.delete()
        self.serve_history()
        original = self.adapter._handle

        def inventory(method, url, **kwargs):
            if url.endswith("/intent-summary"):
                return make_response(
                    200, {"device_id": self.management.adapter_device_id, "scopes": {}, "pending_clear": {}}
                )
            if url.endswith("/intent-receipts"):
                return make_response(200, {"receipts": [], "global_max_push_seq": None, "global_max_route_id": None})
            return original(method, url, **kwargs)

        self.adapter._handle = inventory
        with transaction.atomic(), self.assertRaisesRegex(ProbeBlocked, "missing_generation_evidence"):
            check_active_streams()
        consume_static_route_settlements(self.management)
        issue = NSOSettlementIssue.objects.get(state="open")
        acknowledge(issue, issue.revision_digest, self.operator(), "pre-ledger generation")

    def test_readiness_resolves_the_acknowledged_generation_without_a_certificate(self):
        from django.db import transaction

        from netbox_nso_plugin.deployment_checks import check_active_streams

        self.acknowledged_readiness_store()
        with transaction.atomic():
            check_active_streams()

    def truncate_listing_at_the_acknowledged_obligation(self):
        """Repeat the acknowledged generation, then list a later unacknowledged carrier."""
        for generation_id, seq, carrier in ((60, 3, 40), (61, 4, 41)):
            self.history_listing.append(
                make_generation_listing(
                    generation_id=generation_id,
                    seq=seq,
                    job_id=carrier,
                    carrier_job_id=carrier,
                    stream_revisions={"bgp": 3},
                    source_push_seq={"bgp": 999},
                )
            )

    def test_readiness_blocks_on_a_listing_truncated_at_an_acknowledged_obligation(self):
        from django.db import transaction

        from netbox_nso_plugin.adapter_client import InvalidJobEvidence
        from netbox_nso_plugin.deployment_checks import check_active_streams

        self.acknowledged_readiness_store()
        self.truncate_listing_at_the_acknowledged_obligation()
        with transaction.atomic(), self.assertRaises(InvalidJobEvidence):
            check_active_streams()

    def test_readiness_ignores_a_failed_listing_row_of_an_acknowledged_obligation(self):
        from django.db import transaction

        from netbox_nso_plugin.deployment_checks import check_active_streams

        self.acknowledged_readiness_store()
        original = self.adapter._handle
        device_id = self.management.adapter_device_id

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/generations"):
                return make_response(
                    200, [failed_item(generation_wire(row, device_id)) for row in self.history_listing]
                )
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        with transaction.atomic():
            check_active_streams()

    def test_settlement_stalls_on_a_listing_truncated_at_an_acknowledged_obligation(self):
        self.acknowledged_history()
        self.assertTrue(consume_static_route_settlements(self.management).drained)
        self.truncate_listing_at_the_acknowledged_obligation()
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        self.assertFalse(result.drained)
        self.management.refresh_from_db()
        self.assertEqual(self.management.settle_evidence_error["identity"]["generation_id"], 60)
        self.assertEqual(self.management.settle_evidence_error["identity"]["seq"], 3)

    def test_readiness_ignores_a_failed_head_of_an_acknowledged_obligation(self):
        from django.db import transaction

        from netbox_nso_plugin.deployment_checks import check_active_streams

        self.acknowledged_readiness_store()
        original = self.adapter._handle
        head = failed_item(generation_wire(self.history_listing[0], self.management.adapter_device_id))

        def handle(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST" and url.endswith("/deployment-evidence"):
                return make_response(200, {**response.json(), "head": head})
            return response

        self.adapter._handle = handle
        with transaction.atomic():
            check_active_streams()


def _retarget(generation, generation_id, seq, carrier_job_id):
    """Return the same frozen outcomes as evidence of another generation and carrier."""
    moved = copy.deepcopy(generation)
    moved.update(generation_id=generation_id, seq=seq, carrier_job_id=carrier_job_id)
    for outcome in moved["row_outcomes"]:
        outcome["generation_id"] = generation_id
        outcome["carrier"]["generation_id"] = generation_id
    for owner in moved["carrier_job_result"]["settlement_owners"]:
        owner["generation_id"] = generation_id
    return moved


class TestLaterManualCertificate(SettlementIssueHelpers, BGPSettlementHelpers, BGPCaptureCase):
    """Apply B freezes the owners, then carrier A is acknowledged, then B's certificate arrives."""

    def second_apply(self, generation_id=83, seq=6, job_id=502):
        from uuid import UUID

        from django.urls import reverse

        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._bgp_settlement import promoted_streams

        def admission(selected):
            response = promoted_streams(selected, {"bgp"})
            response["job_id"] = job_id
            response["generations"][0].update(generation_id=generation_id, seq=seq, job_id=job_id, digest="c" * 64)
            return 202, response

        self.adapter.apply_response = admission
        answer = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(answer.status_code, 200, answer.content)
        return NSOApplyAttempt.objects.get(pk=UUID(self.adapter.manual_reads[-1]["body"]["apply_attempt_id"]))

    def serve_feed(self, carriers, failed):
        from ._adapter_http import make_generation_listing
        from ._settlement_wire import deployment_wire, generation_wire, job_wire
        from .test_durable_bgp_capture import _CaptureHandler

        class Handler(_CaptureHandler):
            def end_headers(handler):
                handler.send_header("X-Store-Incarnation", INCARNATION)
                super().end_headers()

        self.server.RequestHandlerClass = Handler
        from netbox_nso_plugin import adapter_client

        adapter_client.reset_session()
        device_id = self.management.adapter_device_id
        jobs = []
        listing = []
        for settle_seq, (attempt, generation) in enumerate(carriers, start=1):
            link = attempt.response["generations"][0]
            jobs.append(
                {
                    "id": generation["carrier_job_id"],
                    "type": "apply",
                    "device_id": device_id,
                    "status": "succeeded",
                    "result": generation["carrier_job_result"],
                    "row_outcomes": generation["row_outcomes"],
                    "refusals": generation["refusals"],
                    "error": None,
                    "context": None,
                    "created_at": self.stamp.isoformat(),
                    "updated_at": generation["updated_at"],
                    "started_at": None,
                    "heartbeat_at": None,
                    "settle_seq": settle_seq,
                }
            )
            listing.append(
                make_generation_listing(
                    generation_id=generation["generation_id"],
                    seq=generation["seq"],
                    status=generation["status"],
                    job_id=generation["carrier_job_id"],
                    carrier_job_id=generation["carrier_job_id"],
                    digest=link["digest"],
                    stream_revisions=generation["stream_revisions"],
                    source_push_seq=generation["source_push_seq"],
                    updated_at=generation["updated_at"],
                )
            )
        by_attempt = {str(attempt.pk): (attempt, generation) for attempt, generation in carriers}

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/jobs"):
                after = int(kwargs["params"]["after_settle_seq"])
                rows = [job_wire(job) for job in jobs if job["settle_seq"] > after]
                return make_response(
                    200, [failed_item(row) if row["identity"]["id"] in failed else row for row in rows]
                )
            if method == "GET" and url.endswith("/generations"):
                return make_response(200, [generation_wire(row, device_id) for row in listing])
            if method == "POST" and url.endswith("/deployment-evidence"):
                requested = kwargs["json"]["apply_attempt_ids"]
                known = [value for value in requested if value in by_attempt]
                return make_response(
                    200,
                    deployment_wire(
                        {
                            "device_id": device_id,
                            "head": None,
                            "blocked": False,
                            "write_work_pending": False,
                            "held_jobs": [],
                            "pending_generations": 0,
                            "attempts": [
                                {
                                    "apply_attempt_id": value,
                                    "admission_state": "admitted",
                                    "http_status": 202,
                                    "response": by_attempt[value][0].response,
                                    "generations": [by_attempt[value][1]],
                                }
                                for value in known
                            ],
                            "unknown_apply_attempt_ids": [value for value in requested if value not in known],
                        }
                    ),
                )
            return make_response(404, {"error": {"code": "not_found", "message": "unexpected request"}})

        self.adapter._handle = handle
        serve_observation(self)

    def acknowledge_then_certify(self, dispositions):
        self.prepare()
        first = self.attempt
        stalled = _retarget(
            self.evidence(dispositions={"bgp.router": "in_sync", "bgp.router.scope.peer": "in_sync"}), 81, 4, 501
        )
        second = self.second_apply()
        self.attempt = second
        certified = _retarget(self.evidence(dispositions=dispositions), 83, 6, 502)
        self.serve_feed([(first, stalled), (second, certified)], failed={501})
        result = consume_static_route_settlements(self.management)
        self.assertTrue(result.stalled)
        issue = NSOSettlementIssue.objects.get(state="open")
        acknowledge(issue, issue.revision_digest, self.operator(), "carrier result unreadable")
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.cursor, result.stalled), (2, False))
        return second

    def test_a_later_manual_certificate_frozen_before_the_acknowledgement_clears_the_unknown(self):
        second = self.acknowledge_then_certify({"bgp.router": "in_sync", "bgp.router.scope.peer": "in_sync"})
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual((owner.status, owner.apply_attempt_id), ("in_sync", second.pk))
        self.assertEqual(set(second.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"in_sync"})

    def test_a_later_not_applied_decision_keeps_the_acknowledged_state(self):
        second = self.acknowledge_then_certify({"bgp.router": "in_sync", "bgp.router.scope.peer": "not_applied"})
        router, *peers = self.owners
        router.refresh_from_db()
        self.assertEqual(router.status, "in_sync")
        for peer in peers:
            peer.refresh_from_db()
            self.assertEqual((peer.status, peer.last_apply_error), ("apply_failed", ADVISORY.format(501)))
            snapshot = second.overlay_snapshots.get(model_label=peer._meta.label_lower, overlay_pk=peer.pk)
            self.assertEqual(snapshot.settlement_disposition, "not_applied")

    def test_a_later_manual_certificate_clears_unknowns_after_two_acknowledgements(self):
        in_sync = {"bgp.router": "in_sync", "bgp.router.scope.peer": "in_sync"}
        self.prepare()
        first = self.attempt
        stalled = _retarget(self.evidence(dispositions=in_sync), 81, 4, 501)
        second = self.second_apply()
        self.attempt = second
        also_stalled = _retarget(self.evidence(dispositions=in_sync), 83, 6, 502)
        third = self.second_apply(generation_id=84, seq=7, job_id=503)
        self.attempt = third
        certified = _retarget(self.evidence(dispositions=in_sync), 84, 7, 503)
        self.serve_feed([(first, stalled), (second, also_stalled), (third, certified)], failed={501, 502})
        for job_id in (501, 502):
            self.assertTrue(consume_static_route_settlements(self.management).stalled)
            issue = NSOSettlementIssue.objects.get(state="open")
            self.assertEqual(issue.job_id, job_id)
            acknowledge(issue, issue.revision_digest, self.operator(), "carrier result unreadable")
        result = consume_static_route_settlements(self.management)
        self.assertEqual((result.cursor, result.stalled), (3, False))
        for owner in self.owners:
            owner.refresh_from_db()
            self.assertEqual((owner.status, owner.apply_attempt_id), ("in_sync", third.pk))
