# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Detect (and re-sync away) adapter↔NetBox intent split-brain.

The adapter mirrors operator intent per scope in its ``*_intent`` tables; the plugin's
``NSO*State`` overlays are the source of truth for what NetBox *owns*. They can diverge —
e.g. a migration reset the plugin overlays to ``imported`` while the adapter kept its
``accepted`` rows ([[adapter-intent-split-brain]]). Such *orphaned* adapter intent is
invisible in the UI yet would be pushed by a device-wide apply.

This module surfaces it: for each registered scope, if the adapter holds intent but NetBox
owns nothing in that scope, it is orphaned. For scopes with count *parity* (one owned overlay
row ↔ one adapter intent row; all scopes except BGP, where one router intent row covers N
owned peers), an adapter count *exceeding* the owned count is flagged as **partial**
split-brain — stale rows hiding behind legitimate ownership. ``resync_intent`` fixes both by
re-pushing the scope's *current* owned intent — the adapter's full-replace PUT then drops the
surplus rows (empty push → all removed). It can only ever remove intent NetBox doesn't own.

Design + per-scope parity audit: nso-adapter ``docs/intent-split-brain-design.md``.
"""

from __future__ import annotations

import logging

from .deployment import guarded as _deployment_guarded

logger = logging.getLogger(__name__)


def _owned_count(model, device, *, via: str = "management__device") -> int:
    from . import status_machine as sm

    return model.objects.filter(**{via: device}, status__in=list(sm.OWNED_STATES)).count()


def _delivery_key(scope: dict) -> str:
    """Return the delivery key a drift scope re-syncs through."""
    return scope.get("delivery_key", scope["key"])


def _scopes() -> list[dict]:
    """Registry: scope key → label, adapter intent table(s), owned-overlay counter.

    Covers the routing + interface families where the split-brain has been observed (the
    migration-reset overlays). Extend by appending an entry; the detector + re-sync pick it
    up automatically.

    ``parity`` (default True) declares that one owned overlay row corresponds to exactly one
    adapter intent row in a healthy state, enabling partial-drift detection (adapter count >
    owned count). Set it False where that mapping is structurally not 1:1 (BGP: one
    ``bgp_router_intent`` row covers N owned peers) — such scopes fall back to the
    orphan-only rule. Push-time row *skips* (dangling FKs, missing vault refs) only ever make
    the adapter hold FEWER rows than NetBox owns, which the partial rule ignores by design.
    """
    from .models import (
        NSOBFDInterfaceState,
        NSOBGPPeerState,
        NSOInterfaceIPState,
        NSOInterfaceMtuState,
        NSOInterfaceState,
        NSOISISFlexAlgoState,
        NSOISISInstanceState,
        NSOISISInterfaceState,
        NSOL2SapState,
        NSOLoggingHostState,
        NSOLoggingLevelState,
        NSOOSPFInstanceState,
        NSOOSPFInterfaceState,
        NSORoutePolicyState,
        NSOSnmpCommunityState,
        NSOSnmpHostState,
        NSOSnmpSystemInfoState,
        NSOSnmpV3UserState,
        NSOStaticRouteState,
        NSOSubinterfaceState,
        NSOSVIState,
        NSOVLANState,
    )

    def _snmp_owned(d):
        return sum(
            _owned_count(m, d)
            for m in (NSOSnmpCommunityState, NSOSnmpHostState, NSOSnmpSystemInfoState, NSOSnmpV3UserState)
        )

    # Covers every device/interface-keyed *_intent table. Not listed (by design):
    #   redistribution_intent — written/cleared transitively by the protocol pushes
    #     (it rides inside the IS-IS/BGP/OSPF intent payload);
    #   bgp_af/peer*_intent — children of bgp_router_intent (cascade);
    #   switchport / LACP — owned in NetBox, applied directly, NOT mirrored as adapter intent.
    return [
        {
            "key": "isis",
            "label": "IS-IS",
            "tables": ["isis_interface_intent", "isis_process_intent"],
            "owned": lambda d: _owned_count(NSOISISInterfaceState, d) + _owned_count(NSOISISInstanceState, d),
        },
        {
            "key": "isis_flex_algo",
            "label": "IS-IS Flex-Algo",
            "tables": ["isis_flex_algo_intent"],
            "owned": lambda d: _owned_count(NSOISISFlexAlgoState, d),
        },
        {
            "key": "bgp",
            "label": "BGP",
            "tables": ["bgp_router_intent"],
            "owned": lambda d: _owned_count(NSOBGPPeerState, d),
            # One router intent row covers N owned peer rows — counts can never be
            # compared 1:1, so this scope only gets the orphan (owned == 0) rule.
            "parity": False,
        },
        {
            "key": "ospf",
            "label": "OSPF",
            "tables": ["ospf_instance_intent", "ospf_interface_intent"],
            "owned": lambda d: _owned_count(NSOOSPFInstanceState, d) + _owned_count(NSOOSPFInterfaceState, d),
        },
        {
            "key": "route_policy",
            "label": "Route policy",
            "tables": ["route_policy_object_intent"],
            "owned": lambda d: _owned_count(NSORoutePolicyState, d),
        },
        {
            "key": "static_route",
            "label": "Static routes",
            "tables": ["static_route_intent"],
            "owned": lambda d: _owned_count(NSOStaticRouteState, d),
        },
        {
            "key": "interface_ip",
            "label": "Interface IPs",
            # The one scope the two registries name differently (O-P12); every other drift
            # key IS its delivery key, which ``_delivery_key`` states once and no other way.
            "delivery_key": "ip",
            "tables": ["interface_ip_intent"],
            "owned": lambda d: _owned_count(NSOInterfaceIPState, d, via="interface__device"),
        },
        {
            "key": "interface_mtu",
            "label": "Interface MTU",
            "tables": ["interface_mtu_intent"],
            "owned": lambda d: _owned_count(NSOInterfaceMtuState, d),
        },
        {
            "key": "interface",
            "label": "Interface attributes",
            "tables": ["interface_intent"],
            # Ownership is status-based (status in OWNED_STATES) — the canonical test,
            # identical to every other scope. Mirror the push predicate in
            # _push_interface_intent_for_device exactly, or owned rows would read as
            # orphaned/partial.
            "owned": lambda d: _owned_count(NSOInterfaceState, d, via="interface__device"),
        },
        {
            "key": "vlan",
            "label": "VLANs",
            "tables": ["vlan_intent"],
            "owned": lambda d: _owned_count(NSOVLANState, d),
        },
        {
            "key": "svi",
            "label": "SVIs / IRBs",
            "tables": ["svi_intent"],
            "owned": lambda d: _owned_count(NSOSVIState, d),
        },
        {
            "key": "subinterface",
            "label": "Subinterfaces",
            "tables": ["subinterface_intent"],
            "owned": lambda d: _owned_count(NSOSubinterfaceState, d),
        },
        {
            "key": "bfd",
            "label": "BFD",
            "tables": ["bfd_intent"],
            "owned": lambda d: _owned_count(NSOBFDInterfaceState, d),
        },
        {
            "key": "l2_sap",
            "label": "L2 SAPs",
            "tables": ["l2_sap_intent"],
            "owned": lambda d: _owned_count(NSOL2SapState, d),
        },
        {
            "key": "snmp",
            "label": "SNMP",
            "tables": [
                "snmp_community_intent",
                "snmp_host_intent",
                "snmp_system_info_intent",
                "snmp_v3_user_intent",
            ],
            "owned": _snmp_owned,
        },
        {
            "key": "logging",
            "label": "Logging",
            "tables": ["logging_host_intent", "logging_levels_intent"],
            "owned": lambda d: _owned_count(NSOLoggingHostState, d) + _owned_count(NSOLoggingLevelState, d),
        },
    ]


def compute_intent_drift(device, mgmt) -> list[dict]:
    """Return drifted-intent scopes for *device*.

    Two flavours, distinguished by the ``partial`` flag on each entry:

    - **orphaned** (``partial: False``) — adapter holds intent, NetBox owns nothing in the
      scope;
    - **partial** (``partial: True``) — for parity scopes only: the adapter holds *more*
      rows than NetBox owns, so the surplus is stale even though the scope looks healthy.

    One cheap adapter call (GET intent-summary). Returns ``[]`` on any adapter error or when
    nothing is drifted, so the caller renders nothing.
    """
    if mgmt is None or mgmt.adapter_device_id is None:
        return []
    from . import adapter_client as client

    try:
        summary = client.get_intent_summary(mgmt.adapter_device_id)
    except Exception as exc:  # adapter down / unexpected — never break the tab render
        logger.debug("intent-summary unavailable for device %s: %s", device.pk, exc)
        return []

    scopes_intent = summary.get("scopes", {})
    drift: list[dict] = []
    for sc in _scopes():
        count = sum(scopes_intent.get(t, {}).get("count", 0) for t in sc["tables"])
        if count == 0:
            continue
        owned = sc["owned"](device)
        if owned > 0 and not (sc.get("parity", True) and count > owned):
            continue  # NetBox owns enough in this scope → adapter rows are legit
        drift.append(
            {
                "key": sc["key"],
                "label": sc["label"],
                "count": count,
                "owned": owned,
                "partial": owned > 0,
                "applied": sum(scopes_intent.get(t, {}).get("applied", 0) for t in sc["tables"]),
                "failed": sum(scopes_intent.get(t, {}).get("failed", 0) for t in sc["tables"]),
            }
        )
    return drift


@_deployment_guarded("intent resync")
def resync_intent(device, mgmt, keys: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Re-push the owned intent for *keys* (default: all orphaned/partial scopes) → clears them.

    Returns ``(done, failed)``: the scope keys the adapter acknowledged, and the keys whose
    push it refused or never answered. A refusal clears no orphaned row, so reporting it as
    done told the operator the split-brain was repaired while the drift stood (#1557). The
    push is the plugin's normal full-snapshot push, so for a scope NetBox owns nothing in, it
    sends an empty snapshot and the adapter full-replace removes the orphaned rows.

    The pushes use ``delivery.MODE_STORE_ONLY`` (→ ``?store_only=true``): re-sync repairs the
    adapter's intent STORE only, so the adapter must skip its shrink-removal and auto-apply
    enqueues. Without the flag, the reduced snapshot auto-enqueued a removal job whose
    PUT-replace retracted FASTMAP-owned config from the real device — the exact opposite of
    the banner's "does not touch the device" promise (tracker #103, ra1.lab).
    """
    if mgmt is None or mgmt.adapter_device_id is None:
        return [], []
    from . import delivery, drain

    if keys is None:
        keys = [d["key"] for d in compute_intent_drift(device, mgmt)]
    by_key = {sc["key"]: sc for sc in _scopes()}
    done: list[str] = []
    failed: list[str] = []
    for key in keys:
        sc = by_key.get(key)
        if sc is None:
            continue
        # force=True is load-bearing, not belt-and-braces. Re-sync exists precisely for the
        # split-brain where the ADAPTER lost the intent while the plugin's acknowledged
        # baseline still names that body — which is what the claim reads as "unchanged,
        # drop". The re-sync would then silently no-op while the view reported success.
        try:
            outcome = drain.drain_key(mgmt.device_id, _delivery_key(sc), mode=delivery.MODE_STORE_ONLY, force=True)
        except Exception:  # noqa: BLE001 (one scope's refusal must not strand the rest unattempted)
            logger.exception("Intent re-sync raised for device %s scope %s", mgmt.device_id, key)
            outcome = None
        # The outcome is independent of whether the acknowledged response has a body.
        (done if outcome == drain.SUCCEEDED else failed).append(key)
    return done, failed


class _PushNotAcknowledged(Exception):
    """The adapter did not answer this device's push with a stored count."""


def _backfill_static_route_generations(mgmt) -> list[dict]:
    """Arm every owned overlay of *mgmt* still on the unallocated sentinel.

    Returns the pre-arm value of every field it wrote, one dict per row, which is what
    :func:`_restore_static_route_generations` puts back when the push that was supposed to
    carry these generations is not acknowledged.

    Pre-P2 owned rows keep ``intent_generation = 0``: the push sends that as null, the
    adapter adopts nothing, and every result the row ever produces is non-settling. The
    fleet pass is where they get a generation, because "one pass that leaves every owned
    overlay correlatable" is one concern — splitting it across two commands invites an
    operator to run one and not the other.

    A row already ``deploying`` is **demoted to accepted**. Its new generation makes any
    in-flight result uncorrelatable, so leaving it ``deploying`` would strand it until the
    backstop fires; a new generation means unsettled intent, and ``accepted`` is what
    unsettled intent reads as. ``in_sync``, ``accepted`` and ``apply_failed`` rows keep
    their status and only change generation, so the *next* result correlates and no badge
    flickers. ``accepted_at`` is untouched — it dates first ownership.

    The candidate set is the pusher's own (``signals.PUSHED_STATIC_ROUTE_FILTER``), not a
    broader "owned" one: a route with an interface-only next hop is owned but has no place
    in the snapshot, so arming it would mint a generation the adapter never receives — and
    a later run would find no sentinel row to retry, leaving an Apply free to promote a row
    nothing can settle. Only ``self`` is locked, so the join the filter adds cannot take
    ``static_route`` locks against the content transition's own order.

    Rows are locked in the same order the content transition takes them (``management_id``,
    then pk), and the pushes the arming saves would fire are suppressed: the caller's own
    forced push is the one that carries these generations to the adapter.
    """
    from . import signals
    from .intent_generation import UNALLOCATED
    from .models import NSOStaticRouteState
    from .status_machine import DEPLOYING

    rows = list(
        NSOStaticRouteState.objects.select_for_update(of=("self",))
        .filter(signals.PUSHED_STATIC_ROUTE_FILTER, management=mgmt, intent_generation=UNALLOCATED)
        .order_by("management_id", "pk")
    )
    if not rows:
        return []
    armed_fields = signals._STATIC_ROUTE_ARMED_FIELDS
    before = [
        {"pk": row.pk, "status": row.status, **{field: getattr(row, field) for field in armed_fields}} for row in rows
    ]
    demote = [row.pk for row in rows if row.status == DEPLOYING]
    with signals.suppress_intent_push():
        for row in rows:
            signals._arm_static_route_generation(row)
            row.save(update_fields=list(signals._STATIC_ROUTE_ARMED_FIELDS))
    for snapshot, row in zip(before, rows, strict=True):
        # The restore only changes a row that remains in its post-arm state.
        snapshot["armed_generation"] = row.intent_generation
        snapshot["armed_status"] = "accepted" if snapshot["status"] == DEPLOYING else row.status
    if demote:
        # .update(): a status save would re-fire the row's intent push, and this is
        # bookkeeping about intent that has not moved.
        NSOStaticRouteState.objects.filter(pk__in=demote).update(status="accepted")
    logger.info("Armed %s static-route overlay(s) of device %s from the generation sentinel", len(rows), mgmt.device_id)
    return before


def _safe_restore(before: list[dict], device_id: int) -> tuple[int, list[dict]]:
    """Restore *before*, reporting a failure rather than raising it.

    The rollback runs inside the handlers that keep one device's failure local, and a raise
    from an except block is not caught by the handlers beside it. Letting one out would make
    the containment itself abort the pass, stranding every later device unattempted and
    unreported. The rows it could not restore stay armed and a later run retries them.
    """
    restored = 0
    unrestored: list[dict] = []
    for index, snapshot in enumerate(before):
        try:
            restored_now = _restore_static_route_generations([snapshot])
        except Exception:  # noqa: BLE001, the pass must outlive one device's rollback
            logger.exception("Static-route generation rollback failed for device %s", device_id)
            unrestored.extend(before[index:])
            break
        restored += restored_now
        if not restored_now:
            unrestored.append(snapshot)
    return restored, unrestored


def _restore_static_route_generations(before: list[dict]) -> int:
    """Put back every armed row this pass still owns, and return how many.

    The send cannot run inside the arming transaction any more: a claim sets its own
    isolation level, which PostgreSQL accepts only before a transaction's first statement,
    and the send must hold no row lock at all. So the rollback that used to ride the
    transaction becomes an explicit inverse, restoring the sentinel and the demoted status
    so a later run finds these rows and retries them. Without it a generation the adapter
    never stored would correlate with nothing forever.

    Outside a transaction the inverse needs a compare-and-set on the armed generation and
    status. An operator can re-accept, promote, or settle a row while the push is on the
    wire. A row that moved is left alone, and is not counted as rolled back.
    """
    from .models import NSOStaticRouteState
    from .status_machine import DEPLOYING

    restored = 0
    for snapshot in before:
        fields = dict(snapshot)
        pk = fields.pop("pk")
        armed_generation = fields.pop("armed_generation")
        armed_status = fields.pop("armed_status")
        original_status = fields.pop("status")
        if original_status == DEPLOYING:
            fields["status"] = original_status
        restored += NSOStaticRouteState.objects.filter(
            pk=pk,
            intent_generation=armed_generation,
            status=armed_status,
        ).update(**fields)
    return restored


@_deployment_guarded("intent resync")
def resync_static_route_intent_fleet(device_ids: list[int] | None = None) -> list[dict]:
    """Push every managed device's static-route intent once, so the adapter backfills ``route_id``.

    The adapter keeps its replacement fence shut while any stored row has a NULL ``route_id``
    and evaluates the fence on the pre-mutation row set, so this pass fills the ids and the
    *next* ordinary push is the first one that can plan a replacement.

    Deliberately not routed through :func:`resync_intent`: with the default ``keys`` that only
    re-syncs scopes that already look drifted, and a device whose counts agree looks clean
    while every one of its rows is still id-less. Here each device is acknowledged from the
    ``count`` the adapter answers with, and the forced claim makes a ``None`` return
    unambiguously a failure rather than a drop against the acknowledged baseline.

    Store-only throughout: this repairs the adapter's intent MIRROR, so it must not enqueue an
    apply or write a tombstone. A clear the resync happens to detect parks the row instead of
    being authorized.

    It is also the rollout pass for #1502 Appendix S: per device it arms every owned overlay
    still on the generation sentinel and then pushes it. The two still stand or fall
    together — a generation the adapter never stored correlates with nothing, and a later run
    would find no sentinel row left to retry it with — but they can no longer share one
    transaction, because the send holds no lock and sets its own isolation level. So the
    arming commits first and an unacknowledged push RESTORES it, which leaves the same rows
    for the next run. Idempotent: a second run finds no sentinel rows, arms nothing and
    pushes an unchanged snapshot.
    """
    from django.db import transaction

    from . import delivery, drain, signals
    from .models import NSODeviceManagement

    rows = NSODeviceManagement.objects.filter(adapter_device_id__isnull=False).select_related("device")
    if device_ids is not None:
        rows = rows.filter(device_id__in=device_ids)

    results: list[dict] = []
    for mgmt in rows.order_by("device_id"):
        armed_rows: list[dict] = []
        rolled_back = 0
        try:
            with transaction.atomic():
                armed_rows = _backfill_static_route_generations(mgmt)
            response = drain.push_now(mgmt.device_id, "static_route", mode=delivery.MODE_STORE_ONLY, force=True)
            count = signals.stored_static_route_count(response)
            if count is None:
                raise _PushNotAcknowledged
        except _PushNotAcknowledged:
            logger.warning("Static-route intent re-sync was not acknowledged for device %s", mgmt.device_id)
            rolled_back, armed_rows = _safe_restore(armed_rows, mgmt.device_id)
            count = None
        # Not an adapter rejection (the claim records those and answers None), so letting it
        # out would strand every later device unattempted and unreported.
        except Exception:  # noqa: BLE001
            logger.exception("Static-route intent re-sync raised for device %s", mgmt.device_id)
            rolled_back, armed_rows = _safe_restore(armed_rows, mgmt.device_id)
            count = None
        results.append(
            {
                "device_id": mgmt.device_id,
                "device": str(mgmt.device),
                "adapter_device_id": mgmt.adapter_device_id,
                "ok": count is not None,
                "count": count,
                "armed": len(armed_rows),
                "armed_rolled_back": rolled_back,
            }
        )
    return results
