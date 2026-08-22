# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for intent_drift: detect + re-sync orphaned/partial adapter intent (split-brain)."""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from netbox_nso_plugin import drain, intent_drift
from netbox_nso_plugin.models import (
    NSOBGPPeerState,
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceIPState,
    NSOInterfaceState,
    NSOLoggingHostState,
)

from ._adapter_http import make_session
from ._outbox_case import without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

_ADAPTER_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


class TestIntentDrift(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="DriftMfg", slug="driftmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="DriftDev", slug="driftdev")
        role = DeviceRole.objects.create(name="DriftRole", slug="driftrole")
        site = Site.objects.create(name="DriftSite", slug="driftsite")
        cls.device = Device.objects.create(name="drift-rtr", device_type=dt, role=role, site=site)
        cls.iface = Interface.objects.create(device=cls.device, name="Gi0/0", type="1000base-t")
        inst = NSOInstance.objects.create(name="DriftNSO", adapter_instance_id="nso-drift")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="drift-rtr", adapter_device_id=88
        )

    _SUMMARY = {"scopes": {"interface_ip_intent": {"count": 2, "applied": 0, "failed": 0}}}

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_orphaned_when_adapter_has_intent_and_netbox_owns_none(self, mock_sum):
        mock_sum.return_value = self._SUMMARY
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        keys = {d["key"] for d in drift}
        self.assertIn("interface_ip", keys)
        entry = next(d for d in drift if d["key"] == "interface_ip")
        self.assertEqual(entry["count"], 2)
        self.assertFalse(entry["partial"])
        self.assertEqual(entry["owned"], 0)

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_not_flagged_when_owned_matches_adapter_count(self, mock_sum):
        mock_sum.return_value = self._SUMMARY
        NSOInterfaceIPState.objects.create(
            interface=self.iface, address="10.0.0.1/32", vrf="", family="ipv4", status="accepted"
        )
        NSOInterfaceIPState.objects.create(
            interface=self.iface, address="10.0.0.2/32", vrf="", family="ipv4", status="in_sync"
        )
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        self.assertNotIn("interface_ip", {d["key"] for d in drift})

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_not_flagged_when_owned_exceeds_adapter_count(self, mock_sum):
        # Push-time skips can leave the adapter with FEWER rows than NetBox owns — healthy.
        mock_sum.return_value = self._SUMMARY
        for i in (1, 2, 3):
            NSOInterfaceIPState.objects.create(
                interface=self.iface, address=f"10.0.1.{i}/32", vrf="", family="ipv4", status="accepted"
            )
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        self.assertNotIn("interface_ip", {d["key"] for d in drift})

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_partial_when_adapter_holds_more_than_owned(self, mock_sum):
        mock_sum.return_value = self._SUMMARY
        NSOInterfaceIPState.objects.create(
            interface=self.iface, address="10.0.0.1/32", vrf="", family="ipv4", status="accepted"
        )
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        entry = next(d for d in drift if d["key"] == "interface_ip")
        self.assertTrue(entry["partial"])
        self.assertEqual(entry["count"], 2)
        self.assertEqual(entry["owned"], 1)

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_bgp_non_parity_scope_never_partial(self, mock_sum):
        # 1 bgp_router_intent row legitimately covers N owned peers — counts aren't 1:1,
        # so any owned > 0 must suppress the scope regardless of count comparison.
        mock_sum.return_value = {"scopes": {"bgp_router_intent": {"count": 3, "applied": 0, "failed": 0}}}
        NSOBGPPeerState.objects.create(
            management=self.mgmt, asn_str="65000", peer_address_str="192.0.2.1", status="accepted"
        )
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        self.assertNotIn("bgp", {d["key"] for d in drift})
        # ...while the orphan rule still fires at owned == 0.
        NSOBGPPeerState.objects.all().delete()
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        entry = next(d for d in drift if d["key"] == "bgp")
        self.assertFalse(entry["partial"])

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_interface_scope_owned_by_status_not_accepted_at(self, mock_sum):
        # Ownership is status-based (mirrors the now status-based push predicate). An owned
        # STATUS counts as owned even with accepted_at=None; a "changed" row does NOT count
        # as owned even with a stale accepted_at set — so the adapter's 1 intent row reads
        # as orphaned and the scope is flagged.
        mock_sum.return_value = {"scopes": {"interface_intent": {"count": 1, "applied": 0, "failed": 0}}}
        state = NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="accepted", accepted_at=None
        )
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        self.assertNotIn("interface", {d["key"] for d in drift})
        # Flip to an unowned status (with a STALE accepted_at) → owned count drops to 0 →
        # the adapter's 1 row is now orphaned → flagged.
        state.status = "changed"
        state.accepted_at = timezone.now()
        state.save()
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        self.assertIn("interface", {d["key"] for d in drift})

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_interface_mtu_scope_registered(self, mock_sum):
        # interface_mtu_intent was missing from the registry — orphaned MTU intent was
        # invisible. Pin the scope so it can't fall out again.
        mock_sum.return_value = {"scopes": {"interface_mtu_intent": {"count": 2, "applied": 0, "failed": 0}}}
        drift = intent_drift.compute_intent_drift(self.device, self.mgmt)
        entry = next(d for d in drift if d["key"] == "interface_mtu")
        self.assertEqual(entry["count"], 2)
        self.assertFalse(entry["partial"])

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_empty_summary_no_drift(self, mock_sum):
        mock_sum.return_value = {"scopes": {}}
        self.assertEqual(intent_drift.compute_intent_drift(self.device, self.mgmt), [])

    @patch("netbox_nso_plugin.adapter_client.get_intent_summary", side_effect=RuntimeError("adapter down"))
    def test_adapter_error_returns_empty(self, _mock):
        # Must never break the tab render.
        self.assertEqual(intent_drift.compute_intent_drift(self.device, self.mgmt), [])

    @patch("netbox_nso_plugin.drain.drain_key", return_value=drain.SUCCEEDED)
    def test_resync_calls_push_for_scope(self, mock_drain):
        from netbox_nso_plugin.delivery import MODE_STORE_ONLY

        done, failed = intent_drift.resync_intent(self.device, self.mgmt, ["interface_ip"])
        self.assertEqual((done, failed), (["interface_ip"], []))
        # The drift registry calls the scope interface_ip and the delivery registry calls it
        # ip, so the key is resolved through the one registry that enumerates the pushes.
        # force=True: the split-brain re-sync must not be dropped against the acknowledged
        # baseline — that baseline is precisely what is stale here (see TestResyncStoreOnly).
        mock_drain.assert_called_once_with(self.mgmt.device_id, "ip", mode=MODE_STORE_ONLY, force=True)

    def test_resync_no_adapter_id_noop(self):
        self.mgmt.adapter_device_id = None
        self.assertEqual(intent_drift.resync_intent(self.device, self.mgmt, ["interface_ip"]), ([], []))

    @patch("netbox_nso_plugin.drain.drain_key", return_value=drain.SUCCEEDED)
    @patch("netbox_nso_plugin.adapter_client.get_intent_summary")
    def test_resync_default_keys_include_partial_scopes(self, mock_sum, mock_drain):
        from netbox_nso_plugin.delivery import MODE_STORE_ONLY

        mock_sum.return_value = self._SUMMARY
        NSOInterfaceIPState.objects.create(
            interface=self.iface, address="10.0.0.1/32", vrf="", family="ipv4", status="accepted"
        )
        done, failed = intent_drift.resync_intent(self.device, self.mgmt)
        self.assertIn("interface_ip", done)
        self.assertEqual(failed, [])
        mock_drain.assert_called_once_with(self.mgmt.device_id, "ip", mode=MODE_STORE_ONLY, force=True)


class TestResyncStoreOnly(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Tracker #103: a re-sync push must be STORE-ONLY on the adapter side.

    "Re-sync adapter intent" promises it never touches the device, but its reduced
    snapshot PUT made the adapter auto-enqueue a shrink-removal job that PUT-replace
    retracted FASTMAP-owned config from the real device (ra1.lab, removal job 31686).
    The re-sync pushes must therefore carry ``?store_only=true``, which the adapter
    honours by skipping the removal/auto-apply enqueues. These drive the REAL path —
    resync_intent → forced claim → adapter_client PUT — down to the recorded session,
    which is why they run outside a test transaction: the claim refuses to nest in one.
    """

    def setUp(self):
        super().setUp()
        with (
            patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"),
            without_commit_drain(),
            transaction.atomic(),
        ):
            mfg = Manufacturer.objects.create(name="SoMfg", slug="somfg")
            dt = DeviceType.objects.create(manufacturer=mfg, model="SoDev", slug="sodev")
            role = DeviceRole.objects.create(name="SoRole", slug="sorole")
            site = Site.objects.create(name="SoSite", slug="sosite")
            self.device = Device.objects.create(name="so-rtr", device_type=dt, role=role, site=site)
            inst = NSOInstance.objects.create(name="SoNSO", adapter_instance_id="nso-so")
            self.mgmt = NSODeviceManagement.objects.create(
                device=self.device, nso_instance=inst, nso_device_name="so-rtr", adapter_device_id=91
            )
            NSOLoggingHostState.objects.create(management=self.mgmt, address="10.0.0.5", status="accepted")

    def _recorded_requests(self, run):
        session = make_session(json_data={})
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            run()
        return session.request.call_args_list

    def test_resync_push_carries_store_only_flag(self):
        calls = self._recorded_requests(lambda: intent_drift.resync_intent(self.device, self.mgmt, ["logging"]))
        self.assertEqual(len(calls), 1)
        method, url = calls[0].args[0], calls[0].args[1]
        self.assertEqual(method, "PUT")
        self.assertIn("/logging-intent", url)
        params = calls[0].kwargs.get("params") or {}
        self.assertEqual(params.get("store_only"), "true")

    def test_an_empty_success_response_is_reported_as_done(self):
        session = make_session(content=b"")
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=session),
        ):
            result = intent_drift.resync_intent(self.device, self.mgmt, ["logging"])

        self.assertEqual(result, (["logging"], []))

    def test_normal_signal_push_has_no_store_only_flag(self):
        from netbox_nso_plugin.delivery import deliver

        calls = self._recorded_requests(lambda: deliver("logging", self.device.pk, self.mgmt.adapter_device_id))
        self.assertEqual(len(calls), 1)
        params = calls[0].kwargs.get("params") or {}
        self.assertNotIn("store_only", params)

    def test_resync_pushes_even_when_the_acknowledged_baseline_names_that_body(self):
        """The split-brain re-sync exists for: the ADAPTER lost the intent while the plugin's
        ``last_success_identity`` still names the body it last acknowledged. That is exactly
        what the claim drops as unchanged — so without force=True the re-sync would send
        NOTHING while the view reported success, and the split-brain was never repaired.
        """
        from netbox_nso_plugin import outbox

        # The control: an ordinary claim IS dropped once the baseline names its body.
        with without_commit_drain(), transaction.atomic():
            outbox.enqueue(self.device.pk, "logging")
        primed = self._recorded_requests(lambda: drain.drain_key(self.device.pk, "logging"))
        self.assertEqual(len(primed), 1)
        with without_commit_drain(), transaction.atomic():
            outbox.enqueue(self.device.pk, "logging")
        again = self._recorded_requests(lambda: drain.drain_key(self.device.pk, "logging"))
        self.assertEqual(len(again), 0, "an unchanged ordinary claim is still dropped")

        # The re-sync is forced, so its own repeat goes out against the baseline it just set.
        first = self._recorded_requests(lambda: intent_drift.resync_intent(self.device, self.mgmt, ["logging"]))
        repeated = self._recorded_requests(lambda: intent_drift.resync_intent(self.device, self.mgmt, ["logging"]))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(repeated), 1, "the re-sync was dropped against the baseline it had just set")
        self.assertEqual((repeated[0].kwargs.get("params") or {}).get("store_only"), "true")

    def test_a_refused_push_is_reported_rather_than_reported_cleared(self):
        """codex O1 r4 F5 (board #1557): a key nothing was sent for is not a key that cleared.

        The deletion of an owned row is authority a store-only request can never carry
        (§4.3(d)), so the claim refuses and the wire stays empty. Appending the key anyway
        told the operator the orphaned intent was gone while the drift was untouched.
        """
        with without_commit_drain(), transaction.atomic():
            NSOLoggingHostState.objects.filter(management=self.mgmt).delete()

        returned: list = []
        calls = self._recorded_requests(
            lambda: returned.append(intent_drift.resync_intent(self.device, self.mgmt, ["logging"]))
        )

        self.assertEqual(len(calls), 0, "a store-only request writes no removal, so it may not carry one")
        self.assertEqual(returned, [([], ["logging"])], "the refused key is reported, never counted as cleared")
