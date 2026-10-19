# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Quiesce and verify an Appendix O intent-protocol deployment."""

from __future__ import annotations

import copy
import json
import time
from contextlib import suppress

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin import adapter_client, delivery, drain
from netbox_nso_plugin.deployment import gate_bypass, quiesce, resume

_sleep = time.sleep


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
        if isinstance(row, dict)
        and type(row.get("device_id")) is int
        and row.get("device_id") == adapter_device_id
        and row.get("section") == section
    ]
    if len(matches) != 1:
        raise CommandError(f"Missing receipt for adapter device {adapter_device_id}/{section}")
    return matches[0]


def _verification_receipt(claim, receipt):
    """Validate and normalize the adapter receipt for the landed restore resolver."""
    wire = delivery.wire_body(
        claim.rendered,
        claim.payload,
        deletions=[],
        marking_mode=claim.marking_mode,
    )
    expected = {
        "push_seq": claim.push_seq,
        "request_digest": drain.wire_digest(wire),
        "store_only": claim.mode == delivery.MODE_STORE_ONLY,
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
    help = "Quiesce intent work, prepare explicit ownership, and verify paired settlement before resume."

    def add_arguments(self, parser):
        actions = parser.add_mutually_exclusive_group(required=True)
        actions.add_argument(
            "--prepare",
            action="store_true",
            help="Block fleet Apply, accept, drains, callbacks, settlement, and reconciliation. A failed step leaves work quiesced.",
        )
        actions.add_argument(
            "--verify",
            action="store_true",
            help="Run paired verification and resume. Require --device ID and --settled-probe UUID for success, peer refusal, and router refusal. Failure leaves work quiesced.",
        )
        actions.add_argument(
            "--resume",
            action="store_true",
            help="Run paired verification before resume. Require --device ID and three --settled-probe UUID arguments. Failure leaves work quiesced.",
        )
        actions.add_argument(
            "--abort",
            action="store_true",
            help="Run paired verification before abort releases the gate. Require --device ID and three --settled-probe UUID arguments. Failure leaves work quiesced.",
        )
        actions.add_argument(
            "--import-routers",
            action="store_true",
            help="Import BGP routers with --device ID. Grant no ownership. Keep work quiesced.",
        )
        actions.add_argument(
            "--accept-routers",
            nargs="+",
            type=int,
            metavar="ID",
            help="Accept the exact operator-selected overlay IDs with --device ID. Keep work quiesced.",
        )
        actions.add_argument(
            "--prepare-store",
            action="store_true",
            help="Prepare intent with --device ID and --scope. This action never enqueues a device Apply. Keep work quiesced.",
        )
        actions.add_argument(
            "--start-probe",
            metavar="ATTEMPT_UUID",
            help="With --device ID, capture a fresh probe UUID and permit before one manual Apply. Use --register-probe when terminal. Keep work quiesced.",
        )
        actions.add_argument(
            "--register-probe",
            metavar="ATTEMPT_UUID",
            help="Bind the terminal execution before the callback and bounded RQ worker. Use --revoke-probe to cancel its permit. Keep work quiesced.",
        )
        actions.add_argument("--revoke-probe", action="store_true", help="Revoke the probe permit. Keep work quiesced.")
        parser.add_argument("--device", type=int, dest="device_id", metavar="ID")
        parser.add_argument("--scope", choices=tuple(delivery.delivery_keys()))
        parser.add_argument("--settled-probe", action="append", default=[])

    def handle(self, *args, **options):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError
        from netbox_nso_plugin.deployment_probe import ProbeBlocked, locked_gate, register_probe, start_probe
        from netbox_nso_plugin.refusal_contract import ContractError

        try:
            if options["prepare"]:
                self._prepare()
            elif options["start_probe"]:
                if options["device_id"] is None:
                    raise CommandError("--start-probe requires --device ID")
                attempt_id = start_probe(options["start_probe"], options["device_id"])
                self.stdout.write(
                    f"Probe {attempt_id} registered before admission; bind it with --register-probe when terminal"
                )
            elif options["register_probe"]:
                register_probe(options["register_probe"])
                self.stdout.write("Verification probe authority registered; normal work stays quiesced")
            elif options["revoke_probe"]:
                with locked_gate() as control:
                    control.verification_attempt = None
                    control.save(update_fields=("verification_attempt",))
                self.stdout.write("Verification probe authority revoked; normal work stays quiesced")
            elif options["import_routers"] or options["accept_routers"] or options["prepare_store"]:
                self._maintenance(options)
            else:
                self._verify(options)
        except (
            ProbeBlocked,
            EvidenceInvariantError,
            ContractError,
            ObjectDoesNotExist,
            adapter_client.AdapterError,
            ValueError,
            OSError,
        ) as exc:
            raise CommandError(str(exc)) from exc

    def _prepare(self):
        quiesce()
        wait = _old_client_wait()
        self.stdout.write(f"Intent work is quiesced; waiting {wait} seconds for old clients and the lease")
        _sleep(wait)
        blockers = drain.gate_blockers()
        if blockers:
            raise CommandError("Deployment gate blocked: " + "; ".join(blockers))
        self.stdout.write(self.style.SUCCESS("Deployment gate prepared; keep both components quiesced"))

    def _maintenance(self, options):
        from netbox_nso_plugin.deployment_probe import maintenance_gate, read_bgp
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.signals import suppress_intent_push

        if options["device_id"] is None:
            raise CommandError("Maintenance requires --device ID")
        management = (
            NSODeviceManagement.objects.select_related("device")
            .filter(
                device_id=options["device_id"],
                adapter_device_id__isnull=False,
            )
            .first()
        )
        if management is None:
            raise CommandError("Maintenance requires a linked managed device")
        with maintenance_gate(), gate_bypass(), suppress_intent_push():
            if options["import_routers"]:
                read_bgp(management)
                self.stdout.write(json.dumps(router_inventory(management)))
            elif options["accept_routers"]:
                self._accept_routers(management, options["accept_routers"])
                self.stdout.write(json.dumps(router_inventory(management)))
            else:
                self._prepare_store(management, options["scope"])
                self.stdout.write("Store preparation acknowledged; no device Apply was enqueued")

    @staticmethod
    def _accept_routers(management, selected):
        from django.utils import timezone

        from netbox_nso_plugin.bgp_router_content import qualified_router
        from netbox_nso_plugin.models import NSOBGPRouterState
        from netbox_nso_plugin.ownership_grants import OwnershipGrant
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        if len(selected) != len(set(selected)):
            raise CommandError("Select each router overlay exactly once")
        rows = list(NSOBGPRouterState.objects.filter(management=management, pk__in=selected))
        if {row.pk for row in rows} != set(selected):
            raise CommandError("The exact selected router overlays do not belong to this device")
        stamp = timezone.now()
        candidates = []
        fields = ("status", "accepted_at")
        for row in rows:
            qualified_router(row)
            candidate = copy.copy(row)
            candidate.status = "accepted"
            candidate.accepted_at = candidate.accepted_at or stamp
            candidates.append(candidate)
        plan = RendererMutationPlan.build(
            grant=OwnershipGrant("accept"),
            saves=tuple(planned_save(row, update_fields=fields) for row in candidates),
            planned_at=stamp,
        )
        with renderer_writes(plan) as writer:
            for candidate in candidates:
                writer.save(candidate, update_fields=fields)

    @staticmethod
    def _prepare_store(management, scope):
        from netbox_nso_plugin.durable_capture import mapping_epoch
        from netbox_nso_plugin.models import NSOAcceptedSelection

        _page, incarnation = adapter_client.get_settlement_feed(
            management.adapter_device_id, after_settle_seq=0, limit=1
        )
        if (
            incarnation != management.adapter_incarnation
            or management.source_rekey_pending
            or management.reset_pending_incarnation
        ):
            raise CommandError("Import the current adapter store identity before store preparation")
        selector = Command._store_preparation(management, scope)
        _page, final_incarnation = adapter_client.get_settlement_feed(
            management.adapter_device_id,
            after_settle_seq=0,
            limit=1,
        )
        if incarnation != final_incarnation:
            raise CommandError("Adapter store changed during store preparation")
        selection = NSOAcceptedSelection.objects.get(
            management=management,
            mapping_epoch=mapping_epoch(management),
            stream=delivery.delivery_keys()[scope].section,
            **selector,
        )
        if selection.store_incarnation not in {None, incarnation}:
            raise CommandError("Preparation belongs to another adapter store")
        selection.store_incarnation = incarnation
        selection.save(update_fields=("store_incarnation",))

    @staticmethod
    def _store_preparation(management, scope):
        if scope is None:
            raise CommandError("Store preparation requires an explicit --scope")
        if scope in delivery.direct_keys():
            from netbox_nso_plugin.switching_preparation import prepare

            outcome, answer = prepare(management.device_id, scope, force=True, deadline_at=time.monotonic() + 60)
            if outcome != drain.SUCCEEDED or answer is None:
                raise CommandError("Direct store preparation was not acknowledged")
            return {"selection_revision": answer["selection_revision"]}
        claim = drain.claim(management.device_id, scope, mode=delivery.MODE_STORE_ONLY, force=True)
        if claim is None or claim.mode != delivery.MODE_STORE_ONLY or claim.deletions:
            raise CommandError("Could not form an exact store-only preparation")
        try:
            answer = drain.send_claim(claim)
            acknowledgement = drain.acknowledgement(claim, answer)
            if not acknowledgement.exact:
                raise CommandError(acknowledgement.reason)
            section = delivery.delivery_keys()[scope].section
            document = adapter_client.get_intent_receipts(device_id=management.adapter_device_id, section=section)
            receipt = _verification_receipt(
                claim,
                _receipt_for(
                    document,
                    adapter_device_id=management.adapter_device_id,
                    section=section,
                ),
            )
            if drain.resolve_restored_claim(management.device_id, scope, receipt) != drain.RESTORE_SETTLED:
                raise CommandError("Store preparation receipt did not settle its exact selection")
            return {"source_push_seq": claim.push_seq}
        except Exception as exc:
            drain.record_failure(claim, exc)
            if isinstance(exc, CommandError):
                raise
            raise CommandError(f"Store preparation failed: {exc}") from exc

    def _verify(self, options):
        from netbox_nso_plugin.deployment_checks import check_active_streams, check_contracts, check_settled_probes
        from netbox_nso_plugin.deployment_probe import locked_gate

        with locked_gate() as control:
            if control.verification_attempt_id is not None:
                raise CommandError("Revoke verification probe authority before resume")
            check_contracts()
            check_settled_probes(options["settled_probe"], options["device_id"], quiesced_at=control.quiesced_at)
            check_active_streams()
            blockers = drain.gate_blockers()
            if blockers:
                raise CommandError("Deployment gate changed before resume: " + "; ".join(blockers))
            try:
                resume()
            except BaseException:
                with suppress(Exception):
                    self.stderr.write(
                        "Deployment verification passed, but intent work may remain quiesced. "
                        "Fix the cause and re-run the full verification command: "
                        f"nso_intent_deployment_gate --verify --device {options['device_id']} "
                        + " ".join(f"--settled-probe {probe}" for probe in options["settled_probe"])
                        + ". nso_intent_deployment_gate --abort requires the same paired verification."
                    )
                raise
        self.stdout.write(self.style.SUCCESS("Deployment verification passed; normal intent operation resumed"))


def router_inventory(management):
    """Return router identity and full scope membership without ownership inference."""
    from netbox_nso_plugin.bgp_router_content import qualified_router, router_content
    from netbox_nso_plugin.models import NSOBGPRouterState

    return [
        {"id": row.pk, "content": router_content(qualified_router(row))}
        for row in NSOBGPRouterState.objects.filter(management=management).order_by("pk")
    ]
