# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O — the delivery registry: every key a push can be delivered under.

The drift registry (``intent_drift``) answers a different question — which adapter tables a
scope owns — and enumerates sixteen scopes under partly different names (``ip`` there is
``interface_ip`` in the adapter's API). This one enumerates the **eighteen delivery keys**
the push sites actually use, and says of each whether it is *in protocol*: whether its
delivery is a logical operation the adapter admits, receipts and can replay.

``lacp`` and ``switchport`` are **out of protocol**. They are direct-apply endpoints whose
device write happens synchronously inside the request and which answer a failed apply with
HTTP 200 and an error envelope, so no receipt can be atomic with their effect and the
generic admission path cannot tell their success from their failure. They keep today's
direct client calls; the split card owns their entry.

The request mode (normal, store-only) is an argument of :func:`deliver`, never a property of
a key: one scope is delivered both ways — SNMP normally on save and store-only from the
resync, and static routes likewise.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

MODE_NORMAL = "normal"
MODE_STORE_ONLY = "store_only"

MARKING_QUERY_FLAG = "query_flag"
MARKING_PER_OBJECT = "per_object"


@dataclasses.dataclass(frozen=True)
class DeliveryKey:
    """One ``(device, scope)`` delivery key: how it is pushed, and under which contract."""

    key: str
    label: str
    #: In protocol: carries ``X-Push-Seq``, is admitted against a receipt and can be replayed.
    in_protocol: bool
    #: How a deletion is authorized on the wire — a query flag today, per object after O3.
    marking_mode: str
    #: The full-device push, which owns this scope's own success side effects.
    push: Callable


_REGISTRY: dict[str, DeliveryKey] = {}


def _build() -> dict[str, DeliveryKey]:
    from . import signals

    # (key, label, in_protocol, push)
    keys = [
        ("interface", "Interface", True, signals._push_interface_intent_for_device),
        ("ip", "Interface IP", True, signals._push_ip_intent_for_device),
        ("snmp", "SNMP", True, signals._push_snmp_intent_for_device),
        ("logging", "Logging", True, signals._push_logging_intent_for_device),
        ("svi", "SVI", True, signals._push_svi_intent_for_device),
        ("subinterface", "Subinterface", True, signals._push_subinterface_intent_for_device),
        ("interface_mtu", "Interface MTU", True, signals._push_interface_mtu_intent_for_device),
        ("vlan", "VLAN", True, signals._push_vlan_intent_for_device),
        ("bfd", "BFD", True, signals._push_bfd_intent_for_device),
        ("static_route", "Static route", True, signals._push_static_route_intent_for_device),
        ("isis_flex_algo", "IS-IS Flex-Algo", True, signals._push_isis_flex_algo_intent_for_device),
        ("l2_sap", "L2 SAP", True, signals._push_l2_sap_intent_for_device),
        ("isis", "IS-IS", True, signals._push_isis_intent_for_device),
        ("bgp", "BGP", True, signals._push_bgp_intent_for_device),
        ("route_policy", "Route policy", True, signals._push_route_policy_intent_for_device),
        ("ospf", "OSPF", True, signals._push_ospf_intent_for_device),
        ("lacp", "LACP", False, signals._push_lacp_intent_for_device),
        ("switchport", "Switchport", False, signals._push_switchport_intent_for_device),
    ]
    return {
        key: DeliveryKey(
            key=key,
            label=label,
            in_protocol=in_protocol,
            # Static routes leave ``query_flag`` at O3, one key at a time; O1 changes none.
            marking_mode=MARKING_QUERY_FLAG,
            push=push,
        )
        for key, label, in_protocol, push in keys
    }


def delivery_keys() -> dict[str, DeliveryKey]:
    """Return the registry, built once. It is the mapping itself, not a copy."""
    if not _REGISTRY:
        _REGISTRY.update(_build())
    return _REGISTRY


def deliver(key: str, device_id, adapter_device_id, *, mode: str = MODE_NORMAL, force: bool = False):
    """Deliver *key* for one device under *mode*, and return the adapter's answer.

    The mode rides on the request as a query flag, so it is applied here rather than being
    baked into a key: the same scope is delivered normally and store-only.
    """
    entry = delivery_keys()[key]
    if mode == MODE_STORE_ONLY:
        from . import adapter_client

        with adapter_client.store_only_pushes():
            return entry.push(device_id, adapter_device_id, force=force)
    return entry.push(device_id, adapter_device_id, force=force)
