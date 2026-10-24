# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Journal only the frozen policy owners whose execution was consumed."""

import copy

from django.utils import timezone
from extras.models import JournalEntry

from netbox_nso_plugin.durable_capture import mapping_epoch
from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyExecution, NSOApplyOverlaySnapshot, NSORoutePolicyState
from netbox_nso_plugin.refusal_registry import owner_spec
from netbox_nso_plugin.selection_manifest import current_content_digest

from ._ownership_case import save_overlay_fixture

# Module scope: a lazy import would subclass the conftest's blocked Session.
from ._settlement_adapter import FakeAdapter, LoopbackOnlySession
from .test_route_policy_panel_journal import _RoutePolicyFixture


class TestStructuredPolicyJournal(_RoutePolicyFixture):
    def _payload(self):
        payload = super()._payload()
        payload["prefix_lists"][0]["entries"][0]["prefix"] = "198.18.0.0/15"
        return payload

    def _owned_reconcile(self):
        self._reconcile()
        for state in NSORoutePolicyState.objects.filter(management__device=self.device):
            state.status = "accepted"
            save_overlay_fixture(state, update_fields=("status",))

    def execution(self, *, disposition="in_sync", job_id=23, store="placeholder-policy-store"):
        management = self._make_mgmt()
        self._owned_reconcile()
        row = NSORoutePolicyState.objects.get(management=management, family="community_list")
        logical = owner_spec("route-policy.community-list").logical_ref(
            "route-policy.community-list", (("community-list", ("CLJ",)),)
        )
        carrier = {
            "stream": "route_policy",
            "row": {"subject": logical.subject, "key": logical.key},
            "source_push_seq": 41,
            "frozen_revision": 7,
            "accepted_at": None,
            "generation_id": 17,
            "settlement_cohort": 9,
        }
        identity = {key: value for key, value in carrier.items() if key != "accepted_at"}
        identity.update(acceptance_identity=None, carrier=carrier)
        result = {
            "row_outcomes": [{**identity, "disposition": disposition, "refusals": []}],
            "refusals": [],
            "settlement_owners": [{**carrier, "disposition": disposition}],
            "commit_acknowledged": disposition == "in_sync",
            "route_policy_count_by_outcome": {
                "in_sync": int(disposition == "in_sync"),
                "apply_failed": 0,
                "not_applied": int(disposition == "not_applied"),
                "unavailable": int(disposition == "unavailable"),
            },
        }
        at = timezone.now()
        attempt = NSOApplyAttempt.objects.create(management=management, trigger_kind="cadence")
        execution = NSOApplyExecution.objects.create(
            attempt=attempt,
            management=management,
            mapping_epoch=mapping_epoch(management),
            adapter_device_id=management.adapter_device_id,
            store_incarnation=store,
            generation_id=17,
            generation_seq=9,
            carrier_job_id=job_id,
            terminal_at=at,
            terminal_projection={"carrier_job_result": copy.deepcopy(result)},
            consumption_state="consumed",
            consumed_at=at,
        )
        snapshot = NSOApplyOverlaySnapshot.objects.create(
            attempt=attempt,
            model_label=row._meta.label_lower,
            overlay_pk=row.pk,
            mapping_epoch=mapping_epoch(management),
            content_digest=current_content_digest(row),
            accepted_at=row.accepted_at,
            prior_status=row.status,
            expected_status=row.status,
            prior_last_apply_at=row.last_apply_at,
            expected_last_apply_at=row.last_apply_at,
            prior_last_apply_error=row.last_apply_error,
            expected_last_apply_error=row.last_apply_error,
            settlement_disposition=disposition,
            contributors={
                "rows": [{"stream": "route_policy", "logical_ref": {"subject": logical.subject, "key": logical.key}}]
            },
        )
        snapshot.executions.add(execution)
        job = {
            "id": job_id,
            "type": "apply",
            "status": "succeeded",
            "result": result,
            "store_incarnation": store,
            "generation_id": 17,
        }
        return management, row, execution, job

    def test_policy_journal_uses_frozen_object_outcome(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        _journal_route_policy_apply(management, job)

        object_entries = JournalEntry.objects.exclude(
            assigned_object_id=self.device.pk, assigned_object_type__model="device"
        )
        assert object_entries.count() == 1
        assert object_entries.get().assigned_object == row.assigned_object
        row.refresh_from_db()
        assert row.last_apply_at is None

    def test_not_applied_policy_does_not_get_a_success_journal(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution(disposition="not_applied")
        _journal_route_policy_apply(management, job)
        assert not JournalEntry.objects.exists()

    def test_unconsumed_policy_evidence_never_journals_current_objects(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        execution.consumption_state = "unresolved"
        execution.save(update_fields=("consumption_state",))
        _journal_route_policy_apply(management, job)
        assert not JournalEntry.objects.exists()

    def test_policy_manifest_captures_exact_emitted_entry_causes(self):
        from netbox_nso_plugin.refusal_protocol import row_ref
        from netbox_nso_plugin.selection_manifest import frozen_manifest
        from netbox_nso_plugin.signals import route_policy_intent_item

        management = self._make_mgmt()
        self._owned_reconcile()
        rows = list(NSORoutePolicyState.objects.filter(management=management))
        payload = [route_policy_intent_item(row) for row in rows]
        manifest, _ = frozen_manifest(management, "route_policy", payload)
        actual = {owner["overlay_pk"]: owner["contributors"][0]["emitted_refs"] for owner in manifest}
        for row, item in zip(rows, payload, strict=True):
            assert item["entries"], row.family
            subject = "route-policy." + row.family.replace("_", "-")
            path = ((subject.removeprefix("route-policy."), (row.object_name,)),)
            refs = [row_ref("route-policy", subject, path)]
            refs.extend(
                row_ref("route-policy", subject + ".entry", (*path, ("entry", (entry["sequence"],))))
                for entry in item["entries"]
                if "sequence" in entry
            )
            assert actual[row.pk] == [
                {"family": "route-policy", "subject": ref.subject, "key": ref.key} for ref in refs
            ]

    def test_policy_journal_is_idempotent_after_later_carrier(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, old_job = self.execution()
        _journal_route_policy_apply(management, old_job)
        _, _, _, new_job = self.execution(job_id=24)
        _journal_route_policy_apply(management, new_job)
        _journal_route_policy_apply(management, old_job)
        assert (
            JournalEntry.objects.filter(assigned_object_type=row.content_type, assigned_object_id=row.object_id).count()
            == 2
        )

    def test_same_job_number_in_replacement_store_is_independent(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        _journal_route_policy_apply(management, job)
        _, _, _, replacement = self.execution(store="placeholder-replacement-store")
        _journal_route_policy_apply(management, replacement)
        assert (
            JournalEntry.objects.filter(assigned_object_type=row.content_type, assigned_object_id=row.object_id).count()
            == 2
        )

    def test_policy_journal_binds_the_store_to_the_settlement_cursor(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        # A store rebuild: the feed cursor moved to the new store, the read gate still holds the old one.
        NSODeviceManagement.objects.filter(pk=self._make_mgmt().pk).update(
            settle_cursor_incarnation="placeholder-policy-store", adapter_incarnation="placeholder-old-store"
        )
        management, row, execution, job = self.execution()
        _journal_route_policy_apply(management, job)
        assert (
            JournalEntry.objects.filter(assigned_object_type=row.content_type, assigned_object_id=row.object_id).count()
            == 1
        )

    def test_policy_journal_skips_evidence_from_a_store_the_cursor_left(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        NSODeviceManagement.objects.filter(pk=management.pk).update(settle_cursor_incarnation="placeholder-next-store")
        _journal_route_policy_apply(management, job)
        assert not JournalEntry.objects.exists()

    def test_policy_journal_rejects_wrong_execution_identity_or_result(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        for field, value in (("store_incarnation", "other-store"), ("generation_id", 18), ("id", 24), ("result", {})):
            with self.subTest(field=field):
                wrong = {**job, field: value}
                _journal_route_policy_apply(management, wrong)
                assert not JournalEntry.objects.exists()

    def test_superseded_and_unavailable_policy_never_journals_success(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        for disposition in ("superseded", "unavailable"):
            execution.overlay_snapshots.update(settlement_disposition=disposition)
            _journal_route_policy_apply(management, job)
            assert not JournalEntry.objects.exists()

    def test_policy_entry_refusal_journals_only_frozen_object_catalog_reason(self):
        import json

        from netbox_nso_plugin.reconcile import _journal_route_policy_apply
        from netbox_nso_plugin.refusal_protocol import refusal, render_refusals, row_ref

        management, row, execution, job = self.execution(disposition="refused")
        entry = row_ref(
            "route-policy", "route-policy.community-list.entry", (("community-list", ("CLJ",)), ("entry", (1,)))
        )
        cause = refusal("route-policy", entry, "route-policy.deny-entry-unsupported")
        from netbox_nso_plugin.refusal_protocol import PREFIX

        record = json.loads(render_refusals([cause]).removeprefix(PREFIX))["refusals"][0]
        result = job["result"]
        result["row_outcomes"][0]["refusals"] = [record]
        identity = {
            key: value for key, value in result["row_outcomes"][0].items() if key not in {"disposition", "refusals"}
        }
        result["refusals"] = [{**record, "logical_rows": [identity], "provenance": "current", "sender_phase": "commit"}]
        result["route_policy_count_by_outcome"]["apply_failed"] = 1
        execution.terminal_projection = {"carrier_job_result": copy.deepcopy(result)}
        execution.save(update_fields=("terminal_projection",))
        _journal_route_policy_apply(management, job)
        entries = JournalEntry.objects.filter(assigned_object_type=row.content_type, assigned_object_id=row.object_id)
        assert entries.count() == 1
        assert entries.get().kind == "danger"
        assert f"{cause.reason}: {cause.message}" in entries.get().comments
        assert JournalEntry.objects.count() == 2

    def test_policy_journal_rolls_back_partial_writes_before_retry(self):
        from django.db import connection

        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        inserts = 0

        def fail_device_insert(execute, sql, params, many, context):
            nonlocal inserts
            if 'INSERT INTO "extras_journalentry"' in sql:
                inserts += 1
                if inserts == 2:
                    raise RuntimeError("journal write unavailable")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_device_insert), self.assertRaises(RuntimeError):
            _journal_route_policy_apply(management, job)
        assert not JournalEntry.objects.exists()
        management.refresh_from_db()
        assert management.last_journaled_apply_job == ""
        _journal_route_policy_apply(management, job)
        assert JournalEntry.objects.count() == 2

    def test_policy_journal_rejects_changed_mapping(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, execution, job = self.execution()
        type(management).objects.filter(pk=management.pk).update(
            adapter_source_epoch=(management.adapter_source_epoch or 0) + 1
        )
        management.refresh_from_db()
        _journal_route_policy_apply(management, job)
        assert not JournalEntry.objects.exists()

    def policy_terminal(self):
        import json
        from datetime import timedelta
        from types import SimpleNamespace

        from dcim.models import Device
        from django.db import transaction

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.durable_capture import acknowledge_claim_capture, capture_claim
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.refusal_protocol import PREFIX, refusal, render_refusals, row_ref

        from ._outbox_case import mirror_update

        management = self._make_mgmt()
        self._owned_reconcile()
        for index, owner in enumerate(NSORoutePolicyState.objects.filter(management=management).order_by("pk")):
            mirror_update(
                owner,
                last_apply_at=timezone.now() - timedelta(days=index + 1),
                last_apply_error=f"prior policy error {owner.family}",
            )
        prior = {
            row.pk: (row.status, row.last_apply_at, row.last_apply_error, row.apply_attempt_id)
            for row in NSORoutePolicyState.objects.filter(management=management)
        }
        row = NSORoutePolicyState.objects.get(management=management, family="community_list")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="route_policy").revision
        at = timezone.now()
        with transaction.atomic():
            Device.objects.select_for_update().get(pk=self.device.pk)
            type(management).objects.select_for_update().get(pk=management.pk)
            NSOIntentRevision.objects.select_for_update().get(device=self.device, scope="route_policy")
            rendered = delivery.render("route_policy", self.device.pk, management.adapter_device_id)
            selection = capture_claim(management, rendered, 41, revision, delivery.MODE_NORMAL, (), None)
            claim = SimpleNamespace(
                device_id=self.device.pk,
                scope="route_policy",
                push_seq=41,
                payload=rendered.payload,
                revision=revision,
                deletions=(),
                marking_mode=None,
                rendered=rendered,
            )
            acknowledge_claim_capture(claim, {}, at)
        selection.refresh_from_db()
        entry = row_ref(
            "route-policy",
            "route-policy.community-list.entry",
            (("community-list", (row.object_name,)), ("entry", (1,))),
        )
        cause = refusal("route-policy", entry, "route-policy.deny-entry-unsupported")
        record = json.loads(render_refusals([cause]).removeprefix(PREFIX))["refusals"][0]
        outcomes, carriers = [], []
        for owner in selection.contributor_manifest:
            contributor = owner["contributors"][0]
            ref = contributor["logical_ref"]
            identity = {
                "stream": "route_policy",
                "row": {key: ref[key] for key in ("subject", "key")},
                "source_push_seq": 41,
                "frozen_revision": 7,
                "generation_id": 17,
                "settlement_cohort": 9,
            }
            carrier = {**identity, "accepted_at": None}
            disposition = "refused" if owner["overlay_pk"] == row.pk else "not_applied"
            outcomes.append(
                {
                    **identity,
                    "acceptance_identity": None,
                    "carrier": carrier,
                    "disposition": disposition,
                    "refusals": [record] if disposition == "refused" else [],
                }
            )
            carriers.append({**carrier, "disposition": disposition})
        refused = next(item for item in outcomes if item["disposition"] == "refused")
        attributed = {
            **record,
            "logical_rows": [{key: value for key, value in refused.items() if key not in {"disposition", "refusals"}}],
            "provenance": "current",
            "sender_phase": "pre_commit_dry_run",
        }
        result = {
            "row_outcomes": outcomes,
            "refusals": [attributed],
            "settlement_owners": carriers,
            "commit_acknowledged": False,
            "route_policy_count_by_outcome": {"in_sync": 0, "apply_failed": 1, "not_applied": 3, "unavailable": 0},
        }
        generation = {
            "generation_id": 17,
            "seq": 9,
            "status": "failed",
            "sections": ["route_policy"],
            "stream_revisions": {"route_policy": 7},
            "source_push_seq": {"route_policy": 41},
            "carrier_job_id": 23,
            "carrier_job_status": "succeeded",
            "carrier_job_result": result,
            "carrier_job_error": None,
            "updated_at": at.isoformat(),
            "row_outcomes": outcomes,
            "refusals": [attributed],
        }
        return management, row, prior, result, generation, cause

    def test_policy_entry_cause_settles_only_owning_object(self):
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        management, row, prior, result, generation, cause = self.policy_terminal()
        execution = settle_terminal_generation(management, generation, "placeholder-policy-store", settle_seq=1)
        assert execution.consumption_state == "consumed"
        row.refresh_from_db()
        assert row.status == "apply_failed"
        assert row.last_apply_error == f"{cause.reason}: {cause.message}"
        assert row.last_apply_at == prior[row.pk][1]
        for sibling in NSORoutePolicyState.objects.filter(management=management).exclude(pk=row.pk):
            assert (sibling.status, sibling.last_apply_at, sibling.last_apply_error, sibling.apply_attempt_id) == prior[
                sibling.pk
            ]
        job = {"id": 23, "generation_id": 17, "store_incarnation": "placeholder-policy-store", "result": result}
        _journal_route_policy_apply(management, job)
        assert JournalEntry.objects.count() == 2
        object_entry = JournalEntry.objects.get(assigned_object_type=row.content_type, assigned_object_id=row.object_id)
        assert object_entry.kind == "danger"
        assert cause.message in object_entry.comments

    def test_policy_cause_for_unselected_entry_fails_closed(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError
        from netbox_nso_plugin.outcome_settlement import settle_terminal_generation
        from netbox_nso_plugin.refusal_protocol import row_ref

        management, row, prior, result, generation, cause = self.policy_terminal()
        missing = row_ref(
            "route-policy",
            "route-policy.community-list.entry",
            (("community-list", (row.object_name,)), ("entry", (999,))),
        )
        generation["refusals"][0]["row"].update(subject=missing.subject, key=missing.key)
        with self.assertRaises(EvidenceInvariantError):
            settle_terminal_generation(management, generation, "placeholder-policy-store", settle_seq=1)
        for owner in NSORoutePolicyState.objects.filter(management=management):
            assert (owner.status, owner.last_apply_at, owner.last_apply_error, owner.apply_attempt_id) == prior[
                owner.pk
            ]
        assert not JournalEntry.objects.exists()
        execution = NSOApplyExecution.objects.get()
        assert execution.consumption_state == "unconsumed"
        assert execution.invariant_code

    def policy_adapter(self):
        from unittest.mock import patch

        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import AdapterConnection

        adapter = FakeAdapter()
        self.addCleanup(adapter.stop)
        for target, value in (
            ("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession),
            ("netbox_nso_plugin.reconcile.reconcile_device", lambda *args, **kwargs: {}),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        adapter_client.reset_session()
        self.addCleanup(adapter_client.reset_session)
        AdapterConnection.objects.all().delete()
        AdapterConnection.objects.create(url=adapter.url, enabled=True, timeout_seconds=10)
        adapter_client.reset_config_cache()
        self.addCleanup(adapter_client.reset_config_cache)
        adapter.store.add_device(nso_instance="rpj-inst", nso_device_name="rpj-dev", device_id=777)
        return adapter

    def policy_carrier(self, adapter, result, *, generation_id=17, seq=9):
        job = adapter.store.terminal_job(777, status="succeeded", extra=result)
        adapter.store.add_generation(
            777,
            generation_id=generation_id,
            seq=seq,
            status="failed",
            job_id=job["id"],
            carrier_job_id=job["id"],
            carrier_job_status=job["status"],
            mode="networked",
            settlement_cohort=seq,
            digest="a" * 64,
            stream_revisions={"route_policy": 7},
            source_push_seq={"route_policy": 41},
        )
        return job

    def test_run_device_reconcile_journals_the_route_policy_carrier(self):
        from netbox_nso_plugin.delivery import canonical_fingerprint
        from netbox_nso_plugin.reconcile import run_device_reconcile

        management, row, prior, result, generation, cause = self.policy_terminal()
        adapter = self.policy_adapter()
        job = self.policy_carrier(adapter, result)

        run_device_reconcile(self.device.pk)

        object_entry = JournalEntry.objects.get(assigned_object_type=row.content_type, assigned_object_id=row.object_id)
        assert object_entry.kind == "danger"
        assert f"adapter job #{job['id']}." in object_entry.comments
        execution = NSOApplyExecution.objects.get(carrier_job_id=job["id"], consumption_state="consumed")
        management.refresh_from_db()
        assert management.last_journaled_apply_job == canonical_fingerprint(
            (management.pk, execution.mapping_epoch, adapter.store.incarnation, 17, job["id"])
        )

    def test_run_device_reconcile_journals_every_carrier_of_one_feed_page(self):
        from netbox_nso_plugin.reconcile import run_device_reconcile

        management, row, prior, result, generation, cause = self.policy_terminal()
        adapter = self.policy_adapter()
        retry = copy.deepcopy(result)
        for item in (*retry["row_outcomes"], *retry["settlement_owners"], *retry["refusals"][0]["logical_rows"]):
            item["generation_id"] = 18
            item["settlement_cohort"] = 10
            if "carrier" in item:
                item["carrier"].update(generation_id=18, settlement_cohort=10)
        first = self.policy_carrier(adapter, result)
        second = self.policy_carrier(adapter, retry, generation_id=18, seq=10)

        run_device_reconcile(self.device.pk)

        consumed = NSOApplyExecution.objects.filter(consumption_state="consumed")
        assert set(consumed.values_list("carrier_job_id", flat=True)) == {first["id"], second["id"]}
        comments = list(
            JournalEntry.objects.filter(
                assigned_object_type=row.content_type, assigned_object_id=row.object_id
            ).values_list("comments", flat=True)
        )
        assert len(comments) == 2
        for job in (first, second):
            assert any(f"adapter job #{job['id']}." in text for text in comments), job["id"]
