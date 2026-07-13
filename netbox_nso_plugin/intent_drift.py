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
            "tables": ["logging_host_intent"],
            "owned": lambda d: _owned_count(NSOLoggingHostState, d),
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
