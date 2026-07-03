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

    def test_failed_interface_mtu_scope_marks_apply_failed(self):
        """interface_mtu joins the deploying→settle flow. _prepare_apply moves accepted MTU
        rows to deploying, so a failed interface_mtu apply MUST settle the stuck row to
        apply_failed. Regression: the scope was missing from _APPLY_DEPLOYING_SCOPES, so MTU
        rows stranded in 'deploying' forever (or were falsely reported in_sync)."""
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, _row = self._setup()
        iface = Interface.objects.create(device=mgmt.device, name="Gi0/5", type="1000base-t")
        mtu_row = NSOInterfaceMtuState.objects.create(management=mgmt, interface=iface, l2_mtu=9000, status="deploying")
        _settle_apply_failures(mgmt, {"interface_mtu_count_by_outcome": {"in_sync": 0, "apply_failed": 1}})
        mtu_row.refresh_from_db()
        self.assertEqual(mtu_row.status, "apply_failed")
        self.assertTrue(mtu_row.last_apply_error)


class TestRoutePolicyApplySettle(APITestCase):
    """Route-policy joins the deploying→settle flow: Apply marks accepted→deploying,
    a failed route_policy scope flips the stuck deploying row → apply_failed."""

    def _setup(self, status_="deploying"):
        from netbox_nso_plugin.models import NSORoutePolicyState

        device = _make_device("rp-settle")
        inst, _ = NSOInstance.objects.get_or_create(
            name="rp-settle-inst", defaults={"adapter_instance_id": "rp-settle-inst"}
        )
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="rp-settle", adapter_device_id=88
        )
        row = NSORoutePolicyState.objects.create(
            management=mgmt, family="community_list", object_name="CL-X", status=status_
        )
        return mgmt, row

    def test_failed_route_policy_scope_marks_apply_failed(self):
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        _settle_apply_failures(mgmt, {"route_policy_count_by_outcome": {"in_sync": 0, "apply_failed": 1}})
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertTrue(row.last_apply_error)

    def test_failed_route_policy_records_real_error_detail(self):
        """When the apply job carries the device-commit error, last_apply_error shows
        the real reason (not the generic 'see the adapter job' placeholder)."""
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        job = {
            "result": {"route_policy_count_by_outcome": {"in_sync": 0, "apply_failed": 1}},
            "error": {
                "detail": {
                    "items": [
                        {"type": "route_policy", "error": "device parser rejected: invalid community"},
                        {"type": "vlan", "error": "unrelated"},
                    ]
                }
            },
        }
        _settle_apply_failures(mgmt, job["result"], job)
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertIn("device parser rejected: invalid community", row.last_apply_error)
        self.assertNotIn("unrelated", row.last_apply_error)  # other scopes excluded

    def test_prepare_apply_marks_accepted_route_policy_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        with (
            patch("netbox_nso_plugin.signals._push_interface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_lacp_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_switchport_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_vlan_intent_for_device"),
        ):
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_prepare_apply_force_pushes_owned_interface_intent(self):
        """Apply force-re-pushes the owned interface snapshot (status-based), so an owned
        attribute whose adapter intent went stale is actually re-applied instead of silently
        skipped. Ownership is kept durable by the reconciler's owned-guard."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with (
            patch("netbox_nso_plugin.signals._push_interface_intent_for_device") as push_if,
            patch("netbox_nso_plugin.signals._push_lacp_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_switchport_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_vlan_intent_for_device"),
        ):
            _prepare_apply(mgmt)
        push_if.assert_called_once_with(mgmt.device_id, mgmt.adapter_device_id, force=True)


class TestApplyRollbackOnAdapterError(APITestCase):
    """A failed Apply (adapter unreachable / 500 — no job enqueued) must roll the rows
    _prepare_apply moved accepted→deploying back to accepted. Otherwise they are stuck
    'applying' forever: no apply job exists for _settle_apply_failures to ever settle."""

    def _setup(self):
        from netbox_nso_plugin.models import NSORoutePolicyState

        device = _make_device("apply-rb")
        inst, _ = NSOInstance.objects.get_or_create(
            name="apply-rb-inst", defaults={"adapter_instance_id": "apply-rb-inst"}
        )
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="apply-rb", adapter_device_id=99
        )
        row = NSORoutePolicyState.objects.create(
            management=mgmt, family="community_list", object_name="CL-RB", status="accepted"
        )
        return mgmt, row

    def test_non_conflict_adapter_error_rolls_back_deploying(self):
        from django.contrib.auth import get_user_model

        from netbox_nso_plugin.adapter_client import AdapterError

        mgmt, row = self._setup()
        admin = get_user_model().objects.create_superuser(username="apply-rb-admin", password="pw", email="a@x.y")  # noqa: S106
        self.client.force_login(admin)
        with (
            patch("netbox_nso_plugin.signals._push_interface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_lacp_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_switchport_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_vlan_intent_for_device"),
            patch(
                "netbox_nso_plugin.adapter_client.trigger_apply",
                side_effect=AdapterError("adapter unreachable", code="unreachable"),
            ),
        ):
            resp = self.client.post(f"/plugins/nso/device-management/{mgmt.pk}/actions/apply/")
        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")  # rolled back — NOT stuck in deploying


class TestApplyPreviewInterfaceScope(APITestCase):
    """The Apply preview lists OWNED-status interface attributes whose NetBox value differs
    from the device — exactly what the matrix shows as 'pending' and the force-push applies.

    Ownership is status-based (status in OWNED_STATES), NOT accepted_at — a stale accepted_at
    on an unowned status never makes the preview claim a push Apply would not perform.
    """

    def test_preview_includes_owned_interface_pending(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.views import _apply_preview_interface_changes

        device = _make_device("ifprev")
        iface = Interface.objects.create(device=device, name="ae2.0", type="virtual", description="UPLINK")
        # Owned status (accepted) and the device description is empty → value differs →
        # genuinely pending, must show + apply. accepted_at=None proves it's status-driven.
        NSOInterfaceState.objects.create(interface=iface, attribute="description", status="accepted", nso_value="")
        changes = _apply_preview_interface_changes(device.pk)
        self.assertEqual([(c["interface"], c["attribute"]) for c in changes], [("ae2.0", "description")])
        self.assertEqual(changes[0]["netbox"], "UPLINK")

    def test_preview_excludes_unowned_interface_despite_stale_accepted_at(self):
        from dcim.models import Interface
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.views import _apply_preview_interface_changes

        device = _make_device("ifprev2")
        iface = Interface.objects.create(device=device, name="ae2.0", type="virtual", description="UPLINK")
        # The device-27 ae2.0 case: an 'imported' (un-owned) attribute carrying a STALE
        # accepted_at, value differs — Apply won't push it, so it must not be previewed.
        NSOInterfaceState.objects.create(
            interface=iface,
            attribute="description",
            status="imported",
            nso_value="",
            accepted_at=timezone.now(),
        )
        self.assertEqual(_apply_preview_interface_changes(device.pk), [])

    def test_preview_excludes_owned_interface_in_sync(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.views import _apply_preview_interface_changes

        device = _make_device("ifprev3")
        iface = Interface.objects.create(device=device, name="ae3.0", type="virtual", description="MATCH")
        # Owned (in_sync) and the device already matches NetBox → not pending → not previewed.
        NSOInterfaceState.objects.create(interface=iface, attribute="description", status="in_sync", nso_value="MATCH")
        self.assertEqual(_apply_preview_interface_changes(device.pk), [])


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
