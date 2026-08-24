# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Quiesce and verify an Appendix O intent-protocol deployment."""

from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin import adapter_client, delivery, drain
from netbox_nso_plugin.deployment import gate_bypass, is_quiesced, quiesce, resume


def _old_client_wait() -> int:
    """Return capped connect plus longest read timeout plus the outbox lease margin."""
    from netbox_nso_plugin.models import AdapterConnection

    configured = int(settings.PLUGINS_CONFIG.get("netbox_nso_plugin", {}).get("adapter_timeout", 30))
    timeouts = [configured, *AdapterConnection.objects.filter(enabled=True).values_list("timeout_seconds", flat=True)]
    read_timeout = max(timeouts)
    connect_timeout = min(adapter_client._CONNECT_TIMEOUT, read_timeout)
    return int(connect_timeout + read_timeout + drain.LEASE.total_seconds())


def _receipt_for(document, *, adapter_device_id: int, section: str):
    receipts = document.get("receipts") if isinstance(document, dict) else None
    if not isinstance(receipts, list):
        raise CommandError("Adapter receipt response has no receipts list")
    matches = [
        row
        for row in receipts
        if isinstance(row, dict) and row.get("device_id") == adapter_device_id and row.get("section") == section
    ]
    if len(matches) != 1:
        raise CommandError(f"Missing receipt for adapter device {adapter_device_id}/{section}")
    return matches[0]


def _verification_receipt(claim, receipt):
    """Validate and normalize the adapter receipt for the landed restore resolver."""
    wire = delivery.wire_body(claim.rendered, claim.payload, deletions=[])
    expected = {
        "push_seq": claim.push_seq,
        "request_digest": drain.wire_digest(wire),
        "store_only": False,
        "delete_origin": False,
        "backfill_only": False,
        "status_code": 200,
    }
    wrong = {
        name: (receipt.get(name), value)
        for name, value in expected.items()
        if type(receipt.get(name)) is not type(value) or receipt.get(name) != value
    }
    if wrong:
        raise CommandError(f"Verification receipt does not match the exact push: {wrong}")
    return {
        "accepted_push_seq": receipt["push_seq"],
        "request_digest": receipt["request_digest"],
        "stored_response": receipt.get("response"),
    }


class Command(BaseCommand):
    help = "Quiesce intent work before deployment, then verify the deployed protocol and resume it."

    def add_arguments(self, parser):
        actions = parser.add_mutually_exclusive_group(required=True)
        actions.add_argument("--prepare", action="store_true", help="Quiesce, wait, and run the pre-deploy checks.")
        actions.add_argument("--verify", action="store_true", help="Run the post-deploy verification push and resume.")
        actions.add_argument("--abort", action="store_true", help="Cancel a prepared gate and resume normal operation.")
        parser.add_argument("--device", type=int, dest="device_id", metavar="ID", help="Verification NetBox device id.")

    def handle(self, *args, **options):
        if options["abort"]:
            resume()
            self.stdout.write(self.style.SUCCESS("Deployment gate aborted; normal intent operation resumed"))
            return
        if options["prepare"]:
            self._prepare()
            return
        if options.get("device_id") is None:
            raise CommandError("--verify requires --device ID")
        self._verify(options["device_id"])

    def _prepare(self):
        # Release on failure ONLY a gate this invocation created: a re-prepare over a gate
        # a failed verification left active must keep writes blocked (§4.6).
        created = quiesce()
        try:
            wait = _old_client_wait()
            self.stdout.write(f"Intent work is quiesced; waiting {wait} seconds for old clients and the lease")
            time.sleep(wait)
            blockers = drain.gate_blockers()
            if blockers:
                raise CommandError("Deployment gate blocked: " + "; ".join(blockers))
        except BaseException:
            if created:
                resume()
            raise
        self.stdout.write(self.style.SUCCESS("Deployment gate prepared; deploy the adapter, then the plugin"))

    def _verify(self, device_id):
        from netbox_nso_plugin.models import NSODeviceManagement

        if not is_quiesced():
            raise CommandError("The deployment gate is not prepared")
        # A failure leaves the gate quiesced: §4.6 rolls back with writes still blocked,
        # and only a passed verification or an explicit --abort resumes normal operation.
        blockers = drain.gate_blockers()
        if blockers:
            raise CommandError("Deployment gate changed before verification: " + "; ".join(blockers))
        mgmt = NSODeviceManagement.objects.filter(device_id=device_id, adapter_device_id__isnull=False).first()
        if mgmt is None:
            raise CommandError(f"No linked NSO-managed device exists for id {device_id}")
        with gate_bypass():
            claim = drain.claim(device_id, "static_route", force=True)
            if claim is None:
                raise CommandError("Could not form the no-deletion static verification push")
            if claim.deletions:
                drain.abandon(claim)
                raise CommandError("Could not form the no-deletion static verification push")
            try:
                response = drain.send_claim(claim)
            except Exception as exc:
                if getattr(exc, "status_code", None) in {409, 422}:
                    drain.abandon(claim)
                else:
                    drain.record_failure(claim, exc)
                raise CommandError(f"Verification push failed: {exc}") from exc
            try:
                section = delivery.delivery_keys()[claim.scope].section
                document = adapter_client.get_intent_receipts(
                    device_id=mgmt.adapter_device_id,
                    section=section,
                )
                receipt = _verification_receipt(
                    claim,
                    _receipt_for(document, adapter_device_id=mgmt.adapter_device_id, section=section),
                )
                acknowledgement = drain.acknowledgement(claim, response)
                if not acknowledgement.exact:
                    raise drain.ProtocolViolation(acknowledgement.reason)
                if drain.resolve_restored_claim(device_id, claim.scope, receipt) != drain.RESTORE_SETTLED:
                    raise CommandError("Verification receipt did not settle its exact push")
            except Exception as exc:
                if self._active_verification_claim(device_id, claim.push_seq):
                    drain.record_failure(claim, exc)
                if isinstance(exc, CommandError):
                    raise
                raise CommandError(f"Verification failed: {exc}") from exc
        blockers = drain.gate_blockers()
        if blockers:
            raise CommandError("New work appeared during verification: " + "; ".join(blockers))
        resume()
        self.stdout.write(self.style.SUCCESS("Deployment verification passed; normal intent operation resumed"))

    @staticmethod
    def _active_verification_claim(device_id, push_seq):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        return NSOIntentOutboxState.objects.filter(
            device_id=device_id,
            scope="static_route",
            push_seq=push_seq,
        ).exists()
