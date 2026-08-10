# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S7) — the joined e2e, over a real socket in **both** directions.

Pins S7.1 (P5.14, OQ-R3-4) and the plugin half of S7.2 (P1.3).

Every earlier settlement suite starts somewhere inside the plugin: S5.4 hands the notify
endpoint a request the test wrote itself, against a feed the test seeded by hand. This one
starts at the operator's edit and never touches the plugin again:

    operator edits the route  →  the real save signals arm a generation and push
    →  a real ``requests`` PUT over a real loopback socket
    →  the adapter double matches by ``route_id``, stores the replacement, echoes the
       fingerprint, reports an apply for what it stored
    →  the adapter's own ``POST /api/plugins/nso/sync-complete/`` over a real socket into
       the plugin's own HTTP server (``LiveServerTestCase``)
    →  the queued-carrier arbiter  →  a real async RQ queue  →  a real worker
    →  ``run_device_reconcile`` Step 4  →  the ascending feed  →  the verdict

Nothing here calls the consumer, ``run_device_reconcile``, the management command or the
notify endpoint directly, and nothing seeds a feed row: every job in the feed was minted by
the double because a push reached it. That is the point — a consumer whose only caller is a
test looks alive and is dead (DO-NOT-REVISIT #11).

**Topology note, and what this module does NOT prove.** The adapter side is the in-suite
real-socket server OQ-R3-4 decided on, not the adapter binary: running the real adapter
needs a deterministic RESTCONF/NSO boundary to execute an apply at all, and that boundary
was deliberately left to R5's live gate. So everything asserted here is the **plugin's**
half — what it puts on the wire, what it does with what comes back, and that a production
path carries it. The adapter's own half is pinned where the real adapter runs:

- ``contract_tests/test_live_adapter_contract.py`` — the real adapter process on a real
  PostgreSQL, driven by the plugin's real client: that the pk decides a replacement, that
  the edited store really ends holding the edited value, and the ordered feed itself.
- ``nso-adapter/tests/api/test_static_route_pending_clear.py`` — the ``pending_clear``
  carrier, which has no wire form and can only be read off the column.
- ``deployed_key`` advancing on a **successful device write** is reachable from neither,
  because it needs the device. It stays with the R5 live gate.
"""

from __future__ import annotations

import hashlib
import json

import requests
from django.test import LiveServerTestCase

from ._settlement_adapter import FakeAdapter, SettlementStore
from ._settlement_case import _AdapterDoubleMixin, _CarrierMixin, _make_device, _make_mgmt
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

ADAPTER_DEVICE_ID = 70

#: The leaves a static-route entry actually puts on the wire. ``route_id`` and ``generation``
#: are correlation metadata and never reach the device, so neither moves the fingerprint.
_WIRE_FIELDS = ("vrf", "prefix", "next_hop", "metric", "permanent", "tag")


def _fingerprint(entry: dict) -> str:
    """A content hash of the entry — NOT the adapter's own digest, and it does not need to be.

    The plugin never computes a fingerprint: it records the one the PUT echoes and compares
    it with the one the apply result reports. What the double owes it is therefore only the
    property those two comparisons rest on — that the value **moves when the content moves**
    and is stable when it does not. The real digest (the NSO renderer's leaf names, and its
    omission of a false ``permanent``) is the adapter's business and is pinned there.
    """
    body = {key: entry[key] for key in _WIRE_FIELDS if entry.get(key) is not None}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ApplyingStore(SettlementStore):
    """An adapter store that answers a push the way the adapter does, then applies it.

    Only the two behaviors the e2e is about are modelled, and both are observable contract,
    not a re-implementation: an entry is matched **by ``route_id``**, so an identity edit
    updates the row in place instead of reading as a delete plus an insert; and the echoed
    fingerprint is a hash of the wire entry, so it moves when the content moves.
    """

    def __init__(self):
        super().__init__()
        #: adapter device id → {route_id: the stored entry}
        self.intent: dict[int, dict[int, dict]] = {}
        #: entries a push dropped — a deletion the adapter would have to carry
        self.dropped: list[tuple[int, dict]] = []
        #: every push, as ``(device_id, routes)`` — the wire, for asserting on it
        self.pushes: list[tuple[int, list[dict]]] = []
        #: what the apply reports; a test overrides these to drive an uncorrelated result
        self.apply_outcome = "in_sync"
        self.apply_generation_delta = 0
        self.apply_fingerprint: str | None = None
        #: called with the adapter device id — the adapter's own sync-complete callback
        self.notify = None

    def put_static_route_intent(self, device_id: int, routes: list[dict]) -> dict:
        """Replace this device's intent, then report an apply for it and notify.

        The apply and the notification run **before** the response is written, which is the
        only ordering that keeps the test deterministic without a sleep: when the plugin's
        PUT returns, the callback has already been delivered and its carrier queued. The
        plugin's push runs from ``transaction.on_commit``, so its transaction is committed
        and holds no lock the callback could block on.
        """
        self.pushes.append((device_id, routes))
        stored = self.intent.setdefault(device_id, {})
        echoes = []
        seen = set()
        for entry in routes:
            route_id = entry.get("route_id")
            if route_id is None:
                # No provenance: this row cannot be told apart from an unrelated insert, so
                # the replacement fence stays shut for it and it settles nothing.
                continue
            seen.add(route_id)
            row = {**entry, "fingerprint": _fingerprint(entry)}
            stored[route_id] = row
            echoes.append(
                {"route_id": route_id, "generation": entry.get("generation"), "fingerprint": row["fingerprint"]}
            )
        for route_id in [r for r in stored if r not in seen]:
            self.dropped.append((device_id, stored.pop(route_id)))

        # The read-back GET re-serves exactly what this PUT echoed (P5.11(b)).
        self.routes[device_id] = list(echoes)
        self._apply_and_notify(device_id)
        return {"device_id": device_id, "count": len(routes), "routes": echoes}

    def _apply_and_notify(self, device_id: int) -> None:
        rows = list(self.intent.get(device_id, {}).values())
        if not rows:
            return
        results = [
            {
                "route_id": row["route_id"],
                "row_id": index + 1,
                "key": [row.get("vrf") or "", row.get("prefix"), row.get("next_hop")],
                "fingerprint": self.apply_fingerprint or row["fingerprint"],
                "generation": (row.get("generation") or 0) + self.apply_generation_delta,
                "outcome": self.apply_outcome,
                "error": None,
            }
            for index, row in enumerate(rows)
        ]
        self.terminal_job(device_id, results=results)
        if self.notify is not None:
            self.notify(device_id)


class ApplyingAdapter(FakeAdapter):
    """A running adapter double whose store applies and notifies."""

    def __init__(self):
        super().__init__(ApplyingStore())


class TestTheIdentityEditSettlesEndToEnd(
    _CarrierMixin, IntentPushResetMixin, _CascadeFlushMixin, _AdapterDoubleMixin, LiveServerTestCase
):
    """S7.1 — one identity edit, every hop production, both sockets real."""

    adapter_factory = ApplyingAdapter
    serialized_rollback = False
    #: IPv4 explicitly: the default ``localhost`` can resolve to ``::1`` here, and the
    #: loopback guard the adapter double runs under is written against ``127.0.0.1``.
    host = "127.0.0.1"

    def setUp(self):
        super().setUp()
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        self.states = NSOStaticRouteState.objects
        #: what each adapter-side callback answered, as ``(status_code, body)``
        self.notify_responses: list[tuple[int, str]] = []
        self.device = _make_device("e2e")
        self.mgmt = _make_mgmt(self.device, "e2e", ADAPTER_DEVICE_ID)
        self.store = self.adapter.store
        self.store.add_device(
            nso_instance="se-e2e-inst",
            nso_device_name="nso-se-e2e",
            netbox_device_id=self.device.pk,
            device_id=ADAPTER_DEVICE_ID,
        )
        self.store.notify = self._adapter_notifies

        # A route NetBox already knows about; nothing owns it yet.
        self.route = StaticRoute.objects.create(prefix="10.9.0.0/16", next_hop="10.0.0.1", metric=1)

    def _adapter_notifies(self, adapter_device_id: int) -> None:
        """The callback the adapter makes after an apply, issued from the adapter side.

        Same request its ``NetboxClient`` builds — same path, same body, a real NetBox API
        token — but sent by the double, so what is proven is that the plugin's endpoint
        carries it, not that the adapter's client constructs it. The client itself is the
        adapter suite's to pin.

        The response is recorded rather than asserted: this runs on the double's handler
        thread, inside the PUT the plugin is still waiting on, so an assertion here would
        abort that request and report a rejected callback as a transport error.
        """
        netbox_device_id = self.store.devices[adapter_device_id]["netbox_device_id"]
        self._notified = {*getattr(self, "_notified", set()), netbox_device_id}
        response = requests.post(
            f"{self.live_server_url}/api/plugins/nso/sync-complete/",
            json={"netbox_device_id": netbox_device_id},
            headers={**self._bearer(), "X-NSO-Adapter-Import": "1"},
            # The devcontainer exports an HTTP proxy; a loopback callback must not take it.
            proxies={"http": None, "https": None},
            timeout=30,
        )
        self.notify_responses.append((response.status_code, response.text[:400]))

    def _drain(self):
        """Judge the callbacks on the test thread, then run the queued carrier."""
        rejected = [row for row in self.notify_responses if row[0] != 202]
        assert not rejected, f"the plugin's notify endpoint rejected the adapter's callback: {rejected}"
        super()._drain()

    def _bearer(self) -> dict:
        return {"Authorization": self.header["HTTP_AUTHORIZATION"]}

    def _own(self):
        """The operator adds the device to the route — the production accept path."""
        self.route.devices.add(self.device)

    def _state(self):
        return self.states.get(static_route=self.route, management=self.mgmt)

    def test_identity_edit_settles_end_to_end(self):
        self._own()
        self._drain()
        self.assertEqual(self._state().status, "in_sync", "the greenfield accept never settled")

        first_generation = self._state().intent_generation
        self.route.prefix = "10.9.0.0/24"  # A → B: the identity itself moves
        self.route.save()
        self._drain()

        state = self._state()
        self.assertEqual(state.status, "in_sync")
        self.assertGreater(state.intent_generation, first_generation, "the edit did not re-arm the row")

        # The plugin identified the route by pk, which is the only thing that lets the
        # adapter tell a replacement from an unrelated delete plus insert.
        edit_push = self.store.pushes[-1]
        self.assertEqual([entry["route_id"] for entry in edit_push[1]], [self.route.pk])
        self.assertEqual(edit_push[1][0]["prefix"], "10.9.0.0/24")

        # Adapter-side, A→B closed on the one row: no deletion, no second row.
        self.assertEqual(self.store.dropped, [], "the edit read as a delete plus an insert")
        self.assertEqual(list(self.store.intent[ADAPTER_DEVICE_ID]), [self.route.pk])

        # And it settled on the generation and the fingerprint it was waiting for.
        self.assertEqual(state.expected_generation, state.intent_generation)
        self.assertEqual(
            state.expected_fingerprint,
            self.store.intent[ADAPTER_DEVICE_ID][self.route.pk]["fingerprint"],
        )
        self.assertEqual(state.last_apply_error, "")
        self.assertGreater(self._cursor().settle_cursor_seq, 1, "the feed was not walked to its end")

    def test_an_apply_reporting_the_superseded_content_does_not_settle(self):
        """The forbidden outcome of S7.1: reaching ``in_sync`` on an uncorrelated result."""
        self._own()
        self._drain()
        stale = self.store.intent[ADAPTER_DEVICE_ID][self.route.pk]["fingerprint"]

        # The device kept the pre-edit content: the apply reports A's fingerprint for B.
        self.store.apply_fingerprint = stale
        self.route.prefix = "10.9.0.0/24"
        self.route.save()
        self._drain()

        state = self._state()
        self.assertEqual(state.status, "accepted", "a stale fingerprint settled the row green")
        self.assertIn("did not settle", state.last_result_advisory)
        self.assertGreater(self._cursor().settle_cursor_seq, 1, "an unsettled result must still advance")

    def test_an_apply_naming_a_superseded_generation_does_not_settle(self):
        self._own()
        self._drain()

        self.store.apply_generation_delta = -1  # the result names the generation before this one
        self.route.prefix = "10.9.0.0/24"
        self.route.save()
        self._drain()

        self.assertEqual(self._state().status, "accepted")
        self.assertGreater(self._cursor().settle_cursor_seq, 1)

    def test_a_timos_metric_edit_creates_no_pending_clear(self):
        """S7.2, plugin half — an edit to the NED default is still sent, so nothing clears.

        Nokia's default preference 5 used to be suppressed on the wire, which turned an edit
        3 → 5 into an omission. An omitted leaf is a **clear** to the adapter, so the edit
        produced a clear record and a networked retract for a value the device already had.
        This arm owns the **wire**: the plugin still sends the leaf. That the adapter then
        stores 5 and records no clear is asserted against the real adapter, in
        ``contract_tests/test_live_adapter_contract.py`` and in the adapter's own
        ``tests/api/test_static_route_pending_clear.py``.
        """
        self.route.metric = 3
        self.route.save()
        self._own()
        self._drain()

        self.route.metric = 5
        self.route.save()
        self._drain()

        entry = self.store.pushes[-1][1][0]
        self.assertEqual(entry["metric"], 5, "the edit to the NED default was omitted, which is a clear")
        self.assertEqual(self.store.intent[ADAPTER_DEVICE_ID][self.route.pk]["metric"], 5)
        self.assertEqual(self.store.dropped, [], "the metric edit produced a removal")
        self.assertEqual(self._state().status, "in_sync")

    def _cursor(self):
        from netbox_nso_plugin.models import NSODeviceManagement

        return NSODeviceManagement.objects.get(pk=self.mgmt.pk)
