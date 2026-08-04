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
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.core.management import CommandError, call_command
from django.test import TestCase

from .mixins import IntentPushResetMixin

COMMAND = "nso_resync_static_route_intent"


class TestStaticRouteFleetResync(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="FleetMfg", slug="fleetmfg")
        cls.dt = DeviceType.objects.create(manufacturer=mfg, model="FleetDev", slug="fleetdev")
        cls.role = DeviceRole.objects.create(name="FleetRole", slug="fleetrole")
        cls.site = Site.objects.create(name="FleetSite", slug="fleetsite")

    def _managed_device(self, tag: str, adapter_device_id: int):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = Device.objects.create(name=f"fleet-{tag}", device_type=self.dt, role=self.role, site=self.site)
        inst, _ = NSOInstance.objects.get_or_create(name="fleet-inst", defaults={"adapter_instance_id": "fleet-inst"})
        mgmt = NSODeviceManagement.objects.create(
            device=device,
            nso_instance=inst,
            nso_device_name=f"nso-fleet-{tag}",
            adapter_device_id=adapter_device_id,
        )
        return device, mgmt

    def _own_route(self, mgmt, prefix: str, next_hop: str):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import suppress_intent_push

        sr = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1)
        with suppress_intent_push():
            sr.devices.add(mgmt.device)
        return NSOStaticRouteState.objects.create(
            management=mgmt, static_route=sr, status="accepted", nso_prefix=prefix, nso_next_hop=next_hop
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
        """P1.6 — the push swallows its exception and returns ``None``; with ``force=True`` that
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

        assert "fleet-cmdbad" in str(raised.exception) or "fleet-cmdbad" in out.getvalue()

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

        assert "fleet-cmdok" in out.getvalue()

    def test_unlinked_devices_are_skipped(self):
        """A management row with no ``adapter_device_id`` has nothing to push to."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = Device.objects.create(name="fleet-unlinked", device_type=self.dt, role=self.role, site=self.site)
        inst, _ = NSOInstance.objects.get_or_create(name="fleet-inst", defaults={"adapter_instance_id": "fleet-inst"})
        NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name="nso-fleet-unlinked", adapter_device_id=None
        )

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent") as put:
            results = resync_static_route_intent_fleet()

        put.assert_not_called()
        assert results == []
