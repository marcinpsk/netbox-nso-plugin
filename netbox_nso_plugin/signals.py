# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Signal handlers for NSODeviceManagement scope propagation and intent push."""

import functools
import hashlib
import json
import logging
import threading
from collections import namedtuple

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

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


# ── Intent-push coalescing + change-detection ──────────────────────────────────
#
# A bulk operation (e.g. NetBox's native bulk-edit) saves N rows in one
# transaction; each save fires a push handler that rebuilds and PUTs the FULL
# device snapshot. Without coalescing that is O(N^2) work and N HTTP PUTs.
#
# Two complementary mitigations, mirroring the adapter perf layers:
#   * Coalescing — collect pushes during a transaction, keyed by (device, category),
#     and flush each key once at commit (one push reflecting the final state).
#   * Change-detection — skip the PUT when the snapshot is byte-identical to the
#     last one pushed for that key. The adapter PUT is an idempotent full-replace,
#     so this in-process, best-effort cache is safe (a cold-cache redundant push
#     is harmless).
#
# Coalescing only engages inside a transaction; with no active transaction (a
# lone programmatic save, or the no-DB unit tests) the push runs immediately —
# this also avoids on_commit's autocommit path forcing a DB connection.

# ``_pending_pushes`` is thread-local: coalescing is per-transaction, so it must not bleed
# between request threads. ``_last_pushed_hashes`` is deliberately the opposite — a single
# process-wide dict shared across threads. Two threads racing on the same key can at worst
# cause one redundant or one un-deduped push, both harmless because the adapter PUT is an
# idempotent full-replace (and the explicit Apply passes ``force=True``, bypassing it). A
# per-thread cache would instead miss every cross-thread dedup, so sharing is the right call.
_pending_pushes = threading.local()
_last_pushed_hashes: dict[tuple, str] = {}


def reset_intent_push_state() -> None:
    """Clear coalescing + change-detection state. Intended for use in tests."""
    _last_pushed_hashes.clear()
    _pending_pushes.map = {}


def _schedule_intent_push(key, fn) -> None:
    """Coalesce *fn* under *key*, flushing once when the current transaction commits.

    Deduped by key, so N saves of the same (device, category) collapse to one push.
    Outside a transaction the push runs immediately (nothing to coalesce).
    """
    from django.db import connection, transaction

    if not connection.in_atomic_block:
        fn()
        return

    pending = getattr(_pending_pushes, "map", None)
    if pending is None:
        pending = {}
        _pending_pushes.map = pending
    was_empty = not pending
    pending[key] = fn  # last fn wins — per-key builders are equivalent
    if was_empty:
        transaction.on_commit(_drain_intent_pushes)


def _drain_intent_pushes() -> None:
    """Run every coalesced push once, isolating failures so one can't abort the rest."""
    pending = getattr(_pending_pushes, "map", None)
    _pending_pushes.map = {}
    if not pending:
        return
    for fn in pending.values():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a failed push must not abort siblings
            logger.warning("Coalesced intent push failed: %s", exc)


def _push_changed(key, payload, do_push, force=False):
    """Run *do_push* only if *payload* differs from the last push for *key*.

    ``do_push`` performs the actual ``client.put_*`` call. Errors are swallowed
    (matching the adapter-unreachable tolerance elsewhere) and the cache is left
    unchanged on failure so the next attempt retries. ``force`` bypasses the
    unchanged-skip (used by the explicit device Apply, which must always commit).

    Returns the ``do_push()`` result on a push that ran (so a caller can read the
    adapter's response, e.g. the route-policy ``unsupported_members`` map), or ``None``
    when the push was skipped-unchanged or failed. Most callers ignore the return.
    """
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    if not force and _last_pushed_hashes.get(key) == digest:
        logger.debug("Intent push skipped (unchanged) for %s", key)
        return None
    try:
        result = do_push()
    except Exception as exc:  # noqa: BLE001 — adapter may be down; log and retry next time
        logger.warning("Intent push failed for %s: %s", key, exc)
        return None
    _last_pushed_hashes[key] = digest
    return result


def _push_interface_intent_for_device(device_id, adapter_device_id, force=False) -> None:
    """Build the full OWNED interface intent snapshot and push it (change-detected).

    Owned = ``status in OWNED_STATES`` (accepted/deploying/in_sync/apply_failed) — the
    canonical ownership test, identical to every other scope's push predicate and to
    what the device tab now displays. (Previously this keyed off ``accepted_at``, a
    one-shot timestamp never cleared on un-own, so a row reverted/drifted back to
    ``imported`` carried a stale accepted_at and was force-pushed despite reading as
    drift — the display/push split-brain this fix removes.) Shared by the accept signal,
    the Decision-G edit signal, and the view-level bulk accept so all three agree on
    what gets pushed. ``force=True`` (the device Apply) bypasses change-detection so an
    owned interface whose adapter intent went stale is re-pushed and actually applied,
    instead of being silently skipped — ownership is kept durable by the reconciler's
    owned-guard (``template_content._upsert_interface_states``), which no longer lets an
    adapter sync clobber an owned status back to ``imported``.
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
        lambda: client.put_intent(adapter_device_id, attributes),
        force=force,
    )


def _schedule_redistribution_push(device_id, adapter_device_id, dest) -> None:
    """Schedule the destination protocol's intent push for a redistribution change.

    Keyed by (device, dest_protocol) so redistribution and the protocol's own state
    saves coalesce into a single push for that protocol.
    """
    if dest == "ospf":
        fn = _push_ospf_intent_for_device
    elif dest == "isis":
        fn = _push_isis_intent_for_device
    elif dest == "bgp":
        fn = _push_bgp_intent_for_device
    else:
        logger.warning(
            "Redistribution: unknown dest_protocol %r for device %s — no push triggered",
            dest,
            device_id,
        )
        return
    _schedule_intent_push((device_id, dest), lambda: fn(device_id, adapter_device_id))


@receiver(post_save, sender="netbox_nso_plugin.NSODeviceManagement")
def sync_scope_to_adapter(sender, instance, created, **kwargs):
    """Push device + scope to the adapter whenever an NSODeviceManagement record is saved.

    After setting scope, calls sync-notify so the adapter starts an immediate sync
    rather than waiting for the next scheduled poll.

    Gated during async onboarding: while a row is ``provisioning`` (the background
    provision job hasn't finished) or ``provision_failed``, the NSO node may not exist
    yet, so mapping/scope/sync would fail or race. The status-advance view clears the
    status to "" on success and re-saves, which fires this handler normally.
    """
    if getattr(instance, "onboard_status", "") in ("provisioning", "provision_failed"):
        return

    from . import adapter_client as client

    try:
        if created or instance.adapter_device_id is None:
            result = client.onboard_device(
                nso_instance=instance.nso_instance.adapter_instance_id,
                nso_device_name=instance.nso_device_name,
                netbox_device_id=instance.device_id,
            )
            type(instance).objects.filter(pk=instance.pk).update(adapter_device_id=result["id"])
            instance.adapter_device_id = result["id"]
        else:
            client.patch_device(
                adapter_device_id=instance.adapter_device_id,
                nso_instance=instance.nso_instance.adapter_instance_id,
                nso_device_name=instance.nso_device_name,
            )

        # Carry the device's management addresses so the adapter's failover loop can probe
        # primary and fall back to OOB. Resolved by the SAME helper onboarding uses, so the
        # provision address and the failover-probed addresses never diverge. Explicit values
        # (incl. None to clear) — the plugin is authoritative, so a removed OOB IP in NetBox
        # clears it adapter-side.
        from .onboarding import device_mgmt_addresses

        primary_ip, oob_ip = device_mgmt_addresses(instance.device)
        client.set_scope(
            instance.adapter_device_id,
            instance.managed_attributes,
            auto_apply=instance.auto_apply,
            sync_before_apply=instance.sync_before_apply,
            primary_ip=primary_ip,
            oob_ip=oob_ip,
        )

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
    """Remove device from adapter when the management record is deleted."""
    if instance.adapter_device_id is None:
        return
    from . import adapter_client as client

    try:
        client.delete_device(instance.adapter_device_id)
    except Exception as exc:
        logger.warning("Failed to offboard device %s from adapter: %s", instance.adapter_device_id, exc)


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
    """
    if instance.status not in _OWNED_PUSH_STATUSES:
        return

    from .models import NSODeviceManagement

    device_id = instance.interface.device_id
    try:
        mgmt = NSODeviceManagement.objects.get(device_id=device_id)
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "interface"),
        lambda: _push_interface_intent_for_device(device_id, adapter_device_id),
    )


def _templates():
    """Return the parsed derived-intent template list from the AppConfig.

    Returns an empty list when the feature is off (config absent or empty).
    """
    from django.apps import apps

    cfg = apps.get_app_config("netbox_nso_plugin")
    return getattr(cfg, "_derived_intent_templates", [])


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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "interface"),
        lambda: _push_interface_intent_for_device(device_id, adapter_device_id),
    )


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


def _push_ip_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full IP intent snapshot for a device.

    ``force=True`` bypasses change-detection (used by provisioning, which must always land
    its computed intent even if an identical snapshot was pushed earlier this process).
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

    _push_changed((device_id, "ip"), addresses, lambda: client.put_ip_intent(adapter_device_id, addresses), force=force)


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
    for row in NSOSnmpV3UserState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        if not row.vault_ref:
            continue
        # vault_ref is a PATH ref ("mount/path"); the auth/priv fields live at
        # "#auth"/"#priv" by convention. A leg without its protocol is not
        # derivable on-device, so its ref is withheld (the reconciler would
        # otherwise resolve a secret it cannot apply).
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

    hosts = []
    for row in NSOSnmpHostState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        if row.version == "v3":
            # The host model has no v3 username source (community_hash is v1/v2c
            # only) — pushing would configure a host keyed on an empty user.
            logger.warning(
                "SNMP v3 trap host %s on device %s skipped: v3 hosts are not yet pushable "
                "(no username field on the host overlay)",
                row.address,
                device_id,
            )
            continue
        hosts.append(
            {
                "address": row.address,
                "version": row.version,
                "notify_type": row.notify_type,
                "community_or_user": row.community_hash or "",  # hash used as community label reference
                "port": row.port,
            }
        )

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
        lambda: client.put_snmp_intent(adapter_device_id, communities, v3_users, hosts, system_info),
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "snmp"),
        lambda: _push_snmp_intent_for_device(device_id, adapter_device_id),
    )


def _push_logging_intent_for_device(device_id, adapter_device_id):
    """Build and push the full remote-syslog (logging) intent snapshot for a device.

    Store-only (deferred): the device commit happens on the single device Apply via
    the adapter's logging-reconciler. Only owned rows are included.
    """
    from . import adapter_client as client
    from .models import NSOLoggingHostState

    hosts = []
    for row in NSOLoggingHostState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ):
        hosts.append(
            {
                "address": row.address,
                "port": row.port,
                "severity": row.severity or "",
                "facility": row.facility or "",
                "transport": row.transport or "",
                "vrf": row.vrf or "",
                "source": row.source or "",
            }
        )

    _push_changed(
        (device_id, "logging"),
        hosts,
        lambda: client.put_logging_intent(adapter_device_id, hosts),
    )


@_skip_on_render
def _on_logging_state_save(sender, instance, **kwargs):
    """Push logging intent whenever an NSOLoggingHostState row is saved (accept → push)."""
    from .models import NSODeviceManagement

    try:
        mgmt = instance.management
    except NSODeviceManagement.DoesNotExist:
        return

    if mgmt.adapter_device_id is None:
        return

    device_id = mgmt.device_id
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "logging"),
        lambda: _push_logging_intent_for_device(device_id, adapter_device_id),
    )


def _push_svi_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full owned SVI/IRB intent snapshot for a device.

    Store-only (deferred): the single device Apply commits via the svi-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.

    ``force=True`` (the device Apply) bypasses change-detection so an owned SVI whose
    adapter intent went stale/empty is re-pushed and actually applied instead of being
    silently skipped — mirrors the interface/VLAN/route-policy force-push. Without it,
    Apply marks the row 'deploying', pushes nothing, applies 0 items and the row sticks
    in 'deploying' forever.
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
        lambda: client.put_svi_intent(adapter_device_id, interfaces),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "svi"),
        lambda: _push_svi_intent_for_device(device_id, adapter_device_id),
    )


def _push_subinterface_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full owned dot1q subinterface intent snapshot.

    Store-only (deferred): the single device Apply commits via the
    subinterface-reconciler. Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) included.

    ``force=True`` (the device Apply) bypasses change-detection so an owned subinterface
    whose adapter intent went stale/empty is re-pushed and actually applied instead of
    silently skipped — mirrors the interface/VLAN/route-policy force-push. Without it, Apply
    marks the row 'deploying', pushes nothing, applies 0 items and it sticks 'deploying'.
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
        lambda: client.put_subinterface_intent(adapter_device_id, interfaces),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "subinterface"),
        lambda: _push_subinterface_intent_for_device(device_id, adapter_device_id),
    )


def _push_interface_mtu_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full owned per-interface MTU intent snapshot (Phase 2b).

    Store-only (deferred): the single device Apply commits via the mtu-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.

    ``force=True`` (the device Apply) bypasses change-detection so an owned MTU whose
    adapter intent went stale/empty is re-pushed and actually applied instead of silently
    skipped — mirrors the interface/VLAN/route-policy force-push. Without it, Apply marks
    the row 'deploying', pushes nothing, applies 0 items and it sticks 'deploying'.
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
        lambda: client.put_interface_mtu_intent(adapter_device_id, interfaces),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "interface_mtu"),
        lambda: _push_interface_mtu_intent_for_device(device_id, adapter_device_id),
    )


def _push_vlan_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full owned VLAN-database intent snapshot for a device (write).

    Store-only (deferred): the single device Apply commits via the vlan-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included; the VLAN name pushed
    is the LIVE NetBox name (operator is the source of truth for it).

    ``force`` re-pushes even if the snapshot looks unchanged — the single Apply calls
    this with ``force=True`` so a VLAN renamed in NetBox *after* it was accepted (the
    rename touches ipam.VLAN, which fires no plugin signal) still reaches the device.
    """
    from . import adapter_client as client
    from .models import NSOVLANState

    vlans = []
    for row in NSOVLANState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("vlan"):
        if row.vlan is None:
            continue
        vlans.append({"vlan_id": row.vlan.vid, "name": row.vlan.name or ""})

    _push_changed(
        (device_id, "vlan"),
        vlans,
        lambda: client.put_vlan_intent(adapter_device_id, vlans),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "vlan"),
        lambda: _push_vlan_intent_for_device(device_id, adapter_device_id),
    )


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
        _schedule_intent_push(
            (device_id, "vlan"),
            lambda d=device_id, a=adapter_device_id: _push_vlan_intent_for_device(d, a),
        )


def _push_bfd_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full owned per-interface BFD intent snapshot for a device.

    Store-only (deferred): the single device Apply commits via the bfd-reconciler.
    Only owned rows (_OWNED_PUSH_STATUSES, incl. apply_failed) are included.

    ``force=True`` (the device Apply) bypasses change-detection so an owned BFD whose
    adapter intent went stale/empty is re-pushed and actually applied instead of silently
    skipped — mirrors the interface/VLAN/route-policy force-push. Without it, Apply marks
    the row 'deploying', pushes nothing, applies 0 items and it sticks 'deploying'.
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
        lambda: client.put_bfd_intent(adapter_device_id, interfaces),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "bfd"),
        lambda: _push_bfd_intent_for_device(device_id, adapter_device_id),
    )


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
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "ip"),
        lambda: _push_ip_intent_for_device(device_id, adapter_device_id),
    )


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
        adapter_device_id = mgmt.adapter_device_id
        _schedule_intent_push(
            (device_id, "ip"),
            lambda: _push_ip_intent_for_device(device_id, adapter_device_id),
        )


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

    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "ip"),
        lambda: _push_ip_intent_for_device(device_id, adapter_device_id),
    )


def _push_static_route_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full static route intent snapshot for a device."""
    from . import adapter_client as client
    from .models import NSOStaticRouteState

    routes = []
    for row in NSOStaticRouteState.objects.filter(
        management__device_id=device_id,
        status__in=_OWNED_PUSH_STATUSES,
    ).select_related("static_route", "static_route__vrf"):
        sr = row.static_route
        if sr.next_hop is None:
            continue  # interface-only next-hop not supported by static-route-reconciler v1
        vrf_name = sr.vrf.name if sr.vrf else ""
        routes.append(
            {
                "vrf": vrf_name,
                "prefix": str(sr.prefix),
                "next_hop": str(sr.next_hop),
                "metric": sr.metric,
                "permanent": sr.permanent or False,
                "tag": sr.tag,
            }
        )

    _push_changed(
        (device_id, "static_route"),
        routes,
        lambda: client.put_static_route_intent(adapter_device_id, routes),
        force=force,
    )


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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "static_route"),
        lambda: _push_static_route_intent_for_device(device_id, adapter_device_id),
    )


# ── Greenfield static routes (operator-created in NetBox, not yet on the device) ──
#
# The reconcile path only ever creates an NSOStaticRouteState overlay for a route the
# device already reports (brownfield adoption). These handlers add the missing direction:
# a netbox_routing.StaticRoute the operator assigns to a managed device becomes an
# *accepted* overlay (owned intent) and pushes; removing/deleting it pushes the removal
# (full-replace). All wired from the plugin against the netbox_routing model — no fork edit.


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
        defaults={"status": "accepted", "accepted_at": timezone.now()},
    )
    if not created and state.status not in ("accepted", "deploying", "in_sync", "apply_failed"):
        state.status = "accepted"
        state.accepted_at = timezone.now()
    state.nso_prefix = str(static_route.prefix or "")
    state.nso_next_hop = str(static_route.next_hop or "")
    state.last_sync_at = timezone.now()
    state.save()  # → _on_static_route_state_save schedules the push


def _remove_static_route_for_device(static_route, device) -> None:
    """Drop the overlay for (device, route) and push the removal (full-replace)."""
    from .models import NSODeviceManagement, NSOStaticRouteState

    try:
        mgmt = NSODeviceManagement.objects.get(device=device)
    except NSODeviceManagement.DoesNotExist:
        return
    if mgmt.adapter_device_id is None:
        return
    NSOStaticRouteState.objects.filter(management=mgmt, static_route=static_route).delete()
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "static_route"),
        lambda: _push_static_route_intent_for_device(device_id, adapter_device_id),
    )


@_skip_on_render
def _on_routing_static_route_save(sender, instance, **kwargs):
    """Re-push when an owned greenfield route's fields (prefix/next-hop/…) are edited."""
    from .models import NSODeviceManagement, NSOStaticRouteState

    for device in instance.devices.all():
        try:
            mgmt = NSODeviceManagement.objects.get(device=device)
        except NSODeviceManagement.DoesNotExist:
            continue
        if mgmt.adapter_device_id is None:
            continue
        owned = NSOStaticRouteState.objects.filter(
            management=mgmt, static_route=instance, status__in=("accepted", "deploying", "in_sync", "apply_failed")
        ).exists()
        if owned:
            device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
            _schedule_intent_push(
                (device_id, "static_route"),
                lambda d=device_id, a=adapter_device_id: _push_static_route_intent_for_device(d, a),
            )


@_skip_on_render
def _on_routing_static_route_devices_changed(sender, instance, action, pk_set, reverse, **kwargs):
    """Device assigned to / removed from a route → own / remove + push (greenfield)."""
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
        lambda: client.put_isis_flex_algo_intent(adapter_device_id, flex_algos),
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
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "isis_flex_algo"),
        lambda: _push_isis_flex_algo_intent_for_device(device_id, adapter_device_id),
    )


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
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "isis_flex_algo"),
        lambda: _push_isis_flex_algo_intent_for_device(device_id, adapter_device_id),
    )


@_skip_on_render
def _on_routing_isis_flex_algo_save(sender, instance, **kwargs):
    """Flex-algo created/edited in NetBox → own it (accepted overlay) + push."""
    _accept_isis_flex_algo(instance)


@_skip_on_render
def _on_routing_isis_flex_algo_pre_delete(sender, instance, **kwargs):
    """Flex-algo deleted in NetBox → drop overlay + push removal before the cascade."""
    _remove_isis_flex_algo(instance)


def _push_l2_sap_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full Nokia L2 SAP intent snapshot for a device.

    ``force=True`` (the device Apply) bypasses change-detection so an owned SAP whose
    adapter intent went stale/empty is re-pushed and actually applied — mirrors the
    SVI/static-route force-push (rows are marked deploying, so a silently skipped push
    would strand them there forever).
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
        lambda: client.put_l2_sap_intent(adapter_device_id, saps),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "l2_sap"),
        lambda: _push_l2_sap_intent_for_device(device_id, adapter_device_id),
    )


def _push_lacp_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push (apply) the full LACP bundle intent snapshot for a device.

    Committing LACP is a device write, so on accept it only fires when the device
    is in auto-apply mode (see _on_lacp_state_save); the manual device Apply calls
    this with ``force=True`` to commit the owned snapshot as part of the one Apply.
    """
    from . import adapter_client as client
    from .models import NSOLACPBundleState, NSOLACPMemberState

    _owned = ("accepted", "deploying", "in_sync")
    bundles = []
    for b in NSOLACPBundleState.objects.filter(management__device_id=device_id, status__in=_owned).select_related(
        "interface"
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
        lambda: client.apply_lag_config(adapter_device_id, bundles),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "lacp"),
        lambda: _push_lacp_intent_for_device(device_id, adapter_device_id),
    )


# NetBox interface mode -> NSO switchport vocabulary.
_NETBOX_TO_NSO_MODE = {"access": "access", "tagged": "trunk", "tagged-all": "trunk-all"}


def _push_switchport_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push (apply) the device's owned L2 switchport snapshot.

    A device write, so on accept it only fires in auto-apply mode; the manual
    device Apply calls this with ``force=True`` as part of the single Apply.
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
        lambda: client.apply_switchport_config(adapter_device_id, interfaces),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "switchport"),
        lambda: _push_switchport_intent_for_device(device_id, adapter_device_id),
    )


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


def _push_isis_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full IS-IS intent snapshot (interfaces + processes) for a device.

    ``force=True`` bypasses change-detection (used by provisioning, which must always land
    its computed intent even if an identical snapshot was pushed earlier this process).
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
        lambda: client.put_isis_interface_intent(adapter_device_id, interfaces, processes=processes),
        force=force,
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "isis"),
        lambda: _push_isis_intent_for_device(device_id, adapter_device_id),
    )


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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "isis"),
        lambda: _push_isis_intent_for_device(device_id, adapter_device_id),
    )


def _build_bgp_router_list(routers: dict, scope_afs: dict) -> list:
    """Convert the routers dict + scope_afs into the adapter router_list format."""
    router_list = []
    covered: set[tuple[str, str]] = set()
    for asn_str, router_data in routers.items():
        scopes_out = []
        for vrf_str, scope_data in router_data["scopes"].items():
            af_map = scope_afs.get((asn_str, vrf_str), {})
            covered.add((asn_str, vrf_str))
            afs_out = [{"af": af_str, "redistribution": redist_entries} for af_str, redist_entries in af_map.items()]
            scope_out = dict(scope_data)
            scope_out["address_families"] = afs_out
            scopes_out.append(scope_out)
        router_list.append({"asn": asn_str, "scopes": scopes_out})

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
    ).select_related("management", "bgp_peer"):
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
        scopes[vrf_name]["peers"].append(peer_dict)

    router_list = _build_bgp_router_list(routers, scope_afs)
    _push_changed(
        (device_id, "bgp"),
        router_list,
        lambda: client.put_bgp_intent(adapter_device_id, router_list),
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "bgp"),
        lambda: _push_bgp_intent_for_device(device_id, adapter_device_id),
    )


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

    _schedule_redistribution_push(mgmt.device_id, mgmt.adapter_device_id, instance.dest_protocol)


def _push_route_policy_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full route-policy intent snapshot for a device.

    ``force=True`` (the device Apply) bypasses change-detection so an owned route-policy
    object whose adapter intent went stale/empty is re-pushed and actually applied, instead
    of being silently skipped — mirrors the interface/VLAN force-push. Without it, an owned
    route-policy row whose adapter intent row is missing applies 0 items and sticks in
    'deploying' forever (the device never gets the definition, so it never settles).
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

    resp = _push_changed(
        (device_id, "route_policy"),
        objects,
        lambda: client.put_route_policy_intent(adapter_device_id, objects),
        force=force,
    )
    _store_unsupported_members(owned_rows, resp)


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
        for e in obj.route_map_entries.all().order_by("sequence"):
            match_data = _as_json_dict(e.match)
            set_data = _as_json_dict(e.set)
            if e.flow_control is not None and "flow_control" not in set_data:
                # the read path lifts flow_control out of set-json into the model
                # field — put it back so the round-trip stays symmetric
                set_data["flow_control"] = e.flow_control
            # The universal Community model means a community-list of any kind is just the
            # one CommunityList — match_community_list carries every referenced list.
            community_names = list(e.match_community_list.values_list("name", flat=True))
            entry: dict = {
                "sequence": e.sequence,
                "action": e.action.lower() if e.action else "permit",
                "match-prefix-lists": list(e.match_prefix_list.values_list("name", flat=True)),
                "match-community-lists": community_names,
                "match-as-paths": list(e.match_aspath.values_list("name", flat=True)),
                "match-json": json.dumps(match_data, sort_keys=True),
                "set-json": json.dumps(set_data, sort_keys=True),
            }
            entries.append(entry)
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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "route_policy"),
        lambda: _push_route_policy_intent_for_device(device_id, adapter_device_id),
    )


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
        _schedule_intent_push(
            (device_id, "route_policy"),
            lambda d=device_id, a=adapter_device_id: _push_route_policy_intent_for_device(d, a),
        )


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


def _push_ospf_intent_for_device(device_id, adapter_device_id, force=False):
    """Build and push the full OSPF intent snapshot for a device.

    ``force=True`` bypasses change-detection (used by provisioning, which must always land
    its computed intent even if an identical snapshot was pushed earlier this process).
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
    _push_changed((device_id, "ospf"), payload, lambda: client.put_ospf_intent(adapter_device_id, payload), force=force)


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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "ospf"),
        lambda: _push_ospf_intent_for_device(device_id, adapter_device_id),
    )


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
    adapter_device_id = mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "ospf"),
        lambda: _push_ospf_intent_for_device(device_id, adapter_device_id),
    )


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
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "isis"),
        lambda: _push_isis_intent_for_device(device_id, adapter_device_id),
    )


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
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "isis"),
        lambda: _push_isis_intent_for_device(device_id, adapter_device_id),
    )


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
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "ospf"),
        lambda: _push_ospf_intent_for_device(device_id, adapter_device_id),
    )


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
    device_id, adapter_device_id = mgmt.device_id, mgmt.adapter_device_id
    _schedule_intent_push(
        (device_id, "ospf"),
        lambda: _push_ospf_intent_for_device(device_id, adapter_device_id),
    )


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
        _schedule_redistribution_push(mgmt.device_id, mgmt.adapter_device_id, state.dest_protocol)


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
        _recompute_on_cable_delete,
        sender=Cable,
        dispatch_uid="nso_plugin_cable_post_delete",
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
        _on_ip_address_delete,
        sender=IPAddress,
        dispatch_uid="nso_plugin_ipaddress_post_delete",
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
            _on_snmp_state_save,
            sender=snmp_model,
            dispatch_uid=f"nso_plugin_snmp_{snmp_model.__name__}_post_delete",
        )

    # Logging (remote syslog) state → intent push
    from .models import NSOLoggingHostState

    post_save.connect(
        _on_logging_state_save,
        sender=NSOLoggingHostState,
        dispatch_uid="nso_plugin_logging_host_state_post_save",
    )
    # Deletes must push the REDUCED snapshot too (the WP7-P1 SNMP regression class):
    # with only post_save wired, the adapter keeps applying a deleted host/SVI/subif/MTU
    # until some unrelated sibling row is saved. Caught live on sw01 — deleting an
    # applied SVI's overlay never retracted the irb unit.
    post_delete.connect(
        _on_logging_state_save,
        sender=NSOLoggingHostState,
        dispatch_uid="nso_plugin_logging_host_state_post_delete",
    )

    # SVI/IRB state → intent push (write path)
    from .models import NSOSVIState

    post_save.connect(
        _on_svi_state_save,
        sender=NSOSVIState,
        dispatch_uid="nso_plugin_svi_state_post_save",
    )
    post_delete.connect(
        _on_svi_state_save,
        sender=NSOSVIState,
        dispatch_uid="nso_plugin_svi_state_post_delete",
    )

    # dot1q subinterface state → intent push (write path)
    from .models import NSOSubinterfaceState

    post_save.connect(
        _on_subinterface_state_save,
        sender=NSOSubinterfaceState,
        dispatch_uid="nso_plugin_subinterface_state_post_save",
    )
    post_delete.connect(
        _on_subinterface_state_save,
        sender=NSOSubinterfaceState,
        dispatch_uid="nso_plugin_subinterface_state_post_delete",
    )

    # per-interface MTU state → intent push (Phase 2b write path)
    from .models import NSOInterfaceMtuState

    post_save.connect(
        _on_mtu_state_save,
        sender=NSOInterfaceMtuState,
        dispatch_uid="nso_plugin_interface_mtu_state_post_save",
    )
    post_delete.connect(
        _on_mtu_state_save,
        sender=NSOInterfaceMtuState,
        dispatch_uid="nso_plugin_interface_mtu_state_post_delete",
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
        _on_vlan_state_save,
        sender=NSOVLANState,
        dispatch_uid="nso_plugin_vlan_state_post_delete",
    )

    # ipam.VLAN rename → overlay drift visibility (no NSOVLANState signal otherwise)
    from ipam.models import VLAN

    post_save.connect(
        _on_vlan_change,
        sender=VLAN,
        dispatch_uid="nso_plugin_ipam_vlan_post_save",
    )
    pre_delete.connect(
        _on_ipam_vlan_pre_delete,
        sender=VLAN,
        dispatch_uid="nso_plugin_ipam_vlan_pre_delete",
    )

    # BFD state → intent push (BFD write path)
    from .models import NSOBFDInterfaceState

    post_save.connect(
        _on_bfd_state_save,
        sender=NSOBFDInterfaceState,
        dispatch_uid="nso_plugin_bfd_state_post_save",
    )
    post_delete.connect(
        _on_bfd_state_save,
        sender=NSOBFDInterfaceState,
        dispatch_uid="nso_plugin_bfd_state_post_delete",
    )

    # Static route state → intent push
    from .models import NSOStaticRouteState

    post_save.connect(
        _on_static_route_state_save,
        sender=NSOStaticRouteState,
        dispatch_uid="nso_plugin_static_route_state_post_save",
    )
    post_delete.connect(
        _on_static_route_state_save,
        sender=NSOStaticRouteState,
        dispatch_uid="nso_plugin_static_route_state_post_delete",
    )

    # L2 SAP state → intent push
    from .models import NSOL2SapState

    post_save.connect(
        _on_l2_sap_state_save,
        sender=NSOL2SapState,
        dispatch_uid="nso_plugin_l2_sap_state_post_save",
    )
    post_delete.connect(
        _on_l2_sap_state_save,
        sender=NSOL2SapState,
        dispatch_uid="nso_plugin_l2_sap_state_post_delete",
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
        _on_lacp_state_save,
        sender=NSOLACPBundleState,
        dispatch_uid="nso_plugin_lacp_bundle_state_post_delete",
    )
    post_delete.connect(
        _on_lacp_state_save,
        sender=NSOLACPMemberState,
        dispatch_uid="nso_plugin_lacp_member_state_post_delete",
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
        _on_switchport_state_save,
        sender=NSOSwitchportState,
        dispatch_uid="nso_plugin_switchport_state_post_delete",
    )

    # IS-IS interface state → intent push
    from .models import NSOISISInstanceState, NSOISISInterfaceState

    post_save.connect(
        _on_isis_interface_state_save,
        sender=NSOISISInterfaceState,
        dispatch_uid="nso_plugin_isis_interface_state_post_save",
    )
    post_delete.connect(
        _on_isis_interface_state_save,
        sender=NSOISISInterfaceState,
        dispatch_uid="nso_plugin_isis_interface_state_post_delete",
    )

    # IS-IS process (instance) state → intent push
    post_save.connect(
        _on_isis_instance_state_save,
        sender=NSOISISInstanceState,
        dispatch_uid="nso_plugin_isis_instance_state_post_save",
    )
    # No native ISISInstance pre_delete exists — this is the ONLY retraction path.
    post_delete.connect(
        _on_isis_instance_state_save,
        sender=NSOISISInstanceState,
        dispatch_uid="nso_plugin_isis_instance_state_post_delete",
    )

    # IS-IS Flex-Algo state → intent push
    from .models import NSOISISFlexAlgoState

    post_save.connect(
        _on_isis_flex_algo_state_save,
        sender=NSOISISFlexAlgoState,
        dispatch_uid="nso_plugin_isis_flex_algo_state_post_save",
    )
    post_delete.connect(
        _on_isis_flex_algo_state_save,
        sender=NSOISISFlexAlgoState,
        dispatch_uid="nso_plugin_isis_flex_algo_state_post_delete",
    )

    # BGP peer state → intent push
    from .models import NSOBGPPeerState

    post_save.connect(
        _on_bgp_peer_state_save,
        sender=NSOBGPPeerState,
        dispatch_uid="nso_plugin_bgp_peer_state_post_save",
    )
    # No native BGPPeer pre_delete exists — this is the ONLY retraction path
    # (gap confirmed live on rg03 during #7).
    post_delete.connect(
        _on_bgp_peer_state_save,
        sender=NSOBGPPeerState,
        dispatch_uid="nso_plugin_bgp_peer_state_post_delete",
    )

    # Route-policy state → intent push
    from .models import NSORoutePolicyState

    post_save.connect(
        _on_route_policy_state_save,
        sender=NSORoutePolicyState,
        dispatch_uid="nso_plugin_route_policy_state_post_save",
    )
    post_delete.connect(
        _on_route_policy_state_save,
        sender=NSORoutePolicyState,
        dispatch_uid="nso_plugin_route_policy_state_post_delete",
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
                _on_routing_policy_pre_delete,
                sender=_model,
                dispatch_uid=f"nso_plugin_routing_policy_pre_delete_{_model.__name__.lower()}",
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
                _on_routing_policy_entry_delete,
                sender=_entry,
                dispatch_uid=f"nso_plugin_routing_policy_entry_delete_{_entry.__name__.lower()}",
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
        _on_ospf_instance_state_save,
        sender=NSOOSPFInstanceState,
        dispatch_uid="nso_plugin_ospf_instance_state_post_delete",
    )
    post_delete.connect(
        _on_ospf_interface_state_save,
        sender=NSOOSPFInterfaceState,
        dispatch_uid="nso_plugin_ospf_interface_state_post_delete",
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
        _on_redistribution_state_save,
        sender=NSORedistributionState,
        dispatch_uid="nso_plugin_redistribution_state_post_delete",
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

        post_save.connect(
            _on_routing_static_route_save,
            sender=StaticRoute,
            dispatch_uid="nso_plugin_routing_static_route_post_save",
        )
        m2m_changed.connect(
            _on_routing_static_route_devices_changed,
            sender=StaticRoute.devices.through,
            dispatch_uid="nso_plugin_routing_static_route_devices_changed",
        )
        pre_delete.connect(
            _on_routing_static_route_pre_delete,
            sender=StaticRoute,
            dispatch_uid="nso_plugin_routing_static_route_pre_delete",
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
            _on_routing_ospf_instance_pre_delete,
            sender=OSPFInstance,
            dispatch_uid="nso_plugin_routing_ospf_instance_pre_delete",
        )
        pre_delete.connect(
            _on_routing_ospf_interface_pre_delete,
            sender=OSPFInterface,
            dispatch_uid="nso_plugin_routing_ospf_interface_pre_delete",
        )
    except ImportError:
        logger.debug("netbox_routing not installed — OSPF greenfield signals not registered")

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
            dispatch_uid="nso_plugin_routing_isis_flex_algo_pre_delete",
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
            _on_routing_isis_level_post_delete,
            sender=ISISLevel,
            dispatch_uid="nso_plugin_routing_isis_level_post_delete",
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
            dispatch_uid="nso_plugin_routing_isis_interface_pre_delete",
        )
    except ImportError:
        logger.debug("netbox_routing not installed — IS-IS interface greenfield signals not registered")
