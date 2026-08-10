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

logger = logging.getLogger(__name__)


def _owned_count(model, device, *, via: str = "management__device") -> int:
    from . import status_machine as sm

    return model.objects.filter(**{via: device}, status__in=list(sm.OWNED_STATES)).count()


def _scopes() -> list[dict]:
    """Registry: scope key → label, adapter intent table(s), owned-overlay counter, push fn.

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
    from . import signals
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
            "push": signals._push_isis_intent_for_device,
        },
        {
            "key": "isis_flex_algo",
            "label": "IS-IS Flex-Algo",
            "tables": ["isis_flex_algo_intent"],
            "owned": lambda d: _owned_count(NSOISISFlexAlgoState, d),
            "push": signals._push_isis_flex_algo_intent_for_device,
        },
        {
            "key": "bgp",
            "label": "BGP",
            "tables": ["bgp_router_intent"],
            "owned": lambda d: _owned_count(NSOBGPPeerState, d),
            "push": signals._push_bgp_intent_for_device,
            # One router intent row covers N owned peer rows — counts can never be
            # compared 1:1, so this scope only gets the orphan (owned == 0) rule.
            "parity": False,
        },
        {
            "key": "ospf",
            "label": "OSPF",
            "tables": ["ospf_instance_intent", "ospf_interface_intent"],
            "owned": lambda d: _owned_count(NSOOSPFInstanceState, d) + _owned_count(NSOOSPFInterfaceState, d),
            "push": signals._push_ospf_intent_for_device,
        },
        {
            "key": "route_policy",
            "label": "Route policy",
            "tables": ["route_policy_object_intent"],
            "owned": lambda d: _owned_count(NSORoutePolicyState, d),
            "push": signals._push_route_policy_intent_for_device,
        },
        {
            "key": "static_route",
            "label": "Static routes",
            "tables": ["static_route_intent"],
            "owned": lambda d: _owned_count(NSOStaticRouteState, d),
            "push": signals._push_static_route_intent_for_device,
        },
        {
            "key": "interface_ip",
            "label": "Interface IPs",
            "tables": ["interface_ip_intent"],
            "owned": lambda d: _owned_count(NSOInterfaceIPState, d, via="interface__device"),
            "push": signals._push_ip_intent_for_device,
        },
        {
            "key": "interface_mtu",
            "label": "Interface MTU",
            "tables": ["interface_mtu_intent"],
            "owned": lambda d: _owned_count(NSOInterfaceMtuState, d),
            "push": signals._push_interface_mtu_intent_for_device,
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
            "push": signals._push_interface_intent_for_device,
        },
        {
            "key": "vlan",
            "label": "VLANs",
            "tables": ["vlan_intent"],
            "owned": lambda d: _owned_count(NSOVLANState, d),
            "push": signals._push_vlan_intent_for_device,
        },
        {
            "key": "svi",
            "label": "SVIs / IRBs",
            "tables": ["svi_intent"],
            "owned": lambda d: _owned_count(NSOSVIState, d),
            "push": signals._push_svi_intent_for_device,
        },
        {
            "key": "subinterface",
            "label": "Subinterfaces",
            "tables": ["subinterface_intent"],
            "owned": lambda d: _owned_count(NSOSubinterfaceState, d),
            "push": signals._push_subinterface_intent_for_device,
        },
        {
            "key": "bfd",
            "label": "BFD",
            "tables": ["bfd_intent"],
            "owned": lambda d: _owned_count(NSOBFDInterfaceState, d),
            "push": signals._push_bfd_intent_for_device,
        },
        {
            "key": "l2_sap",
            "label": "L2 SAPs",
            "tables": ["l2_sap_intent"],
            "owned": lambda d: _owned_count(NSOL2SapState, d),
            "push": signals._push_l2_sap_intent_for_device,
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
            "push": signals._push_snmp_intent_for_device,
        },
        {
            "key": "logging",
            "label": "Logging",
            "tables": ["logging_host_intent", "logging_levels_intent"],
            "owned": lambda d: _owned_count(NSOLoggingHostState, d) + _owned_count(NSOLoggingLevelState, d),
            "push": signals._push_logging_intent_for_device,
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


def resync_intent(device, mgmt, keys: list[str] | None = None) -> list[str]:
    """Re-push the owned intent for *keys* (default: all orphaned/partial scopes) → clears them.

    Returns the scope keys re-synced. The push is the plugin's normal full-snapshot push, so
    for a scope NetBox owns nothing in, it sends an empty snapshot and the adapter full-replace
    removes the orphaned rows.

    The pushes run under ``store_only_pushes()`` (→ ``?store_only=true``): re-sync repairs the
    adapter's intent STORE only, so the adapter must skip its shrink-removal and auto-apply
    enqueues. Without the flag, the reduced snapshot auto-enqueued a removal job whose
    PUT-replace retracted FASTMAP-owned config from the real device — the exact opposite of
    the banner's "does not touch the device" promise (tracker #103, ra1.lab).
    """
    if mgmt is None or mgmt.adapter_device_id is None:
        return []
    from . import adapter_client as client

    if keys is None:
        keys = [d["key"] for d in compute_intent_drift(device, mgmt)]
    by_key = {sc["key"]: sc for sc in _scopes()}
    done: list[str] = []
    with client.store_only_pushes():
        for key in keys:
            sc = by_key.get(key)
            if sc is None:
                continue
            # force=True is load-bearing, not belt-and-braces. Re-sync exists precisely for
            # the split-brain where the ADAPTER lost the intent while the plugin's
            # process-global _last_pushed_hashes still holds the digest of the last push —
            # which is exactly the condition _push_changed reads as "unchanged, skip". The
            # re-sync would then silently no-op while the view reported success.
            sc["push"](mgmt.device_id, mgmt.adapter_device_id, force=True)
            done.append(key)
    return done


class _PushNotAcknowledged(Exception):
    """The adapter did not answer this device's push with a stored count.

    Carries the push record the rollback is about to discard, as ``(attempt, entry)``.
    """

    def __init__(self, record=(None, None)):
        super().__init__("the adapter did not acknowledge the static-route intent push")
        self.record = record


def _backfill_static_route_generations(mgmt) -> int:
    """Arm every owned overlay of *mgmt* still on the unallocated sentinel. Returns how many.

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
        return 0
    demote = [row.pk for row in rows if row.status == DEPLOYING]
    with signals.suppress_intent_push():
        for row in rows:
            signals._arm_static_route_generation(row)
            row.save(update_fields=list(signals._STATIC_ROUTE_ARMED_FIELDS))
    if demote:
        # .update(): a status save would re-fire the row's intent push, and this is
        # bookkeeping about intent that has not moved.
        NSOStaticRouteState.objects.filter(pk__in=demote).update(status="accepted")
    logger.info("Armed %s static-route overlay(s) of device %s from the generation sentinel", len(rows), mgmt.device_id)
    return len(rows)


def resync_static_route_intent_fleet(device_ids: list[int] | None = None) -> list[dict]:
    """Push every managed device's static-route intent once, so the adapter backfills ``route_id``.

    The adapter keeps its replacement fence shut while any stored row has a NULL ``route_id``
    and evaluates the fence on the pre-mutation row set, so this pass fills the ids and the
    *next* ordinary push is the first one that can plan a replacement.

    Deliberately not routed through :func:`resync_intent`: with the default ``keys`` that only
    re-syncs scopes that already look drifted — a device whose counts agree looks clean while
    every one of its rows is still id-less — and it appends each key unconditionally, so it
    cannot tell a rejected push from a skipped one. Here each device is acknowledged from the
    ``count`` the adapter answers with, and ``force=True`` makes a ``None`` return
    unambiguously a failure rather than a change-detection skip.

    Store-only throughout: this repairs the adapter's intent MIRROR, so it must not enqueue an
    apply or write a tombstone. A clear the resync happens to detect parks the row instead of
    being authorized.

    It is also the rollout pass for #1502 Appendix S: per device, in **one** transaction, it
    arms every owned overlay still on the generation sentinel and then pushes it. Arming and
    pushing cannot be separated — a generation the adapter never stored correlates with
    nothing, and a later run would find no sentinel row left to retry it with, so a push the
    adapter did not acknowledge rolls the arming back with it. Idempotent: a second run finds
    no sentinel rows, arms nothing and pushes an unchanged snapshot.
    """
    from django.db import transaction

    from . import adapter_client as client
    from . import signals
    from .models import NSODeviceManagement

    rows = NSODeviceManagement.objects.filter(adapter_device_id__isnull=False).select_related("device")
    if device_ids is not None:
        rows = rows.filter(device_id__in=device_ids)

    results: list[dict] = []
    with client.store_only_pushes():
        for mgmt in rows.order_by("device_id"):
            armed = 0
            try:
                with transaction.atomic():
                    armed = _backfill_static_route_generations(mgmt)
                    response = signals._push_static_route_intent_for_device(
                        mgmt.device_id, mgmt.adapter_device_id, force=True
                    )
                    count = signals.stored_static_route_count(response)
                    if count is None:
                        # Read the rejection the push just persisted, because rolling the
                        # arming back would take that record with it.
                        raise _PushNotAcknowledged(signals.read_push_record(mgmt.device_id, "static_route"))
            except _PushNotAcknowledged as rejected:
                logger.warning("Static-route intent re-sync was not acknowledged for device %s", mgmt.device_id)
                # Outside the rolled-back transaction: the reason the adapter gave is what
                # the operator acts on, and it is not part of what the rollback undoes.
                signals.restore_push_record(mgmt.device_id, "static_route", *rejected.record)
                armed, count = 0, None
            # Not an adapter rejection (the push returns None for those), so letting it out
            # would strand every later device unattempted and unreported.
            except Exception:  # noqa: BLE001
                logger.exception("Static-route intent re-sync raised for device %s", mgmt.device_id)
                armed, count = 0, None
            results.append(
                {
                    "device_id": mgmt.device_id,
                    "device": str(mgmt.device),
                    "adapter_device_id": mgmt.adapter_device_id,
                    "ok": count is not None,
                    "count": count,
                    "armed": armed,
                }
            )
    return results
