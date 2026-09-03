# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""READSEM S4 Slice B2 — the D5 read-state gate: model, transitions, incarnation
adoption, the device-wide redis lease, and the per-call-class contention policies.

The COMPLETE red-first list from the ratified plan's D5 (rev 17): transition-table
rows (incl. body-failure-after-admission), lease atomicity/lifecycle (Lua
compare-and-extend/-delete, successor protection, loud loss), both contention
directions (web fail-fast; RQ retry → marker handoff with nonce successor ids —
the R9-1 and R10-1 scenarios, GETDEL exactly-one), aggregate-observation protocol
(observed-only, 12-vs-11, reset-pending markers, the R13 monotonic marker, the
R15 equal-born sequences and the R16 A@10/B@20/C@20/D@30 crossed sequence),
adoption ordering, and redis-down fail-closed.

Redis tests follow the repo's real-behavior convention (test_reconcile.py's orphan
suite): the CONFIGURED redis connection, but uuid-isolated keys/queues no worker
consumes, cleaned up in tearDown.
"""

import os
import threading
import time
import uuid
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance
from netbox_nso_plugin.tests.mixins import _CascadeFlushMixin

from ._outbox_case import mirror_update


def _make_device(name):
    mfg = Manufacturer.objects.create(name=f"{name}-mfg", slug=f"{name}-mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{name}-dt", slug=f"{name}-dt")
    role = DeviceRole.objects.create(name=f"{name}-role", slug=f"{name}-role")
    site = Site.objects.create(name=f"{name}-site", slug=f"{name}-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _make_mgmt(device, adapter_device_id=None):
    inst, _ = NSOInstance.objects.get_or_create(name="rg-inst", defaults={"adapter_instance_id": "rg-inst"})
    return NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=device.name,
        adapter_device_id=adapter_device_id if adapter_device_id is not None else device.pk,
    )


#: Incarnation fixtures — UUIDs with distinct borns (the born ORDER is what matters).
_INC_A = ("11111111-aaaa-4aaa-8aaa-111111111111", "2026-07-01T00:00:10Z")
_INC_B = ("22222222-bbbb-4bbb-8bbb-222222222222", "2026-07-01T00:00:20Z")
_INC_C = ("33333333-cccc-4ccc-8ccc-333333333333", "2026-07-01T00:00:20Z")  # equal-born vs B
_INC_D = ("44444444-dddd-4ddd-8ddd-444444444444", "2026-07-01T00:00:30Z")
_DEFAULT_REVISION = object()


def _rs(
    outcome="present",
    reason=None,
    freshness="fresh",
    result="replaced",
    succeeded=True,
    attempt_id=1,
    incarnation=_INC_A[0],
    incarnation_born=_INC_A[1],
    read_at="2026-07-21T10:00:00Z",
    source_epoch=1,
    payload_revision=_DEFAULT_REVISION,
):
    """A wire read_state block (D3 shape)."""
    return {
        "outcome": outcome,
        "reason": reason,
        "freshness": freshness,
        "result": result,
        "succeeded": succeeded,
        "read_at": read_at,
        "attempt_id": attempt_id,
        "incarnation": incarnation,
        "incarnation_born": incarnation_born,
        "source_epoch": source_epoch,
        "payload_revision": attempt_id if payload_revision is _DEFAULT_REVISION else payload_revision,
    }


def _synth(incarnation=_INC_A[0], incarnation_born=_INC_A[1]):
    """The adapter's synthesized not_ready block (attempt_id null, carries the pair)."""
    return _rs(
        outcome="unavailable",
        reason="not_ready",
        freshness=None,
        result=None,
        succeeded=None,
        attempt_id=None,
        incarnation=incarnation,
        incarnation_born=incarnation_born,
        read_at=None,
    )


class _Recorder:
    """A gate body that records calls and returns a marker value."""

    def __init__(self, value="body-value", exc=None):
        self.calls = 0
        self.value = value
        self.exc = exc

    def __call__(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.value


def _redis():
    import django_rq

    return django_rq.get_queue("default").connection


_SYNC_RECONCILE_CALLS: list = []


def _sync_reconcile_recorder(device_id):
    """A REAL (picklable) stand-in for run_device_reconcile, so a sync RQ queue can persist + run it
    inline. A MagicMock cannot be pickled by RQ's sync-queue job store."""
    _SYNC_RECONCILE_CALLS.append(device_id)
    return {"ok": True}


def _noop_job():
    """RQ needs an importable (module-level) function for enqueued fixtures."""


def _close_thread_db():
    """Close this worker thread's Django DB connections (TransactionTestCase hygiene)."""
    from django.db import connections

    connections.close_all()


# ---------------------------------------------------------------------------
# Model schema
# ---------------------------------------------------------------------------


class TestFamilyReadStateModel(TestCase):
    def test_unique_per_management_family(self):
        from netbox_nso_plugin.models import NSOFamilyReadState

        mgmt = _make_mgmt(_make_device("rg-uniq"))
        NSOFamilyReadState.objects.create(management=mgmt, family="bfd")
        with self.assertRaises(IntegrityError):
            NSOFamilyReadState.objects.create(management=mgmt, family="bfd")

    def test_management_reset_marker_fields_exist(self):
        mgmt = _make_mgmt(_make_device("rg-flds"))
        self.assertEqual(mgmt.adapter_incarnation, "")
        self.assertIsNone(mgmt.adapter_incarnation_born)
        self.assertEqual(mgmt.reset_pending_incarnation, "")
        self.assertIsNone(mgmt.reset_pending_born)
        self.assertIsNone(mgmt.reset_conflict_born)


# ---------------------------------------------------------------------------
# Transition table (D5, R6-2 — one table, no prose drift)
# ---------------------------------------------------------------------------


class TestGateTransitions(TestCase):
    def setUp(self):
        self.mgmt = _make_mgmt(_make_device(f"rg-tr-{uuid.uuid4().hex[:8]}"))
        self.epoch = self.mgmt.adapter_device_id

    def _run(self, read_state, body=None, family="bfd"):
        from netbox_nso_plugin.read_gate import gated_family_run

        body = body or _Recorder()
        result = gated_family_run(self.mgmt, family, read_state, body, epoch=self.epoch)
        return result, body

    def _row(self, family="bfd"):
        from netbox_nso_plugin.models import NSOFamilyReadState

        return NSOFamilyReadState.objects.get(management=self.mgmt, family=family)

    def test_admit_advances_observed_and_applied_and_runs_body(self):
        from netbox_nso_plugin.read_gate import RAN

        result, body = self._run(_rs(attempt_id=7))
        self.assertEqual(result.disposition, RAN)
        self.assertEqual(result.value, "body-value")
        self.assertEqual(body.calls, 1)
        row = self._row()
        self.assertEqual(row.observed_outcome, "present")
        self.assertEqual(row.observed_attempt_id, 7)
        self.assertEqual(row.applied_attempt_id, 7)
        self.assertEqual(row.applied_incarnation, _INC_A[0])
        self.assertEqual(row.observed_epoch, self.epoch)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_incarnation, _INC_A[0])

    def test_admitted_mirror_body_does_not_advance_intent_revision(self):
        from netbox_nso_plugin.models import NSOIntentRevision

        revision, _ = NSOIntentRevision.objects.get_or_create(
            device=self.mgmt.device,
            scope="bfd",
        )
        before = revision.revision

        self._run(
            _rs(attempt_id=71),
            body=lambda: mirror_update(self.mgmt, last_sync_status="success"),
        )

        self.mgmt.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(self.mgmt.last_sync_status, "success")
        self.assertEqual(revision.revision, before)

    def test_absent_authoritative_cleared_admits(self):
        from netbox_nso_plugin.read_gate import RAN

        result, body = self._run(_rs(outcome="absent_authoritative", freshness=None, result="cleared", attempt_id=3))
        self.assertEqual(result.disposition, RAN)
        self.assertEqual(body.calls, 1)
        self.assertEqual(self._row().applied_attempt_id, 3)

    def test_stale_freshness_still_admits(self):
        from netbox_nso_plugin.read_gate import RAN

        result, _ = self._run(_rs(freshness="stale", attempt_id=4))
        self.assertEqual(result.disposition, RAN)
        self.assertEqual(self._row().observed_freshness, "stale")

    def test_unavailable_advances_observed_only_and_skips_body(self):
        from netbox_nso_plugin.read_gate import SKIPPED, SKIPPED_UNAVAILABLE

        result, body = self._run(
            _rs(
                outcome="unavailable",
                reason="export_down",
                freshness=None,
                result="kept",
                succeeded=False,
                attempt_id=5,
            )
        )
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertIs(result.value, SKIPPED)
        self.assertEqual(body.calls, 0)
        row = self._row()
        self.assertEqual(row.observed_outcome, "unavailable")
        self.assertEqual(row.observed_attempt_id, 5)
        self.assertIsNone(row.applied_attempt_id)

    def test_result_error_fails_closed(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE

        result, body = self._run(_rs(result="error", succeeded=False, attempt_id=5))
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body.calls, 0)
        self.assertIsNone(self._row().applied_attempt_id)

    def test_unknown_future_outcome_fails_closed(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE

        result, body = self._run(_rs(outcome="quantum_flux", attempt_id=5))
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body.calls, 0)
        self.assertEqual(self._row().observed_outcome, "quantum_flux")

    def test_succeeded_null_fails_closed(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE

        result, body = self._run(_rs(succeeded=None, attempt_id=5))
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body.calls, 0)

    def test_strictly_older_than_applied_skips_everything(self):
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=9))
        result, body = self._run(_rs(attempt_id=8, freshness="stale"))
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        row = self._row()
        self.assertEqual(row.observed_attempt_id, 9)  # nothing advanced
        self.assertEqual(row.observed_freshness, "fresh")
        self.assertEqual(row.applied_attempt_id, 9)

    def test_equal_to_applied_reruns_body_and_refreshes_observed(self):
        from netbox_nso_plugin.read_gate import RAN

        self._run(_rs(attempt_id=9))
        result, body = self._run(_rs(attempt_id=9, freshness="aged"))
        self.assertEqual(result.disposition, RAN)
        self.assertEqual(body.calls, 1)
        row = self._row()
        self.assertEqual(row.observed_freshness, "aged")  # refreshed
        self.assertEqual(row.applied_attempt_id, 9)

    def test_explicit_null_payload_revision_is_a_valid_cleared_publication(self):
        from netbox_nso_plugin.read_gate import RAN

        result, body = self._run(
            _rs(
                outcome="absent_authoritative",
                freshness=None,
                result="cleared",
                attempt_id=3,
                payload_revision=None,
            )
        )
        self.assertEqual(result.disposition, RAN)
        self.assertEqual(body.calls, 1)
        self.assertIsNone(self._row().applied_payload_revision)

    def test_body_failure_after_admission_keeps_applied_truthful(self):
        boom = RuntimeError("materializer failed")
        with self.assertRaises(RuntimeError):
            self._run(_rs(attempt_id=7), body=_Recorder(exc=boom))
        row = self._row()
        self.assertEqual(row.admitted_attempt_id, 7)
        self.assertIsNone(row.applied_attempt_id)
        self.assertNotEqual(row.publication_sequence, row.applied_publication_sequence)

    def test_plan_failure_carries_the_publication_guard(self):
        from netbox_nso_plugin.read_gate import gated_family_run

        boom = RuntimeError("plan failed")
        with self.assertRaises(RuntimeError) as raised:
            gated_family_run(
                self.mgmt,
                "bfd",
                _rs(attempt_id=8),
                _Recorder(),
                epoch=self.epoch,
                pre_body=lambda: (_ for _ in ()).throw(boom),
            )

        self.assertEqual(raised.exception._nso_publication_guard[0], "bfd")

    def test_changed_renderer_targets_skip_the_admitted_publication(self):
        from netbox_nso_plugin.intent_state import MutationFootprint, ReconcileMutationPlan, RendererTargetsChanged
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT, gated_family_run

        def targets_changed():
            raise RendererTargetsChanged("renderer targets changed during acquisition")

        body = _Recorder()
        plan = ReconcileMutationPlan(
            MutationFootprint.for_keys({(self.mgmt.device_id, "bfd")}),
            validate_after_acquire=targets_changed,
        )
        result = gated_family_run(
            self.mgmt,
            "bfd",
            _rs(attempt_id=8),
            body,
            epoch=self.epoch,
            pre_body=lambda: plan,
        )

        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        self.assertIsNone(self._row().applied_attempt_id)

    def test_late_synthesized_null_loses(self):
        self._run(_rs(attempt_id=5))
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        result, body = self._run(_synth())
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        row = self._row()
        self.assertEqual(row.observed_attempt_id, 5)
        self.assertEqual(row.observed_outcome, "present")  # not regressed

    def test_synthesized_on_fresh_row_records_not_ready(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE

        result, body = self._run(_synth())
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body.calls, 0)
        row = self._row()
        self.assertEqual(row.observed_outcome, "unavailable")
        self.assertEqual(row.observed_reason, "not_ready")
        self.assertIsNone(row.observed_attempt_id)

    def test_epoch_mismatch_writes_nothing(self):
        from netbox_nso_plugin.models import NSOFamilyReadState
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT, gated_family_run

        body = _Recorder()
        result = gated_family_run(self.mgmt, "bfd", _rs(attempt_id=7), body, epoch=self.epoch + 1)
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        self.assertFalse(NSOFamilyReadState.objects.filter(management=self.mgmt, family="bfd").exists())

    def test_pending_source_rekey_fails_closed_before_admission(self):
        from netbox_nso_plugin.models import NSOFamilyReadState
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE

        mirror_update(self.mgmt, source_rekey_pending=True)
        result, body = self._run(_rs(attempt_id=7))
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body.calls, 0)
        self.assertFalse(NSOFamilyReadState.objects.filter(management=self.mgmt, family="bfd").exists())

    def test_admitted_body_is_fenced_against_a_newer_applied_attempt(self):
        """codex B5-F2: worker A admits attempt 5, stalls before its body runs, and a
        successor admits AND materializes attempt 6. When A resumes, the body fence
        must refuse A's stale body — otherwise the overlays regress to attempt-5 data
        while the read-state row still claims 6."""
        import netbox_nso_plugin.read_gate as read_gate
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        real = read_gate._gate_and_record
        raced = {"done": False}

        def racing(mgmt, family, read_state, *, epoch):
            decision = real(mgmt, family, read_state, epoch=epoch)
            if not raced["done"]:
                raced["done"] = True
                # B runs to completion in A's stall window (after A's admission commit)
                read_gate.gated_family_run(mgmt, family, _rs(attempt_id=6), _Recorder(), epoch=epoch)
            return decision

        body_a = _Recorder()
        with patch.object(read_gate, "_gate_and_record", side_effect=racing):
            result = read_gate.gated_family_run(self.mgmt, "bfd", _rs(attempt_id=5), body_a, epoch=self.epoch)
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body_a.calls, 0)
        self.assertEqual(self._row().applied_attempt_id, 6)

    def test_admitted_body_is_fenced_across_incarnations(self):
        """codex B5-R2-1: attempts RESTART after a store rebuild, so an attempt-id-only
        fence admits a stale body when a successor ADOPTED a newer incarnation and
        applied the SAME attempt number. The fence must compare the incarnation too."""
        import netbox_nso_plugin.read_gate as read_gate
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        real = read_gate._gate_and_record
        raced = {"done": False}

        def racing(mgmt, family, read_state, *, epoch):
            decision = real(mgmt, family, read_state, epoch=epoch)
            if not raced["done"]:
                raced["done"] = True
                # B adopts the NEWER incarnation and applies the SAME attempt number
                read_gate.gated_family_run(
                    mgmt,
                    family,
                    _rs(attempt_id=5, incarnation=_INC_B[0], incarnation_born=_INC_B[1]),
                    _Recorder(),
                    epoch=epoch,
                )
            return decision

        body_a = _Recorder()
        with patch.object(read_gate, "_gate_and_record", side_effect=racing):
            result = read_gate.gated_family_run(self.mgmt, "bfd", _rs(attempt_id=5), body_a, epoch=self.epoch)
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body_a.calls, 0)
        row = self._row()
        self.assertEqual(row.applied_incarnation, _INC_B[0])
        self.assertEqual(row.applied_attempt_id, 5)

    def test_aggregate_source_ratchet_fences_an_admitted_old_source_body(self):
        import netbox_nso_plugin.read_gate as read_gate
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=1, source_epoch=1))
        real = read_gate._gate_and_record
        raced = {"done": False}

        def racing(mgmt, family, read_state, *, epoch):
            decision = real(mgmt, family, read_state, epoch=epoch)
            if not raced["done"]:
                raced["done"] = True
                read_gate.observe_aggregate(
                    mgmt,
                    {"bfd": _rs(attempt_id=1, source_epoch=2)},
                    epoch=epoch,
                )
            return decision

        body = _Recorder()
        with patch.object(read_gate, "_gate_and_record", side_effect=racing):
            result = read_gate.gated_family_run(
                self.mgmt,
                "bfd",
                _rs(attempt_id=2, source_epoch=1),
                body,
                epoch=self.epoch,
            )
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        self.assertEqual(self._row().applied_attempt_id, 1)

    def test_aggregate_pending_source_rejects_a_new_old_epoch_admission(self):
        import netbox_nso_plugin.read_gate as read_gate
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=1, source_epoch=1))
        read_gate.observe_aggregate(
            self.mgmt,
            {"bfd": _rs(attempt_id=1, source_epoch=2)},
            epoch=self.epoch,
        )

        result, body = self._run(_rs(attempt_id=2, source_epoch=1))
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        self.assertEqual(self._row().applied_attempt_id, 1)

    def test_legacy_key_absent_fails_after_epoch_ratchet(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE

        self._run(_rs(attempt_id=7))
        result, body = self._run(None)
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body.calls, 0)
        row = self._row()
        self.assertEqual(row.observed_outcome, "present")

    def test_legacy_key_absent_runs_before_epoch_ratchet(self):
        from netbox_nso_plugin.read_gate import LEGACY

        result, body = self._run(None)
        self.assertEqual(result.disposition, LEGACY)
        self.assertEqual(body.calls, 1)

    def test_first_explicit_source_epoch_resets_other_legacy_family_rows(self):
        self._run(None, family="ospf")
        legacy_row = self._row("ospf")
        prior_sequence = legacy_row.publication_sequence

        self._run(_rs(attempt_id=1), family="bfd")

        legacy_row.refresh_from_db()
        self.assertEqual(legacy_row.admitted_incarnation, "")
        self.assertIsNone(legacy_row.applied_attempt_id)
        self.assertGreater(legacy_row.publication_sequence, prior_sequence)


# ---------------------------------------------------------------------------
# Incarnation adoption / reset markers (R4-2, R13-1..R16-1)
# ---------------------------------------------------------------------------


class TestIncarnationAdoption(TestCase):
    def setUp(self):
        self.mgmt = _make_mgmt(_make_device(f"rg-inc-{uuid.uuid4().hex[:8]}"))
        self.epoch = self.mgmt.adapter_device_id

    def _run(self, read_state, family="bfd", body=None):
        from netbox_nso_plugin.read_gate import gated_family_run

        body = body or _Recorder()
        return gated_family_run(self.mgmt, family, read_state, body, epoch=self.epoch), body

    def _observe(self, families):
        from netbox_nso_plugin.read_gate import observe_aggregate

        return observe_aggregate(self.mgmt, families, epoch=self.epoch)

    def _row(self, family="bfd"):
        from netbox_nso_plugin.models import NSOFamilyReadState

        return NSOFamilyReadState.objects.get(management=self.mgmt, family=family)

    def test_new_incarnation_starts_a_new_source_epoch_domain(self):
        from netbox_nso_plugin.read_gate import RAN

        result, _ = self._run(_rs(attempt_id=5, source_epoch=5))
        self.assertEqual(result.disposition, RAN)
        result, body = self._run(
            _rs(
                attempt_id=1,
                source_epoch=1,
                incarnation=_INC_B[0],
                incarnation_born=_INC_B[1],
            )
        )
        self.assertEqual(result.disposition, RAN)
        self.assertEqual(body.calls, 1)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_incarnation, _INC_B[0])
        self.assertEqual(self.mgmt.adapter_source_epoch, 1)

    def _mgmt(self):
        self.mgmt.refresh_from_db()
        return self.mgmt

    def test_first_contact_adopts(self):
        self._run(_rs(attempt_id=1))
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_A[0])
        self.assertIsNotNone(m.adapter_incarnation_born)

    def test_newer_born_adopts_and_resets_all_family_rows(self):
        self._run(_rs(attempt_id=5), family="bfd")
        self._run(_rs(attempt_id=6), family="ospf")
        result, body = self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="bfd")
        from netbox_nso_plugin.read_gate import RAN

        self.assertEqual(result.disposition, RAN)
        self.assertEqual(body.calls, 1)
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_B[0])
        # the OTHER family's row was reset to unknown by the adoption
        other = self._row("ospf")
        self.assertEqual(other.observed_outcome, "")
        self.assertIsNone(other.observed_attempt_id)
        self.assertIsNone(other.applied_attempt_id)
        self.assertEqual(other.applied_incarnation, "")
        # the adopting family's row carries the new incarnation
        row = self._row("bfd")
        self.assertEqual(row.observed_incarnation, _INC_B[0])
        self.assertEqual(row.applied_attempt_id, 1)

    def test_new_incarnation_invalidates_every_delivery_baseline(self):
        """An adopted adapter store creates audit work for every delivery scope."""
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision

        self._run(_rs(attempt_id=5), family="bfd")
        scopes = tuple(delivery.delivery_keys())
        self.assertTrue(scopes)
        for scope in scopes:
            NSOIntentRevision.objects.update_or_create(
                device=self.mgmt.device,
                scope=scope,
                defaults={
                    "revision": 3,
                    "verified_revision": 3,
                    "verified_fingerprint": f"verified-{scope}",
                    "verified_at": timezone.now(),
                },
            )

        self._run(
            _rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]),
            family="bfd",
        )

        self.assertFalse(
            NSOIntentRevision.objects.filter(
                device=self.mgmt.device,
                verified_revision__isnull=False,
            ).exists()
        )

    def test_adoption_with_existing_rows_keeps_reset_marker_until_all_reobserved(self):
        """codex B5-F1: adoption blanks every family row; until each one re-observes
        under the adopted incarnation, old overlay rows must not render healthy —
        the device-wide reset marker has to SURVIVE the adoption."""
        self._run(_rs(attempt_id=5), family="bfd")
        self._run(_rs(attempt_id=6), family="ospf")
        self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="bfd")
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_B[0])
        self.assertIsNotNone(m.reset_pending_born)
        self.assertEqual(m.reset_pending_incarnation, _INC_B[0])

    def test_reset_marker_clears_when_last_blank_family_reobserves(self):
        self._run(_rs(attempt_id=5), family="bfd")
        self._run(_rs(attempt_id=6), family="ospf")
        self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="bfd")
        self._run(_rs(attempt_id=2, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="ospf")
        m = self._mgmt()
        self.assertIsNone(m.reset_pending_born)
        self.assertEqual(m.reset_pending_incarnation, "")

    def test_observe_aggregate_completing_reobservation_clears_marker(self):
        """The tab's aggregate observation also completes the re-observation: once no
        family row is blank the marker clears (per-family states take over)."""
        self._run(_rs(attempt_id=5), family="bfd")
        self._run(_rs(attempt_id=6), family="ospf")
        self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="bfd")
        self.assertIsNotNone(self._mgmt().reset_pending_born)
        self._observe({"ospf": _synth(incarnation=_INC_B[0], incarnation_born=_INC_B[1])})
        self.assertIsNone(self._mgmt().reset_pending_born)

    def test_newer_pending_marker_survives_older_adoption_completion(self):
        """Completing incarnation B's re-observation must not clear a pending marker
        that already points at a NEWER incarnation D."""
        self._run(_rs(attempt_id=5), family="bfd")
        self._run(_rs(attempt_id=6), family="ospf")
        self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="bfd")
        self._observe({"bfd": _synth(incarnation=_INC_D[0], incarnation_born=_INC_D[1])})
        self._run(_rs(attempt_id=2, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="ospf")
        m = self._mgmt()
        self.assertEqual(m.reset_pending_incarnation, _INC_D[0])
        self.assertIsNotNone(m.reset_pending_born)

    def test_fresh_device_adoption_sets_no_marker(self):
        """First contact (no pre-existing family rows) adopts clean — no reset chip."""
        self._run(_rs(attempt_id=1))
        self.assertIsNone(self._mgmt().reset_pending_born)

    def test_older_born_replay_rejected(self):
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=5, incarnation=_INC_B[0], incarnation_born=_INC_B[1]))
        result, body = self._run(_rs(attempt_id=99, incarnation=_INC_A[0], incarnation_born=_INC_A[1]))
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        row = self._row()
        self.assertEqual(row.applied_attempt_id, 5)
        self.assertEqual(row.observed_incarnation, _INC_B[0])
        self.assertEqual(self._mgmt().adapter_incarnation, _INC_B[0])

    def test_rebuild_convergence_from_both_gate_orders(self):
        # order 1: family bfd sees the new incarnation first, then ospf
        self._run(_rs(attempt_id=5), family="bfd")
        self._run(_rs(attempt_id=5), family="ospf")
        self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="bfd")
        self._run(_rs(attempt_id=2, incarnation=_INC_B[0], incarnation_born=_INC_B[1]), family="ospf")
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_B[0])
        self.assertEqual(self._row("bfd").applied_attempt_id, 1)
        self.assertEqual(self._row("ospf").applied_attempt_id, 2)
        # a replay of the PRIOR incarnation on either family is rejected
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        result, _ = self._run(_rs(attempt_id=50), family="ospf")  # _INC_A default
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(self._row("ospf").applied_attempt_id, 2)

    def test_equal_born_against_the_pending_marker_is_a_conflict_not_an_adoption(self):
        """codex B5-R2-2: A@10 adopted, B@20 recorded PENDING by an observation — a
        gated C@20 (equal born, different UUID vs the PENDING pair) must fail closed
        as a durable conflict, never adopt an ambiguous incarnation (R15 algebra)."""
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=5))  # adopt A@10
        self._observe({"bfd": _synth(incarnation=_INC_B[0], incarnation_born=_INC_B[1])})  # pending B@20
        result, body = self._run(_rs(attempt_id=1, incarnation=_INC_C[0], incarnation_born=_INC_C[1]))  # C@20
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_A[0])  # NOT adopted
        self.assertIsNotNone(m.reset_conflict_born)  # the collision is durable

    def test_equal_born_different_uuid_at_gate_sets_conflict_and_rejects(self):
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=5, incarnation=_INC_B[0], incarnation_born=_INC_B[1]))
        result, body = self._run(_rs(attempt_id=1, incarnation=_INC_C[0], incarnation_born=_INC_C[1]))
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_B[0])  # unchanged
        self.assertIsNotNone(m.reset_conflict_born)

    def test_conflict_blocks_equal_born_adoption_until_strictly_later(self):
        """R15 sequence (a): after an equal-born collision NO equal-born incarnation
        can adopt — only a strictly-later born resolves."""
        from netbox_nso_plugin.read_gate import RAN, SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=5, incarnation=_INC_B[0], incarnation_born=_INC_B[1]))
        self._run(_rs(attempt_id=1, incarnation=_INC_C[0], incarnation_born=_INC_C[1]))  # conflict@20
        # B itself (the adopted one, equal born) keeps running — same-uuid path:
        result, _ = self._run(_rs(attempt_id=6, incarnation=_INC_B[0], incarnation_born=_INC_B[1]))
        self.assertEqual(result.disposition, RAN)
        # C retries: still rejected
        result, _ = self._run(_rs(attempt_id=2, incarnation=_INC_C[0], incarnation_born=_INC_C[1]))
        self.assertEqual(result.disposition, SKIPPED_STALE_ATTEMPT)
        # a strictly-later born adopts and clears the conflict
        result, _ = self._run(_rs(attempt_id=1, incarnation=_INC_D[0], incarnation_born=_INC_D[1]))
        self.assertEqual(result.disposition, RAN)
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_D[0])
        self.assertIsNone(m.reset_conflict_born)
        self.assertEqual(m.reset_pending_incarnation, "")

    def test_crossed_sequence_A10_B20_C20_D30(self):
        """The R16 flagship: A adopted, B@20 observed (pending), C@20 observed
        (collision), D@30 observed — B and C can never adopt or run bodies;
        D's exact adoption resolves and clears both markers."""
        from netbox_nso_plugin.read_gate import RAN, SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=5))  # adopt A@10
        self._observe({"bfd": _rs(attempt_id=6, incarnation=_INC_B[0], incarnation_born=_INC_B[1])})
        m = self._mgmt()
        self.assertEqual(m.reset_pending_incarnation, _INC_B[0])
        self._observe({"bfd": _rs(attempt_id=6, incarnation=_INC_C[0], incarnation_born=_INC_C[1])})
        m = self._mgmt()
        self.assertIsNotNone(m.reset_conflict_born)
        self._observe({"bfd": _rs(attempt_id=6, incarnation=_INC_D[0], incarnation_born=_INC_D[1])})
        m = self._mgmt()
        self.assertEqual(m.reset_pending_incarnation, _INC_D[0])
        # B and C can never adopt (born 20 is not > conflict born 20)
        r, body = self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]))
        self.assertEqual(r.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        r, body = self._run(_rs(attempt_id=1, incarnation=_INC_C[0], incarnation_born=_INC_C[1]))
        self.assertEqual(r.disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(body.calls, 0)
        # D's exact adoption passes (30 > 20) and clears pending + conflict
        r, body = self._run(_rs(attempt_id=1, incarnation=_INC_D[0], incarnation_born=_INC_D[1]))
        self.assertEqual(r.disposition, RAN)
        self.assertEqual(body.calls, 1)
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_D[0])
        self.assertEqual(m.reset_pending_incarnation, "")
        self.assertIsNone(m.reset_pending_born)
        self.assertIsNone(m.reset_conflict_born)

    def test_exact_pending_match_cannot_clear_a_conflicted_marker(self):
        """R15 sequence (b): pending B@20 + conflict@20 → even B's EXACT adoption
        attempt fails (born not > conflict born); the device stays reset-pending."""
        from netbox_nso_plugin.read_gate import SKIPPED_STALE_ATTEMPT

        self._run(_rs(attempt_id=5))  # adopt A@10
        self._observe({"bfd": _rs(attempt_id=6, incarnation=_INC_B[0], incarnation_born=_INC_B[1])})
        self._observe({"bfd": _rs(attempt_id=6, incarnation=_INC_C[0], incarnation_born=_INC_C[1])})
        m = self._mgmt()
        pending_before = m.reset_pending_incarnation
        r, _ = self._run(_rs(attempt_id=1, incarnation=pending_before, incarnation_born=_INC_B[1]))
        self.assertEqual(r.disposition, SKIPPED_STALE_ATTEMPT)
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_A[0])
        self.assertNotEqual(m.reset_pending_incarnation, "")

    def test_monotonic_marker_delayed_older_observation_never_regresses(self):
        """R13: C@30 pending, then a DELAYED B@20 observation must not replace it;
        B's later adoption (20 > adopted 10) must NOT clear pending C — the device
        stays reset-pending until adoption reaches ≥ C's born."""
        from netbox_nso_plugin.read_gate import RAN

        self._run(_rs(attempt_id=5))  # adopt A@10
        self._observe({"bfd": _rs(attempt_id=1, incarnation=_INC_D[0], incarnation_born=_INC_D[1])})
        self._observe({"bfd": _rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1])})
        m = self._mgmt()
        self.assertEqual(m.reset_pending_incarnation, _INC_D[0])  # not regressed
        # B adopts (20 > 10, no conflict) but pending D survives (20 < 30)
        r, _ = self._run(_rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1]))
        self.assertEqual(r.disposition, RAN)
        m = self._mgmt()
        self.assertEqual(m.adapter_incarnation, _INC_B[0])
        self.assertEqual(m.reset_pending_incarnation, _INC_D[0])
        # only adoption ≥ C/D born clears
        r, _ = self._run(_rs(attempt_id=1, incarnation=_INC_D[0], incarnation_born=_INC_D[1]))
        self.assertEqual(r.disposition, RAN)
        self.assertEqual(self._mgmt().reset_pending_incarnation, "")


# ---------------------------------------------------------------------------
# Aggregate observation protocol (R6-3)
# ---------------------------------------------------------------------------


class TestAggregateObservation(TestCase):
    def setUp(self):
        self.mgmt = _make_mgmt(_make_device(f"rg-agg-{uuid.uuid4().hex[:8]}"))
        self.epoch = self.mgmt.adapter_device_id

    def _adopt(self, attempt_id=1):
        from netbox_nso_plugin.read_gate import gated_family_run

        gated_family_run(self.mgmt, "bfd", _rs(attempt_id=attempt_id), _Recorder(), epoch=self.epoch)

    def _observe(self, families, epoch=None):
        from netbox_nso_plugin.read_gate import observe_aggregate

        return observe_aggregate(self.mgmt, families, epoch=self.epoch if epoch is None else epoch)

    def _row(self, family="bfd"):
        from netbox_nso_plugin.models import NSOFamilyReadState

        return NSOFamilyReadState.objects.get(management=self.mgmt, family=family)

    def test_observed_advances_applied_untouched(self):
        """The R5-3 scenario: aggregate observes 12 while applied is 11 —
        the row ends (observed=12, applied=11)."""
        self._adopt(attempt_id=11)
        wrote = self._observe({"bfd": _rs(attempt_id=12, freshness="aged")})
        self.assertTrue(wrote)
        row = self._row()
        self.assertEqual(row.observed_attempt_id, 12)
        self.assertEqual(row.observed_freshness, "aged")
        self.assertEqual(row.applied_attempt_id, 11)

    def test_older_observation_never_regresses(self):
        self._adopt(attempt_id=11)
        self._observe({"bfd": _rs(attempt_id=12)})
        self._observe({"bfd": _rs(attempt_id=3, freshness="stale")})
        row = self._row()
        self.assertEqual(row.observed_attempt_id, 12)
        self.assertEqual(row.observed_freshness, "fresh")

    def test_creates_missing_family_rows(self):
        self._adopt()
        self._observe({"ospf": _rs(attempt_id=2)})
        self.assertEqual(self._row("ospf").observed_attempt_id, 2)
        self.assertIsNone(self._row("ospf").applied_attempt_id)

    def test_never_adopts_and_requires_adopted_incarnation(self):
        # no adopted incarnation yet → observation is a no-op
        from netbox_nso_plugin.models import NSOFamilyReadState

        wrote = self._observe({"bfd": _rs(attempt_id=1)})
        self.assertFalse(wrote)
        self.assertFalse(NSOFamilyReadState.objects.filter(management=self.mgmt).exists())
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_incarnation, "")

    def test_newer_incarnation_sets_durable_reset_pending_never_adopts(self):
        """R11-1/R12-1: the tab may LEARN of a rebuild but never adopt it; the
        marker is durable on the management row."""
        self._adopt()
        self._observe({"bfd": _rs(attempt_id=1, incarnation=_INC_B[0], incarnation_born=_INC_B[1])})
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_incarnation, _INC_A[0])  # NOT adopted
        self.assertEqual(self.mgmt.reset_pending_incarnation, _INC_B[0])
        self.assertIsNotNone(self.mgmt.reset_pending_born)
        # the non-adopted incarnation's payload must not advance the row either
        row = self._row()
        self.assertEqual(row.observed_incarnation, _INC_A[0])

    def test_epoch_mismatch_skips(self):
        self._adopt(attempt_id=1)
        wrote = self._observe({"bfd": _rs(attempt_id=12)}, epoch=self.epoch + 1)
        self.assertFalse(wrote)
        self.assertEqual(self._row().observed_attempt_id, 1)

    def test_null_adapter_device_id_skips(self):
        self._adopt()
        mirror_update(self.mgmt, adapter_device_id=None)
        wrote = self._observe({"bfd": _rs(attempt_id=12)})
        self.assertFalse(wrote)

    def test_new_source_epoch_ratchets_aggregate_and_blocks_legacy_replay(self):
        self._adopt(attempt_id=1)
        wrote = self._observe({"bfd": _rs(attempt_id=2, source_epoch=2)})
        self.assertTrue(wrote)
        self.mgmt.refresh_from_db()
        self.assertTrue(self.mgmt.source_epoch_aware)
        self.assertEqual(self.mgmt.adapter_source_epoch, 1)
        self.assertEqual(self.mgmt.reset_pending_source_epoch, 2)
        self.assertEqual(self._row().observed_attempt_id, 1)

        wrote = self._observe({"bfd": {**_rs(attempt_id=3), "source_epoch": None}})
        self.assertFalse(wrote)
        self.assertEqual(self._row().observed_attempt_id, 1)


# ---------------------------------------------------------------------------
# The device-wide redis lease (R6-4 lifecycle, Lua atomicity)
# ---------------------------------------------------------------------------


class TestRedisCoordinationKeyNamespace(SimpleTestCase):
    def test_workers_with_identical_database_ids_get_distinct_keys(self):
        from netbox_nso_plugin.read_gate import carrier_key, lease_key, marker_key

        with patch.dict(os.environ, {"NETBOX_NSO_REDIS_KEY_NAMESPACE": "test-run:gw0"}):
            worker_zero = (lease_key(7), marker_key(11), carrier_key(11))
        with patch.dict(os.environ, {"NETBOX_NSO_REDIS_KEY_NAMESPACE": "test-run:gw1"}):
            worker_one = (lease_key(7), marker_key(11), carrier_key(11))

        for zero_key, one_key in zip(worker_zero, worker_one, strict=True):
            self.assertNotEqual(zero_key, one_key)

    def test_empty_namespace_preserves_production_key_names(self):
        from netbox_nso_plugin.read_gate import carrier_key, lease_key, marker_key

        with patch.dict(os.environ, {"NETBOX_NSO_REDIS_KEY_NAMESPACE": ""}):
            self.assertEqual(lease_key(7), "nso-read-lease:7")
            self.assertEqual(marker_key(11), "nso-reconcile-pending:11")
            self.assertEqual(carrier_key(11), "nso-reconcile-carrier:11")


class TestDeviceReadLease(TestCase):
    def setUp(self):
        self.conn = _redis()
        self.key = f"nso-test-lease:{uuid.uuid4().hex}"

    def tearDown(self):
        self.conn.delete(self.key)

    def _lease(self, **kw):
        from netbox_nso_plugin.read_gate import DeviceReadLease

        return DeviceReadLease(self.conn, self.key, **kw)

    def test_acquire_and_contend(self):
        a = self._lease()
        self.assertTrue(a.acquire())
        b = self._lease()
        self.assertFalse(b.acquire())
        with a:
            pass
        self.assertTrue(self._lease().acquire())

    def test_extend_only_if_owner(self):
        a = self._lease(ttl_s=2)
        self.assertTrue(a.acquire())
        self.assertTrue(a._extend())
        # a foreign writer stole/replaced the key → extend must FAIL, not re-EXPIRE
        self.conn.set(self.key, "someone-else")
        self.assertFalse(a._extend())

    def test_release_never_deletes_a_successors_lease(self):
        a = self._lease(ttl_s=1)
        self.assertTrue(a.acquire())
        time.sleep(1.2)  # a's lease expired
        b = self._lease(ttl_s=30)
        self.assertTrue(b.acquire())
        # naive GET-then-DEL would delete b's lease here
        self.assertFalse(a.release())
        self.assertEqual(self.conn.get(self.key), b.token.encode())
        self.assertFalse(a._extend())  # nor can a renew itself back to life

    def test_context_manager_releases_on_body_exception(self):
        a = self._lease()
        self.assertTrue(a.acquire())
        with self.assertRaises(RuntimeError):
            with a:
                raise RuntimeError("body blew up")
        self.assertIsNone(self.conn.get(self.key))
        self.assertFalse(a._hb_thread.is_alive())

    def test_heartbeat_keeps_short_lease_alive(self):
        a = self._lease(ttl_s=1)
        self.assertTrue(a.acquire())
        with a:
            time.sleep(1.5)  # > TTL: only the heartbeat can have kept it
            self.assertEqual(self.conn.get(self.key), a.token.encode())
        self.assertIsNone(self.conn.get(self.key))

    def test_expiry_while_paused_logs_loud_loss(self):
        a = self._lease(ttl_s=1)
        self.assertTrue(a.acquire())
        with self.assertLogs("netbox_nso_plugin.read_gate", level="ERROR") as logs:
            with a:
                a._stop.set()  # simulate a stalled holder: renewals pause
                a._hb_thread.join(timeout=5)
                time.sleep(1.2)  # lease expires while "paused"
            # exit path: release finds the token gone → LOUD loss
        self.assertTrue(any("lease" in m.lower() and "lost" in m.lower() for m in logs.output))
        self.assertTrue(a.lost)

    def test_renewal_exception_lifecycle_still_releases(self):
        a = self._lease(ttl_s=1)
        self.assertTrue(a.acquire())
        with patch.object(a, "_extend", side_effect=RuntimeError("redis hiccup")):
            with a:
                time.sleep(0.6)  # give the heartbeat a chance to blow up
        self.assertFalse(a._hb_thread.is_alive())
        self.assertIsNone(self.conn.get(self.key))  # release still ran


# ---------------------------------------------------------------------------
# Per-call-class contention (R6-1, R7-1, R8-1, R9-1, R10-1)
# ---------------------------------------------------------------------------


class TestContentionPolicies(TestCase):
    def setUp(self):
        import django_rq
        from rq import Queue

        self.conn = _redis()
        self.key = f"nso-test-lease:{uuid.uuid4().hex}"
        self.device_id = int(uuid.uuid4().int % 10**9) + 10**9  # can't collide
        self.queue = Queue(f"nso-test-readgate-{uuid.uuid4().hex[:12]}", connection=self.conn)
        self._django_rq = django_rq

    def tearDown(self):
        from netbox_nso_plugin.read_gate import marker_key

        for job in self.queue.jobs:
            job.delete()
        self.queue.empty()
        self.conn.delete(self.key)
        self.conn.delete(marker_key(self.device_id))

    def _hold(self, ttl_s=30):
        from netbox_nso_plugin.read_gate import DeviceReadLease

        holder = DeviceReadLease(self.conn, self.key, ttl_s=ttl_s)
        assert holder.acquire()
        return holder

    def test_web_fail_fast(self):
        from netbox_nso_plugin.read_gate import acquire_for_web

        holder = self._hold()
        self.assertIsNone(acquire_for_web(self.conn, self.key))
        holder.release()
        lease = acquire_for_web(self.conn, self.key)
        self.assertIsNotNone(lease)
        lease.release()

    def test_web_lease_release_consumes_pending_marker_and_enqueues_successor(self):
        """codex B5-F3: an RQ reconcile that deferred while a WEB reconcile held the
        lease must still get its successor — the web release has to consume the
        pending marker too, or the handoff is stranded until the cadence backstop.
        READSEM 1334: the successor is now a carrier job (arbiter), atomic with the delete."""
        from netbox_nso_plugin.read_gate import acquire_for_web, marker_key

        lease = acquire_for_web(self.conn, self.key, device_id=self.device_id, queue=self.queue)
        self.assertIsNotNone(lease)
        self.conn.set(marker_key(self.device_id), uuid.uuid4().hex, ex=60)
        lease.release()
        self.assertIsNone(self.conn.get(marker_key(self.device_id)))  # consumed
        carriers = [jid for jid in self.queue.get_job_ids() if f"-{self.device_id}-carrier-" in jid]
        self.assertEqual(len(carriers), 1)

    def test_rq_retries_then_defers_with_marker(self):
        from netbox_nso_plugin.read_gate import Deferred, acquire_for_rq, marker_key

        self._hold()
        with self.assertLogs("netbox_nso_plugin.read_gate", level="WARNING"):
            out = acquire_for_rq(
                self.conn,
                self.key,
                self.device_id,
                self.queue,
                retry_budget_s=0.2,
                base_delay_s=0.05,
                sleep=time.sleep,
            )
        self.assertIsInstance(out, Deferred)
        self.assertGreaterEqual(out.attempts, 2)
        marker = self.conn.get(marker_key(self.device_id))
        self.assertIsNotNone(marker)
        self.assertEqual(marker.decode(), out.nonce)

    def test_release_before_marker_race_final_attempt_wins(self):
        """If the owner releases between the marker write and the final attempt, that
        ONE post-marker attempt must succeed and consume the deferrer's own marker."""
        import netbox_nso_plugin.read_gate as read_gate
        from netbox_nso_plugin.read_gate import DeviceReadLease, acquire_for_rq, marker_key

        holder = self._hold()
        real_write = read_gate.write_defer_marker

        def marker_then_owner_exits(conn, device_id):
            nonce = real_write(conn, device_id)
            holder.release()  # the owner exits right after the marker is written
            return nonce

        with patch.object(read_gate, "write_defer_marker", side_effect=marker_then_owner_exits):
            out = acquire_for_rq(
                self.conn,
                self.key,
                self.device_id,
                self.queue,
                retry_budget_s=0.05,
                base_delay_s=0.01,
                sleep=lambda _s: None,
            )
        self.assertIsInstance(out, DeviceReadLease)
        self.assertIsNone(self.conn.get(marker_key(self.device_id)))  # own marker consumed
        self.assertEqual(self.queue.get_job_ids(), [])  # no successor was spawned
        out.release()

    def test_owner_release_consumes_marker_and_enqueues_successor(self):
        """R9-1 / READSEM 1334: a lease release with a pending marker enqueues exactly one
        carrier successor and consumes the marker — atomically, via the arbiter."""
        from netbox_nso_plugin import read_gate
        from netbox_nso_plugin.read_gate import DeviceReadLease, write_defer_marker

        write_defer_marker(self.conn, self.device_id)
        holder = DeviceReadLease(self.conn, self.key, device_id=self.device_id, queue=self.queue)
        self.assertTrue(holder.acquire())
        with holder:
            pass  # release path ensures the carrier + consumes the marker in one MULTI

        carriers = [jid for jid in self.queue.get_job_ids() if f"-{self.device_id}-carrier-" in jid]
        self.assertEqual(len(carriers), 1)
        self.assertIsNone(self.conn.get(read_gate.marker_key(self.device_id)))

    def test_two_consecutive_handoffs_collapse_to_one_carrier(self):
        """READSEM 1334 (reworks R10-1): a second handoff, arriving while the first carrier
        is still QUEUED, suppresses onto it — the arbiter collapses to ONE carrier, not two."""
        from netbox_nso_plugin.read_gate import DeviceReadLease, write_defer_marker

        for _ in range(2):
            write_defer_marker(self.conn, self.device_id)
            holder = DeviceReadLease(self.conn, self.key, device_id=self.device_id, queue=self.queue)
            self.assertTrue(holder.acquire())
            with holder:
                pass
        carriers = [jid for jid in self.queue.get_job_ids() if f"-{self.device_id}-carrier-" in jid]
        self.assertEqual(len(carriers), 1)  # the second suppressed onto the first (still queued)

    def test_concurrent_releases_one_successor(self):
        """Two concurrent release hooks race on ONE marker → exactly one queued carrier
        (WATCH/MULTI: one wins the create+delete; the other retries, sees the marker gone,
        suppresses onto the winner's still-queued carrier)."""
        from netbox_nso_plugin.read_gate import consume_marker_and_enqueue_successor, write_defer_marker

        write_defer_marker(self.conn, self.device_id)
        results = []
        errors = []
        lock = threading.Lock()

        def consume():
            try:
                r = consume_marker_and_enqueue_successor(self.conn, self.device_id, self.queue)
            except Exception as exc:  # noqa: BLE001 — a leaked WatchError/redis error is a real failure
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(r)

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse([t for t in threads if t.is_alive()], "a release thread hung")
        self.assertEqual(errors, [], "a release leaked an exception")
        self.assertEqual(len(results), 2, "every release returned")
        carriers = [jid for jid in self.queue.get_job_ids() if f"-{self.device_id}-carrier-" in jid]
        self.assertEqual(len(carriers), 1)
        returned = {r.id for r in results if r is not None}
        self.assertEqual(returned, set(carriers))  # both callers converged on the one carrier

    def test_successor_job_runs_the_reconcile(self):
        from rq import SimpleWorker

        from netbox_nso_plugin.read_gate import consume_marker_and_enqueue_successor, write_defer_marker

        write_defer_marker(self.conn, self.device_id)
        job = consume_marker_and_enqueue_successor(self.conn, self.device_id, self.queue)
        self.assertIsNotNone(job)
        ran = {}
        with patch(
            "netbox_nso_plugin.reconcile.run_device_reconcile",
            side_effect=lambda dev_id: ran.setdefault("device_id", dev_id) or {"ok": True},
        ):
            SimpleWorker([self.queue], connection=self.conn).work(burst=True)
        self.assertEqual(ran.get("device_id"), self.device_id)

    def test_redis_down_raises_lock_unavailable(self):
        import redis as redis_mod

        from netbox_nso_plugin.read_gate import LockUnavailable, acquire_for_rq, acquire_for_web

        dead = redis_mod.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
        with self.assertRaises(LockUnavailable):
            acquire_for_web(dead, self.key)
        with self.assertRaises(LockUnavailable):
            acquire_for_rq(dead, self.key, self.device_id, self.queue, retry_budget_s=0.1)


class TestQueuedCarrierArbiter(TestCase):
    """READSEM 1334 — the atomic per-device queued-carrier arbiter (real Redis + isolated queue)."""

    def setUp(self):
        from rq import Queue

        self.conn = _redis()
        self.device_id = int(uuid.uuid4().int % 10**9) + 10**9
        self.queue = Queue(f"nso-test-arbiter-{uuid.uuid4().hex[:12]}", connection=self.conn)

    def tearDown(self):
        from netbox_nso_plugin.read_gate import carrier_key, marker_key

        for job in self.queue.jobs:
            job.delete()
        self.queue.empty()
        self.conn.delete(carrier_key(self.device_id))
        self.conn.delete(marker_key(self.device_id))

    def _carriers(self):
        return [jid for jid in self.queue.get_job_ids() if f"-{self.device_id}-carrier-" in jid]

    def test_absent_slot_enqueues_one_carrier(self):
        from netbox_nso_plugin.read_gate import carrier_key, enqueue_reconcile_carrier

        job = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        self.assertEqual(self._carriers(), [job.id])
        self.assertEqual(self.conn.get(carrier_key(self.device_id)).decode(), job.id)

    def test_second_edge_suppresses_onto_queued_carrier(self):
        """A second edge onto a genuinely-queued carrier suppresses (one carrier, same id).
        Also exercises the bytes→str pointer read (codex r1-f1)."""
        from netbox_nso_plugin.read_gate import enqueue_reconcile_carrier

        first = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        second = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self._carriers(), [first.id])

    def test_concurrent_producers_collapse_to_one_carrier(self):
        """Bug (a)/(b): N producers racing on an absent slot → exactly ONE carrier. A barrier on
        the first N queued-checks forces every thread past the read before any commits; the CAS
        (WATCH/MULTI) then lets exactly one win and the rest retry+suppress."""
        import netbox_nso_plugin.read_gate as read_gate
        from netbox_nso_plugin.read_gate import enqueue_reconcile_carrier

        n = 4
        real = read_gate._queued_carrier
        barrier = threading.Barrier(n, timeout=10)
        seen = []
        seen_lock = threading.Lock()

        def barriered(queue, conn, job_id):
            with seen_lock:
                first_wave = len(seen) < n
                seen.append(1)
            if first_wave:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return real(queue, conn, job_id)

        results = []
        errors = []
        res_lock = threading.Lock()

        def produce():
            try:
                job = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
            except Exception as exc:  # noqa: BLE001 — a leaked WatchError/redis error is a real failure
                with res_lock:
                    errors.append(exc)
                return
            with res_lock:
                results.append(job)

        with patch.object(read_gate, "_queued_carrier", barriered):
            threads = [threading.Thread(target=produce) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
        self.assertFalse([t for t in threads if t.is_alive()], "a producer thread hung")
        self.assertEqual(errors, [], "a producer leaked an exception")
        self.assertEqual(len(results), n, "every producer returned a job")
        self.assertEqual(len(self._carriers()), 1)
        self.assertEqual({j.id for j in results}, set(self._carriers()))

    def test_started_carrier_not_suppressible_gets_trailing(self):
        """r3-2 / pop-boundary: a carrier removed from the queue list (popped/started) is NOT
        suppressible — a new edge enqueues a DISTINCT trailing carrier and repoints."""
        from netbox_nso_plugin.read_gate import carrier_key, enqueue_reconcile_carrier

        first = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        self.queue.remove(first.id)  # simulate the worker popping it (off the queue list; pointer stays)
        second = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self._carriers(), [second.id])  # only the trailing one is queued
        self.assertEqual(self.conn.get(carrier_key(self.device_id)).decode(), second.id)

    def test_stale_pointer_fails_open(self):
        """A pointer referencing a job absent from the queue list → not suppressible → fresh carrier."""
        from netbox_nso_plugin.read_gate import carrier_key, enqueue_reconcile_carrier

        self.conn.set(carrier_key(self.device_id), f"nso-reconcile-{self.device_id}-carrier-ghost")
        job = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        self.assertEqual(self._carriers(), [job.id])
        self.assertEqual(self.conn.get(carrier_key(self.device_id)).decode(), job.id)

    def test_persistent_pointer_has_no_ttl(self):
        """The carrier pointer must NOT expire while its carrier can still be queued (codex r1-f2)."""
        from netbox_nso_plugin.read_gate import carrier_key, enqueue_reconcile_carrier

        enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        self.assertEqual(self.conn.ttl(carrier_key(self.device_id)), -1)  # -1 = persistent

    def test_committed_job_is_runnable(self):
        """Real-RQ commit: after EXEC the job hash exists, it is in the queue list, and the pointer matches."""
        from rq.job import Job as RqJob

        from netbox_nso_plugin.read_gate import carrier_key, enqueue_reconcile_carrier

        job = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id)
        fetched = RqJob.fetch(job.id, connection=self.conn)
        self.assertEqual(fetched.func_name, "netbox_nso_plugin.reconcile.run_device_reconcile")
        self.assertEqual(fetched.args, (self.device_id,))
        self.assertIn(job.id, self.queue.get_job_ids())
        self.assertEqual(self.conn.get(carrier_key(self.device_id)).decode(), job.id)

    def test_handoff_absent_marker_is_noop(self):
        """consume_marker=True with no pending marker → never create a phantom successor (codex r3-1)."""
        from netbox_nso_plugin.read_gate import enqueue_reconcile_carrier

        result = enqueue_reconcile_carrier(self.conn, self.queue, self.device_id, consume_marker=True)
        self.assertIsNone(result)
        self.assertEqual(self._carriers(), [])

    def test_handoff_does_not_lose_edge_when_enqueue_fails(self):
        """Bug (c) regression: the old GETDEL→enqueue could delete the marker then fail to enqueue,
        losing the edge. The arbiter deletes the marker only in the SAME EXEC as the durable carrier,
        so a failing enqueue leaves the edge represented — a successor exists OR the marker is retained."""
        from netbox_nso_plugin.read_gate import (
            consume_marker_and_enqueue_successor,
            marker_key,
            write_defer_marker,
        )

        write_defer_marker(self.conn, self.device_id)
        with patch.object(self.queue, "enqueue", side_effect=RuntimeError("enqueue boom")):
            try:
                consume_marker_and_enqueue_successor(self.conn, self.device_id, self.queue)
            except RuntimeError:
                pass  # the OLD code raises here (marker already GETDEL'd → the lost edge)
        edge_represented = bool(self.queue.get_job_ids()) or self.conn.get(marker_key(self.device_id)) is not None
        self.assertTrue(edge_represented, "handoff lost the edge: marker gone AND no successor")

    def test_handoff_crash_mid_commit_retains_marker(self):
        """codex r4-3 / diff-2: a crash DURING the CAS commit (before EXEC) must retain the marker and
        leave NO partial carrier — the marker DELETE is buffered in the same MULTI as the job, so an
        aborted commit runs neither. Injects the failure at the real CAS path (queue.enqueue_job)."""
        from netbox_nso_plugin.read_gate import (
            consume_marker_and_enqueue_successor,
            marker_key,
            write_defer_marker,
        )

        write_defer_marker(self.conn, self.device_id)
        with patch.object(self.queue, "enqueue_job", side_effect=RuntimeError("crash mid-commit")):
            with self.assertRaises(RuntimeError):
                consume_marker_and_enqueue_successor(self.conn, self.device_id, self.queue)
        self.assertIsNotNone(self.conn.get(marker_key(self.device_id)))  # marker RETAINED (never deleted)
        self.assertEqual(self._carriers(), [])  # no partial carrier committed

    def test_inline_sync_handoff_consumes_marker_no_recursion(self):
        """is_async=False (codex r3-4): the handoff consumes the marker once before the inline run,
        so a re-entry finds no marker and stops — no infinite recursion."""
        from rq import Queue

        from netbox_nso_plugin.read_gate import enqueue_reconcile_carrier, marker_key, write_defer_marker

        sync_q = Queue(f"nso-test-sync-{uuid.uuid4().hex[:8]}", connection=self.conn, is_async=False)
        _SYNC_RECONCILE_CALLS.clear()
        with patch("netbox_nso_plugin.reconcile.run_device_reconcile", new=_sync_reconcile_recorder):
            # no marker → the recursion guard: nothing to hand off, no inline run
            self.assertIsNone(enqueue_reconcile_carrier(self.conn, sync_q, self.device_id, consume_marker=True))
            self.assertEqual(_SYNC_RECONCILE_CALLS, [])
            # marker present → consume once, run inline once
            write_defer_marker(self.conn, self.device_id)
            enqueue_reconcile_carrier(self.conn, sync_q, self.device_id, consume_marker=True)
        self.assertEqual(_SYNC_RECONCILE_CALLS, [self.device_id])
        self.assertIsNone(self.conn.get(marker_key(self.device_id)))  # consumed


# ---------------------------------------------------------------------------
# Orchestrated two-worker scenarios (real threads + committed rows)
# ---------------------------------------------------------------------------


class TestOrchestratedOverwrites(_CascadeFlushMixin, TransactionTestCase):
    def setUp(self):
        with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"), transaction.atomic():
            self.mgmt = _make_mgmt(_make_device(f"rg-orch-{uuid.uuid4().hex[:8]}"))
        self.epoch = self.mgmt.adapter_device_id
        self.conn = _redis()
        self.key = f"nso-test-lease:{uuid.uuid4().hex}"

    def tearDown(self):
        self.conn.delete(self.key)

    def test_publication_takes_the_device_intent_lock_before_running_the_body(self):
        """Serialize reconciliation bodies with native intent edits before either writes."""
        from netbox_nso_plugin import read_gate
        from netbox_nso_plugin.apply_state import lock_device_intent_transaction
        from netbox_nso_plugin.read_gate import RAN, gated_family_run

        identity_checked = threading.Event()
        intent_locked = threading.Event()
        release_intent = threading.Event()
        body_started = threading.Event()
        errors = []
        outcome = {}

        original_identity_check = read_gate._publication_identity_current

        def pause_after_identity_check(*args, **kwargs):
            current = original_identity_check(*args, **kwargs)
            identity_checked.set()
            self.assertTrue(intent_locked.wait(20), "the competing writer did not lock device intent")
            return current

        def hold_intent():
            try:
                self.assertTrue(identity_checked.wait(20), "publication did not finish its identity check")
                with transaction.atomic():
                    lock_device_intent_transaction(self.mgmt.device_id)
                    intent_locked.set()
                    self.assertTrue(release_intent.wait(20), "publication did not inspect the lock order")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                _close_thread_db()

        def body():
            body_started.set()

        def publish():
            try:
                with patch.object(read_gate, "_publication_identity_current", side_effect=pause_after_identity_check):
                    outcome["result"] = gated_family_run(
                        self.mgmt,
                        "bfd",
                        _rs(attempt_id=5),
                        body,
                        epoch=self.epoch,
                    )
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                _close_thread_db()

        holder = threading.Thread(target=hold_intent)
        publisher = threading.Thread(target=publish)
        holder.start()
        publisher.start()
        try:
            self.assertTrue(intent_locked.wait(20), "the competing writer did not lock device intent")
            self.assertFalse(body_started.wait(1), "publication ran its body before it locked device intent")
        finally:
            release_intent.set()
            holder.join(30)
            publisher.join(30)

        self.assertFalse(holder.is_alive())
        self.assertFalse(publisher.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(outcome["result"].disposition, RAN)
        self.assertTrue(body_started.is_set())

    def test_publication_error_and_operator_edit_follow_canonical_lock_order(self):
        from dcim.models import Interface
        from django.db import connection

        from netbox_nso_plugin.apply_state import lock_order_scope
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInterfaceMtuState
        from netbox_nso_plugin.read_gate import _gate_and_record, mark_publication_error_if_current
        from netbox_nso_plugin.reconcile import _mark_scope_error
        from netbox_nso_plugin.tests._outbox_case import without_commit_drain

        with without_commit_drain(), transaction.atomic():
            interface = Interface.objects.create(
                device=self.mgmt.device,
                name="Ethernet-publication-error",
                type="1000base-t",
            )
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="imported",
            )
        decision = _gate_and_record(
            self.mgmt,
            "interface_mtu",
            _rs(attempt_id=5),
            epoch=self.epoch,
        )

        error_holds_level_five = threading.Event()
        release_error = threading.Event()
        operator_holds_level_four = threading.Event()
        operator_waits_for_level_five = threading.Event()
        errors = []
        outcome = {}
        management_table = NSODeviceManagement._meta.db_table

        def mark_error():
            error_holds_level_five.set()
            if not release_error.wait(10):
                raise AssertionError("the operator interleaving did not release the publication error")
            _mark_scope_error(self.mgmt, ("NSOInterfaceMtuState",))

        def publication_error():
            try:
                with lock_order_scope():
                    outcome["marked"] = mark_publication_error_if_current(
                        self.mgmt,
                        "interface_mtu",
                        decision,
                        self.epoch,
                        mark_error,
                    )
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                _close_thread_db()

        def operator_edit():
            try:
                if not error_holds_level_five.wait(10):
                    raise AssertionError("the publication error did not lock its read state")
                current = NSOInterfaceMtuState.objects.get(pk=state.pk)
                current.l2_mtu = 1600

                def observe_lock_order(execute, sql, params, many, context):
                    statement = str(sql)
                    if "pg_advisory_xact_lock" in statement:
                        result = execute(sql, params, many, context)
                        operator_holds_level_four.set()
                        return result
                    if f'FROM "{management_table}"' in statement and "FOR UPDATE" in statement:
                        operator_waits_for_level_five.set()
                    return execute(sql, params, many, context)

                with (
                    without_commit_drain(),
                    connection.execute_wrapper(observe_lock_order),
                    transaction.atomic(),
                    lock_order_scope(),
                ):
                    current.save(update_fields=["l2_mtu"])
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                _close_thread_db()

        error_worker = threading.Thread(target=publication_error)
        operator_worker = threading.Thread(target=operator_edit)
        error_worker.start()
        operator_worker.start()
        try:
            self.assertTrue(error_holds_level_five.wait(10), "the publication error did not reach its callback")
            self.assertTrue(
                operator_holds_level_four.wait(10),
                "the operator did not acquire the device-intent lock",
            )
            self.assertTrue(
                operator_waits_for_level_five.wait(5),
                "the operator did not reach the management-row lock",
            )
        finally:
            release_error.set()
            error_worker.join(20)
            operator_worker.join(20)

        self.assertFalse(error_worker.is_alive())
        self.assertFalse(operator_worker.is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(outcome["marked"])
        state.refresh_from_db()
        self.assertEqual(state.l2_mtu, 1600)

    def test_old_starts_new_finishes_old_resumes_cannot_overwrite(self):
        """A stalls with the lease past expiry; B (successor) applies attempt 6;
        A resumes with its stale attempt 5 → the gate refuses, applied stays 6,
        and A's exit logs the loud lease loss."""
        from netbox_nso_plugin.models import NSOFamilyReadState
        from netbox_nso_plugin.read_gate import (
            RAN,
            SKIPPED_STALE_ATTEMPT,
            DeviceReadLease,
            gated_family_run,
        )

        a = DeviceReadLease(self.conn, self.key, ttl_s=1)
        self.assertTrue(a.acquire())
        outcome = {}
        with self.assertLogs("netbox_nso_plugin.read_gate", level="ERROR"):
            with a:
                a._stop.set()  # A stalls: renewals stop, lease will expire
                a._hb_thread.join(timeout=5)
                time.sleep(1.2)
                # B takes over and completes the NEWER attempt
                b = DeviceReadLease(self.conn, self.key, ttl_s=30)
                self.assertTrue(b.acquire())
                with b:
                    rb = gated_family_run(self.mgmt, "bfd", _rs(attempt_id=6), _Recorder(), epoch=self.epoch)
                self.assertEqual(rb.disposition, RAN)
                # A resumes, still believing it owns the device
                body_a = _Recorder()
                ra = gated_family_run(self.mgmt, "bfd", _rs(attempt_id=5), body_a, epoch=self.epoch)
                outcome["ra"] = ra
                outcome["body_a_calls"] = body_a.calls
        self.assertEqual(outcome["ra"].disposition, SKIPPED_STALE_ATTEMPT)
        self.assertEqual(outcome["body_a_calls"], 0)
        row = NSOFamilyReadState.objects.get(management=self.mgmt, family="bfd")
        self.assertEqual(row.applied_attempt_id, 6)
        self.assertEqual(row.observed_attempt_id, 6)
        self.assertTrue(a.lost)

    def test_concurrent_first_create_single_row(self):
        from netbox_nso_plugin.models import NSOFamilyReadState
        from netbox_nso_plugin.read_gate import gated_family_run

        errs = []
        barrier = threading.Barrier(2)

        def run(attempt):
            try:
                barrier.wait(timeout=10)
                gated_family_run(self.mgmt, "bfd", _rs(attempt_id=attempt), _Recorder(), epoch=self.epoch)
            except Exception as exc:  # noqa: BLE001 - the test asserts none happen
                errs.append(exc)
            finally:
                _close_thread_db()

        threads = [threading.Thread(target=run, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errs, [])
        rows = NSOFamilyReadState.objects.filter(management=self.mgmt, family="bfd")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().applied_attempt_id, 2)

    def test_adoption_serialized_across_families(self):
        """Two families racing with DIFFERENT incarnations must converge on the
        newest born with no out-of-order adoption (row locks serialize them)."""
        from netbox_nso_plugin.read_gate import gated_family_run

        barrier = threading.Barrier(2)
        errs = []

        def run(family, inc):
            try:
                barrier.wait(timeout=10)
                gated_family_run(
                    self.mgmt,
                    family,
                    _rs(attempt_id=1, incarnation=inc[0], incarnation_born=inc[1]),
                    _Recorder(),
                    epoch=self.epoch,
                )
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)
            finally:
                _close_thread_db()

        threads = [
            threading.Thread(target=run, args=("bfd", _INC_A)),
            threading.Thread(target=run, args=("ospf", _INC_B)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errs, [])
        self.mgmt.refresh_from_db()
        # whichever interleaving happened, the newest born must have won
        self.assertEqual(self.mgmt.adapter_incarnation, _INC_B[0])

    def test_aggregate_vs_adoption_interleave_no_deadlock(self):
        from netbox_nso_plugin.read_gate import gated_family_run, observe_aggregate

        gated_family_run(self.mgmt, "bfd", _rs(attempt_id=1), _Recorder(), epoch=self.epoch)
        errs = []
        stop = threading.Event()

        def gate_loop():
            try:
                for n in range(2, 12):
                    gated_family_run(self.mgmt, "bfd", _rs(attempt_id=n), _Recorder(), epoch=self.epoch)
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)
            finally:
                stop.set()
                _close_thread_db()

        def observe_loop():
            try:
                n = 2
                while not stop.is_set():
                    observe_aggregate(
                        self.mgmt,
                        {"bfd": _rs(attempt_id=n), "ospf": _rs(attempt_id=n)},
                        epoch=self.epoch,
                    )
                    n += 1
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)
            finally:
                _close_thread_db()

        threads = [threading.Thread(target=gate_loop), threading.Thread(target=observe_loop)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), "deadlock: threads never finished")
        self.assertEqual(errs, [])
