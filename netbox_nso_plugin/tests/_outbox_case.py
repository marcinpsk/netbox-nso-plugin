# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — shared fixtures for the claim protocol and the drain pass.

The claim renders inside its own transaction and sends outside it, so its pins cross
transaction boundaries and run as ``TransactionTestCase``. That means no wrapping test
transaction and real commits, which is also what lets :class:`ReceiptAdapter` stand in for
the far side at the transport boundary: it keeps one receipt per endpoint, so a replay of
an already-accepted sequence returns the stored response and applies nothing, exactly as
§4.4's admission table requires.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

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
    """A managed device, with the fixture's own pushes silenced."""
    with patch(PUT_STATIC), patch(PUT_VLAN):
        device = make_device(tag, index)
        return device, make_mgmt(device, tag, adapter_device_id)


def own_vlan(mgmt, vid: int, tag: str):
    """One owned VLAN overlay, which is what a VLAN render puts on the wire."""
    from ipam.models import VLAN

    from netbox_nso_plugin.models import NSOVLANState

    with patch(PUT_VLAN):
        vlan = VLAN.objects.create(vid=vid, name=f"cl-{tag}-v{vid}")
        return NSOVLANState.objects.create(management=mgmt, vlan=vlan, status="accepted")


def own_route(mgmt, prefix: str, next_hop: str, *, device=None):
    """A route assigned to the device and owned by it, as the accept path leaves it."""
    from netbox_routing.models import StaticRoute

    from netbox_nso_plugin.signals import _accept_static_route_for_device, suppress_intent_push

    route = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1)
    with suppress_intent_push():
        route.devices.add(device or mgmt.device)
    with patch(PUT_STATIC):
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

    outbox.enqueue(device.pk, scope, transitions=transitions, delete_origin=delete_origin)


class ReceiptAdapter:
    """The far side of a push, keeping §4.4's receipt per endpoint.

    Stands in at the transport boundary only — the one place a double is warranted — so the
    sequence header, the query flags and the body all travel through the real client. A
    request whose sequence and digest match the stored receipt is a REPLAY: the stored
    response comes back and nothing is applied a second time, which is what makes a lost
    outcome resolvable.
    """

    def __init__(self, respond=None):
        self.receipts: dict[str, dict] = {}
        self.applied: list[tuple[str, object]] = []
        self.requests: list[dict] = []
        self.replays = 0
        self.fail_with: Exception | None = None
        self._respond = respond or (lambda body: {"count": len(next(iter(body.values()), []) or [])})

    @property
    def sequences(self) -> list[int | None]:
        return [request["push_seq"] for request in self.requests]

    def session(self):
        """A ``spec=requests.Session`` stand-in whose ``request`` runs the admission."""
        session = MagicMock(spec=_REAL_SESSION)
        session.request.side_effect = self._handle
        return session

    def patches(self):
        """The two patches a send needs: the resolved config and the session factory."""
        return (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=self.session()),
        )

    def _handle(self, method, url, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        headers = kwargs.get("headers") or {}
        raw_seq = headers.get("X-Push-Seq")
        seq = int(raw_seq) if raw_seq is not None else None
        body = kwargs.get("json")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        self.requests.append({"url": url, "push_seq": seq, "body": body, "params": kwargs.get("params") or {}})

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
        if seq is not None:
            self.receipts[url] = {"push_seq": seq, "digest": digest, "response": response}
        return make_response(200, response)
