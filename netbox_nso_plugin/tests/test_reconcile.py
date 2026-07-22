# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the off-request reconcile job and the sync-complete callback endpoint."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase
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


class TestProvisionCompleteEndpoint(APITestCase):
    """POST /api/plugins/nso/provision-complete/ — the adapter's provision-done callback."""

    def _url(self):
        return "/api/plugins/nso/provision-complete/"

    def _provisioning_row(self, job_id="55"):
        device = _make_device(f"prov-{job_id}")
        inst = NSOInstance.objects.create(name=f"prov-nso-{job_id}", adapter_instance_id=f"prov-nso-{job_id}")
        return NSODeviceManagement.objects.create(
            device=device,
            nso_instance=inst,
            nso_device_name=f"prov-{job_id}",
            onboard_status="provisioning",
            onboard_job_id=job_id,
        )

    def test_missing_job_id_returns_400(self):
        response = self.client.post(self._url(), {}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_known_job_enqueues_advance_and_returns_202(self):
        """A provision_job_id matching a row's onboard_job_id enqueues that row's advance."""
        mgmt = self._provisioning_row("77")
        with patch("netbox_nso_plugin.reconcile.enqueue_onboard_advance") as m:
            response = self.client.post(self._url(), {"provision_job_id": 77}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        m.assert_called_once_with(mgmt.id)  # matched by str(77) == onboard_job_id
        self.assertTrue(response.data["queued"])

    def test_unknown_job_acked_without_enqueue(self):
        """An untracked provision job is acked (202) without enqueue — the callback is best-effort."""
        with patch("netbox_nso_plugin.reconcile.enqueue_onboard_advance") as m:
            response = self.client.post(self._url(), {"provision_job_id": 999999}, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertFalse(response.data["queued"])
        m.assert_not_called()


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


class TestEscalateStuckDeploying(APITestCase):
    """#26: a row still 'deploying' long after a SUCCEEDED apply is a silent drop.

    The adapter's post-apply verify re-issues the committed payload as a native dry-run
    against NSO's CDB — a writer/NED that silently dropped the value leaves the CDB
    service tree matching the payload, so that verify passes and the job reports in_sync
    (proven live on rg03: static route absent from the device, apply job succeeded).
    The device-truth signal is the reconcile value-match settle; when it never fires,
    the row must escalate to apply_failed instead of spinning in 'deploying' forever.
    """

    def _setup(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        device = _make_device("stuck")
        inst, _ = NSOInstance.objects.get_or_create(name="stuck-inst", defaults={"adapter_instance_id": "stuck-inst"})
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="stuck", adapter_device_id=78
        )
        vlan = VLAN.objects.create(group=_device_vlan_group(device), vid=101, name="V101")
        row = NSOVLANState.objects.create(management=mgmt, vlan=vlan, device_name="V101", status="deploying")
        return mgmt, row

    @staticmethod
    def _job(status="succeeded", minutes_ago=30, job_id=900):
        # The adapter serializes timestamps as naive-UTC isoformat + "Z" (api/jobs.py).
        from datetime import timedelta

        from django.utils import timezone

        ts = (timezone.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        return {
            "id": job_id,
            "type": "apply",
            "status": status,
            "updated_at": ts,
            "result": {"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}},
        }

    def test_stale_succeeded_apply_escalates_to_apply_failed(self):
        from netbox_nso_plugin.reconcile import _escalate_stuck_deploying

        mgmt, row = self._setup()
        _escalate_stuck_deploying(mgmt, self._job(minutes_ago=30))
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertIn("never", row.last_apply_error)

    def test_within_grace_stays_deploying(self):
        from netbox_nso_plugin.reconcile import _escalate_stuck_deploying

        mgmt, row = self._setup()
        _escalate_stuck_deploying(mgmt, self._job(minutes_ago=1))
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_failed_job_is_left_to_the_failure_settle(self):
        from netbox_nso_plugin.reconcile import _escalate_stuck_deploying

        mgmt, row = self._setup()
        _escalate_stuck_deploying(mgmt, self._job(status="failed", minutes_ago=30))
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_missing_job_is_noop(self):
        from netbox_nso_plugin.reconcile import _escalate_stuck_deploying

        mgmt, row = self._setup()
        _escalate_stuck_deploying(mgmt, None)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_a_scope_the_job_never_applied_is_not_escalated(self):
        """The escalation used to flip EVERY deploying row in all 8 scopes purely on the AGE
        of the last terminal apply — with nothing tying that job to those rows. A job that
        only ever applied VLANs would fabricate a failure on an in-flight route-policy row,
        with a message blaming the NED for silently dropping a value that job never carried.
        The job's own per-scope outcome counts are the linkage.
        """
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.reconcile import _escalate_stuck_deploying

        mgmt, vlan_row = self._setup()
        rp = NSORoutePolicyState.objects.create(
            management=mgmt,
            family="route_map",
            object_name="RM-STUCK",
            status="deploying",
        )

        # The job carried VLANs only — no route_policy_count_by_outcome at all.
        _escalate_stuck_deploying(mgmt, self._job(minutes_ago=30))

        vlan_row.refresh_from_db()
        rp.refresh_from_db()
        self.assertEqual(vlan_row.status, "apply_failed", "the scope the job DID apply still escalates")
        self.assertEqual(rp.status, "deploying", "a scope this job never applied must not be judged by it")
        self.assertFalse(rp.last_apply_error, "and no failure may be fabricated on it")

    def test_a_scope_the_job_applied_with_zero_items_is_not_escalated(self):
        """An empty count block means the job carried nothing for that scope either."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.reconcile import _escalate_stuck_deploying

        mgmt, _vlan_row = self._setup()
        rp = NSORoutePolicyState.objects.create(
            management=mgmt, family="route_map", object_name="RM-EMPTY", status="deploying"
        )
        job = self._job(minutes_ago=30)
        job["result"]["route_policy_count_by_outcome"] = {"in_sync": 0, "apply_failed": 0}

        _escalate_stuck_deploying(mgmt, job)

        rp.refresh_from_db()
        self.assertEqual(rp.status, "deploying")

    def test_run_device_reconcile_escalates_after_grace(self):
        from netbox_nso_plugin import reconcile

        mgmt, row = self._setup()
        jobs = [self._job(minutes_ago=30)]
        with (
            patch.object(reconcile, "reconcile_device", return_value={}),
            patch("netbox_nso_plugin.adapter_client.list_jobs", return_value=jobs),
        ):
            reconcile.run_device_reconcile(mgmt.device_id)
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")

    def test_run_device_reconcile_skips_escalation_while_apply_in_flight(self):
        from netbox_nso_plugin import reconcile

        mgmt, row = self._setup()
        # A new apply is running: its rows were just re-marked deploying — escalating on
        # the OLD terminal job's age would misfire.
        jobs = [
            {"id": 901, "type": "apply", "status": "running", "updated_at": None, "result": None},
            self._job(minutes_ago=30),
        ]
        with (
            patch.object(reconcile, "reconcile_device", return_value={}),
            patch("netbox_nso_plugin.adapter_client.list_jobs", return_value=jobs),
        ):
            reconcile.run_device_reconcile(mgmt.device_id)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")


class TestStaticRouteApplySettle(APITestCase):
    """Static routes join the deploying→settle flow (the MTU/route-policy regression class:
    a scope missing from _prepare_apply/_APPLY_DEPLOYING_SCOPES strands its rows). Found
    live on rg03: a successfully applied static route stayed 'pending apply' forever —
    _prepare_apply never marked it deploying and never force-pushed its snapshot."""

    def _setup(self, status_="deploying"):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        device = _make_device("sr-settle")
        inst, _ = NSOInstance.objects.get_or_create(
            name="sr-settle-inst", defaults={"adapter_instance_id": "sr-settle-inst"}
        )
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="sr-settle", adapter_device_id=89
        )
        # No device M2M on the route — the greenfield-accept signal must not fire here;
        # the overlay row is created directly in the state under test.
        route = StaticRoute.objects.create(prefix="198.18.99.0/24", next_hop="198.18.0.1", name="sr-settle", metric=1)
        row = NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=route,
            nso_prefix="198.18.99.0/24",
            nso_next_hop="198.18.0.1",
            status=status_,
        )
        return mgmt, row

    def test_failed_static_route_scope_marks_apply_failed(self):
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        _settle_apply_failures(mgmt, {"static_route_count_by_outcome": {"in_sync": 0, "apply_failed": 1}})
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertTrue(row.last_apply_error)

    def test_prepare_apply_marks_accepted_static_route_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        with (
            patch("netbox_nso_plugin.signals._push_interface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_lacp_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_switchport_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_vlan_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_svi_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_subinterface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_bfd_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_interface_mtu_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_route_policy_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_static_route_intent_for_device") as push_static,
        ):
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        # And the owned snapshot is force-re-pushed so a stale adapter intent still applies.
        push_static.assert_called_once()
        self.assertTrue(push_static.call_args.kwargs.get("force"))


class TestL2SapApplySettle(APITestCase):
    """L2 SAPs join the deploying→settle flow (the MTU/route-policy/static-route regression
    class: a scope missing from _prepare_apply/_APPLY_DEPLOYING_SCOPES strands its rows).
    Found by the item-12 real-apply scoping on ra1 (Nokia): an accepted SAP would apply
    adapter-side but never read 'deploying' nor flip to apply_failed on a failed scope."""

    def _setup(self, status_="deploying"):
        from netbox_nso_plugin.models import NSOL2SapState

        device = _make_device("sap-settle")
        inst, _ = NSOInstance.objects.get_or_create(
            name="sap-settle-inst", defaults={"adapter_instance_id": "sap-settle-inst"}
        )
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="sap-settle", adapter_device_id=90
        )
        row = NSOL2SapState.objects.create(
            management=mgmt,
            service_name="vpls-701",
            service_type="vpls",
            sap_id="1/1/c31/3:702",
            port="1/1/c31/3",
            outer_tag=702,
            status=status_,
        )
        return mgmt, row

    def test_failed_l2_sap_scope_marks_apply_failed(self):
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        _settle_apply_failures(mgmt, {"l2_sap_count_by_outcome": {"in_sync": 0, "apply_failed": 1}})
        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertTrue(row.last_apply_error)

    def test_prepare_apply_marks_accepted_l2_sap_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        with (
            patch("netbox_nso_plugin.signals._push_interface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_lacp_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_switchport_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_vlan_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_svi_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_subinterface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_bfd_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_interface_mtu_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_route_policy_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_static_route_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_l2_sap_intent_for_device") as push_sap,
        ):
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        # And the owned snapshot is force-re-pushed so a stale adapter intent still applies.
        push_sap.assert_called_once()
        self.assertTrue(push_sap.call_args.kwargs.get("force"))


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
            patch("netbox_nso_plugin.signals._push_svi_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_subinterface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_bfd_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_interface_mtu_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_route_policy_intent_for_device"),
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
            patch("netbox_nso_plugin.signals._push_svi_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_subinterface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_bfd_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_interface_mtu_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_route_policy_intent_for_device"),
        ):
            _prepare_apply(mgmt)
        push_if.assert_called_once_with(mgmt.device_id, mgmt.adapter_device_id, force=True)

    def test_prepare_apply_force_pushes_owned_route_policy_intent(self):
        """Apply must force-re-push owned ROUTE-POLICY intent too (like interface/VLAN). An owned
        route-policy object whose adapter intent went stale/empty otherwise applies 0 items and
        the row sticks in 'deploying' forever — observed on rg03, where an owned as-path had NO
        adapter intent row, so Apply pushed nothing and the row never settled."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with (
            patch("netbox_nso_plugin.signals._push_interface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_lacp_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_switchport_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_vlan_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_svi_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_subinterface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_bfd_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_interface_mtu_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_route_policy_intent_for_device") as push_rp,
        ):
            _prepare_apply(mgmt)
        push_rp.assert_called_once_with(mgmt.device_id, mgmt.adapter_device_id, force=True)

    def test_prepare_apply_force_pushes_deferred_scopes(self):
        """Apply marks SVI/subinterface/BFD/MTU accepted->deploying, so it must also force-re-push
        their owned snapshots. These are mirrored as adapter intent (reactive push on accept/edit),
        but that mirror can go stale/empty (a failed push, an out-of-band adapter reset). Without a
        force-push, Apply marks the row 'deploying' but the change-detection cache skips the push →
        0 items applied → the row sticks in 'deploying' forever (route-policy's rg03 failure mode)."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with (
            patch("netbox_nso_plugin.signals._push_interface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_lacp_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_switchport_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_vlan_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_route_policy_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_svi_intent_for_device") as push_svi,
            patch("netbox_nso_plugin.signals._push_subinterface_intent_for_device") as push_subif,
            patch("netbox_nso_plugin.signals._push_bfd_intent_for_device") as push_bfd,
            patch("netbox_nso_plugin.signals._push_interface_mtu_intent_for_device") as push_mtu,
        ):
            _prepare_apply(mgmt)
        for push in (push_svi, push_subif, push_bfd, push_mtu):
            push.assert_called_once_with(mgmt.device_id, mgmt.adapter_device_id, force=True)


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
            patch("netbox_nso_plugin.signals._push_svi_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_subinterface_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_bfd_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_interface_mtu_intent_for_device"),
            patch("netbox_nso_plugin.signals._push_route_policy_intent_for_device"),
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


class TestEnqueueDeviceReconcileOrphanRecovery(TestCase):
    """`enqueue_device_reconcile` dedups on a deterministic job id (`nso-reconcile-<id>`) so the
    15-min sync fan-out doesn't pile up. But an ORPHANED job — worker died mid-run (dev auto-reload,
    prod redeploy/crash), leaving a stale ``queued``/``started`` status while tracked by NEITHER the
    pending queue nor the started registry — must NOT block every future reconcile for that device.
    Observed live: an ``nso-reconcile-90`` stuck ``queued`` since June silently no-op'd every
    sync/apply notify, so rows only settled on an inline tab-render reconcile.

    Real-behavior: exercises the actual RQ Queue/Job API on the configured Redis, but on an isolated
    throwaway queue name (no worker consumes it) with a device id that can't collide with real jobs.
    """

    _DEV = 987654  # won't collide with any real nso-reconcile-<id> job hash
    _QUEUE = "nso-test-reconcile-orphan"
    _JOB_ID = f"nso-reconcile-{_DEV}"

    def _queue(self):
        import django_rq
        from rq import Queue

        conn = django_rq.get_queue("default").connection
        return Queue(self._QUEUE, connection=conn)

    def setUp(self):
        super().setUp()
        self._clear()

    def tearDown(self):
        self._clear()
        super().tearDown()

    def _clear(self):
        from rq.exceptions import NoSuchJobError
        from rq.job import Job as RqJob

        q = self._queue()
        try:
            RqJob.fetch(self._JOB_ID, connection=q.connection).delete()
        except NoSuchJobError:
            pass
        q.empty()

    def _stale(self, job, hours=1):
        """Age *job*'s timestamps — a real orphan has gone untouched for a long time.

        Freshness is the ONLY thing separating an orphan from a job a worker has just
        dequeued and not yet registered as started: by registry membership alone the two are
        identical (in neither). So an orphan fixture that is seconds old is not an orphan
        fixture at all.
        """
        from datetime import UTC, datetime, timedelta

        old = datetime.now(UTC) - timedelta(hours=hours)
        job.enqueued_at = old
        job.started_at = None
        job.last_heartbeat = old
        job.save()
        return job

    def test_orphaned_job_is_cleared_and_reconcile_re_enqueued(self):
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile, run_device_reconcile

        q = self._queue()
        # Build an orphan exactly like a worker that died after dequeue: the job hash persists with
        # a stale 'queued' status, but it's no longer in the pending list — and nothing has touched
        # it since (live: an nso-reconcile-<id> stuck 'queued' for a month).
        orphan = self._stale(q.enqueue(run_device_reconcile, self._DEV, job_id=self._JOB_ID))
        q.remove(orphan.id)
        self.assertNotIn(self._JOB_ID, q.get_job_ids())  # orphaned: fetchable but not pending

        with patch("django_rq.get_queue", return_value=q):
            job = enqueue_device_reconcile(self._DEV)

        # The orphan must be cleared and a genuine reconcile enqueued (unfixed code returned the
        # orphan and never re-enqueued → the id would still be absent from the pending queue).
        self.assertIsNotNone(job)
        self.assertEqual(job.id, self._JOB_ID)
        self.assertIn(self._JOB_ID, q.get_job_ids())

    def test_a_job_mid_handoff_to_a_worker_is_not_reclaimed(self):
        """An RQ worker POPS the job (it leaves the pending queue) and only then adds it to
        StartedJobRegistry. In between it is in NEITHER — by registry membership alone it is
        indistinguishable from an orphan. The deterministic hash must NOT be reclaimed
        (deleting/re-enqueuing under the live worker overwrites the job it is about to run).

        S5a F (codex R6-3) UPDATE: the notify itself is no longer suppressed — the popped
        worker may die before its first read, losing the edge. The hash is preserved AND the
        notify continues through the unique path, whose job serializes behind the device
        lease (the old two-concurrent-reconciles race this test once guarded is now the
        lease layer's job).
        """
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile, run_device_reconcile

        q = self._queue()
        job = q.enqueue(run_device_reconcile, self._DEV, job_id=self._JOB_ID)
        q.remove(job.id)  # the worker has just popped it; it has not registered it yet
        self.assertNotIn(self._JOB_ID, q.get_job_ids())

        with patch("django_rq.get_queue", return_value=q):
            returned = enqueue_device_reconcile(self._DEV)

        # Hash preserved: the deterministic id was NOT reclaimed/re-enqueued.
        self.assertNotIn(self._JOB_ID, q.get_job_ids())
        # …but the notify was NOT dropped: a unique notify job now carries the edge.
        self.assertTrue(returned.id.startswith(f"nso-reconcile-{self._DEV}-notify-"))
        self.assertIn(returned.id, q.get_job_ids())

    def test_genuinely_queued_job_is_not_duplicated(self):
        """A truly-pending reconcile is still deduped (no pile-up) — the fix must not regress that."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            self.assertIn(self._JOB_ID, q.get_job_ids())
            second = enqueue_device_reconcile(self._DEV)  # genuinely pending → dedup

        self.assertEqual(second.id, first.id)
        self.assertEqual(q.get_job_ids().count(self._JOB_ID), 1)


class TestTrailingEdgeNotify(TestCase):
    """S5a work stream F (codex R1-F1): a notify arriving while the deterministic reconcile
    is RUNNING (or just finished) was silently dropped by the enqueue-time dedupe — the
    running job had already snapshotted, so the newest refresh never reached the overlays.
    The debounced unique enqueue makes every such notify durable; the shipped device lease
    serializes execution. Real RQ + real Redis on an isolated throwaway queue.
    """

    _DEV = 987655  # distinct from the orphan-recovery class's device
    _QUEUE = "nso-test-reconcile-trailing"
    _JOB_ID = f"nso-reconcile-{_DEV}"

    def _queue(self):
        import django_rq
        from rq import Queue

        conn = django_rq.get_queue("default").connection
        return Queue(self._QUEUE, connection=conn)

    def setUp(self):
        super().setUp()
        self._clear()

    def tearDown(self):
        self._clear()
        super().tearDown()

    def _clear(self):
        from rq.exceptions import NoSuchJobError
        from rq.job import Job as RqJob
        from rq.registry import StartedJobRegistry

        from netbox_nso_plugin.read_gate import unique_pending_key

        q = self._queue()
        conn = q.connection
        for jid in [self._JOB_ID, *q.get_job_ids(), *StartedJobRegistry(queue=q).get_job_ids(cleanup=False)]:
            try:
                RqJob.fetch(jid, connection=conn).delete()
            except NoSuchJobError:
                pass
        conn.delete(StartedJobRegistry(queue=q).key)  # drop wip composite entries wholesale
        conn.delete(unique_pending_key(self._DEV))
        q.empty()

    def _running_deterministic(self, q):
        """Fixture: the deterministic job is genuinely RUNNING (in StartedJobRegistry).

        RQ 2.10 stores ``{job_id}:{execution_id}`` composite entries written by the
        worker's execution machinery (plain ``.add()`` is NotImplemented) — write the
        entry the way a worker does, scored in the future so cleanup keeps it.
        """
        import time as _time

        from rq.registry import StartedJobRegistry

        from netbox_nso_plugin.reconcile import run_device_reconcile

        job = q.enqueue(run_device_reconcile, self._DEV, job_id=self._JOB_ID)
        q.remove(job.id)
        registry = StartedJobRegistry(queue=q)
        registry.connection.zadd(registry.key, {f"{job.id}:testexec": _time.time() + 600})
        return job

    def test_notify_during_running_reconcile_enqueues_unique_job(self):
        """RED against the drop: a notify during a RUNNING reconcile must enqueue its own
        (unique-id) job — the running job's snapshot predates this refresh."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        self._running_deterministic(q)

        with patch("django_rq.get_queue", return_value=q):
            returned = enqueue_device_reconcile(self._DEV)

        self.assertTrue(
            returned.id.startswith(f"nso-reconcile-{self._DEV}-notify-"),
            f"notify during RUNNING must not be suppressed (got {returned.id})",
        )
        self.assertIn(returned.id, q.get_job_ids())

    def test_notify_shortly_after_completion_enqueues(self):
        """RED (codex R2-F2): a notify inside the post-completion result_ttl window saw a
        fresh terminal hash in no registry and was misread as mid-handoff → dropped."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile, run_device_reconcile

        q = self._queue()
        job = q.enqueue(run_device_reconcile, self._DEV, job_id=self._JOB_ID)
        q.remove(job.id)
        job.set_status("finished")  # terminal, timestamps seconds old

        with patch("django_rq.get_queue", return_value=q):
            returned = enqueue_device_reconcile(self._DEV)

        self.assertIsNotNone(returned)
        self.assertIn(returned.id, q.get_job_ids(), "post-completion notify must enqueue")

    def test_validated_pending_unique_absorbs_further_notifies(self):
        """Steady state: ONE queued unique job absorbs every further notify (it reads
        post-refresh state when it starts) — the R6-4 pile-up bound."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        self._running_deterministic(q)

        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            second = enqueue_device_reconcile(self._DEV)
            third = enqueue_device_reconcile(self._DEV)

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.id, third.id)
        unique_ids = [j for j in q.get_job_ids() if "-notify-" in j]
        self.assertEqual(len(unique_ids), 1, "exactly one outstanding unique job")

    def test_stale_unique_pointer_fails_open(self):
        """A pointer referencing a vanished job must NOT suppress — fail open to enqueue
        (the self-healing half of the R7-2 invariant)."""
        from netbox_nso_plugin.read_gate import unique_pending_key
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        self._running_deterministic(q)
        q.connection.set(unique_pending_key(self._DEV), "nso-reconcile-ghost-notify-dead", ex=900)

        with patch("django_rq.get_queue", return_value=q):
            returned = enqueue_device_reconcile(self._DEV)

        self.assertIn(returned.id, q.get_job_ids())
        self.assertTrue(returned.id.startswith(f"nso-reconcile-{self._DEV}-notify-"))

    def test_enqueue_failure_leaves_no_pointer(self):
        """codex R6-2: the pointer is SET only AFTER a durable enqueue — a crash between
        them must leave NO pointer (a duplicate later, never a suppressed loss)."""
        from netbox_nso_plugin.read_gate import unique_pending_key
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        self._running_deterministic(q)

        with (
            patch("django_rq.get_queue", return_value=q),
            patch.object(type(q), "enqueue", side_effect=RuntimeError("redis hiccup")),
        ):
            with self.assertRaises(RuntimeError):
                enqueue_device_reconcile(self._DEV)

        self.assertIsNone(q.connection.get(unique_pending_key(self._DEV)))

    def test_body_cad_consumes_only_own_pointer(self):
        """Owner-checked compare-and-delete (codex R7-3): an older job's consume must not
        erase a NEWER job's pointer and reopen the suppression gate early."""
        from netbox_nso_plugin.read_gate import consume_unique_pending, unique_pending_key

        q = self._queue()
        conn = q.connection
        conn.set(unique_pending_key(self._DEV), "job-B", ex=900)

        self.assertFalse(consume_unique_pending(conn, self._DEV, "job-A"))
        self.assertEqual(conn.get(unique_pending_key(self._DEV)), b"job-B")
        self.assertTrue(consume_unique_pending(conn, self._DEV, "job-B"))
        self.assertIsNone(conn.get(unique_pending_key(self._DEV)))

    def test_notify_class_lease_uses_zero_retry_budget(self):
        """codex R6-4: notify-class jobs must not burn the 90s retry budget on general RQ
        workers — single attempt, defer-marker, one post-marker attempt (shipped shape)."""
        from netbox_nso_plugin.read_gate import Deferred
        from netbox_nso_plugin.reconcile import _acquire_reconcile_lease

        mgmt = type("M", (), {"pk": self._DEV})()
        with patch("netbox_nso_plugin.read_gate.acquire_for_rq") as acq:
            acq.return_value = Deferred(attempts=2, nonce="n")
            out = _acquire_reconcile_lease(mgmt, self._DEV, "notify")

        acq.assert_called_once()
        self.assertEqual(acq.call_args.kwargs.get("retry_budget_s"), 0.0)
        self.assertEqual(out.state, "deferred")
