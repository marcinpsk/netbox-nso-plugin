# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Capture redaction and native edits keep the same PostgreSQL lock order."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import suppress
from threading import Event
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

from . import test_lifecycle_fence as lifecycle_cases
from ._outbox_case import content_update, make_managed, mirror_update, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestCaptureRedactionLocking(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    bgp_owners = lifecycle_cases.TestOwnerLifecycleFence.bgp_owners
    _wait_for_blocking = lifecycle_cases.TestOwnerLifecycleFence._wait_for_blocking

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("capture-redaction", 1768)
        with without_commit_drain():
            self.management.manage_bgp = True
            self.management.save(update_fields={"manage_bgp"})
            parent, (self.peer, sibling) = self.bgp_owners()
            for row in (parent, self.peer, sibling):
                mirror_update(row, status="accepted", apply_attempt_id=None)
            content_update(self.peer.bgp_peer, password="placeholder-secret-canary")
            mirror_update(self.peer, last_apply_error="device refused placeholder-secret-canary")

    def test_claim_first_redaction_does_not_invert_native_writer_locks(self):
        self._run_schedule(claim_first=True)

    def test_native_writer_first_redaction_does_not_invert_claim_locks(self):
        self._run_schedule(claim_first=False)

    def _run_schedule(self, *, claim_first):
        from netbox_nso_plugin import drain, durable_capture
        from netbox_nso_plugin.apply_state import lock_device_intent_transaction

        at_capture, continue_capture, at_device, continue_edit = Event(), Event(), Event(), Event()
        backends = []
        original = durable_capture._snapshot

        def snapshot(attempt, selection, owner, row, revisions, **kwargs):
            if row._meta.label_lower == self.peer._meta.label_lower and row.pk == self.peer.pk:
                at_capture.set()
                if not continue_capture.wait(30):
                    raise AssertionError("native writer did not reach its device lock")
            return original(attempt, selection, owner, row, revisions, **kwargs)

        def run(claim):
            close_old_connections()
            try:
                with without_commit_drain(), transaction.atomic():
                    with connection.cursor() as cursor:
                        if claim:
                            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                        cursor.execute("SELECT pg_backend_pid()")
                        backends.append(cursor.fetchone()[0])
                    if claim:
                        return drain._claim_locked(self.device.pk, "bgp", drain.delivery.MODE_NORMAL, True)
                    lock_device_intent_transaction(self.device.pk)
                    at_device.set()
                    if not continue_edit.wait(30):
                        raise AssertionError("claim did not reach its capture")
                    native = type(self.peer.bgp_peer).objects.get(pk=self.peer.bgp_peer_id)
                    content_update(native, enabled=False)
            finally:
                connection.close()

        with patch.object(durable_capture, "_snapshot", autospec=True, side_effect=snapshot):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(run, claim_first)
                self.assertTrue((at_capture if claim_first else at_device).wait(30))
                second = executor.submit(run, not claim_first)
                self.assertTrue((at_device if claim_first else at_capture).wait(30))
                try:
                    if claim_first:
                        continue_edit.set()
                        self._wait_for_blocking(backends, second)
                    else:
                        continue_capture.set()
                        # Release the writer if the claim waits on its device lock.
                        with suppress(TimeoutError):
                            second.result(timeout=2)
                finally:
                    continue_edit.set()
                    continue_capture.set()
                errors = []
                for future in (first, second):
                    try:
                        future.result(timeout=60)
                    except Exception as exc:  # noqa: BLE001 - report both real transaction failures
                        errors.append((type(exc).__name__, getattr(exc.__cause__, "sqlstate", None), str(exc)))
        self.assertEqual(len(set(backends)), 2)
        self.assertEqual(errors, [])
        self.peer.refresh_from_db()
        self.assertNotIn("placeholder-secret-canary", self.peer.last_apply_error)

    def test_capture_bookkeeping_rejects_an_unlocked_owner(self):
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.lifecycle_fence import locked_owner_bookkeeping

        with transaction.atomic(), self.assertRaisesMessage(IntentMutationProtocolError, "current capture"):
            with locked_owner_bookkeeping(self.peer):
                self.fail("an unlocked owner received a bookkeeping permit")

    def test_capture_bookkeeping_rejects_a_marker_from_a_finished_transaction(self):
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.lifecycle_fence import locked_owner_bookkeeping
        from netbox_nso_plugin.selection_manifest import lock_manifest_rows

        with transaction.atomic():
            row = lock_manifest_rows(self.management, ("bgp",))[(self.peer._meta.label_lower, self.peer.pk)]
        with transaction.atomic(), self.assertRaisesMessage(IntentMutationProtocolError, "current capture"):
            with locked_owner_bookkeeping(row):
                self.fail("an expired capture received a bookkeeping permit")
