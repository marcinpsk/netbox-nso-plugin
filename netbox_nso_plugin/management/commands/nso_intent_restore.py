# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Reconcile a restored plugin database with the adapter's durable receipts."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from netbox_nso_plugin import adapter_client, delivery, drain, outbox
from netbox_nso_plugin.deployment import gate_bypass, quiesce, resume
from netbox_nso_plugin.restore import advance_static_route_pk


def _document(document):
    """Validate the required top-level adapter restore surface without defaults."""
    if not isinstance(document, dict):
        raise CommandError("Adapter receipt response is not an object")
    missing = {"receipts", "global_max_push_seq", "global_max_route_id"} - document.keys()
    if missing:
        raise CommandError("Adapter receipt response is missing: " + ", ".join(sorted(missing)))
    if not isinstance(document["receipts"], list):
        raise CommandError("Adapter receipt response has no receipts list")
    return document


def _watermark(value, name):
    """Validate one nullable positive integer watermark."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CommandError(f"Adapter receipt response has an invalid {name}")
    return value


def _key_receipt(document, *, adapter_device_id, section):
    """Return this filtered response's only matching receipt, or ``None``."""
    matches = [
        row
        for row in document["receipts"]
        if isinstance(row, dict) and row.get("device_id") == adapter_device_id and row.get("section") == section
    ]
    if len(matches) > 1:
        raise CommandError(f"Adapter returned duplicate receipts for {adapter_device_id}/{section}")
    return matches[0] if matches else None


def _normalize(receipt):
    """Translate the adapter wire vocabulary into the landed restore resolver's input."""
    if receipt is None:
        return None
    required = {"push_seq", "request_digest", "response", "store_only", "delete_origin", "backfill_only"}
    missing = required - receipt.keys()
    if missing:
        raise CommandError("Adapter key receipt is missing: " + ", ".join(sorted(missing)))
    mode_fields = ("store_only", "delete_origin", "backfill_only")
    invalid_modes = [name for name in mode_fields if type(receipt[name]) is not bool]
    if invalid_modes:
        raise CommandError("Adapter key receipt has invalid boolean modes: " + ", ".join(invalid_modes))
    return {
        "accepted_push_seq": receipt["push_seq"],
        "request_digest": receipt["request_digest"],
        "stored_response": receipt["response"],
        "store_only": receipt["store_only"],
        "delete_origin": receipt["delete_origin"],
        "backfill_only": receipt["backfill_only"],
    }


def _claim_modes(state, entry):
    """Return the receipt-mode booleans the restored claim's own send carried."""
    mode = (state.claim_flags or {}).get("mode", delivery.MODE_NORMAL)
    return {
        "store_only": mode == delivery.MODE_STORE_ONLY,
        "backfill_only": mode == delivery.MODE_BACKFILL_ONLY,
        "delete_origin": bool(state.claim_mark) and entry.marking_mode == delivery.MARKING_QUERY_FLAG,
    }


class Command(BaseCommand):
    help = "Resolve a plugin-only database restore against the adapter's durable intent receipts."

    def handle(self, *args, **options):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentOutboxState

        created = quiesce()
        with gate_bypass():
            global_document = _document(adapter_client.get_intent_receipts())
            max_push_seq = _watermark(global_document["global_max_push_seq"], "global_max_push_seq")
            max_route_id = _watermark(global_document["global_max_route_id"], "global_max_route_id")
            if max_push_seq is not None:
                outbox.advance_push_seq(max_push_seq)
            if max_route_id is not None:
                advance_static_route_pk(max_route_id)
            drain.clear_acknowledged_lineage()

            states = list(NSOIntentOutboxState.objects.filter(push_seq__isnull=False).order_by("device_id", "scope"))
            for state in states:
                key_name = f"{state.device_id}/{state.scope}"
                entry = delivery.delivery_keys().get(state.scope)
                if entry is None or not entry.in_protocol:
                    raise CommandError(f"Restore failed closed for {key_name}: unknown receipt section")
                adapter_device_id = (
                    NSODeviceManagement.objects.filter(
                        device_id=state.device_id,
                        adapter_device_id__isnull=False,
                    )
                    .values_list("adapter_device_id", flat=True)
                    .first()
                )
                if adapter_device_id is None:
                    raise CommandError(f"Restore failed closed for {key_name}: no adapter device id")
                key_document = _document(
                    adapter_client.get_intent_receipts(
                        device_id=adapter_device_id,
                        section=entry.section,
                    )
                )
                receipt = _normalize(
                    _key_receipt(
                        key_document,
                        adapter_device_id=adapter_device_id,
                        section=entry.section,
                    )
                )
                if receipt is not None and receipt["accepted_push_seq"] == state.push_seq:
                    # Mode is part of the receipt identity (seq + digest + three booleans):
                    # a same-sequence receipt in another mode is NOT this claim's outcome.
                    wanted = _claim_modes(state, entry)
                    served = {name: receipt[name] for name in wanted}
                    if served != wanted:
                        raise CommandError(
                            f"Restore failed closed for {key_name}: the receipt's mode {served} "
                            f"is not the claim's {wanted}"
                        )
                verdict = drain.resolve_restored_claim(state.device_id, state.scope, receipt)
                if verdict == drain.RESTORE_FAILED_CLOSED:
                    raise CommandError(f"Restore failed closed for {key_name}")
                if verdict == drain.RESTORE_REPLAY:
                    drain.release_restored_replay(state.device_id, state.scope)
                self.stdout.write(f"{key_name}: {verdict}")
        if created:
            resume()
        self.stdout.write(self.style.SUCCESS(f"Restore resolved {len(states)} outstanding claim(s)"))
