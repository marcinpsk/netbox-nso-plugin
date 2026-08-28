# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Signal handlers for NSODeviceManagement scope propagation and intent push."""

import contextlib
import contextvars
import copy
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
    ned_id = (
        NSOPlatformNedMapping.objects.filter(platform_id=platform_id)
        .order_by()
        .values_list("ned_id", flat=True)
        .first()
    )
    return str(ned_id or "")


def _is_intent_push_suppressed() -> bool:
    return getattr(_intent_push_suppressed, "active", False)


def _converted_writer_owns_content(device_id, scope) -> bool:
    """Return whether a converted behavior signal belongs to its explicit writer."""
    from .renderer_writer import renderer_writer_owns_key

    return renderer_writer_owns_key(device_id, scope, content=True)


def _require_converted_writer(handler):
    """Run a converted state handler only inside an exact writer context."""

    @functools.wraps(handler)
    def _wrapped(*args, **kwargs):
        from .renderer_writer import active_renderer_writer

        if active_renderer_writer() is None:
            return None
        return handler(*args, **kwargs)

    return _wrapped


def _schedule_exact_writer_scope(target_scope) -> None:
    """Schedule keys for one scope only from its active exact content writer."""
    from .renderer_writer import active_renderer_writer

    writer = active_renderer_writer()
    if writer is None:
        return
    for device_id, scope in writer.plan.content_keys:
        if scope == target_scope and _converted_writer_owns_content(device_id, scope):
            _schedule_intent_push((device_id, scope))


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
        try:
            # suppress_intent_push() (the reconcile/import path) is the authoritative
            # guard; the GET-render check is a belt-and-suspenders for the legacy
            # render-time reconcile and becomes redundant once render is read-only.
            if _is_intent_push_suppressed() or _is_render_request():
                return None
            return handler(*args, **kwargs)
        except BaseException:
            from .intent_state import _abort_m2m_implicit, _end_implicit

            sender = kwargs.get("sender", args[0] if len(args) > 0 else None)
            instance = kwargs.get("instance", args[1] if len(args) > 1 else None)
            action = kwargs.get("action", args[2] if len(args) > 2 else None)
            details = {key: value for key, value in kwargs.items() if key not in {"sender", "instance", "action"}}
            if action is not None:
                _abort_m2m_implicit(sender, instance)
            else:
                _end_implicit(sender, instance, **details)
            raise

    return _wrapped


def _close_renderer_m2m_permit(handler):
    """Close an implicit M2M permit even when a behavior handler raises."""

    @functools.wraps(handler)
    def _wrapped(sender, instance, action, **kwargs):
        try:
            return handler(sender, instance, action, **kwargs)
        finally:
            if action.startswith("post_"):
                from .intent_state import _end_m2m_implicit

                _end_m2m_implicit(sender, instance, action, **kwargs)

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
    from .intent_state import mirror_refresh_is_active

    if _is_intent_push_suppressed() or _is_render_request() or mirror_refresh_is_active():
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
            _update_management_mirror(mgmt, intent_push_attempts=attempts)
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
            _update_management_mirror(mgmt, intent_push_errors=errors)
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

    The claim drain reaches this through :func:`drain.send_claim`, which calls
    :func:`delivery.send`. Direct delivery reaches this through :func:`delivery.deliver`,
    which also calls :func:`delivery.send`. It raises on a failed call so each caller can
    tell a failure from a success.
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


def interface_intent_item(state):
    """Return one interface attribute in the adapter's exact wire shape."""
    iface = state.interface
    if state.attribute == "description":
        intent_value = iface.description or ""
    elif state.attribute == "enabled":
        intent_value = str(iface.enabled).lower()
    else:
        return None
    return {
        "interface": iface.name,
        "attribute": state.attribute,
        "intent_value": intent_value,
        "accepted_at": state.accepted_at.isoformat() if state.accepted_at else None,
    }


def _push_interface_intent_for_device(device_id, adapter_device_id) -> None:
    """Build and capture the full OWNED interface intent snapshot.

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
        if item := interface_intent_item(state):
            attributes.append(item)

    _push_changed(
        (device_id, "interface"),
        attributes,
        lambda body: client.put_intent(adapter_device_id, body),
    )


#: The destination protocols a redistribution change can be scheduled against. It is the
#: delivery key itself, so an unknown value names no renderer and must be refused, not sent.
_REDISTRIBUTION_PROTOCOLS = ("ospf", "isis", "bgp")
_MANAGEMENT_MIRROR_FIELDS = frozenset(
    {
        "adapter_device_id",
        "adapter_incarnation_born",
        "adapter_source_epoch",
        "source_epoch_aware",
        "source_rekey_pending",
        "reset_pending_source_epoch",
        "reset_pending_incarnation",
        "reset_pending_born",
        "reset_conflict_born",
        "adapter_incarnation",
        "adapter_link_error",
        "adapter_link_attempted_at",
        "settle_cursor_seq",
        "settle_cursor_incarnation",
        "settle_cursor_device_id",
        "settle_stall_seq",
        "settle_stall_attempts",
        "settle_stall_first_seen_at",
        "onboard_status",
        "onboard_error",
        "onboarded_at",
        "onboard_steps",
        "onboard_job_id",
        "last_sync_at",
        "last_sync_status",
        "degraded_surfaces",
        "last_journaled_apply_job",
        "state_snapshot",
        "intent_push_attempts",
        "intent_push_errors",
    }
)


def redistribution_destinations() -> tuple[str, ...]:
    """Return the one allow-list both scheduling paths refuse against."""
    from . import delivery

    return tuple(dest for dest in _REDISTRIBUTION_PROTOCOLS if dest in delivery.delivery_keys())


def _schedule_redistribution_push(device_id, dest) -> None:
    """Schedule the destination protocol's intent push for a redistribution change.

    Keyed by (device, dest_protocol) so redistribution and the protocol's own state
    saves fold into a single push for that protocol.
    """
    if dest not in redistribution_destinations():
        logger.warning(
            "Redistribution: unknown dest_protocol %r for device %s — no push triggered",
            dest,
            device_id,
        )
        return
    _schedule_intent_push((device_id, dest))


@receiver(pre_save, sender="netbox_nso_plugin.NSODeviceManagement")
def remember_adapter_source(sender, instance, **kwargs):
    """Verify that a source change already carries its fail-closed writer fence."""
    from .renderer_writer import active_renderer_writer

    if active_renderer_writer() is None:
        return
    if not instance.pk:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields and set(update_fields) <= _MANAGEMENT_MIRROR_FIELDS:
        return
    previous = sender.objects.filter(pk=instance.pk).values_list("nso_instance_id", "nso_device_name").first()
    changed = previous is not None and previous != (instance.nso_instance_id, instance.nso_device_name)
    if changed and not instance.source_rekey_pending:
        from .intent_state import IntentMutationProtocolError

        raise IntentMutationProtocolError("a management source change omitted its source-rekey fence")


def _invalidate_source_admissions(instance) -> int:
    """Fence every in-flight family body before a remote source rekey."""
    from django.db.models import F

    from .models import NSOFamilyReadState
    from .read_gate import _RESET_FIELDS

    return NSOFamilyReadState.objects.filter(management=instance).update(
        **_RESET_FIELDS,
        publication_sequence=F("publication_sequence") + 1,
    )


def _sync_source_change(instance, client) -> bool:
    """Fence, call, then finalize a source rekey in two durable transactions."""
    from django.db import transaction

    from .apply_state import lock_device_intent_transaction, lock_order_scope
    from .deployment import lock_mutation

    management_model = type(instance)
    expected_source = (instance.nso_instance_id, instance.nso_device_name)
    with transaction.atomic(), lock_order_scope():
        lock_mutation()
        lock_device_intent_transaction(instance.device_id)
        # of=("self",): without it the joined instance row is locked too, and every device shares it.
        current = (
            management_model.objects.select_for_update(of=("self",)).select_related("nso_instance").get(pk=instance.pk)
        )
        if (current.nso_instance_id, current.nso_device_name) != expected_source:
            return False
        invalidated = _invalidate_source_admissions(current)

    from .adapter_client import AdapterError

    try:
        result = client.patch_device(
            adapter_device_id=current.adapter_device_id,
            nso_instance=current.nso_instance.adapter_instance_id,
            nso_device_name=current.nso_device_name,
        )
    except AdapterError as exc:
        # The old adapter mapping can disappear before the rekey runs. Onboard the new
        # durable source identity and finalize it through the same fenced second phase.
        if exc.code != "not_found":
            raise
        logger.warning(
            "Rekey target %s is gone for NetBox device %s. Re-onboarding the new source.",
            current.adapter_device_id,
            current.device_id,
        )
        _onboard_into_adapter(current, client)
        instance.adapter_device_id = current.adapter_device_id
        result = {"source_epoch": current.adapter_source_epoch}
    if result.get("source_epoch") is None:
        raise RuntimeError("adapter rekey response omitted source_epoch; publication remains fenced")
    from .intent_state import IntentMutationProtocolError, footprint_for_instance, mirror_transaction
    from .management_lifecycle import save_management

    with mirror_transaction(footprint_for_instance(instance)):
        current = management_model.objects.get(pk=instance.pk)
        if (current.nso_instance_id, current.nso_device_name) != expected_source:
            return False
        source_epoch = result["source_epoch"]
        source_aware = True
        current.adapter_source_epoch = source_epoch
        current.source_epoch_aware = source_aware
        current.source_rekey_pending = False
        current.reset_pending_source_epoch = source_epoch if invalidated else None
        try:
            save_management(
                current,
                update_fields=[
                    "adapter_source_epoch",
                    "source_epoch_aware",
                    "source_rekey_pending",
                    "reset_pending_source_epoch",
                ],
            )
        except IntentMutationProtocolError:
            latest = (
                management_model.objects.filter(pk=instance.pk)
                .values_list("nso_instance_id", "nso_device_name")
                .first()
            )
            if latest != expected_source:
                return False
            raise
    instance.adapter_source_epoch = source_epoch
    instance.source_epoch_aware = source_aware
    instance.source_rekey_pending = False
    return True


def _onboard_into_adapter(instance, client):
    """Register the device with the adapter and store the returned mapping on the row.

    The adapter identity fields are admission metadata, not renderer content.
    """
    result = client.onboard_device(
        nso_instance=instance.nso_instance.adapter_instance_id,
        nso_device_name=instance.nso_device_name,
        netbox_device_id=instance.device_id,
    )
    from .management_lifecycle import save_management

    current = type(instance).objects.get(pk=instance.pk)
    current.adapter_device_id = result["id"]
    current.adapter_source_epoch = result.get("source_epoch")
    current.source_epoch_aware = result.get("source_epoch") is not None
    save_management(
        current,
        update_fields=["adapter_device_id", "adapter_source_epoch", "source_epoch_aware"],
    )
    instance.adapter_device_id = current.adapter_device_id
    instance.adapter_source_epoch = current.adapter_source_epoch
    instance.source_epoch_aware = current.source_epoch_aware


@receiver(post_save, sender="netbox_nso_plugin.NSODeviceManagement")
def sync_scope_to_adapter(sender, instance, created, update_fields=None, **kwargs):
    """Run adapter side effects only after the management-row transaction commits."""
    from .renderer_writer import active_renderer_writer

    if active_renderer_writer() is None:
        return
    _queue_scope_sync(sender, instance, created, update_fields=update_fields)


def _queue_scope_sync(sender, instance, created, *, update_fields=None):
    """Queue the adapter-link work for one sanctioned management-row save."""
    from django.db import transaction

    if update_fields and set(update_fields) <= _MANAGEMENT_MIRROR_FIELDS:
        return
    if getattr(instance, "onboard_status", "") in ("provisioning", "provision_failed"):
        return
    transaction.on_commit(lambda: _sync_committed_scope_to_adapter(sender, instance.pk, created))


def _update_management_mirror(instance, **values):
    """Persist management lifecycle fields through the exact mirror writer."""
    from .management_lifecycle import save_management

    fields = set(values)
    current = type(instance).objects.filter(pk=instance.pk).first()
    if current is None:
        return
    for field_name, value in values.items():
        setattr(current, field_name, value)
    save_management(current, update_fields=fields)
    for field_name, value in values.items():
        setattr(instance, field_name, value)


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
        # goes away.
        if instance.adapter_link_error:
            _update_management_mirror(instance, adapter_link_error="")
    except Exception as exc:
        logger.warning("Failed to sync scope to adapter for device %s: %s", instance.device_id, exc)
        # Surface the failure on the row instead of only logging it: otherwise the device looks
        # managed in NetBox while silently unlinked from the adapter (adapter_device_id stays None),
        # with nothing mirrored/applied and no operator-visible signal.
        from .adapter_client import public_error_message

        message = (
            public_error_message(exc)
            if isinstance(exc, AdapterError)
            else "The adapter link failed. See the server log."
        )
        instance.adapter_link_error = message
        # Persist for the tab banner ONLY if the failure didn't already break the surrounding
        # transaction: a DB-origin error in the try (e.g. a bad adapter response fed into the
        # adapter_device_id update) marks the connection needs_rollback, and writing then raises
        # TransactionManagementError — and that save is rolling back regardless, so recording is moot.
        from django.db import connection

        if not connection.needs_rollback:
            from .intent_state import RendererTargetsChanged
            from .renderer_writer import IntentPlanStaleError

            try:
                _update_management_mirror(instance, adapter_link_error=message)
            except (IntentPlanStaleError, RendererTargetsChanged) as mirror_exc:
                logger.warning(
                    "Skipped adapter error mirror update for management row %s because its renderer plan changed: %s",
                    instance.pk,
                    mirror_exc,
                )


@receiver(post_delete, sender="netbox_nso_plugin.NSODeviceManagement")
def offboard_device_from_adapter(sender, instance, **kwargs):
    """Remove the device from the adapter once the management row's deletion COMMITS.

    Deferred to on_commit for two reasons. A rolled-back delete must not have already
    offboarded the device adapter-side; and while the deleting transaction is open the row is
    still visible to other connections, so a concurrent save (or the periodic link repair)
    could see the mapping, get a 404 from the just-deleted adapter device, and re-onboard a
    fresh row that nothing then owns — this handler has already fired against the old id.
    """
    from .renderer_writer import active_renderer_writer

    origin = kwargs.get("origin")
    origin_meta = getattr(origin, "_meta", None) or getattr(getattr(origin, "model", None), "_meta", None)
    cascaded_from_device = getattr(origin_meta, "label_lower", None) == "dcim.device"
    if active_renderer_writer() is None and not cascaded_from_device:
        return
    _queue_adapter_offboard(instance)


def _queue_adapter_offboard(instance):
    """Queue adapter offboarding for one sanctioned management-row delete."""
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
    if not _converted_writer_owns_content(device_id, "interface"):
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
    """Recompute one managed description through an exact writer.

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
    candidate = copy.copy(interface)
    candidate.description = new_value

    from .renderer_writer import (
        RendererMutationPlan,
        active_renderer_writer,
        planned_save,
        renderer_mirror_writes,
        renderer_writes,
    )

    active = active_renderer_writer()
    if active is not None:
        active.save(candidate, update_fields=("description",))
        return
    plan = RendererMutationPlan.build(saves=(planned_save(candidate, update_fields=("description",)),))
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer:
        writer.save(candidate, update_fields=("description",))


def _recompute_on_cable_change(sender, instance, **kwargs):
    """Recompute planned cable endpoints only for an active interface writer."""
    templates = _templates()
    if not templates:
        return
    for iface in _affected_interfaces(instance):
        if not _converted_writer_owns_content(iface.device_id, "interface"):
            continue
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
        if not _converted_writer_owns_content(iface.device_id, "interface"):
            continue
        # The termination objects retained by Django's post_delete signal still carry
        # the deleted cable's PK. NetBox's cached ``link_peers`` property would follow
        # that stale FK and raise Cable.DoesNotExist instead of seeing a disconnected
        # interface. Mirror the database's post-delete state on the in-memory object.
        iface.cable_id = None
        iface._state.fields_cache.pop("cable", None)
        iface.__dict__.pop("link_peers", None)
        _recompute_one(iface, templates)


def _recompute_on_interface_save(sender, instance, created, **kwargs):
    """Consume a preplanned derived description only for an exact interface writer."""
    if (
        _is_intent_push_suppressed()
        or _is_adapter_origin_write()
        or not _converted_writer_owns_content(instance.device_id, "interface")
    ):
        return
    templates = _templates()
    if not templates:
        return
    _recompute_one(instance, templates)


def _stash_interface_old_values(sender, instance, **kwargs):
    """Capture the owned scope targets of a planned interface rename."""
    if not instance.pk:
        instance._intent_rename_targets = set()
        return
    instance._intent_rename_targets = set()
    update_fields = kwargs.get("update_fields")
    if (
        _is_intent_push_suppressed()
        or _is_render_request()
        or _is_adapter_origin_write()
        or (update_fields is not None and "name" not in update_fields)
    ):
        return

    current_name = sender.objects.filter(pk=instance.pk).values_list("name", flat=True).first()
    if current_name is None or instance.name == current_name:
        return
    from .apply_state import interface_intent_targets

    device_ids, scopes = interface_intent_targets(instance.pk)
    instance._intent_rename_targets = {(device_id, scope) for device_id in device_ids for scope in scopes}


@_skip_on_render
def _repend_intent_on_interface_rename(sender, instance, created, **kwargs):
    """Queue exact-writer scopes whose payload contains a renamed interface."""
    if created:
        return
    from . import delivery
    from .models import NSODeviceManagement

    targets = getattr(instance, "_intent_rename_targets", set())
    targets = {key for key in targets if _converted_writer_owns_content(*key)}
    if not targets:
        return
    auto_apply = dict(
        NSODeviceManagement.objects.filter(device_id__in={device_id for device_id, _scope in targets}).values_list(
            "device_id", "auto_apply"
        )
    )
    for key in sorted(targets):
        device_id, scope = key
        if not delivery.delivery_keys()[scope].in_protocol and not auto_apply.get(device_id, False):
            continue
        _schedule_intent_push(key)


@_skip_on_render
def _push_intent_on_interface_edit(sender, instance, created, **kwargs):
    """Schedule explicit interface writer changes without acquiring in a signal.

    The mutation planner owns the interface and overlay transition. The signal only
    schedules behavior when that exact content writer is active. A foreign native
    save is not ownership evidence and has no side effects.
    """
    if created or not _converted_writer_owns_content(instance.device_id, "interface"):
        return
    _schedule_intent_push((instance.device_id, "interface"))


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

    if not _converted_writer_owns_content(instance.device_id, "subinterface"):
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


def interface_ip_intent_item(row):
    """Return one interface address in the adapter's exact wire shape."""
    entry = {
        "interface": row.interface.name,
        "address": row.address,
        "family": row.family,
        "secondary": bool(row.secondary),
        "vrf": row.vrf,
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
    }
    entry.update(_nokia_routed_binding(row.interface))
    return entry


def _push_ip_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IP intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOInterfaceIPState

    ip_states = NSOInterfaceIPState.objects.filter(
        interface__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface", "interface__parent")

    addresses = [interface_ip_intent_item(ip_state) for ip_state in ip_states]

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


def snmp_vault_ref_push_blocker(row) -> str:
    """Why *row* cannot be faithfully pushed, or "" when it can."""
    if not row.vault_ref:
        return "this owned SNMP row has no Vault reference"
    return ""


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
    if not row.vault_ref:
        return (
            "this SNMPv3 user has no Vault reference — the full-replace snapshot would omit it. "
            "Set the Vault reference first."
        )
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


def _snmp_push_blockers(rows, blocker):
    """Return the reasons that prevent a complete SNMP snapshot."""
    blocked = []
    for row in rows:
        reason = blocker(row)
        if reason:
            logger.warning("SNMP intent: %s blocks the full snapshot: %s", row, reason)
            blocked.append(f"{row}: {reason}")
    return blocked


def snmp_community_intent_item(row):
    """Return one SNMP community in the adapter's exact wire shape."""
    return {
        "label": row.community_hash,
        "vault_ref": row.vault_ref,
        "access": row.access,
        "acl": row.acl or None,
    }


def snmp_v3_user_intent_item(row):
    """Return one SNMPv3 user in the adapter's exact wire shape."""
    return {
        "username": row.username,
        "group": row.group_name or None,
        "auth_protocol": row.auth_protocol or None,
        "priv_protocol": row.priv_protocol or None,
        "auth_vault_ref": f"{row.vault_ref}#auth" if row.auth_protocol else None,
        "priv_vault_ref": f"{row.vault_ref}#priv" if row.priv_protocol else None,
    }


def snmp_host_intent_item(row, ned_id):
    """Return one SNMP notification host in the adapter's exact wire shape."""
    host = {
        "address": row.address,
        "version": row.version,
        "notify_type": row.notify_type,
        "community_or_user": (row.username if _host_is_v3(row.version) else row.community_hash) or "",
    }
    suppress_default_port = row.port == 162 and ned_id.startswith(
        ("timos", "arcos-", "cisco-ios-cli", "cisco-iosxe-cli")
    )
    if row.port is not None and not suppress_default_port:
        host["port"] = row.port
    return host


def snmp_system_info_intent_item(row):
    """Return the SNMP system-information singleton in its exact wire shape."""
    return {"location": row.location or None, "contact": row.contact or None}


def _push_snmp_intent_for_device(device_id, adapter_device_id):
    """Build and push the full SNMP intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOSnmpCommunityState, NSOSnmpHostState, NSOSnmpSystemInfoState, NSOSnmpV3UserState

    owned_communities = list(
        NSOSnmpCommunityState.objects.filter(
            management__device_id=device_id,
            status__in=_OWNED_PUSH_STATUSES,
        ).select_related("management")
    )
    blocked = _snmp_push_blockers(owned_communities, snmp_vault_ref_push_blocker)
    communities = []
    for row in owned_communities:
        communities.append(snmp_community_intent_item(row))

    owned_v3 = list(
        NSOSnmpV3UserState.objects.filter(
            management__device_id=device_id,
            status__in=_OWNED_PUSH_STATUSES,
        )
    )
    blocked.extend(_snmp_push_blockers(owned_v3, snmp_v3_user_push_blocker))
    blocked.extend(_snmp_push_blockers(owned_v3, snmp_vault_ref_push_blocker))
    v3_users = []
    for row in owned_v3:
        # vault_ref is a PATH ref ("mount/path"); the auth/priv fields live at
        # "#auth"/"#priv" by convention. A leg without its protocol is not
        # derivable on-device, so its ref is withheld (the reconciler would
        # otherwise resolve a secret it cannot apply). snmp_v3_user_push_blocker
        # has already rejected the case where the DEVICE holds a secret whose
        # protocol was never declared — withholding there would downgrade the user.
        v3_users.append(snmp_v3_user_intent_item(row))

    ned_id = _ned_id_for_device(device_id)
    hosts = []
    owned_hosts = list(
        NSOSnmpHostState.objects.filter(
            management__device_id=device_id,
            status__in=_OWNED_PUSH_STATUSES,
        )
    )
    blocked.extend(_snmp_push_blockers(owned_hosts, snmp_host_push_blocker))
    for row in owned_hosts:
        hosts.append(snmp_host_intent_item(row, ned_id))

    system_info = None
    try:
        sysinfo = NSOSnmpSystemInfoState.objects.get(
            management__device_id=device_id,
        )
        if sysinfo.status in _OWNED_PUSH_STATUSES:
            system_info = snmp_system_info_intent_item(sysinfo)
    except NSOSnmpSystemInfoState.DoesNotExist:
        pass

    payload = {"blocked": blocked} if blocked else [communities, v3_users, hosts, system_info]

    def push(body):
        if isinstance(body, dict) and body.get("blocked"):
            from .adapter_client import AdapterError

            raise AdapterError(
                f"SNMP snapshot is blocked: {'; '.join(body['blocked'])}",
                code="validation_error",
                detail={"reason": "blocked_owned_row"},
            )
        return client.put_snmp_intent(adapter_device_id, *body)

    _push_changed((device_id, "snmp"), payload, push)


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "snmp"):
        return
    _schedule_intent_push((device_id, "snmp"))


def logging_host_intent_item(row, ned_id):
    """Return one remote logging host in the adapter's exact wire shape."""
    from .template_content import _canonical_logging_intent_field

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
    return host


def logging_levels_intent_item(row):
    """Return the local logging singleton, including its explicit null shape."""
    return row.set_severities() or None


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

    ned_id = _ned_id_for_device(device_id)
    hosts = []
    for row in NSOLoggingHostState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        hosts.append(logging_host_intent_item(row, ned_id))

    local_levels = None
    levels_row = NSOLoggingLevelState.objects.filter(management__device_id=device_id).first()
    if levels_row is not None and levels_row.status in _OWNED_PUSH_STATUSES:
        # An owned row with every severity blank manages nothing → null (un-manage);
        # the #83 cleared-owned-scalar shape, same as deleting the row.
        local_levels = logging_levels_intent_item(levels_row)

    _push_changed(
        (device_id, "logging"),
        [hosts, local_levels],
        lambda body: client.put_logging_intent(adapter_device_id, *body),
    )


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "logging"):
        return
    _schedule_intent_push((device_id, "logging"))


def svi_intent_item(row):
    """Return one SVI in the adapter's exact wire shape, or None when unkeyed."""
    vid = row.vlan.vid if row.vlan else None
    if vid is None:
        return None
    return {
        "interface_name": row.interface.name,
        "vlan_id": vid,
        "type": row.svi_type or "svi",
        "vrf": row.vrf or "",
    }


def _push_svi_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned SVI/IRB intent snapshot for a device.

    Store-only (deferred): the single device Apply commits via the svi-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.
    """
    from . import adapter_client as client
    from .models import NSOSVIState

    interfaces = []
    for row in NSOSVIState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface", "vlan"):
        if item := svi_intent_item(row):
            interfaces.append(item)

    _push_changed(
        (device_id, "svi"),
        interfaces,
        lambda body: client.put_svi_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "svi"):
        return
    _schedule_intent_push((device_id, "svi"))


def subinterface_intent_item(row):
    """Return one dot1q subinterface in its exact wire shape, or None when unkeyed."""
    if row.dot1q_vlan is None or row.parent_interface is None:
        return None
    return {
        "interface_name": row.interface.name,
        "parent_interface": row.parent_interface.name,
        "dot1q_vlan": row.dot1q_vlan,
        "type": "subinterface",
        "vrf": row.vrf or "",
    }


def _push_subinterface_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned dot1q subinterface intent snapshot.

    Store-only (deferred): the single device Apply commits via the
    subinterface-reconciler. Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) included.
    """
    from . import adapter_client as client
    from .models import NSOSubinterfaceState

    interfaces = []
    for row in NSOSubinterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface", "parent_interface"):
        if item := subinterface_intent_item(row):
            interfaces.append(item)

    _push_changed(
        (device_id, "subinterface"),
        interfaces,
        lambda body: client.put_subinterface_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "subinterface"):
        return
    _schedule_intent_push((device_id, "subinterface"))


def interface_mtu_intent_item(row):
    """Return one MTU intent item, or None when it manages no MTU leaf."""
    if row.l2_mtu is None and row.ip_mtu is None and row.mpls_mtu is None:
        return None
    return {
        "interface_name": row.interface.name,
        "mtu": row.l2_mtu,
        "ip_mtu": row.ip_mtu,
        "mpls_mtu": row.mpls_mtu,
    }


def _push_interface_mtu_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned per-interface MTU intent snapshot (Phase 2b).

    Store-only (deferred): the single device Apply commits via the mtu-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.
    """
    from . import adapter_client as client
    from .models import NSOInterfaceMtuState

    interfaces = []
    for row in NSOInterfaceMtuState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface"):
        if item := interface_mtu_intent_item(row):
            interfaces.append(item)

    _push_changed(
        (device_id, "interface_mtu"),
        interfaces,
        lambda body: client.put_interface_mtu_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "interface_mtu"):
        return
    _schedule_intent_push((device_id, "interface_mtu"))


def vlan_intent_item(row):
    """Return one VLAN row in the adapter's exact wire shape."""
    from .vlan_reconciler import rendered_vlan_name

    if row.vlan is None:
        return None
    return {"vlan_id": row.vlan.vid, "name": rendered_vlan_name(row)}


def _push_vlan_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned VLAN-database intent snapshot for a device (write).

    Store-only (deferred): the single device Apply commits via the vlan-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included; the VLAN name pushed
    is the LIVE NetBox name (operator is the source of truth for it).
    """
    from . import adapter_client as client
    from .models import NSOVLANState

    vlans = []
    for row in NSOVLANState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("vlan"):
        if item := vlan_intent_item(row):
            vlans.append(item)

    _push_changed(
        (device_id, "vlan"),
        vlans,
        lambda body: client.put_vlan_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "vlan"):
        return
    _schedule_intent_push((device_id, "vlan"))


@_skip_on_render
def _on_vlan_pre_save(sender, instance, **kwargs):
    """Record the exact VLAN or SVI fields changed by this save."""
    update_fields = kwargs.get("update_fields")
    candidate_fields = {"name", "vid"}
    if update_fields is not None:
        candidate_fields.intersection_update(update_fields)
    instance._intent_vlan_changed_fields = frozenset()
    instance._intent_vlan_rows = {}
    if instance._state.adding or not candidate_fields:
        return

    from .apply_state import vlan_intent_targets

    scopes = ("vlan", "svi", "switchport") if "vid" in candidate_fields else ("vlan",)
    current_vlan = sender.objects.filter(pk=instance.pk).first()
    if current_vlan is None:
        return
    _device_ids, rows = vlan_intent_targets(instance.pk, scopes)
    changed_fields = {field for field in candidate_fields if getattr(current_vlan, field) != getattr(instance, field)}
    instance._intent_vlan_changed_fields = frozenset(changed_fields)
    instance._intent_vlan_rows = rows


@_skip_on_render
def _on_vlan_change(sender, instance, **kwargs):
    """Surface a NetBox VLAN intent change and queue each affected snapshot.

    Editing an ``ipam.VLAN`` fires no NSOVLANState signal, so the overlay would
    otherwise sit at a stale ``in_sync``/``imported`` until the next full reconcile.
    A name change affects VLAN intent. A VID change affects VLAN, SVI, and switchport intent.
    The pre-save locks serialize these shared native fields with Apply promotion.
    """
    changed_fields = getattr(instance, "_intent_vlan_changed_fields", frozenset())
    if not changed_fields:
        return
    from . import delivery
    from . import status_machine as sm
    from .intent_state import revision_was_acquired

    rows = getattr(instance, "_intent_vlan_rows", {})
    vid_changed = "vid" in changed_fields
    targets = set()
    for scope, states in rows.items():
        if scope in ("svi", "switchport") and not vid_changed:
            continue
        for state in states:
            was_owned = sm.is_owned(state.status)
            entry = delivery.delivery_keys()[scope]
            may_deliver = entry.in_protocol or state.management.auto_apply
            if (
                was_owned
                and state.management.adapter_device_id is not None
                and may_deliver
                and (
                    _converted_writer_owns_content(state.management.device_id, scope)
                    or revision_was_acquired(state.management.device_id, scope)
                )
            ):
                targets.add((state.management.device_id, scope))
    for key in sorted(targets):
        _schedule_intent_push(key)


@_skip_on_render
def _on_ipam_vlan_pre_delete(sender, instance, **kwargs):
    """VLAN deleted in NetBox → push the reduced VLAN intent to each attached device.

    Deleting an ipam.VLAN cascade-deletes its NSOVLANState overlays but fires no
    per-overlay signal, so without this the device keeps the (now-orphaned) VLAN.
    Capture the attached devices *before* the cascade, then schedule a deferred push;
    by the time it runs (post-commit) the overlays are gone, so the snapshot omits this
    vid and the adapter PUT-replaces the vlan-reconciler instance → FASTMAP reverts it.
    """
    from .intent_state import revision_was_acquired

    targets = []
    for state in instance.nso_vlan_states.select_related("management").all():
        mgmt = state.management
        if mgmt.adapter_device_id is not None and (
            _converted_writer_owns_content(mgmt.device_id, "vlan") or revision_was_acquired(mgmt.device_id, "vlan")
        ):
            targets.append((mgmt.device_id, mgmt.adapter_device_id))
    for device_id, adapter_device_id in targets:
        _schedule_intent_push((device_id, "vlan"))


def bfd_intent_item(row):
    """Return one BFD interface in the adapter's exact wire shape."""
    return {
        "interface_name": row.interface.name,
        "min_tx": row.min_tx,
        "min_rx": row.min_rx,
        "multiplier": row.multiplier,
        "micro_bfd": bool(row.micro_bfd),
    }


def _push_bfd_intent_for_device(device_id, adapter_device_id):
    """Build and push the full owned per-interface BFD intent snapshot for a device.

    Store-only (deferred): the single device Apply commits via the bfd-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.
    """
    from . import adapter_client as client
    from .models import NSOBFDInterfaceState

    interfaces = []
    for row in NSOBFDInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface"):
        interfaces.append(bfd_intent_item(row))

    _push_changed(
        (device_id, "bfd"),
        interfaces,
        lambda body: client.put_bfd_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "bfd"):
        return
    _schedule_intent_push((device_id, "bfd"))


@_skip_on_render
def _on_ip_address_change(sender, instance, **kwargs):
    """Schedule only the IP keys declared by the active exact writer."""
    if getattr(_p2p_allocation_active, "active", False):
        return
    from .renderer_writer import active_renderer_writer

    writer = active_renderer_writer()
    if writer is None:
        return
    for device_id, scope in writer.plan.content_keys:
        if scope == "ip" and _converted_writer_owns_content(device_id, scope):
            _schedule_intent_push((device_id, scope))


@_skip_on_render
def _on_ip_address_delete(sender, instance, **kwargs):
    """Schedule only the IP keys declared by the active exact delete writer."""
    _on_ip_address_change(sender, instance, **kwargs)


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


def static_route_intent_item(row):
    """Return one owned static-route overlay in the adapter's exact wire shape."""
    route = row.static_route
    item = {
        "route_id": route.pk,
        "generation": row.intent_generation or None,
        "vrf": route.vrf.name if route.vrf else "",
        "prefix": str(route.prefix),
        "next_hop": str(route.next_hop),
        "permanent": route.permanent or False,
        "tag": route.tag,
    }
    if route.metric is not None:
        item["metric"] = route.metric
    return item


def _push_static_route_intent_for_device(device_id, adapter_device_id):
    """Build and capture the full static route intent snapshot for a device.

    Each route names the NetBox ``StaticRoute`` pk and the generation of the intent it
    carries: the pk is what lets the adapter tell a *replacement* from an unrelated
    delete-plus-insert, and the generation is the token an apply result is correlated
    against. ``intent_generation`` 0 is the unallocated sentinel and goes on the wire as
    NULL — the adapter adopts a generation only when non-null, so a sentinel row simply has
    nothing to correlate with instead of correlating with everything at 0.

    The captured ``on_response`` hook records echoed fingerprints as this device's settlement
    expectations. :func:`_send_rendered` runs the hook after it sends the captured body.
    """
    from . import adapter_client as client
    from .models import NSOStaticRouteState

    routes = []
    generations: dict[int, int] = {}
    for row in NSOStaticRouteState.objects.filter(
        PUSHED_STATIC_ROUTE_FILTER, management__device_id=device_id
    ).select_related("static_route", "static_route__vrf"):
        generation = row.intent_generation or None
        routes.append(static_route_intent_item(row))
        if generation is not None:
            generations[row.static_route_id] = generation

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
    from django.db import transaction

    from .intent_state import mirror_refresh
    from .models import NSOStaticRouteState

    for echo in echoes:
        if not isinstance(echo, dict):
            continue
        route_id, generation, fingerprint = echo.get("route_id"), echo.get("generation"), echo.get("fingerprint")
        if route_id is None or not fingerprint:
            continue
        if generation is None or generations.get(route_id) != generation:
            continue  # never pushed by us at this generation — not an expectation we may record
        with transaction.atomic(), suppress_intent_push():
            rows = NSOStaticRouteState.objects.select_for_update(of=("self",)).filter(
                management__device_id=device_id,
                static_route_id=route_id,
                intent_generation=generation,
            )
            for row in rows:
                fields = {"expected_generation", "expected_fingerprint"}
                with mirror_refresh(row, fields) as locked:
                    if locked is None:
                        continue
                    locked.expected_generation = generation
                    locked.expected_fingerprint = fingerprint
                    locked.save(update_fields=fields)


@_skip_on_render
@_require_converted_writer
def _on_static_route_state_save(sender, instance, **kwargs):
    """Schedule static-route intent only for an active exact content writer."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if not _converted_writer_owns_content(device_id, "static_route"):
        return
    _schedule_intent_push((device_id, "static_route"))


@_skip_on_render
def _on_static_route_state_delete(sender, instance, **kwargs):
    """Push a reduced static-route snapshot with this row's deletion authority."""
    from .models import NSODeviceManagement

    if instance.status not in _OWNED_PUSH_STATUSES:
        return
    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return

    transition = _static_route_delete_transition(instance, instance.static_route_id)
    _schedule_intent_push((mgmt.device_id, "static_route"), transitions=[transition])


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
    rows.delete()


def _on_routing_static_route_pre_save(sender, instance, **kwargs):
    """Treat a native save event as neither ownership evidence nor a mutation planner."""


@_skip_on_render
def _on_routing_static_route_save(sender, instance, created=False, **kwargs):
    """Schedule only the static-route keys declared by the active exact writer."""
    from .renderer_writer import active_renderer_writer

    writer = active_renderer_writer()
    if writer is None:
        return
    for device_id, scope in writer.plan.content_keys:
        if scope == "static_route" and _converted_writer_owns_content(device_id, scope):
            _schedule_intent_push((device_id, scope))


@_close_renderer_m2m_permit
@_skip_on_render
def _on_routing_static_route_devices_changed(sender, instance, action, pk_set, reverse, **kwargs):
    """Schedule only exact-writer assignment changes, without acquiring in the signal."""
    if not action.startswith("post_"):
        return
    from .renderer_writer import active_renderer_writer

    writer = active_renderer_writer()
    if writer is None:
        return
    for device_id, scope in writer.plan.content_keys:
        if scope == "static_route" and _converted_writer_owns_content(device_id, scope):
            _schedule_intent_push((device_id, scope))


@_skip_on_render
def _on_routing_static_route_pre_delete(sender, instance, **kwargs):
    """Schedule only exact-writer route deletions, without mutating overlays."""
    from .renderer_writer import active_renderer_writer

    writer = active_renderer_writer()
    if writer is None:
        return
    for device_id, scope in writer.plan.content_keys:
        if scope == "static_route" and _converted_writer_owns_content(device_id, scope):
            _schedule_intent_push((device_id, scope))


# ── IS-IS Flex-Algorithm intent (process-tag scoped) ────────────────────────


def isis_flex_algo_intent_item(row):
    """Return one IS-IS Flex-Algo in the adapter's exact wire shape."""
    return {
        "process_tag": row.process_tag or "",
        "algo_id": int(row.algo_id),
        "metric_type": row.metric_type or None,
        "priority": row.priority,
        "admin_group_exclude": row.admin_group_exclude or None,
        "admin_group_include_any": row.admin_group_include_any or None,
        "admin_group_include_all": row.admin_group_include_all or None,
    }


def _push_isis_flex_algo_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IS-IS Flex-Algo intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOISISFlexAlgoState

    flex_algos = []
    for row in NSOISISFlexAlgoState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        flex_algos.append(isis_flex_algo_intent_item(row))

    _push_changed(
        (device_id, "isis_flex_algo"),
        flex_algos,
        lambda body: client.put_isis_flex_algo_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
def _on_isis_flex_algo_state_save(sender, instance, **kwargs):
    """Schedule Flex-Algo intent only for a declared exact writer mutation."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    device_id, _adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    if _converted_writer_owns_content(device_id, "isis_flex_algo"):
        _schedule_intent_push((device_id, "isis_flex_algo"))


@_skip_on_render
def _on_routing_isis_flex_algo_save(sender, instance, **kwargs):
    """Keep foreign native Flex-Algo saves outside synchronous bookkeeping."""


@_skip_on_render
def _on_routing_isis_flex_algo_pre_delete(sender, instance, **kwargs):
    """Keep foreign native Flex-Algo deletes outside synchronous bookkeeping."""


def l2_sap_intent_item(row):
    """Return one L2 SAP in the adapter's exact wire shape."""
    return {
        "service_name": row.service_name,
        "service_type": row.service_type,
        "sap_id": row.sap_id,
        "port": row.port,
        "outer_tag": row.outer_tag,
        "inner_tag": row.inner_tag,
    }


def _push_l2_sap_intent_for_device(device_id, adapter_device_id):
    """Build and push the full Nokia L2 SAP intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOL2SapState

    saps = []
    for row in NSOL2SapState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        saps.append(l2_sap_intent_item(row))

    _push_changed(
        (device_id, "l2_sap"),
        saps,
        lambda body: client.put_l2_sap_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
def _on_l2_sap_state_save(sender, instance, **kwargs):
    """Schedule L2 SAP intent only for an active exact content writer."""
    from .models import NSODeviceManagement
    from .renderer_writer import active_renderer_writer

    writer = active_renderer_writer()
    if writer is None:
        return

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if _converted_writer_owns_content(device_id, "l2_sap"):
        _schedule_intent_push((device_id, "l2_sap"))


def lacp_member_intent_item(row):
    """Return one LACP member in the adapter's exact nested wire shape."""
    return {
        "interface_name": row.interface.name,
        "mode": row.mode,
        "port_priority": row.port_priority,
    }


def lacp_bundle_intent_item(row, members):
    """Return one LACP bundle in the adapter's exact wire shape."""
    return {
        "name": row.interface.name,
        "lag_id": row.lag_id,
        "min_links": row.min_links,
        "system_priority": row.system_priority,
        "system_id": row.system_id,
        "timer": row.timer,
        "admin_key": row.admin_key,
        "members": list(members),
    }


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
        members = (
            lacp_member_intent_item(member)
            for member in NSOLACPMemberState.objects.filter(
                management__device_id=device_id,
                lag_bundle=b.interface,
                status__in=_owned,
            ).select_related("interface")
        )
        bundles.append(lacp_bundle_intent_item(b, members))

    _push_changed(
        (device_id, "lacp"),
        bundles,
        lambda body: client.apply_lag_config(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
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
    if not _converted_writer_owns_content(device_id, "lacp"):
        return
    _schedule_intent_push((device_id, "lacp"))


# NetBox interface mode -> NSO switchport vocabulary.
_NETBOX_TO_NSO_MODE = {"access": "access", "tagged": "trunk", "tagged-all": "trunk-all"}


def switchport_intent_item(row, tagged_vlan_ids):
    """Return one switchport row in the adapter's exact wire shape."""
    return {
        "interface_name": row.interface.name,
        "mode": _NETBOX_TO_NSO_MODE.get(row.mode or "", row.mode or ""),
        "untagged_vlan": row.untagged_vlan.vid if row.untagged_vlan else None,
        "tagged_vlans": sorted(tagged_vlan_ids),
    }


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
        interfaces.append(switchport_intent_item(st, (v.vid for v in st.tagged_vlans.all())))

    _push_changed(
        (device_id, "switchport"),
        interfaces,
        lambda body: client.apply_switchport_config(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
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
    from . import status_machine as sm

    if not sm.is_owned(instance.status):
        return
    device_id = mgmt.device_id
    if not _converted_writer_owns_content(device_id, "switchport"):
        return
    _schedule_intent_push((device_id, "switchport"))


def isis_level_intent_item(row):
    """Return one IS-IS level contribution, or None for a knobless row."""
    entry = {"level": int(row.level)}
    if row.wide_metrics_only is not None:
        entry["wide_metrics_only"] = row.wide_metrics_only
    if getattr(row, "labeled_preference", None) is not None:
        entry["labeled_preference"] = row.labeled_preference
    if row.disabled is not None:
        entry["disabled"] = row.disabled
    return entry if len(entry) > 1 else None


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
        if entry := isis_level_intent_item(lv):
            out.append(entry)
    return out


def isis_interface_intent_item(row):
    """Return one IS-IS interface in the adapter's exact wire shape."""
    return {
        "interface_name": row.interface.name,
        "af": row.af,
        "process_tag": row.process_tag or "",
        "circuit_type": row.circuit_type,
        "network_type": row.network_type,
        "metric": row.metric,
        "passive": row.passive or False,
        "bfd_enabled": row.bfd_enabled,
        "frr_enabled": row.frr_enabled,
        "frr_protection": row.frr_protection or None,
    }


def isis_instance_intent_item(row, *, redistribution=(), levels=()):
    """Return one IS-IS process in the adapter's exact wire shape."""
    entry = {
        "process_tag": row.process_tag or "",
        "net": row.net,
        "is_type": row.is_type,
        "metric_style": row.metric_style,
        "overload_bit": row.overload_bit,
        "area_auth_type": row.area_auth_type,
        "area_auth_key": row.area_auth_key or None,
        "domain_auth_type": row.domain_auth_type,
        "domain_auth_key": row.domain_auth_key or None,
        "fast_reroute": row.fast_reroute or None,
        "microloop_avoidance": row.microloop_avoidance,
    }
    if redistribution:
        entry["redistribution"] = list(redistribution)
    if levels:
        entry["levels"] = list(levels)
    return entry


def _push_isis_intent_for_device(device_id, adapter_device_id):
    """Build and push the full IS-IS intent snapshot (interfaces + processes) for a device."""
    from . import adapter_client as client
    from .models import NSOISISInstanceState, NSOISISInterfaceState

    redist_by_proc = _collect_redistribution_by_dest_ref(device_id, "isis")

    interfaces = []
    for row in NSOISISInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("interface"):
        interfaces.append(isis_interface_intent_item(row))

    processes = []
    for row in NSOISISInstanceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        proc_redist = redist_by_proc.get(row.process_tag or "", [])
        levels = _isis_levels_for_state(row)
        processes.append(isis_instance_intent_item(row, redistribution=proc_redist, levels=levels))

    _push_changed(
        (device_id, "isis"),
        [interfaces, processes],
        lambda body: client.put_isis_interface_intent(adapter_device_id, body[0], processes=body[1]),
    )


@_skip_on_render
@_require_converted_writer
def _on_isis_interface_state_save(sender, instance, **kwargs):
    """Schedule IS-IS only when the exact writer owns this device key."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if _converted_writer_owns_content(device_id, "isis"):
        _schedule_intent_push((device_id, "isis"))


@_skip_on_render
@_require_converted_writer
def _on_isis_instance_state_save(sender, instance, **kwargs):
    """Schedule IS-IS only when the exact writer owns this device key."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if _converted_writer_owns_content(device_id, "isis"):
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


def bgp_peer_address_family_intent_item(row):
    """Return one BGP peer address family in the adapter's exact nested wire shape."""
    entry = {
        "af": row.address_family.address_family,
        "enabled": row.enabled if row.enabled is not None else True,
    }
    for field_name in ("routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out"):
        if value := getattr(row, field_name):
            entry[field_name] = value.name
    return entry


def bgp_peer_intent_item(row, address_families):
    """Return one BGP peer in the adapter's exact nested wire shape."""
    if row.bgp_peer is None:
        return None
    entry = {
        "peer_address": row.peer_address_str,
        "enabled": row.enabled if row.enabled is not None else True,
        "remote_as": row.remote_as_str or None,
        "address_families": list(address_families),
    }
    source_value = _bgp_peer_source_value(row.bgp_peer)
    if source_value is not None:
        entry["source"] = source_value
    entry.update(_bgp_peer_model_fields(row.bgp_peer))
    return entry


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
        if row.bgp_peer is None:
            continue
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
                peer_afs.append(bgp_peer_address_family_intent_item(paf))

        scopes[vrf_name]["peers"].append(bgp_peer_intent_item(row, peer_afs))

    router_list = _build_bgp_router_list(routers, scope_afs, _bgp_router_id_map(device_id))
    _push_changed(
        (device_id, "bgp"),
        router_list,
        lambda body: client.put_bgp_intent(adapter_device_id, body),
    )


@_skip_on_render
@_require_converted_writer
def _on_bgp_peer_state_save(sender, instance, **kwargs):
    """Schedule BGP only when the exact writer owns this device key."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if _converted_writer_owns_content(device_id, "bgp"):
        _schedule_intent_push((device_id, "bgp"))


@_skip_on_render
def _on_routing_bgp_peer_save(sender, instance, **kwargs):
    """Keep foreign native BGP peer saves outside ownership bookkeeping."""
    _schedule_exact_writer_scope("bgp")


@_skip_on_render
def _on_routing_bgp_peer_pre_delete(sender, instance, **kwargs):
    """Keep foreign native BGP peer deletes outside ownership bookkeeping."""
    _schedule_exact_writer_scope("bgp")


@_skip_on_render
@_require_converted_writer
def _on_redistribution_state_save(sender, instance, **kwargs):
    """Schedule redistribution only when its exact writer owns the destination key."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    if _converted_writer_owns_content(mgmt.device_id, instance.dest_protocol):
        _schedule_redistribution_push(mgmt.device_id, instance.dest_protocol)


def route_policy_intent_item(row):
    """Return one owned route-policy object in the adapter's exact wire shape."""
    obj = row.assigned_object
    if obj is None:
        return None
    return {
        "family": row.family,
        "name": row.object_name,
        "entries": _build_route_policy_entries(row.family, obj),
        "accepted": True,
        **({"invert_match": bool(getattr(obj, "invert_match", False))} if row.family == "community_list" else {}),
    }


def _push_route_policy_intent_for_device(device_id, adapter_device_id):
    """Build and push the full route-policy intent snapshot for a device."""
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
        if item := route_policy_intent_item(row):
            objects.append(item)

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
    from django.db import transaction

    from .intent_state import mirror_refresh

    unsupported = resp.get("unsupported_members") or {}
    with transaction.atomic(), suppress_intent_push():
        for row in owned_rows:
            members = unsupported.get(row.object_name, []) if row.family == "community_list" else []
            if list(row.unsupported_members or []) != list(members):
                with mirror_refresh(row, {"unsupported_members"}) as locked:
                    if locked is None:
                        continue
                    locked.unsupported_members = members
                    locked.save(update_fields=["unsupported_members"])


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
@_require_converted_writer
def _on_route_policy_state_save(sender, instance, **kwargs):
    """Schedule route policy only when the exact writer owns this device key."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if _converted_writer_owns_content(device_id, "route_policy"):
        _schedule_intent_push((device_id, "route_policy"))


@_skip_on_render
def _on_routing_policy_pre_delete(sender, instance, **kwargs):
    """Keep foreign policy-root deletions outside ownership bookkeeping."""
    _schedule_exact_writer_scope("route_policy")


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


def _route_map_contributors(route_maps):
    """Return de-duplicated policy objects referenced by route maps."""
    referenced = []
    seen_refs: set = set()

    def _add_ref(family, obj):
        key = (family, obj.name)
        if key not in seen_refs:
            seen_refs.add(key)
            referenced.append((family, obj))

    for route_map in route_maps:
        for entry in route_map.route_map_entries.all():
            for obj in entry.match_prefix_list.all():
                _add_ref("prefix_list", obj)
            for obj in entry.match_community_list.all():
                _add_ref("community_list", obj)
            for obj in entry.match_aspath.all():
                _add_ref("as_path", obj)
            for set_community in entry.set_communities.all():
                if set_community.community_list_id:
                    _add_ref("community_list", set_community.community_list)
    return referenced


def _route_policy_acquisition_plan(mgmt, *, primary_operations=(), route_maps=()):
    """Freeze explicit root acquisitions and eligible route-map contributors."""
    import copy

    from django.contrib.contenttypes.models import ContentType

    from .models import NSORoutePolicyState
    from .renderer_writer import RendererMutationPlan, planned_save
    from .shared_object_ownership import materialized_row
    from .status_machine import CHANGED, CONFLICT

    planned_at = timezone.now()
    saves = []
    operations = list(primary_operations)
    staged = {(candidate.family, candidate.object_name.casefold()) for candidate, _fields, _created in operations}
    for candidate, fields, created in operations:
        saves.append(
            planned_save(
                candidate,
                update_fields=fields,
                force_insert=created,
                natural_key=("management", "family", "object_name") if created else (),
            )
        )
    drifted: list = []
    cross_device: list = []
    for family, obj in _route_map_contributors(route_maps):
        key = (family, obj.name.casefold())
        if key in staged:
            continue
        ct = ContentType.objects.get_for_model(obj)
        state = NSORoutePolicyState.objects.filter(
            management=mgmt,
            family=family,
            object_name__iexact=obj.name,
        ).first()
        created = state is None
        if state is None:
            candidate = NSORoutePolicyState(
                management=mgmt,
                family=family,
                object_name=obj.name,
                content_type=ct,
                object_id=obj.pk,
                status="accepted",
                accepted_at=planned_at,
            )
            fields = None
            owner = materialized_row(NSORoutePolicyState, family, obj.name)
            if owner is not None and owner.management.device_id != mgmt.device_id:
                cross_device.append((family, obj.name, owner.management.device.name))
        elif state.status in _OWNED_PUSH_STATUSES:
            continue  # already owned → nothing to do
        elif state.status in (CHANGED, CONFLICT):
            drifted.append((family, obj.name))
            continue
        else:
            candidate = copy.copy(state)
            candidate.content_type = ct
            candidate.object_id = obj.pk
            candidate.status = "accepted"
            candidate.accepted_at = planned_at
            fields = ("content_type", "object_id", "status", "accepted_at")
        saves.append(
            planned_save(
                candidate,
                update_fields=fields,
                force_insert=created,
                natural_key=("management", "family", "object_name") if created else (),
            )
        )
        operations.append((candidate, fields, created))
        staged.add(key)
    return (
        RendererMutationPlan.build(saves=saves, planned_at=planned_at),
        operations,
        CascadeResult(drifted=drifted, cross_device=cross_device),
    )


@_skip_on_render
def _on_routing_policy_object_save(sender, instance, **kwargs):
    """Keep foreign policy-root saves outside ownership bookkeeping."""
    _schedule_exact_writer_scope("route_policy")


@_skip_on_render
def _on_routing_policy_entry_save(sender, instance, **kwargs):
    """Keep foreign policy-entry saves outside ownership bookkeeping."""
    _schedule_exact_writer_scope("route_policy")


@_skip_on_render
def _on_routing_policy_entry_delete(sender, instance, **kwargs):
    """Keep foreign policy-entry deletes outside ownership bookkeeping."""
    _schedule_exact_writer_scope("route_policy")


def redistribution_intent_item(row):
    """Return one redistribution row in the adapter's exact nested wire shape."""
    entry = {"source_protocol": row.source_protocol, "source_ref": row.source_ref}
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
    return entry


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
        by_ref.setdefault(row.dest_ref, []).append(redistribution_intent_item(row))
    return by_ref


def ospf_instance_intent_item(row, redistribution=()):
    """Return one OSPF process in the adapter's exact wire shape."""
    entry = {"process_id": row.process_id, "vrf": row.vrf or "", "areas": row.areas or []}
    if row.router_id:
        entry["router_id"] = row.router_id
    if row.enabled is not None:
        entry["enabled"] = row.enabled
    if redistribution:
        entry["redistribution"] = list(redistribution)
    return entry


def ospf_interface_intent_item(row):
    """Return one OSPF interface in the adapter's exact wire shape."""
    entry = {
        "interface_name": row.interface.name,
        "passive": row.passive if row.passive is not None else False,
        "auth_present": row.auth_present if row.auth_present is not None else False,
    }
    for field_name in ("process_id", "area_id", "priority", "cost", "network_type", "auth_type"):
        value = getattr(row, field_name)
        if value is not None:
            entry[field_name] = value
    return entry


def _push_ospf_intent_for_device(device_id, adapter_device_id):
    """Build and push the full OSPF intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOOSPFInstanceState, NSOOSPFInterfaceState

    redist_by_proc = _collect_redistribution_by_dest_ref(device_id, "ospf")

    instances = []
    for row in NSOOSPFInstanceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("management"):
        proc_redist = redist_by_proc.get(str(row.process_id), [])
        instances.append(ospf_instance_intent_item(row, proc_redist))

    interfaces = []
    for row in NSOOSPFInterfaceState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("management"):
        interfaces.append(ospf_interface_intent_item(row))

    payload = {"instances": instances, "interfaces": interfaces}
    _push_changed((device_id, "ospf"), payload, lambda body: client.put_ospf_intent(adapter_device_id, body))


@_skip_on_render
@_require_converted_writer
def _on_ospf_instance_state_save(sender, instance, **kwargs):
    """Schedule OSPF only when the exact writer owns this device key."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if _converted_writer_owns_content(device_id, "ospf"):
        _schedule_intent_push((device_id, "ospf"))


@_skip_on_render
@_require_converted_writer
def _on_ospf_interface_state_save(sender, instance, **kwargs):
    """Schedule OSPF only when the exact writer owns this device key."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    if _converted_writer_owns_content(device_id, "ospf"):
        _schedule_intent_push((device_id, "ospf"))


# ── netbox_routing OSPF greenfield write path ───────────────────────────────
# Native OSPF signals are delivery notifications only. Exact planners establish ownership.


@_skip_on_render
def _on_routing_ospf_instance_save(sender, instance, **kwargs):
    """Keep foreign native OSPF process saves outside ownership bookkeeping."""
    _schedule_exact_writer_scope("ospf")


@_skip_on_render
def _on_routing_ospf_interface_save(sender, instance, **kwargs):
    """Keep foreign native OSPF interface saves outside ownership bookkeeping."""
    _schedule_exact_writer_scope("ospf")


@_skip_on_render
def _on_routing_isis_level_save(sender, instance, **kwargs):
    """Keep foreign native IS-IS child saves outside ownership bookkeeping."""
    _schedule_exact_writer_scope("isis")


@_skip_on_render
def _on_routing_isis_level_post_delete(sender, instance, **kwargs):
    """Keep foreign native IS-IS child deletes outside ownership bookkeeping."""
    _schedule_exact_writer_scope("isis")


@_skip_on_render
def _on_routing_isis_interface_save(sender, instance, **kwargs):
    """Keep foreign native IS-IS interface saves outside ownership bookkeeping."""
    _schedule_exact_writer_scope("isis")


def _on_routing_isis_interface_pre_delete(sender, instance, **kwargs):
    """Keep foreign native IS-IS interface deletes outside ownership bookkeeping."""
    _schedule_exact_writer_scope("isis")


@_skip_on_render
def _on_routing_ospf_instance_pre_delete(sender, instance, **kwargs):
    """Keep foreign native OSPF process deletes outside ownership bookkeeping."""
    _schedule_exact_writer_scope("ospf")


@_skip_on_render
def _on_routing_ospf_interface_pre_delete(sender, instance, **kwargs):
    """Keep foreign native OSPF interface deletes outside ownership bookkeeping."""
    _schedule_exact_writer_scope("ospf")


@_skip_on_render
def _on_redistribution_fork_save(sender, instance, **kwargs):
    """Keep foreign native redistribution saves outside synchronous bookkeeping."""
    from .renderer_writer import active_renderer_writer

    writer = active_renderer_writer()
    if writer is None:
        return
    for device_id, scope in writer.plan.content_keys:
        if scope in redistribution_destinations() and _converted_writer_owns_content(device_id, scope):
            _schedule_redistribution_push(device_id, scope)


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
        _repend_intent_on_interface_rename,
        sender=Interface,
        dispatch_uid="nso_plugin_iface_intent_name",
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

    pre_save.connect(
        _on_vlan_pre_save,
        sender=VLAN,
        dispatch_uid="nso_plugin_ipam_vlan_pre_save",
    )
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
        _as_delete_origin(_on_static_route_state_delete),
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
    # An exact overlay deletion pushes the reduced owned snapshot.
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

    # netbox_routing OSPF exact-writer delivery notifications.
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

    # netbox_routing.BGPPeer exact-writer delivery notifications.
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
            _on_routing_isis_flex_algo_pre_delete,
            sender=ISISFlexAlgo,
            dispatch_uid="nso_plugin_routing_isis_flex_algo_pre_delete_capture",
        )
        post_delete.connect(
            _as_delete_origin(_on_routing_isis_flex_algo_post_delete),
            sender=ISISFlexAlgo,
            dispatch_uid="nso_plugin_routing_isis_flex_algo_post_delete",
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
            _on_routing_isis_interface_pre_delete,
            sender=ISISInterface,
            dispatch_uid="nso_plugin_routing_isis_interface_pre_delete_capture",
        )
        post_delete.connect(
            _as_delete_origin(_on_routing_isis_interface_post_delete),
            sender=ISISInterface,
            dispatch_uid="nso_plugin_routing_isis_interface_post_delete",
            weak=False,
        )
    except ImportError:
        logger.debug("netbox_routing not installed — IS-IS interface greenfield signals not registered")
