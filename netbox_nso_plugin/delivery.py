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

import contextlib
import contextvars
import dataclasses
import threading
from collections.abc import Callable

MODE_NORMAL = "normal"
MODE_STORE_ONLY = "store_only"
#: Adopt the ids of the rows the body still names and prune the uncorrelated residue; accept
#: no content, carry no authority, spawn no job. It exists to open a fence a pending genuine
#: deletion cannot open for itself (§4.4, OQ-O-8), and it is never a way to deliver anything.
MODE_BACKFILL_ONLY = "backfill_only"

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


# ── Rendering and sending, which the claim protocol must be able to separate ───
#
# A claim renders inside its own repeatable-read transaction and sends outside every
# transaction (§4.2), so the two halves of a push have to come apart. The push functions are
# the only renderers there are and each reaches exactly one choke point, ``_push_changed``,
# so the render is that function run with the send captured instead of made.

_CAPTURE: contextvars.ContextVar[list | None] = contextvars.ContextVar("nso_render_capture", default=None)


@dataclasses.dataclass
class Rendered:
    """One key's rendered request: the body, the call that sends a body, the success hook."""

    key: tuple
    payload: object
    #: Takes the body to send, so a replay can send the claim's stored body rather than
    #: whatever the re-render produced. The sequence must carry the digest it was admitted at.
    do_push: Callable
    #: The scope's own success side effect, run on the response the send returned.
    on_response: Callable | None = None


def capture(rendered: Rendered) -> bool:
    """Record *rendered* when a render is in progress, and answer whether it was recorded."""
    sink = _CAPTURE.get()
    if sink is None:
        return False
    sink.append(rendered)
    return True


def render(key: str, device_id, adapter_device_id) -> Rendered:
    """Build the key's request body for one device without sending anything."""
    entry = delivery_keys()[key]
    sink: list = []
    token = _CAPTURE.set(sink)
    try:
        entry.push(device_id, adapter_device_id)
    finally:
        _CAPTURE.reset(token)
    if len(sink) != 1:
        raise RuntimeError(f"the {key} push rendered {len(sink)} bodies, expected exactly one")
    return sink[0]


class SendDeadlineExceeded(Exception):
    """One send outlived its total wall-clock budget (O-P16)."""

    code = "nso_send_deadline"


def _under_deadline(do_push: Callable, seconds: float) -> Callable:
    """Bound one transport call by wall clock, which the client's timeouts cannot do.

    ``(connect, read)`` measures the gap between bytes, so a response dripping one byte at
    a time resets it forever. The call therefore runs on its own thread and is abandoned
    when the budget runs out; the budget is well under the lease, so an abandoned call is
    long finished before a scavenger may take the operation over.
    """

    def _call(body):
        context = contextvars.copy_context()
        answer: dict = {}
        done = threading.Event()

        def _run():
            from django.db import connections

            try:
                answer["result"] = context.run(do_push, body)
            except BaseException as exc:  # noqa: BLE001 (re-raised on the sender's thread)
                answer["error"] = exc
            finally:
                connections.close_all()  # this thread's own connections, nobody else's
                done.set()

        threading.Thread(target=_run, name="nso-intent-push", daemon=True).start()
        if not done.wait(seconds):
            raise SendDeadlineExceeded(f"the adapter did not answer within {seconds}s")
        if "error" in answer:
            raise answer["error"]
        return answer["result"]

    return _call


def send(
    rendered: Rendered,
    body,
    *,
    mode: str = MODE_NORMAL,
    mark: bool = False,
    push_seq: int | None = None,
    deadline: float | None = None,
):
    """Send *body* for an already-rendered key, and return the adapter's answer.

    The mode and the deletion mark ride on the request as query flags, so they are applied
    here rather than being baked into a key: the same scope is delivered normally and
    store-only. The sequence is a header, and only an in-protocol key carries one.
    """
    from . import adapter_client, signals

    entry = delivery_keys()[rendered.key[1]]
    if deadline is not None:
        rendered = dataclasses.replace(rendered, do_push=_under_deadline(rendered.do_push, deadline))
    with contextlib.ExitStack() as stack:
        if mode == MODE_STORE_ONLY:
            stack.enter_context(adapter_client.store_only_pushes())
        if mode == MODE_BACKFILL_ONLY:
            stack.enter_context(adapter_client.backfill_only_pushes())
        if mark:
            stack.enter_context(adapter_client.delete_origin_pushes())
        if push_seq is not None and entry.in_protocol:
            stack.enter_context(adapter_client.push_seq(push_seq))
        return signals._send_rendered(rendered, body)


def deliver(key: str, device_id, adapter_device_id, *, mode: str = MODE_NORMAL, mark: bool = False):
    """Render *key* for one device and send it straight away, outside the claim protocol."""
    rendered = render(key, device_id, adapter_device_id)
    return send(rendered, rendered.payload, mode=mode, mark=mark)
