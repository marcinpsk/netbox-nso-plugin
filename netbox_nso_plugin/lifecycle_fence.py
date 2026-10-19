# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Serialize owner bookkeeping before ranked overlay locks.

Device, management, source and intent revision locks precede these locks. All
owner fences are locked in overlay rank and primary-key order before overlays.
Family read-state locks follow the overlay footprint.
"""

import contextlib
import contextvars
from dataclasses import dataclass, field

from django.apps import apps
from django.db import transaction

BOOKKEEPING_FIELDS = frozenset({"status", "accepted_at", "last_apply_at", "last_apply_error", "apply_attempt_id"})
_UNSET = object()
_AUTHORITATIVE_BOOKKEEPING = contextvars.ContextVar("nso_authoritative_bookkeeping", default=False)


def owner_rows(footprint):
    """Include unchanged siblings so the final owner projection is authoritative."""
    from .intent_state import SourceRow
    from .refusal_registry import OVERLAYS

    rows = set(footprint.overlay_rows)
    for label, spec in OVERLAYS.items():
        device_ids = {device_id for device_id, scope in footprint.revision_keys if scope in spec.delivery_keys}
        if device_ids:
            lookup = {f"{spec.device_lookup}__in": device_ids}
            rows.update(SourceRow(label, pk) for pk in spec.model.objects.filter(**lookup).values_list("pk", flat=True))
    return tuple(rows)


def _management(row):
    from .models import NSODeviceManagement
    from .refusal_registry import OVERLAYS

    if hasattr(row, "management_id"):
        return NSODeviceManagement.objects.get(pk=row.management_id)
    lookup = OVERLAYS[row._meta.label_lower].device_lookup
    device_id = type(row).objects.filter(pk=row.pk).values_list(lookup, flat=True).get()
    return NSODeviceManagement.objects.filter(device_id=device_id).first()


def applied_watermark(management, spec):
    """Use payload identity rather than admission identity to order observations."""
    from .models import NSOFamilyObservation, NSOFamilyReadState

    states = list(NSOFamilyReadState.objects.filter(management=management, family__in=spec.read_families))
    digests = dict(NSOFamilyObservation.objects.filter(read_state__in=states).values_list("read_state_id", "digest"))
    return {
        state.family: {
            "incarnation": state.applied_incarnation,
            "source_epoch": state.applied_source_epoch,
            "payload_revision": state.applied_payload_revision,
            "digest": digests.get(state.pk),
        }
        for state in states
        if state.applied_payload_revision is not None
    }


def fence_for(row, *, lock=False):
    """Return this owner's current mapping fence inside its mutation transaction."""
    from .durable_capture import mapping_epoch
    from .intent_state import _ACTIVE_PERMIT, SourceRow
    from .models import NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS

    permit = _ACTIVE_PERMIT.get()
    ref = SourceRow(row._meta.label_lower, row.pk)
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("owner fence requires transaction.atomic()")
    cached = permit.owner_fences.get(ref) if permit is not None else None
    marker = getattr(row, "_nso_locked_owner", None)
    if cached is None and marker is not None and marker[0] == tuple(transaction.get_connection().atomic_blocks):
        cached = marker[1]
    if cached is not None:
        same_management = (
            row.management_id == cached.management_id
            if hasattr(row, "management_id")
            else row.interface.device_id == cached.management.device_id
        )
        if same_management:
            return cached
    management = _management(row)
    if management is None:
        return None
    fence, _ = NSOOverlaySettlementFence.objects.get_or_create(
        management=management,
        mapping_epoch=mapping_epoch(management),
        model_label=row._meta.label_lower,
        overlay_pk=row.pk,
        defaults={"observation_watermark": applied_watermark(management, OVERLAYS[row._meta.label_lower])},
    )
    fence = NSOOverlaySettlementFence.objects.select_for_update().get(pk=fence.pk) if lock else fence
    fence.management = management
    if permit is not None:
        permit.owner_fences[ref] = fence
    return fence


def lock_owner_fences(rows, *, managements=()):
    """Create and lock fences by model rank and owner key before ranked overlays."""
    from .durable_capture import mapping_epoch
    from .intent_state import _ACTIVE_PERMIT, OVERLAY_MODEL_RANKS, SourceRow
    from .models import NSODeviceManagement, NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS

    ranks = {label: rank for rank, label in enumerate(OVERLAY_MODEL_RANKS)}
    refs = sorted(
        {ref for ref in rows if ref.pk is not None and ref.model_label in OVERLAYS},
        key=lambda ref: (ranks[ref.model_label], ref.pk),
    )
    owners = {}
    device_ids = {}
    for label in {ref.model_label for ref in refs}:
        spec = OVERLAYS[label]
        related = spec.device_lookup.rsplit("__", 1)[0]
        queryset = spec.model.objects.filter(pk__in=[ref.pk for ref in refs if ref.model_label == label])
        for row in queryset.select_related(related):
            ref = SourceRow(label, row.pk)
            owners[ref] = row
            value = row
            for field_name in spec.device_lookup.split("__"):
                value = getattr(value, field_name)
            device_ids[ref] = value
    permit = _ACTIVE_PERMIT.get()
    scoped_devices = set(device_ids.values())
    if set(refs) - owners.keys() and permit is not None:
        scoped_devices.update(permit.footprint.device_ids)
    supplied = {management.device_id: management for management in managements}
    managements = {device_id: management for device_id, management in supplied.items() if device_id in scoped_devices}
    managements.update(
        {
            management.device_id: management
            for management in NSODeviceManagement.objects.filter(device_id__in=scoped_devices - supplied.keys())
        }
    )
    managements_by_pk = {management.pk: management for management in managements.values()}
    epochs = {management.pk: mapping_epoch(management) for management in managements.values()}
    existing = {
        (fence.management_id, fence.mapping_epoch, fence.model_label, fence.overlay_pk): fence
        for fence in NSOOverlaySettlementFence.objects.filter(
            management_id__in=epochs, mapping_epoch__in=epochs.values()
        )
    }
    wanted = {}
    missing = []
    watermarks = {}
    for ref, row in owners.items():
        management = managements.get(device_ids[ref])
        if management is None:
            continue
        key = (management.pk, epochs[management.pk], ref.model_label, ref.pk)
        fence = existing.get(key)
        if fence is None:
            watermark_key = (management.pk, ref.model_label)
            if watermark_key not in watermarks:
                watermarks[watermark_key] = applied_watermark(management, OVERLAYS[ref.model_label])
            fence = NSOOverlaySettlementFence(
                management=management,
                mapping_epoch=epochs[management.pk],
                model_label=ref.model_label,
                overlay_pk=ref.pk,
                observation_watermark=watermarks[watermark_key],
            )
            missing.append(fence)
        wanted[ref] = fence
    NSOOverlaySettlementFence.objects.bulk_create(missing)
    candidates_by_label = {}
    for ref in refs:
        candidates = (
            [wanted[ref]]
            if ref in wanted
            else sorted(
                (
                    fence
                    for fence in existing.values()
                    if ref not in owners
                    and fence.model_label == ref.model_label
                    and fence.overlay_pk == ref.pk
                    and fence.mapping_epoch == epochs[fence.management_id]
                ),
                key=lambda fence: fence.management_id,
            )
        )
        for candidate in candidates:
            candidates_by_label.setdefault(ref.model_label, {})[candidate.pk] = ref
    locked = {}
    for label in sorted(candidates_by_label, key=ranks.__getitem__):
        candidates = candidates_by_label[label]
        for fence in (
            NSOOverlaySettlementFence.objects.select_for_update()
            .filter(pk__in=candidates)
            .order_by("overlay_pk", "management_id")
        ):
            ref = candidates[fence.pk]
            fence.management = managements_by_pk[fence.management_id]
            fence._owner_row = owners.get(ref)
            locked[ref] = fence
            if permit is not None:
                permit.owner_fences[ref] = fence
    return locked


def advance_fence(
    row,
    *,
    owner_successor=False,
    current_attempt=_UNSET,
    last_execution=_UNSET,
    observation_watermark=_UNSET,
    last_settle_seq=_UNSET,
):
    """Advance an already locked owner and atomically retain settlement identity."""
    fence = fence_for(row)
    if fence is None:
        return None
    fence.bookkeeping_revision += 1
    fence.accepted_at = getattr(row, "accepted_at", None)
    fence.content_digest = owner_content_digest(row)
    fields = ["bookkeeping_revision", "accepted_at", "content_digest"]
    if owner_successor:
        fence.owner_revision += 1
        fields.append("owner_revision")
    for name, value in (
        ("current_attempt", current_attempt),
        ("last_execution", last_execution),
        ("observation_watermark", observation_watermark),
        ("last_settle_seq", last_settle_seq),
    ):
        if value is not _UNSET:
            setattr(fence, name, value)
            fields.append(name)
    fence.save(update_fields=fields)
    return fence


def owner_content_digest(row):
    """Fence invalid policy bindings without treating them as deliverable content."""
    from .delivery import canonical_fingerprint
    from .selection_manifest import current_content_digest
    from .signals import RoutePolicyBindingInvalid

    try:
        return current_content_digest(row)
    except RoutePolicyBindingInvalid as exc:
        return canonical_fingerprint(("invalid_route_policy_binding", str(exc)))


def deleted_owner_digest(model_label, overlay_pk):
    """Hash an absent owner separately from a living non-rendered contribution."""
    from .delivery import canonical_fingerprint

    return canonical_fingerprint(("deleted-owner", model_label, overlay_pk))


def owner_deletion_proven(management, snapshot):
    """Read exact deletion evidence after the mutation footprint locks owner fences."""
    from .durable_capture import mapping_epoch
    from .models import NSOOverlaySettlementFence

    if snapshot.mapping_epoch != mapping_epoch(management):
        return False
    return NSOOverlaySettlementFence.objects.filter(
        management=management,
        mapping_epoch=snapshot.mapping_epoch,
        model_label=snapshot.model_label,
        overlay_pk=snapshot.overlay_pk,
        bookkeeping_revision__gt=snapshot.expected_fence_revision,
        content_digest=deleted_owner_digest(snapshot.model_label, snapshot.overlay_pk),
        current_attempt__isnull=True,
    ).exists()


def owner_projection(row):
    from .delivery import canonical_fingerprint
    from .intent_state import ABSENT, canonical_fragment
    from .ownership_planner import manifest_binding
    from .status_machine import is_owned

    content_digest = owner_content_digest(row)
    fields = BOOKKEEPING_FIELDS
    if row._meta.model_name == "nsostaticroutestate":
        fragment = canonical_fragment(row)
        if fragment != ABSENT:
            content_digest = canonical_fingerprint(
                tuple((name, value) for name, value in fragment if name != "generation")
            )
        # A new correlation generation fences old outcomes without changing device content.
        fields = fields | {"intent_generation"}
    binding = manifest_binding(row) if is_owned(row.status) else None
    identity = None
    if binding is not None:
        _rule, _scope, device_id, native_label, native_pk, native_key, state_label, state_key = binding
        identity = (device_id, native_label, native_pk, native_key, state_label, state_key)
    return (
        (content_digest, identity),
        tuple((name, getattr(row, name)) for name in sorted(fields) if hasattr(row, name)),
    )


def capture_locked_owners(fences):
    return {
        ref: (fence, fence.bookkeeping_revision, owner_projection(row))
        for ref, fence in fences.items()
        if (row := getattr(fence, "_owner_row", None)) is not None
    }


def finish_locked_owners(before, *, repend=False, deleted_rows=()):
    """Re-pend only changed contributions; scope revisions do not change siblings."""
    from .intent_state import IntentMutationProtocolError, locked_mirror_refresh
    from .signals import suppress_intent_push
    from .status_machine import OWNED_STATES

    deleted_managements = {ref.pk for ref in deleted_rows if ref.model_label == "netbox_nso_plugin.nsodevicemanagement"}
    if deleted_managements:
        from .models import NSODeviceManagement

        deleted_managements.difference_update(
            NSODeviceManagement.objects.filter(pk__in=deleted_managements).values_list("pk", flat=True)
        )
    current = {
        (label, row.pk): row
        for label in {ref.model_label for ref in before}
        for row in apps.get_model(label).objects.filter(pk__in=[ref.pk for ref in before if ref.model_label == label])
    }
    unselected = set()
    for ref, (fence, _revision, projection) in before.items():
        if ref not in deleted_rows or (ref.model_label, ref.pk) in current:
            continue
        prior = dict(projection[1])
        if (
            prior.get("status") not in OWNED_STATES
            and prior.get("accepted_at") is None
            and prior.get("apply_attempt_id") is None
            and fence.owner_revision == 0
            and fence.accepted_at is None
            and fence.current_attempt_id is None
            and fence.last_execution_id is None
        ):
            unselected.add(ref)
    if unselected:
        from .models import NSOApplyOverlaySnapshot

        retained = set(
            NSOApplyOverlaySnapshot.objects.filter(
                model_label__in={ref.model_label for ref in unselected}, overlay_pk__in={ref.pk for ref in unselected}
            ).values_list("model_label", "overlay_pk")
        )
        unselected = {ref for ref in unselected if (ref.model_label, ref.pk) not in retained}
    for ref, (fence, revision, projection) in before.items():
        row = current.get((ref.model_label, ref.pk))
        if row is None:
            if ref not in deleted_rows:
                raise IntentMutationProtocolError(
                    f"{ref.model_label} row {ref.pk!r} vanished before owner finalization"
                )
            if fence.management_id in deleted_managements or ref in unselected:
                continue
            fence.bookkeeping_revision += 1
            fence.owner_revision += 1
            fence.content_digest = deleted_owner_digest(ref.model_label, ref.pk)
            fence.current_attempt = None
            fence.save(update_fields=("bookkeeping_revision", "owner_revision", "content_digest", "current_attempt"))
            continue
        after = owner_projection(row)
        if projection == after:
            continue
        if (
            repend
            and dict(projection[1]).get("status") in OWNED_STATES
            and projection[0] != after[0]
            and row.status in OWNED_STATES
        ):
            with suppress_intent_push(), locked_mirror_refresh(row, {"status", "apply_attempt_id"}):
                row.status = "accepted"
                row.apply_attempt_id = None
                row.save(update_fields={"status", "apply_attempt_id"})
        fence = fence_for(row)
        if fence.bookkeeping_revision == revision:
            successor = projection[0] != after[0] or dict(projection[1]).get("accepted_at") != getattr(
                row, "accepted_at", None
            )
            if dict(projection[1]).get("intent_generation") != getattr(row, "intent_generation", None):
                successor = True
            if (
                _READ_REFRESH.get() is None
                and not _AUTHORITATIVE_BOOKKEEPING.get()
                and row.status == "accepted"
                and dict(projection[1]).get("status") != "accepted"
            ):
                successor = True
            advance_fence(row, owner_successor=successor, current_attempt=None if successor else _UNSET)


def bind_locked_owner(row, fence):
    """Bind a row returned by SELECT FOR UPDATE to its prior owner fence lock."""
    connection = transaction.get_connection()
    if not connection.in_atomic_block:
        raise RuntimeError("locked owner binding requires transaction.atomic()")
    row._nso_locked_owner = (tuple(connection.atomic_blocks), fence)


@contextlib.contextmanager
def locked_owner_bookkeeping(row):
    """Normalize bookkeeping under capture locks before its snapshot freezes."""
    from .intent_state import _ACTIVE_PERMIT, IntentMutationProtocolError, MutationFootprint, SourceRow, _Permit

    marker = getattr(row, "_nso_locked_owner", None)
    connection = transaction.get_connection()
    if marker is None or marker[0] != tuple(connection.atomic_blocks):
        raise IntentMutationProtocolError("owner bookkeeping requires its current capture row and fence locks")
    fence = marker[1]
    ref = SourceRow(row._meta.label_lower, row.pk)
    active = _ACTIVE_PERMIT.get()
    permit = active or _Permit(
        footprint=MutationFootprint(overlay_rows=(ref,)),
        dml_kind="locked_bookkeeping",
        owner_fences={ref: fence},
    )
    token = _ACTIVE_PERMIT.set(permit)
    before = {ref: (fence, fence.bookkeeping_revision, owner_projection(row))}
    try:
        with authoritative_bookkeeping():
            yield
        finish_locked_owners(before)
    finally:
        _ACTIVE_PERMIT.reset(token)


@dataclass
class _ReadRefresh:
    tokens: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)


_READ_REFRESH = contextvars.ContextVar("nso_lifecycle_read_refresh", default=None)


@contextlib.contextmanager
def authoritative_bookkeeping():
    """Let certified settlement and exact rollback write their own bookkeeping."""
    token = _READ_REFRESH.set(None)
    authority_token = _AUTHORITATIVE_BOOKKEEPING.set(True)
    try:
        yield
    finally:
        _AUTHORITATIVE_BOOKKEEPING.reset(authority_token)
        _READ_REFRESH.reset(token)


@contextlib.contextmanager
def read_refresh():
    """Bind planning and publication to the same owner bookkeeping baseline."""
    if _READ_REFRESH.get() is not None:
        yield
        return
    token = _READ_REFRESH.set(_ReadRefresh())
    try:
        yield
    finally:
        _READ_REFRESH.reset(token)


def capture_read_tokens(rows):
    from .durable_capture import mapping_epoch
    from .models import NSODeviceManagement, NSOOverlaySettlementFence
    from .refusal_registry import OVERLAYS

    read = _READ_REFRESH.get()
    if read is None:
        return
    refs = {
        ref
        for ref in rows
        if ref.pk is not None and ref.model_label in OVERLAYS and (ref.model_label, ref.pk) not in read.tokens
    }
    if not refs:
        return
    devices = {
        (label, pk): device_id
        for label in {ref.model_label for ref in refs}
        for pk, device_id in OVERLAYS[label]
        .model.objects.filter(pk__in=[ref.pk for ref in refs if ref.model_label == label])
        .values_list("pk", OVERLAYS[label].device_lookup)
    }
    managements = {
        management.device_id: management
        for management in NSODeviceManagement.objects.filter(device_id__in=set(devices.values()))
    }
    epochs = {management.pk: mapping_epoch(management) for management in managements.values()}
    revisions = {
        (fence.management_id, fence.mapping_epoch, fence.model_label, fence.overlay_pk): fence.bookkeeping_revision
        for fence in NSOOverlaySettlementFence.objects.filter(
            management_id__in=epochs, mapping_epoch__in=epochs.values()
        )
    }
    for key, device_id in devices.items():
        management = managements.get(device_id)
        if management is not None:
            epoch = epochs[management.pk]
            read.tokens[key] = (epoch, revisions.get((management.pk, epoch, *key), 0))


def bind_read_observation(family, identity):
    """Expose the validated publication identity while the read plans its writes."""
    read = _READ_REFRESH.get()
    if read is not None:
        read.observations[family] = identity


def protected_read_fields(row):
    """Refresh facts without certifying Apply or erasing pinned causes."""
    from .refusal_registry import OVERLAYS

    read = _READ_REFRESH.get()
    if read is None or row._meta.label_lower not in OVERLAYS:
        return frozenset()
    fence = fence_for(row)
    identity = (fence.mapping_epoch, fence.bookkeeping_revision) if fence is not None else None
    stale = read.tokens.get((row._meta.label_lower, row.pk), identity) != identity
    if stale:
        return BOOKKEEPING_FIELDS
    if fence is not None and fence.current_attempt_id is not None:
        fresh_success_observation = getattr(row, "status", None) == "in_sync" and any(
            family in read.observations and read.observations[family] != fence.observation_watermark.get(family)
            for family in OVERLAYS[row._meta.label_lower].read_families
        )
        if not fresh_success_observation:
            return BOOKKEEPING_FIELDS
    if getattr(row, "status", None) in {"accepted", "deploying", "apply_failed"}:
        return BOOKKEEPING_FIELDS
    return frozenset({"last_apply_at", "last_apply_error", "apply_attempt_id"})


def publish_observation(footprint, family, identity):
    """Advance only for a different applied payload, never for cached admission."""
    from .refusal_registry import OVERLAYS

    if identity["payload_revision"] is None:
        return

    for ref in owner_rows(footprint):
        spec = OVERLAYS.get(ref.model_label)
        if ref.pk is None or spec is None or family not in spec.read_families:
            continue
        row = spec.model.objects.filter(pk=ref.pk).first()
        if row is None:
            continue
        fence = fence_for(row)
        if fence.observation_watermark.get(family) == identity:
            continue
        advance_fence(row, observation_watermark={**fence.observation_watermark, family: identity})
