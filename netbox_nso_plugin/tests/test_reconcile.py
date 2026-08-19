# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the off-request reconcile job and the sync-complete callback endpoint."""

import os
import threading
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from utilities.testing import APITestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

from ._outbox_case import without_commit_drain
from ._settlement_case import _make_mgmt, _SettlementCase
from .mixins import IntentPushResetMixin, _CascadeFlushMixin, isolate_other_scopes


def _make_device(name="rec-dev"):
    mfg = Manufacturer.objects.create(name=f"{name}-mfg", slug=f"{name}-mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{name}-dt", slug=f"{name}-dt")
    role = DeviceRole.objects.create(name=f"{name}-role", slug=f"{name}-role")
    site = Site.objects.create(name=f"{name}-site", slug=f"{name}-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _patch_apply_pushes(answers=None):
    """Double the forced claims the Apply takes, keyed by delivery scope.

    Apply routes every scope through ``drain.push_now``, so the claim is the boundary: what
    it answers is what the promotion gate reads. *answers* maps a scope to that answer;
    every other scope answers ``None``, which is what an unacknowledged claim returns.
    """
    answers = answers or {}
    return patch(
        "netbox_nso_plugin.drain.push_now",
        side_effect=lambda device_id, scope, **kwargs: answers.get(scope),
    )


def _forced_scopes(push, device_id):
    """The scopes the Apply forced for *device_id*, in call order."""
    return [call.args[1] for call in push.call_args_list if call.args[0] == device_id and call.kwargs.get("force")]


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

    def test_an_unmanaged_device_is_not_reported_as_a_settle_failure(self):
        """Step 4 has nothing to settle for a device with no management row, and says nothing.

        ``device.nso_management`` is a reverse one-to-one: reading it on an unmanaged device
        raises, so the guard below it never runs and the step's own handler logs a warning
        that names a failure which did not happen.
        """
        from netbox_nso_plugin import reconcile

        device = _make_device("rec-unmanaged")
        with (
            patch.object(reconcile, "reconcile_device", return_value={}),
            self.assertNoLogs("netbox_nso_plugin.reconcile", level="WARNING"),
        ):
            reconcile.run_device_reconcile(device.pk)


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
        # The adapter serializes every wire timestamp as UTC isoformat + "Z" (api/jobs.py),
        # fractional seconds included — the shape the escalation's clock has to parse.
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


def _during_settlement(action):
    """Run *action* inside Step 4's ``settle_static_routes`` call, before it locks the row.

    That call resolves its adapter device id from the row it LOCKS, so whatever *action*
    lands is what the settlement consumes, while Step 4 read its job state before the call.
    """
    from netbox_nso_plugin import settlement

    real_settle = settlement.settle_static_routes

    def act_then_settle(mgmt, **kwargs):
        action()
        return real_settle(mgmt, **kwargs)

    return patch.object(settlement, "settle_static_routes", act_then_settle)


def _commit_link_repair(mgmt_pk: int, new_adapter_device_id: int | None):
    """A link repair that really COMMITS: written on another connection, joined before it returns.

    The consumer takes the row with ``SELECT … FOR UPDATE`` and so can only ever see a
    committed write. A same-connection write inside a test transaction would discriminate
    just as well while exercising nothing but same-connection visibility.
    """

    def repair():
        def other_connection():
            try:
                NSODeviceManagement.objects.filter(pk=mgmt_pk).update(adapter_device_id=new_adapter_device_id)
            finally:
                connections.close_all()

        thread = threading.Thread(target=other_connection)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "the repair never committed, so the window is not exercised"

    return repair


class TestStepFourRereadsAfterTheSettlement(_SettlementCase):
    """Step 4's later helpers must judge the state the settlement left, not the one before it.

    ``settle_static_routes`` locks the management row, resolves its adapter device id from
    that locked row and walks the adapter's feed. The failure settle, the stuck-deploying
    escalation and the apply journal run after it, and everything Step 4 read BEFORE it can
    by then be about another adapter device (a link repair committed in the window) or about
    an apply that was not running yet (one started while the feed was walked).

    A real transaction boundary is what makes that window real, so this suite runs on
    ``_SettlementCase`` (``TransactionTestCase``) like every other adapter-double suite.
    """

    def _setup(self, tag, adapter_device_id):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id)
        # Real commits here, so the fixture's own writes would push VLAN intent the double
        # does not serve, and the failed push re-onboards the device onto a fresh adapter id.
        with suppress_intent_push():
            vlan = VLAN.objects.create(group=_device_vlan_group(device), vid=303, name="V303")
            row = NSOVLANState.objects.create(management=mgmt, vlan=vlan, device_name="V303", status="deploying")
        return mgmt, row

    @staticmethod
    def _aged(job, minutes=30):
        """Age one of the double's jobs past the stuck-deploying grace, in the adapter's wire format."""
        from datetime import timedelta

        from django.utils import timezone

        job["updated_at"] = (timezone.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        return job

    def _reconcile(self, mgmt, action):
        """Run Step 4 with *action* landing inside the settlement call."""
        from netbox_nso_plugin import reconcile

        with (
            patch.object(reconcile, "reconcile_device", return_value={}),
            _during_settlement(action),
        ):
            reconcile.run_device_reconcile(mgmt.device_id)

    def test_the_escalation_stands_down_when_the_repaired_device_is_mid_apply(self):
        """Escalating a row whose device is mid-apply is unrecoverable: that apply's own
        in_sync cannot lift a row back out of apply_failed."""
        mgmt, row = self._setup("rec-repair-active", 70)
        # 70's last apply succeeded long ago, which is an escalation verdict; 71 is mid-apply.
        self._aged(
            self.adapter.store.terminal_job(70, extra={"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}})
        )
        self.adapter.store.queued_job(71)

        self._reconcile(mgmt, _commit_link_repair(mgmt.pk, 71))

        self.assertEqual(self.adapter.store.feed_requests[0][0], 71, "the settlement did not consume the repair")
        row.refresh_from_db()
        self.assertEqual(
            row.status,
            "deploying",
            "the escalation stood on adapter device 70's job and failed a row that now belongs "
            "to 71, where an apply is in flight",
        )

    def test_the_failure_settle_and_the_journal_read_the_repaired_devices_job(self):
        mgmt, row = self._setup("rec-repair-job", 72)
        # 72's apply failed the VLAN scope; 73's succeeded and carried route-policy only.
        self._aged(
            self.adapter.store.terminal_job(
                72, status="failed", extra={"vlan_count_by_outcome": {"in_sync": 0, "apply_failed": 1}}
            )
        )
        repaired = self._aged(
            self.adapter.store.terminal_job(
                73, extra={"route_policy_count_by_outcome": {"in_sync": 2, "apply_failed": 0}}
            )
        )

        self._reconcile(mgmt, _commit_link_repair(mgmt.pk, 73))

        self.assertEqual(self.adapter.store.feed_requests[0][0], 73, "the settlement did not consume the repair")
        row.refresh_from_db()
        mgmt.refresh_from_db()
        self.assertEqual(
            row.status, "deploying", "adapter device 72's failed apply settled a row that now belongs to 73"
        )
        self.assertEqual(
            mgmt.last_journaled_apply_job,
            repaired["id"],
            "the apply journal recorded adapter device 72's job for a device that now holds 73",
        )

    def test_an_unlinked_device_is_not_judged_by_the_id_it_held(self):
        """The repair's other branch: the row loses its adapter device id entirely. There is
        no apply to judge it by, so the helpers must skip it exactly as the outer guard does."""
        mgmt, row = self._setup("rec-repair-unlinked", 74)
        self._aged(
            self.adapter.store.terminal_job(74, extra={"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}})
        )

        self._reconcile(mgmt, _commit_link_repair(mgmt.pk, None))

        row.refresh_from_db()
        mgmt.refresh_from_db()
        self.assertEqual(row.status, "deploying", "an unlinked device was judged by adapter device 74's job")
        self.assertFalse(mgmt.last_journaled_apply_job, "and its apply was journaled onto the unlinked device")
        self.assertIsNone(mgmt.adapter_device_id, "the repair did not land, so this proves nothing")

    def test_an_apply_that_starts_during_the_settlement_stands_the_escalation_down(self):
        """The re-read is not only about a repaired id. This device keeps the one it had, and
        its own apply state turns active while the feed is walked: the operator pressed Apply,
        which re-marks these rows deploying without re-stamping anything Step 4 read.

        Judging them by the pre-settlement probe fails every one of them, and that is
        unrecoverable: the running apply's own in_sync cannot lift a row back out of
        apply_failed. A re-read taken only when the adapter device id CHANGED skips exactly
        this case, which is the common one.
        """
        mgmt, row = self._setup("rec-apply-starts", 75)
        # A stale succeeded apply: an escalation verdict, until 75's next apply starts.
        self._aged(
            self.adapter.store.terminal_job(75, extra={"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}})
        )

        self._reconcile(mgmt, lambda: self.adapter.store.queued_job(75))

        self.assertEqual(
            self.adapter.store.feed_requests[0][0], 75, "the settlement consumed the same id: nothing was repaired"
        )
        row.refresh_from_db()
        self.assertEqual(
            row.status,
            "deploying",
            "the escalation stood on the probe taken before the settlement and failed a row "
            "whose device has an apply in flight",
        )


class TestStaticRouteApplySettle(APITestCase):
    """Static routes join the deploying→settle flow (the MTU/route-policy regression class:
    a scope missing from _prepare_apply/_APPLY_DEPLOYING_SCOPES strands its rows). Found
    live on rg03: a successfully applied static route stayed 'pending apply' forever —
    _prepare_apply never marked it deploying and never force-pushed its snapshot."""

    def _setup(self, status_="deploying", tag="sr-settle"):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        device = _make_device(tag)
        inst, _ = NSOInstance.objects.get_or_create(name=f"{tag}-inst", defaults={"adapter_instance_id": f"{tag}-inst"})
        mgmt = NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name=tag, adapter_device_id=89
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

    def test_the_coarse_scope_settle_no_longer_judges_static_routes(self):
        """#1502 S5 handover: the scope counter is not evidence about any particular row.

        It says the apply reported N failures for the scope, not that this device's route
        was one of them, and not which generation the result is about. The generation-
        correlated consumer is the only writer of a static-route apply verdict now, so the
        coarse settle must leave the row exactly as it found it — the alternative is a red
        badge on a route that applied cleanly beside a sibling that did not.
        """
        from netbox_nso_plugin.reconcile import _APPLY_DEPLOYING_SCOPES, _settle_apply_failures

        mgmt, row = self._setup()
        _settle_apply_failures(mgmt, {"static_route_count_by_outcome": {"in_sync": 0, "apply_failed": 1}})
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        self.assertFalse(row.last_apply_error)
        self.assertNotIn("static_route", _APPLY_DEPLOYING_SCOPES)

    def test_prepare_apply_marks_accepted_static_route_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        # A stored count, not a bare mock: the promotion gate reads the count.
        with _patch_apply_pushes({"static_route": {"device_id": 89, "count": 1, "routes": []}}) as push:
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        # And the owned snapshot is re-sent under its own forced claim, so a stale adapter
        # intent still applies.
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("static_route"), 1)

    def test_a_push_answer_with_no_stored_count_does_not_promote(self):
        """An acknowledgement is a stored count, not a truthy answer.

        The fleet re-sync already reads it that way. Promoting on anything truthy puts the
        row in 'deploying' against intent the adapter may not be holding, and no apply
        result can then name it.
        """
        from netbox_nso_plugin.views import _prepare_apply

        for tag, answer in (("sr-nondict", "stored"), ("sr-nocount", {"device_id": 89, "routes": []})):
            with self.subTest(answer=answer):
                mgmt, row = self._setup(status_="accepted", tag=tag)
                with _patch_apply_pushes({"static_route": answer}):
                    _prepare_apply(mgmt)
                row.refresh_from_db()
                self.assertEqual(row.status, "accepted")


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
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        # And the owned snapshot is re-sent under its own forced claim, so a stale adapter
        # intent still applies.
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("l2_sap"), 1)


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

    def test_a_free_form_counts_member_does_not_abort_the_settle(self):
        """``result`` is an object by contract; each ``<scope>_count_by_outcome`` is not.

        A scalar there raised out of _settle_apply_failures, and the caller's blanket except
        then skipped the whole device's settle, escalate and journal, not just this scope.
        A junk counts map must read as "this job says nothing about this scope" instead.
        """
        from netbox_nso_plugin.reconcile import _settle_apply_failures

        mgmt, row = self._setup()
        for counts in ("boom", 3, ["apply_failed"]):
            with self.subTest(counts=counts):
                row.status = "deploying"
                row.save(update_fields=["status"])

                _settle_apply_failures(mgmt, {"route_policy_count_by_outcome": counts})

                row.refresh_from_db()
                self.assertEqual(row.status, "deploying", "a junk counts map decided the row")

    def test_a_free_form_error_detail_falls_back_to_the_generic_message(self):
        """``error`` is an object by contract; ``detail`` and ``items`` inside it are not.

        A scalar ``items`` raised on iteration and a string one iterated its CHARACTERS, so
        the walk has to check what it found before walking it. Either way the row still
        settles, on the generic pointer.
        """
        from netbox_nso_plugin.reconcile import _GENERIC_APPLY_ERROR, _settle_apply_failures

        result = {"route_policy_count_by_outcome": {"in_sync": 0, "apply_failed": 1}}
        mgmt, row = self._setup()
        for error in ({"detail": "boom"}, {"detail": {"items": 3}}, {"detail": {"items": "boom"}}):
            with self.subTest(error=error):
                # Both fields are reset: a value leaking in from the previous iteration
                # would let a broken walk pass on the last one's evidence.
                row.status = "deploying"
                row.last_apply_error = ""
                row.save(update_fields=["status", "last_apply_error"])

                _settle_apply_failures(mgmt, result, {"result": result, "error": error})

                row.refresh_from_db()
                self.assertEqual(row.status, "apply_failed")
                self.assertEqual(row.last_apply_error, _GENERIC_APPLY_ERROR)

    def test_prepare_apply_marks_accepted_route_policy_deploying(self):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, row = self._setup(status_="accepted")
        with _patch_apply_pushes():
            _prepare_apply(mgmt)
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_prepare_apply_force_pushes_owned_interface_intent(self):
        """Apply re-sends the owned interface snapshot (status-based) under a forced claim, so
        an owned attribute whose adapter intent went stale is actually re-applied instead of
        silently dropped. Ownership is kept durable by the reconciler's owned-guard."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("interface"), 1)

    def test_prepare_apply_force_pushes_owned_route_policy_intent(self):
        """Apply must re-send owned ROUTE-POLICY intent too (like interface/VLAN). An owned
        route-policy object whose adapter intent went stale/empty otherwise applies 0 items and
        the row sticks in 'deploying' forever — observed on rg03, where an owned as-path had NO
        adapter intent row, so Apply pushed nothing and the row never settled."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        self.assertEqual(_forced_scopes(push, mgmt.device_id).count("route_policy"), 1)

    def test_prepare_apply_force_pushes_deferred_scopes(self):
        """Apply marks SVI/subinterface/BFD/MTU accepted->deploying, so it must also re-send
        their owned snapshots. These are mirrored as adapter intent (reactive push on accept/edit),
        but that mirror can go stale/empty (a failed push, an out-of-band adapter reset). Without a
        forced claim, Apply marks the row 'deploying' but the acknowledged baseline drops the send →
        0 items applied → the row sticks in 'deploying' forever (route-policy's rg03 failure mode)."""
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _row = self._setup(status_="accepted")
        with _patch_apply_pushes() as push:
            _prepare_apply(mgmt)
        forced = _forced_scopes(push, mgmt.device_id)
        for scope in ("svi", "subinterface", "bfd", "interface_mtu"):
            self.assertEqual(forced.count(scope), 1, scope)


class TestSnmpApplyForcePush(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """SNMP intent is mirrored reactively on accept, and a failed PUT is swallowed — so a
    device whose accept-time push failed has no adapter intent at all. Apply is the only
    recovery, exactly as for logging hosts, and must re-send the owned SNMP snapshot.

    The refresh must be STORE-ONLY: a plain put_snmp_intent enqueues the shrink-removal job
    (and auto-apply on auto_apply devices), and _prepare_apply runs before trigger_apply —
    whose _trigger 409s while any job is active, so the recovery would kill the Apply it serves.

    It runs the real claim, which is why there is no test transaction: the mode has to reach
    the wire as the query flag, and only the transport is doubled.
    """

    def test_prepare_apply_force_pushes_snmp_snapshot(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import NSOSnmpHostState
        from netbox_nso_plugin.views import _prepare_apply

        with (
            patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"),
            without_commit_drain(),
            transaction.atomic(),
        ):
            device = _make_device("snmp-apply")
            inst, _ = NSOInstance.objects.get_or_create(
                name="snmp-apply-inst", defaults={"adapter_instance_id": "snmp-apply-inst"}
            )
            mgmt = NSODeviceManagement.objects.create(
                device=device, nso_instance=inst, nso_device_name="snmp-apply", adapter_device_id=91
            )
            NSOSnmpHostState.objects.create(
                management=mgmt,
                address="198.18.0.40",
                version="v2c",
                notify_type="trap",
                community_hash="abcd1234abcd1234",
                status="accepted",
            )
        seen = {}

        def _record_store_only(*args, **kwargs):
            seen["store_only"] = adapter_client._store_only_push.get()

        with isolate_other_scopes("snmp") as stack:
            put_snmp = stack.enter_context(
                patch("netbox_nso_plugin.adapter_client.put_snmp_intent", side_effect=_record_store_only)
            )
            _prepare_apply(mgmt)
        put_snmp.assert_called_once()
        self.assertEqual(put_snmp.call_args.args[0], mgmt.adapter_device_id)
        self.assertEqual([h["address"] for h in put_snmp.call_args.args[3]], ["198.18.0.40"])
        self.assertTrue(seen.get("store_only"))


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
            _patch_apply_pushes(),
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

        from netbox_nso_plugin.reconcile import ReconcileScopeError

        with self.assertRaises(ReconcileScopeError):
            _safe_reconcile(ctx, "vlan_states", mgmt, ("NSOVLANState",), boom, object())
        self.assertEqual(ctx["vlan_states"], [])
        imported.refresh_from_db()
        owned.refresh_from_db()
        self.assertEqual(imported.status, "imported")  # marking happens only after the gate rolls back
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


class TestEnqueueDeviceReconcileWiring(TestCase):
    """READSEM 1334: `enqueue_device_reconcile` funnels every public producer through the
    queued-carrier arbiter. Real RQ Queue/Job on the configured Redis, isolated throwaway queue.
    (The former deterministic-id + orphan-reclaim machinery is retired — unique carrier ids mean a
    dead carrier is just 'not queued', so the next producer enqueues a fresh one. The arbiter's
    internal invariants live in test_read_gate.TestQueuedCarrierArbiter; this class covers the
    public wrapper + django_rq wiring end-to-end.)
    """

    _DEV = 987654  # won't collide with any real nso-reconcile-<id> job hash
    _QUEUE = f"nso-test-reconcile-wiring-{os.environ.get('NETBOX_NSO_REDIS_KEY_NAMESPACE', 'local')}"

    def _queue(self):
        import django_rq
        from rq import Queue

        conn = django_rq.get_queue("default").connection
        return Queue(self._QUEUE, connection=conn)

    def setUp(self):
        super().setUp()
        self._detached_jobs = []
        self._clear()

    def tearDown(self):
        self._clear()
        super().tearDown()

    def _clear(self):
        from netbox_nso_plugin.read_gate import carrier_key, marker_key

        q = self._queue()
        q.delete(delete_jobs=True)
        for job in self._detached_jobs:
            job.delete()
        self._detached_jobs.clear()
        q.connection.delete(carrier_key(self._DEV))
        q.connection.delete(marker_key(self._DEV))

    def _carriers(self, q):
        return [jid for jid in q.get_job_ids() if f"-{self._DEV}-carrier-" in jid]

    def test_enqueues_one_carrier(self):
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            job = enqueue_device_reconcile(self._DEV)
        self.assertEqual(self._carriers(q), [job.id])

    def test_genuinely_queued_carrier_is_not_duplicated(self):
        """A truly-queued carrier absorbs a second edge — no pile-up (it snapshots post-refresh)."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            second = enqueue_device_reconcile(self._DEV)  # genuinely queued → suppress
        self.assertEqual(first.id, second.id)
        self.assertEqual(self._carriers(q), [first.id])

    def test_started_carrier_gets_a_trailing_edge(self):
        """A carrier a worker has popped (off the queue list) is NOT suppressible — the next edge
        gets a distinct trailing carrier (the former S5a unique-notify trailing edge, now unified)."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            self._detached_jobs.append(first)
            q.remove(first.id)  # worker popped it; pointer still references it
            second = enqueue_device_reconcile(self._DEV)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self._carriers(q), [second.id])

    def test_completed_carrier_gets_a_fresh_edge(self):
        """A notify after a carrier finished (terminal hash, off the queue) → a fresh carrier."""
        from rq.job import Job as RqJob

        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            first = enqueue_device_reconcile(self._DEV)
            self._detached_jobs.append(first)
            q.remove(first.id)
            RqJob.fetch(first.id, connection=q.connection).set_status("finished")
            second = enqueue_device_reconcile(self._DEV)
        self.assertIsNotNone(second)
        self.assertIn(second.id, q.get_job_ids())

    def test_clear_deletes_queue_registry_and_detached_job(self):
        """Per-run queues and jobs already popped from them must not accumulate in Redis."""
        from netbox_nso_plugin.reconcile import enqueue_device_reconcile

        q = self._queue()
        with patch("django_rq.get_queue", return_value=q):
            detached = enqueue_device_reconcile(self._DEV)
        q.remove(detached.id)
        self._detached_jobs = [detached]
        self.assertTrue(q.connection.sismember(q.redis_queues_keys, q.key))
        self.assertTrue(q.connection.exists(detached.key))

        self._clear()

        self.assertFalse(q.connection.sismember(q.redis_queues_keys, q.key))
        self.assertFalse(q.connection.exists(detached.key))


class TestNotifyClassLeaseBudget(TestCase):
    """READSEM 1334 keeps the notify-class lease branch + ``run_device_reconcile`` param (in-flight
    jobs serialized with ``notify_class=True`` across a deploy) even though the enqueue plane no
    longer sets it. The trailing-edge is now the arbiter's job (a started carrier is not suppressible
    → a trailing carrier; see TestEnqueueDeviceReconcileWiring / TestQueuedCarrierArbiter); the
    lease-acquisition semantics for the ``notify`` call class are unchanged.
    """

    _DEV = 987655

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
