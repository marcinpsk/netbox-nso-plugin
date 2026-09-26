# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Prepare switching snapshots and retain root deletion identity until authorization."""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses

from django.db import connection, transaction

from . import adapter_client, delivery

SCOPES = delivery.direct_keys()
_ROOT_FIELD = {"lacp": "name", "switchport": "interface_name"}
_BOUND: contextvars.ContextVar[tuple[tuple[str, ...], int] | None] = contextvars.ContextVar(
    "nso_switching_preparation", default=None
)


class FingerprintMismatch(Exception):
    """The capture must roll back before the audit takes its broader lock footprint."""


def _version() -> int:
    """Allocate a version that cannot be reused by a later deletion event."""
    from .models import NSOSwitchingRootDeletion

    table = connection.ops.quote_name(NSOSwitchingRootDeletion._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(pg_get_serial_sequence(%s, 'id'))", [table.strip('"')])
        return int(cursor.fetchone()[0])


def record(management, scope: str, root_name: str) -> None:
    """Record an exact writer's root deletion inside its content transaction."""
    from .models import NSOSwitchingRootDeletion

    if scope not in SCOPES or not root_name or not connection.in_atomic_block:
        raise ValueError("a switching root deletion requires a writer transaction and a root name")
    NSOSwitchingRootDeletion.objects.update_or_create(
        management=management,
        scope=scope,
        root_name=root_name,
        defaults={"event_version": _version()},
    )


def cancel(management, scope: str, root_name: str) -> None:
    """Withdraw a deletion when an owned root returns to the rendered snapshot."""
    from .models import NSOSwitchingRootDeletion

    if scope not in SCOPES or not connection.in_atomic_block:
        raise ValueError("a switching cancellation requires a writer transaction")
    NSOSwitchingRootDeletion.objects.filter(management=management, scope=scope, root_name=root_name).delete()


def cancel_if_rendered(management, scope: str, root_name: str) -> None:
    """Withdraw a restored root only when the owned snapshot contains its identity."""
    rendered = delivery.render(scope, management.device_id, management.adapter_device_id)
    if root_name in {item[_ROOT_FIELD[scope]] for item in rendered.payload}:
        cancel(management, scope, root_name)


@dataclasses.dataclass(frozen=True)
class Captured:
    rendered: delivery.Rendered
    adapter_device_id: int
    entry_ids: tuple[int, ...]
    deletion_versions: dict[str, int]
    deleted_roots: tuple[str, ...]
    source_revision: int


def _capture(device_id: int, scope: str, force: bool):
    """Read one content revision and snapshot under the direct drain's lock order."""
    from . import drain
    from .models import NSODeviceManagement, NSOIntentRevision, NSOSwitchingRootDeletion

    state = drain._lock_state(device_id, scope)
    state.last_drain_attempted_at = drain._db_now()
    state.save(update_fields=["last_drain_attempted_at"])
    management = (
        NSODeviceManagement.objects.select_for_update(of=("self",))
        .order_by()
        .filter(device_id=device_id, adapter_device_id__isnull=False)
        .first()
    )
    if management is None:
        return drain.PARKED, None
    rows = list(drain._unconsumed(device_id, scope).select_for_update().order_by("id"))
    if not rows and not force:
        return drain.NOTHING, None
    rendered = delivery.render(scope, device_id, management.adapter_device_id)
    revision, _ = NSOIntentRevision.objects.get_or_create(device_id=device_id, scope=scope)
    revision = NSOIntentRevision.objects.select_for_update(of=("self",)).get(pk=revision.pk)
    if (
        revision.verified_revision != revision.revision
        or revision.verified_fingerprint != delivery.canonical_fingerprint(rendered.payload)
    ):
        raise FingerprintMismatch
    deletions = list(NSOSwitchingRootDeletion.objects.filter(management=management, scope=scope).order_by("root_name"))
    rendered_names = {item[_ROOT_FIELD[scope]] for item in rendered.payload}
    for row in deletions:
        if row.root_name in rendered_names:
            NSOSwitchingRootDeletion.objects.filter(pk=row.pk, event_version=row.event_version).delete()
    versions = {row.root_name: row.event_version for row in deletions if row.root_name not in rendered_names}
    return "sending", Captured(
        rendered,
        management.adapter_device_id,
        tuple(row.pk for row in rows),
        versions,
        tuple(sorted(set(versions) - rendered_names)),
        int(revision.revision),
    )


@contextlib.contextmanager
def _bound(deleted_roots: tuple[str, ...], source_revision: int):
    token = _BOUND.set((deleted_roots, source_revision))
    try:
        yield
    finally:
        _BOUND.reset(token)


def _prepared_fields():
    bound = _BOUND.get()
    if bound is None:
        raise RuntimeError("switching calls require a captured preparation")
    return bound


def send_lag(adapter_device_id, bundles):
    """Send LAG content with the captured deletion authority and revision."""
    deleted_roots, source_revision = _prepared_fields()
    return adapter_client.apply_lag_config(
        adapter_device_id, bundles, deleted_roots=list(deleted_roots), source_revision=source_revision
    )


def send_switchport(adapter_device_id, interfaces):
    """Send switchport content with the captured deletion authority and revision."""
    deleted_roots, source_revision = _prepared_fields()
    return adapter_client.apply_switchport_config(
        adapter_device_id, interfaces, deleted_roots=list(deleted_roots), source_revision=source_revision
    )


def send(captured: Captured, *, deadline: float | None = None):
    """Send one captured body through the ordinary attempt and error journal."""
    from . import signals

    rendered = captured.rendered
    if deadline is not None:
        rendered = dataclasses.replace(rendered, do_push=delivery._under_deadline(rendered.do_push, deadline))
    with _bound(captured.deleted_roots, captured.source_revision):
        return signals._send_rendered(rendered, rendered.payload)


def _validate(answer, captured: Captured) -> tuple[str, ...]:
    stream = delivery.delivery_keys()[captured.rendered.key[1]].section
    if not isinstance(answer, dict) or answer.get("status") != "prepared":
        raise adapter_client.AdapterError("Switching preparation was not acknowledged.", code="invalid_preparation")
    if answer.get("device_id") != captured.adapter_device_id:
        raise adapter_client.AdapterError("Switching preparation named another device.", code="invalid_preparation")
    if answer.get("stream") != stream:
        raise adapter_client.AdapterError("Switching preparation named another stream.", code="invalid_preparation")
    if type(answer.get("selection_revision")) is not int or answer["selection_revision"] <= 0:
        raise adapter_client.AdapterError("Switching preparation omitted its selection.", code="invalid_preparation")
    unauthorized = answer.get("unauthorized_deleted_roots")
    if not isinstance(unauthorized, list) or any(type(name) is not str for name in unauthorized):
        raise adapter_client.AdapterError(
            "Switching preparation omitted deletion evidence.", code="invalid_preparation"
        )
    if not set(unauthorized) <= set(captured.deleted_roots):
        raise adapter_client.AdapterError("Switching preparation returned unknown roots.", code="invalid_preparation")
    return tuple(unauthorized)


def _retire(captured: Captured, unauthorized: tuple[str, ...]) -> None:
    from . import drain
    from .models import NSOIntentOutboxEntry, NSOSwitchingRootDeletion

    with transaction.atomic():
        drain._lock_state(*captured.rendered.key)
        if captured.entry_ids:
            drain._retire(NSOIntentOutboxEntry.objects.filter(id__in=captured.entry_ids), captured.entry_ids)
        for root_name in unauthorized:
            NSOSwitchingRootDeletion.objects.filter(
                management__device_id=captured.rendered.key[0],
                scope=captured.rendered.key[1],
                root_name=root_name,
                event_version=captured.deletion_versions[root_name],
            ).delete()


def prepare(device_id: int, scope: str, *, force: bool, deadline_at: float):
    """Capture, repair if needed, prepare, and discharge only proven superseded facts."""
    from . import drain, renderer_audit

    repaired = False
    conflicted = False
    first_conflict = None
    while True:
        try:
            drain._remaining_send_deadline(deadline_at)
        except Exception as exc:  # noqa: BLE001 (the attempt journal records the exhausted budget)
            drain._report_refusal(device_id, scope, exc)
            return drain.FAILED, None
        try:
            outcome, captured = drain._repeatable_read(lambda: _capture(device_id, scope, force))
        except FingerprintMismatch:
            if repaired:
                drain._report_refusal(
                    device_id,
                    scope,
                    adapter_client.AdapterError("Concurrent switching change.", code="concurrent_change"),
                )
                return drain.FAILED, None
            try:
                renderer_audit.repair_scope(
                    device_id,
                    scope,
                    deadline=renderer_audit._monotonic() + drain._remaining_send_deadline(deadline_at),
                )
            except Exception as exc:  # noqa: BLE001 (failed repair keeps the captured work)
                drain._report_refusal(device_id, scope, exc)
                return drain.FAILED, None
            repaired = True
            continue
        if captured is None:
            return outcome, None
        try:
            answer = send(captured, deadline=drain._remaining_send_deadline(deadline_at))
            unauthorized = _validate(answer, captured)
        except adapter_client.AdapterError as exc:
            reason = exc.detail.get("reason") if isinstance(exc.detail, dict) else exc.code
            if exc.status_code == 409 and reason == "stale_preparation":
                _retire(captured, ())
                return drain.SUPERSEDED, None
            if exc.status_code == 409 and reason == "revision_conflict" and not conflicted:
                conflicted = True
                first_conflict = (captured.source_revision, delivery.canonical_fingerprint(captured.rendered.payload))
                continue
            if exc.status_code == 409 and reason == "revision_conflict" and conflicted:
                identity = (captured.source_revision, delivery.canonical_fingerprint(captured.rendered.payload))
                if identity == first_conflict:
                    exc = adapter_client.AdapterError(
                        "Unchanged switching revision conflicted twice.", code="revision_invariant"
                    )
            drain._report_refusal(device_id, scope, exc)
            return drain.FAILED, None
        except Exception as exc:  # noqa: BLE001 (the entry remains for a later tick)
            drain._report_refusal(device_id, scope, exc)
            return drain.FAILED, None
        _retire(captured, unauthorized)
        return drain.SUCCEEDED, {**answer, "source_revision": captured.source_revision}
