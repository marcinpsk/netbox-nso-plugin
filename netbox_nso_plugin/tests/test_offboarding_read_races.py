# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Management deletion tolerates a concurrent read-state publication."""

import threading

from dcim.models import Interface
from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from netbox_nso_plugin import signals
from netbox_nso_plugin.models import NSODeviceManagement, NSOFamilyReadState, NSOInterfaceState
from netbox_nso_plugin.read_gate import SKIPPED_UNAVAILABLE, gated_family_run

from ._outbox_case import make_managed
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def unavailable_read(attempt_id):
    """Return a failed read in the same adapter incarnation and source epoch."""
    return {
        "outcome": "unavailable",
        "reason": "read_error",
        "freshness": "stale",
        "result": "error",
        "succeeded": False,
        "attempt_id": attempt_id,
        "incarnation": "11111111-aaaa-4aaa-8aaa-111111111111",
        "incarnation_born": "2026-07-01T00:00:10Z",
        "read_at": "2026-07-21T10:00:00Z",
        "source_epoch": 1,
        "payload_revision": 1,
    }


class TestOffboardingReadRaces(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        with signals.suppress_intent_push():
            self.device, self.management = make_managed("offboard-read", 4201)
            self.other_device, self.other_management = make_managed("offboard-read-neighbor", 4202)
            self.interface = Interface.objects.create(
                device=self.device, name="Ethernet1", type="1000base-t", description="Keep native description"
            )
            self.overlay = NSOInterfaceState.objects.create(
                interface=self.interface, attribute="description", status="imported"
            )
            for management in (self.management, self.other_management):
                self.assertEqual(self._observe(management, 1).disposition, SKIPPED_UNAVAILABLE)

    def _observe(self, management, attempt_id):
        def unexpected_body():
            raise AssertionError("an unavailable read must not run the reconcile body")

        return gated_family_run(
            management, "bfd", unavailable_read(attempt_id), unexpected_body, epoch=management.adapter_device_id
        )

    def test_delete_completes_when_reconcile_updates_read_state_after_planning(self):
        self._delete_during_read_update()

    def test_caller_rollback_restores_deletion_after_read_state_retry(self):
        management_id = self.management.pk
        with self.assertRaisesRegex(RuntimeError, "cancel offboard"), transaction.atomic():
            self._delete_during_read_update()
            raise RuntimeError("cancel offboard")

        self.assertTrue(NSODeviceManagement.objects.filter(pk=management_id).exists())
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.status, "imported")
        read = NSOFamilyReadState.objects.get(management_id=management_id, family="bfd")
        self.assertEqual(read.observed_attempt_id, 2)

    def _delete_during_read_update(self):
        before_lock = threading.Event()
        published = threading.Event()
        failures = []
        observations = []
        management_id = self.management.pk

        def reconcile():
            try:
                if not before_lock.wait(timeout=30):
                    raise AssertionError("deletion did not reach management lock acquisition")
                observations.append(self._observe(self.management, 2).disposition)
            except BaseException as exc:  # noqa: BLE001 (asserted on the test thread)
                failures.append(exc)
            finally:
                connections.close_all()
                published.set()

        worker = threading.Thread(target=reconcile, daemon=True)
        worker.start()

        def publish_before_lock(execute, sql, params, many, context):
            if (
                not before_lock.is_set()
                and sql.startswith("SELECT")
                and '"netbox_nso_plugin_nsodevicemanagement"' in sql
                and "FOR UPDATE" in sql
            ):
                before_lock.set()
                if not published.wait(timeout=30):
                    raise AssertionError("the concurrent read did not commit")
                self.assertEqual(failures, [])
                self.assertEqual(observations, [SKIPPED_UNAVAILABLE])
                self.assertEqual(
                    NSOFamilyReadState.objects.get(management_id=management_id, family="bfd").observed_attempt_id,
                    2,
                )
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(publish_before_lock):
                self.management.delete()
        finally:
            before_lock.set()
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive(), "the reconcile worker did not stop")
            self.assertEqual(failures, [])

        self.assertTrue(published.is_set())
        self.assertFalse(NSODeviceManagement.objects.filter(pk=management_id).exists())
        self.assertFalse(NSOFamilyReadState.objects.filter(management_id=management_id).exists())
        self.assertFalse(NSOInterfaceState.objects.filter(interface=self.interface).exists())
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.description, "Keep native description")
        self.other_management.refresh_from_db()
        other_read = NSOFamilyReadState.objects.get(management=self.other_management, family="bfd")
        self.assertEqual(other_read.observed_attempt_id, 1)
