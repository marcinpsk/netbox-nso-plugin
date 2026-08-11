# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1): shared fixtures for the claim protocol and the drain pass.

The claim renders inside its own transaction and sends outside it, so its pins cross
transaction boundaries and run as ``TransactionTestCase``. That means no wrapping test
transaction and real commits, which is also what lets :class:`ReceiptAdapter` stand in for
the far side at the transport boundary: it keeps one receipt per endpoint, so a replay of
an already-accepted sequence returns the stored response and applies nothing, exactly as
§4.4's admission table requires.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
from unittest.mock import MagicMock, patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import transaction

from ._adapter_http import _REAL_SESSION, make_response

PUT_STATIC = "netbox_nso_plugin.adapter_client.put_static_route_intent"
PUT_VLAN = "netbox_nso_plugin.adapter_client.put_vlan_intent"
CFG = {"url": "http://adapter", "token": "tok", "verify_tls": True, "ca_cert_path": None, "timeout": 30}


def make_device(tag: str, index: int = 1):
    """A device with the minimum NetBox scaffolding a management row needs."""
    mfg, _ = Manufacturer.objects.get_or_create(name=f"Cl{tag}Mfg", slug=f"cl{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Cl{tag}Dev", slug=f"cl{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Cl{tag}Role", slug=f"cl{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Cl{tag}Site", slug=f"cl{tag}site")
    return Device.objects.create(name=f"cl-{tag}-rtr-{index}", device_type=dt, role=role, site=site)


def make_mgmt(device, tag: str, adapter_device_id: int):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    inst, _ = NSOInstance.objects.get_or_create(
        name=f"cl-{tag}-inst", defaults={"adapter_instance_id": f"cl-{tag}-inst"}
    )
    return NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"nso-cl-{tag}-{device.pk}",
        adapter_device_id=adapter_device_id,
    )


def make_managed(tag: str, adapter_device_id: int, index: int = 1):
    """A managed device, with the fixture's own drain silenced."""
    with without_commit_drain(), transaction.atomic():
        device = make_device(tag, index)
        return device, make_mgmt(device, tag, adapter_device_id)


def own_vlan(mgmt, vid: int, tag: str):
    """One owned VLAN overlay, which is what a VLAN render puts on the wire."""
    from ipam.models import VLAN

    from netbox_nso_plugin.models import NSOVLANState

    with without_commit_drain(), transaction.atomic():
        vlan = VLAN.objects.create(vid=vid, name=f"cl-{tag}-v{vid}")
        return NSOVLANState.objects.create(management=mgmt, vlan=vlan, status="accepted")


def own_route(mgmt, prefix: str, next_hop: str, *, device=None):
    """A route assigned to the device and owned by it, as the accept path leaves it."""
    from netbox_routing.models import StaticRoute

    from netbox_nso_plugin.signals import _accept_static_route_for_device, suppress_intent_push

    with without_commit_drain(), transaction.atomic():
        route = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1)
        with suppress_intent_push():
            route.devices.add(device or mgmt.device)
        _accept_static_route_for_device(route, device or mgmt.device)
    return route


def entries(device, scope, *, unconsumed=None):
    """The key's outbox rows in entry-id order, optionally only the unconsumed ones."""
    from netbox_nso_plugin.models import NSOIntentOutboxEntry

    rows = NSOIntentOutboxEntry.objects.filter(device=device, scope=scope)
    if unconsumed is True:
        rows = rows.filter(consumed_by_push_seq__isnull=True)
    elif unconsumed is False:
        rows = rows.filter(consumed_by_push_seq__isnull=False)
    return list(rows.order_by("id"))


def state_of(device, scope):
    from netbox_nso_plugin.models import NSOIntentOutboxState

    return NSOIntentOutboxState.objects.filter(device=device, scope=scope).first()


def expire_claim(device, scope):
    """Age the key's lease past ``LEASE``, which is what a crashed sender leaves behind."""
    from datetime import timedelta

    from netbox_nso_plugin import drain
    from netbox_nso_plugin.models import NSOIntentOutboxState

    state = state_of(device, scope)
    NSOIntentOutboxState.objects.filter(pk=state.pk).update(
        claimed_at=state.claimed_at - drain.LEASE - timedelta(seconds=1)
    )


def enqueue(device, scope, *, transitions=(), delete_origin=False):
    """Append one entry the way an operator transaction does, without a render."""
    from netbox_nso_plugin import outbox

    with transaction.atomic():
        outbox.enqueue(device.pk, scope, transitions=transitions, delete_origin=delete_origin)


def in_thread(work, timeout=30):
    """Run *work* on its own database connection, and re-raise whatever it raised.

    A pin that needs a second committed transaction needs a second connection: the test's
    own connection is the one under test, and a nested ``atomic()`` on it is a savepoint.
    """
    import threading

    from django.db import connection

    errors: list[BaseException] = []

    def _run():
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 (re-raised on the caller's thread)
            errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(timeout=timeout)
    assert not worker.is_alive(), "the worker transaction never finished"
    if errors:
        raise errors[0]


@contextlib.contextmanager
def without_commit_drain():
    """Leave the key's entries unconsumed by silencing the commit callback.

    Since the O1.19 swap the commit callback IS the drain, so an edit that commits sends
    for real and retires its rows. A pin that has to construct a specific outbox state —
    an unconsumed tail, a stuck claim, a barrier between two operations — cannot let that
    happen mid-fixture. The production trigger has pins of its own
    (``test_intent_outbox_swap``); silencing it here is about arranging the world, never
    about what the drain does with it.
    """
    with patch("netbox_nso_plugin.signals._drain_intent_pushes"):
        yield


def partition(*, executed=(), degraded=(), moot=(), removed=()) -> dict:
    """One adapter answer in §4.4's shape: three id lists plus the rows it removed uncorrelated.

    The three lists must partition the requested set exactly, which is what the plugin
    validates. ``removed`` carries the triples of the ``route_id IS NULL`` rows the request
    removed that no requested id claimed, reported on every request mode.
    """
    return {
        "count": 0,
        "deleted_executed_ids": list(executed),
        "deleted_degraded_ids": list(degraded),
        "deleted_moot_ids": list(moot),
        "removed_uncorrelated": list(removed),
    }


def triple(prefix, next_hop, vrf=""):
    from netbox_nso_plugin.outbox import triple_of

    return triple_of(vrf, prefix, next_hop)


def last_acked(mgmt, route):
    from netbox_nso_plugin.models import NSOStaticRouteState

    row = NSOStaticRouteState.objects.filter(management=mgmt, static_route=route).first()
    return row.last_acked_triple if row else None


@contextlib.contextmanager
def as_per_object(scope):
    """Run the block with *scope* in ``per_object`` marking mode, which O3 makes permanent.

    O1.20 records the ids in both modes and gates only emission on the mode, so a pin over
    the per-object acknowledgement can flip the registry entry and change nothing else.
    """
    from netbox_nso_plugin import delivery

    registry = delivery.delivery_keys()
    original = registry[scope]
    registry[scope] = dataclasses.replace(original, marking_mode=delivery.MARKING_PER_OBJECT)
    try:
        yield
    finally:
        registry[scope] = original


_DEVICE_IN_URL = re.compile(r"/devices/(\d+)/")
#: What a body entry is called on each scope's wire, so one device model serves both.
_BODY_KEYS = ("route_id", "vlan_id")


def _body_members(body) -> set:
    """The objects a full-replace body claims for the device.

    Every mirrored scope wraps its list under one key of its own (``vlans``, ``routes``),
    so the wrapper is walked rather than named: the pin is about membership, not spelling.
    """
    if isinstance(body, dict):
        entries_ = [entry for value in body.values() if isinstance(value, list) for entry in value]
    else:
        entries_ = list(body or [])
    members = set()
    for entry in entries_:
        if not isinstance(entry, dict):
            continue
        for name in _BODY_KEYS:
            if name in entry:
                members.add((name, entry[name]))
    return members


class ReceiptAdapter:
    """The far side of a push, keeping §4.4's receipt per endpoint.

    Stands in at the transport boundary only (the one place a double is warranted), so the
    sequence header, the query flags and the body all travel through the real client. A
    request whose sequence and digest match the stored receipt is a REPLAY: the stored
    response comes back and nothing is applied a second time, which is what makes a lost
    outcome resolvable.

    It also keeps the DEVICE, because the pins over marking are about what the device ends
    up carrying, not about which request went out: a full-replace push RETRACTS what it
    omits when it is marked and DETACHES it when it is not, which is X9's ratified rule.
    """

    def __init__(self, respond=None):
        self.receipts: dict[str, dict] = {}
        self.applied: list[tuple[str, object]] = []
        self.requests: list[dict] = []
        self.replays = 0
        self.fail_with: Exception | None = None
        #: Adapter device ids whose every request fails, for the replayably failing key.
        self.fail_devices: set[int] = set()
        #: Per adapter device id: what the device carries, and what it no longer owns.
        self.on_device: dict[int, set] = {}
        self.detached: dict[int, set] = {}
        self._owned: dict[int, set] = {}
        self._respond = respond or (lambda body: {"count": len(next(iter(body.values()), []) or [])})

    @property
    def sequences(self) -> list[int | None]:
        return [request["push_seq"] for request in self.requests]

    def _apply_to_device(self, url, body, params) -> None:
        """Move the device to what this request authorizes, and nothing further."""
        found = _DEVICE_IN_URL.search(url)
        if found is None:
            return
        device_id = int(found.group(1))
        members = _body_members(body)
        on_device = self.on_device.setdefault(device_id, set())
        detached = self.detached.setdefault(device_id, set())
        dropped = self._owned.setdefault(device_id, set()) - members
        if params.get("store_only") == "true":
            self._owned[device_id] = members
            return
        if params.get("delete_origin") == "true":
            on_device -= dropped  # authorized retraction: the object leaves the device
        else:
            detached |= dropped & on_device  # an unmarked shrink un-owns, it never removes
        on_device |= members
        detached -= members
        self._owned[device_id] = members

    def session(self):
        """A ``spec=requests.Session`` stand-in whose ``request`` runs the admission.

        It models admission and nothing about the transport. In particular ``close()`` does
        NOT abort a request in flight, because the real one does not either: it empties the
        connection pool and leaves a borrowed connection alone. The deadline's abort is
        pinned against a real socket instead (``test_intent_outbox_deadline``).
        """
        session = MagicMock(spec=_REAL_SESSION)
        session.request.side_effect = self._handle
        return session

    def patches(self):
        """The two patches a send needs: the resolved config and the session factory.

        A NEW stand-in per call, because a deadline-bearing send owns its session and closes
        it: one shared object would make the first close the last request of the test.
        """
        return (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", side_effect=self.session),
        )

    def _handle(self, method, url, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        if any(f"/devices/{device_id}/" in url for device_id in self.fail_devices):
            raise ConnectionError(f"the far side refuses {url}")
        headers = kwargs.get("headers") or {}
        raw_seq = headers.get("X-Push-Seq")
        seq = int(raw_seq) if raw_seq is not None else None
        body = kwargs.get("json")
        params = kwargs.get("params") or {}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        self.requests.append({"url": url, "push_seq": seq, "body": body, "params": params})

        receipt = self.receipts.get(url)
        if receipt is not None and seq is not None and seq <= receipt["push_seq"]:
            if seq < receipt["push_seq"]:
                return make_response(409, {"detail": {"code": "stale"}})
            if digest != receipt["digest"]:
                return make_response(409, {"detail": {"code": "sequence_reuse"}})
            self.replays += 1
            return make_response(200, receipt["response"])

        response = self._respond(body)
        self.applied.append((url, body))
        self._apply_to_device(url, body, params)
        if seq is not None:
            self.receipts[url] = {"push_seq": seq, "digest": digest, "response": response}
        return make_response(200, response)

    def place(self, adapter_device_id: int, *members) -> None:
        """Seed what the device already carries, which a push may only narrow when marked."""
        self.on_device.setdefault(adapter_device_id, set()).update(members)
        self._owned.setdefault(adapter_device_id, set()).update(members)
