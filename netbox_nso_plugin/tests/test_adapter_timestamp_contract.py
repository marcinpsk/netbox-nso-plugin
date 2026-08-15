# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The adapter's canonical wire-timestamp shape, through every plugin site that parses one.

Every timestamp in every adapter API response matches ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?Z$``
— strict UTC, trailing ``Z``, optional fractional seconds. Four plugin sites turn those strings
back into datetimes, and each one silently degrades rather than failing loudly when the parse
misses: the job clock (``reconcile._parse_adapter_ts``) stops the stuck-deploying escalation,
the interface tab (``template_content``) blanks ``last_apply_at``, the read gate
(``read_gate._parse_dt``) nulls ``incarnation_born`` — which is never-null on the wire and is
what orders incarnation adoption, so a null fails the gate closed — and the sync mirror
(``sync_cache``) loses the device's last-sync time.

Each test drives the REAL consumer (no parser stubs) with both permitted shapes, whole-second
and fractional, and asserts an aware-UTC result rather than mere equality: a parser can return
the right instant carrying a host-local tzinfo, which compares equal here but is wrong the
moment the host is not UTC.
"""

from datetime import UTC, datetime, timedelta

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceState

#: The two shapes the adapter is allowed to emit, and the instant each denotes.
_WHOLE = ("2026-06-01T10:00:00Z", datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC))
_FRACTIONAL = ("2026-06-01T10:00:00.123456Z", datetime(2026, 6, 1, 10, 0, 0, 123456, tzinfo=UTC))
_SHAPES = (_WHOLE, _FRACTIONAL)


def _make_device(name):
    mfg = Manufacturer.objects.create(name=f"{name}-mfg", slug=f"{name}-mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{name}-dt", slug=f"{name}-dt")
    role = DeviceRole.objects.create(name=f"{name}-role", slug=f"{name}-role")
    site = Site.objects.create(name=f"{name}-site", slug=f"{name}-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _make_mgmt(device, adapter_device_id=None):
    inst, _ = NSOInstance.objects.get_or_create(name="ts-inst", defaults={"adapter_instance_id": "ts-inst"})
    return NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=device.name,
        adapter_device_id=device.pk if adapter_device_id is None else adapter_device_id,
    )


class _AwareUTCMixin:
    def assertAwareUTC(self, value, expected, wire):  # noqa: N802 — unittest camelCase convention
        self.assertIsNotNone(value, f"{wire!r} must parse, not degrade to None")
        self.assertIsNotNone(value.tzinfo, f"{wire!r} must parse aware, not naive")
        self.assertEqual(value.utcoffset(), timedelta(0), f"{wire!r} must parse as UTC")
        # Not just the right offset: a host-local tzinfo that happens to be UTC today
        # (``tzlocal()``) is the wrong answer on any host that is not.
        self.assertEqual(str(value.tzinfo), "UTC", f"{wire!r} must carry UTC, not a host-local zone")
        self.assertEqual(value, expected)
        self.assertEqual(value.microsecond, expected.microsecond)


class TestJobTimestampParser(_AwareUTCMixin, TestCase):
    """``reconcile._parse_adapter_ts`` — the clock the stuck-deploying escalation runs on."""

    def test_both_shapes_parse_aware_utc(self):
        from netbox_nso_plugin.reconcile import _parse_adapter_ts

        for wire, expected in _SHAPES:
            with self.subTest(wire=wire):
                self.assertAwareUTC(_parse_adapter_ts(wire), expected, wire)

    def test_escalation_fires_on_a_whole_second_job_timestamp(self):
        """The grace comparison is ``timezone.now() - finished``; a None here silently
        disables the escalation, so drive it through the real function end to end."""
        from django.utils import timezone
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.reconcile import _escalate_stuck_deploying
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

        mgmt = _make_mgmt(_make_device("ts-escalate"))
        vlan = VLAN.objects.create(group=_device_vlan_group(mgmt.device), vid=131, name="V131")
        row = NSOVLANState.objects.create(management=mgmt, vlan=vlan, device_name="V131", status="deploying")
        finished = (timezone.now() - timedelta(minutes=30)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        _escalate_stuck_deploying(
            mgmt,
            {
                "id": 931,
                "type": "apply",
                "status": "succeeded",
                "updated_at": finished,
                "result": {"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}},
            },
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")


class TestInterfaceStateTimestamp(_AwareUTCMixin, TestCase):
    """``template_content._upsert_interface_states`` — ``last_apply_at`` on the interface tab."""

    def setUp(self):
        self.device = _make_device("ts-iface")
        self.interface = Interface.objects.create(device=self.device, name="Gi0/1", type="1000base-t")

    def _upsert(self, wire):
        from netbox_nso_plugin.template_content import _upsert_interface_states

        _upsert_interface_states(
            self.device,
            [
                {
                    "name": "Gi0/1",
                    "attrs": {
                        "description": {"nso_value": "core link", "status": "in_sync", "last_apply_at": wire},
                    },
                }
            ],
        )
        return NSOInterfaceState.objects.get(interface=self.interface, attribute="description")

    def test_both_shapes_land_aware_utc_on_the_row(self):
        for wire, expected in _SHAPES:
            with self.subTest(wire=wire):
                NSOInterfaceState.objects.filter(interface=self.interface).delete()
                self.assertAwareUTC(self._upsert(wire).last_apply_at, expected, wire)


class TestReadGateTimestamp(_AwareUTCMixin, TestCase):
    """``read_gate._parse_dt`` — ``read_at`` and the never-null ``incarnation_born``.

    ``incarnation_born`` orders incarnation adoption: a null makes the gate refuse the block
    (``SKIPPED_UNAVAILABLE``, nothing written), so a shape the parser misses does not merely
    lose a display value — it stops every family read from ever publishing.
    """

    def setUp(self):
        self.mgmt = _make_mgmt(_make_device("ts-gate"))
        self.epoch = self.mgmt.adapter_device_id

    def test_both_shapes_parse_aware_utc(self):
        from netbox_nso_plugin.read_gate import _parse_dt

        for wire, expected in _SHAPES:
            with self.subTest(wire=wire):
                self.assertAwareUTC(_parse_dt(wire), expected, wire)

    def _read_state(self, born, read_at):
        return {
            "outcome": "present",
            "reason": None,
            "freshness": "fresh",
            "result": "replaced",
            "succeeded": True,
            "read_at": read_at,
            "attempt_id": 1,
            "incarnation": "88888888-cccc-4ccc-8ccc-888888888888",
            "incarnation_born": born,
            "source_epoch": 1,
            "payload_revision": 1,
        }

    def test_incarnation_born_adopts_non_null_for_both_shapes(self):
        from netbox_nso_plugin.models import NSOFamilyReadState
        from netbox_nso_plugin.read_gate import RAN, gated_family_run

        for wire, expected in _SHAPES:
            with self.subTest(wire=wire):
                mgmt = _make_mgmt(_make_device(f"ts-gate-{expected.microsecond}"))
                result = gated_family_run(
                    mgmt,
                    "bfd",
                    self._read_state(wire, wire),
                    lambda: "body-value",
                    epoch=mgmt.adapter_device_id,
                )
                self.assertEqual(result.disposition, RAN)
                mgmt.refresh_from_db()
                self.assertAwareUTC(mgmt.adapter_incarnation_born, expected, wire)
                row = NSOFamilyReadState.objects.get(management=mgmt, family="bfd")
                self.assertAwareUTC(row.observed_incarnation_born, expected, wire)
                self.assertAwareUTC(row.observed_read_at, expected, wire)


class TestSyncCacheTimestamp(_AwareUTCMixin, TestCase):
    """``sync_cache.refresh_sync_cache`` — the mirror of the adapter's ``devices.last_sync_at``."""

    def setUp(self):
        self.mgmt = _make_mgmt(_make_device("ts-sync"), adapter_device_id=619)

    def _adapter_row(self, last_sync_at):
        return {
            "id": self.mgmt.adapter_device_id,
            "nso_instance": self.mgmt.nso_instance.adapter_instance_id,
            "nso_device_name": self.mgmt.nso_device_name,
            "netbox_device_id": self.mgmt.device_id,
            "last_sync_at": last_sync_at,
            "last_sync_status": "succeeded",
            "degraded_surfaces": None,
        }

    def test_both_shapes_mirror_aware_utc(self):
        from netbox_nso_plugin.sync_cache import refresh_sync_cache

        for wire, expected in _SHAPES:
            with self.subTest(wire=wire):
                self.mgmt.last_sync_at = None
                NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(last_sync_at=None)
                changed = refresh_sync_cache(self.mgmt, self._adapter_row(wire))
                self.assertIn("last_sync_at", changed)
                # In memory first: this is the parser's OWN tzinfo, before the DB round-trip
                # normalizes whatever it was handed.
                self.assertAwareUTC(self.mgmt.last_sync_at, expected, wire)
                self.mgmt.refresh_from_db()
                self.assertAwareUTC(self.mgmt.last_sync_at, expected, wire)

    def test_an_unparseable_timestamp_mirrors_as_none_instead_of_raising(self):
        """This runs on a list-view render — a lenient parser that raises 500s the page."""
        from netbox_nso_plugin.sync_cache import refresh_sync_cache

        with self.assertLogs("netbox_nso_plugin.sync_cache", level="WARNING"):
            refresh_sync_cache(self.mgmt, self._adapter_row("2026-06-01T10:00:00+00:00Z"))
        self.mgmt.refresh_from_db()
        self.assertIsNone(self.mgmt.last_sync_at)

    def test_an_offsetless_timestamp_mirrors_as_none_instead_of_naive(self):
        """The contract is ``<iso>Z``. An offset-less value names no instant, so it is absent."""
        from netbox_nso_plugin.sync_cache import refresh_sync_cache

        with self.assertLogs("netbox_nso_plugin.sync_cache", level="WARNING"):
            refresh_sync_cache(self.mgmt, self._adapter_row("2026-06-01T10:00:00"))
        self.assertIsNone(self.mgmt.last_sync_at, "a naive datetime was mirrored as if it were UTC")
        self.mgmt.refresh_from_db()
        self.assertIsNone(self.mgmt.last_sync_at)

    def test_a_non_string_timestamp_degrades_instead_of_raising(self):
        """``parse_datetime`` raises TypeError for a non-string; every caller is on a page render."""
        from netbox_nso_plugin.sync_cache import parse_adapter_timestamp

        for value in (1717236000, {"at": "2026-06-01T10:00:00Z"}, ["2026-06-01T10:00:00Z"]):
            with self.subTest(value=value):
                with self.assertLogs("netbox_nso_plugin.sync_cache", level="WARNING"):
                    self.assertIsNone(parse_adapter_timestamp(value, "last_probe_at"))

    def test_refresh_degrades_non_string_timestamps_before_persistence(self):
        """The refresh boundary must not pass malformed adapter values to the DateTimeField."""
        from netbox_nso_plugin.sync_cache import refresh_sync_cache

        previous = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)
        self.mgmt.last_sync_at = previous
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(last_sync_at=previous)
        for value in (1717236000, {"at": "2026-06-01T10:00:00Z"}, ["2026-06-01T10:00:00Z"]):
            with self.subTest(value=value):
                with self.assertLogs("netbox_nso_plugin.sync_cache", level="WARNING"):
                    changed = refresh_sync_cache(self.mgmt, self._adapter_row(value))

                self.assertNotIn("last_sync_at", changed)
                self.assertEqual(self.mgmt.last_sync_at, previous)
                self.mgmt.refresh_from_db()
                self.assertEqual(self.mgmt.last_sync_at, previous)
