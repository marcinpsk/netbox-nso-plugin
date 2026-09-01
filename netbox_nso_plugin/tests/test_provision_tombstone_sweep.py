# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Provision tombstone completion at the real database seam."""

import threading
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from ._outbox_case import make_device
from .mixins import _CascadeFlushMixin


def _in_second_connection(work) -> bool:
    """Run *work* on its own connection and report whether it completed unblocked."""
    observed = {}

    def probe():
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                work()
            observed["free"] = True
        except DatabaseError:
            observed["free"] = False
        finally:
            close_old_connections()

    prober = threading.Thread(target=probe)
    prober.start()
    prober.join(timeout=30)
    return observed.get("free", "probe did not finish")


def _device_is_editable(device_pk) -> bool:
    """Report whether a foreign NetBox edit of the device row can proceed right now."""
    from dcim.models import Device

    return _in_second_connection(lambda: Device.objects.filter(pk=device_pk).update(description="foreign edit"))


def _row_is_lockable(model, **filters) -> bool:
    """Report whether a second connection can take the row lock right now."""
    return _in_second_connection(lambda: list(model.objects.select_for_update().filter(**filters)))


class TestProvisionEvidenceValidation(SimpleTestCase):
    def test_terminal_evidence_rejects_an_unknown_status(self):
        from netbox_nso_plugin.provision_lifecycle import validate_provision_evidence

        with self.assertRaises(ValueError):
            validate_provision_evidence({"status": "suceeded"}, terminal_required=True)

    def test_success_evidence_requires_a_boolean_result(self):
        from netbox_nso_plugin.provision_lifecycle import validate_provision_evidence

        with self.assertRaises(ValueError):
            validate_provision_evidence(
                {"status": "succeeded", "result": {"ok": "false"}},
                terminal_required=True,
            )

    def test_terminal_evidence_rejects_an_invalid_adapter_device_id(self):
        from netbox_nso_plugin.provision_lifecycle import validate_provision_evidence

        for device_id in (True, 0, -1, "701"):
            with self.subTest(device_id=device_id), self.assertRaises(ValueError):
                validate_provision_evidence(
                    {"status": "succeeded", "result": {"ok": True, "device_id": device_id}},
                    terminal_required=True,
                )


class TestProvisionTombstoneSweep(TestCase):
    def _attempt(self, tag, *, with_management, state="terminal"):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOProvisionTombstone

        device = make_device(tag)
        instance = NSOInstance.objects.create(name=f"{tag}-nso", adapter_instance_id=f"{tag}-nso")
        management = None
        if with_management:
            management = NSODeviceManagement.objects.create(
                device=device,
                nso_instance=instance,
                nso_device_name=f"{tag}-device",
                onboard_status="provisioning",
                onboard_job_id="71",
            )
        provision_attempt_id = uuid4()
        evidence = {
            "provision_attempt_id": str(provision_attempt_id),
            "status": "succeeded",
            "job_id": 71,
            "result": {"ok": True, "steps": [{"name": "create", "ok": True}]},
        }
        tombstone = NSOProvisionTombstone.objects.create(
            provision_attempt_id=provision_attempt_id,
            netbox_device_id=device.pk,
            nso_instance=instance.adapter_instance_id,
            nso_device_name=f"{tag}-device",
            canonical_request={"provision_attempt_id": evidence["provision_attempt_id"]},
            adapter_job_id="71",
            adapter_device_id=701,
            state=state,
            terminal_status="succeeded" if state != "open" else "",
            terminal_evidence=evidence if state != "open" else None,
        )
        return device, instance, management, tombstone

    def test_terminal_attempt_with_surviving_management_closes_without_offboarding(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, management, tombstone = self._attempt("provision-survives", with_management=True)
        with patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard:
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        management.refresh_from_db()
        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(management.onboard_status, "")
        self.assertEqual(management.onboard_steps, [{"name": "create", "ok": True}])
        self.assertEqual(tombstone.state, "closed")
        self.assertIsNotNone(tombstone.closed_at)
        offboard.assert_not_called()

    def test_success_evidence_does_not_save_unrelated_management_fields(self):
        from netbox_nso_plugin.provision_lifecycle import _apply_terminal_evidence

        _device, _instance, management, tombstone = self._attempt(
            "provision-success-fields",
            with_management=True,
        )
        stored_name = management.nso_device_name
        management.nso_device_name = "stale-in-memory-name"

        with (
            patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter") as sync,
            self.captureOnCommitCallbacks(execute=True),
        ):
            _apply_terminal_evidence(management, tombstone)

        management.refresh_from_db()
        self.assertEqual(management.nso_device_name, stored_name)
        sync.assert_called_once_with(type(management), management.pk, False)

    def test_terminal_orphan_is_offboarded_and_closed(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt("provision-orphan", with_management=False)
        with patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard:
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(tombstone.state, "closed")
        self.assertIsNotNone(tombstone.closed_at)
        offboard.assert_called_once_with(701)

    def test_terminal_orphan_recovers_the_device_id_from_logical_identity(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-identity-recovery",
            with_management=False,
        )
        tombstone.adapter_device_id = None
        tombstone.save(update_fields=["adapter_device_id"])
        adapter_row = {
            "id": 702,
            "nso_instance": tombstone.nso_instance,
            "nso_device_name": tombstone.nso_device_name,
            "netbox_device_id": None,
        }

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[adapter_row]) as inventory,
            patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard,
        ):
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(tombstone.state, "closed")
        self.assertEqual(tombstone.adapter_device_id, 702)
        inventory.assert_called_once()
        offboard.assert_called_once_with(702)

    def test_failed_orphan_with_no_adapter_mapping_closes_as_already_absent(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-no-device",
            with_management=False,
        )
        tombstone.adapter_device_id = None
        tombstone.terminal_status = "failed"
        tombstone.terminal_evidence = {"status": "failed", "error": {"code": "connect_failed"}}
        tombstone.save(update_fields=["adapter_device_id", "terminal_status", "terminal_evidence"])

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]) as inventory,
            patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard,
        ):
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(tombstone.state, "closed")
        inventory.assert_called_once()
        offboard.assert_not_called()

    def test_invalid_adapter_inventory_is_an_adapter_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-invalid-inventory",
            with_management=False,
        )
        tombstone.adapter_device_id = None
        tombstone.save(update_fields=["adapter_device_id"])

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value={"unexpected": "object"}),
            self.assertRaises(AdapterError) as caught,
        ):
            sweep_provision_tombstones(tombstone.provision_attempt_id)

        self.assertEqual(caught.exception.code, "invalid_response")

    def test_falsey_invalid_adapter_inventory_is_an_adapter_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-falsey-inventory",
            with_management=False,
        )
        tombstone.adapter_device_id = None
        tombstone.save(update_fields=["adapter_device_id"])

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value={}),
            self.assertRaises(AdapterError) as caught,
        ):
            sweep_provision_tombstones(tombstone.provision_attempt_id)

        self.assertEqual(caught.exception.code, "invalid_response")

    def test_invalid_adapter_inventory_entry_is_an_adapter_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-invalid-inventory-entry",
            with_management=False,
        )
        tombstone.adapter_device_id = None
        tombstone.save(update_fields=["adapter_device_id"])

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[None]),
            self.assertRaises(AdapterError) as caught,
        ):
            sweep_provision_tombstones(tombstone.provision_attempt_id)

        self.assertEqual(caught.exception.code, "invalid_response")

    def test_open_attempt_is_polled_by_attempt_identity_and_completed(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, management, tombstone = self._attempt(
            "provision-open",
            with_management=True,
            state="open",
        )
        evidence = {
            "provision_attempt_id": str(tombstone.provision_attempt_id),
            "status": "succeeded",
            "job_id": 71,
            "result": {"ok": True, "device_id": 701, "steps": [{"name": "sync", "ok": True}]},
        }
        with (
            patch("netbox_nso_plugin.adapter_client.get_provision_attempt", return_value=evidence) as poll,
            patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard,
        ):
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        management.refresh_from_db()
        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(management.onboard_status, "")
        self.assertEqual(management.onboard_steps, [{"name": "sync", "ok": True}])
        self.assertEqual(tombstone.state, "closed")
        poll.assert_called_once_with(tombstone.provision_attempt_id)
        offboard.assert_not_called()

    def test_malformed_open_attempt_evidence_is_an_adapter_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-invalid-attempt",
            with_management=False,
            state="open",
        )

        with (
            patch("netbox_nso_plugin.adapter_client.get_provision_attempt", return_value={"status": []}),
            self.assertRaises(AdapterError) as caught,
        ):
            sweep_provision_tombstones(tombstone.provision_attempt_id)

        self.assertEqual(caught.exception.code, "invalid_response")

    def test_open_attempt_recovers_the_adapter_job_id_from_its_receipt(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-job-recovery",
            with_management=False,
            state="open",
        )
        tombstone.adapter_job_id = ""
        tombstone.save(update_fields=["adapter_job_id"])
        evidence = {
            "provision_attempt_id": str(tombstone.provision_attempt_id),
            "status": "running",
            "job_id": 73,
            "result": None,
            "error": None,
        }

        with patch("netbox_nso_plugin.adapter_client.get_provision_attempt", return_value=evidence):
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 0))
        self.assertEqual(tombstone.state, "open")
        self.assertEqual(tombstone.adapter_job_id, "73")

    def test_terminal_evidence_with_a_conflicting_job_id_is_an_adapter_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import mark_provision_terminal

        _device, _instance, _management, tombstone = self._attempt(
            "provision-terminal-job-conflict",
            with_management=False,
            state="open",
        )
        evidence = {
            "provision_attempt_id": str(tombstone.provision_attempt_id),
            "status": "failed",
            "job_id": 72,
            "error": {"code": "provision_failed"},
        }

        with self.assertRaises(AdapterError) as caught:
            mark_provision_terminal(tombstone.provision_attempt_id, evidence)

        self.assertEqual(caught.exception.code, "invalid_response")
        tombstone.refresh_from_db()
        self.assertEqual(tombstone.state, "open")

    def test_failed_attempt_marks_surviving_management_failed(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, management, tombstone = self._attempt("provision-failed", with_management=True)
        tombstone.terminal_status = "failed"
        tombstone.terminal_evidence = {
            "status": "failed",
            "error": {"code": "provision_failed"},
        }
        tombstone.save(update_fields=["terminal_status", "terminal_evidence"])

        with patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard:
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        management.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(management.onboard_status, "provision_failed")
        self.assertEqual(management.onboard_error, "Provisioning failed. See the server log.")
        offboard.assert_not_called()

    def test_offboard_404_is_success(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt("provision-absent", with_management=False)
        with patch(
            "netbox_nso_plugin.adapter_client.delete_provisioned_device",
            side_effect=AdapterError("not found", code="not_found", status_code=404),
        ) as offboard:
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(tombstone.state, "closed")
        offboard.assert_called_once_with(701)

    def test_offboarded_attempt_recovers_a_lost_close_without_repeating_delete(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, _management, tombstone = self._attempt(
            "provision-offboarded",
            with_management=False,
            state="offboarded",
        )
        with patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard:
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(tombstone.state, "closed")
        offboard.assert_not_called()

    def test_new_management_incarnation_closes_old_attempt_without_device_delete(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, management, tombstone = self._attempt("provision-reonboarded", with_management=True)
        management.onboard_job_id = "72"
        management.save(update_fields=["onboard_job_id"])

        with patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard:
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        management.refresh_from_db()
        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(management.onboard_status, "provisioning")
        self.assertEqual(tombstone.state, "closed")
        offboard.assert_not_called()

    def test_ui_poll_delegates_completion_to_the_attempt_sweep(self):
        from netbox_nso_plugin.onboarding import advance_provisioning

        _device, _instance, management, tombstone = self._attempt(
            "provision-ui",
            with_management=True,
            state="open",
        )
        with (
            patch("netbox_nso_plugin.provision_lifecycle.sweep_provision_tombstones", return_value=(1, 0)) as sweep,
            patch("netbox_nso_plugin.adapter_client.get_job") as legacy_poll,
        ):
            result = advance_provisioning(management)

        self.assertEqual(result["status"], "provisioning")
        sweep.assert_called_once_with(tombstone.provision_attempt_id)
        legacy_poll.assert_not_called()


class TestProvisionSweepBudget(TestCase):
    """The fleet sweep is bounded, and an attempt the adapter lost cannot strand a row."""

    def _open_tombstone(self, tag, *, netbox_device_id, age, adapter_job_id=""):
        from netbox_nso_plugin.models import NSOProvisionTombstone

        row = NSOProvisionTombstone.objects.create(
            netbox_device_id=netbox_device_id,
            nso_instance=f"{tag}-nso",
            nso_device_name=f"{tag}-device",
            canonical_request={},
            adapter_job_id=adapter_job_id,
        )
        NSOProvisionTombstone.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
        row.refresh_from_db()
        return row

    def _stranded_attempt(self, tag, *, age):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = make_device(tag)
        instance = NSOInstance.objects.create(name=f"{tag}-nso", adapter_instance_id=f"{tag}-nso")
        management = NSODeviceManagement.objects.create(
            device=device,
            nso_instance=instance,
            nso_device_name=f"{tag}-device",
            onboard_status="provisioning",
            onboard_job_id="99",
        )
        tombstone = self._open_tombstone(tag, netbox_device_id=device.pk, age=age, adapter_job_id="99")
        return management, tombstone

    def test_a_fleet_sweep_polls_at_most_one_budgeted_page(self):
        from netbox_nso_plugin.provision_lifecycle import _FLEET_SWEEP_LIMIT, sweep_provision_tombstones

        rows = [
            self._open_tombstone("budget", netbox_device_id=index + 1, age=timedelta(seconds=10_000 - index))
            for index in range(_FLEET_SWEEP_LIMIT + 2)
        ]
        polled = []

        def poll(attempt_id):
            polled.append(attempt_id)
            return {"status": "running"}

        with patch("netbox_nso_plugin.adapter_client.get_provision_attempt", side_effect=poll):
            checked, closed = sweep_provision_tombstones()

        self.assertEqual((checked, closed), (_FLEET_SWEEP_LIMIT, 0))
        self.assertEqual(polled, [row.provision_attempt_id for row in rows[:_FLEET_SWEEP_LIMIT]])

    def test_a_fleet_sweep_stops_before_starting_work_past_its_deadline(self):
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        rows = [
            self._open_tombstone("deadline", netbox_device_id=index + 1, age=timedelta(seconds=100 - index))
            for index in range(2)
        ]
        polled = []

        def poll(attempt_id):
            polled.append(attempt_id)
            return {"status": "running"}

        with (
            patch("netbox_nso_plugin.adapter_client.get_provision_attempt", side_effect=poll),
            patch("netbox_nso_plugin.provision_lifecycle._monotonic", side_effect=(0.0, 1.0)),
        ):
            checked, closed = sweep_provision_tombstones(deadline=1.0)

        self.assertEqual((checked, closed), (1, 0))
        self.assertEqual(polled, [rows[0].provision_attempt_id])

    def test_a_later_attempt_reaches_the_next_budgeted_page(self):
        from netbox_nso_plugin.provision_lifecycle import _FLEET_SWEEP_LIMIT, sweep_provision_tombstones

        rows = [
            self._open_tombstone("rotation", netbox_device_id=index + 1, age=timedelta(seconds=10_000 - index))
            for index in range(_FLEET_SWEEP_LIMIT + 1)
        ]
        polled = []

        def poll(attempt_id):
            polled.append(attempt_id)
            return {"status": "running"}

        with patch("netbox_nso_plugin.adapter_client.get_provision_attempt", side_effect=poll):
            sweep_provision_tombstones()
            sweep_provision_tombstones()

        self.assertIn(rows[-1].provision_attempt_id, polled[_FLEET_SWEEP_LIMIT:])

    def test_an_attempt_the_adapter_has_no_record_of_ages_out_to_failed(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import _UNKNOWN_ATTEMPT_MAX_AGE, sweep_provision_tombstones

        management, tombstone = self._stranded_attempt(
            "aged-out",
            age=_UNKNOWN_ATTEMPT_MAX_AGE + timedelta(minutes=1),
        )
        missing = AdapterError("no such provision attempt", code="not_found", status_code=404)

        with (
            patch("netbox_nso_plugin.adapter_client.get_provision_attempt", side_effect=missing),
            patch("netbox_nso_plugin.adapter_client.delete_provisioned_device") as offboard,
        ):
            checked, closed = sweep_provision_tombstones()

        management.refresh_from_db()
        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(management.onboard_status, "provision_failed")
        self.assertEqual(tombstone.state, "closed")
        self.assertEqual(tombstone.terminal_status, "failed")
        offboard.assert_not_called()

    def test_a_recent_unknown_attempt_waits_without_logging_a_traceback(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        management, tombstone = self._stranded_attempt("still-young", age=timedelta(minutes=1))
        missing = AdapterError("no such provision attempt", code="not_found", status_code=404)

        with patch("netbox_nso_plugin.adapter_client.get_provision_attempt", side_effect=missing):
            with self.assertNoLogs("netbox_nso_plugin.provision_lifecycle", level="ERROR"):
                checked, closed = sweep_provision_tombstones()

        management.refresh_from_db()
        tombstone.refresh_from_db()
        self.assertEqual((checked, closed), (1, 0))
        self.assertEqual(tombstone.state, "open")
        self.assertEqual(management.onboard_status, "provisioning")

    def test_a_single_attempt_sweep_surfaces_its_adapter_failure(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _management, tombstone = self._stranded_attempt("surfaced", age=timedelta(minutes=1))

        with patch(
            "netbox_nso_plugin.adapter_client.get_provision_attempt",
            side_effect=AdapterError("adapter down", code="nso_unreachable"),
        ):
            with self.assertRaises(AdapterError):
                sweep_provision_tombstones(tombstone.provision_attempt_id)


class TestProvisionOffboardFence(_CascadeFlushMixin, TransactionTestCase):
    def test_provision_attempt_is_committed_before_the_adapter_post(self):
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInstance, NSOProvisionTombstone
        from netbox_nso_plugin.onboarding import onboard_candidate

        device = make_device("provision-durable")
        interface = Interface.objects.create(device=device, name="mgmt0", type="virtual")
        address = IPAddress.objects.create(address="198.18.70.1/24", assigned_object=interface)
        device.primary_ip4 = address
        device.save(update_fields=["primary_ip4"])
        instance = NSOInstance.objects.create(name="provision-durable-nso", adapter_instance_id="durable-nso")
        observed = []

        def read_committed_attempt(attempt_id):
            close_old_connections()
            try:
                observed.append(NSOProvisionTombstone.objects.filter(provision_attempt_id=attempt_id).exists())
            finally:
                close_old_connections()

        def provision(**request):
            reader = threading.Thread(
                target=read_committed_attempt,
                args=(request["provision_attempt_id"],),
            )
            reader.start()
            reader.join(timeout=10)
            self.assertFalse(reader.is_alive())
            return {"job_id": "72", "status": "queued"}

        with patch("netbox_nso_plugin.adapter_client.provision_device", side_effect=provision):
            result = onboard_candidate(device, instance, ned_id="cisco-ios-cli-6.114")

        self.assertTrue(result["ok"])
        self.assertEqual(observed, [True])

    def test_adapter_post_does_not_hold_the_device_row_lock(self):
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInstance
        from netbox_nso_plugin.onboarding import onboard_candidate

        device = make_device("provision-unlocked-post")
        interface = Interface.objects.create(device=device, name="mgmt0", type="virtual")
        address = IPAddress.objects.create(address="198.18.72.1/24", assigned_object=interface)
        device.primary_ip4 = address
        device.save(update_fields=["primary_ip4"])
        instance = NSOInstance.objects.create(
            name="provision-unlocked-post-nso",
            adapter_instance_id="provision-unlocked-post-nso",
        )
        observed = []

        def provision(**_request):
            observed.append(_device_is_editable(device.pk))
            return {"job_id": "73", "status": "queued"}

        with patch("netbox_nso_plugin.adapter_client.provision_device", side_effect=provision):
            result = onboard_candidate(device, instance, ned_id="cisco-ios-cli-6.114")

        self.assertTrue(result["ok"])
        self.assertEqual(observed, [True])

    def test_reonboard_waits_while_old_attempt_is_offboarding(self):
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOProvisionTombstone
        from netbox_nso_plugin.onboarding import normalize_nso_device_name, onboard_candidate
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        device = make_device("provision-fence")
        interface = Interface.objects.create(device=device, name="mgmt0", type="virtual")
        address = IPAddress.objects.create(address="198.18.71.1/24", assigned_object=interface)
        device.primary_ip4 = address
        device.save(update_fields=["primary_ip4"])
        instance = NSOInstance.objects.create(name="provision-fence-nso", adapter_instance_id="provision-fence-nso")
        tombstone = NSOProvisionTombstone.objects.create(
            netbox_device_id=device.pk,
            nso_instance=instance.adapter_instance_id,
            nso_device_name=normalize_nso_device_name(device.name),
            canonical_request={"provision_attempt_id": "9b0e2948-8104-4d32-adf3-0f0a8817409b"},
            adapter_job_id="71",
            adapter_device_id=701,
            state="terminal",
            terminal_status="succeeded",
            terminal_evidence={"status": "succeeded", "result": {"ok": True, "device_id": 701}},
        )
        delete_started = threading.Event()
        release_delete = threading.Event()
        provision_started = threading.Event()
        errors = []

        def delete_old(_adapter_device_id):
            delete_started.set()
            if not release_delete.wait(timeout=10):
                raise TimeoutError("test did not release the offboard request")

        def run_sweep():
            close_old_connections()
            try:
                sweep_provision_tombstones(tombstone.provision_attempt_id)
            except Exception as exc:  # pragma: no cover - reported by the parent assertion
                errors.append(exc)
            finally:
                close_old_connections()

        def run_onboard():
            close_old_connections()
            try:
                current_device = type(device).objects.get(pk=device.pk)
                current_instance = NSOInstance.objects.get(pk=instance.pk)
                onboard_candidate(current_device, current_instance, ned_id="cisco-ios-cli-6.114")
            except Exception as exc:  # pragma: no cover - reported by the parent assertion
                errors.append(exc)
            finally:
                close_old_connections()

        def provision_new(**_request):
            provision_started.set()
            return {"job_id": "72", "status": "queued"}

        with (
            patch("netbox_nso_plugin.adapter_client.delete_provisioned_device", side_effect=delete_old),
            patch("netbox_nso_plugin.adapter_client.provision_device", side_effect=provision_new),
        ):
            sweeping = threading.Thread(target=run_sweep)
            sweeping.start()
            self.assertTrue(delete_started.wait(timeout=10))
            onboarding = threading.Thread(target=run_onboard)
            onboarding.start()
            crossed_delete = provision_started.wait(timeout=1)
            release_delete.set()
            sweeping.join(timeout=10)
            onboarding.join(timeout=10)

        self.assertFalse(crossed_delete)
        self.assertFalse(sweeping.is_alive())
        self.assertFalse(onboarding.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(provision_started.is_set())
        self.assertTrue(NSODeviceManagement.objects.filter(device=device, onboard_job_id="72").exists())


class TestProvisionSweepLockWindow(_CascadeFlushMixin, TransactionTestCase):
    """The sweep must not pin foreign NetBox rows while it talks to the adapter."""

    def _terminal_attempt(self, tag, *, with_management):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOProvisionTombstone

        device = make_device(tag)
        instance = NSOInstance.objects.create(name=f"{tag}-nso", adapter_instance_id=f"{tag}-nso")
        management = None
        if with_management:
            management = NSODeviceManagement.objects.create(
                device=device,
                nso_instance=instance,
                nso_device_name=f"{tag}-device",
                onboard_status="provisioning",
                onboard_job_id="71",
            )
        evidence = {
            "status": "succeeded",
            "job_id": 71,
            "result": {"ok": True, "device_id": 701, "steps": [{"name": "create", "ok": True}]},
        }
        tombstone = NSOProvisionTombstone.objects.create(
            netbox_device_id=device.pk,
            nso_instance=instance.adapter_instance_id,
            nso_device_name=f"{tag}-device",
            canonical_request={},
            adapter_job_id="71",
            adapter_device_id=701,
            state="terminal",
            terminal_status="succeeded",
            terminal_evidence=evidence,
        )
        return device, instance, management, tombstone

    def test_the_orphan_offboard_holds_no_device_or_instance_lock(self):
        """A foreign device edit must not queue behind the offboard round trip."""
        from netbox_nso_plugin.models import NSOInstance
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        device, instance, _management, tombstone = self._terminal_attempt(
            "provision-unlocked-offboard",
            with_management=False,
        )
        observed = {}

        def offboard(_adapter_device_id):
            observed["device"] = _device_is_editable(device.pk)
            observed["instance"] = _row_is_lockable(NSOInstance, pk=instance.pk)

        with patch("netbox_nso_plugin.adapter_client.delete_provisioned_device", side_effect=offboard):
            checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(observed, {"device": True, "instance": True})

    def test_the_completion_save_does_not_pin_the_shared_instance_row(self):
        """Every device on the instance shares that row; the completion must not lock it.

        The device row IS locked here, by the writer protocol's own renderer-source lock
        (intent_state._lock_rows), not by the sweep. No adapter call runs under it.
        """
        from django.db.models.signals import post_save

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        _device, _instance, management, tombstone = self._terminal_attempt(
            "provision-unlocked-save",
            with_management=True,
        )
        observed = {}

        def probe(sender, instance, **kwargs):
            if instance.pk != management.pk or observed:
                return
            observed["instance"] = _row_is_lockable(NSOInstance, pk=instance.nso_instance_id)

        post_save.connect(probe, sender=NSODeviceManagement)
        self.addCleanup(post_save.disconnect, probe, sender=NSODeviceManagement)

        checked, closed = sweep_provision_tombstones(tombstone.provision_attempt_id)

        management.refresh_from_db()
        self.assertEqual((checked, closed), (1, 1))
        self.assertEqual(management.onboard_status, "")
        self.assertEqual(observed, {"instance": True})
