# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The five-minute fleet cadence: rotation, isolation, quiescence, and its backstop sweep."""

from unittest.mock import patch

from django.test import TransactionTestCase

from ._outbox_case import make_managed, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class _FleetCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Three managed devices, in device-id order, with the control POST answered."""

    def setUp(self):
        super().setUp()
        set_scope = patch("netbox_nso_plugin.adapter_client.set_scope", return_value={})
        set_scope.start()
        self.addCleanup(set_scope.stop)
        self.fleet = [make_managed("fleet", 16280 + index, index=index) for index in range(3)]
        self.device_ids = [device.pk for device, _management in self.fleet]

    @staticmethod
    def _canned(audited=(), repaired=(), deferred=(), unknown=(), record=None):
        """Stand in for one device audit, recording the order the pass reached devices."""
        from netbox_nso_plugin.renderer_audit import RendererAuditResult

        def audit(device_id, scopes, trigger, deadline=None, pre_capture=False):
            if record is not None:
                record.append(device_id)
            return RendererAuditResult(audited, repaired, deferred, unknown)

        return audit


class TestRendererFleetAudit(_FleetCase):
    def test_the_pass_establishes_a_trusted_baseline_on_every_managed_device(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.renderer_audit import audit_renderer_fleet

        for index, (_device, management) in enumerate(self.fleet):
            own_vlan(management, 1640 + index, f"fleet-{index}")

        result = audit_renderer_fleet()

        self.assertEqual((result.devices, result.failed, result.unknown, result.deferred), (3, 0, 0, 0))
        for device_id in self.device_ids:
            self.assertEqual(
                NSOIntentRevision.objects.filter(
                    device_id=device_id,
                    verified_fingerprint__isnull=False,
                ).count(),
                len(delivery.delivery_keys()),
            )

    def test_the_pass_totals_every_device_result_it_collected(self):
        from netbox_nso_plugin.renderer_audit import audit_renderer_fleet

        audit = self._canned(audited=("vlan",), repaired=("vlan",), deferred=("bgp",), unknown=("isis",))
        with patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit):
            result = audit_renderer_fleet()

        self.assertEqual(
            (result.devices, result.repaired, result.deferred, result.unknown, result.failed),
            (3, 3, 3, 3, 0),
        )

    def test_the_shared_deadline_defers_the_devices_the_tick_could_not_reach(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.renderer_audit import audit_renderer_fleet

        reached = []
        audit = self._canned(audited=("vlan",), record=reached)
        with (
            patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit),
            patch("netbox_nso_plugin.renderer_audit._budget_expired", side_effect=[False, True]),
        ):
            result = audit_renderer_fleet()

        self.assertEqual(len(reached), 1)
        self.assertEqual(result.devices, 1)
        self.assertEqual(result.deferred, 2 * len(delivery.delivery_keys()))

    def test_one_failing_device_does_not_stop_its_siblings(self):
        from netbox_nso_plugin.renderer_audit import RendererAuditResult, audit_renderer_fleet

        reached = []

        def audit(device_id, scopes, trigger, deadline=None, pre_capture=False):
            reached.append(device_id)
            if device_id == self.device_ids[1]:
                raise RuntimeError("this device alone is broken")
            return RendererAuditResult(("vlan",), ())

        with patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit):
            result = audit_renderer_fleet()

        self.assertEqual(reached, self.device_ids)
        self.assertEqual((result.devices, result.failed), (2, 1))

    def test_the_cadence_rotates_over_the_devices_the_previous_tick_did_not_reach(self):
        from netbox_nso_plugin.renderer_audit import audit_renderer_fleet

        reached = []
        audit = self._canned(audited=("vlan",), record=reached)
        for _tick in range(2):
            with (
                patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit),
                patch("netbox_nso_plugin.renderer_audit._budget_expired", side_effect=[False, True]),
            ):
                audit_renderer_fleet()

        self.assertEqual(reached, self.device_ids[:2])

    def test_a_device_that_raises_every_tick_does_not_hold_the_rotation(self):
        from netbox_nso_plugin.renderer_audit import audit_renderer_fleet

        reached = []

        def audit(device_id, scopes, trigger, deadline=None, pre_capture=False):
            reached.append(device_id)
            raise RuntimeError("this device is broken on every tick")

        for _tick in range(2):
            with (
                patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit),
                patch("netbox_nso_plugin.renderer_audit._budget_expired", side_effect=[False, True]),
            ):
                audit_renderer_fleet()

        self.assertEqual(reached, self.device_ids[:2])

    def test_a_quiesced_deployment_pauses_the_pass_instead_of_failing_each_device(self):
        from netbox_nso_plugin.deployment import DeploymentQuiesced, quiesce, resume
        from netbox_nso_plugin.renderer_audit import audit_renderer_fleet

        quiesce()
        try:
            with self.assertRaises(DeploymentQuiesced):
                audit_renderer_fleet()
        finally:
            resume()


class TestAuditRendererScopesJob(_FleetCase):
    def test_the_job_returns_the_fleet_result_it_ran(self):
        from netbox_nso_plugin.jobs import AuditRendererScopesJob

        audit = self._canned(audited=("vlan",), repaired=("vlan",))
        with patch("netbox_nso_plugin.renderer_audit.audit_renderer_scopes", side_effect=audit):
            result = AuditRendererScopesJob.run(None)  # self is unused by run()

        self.assertEqual((result.devices, result.repaired), (3, 3))

    def test_the_job_reports_a_quiesced_deployment_as_a_pause(self):
        from netbox_nso_plugin.deployment import quiesce, resume
        from netbox_nso_plugin.jobs import AuditRendererScopesJob

        quiesce()
        try:
            with self.assertLogs("netbox_nso_plugin.jobs", level="INFO") as logs:
                result = AuditRendererScopesJob.run(None)
        finally:
            resume()

        self.assertIsNone(result)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("paused", logs.output[0])
