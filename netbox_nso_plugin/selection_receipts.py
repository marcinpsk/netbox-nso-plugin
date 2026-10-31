# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Recover retained publication admission without replaying an intent write."""

import contextlib
import copy
from dataclasses import dataclass

from django.db import transaction

from . import delivery, drain
from . import durable_capture as capture
from .apply_settlement import EvidenceInvariantError, identify_generation_stall
from .models import NSOAcceptedSelection, NSOIntentOutboxState
from .refusal_contract import GENERATION_STATUSES
from .refusal_registry import RegistryError


def _require(condition):
    if not condition:
        raise EvidenceInvariantError("invalid_selection_receipt")


@dataclass(frozen=True)
class ReceiptRecovery:
    selection_id: int
    mapping_epoch: str
    store_incarnation: str
    request_digest: str
    claim: drain.Claim
    receipt: dict


def pending_selections(management, evidence, generations):
    """Find only unacknowledged protocol sources named by terminal evidence."""
    authorities = list(generations)
    for attempt in evidence["attempts"]:
        authorities.extend(attempt["generations"])
    pairs = {pair for generation in authorities for pair in _source_pairs(generation)}
    return [
        selection
        for selection in NSOAcceptedSelection.objects.filter(
            management=management,
            mapping_epoch=capture.mapping_epoch(management),
            acknowledged_at__isnull=True,
            source_push_seq__in={source for _stream, source in pairs},
        )
        if (selection.stream, selection.source_push_seq) in pairs
    ]


def _source_pairs(generation):
    with identify_generation_stall(generation):
        if (
            not isinstance(generation, dict)
            or not isinstance(generation.get("status"), str)
            or generation["status"] not in GENERATION_STATUSES
        ):
            raise EvidenceInvariantError("invalid_terminal_batch")
        if generation["status"] in {"pending", "running"}:
            return set()
        sources, revisions = generation.get("source_push_seq"), generation.get("stream_revisions")
        if (
            not isinstance(sources, dict)
            or not isinstance(revisions, dict)
            or not revisions.keys() <= sources.keys()
            or any(
                not isinstance(stream, str) or source is not None and (type(source) is not int or source <= 0)
                for stream, source in sources.items()
            )
        ):
            raise EvidenceInvariantError("invalid_terminal_batch")
        streams = {entry.section for entry in delivery.delivery_keys().values()}
        if any(
            not isinstance(stream, str) or stream not in streams or type(revision) is not int or revision <= 0
            for stream, revision in revisions.items()
        ):
            raise EvidenceInvariantError("invalid_terminal_batch")
        return {(stream, sources[stream]) for stream in revisions if sources[stream] is not None}


def _captured_claim(selection, management):
    scope = next(
        (
            key
            for key, entry in delivery.delivery_keys().items()
            if entry.in_protocol and entry.section == selection.stream
        ),
        None,
    )
    _require(scope is not None)
    state = NSOIntentOutboxState.objects.filter(device_id=management.device_id, scope=scope).first()
    _require(
        state is not None
        and state.push_seq == selection.source_push_seq
        and state.claim_revision == selection.plugin_revision
    )
    try:
        flags = drain.ClaimFlags.from_json(state.claim_flags)
        return drain.Claim(
            device_id=management.device_id,
            adapter_device_id=management.adapter_device_id,
            scope=scope,
            push_seq=state.push_seq,
            payload=copy.deepcopy(state.claim_payload),
            identity=state.claim_identity,
            revision=state.claim_revision,
            deletions=copy.deepcopy(state.claim_deletions or []),
            mark=state.claim_mark,
            mark_any=flags.mark_any,
            mode=flags.mode,
            marking_mode=flags.marking_mode,
            rendered=None,
        )
    except (TypeError, ValueError):
        raise EvidenceInvariantError("invalid_selection_receipt") from None


def prepare_recoveries(management, selections, document, incarnation):
    """Build immutable proposals; acknowledge only inside the terminal batch transaction."""
    receipts = {(row["device_id"], row["section"]): row for row in document["receipts"]}
    recoveries = {}
    for prior in selections:
        selection = NSOAcceptedSelection.objects.get(pk=prior.pk)
        if selection.acknowledged_at is not None:
            continue
        receipt = receipts.get((management.adapter_device_id, selection.stream))
        _require(receipt is not None)
        recovery = ReceiptRecovery(
            selection.pk,
            capture.mapping_epoch(management),
            incarnation,
            selection.request_digest,
            _captured_claim(selection, management),
            copy.deepcopy(receipt),
        )
        verify_recovery(management, selection, recovery, incarnation)
        recoveries[selection.pk] = recovery
    return recoveries


def verify_recovery(management, selection, recovery, incarnation):
    """Prove the original sequence, wire digest, mapping, modes and acknowledgment."""
    claim, receipt = recovery.claim, recovery.receipt
    _require(selection.pk == recovery.selection_id and selection.management_id == management.pk)
    _require(selection.mapping_epoch == recovery.mapping_epoch == capture.mapping_epoch(management))
    _require(recovery.store_incarnation == incarnation and selection.store_incarnation in (None, incarnation))
    _require(selection.binding_state != "invalidated")
    _require(receipt["device_id"] == claim.adapter_device_id == management.adapter_device_id)
    _require(
        claim.device_id == management.device_id
        and delivery.delivery_keys()[claim.scope].section == selection.stream == receipt["section"]
    )
    _require(
        selection.source_push_seq == claim.push_seq == receipt["push_seq"]
        and selection.plugin_revision == claim.revision
    )
    # The capture's wire digest names the original request, independent of the current renderer.
    _require(selection.request_digest == recovery.request_digest == receipt["request_digest"])
    modes = {
        "store_only": claim.mode == delivery.MODE_STORE_ONLY,
        "backfill_only": claim.mode == delivery.MODE_BACKFILL_ONLY,
        "delete_origin": bool(claim.mark) and claim.marking_mode == delivery.MARKING_QUERY_FLAG,
    }
    _require(all(receipt[key] == value for key, value in modes.items()))
    _require(200 <= receipt["status_code"] < 300 and drain.acknowledgement(claim, receipt["response"]).exact)
    try:
        validated = capture.validate_claim_capture(
            management,
            claim.scope,
            claim.push_seq,
            claim.payload,
            claim.revision,
            claim.deletions,
            claim.marking_mode,
            rendered=claim.rendered,
        )
        _require(validated.pk == selection.pk)
        capture._validate_snapshot_membership(selection)
    except RegistryError:
        raise EvidenceInvariantError("invalid_selection_receipt") from None


def recovery_footprint(recoveries):
    from .intent_state import MutationFootprint

    return MutationFootprint.for_keys((row.claim.device_id, row.claim.scope) for row in recoveries.values())


@contextlib.contextmanager
def lock_recovery_claims(recoveries):
    """Acquire outbox state locks before the combined management and owner footprint."""
    if not recoveries:
        yield {}
        return
    drain._refuse_in_transaction("receipt recovery")
    keys = sorted({(row.claim.device_id, row.claim.scope) for row in recoveries.values()})
    with transaction.atomic():
        states = {key: drain._lock_state(*key) for key in keys}
        yield states


def _same_claim(left, right):
    return all(
        getattr(left, name) == getattr(right, name)
        for name in (
            "device_id",
            "adapter_device_id",
            "scope",
            "push_seq",
            "payload",
            "identity",
            "revision",
            "deletions",
            "mark",
            "mark_any",
            "mode",
            "marking_mode",
        )
    )


def acknowledge_recoveries(management, recoveries, incarnation, states):
    """Acknowledge exact receipt facts inside the caller's combined owner transaction."""
    capture._require_transaction()
    if not recoveries:
        return
    management = type(management).objects.select_for_update().get(pk=management.pk)
    for selection_id, recovery in recoveries.items():
        selection = NSOAcceptedSelection.objects.select_for_update().get(pk=selection_id)
        verify_recovery(management, selection, recovery, incarnation)
        if selection.acknowledged_at is None:
            claim = recovery.claim
            state = states[(claim.device_id, claim.scope)]
            _require(_same_claim(_captured_claim(selection, management), claim))
            now = drain._db_now()
            response = recovery.receipt["response"]
            ack = drain.acknowledgement(claim, response)
            capture.acknowledge_claim_capture(claim, response, now)
            drain._commit_acknowledged_claim(state, claim, response, ack, now)
            _record_receipt_expectations(claim, response)


def _record_receipt_expectations(claim, response):
    if claim.scope != "static_route" or not isinstance(response, dict):
        return
    from .signals import _record_static_route_expectations

    generations = {row["route_id"]: row.get("generation") for row in claim.payload if row.get("route_id") is not None}
    _record_static_route_expectations(claim.device_id, generations, response.get("routes") or [])
