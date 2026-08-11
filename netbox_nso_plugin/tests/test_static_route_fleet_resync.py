# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1396 R3 P1 — the fleet driver that backfills ``route_id`` into the adapter's intent.

The adapter keeps its replacement fence shut while any stored row has a NULL ``route_id``,
and it evaluates the fence on the PRE-mutation row set — so the push that fills the last
NULL is still fence-shut and the fence opens only on the *next* one. One pass over the
fleet is what gets every device there.

Pins P1.5 (drift detection must not gate the backfill), P1.6 (a rejected device is reported
failed and the command exits non-zero), P1.7/P1.8 (store-only, so the reduced-or-changed
snapshot writes no tombstone and enqueues no job).

Every case is a ``TransactionTestCase``: the pass now arms in one committed transaction and
then takes a forced claim, which refuses to run inside a caller's block (§4.2). A wrapping
test transaction would make every device report the refusal as a rejection.
"""

from __future__ import annotations

import contextlib
from io import StringIO
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import TransactionTestCase

from ._outbox_case import without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

COMMAND = "nso_resync_static_route_intent"


@contextlib.contextmanager
def _quiet_fixture():
    """Build a fixture without its own commit callbacks reaching the adapter."""
    with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"), without_commit_drain():
        yield


class TestStaticRouteFleetResync(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        mfg = Manufacturer.objects.create(name="FleetMfg", slug="fleetmfg")
        self.dt = DeviceType.objects.create(manufacturer=mfg, model="FleetDev", slug="fleetdev")
        self.role = DeviceRole.objects.create(name="FleetRole", slug="fleetrole")
        self.site = Site.objects.create(name="FleetSite", slug="fleetsite")

    def _managed_device(self, tag: str, adapter_device_id: int):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        with _quiet_fixture():
            device = Device.objects.create(name=f"fleet-{tag}", device_type=self.dt, role=self.role, site=self.site)
            inst, _ = NSOInstance.objects.get_or_create(
                name="fleet-inst", defaults={"adapter_instance_id": "fleet-inst"}
            )
            mgmt = NSODeviceManagement.objects.create(
                device=device,
                nso_instance=inst,
                nso_device_name=f"nso-fleet-{tag}",
                adapter_device_id=adapter_device_id,
            )
        return device, mgmt

    def _own_route(self, mgmt, prefix: str, next_hop: str, status: str = "accepted"):
        """A pre-P2 owned overlay: owned, pushed, and still on the generation sentinel."""
        from django.utils import timezone
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import suppress_intent_push

        with _quiet_fixture(), transaction.atomic():
            sr = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1)
            with suppress_intent_push():
                sr.devices.add(mgmt.device)
            return NSOStaticRouteState.objects.create(
                management=mgmt,
                static_route=sr,
                status=status,
                nso_prefix=prefix,
                nso_next_hop=next_hop,
                accepted_at=timezone.now(),
            )

    def test_backfills_a_device_with_no_detected_drift(self):
        """P1.5 — ``resync_intent``'s default ``keys`` re-syncs only scopes that already LOOK
        drifted, and a device whose counts agree looks clean while every stored row still has a
        NULL ``route_id``. The driver must not be gated on drift detection."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet

        _, mgmt = self._managed_device("nodrift", 8001)
        state = self._own_route(mgmt, "10.60.0.0/16", "10.0.0.60")

        with patch("netbox_nso_plugin.intent_drift.compute_intent_drift", return_value=[]) as drift:
            with patch(
                "netbox_nso_plugin.adapter_client.put_static_route_intent",
                return_value={"device_id": 8001, "count": 1, "routes": []},
            ) as put:
                results = resync_static_route_intent_fleet()

        drift.assert_not_called()
        put.assert_called_once()
        assert put.call_args.args[1][0]["route_id"] == state.static_route.pk
        assert [r["ok"] for r in results] == [True]

    def test_pushes_store_only_and_carries_every_route_id(self):
        """P1.7/P1.8 — store-only, so the adapter repairs its mirror without writing a tombstone
        or enqueuing a job; a clear detected during the resync parks the row instead of being
        authorized."""
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet

        _, mgmt = self._managed_device("storeonly", 8002)
        first = self._own_route(mgmt, "10.61.0.0/16", "10.0.0.61")
        second = self._own_route(mgmt, "10.61.1.0/24", "10.0.0.62")
        seen = {}

        def _record(adapter_device_id, routes):
            seen["store_only"] = adapter_client._store_only_push.get()
            seen["delete_origin"] = adapter_client._delete_origin_push.get()
            seen["routes"] = routes
            return {"device_id": adapter_device_id, "count": len(routes), "routes": []}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_record):
            resync_static_route_intent_fleet()

        assert seen["store_only"] is True
        assert seen["delete_origin"] is False
        assert {r["route_id"] for r in seen["routes"]} == {
            first.static_route.pk,
            second.static_route.pk,
        }

    def test_a_rejected_device_is_reported_failed(self):
        """P1.6 — the claim records the rejection and answers ``None``; with ``force=True`` that
        ``None`` is unambiguously a failure, and reporting it done would leave the operator
        believing a device is backfilled when its fence is still shut."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet

        _, good = self._managed_device("good", 8003)
        _, bad = self._managed_device("bad", 8004)
        self._own_route(good, "10.62.0.0/16", "10.0.0.63")
        self._own_route(bad, "10.63.0.0/16", "10.0.0.64")

        def _reject_one(adapter_device_id, routes):
            if adapter_device_id == 8004:
                raise RuntimeError("422 duplicate_triple")
            return {"device_id": adapter_device_id, "count": len(routes), "routes": []}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_reject_one):
            results = resync_static_route_intent_fleet()

        by_device = {r["device_id"]: r for r in results}
        assert by_device[good.device_id]["ok"] is True
        assert by_device[good.device_id]["count"] == 1
        assert by_device[bad.device_id]["ok"] is False
        assert by_device[bad.device_id]["count"] is None

    def test_command_exits_non_zero_when_a_device_is_rejected(self):
        """P1.6 — a partial fleet pass is a failure the operator has to see."""
        _, good = self._managed_device("cmdgood", 8005)
        _, bad = self._managed_device("cmdbad", 8006)
        self._own_route(good, "10.64.0.0/16", "10.0.0.65")
        self._own_route(bad, "10.65.0.0/16", "10.0.0.66")

        def _reject_one(adapter_device_id, routes):
            if adapter_device_id == 8006:
                return None  # a response the client could not read as a stored count
            return {"device_id": adapter_device_id, "count": len(routes), "routes": []}

        out = StringIO()
        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_reject_one):
            with self.assertRaises(CommandError) as raised:
                call_command(COMMAND, stdout=out, stderr=StringIO())

        assert "fleet-cmdbad" in str(raised.exception)
        assert "fleet-cmdbad" in out.getvalue()
        assert "fleet-cmdgood" in out.getvalue()  # the device that did land is still reported stored

    def test_command_reports_success_for_a_clean_fleet(self):
        _, mgmt = self._managed_device("cmdok", 8007)
        self._own_route(mgmt, "10.66.0.0/16", "10.0.0.67")

        out = StringIO()
        with patch(
            "netbox_nso_plugin.adapter_client.put_static_route_intent",
            side_effect=lambda adapter_device_id, routes: {
                "device_id": adapter_device_id,
                "count": len(routes),
                "routes": [],
            },
        ):
            call_command(COMMAND, stdout=out, stderr=StringIO())

        output = out.getvalue()
        assert "fleet-cmdok" in output
        assert "1 route(s) stored" in output
        assert "Re-synced 1 device(s)" in output

    def test_a_device_that_raises_does_not_abort_the_rest_of_the_pass(self):
        """The claim records *adapter* failures and answers ``None``, but the echo recorder
        runs on the send's own thread. An answer it cannot read would otherwise raise out of
        the loop, leaving every later device unattempted and unreported."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet

        _, first = self._managed_device("raiser", 8008)
        _, second = self._managed_device("after", 8009)
        self._own_route(first, "10.67.0.0/16", "10.0.0.68")
        self._own_route(second, "10.68.0.0/16", "10.0.0.69")

        def _malformed_echo(adapter_device_id, routes):
            if adapter_device_id == 8008:
                return {"device_id": adapter_device_id, "count": len(routes), "routes": 1}  # not a list
            return {"device_id": adapter_device_id, "count": len(routes), "routes": []}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_malformed_echo):
            results = resync_static_route_intent_fleet()

        by_device = {r["device_id"]: r for r in results}
        assert by_device[first.device_id]["ok"] is False
        assert by_device[first.device_id]["count"] is None
        assert by_device[second.device_id]["ok"] is True  # the pass carried on

    def test_only_a_real_route_count_acknowledges_a_push(self):
        """The count IS the acknowledgement, so an answer that carries no honest one must arm
        nothing: a device reported stored holds a generation the adapter has never seen. ``True``
        is an ``int`` in Python and no push stores a negative number of rows. An honest zero still
        acknowledges — a device with nothing to push really does store zero routes."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet

        _, boolean = self._managed_device("boolcount", 8021)
        _, text = self._managed_device("textcount", 8022)
        _, negative = self._managed_device("negcount", 8023)
        _, no_routes = self._managed_device("zerocount", 8024)  # owns no route, so zero is the truth
        for mgmt, prefix in ((boolean, "10.70.0.0/16"), (text, "10.71.0.0/16"), (negative, "10.72.0.0/16")):
            self._own_route(mgmt, prefix, "10.0.0.71")

        answers = {8021: True, 8022: "1", 8023: -1, 8024: 0}

        def _answer(adapter_device_id, routes):
            return {"device_id": adapter_device_id, "count": answers[adapter_device_id], "routes": []}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_answer):
            results = resync_static_route_intent_fleet()

        by_device = {r["device_id"]: r for r in results}
        for mgmt in (boolean, text, negative):
            assert by_device[mgmt.device_id]["ok"] is False, by_device[mgmt.device_id]
            assert by_device[mgmt.device_id]["count"] is None
            assert by_device[mgmt.device_id]["armed"] == 0, "a refused push kept its arming"
        assert by_device[no_routes.device_id]["ok"] is True
        assert by_device[no_routes.device_id]["count"] == 0

    def test_command_fails_when_a_requested_device_id_matched_nothing(self):
        """``--device`` filters the queryset, so a mistyped id simply yields no result row —
        and an unrepaired fleet must never print a clean ``Re-synced 0 device(s)``."""
        device, mgmt = self._managed_device("cmdsel", 8010)
        self._own_route(mgmt, "10.69.0.0/16", "10.0.0.70")

        out = StringIO()
        with patch(
            "netbox_nso_plugin.adapter_client.put_static_route_intent",
            side_effect=lambda adapter_device_id, routes: {
                "device_id": adapter_device_id,
                "count": len(routes),
                "routes": [],
            },
        ):
            with self.assertRaises(CommandError) as raised:
                call_command(COMMAND, "--device", str(device.pk), "--device", "424242", stdout=out, stderr=StringIO())

        assert "424242" in str(raised.exception)
        assert "fleet-cmdsel" in out.getvalue()  # the id that did match was still re-synced

    def test_the_pass_backfills_generations_not_only_route_ids(self):
        """S6.1 (#1502) — a pre-P2 owned row keeps ``intent_generation = 0`` through the pass.

        Zero is the unallocated sentinel: it goes on the wire as null, the adapter adopts
        nothing, and every result the row ever produces is non-settling. A pass that fills
        ``route_id`` and reports success leaves exactly that state behind, fleet-wide.
        """
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet
        from netbox_nso_plugin.intent_generation import UNALLOCATED

        _, first = self._managed_device("genone", 8101)
        _, second = self._managed_device("gentwo", 8102)
        _, rejected = self._managed_device("genbad", 8103)
        rows = [
            self._own_route(first, "10.70.0.0/16", "10.0.0.71"),
            self._own_route(second, "10.71.0.0/16", "10.0.0.72"),
            self._own_route(rejected, "10.72.0.0/16", "10.0.0.73"),
        ]
        assert {row.intent_generation for row in rows} == {UNALLOCATED}
        sent: dict[int, list] = {}

        def _ack_but_one(adapter_device_id, routes):
            sent[adapter_device_id] = routes
            if adapter_device_id == 8103:
                return None  # the adapter refused this device's intent
            return {"device_id": adapter_device_id, "count": len(routes), "routes": []}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_ack_but_one):
            results = resync_static_route_intent_fleet()

        by_device = {r["device_id"]: r for r in results}
        for mgmt, row in ((first, rows[0]), (second, rows[1])):
            row.refresh_from_db()
            assert by_device[mgmt.device_id]["armed"] == 1
            assert row.intent_generation > UNALLOCATED, "the row is still on the sentinel: no result can settle it"
            assert row.generation_started_at is not None, "an armed row with no clock cannot be timed out either"
            assert sent[mgmt.adapter_device_id][0]["generation"] == row.intent_generation, (
                "the generation was armed in NetBox but never put on the wire"
            )
        rows[2].refresh_from_db()
        assert by_device[rejected.device_id]["ok"] is False
        assert by_device[rejected.device_id]["armed"] == 0
        # armed == 0 is the rolled-back arming, not "nothing needed arming"; only the second
        # key tells the two apart, and a partial pass has to be visible without a re-run.
        assert by_device[rejected.device_id]["armed_rolled_back"] == 1
        assert by_device[first.device_id]["armed_rolled_back"] == 0
        assert rows[2].intent_generation == UNALLOCATED, (
            "the arming outlived the push the adapter refused: nothing is holding that generation, "
            "and no later pass would find a sentinel row to retry"
        )

        # The operator has to see the partial pass, and a re-run must arm nothing twice.
        out = StringIO()
        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_ack_but_one):
            with self.assertRaises(CommandError) as raised:
                call_command(COMMAND, stdout=out, stderr=StringIO())
        assert "fleet-genbad" in str(raised.exception)
        assert "0 generation(s) armed" in out.getvalue()
        assert "1 generation(s) rolled back" in out.getvalue()

        with patch(
            "netbox_nso_plugin.adapter_client.put_static_route_intent",
            side_effect=lambda adapter_device_id, routes: {
                "device_id": adapter_device_id,
                "count": len(routes),
                "routes": [],
            },
        ):
            second_pass = {r["device_id"]: r for r in resync_static_route_intent_fleet()}

        assert second_pass[first.device_id]["armed"] == 0, "an armed row was re-armed, so its expectation went stale"
        assert second_pass[second.device_id]["armed"] == 0
        assert second_pass[rejected.device_id]["armed"] == 1, "the rejected device was never retried"

    def test_a_route_the_push_cannot_carry_is_never_armed(self):
        """Codex S6 P2 — the arm set must equal the set the pusher serializes.

        An interface-only next hop has no place in the static-route snapshot, so the push
        drops that row. Arming it anyway mints a generation the adapter never receives:
        nothing can correlate it, no later run finds a sentinel row to retry it with, and
        an Apply is then free to promote a row only the backstop can end.
        """
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet
        from netbox_nso_plugin.intent_generation import UNALLOCATED
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import suppress_intent_push

        _, mgmt = self._managed_device("ifacenh", 8105)
        carried = self._own_route(mgmt, "10.77.0.0/16", "10.0.0.78")
        with _quiet_fixture(), transaction.atomic():
            iface_route = StaticRoute.objects.create(
                prefix="10.78.0.0/16", next_hop=None, interface_next_hop="Ethernet1/1", metric=1
            )
            with suppress_intent_push():
                iface_route.devices.add(mgmt.device)
            skipped = NSOStaticRouteState.objects.create(
                management=mgmt,
                static_route=iface_route,
                status="accepted",
                nso_prefix="10.78.0.0/16",
            )
        sent = {}

        def _ack(adapter_device_id, routes):
            sent["routes"] = routes
            return {"device_id": adapter_device_id, "count": len(routes), "routes": []}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_ack):
            results = resync_static_route_intent_fleet()

        carried.refresh_from_db()
        skipped.refresh_from_db()
        assert [r["route_id"] for r in sent["routes"]] == [carried.static_route_id]
        assert carried.intent_generation > UNALLOCATED
        assert skipped.intent_generation == UNALLOCATED, (
            "a row the push never carries was armed: the adapter has never seen that generation, "
            "so no result can name it and no later pass would re-arm it"
        )
        assert skipped.generation_started_at is None
        assert results[0]["armed"] == 1

    def test_a_rejected_push_keeps_its_reason_on_the_device(self):
        """Codex S6 P2 — the rollback undoes the arming, and must not undo the diagnosis.

        The push persists the adapter's rejection on the management row, which is what the
        device tab shows and what the operator acts on. The arming is undone by an explicit
        inverse now, so the two are independent, and this is what keeps them so.
        """
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet

        _, mgmt = self._managed_device("keepreason", 8106)
        row = self._own_route(mgmt, "10.79.0.0/16", "10.0.0.80")

        def _reject(adapter_device_id, routes):
            raise AdapterError(
                "422 duplicate_triple", code="duplicate_triple", detail={"route_ids": [row.static_route_id]}
            )

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_reject):
            results = resync_static_route_intent_fleet()

        mgmt.refresh_from_db()
        row.refresh_from_db()
        assert results[0]["ok"] is False
        assert row.intent_generation == 0, "the arming outlived the push the adapter refused"
        recorded = (mgmt.intent_push_errors or {}).get("static_route") or {}
        assert recorded.get("code") == "duplicate_triple", (
            "the rollback discarded the adapter's rejection: the device tab can only say "
            f"'not acknowledged' — {mgmt.intent_push_errors!r}"
        )
        assert (mgmt.intent_push_attempts or {}).get("static_route") == 1, "the attempt mark was rewound"

    def test_a_stale_unacknowledged_claim_does_not_answer_the_resync(self):
        """Codex O1 F2 (§4.2) — the forced call forms its own claim, with its own body.

        A takeover replays the operation the adapter never answered, at ITS body and ITS
        mode. Answering the resync with that replay reports a device backfilled while the
        generations this pass armed were never on the wire and no result can settle them.
        """
        from netbox_nso_plugin import adapter_client, drain
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet
        from netbox_nso_plugin.intent_generation import UNALLOCATED

        _, mgmt = self._managed_device("staleclaim", 8108)
        row = self._own_route(mgmt, "10.81.0.0/16", "10.0.0.82")
        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=ConnectionError("down")):
            assert drain.drain_key(mgmt.device_id, "static_route") == drain.FAILED
        sent: list[dict] = []

        def _record(adapter_device_id, routes):
            sent.append({"routes": routes, "store_only": adapter_client._store_only_push.get()})
            return {"device_id": adapter_device_id, "count": len(routes), "routes": []}

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_record):
            results = resync_static_route_intent_fleet()

        row.refresh_from_db()
        assert results[0]["ok"] is True
        assert row.intent_generation > UNALLOCATED
        assert sent[-1]["store_only"] is True, "the resync repairs the mirror, so it may never enqueue a job"
        assert sent[-1]["routes"][0]["generation"] == row.intent_generation, (
            "the resync was answered by the replay of the stale claim, so the armed generation never went out"
        )

    def test_a_concurrent_edit_during_the_push_survives_the_restore(self):
        """Codex O1 F4 — the restore is a compare-and-set on the generation this pass armed.

        An operator re-accepting the row while the resync push is on the wire gives it a new
        generation. An unconditional restore puts the sentinel back over that edit, so intent
        the operator has just stated reads as never-armed and no result can ever settle it.
        """
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet
        from netbox_nso_plugin.intent_generation import UNALLOCATED
        from netbox_nso_plugin.models import NSOStaticRouteState

        _, mgmt = self._managed_device("concurrent", 8107)
        row = self._own_route(mgmt, "10.80.0.0/16", "10.0.0.81")
        edited: dict = {}

        def _edit_then_refuse(adapter_device_id, routes):
            """Re-accept the row on the sender's own connection, then answer unacknowledged."""
            from netbox_nso_plugin.signals import (
                _STATIC_ROUTE_ARMED_FIELDS,
                _arm_static_route_generation,
                suppress_intent_push,
            )

            live = NSOStaticRouteState.objects.get(pk=row.pk)
            with suppress_intent_push():
                _arm_static_route_generation(live)
                live.save(update_fields=list(_STATIC_ROUTE_ARMED_FIELDS))
            edited["generation"] = live.intent_generation

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=_edit_then_refuse):
            results = resync_static_route_intent_fleet()

        row.refresh_from_db()
        assert results[0]["ok"] is False
        assert edited["generation"] > UNALLOCATED
        assert row.intent_generation == edited["generation"], "the restore clobbered a concurrent operator edit"
        assert results[0]["armed_rolled_back"] == 0, "a row that moved is left alone, and reported as left alone"

    def test_backfill_demotes_deploying_and_leaves_other_statuses(self):
        """S6.2 — a row already ``deploying`` cannot wait on a generation it has just replaced."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet
        from netbox_nso_plugin.intent_generation import UNALLOCATED

        _, mgmt = self._managed_device("demote", 8104)
        deploying = self._own_route(mgmt, "10.73.0.0/16", "10.0.0.74", status="deploying")
        in_sync = self._own_route(mgmt, "10.74.0.0/16", "10.0.0.75", status="in_sync")
        failed = self._own_route(mgmt, "10.75.0.0/16", "10.0.0.76", status="apply_failed")
        accepted = self._own_route(mgmt, "10.76.0.0/16", "10.0.0.77")
        owned_since = deploying.accepted_at

        with patch(
            "netbox_nso_plugin.adapter_client.put_static_route_intent",
            side_effect=lambda adapter_device_id, routes: {
                "device_id": adapter_device_id,
                "count": len(routes),
                "routes": [],
            },
        ):
            resync_static_route_intent_fleet()

        for row in (deploying, in_sync, failed, accepted):
            row.refresh_from_db()
            assert row.intent_generation > UNALLOCATED
        assert deploying.status == "accepted", (
            "the row is left deploying under a generation no in-flight result can name — "
            "stranded until the backstop calls it failed"
        )
        assert deploying.accepted_at == owned_since, "the demotion re-dated first ownership"
        assert in_sync.status == "in_sync", "a settled row's badge flickered on a pass that changed no content"
        assert failed.status == "apply_failed"
        assert accepted.status == "accepted"

    def test_unlinked_devices_are_skipped(self):
        """A management row with no ``adapter_device_id`` has nothing to push to."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        with _quiet_fixture():
            device = Device.objects.create(name="fleet-unlinked", device_type=self.dt, role=self.role, site=self.site)
            inst, _ = NSOInstance.objects.get_or_create(
                name="fleet-inst", defaults={"adapter_instance_id": "fleet-inst"}
            )
            NSODeviceManagement.objects.create(
                device=device, nso_instance=inst, nso_device_name="nso-fleet-unlinked", adapter_device_id=None
            )

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent") as put:
            results = resync_static_route_intent_fleet()

        put.assert_not_called()
        assert results == []
