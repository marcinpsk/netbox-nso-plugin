# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox-side mirror of the adapter's per-device last-sync state, and link reconciliation.

``NSODeviceManagement.last_sync_at``/``last_sync_status``/``degraded_surfaces`` cache what
the adapter reports for the mapped device. The adapter refreshes its own side on a poll
interval; this module is the only writer of the NetBox copy.

Everything here matches on the device's LOGICAL IDENTITY — ``(nso_instance, nso_device_name)``
plus the NetBox device — never on ``adapter_device_id`` alone. Adapter ids are per-install
serials, so a rebuilt or restored adapter DB can hand id 196 to a different device; trusting
the bare number would mirror that device's sync status onto this row and, on the next save,
push this row's scope, failover addresses and auto-apply flag onto it.
"""

import logging
from datetime import UTC, datetime

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import NSODeviceManagement

logger = logging.getLogger(__name__)

# Sort key for a row the link repair has never tried, so it goes to the front of the queue.
_NEVER_ATTEMPTED = datetime.min.replace(tzinfo=UTC)

# Most link repairs to attempt per sweep. A genuine fleet-wide loss then heals over several
# ticks instead of firing one onboard+sync burst at the adapter, and — unlike a "suppress
# when more than half look broken" rule — it can never wedge into repairing nothing forever.
MAX_RELINKS_PER_RUN = 10

# Row classifications against one adapter snapshot.
_MATCHED = "matched"  # our id, and it is our device — safe to mirror
_REUSED = "reused"  # our id exists but belongs to a DIFFERENT device — never touch it
_MOVED = "moved"  # our device is there under a different id — adopt it
_MISSING = "missing"  # our device is not in the adapter at all — re-link it

_BROKEN_LINK_MESSAGE = "This device's adapter mapping is broken; the next sync-cache sweep will repair it."


class _LinkReconcileNoOp(Exception):
    """Roll back a repair whose authoritative source identity changed."""


def _mirror_management(mgmt, **values) -> bool:
    """Persist lifecycle-only adapter observations through the exact writer."""
    from .intent_state import RendererTargetsChanged
    from .management_lifecycle import save_management
    from .renderer_writer import IntentPlanStaleError

    fields = frozenset(values)
    current = NSODeviceManagement.objects.filter(pk=mgmt.pk).first()
    if current is None:
        return False
    for field_name, value in values.items():
        setattr(current, field_name, value)
    try:
        save_management(current, update_fields=fields)
    except (IntentPlanStaleError, RendererTargetsChanged) as exc:
        logger.warning(
            "Skipped mirror update for management row %s because its renderer plan changed: %s",
            mgmt.pk,
            exc,
        )
        return False
    for field_name, value in values.items():
        setattr(mgmt, field_name, value)
    return True


def parse_adapter_timestamp(value, field="timestamp"):
    """Parse the adapter's canonical wire timestamp — ``<iso>Z``, optional fraction — to aware UTC.

    Anything else degrades to None rather than raising: every caller is on a page render.
    """
    try:
        parsed = parse_datetime(value)
    except (TypeError, ValueError):  # not a string, or regex-shaped but not a real datetime
        parsed = None
    if parsed is not None and parsed.tzinfo is None:
        # The contract is "<iso>Z"; an offset-less value names no instant we may trust.
        parsed = None
    if parsed is None:
        logger.warning("Adapter %s %r is not a timestamp — treating as absent", field, value)
    return parsed


def refresh_sync_cache(mgmt, adapter_device):
    """Update one row's cached last_sync_* from an adapter device dict.

    Writes only changed fields through the lifecycle writer's exact mutation plan.
    Returns the list of fields actually changed (empty if already current).
    """
    # Fail closed on identity: callers that fetch a single device by the stored id (the device
    # NSO tab) would otherwise copy a reused id's owner status onto this row — the same wrong
    # answer the bulk path refuses, arriving through a different door.
    if not _is_ours(mgmt, adapter_device):
        logger.warning(
            "Adapter device %s is not %s — refusing to mirror its state",
            adapter_device.get("id"),
            mgmt.nso_device_name,
        )
        return []
    update_fields = []
    # Mirror, don't merge: a key the adapter reports as null means "no sync on record", and
    # keeping the previous value there left a never-synced device reading days-old success.
    # A key absent from the payload is a different thing (partial shape) — leave it alone.
    if "last_sync_at" in adapter_device:
        raw_ts = adapter_device["last_sync_at"]
        last_sync_at = parse_adapter_timestamp(raw_ts, "last_sync_at") if raw_ts is not None else None
        if (raw_ts is None or last_sync_at is not None) and mgmt.last_sync_at != last_sync_at:
            mgmt.last_sync_at = last_sync_at
            update_fields.append("last_sync_at")
    last_sync_status = adapter_device.get("last_sync_status") or ""
    if mgmt.last_sync_status != last_sync_status:
        mgmt.last_sync_status = last_sync_status
        update_fields.append("last_sync_status")
    # Which routing surfaces went stale on a "partial" sync (e.g. ["bgp", "ospf"]),
    # normalized to None when nothing degraded so the display branches on truthiness.
    degraded_surfaces = adapter_device.get("degraded_surfaces") or None
    if mgmt.degraded_surfaces != degraded_surfaces:
        mgmt.degraded_surfaces = degraded_surfaces
        update_fields.append("degraded_surfaces")
    # adapter_link_error is deliberately NOT touched here: it records a plugin→adapter scope-push
    # failure, on a different clock from the adapter→NSO sync status, so a 'succeeded' predating
    # the failure would retire a banner whose scope never landed. It is cleared by the successful
    # push (signals.sync_scope_to_adapter) or the banner's own "Retry adapter link" action.
    if update_fields:
        persisted = _mirror_management(mgmt, **{field_name: getattr(mgmt, field_name) for field_name in update_fields})
        if not persisted:
            try:
                mgmt.refresh_from_db(fields=update_fields)
            except NSODeviceManagement.DoesNotExist:
                pass
            return []
    return update_fields


def _row_identity(mgmt) -> tuple:
    """Return the logical identity of the NSO node this management row owns."""
    return (mgmt.nso_instance.adapter_instance_id, mgmt.nso_device_name)


def _is_ours(mgmt, adapter_device) -> bool:
    """Report whether *adapter_device* is the same NSO node AND the same NetBox device.

    An adapter row still unlinked (``netbox_device_id`` null) counts as ours — that is the
    leftover an onboard adopts rather than duplicating.
    """
    if (adapter_device.get("nso_instance"), adapter_device.get("nso_device_name")) != _row_identity(mgmt):
        return False
    netbox_device_id = adapter_device.get("netbox_device_id")
    return netbox_device_id is None or netbox_device_id == mgmt.device_id


def _index(payload):
    """Index one adapter snapshot by id and by logical identity."""
    by_id, by_identity = {}, {}
    for device in payload:
        if not isinstance(device, dict) or device.get("id") is None:
            continue
        by_id[device["id"]] = device
        by_identity.setdefault((device.get("nso_instance"), device.get("nso_device_name")), []).append(device)
    return by_id, by_identity


def _classify(mgmt, by_id, by_identity):
    """Classify one management row against the snapshot. Returns ``(state, adapter_device)``."""
    current = by_id.get(mgmt.adapter_device_id)
    if current is not None:
        return (_MATCHED, current) if _is_ours(mgmt, current) else (_REUSED, current)
    candidates = [d for d in by_identity.get(_row_identity(mgmt), []) if _is_ours(mgmt, d)]
    # Exactly one unambiguous owner can be adopted; several means duplicate adapter rows for
    # one node, which is a conflict to surface rather than a mapping to guess at.
    if len(candidates) == 1:
        return _MOVED, candidates[0]
    if len(candidates) > 1:
        return _REUSED, None
    return _MISSING, None


def _snapshot(rows):
    """Fetch one adapter device snapshot for *rows*. Returns ``(mapped, by_id, by_identity)``.

    ``by_id`` is None when the adapter could not be reached — nothing is provable, so callers
    must do nothing rather than treat an outage as "every mapping is broken".
    """
    from . import adapter_client as client
    from .adapter_client import AdapterError

    mapped = [m for m in rows if m.adapter_device_id is not None]
    if not mapped:
        return [], None, None
    try:
        payload = client.list_devices() or []
    except AdapterError as exc:
        logger.debug("adapter snapshot unavailable: %s", exc)
        return mapped, None, None
    by_id, by_identity = _index(payload)
    return mapped, by_id, by_identity


def refresh_sync_caches(rows, snapshot=None) -> tuple[int, int]:
    """Refresh the cached last-sync mirror for *rows* from a single bulk adapter call.

    Pass *snapshot* (the ``(mapped, by_id, by_identity)`` triple from :func:`_snapshot`) to
    share one adapter call with :func:`reconcile_device_links`. Returns ``(checked, updated)``.
    Only rows whose stored id still resolves to their own device are mirrored — see the module
    docstring on why a bare id match is unsafe. An adapter outage leaves the mirror untouched.
    """
    mapped, by_id, by_identity = snapshot if snapshot is not None else _snapshot(rows)
    if by_id is None:
        return len(mapped), 0
    updated = 0
    for mgmt in mapped:
        state, adapter_device = _classify(mgmt, by_id, by_identity)
        if state is _MATCHED:
            if refresh_sync_cache(mgmt, adapter_device):
                updated += 1
        elif state in (_REUSED, _MISSING) and not (mgmt.onboard_status or mgmt.source_rekey_pending):
            # A page render can PROVE the mapping is wrong but does not repair it (that is the
            # job's work). Record it now, or the row keeps rendering its last good 'succeeded'
            # until the next sweep — the stale-green lie this whole change exists to remove.
            _flag_link_error(mgmt, _BROKEN_LINK_MESSAGE)
    return len(mapped), updated


def _flag_link_error(mgmt, message) -> None:
    """Record an operator-visible link error without firing the post_save adapter push."""
    if mgmt.adapter_link_error != message:
        _mirror_management(mgmt, adapter_link_error=message)


def reconcile_device_links(rows, snapshot=None) -> tuple[int, int]:
    """Repair rows whose ``adapter_device_id`` no longer resolves to their own adapter device.

    An adapter device row can disappear under a live management row — a provision that rolled
    back, a manual delete, a restored DB — or its id can be reused by a different device. The
    plugin is then left holding a wrong pointer and nothing notices: the link path skips
    onboarding because an id is set, so the row keeps rendering its last good sync. This is the
    plugin-side half of the reconcile; the adapter's own sweep only ever deletes its rows.

    Repairs, all of which re-save the row so the real link path (onboard → scope → sync-notify)
    runs and records its own outcome on the row:

    * *moved* — our device is present under a different id → adopt that id, no onboard needed.
    * *reused* — our id belongs to someone else → drop the pointer FIRST, so the re-save can
      never push this device's scope onto the other one, then re-link by identity.
    * *missing* — our device is absent → re-save; the not-found scope push re-onboards it.

    Returns ``(broken, attempted)``. Attempts are capped at :data:`MAX_RELINKS_PER_RUN`; rows
    over the cap keep an ``adapter_link_error`` so nothing is silently deferred, and the
    broken list is walked **least-recently-attempted first** so the cap rotates. Without that
    order a permanently broken head starves a repairable tail forever: the re-onboard an
    attempt triggers runs in an ``on_commit`` callback that swallows its own failure, so a
    row that repaired nothing still spends the cap, on every run. With ``B`` broken rows and
    cap ``C`` every one of them is attempted within ``ceil(B / C)`` ticks — five minutes each.
    """
    mapped, by_id, by_identity = snapshot if snapshot is not None else _snapshot(rows)
    if by_id is None:
        return 0, 0

    broken = []
    for mgmt in mapped:
        # A row mid-provision has no adapter device yet by design and its push is gated.
        # A row mid-rekey is NOT broken either: NetBox already carries the new NSO name while
        # the adapter still carries the old one, so it reads as "reused". Dropping its pointer
        # here would strand it for good — _snapshot only considers rows that HAVE an id, and
        # re-onboarding the new identity collides with the old row still holding this
        # netbox_device_id. _sync_source_change owns that transition, dead mapping included.
        if mgmt.onboard_status or mgmt.source_rekey_pending:
            continue
        state, adapter_device = _classify(mgmt, by_id, by_identity)
        if state is not _MATCHED:
            broken.append((mgmt, state, adapter_device))
    if not broken:
        return 0, 0

    # Least-recently-attempted first (never-attempted first, pk to break ties deterministically).
    broken.sort(key=lambda item: (item[0].adapter_link_attempted_at or _NEVER_ATTEMPTED, item[0].pk))

    attempted = 0
    now = timezone.now()
    for mgmt, state, adapter_device in broken:
        if attempted >= MAX_RELINKS_PER_RUN:
            _flag_link_error(mgmt, "Adapter mapping is broken; repair deferred to the next sweep.")
            continue
        expected_source = (mgmt.nso_instance_id, mgmt.nso_device_name, mgmt.adapter_device_id)
        try:
            from .intent_state import footprint_for_instance, intent_transaction
            from .management_lifecycle import save_management

            with intent_transaction(footprint_for_instance(mgmt)):
                current = type(mgmt).objects.get(pk=mgmt.pk)
                if (
                    current.source_rekey_pending
                    or (current.nso_instance_id, current.nso_device_name, current.adapter_device_id) != expected_source
                ):
                    raise _LinkReconcileNoOp
                # Stamp for being TRIED, not for succeeding, and before the try: whether the
                # re-onboard worked is not observable here (it happens in an on_commit callback
                # that logs and returns), so a stamp conditional on success would never move a
                # permanently broken row to the back of the queue. Persist the mirror-only
                # stamp through the lifecycle writer before attempting the repair.
                attempted += 1
                _mirror_management(current, adapter_link_attempted_at=now)
                if state is _MOVED:
                    logger.warning(
                        "Adapter device for %s moved from id %s to %s — adopting",
                        current.nso_device_name,
                        current.adapter_device_id,
                        adapter_device["id"],
                    )
                    _mirror_management(current, adapter_device_id=adapter_device["id"])
                elif state is _REUSED:
                    logger.warning(
                        "Adapter device id %s no longer belongs to %s — dropping the stale pointer",
                        current.adapter_device_id,
                        current.nso_device_name,
                    )
                    _mirror_management(current, adapter_device_id=None)
                save_management(current)
        except _LinkReconcileNoOp:
            continue
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            logger.exception("Link reconcile failed for management row %s", mgmt.pk)
            continue
    logger.info("Link reconcile: %d broken, %d repair attempted", len(broken), attempted)
    return len(broken), attempted
