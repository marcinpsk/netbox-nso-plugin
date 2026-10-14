# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Capture emitted ownership and restore exact local Apply bookkeeping."""

import copy
import dataclasses
import json

from django.db import connection, transaction
from django.utils import timezone

from . import delivery
from .models import (
    NSOAcceptedSelection,
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSODeviceManagement,
    NSOFamilyReadState,
    NSOOverlaySettlementFence,
)
from .refusal_registry import OVERLAYS, RegistryError

_BOOKKEEPING = ("status", "last_apply_at", "last_apply_error", "apply_attempt_id")
_SECRET_FIELDS = frozenset(
    {
        "password",
        "auth_password",
        "priv_password",
        "authentication_key",
        "auth_key",
        "priv_key",
        "area_auth_key",
        "domain_auth_key",
    }
)


def mapping_epoch(management):
    """Hash the local mapping and the adapter store identity used by delivery."""
    from . import drain

    return delivery.canonical_fingerprint(
        (management.pk, management.device_id, management.adapter_source_epoch, drain.mapping_epoch(management))
    )


def _require_transaction():
    if not connection.in_atomic_block:
        raise RuntimeError("durable capture requires the caller's transaction")


def _wire(rendered, *, deletions=(), marking_mode=None, direct=None):
    from . import drain, switching_preparation

    if direct is None:
        wire = delivery.wire_body(rendered, rendered.payload, deletions=deletions, marking_mode=marking_mode)
    else:
        revision, deleted_roots = direct
        with switching_preparation._bound(tuple(deleted_roots), revision):
            wire = delivery.wire_body(rendered, rendered.payload)
    return drain.wire_digest(wire), json.loads(wire)


def _secret_values(value):
    """Find emitted credentials in raw and JSON-escaped text forms."""
    if isinstance(value, dict):
        return {
            secret
            for key, child in value.items()
            for secret in (
                {child, json.dumps(child, ensure_ascii=False)[1:-1]}
                if key in _SECRET_FIELDS and isinstance(child, str) and child
                else _secret_values(child)
            )
        }
    if isinstance(value, list | tuple):
        if len(value) == 2 and isinstance(value[0], str) and value[0] in _SECRET_FIELDS and isinstance(value[1], str):
            return {value[1], json.dumps(value[1], ensure_ascii=False)[1:-1]} if value[1] else set()
        return {secret for child in value for secret in _secret_values(child)}
    return set()


def _watermark(management, spec):
    return {
        row.family: {
            "attempt_id": row.applied_attempt_id,
            "incarnation": row.applied_incarnation,
            "source_epoch": row.applied_source_epoch,
            "payload_revision": row.applied_payload_revision,
            "publication_sequence": row.applied_publication_sequence,
        }
        for row in NSOFamilyReadState.objects.filter(management=management, family__in=spec.read_families)
    }


def _fence_revision(management, epoch, row):
    return (
        NSOOverlaySettlementFence.objects.filter(
            management=management, mapping_epoch=epoch, model_label=row._meta.label_lower, overlay_pk=row.pk
        )
        .values_list("bookkeeping_revision", flat=True)
        .first()
        or 0
    )


def _static_fences(row):
    if row._meta.label_lower != "netbox_nso_plugin.nsostaticroutestate":
        return None
    return {
        "route_id": row.static_route_id,
        "intent_generation": row.intent_generation,
        "expected_generation": row.expected_generation,
        "expected_fingerprint": row.expected_fingerprint,
    }


def _selector(manifest, selector):
    manifest = copy.deepcopy(manifest)
    for owner in manifest:
        for contributor in owner["contributors"]:
            contributor["source_selector"] = dict(selector)
    return manifest


def _redact_error(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
    elif isinstance(value, dict):
        value = {key: _redact_error(child, secrets) for key, child in value.items()}
    elif isinstance(value, list):
        value = [_redact_error(child, secrets) for child in value]
    return value


def _snapshot(attempt, selection, owner, row, revisions, *, promote=False, at=None, secrets=()):
    from .intent_state import canonical_fragment, mirror_refresh
    from .signals import suppress_intent_push

    error = json.dumps(row.last_apply_error, ensure_ascii=False)
    secrets = set(secrets) | _secret_values(canonical_fragment(row))
    if any(secret in error for secret in secrets):
        redacted = _redact_error(row.last_apply_error, sorted(secrets, key=len, reverse=True))
        with suppress_intent_push(), mirror_refresh(row, ("last_apply_error",)) as target:
            if target is None:
                raise RegistryError("captured overlay disappeared before error redaction")
            target.last_apply_error = redacted
            target.save(update_fields=("last_apply_error",))
        row.last_apply_error = redacted
        error = json.dumps(row.last_apply_error, ensure_ascii=False)
    if any(secret in error for secret in secrets):
        raise RegistryError("snapshot bookkeeping contains an emitted secret")
    spec = OVERLAYS[row._meta.label_lower]
    prior = {field: copy.deepcopy(getattr(row, field)) for field in _BOOKKEEPING}
    expected = dict(prior)
    if promote:
        expected.update(status="deploying", last_apply_at=at, last_apply_error="", apply_attempt_id=attempt.pk)
    epoch = selection.mapping_epoch
    streams = {item["stream"] for item in owner["contributors"]}
    scopes = {scope for scope, entry in delivery.delivery_keys().items() if entry.section in streams}
    return NSOApplyOverlaySnapshot.objects.create(
        attempt=attempt,
        selection=selection,
        model_label=row._meta.label_lower,
        overlay_pk=row.pk,
        mapping_epoch=epoch,
        **{f"prior_{field}": value for field, value in prior.items()},
        **{f"expected_{field}": value for field, value in expected.items()},
        accepted_at=row.accepted_at,
        scope_revisions={scope: revision for scope, revision in revisions.items() if scope in scopes},
        content_digest=owner["content_digest"],
        observation_watermark=_watermark(attempt.management, spec),
        expected_fence_revision=_fence_revision(attempt.management, epoch, row),
        contributors={"rows": copy.deepcopy(owner["contributors"])},
        static_route_fences=_static_fences(row),
    )


def _capture(management, rendered, revision, *, push_seq=None, deletions=(), marking_mode=None, direct=None):
    from .selection_manifest import frozen_manifest

    _require_transaction()
    scope = rendered.key[1]
    epoch = mapping_epoch(management)
    digest, wire = _wire(rendered, deletions=deletions, marking_mode=marking_mode, direct=direct)
    stream = delivery.delivery_keys()[scope].section
    if direct is not None:
        existing = (
            NSOAcceptedSelection.objects.filter(
                management=management,
                mapping_epoch=epoch,
                stream=stream,
                plugin_revision=revision,
                request_digest=digest,
                binding_state="unbound",
                publication_state="unresolved",
                selection_revision__isnull=True,
                acknowledged_at__isnull=True,
            )
            .order_by("pk")
            .first()
        )
        if existing is not None:
            _validate_snapshot_membership(existing)
            NSOAcceptedSelection.objects.filter(pk=existing.pk).update(publication_state="recorded")
            return existing
    manifest, rows = frozen_manifest(management, scope, rendered.payload)
    selector = {"source_push_seq": push_seq} if push_seq is not None else {}
    selection = NSOAcceptedSelection.objects.create(
        management=management,
        mapping_epoch=epoch,
        stream=stream,
        source_push_seq=push_seq,
        plugin_revision=revision,
        request_digest=digest,
        contributor_manifest=_selector(manifest, selector),
    )
    attempt = NSOApplyAttempt.objects.create(
        management=management,
        adapter_device_id=management.adapter_device_id,
        source_epoch=management.adapter_source_epoch,
        trigger_kind="push",
        scope_revisions={scope: revision},
        selected={stream: push_seq} if push_seq is not None else {},
    )
    secrets = _secret_values(wire)
    for owner in selection.contributor_manifest:
        key = (owner["model_label"], owner["overlay_pk"])
        _snapshot(attempt, selection, owner, rows[key], {scope: revision}, secrets=secrets)
    return selection


def capture_claim(management, rendered, push_seq, revision, mode, deletions, marking_mode):
    if mode not in (delivery.MODE_NORMAL, delivery.MODE_STORE_ONLY, delivery.MODE_BACKFILL_ONLY):
        raise RegistryError("unknown capture publication mode")
    return _capture(management, rendered, revision, push_seq=push_seq, deletions=deletions, marking_mode=marking_mode)


def capture_direct(management, rendered, revision, deleted_roots):
    return _capture(management, rendered, revision, direct=(revision, deleted_roots)).pk


def _claim_selection(management, scope, push_seq):
    try:
        return NSOAcceptedSelection.objects.get(
            management=management,
            mapping_epoch=mapping_epoch(management),
            stream=delivery.delivery_keys()[scope].section,
            source_push_seq=push_seq,
        )
    except NSOAcceptedSelection.DoesNotExist:
        raise RegistryError("claim has no original durable selection") from None


def _validate_snapshot_membership(selection):
    owners = {(owner["model_label"], owner["overlay_pk"]) for owner in selection.contributor_manifest}
    snapshots = list(selection.overlay_snapshots.filter(attempt__trigger_kind="push"))
    if len(owners) != len(selection.contributor_manifest) or len(snapshots) != len(owners):
        raise RegistryError("selection has incomplete durable ownership")
    if {(row.model_label, row.overlay_pk) for row in snapshots} != owners:
        raise RegistryError("selection snapshot owners differ from the frozen manifest")
    by_owner = {(row.model_label, row.overlay_pk): row for row in snapshots}
    for owner in selection.contributor_manifest:
        snapshot = by_owner[(owner["model_label"], owner["overlay_pk"])]
        if snapshot.content_digest != owner["content_digest"] or snapshot.contributors != {
            "rows": owner["contributors"]
        }:
            raise RegistryError("selection snapshot provenance changed")


def validate_claim_capture(management, scope, push_seq, payload, revision, deletions, marking_mode, *, rendered=None):
    selection = _claim_selection(management, scope, push_seq)
    if rendered is None:
        rendered = delivery.render(scope, management.device_id, management.adapter_device_id)
    rendered = dataclasses.replace(rendered, payload=payload)
    digest, _ = _wire(rendered, deletions=deletions, marking_mode=marking_mode)
    if selection.plugin_revision != revision or selection.request_digest != digest:
        raise RegistryError("claim differs from its original durable selection")
    _validate_snapshot_membership(selection)
    return selection


def acknowledge_claim_capture(claim, response, now):
    _require_transaction()
    management = NSODeviceManagement.objects.get(device_id=claim.device_id)
    selection = validate_claim_capture(
        management,
        claim.scope,
        claim.push_seq,
        claim.payload,
        claim.revision,
        claim.deletions,
        claim.marking_mode,
        rendered=claim.rendered,
    )
    NSOAcceptedSelection.objects.filter(pk=selection.pk).update(acknowledged_at=now, publication_state="sent")


def release_direct_capture(selection_id):
    """Let a later preparation reuse a capture whose send ended without an acknowledgement."""
    NSOAcceptedSelection.objects.filter(pk=selection_id, publication_state="recorded").update(
        publication_state="unresolved"
    )


def acknowledge_direct_capture(selection_id, answer):
    _require_transaction()
    revision = answer.get("selection_revision")
    if type(revision) is not int or revision <= 0:
        raise RegistryError("direct acknowledgement has no exact selection revision")
    selection = NSOAcceptedSelection.objects.select_for_update().get(pk=selection_id)
    if selection.selection_revision is not None and selection.selection_revision != revision:
        raise RegistryError("direct acknowledgement changed the selection identity")
    _validate_snapshot_membership(selection)
    manifest = _selector(selection.contributor_manifest, {"selection_revision": revision})
    selection.selection_revision = revision
    selection.contributor_manifest = manifest
    selection.acknowledged_at = timezone.now()
    selection.publication_state = "sent"
    selection.save(update_fields=("selection_revision", "contributor_manifest", "acknowledged_at", "publication_state"))
    for owner in manifest:
        selection.overlay_snapshots.filter(model_label=owner["model_label"], overlay_pk=owner["overlay_pk"]).update(
            contributors={"rows": owner["contributors"]}
        )


def capture_manual(
    attempt, management, registry, pushed, direct_selected, direct_source_revisions, static_route_stored
):
    from .apply_state import IntentChangedDuringPreparation
    from .selection_manifest import current_content_digest, lock_manifest_rows

    _require_transaction()
    locked_rows = lock_manifest_rows(management, registry)
    owners = {}
    for scope, entry in registry.items():
        if entry.in_protocol:
            selection = _claim_selection(management, scope, pushed[scope].push_seq)
            revision = pushed[scope].revision
        else:
            try:
                selection = NSOAcceptedSelection.objects.get(
                    management=management,
                    mapping_epoch=mapping_epoch(management),
                    stream=entry.section,
                    selection_revision=direct_selected[entry.section],
                )
            except NSOAcceptedSelection.DoesNotExist:
                raise IntentChangedDuringPreparation from None
            revision = direct_source_revisions[scope]
        if selection.plugin_revision != revision or selection.acknowledged_at is None:
            raise IntentChangedDuringPreparation
        _validate_snapshot_membership(selection)
        for owner in selection.contributor_manifest:
            key = (owner["model_label"], owner["overlay_pk"])
            row = locked_rows.get(key)
            if row is None or current_content_digest(row) != owner["content_digest"]:
                raise IntentChangedDuringPreparation
            if key in owners:
                owners[key][1]["contributors"].extend(copy.deepcopy(owner["contributors"]))
            else:
                owners[key] = (selection, copy.deepcopy(owner), row)
    at = timezone.now()
    for selection, owner, row in owners.values():
        promote = row.status in ("accepted", "apply_failed")
        if row._meta.label_lower == "netbox_nso_plugin.nsostaticroutestate" and not static_route_stored:
            promote = False
        _snapshot(attempt, selection, owner, row, attempt.scope_revisions, promote=promote, at=at)


def _locked_attempt(attempt_id):
    from .apply_state import _enter_level, lock_device_intent_transaction

    attempt = NSOApplyAttempt.objects.select_related("management").get(pk=attempt_id)
    device_id = attempt.management.device_id
    lock_device_intent_transaction(device_id)
    _enter_level(5, attempt.management_id)
    management = NSODeviceManagement.objects.select_for_update(of=("self",)).get(pk=attempt.management_id)
    if management.device_id != device_id:
        raise RegistryError("attempt mapping changed while acquiring its locks")
    attempt.management = management
    return attempt, management


def _snapshot_rows(management, snapshots):
    from .apply_state import _enter_level
    from .intent_state import OVERLAY_MODEL_RANKS

    rows = {}
    for rank, label in enumerate(OVERLAY_MODEL_RANKS):
        wanted = {item.overlay_pk for item in snapshots if item.model_label == label}
        if not wanted:
            continue
        spec = OVERLAYS[label]
        for pk in sorted(wanted):
            _enter_level(8, (rank, pk))
        targets = (
            spec.model.objects.select_for_update(of=("self",))
            .filter(pk__in=wanted, **{spec.device_lookup: management.device_id})
            .order_by("pk")
        )
        rows.update(((label, row.pk), row) for row in targets)
    return rows


def _matches(snapshot, row, management, revisions, prefix, *, check_watermark=True):
    from .selection_manifest import current_content_digest

    if row is None or snapshot.mapping_epoch != mapping_epoch(management):
        return False
    if any(revisions.get(scope) != revision for scope, revision in snapshot.scope_revisions.items()):
        return False
    return (
        all(getattr(row, field) == getattr(snapshot, f"{prefix}_{field}") for field in _BOOKKEEPING)
        and row.accepted_at == snapshot.accepted_at
        and current_content_digest(row) == snapshot.content_digest
        and (
            not check_watermark
            or _watermark(management, OVERLAYS[snapshot.model_label]) == snapshot.observation_watermark
        )
        and _fence_revision(management, snapshot.mapping_epoch, row) == snapshot.expected_fence_revision
        and _static_fences(row) == snapshot.static_route_fences
    )


def _write_bookkeeping(row, snapshot, prefix):
    from .intent_state import mirror_refresh
    from .signals import suppress_intent_push

    with suppress_intent_push(), mirror_refresh(row, _BOOKKEEPING) as target:
        if target is None:
            raise RegistryError("captured overlay disappeared before bookkeeping")
        for field in _BOOKKEEPING:
            setattr(target, field, copy.deepcopy(getattr(snapshot, f"{prefix}_{field}")))
        target.save(update_fields=_BOOKKEEPING)


def promote_snapshots(attempt):
    from .apply_state import IntentChangedDuringPreparation, lock_intent_revisions, lock_order_scope

    if connection.in_atomic_block:
        raise RuntimeError("manual snapshots must commit before promotion")
    with transaction.atomic(), lock_order_scope():
        locked, management = _locked_attempt(attempt.pk)
        if locked.trigger_kind != "manual" or locked.adapter_apply_attempt_id is not None:
            raise IntentChangedDuringPreparation
        if (
            locked.adapter_device_id != management.adapter_device_id
            or locked.source_epoch != management.adapter_source_epoch
        ):
            raise IntentChangedDuringPreparation
        revisions = lock_intent_revisions(management.device_id, locked.scope_revisions)
        if revisions != locked.scope_revisions or management.source_rekey_pending:
            raise IntentChangedDuringPreparation
        epoch = mapping_epoch(management)
        entries = {entry.section: entry for entry in delivery.delivery_keys().values()}
        for stream, selector in locked.selected.items():
            entry = entries.get(stream)
            if entry is None:
                raise IntentChangedDuringPreparation
            selector_field = "source_push_seq" if entry.in_protocol else "selection_revision"
            if not NSOAcceptedSelection.objects.filter(
                management=management,
                mapping_epoch=epoch,
                stream=stream,
                plugin_revision=locked.scope_revisions[entry.key],
                acknowledged_at__isnull=False,
                **{selector_field: selector},
            ).exists():
                raise IntentChangedDuringPreparation
        snapshots = list(locked.overlay_snapshots.order_by("model_label", "overlay_pk"))
        rows = _snapshot_rows(management, snapshots)
        if any(
            not _matches(item, rows.get((item.model_label, item.overlay_pk)), management, revisions, "prior")
            for item in snapshots
        ):
            raise IntentChangedDuringPreparation
        for item in snapshots:
            if any(getattr(item, f"prior_{field}") != getattr(item, f"expected_{field}") for field in _BOOKKEEPING):
                _write_bookkeeping(rows[(item.model_label, item.overlay_pk)], item, "expected")
        locked.adapter_apply_attempt_id = locked.pk
        locked.save(update_fields=("adapter_apply_attempt_id",))
        attempt.adapter_apply_attempt_id = locked.pk


def restore_attempt(attempt_id, keep_streams=()):
    from .apply_state import lock_intent_revisions, lock_order_scope

    with transaction.atomic(), lock_order_scope():
        attempt, management = _locked_attempt(attempt_id)
        if attempt.trigger_kind != "manual" or attempt.adapter_apply_attempt_id != attempt.pk:
            raise RegistryError("restoration requires a promoted manual attempt")
        revisions = lock_intent_revisions(management.device_id, attempt.scope_revisions)
        snapshots = list(attempt.overlay_snapshots.order_by("model_label", "overlay_pk"))
        rows = _snapshot_rows(management, snapshots)
        for item in snapshots:
            if item.settlement_disposition != "unresolved":
                continue
            contributors = item.contributors.get("rows", [])
            if not contributors:
                raise RegistryError("manual snapshot has no original contributors")
            if any(contributor["stream"] in keep_streams for contributor in contributors):
                continue
            row = rows.get((item.model_label, item.overlay_pk))
            if _matches(item, row, management, revisions, "expected", check_watermark=False):
                if any(getattr(item, f"prior_{field}") != getattr(item, f"expected_{field}") for field in _BOOKKEEPING):
                    _write_bookkeeping(row, item, "prior")
                item.settlement_disposition = "not_applied"
            else:
                item.settlement_disposition = "superseded"
            item.save(update_fields=("settlement_disposition",))
