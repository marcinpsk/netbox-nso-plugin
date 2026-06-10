# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the off-request reconcile job and the sync-complete callback endpoint."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from rest_framework import status
from utilities.testing import APITestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance


def _make_device(name="rec-dev"):
    mfg = Manufacturer.objects.create(name=f"{name}-mfg", slug=f"{name}-mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{name}-dt", slug=f"{name}-dt")
    role = DeviceRole.objects.create(name=f"{name}-role", slug=f"{name}-role")
    site = Site.objects.create(name=f"{name}-site", slug=f"{name}-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


class TestRunDeviceReconcile(APITestCase):
    """run_device_reconcile is the rqworker entrypoint (off-request)."""

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


class TestSyncCompleteEndpoint(APITestCase):
    """POST /api/plugins/nso/sync-complete/ — the adapter's sync-done callback."""

    def _url(self):
        return "/api/plugins/nso/sync-complete/"

    def test_missing_ids_returns_400(self):
        response = self.client.post(self._url(), {}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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
        device = _make_device("rec-map")
        inst = NSOInstance.objects.create(name="rec-nso", adapter_instance_id="rec-nso")
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


class TestSettleApplyFailures(APITestCase):
    """Step 4: a stuck 'deploying' row in a scope whose apply failed → apply_failed."""

    def _setup(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        device = _make_device("settle")
        inst, _ = NSOInstance.objects.get_or_create(name="settle-inst", defaults={"adapter_instance_id": "settle-inst"})
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="settle", adapter_device_id=77
        )
        vlan = VLAN.objects.create(group=_device_vlan_group(device), vid=100, name="V100")
        row = NSOVLANState.objects.create(management=mgmt, vlan=vlan, device_name="V100", status="deploying")
        return mgmt, row

    def test_failed_scope_marks_deploying_apply_failed(self):
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        _settle_apply_failures(mgmt, {"vlan_count_by_outcome": {"in_sync": 0, "apply_failed": 1}})
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertTrue(row.last_apply_error)

    def test_no_failure_leaves_deploying(self):
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        _settle_apply_failures(mgmt, {"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}})
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")  # apply succeeded → reconcile settles it, not us

    def test_no_result_is_noop(self):
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        _settle_apply_failures(mgmt, None)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")


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
        owned = NSOVLANState.objects.create(management=mgmt, vlan=vlan2, device_name="V20", status="accepted")
        return mgmt, imported, owned

    def test_failure_marks_unowned_error_preserves_owned(self):
        from netbox_nso_plugin.reconcile import _safe_reconcile

        mgmt, imported, owned = self._setup()
        ctx = {"vlan_states": []}

        def boom(*_a):
            raise RuntimeError("malformed payload")

        # No exception escapes; ctx keeps its default; scope rows are reconciled.
        _safe_reconcile(ctx, "vlan_states", mgmt, ("NSOVLANState",), boom, object())
        self.assertEqual(ctx["vlan_states"], [])
        imported.refresh_from_db()
        owned.refresh_from_db()
        self.assertEqual(imported.status, "error")  # unowned → error
        self.assertEqual(owned.status, "accepted")  # owned ownership preserved

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
