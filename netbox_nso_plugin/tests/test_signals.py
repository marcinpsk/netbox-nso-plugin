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

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils import timezone

from .mixins import IntentPushResetMixin

_MOD = "netbox_nso_plugin.adapter_client"


class _SignalDBBase(IntentPushResetMixin, TestCase):
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

        NSODeviceManagement.objects.bulk_create(
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
        NSOInterfaceState.objects.filter(pk=state.pk).update(status="accepted", accepted_at=now)
        state.status = "accepted"
        state.accepted_at = now
        return state


class TestSyncScopeToAdapter(_SignalDBBase):
    """Tests for the sync_scope_to_adapter signal handler (real NSODeviceManagement row)."""

    def _sync_scope(self, instance, *, created):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        with self.captureOnCommitCallbacks(execute=True):
            sync_scope_to_adapter(sender=type(instance), instance=instance, created=created)

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
        type(mgmt).objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)
        mgmt.source_rekey_pending = True

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

    def test_source_save_is_fail_closed_before_its_on_commit_callback(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE, gated_family_run

        mgmt = self._make_mgmt(adapter_device_id=7)
        mgmt.nso_device_name = "replacement-source"
        with (
            self.captureOnCommitCallbacks(execute=False) as callbacks,
            patch(f"{_MOD}.patch_device") as mock_patch,
        ):
            mgmt.save()

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
        type(mgmt).objects.filter(pk=mgmt.pk).update(adapter_source_epoch=4, source_epoch_aware=True)
        mgmt.refresh_from_db()
        type(mgmt).objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)
        mgmt.source_rekey_pending = True

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
        self.assertIn("omitted source_epoch", mgmt.adapter_link_error)

    def test_old_wire_rekey_cannot_reopen_legacy_admission(self):
        from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE, gated_family_run

        mgmt = self._make_mgmt(adapter_device_id=7)
        type(mgmt).objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)
        mgmt.source_rekey_pending = True
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
        type(mgmt).objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)
        mgmt.source_rekey_pending = True

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
        type(mgmt).objects.filter(pk=mgmt.pk).update(
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
        type(mgmt).objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)
        mgmt.source_rekey_pending = True

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
        type(mgmt).objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)
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
        self.assertIn("nso down", mgmt.adapter_link_error)  # recorded, not swallowed
        self.assertIsNone(mgmt.adapter_device_id)  # still unlinked

    def test_successful_link_clears_prior_error(self):
        """Once linking succeeds, a stale adapter_link_error from a prior failed attempt is cleared
        so the tab's failure banner disappears."""
        mgmt = self._make_mgmt(adapter_device_id=None)
        # Simulate a leftover error from an earlier failed link attempt.
        type(mgmt).objects.filter(pk=mgmt.pk).update(adapter_link_error="earlier failure")
        mgmt.refresh_from_db()

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


class TestOffboardDeviceFromAdapter(unittest.TestCase):
    """Tests for the offboard_device_from_adapter signal handler.

    The handler reads exactly one attribute (``instance.adapter_device_id``) and calls
    the adapter ``delete_device`` boundary. A SimpleNamespace is the honest stand-in: it
    carries that field and raises AttributeError on anything else, whereas a MagicMock
    would silently fabricate any attribute. ``delete_device`` is the real external
    boundary and stays patched.
    """

    def test_offboards_when_adapter_device_id_set(self):
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = SimpleNamespace(adapter_device_id=55)
        with patch(f"{_MOD}.delete_device") as mock_delete:
            offboard_device_from_adapter(sender=None, instance=instance)

        mock_delete.assert_called_once_with(55)

    def test_skips_when_adapter_device_id_none(self):
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = SimpleNamespace(adapter_device_id=None)
        with patch(f"{_MOD}.delete_device") as mock_delete:
            offboard_device_from_adapter(sender=None, instance=instance)

        mock_delete.assert_not_called()

    def test_adapter_error_swallowed(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = SimpleNamespace(adapter_device_id=5)
        with patch(f"{_MOD}.delete_device", side_effect=AdapterError("gone", code="not_found")):
            # Should not raise — a warning is logged instead.
            offboard_device_from_adapter(sender=None, instance=instance)


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
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        # status not in OWNED_STATES → not owned.
        state = NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="imported", nso_value="x"
        )

        with patch(f"{_MOD}.put_intent") as mock_put:
            push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_not_called()

    def test_skips_when_status_unowned_despite_stale_accepted_at(self):
        # Behavior change: ownership is status-based, NOT accepted_at. An attribute reverted/
        # drifted back to an unowned status keeps a stale accepted_at from a past acceptance
        # (accepted_at is never cleared) — it must NOT be pushed (the device-27 ae2.0 case).
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

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
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_not_called()

    def test_pushes_intent_on_accepted(self):
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_called_once()
        adapter_id, attrs = mock_put.call_args[0]
        self.assertEqual(adapter_id, 7)
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["interface"], "GigabitEthernet0/0")
        self.assertEqual(attrs[0]["attribute"], "description")
        self.assertEqual(attrs[0]["intent_value"], "uplink")  # from the real Interface.description

    def test_pushes_enabled_attribute(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=3)
        iface = Interface.objects.create(device=self.device, name="Loopback0", type="virtual", enabled=False)
        state = self._accepted_state(iface, "enabled", nso_value="true")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        attrs = mock_put.call_args[0][1]
        self.assertEqual(attrs[0]["intent_value"], "false")  # str(Interface.enabled).lower()

    def test_snapshot_includes_only_owned_status_rows(self):
        # The device-wide snapshot filters by status (OWNED_STATES), not accepted_at: an
        # owned (accepted) row is pushed; a stale-accepted_at row reverted to an unowned
        # status is NOT — even though accepted_at is set on both (device-27 ae2.0 fix).
        from dcim.models import Interface
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import _push_interface_intent_for_device

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
            _push_interface_intent_for_device(self.device.id, 42, force=True)

        mock_put.assert_called_once()
        attrs = mock_put.call_args[0][1]
        self.assertEqual([(a["interface"], a["attribute"]) for a in attrs], [("GigabitEthernet0/0", "description")])
        self.assertEqual(attrs[0]["intent_value"], "uplink to spine")

    def test_skips_when_mgmt_does_not_exist(self):
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        # No NSODeviceManagement for this device → NSODeviceManagement.objects.get raises.
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_not_called()

    def test_skips_when_adapter_id_none(self):
        """A management row without an adapter_device_id yet → nothing to push to."""
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=None)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_not_called()

    def test_skips_unknown_attribute(self):
        """An owned state with an attribute outside (description, enabled) is dropped."""
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "mtu", nso_value="1500")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        # put_intent is still called, but the unknown attribute was filtered out.
        attrs = mock_put.call_args[0][1]
        self.assertEqual(attrs, [])

    def test_put_intent_error_is_swallowed(self):
        """put_intent raising AdapterError is caught and logged, not propagated."""
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=3)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent", side_effect=AdapterError("down", code="nso_unreachable")):
            with self.captureOnCommitCallbacks(execute=True):
                # Should not raise — a warning is logged instead.
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)


class TestSkipOnRenderGuard(_SignalDBBase):
    """An intent push must never fire during a GET render.

    Regression for the device-27 NSO-tab loop: rendering the tab re-saves every
    NSOInterfaceState row, and each save of an 'accepted' row pushed the full intent
    snapshot — O(N) pushes per render, each O(N) — hanging the page and re-minting
    accepts. The @_skip_on_render guard drops the push when current_request is a GET.
    """

    def _fire_with_method(self, method):
        """Drive push_intent_on_accept with current_request set to a real GET/POST/None."""
        from netbox.context import current_request

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        req = None if method is None else getattr(RequestFactory(), method.lower())("/")
        token = current_request.set(req)
        try:
            with patch(f"{_MOD}.put_intent") as mock_put:
                with self.captureOnCommitCallbacks(execute=True):
                    push_intent_on_accept(sender=NSOInterfaceState, instance=state)
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


# ---------------------------------------------------------------------------
# TestIPAddressSignals — Django DB integration tests for the IP signal path
# ---------------------------------------------------------------------------

try:
    from django.test import TestCase as DjangoTestCase

    class TestIPAddressSignals(IntentPushResetMixin, DjangoTestCase):
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
            NSODeviceManagement.objects.bulk_create(
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
        def test_post_save_creates_ip_state_accepted_and_pushes(self, mock_put):
            """Creating an IPAddress on a managed interface → state=accepted + push."""
            from ipam.models import IPAddress

            from netbox_nso_plugin.models import NSOInterfaceIPState

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="10.1.0.1/24", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )

            state = NSOInterfaceIPState.objects.get(interface=self.iface, address="10.1.0.1/24", vrf="")
            self.assertEqual(state.status, "accepted")
            self.assertIsNotNone(state.accepted_at)

            mock_put.assert_called_once()
            call_device_id, call_addresses = mock_put.call_args[0]
            self.assertEqual(call_device_id, 42)
            self.assertEqual(len(call_addresses), 1)
            self.assertEqual(call_addresses[0]["address"], "10.1.0.1/24")
            self.assertEqual(call_addresses[0]["interface"], "GigabitEthernet0/0")

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_greenfield_nokia_routed_binding_in_push(self, mock_put):
            """A parented LAG99:99 sub-interface pushes routed/parent_binding/encap_tag."""
            from dcim.models import Interface
            from ipam.models import IPAddress

            lag = Interface.objects.create(device=self.device, name="lag-99", type="lag")
            sub = Interface.objects.create(device=self.device, name="LAG99:99", type="virtual", parent=lag)

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="198.18.249.160/31", assigned_object_type=self._ct(), assigned_object_id=sub.pk
                )

            mock_put.assert_called_once()
            _, call_addresses = mock_put.call_args[0]
            entry = next(a for a in call_addresses if a["interface"] == "LAG99:99")
            self.assertTrue(entry["routed"])
            self.assertEqual(entry["parent_binding"], "lag-99")
            self.assertEqual(entry["encap_tag"], "99")

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

            with self.captureOnCommitCallbacks(execute=True), suppress_intent_push():
                IPAddress.objects.create(
                    address="10.2.0.1/24", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )

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
        def test_post_delete_pushes_snapshot_without_deleted_ip(self, mock_put):
            """Deleting an IPAddress fires push with that address excluded."""
            from ipam.models import IPAddress

            with self.captureOnCommitCallbacks(execute=True):
                ip = IPAddress.objects.create(
                    address="10.1.2.1/30", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )
            mock_put.reset_mock()

            with self.captureOnCommitCallbacks(execute=True):
                ip.delete()

            mock_put.assert_called_once()
            call_device_id, call_addresses = mock_put.call_args[0]
            self.assertEqual(call_device_id, 42)
            self.assertFalse(
                any(a["address"] == "10.1.2.1/30" for a in call_addresses),
                "Deleted IP must not appear in the push snapshot",
            )

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

    class TestGActivatedInterfaceIntentOrigin(IntentPushResetMixin, DjangoTestCase):
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
            NSODeviceManagement.objects.bulk_create(
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

            from netbox_nso_plugin.signals import _push_intent_on_interface_edit

            req = RequestFactory().post("/", headers=header) if header is not None else None
            # Simulate the pre_save snapshot: operator changed the description,
            # left enabled untouched.
            self.iface._nso_old_values = {"description": "PREVIOUS-DESC", "enabled": self.iface.enabled}
            token = current_request.set(req)
            try:
                with patch("netbox_nso_plugin.adapter_client.put_intent") as mock_put:
                    with self.captureOnCommitCallbacks(execute=True):
                        _push_intent_on_interface_edit(None, self.iface, created=False)
                    return mock_put
            finally:
                current_request.reset(token)

        def _state(self):
            from netbox_nso_plugin.models import NSOInterfaceState

            return NSOInterfaceState.objects.get(interface=self.iface, attribute="description")

        def test_operator_edit_promotes_and_pushes(self):
            """A normal (non-adapter) edit promotes imported→accepted and pushes intent.

            (put_intent may fire more than once — the state's own post_save also
            pushes — so assert it was called, not the exact count.)
            """
            mock_put = self._fire(header=None)
            self.assertEqual(self._state().status, "accepted")
            mock_put.assert_called()

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
            from netbox_nso_plugin.signals import _push_intent_on_interface_edit

            enabled_state = NSOInterfaceState.objects.create(
                interface=self.iface, attribute="enabled", status="imported", nso_value="True"
            )
            # description changed; enabled untouched.
            self.iface._nso_old_values = {"description": "PREVIOUS-DESC", "enabled": self.iface.enabled}
            token = current_request.set(None)
            try:
                with patch("netbox_nso_plugin.adapter_client.put_intent"):
                    with self.captureOnCommitCallbacks(execute=True):
                        _push_intent_on_interface_edit(None, self.iface, created=False)
            finally:
                current_request.reset(token)

            enabled_state.refresh_from_db()
            self.assertEqual(enabled_state.status, "imported")
            self.assertIsNone(enabled_state.accepted_at)
            # the changed attribute (description) IS promoted
            self.assertEqual(self._state().status, "accepted")

    class TestGreenfieldOspfSignals(IntentPushResetMixin, DjangoTestCase):
        """Operator-created netbox_routing OSPF → accepted overlays + OSPF intent push."""

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
            NSODeviceManagement.objects.bulk_create(
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
        def test_create_ospf_iface_owns_overlays_and_pushes(self, mock_put):
            from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

            from netbox_nso_plugin.models import NSOOSPFInstanceState, NSOOSPFInterfaceState

            with self.captureOnCommitCallbacks(execute=True):
                inst = OSPFInstance.objects.create(
                    name="ospf-1", router_id="198.18.250.117", process_id="1", device=self.device
                )
                area = OSPFArea.objects.create(area_id="0", area_type="standard")
                OSPFInterface.objects.create(instance=inst, area=area, interface=self.iface, cost=100)

            inst_state = NSOOSPFInstanceState.objects.get(management__device=self.device, process_id="1")
            self.assertEqual(inst_state.status, "accepted")
            iface_state = NSOOSPFInterfaceState.objects.get(management__device=self.device, interface=self.iface)
            self.assertEqual(iface_state.status, "accepted")
            self.assertEqual(iface_state.area_id, "0")
            self.assertEqual(iface_state.process_id, "1")
            self.assertEqual(iface_state.cost, 100)

            self.assertTrue(mock_put.called)
            _, payload = mock_put.call_args[0]
            iface_entry = next(i for i in payload["interfaces"] if i["interface_name"] == "LAG99:99")
            self.assertEqual(iface_entry["area_id"], "0")
            self.assertEqual(iface_entry["process_id"], "1")
            self.assertEqual(iface_entry["cost"], 100)
            self.assertTrue(any(i["process_id"] == "1" for i in payload["instances"]))

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
        with (
            patch("netbox_nso_plugin.adapter_client.put_svi_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
        mock_put.assert_called_once()
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
        with (
            patch("netbox_nso_plugin.adapter_client.put_subinterface_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
        mock_put.assert_called_once()
        self.assertEqual(mock_put.call_args[0][1], [])

    def test_logging_host_delete_pushes_reduced_snapshot(self):
        from netbox_nso_plugin.models import NSOLoggingHostState

        mgmt = self._mgmt()
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            row = NSOLoggingHostState.objects.create(management=mgmt, address="198.51.100.7", status="accepted")
        with (
            patch("netbox_nso_plugin.adapter_client.put_logging_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
        mock_put.assert_called_once()
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
        with (
            patch("netbox_nso_plugin.adapter_client.put_interface_mtu_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
        mock_put.assert_called_once()
        self.assertEqual(mock_put.call_args[0][1], [])

    # ── #105 sweep: the 13 families that had post_save ONLY (f282e9e class) ──
    # Each red-first test: create an OWNED row (push #1 fires and warms the
    # change-detection cache), then DELETE it — without a post_delete receiver no
    # push fires and the adapter keeps applying the deleted intent forever.

    def _delete_pushes(self, row, patch_target, expect_empty_list=True):
        """Delete *row* and assert the reduced snapshot push fired at the client boundary."""
        with (
            patch(f"netbox_nso_plugin.adapter_client.{patch_target}") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
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
        """Deleting the overlay row directly pushes the reduced (owned-only) snapshot.

        This is the mechanism the native BGPPeer pre_delete reuses (it drops the overlay
        row, firing this post_delete); greenfield end-to-end coverage is in
        test_bgp_greenfield.TestBgpPeerGreenfieldDelete."""
        from netbox_nso_plugin.models import NSOBGPPeerState

        mgmt = self._mgmt()
        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent"), self.captureOnCommitCallbacks(execute=True):
            row = NSOBGPPeerState.objects.create(
                management=mgmt, asn_str="65000", peer_address_str="192.0.2.1", status="accepted"
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
        with (
            patch("netbox_nso_plugin.adapter_client.apply_lag_config") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            member.delete()
        mock_put.assert_called_once()

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
    """#106: only pushes born from a DELETION may let the adapter retract from the
    device. The adapter treats every UNMARKED intent shrink as an un-own and DETACHES
    (no-networking + sync-from, device untouched) — so deletion-driven pushes must
    carry ``?delete_origin=true``, and un-own pushes must not.
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

    def _recorded_calls(self, act):
        """Run *act* with the adapter HTTP transport recorded → [(method, url, params), ...]."""
        from ._adapter_http import make_response, make_session

        session = make_session(response=make_response(200, json_data={}))
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=self._CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            self.captureOnCommitCallbacks(execute=True),
        ):
            act()
        return [(c.args[0], c.args[1], c.kwargs.get("params") or {}) for c in session.request.call_args_list]

    def _recorded_params(self, act):
        """Run *act* with the adapter HTTP transport recorded → list of params dicts."""
        return [params for _method, _url, params in self._recorded_calls(act)]

    def _owned_svi(self, mgmt):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOSVIState

        vlan = VLAN.objects.create(vid=444, name="do-v444")
        with (
            patch("netbox_nso_plugin.adapter_client.put_svi_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            return NSOSVIState.objects.create(
                management=mgmt, interface=self.iface, vlan=vlan, svi_type="irb", status="accepted"
            )

    def test_overlay_delete_push_is_marked_delete_origin(self):
        mgmt = self._mgmt()
        row = self._owned_svi(mgmt)
        params = self._recorded_params(row.delete)
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
            row.save()

        params = self._recorded_params(_unown)
        self.assertTrue(params, "the un-own shrink must push")
        self.assertFalse(
            any("delete_origin" in p for p in params),
            f"an un-own push must stay unmarked (detach-safe); saw params {params}",
        )

    def test_native_static_route_delete_is_marked(self):
        """The native pre_delete safety-net path (routing.StaticRoute) is a deletion —
        its reduced push must carry the mark too."""
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._mgmt()
        route = StaticRoute.objects.create(prefix="198.18.77.0/24", next_hop="198.18.0.1", name="do-sr", metric=1)
        with (
            patch("netbox_nso_plugin.adapter_client.put_static_route_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            NSOStaticRouteState.objects.create(
                management=mgmt, static_route=route, nso_prefix="198.18.77.0/24", status="accepted"
            )
        params = self._recorded_params(route.delete)
        self.assertTrue(params, "the native delete must push")
        self.assertTrue(
            any(p.get("delete_origin") == "true" for p in params),
            f"native-delete push must be marked delete_origin; saw params {params}",
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

        params = self._recorded_params(lambda: route.devices.add(mgmt.device))
        self.assertTrue(params, "assigning the device must push the (grown) snapshot")
        self.assertFalse(
            any("delete_origin" in p for p in params),
            f"an ADD is not a deletion — its push must stay unmarked; saw params {params}",
        )

    def test_unassigning_a_device_from_a_static_route_is_marked(self):
        """post_remove IS a deletion — the reduced snapshot must still carry the mark."""
        from netbox_routing.models import StaticRoute

        mgmt = self._mgmt()
        route = StaticRoute.objects.create(prefix="198.18.88.0/24", next_hop="198.18.0.1", name="do-rm", metric=1)
        with (
            patch("netbox_nso_plugin.adapter_client.put_static_route_intent"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            route.devices.add(mgmt.device)

        params = self._recorded_params(lambda: route.devices.remove(mgmt.device))
        self.assertTrue(params, "un-assigning the device must push the reduced snapshot")
        self.assertTrue(
            any(p.get("delete_origin") == "true" for p in params),
            f"un-assigning is a deletion — its push must be marked; saw params {params}",
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
        mgmt = self._mgmt()
        self._owned_svi(mgmt)
        self._assert_teardown_touched_only_the_offboard(self._recorded_calls(mgmt.delete))

    def test_deleting_a_device_pushes_no_intent(self):
        mgmt = self._mgmt()
        self._owned_svi(mgmt)
        self._assert_teardown_touched_only_the_offboard(self._recorded_calls(self.device.delete))
