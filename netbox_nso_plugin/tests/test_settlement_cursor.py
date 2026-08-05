# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S4) — the cursor epoch, the durable stall bound, the consumer.

Pins S4.1 through S4.6. Every test drives the consumer over a **real HTTP** adapter double
on a loopback socket, so the parameters it builds, the header it reads and the errors it
maps are its own and not a canned return value. S4.3 goes further and drives two real
``manage.py`` subprocesses against the committed test database, because a durable counter
and a module global are indistinguishable from inside one process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from ._settlement_adapter import FakeAdapter, LoopbackOnlySession
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

PUT = "netbox_nso_plugin.adapter_client.put_static_route_intent"
FINGERPRINT = "fp-a"


def _make_device(tag: str):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"Se{tag}Mfg", slug=f"se{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Se{tag}Dev", slug=f"se{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Se{tag}Role", slug=f"se{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Se{tag}Site", slug=f"se{tag}site")
    return Device.objects.create(name=f"se-{tag}-rtr", device_type=dt, role=role, site=site)


def _make_mgmt(device, tag: str, adapter_device_id: int | None):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    inst, _ = NSOInstance.objects.get_or_create(
        name=f"se-{tag}-inst", defaults={"adapter_instance_id": f"se-{tag}-inst"}
    )
    with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"):
        return NSODeviceManagement.objects.create(
            device=device,
            nso_instance=inst,
            nso_device_name=f"nso-se-{tag}",
            adapter_device_id=adapter_device_id,
        )


def _route(prefix, next_hop, *, devices=()):
    from netbox_routing.models import StaticRoute

    from netbox_nso_plugin.signals import suppress_intent_push

    sr = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1)
    if devices:
        with suppress_intent_push():
            sr.devices.add(*devices)
    return sr


def _own(sr, mgmt, *, generation, expected=True, status="deploying"):
    """An owned overlay at *generation*, with or without a recorded expectation."""
    from netbox_nso_plugin.models import NSOStaticRouteState

    with patch(PUT):
        return NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=sr,
            status=status,
            nso_prefix=str(sr.prefix or ""),
            nso_next_hop=str(sr.next_hop or ""),
            accepted_at=timezone.now(),
            intent_generation=generation,
            generation_started_at=timezone.now(),
            expected_generation=generation if expected else None,
            expected_fingerprint=FINGERPRINT if expected else "",
        )


def _result(route_id, generation, *, outcome="in_sync"):
    return {
        "route_id": route_id,
        "row_id": 1,
        "key": ["", "10.0.0.0/8", "10.0.0.1"],
        "fingerprint": FINGERPRINT,
        "generation": generation,
        "outcome": outcome,
        "error": None,
    }


class _SettlementCase(IntentPushResetMixin, _CascadeFlushMixin, TransactionTestCase):
    """Point the plugin at a live adapter double for the duration of one test."""

    serialized_rollback = False

    def setUp(self):
        super().setUp()
        from netbox_nso_plugin import adapter_client

        blocked = patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession)
        blocked.start()
        self.addCleanup(blocked.stop)
        adapter_client.reset_session()
        self.addCleanup(adapter_client.reset_session)

        self.adapter = FakeAdapter()
        self.addCleanup(self.adapter.stop)
        self.addCleanup(self._reset_adapter_config)
        self._point_at(self.adapter)

    def _point_at(self, adapter):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import AdapterConnection

        conn = AdapterConnection.objects.first()
        if conn is None:
            AdapterConnection.objects.create(url=adapter.url, enabled=True, timeout_seconds=10)
        else:
            AdapterConnection.objects.filter(pk=conn.pk).update(url=adapter.url, enabled=True)
        adapter_client.reset_config_cache()

    def _reset_adapter_config(self):
        from netbox_nso_plugin import adapter_client

        adapter_client.reset_config_cache()

    def _cursor(self, mgmt):
        from netbox_nso_plugin.models import NSODeviceManagement

        return NSODeviceManagement.objects.get(pk=mgmt.pk)


class TestConsumptionIsOnceAndMonotone(_SettlementCase):
    """S4.1 (P5.6) — one settlement, one advance, under one lock and one transaction."""

    def test_consumption_is_result_once_and_monotone(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("once")
        mgmt = _make_mgmt(device, "once", 10)
        sr = _route("10.1.0.0/16", "10.1.0.1", devices=[device])
        _own(sr, mgmt, generation=41)
        self.adapter.store.terminal_job(10, results=[_result(sr.pk, 41)])

        first = consume_static_route_settlements(mgmt)
        assert first.consumed == 1
        assert first.cursor == 1
        assert self._cursor(mgmt).settle_cursor_seq == 1

        second = consume_static_route_settlements(mgmt)
        assert second.consumed == 0, "the same job was consumed twice"
        assert second.cursor == 1, "the cursor moved without a new settlement"
        # The second pass asked the adapter for what comes AFTER the cursor, so the already
        # consumed job was never served again.
        assert self.adapter.store.feed_requests[-1][1] == 1

    def test_racing_consumers_advance_the_cursor_once(self):
        """Two consumers on one device serialize on the management row, never double-advance."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("race")
        mgmt = _make_mgmt(device, "race", 20)
        sr = _route("10.2.0.0/16", "10.2.0.1", devices=[device])
        _own(sr, mgmt, generation=42)
        self.adapter.store.terminal_job(20, results=[_result(sr.pk, 42)])

        results, errors = [], []
        start = threading.Barrier(2)

        def run():
            start.wait(timeout=10)
            try:
                results.append(consume_static_route_settlements(mgmt.pk))
            except Exception as exc:  # noqa: BLE001 — reported through the assertion below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert sorted(r.consumed for r in results) == [0, 1], "the one settlement was consumed twice"
        assert self._cursor(mgmt).settle_cursor_seq == 1

    def test_the_cursor_advance_does_not_refire_the_management_row_handlers(self):
        """Bookkeeping is not an intent change: a ``save()`` here would re-push the scope."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("fields")
        mgmt = _make_mgmt(device, "fields", 30)
        sr = _route("10.3.0.0/16", "10.3.0.1", devices=[device])
        _own(sr, mgmt, generation=43)
        store = self.adapter.store
        store.terminal_job(30, results=[_result(sr.pk, 43)])
        store.requests.clear()

        consume_static_route_settlements(mgmt)

        assert self._cursor(mgmt).settle_cursor_seq == 1
        pushed = [
            req
            for req in store.requests
            if req[1].endswith(("/scope", "/sync-notify")) or req[1].endswith("/api/v1/devices")
        ]
        assert pushed == [], f"the cursor advance re-fired the row's adapter push: {pushed}"

    def test_the_consumer_polls_the_locked_rows_device_id_not_the_callers(self):
        """The epoch comes off the row the consumer locks, never off the object handed to it."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("locked")
        mgmt = _make_mgmt(device, "locked", 31)
        sr = _route("10.31.0.0/16", "10.31.0.1", devices=[device])
        _own(sr, mgmt, generation=44)
        self.adapter.store.terminal_job(31, results=[_result(sr.pk, 44)])

        # A caller whose in-memory row is stale — exactly what the link repair leaves behind.
        mgmt.adapter_device_id = 999

        result = consume_static_route_settlements(mgmt)

        assert result.adapter_device_id == 31
        polled = {req[0] for req in self.adapter.store.feed_requests}
        assert polled == {31}, f"the stale caller id was polled: {polled}"
        assert result.consumed == 1


class TestCursorEpoch(_SettlementCase):
    """S4.2 / S4.2b / S4.2c — the epoch is compared on read, and neither half is cached."""

    def test_cursor_resets_on_incarnation_change(self):
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("inc")
        mgmt = _make_mgmt(device, "inc", 40)
        sr = _route("10.4.0.0/16", "10.4.0.1", devices=[device])
        _own(sr, mgmt, generation=44)
        self.adapter.store.terminal_job(40, results=[_result(sr.pk, 44)])

        # A cursor recorded against a store that is gone: everything below it would be
        # skipped forever if the epoch were not compared.
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
            settle_cursor_seq=100,
            settle_cursor_incarnation="a-dead-store",
            settle_cursor_device_id=40,
        )

        result = consume_static_route_settlements(mgmt)

        assert result.epoch_reset is True
        assert result.consumed == 1, "the settlement below the stale cursor was skipped"
        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1
        assert row.settle_cursor_incarnation == self.adapter.store.incarnation

    def test_a_recreated_management_row_starts_from_the_beginning(self):
        """P29 — a delete/recreate leaves no epoch, so the feed is walked from the start."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("recreate")
        mgmt = _make_mgmt(device, "recreate", 41)
        sr = _route("10.5.0.0/16", "10.5.0.1", devices=[device])
        _own(sr, mgmt, generation=45)
        self.adapter.store.terminal_job(41, results=[_result(sr.pk, 45)])
        consume_static_route_settlements(mgmt)
        assert self._cursor(mgmt).settle_cursor_seq == 1

        mgmt.delete()
        fresh = _make_mgmt(device, "recreate", 41)
        sr2 = _route("10.5.1.0/24", "10.5.1.1", devices=[device])
        _own(sr2, fresh, generation=46)

        result = consume_static_route_settlements(fresh)

        assert result.epoch_reset is True
        assert result.consumed == 1, "the replayed job must be walked, not skipped"
        assert self._cursor(fresh).settle_cursor_seq == 1

    def test_a_device_remap_within_one_incarnation_resets_the_cursor(self):
        """S4.2b — the same store, a different adapter device, whose counter restarts at 1."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        for arm in ("moved", "reused", "reonboard"):
            with self.subTest(arm=arm):
                store = self.adapter.store
                device = _make_device(f"remap{arm}")
                mgmt = _make_mgmt(device, f"remap{arm}", 100)
                identity = mgmt.nso_instance.adapter_instance_id
                sr = _route(f"10.6.{len(arm)}.0/24", "10.6.0.1", devices=[device])
                _own(sr, mgmt, generation=50 + len(arm))

                # A cursor already well past this device's history, on adapter device 100.
                NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
                    settle_cursor_seq=100,
                    settle_cursor_incarnation=store.incarnation,
                    settle_cursor_device_id=100,
                    settle_stall_seq=100,
                    settle_stall_attempts=3,
                    settle_stall_first_seen_at=timezone.now(),
                )

                if arm == "moved":
                    # Our device is present under a different id.
                    store.add_device(
                        nso_instance=identity,
                        nso_device_name=mgmt.nso_device_name,
                        netbox_device_id=device.pk,
                    )
                elif arm == "reused":
                    # Our id belongs to somebody else; our device is present under another id.
                    store.add_device(
                        nso_instance=identity,
                        nso_device_name="somebody-else",
                        netbox_device_id=device.pk + 9000,
                        device_id=100,
                    )
                    store.add_device(
                        nso_instance=identity,
                        nso_device_name=mgmt.nso_device_name,
                        netbox_device_id=device.pk,
                    )
                else:
                    # Our device is gone: the re-save's not-found scope push re-onboards it.
                    pass

                reconcile_device_links([NSODeviceManagement.objects.get(pk=mgmt.pk)])

                row = NSODeviceManagement.objects.get(pk=mgmt.pk)
                assert row.adapter_device_id not in (None, 100), f"{arm}: the link was not repaired"
                new_id = row.adapter_device_id
                store.terminal_job(new_id, results=[_result(sr.pk, 50 + len(arm))])

                result = consume_static_route_settlements(mgmt)

                assert result.epoch_reset is True, f"{arm}: the remap did not reset the epoch"
                assert result.consumed == 1, f"{arm}: sequence 1 on the new device was skipped"
                after = NSODeviceManagement.objects.get(pk=mgmt.pk)
                assert after.settle_cursor_seq == 1
                assert after.settle_cursor_device_id == new_id
                assert after.settle_stall_seq is None, f"{arm}: the stall triple survived the reset"
                assert after.settle_stall_attempts == 0

    def test_a_store_rebuild_is_caught_from_the_feed_not_the_cached_mirror(self):
        """S4.2c — a new adapter lifespan under the same device id and the same cached mirrors."""
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("rebuild")
        mgmt = _make_mgmt(device, "rebuild", 60)
        sr = _route("10.7.0.0/16", "10.7.0.1", devices=[device])
        _own(sr, mgmt, generation=47)

        for _ in range(3):
            self.adapter.store.terminal_job(60, results=[_result(sr.pk, 47)])
        first = consume_static_route_settlements(mgmt)
        assert first.cursor == 3
        born = self.adapter.store.incarnation
        # The cached mirrors both still match: the tab adopted this incarnation and the
        # device pointer is unchanged. Only the feed can tell the store is gone.
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(adapter_incarnation=born)

        # A REAL new lifespan: the serving process is stopped and a new one is started
        # against a rebuilt store, which recreates the device under the same numeric id and
        # restarts its settlement counter at 1.
        rebuilt = self.adapter.rebuild()
        assert rebuilt.incarnation != born
        self._point_at(self.adapter)
        rebuilt.terminal_job(60, results=[_result(sr.pk, 47)])

        row = NSODeviceManagement.objects.get(pk=mgmt.pk)
        assert row.adapter_device_id == 60
        assert row.adapter_incarnation == born, "the cached mirror must still name the dead store"

        result = consume_static_route_settlements(mgmt)

        assert result.epoch_reset is True
        assert result.consumed == 1, "settlement 1 of the rebuilt store was skipped behind a dead cursor"
        after = NSODeviceManagement.objects.get(pk=mgmt.pk)
        assert after.settle_cursor_seq == 1
        assert after.settle_cursor_incarnation == rebuilt.incarnation


class TestStallBound(_SettlementCase):
    """S4.4 / S4.5 / S4.6 — the triple describes one sequence, and the bound is five."""

    def _unresolvable(self, tag, adapter_device_id):
        """A device whose head settlement cannot be correlated: no expectation, no read-back."""
        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id)
        sr = _route(f"10.8.{adapter_device_id % 250}.0/24", "10.8.0.1", devices=[device])
        _own(sr, mgmt, generation=70, expected=False)
        self.adapter.store.terminal_job(adapter_device_id, results=[_result(sr.pk, 70)])
        self.adapter.store.intent_status = 503
        return mgmt, sr

    def test_the_stall_bound_is_exactly_five_attempts(self):
        from netbox_nso_plugin.settlement import SETTLE_STALL_MAX_ATTEMPTS, consume_static_route_settlements

        mgmt, _sr = self._unresolvable("bound", 70)

        for attempt in range(1, SETTLE_STALL_MAX_ATTEMPTS):
            result = consume_static_route_settlements(mgmt)
            assert result.stalled is True
            assert result.advanced_past_stall is False, f"advanced early, on attempt {attempt}"
            row = self._cursor(mgmt)
            assert row.settle_stall_attempts == attempt
            assert row.settle_cursor_seq == 0, "the cursor moved past an unresolved settlement"

        final = consume_static_route_settlements(mgmt)

        assert final.advanced_past_stall is True
        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1
        assert row.settle_stall_seq is None
        assert row.settle_stall_attempts == 0
        assert SETTLE_STALL_MAX_ATTEMPTS == 5

    def test_stall_state_describes_exactly_one_sequence(self):
        """S4.4 — a stall on N may not mis-bound a later stall on M."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, sr = self._unresolvable("onlyone", 71)

        consume_static_route_settlements(mgmt)
        consume_static_route_settlements(mgmt)
        assert self._cursor(mgmt).settle_stall_attempts == 2

        # The read-back recovers, so the head resolves and the walk advances past it.
        self.adapter.store.intent_status = 200
        self.adapter.store.echo(71, sr.pk, 70, FINGERPRINT)
        result = consume_static_route_settlements(mgmt)

        assert result.consumed == 1
        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1
        assert row.settle_stall_seq is None, "the triple survived an advance"
        assert row.settle_stall_attempts == 0
        assert row.settle_stall_first_seen_at is None

        # A later head that stalls starts its own count from one, not from three.
        sr2 = _route("10.9.0.0/16", "10.9.0.1", devices=[mgmt.device])
        _own(sr2, mgmt, generation=71, expected=False)
        self.adapter.store.terminal_job(71, results=[_result(sr2.pk, 71)])
        self.adapter.store.intent_status = 503

        consume_static_route_settlements(mgmt)

        row = self._cursor(mgmt)
        assert row.settle_stall_seq == 2
        assert row.settle_stall_attempts == 1

    def test_a_read_back_that_names_nothing_advances_instead_of_stalling(self):
        """An expectation the store no longer holds can never arrive, so nothing may wait."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        mgmt, _sr = self._unresolvable("nothing", 72)
        # The read-back answers, but the store no longer carries this route at all.
        self.adapter.store.intent_status = 200

        result = consume_static_route_settlements(mgmt)

        assert result.stalled is False, "an undecidable-forever result was stalled instead of advanced"
        assert result.consumed == 1
        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1
        assert row.settle_stall_seq is None

    def test_a_queued_sibling_never_blocks_the_cursor(self):
        """S4.5 (P5.12) — an unsequenced job is invisible and consumed later, at its own seq."""
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("queued")
        mgmt = _make_mgmt(device, "queued", 80)
        sr = _route("10.10.0.0/16", "10.10.0.1", devices=[device])
        _own(sr, mgmt, generation=72)
        store = self.adapter.store
        store.queued_job(80)
        store.terminal_job(80, results=[_result(sr.pk, 72)])

        first = consume_static_route_settlements(mgmt)

        assert first.consumed == 1, "the queued sibling blocked the terminal job behind it"
        assert self._cursor(mgmt).settle_cursor_seq == 1

        # The queued job terminalizes later and takes the NEXT sequence, above the cursor.
        store.terminal_job(80, results=[_result(sr.pk, 72)])
        second = consume_static_route_settlements(mgmt)

        assert second.consumed == 1
        assert self._cursor(mgmt).settle_cursor_seq == 2


class TestDurableStallAcrossProcesses(_SettlementCase):
    """S4.3 (P5.11) — the bound survives a real process boundary, or it is not durable."""

    def _run_consumer(self, device_id: int, passes: int):
        env = dict(os.environ)
        env["DB_NAME"] = connection.settings_dict["NAME"]
        env.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")
        return subprocess.run(  # noqa: S603 — a fixed argv, no shell
            [
                sys.executable,
                "manage.py",
                "nso_consume_static_route_settlements",
                "--device",
                str(device_id),
                "--passes",
                str(passes),
            ],
            cwd="/opt/netbox/netbox",
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_stall_attempts_survive_a_real_process_restart(self):
        from netbox_nso_plugin.settlement import SETTLE_STALL_MAX_ATTEMPTS

        device = _make_device("proc")
        mgmt = _make_mgmt(device, "proc", 90)
        sr = _route("10.11.0.0/16", "10.11.0.1", devices=[device])
        _own(sr, mgmt, generation=73, expected=False)
        self.adapter.store.terminal_job(90, results=[_result(sr.pk, 73)])
        self.adapter.store.intent_status = 503

        first = self._run_consumer(device.pk, passes=SETTLE_STALL_MAX_ATTEMPTS - 1)
        assert first.returncode == 0, first.stderr

        row = self._cursor(mgmt)
        assert row.settle_stall_attempts == SETTLE_STALL_MAX_ATTEMPTS - 1
        assert row.settle_cursor_seq == 0, "the cursor advanced before the bound was reached"

        # A second, genuinely separate process. An in-memory counter starts it at zero and
        # this single pass can never reach the bound.
        second = self._run_consumer(device.pk, passes=1)
        assert second.returncode == 0, second.stderr

        row = self._cursor(mgmt)
        assert row.settle_cursor_seq == 1, "the bound was not reached, so the attempts did not survive"
        assert row.settle_stall_seq is None
        assert row.settle_stall_attempts == 0
