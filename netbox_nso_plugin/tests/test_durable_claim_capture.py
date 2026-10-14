# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Persist exact automatic ownership before the adapter can start execution."""

from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from dcim.models import Interface
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.test import TransactionTestCase
from netbox_routing.models import Community, CommunityList, CommunityListEntry

from netbox_nso_plugin import delivery, drain, read_gate
from netbox_nso_plugin.models import (
    NSOAcceptedSelection,
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSOIntentRevision,
    NSOLACPBundleState,
    NSOLACPMemberState,
    NSORoutePolicyObjectClass,
    NSORoutePolicyState,
)
from netbox_nso_plugin.refusal_protocol import row_ref
from netbox_nso_plugin.refusal_registry import RegistryError

from ._outbox_case import (
    ReceiptAdapter,
    entries,
    expire_claim,
    make_managed,
    mirror_update,
    own_vlan,
    rename_vlan,
    state_of,
    without_commit_drain,
)
from ._ownership_case import save_overlay_fixture
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def _committed_records(management_id):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid = cursor.fetchone()[0]
        selections = list(NSOAcceptedSelection.objects.filter(management_id=management_id).order_by("pk").values())
        snapshots = list(
            NSOApplyOverlaySnapshot.objects.filter(attempt__management_id=management_id).order_by("pk").values()
        )
        return {"backend_pid": backend_pid, "selections": selections, "snapshots": snapshots}
    finally:
        connection.close()


class _CaptureAdapter(ReceiptAdapter):
    def __init__(self, management_id, respond=None):
        super().__init__(respond)
        self.management_id = management_id
        self.observations = []

    def _handle(self, method, url, **kwargs):
        if method != "GET":
            with ThreadPoolExecutor(max_workers=1) as executor:
                records = executor.submit(_committed_records, self.management_id).result(timeout=30)
            wire = kwargs.get("data")
            if wire is None:
                wire = json.dumps(kwargs.get("json"), allow_nan=False).encode()
            elif isinstance(wire, str):
                wire = wire.encode()
            self.observations.append(
                {**records, "request_digest": hashlib.sha256(wire).hexdigest(), "headers": kwargs.get("headers", {})}
            )
        return super()._handle(method, url, **kwargs)


class TestDurableClaimCapture(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("durable-capture", 4271)
        self.adapter = _CaptureAdapter(self.management.pk)
        self.accepted_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        self.applied_at = datetime(2026, 1, 3, 4, 5, 6, tzinfo=UTC)

    def _drain(self, scope="vlan", **kwargs):
        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_key(self.device.pk, scope, chain=0, **kwargs)

    def _push(self, scope="vlan", **kwargs):
        config, session = self.adapter.patches()
        with config, session:
            return drain.push_now(self.device.pk, scope, **kwargs)

    def _vlan(self, vid, *, status="accepted"):
        state = own_vlan(self.management, vid, "durable-capture")
        prior = NSOApplyAttempt.objects.create(management=self.management)
        mirror_update(
            state,
            status=status,
            accepted_at=self.accepted_at,
            last_apply_at=self.applied_at,
            last_apply_error="placeholder prior apply error",
            apply_attempt_id=prior.pk,
        )
        return state, prior

    def _adopt_source_epoch(self, source_epoch):
        result = read_gate.gated_family_run(
            self.management,
            "vlan",
            {
                "outcome": "unavailable",
                "reason": "not_ready",
                "incarnation": "11111111-aaaa-4aaa-8aaa-111111111111",
                "incarnation_born": "2026-07-01T00:00:10Z",
                "source_epoch": source_epoch,
            },
            lambda: self.fail("an unavailable read must not run its body"),
            epoch=self.management.adapter_device_id,
        )
        self.assertEqual(result.disposition, read_gate.SKIPPED_UNAVAILABLE)
        self.management.refresh_from_db()
        self.assertEqual(self.management.adapter_source_epoch, source_epoch)

    def test_takeover_reforms_claim_after_source_epoch_changes(self):
        self._vlan(776)
        self._adopt_source_epoch(1)
        self.adapter._respond = lambda body: (503, {"error": {"code": "unavailable", "message": "retry"}})
        self.assertEqual(self._drain(), drain.FAILED)
        old = NSOAcceptedSelection.objects.get(management=self.management, stream="vlan")
        consumed = entries(self.device, "vlan", unconsumed=False)
        self.assertTrue(consumed)
        self.assertEqual({row.consumed_by_push_seq for row in consumed}, {old.source_push_seq})
        identity = state_of(self.device, "vlan").claim_identity

        self._adopt_source_epoch(2)

        with self.assertLogs("netbox_nso_plugin.drain", level="INFO") as logged:
            self.assertEqual(self._drain(), drain.FAILED)

        current = state_of(self.device, "vlan")
        self.assertGreater(current.push_seq, old.source_push_seq)
        self.assertEqual(current.claim_identity, identity)
        self.assertEqual(self.adapter.sequences, [old.source_push_seq, current.push_seq])
        reformed = entries(self.device, "vlan", unconsumed=False)
        self.assertEqual([row.pk for row in reformed], [row.pk for row in consumed])
        self.assertEqual({row.consumed_by_push_seq for row in reformed}, {current.push_seq})
        new = NSOAcceptedSelection.objects.get(
            management=self.management, stream="vlan", source_push_seq=current.push_seq
        )
        self.assertNotEqual(new.mapping_epoch, old.mapping_epoch)
        old.refresh_from_db()
        self.assertIsNone(old.acknowledged_at)
        self.assertIsNone(new.acknowledged_at)
        self.assertTrue(any("abandoning push_seq" in line for line in logged.output))

    def test_exact_acknowledgement_settles_after_source_epoch_changes(self):
        self._vlan(777)
        self._adopt_source_epoch(1)
        claimed = drain.claim(self.device.pk, "vlan")
        selection = NSOAcceptedSelection.objects.get(management=self.management, source_push_seq=claimed.push_seq)
        consumed = entries(self.device, "vlan", unconsumed=False)
        self.assertTrue(consumed)
        config, session = self.adapter.patches()
        with config, session:
            response = drain.send_claim(claimed)
        self.assertTrue(drain.acknowledgement(claimed, response).exact)

        self._adopt_source_epoch(2)

        with self.assertLogs("netbox_nso_plugin.drain", level="WARNING") as logged:
            self.assertEqual(drain.settle(claimed, response), drain.SUCCEEDED)

        self.assertFalse(entries(self.device, "vlan"))
        current = state_of(self.device, "vlan")
        self.assertIsNone(current.push_seq)
        self.assertIsNone(current.claimed_at)
        self.assertEqual(current.last_success_identity, claimed.identity)
        selection.refresh_from_db()
        self.assertIsNone(selection.acknowledged_at)
        self.assertEqual(selection.publication_state, "recorded")
        self.assertTrue(any("earlier source epoch" in line and "unacknowledged" in line for line in logged.output))

    def test_claim_without_any_selection_fails_loudly(self):
        self._vlan(778)
        self._adopt_source_epoch(1)
        claimed = drain.claim(self.device.pk, "vlan")
        NSOApplyOverlaySnapshot.objects.filter(selection__management=self.management).delete()
        NSOAcceptedSelection.objects.filter(management=self.management).delete()
        self._adopt_source_epoch(2)
        expire_claim(self.device, "vlan")

        for operation in (lambda: self._drain(), lambda: drain.settle(claimed, {"count": 1})):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(RegistryError, "claim has no original durable selection") as caught:
                    operation()
                self.assertIs(type(caught.exception), RegistryError)
                self.assertEqual(state_of(self.device, "vlan").push_seq, claimed.push_seq)
                self.assertTrue(entries(self.device, "vlan", unconsumed=False))

    def _bundle(self, name="Port-channel71"):
        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(device=self.device, name=name, type="lag")
            return save_overlay_fixture(
                NSOLACPBundleState(
                    management=self.management,
                    interface=interface,
                    lag_id=71,
                    status="accepted",
                    accepted_at=self.accepted_at,
                )
            )

    def _member(self, bundle, name):
        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(device=self.device, name=name, type="1000base-t", lag=bundle.interface)
            return save_overlay_fixture(
                NSOLACPMemberState(
                    management=self.management, interface=interface, status="accepted", accepted_at=self.accepted_at
                )
            )

    def _selection(self, stream, *, before_send=False):
        records = (
            self.adapter.observations[-1]["selections"]
            if before_send
            else list(NSOAcceptedSelection.objects.filter(management=self.management).values())
        )
        matches = [record for record in records if record["stream"] == stream]
        self.assertEqual(len(matches), 1, "the exact renderer selection must remain durable")
        return matches[0]

    def _assert_owners(self, selection, owners):
        manifest = selection["contributor_manifest"]
        self.assertEqual(
            {(owner["model_label"], owner["overlay_pk"]) for owner in manifest},
            {(owner._meta.label_lower, owner.pk) for owner in owners},
        )
        for owner in manifest:
            self.assertRegex(owner["content_digest"], r"^[0-9a-f]{64}$")
            self.assertTrue(owner["contributors"])
            for contributor in owner["contributors"]:
                for name in ("logical_ref", "carrier_ref"):
                    self.assertEqual(set(contributor[name]), {"family", "subject", "key"})
                    self.assertRegex(contributor[name]["key"], r"^[0-9a-f]{64}$")
                self.assertEqual(contributor["stream"], selection["stream"])
                self.assertRegex(contributor["contribution_digest"], r"^[0-9a-f]{64}$")

    def test_automatic_claim_snapshot_commits_before_http_send(self):
        state, prior = self._vlan(771)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            writer_pid = cursor.fetchone()[0]

        self.assertEqual(self._drain(), drain.SUCCEEDED)

        observation = self.adapter.observations[0]
        self.assertNotEqual(observation["backend_pid"], writer_pid)
        selection = self._selection("vlan", before_send=True)
        self._assert_owners(selection, [state])
        self.assertEqual(selection["source_push_seq"], int(observation["headers"]["X-Push-Seq"]))
        self.assertEqual(selection["request_digest"], observation["request_digest"])
        self.assertEqual(selection["binding_state"], "unbound")
        self.assertIsNone(selection["acknowledged_at"])
        [snapshot] = observation["snapshots"]
        self.assertEqual(snapshot["selection_id"], selection["id"])
        self.assertEqual(snapshot["prior_status"], "accepted")
        self.assertEqual(snapshot["prior_last_apply_at"], self.applied_at)
        self.assertEqual(snapshot["prior_last_apply_error"], "placeholder prior apply error")
        self.assertEqual(snapshot["prior_apply_attempt_id"], prior.pk)
        self.assertEqual(snapshot["accepted_at"], self.accepted_at)
        self.assertEqual(snapshot["contributors"]["rows"], selection["contributor_manifest"][0]["contributors"])

    def test_store_only_selection_survives_claim_clear(self):
        state, _prior = self._vlan(772)

        self.assertIsNotNone(self._push(mode=delivery.MODE_STORE_ONLY, force=True))

        self.assertEqual(self.adapter.requests[0]["params"].get("store_only"), "true")
        self.assertIsNone(state_of(self.device, "vlan").push_seq)
        selection = self._selection("vlan")
        self._assert_owners(selection, [state])
        self.assertEqual(
            selection["contributor_manifest"], self._selection("vlan", before_send=True)["contributor_manifest"]
        )
        self.assertIsNotNone(selection["acknowledged_at"])
        self.assertEqual(selection["binding_state"], "unbound")
        [snapshot] = NSOApplyOverlaySnapshot.objects.filter(selection_id=selection["id"])
        self.assertEqual(snapshot.settlement_disposition, "unresolved")
        self.assertEqual(snapshot.prior_last_apply_at, self.applied_at)
        self.assertEqual(snapshot.prior_last_apply_error, "placeholder prior apply error")
        self.assertEqual(snapshot.expected_status, snapshot.prior_status)
        self.assertEqual(snapshot.expected_last_apply_at, snapshot.prior_last_apply_at)
        self.assertEqual(snapshot.expected_last_apply_error, snapshot.prior_last_apply_error)
        self.assertEqual(snapshot.expected_apply_attempt_id, snapshot.prior_apply_attempt_id)

    def test_backfill_selection_preserves_exact_owners(self):
        first, _prior = self._vlan(773)
        second, _prior = self._vlan(774, status="in_sync")

        self.assertIsNotNone(self._push(mode=delivery.MODE_BACKFILL_ONLY, force=True))

        self.assertEqual(self.adapter.requests[0]["params"].get("backfill_only"), "true")
        selection = self._selection("vlan", before_send=True)
        self._assert_owners(selection, [first, second])
        self.assertEqual(
            {
                (snapshot["overlay_pk"], snapshot["prior_status"])
                for snapshot in self.adapter.observations[0]["snapshots"]
            },
            {(first.pk, "accepted"), (second.pk, "in_sync")},
        )
        self.assertEqual(self._selection("vlan")["contributor_manifest"], selection["contributor_manifest"])

    def test_direct_preparation_records_exact_selection_revision(self):
        bundle = self._bundle()
        member = self._member(bundle, "Ethernet71")
        self.adapter._respond = lambda body: {
            "status": "prepared",
            "stream": "lag",
            "selection_revision": 91,
            "unauthorized_deleted_roots": [],
        }

        answer = self._push("lacp", force=True)

        self.assertIsNotNone(answer)
        captured = self._selection("lag", before_send=True)
        self._assert_owners(captured, [bundle, member])
        self.assertIsNone(captured["selection_revision"])
        self.assertIsNone(captured["source_push_seq"])
        self.assertEqual(captured["request_digest"], self.adapter.observations[0]["request_digest"])
        selection = self._selection("lag")
        self.assertEqual(selection["selection_revision"], 91)
        self.assertEqual(selection["plugin_revision"], self.adapter.requests[0]["body"]["source_revision"])
        self.assertEqual(
            selection["plugin_revision"], NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision
        )
        self.assertNotEqual(selection["plugin_revision"], selection["selection_revision"])
        for owner in selection["contributor_manifest"]:
            for contributor in owner["contributors"]:
                self.assertEqual(contributor["source_selector"], {"selection_revision": 91})
            snapshot = NSOApplyOverlaySnapshot.objects.get(
                selection_id=selection["id"], model_label=owner["model_label"], overlay_pk=owner["overlay_pk"]
            )
            self.assertEqual(snapshot.contributors["rows"], owner["contributors"])

    def test_repeated_direct_preparation_retains_each_exact_selection(self):
        bundle = self._bundle()
        member = self._member(bundle, "Ethernet75")
        revisions = iter((91, 92))

        def prepared(body):
            return {
                "status": "prepared",
                "stream": "lag",
                "selection_revision": next(revisions),
                "unauthorized_deleted_roots": [],
            }

        self.adapter._respond = prepared
        first = self._push("lacp", force=True)
        self.assertIsNotNone(first)
        first_selection = self._selection("lag")
        frozen_first = copy.deepcopy(first_selection["contributor_manifest"])

        second = self._push("lacp", force=True)

        self.assertIsNotNone(second)
        self.assertEqual((first["selection_revision"], second["selection_revision"]), (91, 92))
        self.assertEqual(len(self.adapter.requests), 2)
        self.assertEqual(self.adapter.requests[0]["body"], self.adapter.requests[1]["body"])
        self.assertEqual(len(self.adapter.observations[0]["selections"]), 1)
        self.assertEqual(len(self.adapter.observations[1]["selections"]), 2)
        prior_at_second_send = self.adapter.observations[1]["selections"]
        self.assertEqual({row["selection_revision"] for row in prior_at_second_send}, {91, None})
        selections = list(
            NSOAcceptedSelection.objects.filter(management=self.management, stream="lag")
            .order_by("selection_revision")
            .values()
        )
        self.assertEqual([row["selection_revision"] for row in selections], [91, 92])
        self.assertNotEqual(selections[0]["id"], selections[1]["id"])
        self.assertEqual(selections[0]["contributor_manifest"], frozen_first)
        for selection in selections:
            self._assert_owners(selection, [bundle, member])
            self.assertIsNone(selection["source_push_seq"])
            self.assertEqual(selection["plugin_revision"], first["source_revision"])
            for owner in selection["contributor_manifest"]:
                for contributor in owner["contributors"]:
                    self.assertEqual(
                        contributor["source_selector"], {"selection_revision": selection["selection_revision"]}
                    )
                snapshot = NSOApplyOverlaySnapshot.objects.get(
                    selection_id=selection["id"], model_label=owner["model_label"], overlay_pk=owner["overlay_pk"]
                )
                self.assertEqual(snapshot.contributors["rows"], owner["contributors"])

    def test_overlapping_direct_preparations_bind_separate_selections(self):
        bundle = self._bundle()
        member = self._member(bundle, "Ethernet77")
        revisions = iter((91, 92))
        inner = []

        def prepared(body):
            revision = next(revisions)
            if not inner:
                inner.append(None)
                inner[0] = drain.push_now(self.device.pk, "lacp", force=True)
            return {
                "status": "prepared",
                "stream": "lag",
                "selection_revision": revision,
                "unauthorized_deleted_roots": [],
            }

        self.adapter._respond = prepared
        outer = self._push("lacp", force=True)

        self.assertIsNotNone(inner[0])
        self.assertIsNotNone(outer)
        selections = list(
            NSOAcceptedSelection.objects.filter(management=self.management, stream="lag").order_by("pk").values()
        )
        self.assertEqual([row["selection_revision"] for row in selections], [91, 92])
        for selection in selections:
            self.assertEqual(selection["publication_state"], "sent")
            self._assert_owners(selection, [bundle, member])

    def test_failed_direct_send_releases_its_capture_for_the_retry(self):
        bundle = self._bundle()
        member = self._member(bundle, "Ethernet78")
        answers = iter(
            (
                (503, {"error": {"code": "unavailable", "message": "retry"}}),
                {"status": "prepared", "stream": "lag", "selection_revision": 91, "unauthorized_deleted_roots": []},
            )
        )
        self.adapter._respond = lambda body: next(answers)

        self.assertIsNone(self._push("lacp", force=True))
        [failed] = NSOAcceptedSelection.objects.filter(management=self.management, stream="lag").values()
        self.assertEqual(failed["publication_state"], "unresolved")

        self.assertIsNotNone(self._push("lacp", force=True))
        [selection] = NSOAcceptedSelection.objects.filter(management=self.management, stream="lag").values()
        self.assertEqual(selection["id"], failed["id"])
        self.assertEqual((selection["selection_revision"], selection["publication_state"]), (91, "sent"))
        self._assert_owners(selection, [bundle, member])

    def test_restored_claim_retains_frozen_manifest(self):
        state, _prior = self._vlan(775)
        self.adapter.drop_response_once = True
        self.assertEqual(self._drain(), drain.FAILED)
        selection = self._selection("vlan")
        frozen = copy.deepcopy(selection["contributor_manifest"])
        [stored] = self.adapter.receipts.values()
        receipt = {
            "accepted_push_seq": stored["push_seq"],
            "request_digest": stored["digest"],
            "stored_response": stored["response"],
        }
        rename_vlan(state, "placeholder successor vlan")

        self.assertEqual(drain.resolve_restored_claim(self.device.pk, "vlan", receipt), drain.RESTORE_SETTLED)

        selection = self._selection("vlan")
        self.assertEqual(selection["contributor_manifest"], frozen)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertIsNone(state_of(self.device, "vlan").push_seq)
        [snapshot] = NSOApplyOverlaySnapshot.objects.filter(selection_id=selection["id"])
        self.assertEqual(snapshot.content_digest, frozen[0]["content_digest"])
        self.assertEqual(snapshot.contributors["rows"], frozen[0]["contributors"])

    def test_manifest_excludes_filtered_lag_member_and_omitted_policy_member(self):
        bundle = self._bundle()
        member = self._member(bundle, "Ethernet72")
        filtered = self._member(bundle, "Ethernet73")
        filtered.interface.lag = None
        with without_commit_drain():
            filtered.interface.save(update_fields=("lag",))
        self.assertIsNotNone(self._push("lacp", force=True))
        lag_selection = self._selection("lag")
        self._assert_owners(lag_selection, [bundle, member])
        self.assertNotIn(
            filtered.pk,
            [
                owner["overlay_pk"]
                for owner in lag_selection["contributor_manifest"]
                if owner["model_label"] == filtered._meta.label_lower
            ],
        )

        with without_commit_drain(), transaction.atomic():
            policy = CommunityList.objects.create(name="placeholder-members")
            CommunityListEntry.objects.create(
                community_list=policy, action="permit", community=Community.objects.create(community="64512:10")
            )
            state = save_overlay_fixture(
                NSORoutePolicyState(
                    management=self.management,
                    family="community_list",
                    object_name=policy.name,
                    content_type=ContentType.objects.get_for_model(CommunityList),
                    object_id=policy.pk,
                    status="accepted",
                )
            )
            omitted_name = "placeholder-omitted"
            NSORoutePolicyObjectClass.objects.create(family="community_list", object_name=omitted_name, mode="local")
            omitted_state = save_overlay_fixture(
                NSORoutePolicyState(
                    management=self.management,
                    family="community_list",
                    object_name=omitted_name,
                    status="accepted",
                )
            )
        self.assertIsNone(NSORoutePolicyState.objects.get(pk=omitted_state.pk).assigned_object)
        self.assertIsNotNone(self._push("route_policy", force=True))
        selection = self._selection("route_policy")
        self._assert_owners(selection, [state])
        self.assertEqual([item["name"] for item in self.adapter.requests[-1]["body"]["objects"]], [policy.name])
        supported = row_ref(
            "route-policy",
            "route-policy.community-list",
            (("community-list", (policy.name,)),),
        )
        emitted_refs = [
            ref
            for owner in selection["contributor_manifest"]
            for contributor in owner["contributors"]
            for ref in contributor["emitted_refs"]
        ]
        self.assertIn({"family": "route-policy", "subject": supported.subject, "key": supported.key}, emitted_refs)
        omitted = row_ref(
            "route-policy", "route-policy.community-list", (("community-list", (omitted_state.object_name,)),)
        )
        self.assertNotIn({"family": "route-policy", "subject": omitted.subject, "key": omitted.key}, emitted_refs)

    def test_nullable_carried_acceptance_is_not_child_accepted_at(self):
        bundle = self._bundle()
        member = self._member(bundle, "Ethernet74")

        self.assertIsNotNone(self._push("lacp", force=True))

        selection = self._selection("lag")
        self.assertIsNone(selection["acceptance_identity"])
        owner = next(
            item
            for item in selection["contributor_manifest"]
            if item["model_label"] == member._meta.label_lower and item["overlay_pk"] == member.pk
        )
        for contributor in owner["contributors"]:
            self.assertIsNone(contributor["remote_acceptance_identity"])
            self.assertEqual(contributor["carrier_ref"]["subject"], "lag.bundle")
        snapshot = NSOApplyOverlaySnapshot.objects.get(
            selection_id=selection["id"], model_label=member._meta.label_lower, overlay_pk=member.pk
        )
        self.assertEqual(snapshot.accepted_at, self.accepted_at)
