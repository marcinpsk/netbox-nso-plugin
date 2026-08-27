# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Self-healing of stranded async-onboarding rows.

A provision job that finishes while nobody is polling the dashboard would otherwise strand
the NSODeviceManagement row in ``provisioning`` forever — NSO has onboarded the node, yet the
gated adapter-push signal never fires, so the plugin never maps/scopes/syncs it (the reported
bug: device onboarded in NSO, but the NSO tab shows nothing and there is no journal/changelog).

Two backstops advance such a row without the dashboard being open, both via
``advance_provisioning`` so the un-gated signal re-fires on success:
  * opening the device NSO tab (:class:`DeviceNSOTabView`), and
  * the hourly :class:`AdvanceStaleOnboardingJob` sweep.
"""

from unittest.mock import patch

from dcim.models import Device
from django.test import TestCase

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

from .test_django_views import ViewTestBase
from .test_onboarding import _device

# A finished, successful provision job as the adapter's /jobs/{id} would report it.
_SUCCEEDED_OK = {
    "status": "succeeded",
    "result": {"ok": True, "steps": [{"step": "sync_from", "status": "ok"}], "device_id": None},
}


class TestOnboardTabSelfHeal(ViewTestBase):
    """Opening the device NSO tab advances a row whose provision job already finished."""

    def _provisioning_device(self, name, job_id="88"):
        dev = Device.objects.create(
            name=name, device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        # Created 'provisioning' → the post_save signal is gated (no adapter call yet).
        mgmt = NSODeviceManagement.objects.create(
            device=dev,
            nso_instance=self.nso_instance,
            nso_device_name=name,
            onboard_status="provisioning",
            onboard_job_id=job_id,
        )
        return dev, mgmt

    @patch("netbox_nso_plugin.adapter_client.get_device", side_effect=AdapterError("no adapter"))
    @patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None)
    @patch("netbox_nso_plugin.adapter_client.set_scope")
    @patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 4242})
    @patch("netbox_nso_plugin.adapter_client.get_job", return_value=_SUCCEEDED_OK)
    def test_tab_render_advances_finished_row(self, _job, onboard, _scope, _notify, _dev):
        """GET on the device NSO tab flips a finished provisioning row to ready and maps it."""
        dev, mgmt = self._provisioning_device("tab-heal-rtr")
        # The adapter push is deferred to transaction.on_commit (signals.py
        # sync_scope_to_adapter); TestCase rolls its transaction back, so without this the
        # callback never runs and onboard_device is never called.
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.get(f"/dcim/devices/{dev.pk}/nso/")
        self.assertEqual(resp.status_code, 200)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "")  # advanced to ready
        onboard.assert_called_once()  # the gated adapter-push signal re-fired
        self.assertEqual(mgmt.adapter_device_id, 4242)  # device now mapped in the adapter

    @patch("netbox_nso_plugin.adapter_client.get_job", return_value={"status": "running"})
    def test_tab_render_leaves_running_row_provisioning(self, _job):
        """A still-running job leaves the row provisioning (nothing to map yet)."""
        dev, mgmt = self._provisioning_device("tab-still-running")
        resp = self.client.get(f"/dcim/devices/{dev.pk}/nso/")
        self.assertEqual(resp.status_code, 200)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provisioning")
        self.assertIsNone(mgmt.adapter_device_id)  # signal still gated

    @patch("netbox_nso_plugin.adapter_client.get_device", side_effect=AdapterError("no adapter"))
    @patch("netbox_nso_plugin.adapter_client.get_job", side_effect=AdapterError("adapter down"))
    def test_tab_render_survives_poll_error(self, _job, _dev):
        """A transient adapter outage during the self-heal never 500s the tab; row stays provisioning."""
        dev, mgmt = self._provisioning_device("tab-adapter-down")
        resp = self.client.get(f"/dcim/devices/{dev.pk}/nso/")
        self.assertEqual(resp.status_code, 200)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provisioning")


class TestAdvanceStaleOnboardingSweep(TestCase):
    """The hourly system job advances stranded rows when no page is open to poll them."""

    @classmethod
    def setUpTestData(cls):
        cls.instance = NSOInstance.objects.create(name="sweep", adapter_instance_id="sweep")

    def _provisioning(self, name, job_id):
        dev = _device(name)
        return NSODeviceManagement.objects.create(
            device=dev,
            nso_instance=self.instance,
            nso_device_name=name,
            onboard_status="provisioning",
            onboard_job_id=job_id,
        )

    @patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None)
    @patch("netbox_nso_plugin.adapter_client.set_scope")
    @patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 501})
    def test_sweep_advances_finished_leaves_running(self, onboard, _scope, _notify):
        """Sweep advances the row whose job finished, leaves the still-running one alone."""
        from netbox_nso_plugin.onboarding import advance_stale_onboarding_rows

        done = self._provisioning("sweep-done", "J-DONE")
        running = self._provisioning("sweep-run", "J-RUN")

        def fake_get_job(job_id):
            return _SUCCEEDED_OK if job_id == "J-DONE" else {"status": "running"}

        with (
            patch("netbox_nso_plugin.adapter_client.get_job", side_effect=fake_get_job),
            # the mapping push is an on_commit callback — execute it inside the test txn
            self.captureOnCommitCallbacks(execute=True),
        ):
            checked, advanced = advance_stale_onboarding_rows()

        self.assertEqual((checked, advanced), (2, 1))
        done.refresh_from_db()
        running.refresh_from_db()
        self.assertEqual(done.onboard_status, "")
        self.assertEqual(done.adapter_device_id, 501)
        self.assertEqual(running.onboard_status, "provisioning")
        onboard.assert_called_once()  # only the finished row fired the mapping signal

    @patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None)
    @patch("netbox_nso_plugin.adapter_client.set_scope")
    @patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 777})
    def test_sweep_isolates_row_errors(self, _onboard, _scope, _notify):
        """One row raising must not abort the sweep — the healthy row still advances."""
        from netbox_nso_plugin.onboarding import advance_stale_onboarding_rows

        bad = self._provisioning("sweep-bad", "J-BAD")  # created first → visited first
        good = self._provisioning("sweep-good", "J-GOOD")

        def fake_get_job(job_id):
            if job_id == "J-BAD":
                raise ValueError("boom")  # NOT AdapterError → propagates out of advance_provisioning
            return _SUCCEEDED_OK

        with patch("netbox_nso_plugin.adapter_client.get_job", side_effect=fake_get_job):
            checked, advanced = advance_stale_onboarding_rows()

        self.assertEqual(checked, 2)
        self.assertEqual(advanced, 1)  # only the good row
        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.onboard_status, "")
        self.assertEqual(bad.onboard_status, "provisioning")  # untouched by its own error

    def test_a_free_form_steps_member_still_records_the_failure(self):
        """``result`` is an object by contract; ``steps`` inside it is free-form JSON.

        A scalar steps raised on iteration and a non-dict entry raised on .get(), both out
        of the poll, so the row could never reach its terminal verdict and every later poll
        raised again. The verdict still lands, on the generic summary.
        """
        from netbox_nso_plugin.onboarding import advance_provisioning

        for index, steps in enumerate(("boom", 3, [{"status": "failed"}, "boom"])):
            with self.subTest(steps=steps):
                mgmt = self._provisioning(f"sweep-steps-{index}", "J-STEPS")
                job = {"status": "succeeded", "result": {"ok": False, "steps": steps}}

                with patch("netbox_nso_plugin.adapter_client.get_job", return_value=job):
                    result = advance_provisioning(mgmt)

                self.assertEqual(result["status"], "provision_failed")
                mgmt.refresh_from_db()
                self.assertEqual(mgmt.onboard_status, "provision_failed")
                self.assertTrue(mgmt.onboard_error)

    def test_system_job_run_delegates_to_sweep(self):
        """The JobRunner.run wrapper calls the sweep (thin shell around the domain function)."""
        from netbox_nso_plugin.jobs import AdvanceStaleOnboardingJob

        with patch("netbox_nso_plugin.onboarding.advance_stale_onboarding_rows", return_value=(0, 0)) as sweep:
            AdvanceStaleOnboardingJob.run(None)  # self unused by run()
        sweep.assert_called_once()

    def test_system_job_pauses_once_without_logging_a_failed_poll(self):
        from netbox_nso_plugin.deployment import quiesce, resume
        from netbox_nso_plugin.jobs import AdvanceStaleOnboardingJob

        mgmt = self._provisioning("sweep-quiesced", "J-QUIESCED")
        quiesce()
        try:
            with (
                self.assertNoLogs("netbox_nso_plugin.onboarding", level="ERROR"),
                self.assertLogs("netbox_nso_plugin.jobs", level="INFO") as logged,
                patch("netbox_nso_plugin.adapter_client.get_job") as get_job,
            ):
                result = AdvanceStaleOnboardingJob.run(None)
        finally:
            resume()

        self.assertIsNone(result)
        self.assertEqual(len(logged.output), 1)
        self.assertIn("paused for an intent deployment", logged.output[0])
        get_job.assert_not_called()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "provisioning")

    def test_system_job_registered_hourly(self):
        """The job is registered as an hourly system job so NetBox schedules it automatically."""
        from netbox.registry import registry

        from netbox_nso_plugin.jobs import AdvanceStaleOnboardingJob

        self.assertIn(AdvanceStaleOnboardingJob, registry["system_jobs"])
        self.assertEqual(registry["system_jobs"][AdvanceStaleOnboardingJob]["interval"], 60)


class TestOnboardAdvanceJob(TestCase):
    """run_onboard_advance is the RQ job body enqueued by the provision-complete callback."""

    @classmethod
    def setUpTestData(cls):
        cls.instance = NSOInstance.objects.create(name="advjob", adapter_instance_id="advjob")

    @patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None)
    @patch("netbox_nso_plugin.adapter_client.set_scope")
    @patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 909})
    @patch("netbox_nso_plugin.adapter_client.get_job", return_value=_SUCCEEDED_OK)
    def test_run_onboard_advance_flips_ready_and_maps(self, _job, onboard, _scope, _notify):
        """The job advances a finished provisioning row to ready and fires the mapping signal."""
        from netbox_nso_plugin.reconcile import run_onboard_advance

        dev = _device("advjob-rtr")
        mgmt = NSODeviceManagement.objects.create(
            device=dev,
            nso_instance=self.instance,
            nso_device_name="advjob-rtr",
            onboard_status="provisioning",
            onboard_job_id="J1",
        )
        with self.captureOnCommitCallbacks(execute=True):  # deferred adapter push
            run_onboard_advance(mgmt.id)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.onboard_status, "")
        self.assertEqual(mgmt.adapter_device_id, 909)
        onboard.assert_called_once()

    def test_run_onboard_advance_missing_row_is_noop(self):
        """A deleted/unknown row id is a safe no-op (the callback can race a row delete)."""
        from netbox_nso_plugin.reconcile import run_onboard_advance

        run_onboard_advance(9_999_999)  # must not raise
