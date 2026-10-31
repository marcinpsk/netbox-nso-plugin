# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1869: management bookkeeping takes the device intent lock before the management row.

Every intent footprint takes the device advisory lock (L4) and then the management row (L5).
A writer that locks the row first and then saves it through the exact writer asks for L4
while it holds L5, and deadlocks against any footprint that already holds L4. Each pin runs
the real writer on its own connection against a real footprint-ordered holder.
"""

from __future__ import annotations

import threading

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

from ._outbox_case import make_device, make_managed, wait_until_postgres_blocks
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestManagementLockOrder(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def _race_footprint_holder(self, device_id, management_pk, work):
        """Hold L4, let *work* queue behind it, take L5 as a footprint does, then return *work*'s result."""
        from netbox_nso_plugin.apply_state import lock_device_intent_transaction
        from netbox_nso_plugin.models import NSODeviceManagement

        backend, outcome = {}, {}
        connected = threading.Event()

        def run():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend["pid"] = cursor.fetchone()[0]
                connected.set()
                outcome["value"] = work()
            except BaseException as exc:  # noqa: BLE001 (re-raised on the test thread)
                outcome["error"] = exc
            finally:
                connected.set()
                connection.close()

        worker = threading.Thread(target=run)
        with transaction.atomic():
            lock_device_intent_transaction(device_id)
            worker.start()
            self.assertTrue(connected.wait(30), "the writer never connected")
            wait_until_postgres_blocks(backend["pid"], "the management writer", timeout=30, locktype="advisory")
            list(NSODeviceManagement.objects.select_for_update(of=("self",)).filter(pk=management_pk))
        worker.join(60)
        self.assertFalse(worker.is_alive(), "the management writer never finished")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def test_push_attempt_allocation_waits_for_the_device_lock_before_the_row(self):
        from netbox_nso_plugin import signals
        from netbox_nso_plugin.models import NSODeviceManagement

        device, mgmt = make_managed("lock-order-attempt", 7980)

        attempt = self._race_footprint_holder(
            device.pk, mgmt.pk, lambda: signals._allocate_push_attempt(device.pk, "vlan")
        )

        self.assertEqual(attempt, 1)
        self.assertEqual(NSODeviceManagement.objects.get(pk=mgmt.pk).intent_push_attempts, {"vlan": 1})

    def test_push_outcome_record_waits_for_the_device_lock_before_the_row(self):
        from netbox_nso_plugin import signals
        from netbox_nso_plugin.models import NSODeviceManagement

        device, mgmt = make_managed("lock-order-outcome", 7981)
        attempt = signals._allocate_push_attempt(device.pk, "vlan")

        self._race_footprint_holder(
            device.pk,
            mgmt.pk,
            lambda: signals._record_push_outcome(device.pk, "vlan", attempt, RuntimeError("refused")),
        )

        self.assertIn("vlan", NSODeviceManagement.objects.get(pk=mgmt.pk).intent_push_errors)

    def test_untracked_provisioning_failure_waits_for_the_device_lock_before_the_row(self):
        from netbox_nso_plugin import onboarding
        from netbox_nso_plugin.management_lifecycle import save_management
        from netbox_nso_plugin.models import NSODeviceManagement

        device, mgmt = make_managed("lock-order-untracked", 7982)
        mgmt.onboard_status = "provisioning"
        save_management(mgmt, update_fields=["onboard_status"])

        result = self._race_footprint_holder(device.pk, mgmt.pk, lambda: onboarding._fail_untracked_provisioning(mgmt))

        self.assertEqual(result["status"], "provision_failed")
        self.assertEqual(NSODeviceManagement.objects.get(pk=mgmt.pk).onboard_status, "provision_failed")

    def test_terminal_provision_completion_waits_for_the_device_lock_before_the_row(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOProvisionTombstone
        from netbox_nso_plugin.provision_lifecycle import sweep_provision_tombstones

        device = make_device("lock-order-terminal")
        instance = NSOInstance.objects.create(name="lock-order-terminal-nso", adapter_instance_id="lock-order-nso")
        management = NSODeviceManagement.objects.create(
            device=device,
            nso_instance=instance,
            nso_device_name="lock-order-terminal-device",
            onboard_status="provisioning",
            onboard_job_id="72",
        )
        tombstone = NSOProvisionTombstone.objects.create(
            netbox_device_id=device.pk,
            nso_instance=instance.adapter_instance_id,
            nso_device_name="lock-order-terminal-device",
            canonical_request={},
            adapter_job_id="72",
            adapter_device_id=702,
            state="terminal",
            terminal_status="succeeded",
            terminal_evidence={
                "status": "succeeded",
                "job_id": 72,
                "result": {"ok": True, "device_id": 702, "steps": [{"name": "create", "ok": True}]},
            },
        )

        swept = self._race_footprint_holder(
            device.pk, management.pk, lambda: sweep_provision_tombstones(tombstone.provision_attempt_id)
        )

        self.assertEqual(swept, (1, 1))
        self.assertEqual(NSODeviceManagement.objects.get(pk=management.pk).onboard_status, "")
