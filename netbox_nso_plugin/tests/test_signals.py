# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the signal handlers in signals.py.

Every handler is driven against real Django model rows — a real device + interface +
NSODeviceManagement / NSOInterfaceState — so the handlers' own ORM queries and updates
are exercised for real. Only the adapter_client HTTP functions are patched, since those
are the genuine external boundary. (Earlier revisions called the handlers with MagicMock'd
instances plus a sys.modules-injected fake `models` module, which bypassed exactly those
queries — e.g. the OWNED-states filter — and so could not catch a regression in them.)
"""

import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connections
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from ._outbox_case import content_bulk_update, in_thread, mirror_update, wait_until_postgres_blocks
from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin, _CascadeFlushMixin

_MOD = "netbox_nso_plugin.adapter_client"


def _bulk_create_management_without_signals(rows):
    from netbox_nso_plugin.intent_state import MutationFootprint, footprint_for_instance, intent_transaction

    rows = tuple(rows)
    footprint = MutationFootprint.merge(*(footprint_for_instance(row) for row in rows))
    with intent_transaction(footprint):
        type(rows[0]).objects.bulk_create(rows)


def _invoke_push_intent_on_accept(state):
    from datetime import timedelta

    from netbox_nso_plugin.renderer_writer import (
        RendererMutationPlan,
        planned_save,
        renderer_mirror_writes,
        renderer_writes,
    )

    candidate = copy.copy(state)
    candidate.accepted_at = (candidate.accepted_at or timezone.now()) + timedelta(microseconds=1)
    fields = ("accepted_at",)
    plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=fields),))
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer:
        writer.save(candidate, update_fields=fields)


def _invoke_interface_edit(interface):
    from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
    from netbox_nso_plugin.signals import _push_intent_on_interface_edit

    with intent_transaction(footprint_for_instance(interface)):
        _push_intent_on_interface_edit(None, interface, created=False)


class _SignalDBBase(IntentPushDeliveryMixin, TestCase):
    """Shared real-DB fixture for the signal-handler tests.

    These handlers read and update the real overlay models (NSODeviceManagement /
    NSOInterfaceState) — ``sync_scope_to_adapter`` does
    ``type(instance).objects.filter(pk=…).update(…)`` and ``push_intent_on_accept``
    queries ``NSOInterfaceState.objects.filter(…).select_related(…)`` — so they are
    driven against real rows, not a MagicMock'd ORM. Only the adapter_client HTTP
    functions (onboard_device/set_scope/sync_notify/patch_device/put_intent) are
    patched: those are the genuine external boundary. (A sibling TestIPAddressSignals
    already follows this pattern.)
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

        from netbox_nso_plugin.models import NSOInstance

        mfg = Manufacturer.objects.create(name="SigMfg", slug="sigmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SigDev", slug="sigdev")
        role = DeviceRole.objects.create(name="SigRole", slug="sigrole")
        site = Site.objects.create(name="SigSite", slug="sigsite")
        cls.device = Device.objects.create(name="core-rtr-01", device_type=dt, role=role, site=site)
        cls.iface = Interface.objects.create(
            device=cls.device, name="GigabitEthernet0/0", type="1000base-t", description="uplink", enabled=True
        )
        cls.nso_instance = NSOInstance.objects.create(name="nso-prod", adapter_instance_id="nso-prod")

    def _make_mgmt(
        self,
        *,
        adapter_device_id=None,
        manage_description=True,
        manage_enabled=False,
        auto_apply=False,
        sync_before_apply=True,
    ):
        """Create a real NSODeviceManagement row WITHOUT firing its post_save sync signal.

        bulk_create skips signals, so the test can drive sync_scope_to_adapter explicitly
        rather than have the row's own save trigger it.
        """
        from netbox_nso_plugin.models import NSODeviceManagement

        _bulk_create_management_without_signals(
            [
                NSODeviceManagement(
                    device=self.device,
                    nso_instance=self.nso_instance,
                    nso_device_name="core-rtr-01",
                    adapter_device_id=adapter_device_id,
                    manage_description=manage_description,
                    manage_enabled=manage_enabled,
                    auto_apply=auto_apply,
                    sync_before_apply=sync_before_apply,
                    custom_field_data={},
                )
            ]
        )
        return NSODeviceManagement.objects.get(device=self.device)

    def _accepted_state(self, interface, attribute, *, nso_value=""):
        """Create an OWNED (accepted_at-set) NSOInterfaceState without firing the push signal.

        Created as ``imported`` then promoted via a queryset ``update`` (no post_save), so
        the test drives push_intent_on_accept explicitly. The returned in-memory object has
        the accepted markers set to match the row.
        """
        from netbox_nso_plugin.models import NSOInterfaceState

        now = timezone.now()
        state = NSOInterfaceState.objects.create(
            interface=interface, attribute=attribute, status="imported", nso_value=nso_value
        )
        content_bulk_update(state, status="accepted", accepted_at=now)
        state.status = "accepted"
        state.accepted_at = now
        return state


class TestUntrackedNativeDeleteIsNoOp(_SignalDBBase):
    def test_untracked_isis_flex_algo_delete_changes_nothing(self):
        from netbox_routing.models import ISISFlexAlgo, ISISInstance

        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision

        instance = ISISInstance.objects.create(device=self.device, process_tag="CORE")
        flex_algo = ISISFlexAlgo.objects.create(instance=instance, algo_id=130)
        self._make_mgmt(adapter_device_id=42)
        revision, _ = NSOIntentRevision.objects.get_or_create(device=self.device, scope="isis_flex_algo")
        before_revision = revision.revision
        before_outbox = list(
            NSOIntentOutboxEntry.objects.filter(device=self.device, scope="isis_flex_algo").values_list("pk", flat=True)
        )

        with (
            patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent") as push,
            self.captureOnCommitCallbacks(execute=True),
        ):
            flex_algo.delete()

        revision.refresh_from_db()
        self.assertEqual(revision.revision, before_revision)
        self.assertEqual(
            list(
                NSOIntentOutboxEntry.objects.filter(device=self.device, scope="isis_flex_algo").values_list(
                    "pk", flat=True
                )
            ),
            before_outbox,
        )
        push.assert_not_called()

    def test_untracked_isis_interface_delete_changes_nothing(self):
        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision

        instance = ISISInstance.objects.create(device=self.device, process_tag="CORE")
        isis_interface = ISISInterface.objects.create(
            interface=self.iface,
            address_family="ipv4",
            instance=instance,
        )
        self._make_mgmt(adapter_device_id=42)
        revision, _ = NSOIntentRevision.objects.get_or_create(device=self.device, scope="isis")
        before_revision = revision.revision
        before_outbox = list(
            NSOIntentOutboxEntry.objects.filter(device=self.device, scope="isis").values_list("pk", flat=True)
        )

        with (
            patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent") as push,
            self.captureOnCommitCallbacks(execute=True),
        ):
            isis_interface.delete()

        revision.refresh_from_db()
        self.assertEqual(revision.revision, before_revision)
        self.assertEqual(
            list(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="isis").values_list("pk", flat=True)),
            before_outbox,
        )
        push.assert_not_called()


class TestSyncScopeToAdapter(_SignalDBBase):
    """Tests for the sync_scope_to_adapter signal handler (real NSODeviceManagement row)."""

    def _sync_scope(self, instance, *, created):
        from netbox_nso_plugin.signals import _queue_scope_sync

        with self.captureOnCommitCallbacks(execute=True):
            _queue_scope_sync(type(instance), instance, created)

    def test_intent_delivery_bookkeeping_does_not_resync_the_adapter_link(self):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        mgmt = self._make_mgmt(adapter_device_id=7)
        for field_name in ("intent_push_attempts", "intent_push_errors"):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                sync_scope_to_adapter(
                    sender=type(mgmt),
                    instance=mgmt,
                    created=False,
                    update_fields={field_name},
                )
            self.assertEqual(callbacks, [], field_name)

    def test_direct_management_save_schedules_adapter_work_through_writer(self):
        """A direct save cannot bypass the management writer and adapter sync."""
        from netbox_nso_plugin import management_lifecycle

        mgmt = self._make_mgmt(adapter_device_id=7)
        mgmt.manage_enabled = True

        with (
            patch.object(
                management_lifecycle,
                "save_management",
                wraps=management_lifecycle.save_management,
            ) as save_management,
            self.captureOnCommitCallbacks(execute=False) as callbacks,
        ):
            mgmt.save(update_fields=["manage_enabled"])

        save_management.assert_called_once_with(
            mgmt,
            update_fields=["manage_enabled"],
            force_insert=False,
        )
        self.assertEqual(len(callbacks), 1)
        mgmt.refresh_from_db()
        self.assertTrue(mgmt.manage_enabled)

    def test_management_mirror_ignores_a_row_deleted_before_the_callback(self):
        from netbox_nso_plugin.signals import _update_management_mirror

        mgmt = self._make_mgmt(adapter_device_id=7)
        type(mgmt).objects.filter(pk=mgmt.pk).delete()

        _update_management_mirror(mgmt, adapter_link_error="adapter unavailable")

        self.assertFalse(type(mgmt).objects.filter(pk=mgmt.pk).exists())

    def test_created_onboards_device_and_sets_scope(self):
        mgmt = self._make_mgmt(adapter_device_id=None)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 99}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={"device_id": 99}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value={"job_id": 5}) as mock_notify,
            patch(f"{_MOD}.patch_device") as mock_patch,
        ):
            self._sync_scope(mgmt, created=True)

            mock_onboard.assert_called_once_with(
                nso_instance="nso-prod",
                nso_device_name="core-rtr-01",
                netbox_device_id=self.device.pk,
            )
            # The base fixture device has no primary/OOB IP → both pushed as None (clear).
            mock_scope.assert_called_once_with(
                99, ["description"], auto_apply=False, sync_before_apply=True, primary_ip=None, oob_ip=None
            )
            mock_notify.assert_called_once_with(99)
            mock_patch.assert_not_called()

        # The handler wrote the adapter id back to the real row via a queryset update.
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 99)

    def test_source_update_patches_device_and_sets_scope(self):
        mgmt = self._make_mgmt(adapter_device_id=7)
        mirror_update(mgmt, source_rekey_pending=True)

        with (
            patch(f"{_MOD}.patch_device", return_value={"source_epoch": 2}) as mock_patch,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
            patch(f"{_MOD}.onboard_device") as mock_onboard,
        ):
            self._sync_scope(mgmt, created=False)

        mock_patch.assert_called_once_with(
            adapter_device_id=7,
            nso_instance="nso-prod",
            nso_device_name="core-rtr-01",
        )
        mock_scope.assert_called_once()
        mock_onboard.assert_not_called()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_source_epoch, 2)
        self.assertFalse(mgmt.source_rekey_pending)

    def test_rekey_of_a_dead_mapping_reonboards_and_keeps_the_reset_marker(self):
        """A rekey whose target mapping is gone re-onboards, and still records the reset fence.

        The rekey PATCHes the stored adapter id, so a dead mapping fails BEFORE the scope push
        and would loop forever otherwise. Recovery has to happen inside _sync_source_change:
        admissions are blanked before the PATCH, so completing without
        ``reset_pending_source_epoch`` would leave every family fenced while the UI (summary.py
        reads exactly these three markers) reported all-clear.
        """
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.models import NSOFamilyReadState

        mgmt = self._make_mgmt(adapter_device_id=7)
        NSOFamilyReadState.objects.create(management=mgmt, family="bfd", observed_outcome="ok")
        mirror_update(mgmt, source_rekey_pending=True)

        with (
            patch(f"{_MOD}.patch_device", side_effect=AdapterError("Device not found", code="not_found")),
            patch(f"{_MOD}.onboard_device", return_value={"id": 71, "source_epoch": 5}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=False)

        mock_onboard.assert_called_once()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 71)  # re-onboarded
        self.assertFalse(mgmt.source_rekey_pending)  # the rekey is satisfied
        self.assertEqual(mgmt.adapter_source_epoch, 5)
        # The fence survives: families were blanked, so the reset stays pending until they
        # re-observe at the new epoch.
        self.assertEqual(mgmt.reset_pending_source_epoch, 5)
        self.assertEqual(mock_scope.call_args[0][0], 71)  # scope pushed to the fresh id

    def test_rekey_does_not_reonboard_on_a_transient_patch_failure(self):
        """Only a not-found rekey means the mapping is dead — an outage must not re-onboard."""
        from netbox_nso_plugin.adapter_client import AdapterError

        mgmt = self._make_mgmt(adapter_device_id=7)
        mirror_update(mgmt, source_rekey_pending=True)

        with (
            patch(f"{_MOD}.patch_device", side_effect=AdapterError("adapter down", code="nso_unreachable")),
            patch(f"{_MOD}.onboard_device") as mock_onboard,
            patch(f"{_MOD}.set_scope") as mock_scope,
            # A regression past the fail-closed guard would otherwise reach the live adapter.
            patch(f"{_MOD}.sync_notify") as mock_notify,
        ):
            self._sync_scope(mgmt, created=False)

        mock_onboard.assert_not_called()
        mock_scope.assert_not_called()
        mock_notify.assert_not_called()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 7)  # mapping untouched
        self.assertTrue(mgmt.source_rekey_pending)  # still pending, retried next save

    def test_source_save_is_fail_closed_before_its_on_commit_callback(self):
        from netbox_nso_plugin.management_lifecycle import save_management
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE, gated_family_run

        mgmt = self._make_mgmt(adapter_device_id=7)
        mgmt.nso_device_name = "replacement-source"
        with (
            self.captureOnCommitCallbacks(execute=False) as callbacks,
            patch(f"{_MOD}.patch_device") as mock_patch,
        ):
            save_management(mgmt)

        self.assertEqual(len(callbacks), 1)
        mock_patch.assert_not_called()
        mgmt.refresh_from_db()
        self.assertTrue(mgmt.source_rekey_pending)
        body_calls = []

        def body():
            body_calls.append(True)
            return "must not run"

        result = gated_family_run(
            mgmt,
            "bfd",
            {
                "outcome": "present",
                "freshness": "fresh",
                "result": "replaced",
                "succeeded": True,
                "attempt_id": 1,
                "incarnation": "11111111-aaaa-4aaa-8aaa-111111111111",
                "incarnation_born": "2026-07-01T00:00:10Z",
                "source_epoch": 1,
                "payload_revision": 1,
            },
            body,
            epoch=7,
        )
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body_calls, [])

    def test_source_update_without_epoch_preserves_floor_and_stays_fenced(self):
        mgmt = self._make_mgmt(adapter_device_id=7)
        mirror_update(mgmt, adapter_source_epoch=4, source_epoch_aware=True)
        mirror_update(mgmt, source_rekey_pending=True)

        with (
            patch(f"{_MOD}.patch_device", return_value={}),
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=False)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_source_epoch, 4)
        self.assertTrue(mgmt.source_epoch_aware)
        self.assertTrue(mgmt.source_rekey_pending)
        self.assertEqual(mgmt.adapter_link_error, "The adapter link failed. See the server log.")

    def test_old_wire_rekey_cannot_reopen_legacy_admission(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE, gated_family_run

        mgmt = self._make_mgmt(adapter_device_id=7)
        mirror_update(mgmt, source_rekey_pending=True)
        with patch(f"{_MOD}.patch_device", return_value={}):
            self._sync_scope(mgmt, created=False)

        mgmt.refresh_from_db()
        self.assertFalse(mgmt.source_epoch_aware)
        self.assertTrue(mgmt.source_rekey_pending)
        body_calls = []

        def body():
            body_calls.append(True)
            return "must not run"

        result = gated_family_run(mgmt, "bfd", None, body, epoch=7)
        self.assertEqual(result.disposition, SKIPPED_UNAVAILABLE)
        self.assertEqual(body_calls, [])

    def test_successful_source_rekey_blanks_old_observations_until_reobserved(self):
        from netbox_nso_plugin.models import NSOFamilyReadState

        mgmt = self._make_mgmt(adapter_device_id=7)
        NSOFamilyReadState.objects.create(
            management=mgmt,
            family="ospf",
            observed_outcome="present",
            observed_attempt_id=8,
            observed_incarnation="11111111-aaaa-4aaa-8aaa-111111111111",
            admitted_attempt_id=8,
            admitted_incarnation="11111111-aaaa-4aaa-8aaa-111111111111",
            applied_attempt_id=8,
            applied_incarnation="11111111-aaaa-4aaa-8aaa-111111111111",
            publication_sequence=4,
            applied_publication_sequence=4,
        )
        mirror_update(mgmt, source_rekey_pending=True)

        with (
            patch(f"{_MOD}.patch_device", return_value={"source_epoch": 2}),
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=False)

        row = NSOFamilyReadState.objects.get(management=mgmt, family="ospf")
        self.assertEqual(row.observed_outcome, "")
        self.assertIsNone(row.applied_attempt_id)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.reset_pending_source_epoch, 2)

    def test_committed_callback_rekeys_the_latest_source_tuple(self):
        mgmt = self._make_mgmt(adapter_device_id=7)
        stale = type(mgmt).objects.get(pk=mgmt.pk)
        content_bulk_update(
            mgmt,
            nso_device_name="newer-source",
            source_rekey_pending=True,
        )

        with (
            patch(f"{_MOD}.patch_device", return_value={"source_epoch": 2}) as mock_patch,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None) as mock_notify,
        ):
            self._sync_scope(stale, created=False)

        mock_patch.assert_called_once_with(
            adapter_device_id=7,
            nso_instance="nso-prod",
            nso_device_name="newer-source",
        )
        mock_scope.assert_called_once()
        mock_notify.assert_called_once()

    def test_failed_source_patch_keeps_the_committed_publication_fence(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.models import NSOFamilyReadState

        mgmt = self._make_mgmt(adapter_device_id=7)
        row = NSOFamilyReadState.objects.create(
            management=mgmt,
            family="bfd",
            admitted_attempt_id=8,
            admitted_incarnation="11111111-aaaa-4aaa-8aaa-111111111111",
            applied_attempt_id=8,
            applied_incarnation="11111111-aaaa-4aaa-8aaa-111111111111",
            publication_sequence=4,
            applied_publication_sequence=4,
        )
        mirror_update(mgmt, source_rekey_pending=True)

        with patch(
            f"{_MOD}.patch_device",
            side_effect=AdapterError("patch failed", code="adapter_unreachable"),
        ):
            self._sync_scope(mgmt, created=False)

        row.refresh_from_db()
        self.assertIsNone(row.admitted_attempt_id)
        self.assertIsNone(row.applied_attempt_id)
        self.assertEqual(row.publication_sequence, 5)
        mgmt.refresh_from_db()
        self.assertTrue(mgmt.adapter_link_error)
        self.assertTrue(mgmt.source_rekey_pending)

    def test_pending_source_rekey_is_retried_on_an_ordinary_save(self):
        mgmt = self._make_mgmt(adapter_device_id=7)
        mirror_update(mgmt, source_rekey_pending=True)
        mgmt.refresh_from_db()
        mgmt._nso_source_changed = False

        with (
            patch(f"{_MOD}.patch_device", return_value={"source_epoch": 2}) as mock_patch,
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=False)

        mock_patch.assert_called_once()
        mgmt.refresh_from_db()
        self.assertFalse(mgmt.source_rekey_pending)
        self.assertEqual(mgmt.adapter_source_epoch, 2)

    def test_stale_full_writer_save_preserves_a_pending_source_rekey(self):
        from netbox_nso_plugin.management_lifecycle import save_management

        stale = self._make_mgmt(adapter_device_id=7)
        mirror_update(stale, source_rekey_pending=True)
        stale.source_rekey_pending = False

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            save_management(stale)

        stale.refresh_from_db()
        self.assertTrue(stale.source_rekey_pending)
        self.assertEqual(len(callbacks), 1)

    def test_ordinary_update_does_not_patch_device(self):
        mgmt = self._make_mgmt(adapter_device_id=7)
        mgmt._nso_source_changed = False
        with (
            patch(f"{_MOD}.patch_device") as mock_patch,
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=False)
        mock_patch.assert_not_called()

    def test_adapter_error_is_recorded_not_silently_swallowed(self):
        """A failed onboard must be SURFACED on the row (adapter_link_error), not swallowed with only
        a log line — otherwise the device looks managed in NetBox while silently unlinked from the
        adapter (adapter_device_id stays None) with no operator-visible signal."""
        from netbox_nso_plugin.adapter_client import AdapterError

        mgmt = self._make_mgmt(adapter_device_id=None)

        with patch(f"{_MOD}.onboard_device", side_effect=AdapterError("nso down", code="nso_unreachable")):
            # Must not raise — the failure is recorded on the row instead.
            self._sync_scope(mgmt, created=True)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_link_error, "The NSO adapter request failed. See the server log.")
        self.assertIsNone(mgmt.adapter_device_id)  # still unlinked

    def test_successful_link_clears_prior_error(self):
        """Once linking succeeds, a stale adapter_link_error from a prior failed attempt is cleared
        so the tab's failure banner disappears."""
        mgmt = self._make_mgmt(adapter_device_id=None)
        # Simulate a leftover error from an earlier failed link attempt.
        mirror_update(mgmt, adapter_link_error="earlier failure")

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 88}),
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=True)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_link_error, "")  # cleared on success
        self.assertEqual(mgmt.adapter_device_id, 88)

    def test_sync_notify_job_logged(self):
        mgmt = self._make_mgmt(adapter_device_id=None)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 10}),
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value={"job_id": 7}),
        ):
            with self.assertLogs("netbox_nso_plugin.signals", level="DEBUG"):
                self._sync_scope(mgmt, created=True)

    def test_created_none_adapter_id_triggers_onboard(self):
        """created=False but adapter_device_id=None should also trigger onboard."""
        mgmt = self._make_mgmt(adapter_device_id=None)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 3}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=False)

        mock_onboard.assert_called_once()

    def test_manage_enabled_included_in_scope(self):
        """manage_enabled=True includes 'enabled' in the managed_attributes scope call."""
        mgmt = self._make_mgmt(adapter_device_id=None, manage_enabled=True)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 5}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=True)

        mock_onboard.assert_called_once()
        # Both description and enabled should be in the scope call.
        mock_scope.assert_called_once_with(
            5, ["description", "enabled"], auto_apply=False, sync_before_apply=True, primary_ip=None, oob_ip=None
        )

    def test_scope_carries_primary_and_oob_ips(self):
        """The scope push carries the device's primary + OOB management IPs as bare host
        strings, so the adapter's failover loop can probe primary and fall back to OOB."""
        from ipam.models import IPAddress

        primary = IPAddress.objects.create(address="10.0.0.1/32", assigned_object=self.iface)
        oob = IPAddress.objects.create(address="192.0.2.5/24", assigned_object=self.iface)
        self.device.primary_ip4 = primary
        self.device.oob_ip = oob
        self.device.save()

        mgmt = self._make_mgmt(adapter_device_id=7)
        with (
            patch(f"{_MOD}.patch_device", return_value=None),
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            self._sync_scope(mgmt, created=False)

        _, kw = mock_scope.call_args
        self.assertEqual(kw["primary_ip"], "10.0.0.1")  # /32 stripped → host only
        self.assertEqual(kw["oob_ip"], "192.0.2.5")  # /24 stripped → host only


class TestAdapterLinkConcurrency(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        manufacturer = Manufacturer.objects.create(name="Signal race", slug="signal-race")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Signal race",
            slug="signal-race",
        )
        role = DeviceRole.objects.create(name="Signal race", slug="signal-race")
        site = Site.objects.create(name="Signal race", slug="signal-race")
        self.device = Device.objects.create(
            name="signal-race-device",
            device_type=device_type,
            role=role,
            site=site,
        )
        self.nso_instance = NSOInstance.objects.create(
            name="signal-race-instance",
            adapter_instance_id="signal-race-instance",
        )
        row = NSODeviceManagement(
            device=self.device,
            nso_instance=self.nso_instance,
            nso_device_name="signal-race-device",
            adapter_device_id=7,
        )
        _bulk_create_management_without_signals([row])
        self.mgmt = NSODeviceManagement.objects.get(pk=row.pk)

    def test_adapter_failure_survives_a_stale_error_mirror_plan(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.signals import _sync_committed_scope_to_adapter

        original_build = RendererMutationPlan.build
        plan_staled = False

        def build_then_stale(*args, **kwargs):
            nonlocal plan_staled
            plan = original_build(*args, **kwargs)
            if not plan_staled:
                plan_staled = True
                in_thread(
                    lambda: NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(last_sync_status="concurrent")
                )
            return plan

        errors = []
        with (
            patch.object(RendererMutationPlan, "build", side_effect=build_then_stale),
            patch(
                f"{_MOD}.set_scope",
                side_effect=AdapterError("adapter unavailable", code="nso_unreachable"),
            ),
            self.assertLogs("netbox_nso_plugin.signals", level="WARNING") as captured,
        ):
            try:
                _sync_committed_scope_to_adapter(type(self.mgmt), self.mgmt.pk, False)
            except Exception as exc:  # noqa: BLE001 (the assertion reports the escaped callback error)
                errors.append(exc)

        self.assertEqual(errors, [], f"the adapter callback raised {errors!r}")
        self.assertTrue(any("adapter unavailable" in message for message in captured.output))
        self.assertTrue(any("Skipped adapter error mirror update" in message for message in captured.output))
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.last_sync_status, "concurrent")
        self.assertEqual(self.mgmt.adapter_link_error, "")

    def test_new_source_rekey_waits_for_adapter_finalization(self):
        import select
        import threading

        from django.db import connection, transaction
        from psycopg import pq

        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.signals import _sync_source_change

        mirror_update(self.mgmt, source_rekey_pending=True)
        original_build = RendererMutationPlan.build
        committed = threading.Event()
        rekey_started = threading.Event()
        worker_pid = []
        worker_errors = []
        workers = []

        def commit_new_source():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_pid.append(cursor.fetchone()[0])
                with transaction.atomic():
                    pg_connection = connection.connection.pgconn
                    table = connection.ops.quote_name(NSODeviceManagement._meta.db_table)
                    query = (
                        f"UPDATE {table} SET nso_device_name = $1, source_rekey_pending = TRUE WHERE id = $2"
                    ).encode()
                    pg_connection.send_query_params(
                        query,
                        [b"newer-source", str(self.mgmt.pk).encode()],
                    )
                    while pg_connection.flush() == 1:
                        select.select([], [pg_connection.socket], [])
                    rekey_started.set()
                    while pg_connection.is_busy():
                        select.select([pg_connection.socket], [], [])
                        pg_connection.consume_input()
                    result = pg_connection.get_result()
                    if result is None or result.status != pq.ExecStatus.COMMAND_OK:
                        raise AssertionError("the competing source rekey UPDATE failed")
                    if pg_connection.get_result() is not None:
                        raise AssertionError("the competing source rekey returned an extra result")
                committed.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                worker_errors.append(exc)
            finally:
                connection.close()

        def build_after_rekey_starts(*args, **kwargs):
            if not workers:
                worker = threading.Thread(target=commit_new_source)
                workers.append(worker)
                worker.start()
                self.assertTrue(rekey_started.wait(10), "the competing source rekey did not start")
                wait_until_postgres_blocks(worker_pid[0], "the competing source rekey", timeout=3)
            return original_build(*args, **kwargs)

        client = SimpleNamespace(patch_device=lambda **kwargs: {"source_epoch": 2})
        with patch.object(RendererMutationPlan, "build", side_effect=build_after_rekey_starts):
            finalized = _sync_source_change(self.mgmt, client)

        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive(), "the competing source rekey did not finish")
        if worker_errors:
            raise worker_errors[0]

        self.assertTrue(finalized)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.nso_device_name, "newer-source")
        self.assertTrue(self.mgmt.source_rekey_pending, "adapter finalization cleared the newer source fence")
        self.assertTrue(committed.is_set())


class TestOffboardDeviceFromAdapter(unittest.TestCase):
    """Tests for the offboard_device_from_adapter signal handler.

    The handler reads exactly one attribute (``instance.adapter_device_id``) and calls
    the adapter ``delete_device`` boundary. A SimpleNamespace is the honest stand-in: it
    carries that field and raises AttributeError on anything else, whereas a MagicMock
    would silently fabricate any attribute. ``delete_device`` is the real external
    boundary and stays patched.
    """

    def setUp(self):
        """Run callbacks immediately, matching production's outer-autocommit path."""
        on_commit = patch("django.db.transaction.on_commit", side_effect=lambda callback: callback())
        on_commit.start()
        self.addCleanup(on_commit.stop)

    def test_offboards_when_adapter_device_id_set(self):
        from netbox_nso_plugin.signals import _queue_adapter_offboard

        instance = SimpleNamespace(adapter_device_id=55)
        with patch(f"{_MOD}.delete_device") as mock_delete:
            _queue_adapter_offboard(instance)

        mock_delete.assert_called_once_with(55)

    def test_skips_when_adapter_device_id_none(self):
        from netbox_nso_plugin.signals import _queue_adapter_offboard

        instance = SimpleNamespace(adapter_device_id=None)
        with patch(f"{_MOD}.delete_device") as mock_delete:
            _queue_adapter_offboard(instance)

        mock_delete.assert_not_called()

    def test_adapter_error_swallowed(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.signals import _queue_adapter_offboard

        instance = SimpleNamespace(adapter_device_id=5)
        with patch(f"{_MOD}.delete_device", side_effect=AdapterError("gone", code="not_found")):
            # Should not raise — a warning is logged instead.
            _queue_adapter_offboard(instance)

    def test_foreign_delete_receiver_does_not_schedule_offboarding(self):
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = SimpleNamespace(adapter_device_id=55)
        with patch(f"{_MOD}.delete_device") as mock_delete:
            offboard_device_from_adapter(sender=None, instance=instance)

        mock_delete.assert_not_called()


class TestPushIntentOnAccept(_SignalDBBase):
    """Tests for push_intent_on_accept (real overlay rows; put_intent is the boundary).

    The handler resolves the device's NSODeviceManagement, then schedules
    _push_interface_intent_for_device, which queries every OWNED NSOInterfaceState for
    the device and builds the put_intent payload. Driving it against real rows exercises
    that real filter + select_related + attribute-building — the part a MagicMock'd
    queryset (returning a hand-built [state]) entirely bypassed.
    """

    def test_skips_when_not_owned(self):
        from netbox_nso_plugin.models import NSOInterfaceState

        self._make_mgmt(adapter_device_id=7)
        # status not in OWNED_STATES → not owned.
        state = NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="imported", nso_value="x"
        )

        with patch(f"{_MOD}.put_intent") as mock_put:
            _invoke_push_intent_on_accept(state)

        mock_put.assert_not_called()

    def test_foreign_owned_overlay_save_does_not_schedule_interface_behavior(self):
        """A registered row save is behavior-neutral without its exact writer."""
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with (
            patch("netbox_nso_plugin.signals._schedule_intent_push") as mock_schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            state.status = "in_sync"
            state.save(update_fields=["status"])

        mock_schedule.assert_not_called()

    def test_skips_when_status_unowned_despite_stale_accepted_at(self):
        # Behavior change: ownership is status-based, NOT accepted_at. An attribute reverted/
        # drifted back to an unowned status keeps a stale accepted_at from a past acceptance
        # (accepted_at is never cleared) — it must NOT be pushed (the device-27 ae2.0 case).
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState

        self._make_mgmt(adapter_device_id=7)
        state = NSOInterfaceState.objects.create(
            interface=self.iface,
            attribute="description",
            status="imported",
            nso_value="",
            accepted_at=timezone.now(),  # stale ownership marker
        )

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                _invoke_push_intent_on_accept(state)

        mock_put.assert_not_called()

    def test_pushes_intent_on_accepted(self):
        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                _invoke_push_intent_on_accept(state)

        mock_put.assert_called_once()
        adapter_id, attrs = mock_put.call_args[0]
        self.assertEqual(adapter_id, 7)
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["interface"], "GigabitEthernet0/0")
        self.assertEqual(attrs[0]["attribute"], "description")
        self.assertEqual(attrs[0]["intent_value"], "uplink")  # from the real Interface.description

    def test_pushes_enabled_attribute(self):
        from dcim.models import Interface

        self._make_mgmt(adapter_device_id=3)
        iface = Interface.objects.create(device=self.device, name="Loopback0", type="virtual", enabled=False)
        state = self._accepted_state(iface, "enabled", nso_value="true")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                _invoke_push_intent_on_accept(state)

        attrs = mock_put.call_args[0][1]
        self.assertEqual(attrs[0]["intent_value"], "false")  # str(Interface.enabled).lower()

    def test_snapshot_includes_only_owned_status_rows(self):
        # The device-wide snapshot filters by status (OWNED_STATES), not accepted_at: an
        # owned (accepted) row is pushed; a stale-accepted_at row reverted to an unowned
        # status is NOT — even though accepted_at is set on both (device-27 ae2.0 fix).
        from dcim.models import Interface
        from django.utils import timezone

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOInterfaceState

        self._make_mgmt(adapter_device_id=42)
        # Owned: accepted status, real NetBox value to push.
        self.iface.description = "uplink to spine"
        self.iface.save(update_fields=["description"])
        self._accepted_state(self.iface, "description", nso_value="")
        # Unowned despite a stale accepted_at: reverted/drifted back to imported.
        other = Interface.objects.create(device=self.device, name="GigabitEthernet0/1", type="1000base-t")
        NSOInterfaceState.objects.create(
            interface=other,
            attribute="description",
            status="imported",
            nso_value="",
            accepted_at=timezone.now(),  # stale
        )

        with patch(f"{_MOD}.put_intent") as mock_put:
            deliver("interface", self.device.id, 42)

        mock_put.assert_called_once()
        attrs = mock_put.call_args[0][1]
        self.assertEqual([(a["interface"], a["attribute"]) for a in attrs], [("GigabitEthernet0/0", "description")])
        self.assertEqual(attrs[0]["intent_value"], "uplink to spine")

    def test_skips_when_mgmt_does_not_exist(self):
        # No NSODeviceManagement for this device → NSODeviceManagement.objects.get raises.
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                _invoke_push_intent_on_accept(state)

        mock_put.assert_not_called()

    def test_skips_when_adapter_id_none(self):
        """A management row without an adapter_device_id yet → nothing to push to."""
        self._make_mgmt(adapter_device_id=None)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                _invoke_push_intent_on_accept(state)

        mock_put.assert_not_called()

    def test_skips_unknown_attribute(self):
        """An owned row outside the wire schema schedules no interface behavior."""
        from netbox_nso_plugin.signals import interface_intent_item

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "mtu", nso_value="1500")

        self.assertIsNone(interface_intent_item(state))
        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                _invoke_push_intent_on_accept(state)

        mock_put.assert_not_called()

    def test_put_intent_error_is_swallowed(self):
        """put_intent raising AdapterError is caught and logged, not propagated."""
        from netbox_nso_plugin.adapter_client import AdapterError

        self._make_mgmt(adapter_device_id=3)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent", side_effect=AdapterError("down", code="nso_unreachable")):
            with self.captureOnCommitCallbacks(execute=True):
                # Should not raise — a warning is logged instead.
                _invoke_push_intent_on_accept(state)


class TestSkipOnRenderGuard(_SignalDBBase):
    """An intent push must never fire during a GET render.

    Regression for the device-27 NSO-tab loop: rendering the tab re-saves every
    NSOInterfaceState row, and each save of an 'accepted' row pushed the full intent
    snapshot — O(N) pushes per render, each O(N) — hanging the page and re-minting
    accepts. The @_skip_on_render guard drops the push when current_request is a GET.
    """

    def _fire_with_method(self, method):
        """Drive push_intent_on_accept with current_request set to a real GET/POST/None."""
        import uuid

        from django.contrib.auth import get_user_model
        from netbox.context import current_request

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        req = None if method is None else getattr(RequestFactory(), method.lower())("/")
        if req is not None:
            req.user = get_user_model().objects.create_user(username=f"render-{method.lower()}", password="x")
            req.id = uuid.uuid4()
        token = current_request.set(req)
        try:
            with patch(f"{_MOD}.put_intent") as mock_put:
                with self.captureOnCommitCallbacks(execute=True):
                    _invoke_push_intent_on_accept(state)
                return mock_put
        finally:
            current_request.reset(token)

    def test_get_render_does_not_push(self):
        """A GET (page render) is suppressed even for an accepted state."""
        self._fire_with_method("GET").assert_not_called()

    def test_post_accept_pushes(self):
        """An operator accept arrives as a POST — push proceeds."""
        self._fire_with_method("POST").assert_called_once()

    def test_no_request_pushes(self):
        """Programmatic / CLI context (no request) still pushes."""
        self._fire_with_method(None).assert_called_once()


class TestExactWriterNativeNotifications(unittest.TestCase):
    def test_flex_algo_save_schedules_the_exact_writer_scope(self):
        from netbox_nso_plugin.signals import _on_routing_isis_flex_algo_save

        with patch("netbox_nso_plugin.signals._schedule_exact_writer_scope") as schedule:
            _on_routing_isis_flex_algo_save(sender=None, instance=None)

        schedule.assert_called_once_with("isis_flex_algo")

    def test_flex_algo_delete_schedules_the_exact_writer_scope(self):
        from netbox_nso_plugin.signals import _on_routing_isis_flex_algo_pre_delete

        with patch("netbox_nso_plugin.signals._schedule_exact_writer_scope") as schedule:
            _on_routing_isis_flex_algo_pre_delete(sender=None, instance=None)

        schedule.assert_called_once_with("isis_flex_algo")


# ---------------------------------------------------------------------------
# TestIPAddressSignals — Django DB integration tests for the IP signal path
# ---------------------------------------------------------------------------

try:
    from django.test import TestCase as DjangoTestCase

    class TestIPAddressSignals(IntentPushDeliveryMixin, DjangoTestCase):
        """Django-DB integration tests for the IPAddress signal → put_ip_intent path."""

        @classmethod
        def setUpTestData(cls):
            from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

            from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

            manufacturer = Manufacturer.objects.create(name="IpSigMfg", slug="ipsigmfg")
            device_type = DeviceType.objects.create(manufacturer=manufacturer, model="IpSigDev", slug="ipsigdev")
            role = DeviceRole.objects.create(name="IpSigRole", slug="ipsigrole")
            site = Site.objects.create(name="IpSigSite", slug="ipsigsite")
            cls.device = Device.objects.create(name="ip-sig-router", device_type=device_type, role=role, site=site)
            cls.iface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")

            cls.unmanaged_device = Device.objects.create(
                name="ip-sig-unmanaged", device_type=device_type, role=role, site=site
            )
            cls.unmanaged_iface = Interface.objects.create(
                device=cls.unmanaged_device, name="GigabitEthernet0/0", type="1000base-t"
            )

            nso_instance = NSOInstance.objects.create(name="IpSigNSO", adapter_instance_id="nso-ipsig")

            # Bypass sync_scope_to_adapter signal
            _bulk_create_management_without_signals(
                [
                    NSODeviceManagement(
                        device=cls.device,
                        nso_instance=nso_instance,
                        nso_device_name="ip-sig-router",
                        adapter_device_id=42,
                        custom_field_data={},
                    )
                ]
            )

        def _ct(self):
            from dcim.models import Interface
            from django.contrib.contenttypes.models import ContentType

            return ContentType.objects.get_for_model(Interface)

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_foreign_post_save_does_not_acquire_or_push(self, mock_put):
            """A native IP save event is not persisted ownership evidence."""
            from ipam.models import IPAddress

            from netbox_nso_plugin.models import NSOInterfaceIPState

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="10.1.0.1/24", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )

            self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.iface, address="10.1.0.1/24").exists())
            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_foreign_greenfield_nokia_ip_does_not_push(self, mock_put):
            """A native IP event is not ownership evidence, including on a Nokia sub-interface."""
            from dcim.models import Interface
            from ipam.models import IPAddress

            lag = Interface.objects.create(device=self.device, name="lag-99", type="lag")
            sub = Interface.objects.create(device=self.device, name="LAG99:99", type="virtual", parent=lag)

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="198.18.249.160/31", assigned_object_type=self._ct(), assigned_object_id=sub.pk
                )

            mock_put.assert_not_called()

        def test_nokia_routed_binding_helper(self):
            """_nokia_routed_binding: only emits for a parented :tag interface."""
            from types import SimpleNamespace

            from netbox_nso_plugin.signals import _nokia_routed_binding

            parent = SimpleNamespace(name="lag-99")
            self.assertEqual(
                _nokia_routed_binding(SimpleNamespace(name="LAG99:99", parent=parent)),
                {"routed": True, "parent_binding": "lag-99", "encap_tag": "99"},
            )
            # no parent → not a sub-interface
            self.assertEqual(_nokia_routed_binding(SimpleNamespace(name="LAG99:99", parent=None)), {})
            # IOS/Junos dotted subif (no ':') → no-op
            self.assertEqual(_nokia_routed_binding(SimpleNamespace(name="Gi0/1.100", parent=parent)), {})
            # non-numeric suffix (e.g. VPRN logical name) → no encap tag to derive
            self.assertEqual(_nokia_routed_binding(SimpleNamespace(name="CRPD-VPN:LO7", parent=parent)), {})

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_conflict_state_blocks_push(self, mock_put):
            """Pre-existing conflict state blocks automatic acceptance and push."""
            from ipam.models import IPAddress

            from netbox_nso_plugin.models import NSOInterfaceIPState

            NSOInterfaceIPState.objects.create(
                interface=self.iface, address="10.1.1.1/24", vrf="", status="conflict", family="ipv4"
            )

            IPAddress.objects.create(
                address="10.1.1.1/24", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
            )

            state = NSOInterfaceIPState.objects.get(interface=self.iface, address="10.1.1.1/24")
            self.assertEqual(state.status, "conflict")
            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_suppress_intent_push_silences_ip_handler(self, mock_put):
            """Under suppress_intent_push() the IP handler must neither push intent nor
            force-promote a machine-owned 'imported' row to 'accepted'.

            This is the reconcile/import path (rqworker): suppress_intent_push() — not
            the GET-render guard — is what must keep the IP save from echoing intent back
            to the adapter and from re-minting 'accepted' rows the operator never clicked.
            """
            from ipam.models import IPAddress

            from netbox_nso_plugin.models import NSOInterfaceIPState
            from netbox_nso_plugin.signals import suppress_intent_push

            NSOInterfaceIPState.objects.create(
                interface=self.iface, address="10.2.0.1/24", vrf="", status="imported", family="ipv4"
            )

            address = IPAddress(
                address="10.2.0.1/24", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
            )
            from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction

            with (
                self.captureOnCommitCallbacks(execute=True),
                suppress_intent_push(),
                intent_transaction(footprint_for_instance(address)),
            ):
                address.save()

            state = NSOInterfaceIPState.objects.get(interface=self.iface, address="10.2.0.1/24", vrf="")
            self.assertEqual(state.status, "imported", "suppressed IP save must not force-promote to accepted")
            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_no_management_record_skips_push(self, mock_put):
            """IPAddress on an unmanaged device → no push."""
            from ipam.models import IPAddress

            IPAddress.objects.create(
                address="192.168.99.1/24", assigned_object_type=self._ct(), assigned_object_id=self.unmanaged_iface.pk
            )

            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_foreign_post_delete_does_not_push(self, mock_put):
            """A native IP delete event is not ownership evidence."""
            from ipam.models import IPAddress

            with self.captureOnCommitCallbacks(execute=True):
                ip = IPAddress.objects.create(
                    address="10.1.2.1/30", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )
            mock_put.reset_mock()

            with self.captureOnCommitCallbacks(execute=True):
                ip.delete()

            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_ip_not_assigned_skipped(self, mock_put):
            """IPAddress with no assignment → signal skips push."""
            from ipam.models import IPAddress

            IPAddress.objects.create(address="203.0.113.1/32")
            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_adapter_error_is_swallowed(self, mock_put):
            """put_ip_intent raising AdapterError is caught and does not propagate."""
            from ipam.models import IPAddress

            from netbox_nso_plugin.adapter_client import AdapterError

            mock_put.side_effect = AdapterError("down", code="nso_unreachable")

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="10.1.3.1/28", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )

    class TestGActivatedInterfaceIntentOrigin(IntentPushDeliveryMixin, DjangoTestCase):
        """Decision-G intent signal discriminates operator edits from adapter imports."""

        @classmethod
        def setUpTestData(cls):
            from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

            from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceState

            mfg = Manufacturer.objects.create(name="GsigMfg", slug="gsigmfg")
            dt = DeviceType.objects.create(manufacturer=mfg, model="GsigDev", slug="gsigdev")
            role = DeviceRole.objects.create(name="GsigRole", slug="gsigrole")
            site = Site.objects.create(name="GsigSite", slug="gsigsite")
            cls.device = Device.objects.create(name="gsig-router", device_type=dt, role=role, site=site)
            cls.iface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")
            inst = NSOInstance.objects.create(name="GsigNSO", adapter_instance_id="nso-gsig")
            _bulk_create_management_without_signals(
                [
                    NSODeviceManagement(
                        device=cls.device,
                        nso_instance=inst,
                        nso_device_name="gsig-router",
                        adapter_device_id=77,
                        manage_description=True,
                        manage_enabled=True,
                        custom_field_data={},
                    )
                ]
            )
            # An imported (not-yet-accepted) state the signal could promote.
            NSOInterfaceState.objects.create(
                interface=cls.iface, attribute="description", status="imported", nso_value="old"
            )

        def _fire(self, header=None):
            """Invoke the G-activated handler with current_request set to a real request.

            A non-None *header* is the adapter-import marker; it rides on a real POST
            (deliberately not a GET, so it is the import header — not the render guard —
            that suppresses the push). header=None models a programmatic write (no request).
            """
            from netbox.context import current_request

            req = RequestFactory().post("/", headers=header) if header is not None else None
            # Simulate the pre_save snapshot: operator changed the description,
            # left enabled untouched.
            self.iface._nso_old_values = {"description": "PREVIOUS-DESC", "enabled": self.iface.enabled}
            token = current_request.set(req)
            try:
                with patch("netbox_nso_plugin.adapter_client.put_intent") as mock_put:
                    with self.captureOnCommitCallbacks(execute=True):
                        _invoke_interface_edit(self.iface)
                    return mock_put
            finally:
                current_request.reset(token)

        def _state(self):
            from netbox_nso_plugin.models import NSOInterfaceState

            return NSOInterfaceState.objects.get(interface=self.iface, attribute="description")

        def test_foreign_native_edit_does_not_acquire_or_push(self):
            """A native save event is not persisted ownership evidence."""
            mock_put = self._fire(header=None)
            self.assertEqual(self._state().status, "imported")
            mock_put.assert_not_called()

        def test_adapter_origin_edit_is_skipped(self):
            """An adapter-origin write (import header) does NOT promote or push."""
            mock_put = self._fire(header={"X-NSO-Adapter-Import": "1"})
            self.assertEqual(self._state().status, "imported")  # unchanged
            mock_put.assert_not_called()

        def test_edit_does_not_own_untouched_attribute(self):
            """Editing description must NOT promote/own the untouched 'enabled' attribute.

            Regression: previously any save promoted every managed attribute, so
            editing the description silently owned enabled (a value never accepted).
            """
            from netbox.context import current_request

            from netbox_nso_plugin.models import NSOInterfaceState

            enabled_state = NSOInterfaceState.objects.create(
                interface=self.iface, attribute="enabled", status="imported", nso_value="True"
            )
            # description changed; enabled untouched.
            self.iface._nso_old_values = {"description": "PREVIOUS-DESC", "enabled": self.iface.enabled}
            token = current_request.set(None)
            try:
                with patch("netbox_nso_plugin.adapter_client.put_intent"):
                    with self.captureOnCommitCallbacks(execute=True):
                        _invoke_interface_edit(self.iface)
            finally:
                current_request.reset(token)

            enabled_state.refresh_from_db()
            self.assertEqual(enabled_state.status, "imported")
            self.assertIsNone(enabled_state.accepted_at)
            self.assertEqual(self._state().status, "imported")

    class TestForeignOspfSignals(IntentPushDeliveryMixin, DjangoTestCase):
        """Foreign native OSPF writes remain outside ownership and delivery."""

        @classmethod
        def setUpTestData(cls):
            from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

            from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

            mfg = Manufacturer.objects.create(name="OspfGfMfg", slug="ospfgfmfg")
            dt = DeviceType.objects.create(manufacturer=mfg, model="OspfGfDev", slug="ospfgfdev")
            role = DeviceRole.objects.create(name="OspfGfRole", slug="ospfgfrole")
            site = Site.objects.create(name="OspfGfSite", slug="ospfgfsite")
            cls.device = Device.objects.create(name="ospf-gf-rtr", device_type=dt, role=role, site=site)
            cls.iface = Interface.objects.create(device=cls.device, name="LAG99:99", type="virtual")
            nso_inst = NSOInstance.objects.create(name="OspfGfNSO", adapter_instance_id="nso-ospfgf")
            _bulk_create_management_without_signals(
                [
                    NSODeviceManagement(
                        device=cls.device,
                        nso_instance=nso_inst,
                        nso_device_name="ospf-gf-rtr",
                        adapter_device_id=77,
                        custom_field_data={},
                    )
                ]
            )

        @patch("netbox_nso_plugin.adapter_client.put_ospf_intent")
        def test_create_ospf_graph_does_not_acquire_or_push(self, mock_put):
            from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

            from netbox_nso_plugin.models import NSOOSPFInstanceState, NSOOSPFInterfaceState

            with self.captureOnCommitCallbacks(execute=True):
                inst = OSPFInstance.objects.create(
                    name="ospf-1", router_id="198.18.250.117", process_id="1", device=self.device
                )
                area = OSPFArea.objects.create(area_id="0", area_type="standard")
                OSPFInterface.objects.create(instance=inst, area=area, interface=self.iface, cost=100)

            self.assertFalse(NSOOSPFInstanceState.objects.exists())
            self.assertFalse(NSOOSPFInterfaceState.objects.exists())
            mock_put.assert_not_called()

except ImportError:
    pass  # Outside devcontainer — Django not available; tests skipped


class TestPushFailoverSettings(TestCase):
    """The NSOFailoverSettings post_save signal pushes the tuning to the adapter."""

    def test_save_pushes_full_config_payload(self):
        from netbox_nso_plugin.models import NSOFailoverSettings

        with patch(f"{_MOD}.put_failover_config") as mock_put:
            NSOFailoverSettings.objects.create(
                primary_probe_interval=20, oob_probe_interval=720, probe_concurrency=12, enabled=False
            )

        mock_put.assert_called_once()
        payload = mock_put.call_args.args[0]
        self.assertEqual(payload["primary_probe_interval"], 20)
        self.assertEqual(payload["oob_probe_interval"], 720)
        self.assertEqual(payload["probe_concurrency"], 12)
        self.assertIs(payload["enabled"], False)
        # The adapter applies a complete config, so all knobs are always sent.
        self.assertEqual(
            set(payload),
            {
                "enabled",
                "primary_probe_interval",
                "oob_probe_interval",
                "failure_threshold",
                "success_threshold",
                "probe_timeout",
                "active_probe_timeout",
                "probe_concurrency",
                "max_flips_per_tick",
                "sync_from_after_switch",
            },
        )

    def test_adapter_error_is_swallowed(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.models import NSOFailoverSettings

        with patch(f"{_MOD}.put_failover_config", side_effect=AdapterError("down", code="nso_unreachable")):
            NSOFailoverSettings.objects.create()  # must not raise
        self.assertEqual(NSOFailoverSettings.objects.count(), 1)


class TestOverlayDeletePushesReducedSnapshot(_SignalDBBase):
    """Deleting an owned overlay row must re-push the REDUCED intent snapshot —
    the WP7-P1 SNMP regression class: with only post_save wired, the adapter keeps
    applying the deleted row until some unrelated sibling is saved. Found live on
    sw01: deleting an applied SVI's overlay never retracted irb.987."""

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        return NSODeviceManagement.objects.create(
            device=self.device, nso_instance=self.nso_instance, nso_device_name="core-rtr-01", adapter_device_id=42
        )

    def test_svi_delete_pushes_reduced_snapshot(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOSVIState

        mgmt = self._mgmt()
        vlan = VLAN.objects.create(vid=987, name="sig-v987")
        with (
            patch("netbox_nso_plugin.adapter_client.put_svi_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOSVIState.objects.create(
                management=mgmt, interface=self.iface, vlan=vlan, svi_type="irb", status="accepted"
            )
        mock_put = self._delete_pushes(row, "put_svi_intent")
        _dev, interfaces = mock_put.call_args[0]
        self.assertEqual(interfaces, [], "Deleted SVI must not appear in the push snapshot")

    def test_subinterface_delete_pushes_reduced_snapshot(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOSubinterfaceState

        mgmt = self._mgmt()
        child = Interface.objects.create(device=self.device, name="GigabitEthernet0/0.99", type="virtual")
        with (
            patch("netbox_nso_plugin.adapter_client.put_subinterface_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOSubinterfaceState.objects.create(
                management=mgmt, interface=child, parent_interface=self.iface, dot1q_vlan=99, status="accepted"
            )
        self._delete_pushes(row, "put_subinterface_intent")

    def test_logging_host_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOLoggingHostState.objects.create(management=mgmt, address="198.51.100.7", status="accepted")
        mock_put = self._delete_pushes(row, "put_logging_intent")
        self.assertEqual(mock_put.call_args[0][1], [])

    def test_interface_mtu_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_interface_mtu_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOInterfaceMtuState.objects.create(
                management=mgmt, interface=self.iface, l2_mtu=9000, status="accepted"
            )
        self._delete_pushes(row, "put_interface_mtu_intent")

    # ── #105 sweep: the 13 families that had post_save ONLY (f282e9e class) ──
    # Each red-first test: create an OWNED row (push #1 fires and warms the
    # change-detection cache), then DELETE it — without a post_delete receiver no
    # push fires and the adapter keeps applying the deleted intent forever.

    def _delete_pushes(self, row, patch_target, expect_empty_list=True):
        """Delete *row* and assert the reduced snapshot push fired at the client boundary."""
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        with (
            patch(f"netbox_nso_plugin.adapter_client.{patch_target}") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            plan = RendererMutationPlan.build(deletes=(planned_delete(row),))
            with renderer_writes(plan) as writer:
                writer.delete(row)
        mock_put.assert_called_once()
        if expect_empty_list:
            self.assertEqual(mock_put.call_args[0][1], [], "Deleted row must not appear in the push snapshot")
        return mock_put

    def test_interface_state_delete_pushes_reduced_snapshot(self):
        """NSOInterfaceState is the one family with single AND bulk overlay delete views, so
        an operator can drop an owned description/enabled intent straight from the UI — but
        only post_save was wired, so the reduced snapshot was never pushed and the adapter
        kept applying the intent NetBox had just deleted."""
        from netbox_nso_plugin.models import NSOInterfaceState

        self._mgmt()  # links the device to the adapter (intent is keyed off the Interface FK)
        self.iface.description = "owned-by-nso"
        self.iface.save()
        with (
            patch("netbox_nso_plugin.adapter_client.put_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOInterfaceState.objects.create(
                interface=self.iface,
                attribute="description",
                nso_value="owned-by-nso",
                status="accepted",
            )
        self._delete_pushes(row, "put_intent")

    def test_vlan_delete_pushes_reduced_snapshot(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        mgmt = self._mgmt()
        vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=105, name="del-v105")
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent"), self.captureOnCommitCallbacks(execute=True):
            row = NSOVLANState.objects.create(management=mgmt, vlan=vlan, device_name="del-v105", status="accepted")
        self._delete_pushes(row, "put_vlan_intent")

    def test_bfd_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOBFDInterfaceState

        mgmt = self._mgmt()
        with patch("netbox_nso_plugin.adapter_client.put_bfd_intent"), self.captureOnCommitCallbacks(execute=True):
            row = NSOBFDInterfaceState.objects.create(
                management=mgmt, interface=self.iface, min_tx=300, min_rx=300, multiplier=3, status="accepted"
            )
        self._delete_pushes(row, "put_bfd_intent")

    def test_static_route_overlay_delete_pushes_reduced_snapshot(self):
        """Direct OVERLAY deletion (the native StaticRoute pre_delete path is separately
        covered) must push the reduced snapshot itself."""
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._mgmt()
        # No devices M2M — the greenfield-accept signal must not interfere here.
        route = StaticRoute.objects.create(prefix="198.18.99.0/24", next_hop="198.18.0.1", name="del-sr", metric=1)
        with (
            patch("netbox_nso_plugin.adapter_client.put_static_route_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOStaticRouteState.objects.create(
                management=mgmt, static_route=route, nso_prefix="198.18.99.0/24", status="accepted"
            )
        self._delete_pushes(row, "put_static_route_intent")

    def test_static_route_overlay_delete_records_per_object_authority(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOStaticRouteState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        mgmt = self._mgmt()
        route = StaticRoute.objects.create(prefix="198.18.98.0/24", next_hop="198.18.0.1", metric=1)
        row = NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=route,
            nso_prefix="198.18.98.0/24",
            status="accepted",
        )
        plan = RendererMutationPlan.build(deletes=(planned_delete(row),))

        with self.captureOnCommitCallbacks(execute=False), renderer_writes(plan) as writer:
            writer.delete(row)

        entry = NSOIntentOutboxEntry.objects.get(
            device=self.device,
            scope="static_route",
            consumed_by_push_seq__isnull=True,
        )
        self.assertTrue(entry.mark_and)
        self.assertEqual(
            entry.transitions,
            [
                {
                    "op": "delete",
                    "route_id": route.pk,
                    "triples": [{"vrf": "", "prefix": "198.18.98.0/24", "next_hop": ""}],
                    "unverified": True,
                }
            ],
        )

    def test_foreign_l2_sap_delete_does_not_push(self):
        from netbox_nso_plugin.models import NSOL2SapState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOL2SapState.objects.create(
                management=mgmt,
                service_name="TL",
                service_type="epipe",
                sap_id="lag-60:3999",
                port="lag-60",
                outer_tag=3999,
                status="accepted",
            )
        with (
            patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent") as push,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
        push.assert_not_called()

    def test_l2_sap_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOL2SapState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOL2SapState.objects.create(
                management=mgmt,
                service_name="TL",
                service_type="epipe",
                sap_id="lag-60:3999",
                port="lag-60",
                outer_tag=3999,
                status="accepted",
            )
        self._delete_pushes(row, "put_l2_sap_intent")

    def test_isis_flex_algo_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOISISFlexAlgoState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOISISFlexAlgoState.objects.create(
                management=mgmt, process_tag="CORE", algo_id=130, status="accepted"
            )
        self._delete_pushes(row, "put_isis_flex_algo_intent")

    def test_isis_interface_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOISISInterfaceState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOISISInterfaceState.objects.create(
                management=mgmt, interface=self.iface, af="ipv4", status="accepted"
            )
        self._delete_pushes(row, "put_isis_interface_intent")

    def test_isis_instance_delete_pushes_reduced_snapshot(self):
        """No native pre_delete exists for ISISInstance — the overlay post_delete is the
        ONLY retraction path for this family."""
        from netbox_nso_plugin.models import NSOISISInstanceState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOISISInstanceState.objects.create(management=mgmt, process_tag="CORE", status="accepted")
        mock_put = self._delete_pushes(row, "put_isis_interface_intent")
        self.assertEqual(mock_put.call_args.kwargs.get("processes"), [])

    def test_bgp_peer_delete_pushes_reduced_snapshot(self):
        """An exact overlay deletion pushes the reduced owned snapshot."""
        from dcim.models import Device
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        from netbox_nso_plugin.models import NSOBGPPeerState

        mgmt = self._mgmt()
        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent"), self.captureOnCommitCallbacks(execute=True):
            rir = RIR.objects.create(name="Signal private ASNs", slug="signal-private-asns", is_private=True)
            local_as = ASN.objects.create(asn=64512, rir=rir)
            remote_as = ASN.objects.create(asn=64513, rir=rir)
            router = BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(Device),
                assigned_object_id=self.device.pk,
                asn=local_as,
                name=str(local_as.asn),
            )
            scope = BGPScope.objects.create(router=router)
            peer = BGPPeer.objects.create(
                scope=scope,
                peer=IPAddress.objects.create(address="198.18.0.2/32"),
                remote_as=remote_as,
                enabled=True,
            )
            row = NSOBGPPeerState.objects.create(
                management=mgmt,
                bgp_peer=peer,
                asn_str=str(local_as.asn),
                peer_address_str="198.18.0.2",
                remote_as_str=str(remote_as.asn),
                status="accepted",
            )
        self._delete_pushes(row, "put_bgp_intent")

    def test_redistribution_delete_pushes_reduced_snapshot(self):
        """No native pre_delete exists for Redistribution — the overlay post_delete is
        the ONLY retraction path. The push rides the DEST protocol (bgp here)."""
        from netbox_nso_plugin.models import NSORedistributionState

        mgmt = self._mgmt()
        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent"), self.captureOnCommitCallbacks(execute=True):
            row = NSORedistributionState.objects.create(
                management=mgmt,
                dest_protocol="bgp",
                dest_ref="65100::ipv4-unicast",
                source_protocol="connected",
                source_ref="",
                status="accepted",
            )
        self._delete_pushes(row, "put_bgp_intent")

    def test_route_policy_delete_pushes_reduced_snapshot(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        mgmt = self._mgmt()
        pl = PrefixList.objects.create(name="PL-DEL-105")
        with (
            patch("netbox_nso_plugin.adapter_client.put_route_policy_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSORoutePolicyState.objects.create(
                management=mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status="accepted",
            )
        self._delete_pushes(row, "put_route_policy_intent")

    def test_ospf_instance_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOOSPFInstanceState

        mgmt = self._mgmt()
        with patch("netbox_nso_plugin.adapter_client.put_ospf_intent"), self.captureOnCommitCallbacks(execute=True):
            row = NSOOSPFInstanceState.objects.create(
                management=mgmt, process_id="999", ospf_instance=None, status="accepted"
            )
        self._delete_pushes(row, "put_ospf_intent", expect_empty_list=False)

    def test_ospf_interface_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOOSPFInterfaceState

        mgmt = self._mgmt()
        with patch("netbox_nso_plugin.adapter_client.put_ospf_intent"), self.captureOnCommitCallbacks(execute=True):
            row = NSOOSPFInterfaceState.objects.create(
                management=mgmt, interface=self.iface, process_id="10", area_id="0.0.0.0", status="accepted"
            )
        self._delete_pushes(row, "put_ospf_intent", expect_empty_list=False)

    def test_lacp_bundle_delete_pushes_reduced_snapshot(self):
        """LACP rides the direct-apply path and is auto_apply-gated on save; deletion
        retracts under the same gate (matching the save-path semantics)."""
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSODeviceManagement, NSOLACPBundleState

        mgmt = NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=self.nso_instance,
            nso_device_name="core-rtr-01",
            adapter_device_id=42,
            auto_apply=True,
        )
        lag = Interface.objects.create(device=self.device, name="Port-channel10", type="lag")
        with patch("netbox_nso_plugin.adapter_client.apply_lag_config"), self.captureOnCommitCallbacks(execute=True):
            row = NSOLACPBundleState.objects.create(
                management=mgmt,
                interface=lag,
                lag_id=10,
                min_links=2,
                system_priority=100,
                timer="fast",
                status="accepted",
            )
        self._delete_pushes(row, "apply_lag_config")

    def test_lacp_member_delete_pushes_reduced_snapshot(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSODeviceManagement, NSOLACPBundleState, NSOLACPMemberState

        mgmt = NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=self.nso_instance,
            nso_device_name="core-rtr-01",
            adapter_device_id=42,
            auto_apply=True,
        )
        lag = Interface.objects.create(device=self.device, name="Port-channel11", type="lag")
        member_iface = Interface.objects.create(device=self.device, name="Gi9/1", type="1000base-t")
        with patch("netbox_nso_plugin.adapter_client.apply_lag_config"), self.captureOnCommitCallbacks(execute=True):
            NSOLACPBundleState.objects.create(
                management=mgmt,
                interface=lag,
                lag_id=11,
                min_links=1,
                system_priority=100,
                timer="fast",
                status="accepted",
            )
            member = NSOLACPMemberState.objects.create(
                management=mgmt,
                interface=member_iface,
                lag_bundle=lag,
                mode="active",
                port_priority=128,
                status="accepted",
            )
        self._delete_pushes(member, "apply_lag_config", expect_empty_list=False)

    def test_switchport_delete_pushes_reduced_snapshot(self):
        """Switchport rides the direct-apply path and is auto_apply-gated on save;
        deletion retracts under the same gate (matching the save-path semantics)."""
        from netbox_nso_plugin.models import NSODeviceManagement, NSOSwitchportState

        mgmt = NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=self.nso_instance,
            nso_device_name="core-rtr-01",
            adapter_device_id=42,
            auto_apply=True,
        )
        with (
            patch("netbox_nso_plugin.adapter_client.apply_switchport_config"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOSwitchportState.objects.create(
                management=mgmt, interface=self.iface, mode="trunk", status="accepted"
            )
        self._delete_pushes(row, "apply_switchport_config")


class TestDeleteOriginMarking(_SignalDBBase):
    """#106/#1503: only proven deletion authority may let the adapter retract.

    Query-mode scopes carry ``?delete_origin=true``. Activated static-route pushes carry
    per-object authority in ``deleted_routes`` and never send that query parameter.
    """

    _CFG = {
        "url": "http://adapter",
        "token": "tok",
        "verify_tls": True,
        "ca_cert_path": None,
        "timeout": 30,
    }

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        return NSODeviceManagement.objects.create(
            device=self.device, nso_instance=self.nso_instance, nso_device_name="core-rtr-01", adapter_device_id=43
        )

    def _recorded_requests(self, act):
        """Run *act* with the adapter HTTP transport recorded."""
        from ._adapter_http import make_response, make_session

        session = make_session(response=make_response(200, json_data={}))
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=self._CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            self.captureOnCommitCallbacks(execute=True),
        ):
            act()
        return [
            (c.args[0], c.args[1], c.kwargs.get("params") or {}, c.kwargs.get("json"))
            for c in session.request.call_args_list
        ]

    def _recorded_calls(self, act):
        """Run *act* and return its adapter method, URL, and query parameters."""
        return [(method, url, params) for method, url, params, _body in self._recorded_requests(act)]

    def _recorded_params(self, act):
        """Run *act* with the adapter HTTP transport recorded → list of params dicts."""
        return [params for _method, _url, params in self._recorded_calls(act)]

    @staticmethod
    def _arranged():
        """Own the fixture row the way an ALREADY-DRAINED transaction left it.

        The mark survives the fold only when every unconsumed contributor carried it (AND),
        and in production the transaction that owned the row drained and retired its own
        entry long before the deletion under test appends one. A ``TestCase`` cannot drain,
        so an un-suppressed fixture would leave its unmarked entry standing and AND the
        deletion's mark away — a harness artifact, not the behaviour these pins are about.
        """
        from netbox_nso_plugin.signals import suppress_intent_push

        return suppress_intent_push()

    @staticmethod
    def _body_deletes_route(body, route_id):
        return isinstance(body, dict) and any(
            isinstance(record, dict) and record.get("route_id") == route_id for record in body.get("deleted_routes", [])
        )

    def _owned_svi(self, mgmt):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOSVIState

        vlan = VLAN.objects.create(vid=444, name="do-v444")
        state = NSOSVIState(management=mgmt, interface=self.iface, vlan=vlan, svi_type="irb", status="accepted")
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        plan = RendererMutationPlan.build(
            saves=(planned_save(state, natural_key=("management", "interface", "vlan", "svi_type")),)
        )
        with self._arranged(), renderer_writes(plan) as writer:
            writer.save(state)
        return state

    @staticmethod
    def _delete_with_writer(row):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_delete, renderer_writes

        plan = RendererMutationPlan.build(deletes=(planned_delete(row),))
        with renderer_writes(plan) as writer:
            writer.delete(row)

    @staticmethod
    def _save_with_writer(row):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        plan = RendererMutationPlan.build(saves=(planned_save(row),))
        with renderer_writes(plan) as writer:
            writer.save(row)

    def test_overlay_delete_push_is_marked_delete_origin(self):
        mgmt = self._mgmt()
        row = self._owned_svi(mgmt)
        params = self._recorded_params(lambda: self._delete_with_writer(row))
        self.assertTrue(params, "the delete must push")
        self.assertTrue(
            any(p.get("delete_origin") == "true" for p in params),
            f"delete push must be marked delete_origin; saw params {params}",
        )

    def test_unown_save_push_is_unmarked(self):
        mgmt = self._mgmt()
        row = self._owned_svi(mgmt)

        def _unown():
            row.status = "imported"
            self._save_with_writer(row)

        params = self._recorded_params(_unown)
        self.assertTrue(params, "the un-own shrink must push")
        self.assertFalse(
            any("delete_origin" in p for p in params),
            f"an un-own push must stay unmarked (detach-safe); saw params {params}",
        )

    def test_native_static_route_delete_uses_no_query_flag(self):
        """The native pre_delete safety-net path (routing.StaticRoute) is a deletion, so
        its activated push must leave the legacy query flag off."""
        from netbox_routing.models import StaticRoute

        mgmt = self._mgmt()
        route = StaticRoute.objects.create(prefix="198.18.77.0/24", next_hop="198.18.0.1", name="do-sr", metric=1)
        from ._static_route_case import _assign_and_accept, _delete_owned_route

        with self._arranged():
            _assign_and_accept(route, mgmt.device)
        route_id = route.pk
        requests = self._recorded_requests(lambda: _delete_owned_route(route))
        params = [params for _method, _url, params, _body in requests]
        self.assertTrue(params, "the native delete must push")
        self.assertFalse(
            any("delete_origin" in item for item in params),
            f"activated static-route pushes must not use the query flag; saw params {params}",
        )
        bodies = [body for _method, _url, _params, body in requests]
        self.assertTrue(
            any(self._body_deletes_route(body, route_id) for body in bodies),
            f"the deleted route must carry per-object authority; saw bodies {bodies}",
        )

    def test_assigning_a_device_to_a_static_route_is_unmarked(self):
        """m2m_changed is not a deletion-only signal: it also carries post_add. Registering
        the handler under _as_delete_origin stamped the ADD's push ?delete_origin=true —
        authorizing the adapter to retract from the LIVE device any static route the
        full-replace snapshot happens not to carry (e.g. one un-owned earlier).
        """
        from netbox_routing.models import StaticRoute

        mgmt = self._mgmt()
        route = StaticRoute.objects.create(prefix="198.18.88.0/24", next_hop="198.18.0.1", name="do-add", metric=1)

        from ._static_route_case import _assign_and_accept

        def assign():
            _assign_and_accept(route, mgmt.device)

        params = self._recorded_params(assign)
        self.assertTrue(params, "assigning the device must push the (grown) snapshot")
        self.assertFalse(
            any("delete_origin" in p for p in params),
            f"an ADD is not a deletion — its push must stay unmarked; saw params {params}",
        )

    def test_unassigning_a_device_from_a_static_route_uses_no_query_flag(self):
        """post_remove is a deletion, but its activated push uses no legacy query flag."""
        from netbox_routing.models import StaticRoute

        mgmt = self._mgmt()
        route = StaticRoute.objects.create(prefix="198.18.88.0/24", next_hop="198.18.0.1", name="do-rm", metric=1)
        from ._static_route_case import _assign_and_accept, _unassign_and_retire

        with self._arranged():
            _assign_and_accept(route, mgmt.device)

        requests = self._recorded_requests(lambda: _unassign_and_retire(route, mgmt.device))
        params = [params for _method, _url, params, _body in requests]
        self.assertTrue(params, "un-assigning the device must push the reduced snapshot")
        self.assertFalse(
            any("delete_origin" in item for item in params),
            f"activated static-route pushes must not use the query flag; saw params {params}",
        )
        bodies = [body for _method, _url, _params, body in requests]
        self.assertTrue(
            any(self._body_deletes_route(body, route.pk) for body in bodies),
            f"the unassigned route must carry per-object authority; saw bodies {bodies}",
        )

    # ── teardown must never be read as a retraction ───────────────────────────
    #
    # Deleting NSODeviceManagement (unmanage) — or the Device, which CASCADEs into it —
    # tears down every NSO*State overlay row. Each of those post_deletes is
    # _as_delete_origin-wrapped, so an unguarded teardown schedules a push per scope that
    # builds its snapshot from the now-EMPTY overlay and ships it as ?delete_origin=true:
    # the adapter reads an authorized full-replace to nothing and retracts every NSO-owned
    # service from the LIVE device. Unmanaging is a NetBox-side bookkeeping act — the only
    # adapter call it may make is the offboard DELETE.

    def _assert_teardown_touched_only_the_offboard(self, calls):
        retracting = [(m, u, p) for m, u, p in calls if p.get("delete_origin") == "true"]
        self.assertEqual(
            retracting,
            [],
            f"teardown must never push a delete-origin snapshot (it would retract from the live device); saw {retracting}",
        )
        self.assertEqual(
            [(m, u) for m, u, _p in calls],
            [("DELETE", "http://adapter/api/v1/devices/43")],
            f"the only adapter call for a teardown is the offboard DELETE; saw {calls}",
        )

    def test_unmanaging_a_device_pushes_no_intent(self):
        from netbox_nso_plugin.management_lifecycle import delete_management

        mgmt = self._mgmt()
        self._owned_svi(mgmt)
        self._assert_teardown_touched_only_the_offboard(self._recorded_calls(lambda: delete_management(mgmt)))

    def test_deleting_a_device_pushes_no_intent(self):
        from django.db.models.signals import pre_delete

        from netbox_nso_plugin.models import NSOSVIState

        mgmt = self._mgmt()
        self._owned_svi(mgmt)
        origins = []

        def capture_origin(sender, origin, **kwargs):
            origins.append(origin)

        pre_delete.connect(capture_origin, sender=NSOSVIState, weak=False)
        try:
            calls = self._recorded_calls(self.device.delete)
        finally:
            pre_delete.disconnect(capture_origin, sender=NSOSVIState)

        self.assertEqual(origins, [self.device])
        self._assert_teardown_touched_only_the_offboard(calls)

    def test_bulk_deleting_a_device_pushes_no_intent(self):
        mgmt = self._mgmt()
        self._owned_svi(mgmt)

        calls = self._recorded_calls(lambda: type(self.device).objects.filter(pk=self.device.pk).delete())

        self._assert_teardown_touched_only_the_offboard(calls)


class TestSourceRekeyLocksOnlyItsManagementRow(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The rekey's first locking read joins NSOInstance, and a bare FOR UPDATE locks every
    joined table, so it also held the instance row that every managed device shares.
    """

    def setUp(self):
        super().setUp()
        from ._outbox_case import make_managed, mirror_update

        self.device, self.mgmt = make_managed("rekeylock", 8801)
        mirror_update(self.mgmt, source_rekey_pending=True)
        self.mgmt.refresh_from_db()

    def _can_lock(self, table, pk):
        """Whether a second connection can take that row now, without waiting for the rekey."""
        from django.db import connection
        from django.db.utils import OperationalError

        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT 1 FROM {table} WHERE id = %s FOR UPDATE NOWAIT", [pk])
                cursor.fetchone()
        except OperationalError:
            return False
        return True

    def test_the_rekey_leaves_the_shared_nso_instance_row_lockable(self):
        from netbox_nso_plugin import signals
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        held = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []
        probes: dict[str, bool] = {}

        def hold_the_first_locking_read(execute, sql, params, many, context):
            # Only the rekey's first query joins the instance table into a locking read.
            if held.is_set() or "FOR UPDATE" not in sql or NSOInstance._meta.db_table not in sql:
                return execute(sql, params, many, context)
            rows = execute(sql, params, many, context)
            held.set()
            assert release.wait(timeout=30), "the rekey hold was never released"
            return rows

        def rekey():
            try:
                # The post_save receiver now runs only under a renderer writer, so drive the
                # committed callback it schedules: the rekey seam a plain save still reaches.
                with connections["default"].execute_wrapper(hold_the_first_locking_read):
                    signals._sync_committed_scope_to_adapter(NSODeviceManagement, self.mgmt.pk, created=False)
            except BaseException as exc:  # noqa: BLE001 (re-raised on the caller's thread)
                failures.append(exc)
            finally:
                held.set()
                connections.close_all()

        with (
            patch(f"{_MOD}.patch_device", return_value={"source_epoch": 9}) as mock_patch,
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
            patch(f"{_MOD}.onboard_device") as mock_onboard,
        ):
            worker = threading.Thread(target=rekey, name="source-rekey")
            worker.start()
            self.addCleanup(worker.join, 30)
            self.addCleanup(release.set)
            assert held.wait(timeout=30), failures
            try:
                probes["instance"] = self._can_lock(NSOInstance._meta.db_table, self.mgmt.nso_instance_id)
                probes["management"] = self._can_lock(NSODeviceManagement._meta.db_table, self.mgmt.pk)
            finally:
                release.set()
            worker.join(timeout=30)

        assert not worker.is_alive(), "the rekey never finished"
        assert not failures, failures
        self.assertTrue(probes["instance"], "the rekey locked the NSOInstance row every device shares")
        self.assertFalse(probes["management"], "the rekey did not hold its own management row")
        mock_patch.assert_called_once()
        mock_onboard.assert_not_called()
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.adapter_source_epoch, 9)
        self.assertFalse(self.mgmt.source_rekey_pending)
