# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Signal handlers for NSODeviceManagement scope propagation and intent push."""

import contextlib
import contextvars
import functools
import json
import logging
import threading
from collections import namedtuple

from django.db.models import Q
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .snmp_versions import canonical_snmp_version
from .status_machine import OWNED_STATES as _OWNED_PUSH_STATUSES

# Every intent-mirror push filters its overlay rows by _OWNED_PUSH_STATUSES (the canonical
# OWNED_STATES, *including* apply_failed): intent stays accepted underneath a failed apply,
# and the adapter PUTs are full-replace — a push that skipped apply_failed rows would drop
# their intent from the mirror and a retry-apply could no longer see them. Direct-apply
# pushes (switchport, LACP) keep narrower filters on purpose: including apply_failed there
# would re-attempt the failed device write on every save.

logger = logging.getLogger(__name__)

# Thread-local flag set by ip_autoassign._suppress_ip_intent_push() to prevent
# premature intent pushes during P2P IPAddress pair reservation.
_p2p_allocation_active = threading.local()

# Thread-local flag set by suppress_intent_push() around any reconcile/import that
# mirrors adapter state into the plugin's NSO*State tables. Such writes are NOT
# operator intent — they must never push intent back to the adapter. This is the
# single authoritative guard for the reconcile path (render today, the background
# reconcile job and the manual Refresh buttons next); see _skip_on_render.
_intent_push_suppressed = threading.local()


def _ned_id_for_device(device_id: int) -> str:
    """Return the platform's exact mapped NED id, or an empty string."""
    from .models import NSODeviceManagement, NSOPlatformNedMapping

    platform_id = (
        NSODeviceManagement.objects.filter(device_id=device_id).values_list("device__platform_id", flat=True).first()
    )
    if platform_id is None:
        return ""
    ned_id = NSOPlatformNedMapping.objects.filter(platform_id=platform_id).values_list("ned_id", flat=True).first()
    return str(ned_id or "")


def _is_intent_push_suppressed() -> bool:
    return getattr(_intent_push_suppressed, "active", False)


class suppress_intent_push:  # noqa: N801 — context-manager named like a verb on purpose
    """Context manager: silence intent-push signals for the duration of a reconcile.

    Wrap any code path that writes NSO*State rows from adapter data (the reconcilers)
    so that mirroring adapter state never fires a push back to the adapter. Reentrant.
    """

    def __enter__(self):
        self._prev = getattr(_intent_push_suppressed, "active", False)
        _intent_push_suppressed.active = True
        return self

    def __exit__(self, *exc):
        _intent_push_suppressed.active = self._prev
        return False


# Header the nso-adapter sets on every write it makes to NetBox. Such writes are
# imports/applies (adapter-origin), NOT operator intent edits, so the Decision-G
# signal must not promote them to 'accepted' or push them back as intent.
_ADAPTER_IMPORT_HEADER = "X-NSO-Adapter-Import"


def _is_adapter_origin_write() -> bool:
    """Return True if the current request is an adapter-origin write (carries the import header).

    Origin — not content — is the correct discriminator: an import that writes a
    *changed* value and an operator edit to the *same* value leave identical
    interface state; only the request origin distinguishes them. The adapter runs
    in a separate process, so a thread-local can't reach here; the marker rides on
    the HTTP request via NetBox's current_request contextvar.
    """
    try:
        from netbox.context import current_request

        request = current_request.get()
    except Exception:
        return False
    if request is None:
        return False
    return request.headers.get(_ADAPTER_IMPORT_HEADER) is not None


def _is_render_request() -> bool:
    """Return True when the active HTTP request is a GET (a page render).

    Intent pushes are mutations and must never fire as a side effect of *rendering*
    a page. The device NSO tab's render reconcilers re-save NSO*State rows on every
    view (to refresh display fields / ``last_sync_at``); each such save of an
    ``accepted`` row would otherwise push the full intent snapshot to the adapter —
    O(N) pushes per render, each O(N) — which hung the device-27 tab and re-minted
    'accepted' rows the operator never clicked. Genuine accepts and interface edits
    arrive as POSTs, so they still push. Returns False when there is no request
    (programmatic / CLI / test contexts) so those keep pushing normally.
    """
    try:
        from netbox.context import current_request

        request = current_request.get()
    except Exception:
        return False
    return request is not None and request.method == "GET"


def _skip_on_render(handler):
    """Drop an intent-push signal handler's effect when it fires during a GET render.

    Decorator applied to every push-on-save handler so that merely viewing the NSO
    tab never pushes intent to the adapter. See :func:`_is_render_request`.
    """

    @functools.wraps(handler)
    def _wrapped(*args, **kwargs):
        # suppress_intent_push() (the reconcile/import path) is the authoritative
        # guard; the GET-render check is a belt-and-suspenders for the legacy
        # render-time reconcile and becomes redundant once render is read-only.
        if _is_intent_push_suppressed() or _is_render_request():
            return None
        return handler(*args, **kwargs)

    return _wrapped


# ── Intent-push scheduling: the durable outbox ────────────────────────────────
#
# A bulk operation (e.g. NetBox's native bulk-edit) saves N rows in one transaction; each
# save fires a push handler that would rebuild and PUT the FULL device snapshot. Without
# coalescing that is O(N^2) work and N HTTP PUTs.
#
# So a save does not push. It APPENDS a row to the outbox (#1503 Appendix O), and the
# commit callback drains the key once through the claim protocol: one fold, one render, one
# send. The two in-memory carriers this replaced are gone for cause — a thread-local map of
# pending pushes survived the rollback that discarded its reason (§2), and a process-global
# last-pushed digest authorized deleting routes a stale worker never knew about (§8.3).
# Change detection is now the state row's own ``last_success_identity``, which names a body
# the adapter ACKNOWLEDGED rather than one this process happened to send.
#
# The cell below is thread-local and holds only keys. Registration is unconditional and the
# first callback clears it, so a cell a rollback left behind costs extra O(1) callbacks and
# never a missing drain. The append refuses when no writer transaction is open.
_intent_keys = threading.local()


def _pending_intent_keys() -> set:
    """Return the keys this thread's current transaction has appended to."""
    keys = getattr(_intent_keys, "keys", None)
    if keys is None:
        keys = set()
        _intent_keys.keys = keys
    return keys


def reset_intent_push_state() -> None:
    """Clear the pending-key cell. Intended for use in tests."""
    _intent_keys.keys = set()


# True while a DELETION-signal receiver is dispatching (see _as_delete_origin). Pushes
# scheduled under it are stamped ``?delete_origin=true`` so the adapter knows the shrink
# came from a NetBox object deletion and may retract from the device; every unmarked
# shrink is an un-own and DETACHES instead (device untouched, #106).
_DELETE_DISPATCH: contextvars.ContextVar[bool] = contextvars.ContextVar("nso_delete_dispatch", default=False)


@contextlib.contextmanager
def _delete_origin_dispatch():
    """Mark every push scheduled inside the block as deletion-driven."""
    token = _DELETE_DISPATCH.set(True)
    try:
        yield
    finally:
        _DELETE_DISPATCH.reset(token)


def _as_delete_origin(handler):
    """Wrap a deletion-signal receiver so every push it schedules is deletion-marked.

    Only for receivers that fire EXCLUSIVELY on deletion (pre_delete/post_delete). A
    multi-action signal such as m2m_changed must not be wrapped — it would stamp an ADD
    as a deletion; those handlers open :func:`_delete_origin_dispatch` around their
    removal branch instead.

    Connected with ``weak=False`` (the wrapper is otherwise only weakly referenced by
    the signal registry and would be garbage-collected).
    """

    @functools.wraps(handler)
    def _wrapped(sender=None, **kwargs):
        with _delete_origin_dispatch():
            return handler(sender=sender, **kwargs)

    return _wrapped


def _device_is_managed(device_id) -> bool:
    """Whether *device_id* still has an adapter-linked NSODeviceManagement row.

    Deleting that row — unmanaging, or deleting the Device, which CASCADEs into it —
    tears down every NSO*State overlay with it, and each of those post_deletes is
    _as_delete_origin-wrapped. Without this guard the drain would then build one snapshot
    per scope from the now-EMPTY overlay and ship it as ?delete_origin=true: the adapter
    reads an authorized full-replace to nothing and retracts every NSO-owned service from
    the LIVE device. Unmanaging is NetBox-side bookkeeping; the device config must not move
    (the offboard DELETE is the only adapter call a teardown makes).
    """
    from .models import NSODeviceManagement

    return NSODeviceManagement.objects.filter(device_id=device_id, adapter_device_id__isnull=False).exists()


# ── Teardown: a device on its way out records nothing ──────────────────────────
#
# Deleting a Device (or unmanaging it) cascades every overlay away, and each of those
# post_deletes schedules a push the drain then drops (see _device_is_managed). The outbox
# must drop it EARLIER, at the append: the cascade is NetBox-side bookkeeping and carries no
# operator intent, and a row appended after Django's collector took its snapshot would fail
# the deferred foreign key at COMMIT. Every pre_delete fires before any post_delete, so the
# mark is up before the first overlay handler runs, and the Device's own mark outlives the
# management row's.


@receiver(pre_delete, sender="dcim.Device")
def _mark_device_teardown(sender, instance, **kwargs):
    from . import outbox

    outbox.mark_device_teardown(instance.pk, outbox.current_txid())


@receiver(post_delete, sender="dcim.Device")
def _clear_device_teardown(sender, instance, **kwargs):
    from . import outbox

    outbox.clear_device_teardown(instance.pk, outbox.current_txid())


@receiver(pre_delete, sender="netbox_nso_plugin.NSODeviceManagement")
def _mark_management_teardown(sender, instance, **kwargs):
    from . import outbox

    outbox.mark_device_teardown(instance.device_id, outbox.current_txid())


@receiver(post_delete, sender="netbox_nso_plugin.NSODeviceManagement")
def _clear_management_teardown(sender, instance, **kwargs):
    from . import outbox

    outbox.clear_device_teardown(instance.device_id, outbox.current_txid())


def _schedule_intent_push(key, transitions=()) -> None:
    """Append this transaction's contribution to *key* and arrange for the key to drain.

    *transitions* is the provenance of what this transaction did to the key — which routes
    it deleted, which it re-owned — recorded alongside the dispatch mark it did it under.
    The entry is the operator transaction's own row, so a rollback discards it.

    The push itself is the drain's: it folds every unconsumed entry, renders one body and
    sends it once. The delete-origin mark survives that fold only when EVERY contributor
    was a deletion (AND), so an un-own folded with a delete leaves the shrink unmarked and
    the adapter detaches, erring toward never touching the device.

    The drain always runs on commit, never inline: the append refuses to run outside the
    writer's transaction (O1.2), so by the time there is anything to drain there is a
    commit to wait for.
    """
    from django.db import transaction

    from . import outbox

    if _is_intent_push_suppressed() or _is_render_request():
        return  # a reconcile or render write mirrors the adapter; it is not operator intent
    outbox.enqueue(key[0], key[1], transitions=transitions, delete_origin=_DELETE_DISPATCH.get())
    _pending_intent_keys().add(tuple(key))
    transaction.on_commit(_drain_intent_pushes)


def _drain_intent_pushes() -> None:
    """Drain every key this transaction appended to, isolating failures between them.

    The first callback takes the whole cell and clears it, so callbacks 2..N of a bulk edit
    are O(1). A per-key failure is data: the claim keeps its rows and its sequence, and the
    five-minute tick supplies the next attempt.
    """
    from . import drain

    keys = _pending_intent_keys()
    claimed = sorted(keys)
    keys.clear()
    for device_id, scope in claimed:
        try:
            drain.drain_key(device_id, scope)
        except Exception as exc:  # noqa: BLE001 — one key's drain must not abort its siblings
            logger.warning("Intent outbox drain failed for %s/%s: %s", device_id, scope, exc)


def _allocate_push_attempt(device_id, scope):
    """Bump and return this scope's attempt high-water mark, or ``None`` if unmanaged.

    Allocated BEFORE the request and never cleared. The mark is what tells a delayed
    response from a current one: a success clears the visible error entry but leaves the
    mark standing, so an attempt-1 failure that arrives after attempt 2 already succeeded
    is discarded instead of resurrecting over it.
    """
    from django.db import transaction

    from .models import NSODeviceManagement

    try:
        with transaction.atomic():
            mgmt = NSODeviceManagement.objects.select_for_update().filter(device_id=device_id).first()
            if mgmt is None:
                return None
            attempts = dict(mgmt.intent_push_attempts or {})
            attempt = int(attempts.get(scope) or 0) + 1
            attempts[scope] = attempt
            # Queryset update, not save(): the mark is bookkeeping and must not re-date
            # last_updated or wake a management post_save.
            NSODeviceManagement.objects.filter(pk=mgmt.pk).update(intent_push_attempts=attempts)
            return attempt
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never block the push itself
        logger.warning("Could not allocate an intent-push attempt for device %s/%s: %s", device_id, scope, exc)
        return None


def _push_error_entry(exc, attempt):
    """Render an exception as the persisted per-scope rejection record."""
    from django.utils import timezone

    from .adapter_client import AdapterError

    if isinstance(exc, AdapterError):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return {
            "code": exc.code or "",
            "message": str(exc),
            "detail": detail,
            "at": timezone.now().isoformat(),
            "attempt": attempt,
        }
    # Not an AdapterError — no structured code or detail exists, so say so rather than
    # inventing one. repr() keeps the exception type, which is the whole diagnostic.
    return {
        "code": "",
        "message": repr(exc),
        "detail": {},
        "at": timezone.now().isoformat(),
        "attempt": attempt,
    }


def _attribute_static_route_error(device_id, detail):
    """Which owned routes an adapter static-route rejection names.

    ``duplicate_route_id`` names one pk, so it resolves to exactly one overlay.
    ``duplicate_triple`` does NOT: it fires because two *payload entries* share a triple,
    so every owned route holding that triple is a candidate and all of them are named.
    An unresolvable detail yields ``[]`` — device-scoped, which is honest rather than a
    guess at one row.
    """
    from .models import NSOStaticRouteState

    reason = (detail or {}).get("reason")
    if reason == "duplicate_route_id":
        route_id = detail.get("route_id")
        return [route_id] if isinstance(route_id, int) else []
    if reason != "duplicate_triple":
        return []
    triple = detail.get("triple")
    if not (isinstance(triple, list) and len(triple) == 3):
        return []
    vrf, prefix, next_hop = (str(part) for part in triple)
    matched = []
    # The same predicate the push serializes by: the rejection names payload entries, so a
    # row the push never sent may never be attributed one.
    for row in NSOStaticRouteState.objects.filter(
        PUSHED_STATIC_ROUTE_FILTER,
        management__device_id=device_id,
    ).select_related("static_route", "static_route__vrf"):
        sr = row.static_route
        row_vrf = sr.vrf.name if sr.vrf else ""
        if (row_vrf, str(sr.prefix), str(sr.next_hop)) == (vrf, prefix, next_hop):
            matched.append(sr.pk)
    return sorted(matched)


# Scope → the attribution that turns an adapter rejection into the objects it names.
# Only static routes carry per-object identity on the wire today.
_PUSH_ERROR_ATTRIBUTION = {"static_route": _attribute_static_route_error}


def _record_push_outcome(device_id, scope, attempt, exc):
    """Persist (or clear) this scope's rejection record, discarding a superseded response.

    Per ``(device, scope)`` under ``select_for_update``: the record is a JSONField shared
    by every scope, so a plain read-modify-write from two workers loses one of them, and a
    device-wide record would let one scope's failure erase another's.
    """
    from django.db import transaction

    from .models import NSODeviceManagement

    if attempt is None:
        return
    try:
        with transaction.atomic():
            mgmt = NSODeviceManagement.objects.select_for_update().filter(device_id=device_id).first()
            if mgmt is None:
                return
            high_water = int((mgmt.intent_push_attempts or {}).get(scope) or 0)
            if attempt < high_water:
                # A newer attempt has since been made; this response describes a
                # superseded request and must not overwrite what that attempt recorded.
                logger.info(
                    "Discarding a superseded intent-push outcome for device %s/%s (attempt %s < %s)",
                    device_id,
                    scope,
                    attempt,
                    high_water,
                )
                return
            errors = dict(mgmt.intent_push_errors or {})
            if exc is None:
                if errors.pop(scope, None) is None:
                    return
            else:
                entry = _push_error_entry(exc, attempt)
                attribute = _PUSH_ERROR_ATTRIBUTION.get(scope)
                if attribute is not None:
                    entry["route_ids"] = attribute(device_id, entry["detail"])
                errors[scope] = entry
            NSODeviceManagement.objects.filter(pk=mgmt.pk).update(intent_push_errors=errors)
    except Exception as exc2:  # noqa: BLE001 — surfacing must never turn a swallowed push into a raise
        logger.warning("Could not record the intent-push outcome for device %s/%s: %s", device_id, scope, exc2)


def read_push_attempt(device_id, scope):
    """Return this scope's attempt high-water mark, or ``None`` when it has none.

    A caller that reports an outcome it did not itself allocate an attempt for — the claim's
    response validation, which runs after the send returned — names the attempt already
    standing, so its record cannot be discarded as superseded by the attempt it belongs to.
    """
    from .models import NSODeviceManagement

    row = NSODeviceManagement.objects.filter(device_id=device_id).values("intent_push_attempts").first()
    attempt = (row["intent_push_attempts"] or {}).get(scope) if row else None
    return int(attempt) if attempt is not None else None


def _push_changed(key, payload, do_push, on_response=None):
    """Offer *payload* for *key* to the render in progress, and send nothing.

    This is the render/send choke point the claim protocol needs (#1503 Appendix O, §4.2):
    the body is captured here and the send happens later, outside every transaction, so a
    claim can render inside its own one. Every push function reaches exactly one of these,
    which is what makes the delivery registry an enumeration rather than a promise.

    The direct send this used to fall back to is GONE with its callers (§4.2): it sent with
    no claim and no ``X-Push-Seq``, and it swallowed the failure of a push that had already
    published ownership, leaving nothing durable for the tick to carry. Reaching this
    outside a render is therefore a programming error and says so, rather than delivering
    intent by a route the protocol cannot see. :func:`delivery.deliver` is the supported way
    to render and send a key on the spot.

    The name is history: change detection used to live here, against a process-global digest
    of the last body this worker sent. Appendix O deleted that too, because a stale worker's
    cache authorized deleting routes it never knew about (§8.3); the claim now dedupes
    against the state row's ``last_success_identity``, which names a body the adapter
    acknowledged.
    """
    from .delivery import Rendered, capture

    rendered = Rendered(key=key, payload=payload, do_push=do_push, on_response=on_response)
    if not capture(rendered):
        raise RuntimeError(f"the {key} push ran outside a render: every send goes through the outbox")


def _send_rendered(rendered, body):
    """Send one rendered body: the attempt mark, the call, the outcome record, the side effect.

    The coalescer and the claim protocol share this, so a push made either way is marked,
    recorded and settled identically. It RAISES on a failed call, because the claim has to
    tell a failure from a success; :func:`_push_changed` is what swallows it for the
    coalescer.
    """
    device_id, scope = rendered.key
    attempt = _allocate_push_attempt(device_id, scope)
    try:
        result = rendered.do_push(body)
    except Exception as exc:
        _record_push_outcome(device_id, scope, attempt, exc)
        raise
    _record_push_outcome(device_id, scope, attempt, None)
    if rendered.on_response is not None and isinstance(result, dict):
        try:
            rendered.on_response(result)
        except Exception:  # noqa: BLE001 (the adapter already acknowledged the send)
            logger.exception("Intent push success hook failed for device %s/%s", device_id, scope)
    return result


def _push_interface_intent_for_device(device_id, adapter_device_id) -> None:
    """Build the full OWNED interface intent snapshot and push it (change-detected).

    Owned = ``status in OWNED_STATES`` (accepted/deploying/in_sync/apply_failed) — the
    canonical ownership test, identical to every other scope's push predicate and to
    what the device tab now displays. (Previously this keyed off ``accepted_at``, a
    one-shot timestamp never cleared on un-own, so a row reverted/drifted back to
    ``imported`` carried a stale accepted_at and was force-pushed despite reading as
    drift — the display/push split-brain this fix removes.) Shared by the accept signal,
    the Decision-G edit signal, and the view-level bulk accept so all three agree on
    what gets pushed. Ownership is kept durable by the reconciler's owned-guard
    (``template_content._upsert_interface_states``), which no longer lets an adapter sync
    clobber an owned status back to ``imported``.
    """
    from . import adapter_client as client
    from .models import NSOInterfaceState

    states = NSOInterfaceState.objects.filter(
        interface__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface")

    attributes = []
    for state in states:
        iface = state.interface
        if state.attribute == "description":
            intent_value = iface.description or ""
        elif state.attribute == "enabled":
            intent_value = str(iface.enabled).lower()
        else:
            continue
        attributes.append(
            {
                "interface": iface.name,
                "attribute": state.attribute,
                "intent_value": intent_value,
                "accepted_at": state.accepted_at.isoformat() if state.accepted_at else None,
            }
        )

    _push_changed(
        (device_id, "interface"),
        attributes,
        lambda body: client.put_intent(adapter_device_id, body),
    )


#: The destination protocols a redistribution change can be scheduled against. It is the
#: delivery key itself, so an unknown value names no renderer and must be refused, not sent.
_REDISTRIBUTION_DESTINATIONS = ("ospf", "isis", "bgp")


def _schedule_redistribution_push(device_id, dest) -> None:
    """Schedule the destination protocol's intent push for a redistribution change.

    Keyed by (device, dest_protocol) so redistribution and the protocol's own state
    saves fold into a single push for that protocol.
    """
    if dest not in _REDISTRIBUTION_DESTINATIONS:
        logger.warning(
            "Redistribution: unknown dest_protocol %r for device %s — no push triggered",
            dest,
            device_id,
        )
        return
    _schedule_intent_push((device_id, dest))


@receiver(pre_save, sender="netbox_nso_plugin.NSODeviceManagement")
def remember_adapter_source(sender, instance, **kwargs):
    """Carry a source change's fail-closed fence in the same durable row update."""
    if not instance.pk:
        instance._nso_source_changed = True
        return
    previous = (
        sender.objects.filter(pk=instance.pk)
        .values_list("nso_instance_id", "nso_device_name", "source_rekey_pending")
        .first()
    )
    changed = previous is not None and (
        previous[:2] != (instance.nso_instance_id, instance.nso_device_name) or previous[2]
    )
    instance._nso_source_changed = changed
    if changed:
        # This field is part of the source-tuple UPDATE itself. A process death
        # before the on_commit callback can therefore never leave the new tuple
        # admitting payloads from the old adapter source.
        instance.source_rekey_pending = True


def _invalidate_source_admissions(instance) -> int:
    """Fence every in-flight family body before a remote source rekey."""
    from django.db.models import F

    from .models import NSOFamilyReadState
    from .read_gate import _RESET_FIELDS

    return NSOFamilyReadState.objects.filter(management=instance).update(
        **_RESET_FIELDS,
        publication_sequence=F("publication_sequence") + 1,
    )


@contextlib.contextmanager
def _source_rekey_lock(management_id):
    """Serialize remote rekeys across transactions on this management row."""
    from django.db import connection

    namespace = 0x4E534F  # "NSO"; two-int PostgreSQL advisory-lock namespace
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [namespace, management_id])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [namespace, management_id])


def _sync_source_change(instance, client) -> bool:
    """Serialize a source rekey and adopt its epoch only for the persisted tuple."""
    from django.db import transaction

    management_model = type(instance)
    expected_source = (instance.nso_instance_id, instance.nso_device_name)
    with _source_rekey_lock(instance.pk):
        with transaction.atomic():
            current = management_model.objects.select_for_update().select_related("nso_instance").get(pk=instance.pk)
            if (current.nso_instance_id, current.nso_device_name) != expected_source:
                return False
            invalidated = _invalidate_source_admissions(current)
            management_model.objects.filter(pk=current.pk).update(source_rekey_pending=True)
            current.source_rekey_pending = True

        from .adapter_client import AdapterError

        try:
            result = client.patch_device(
                adapter_device_id=current.adapter_device_id,
                nso_instance=current.nso_instance.adapter_instance_id,
                nso_device_name=current.nso_device_name,
            )
        except AdapterError as exc:
            # The mapping this rekey retargets is gone, so the PATCH can never land and both the
            # periodic repair and the Retry button would re-enter here forever. Re-onboard under
            # the NEW identity — which is what the rekey was expressing — then fall through to
            # the same completion below, so the epoch fence and the reset-pending marker are
            # recorded exactly as on the normal path. Handling it HERE rather than at the caller
            # is what keeps ``invalidated`` in scope: admissions were already blanked above, and
            # dropping that marker would leave every family fenced while the UI read all-clear.
            if exc.code != "not_found":
                raise
            logger.warning(
                "Rekey target %s is gone for NetBox device %s — re-onboarding under the new source",
                current.adapter_device_id,
                current.device_id,
            )
            _onboard_into_adapter(current, client)
            # ``current`` is a separate instance from the caller's ``instance``; without this
            # the scope push below would still target the dead id.
            instance.adapter_device_id = current.adapter_device_id
            result = {"source_epoch": current.adapter_source_epoch}
        if result.get("source_epoch") is None:
            raise RuntimeError("adapter rekey response omitted source_epoch; publication remains fenced")
        with transaction.atomic():
            current = management_model.objects.select_for_update().get(pk=instance.pk)
            if (current.nso_instance_id, current.nso_device_name) != expected_source:
                return False
            source_epoch = result["source_epoch"]
            source_aware = True
            management_model.objects.filter(pk=current.pk).update(
                adapter_source_epoch=source_epoch,
                source_epoch_aware=source_aware,
                source_rekey_pending=False,
                reset_pending_source_epoch=source_epoch if invalidated else None,
            )
    instance.adapter_source_epoch = source_epoch
    instance.source_epoch_aware = source_aware
    instance.source_rekey_pending = False
    return True


def _onboard_into_adapter(instance, client):
    """Register the device with the adapter and store the returned mapping on the row.

    ``.update()`` (not ``.save()``) so storing the mapping doesn't re-enter this handler.
    """
    result = client.onboard_device(
        nso_instance=instance.nso_instance.adapter_instance_id,
        nso_device_name=instance.nso_device_name,
        netbox_device_id=instance.device_id,
    )
    type(instance).objects.filter(pk=instance.pk).update(
        adapter_device_id=result["id"],
        adapter_source_epoch=result.get("source_epoch"),
        source_epoch_aware=result.get("source_epoch") is not None,
    )
    instance.adapter_device_id = result["id"]
    instance.adapter_source_epoch = result.get("source_epoch")
    instance.source_epoch_aware = result.get("source_epoch") is not None


@receiver(post_save, sender="netbox_nso_plugin.NSODeviceManagement")
def sync_scope_to_adapter(sender, instance, created, **kwargs):
    """Run adapter side effects only after the management-row transaction commits."""
    from django.db import transaction

    if getattr(instance, "onboard_status", "") in ("provisioning", "provision_failed"):
        return
    transaction.on_commit(lambda: _sync_committed_scope_to_adapter(sender, instance.pk, created))


def _sync_committed_scope_to_adapter(sender, instance_pk, created):
    """Push device + scope to the adapter whenever an NSODeviceManagement record is saved.

    After setting scope, calls sync-notify so the adapter starts an immediate sync
    rather than waiting for the next scheduled poll.

    Gated during async onboarding: while a row is ``provisioning`` (the background
    provision job hasn't finished) or ``provision_failed``, the NSO node may not exist
    yet, so mapping/scope/sync would fail or race. The status-advance view clears the
    status to "" on success and re-saves, which fires this handler normally.
    """
    try:
        instance = sender.objects.select_related("nso_instance", "device").get(pk=instance_pk)
    except sender.DoesNotExist:
        return
    if getattr(instance, "onboard_status", "") in ("provisioning", "provision_failed"):
        return

    from . import adapter_client as client
    from .adapter_client import AdapterError

    try:
        if created or instance.adapter_device_id is None:
            _onboard_into_adapter(instance, client)
        elif instance.source_rekey_pending:
            # _sync_source_change recovers a dead mapping itself — it owns the fencing state.
            if not _sync_source_change(instance, client):
                return

        # Carry the device's management addresses so the adapter's failover loop can probe
        # primary and fall back to OOB. Resolved by the SAME helper onboarding uses, so the
        # provision address and the failover-probed addresses never diverge. Explicit values
        # (incl. None to clear) — the plugin is authoritative, so a removed OOB IP in NetBox
        # clears it adapter-side.
        from .onboarding import device_mgmt_addresses

        primary_ip, oob_ip = device_mgmt_addresses(instance.device)

        def push_scope():
            client.set_scope(
                instance.adapter_device_id,
                instance.managed_attributes,
                auto_apply=instance.auto_apply,
                sync_before_apply=instance.sync_before_apply,
                primary_ip=primary_ip,
                oob_ip=oob_ip,
            )

        try:
            push_scope()
        except AdapterError as exc:
            # The stored id points at an adapter device row that no longer exists (a provision
            # that rolled back, a manual delete, a restored DB). Without this the branch above
            # never re-onboards — the id is set — so every push 404s forever and even the tab's
            # "Retry adapter link" can't heal it. Only 'not_found' proves the id is dead; any
            # other error is an outage and must not mint a second device row.
            if exc.code != "not_found":
                raise
            logger.warning(
                "Adapter no longer has device %s for NetBox device %s — re-onboarding",
                instance.adapter_device_id,
                instance.device_id,
            )
            _onboard_into_adapter(instance, client)
            push_scope()

        notify_result = client.sync_notify(instance.adapter_device_id)
        if notify_result and notify_result.get("job_id"):
            logger.debug(
                "Sync-notify sent for device %s, job_id=%s",
                instance.device_id,
                notify_result["job_id"],
            )
        # Linking succeeded — clear any error left by a prior failed attempt so the tab banner
        # goes away. .update() (not .save()) so this doesn't re-fire this post_save handler.
        if instance.adapter_link_error:
            type(instance).objects.filter(pk=instance.pk).update(adapter_link_error="")
            instance.adapter_link_error = ""
    except Exception as exc:
        logger.warning("Failed to sync scope to adapter for device %s: %s", instance.device_id, exc)
        # Surface the failure on the row instead of only logging it: otherwise the device looks
        # managed in NetBox while silently unlinked from the adapter (adapter_device_id stays None),
        # with nothing mirrored/applied and no operator-visible signal. .update() avoids recursion.
        message = str(exc) or repr(exc)
        instance.adapter_link_error = message
        # Persist for the tab banner ONLY if the failure didn't already break the surrounding
        # transaction: a DB-origin error in the try (e.g. a bad adapter response fed into the
        # adapter_device_id update) marks the connection needs_rollback, and writing then raises
        # TransactionManagementError — and that save is rolling back regardless, so recording is moot.
        from django.db import connection

        if not connection.needs_rollback:
            type(instance).objects.filter(pk=instance.pk).update(adapter_link_error=message)


@receiver(post_delete, sender="netbox_nso_plugin.NSODeviceManagement")
def offboard_device_from_adapter(sender, instance, **kwargs):
    """Remove the device from the adapter once the management row's deletion COMMITS.

    Deferred to on_commit for two reasons. A rolled-back delete must not have already
    offboarded the device adapter-side; and while the deleting transaction is open the row is
    still visible to other connections, so a concurrent save (or the periodic link repair)
    could see the mapping, get a 404 from the just-deleted adapter device, and re-onboard a
    fresh row that nothing then owns — this handler has already fired against the old id.
    """
    if instance.adapter_device_id is None:
        return
    from django.db import transaction

    from . import adapter_client as client

    adapter_device_id = instance.adapter_device_id

    def _offboard():
        try:
            client.delete_device(adapter_device_id)
        except Exception as exc:  # noqa: BLE001 — best-effort; never block the NetBox delete
            logger.warning("Failed to offboard device %s from adapter: %s", adapter_device_id, exc)

    transaction.on_commit(_offboard)


@receiver(post_save, sender="netbox_nso_plugin.NSOFailoverSettings")
def push_failover_settings_to_adapter(sender, instance, **kwargs):
    """Push the global failover tuning to the adapter whenever the singleton is saved.

    The adapter persists it and applies it on the next base tick (no restart). Failures are
    swallowed with a warning so a transient adapter outage never blocks saving the settings.
    """
    from . import adapter_client as client

    payload = {
        "enabled": instance.enabled,
        "primary_probe_interval": instance.primary_probe_interval,
        "oob_probe_interval": instance.oob_probe_interval,
        "failure_threshold": instance.failure_threshold,
        "success_threshold": instance.success_threshold,
        "probe_timeout": instance.probe_timeout,
        "active_probe_timeout": instance.active_probe_timeout,
        "probe_concurrency": instance.probe_concurrency,
        "max_flips_per_tick": instance.max_flips_per_tick,
        "sync_from_after_switch": instance.sync_from_after_switch,
    }
    try:
        client.put_failover_config(payload)
    except Exception as exc:
        logger.warning("Failed to push failover settings to adapter: %s", exc)


@receiver(post_save, sender="netbox_nso_plugin.NSOInterfaceState")
@_skip_on_render
def push_intent_on_accept(sender, instance, **kwargs):
    """Push the full intent snapshot to the adapter when an interface state is OWNED.

    Owned = ``status in OWNED_STATES`` (accepted/deploying/in_sync/apply_failed) — the
    canonical test, mirroring :func:`_push_interface_intent_for_device`. Accepting a
    value that already matches the device sets ``in_sync`` (an owned status), so it still
    records ownership in the adapter and survives the next sync.

    The push is coalesced + change-detected via :func:`_schedule_intent_push`.

    Also wired to post_delete (deletion-marked, see _connect_g_activated): deleting an owned
    row must push the REDUCED snapshot, or the adapter keeps applying the intent NetBox just
    dropped. The device is resolved through the FK id rather than ``instance.interface`` —
    on the post_delete leg the Interface may already be gone (a cascade from its own
    deletion), and a receiver that raised there would abort the whole delete.
    """
    if instance.status not in _OWNED_PUSH_STATUSES:
        return

    from dcim.models import Interface

    from .models import NSODeviceManagement

    device_id = Interface.objects.filter(pk=instance.interface_id).values_list("device_id", flat=True).first()
    if device_id is None:
        return
    try:
        mgmt = NSODeviceManagement.objects.get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _schedule_intent_push((device_id, "interface"))


def _templates():
    """Return enabled derived-intent templates from the database."""
    from .derived_intent import get_sentinel_templates

    return get_sentinel_templates()


def _affected_interfaces(cable):
    """Yield all Interface objects on either end of *cable*.

    Uses the Phase-1-confirmed API: cable.a_terminations and
    cable.b_terminations are lists of termination objects (Interface, Circuit,
    etc.) — not querysets, not CableTermination wrappers.
    """
    from dcim.models import Interface as _Interface

    for iface in list(cable.a_terminations) + list(cable.b_terminations):
        if isinstance(iface, _Interface):
            yield iface


def _recompute_one(interface, templates):
    """Recompute the description for *interface* if it is managed by a sentinel.

    Idempotent: no write if the current value already matches.
    """
    from .derived_intent import compute_description, is_managed_description

    match = is_managed_description(interface.description or "", templates)
    if match is None:
        return
    try:
        new_value = compute_description(interface, match)
    except Exception as exc:  # noqa: BLE001 — signal handler must not propagate; compute_description traverses cable topology and can raise arbitrary ORM/template exceptions
        logger.exception(
            "derived_intent.compute_failed field=description_from_cable interface_id=%s exc=%s",
            interface.pk,
            exc,
        )
        return
    if new_value is None:
        return  # skip-logged inside compute_description
    if interface.description == new_value:
        return  # idempotent — terminates signal chain
    interface.description = new_value
    interface.save(update_fields=["description"])


def _recompute_on_cable_change(sender, instance, **kwargs):
    """Recompute descriptions for both ends of a cable after it is saved."""
    templates = _templates()
    if not templates:
        return
    for iface in _affected_interfaces(instance):
        _recompute_one(iface, templates)


def _recompute_on_cable_delete(sender, instance, **kwargs):
    """Recompute descriptions for cable endpoints after the cable is deleted.

    Django populates termination FK values in post_delete, so
    ``_affected_interfaces`` still works here.
    """
    templates = _templates()
    if not templates:
        return
    for iface in _affected_interfaces(instance):
        # The termination objects retained by Django's post_delete signal still carry
        # the deleted cable's PK. NetBox's cached ``link_peers`` property would follow
        # that stale FK and raise Cable.DoesNotExist instead of seeing a disconnected
        # interface. Mirror the database's post-delete state on the in-memory object.
        iface.cable_id = None
        iface._state.fields_cache.pop("cable", None)
        iface.__dict__.pop("link_peers", None)
        _recompute_one(iface, templates)


def _recompute_on_interface_save(sender, instance, created, **kwargs):
    """Recompute description when an interface is saved (description may have changed)."""
    if _is_adapter_origin_write():
        return  # adapter import — not an operator edit; don't recompute derived intent
    templates = _templates()
    if not templates:
        return
    _recompute_one(instance, templates)


def _stash_interface_old_values(sender, instance, **kwargs):
    """Capture pre-save description/enabled for the Decision-G edit signal.

    Lets :func:`_push_intent_on_interface_edit` tell which attribute the operator
    actually changed. Without it, every save would promote *every* managed attribute
    — so editing the description would silently own/accept ``enabled`` too (a value
    the operator never accepted).
    """
    if not instance.pk:
        instance._nso_old_values = None
        return
    instance._nso_old_values = sender.objects.filter(pk=instance.pk).values("description", "enabled").first()


@_skip_on_render
def _push_intent_on_interface_edit(sender, instance, created, **kwargs):
    """Treat direct edits to description/enabled on managed interfaces as intent.

    Only the attribute(s) the operator actually CHANGED in this save are promoted —
    determined by comparing against the pre-save snapshot captured in
    :func:`_stash_interface_old_values`. A changed attribute becomes owned (NetBox
    is the source of truth) and pending apply (its value now differs from the
    device). Untouched attributes are left exactly as they were.

    Decision G (activated in Phase 2): editing description/enabled on a managed
    interface IS an intent change — identical to an explicit Accept action.

    Adapter-origin writes (imports/applies) are skipped: importing a value is not
    an operator accept, and re-promoting + pushing it back would both corrupt the
    imported→accepted gate and, during a bulk sync, fire one full-device intent
    push per interface (the device-27 sync wall).
    """
    if created:
        return  # new interface — nothing to accept yet

    if _is_adapter_origin_write():
        return  # import/apply, not an operator intent edit

    old_values = getattr(instance, "_nso_old_values", None)
    if old_values is None:
        # No pre-save snapshot (signal not wired / programmatic save). Be
        # conservative and own nothing, rather than risk adopting untouched
        # attributes — the pre_save handler supplies this for every real edit.
        return

    from .models import NSODeviceManagement, NSOInterfaceState
    from .summary import matches_device_value

    try:
        mgmt = NSODeviceManagement.objects.get(device_id=instance.device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    now = timezone.now()
    updated = False
    for attribute in ("description", "enabled"):
        if attribute not in mgmt.managed_attributes:
            continue
        new_value = instance.description if attribute == "description" else instance.enabled
        if new_value == old_values.get(attribute):
            continue  # operator did not change this attribute — leave it untouched
        state = NSOInterfaceState.objects.filter(
            interface=instance,
            attribute=attribute,
        ).first()
        if state is None:
            continue
        # Operator changed this attribute → NetBox owns it. Whether it is "pending
        # apply" depends on the value: editing it back to the device's value (e.g.
        # flip enabled off then on) leaves nothing to apply → in_sync, not accepted.
        state.status = "in_sync" if matches_device_value(attribute, new_value, state.nso_value) else "accepted"
        if state.accepted_at is None:
            state.accepted_at = now
        state.save(update_fields=["status", "accepted_at"])
        updated = True

    if not updated:
        return

    device_id = instance.device_id
    _schedule_intent_push((device_id, "interface"))


def _create_greenfield_subif_state(sender, instance, created, **kwargs):
    """Own + push a routed sub-interface the operator just created in NetBox (greenfield write).

    Mirror of the greenfield IP path: a NEW virtual sub-interface (``parent`` set, a numeric
    dot1q tag in the name suffix — ``ae99.999`` / ``Gi0/1.100``) on a managed device becomes
    owned intent immediately, so the subinterface-reconciler creates ``<parent> unit <n>
    vlan-id <n>`` (+ flexible-vlan-tagging / encapsulation flexible-ethernet-services on Junos —
    see junos-flexible-ethernet-services-dep) on the device. Adapter-origin writes
    (imports/applies) are skipped — importing a subif from the device is not an operator create.
    Nokia ``:`` logical subifs ride the IP-path greenfield binding, so only ``.``<digits>
    (Junos/IOS) is handled here.
    """
    if not created or _is_adapter_origin_write() or instance.parent_id is None:
        return
    name = instance.name or ""
    if "." not in name:
        return
    suffix = name.rsplit(".", 1)[1]
    if not suffix.isdigit():
        return

    from .models import NSODeviceManagement, NSOSubinterfaceState

    try:
        mgmt = NSODeviceManagement.objects.get(device_id=instance.device_id)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return

    # Brand-new interface → no prior state; create + own it. The post_save signal on the
    # new state pushes the device's full subinterface intent snapshot to the adapter.
    NSOSubinterfaceState.objects.create(
        management=mgmt,
        interface=instance,
        parent_interface=instance.parent,
        dot1q_vlan=int(suffix),
        status="accepted",
        accepted_at=timezone.now(),
    )


def _push_ip_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IP intent snapshot for a device.

    A forced claim (provisioning) re-sends this snapshot whatever the acknowledged
    baseline says, so a computed intent always lands.
    """
    from . import adapter_client as client
    from .models import NSOInterfaceIPState

    ip_states = NSOInterfaceIPState.objects.filter(
        interface__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface", "interface__parent")

    addresses = []
    for ip_state in ip_states:
        entry = {
            "interface": ip_state.interface.name,
            "address": ip_state.address,
            "family": ip_state.family,
            "secondary": bool(ip_state.secondary),
            "vrf": ip_state.vrf,
            "accepted_at": ip_state.accepted_at.isoformat() if ip_state.accepted_at else None,
        }
        entry.update(_nokia_routed_binding(ip_state.interface))
        addresses.append(entry)

    _push_changed((device_id, "ip"), addresses, lambda body: client.put_ip_intent(adapter_device_id, body))


def _nokia_routed_binding(interface) -> dict:
    """Derive the SR OS binding for a greenfield Nokia routed sub-interface.

    Nokia routed logical interfaces are modelled in NetBox by their LOGICAL name
    (``LAG99:99``), type=virtual, ``parent`` = the bound port/LAG (``lag-99``); the dot1q
    tag is the numeric suffix after ``:``. When both are present we emit ``parent_binding``/
    ``encap_tag``/``routed`` so the adapter can materialise the routed interface for an
    operator-created (never-imported) sub-interface and the apply writes
    ``router Base interface LAG99:99 port lag-99:99``. The ``:tag`` convention is Nokia-only
    (IOS/Junos sub-interfaces use ``.``), so this is a no-op for non-Nokia interfaces.
    """
    name = interface.name or ""
    parent = interface.parent
    if parent is None or ":" not in name:
        return {}
    tag = name.rsplit(":", 1)[1]
    if not tag.isdigit():
        return {}
    return {"routed": True, "parent_binding": parent.name, "encap_tag": tag}


def snmp_v3_user_push_blocker(row) -> str:
    """Why *row* cannot be faithfully pushed, or "" when it can.

    The read mirror reports only WHETHER a v3 user holds auth/priv secrets
    (``has_auth_secret``/``has_priv_secret``) — never which protocols they use; the
    protocols are operator intent, entered on the row. Pushing a user whose device-held
    secret has no declared protocol emits ``auth_protocol: null``, and the apply then
    rewrites an authPriv user as **noAuthNoPriv** — a silent security DOWNGRADE of the live
    device. Such a row is not pushable; it must be surfaced, not quietly degraded.
    """
    if row.has_auth_secret and not row.auth_protocol:
        return (
            "the device reports an authentication secret for this user but no auth protocol is set — "
            "pushing would rewrite it as noAuthNoPriv. Set the auth protocol (and its Vault ref) first."
        )
    if row.has_priv_secret and not row.priv_protocol:
        return (
            "the device reports a privacy secret for this user but no priv protocol is set — "
            "pushing would rewrite it without privacy. Set the priv protocol (and its Vault ref) first."
        )
    if row.priv_protocol and not row.auth_protocol:
        return "privacy requires authentication — set an auth protocol as well."
    return ""


def _host_is_v3(version) -> bool:
    """Report whether a trap host runs SNMPv3 — tolerating BOTH spellings of the version.

    This is not pedantry, it is the bug. The reconciler stores `version` VERBATIM from the adapter,
    which carries the NED's grain — `"3"` — while the NetBox-side forms and fixtures say `"v3"`. The
    old `row.version == "v3"` check therefore matched a hand-created row and NEVER an imported one:
    every v3 trap host actually read off a device sailed straight past the refusal that exists to
    stop it, and got pushed with an EMPTY community_or_user. IOS-XR cannot even form the key from
    that (the user is the third key component); IOS would write a host bound to no user at all.
    """
    return canonical_snmp_version(version) == "3"


def snmp_host_push_blocker(row) -> str:
    """Why *row* cannot be faithfully pushed, or "" when it can.

    put_snmp_intent is a FULL-REPLACE snapshot, so a host left out of it is not merely
    unapplied — it is a shrink, and the adapter detaches it from intent. Dropping a v3 host
    with only a server-side log line therefore left an 'accepted' row that looked green in
    the tab forever while nothing had been (or could be) applied.
    """
    if _host_is_v3(row.version) and not row.username:
        # CR-P16 made v3 hosts pushable by exporting the user name. The refusal stays for the one
        # case that is still unpushable: a v3 host whose user name we do not have (an older row
        # imported before the export carried it, or a device that never had one). Both writers KEY
        # the receiver on that field, so pushing it would key the host on an EMPTY user — IOS-XR
        # cannot even form the key, and IOS would write a host bound to no user at all.
        return (
            "this SNMPv3 trap host has no security user name — the NSO writers key the receiver on "
            "it, so pushing would key the host on an empty user. Re-import the device (the user "
            "name is read from it) or set one."
        )
    return ""


def _drop_unpushable_snmp_rows(model, rows, blocker):
    """Split *rows* into (pushable, blocked) and surface each blocked row as an error.

    ``.update()`` (not ``.save()``) — a save would re-enter the post_save receiver and
    schedule another push of the snapshot we are building right now.
    """
    pushable, blocked = [], []
    for row in rows:
        reason = blocker(row)
        if reason:
            blocked.append(row)
            logger.warning("SNMP intent: %s excluded from the snapshot — %s", row, reason)
        else:
            pushable.append(row)
    if blocked:
        model.objects.filter(pk__in=[r.pk for r in blocked]).exclude(status="error").update(status="error")
    return pushable


def _push_snmp_intent_for_device(device_id, adapter_device_id):
    """Build and push the full SNMP intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOSnmpCommunityState, NSOSnmpHostState, NSOSnmpSystemInfoState, NSOSnmpV3UserState

    communities = []
    for row in NSOSnmpCommunityState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("management"):
        if not row.vault_ref:
            continue
        communities.append(
            {
                "label": row.community_hash,  # use hash as stable label
                "vault_ref": row.vault_ref,
                "access": row.access,
                "acl": row.acl or None,
            }
        )

    v3_users = []
    owned_v3 = [
        row
        for row in NSOSnmpV3UserState.objects.filter(
            management__device_id=device_id,
            status__in=_OWNED_PUSH_STATUSES,
        )
        if row.vault_ref
    ]
    for row in _drop_unpushable_snmp_rows(NSOSnmpV3UserState, owned_v3, snmp_v3_user_push_blocker):
        # vault_ref is a PATH ref ("mount/path"); the auth/priv fields live at
        # "#auth"/"#priv" by convention. A leg without its protocol is not
        # derivable on-device, so its ref is withheld (the reconciler would
        # otherwise resolve a secret it cannot apply). snmp_v3_user_push_blocker
        # has already rejected the case where the DEVICE holds a secret whose
        # protocol was never declared — withholding there would downgrade the user.
        v3_users.append(
            {
                "username": row.username,
                "group": row.group_name or None,
                "auth_protocol": row.auth_protocol or None,
                "priv_protocol": row.priv_protocol or None,
                "auth_vault_ref": f"{row.vault_ref}#auth" if row.auth_protocol else None,
                "priv_vault_ref": f"{row.vault_ref}#priv" if row.priv_protocol else None,
            }
        )

    ned_id = _ned_id_for_device(device_id)
    hosts = []
    owned_hosts = list(
        NSOSnmpHostState.objects.filter(
            management__device_id=device_id,
            status__in=_OWNED_PUSH_STATUSES,
        )
    )
    for row in _drop_unpushable_snmp_rows(NSOSnmpHostState, owned_hosts, snmp_host_push_blocker):
        host = {
            "address": row.address,
            "version": row.version,
            "notify_type": row.notify_type,
            # ONE NED field, two meanings — which is the whole reason v3 hosts were unpushable
            # (CR-P16). On v1/v2c it is the community (referenced by its label); on v3 it is the
            # security user name, which both host writers key the receiver on.
            "community_or_user": (row.username if _host_is_v3(row.version) else row.community_hash) or "",
        }
        suppress_default_port = row.port == 162 and ned_id.startswith(
            ("timos", "arcos-", "cisco-ios-cli", "cisco-iosxe-cli")
        )
        if row.port is not None and not suppress_default_port:
            host["port"] = row.port
        hosts.append(host)

    system_info = None
    try:
        sysinfo = NSOSnmpSystemInfoState.objects.get(
            management__device_id=device_id,
        )
        if sysinfo.status in _OWNED_PUSH_STATUSES:
            system_info = {
                "location": sysinfo.location or None,
                "contact": sysinfo.contact or None,
            }
    except NSOSnmpSystemInfoState.DoesNotExist:
        pass

    _push_changed(
        (device_id, "snmp"),
        [communities, v3_users, hosts, system_info],
        lambda body: client.put_snmp_intent(adapter_device_id, *body),
    )


@_skip_on_render
def _on_snmp_state_save(sender, instance, **kwargs):
    """Push SNMP intent whenever an SNMP state row is saved (accept triggers push)."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "snmp"))


def _push_logging_intent_for_device(device_id, adapter_device_id):
    """Build and push the full logging intent snapshot (hosts + local levels) for a device.

    Store-only (deferred): the device commit happens on the single device Apply via
    the adapter's logging-reconciler. Only owned rows are included. The local-levels
    singleton rides along presence-sensitively: a dict of set severities when the
    row is owned, else an explicit null — which the adapter reads as "un-manage"
    (delete the levels intent + retract the owned leaves). This is what makes
    un-accept an actual retraction rather than a stale-intent leak.
    """
    from . import adapter_client as client
    from .models import NSOLoggingHostState, NSOLoggingLevelState
    from .template_content import _canonical_logging_intent_field

    ned_id = _ned_id_for_device(device_id)
    hosts = []
    for row in NSOLoggingHostState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        host = {
            "address": row.address,
            "severity": _canonical_logging_intent_field(ned_id, "severity", row.severity or ""),
            "facility": _canonical_logging_intent_field(ned_id, "facility", row.facility or ""),
            "transport": row.transport or "",
            "vrf": row.vrf or "",
            "source": row.source or "",
        }
        suppress_default_port = row.port == 514 and ned_id.startswith(("timos", "arcos-"))
        if row.port is not None and not suppress_default_port:
            host["port"] = row.port
        hosts.append(host)

    local_levels = None
    levels_row = NSOLoggingLevelState.objects.filter(management__device_id=device_id).first()
    if levels_row is not None and levels_row.status in _OWNED_PUSH_STATUSES:
        # An owned row with every severity blank manages nothing → null (un-manage);
        # the #83 cleared-owned-scalar shape, same as deleting the row.
        local_levels = levels_row.set_severities() or None

    _push_changed(
        (device_id, "logging"),
        [hosts, local_levels],
        lambda body: client.put_logging_intent(adapter_device_id, *body),
    )


@_skip_on_render
def _on_logging_state_save(sender, instance, **kwargs):
    """Push logging intent whenever an NSOLogging*State row is saved (accept → push)."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "logging"))


def _push_svi_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned SVI/IRB intent snapshot for a device.

    Store-only (deferred): the single device Apply commits via the svi-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.

    A forced Apply claim re-sends this snapshot whatever the acknowledged baseline says,
    so an owned row whose adapter intent went stale or empty is applied instead of skipped.
    """
    from . import adapter_client as client
    from .models import NSOSVIState

    interfaces = []
    for row in NSOSVIState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface", "vlan"):
        vid = row.vlan.vid if row.vlan else None
        if vid is None:
            continue  # the svi-reconciler keys on a VLAN id
        interfaces.append(
            {
                "interface_name": row.interface.name,
                "vlan_id": vid,
                "type": row.svi_type or "svi",
                "vrf": row.vrf or "",
            }
        )

    _push_changed(
        (device_id, "svi"),
        interfaces,
        lambda body: client.put_svi_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_svi_state_save(sender, instance, **kwargs):
    """Push SVI intent whenever an NSOSVIState row is saved (accept triggers push)."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "svi"))


def _push_subinterface_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned dot1q subinterface intent snapshot.

    Store-only (deferred): the single device Apply commits via the
    subinterface-reconciler. Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) included.

    A forced Apply claim re-sends this snapshot whatever the acknowledged baseline says,
    so an owned row whose adapter intent went stale or empty is applied instead of skipped.
    """
    from . import adapter_client as client
    from .models import NSOSubinterfaceState

    interfaces = []
    for row in NSOSubinterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface", "parent_interface"):
        # The subinterface-reconciler keys on the dot1q tag and (for Junos) the
        # parent interface — skip rows missing either rather than emit a bad payload.
        if row.dot1q_vlan is None or row.parent_interface is None:
            continue
        interfaces.append(
            {
                "interface_name": row.interface.name,
                "parent_interface": row.parent_interface.name,
                "dot1q_vlan": row.dot1q_vlan,
                "type": "subinterface",
                "vrf": row.vrf or "",
            }
        )

    _push_changed(
        (device_id, "subinterface"),
        interfaces,
        lambda body: client.put_subinterface_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_subinterface_state_save(sender, instance, **kwargs):
    """Push subinterface intent whenever an NSOSubinterfaceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "subinterface"))


def _push_interface_mtu_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned per-interface MTU intent snapshot (Phase 2b).

    Store-only (deferred): the single device Apply commits via the mtu-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.

    A forced Apply claim re-sends this snapshot whatever the acknowledged baseline says,
    so an owned row whose adapter intent went stale or empty is applied instead of skipped.
    """
    from . import adapter_client as client
    from .models import NSOInterfaceMtuState

    interfaces = []
    for row in NSOInterfaceMtuState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface"):
        # At least one MTU value must be set or the reconciler has nothing to write.
        if row.l2_mtu is None and row.ip_mtu is None and row.mpls_mtu is None:
            continue
        interfaces.append(
            {
                "interface_name": row.interface.name,
                "mtu": row.l2_mtu,
                "ip_mtu": row.ip_mtu,
                "mpls_mtu": row.mpls_mtu,
            }
        )

    _push_changed(
        (device_id, "interface_mtu"),
        interfaces,
        lambda body: client.put_interface_mtu_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_mtu_state_save(sender, instance, **kwargs):
    """Push MTU intent whenever an NSOInterfaceMtuState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "interface_mtu"))


def _push_vlan_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned VLAN-database intent snapshot for a device (write).

    Store-only (deferred): the single device Apply commits via the vlan-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included; the VLAN name pushed
    is the LIVE NetBox name (operator is the source of truth for it).

    The single Apply takes a forced claim, so a VLAN renamed in NetBox *after* it was
    accepted (the rename touches ipam.VLAN, which fires no plugin signal) still reaches
    the device.
    """
    from . import adapter_client as client
    from .models import NSOVLANState
    from .vlan_reconciler import is_placeholder_vlan_name

    vlans = []
    for row in NSOVLANState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("vlan"):
        if row.vlan is None:
            continue
        # A nameless device VLAN was imported under a fabricated "VLAN <vid>" placeholder
        # (NetBox cannot hold two name='' VLANs in one group). Pushing it verbatim would
        # write a name the device never had; the writer omits the name when it is empty.
        name = "" if is_placeholder_vlan_name(row) else (row.vlan.name or "")
        vlans.append({"vlan_id": row.vlan.vid, "name": name})

    _push_changed(
        (device_id, "vlan"),
        vlans,
        lambda body: client.put_vlan_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_vlan_state_save(sender, instance, **kwargs):
    """Push VLAN intent whenever an NSOVLANState row is saved (accept triggers push)."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "vlan"))


@_skip_on_render
def _on_vlan_change(sender, instance, **kwargs):
    """Surface a NetBox VLAN rename as overlay drift immediately (visibility only).

    Renaming an ``ipam.VLAN`` fires no NSOVLANState signal, so the overlay would
    otherwise sit at a stale ``in_sync``/``imported`` until the next full reconcile.
    Re-evaluate each linked overlay's drift here (the editable value is the VLAN
    name, compared against the device-observed name) so a rename shows as ``changed``
    (unowned) / re-pends to ``accepted`` (owned) right away — and renaming back to the
    device value clears the drift. The device push still happens on Apply (force-push),
    so this stays side-effect free under suppress_intent_push().
    """
    from . import status_machine as sm

    states = list(instance.nso_vlan_states.all())
    if not states:
        return
    with suppress_intent_push():
        for state in states:
            matches = (not state.device_name) or instance.name == state.device_name
            new_status = sm.on_reconcile(state.status, matches=matches)
            if new_status != state.status:
                state.status = new_status
                state.save(update_fields=["status"])


@_skip_on_render
def _on_ipam_vlan_pre_delete(sender, instance, **kwargs):
    """VLAN deleted in NetBox → push the reduced VLAN intent to each attached device.

    Deleting an ipam.VLAN cascade-deletes its NSOVLANState overlays but fires no
    per-overlay signal, so without this the device keeps the (now-orphaned) VLAN.
    Capture the attached devices *before* the cascade, then schedule a deferred push;
    by the time it runs (post-commit) the overlays are gone, so the snapshot omits this
    vid and the adapter PUT-replaces the vlan-reconciler instance → FASTMAP reverts it.
    """
    targets = []
    for state in instance.nso_vlan_states.select_related("management").all():
        mgmt = state.management
        if mgmt.adapter_device_id is not None:
            targets.append((mgmt.device_id, mgmt.adapter_device_id))
    for device_id, adapter_device_id in targets:
        _schedule_intent_push((device_id, "vlan"))


def _push_bfd_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned per-interface BFD intent snapshot for a device.

    Store-only (deferred): the single device Apply commits via the bfd-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.

    A forced Apply claim re-sends this snapshot whatever the acknowledged baseline says,
    so an owned row whose adapter intent went stale or empty is applied instead of skipped.
    """
    from . import adapter_client as client
    from .models import NSOBFDInterfaceState

    interfaces = []
    for row in NSOBFDInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface"):
        interfaces.append(
            {
                "interface_name": row.interface.name,
                "min_tx": row.min_tx,
                "min_rx": row.min_rx,
                "multiplier": row.multiplier,
                "micro_bfd": bool(row.micro_bfd),
            }
        )

    _push_changed(
        (device_id, "bfd"),
        interfaces,
        lambda body: client.put_bfd_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_bfd_state_save(sender, instance, **kwargs):
    """Push BFD intent whenever an NSOBFDInterfaceState row is saved (accept triggers push)."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "bfd"))


def _on_ip_address_pre_save(sender, instance, **kwargs):
    """Stash the IPAddress's pre-save interface binding for the reassignment cleanup.

    A GenericForeignKey change fires a single post_save keyed on the NEW interface; without
    the previous binding, the OLD interface's ``NSOInterfaceIPState`` is orphaned and its
    device keeps an IP NetBox just moved away. Not ``@_skip_on_render``: it only reads + stashes
    on the instance (no push), and post_save's own guard decides whether the cleanup runs.
    """
    from dcim.models import Interface as _Interface

    instance._nso_prev_ip_binding = None
    if not instance.pk:
        return
    try:
        prev = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return
    prev_assigned = prev.assigned_object
    if isinstance(prev_assigned, _Interface):
        instance._nso_prev_ip_binding = (prev_assigned, str(prev.address), prev.vrf.name if prev.vrf else "")


def _cleanup_reassigned_ip_overlay(instance) -> None:
    """Drop the OLD interface's IP overlay + push the reduced intent when an IP moved.

    Fires when an IPAddress's interface/address/vrf changed vs its pre-save binding (captured by
    :func:`_on_ip_address_pre_save`): the overlay keyed on the OLD (interface, address, vrf) is
    orphaned, so delete it and full-replace-push the OLD device's IP intent (which drops it on the
    device). The new binding's overlay is (re)created by the normal post_save path.
    """
    from dcim.models import Interface as _Interface

    from .models import NSODeviceManagement, NSOInterfaceIPState

    prev = getattr(instance, "_nso_prev_ip_binding", None)
    if not prev:
        return
    prev_iface, prev_addr, prev_vrf = prev
    assigned = instance.assigned_object
    cur_addr = str(instance.address)
    cur_vrf = instance.vrf.name if instance.vrf else ""
    if (
        isinstance(assigned, _Interface)
        and assigned.pk == prev_iface.pk
        and prev_addr == cur_addr
        and prev_vrf == cur_vrf
    ):
        return  # same binding → not a move, nothing to clean
    try:
        mgmt = NSODeviceManagement.objects.get(device_id=prev_iface.device_id)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    deleted, _ = NSOInterfaceIPState.objects.filter(interface=prev_iface, address=prev_addr, vrf=prev_vrf).delete()
    if not deleted:
        return
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "ip"))


@_skip_on_render
def _on_ip_address_change(sender, instance, **kwargs):
    """Push IP intent when an IPAddress assigned to a managed interface changes.

    Decorated ``@_skip_on_render`` like every other push handler so that
    ``suppress_intent_push()`` (the rqworker reconcile/import guard) also silences
    the IP path — otherwise a reconciler saving an interface IP would push intent
    back to the adapter and force-promote imported rows to ``accepted``. The
    P2P-pair guard (``_p2p_allocation_active``) is orthogonal and stays below.
    """
    from dcim.models import Interface as _Interface

    from .models import NSODeviceManagement, NSOInterfaceIPState

    # If this IP was reassigned off another interface (or unassigned), drop the OLD
    # interface's overlay first so it isn't stranded (device keeps a moved-away IP).
    _cleanup_reassigned_ip_overlay(instance)

    assigned = instance.assigned_object
    if not isinstance(assigned, _Interface):
        return

    device_id = assigned.device_id

    try:
        mgmt = NSODeviceManagement.objects.get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    addr_str = str(instance.address)  # "ip/plen"
    vrf_name = instance.vrf.name if instance.vrf else ""
    family = "ipv6" if ":" in addr_str.split("/")[0] else "ipv4"

    ip_state, created = NSOInterfaceIPState.objects.get_or_create(
        interface=assigned,
        address=addr_str,
        vrf=vrf_name,
        defaults={
            "status": "accepted",
            "accepted_at": timezone.now(),
            "family": family,
            "secondary": False,
        },
    )

    if not created:
        if ip_state.status == "conflict":
            logger.debug(
                "IP %s on interface %s is in conflict state; skipping intent push",
                addr_str,
                assigned.name,
            )
            return
        if ip_state.status != "accepted":
            ip_state.status = "accepted"
            ip_state.accepted_at = timezone.now()
            ip_state.save(update_fields=["status", "accepted_at"])

    if not getattr(_p2p_allocation_active, "active", False):
        _schedule_intent_push((device_id, "ip"))


@_skip_on_render
def _on_ip_address_delete(sender, instance, **kwargs):
    """Push IP intent (with the deleted IP removed) when an IPAddress is deleted.

    Decorated ``@_skip_on_render`` so ``suppress_intent_push()`` silences the push
    when a reconciler (or a rolled-back allocation) deletes an interface IP.
    """
    from dcim.models import Interface as _Interface

    from .models import NSODeviceManagement, NSOInterfaceIPState

    assigned = instance.assigned_object
    if not isinstance(assigned, _Interface):
        return

    device_id = assigned.device_id

    try:
        mgmt = NSODeviceManagement.objects.get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    addr_str = str(instance.address)
    vrf_name = instance.vrf.name if instance.vrf else ""
    NSOInterfaceIPState.objects.filter(
        interface=assigned,
        address=addr_str,
        vrf=vrf_name,
    ).delete()

    _schedule_intent_push((device_id, "ip"))


#: The overlays a static-route push actually serializes: owned, and carrying an IP next
#: hop (an interface-only next hop is not supported by static-route-reconciler v1, so the
#: snapshot has no way to express it). Read by the rollout backfill too — a row it armed
#: but the push never sent would hold a generation the adapter has never seen, which no
#: apply result can name and no later pass would re-arm (#1502 Appendix S).
PUSHED_STATIC_ROUTE_FILTER = Q(status__in=_OWNED_PUSH_STATUSES, static_route__next_hop__isnull=False)


def stored_static_route_count(response):
    """How many routes the adapter says it stored, or ``None`` when it did not say.

    One definition for every reader of a static-route push answer: the Apply promotion gate
    and the fleet re-sync both decide "acknowledged" from this, so a malformed answer cannot
    mean stored to one of them and refused to the other. Only a real row count answers —
    ``True`` is an ``int`` in Python, and no push stores a negative number of routes.
    """
    if not isinstance(response, dict):
        return None
    count = response.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def _push_static_route_intent_for_device(device_id, adapter_device_id):
    """Build and push the full static route intent snapshot for a device.

    Each route names the NetBox ``StaticRoute`` pk and the generation of the intent it
    carries: the pk is what lets the adapter tell a *replacement* from an unrelated
    delete-plus-insert, and the generation is the token an apply result is correlated
    against. ``intent_generation`` 0 is the unallocated sentinel and goes on the wire as
    NULL — the adapter adopts a generation only when non-null, so a sentinel row simply has
    nothing to correlate with instead of correlating with everything at 0.

    Records echoed fingerprints as this device's settlement expectations. The claim handles
    the adapter response after this function captures the rendered body.
    """
    from . import adapter_client as client
    from .models import NSOStaticRouteState

    routes = []
    generations: dict[int, int] = {}
    for row in NSOStaticRouteState.objects.filter(
        PUSHED_STATIC_ROUTE_FILTER, management__device_id=device_id
    ).select_related("static_route", "static_route__vrf"):
        sr = row.static_route
        vrf_name = sr.vrf.name if sr.vrf else ""
        generation = row.intent_generation or None
        route = {
            "route_id": sr.pk,
            "generation": generation,
            "vrf": vrf_name,
            "prefix": str(sr.prefix),
            "next_hop": str(sr.next_hop),
            "permanent": sr.permanent or False,
            "tag": sr.tag,
        }
        # Always sent: an omitted optional field is a CLEAR to the adapter, NED-agnostically.
        # Nokia's default preference 5 used to be suppressed here, which turned an edit
        # 3 → 5 into a clear plus a networked retract job for a value the SR OS writer
        # treats identically to None and the exporter suppresses on the way back.
        if sr.metric is not None:
            route["metric"] = sr.metric
        routes.append(route)
        if generation is not None:
            generations[sr.pk] = generation

    _push_changed(
        (device_id, "static_route"),
        routes,
        lambda body: client.put_static_route_intent(adapter_device_id, body),
        on_response=lambda resp: _record_static_route_expectations(device_id, generations, resp.get("routes") or []),
    )


def _record_static_route_expectations(device_id, generations: dict, echoes) -> None:
    """Store each echoed ``{route_id, generation, fingerprint}`` as the row's expectation.

    Written under a compare-and-set on the overlay's *current* ``intent_generation``. The
    adapter commits its store write before it answers, so an operator edit can bump the
    generation while the response is in flight; recording the stale echo would then let the
    next apply result settle content that has already been superseded.
    """
    from .models import NSOStaticRouteState

    for echo in echoes:
        if not isinstance(echo, dict):
            continue
        route_id, generation, fingerprint = echo.get("route_id"), echo.get("generation"), echo.get("fingerprint")
        if route_id is None or not fingerprint:
            continue
        if generation is None or generations.get(route_id) != generation:
            continue  # never pushed by us at this generation — not an expectation we may record
        NSOStaticRouteState.objects.filter(
            management__device_id=device_id,
            static_route_id=route_id,
            intent_generation=generation,
        ).update(expected_generation=generation, expected_fingerprint=fingerprint)


@_skip_on_render
def _on_static_route_state_save(sender, instance, **kwargs):
    """Push static route intent whenever an NSOStaticRouteState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "static_route"))


# ── Greenfield static routes (operator-created in NetBox, not yet on the device) ──
#
# The reconcile path only ever creates an NSOStaticRouteState overlay for a route the
# device already reports (brownfield adoption). These handlers add the missing direction:
# a netbox_routing.StaticRoute the operator assigns to a managed device becomes an
# *accepted* overlay (owned intent) and pushes; removing/deleting it pushes the removal
# (full-replace). All wired from the plugin against the netbox_routing model — no fork edit.


def _static_route_content(static_route) -> tuple:
    """Return the wire-visible content of *static_route* — what a push would carry.

    A delta here is new intent for every device that owns the route; a delta anywhere
    else is not. ``name`` and ``interface_next_hop`` never reach the wire for a pushable
    route, so editing them is not an intent change.
    """
    return (
        static_route.vrf_id,
        str(static_route.prefix or ""),
        str(static_route.next_hop or ""),
        static_route.metric,
        # The wire sends ``permanent or False``, so None and False are one value there —
        # treating them as a delta would bump a generation over an identical payload.
        bool(static_route.permanent),
        static_route.tag,
    )


#: Every field :func:`_arm_static_route_generation` writes. The accept paths save the row
#: with an explicit ``update_fields``, so they read this list rather than restate it — a
#: field the helper gains and a call site does not name is armed in memory and dropped.
_STATIC_ROUTE_ARMED_FIELDS = (
    "intent_generation",
    "generation_started_at",
    "last_apply_error",
    "last_result_advisory",
)


def _arm_static_route_generation(state) -> None:
    """Give *state* a fresh generation in memory — the caller saves it.

    For the accept paths, which never save the native route (so no ``pre_save`` fires and
    the content transition cannot see them) yet are still a new statement of intent: the
    result of the apply that already failed must not be able to settle the row the
    operator has just re-accepted.
    """
    from .intent_generation import allocate_intent_generation

    state.intent_generation = allocate_intent_generation()
    state.generation_started_at = timezone.now()
    # Both describe the generation just superseded.
    state.last_apply_error = ""
    state.last_result_advisory = ""


_STATIC_ROUTE_TRANSITION_FIELDS = (
    "nso_vrf",
    "nso_prefix",
    "nso_next_hop",
    "status",
    *_STATIC_ROUTE_ARMED_FIELDS,
)


def _transition_static_route_content(static_route, previous=None) -> list:
    """Re-arm every owned overlay of *static_route* as fresh, unsettled intent.

    *previous* is the pre-save content the delta is judged against; ``None`` means the
    caller already knows this is a change and wants the transition unconditionally.

    Any operator content edit — identity or not — makes every prior apply result stale.
    The row goes back to ``accepted`` (fail-closed: "pending apply"), takes a generation
    no in-flight result can name, and drops the error and advisory that described the
    generation just superseded. Leaving an edited row ``in_sync`` is a green badge over
    content the device does not have, and leaving it ``deploying`` lets the apply already
    in flight settle the *new* intent from the *old* result.

    The fan-out is resolved by querying the overlays, never through ``instance.devices``:
    the fork's form writes the row before ``devices.set()``, so at ``post_save`` the M2M
    still reads the pre-edit membership. Rows are locked in ascending management-id order
    so two edits of one shared route touching the same devices in opposite order cannot
    deadlock. ``accepted_at`` is left alone — it dates first ownership (staged_days).
    """
    from django.db import transaction

    from .models import NSOStaticRouteState

    with transaction.atomic():
        # The committed row, never the instance: a save(update_fields=…) persists only the
        # named columns, so an unsaved attribute would otherwise be mirrored and bumped as
        # intent the push — which re-queries the row — could never send.
        route_rows = type(static_route)._default_manager.filter(pk=static_route.pk).order_by("pk")
        committed = route_rows.select_for_update().first()
        if committed is None:
            return []
        if previous is not None and previous == _static_route_content(committed):
            return []  # nothing the wire carries actually changed
        vrf_name = committed.vrf.name if committed.vrf else ""
        prefix = str(committed.prefix or "")
        next_hop = str(committed.next_hop or "")
        rows = list(
            NSOStaticRouteState.objects.select_for_update()
            .filter(static_route=static_route, status__in=_OWNED_PUSH_STATUSES)
            .order_by("management_id")
        )
        for row in rows:
            row.nso_vrf = vrf_name
            row.nso_prefix = prefix
            row.nso_next_hop = next_hop
            row.status = "accepted"
            _arm_static_route_generation(row)
            # → _on_static_route_state_save, which coalesces to one push per device.
            row.save(update_fields=list(_STATIC_ROUTE_TRANSITION_FIELDS))
    return rows


def _carried_last_acked(mgmt, route_id):
    """Read the acknowledged triple a pending deletion of *route_id* hands to a new overlay.

    Both the state row's own homes and the key's unfolded entries are read, because the
    deletion may not have been folded yet: the codex sequence that needs this — delete,
    re-own, re-delete, all before the drain — has the record sitting in an entry.
    """
    from . import outbox
    from .models import NSOIntentOutboxEntry, NSOIntentOutboxState

    state = NSOIntentOutboxState.objects.filter(device_id=mgmt.device_id, scope="static_route").first()
    transitions = [
        record
        for row in NSOIntentOutboxEntry.objects.filter(
            device_id=mgmt.device_id, scope="static_route", consumed_by_push_seq__isnull=True
        ).order_by("id")
        for record in row.transitions
    ]
    return outbox.carried_triple(
        route_id,
        transitions=transitions,
        queued=(state.queued_deletions if state else ()),
        claim_deletions=(state.claim_deletions if state else ()),
        lineage_carry=(state.lineage_carry if state else None),
    )


def _accept_static_route_for_device(static_route, device) -> None:
    """Own a greenfield route for *device* (accepted overlay) → its save pushes intent."""
    from .models import NSODeviceManagement, NSOStaticRouteState

    if static_route.next_hop is None:
        return  # interface-only next-hop not supported by static-route-reconciler v1
    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    state, created = NSOStaticRouteState.objects.get_or_create(
        management=mgmt,
        static_route=static_route,
        defaults={
            "status": "accepted",
            "accepted_at": timezone.now(),
        },
    )
    if created:
        # A fresh row inherits the last acknowledged triple from its pending deletion.
        state.last_acked_triple = _carried_last_acked(mgmt, static_route.pk)
    was_owned = not created and state.status in _OWNED_PUSH_STATUSES
    if not created and not was_owned:
        state.status = "accepted"
        state.accepted_at = timezone.now()
    if not was_owned:
        # Entering ownership is intent this device did not carry before, so it needs a
        # generation of its own — an already-owned row keeps the one it is mid-flight on.
        _arm_static_route_generation(state)
    # nso_vrf too: the residue key is the (vrf, prefix, next_hop) triple, so a VRF route
    # adopted with an empty mirror never matches its own device row.
    state.nso_vrf = static_route.vrf.name if static_route.vrf else ""
    state.nso_prefix = str(static_route.prefix or "")
    state.nso_next_hop = str(static_route.next_hop or "")
    state.last_sync_at = timezone.now()
    state.save()  # → _on_static_route_state_save schedules the push
    if not was_owned:
        from . import outbox

        # Re-ownership withdraws whatever deletion authority is pending for this pk: without
        # the record, a delete/re-own/re-delete sequence would ship the deletion of a route
        # NetBox owns again.
        device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
        _schedule_intent_push((device_id, "static_route"), transitions=[outbox.revoke_transition(static_route.pk)])


def _static_route_delete_transition(row, static_route):
    """Record what is leaving, from the overlay that still mirrors it.

    The lineage leads with the triple the adapter last ACKNOWLEDGED, because a content edit
    whose push never landed leaves the adapter holding the older one; an id plus the current
    triple alone would match nothing there, be classified moot and detach the route silently.
    """
    from . import outbox

    return outbox.delete_transition(
        static_route.pk,
        last_acked=row.last_acked_triple,
        current=outbox.triple_of(row.nso_vrf, row.nso_prefix, row.nso_next_hop),
    )


def _remove_static_route_for_device(static_route, device) -> None:
    """Drop the overlay for (device, route) and push the removal (full-replace)."""
    from .models import NSODeviceManagement, NSOStaticRouteState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    rows = NSOStaticRouteState.objects.filter(management=mgmt, static_route=static_route)
    # Captured here because this is the last moment the mirror is alive: the next statement
    # deletes it, and a removed route's content cannot be re-rendered from anything else.
    transitions = [_static_route_delete_transition(row, static_route) for row in rows]
    rows.delete()
    _schedule_intent_push((mgmt.device_id, "static_route"), transitions=transitions)


def _on_routing_static_route_pre_save(sender, instance, **kwargs):
    """Stash the committed row's wire-visible content so post_save can see the delta.

    The stash lives on the instance, so a save of a *different* route on the same thread
    can never be read as this one's baseline. Re-reading on every save is deliberate:
    within one transaction a second save sees the transaction's own first write, which is
    what makes an A→B→A edit two real transitions instead of a silently swallowed one.

    The baseline is read under the row's own lock, held to COMMIT. Two concurrent edits
    would otherwise both load A; the one that lands second — writing A back over the
    first's B — would compare A against A, transition nothing and push nothing, leaving
    the adapter holding B while NetBox reads A.
    """
    from django.db import connection

    instance._nso_static_route_content = None
    # Same guard as post_save's @_skip_on_render: with no transition to feed there is no
    # baseline to read, and reconcile writes must not take a route lock.
    if _is_intent_push_suppressed() or _is_render_request() or not instance.pk:
        return
    # order_by(pk): the model's Meta ordering starts at the nullable ``vrf``, whose LEFT
    # JOIN PostgreSQL refuses to lock ("FOR UPDATE cannot be applied to the nullable side").
    rows = sender.objects.filter(pk=instance.pk).order_by("pk")
    if connection.in_atomic_block:
        rows = rows.select_for_update()  # outside a transaction there is nothing to hold it to
    previous = rows.first()
    if previous is not None:
        instance._nso_static_route_content = _static_route_content(previous)


@_skip_on_render
def _on_routing_static_route_save(sender, instance, created=False, **kwargs):
    """Re-arm every overlay owning this route when its content changed, and push.

    Delta-gated against the pre-save row: a save that touches nothing the wire carries is
    not intent and must neither bump a generation nor push. The comparison itself happens
    inside the transition, against the *committed* row. A create is left to the
    ``post_add`` that assigns the route its first devices — there is no overlay yet.
    """
    previous = getattr(instance, "_nso_static_route_content", None)
    if created or previous is None:
        return
    _transition_static_route_content(instance, previous=previous)


@_skip_on_render
def _on_routing_static_route_devices_changed(sender, instance, action, pk_set, reverse, **kwargs):
    """Device assigned to / removed from a route → own / remove + push (greenfield).

    m2m_changed is NOT a deletion-only signal, so this handler must not be registered under
    _as_delete_origin: that would stamp ``?delete_origin=true`` on the push born from an
    ADD, authorizing the adapter to retract from the live device any route the full-replace
    snapshot happens not to carry. Only the removal branches open the mark.
    """
    from dcim.models import Device

    try:
        from netbox_routing.models import StaticRoute
    except ImportError:
        return
    if reverse or not isinstance(instance, StaticRoute):
        return  # only the StaticRoute.devices side
    if action == "post_add":
        for device in Device.objects.filter(pk__in=pk_set or []):
            _accept_static_route_for_device(instance, device)
    elif action == "post_remove":
        with _delete_origin_dispatch():
            for device in Device.objects.filter(pk__in=pk_set or []):
                _remove_static_route_for_device(instance, device)
    elif action == "post_clear":
        # Django sends pk_set=None on .clear(): every device was detached. `pk_set or []`
        # would silently remove nothing, orphaning every overlay + leaving stale adapter
        # intent. Drive the removal from the overlay rows still referencing this route —
        # their devices are exactly the ones just detached.
        from .models import NSOStaticRouteState

        device_ids = set(
            NSOStaticRouteState.objects.filter(static_route=instance).values_list("management__device_id", flat=True)
        )
        with _delete_origin_dispatch():
            for device in Device.objects.filter(pk__in=device_ids):
                _remove_static_route_for_device(instance, device)


@_skip_on_render
def _on_routing_static_route_pre_delete(sender, instance, **kwargs):
    """Route deleted in NetBox → drop overlays + push removal before the cascade lands."""
    for device in instance.devices.all():
        _remove_static_route_for_device(instance, device)


# ── IS-IS Flex-Algorithm intent (process-tag scoped) ────────────────────────


def _push_isis_flex_algo_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IS-IS Flex-Algo intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOISISFlexAlgoState

    flex_algos = []
    for row in NSOISISFlexAlgoState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        flex_algos.append(
            {
                "process_tag": row.process_tag or "",
                "algo_id": int(row.algo_id),
                "metric_type": row.metric_type or None,
                "priority": row.priority,
                "admin_group_exclude": row.admin_group_exclude or None,
                "admin_group_include_any": row.admin_group_include_any or None,
                "admin_group_include_all": row.admin_group_include_all or None,
            }
        )

    _push_changed(
        (device_id, "isis_flex_algo"),
        flex_algos,
        lambda body: client.put_isis_flex_algo_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_isis_flex_algo_state_save(sender, instance, **kwargs):
    """Push Flex-Algo intent whenever an NSOISISFlexAlgoState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "isis_flex_algo"))


# ── Greenfield Flex-Algo (operator-created in NetBox, not yet on the device) ──
#
# ISISFlexAlgo belongs to an ISISInstance which carries the device + process-tag,
# so a flex-algo the operator creates becomes an *accepted* overlay (owned intent)
# and pushes; editing re-pushes; deleting pushes the removal (full-replace).


def _accept_isis_flex_algo(flex_algo) -> None:
    """Own a greenfield flex-algo (accepted overlay) → its save pushes intent."""
    from .models import NSODeviceManagement, NSOISISFlexAlgoState

    inst = flex_algo.instance
    device = getattr(inst, "device", None)
    if device is None:
        return
    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    state, created = NSOISISFlexAlgoState.objects.get_or_create(
        management=mgmt,
        process_tag=inst.process_tag or "",
        algo_id=int(flex_algo.algo_id),
        defaults={
            "isis_flex_algo": flex_algo,
            "status": "accepted",
            "accepted_at": timezone.now(),
        },
    )
    if not created and state.status not in ("accepted", "deploying", "in_sync", "apply_failed"):
        state.status = "accepted"
        state.accepted_at = timezone.now()
    state.isis_flex_algo = flex_algo
    state.metric_type = flex_algo.metric_type or ""
    state.priority = flex_algo.priority
    state.admin_group_exclude = flex_algo.admin_group_exclude or ""
    state.admin_group_include_any = flex_algo.admin_group_include_any or ""
    state.admin_group_include_all = flex_algo.admin_group_include_all or ""
    state.last_sync_at = timezone.now()
    state.save()  # → _on_isis_flex_algo_state_save schedules the push


def _remove_isis_flex_algo(flex_algo) -> None:
    """Drop the overlay for this flex-algo and push the removal (full-replace)."""
    from .models import NSODeviceManagement, NSOISISFlexAlgoState

    inst = flex_algo.instance
    device = getattr(inst, "device", None)
    if device is None:
        return
    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    NSOISISFlexAlgoState.objects.filter(
        management=mgmt, process_tag=inst.process_tag or "", algo_id=int(flex_algo.algo_id)
    ).delete()
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "isis_flex_algo"))


@_skip_on_render
def _on_routing_isis_flex_algo_save(sender, instance, **kwargs):
    """Flex-algo created/edited in NetBox → own it (accepted overlay) + push."""
    _accept_isis_flex_algo(instance)


@_skip_on_render
def _on_routing_isis_flex_algo_pre_delete(sender, instance, **kwargs):
    """Flex-algo deleted in NetBox → drop overlay + push removal before the cascade."""
    _remove_isis_flex_algo(instance)


def _push_l2_sap_intent_for_device(device_id, adapter_device_id):
    """Build and push the full Nokia L2 SAP intent snapshot for a device.

    A forced Apply claim re-sends this snapshot whatever the acknowledged baseline says,
    so an owned row whose adapter intent went stale or empty is applied instead of skipped.
    """
    from . import adapter_client as client
    from .models import NSOL2SapState

    saps = []
    for row in NSOL2SapState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        saps.append(
            {
                "service_name": row.service_name,
                "service_type": row.service_type,
                "sap_id": row.sap_id,
                "port": row.port,
                "outer_tag": row.outer_tag,
                "inner_tag": row.inner_tag,
            }
        )

    _push_changed(
        (device_id, "l2_sap"),
        saps,
        lambda body: client.put_l2_sap_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_l2_sap_state_save(sender, instance, **kwargs):
    """Push L2 SAP intent whenever an NSOL2SapState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "l2_sap"))


def _push_lacp_intent_for_device(device_id, adapter_device_id):
    """Build and push (apply) the full LACP bundle intent snapshot for a device.

    Committing LACP is a device write, so on accept it only fires when the device
    is in auto-apply mode (see _on_lacp_state_save); the manual device Apply forces the
    owned snapshot out as part of the one Apply.
    """
    from . import adapter_client as client
    from .models import NSOLACPBundleState, NSOLACPMemberState

    _owned = ("accepted", "deploying", "in_sync")
    bundles = []
    # NX-P2 belt-and-suspenders: a vPC-protected bundle can never be owned (the Accept view
    # refuses it), but exclude it here too so it can NEVER enter the write intent — the writer
    # refuses the whole service on a vPC bundle, which would block the legitimate bundles.
    for b in (
        NSOLACPBundleState.objects.filter(management__device_id=device_id, status__in=_owned)
        .exclude(vpc_sensitive=True)
        .select_related("interface")
    ):
        members = []
        for m in NSOLACPMemberState.objects.filter(
            management__device_id=device_id, lag_bundle=b.interface, status__in=_owned
        ).select_related("interface"):
            members.append({"interface_name": m.interface.name, "mode": m.mode, "port_priority": m.port_priority})
        bundles.append(
            {
                "name": b.interface.name,
                "lag_id": b.lag_id,
                "min_links": b.min_links,
                "system_priority": b.system_priority,
                "system_id": b.system_id,
                "timer": b.timer,
                "admin_key": b.admin_key,
                "members": members,
            }
        )

    _push_changed(
        (device_id, "lacp"),
        bundles,
        lambda body: client.apply_lag_config(adapter_device_id, body),
    )


@_skip_on_render
def _on_lacp_state_save(sender, instance, **kwargs):
    """On accept, commit LACP to the device only in auto-apply mode.

    Without auto-apply this is deferred: accept just marks the rows owned and the
    single device Apply commits them (one flow, matching every other scope).
    """
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None or not mgmt.auto_apply:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "lacp"))


# NetBox interface mode -> NSO switchport vocabulary.
_NETBOX_TO_NSO_MODE = {"access": "access", "tagged": "trunk", "tagged-all": "trunk-all"}


def _push_switchport_intent_for_device(device_id, adapter_device_id):
    """Build and push (apply) the device's owned L2 switchport snapshot.

    A device write, so on accept it only fires in auto-apply mode; the manual
    device Apply forces it out as part of the single Apply.
    """
    from . import adapter_client as client
    from .models import NSOSwitchportState

    interfaces = []
    for st in NSOSwitchportState.objects.filter(
        management__device_id=device_id, status__in=("accepted", "deploying", "in_sync")
    ).select_related("interface", "untagged_vlan"):
        interfaces.append(
            {
                "interface_name": st.interface.name,
                "mode": _NETBOX_TO_NSO_MODE.get(st.mode or "", st.mode or ""),
                "untagged_vlan": st.untagged_vlan.vid if st.untagged_vlan else None,
                "tagged_vlans": sorted(v.vid for v in st.tagged_vlans.all()),
            }
        )

    _push_changed(
        (device_id, "switchport"),
        interfaces,
        lambda body: client.apply_switchport_config(adapter_device_id, body),
    )


@_skip_on_render
def _on_switchport_state_save(sender, instance, **kwargs):
    """On accept, commit switchport to the device only in auto-apply mode.

    Without auto-apply this is deferred: accept marks the row owned and the single
    device Apply commits it (one flow, matching every other scope).
    """
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None or not mgmt.auto_apply:
        return
    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "switchport"))


def _isis_levels_for_state(state):
    """Per-level tuning rows of the state's linked netbox_routing instance.

    A level is accepted with its process: the fork ISISLevel rows (operator-editable)
    ride the owned instance's push. None fields are omitted per entry; a knobless row
    contributes nothing. No-op when the fork (or the link) is absent.
    """
    inst = getattr(state, "isis_instance", None)
    if inst is None:
        return []
    try:
        from netbox_routing.models import ISISLevel
    except ImportError:
        return []
    out = []
    for lv in ISISLevel.objects.filter(instance=inst).order_by("level"):
        entry = {"level": int(lv.level)}
        if lv.wide_metrics_only is not None:
            entry["wide_metrics_only"] = lv.wide_metrics_only
        if getattr(lv, "labeled_preference", None) is not None:
            entry["labeled_preference"] = lv.labeled_preference
        if lv.disabled is not None:
            entry["disabled"] = lv.disabled
        if len(entry) > 1:
            out.append(entry)
    return out


def _push_isis_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IS-IS intent snapshot (interfaces + processes) for a device.

    A forced claim (provisioning) re-sends this snapshot whatever the acknowledged
    baseline says, so a computed intent always lands.
    """
    from . import adapter_client as client
    from .models import NSOISISInstanceState, NSOISISInterfaceState

    redist_by_proc = _collect_redistribution_by_dest_ref(device_id, "isis")

    interfaces = []
    for row in NSOISISInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface"):
        interfaces.append(
            {
                "interface_name": row.interface.name,
                "af": row.af,
                "process_tag": row.process_tag or "",
                "circuit_type": row.circuit_type,
                "network_type": row.network_type,
                "metric": row.metric,
                "passive": row.passive or False,
                # tri-state: None passes through the adapter's optional bfd_enabled
                # (no wire leaf emitted → reconcile leaves brownfield BFD untouched).
                "bfd_enabled": row.bfd_enabled,
                # FRR (#83), same tri-state contract; protection '' → None (enum leaf).
                "frr_enabled": row.frr_enabled,
                "frr_protection": row.frr_protection or None,
            }
        )

    processes = []
    for row in NSOISISInstanceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        proc_entry = {
            "process_tag": row.process_tag or "",
            "net": row.net,
            "is_type": row.is_type,
            "metric_style": row.metric_style,
            "overload_bit": row.overload_bit,
            "area_auth_type": row.area_auth_type,
            # Routing-protocol auth keys: pushed when held (empty string → None so the
            # adapter intent treats "no key" as absent rather than a literal empty key).
            "area_auth_key": row.area_auth_key or None,
            "domain_auth_type": row.domain_auth_type,
            "domain_auth_key": row.domain_auth_key or None,
            # FRR (#83): flavor '' → None (enum leaf); microloop tri-state verbatim.
            "fast_reroute": row.fast_reroute or None,
            "microloop_avoidance": row.microloop_avoidance,
        }
        proc_redist = redist_by_proc.get(row.process_tag or "", [])
        if proc_redist:
            proc_entry["redistribution"] = proc_redist
        levels = _isis_levels_for_state(row)
        if levels:
            proc_entry["levels"] = levels
        processes.append(proc_entry)

    _push_changed(
        (device_id, "isis"),
        [interfaces, processes],
        lambda body: client.put_isis_interface_intent(adapter_device_id, body[0], processes=body[1]),
    )


@_skip_on_render
def _on_isis_interface_state_save(sender, instance, **kwargs):
    """Push IS-IS interface intent whenever an NSOISISInterfaceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "isis"))


@_skip_on_render
def _on_isis_instance_state_save(sender, instance, **kwargs):
    """Push IS-IS intent (interfaces + processes) whenever an NSOISISInstanceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "isis"))


def _build_bgp_router_list(routers: dict, scope_afs: dict, router_ids: dict | None = None) -> list:
    """Convert the routers dict + scope_afs into the adapter router_list format.

    ``router_ids`` maps asn_str → the owned global router-id; when present for an ASN it
    is emitted as ``router_id`` on that router so the adapter/bgp-reconciler adopts it.
    """
    router_ids = router_ids or {}
    router_list = []
    covered: set[tuple[str, str]] = set()
    for asn_str, router_data in routers.items():
        scopes_out = []
        for vrf_str, scope_data in router_data["scopes"].items():
            af_map = scope_afs.get((asn_str, vrf_str), {})
            covered.add((asn_str, vrf_str))
            # The scope's AF list must be the UNION of the AFs that carry an accepted
            # redistribution row AND the AFs the scope's peers actually use.
            #
            # It used to be the redistribution AFs alone, which on the ORDINARY path (peers
            # accepted, no redistribution) left it EMPTY — and bgp-reconciler drives AF
            # activation, per-AF policy binding, and the whole of `_apply_ios_vrf_scope` off
            # `scope.address_family`. So the peer's route-maps and prefix-lists never bound (the
            # peer came up UNFILTERED) and every IOS VRF peer was never written at all, silently,
            # with the commit reporting success. IOS auto-activates ipv4-unicast for a plain
            # `neighbor … remote-as`, which is why the peer still came up and nothing looked wrong.
            peer_afs = [
                af["af"]
                for peer in scope_data.get("peers", [])
                for af in peer.get("address_families", [])
                if af.get("af") and af["af"] not in af_map
            ]
            afs_out = [{"af": af_str, "redistribution": redist} for af_str, redist in af_map.items()]
            afs_out += [{"af": af_str, "redistribution": []} for af_str in dict.fromkeys(peer_afs)]
            scope_out = dict(scope_data)
            scope_out["address_families"] = afs_out
            scopes_out.append(scope_out)
        router_entry = {"asn": asn_str, "scopes": scopes_out}
        if router_ids.get(asn_str):
            router_entry["router_id"] = router_ids[asn_str]
        router_list.append(router_entry)

    # Redistribution-only scopes: an accepted redistribution row whose (asn, vrf)
    # has no owned peer still needs its router/scope/AF in the payload — without
    # it the dest_ref join at apply time finds no AF and the entry is dropped.
    by_asn = {r["asn"]: r for r in router_list}
    for (asn_str, vrf_str), af_map in scope_afs.items():
        if (asn_str, vrf_str) in covered:
            continue
        router = by_asn.get(asn_str)
        if router is None:
            router = {"asn": asn_str, "scopes": []}
            if router_ids.get(asn_str):
                router["router_id"] = router_ids[asn_str]
            by_asn[asn_str] = router
            router_list.append(router)
        router["scopes"].append(
            {
                "vrf": vrf_str,
                "peers": [],
                "address_families": [
                    {"af": af_str, "redistribution": redist_entries} for af_str, redist_entries in af_map.items()
                ],
            }
        )
    return router_list


def _bgp_peer_source_value(bgp_peer):
    """Return a BGP peer's session source as the reconciler's polymorphic source string.

    The reconciler dispatches ``peer/source`` per-NED at write time: IOS/IOS-XR
    update-source is an interface NAME (``BGPPeer.update_source``, a dcim.Interface),
    while Junos/Nokia local-address is an IP (``BGPPeer.source``, an ipam.IPAddress).
    The interface name wins when set, else the source host IP — so the session source
    round-trips for every vendor. Returns None when neither is set (or no peer).
    """
    if bgp_peer is None:
        return None
    if bgp_peer.update_source is not None:
        return bgp_peer.update_source.name
    if bgp_peer.source is not None:
        return str(bgp_peer.source.address.ip)
    return None


def _bgp_peer_model_fields(bgp_peer) -> dict:
    """Return the write-path peer leaves the overlay row does not denormalize.

    ``local_as`` / ``ttl`` / ``password`` / ``peer_group`` live only on the linked
    netbox-routing ``BGPPeer`` (``NSOBGPPeerState`` carries just ``remote_as_str`` +
    ``enabled``), yet the reconciler + adapter both write them. Dropping them from the
    pushed intent silently un-managed a brownfield peer's password / ttl / local-AS /
    peer-group. Absent = the reconciler treats the leaf as "do not touch", so include a
    key only when it holds a value — mirroring :func:`_bgp_peer_source_value`.
    """
    fields: dict = {}
    if bgp_peer is None:
        return fields
    if bgp_peer.local_as is not None:
        fields["local_as"] = str(bgp_peer.local_as.asn)
    if bgp_peer.ttl is not None:
        fields["ttl"] = bgp_peer.ttl
    if bgp_peer.password:
        fields["password"] = bgp_peer.password
    if bgp_peer.peer_group is not None:
        fields["peer_group"] = bgp_peer.peer_group.name
    return fields


def _bgp_router_id_map(device_id) -> dict:
    """Map asn_str → owned global router-id for every BGPRouter on this device.

    Empty when netbox_routing is absent or no router-id is set. Only routers already in
    the peer/redistribution-driven push pick their entry up (router-id rides along with
    an owned peer under the same ASN).
    """
    try:
        from netbox_routing.models import BGPRouter
    except ImportError:
        return {}
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(Device)
    out: dict = {}
    for router in BGPRouter.objects.filter(assigned_object_type=ct, assigned_object_id=device_id).select_related("asn"):
        if router.router_id:
            out[str(router.asn.asn)] = str(router.router_id)
    return out


def _push_bgp_intent_for_device(device_id, adapter_device_id):
    """Build and push the full BGP intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOBGPPeerState

    # BGP redistribution: dest_ref = f"{asn}:{vrf}:{af}"
    redist_by_af = _collect_redistribution_by_dest_ref(device_id, "bgp")

    # Build scope-level address_families from redistribution dest_refs.
    # Greenfield rows use "asn:vrf:af"; rows imported off the adapter mirror
    # carry its "asn/vrf/af" form — accept both.
    scope_afs: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for dest_ref, redist_entries in redist_by_af.items():
        parts = dest_ref.split(":", 2)
        if len(parts) != 3:
            parts = dest_ref.split("/", 2)
        if len(parts) != 3:
            continue
        asn_str, vrf_str, af_str = parts
        scope_afs.setdefault((asn_str, vrf_str), {})[af_str] = redist_entries

    routers: dict[str, dict] = {}
    for row in NSOBGPPeerState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("management", "bgp_peer", "bgp_peer__local_as", "bgp_peer__peer_group"):
        asn_str = row.asn_str
        vrf_name = row.vrf_name or ""
        if asn_str not in routers:
            routers[asn_str] = {"asn": asn_str, "scopes": {}}
        scopes = routers[asn_str]["scopes"]
        if vrf_name not in scopes:
            scopes[vrf_name] = {"vrf": vrf_name, "address_families": [], "peers": []}

        # Build address-family list from the linked BGPPeer if available.
        peer_afs = []
        if row.bgp_peer is not None:
            for paf in row.bgp_peer.address_families.select_related(
                "address_family",
                "prefixlist_in",
                "prefixlist_out",
                "routemap_in",
                "routemap_out",
            ):
                af_entry = {
                    "af": paf.address_family.address_family,
                    "enabled": paf.enabled if paf.enabled is not None else True,
                }
                if paf.routemap_in:
                    af_entry["routemap_in"] = paf.routemap_in.name
                if paf.routemap_out:
                    af_entry["routemap_out"] = paf.routemap_out.name
                if paf.prefixlist_in:
                    af_entry["prefixlist_in"] = paf.prefixlist_in.name
                if paf.prefixlist_out:
                    af_entry["prefixlist_out"] = paf.prefixlist_out.name
                peer_afs.append(af_entry)

        peer_dict = {
            "peer_address": row.peer_address_str,
            "enabled": row.enabled if row.enabled is not None else True,
            "remote_as": row.remote_as_str or None,
            "address_families": peer_afs,
        }
        source_value = _bgp_peer_source_value(row.bgp_peer)
        if source_value is not None:
            peer_dict["source"] = source_value
        peer_dict.update(_bgp_peer_model_fields(row.bgp_peer))
        scopes[vrf_name]["peers"].append(peer_dict)

    router_list = _build_bgp_router_list(routers, scope_afs, _bgp_router_id_map(device_id))
    _push_changed(
        (device_id, "bgp"),
        router_list,
        lambda body: client.put_bgp_intent(adapter_device_id, body),
    )


@_skip_on_render
def _on_bgp_peer_state_save(sender, instance, **kwargs):
    """Push BGP intent whenever an NSOBGPPeerState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "bgp"))


# ── Greenfield BGP peers (operator-created in NetBox, not yet on the device) ──
#
# The reconcile path only creates an NSOBGPPeerState overlay for a peer the device already
# reports (brownfield adoption). These handlers add the missing direction: a
# netbox_routing.BGPPeer the operator creates/edits under a managed device's BGP router
# becomes an *accepted* overlay (owned intent) and pushes; deleting the peer drops the
# overlay and retracts (full-replace). This fills the "no native BGPPeer pre_delete" gap:
# the overlay's bgp_peer FK is on_delete=SET_NULL, so a BGPPeer delete would otherwise only
# null the FK and leave the intent stranded on the device.


def _bgp_peer_device(peer):
    """Resolve the managed Device a BGPPeer belongs to (scope→router→assigned_object), or None."""
    from dcim.models import Device

    scope = getattr(peer, "scope", None)
    router = getattr(scope, "router", None) if scope is not None else None
    if router is None:
        return None
    obj = router.assigned_object
    return obj if isinstance(obj, Device) else None


def _bgp_peer_overlay_key(peer):
    """Return (asn_str, vrf_name, peer_address_str) for a BGPPeer.

    MUST match the reconcile derivation (bgp_reconciler: asn_str=str(router.asn.asn),
    vrf_name=scope.vrf.name or '', peer_address_str=host IP) so a greenfield-created
    overlay and a later reconcile of the same peer share ONE identity key (no duplicate).
    """
    router = peer.scope.router
    asn_str = str(router.asn.asn) if router.asn_id else ""
    vrf = peer.scope.vrf
    vrf_name = vrf.name if vrf is not None else ""
    # peer.peer.address is a netaddr IPNetwork once loaded from the DB, but a plain
    # "10.0.0.2/32" str on a freshly-created in-memory IPAddress — take the bare host
    # part robustly in both cases, matching the reconciler's peer_address_str (the raw
    # payload "peer_address", e.g. "10.0.0.2", with no mask).
    peer_address_str = str(peer.peer.address).split("/")[0] if peer.peer_id else ""
    return asn_str, vrf_name, peer_address_str


def _accept_bgp_peer_for_device(peer, device) -> None:
    """Own a greenfield BGP peer for *device* (accepted overlay) → its save pushes intent."""
    from .models import NSOBGPPeerState, NSODeviceManagement

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    asn_str, vrf_name, peer_address_str = _bgp_peer_overlay_key(peer)
    if not asn_str or not peer_address_str:
        return  # incomplete peer (no ASN / no address) — nothing to own yet
    state, created = NSOBGPPeerState.objects.get_or_create(
        management=mgmt,
        asn_str=asn_str,
        vrf_name=vrf_name,
        peer_address_str=peer_address_str,
        defaults={"status": "accepted", "accepted_at": timezone.now()},
    )
    # Greenfield-only ownership (mirrors _accept_ospf_interface): own a peer whose overlay
    # was just created here, or one already owned. A pre-existing UNOWNED (brownfield-adopted)
    # overlay is left to the 3-way reconcile — editing the netbox-routing object surfaces as
    # 'changed' for an explicit Accept, it is NOT force-owned/pushed. (The reconciler that
    # materialized the brownfield peer runs under suppress_intent_push, so this handler never
    # sees that create; it only fires on a genuine operator create/edit.)
    if not created and state.status not in _OWNED_PUSH_STATUSES:
        return
    state.bgp_peer = peer
    state.remote_as_str = str(peer.remote_as.asn) if peer.remote_as_id else ""
    state.enabled = peer.enabled
    state.last_sync_at = timezone.now()
    state.save()  # → _on_bgp_peer_state_save schedules the push


@_skip_on_render
def _on_routing_bgp_peer_save(sender, instance, **kwargs):
    """netbox_routing BGPPeer created/edited on a managed device → own + push (greenfield)."""
    device = _bgp_peer_device(instance)
    if device is not None:
        _accept_bgp_peer_for_device(instance, device)


@_skip_on_render
def _on_routing_bgp_peer_pre_delete(sender, instance, **kwargs):
    """Operator deletes a BGP peer → drop its overlay + push the removal before the cascade.

    Deleting the overlay yields a reduced push snapshot (owned rows only), retracting a
    previously-owned peer; an un-owned (imported) peer was never in the snapshot, so its
    delete is a safe no-op. Registered under _as_delete_origin so the removal is marked
    delete-origin (real retraction, not a detach).
    """
    from .models import NSOBGPPeerState, NSODeviceManagement

    device = _bgp_peer_device(instance)
    if device is None:
        return
    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    asn_str, vrf_name, peer_address_str = _bgp_peer_overlay_key(instance)
    NSOBGPPeerState.objects.filter(
        management=mgmt, asn_str=asn_str, vrf_name=vrf_name, peer_address_str=peer_address_str
    ).delete()
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "bgp"))


@_skip_on_render
def _on_redistribution_state_save(sender, instance, **kwargs):
    """Push the relevant routing protocol intent when an NSORedistributionState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    _schedule_redistribution_push(mgmt.device_id, instance.dest_protocol)


def _push_route_policy_intent_for_device(device_id, adapter_device_id):
    """Build and push the full route-policy intent snapshot for a device.

    A forced Apply claim re-sends this snapshot whatever the acknowledged baseline says,
    so an owned row whose adapter intent went stale or empty is applied instead of skipped.
    """
    from . import adapter_client as client
    from .models import NSORoutePolicyState

    owned_rows = list(
        NSORoutePolicyState.objects.filter(
            management__device_id=device_id,
            status__in=_OWNED_PUSH_STATUSES,
        ).select_related("management")
    )

    objects = []
    for row in owned_rows:
        # Build the entries payload from the associated NetBox object via the GFK.
        obj = row.assigned_object
        if obj is None:
            continue
        entries = _build_route_policy_entries(row.family, obj)
        objects.append(
            {
                "family": row.family,
                "name": row.object_name,
                # Every row here is OWNED (query filters to _OWNED_PUSH_STATUSES), i.e. operator
                # intent that must stay eligible for Apply. Keying this off status=='accepted'
                # dropped the flag once a row advanced to deploying/in_sync/apply_failed, so the
                # adapter stamped no accepted_at and treated the object as ineligible → Apply
                # applied 0 route-policy items and the row stuck in 'deploying' forever (rg03).
                "entries": entries,
                "accepted": True,
                # community-list only: Junos invert-match / Nokia expression NOT(…).
                **(
                    {"invert_match": bool(getattr(obj, "invert_match", False))}
                    if row.family == "community_list"
                    else {}
                ),
            }
        )

    _push_changed(
        (device_id, "route_policy"),
        objects,
        lambda body: client.put_route_policy_intent(adapter_device_id, body),
        on_response=lambda resp: _store_unsupported_members(owned_rows, resp),
    )


def _store_unsupported_members(owned_rows, resp) -> None:
    """Persist the adapter's per-object ``unsupported_members`` map onto the overlay rows.

    The adapter reports which community-list members this device's NED cannot hold
    (e.g. a wildcard color on Nokia — the codec silently skips them). Recording them
    lets the device tab show "unsupported on <ned>" so an owned object sitting at
    "pending apply" is explained rather than a suspicious phantom. Only community-list
    rows carry members; a row absent from the map (or all rows, when the push was
    skipped-unchanged / the adapter is old) is cleared to ``[]``. Runs under
    ``suppress_intent_push`` so these mirror writes don't re-fire the edit handlers.
    """
    if resp is None:
        return  # push skipped-unchanged or failed — keep whatever we last recorded
    unsupported = resp.get("unsupported_members") or {}
    with suppress_intent_push():
        for row in owned_rows:
            members = unsupported.get(row.object_name, []) if row.family == "community_list" else []
            if list(row.unsupported_members or []) != list(members):
                row.unsupported_members = members
                row.save(update_fields=["unsupported_members"])


def _build_community_list_entries(obj):
    """Serialize a community-list's members for the adapter intent payload.

    The universal Community model stores every member VERBATIM (numeric, well-known
    keyword, typed extended `target:…`/`color:…`, RFC 8092 `large:…`, and regex/wildcards),
    so the whole list is the single CommunityList — no parallel typed lists to merge. Emit
    each member's text exactly as stored; the NED-specific split by kind happens reconciler-side.
    """
    out = []
    seq = 0
    for e in obj.communitylistentries.all():
        if not e.community_id:
            continue
        seq += 1
        out.append(
            {
                "sequence": seq,
                "action": e.action.lower() if e.action else "permit",
                "community": str(e.community.community),
            }
        )
    return out


# Canonical AFI → the Junos family token. The Junos writer does `frm.family = str(fam)`, so it
# needs the vendor spelling; the inverse of route_policy_structure._AFI_MAP's junos rows.
_JUNOS_FAMILY_BY_AFI = {
    "ipv4": "inet",
    "ipv6": "inet6",
    "vpn-ipv4": "inet-vpn",
    "vpn-ipv6": "inet6-vpn",
}


def _set_community_targets(entry):
    """(operation, name) for every set-community row — by-ref list name, else inline literals."""
    targets = []
    for sc in entry.set_communities.all():
        if sc.community_list_id:
            targets.append((sc.operation, sc.community_list.name))
        else:
            targets.extend((sc.operation, c.community) for c in sc.communities.all())
    return targets


def _project_set_communities(entry, set_data, current) -> bool:
    """Project set-community rows into the writer's community vocabulary. True when it diverged.

    Per-NED shapes (verified against route-policy-reconciler):
      * ``community`` — IOS-XR interpolates it directly (``set community ({v})``) so a single
        target MUST be a scalar; IOS/Junos wrap either form. >1 target → list (IOS/Junos).
      * ``community_additive`` — bool, the add-vs-set verb for IOS/IOS-XR.
      * ``_junos_community_op`` — parallel per-name verb list, so Junos gets the exact op.
      * ``community_delete`` — IOS-XR only, and SCALAR (``delete community {v}``).

    Delete targets are deliberately kept OUT of ``community``: IOS-XR reads both keys, so a
    name in each would emit ``set community (X)`` *and* ``delete community X`` for one list.
    KNOWN LIMIT: that means a delete lands on IOS-XR but NOT on Junos — Junos expresses delete
    only through ``community`` + ``_junos_community_op``, and there is no Junos-only delete key
    to route it through. Closing that needs a writer-side key in route-policy-reconciler (P3).
    """
    targets = _set_community_targets(entry)
    if not targets or targets == [(sc.operation, sc.name) for sc in current.set_communities]:
        return False  # unset, or the blob already says the same thing → leave it byte-identical

    writes = [(op, nm) for op, nm in targets if op != "delete"]
    deletes = [nm for op, nm in targets if op == "delete"]

    if writes:
        names = [nm for _, nm in writes]
        set_data["community"] = names[0] if len(names) == 1 else names
        set_data["community_additive"] = all(op == "add" for op, _ in writes)
        set_data["_junos_community_op"] = [op for op, _ in writes]
    if deletes:
        set_data["community_delete"] = deletes[0]
        if len(deletes) > 1:
            logger.warning(
                "route-policy: entry %s has %d set-community deletes but the writer's "
                "`delete community` takes ONE set — only %r is pushed; express the rest via vendor_ext",
                entry.pk,
                len(deletes),
                deletes[0],
            )
    return True


def _project_vendor_ext(vendor_ext, match_data, set_data) -> None:
    """Flatten vendor_ext {"junos": {"priority": "high"}} back to the `_junos_priority` key.

    The inverse of route_policy_structure._collect_vendor_ext. The "unmapped" namespace is a
    lossless record of tokens the reader could not map, NOT a writer key — never emit it.
    """
    from .route_policy_structure import _NS_BY_PREFIX

    prefix_by_ns = {ns: prefix for prefix, ns in _NS_BY_PREFIX.items()}
    for ns, kv in (vendor_ext or {}).items():
        prefix = prefix_by_ns.get(ns)
        if prefix is None or not isinstance(kv, dict):
            continue  # "unmapped" (or junk) — not a writer namespace
        for key, value in kv.items():
            # Match-side keys stay on match, everything else rides set; the per-NED writers
            # read their own namespace off whichever blob the reader put it on. Preserve the
            # existing side when the key is already present so we do not duplicate it.
            target = match_data if f"{prefix}{key}" in match_data else set_data
            target[f"{prefix}{key}"] = value


def _project_structured_entry(entry, match_data, set_data) -> bool:
    """Project operator-authored STRUCTURED RouteMapEntry fields back into the match/set blobs.

    Returns True when a field DIVERGED from the blob and was projected — i.e. the operator
    edited it. The caller uses that to invalidate the IOS-XR verbatim body (see
    _build_route_policy_entries); it must stay False for an untouched brownfield policy.

    The reader DERIVES the structured fields from the blobs and keeps the blobs authoritative
    for the write side (route_policy_reconciler ~283), so a structured field an operator sets
    by hand had no path to the device — it was silently dropped at Apply. Each field is written
    into the exact key the nso-packages writer consumes. Structured WINS on divergence (the
    operator edited it); an agreeing blob is left byte-identical so the brownfield round-trip
    does not churn (e.g. a Junos `inet` token is not rewritten to canonical `ipv4`).

    Not projected, deliberately:
      * ``match_community`` — devices match community-LISTS, never individual communities; it is
        a display projection of the referenced list's members, not independent intent.
      * ``match_condition`` — the condition tree has NO key in any per-NED writer; warned below
        rather than silently dropped.
    """
    from .route_policy_structure import structure_entry

    current = structure_entry(match_data, set_data)
    edited = False

    afi = list(entry.match_afi or [])
    if afi and afi != current.match_afi:
        match_data["family"] = afi
        junos_family = _JUNOS_FAMILY_BY_AFI.get(afi[0])
        if junos_family:
            match_data["_junos_family"] = junos_family
        edited = True

    call_name = entry.call_policy.name if entry.call_policy_id else None
    if call_name and call_name != current.call_policy:
        match_data["_junos_from_policy"] = [call_name]
        edited = True

    # apply_policy has no forward projection in structure_entry, so compare the raw key.
    apply_name = entry.apply_policy.name if entry.apply_policy_id else None
    if apply_name:
        existing = set_data.get("apply")
        existing = list(existing) if isinstance(existing, (list, tuple)) else ([existing] if existing else [])
        if apply_name not in [str(p) for p in existing]:
            set_data["apply"] = [apply_name]
            edited = True

    edited |= _project_set_communities(entry, set_data, current)
    # vendor_ext is re-emitted verbatim (it was DERIVED from these blobs, so it is idempotent for
    # a brownfield entry) — it never counts as an edit on its own.
    _project_vendor_ext(entry.vendor_ext, match_data, set_data)

    if entry.match_condition:
        logger.warning(
            "route-policy: entry %s has a match_condition tree, which NO per-NED writer consumes "
            "yet — it is NOT pushed to the device (structured-write P3). Use vendor_ext for a "
            "device-expressible form so the intent is not lost.",
            entry.pk,
        )

    return edited


def _build_route_policy_entries(family, obj):
    """Serialize a NetBox route-policy object's entries for the adapter intent payload."""
    if family == "prefix_list":
        out = []
        for e in obj.prefix_list_entries.all().order_by("sequence"):
            cp = e.assigned_prefix
            if cp is None:
                continue
            out.append(
                {
                    "sequence": e.sequence,
                    "action": e.action.lower() if e.action else "permit",
                    "prefix": str(cp.prefix),
                    **({"ge": e.ge} if getattr(e, "ge", None) is not None else {}),
                    **({"le": e.le} if getattr(e, "le", None) is not None else {}),
                }
            )
        return out
    if family == "community_list":
        return _build_community_list_entries(obj)
    if family == "as_path":
        # netbox-routing: ASPath → aspath_entries (ASPathEntry: sequence/action/pattern).
        return [
            {
                "sequence": e.sequence,
                "action": e.action.lower() if e.action else "permit",
                "pattern": e.pattern or "",
            }
            for e in obj.aspath_entries.all().order_by("sequence")
        ]
    if family == "route_map":
        # netbox-routing: RouteMap → route_map_entries (RouteMapEntry: sequence/action +
        # M2M match refs + match/set JSON blobs). Entry keys are the YANG leaf names of
        # route-policy-reconciler — the adapter passes them verbatim into the service
        # payload (m17-route-policy-contract.md §2).
        entries = []
        match_blobs = []
        edited = False
        for e in obj.route_map_entries.all().order_by("sequence"):
            match_data = _as_json_dict(e.match)
            set_data = _as_json_dict(e.set)
            if e.flow_control is not None and "flow_control" not in set_data:
                # the read path lifts flow_control out of set-json into the model
                # field — put it back so the round-trip stays symmetric
                set_data["flow_control"] = e.flow_control
            # Same class of fix, for the rest of the structured fields: the reader derives them
            # FROM the blobs and leaves the blobs authoritative, so an operator-authored
            # match_afi / call_policy / apply_policy / set-community / vendor_ext would never
            # reach the device. Project each back into the key its writer reads.
            edited |= _project_structured_entry(e, match_data, set_data)
            # The universal Community model means a community-list of any kind is just the
            # one CommunityList — match_community_list carries every referenced list.
            community_names = list(e.match_community_list.values_list("name", flat=True))
            entry: dict = {
                "sequence": e.sequence,
                "action": e.action.lower() if e.action else "permit",
                "match-prefix-lists": list(e.match_prefix_list.values_list("name", flat=True)),
                "match-community-lists": community_names,
                "match-as-paths": list(e.match_aspath.values_list("name", flat=True)),
            }
            entries.append(entry)
            match_blobs.append((entry, match_data, set_data))

        # IOS-XR EDIT-INVALIDATION. RPL is opaque text, so the reader preserves the verbatim body
        # under `_rpl_raw` on the route-map's FIRST entry and the writer PREFERS it
        # (`body = raw if raw is not None else self._iosxr_rpl_body(...)`, _iosxr.py:245). Leaving
        # it in place after a structured edit would replay the STALE body and silently discard the
        # edit. Drop it so the writer falls back to the structured render — but ONLY on a real
        # edit: dropping it unconditionally would force every untouched IOS-XR policy to re-render
        # from a parse that cannot reproduce the text byte-for-byte (fleet-wide spurious diff).
        if edited and match_blobs:
            match_blobs[0][1].pop("_rpl_raw", None)

        for entry, match_data, set_data in match_blobs:
            entry["match-json"] = json.dumps(match_data, sort_keys=True)
            entry["set-json"] = json.dumps(set_data, sort_keys=True)
        return entries
    return []


def _preflight_constructs(family, obj):
    """Derive the capability-preflight inputs from a netbox-routing route-policy object.

    Returns ``(community_members, set_keys, match_keys, aspath_names)`` — the shape the
    adapter's ``/route-policy/preflight`` endpoint checks against a device's capability matrix:

      - community_list → its member strings (checked by KIND: large / color / regex / …);
      - route_map → the union of set-json / match-json keys across entries (checked by
        construct name, e.g. ``metric_type`` → "set metric-type") + its referenced as-path
        list names (IOS needs a numeric 1-500 name);
      - as_path → its own name (same numeric check);
      - prefix_list → nothing (universally representable — always supported).

    Mirrors the keys ``_build_route_policy_entries`` already serializes, so the preflight
    sees exactly what an Apply would push.
    """
    community_members: list[str] = []
    set_keys: set[str] = set()
    match_keys: set[str] = set()
    aspath_names: set[str] = set()
    if family == "community_list":
        for e in obj.communitylistentries.all():
            if e.community_id:
                community_members.append(str(e.community.community))
    elif family == "route_map":
        for e in obj.route_map_entries.all():
            set_keys.update(_as_json_dict(e.set).keys())
            match_keys.update(_as_json_dict(e.match).keys())
            aspath_names.update(e.match_aspath.values_list("name", flat=True))
    elif family == "as_path":
        aspath_names.add(obj.name)
    return sorted(set(community_members)), sorted(set_keys), sorted(match_keys), sorted(aspath_names)


def _as_json_dict(value):
    """Coerce a JSONField value (dict, JSON string, or None) into a dict."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            data = json.loads(value)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


@_skip_on_render
def _on_route_policy_state_save(sender, instance, **kwargs):
    """Push route-policy intent whenever an NSORoutePolicyState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "route_policy"))


@_skip_on_render
def _on_routing_policy_pre_delete(sender, instance, **kwargs):
    """Drop overlays + push the reduced snapshot when a netbox-routing policy is deleted.

    Reverts the removal on each attached device.
    The overlay links via a content-type GFK (no DB cascade), so the overlays must be
    removed explicitly here, before the object is gone. Captures attached devices first,
    then a deferred push (post-commit, overlays gone) sends the reduced snapshot.
    """
    from django.contrib.contenttypes.models import ContentType

    from .models import NSORoutePolicyState

    ct = ContentType.objects.get_for_model(type(instance))
    states = list(
        NSORoutePolicyState.objects.filter(content_type=ct, object_id=instance.pk).select_related("management")
    )
    targets = []
    for state in states:
        mgmt = state.management
        if mgmt.adapter_device_id is not None:
            targets.append((mgmt.device_id, mgmt.adapter_device_id))
    NSORoutePolicyState.objects.filter(content_type=ct, object_id=instance.pk).delete()
    for device_id, adapter_device_id in targets:
        _schedule_intent_push((device_id, "route_policy"))


# ── netbox_routing route-policy edit write path ─────────────────────────────
# The reconcile path only creates NSORoutePolicyState overlays for policy objects the
# device already reports (brownfield adoption); it never reacts to an operator EDIT of a
# netbox-routing object. Without this, editing an OWNED community-list (adding/removing a
# member) leaves the overlay in_sync, so Accept is a no-op and the edit never pushes.
# These handlers mirror the OSPF/IS-IS greenfield path: editing an OWNED route-policy
# object (or one of its entries) on a managed device re-asserts ownership (status →
# accepted) and pushes the updated intent. A brownfield (un-owned/imported) overlay is
# left to the 3-way reconcile so the edit surfaces as changed/conflict, not force-owned.


# Result of cascading ownership from a route-map onto its referenced objects:
#   drifted      — (family, name) refs left as-is because the device has a diverging version
#   cross_device — (family, name, source_device) greenfield refs whose NetBox content was
#                  sourced (materialized) from a DIFFERENT device than the one we're owning onto
CascadeResult = namedtuple("CascadeResult", ["drifted", "cross_device"])


def _own_route_map_contributors(mgmt, route_map) -> CascadeResult:
    """Own (accepted) the prefix-lists / community-lists / as-paths a route-map references.

    Owning a top-level object cascades ownership to everything it depends on: a route-map
    references prefix-lists / community-lists / as-paths by name, and owning the route-map
    WITHOUT owning a GREENFIELD reference leaves a dangling reference on the device (the
    ``match`` line is written but the referenced list/path is never pushed — the exact gap
    that left an ``ip as-path access-list`` missing after a route-map apply).

    BUT a reference that has DRIFTED on the device (``changed``/``conflict`` — the device has a
    diverging version) is NOT force-owned: that would silently overwrite the device's version.
    It already exists on the device, so the route-map's reference still resolves against it — we
    leave it for explicit drift resolution and RETURN the skipped ``(family, name)`` tuples so
    the caller can warn the operator. Already-owned contributors (accepted / deploying / in_sync
    / apply_failed) are left untouched.

    A GREENFIELD reference (no overlay on this device yet) whose shared NetBox content was
    *materialized from a different device* is still owned — the route-map needs it — but its
    provenance is collected into ``cross_device`` so the caller can warn that owning the
    route-map here will push another device's version of that object onto this device.
    """
    from django.contrib.contenttypes.models import ContentType

    from .models import NSORoutePolicyState
    from .shared_object_ownership import materialized_row
    from .status_machine import CHANGED, CONFLICT

    now = timezone.now()
    referenced: list = []  # (family, obj), de-duplicated by (family, name)
    seen_refs: set = set()

    def _add_ref(family, obj):
        key = (family, obj.name)
        if key not in seen_refs:
            seen_refs.add(key)
            referenced.append((family, obj))

    for entry in route_map.route_map_entries.all():
        for o in entry.match_prefix_list.all():
            _add_ref("prefix_list", o)
        for o in entry.match_community_list.all():
            _add_ref("community_list", o)
        for o in entry.match_aspath.all():
            _add_ref("as_path", o)
        # SET community-list references (`set community <list> add|delete|…`) are dependencies
        # too — a `set comm-list delete <CL>` rejects on the device if <CL> isn't defined.
        for sc in entry.set_communities.all():
            if sc.community_list_id:
                _add_ref("community_list", sc.community_list)
    drifted: list = []
    cross_device: list = []
    for family, obj in referenced:
        ct = ContentType.objects.get_for_model(obj)
        state, created = NSORoutePolicyState.objects.get_or_create(
            management=mgmt,
            family=family,
            object_name=obj.name,
            defaults={"content_type": ct, "object_id": obj.pk, "status": "accepted", "accepted_at": now},
        )
        if created:
            # Greenfield on this device — owned. If the shared NetBox content was materialized
            # from ANOTHER device, owning here pushes that device's version; surface provenance.
            owner = materialized_row(NSORoutePolicyState, family, obj.name)
            if owner is not None and owner.management.device_id != mgmt.device_id:
                cross_device.append((family, obj.name, owner.management.device.name))
            continue
        if state.status in _OWNED_PUSH_STATUSES:
            continue  # already owned → nothing to do
        if state.status in (CHANGED, CONFLICT):
            # The device has a diverging version — don't silently overwrite it. The reference
            # resolves against the device's existing object; surface it for explicit resolution.
            drifted.append((family, obj.name))
            continue
        # imported / unknown — device matches NetBox (no drift) → safe to adopt.
        state.content_type, state.object_id = ct, obj.pk
        state.status, state.accepted_at = "accepted", now
        state.save()
    return CascadeResult(drifted=drifted, cross_device=cross_device)


def _accept_route_policy_object(obj) -> None:
    """Re-own + push every OWNED overlay attached to a saved route-policy object."""
    from django.contrib.contenttypes.models import ContentType

    from .models import NSORoutePolicyState

    is_route_map = hasattr(obj, "route_map_entries")
    ct = ContentType.objects.get_for_model(type(obj))
    states = NSORoutePolicyState.objects.filter(content_type=ct, object_id=obj.pk).select_related("management")
    for state in states:
        mgmt = state.management
        if mgmt.adapter_device_id is None:
            continue
        # Only re-own an already-owned overlay (incl. in_sync). A brownfield/un-owned
        # (imported/unknown) overlay must surface the edit via reconcile, not be force-owned.
        if state.status not in _OWNED_PUSH_STATUSES:
            continue
        if state.status != "accepted":
            state.status = "accepted"
        state.last_sync_at = timezone.now()
        state.save()  # → _on_route_policy_state_save schedules the intent push
        if is_route_map:
            # Owning a route-map owns its contributors (else dangling device references).
            _own_route_map_contributors(mgmt, obj)


@_skip_on_render
def _on_routing_policy_object_save(sender, instance, **kwargs):
    """netbox_routing CommunityList/RouteMap/PrefixList/ASPath edited → own + push."""
    _accept_route_policy_object(instance)


@_skip_on_render
def _on_routing_policy_entry_save(sender, instance, **kwargs):
    """Own + push the parent object when a route-policy ENTRY (member) is edited/added."""
    parent = (
        getattr(instance, "community_list", None)
        or getattr(instance, "prefix_list", None)
        or getattr(instance, "route_map", None)
        or getattr(instance, "aspath", None)
    )
    if parent is not None:
        _accept_route_policy_object(parent)


@_skip_on_render
def _on_routing_policy_entry_delete(sender, instance, **kwargs):
    """Own + push the parent object when a route-policy ENTRY is removed (reduced member set)."""
    _on_routing_policy_entry_save(sender, instance, **kwargs)


def _collect_redistribution_by_dest_ref(device_id: int, dest_protocol: str) -> dict[str, list[dict]]:
    """Return redistribution entries grouped by dest_ref for the given protocol and device.

    When a row has its ``redistribution`` FK set (Track B), the fork object's fields
    are used as the source of truth so operator-authored intent reaches the push payload.
    """
    from .models import NSORedistributionState

    by_ref: dict[str, list[dict]] = {}
    for row in NSORedistributionState.objects.filter(
        management__device_id=device_id,
        dest_protocol=dest_protocol,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("redistribution", "redistribution__route_map"):
        entry: dict = {"source_protocol": row.source_protocol, "source_ref": row.source_ref}
        if row.redistribution_id is not None:
            fork = row.redistribution
            if fork.route_map:
                entry["route_map"] = fork.route_map.name
            if fork.metric is not None:
                entry["metric"] = fork.metric
            if fork.metric_type:
                entry["metric_type"] = fork.metric_type
        else:
            if row.route_map:
                entry["route_map"] = row.route_map
            if row.metric is not None:
                entry["metric"] = row.metric
            if row.metric_type:
                entry["metric_type"] = row.metric_type
        by_ref.setdefault(row.dest_ref, []).append(entry)
    return by_ref


def _push_ospf_intent_for_device(device_id, adapter_device_id):
    """Build and push the full OSPF intent snapshot for a device.

    A forced claim (provisioning) re-sends this snapshot whatever the acknowledged
    baseline says, so a computed intent always lands.
    """
    from . import adapter_client as client
    from .models import NSOOSPFInstanceState, NSOOSPFInterfaceState

    redist_by_proc = _collect_redistribution_by_dest_ref(device_id, "ospf")

    instances = []
    for row in NSOOSPFInstanceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("management"):
        entry = {
            "process_id": row.process_id,
            "vrf": row.vrf or "",
            "areas": row.areas or [],
        }
        if row.router_id:
            entry["router_id"] = row.router_id
        if row.enabled is not None:
            entry["enabled"] = row.enabled
        proc_redist = redist_by_proc.get(str(row.process_id), [])
        if proc_redist:
            entry["redistribution"] = proc_redist
        instances.append(entry)

    interfaces = []
    for row in NSOOSPFInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("management"):
        entry = {
            "interface_name": row.interface.name,
            "passive": row.passive if row.passive is not None else False,
            "auth_present": row.auth_present if row.auth_present is not None else False,
        }
        if row.process_id is not None:
            entry["process_id"] = row.process_id
        if row.area_id is not None:
            entry["area_id"] = row.area_id
        if row.priority is not None:
            entry["priority"] = row.priority
        if row.cost is not None:
            entry["cost"] = row.cost
        if row.network_type is not None:
            entry["network_type"] = row.network_type
        if row.auth_type is not None:
            entry["auth_type"] = row.auth_type
        interfaces.append(entry)

    payload = {"instances": instances, "interfaces": interfaces}
    _push_changed((device_id, "ospf"), payload, lambda body: client.put_ospf_intent(adapter_device_id, body))


@_skip_on_render
def _on_ospf_instance_state_save(sender, instance, **kwargs):
    """Push OSPF intent whenever an NSOOSPFInstanceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "ospf"))


@_skip_on_render
def _on_ospf_interface_state_save(sender, instance, **kwargs):
    """Push OSPF intent whenever an NSOOSPFInterfaceState row is saved."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    _schedule_intent_push((device_id, "ospf"))


# ── netbox_routing OSPF greenfield write path ───────────────────────────────
# The reconcile path only creates NSOOSPF*State overlays for OSPF the device already
# reports (brownfield adoption). These handlers add the operator-created direction: a
# netbox_routing OSPFInstance/OSPFInterface created on a managed device becomes an
# *accepted* overlay (owned intent) whose save pushes the OSPF intent → ospf-reconciler.

_OWNED_OSPF = ("accepted", "deploying", "in_sync", "apply_failed")


def _accept_ospf_instance(ospf_instance) -> None:
    """Own a greenfield OSPF process for its device (accepted overlay → push)."""
    from .models import NSODeviceManagement, NSOOSPFInstanceState

    try:
        mgmt = NSODeviceManagement.objects.get(device=ospf_instance.device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    state, created = NSOOSPFInstanceState.objects.get_or_create(
        management=mgmt,
        process_id=str(ospf_instance.process_id),
        defaults={"status": "accepted", "accepted_at": timezone.now()},
    )
    # Only own a GREENFIELD process (overlay newly created here) or one already owned.
    # A pre-existing unowned overlay is brownfield-adopted: editing the netbox-routing
    # object must surface via the 3-way reconcile (changed/conflict), not be force-owned.
    if not created and state.status not in _OWNED_OSPF:
        return
    state.router_id = str(ospf_instance.router_id or "")
    state.vrf = ospf_instance.vrf.name if ospf_instance.vrf else ""
    state.ospf_instance = ospf_instance
    # Greenfield default: an operator-created OSPF process is meant to be enabled —
    # set the admin-state intent True (re-asserts Nokia SR OS 'admin-state enable',
    # which a freshly-created instance lacks). Preserve an explicit prior value.
    if state.enabled is None:
        state.enabled = True
    # Instance-level area list (the timos apply binds areas per-interface, but other
    # NEDs surface the area set here) — collect distinct areas from bound interfaces.
    areas, seen = [], set()
    for oi in ospf_instance.interfaces.all():
        if oi.area and oi.area.area_id not in seen:
            seen.add(oi.area.area_id)
            areas.append({"area_id": oi.area.area_id, "area_type": oi.area.area_type or "standard"})
    state.areas = areas
    state.last_sync_at = timezone.now()
    state.save()  # → _on_ospf_instance_state_save schedules the push


def _accept_ospf_interface(ospf_iface) -> None:
    """Own a greenfield OSPF interface (accepted overlay → push)."""
    from .models import NSODeviceManagement, NSOOSPFInterfaceState

    iface = ospf_iface.interface
    try:
        mgmt = NSODeviceManagement.objects.get(device=iface.device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    state, created = NSOOSPFInterfaceState.objects.get_or_create(
        management=mgmt,
        interface=iface,
        defaults={"status": "accepted", "accepted_at": timezone.now()},
    )
    # Greenfield-only ownership (see _accept_ospf_instance): don't force-own a
    # pre-existing unowned (brownfield) overlay — leave it to the 3-way reconcile.
    if not created and state.status not in _OWNED_OSPF:
        return
    state.process_id = str(ospf_iface.instance.process_id) if ospf_iface.instance else None
    state.area_id = ospf_iface.area.area_id if ospf_iface.area else ""
    state.passive = bool(ospf_iface.passive)
    state.priority = ospf_iface.priority
    state.cost = ospf_iface.cost
    state.network_type = ospf_iface.network_type or ""
    state.last_sync_at = timezone.now()
    state.save()  # → _on_ospf_interface_state_save schedules the push
    # Refresh the instance overlay so its area list reflects the new binding.
    if ospf_iface.instance is not None:
        _accept_ospf_instance(ospf_iface.instance)


@_skip_on_render
def _on_routing_ospf_instance_save(sender, instance, **kwargs):
    """netbox_routing OSPFInstance created/edited on a managed device → own + push."""
    _accept_ospf_instance(instance)


@_skip_on_render
def _on_routing_ospf_interface_save(sender, instance, **kwargs):
    """netbox_routing OSPFInterface created/edited → own + push."""
    _accept_ospf_interface(instance)


_OWNED_ISIS = ("accepted", "deploying", "in_sync", "apply_failed")


def _accept_isis_interface(isis_iface) -> None:
    """Own a greenfield IS-IS interface (operator-edited ISISInterface → accepted overlay → push).

    Mirrors _accept_ospf_interface: copies the operator's metric / network-type /
    circuit-type / passive from the netbox_routing.ISISInterface into the
    NSOISISInterfaceState overlay (keyed by interface + address-family) and marks it
    owned so _on_isis_interface_state_save pushes. Greenfield-only: a pre-existing
    unowned (brownfield) overlay is left to the 3-way reconcile.
    """
    from .models import NSODeviceManagement, NSOISISInterfaceState

    iface = isis_iface.interface
    af = isis_iface.address_family
    if not af:
        return
    try:
        mgmt = NSODeviceManagement.objects.get(device=iface.device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    state, created = NSOISISInterfaceState.objects.get_or_create(
        management=mgmt,
        interface=iface,
        af=af,
        defaults={"status": "accepted", "accepted_at": timezone.now()},
    )
    if not created and state.status not in _OWNED_ISIS:
        return
    state.process_tag = isis_iface.instance.process_tag if isis_iface.instance else ""
    state.circuit_type = isis_iface.circuit_type or ""
    state.network_type = isis_iface.network_type or ""
    state.metric = isis_iface.metric
    state.passive = bool(isis_iface.passive)
    # tri-state (None/True/False preserved verbatim): clearing bfd_enabled on the
    # ISISInterface flows None into the overlay → the push retracts the owned BFD.
    state.bfd_enabled = getattr(isis_iface, "bfd_enabled", None)
    # FRR (#83): same contract as bfd_enabled; the protection kind rides along.
    state.frr_enabled = getattr(isis_iface, "frr_enabled", None)
    state.frr_protection = getattr(isis_iface, "frr_protection", "") or ""
    state.isis_interface = isis_iface
    state.last_sync_at = timezone.now()
    state.save()  # → _on_isis_interface_state_save schedules the push


@_skip_on_render
def _push_isis_for_routing_level(level) -> None:
    """Re-push the isis intent when a fork ISISLevel of an OWNED instance changes.

    Levels ride the process intent (a level is accepted with its process), so an
    operator edit/delete on ISISLevel re-pushes the full snapshot for the owning
    device — but only when the instance is linked to an owned NSOISISInstanceState
    (an unowned instance's levels stay NetBox-local).
    """
    from .models import NSODeviceManagement, NSOISISInstanceState

    inst = getattr(level, "instance", None)
    device = getattr(inst, "device", None)
    if device is None:
        return
    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    if not NSOISISInstanceState.objects.filter(
        management=mgmt,
        process_tag=inst.process_tag or "",
        status__in=_OWNED_PUSH_STATUSES,
    ).exists():
        return
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "isis"))


@_skip_on_render
def _on_routing_isis_level_save(sender, instance, **kwargs):
    """netbox_routing ISISLevel created/edited → re-push the owning process intent."""
    _push_isis_for_routing_level(instance)


@_skip_on_render
def _on_routing_isis_level_post_delete(sender, instance, **kwargs):
    """Operator deletes a per-level row → push the reduced snapshot (full-replace)."""
    _push_isis_for_routing_level(instance)


def _on_routing_isis_interface_save(sender, instance, **kwargs):
    """netbox_routing ISISInterface created/edited → own + push."""
    _accept_isis_interface(instance)


@_skip_on_render
def _on_routing_isis_interface_pre_delete(sender, instance, **kwargs):
    """Operator deletes an IS-IS interface → drop its overlay + push the removal (parity with OSPF).

    Without this, deleting an ISISInterface only SET_NULLs NSOISISInterfaceState.isis_interface;
    the overlay row lingers with its owned status and no reduced IS-IS intent is pushed, so the
    device keeps the IS-IS config NetBox just removed.
    """
    from .models import NSODeviceManagement, NSOISISInterfaceState

    iface = instance.interface
    af = instance.address_family
    try:
        mgmt = NSODeviceManagement.objects.get(device=iface.device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    qs = NSOISISInterfaceState.objects.filter(management=mgmt, interface=iface)
    if af:
        qs = qs.filter(af=af)  # scope to this ISISInterface's address-family; leave a sibling AF alone
    qs.delete()
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "isis"))


@_skip_on_render
def _on_routing_ospf_instance_pre_delete(sender, instance, **kwargs):
    """Operator deletes an OSPF process → drop its overlay + push the removal."""
    from .models import NSODeviceManagement, NSOOSPFInstanceState

    try:
        mgmt = NSODeviceManagement.objects.get(device=instance.device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    NSOOSPFInstanceState.objects.filter(management=mgmt, process_id=str(instance.process_id)).delete()
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "ospf"))


@_skip_on_render
def _on_routing_ospf_interface_pre_delete(sender, instance, **kwargs):
    """Operator deletes an OSPF interface → drop its overlay + push the removal."""
    from .models import NSODeviceManagement, NSOOSPFInterfaceState

    iface = instance.interface
    try:
        mgmt = NSODeviceManagement.objects.get(device=iface.device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    NSOOSPFInterfaceState.objects.filter(management=mgmt, interface=iface).delete()
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push((device_id, "ospf"))


@_skip_on_render
def _on_redistribution_fork_save(sender, instance, **kwargs):
    """Push protocol intent when a netbox_routing.Redistribution fork object is saved.

    Finds all NSORedistributionState rows linked to this Redistribution, determines
    their destination protocol, and triggers the appropriate intent push.
    Deduplicates pushes so each (device, dest_protocol) pair is pushed at most once.
    """
    from .models import NSORedistributionState

    seen: set[tuple] = set()
    for state in NSORedistributionState.objects.filter(redistribution=instance).select_related("management"):
        mgmt = state.management
        if mgmt.adapter_device_id is None:
            continue
        key = (mgmt.device_id, state.dest_protocol)
        if key in seen:
            continue
        seen.add(key)
        _schedule_redistribution_push(mgmt.device_id, state.dest_protocol)


def _connect_g_activated():  # pragma: no cover
    """Wire post-app-load signal handlers that require dcim/ipam models.

    Called from AppConfig.ready() after all apps are loaded so that
    dcim.Interface, dcim.Cable, and ipam.IPAddress are importable.
    """
    from dcim.models import Cable, Interface
    from ipam.models import IPAddress

    pre_save.connect(
        _stash_interface_old_values,
        sender=Interface,
        dispatch_uid="nso_plugin_iface_stash_old_values",
    )
    post_save.connect(
        _push_intent_on_interface_edit,
        sender=Interface,
        dispatch_uid="nso_plugin_iface_g_activated",
    )
    post_save.connect(
        _recompute_on_cable_change,
        sender=Cable,
        dispatch_uid="nso_plugin_cable_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_recompute_on_cable_delete),
        sender=Cable,
        dispatch_uid="nso_plugin_cable_post_delete",
        weak=False,
    )
    post_save.connect(
        _recompute_on_interface_save,
        sender=Interface,
        dispatch_uid="nso_plugin_iface_derived_intent",
    )
    post_save.connect(
        _create_greenfield_subif_state,
        sender=Interface,
        dispatch_uid="nso_plugin_iface_greenfield_subif",
    )
    pre_save.connect(
        _on_ip_address_pre_save,
        sender=IPAddress,
        dispatch_uid="nso_plugin_ipaddress_pre_save",
    )
    post_save.connect(
        _on_ip_address_change,
        sender=IPAddress,
        dispatch_uid="nso_plugin_ipaddress_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_ip_address_delete),
        sender=IPAddress,
        dispatch_uid="nso_plugin_ipaddress_post_delete",
        weak=False,
    )

    # Interface state → intent push. post_save is wired by the @receiver on
    # push_intent_on_accept; the DELETE leg belongs here with the other families.
    #
    # NSOInterfaceState is the family with single AND bulk overlay delete views, so an
    # operator can delete an owned description/enabled intent straight from the UI. Without
    # this the reduced snapshot is never pushed and the adapter keeps applying the intent
    # NetBox just dropped.
    from .models import NSOInterfaceState

    post_delete.connect(
        _as_delete_origin(push_intent_on_accept),
        sender=NSOInterfaceState,
        dispatch_uid="nso_plugin_interface_state_post_delete",
        weak=False,
    )

    # SNMP state → intent push
    from .models import NSOSnmpCommunityState, NSOSnmpHostState, NSOSnmpSystemInfoState, NSOSnmpV3UserState

    for snmp_model in (NSOSnmpCommunityState, NSOSnmpV3UserState, NSOSnmpHostState, NSOSnmpSystemInfoState):
        post_save.connect(
            _on_snmp_state_save,
            sender=snmp_model,
            dispatch_uid=f"nso_plugin_snmp_{snmp_model.__name__}_post_save",
        )
        # Deletes must push the REDUCED snapshot too — without this, the adapter
        # keeps applying a deleted community/user/host until some unrelated SNMP
        # row is saved (the removal replace-apply never fires).
        post_delete.connect(
            _as_delete_origin(_on_snmp_state_save),
            sender=snmp_model,
            dispatch_uid=f"nso_plugin_snmp_{snmp_model.__name__}_post_delete",
            weak=False,
        )

    # Logging (remote syslog + local levels) state → intent push
    from .models import NSOLoggingHostState, NSOLoggingLevelState

    for logging_model in (NSOLoggingHostState, NSOLoggingLevelState):
        post_save.connect(
            _on_logging_state_save,
            sender=logging_model,
            dispatch_uid=f"nso_plugin_logging_{logging_model.__name__}_post_save",
        )
        # Deletes must push the REDUCED snapshot too (the WP7-P1 SNMP regression class):
        # with only post_save wired, the adapter keeps applying a deleted host/SVI/subif/MTU
        # until some unrelated sibling row is saved. Caught live on sw01 — deleting an
        # applied SVI's overlay never retracted the irb unit.
        post_delete.connect(
            _as_delete_origin(_on_logging_state_save),
            sender=logging_model,
            dispatch_uid=f"nso_plugin_logging_{logging_model.__name__}_post_delete",
            weak=False,
        )

    # SVI/IRB state → intent push (write path)
    from .models import NSOSVIState

    post_save.connect(
        _on_svi_state_save,
        sender=NSOSVIState,
        dispatch_uid="nso_plugin_svi_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_svi_state_save),
        sender=NSOSVIState,
        dispatch_uid="nso_plugin_svi_state_post_delete",
        weak=False,
    )

    # dot1q subinterface state → intent push (write path)
    from .models import NSOSubinterfaceState

    post_save.connect(
        _on_subinterface_state_save,
        sender=NSOSubinterfaceState,
        dispatch_uid="nso_plugin_subinterface_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_subinterface_state_save),
        sender=NSOSubinterfaceState,
        dispatch_uid="nso_plugin_subinterface_state_post_delete",
        weak=False,
    )

    # per-interface MTU state → intent push (Phase 2b write path)
    from .models import NSOInterfaceMtuState

    post_save.connect(
        _on_mtu_state_save,
        sender=NSOInterfaceMtuState,
        dispatch_uid="nso_plugin_interface_mtu_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_mtu_state_save),
        sender=NSOInterfaceMtuState,
        dispatch_uid="nso_plugin_interface_mtu_state_post_delete",
        weak=False,
    )

    # VLAN-database state → intent push (write path)
    from .models import NSOVLANState

    post_save.connect(
        _on_vlan_state_save,
        sender=NSOVLANState,
        dispatch_uid="nso_plugin_vlan_state_post_save",
    )
    # Deleting an owned row must push the REDUCED snapshot (the f282e9e/#105 class);
    # the builder re-queries owned rows, so reusing the save handler is enough. Same
    # pattern for every overlay family below.
    post_delete.connect(
        _as_delete_origin(_on_vlan_state_save),
        sender=NSOVLANState,
        dispatch_uid="nso_plugin_vlan_state_post_delete",
        weak=False,
    )

    # ipam.VLAN rename → overlay drift visibility (no NSOVLANState signal otherwise)
    from ipam.models import VLAN

    post_save.connect(
        _on_vlan_change,
        sender=VLAN,
        dispatch_uid="nso_plugin_ipam_vlan_post_save",
    )
    pre_delete.connect(
        _as_delete_origin(_on_ipam_vlan_pre_delete),
        sender=VLAN,
        dispatch_uid="nso_plugin_ipam_vlan_pre_delete",
        weak=False,
    )

    # BFD state → intent push (BFD write path)
    from .models import NSOBFDInterfaceState

    post_save.connect(
        _on_bfd_state_save,
        sender=NSOBFDInterfaceState,
        dispatch_uid="nso_plugin_bfd_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_bfd_state_save),
        sender=NSOBFDInterfaceState,
        dispatch_uid="nso_plugin_bfd_state_post_delete",
        weak=False,
    )

    # Static route state → intent push
    from .models import NSOStaticRouteState

    post_save.connect(
        _on_static_route_state_save,
        sender=NSOStaticRouteState,
        dispatch_uid="nso_plugin_static_route_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_static_route_state_save),
        sender=NSOStaticRouteState,
        dispatch_uid="nso_plugin_static_route_state_post_delete",
        weak=False,
    )

    # L2 SAP state → intent push
    from .models import NSOL2SapState

    post_save.connect(
        _on_l2_sap_state_save,
        sender=NSOL2SapState,
        dispatch_uid="nso_plugin_l2_sap_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_l2_sap_state_save),
        sender=NSOL2SapState,
        dispatch_uid="nso_plugin_l2_sap_state_post_delete",
        weak=False,
    )

    # LACP bundle/member state → intent push + apply
    from .models import NSOLACPBundleState, NSOLACPMemberState

    post_save.connect(
        _on_lacp_state_save,
        sender=NSOLACPBundleState,
        dispatch_uid="nso_plugin_lacp_bundle_state_post_save",
    )
    post_save.connect(
        _on_lacp_state_save,
        sender=NSOLACPMemberState,
        dispatch_uid="nso_plugin_lacp_member_state_post_save",
    )
    # Direct-apply family: deletion retracts under the same auto_apply gate as saves.
    post_delete.connect(
        _as_delete_origin(_on_lacp_state_save),
        sender=NSOLACPBundleState,
        dispatch_uid="nso_plugin_lacp_bundle_state_post_delete",
        weak=False,
    )
    post_delete.connect(
        _as_delete_origin(_on_lacp_state_save),
        sender=NSOLACPMemberState,
        dispatch_uid="nso_plugin_lacp_member_state_post_delete",
        weak=False,
    )

    # Switchport state -> intent push + apply
    from .models import NSOSwitchportState

    post_save.connect(
        _on_switchport_state_save,
        sender=NSOSwitchportState,
        dispatch_uid="nso_plugin_switchport_state_post_save",
    )
    # Direct-apply family: deletion retracts under the same auto_apply gate as saves.
    post_delete.connect(
        _as_delete_origin(_on_switchport_state_save),
        sender=NSOSwitchportState,
        dispatch_uid="nso_plugin_switchport_state_post_delete",
        weak=False,
    )

    # IS-IS interface state → intent push
    from .models import NSOISISInstanceState, NSOISISInterfaceState

    post_save.connect(
        _on_isis_interface_state_save,
        sender=NSOISISInterfaceState,
        dispatch_uid="nso_plugin_isis_interface_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_isis_interface_state_save),
        sender=NSOISISInterfaceState,
        dispatch_uid="nso_plugin_isis_interface_state_post_delete",
        weak=False,
    )

    # IS-IS process (instance) state → intent push
    post_save.connect(
        _on_isis_instance_state_save,
        sender=NSOISISInstanceState,
        dispatch_uid="nso_plugin_isis_instance_state_post_save",
    )
    # No native ISISInstance pre_delete exists — this is the ONLY retraction path.
    post_delete.connect(
        _as_delete_origin(_on_isis_instance_state_save),
        sender=NSOISISInstanceState,
        dispatch_uid="nso_plugin_isis_instance_state_post_delete",
        weak=False,
    )

    # IS-IS Flex-Algo state → intent push
    from .models import NSOISISFlexAlgoState

    post_save.connect(
        _on_isis_flex_algo_state_save,
        sender=NSOISISFlexAlgoState,
        dispatch_uid="nso_plugin_isis_flex_algo_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_isis_flex_algo_state_save),
        sender=NSOISISFlexAlgoState,
        dispatch_uid="nso_plugin_isis_flex_algo_state_post_delete",
        weak=False,
    )

    # BGP peer state → intent push
    from .models import NSOBGPPeerState

    post_save.connect(
        _on_bgp_peer_state_save,
        sender=NSOBGPPeerState,
        dispatch_uid="nso_plugin_bgp_peer_state_post_save",
    )
    # Retraction path: deleting an overlay row pushes the reduced (owned-only) snapshot.
    # Fired both by a direct overlay delete and by the native BGPPeer pre_delete below,
    # which drops the row before the FK cascade would merely SET_NULL it.
    post_delete.connect(
        _as_delete_origin(_on_bgp_peer_state_save),
        sender=NSOBGPPeerState,
        dispatch_uid="nso_plugin_bgp_peer_state_post_delete",
        weak=False,
    )

    # Route-policy state → intent push
    from .models import NSORoutePolicyState

    post_save.connect(
        _on_route_policy_state_save,
        sender=NSORoutePolicyState,
        dispatch_uid="nso_plugin_route_policy_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_route_policy_state_save),
        sender=NSORoutePolicyState,
        dispatch_uid="nso_plugin_route_policy_state_post_delete",
        weak=False,
    )

    # netbox_routing policy object deletion → drop overlays + push removal (full-replace)
    try:
        from netbox_routing.models import (
            ASPath,
            ASPathEntry,
            CommunityList,
            CommunityListEntry,
            PrefixList,
            PrefixListEntry,
            RouteMap,
            RouteMapEntry,
        )

        for _model in (PrefixList, RouteMap, CommunityList, ASPath):
            pre_delete.connect(
                _as_delete_origin(_on_routing_policy_pre_delete),
                sender=_model,
                dispatch_uid=f"nso_plugin_routing_policy_pre_delete_{_model.__name__.lower()}",
                weak=False,
            )
            # Editing an owned policy object → re-own + push (mirror OSPF/IS-IS greenfield).
            post_save.connect(
                _on_routing_policy_object_save,
                sender=_model,
                dispatch_uid=f"nso_plugin_routing_policy_save_{_model.__name__.lower()}",
            )
        # Editing/adding/removing an ENTRY (member) → re-own + push its parent object.
        for _entry in (PrefixListEntry, RouteMapEntry, CommunityListEntry, ASPathEntry):
            post_save.connect(
                _on_routing_policy_entry_save,
                sender=_entry,
                dispatch_uid=f"nso_plugin_routing_policy_entry_save_{_entry.__name__.lower()}",
            )
            post_delete.connect(
                _as_delete_origin(_on_routing_policy_entry_delete),
                sender=_entry,
                dispatch_uid=f"nso_plugin_routing_policy_entry_delete_{_entry.__name__.lower()}",
                weak=False,
            )
    except ImportError:
        logger.debug("netbox_routing not installed — route-policy edit/delete signals not registered")

    # OSPF state → intent push
    from .models import NSOOSPFInstanceState, NSOOSPFInterfaceState

    post_save.connect(
        _on_ospf_instance_state_save,
        sender=NSOOSPFInstanceState,
        dispatch_uid="nso_plugin_ospf_instance_state_post_save",
    )
    post_save.connect(
        _on_ospf_interface_state_save,
        sender=NSOOSPFInterfaceState,
        dispatch_uid="nso_plugin_ospf_interface_state_post_save",
    )
    post_delete.connect(
        _as_delete_origin(_on_ospf_instance_state_save),
        sender=NSOOSPFInstanceState,
        dispatch_uid="nso_plugin_ospf_instance_state_post_delete",
        weak=False,
    )
    post_delete.connect(
        _as_delete_origin(_on_ospf_interface_state_save),
        sender=NSOOSPFInterfaceState,
        dispatch_uid="nso_plugin_ospf_interface_state_post_delete",
        weak=False,
    )

    # Redistribution state → intent push
    from .models import NSORedistributionState

    post_save.connect(
        _on_redistribution_state_save,
        sender=NSORedistributionState,
        dispatch_uid="nso_plugin_redistribution_state_post_save",
    )
    # No native Redistribution pre_delete exists — this is the ONLY retraction path.
    post_delete.connect(
        _as_delete_origin(_on_redistribution_state_save),
        sender=NSORedistributionState,
        dispatch_uid="nso_plugin_redistribution_state_post_delete",
        weak=False,
    )

    # netbox_routing.Redistribution fork save → intent push (routing accept path B)
    try:
        from netbox_routing.models import Redistribution

        post_save.connect(
            _on_redistribution_fork_save,
            sender=Redistribution,
            dispatch_uid="nso_plugin_redistribution_fork_post_save",
        )
    except ImportError:
        logger.debug("netbox_routing not installed — redistribution fork signal not registered")

    # netbox_routing.StaticRoute greenfield write path (operator-created routes → push)
    try:
        from netbox_routing.models import StaticRoute

        # The pre_save stash is what makes post_save delta-gated; without it every save
        # (including one that touched only ``name``) would bump a generation and push.
        pre_save.connect(
            _on_routing_static_route_pre_save,
            sender=StaticRoute,
            dispatch_uid="nso_plugin_routing_static_route_pre_save",
        )
        post_save.connect(
            _on_routing_static_route_save,
            sender=StaticRoute,
            dispatch_uid="nso_plugin_routing_static_route_post_save",
        )
        # NOT _as_delete_origin: m2m_changed also carries post_add — the handler opens the
        # deletion mark itself, around its post_remove / post_clear branches only.
        m2m_changed.connect(
            _on_routing_static_route_devices_changed,
            sender=StaticRoute.devices.through,
            dispatch_uid="nso_plugin_routing_static_route_devices_changed",
        )
        pre_delete.connect(
            _as_delete_origin(_on_routing_static_route_pre_delete),
            sender=StaticRoute,
            dispatch_uid="nso_plugin_routing_static_route_pre_delete",
            weak=False,
        )
    except ImportError:
        logger.debug("netbox_routing not installed — static-route greenfield signals not registered")

    # netbox_routing OSPF greenfield write path (operator-created OSPF → accepted overlay → push)
    try:
        from netbox_routing.models import OSPFInstance, OSPFInterface

        post_save.connect(
            _on_routing_ospf_instance_save,
            sender=OSPFInstance,
            dispatch_uid="nso_plugin_routing_ospf_instance_post_save",
        )
        post_save.connect(
            _on_routing_ospf_interface_save,
            sender=OSPFInterface,
            dispatch_uid="nso_plugin_routing_ospf_interface_post_save",
        )
        pre_delete.connect(
            _as_delete_origin(_on_routing_ospf_instance_pre_delete),
            sender=OSPFInstance,
            dispatch_uid="nso_plugin_routing_ospf_instance_pre_delete",
            weak=False,
        )
        pre_delete.connect(
            _as_delete_origin(_on_routing_ospf_interface_pre_delete),
            sender=OSPFInterface,
            dispatch_uid="nso_plugin_routing_ospf_interface_pre_delete",
            weak=False,
        )
    except ImportError:
        logger.debug("netbox_routing not installed — OSPF greenfield signals not registered")

    # netbox_routing.BGPPeer greenfield write path (operator-created peers → accepted overlay → push)
    try:
        from netbox_routing.models import BGPPeer

        post_save.connect(
            _on_routing_bgp_peer_save,
            sender=BGPPeer,
            dispatch_uid="nso_plugin_routing_bgp_peer_post_save",
        )
        pre_delete.connect(
            _as_delete_origin(_on_routing_bgp_peer_pre_delete),
            sender=BGPPeer,
            dispatch_uid="nso_plugin_routing_bgp_peer_pre_delete",
            weak=False,
        )
    except ImportError:
        logger.debug("netbox_routing not installed — BGP peer greenfield signals not registered")

    # netbox_routing.ISISFlexAlgo greenfield write path (operator-created flex-algos → push)
    try:
        from netbox_routing.models import ISISFlexAlgo

        post_save.connect(
            _on_routing_isis_flex_algo_save,
            sender=ISISFlexAlgo,
            dispatch_uid="nso_plugin_routing_isis_flex_algo_post_save",
        )
        pre_delete.connect(
            _as_delete_origin(_on_routing_isis_flex_algo_pre_delete),
            sender=ISISFlexAlgo,
            dispatch_uid="nso_plugin_routing_isis_flex_algo_pre_delete",
            weak=False,
        )
    except ImportError:
        logger.debug("netbox_routing not installed — flex-algo greenfield signals not registered")

    # netbox_routing.ISISLevel write path (per-level tuning rides the process intent)
    try:
        from netbox_routing.models import ISISLevel

        post_save.connect(
            _on_routing_isis_level_save,
            sender=ISISLevel,
            dispatch_uid="nso_plugin_routing_isis_level_post_save",
        )
        post_delete.connect(
            _as_delete_origin(_on_routing_isis_level_post_delete),
            sender=ISISLevel,
            dispatch_uid="nso_plugin_routing_isis_level_post_delete",
            weak=False,
        )
    except ImportError:
        logger.debug("netbox_routing not installed — ISIS level signals not registered")

    # netbox_routing.ISISInterface greenfield write path (operator-edited metric /
    # network-type / circuit-type → owned overlay → push), parity with OSPF.
    try:
        from netbox_routing.models import ISISInterface

        post_save.connect(
            _on_routing_isis_interface_save,
            sender=ISISInterface,
            dispatch_uid="nso_plugin_routing_isis_interface_post_save",
        )
        pre_delete.connect(
            _as_delete_origin(_on_routing_isis_interface_pre_delete),
            sender=ISISInterface,
            dispatch_uid="nso_plugin_routing_isis_interface_pre_delete",
            weak=False,
        )
    except ImportError:
        logger.debug("netbox_routing not installed — IS-IS interface greenfield signals not registered")
