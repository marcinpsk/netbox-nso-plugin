# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for A4: adapter_client.get_static_routes and _reconcile_static_routes."""

import unittest
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.test import TestCase

from ._adapter_http import make_session

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


# ---------------------------------------------------------------------------
# adapter_client.get_static_routes — unit tests (no Django DB)
# ---------------------------------------------------------------------------


class TestGetStaticRoutes(unittest.TestCase):
    """Tests for adapter_client.get_static_routes()."""

    def _make_session(self, status=200, json_data=None):
        return make_session(status_code=status, json_data=json_data)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_calls_expected_endpoint(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_static_routes

        session = self._make_session(json_data={"routes": []})
        mock_session_cls.return_value = session

        get_static_routes(99)

        args, _ = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://adapter.local/api/v1/devices/99/static-routes")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_returns_response_unchanged(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_static_routes

        expected = {
            "device_id": 99,
            "last_refreshed_at": "2026-06-01T00:00:00Z",
            "refresh_source": "poll",
            "routes": [
                {
                    "vrf": "",
                    "prefix": "0.0.0.0/0",
                    "next_hop": "10.0.0.1",
                    "metric": 1,
                    "permanent": False,
                }
            ],
        }
        session = self._make_session(json_data=expected)
        mock_session_cls.return_value = session

        result = get_static_routes(99)
        self.assertEqual(result, expected)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_http_error_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, get_static_routes

        session = self._make_session(status=404, json_data={"error": {"code": "not_found", "message": "no device"}})
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError):
            get_static_routes(99)


# ---------------------------------------------------------------------------
# _reconcile_static_routes — Django-DB integration tests
# ---------------------------------------------------------------------------


class TestReconcileStaticRoutes(TestCase):
    """Django-DB tests for _reconcile_static_routes in template_content.py."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="SrMfg", slug="srmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SrDevice", slug="srdevice")
        role = DeviceRole.objects.create(name="SrRole", slug="srrole")
        site = Site.objects.create(name="SrSite", slug="srsite")
        cls.device = Device.objects.create(name="sr-router", device_type=device_type, role=role, site=site)
        cls.device2 = Device.objects.create(name="sr-router-2", device_type=device_type, role=role, site=site)

    def _make_mgmt(self, device, nso_device_name="nso-dev"):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="test-inst",
            defaults={"adapter_instance_id": "test-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": nso_device_name,
                "adapter_device_id": device.pk,
            },
        )[0]

    def _auto_create_ctx(self, auto_create: bool):
        """Flip the real AppConfig's auto-create flag (the attribute production reads).

        patch.object targets the live AppConfig singleton and existence-checks the
        attribute, so a rename of `_static_route_auto_create` fails the test loudly —
        unlike a MagicMock config, which would silently fabricate any attribute name.
        """
        from django.apps import apps

        cfg = apps.get_app_config("netbox_nso_plugin")
        return patch.object(cfg, "_static_route_auto_create", auto_create)

    def _route_payload(self, *routes):
        return {
            "device_id": self.device.pk,
            "refresh_source": "poll",
            "last_refreshed_at": "2026-06-01T00:00:00Z",
            "routes": list(routes),
        }

    def _route_entry(self, prefix="10.0.0.0/8", next_hop="192.168.1.1", vrf="", metric=1):
        return {"vrf": vrf, "prefix": prefix, "next_hop": next_hop, "metric": metric, "permanent": False}

    # ── Basic cases ────────────────────────────────────────────────────────────

    def test_no_mgmt_returns_empty(self):
        """Device without NSODeviceManagement → empty list, no crash."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        mfg = Manufacturer.objects.get_or_create(name="NoMgmtMfg", slug="nomgmtmfg")[0]
        dt = DeviceType.objects.get_or_create(manufacturer=mfg, model="NoMgmtDev", slug="nomgmtdev")[0]
        role = DeviceRole.objects.get_or_create(name="NoMgmtRole", slug="nomgmtrole")[0]
        site = Site.objects.get_or_create(name="NoMgmtSite", slug="nomgmtsite")[0]
        orphan = Device.objects.create(name="orphan-device-sr", device_type=dt, role=role, site=site)

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        with self._auto_create_ctx(False):
            result = _reconcile_static_routes(orphan, {"routes": []})
        self.assertEqual(result, [])

    def test_empty_payload_returns_empty(self):
        """Empty routes payload → no state rows created."""
        self._make_mgmt(self.device)
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        with self._auto_create_ctx(False):
            result = _reconcile_static_routes(self.device, {"routes": []})
        self.assertEqual(result, [])

    def test_new_route_auto_create_off_skipped(self):
        """auto_create=False + route not in NetBox → skipped (no state row created)."""
        self._make_mgmt(self.device, nso_device_name="sr-auto-off")
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        payload = self._route_payload(self._route_entry("192.0.2.0/24", "10.255.0.1"))
        with self._auto_create_ctx(False):
            result = _reconcile_static_routes(self.device, payload)
        self.assertEqual(result, [])

    def test_new_route_auto_create_on_creates_route_and_in_sync(self):
        """auto_create=True: creates StaticRoute, adds device to M2M, FK linked → status=in_sync."""

        mgmt = self._make_mgmt(self.device, nso_device_name="sr-auto-on")
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        payload = self._route_payload(self._route_entry("203.0.113.0/24", "10.0.0.254"))
        with self._auto_create_ctx(True):
            result = _reconcile_static_routes(self.device, payload)

        self.assertEqual(len(result), 1)
        state = result[0]
        self.assertEqual(state.status, "imported")  # unowned, materialized → imported (unified)
        self.assertEqual(state.nso_prefix, "203.0.113.0/24")
        self.assertEqual(state.management, mgmt)
        self.assertTrue(state.static_route.devices.filter(pk=self.device.pk).exists())

    def test_nokia_omitted_preference_seeds_its_ned_default(self):
        """Nokia's omitted next-hop preference is 5, not StaticRoute's default 1."""
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-nokia-default")
        platform = Platform.objects.create(name="Static Nokia", slug="static-nokia")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        entry = self._route_entry("198.18.40.0/24", "198.18.0.1")
        entry.pop("metric")

        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, self._route_payload(entry))

        self.assertEqual(StaticRoute.objects.get(prefix="198.18.40.0/24").metric, 5)

    def test_nokia_omitted_preference_does_not_rewrite_shared_metric(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-nokia-history")
        platform = Platform.objects.create(name="Static Nokia history", slug="static-nokia-history")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        route = StaticRoute.objects.create(
            prefix="198.18.41.0/24",
            next_hop="198.18.0.2",
            metric=1,
        )
        from ._static_route_case import _assign_without_push

        _assign_without_push(route, self.device)
        entry = self._route_entry(str(route.prefix), str(route.next_hop))
        entry.pop("metric")

        _reconcile_static_routes(self.device, self._route_payload(entry))

        route.refresh_from_db()
        self.assertEqual(route.metric, 1)
        state = route.nso_states.get(management__device=self.device)
        self.assertNotEqual(state.status, "in_sync")

    # ── tag drift (#1381 codex F1) ─────────────────────────────────────────────

    def _tagged_route(self, prefix, next_hop, *, tag):
        """A StaticRoute already in NetBox and already linked to self.device."""
        from netbox_routing.models import StaticRoute

        route = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1, tag=tag)
        from ._static_route_case import _assign_without_push

        _assign_without_push(route, self.device)
        return route

    def test_new_route_carries_the_payload_tag(self):
        """Control for the create half: a newly-created route already lands the tag."""
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-tag-create")
        entry = self._route_entry("198.18.50.0/24", "198.18.0.1")
        entry["tag"] = 42
        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, self._route_payload(entry))

        self.assertEqual(StaticRoute.objects.get(prefix="198.18.50.0/24").tag, 42)

    def test_tag_drift_on_an_existing_route_surfaces_as_changed(self):
        """The reconcile compare used to check ONLY metric, so an existing row with no tag
        against a device carrying tag 42 stayed `imported`/`in_sync` — the mirror was
        silently wrong and the drift invisible. `tag` now joins the same conjunction as
        `metric`: compare every read, surface the mismatch through the per-device state.
        """
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-tag-drift")
        route = self._tagged_route("198.18.51.0/24", "198.18.0.2", tag=None)
        entry = self._route_entry(str(route.prefix), str(route.next_hop))
        entry["tag"] = 42

        _reconcile_static_routes(self.device, self._route_payload(entry))

        state = route.nso_states.get(management__device=self.device)
        self.assertEqual(state.status, "changed")

    def test_tag_drift_never_clobbers_the_shared_route(self):
        """The metric precedent exactly (test_nokia_omitted_preference_does_not_rewrite_
        shared_metric): StaticRoute is shared across every associated device, so a refresh
        from one device must never rewrite its tag — the mismatch is surfaced, not applied.
        """
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-tag-noclobber")
        route = self._tagged_route("198.18.52.0/24", "198.18.0.3", tag=7)
        entry = self._route_entry(str(route.prefix), str(route.next_hop))
        entry["tag"] = 42

        _reconcile_static_routes(self.device, self._route_payload(entry))

        route.refresh_from_db()
        self.assertEqual(route.tag, 7)
        self.assertEqual(route.nso_states.get(management__device=self.device).status, "changed")

    def test_tag_absent_from_the_payload_against_a_tagged_route_is_drift(self):
        """The other direction: NetBox carries a tag the device no longer reports."""
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-tag-gone")
        route = self._tagged_route("198.18.53.0/24", "198.18.0.4", tag=9)
        entry = self._route_entry(str(route.prefix), str(route.next_hop))  # no tag key at all

        _reconcile_static_routes(self.device, self._route_payload(entry))

        self.assertEqual(route.nso_states.get(management__device=self.device).status, "changed")

    def test_matching_tag_still_imports_clean(self):
        """Control: an agreeing tag (and an agreeing absent tag) must not manufacture drift."""
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-tag-match")
        tagged = self._tagged_route("198.18.54.0/24", "198.18.0.5", tag=42)
        untagged = self._tagged_route("198.18.55.0/24", "198.18.0.6", tag=None)
        e1 = self._route_entry(str(tagged.prefix), str(tagged.next_hop))
        e1["tag"] = 42
        e2 = self._route_entry(str(untagged.prefix), str(untagged.next_hop))

        _reconcile_static_routes(self.device, self._route_payload(e1, e2))

        self.assertEqual(tagged.nso_states.get(management__device=self.device).status, "imported")
        self.assertEqual(untagged.nso_states.get(management__device=self.device).status, "imported")

    def test_tag_drift_preserves_operator_owned_statuses_exactly_like_metric(self):
        """The binding control: `settles_owned=False` means an owned row is never pulled
        back by a value mismatch, and (#1502 P5.1) `settles_deploying=False` means a
        reconcile never settles one either — identical to what a metric mismatch does, for
        the same status set.

        ``deploying`` used to land ``in_sync`` here. That was the live false green this
        appendix exists to remove: re-reading a route says nothing about WHICH generation
        the device is reflecting, so a metric edit still in flight settled green the moment
        the OLD route came back on a sync. Only a generation-correlated apply result may
        settle this family now.
        """
        from netbox_nso_plugin.models import NSOApplyAttempt
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        mgmt = self._make_mgmt(self.device, nso_device_name="sr-tag-owned")
        for i, (owned, expected) in enumerate(
            (
                ("accepted", "accepted"),
                ("in_sync", "in_sync"),
                ("deploying", "deploying"),
                ("apply_failed", "apply_failed"),
            )
        ):
            with self.subTest(status=owned):
                route = self._tagged_route(f"198.18.6{i}.0/24", f"198.18.1.{i + 1}", tag=None)
                attempt = NSOApplyAttempt.objects.create(management=mgmt) if owned == "deploying" else None
                state = route.nso_states.create(
                    management=mgmt,
                    status=owned,
                    apply_attempt_id=attempt.pk if attempt else None,
                )
                entry = self._route_entry(str(route.prefix), str(route.next_hop))

                entry_tag = dict(entry, tag=42)
                _reconcile_static_routes(self.device, self._route_payload(entry_tag))
                state.refresh_from_db()
                tag_result = state.status

                # the same row driven by a METRIC mismatch instead — must land identically
                state.status = owned
                state.save(update_fields=["status"])
                _reconcile_static_routes(self.device, self._route_payload(dict(entry, metric=99)))
                state.refresh_from_db()

                self.assertEqual(tag_result, expected)
                self.assertEqual(tag_result, state.status)

    def test_idempotent_second_reconcile_same_result(self):
        """Second reconcile with same payload → same state rows, no duplicates."""
        from netbox_routing.models import StaticRoute

        self._make_mgmt(self.device, nso_device_name="sr-idem")
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        payload = self._route_payload(self._route_entry("198.51.100.0/24", "10.1.1.1"))
        with self._auto_create_ctx(True):
            result1 = _reconcile_static_routes(self.device, payload)
            result2 = _reconcile_static_routes(self.device, payload)

        self.assertEqual(len(result1), 1)
        self.assertEqual(len(result2), 1)
        count = NSOStaticRouteState.objects.filter(management__device=self.device).count()
        self.assertEqual(count, 1)
        sr_count = StaticRoute.objects.filter(prefix="198.51.100.0/24").count()
        self.assertEqual(sr_count, 1)

    # ── Shared M2M routes ──────────────────────────────────────────────────────

    def test_shared_route_two_devices_one_static_route(self):
        """Two devices reporting the same route → ONE StaticRoute, two M2M members, two state rows."""
        from netbox_routing.models import StaticRoute

        self._make_mgmt(self.device, nso_device_name="sr-shared-1")
        self._make_mgmt(self.device2, nso_device_name="sr-shared-2")
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        route_entry = self._route_entry("100.64.0.0/10", "172.16.0.1")
        payload1 = {
            "device_id": self.device.pk,
            "refresh_source": "poll",
            "routes": [route_entry],
        }
        payload2 = {
            "device_id": self.device2.pk,
            "refresh_source": "poll",
            "routes": [route_entry],
        }
        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, payload1)
            _reconcile_static_routes(self.device2, payload2)

        sr = StaticRoute.objects.get(prefix="100.64.0.0/10", next_hop="172.16.0.1")
        self.assertEqual(sr.devices.count(), 2)
        self.assertIn(self.device, sr.devices.all())
        self.assertIn(self.device2, sr.devices.all())

        from netbox_nso_plugin.models import NSOStaticRouteState

        states = NSOStaticRouteState.objects.filter(static_route=sr)
        self.assertEqual(states.count(), 2)

    # ── Removal (stale routes) ─────────────────────────────────────────────────

    def test_stale_brownfield_route_pruned_from_m2m_and_overlay(self):
        """A brownfield (non-owned) route that disappears from the payload → device removed
        from the M2M AND the overlay pruned (no dangling drift on a device the route's
        devices-list no longer includes)."""
        from netbox_routing.models import StaticRoute

        self._make_mgmt(self.device, nso_device_name="sr-stale")
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        payload_with = self._route_payload(self._route_entry("10.99.0.0/16", "10.0.0.1"))
        payload_without = self._route_payload()  # empty

        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, payload_with)
            _reconcile_static_routes(self.device, payload_without)

        sr = StaticRoute.objects.filter(prefix="10.99.0.0/16").first()
        self.assertIsNotNone(sr)
        self.assertFalse(sr.devices.filter(pk=self.device.pk).exists())
        # Vestigial overlay pruned — not left dangling as 'changed'.
        self.assertFalse(NSOStaticRouteState.objects.filter(management__device=self.device, static_route=sr).exists())

    def test_stale_owned_route_kept_as_changed(self):
        """An owned (greenfield/accepted) route the device stops reporting → overlay kept as
        'changed' and the device↔route association preserved (operator intent not discarded)."""
        from netbox_routing.models import StaticRoute

        self._make_mgmt(self.device, nso_device_name="sr-owned-stale")
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        payload_with = self._route_payload(self._route_entry("10.77.0.0/16", "10.0.0.3"))
        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, payload_with)

        sr = StaticRoute.objects.get(prefix="10.77.0.0/16")
        state = NSOStaticRouteState.objects.get(management__device=self.device, static_route=sr)
        state.status = "in_sync"  # operator owns it
        state.save(update_fields=["status"])

        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, self._route_payload())  # route gone

        state.refresh_from_db()
        self.assertEqual(state.status, "changed")
        self.assertTrue(sr.devices.filter(pk=self.device.pk).exists())  # association kept

    def test_stale_removal_leaves_static_route_object(self):
        """Removing device from M2M never deletes the StaticRoute object."""
        from netbox_routing.models import StaticRoute

        self._make_mgmt(self.device, nso_device_name="sr-nodelete")
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        payload_with = self._route_payload(self._route_entry("10.88.0.0/16", "10.0.0.2"))
        payload_without = self._route_payload()

        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, payload_with)
            _reconcile_static_routes(self.device, payload_without)

        self.assertTrue(StaticRoute.objects.filter(prefix="10.88.0.0/16").exists())

    # ── Conflict detection ─────────────────────────────────────────────────────

    def test_conflict_when_route_exists_owned_by_another_device(self):
        """Route exists in NetBox with another device → status='conflict', no M2M add."""
        from netbox_routing.models import StaticRoute

        self._make_mgmt(self.device, nso_device_name="sr-conflict-owner")
        self._make_mgmt(self.device2, nso_device_name="sr-conflict-reporter")
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        # device2 is the original owner of this route
        payload_owner = {
            "device_id": self.device.pk,
            "refresh_source": "poll",
            "routes": [self._route_entry("10.77.0.0/16", "10.0.0.99")],
        }
        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, payload_owner)

        # Now device (the other device) also reports the same route → conflict
        payload_reporter = {
            "device_id": self.device2.pk,
            "refresh_source": "poll",
            "routes": [self._route_entry("10.77.0.0/16", "10.0.0.99")],
        }
        with self._auto_create_ctx(False):  # auto_create off for reporter
            result = _reconcile_static_routes(self.device2, payload_reporter)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "conflict")
        # device2 must NOT be in the M2M
        sr = StaticRoute.objects.get(prefix="10.77.0.0/16", next_hop="10.0.0.99")
        self.assertFalse(sr.devices.filter(pk=self.device2.pk).exists())

    # ── Write-path status preservation ────────────────────────────────────────

    def test_accepted_status_preserved_on_re_reconcile(self):
        """Once status='accepted', re-reconcile from NSO must NOT reset it to 'imported'."""
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        self._make_mgmt(self.device, nso_device_name="sr-accept-preserve")
        payload = self._route_payload(self._route_entry("10.66.0.0/16", "10.0.0.55"))

        with self._auto_create_ctx(True):
            _reconcile_static_routes(self.device, payload)

        # Simulate operator accepting
        state = NSOStaticRouteState.objects.get(management__device=self.device)
        state.status = "accepted"
        state.save(update_fields=["status"])

        with self._auto_create_ctx(True):
            result = _reconcile_static_routes(self.device, payload)

        self.assertEqual(result[0].status, "accepted")

    # ── Missing next_hop / VRF edge cases ─────────────────────────────────────

    def test_route_without_next_hop_and_no_interface_next_hop_skipped(self):
        """Route with no next_hop and no interface_next_hop → skipped gracefully."""
        self._make_mgmt(self.device, nso_device_name="sr-nonexthop")
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        bad_entry = {"vrf": "", "prefix": "0.0.0.0/0", "next_hop": None, "interface_next_hop": None}
        payload = self._route_payload(bad_entry)
        with self._auto_create_ctx(True):
            result = _reconcile_static_routes(self.device, payload)
        self.assertEqual(result, [])


class TestStaticRouteEnrichment(TestCase):
    """discard/pseudo next-hops, VRF auto-create toggle, AdapterConnection settings."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SrEnrMfg", slug="srenrmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SrEnrDev", slug="srenrdev")
        role = DeviceRole.objects.create(name="SrEnrRole", slug="srenrrole")
        site = Site.objects.create(name="SrEnrSite", slug="srenrsite")
        cls.device = Device.objects.create(name="sr-enr-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="enr-inst", defaults={"adapter_instance_id": "enr-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "sr-enr-dev", "adapter_device_id": self.device.pk},
        )[0]

    def _conn(self, **flags):
        from netbox_nso_plugin.models import AdapterConnection

        return AdapterConnection.objects.create(url="http://adapter.local", enabled=True, **flags)

    def _payload(self, *routes):
        return {"routes": list(routes)}

    def test_discard_route_fills_with_interface_next_hop(self):
        """A blackhole route (no IP next-hop, interface_next_hop='discard') is created."""
        self._make_mgmt()
        self._conn(static_route_auto_create=True)
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        entry = {"vrf": "", "prefix": "8.8.8.0/24", "next_hop": None, "interface_next_hop": "discard"}
        result = _reconcile_static_routes(self.device, self._payload(entry))

        self.assertEqual(len(result), 1)
        route = StaticRoute.objects.get(prefix="8.8.8.0/24")
        self.assertEqual(route.interface_next_hop, "discard")
        self.assertIsNone(route.next_hop)
        self.assertEqual(result[0].status, "imported")  # unowned, materialized → imported (unified)

    def test_next_table_pseudo_hop_fills(self):
        """A next-table route leak is represented via interface_next_hop."""
        self._make_mgmt()
        self._conn(static_route_auto_create=True, vrf_auto_create=True)
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        entry = {"vrf": "ONRAMP_2", "prefix": "0.0.0.0/0", "next_hop": None, "interface_next_hop": "next-table:inet.0"}
        _reconcile_static_routes(self.device, self._payload(entry))
        self.assertTrue(StaticRoute.objects.filter(prefix="0.0.0.0/0", interface_next_hop="next-table:inet.0").exists())

    def test_vrf_auto_create_on_creates_vrf(self):
        """vrf_auto_create=True → missing VRF is created and the route fills."""
        self._make_mgmt()
        self._conn(static_route_auto_create=True, vrf_auto_create=True)
        from ipam.models import VRF
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        entry = {"vrf": "NEWVRF", "prefix": "10.9.0.0/16", "next_hop": "10.0.0.1"}
        _reconcile_static_routes(self.device, self._payload(entry))

        self.assertTrue(VRF.objects.filter(name="NEWVRF").exists())
        self.assertTrue(StaticRoute.objects.filter(prefix="10.9.0.0/16").exists())

    def test_vrf_missing_skipped_when_auto_create_off(self):
        """vrf_auto_create=False → unknown-VRF route skipped, no VRF created."""
        self._make_mgmt()
        self._conn(static_route_auto_create=True, vrf_auto_create=False)
        from ipam.models import VRF
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        entry = {"vrf": "GHOSTVRF", "prefix": "10.8.0.0/16", "next_hop": "10.0.0.1"}
        result = _reconcile_static_routes(self.device, self._payload(entry))

        self.assertEqual(result, [])
        self.assertFalse(VRF.objects.filter(name="GHOSTVRF").exists())
        self.assertFalse(StaticRoute.objects.filter(prefix="10.8.0.0/16").exists())

    def test_adapter_connection_setting_authoritative(self):
        """With an enabled AdapterConnection, its flag drives auto_create (no app config)."""
        self._make_mgmt()
        self._conn(static_route_auto_create=True)
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        entry = {"vrf": "", "prefix": "10.7.0.0/16", "next_hop": "10.0.0.1"}
        _reconcile_static_routes(self.device, self._payload(entry))
        self.assertTrue(StaticRoute.objects.filter(prefix="10.7.0.0/16").exists())

    def test_metric_clamped_to_model_constraint(self):
        """An out-of-range NSO metric falls back to 1 (model constraint 0..255)."""
        self._make_mgmt()
        self._conn(static_route_auto_create=True)
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.template_content import _reconcile_static_routes

        entry = {"vrf": "", "prefix": "10.6.0.0/16", "next_hop": "10.0.0.1", "metric": 9999}
        _reconcile_static_routes(self.device, self._payload(entry))
        self.assertEqual(StaticRoute.objects.get(prefix="10.6.0.0/16").metric, 1)
