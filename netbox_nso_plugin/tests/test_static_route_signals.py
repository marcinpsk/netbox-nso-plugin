# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for: static route intent push signals."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.test import TestCase

from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin

PUT = "netbox_nso_plugin.adapter_client.put_static_route_intent"


class TestPushStaticRouteIntentForDevice(IntentPushResetMixin, TestCase):
    """Unit tests for _push_static_route_intent_for_device."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SrSigMfg", slug="srsigmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SrSigDev", slug="srssigdev")
        role = DeviceRole.objects.create(name="SrSigRole", slug="srssigrole")
        site = Site.objects.create(name="SrSigSite", slug="srssigsite")
        cls.device = Device.objects.create(name="sr-sig-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self, adapter_device_id=42):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="sr-sig-inst",
            defaults={"adapter_instance_id": "sr-sig-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-sr-sig",
                "adapter_device_id": adapter_device_id,
            },
        )[0]

    def _make_state(self, mgmt, prefix="10.0.0.0/8", next_hop="192.168.1.1", vrf=None, status="accepted"):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import suppress_intent_push

        sr, _ = StaticRoute.objects.get_or_create(
            prefix=prefix,
            next_hop=next_hop,
            vrf=vrf,
            defaults={"metric": 1},
        )
        # Brownfield setup mirrors reconcile (under suppress) so the greenfield
        # assign-signal doesn't auto-own the route before we set the desired status.
        with suppress_intent_push():
            sr.devices.add(self.device)
        return NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=sr,
            status=status,
            nso_prefix=prefix,
            nso_next_hop=next_hop,
        )

    def test_pushes_accepted_routes(self):
        """Accepted routes are included in the intent push payload."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, status="accepted")  # create before patch to avoid signal double-call

        with patch(PUT) as mock_push:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            args = mock_push.call_args[0]
            assert args[0] == mgmt.adapter_device_id
            routes = args[1]
            assert len(routes) == 1
            assert routes[0]["prefix"] == "10.0.0.0/8"
            assert routes[0]["next_hop"] == "192.168.1.1"
            assert routes[0]["vrf"] == ""

    def test_nokia_default_metric_is_still_sent(self):
        """The plugin always sends ``metric`` — omission means CLEAR, NED-agnostically.

        Suppressing Nokia's ``metric == 5`` made an edit 3 → 5 arrive as an absent field,
        which the adapter reads as a clear: the store ends holding NULL against NetBox's 5
        and a networked retract job is enqueued. Sending 5 costs nothing on the device —
        the SR OS writer takes the identical branch for ``None`` and ``5``
        (``static_route_reconciler/main.py:1493-1498``) and the exporter suppresses the
        default on the way back (``network_state_export/static_route.py:486-488``).
        """
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        platform = Platform.objects.create(name="Static push Nokia", slug="static-push-nokia")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        mgmt = self._make_mgmt()
        state = self._make_state(mgmt, status="accepted")
        state.static_route.metric = 3
        state.static_route.save(update_fields=["metric"])

        with patch(PUT) as put:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)
        assert put.call_args.args[1][0]["metric"] == 3

        state.static_route.metric = 5
        state.static_route.save(update_fields=["metric"])

        with patch(PUT) as put:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        route = put.call_args.args[1][0]
        assert route["metric"] == 5

    def test_pushes_apply_failed_routes(self):
        """apply_failed rows stay in the push: intent is still owned (retry-eligible), and
        the adapter PUT is full-replace — skipping them would drop their mirror rows."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, status="apply_failed")

        with patch(PUT) as mock_push:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            routes = mock_push.call_args[0][1]
            assert len(routes) == 1
            assert routes[0]["prefix"] == "10.0.0.0/8"

    def test_excludes_non_accepted_routes(self):
        """Routes with status=imported are excluded from the intent push."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, prefix="172.16.0.0/12", next_hop="10.0.0.1", status="imported")

        with patch(PUT) as mock_push:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            routes = mock_push.call_args[0][1]
            assert routes == []

    def test_excludes_interface_only_next_hop(self):
        """Routes with no IP next-hop (interface-only) are skipped."""
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._make_mgmt()
        sr, _ = StaticRoute.objects.get_or_create(
            prefix="192.168.50.0/24",
            next_hop=None,
            vrf=None,
            defaults={"metric": 1, "interface_next_hop": "GigabitEthernet0/0"},
        )
        sr.devices.add(self.device)
        NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=sr,
            status="accepted",
            nso_prefix="192.168.50.0/24",
        )

        with patch(PUT) as mock_push:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            routes = mock_push.call_args[0][1]
            assert routes == []

    def test_adapter_error_is_swallowed(self):
        """AdapterError during push is logged but does not propagate."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, prefix="10.1.0.0/16", next_hop="10.0.0.2", status="accepted")

        with patch(PUT, side_effect=Exception("boom")):
            # Should not raise
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)


class TestOnStaticRouteStateSave(IntentPushDeliveryMixin, TestCase):
    """Tests for _on_static_route_state_save signal handler."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SrSaveMfg", slug="srsavemfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SrSaveDev", slug="srsavedev")
        role = DeviceRole.objects.create(name="SrSaveRole", slug="srsaverole")
        site = Site.objects.create(name="SrSaveSite", slug="srsavesite")
        cls.device = Device.objects.create(name="sr-save-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="sr-save-inst",
            defaults={"adapter_instance_id": "sr-save-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-sr-save",
                "adapter_device_id": 99,
            },
        )[0]

    def _make_route(self, prefix="10.20.0.0/16", next_hop="10.0.0.1"):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.signals import suppress_intent_push

        sr, _ = StaticRoute.objects.get_or_create(
            prefix=prefix,
            next_hop=next_hop,
            vrf=None,
            defaults={"metric": 1},
        )
        with suppress_intent_push():
            sr.devices.add(self.device)
        return sr

    def test_save_triggers_intent_push(self):
        """Saving NSOStaticRouteState triggers put_static_route_intent."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._make_mgmt()
        sr = self._make_route()

        with patch(PUT) as mock_push:
            state = NSOStaticRouteState(
                management=mgmt,
                static_route=sr,
                status="accepted",
                nso_prefix="10.20.0.0/16",
                nso_next_hop="10.0.0.1",
            )
            from netbox_nso_plugin.signals import _on_static_route_state_save

            with self.captureOnCommitCallbacks(execute=True):
                _on_static_route_state_save(sender=NSOStaticRouteState, instance=state)
            mock_push.assert_called_once()
            args = mock_push.call_args[0]
            assert args[0] == 99  # adapter_device_id

    def test_no_push_when_no_adapter_device_id(self):
        """No push when management.adapter_device_id is None."""
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOStaticRouteState

        inst, _ = NSOInstance.objects.get_or_create(
            name="sr-noid-inst",
            defaults={"adapter_instance_id": "sr-noid-inst"},
        )
        dt = DeviceType.objects.get(slug="srsavedev")
        role = DeviceRole.objects.get(slug="srsaverole")
        site = Site.objects.get(slug="srsavesite")
        extra_dev = Device.objects.create(name="sr-noid-router", device_type=dt, role=role, site=site)
        mgmt = NSODeviceManagement.objects.create(
            device=extra_dev,
            nso_instance=inst,
            nso_device_name="nso-sr-noid",
            adapter_device_id=None,
        )
        sr = self._make_route(prefix="10.30.0.0/16", next_hop="10.0.0.3")
        state = NSOStaticRouteState(management=mgmt, static_route=sr, status="accepted")

        with patch(PUT) as mock_push:
            from netbox_nso_plugin.signals import _on_static_route_state_save

            _on_static_route_state_save(sender=NSOStaticRouteState, instance=state)
            mock_push.assert_not_called()


class TestGreenfieldStaticRoute(IntentPushDeliveryMixin, TestCase):
    """Greenfield write path: an operator-created route assigned to a managed device
    becomes accepted intent + pushes; removing/deleting it pushes the removal."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="GfMfg", slug="gfmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="GfDev", slug="gfdev")
        role = DeviceRole.objects.create(name="GfRole", slug="gfrole")
        site = Site.objects.create(name="GfSite", slug="gfsite")
        cls.device = Device.objects.create(name="gf-router", device_type=dt, role=role, site=site)

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="gf-inst", defaults={"adapter_instance_id": "gf-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "nso-gf", "adapter_device_id": 77},
        )[0]

    def test_assign_device_owns_and_pushes(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._mgmt()
        sr = StaticRoute.objects.create(prefix="10.9.9.0/24", next_hop="10.0.0.9", metric=1)

        with patch(PUT) as mock_push:
            with self.captureOnCommitCallbacks(execute=True):
                sr.devices.add(self.device)  # operator assignment (not suppressed)
            state = NSOStaticRouteState.objects.get(management=mgmt, static_route=sr)
            self.assertEqual(state.status, "accepted")
            mock_push.assert_called()
            routes = mock_push.call_args[0][1]
            self.assertTrue(any(r["prefix"] == "10.9.9.0/24" and r["next_hop"] == "10.0.0.9" for r in routes))

    def test_unassign_device_drops_overlay_and_pushes_removal(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._mgmt()
        sr = StaticRoute.objects.create(prefix="10.9.10.0/24", next_hop="10.0.0.10", metric=1)
        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            sr.devices.add(self.device)
        self.assertTrue(NSOStaticRouteState.objects.filter(management=mgmt, static_route=sr).exists())

        with patch(PUT) as mock_push:
            with self.captureOnCommitCallbacks(execute=True):
                sr.devices.remove(self.device)
            self.assertFalse(NSOStaticRouteState.objects.filter(management=mgmt, static_route=sr).exists())
            mock_push.assert_called()
            routes = mock_push.call_args[0][1]
            self.assertFalse(any(r["prefix"] == "10.9.10.0/24" for r in routes))

    def test_clear_devices_drops_overlay_and_pushes_removal(self):
        """StaticRoute.devices.clear() sends pk_set=None (post_clear). The overlay for every
        detached device must still be dropped + a removal pushed. Regression: `pk_set or []`
        silently removed nothing, orphaning the overlay and stranding stale adapter intent."""
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._mgmt()
        sr = StaticRoute.objects.create(prefix="10.9.12.0/24", next_hop="10.0.0.12", metric=1)
        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            sr.devices.add(self.device)
        self.assertTrue(NSOStaticRouteState.objects.filter(management=mgmt, static_route=sr).exists())

        with patch(PUT) as mock_push:
            with self.captureOnCommitCallbacks(execute=True):
                sr.devices.clear()  # pk_set=None on post_clear
            self.assertFalse(NSOStaticRouteState.objects.filter(management=mgmt, static_route=sr).exists())
            mock_push.assert_called()
            routes = mock_push.call_args[0][1]
            self.assertFalse(any(r["prefix"] == "10.9.12.0/24" for r in routes))

    def test_delete_route_pushes_removal(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._mgmt()
        sr = StaticRoute.objects.create(prefix="10.9.11.0/24", next_hop="10.0.0.11", metric=1)
        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            sr.devices.add(self.device)

        with patch(PUT) as mock_push:
            with self.captureOnCommitCallbacks(execute=True):
                sr.delete()
            self.assertFalse(NSOStaticRouteState.objects.filter(management=mgmt, static_route__isnull=False).exists())
            mock_push.assert_called()
            routes = mock_push.call_args[0][1]
            self.assertFalse(any(r["prefix"] == "10.9.11.0/24" for r in routes))


class TestStaticRouteIntentGenerationOnTheWire(IntentPushResetMixin, TestCase):
    """#1396 R3 P1 — ``route_id`` + ``generation`` in the push, and the echoed expectation."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SrGenMfg", slug="srgenmfg")
        cls.dt = DeviceType.objects.create(manufacturer=mfg, model="SrGenDev", slug="srgendev")
        cls.role = DeviceRole.objects.create(name="SrGenRole", slug="srgenrole")
        cls.site = Site.objects.create(name="SrGenSite", slug="srgensite")
        cls.device = Device.objects.create(name="sr-gen-router", device_type=cls.dt, role=cls.role, site=cls.site)

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="sr-gen-inst", defaults={"adapter_instance_id": "sr-gen-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "nso-sr-gen", "adapter_device_id": 4242},
        )[0]

    def _state(self, mgmt, prefix, next_hop, *, generation=0, next_hop_is_none=False):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import suppress_intent_push

        sr = StaticRoute.objects.create(
            prefix=prefix,
            next_hop=None if next_hop_is_none else next_hop,
            metric=1,
            interface_next_hop="GigabitEthernet0/0" if next_hop_is_none else "",
        )
        with suppress_intent_push():
            sr.devices.add(self.device)
        return NSOStaticRouteState.objects.create(
            management=mgmt,
            static_route=sr,
            status="accepted",
            nso_prefix=prefix,
            nso_next_hop="" if next_hop_is_none else next_hop,
            intent_generation=generation,
        )

    def test_push_names_the_netbox_pk_and_the_allocated_generation(self):
        """P1.1 — the pk is what opens the fence; the generation is what a result correlates on."""
        from netbox_nso_plugin.intent_generation import allocate_intent_generation
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._mgmt()
        generation = allocate_intent_generation()
        state = self._state(mgmt, "10.40.0.0/16", "10.0.0.40", generation=generation)

        with patch(PUT) as put:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        route = put.call_args.args[1][0]
        assert route["route_id"] == state.static_route.pk
        assert route["generation"] == generation
        # The pre-R3 keys are unchanged — this is an addition, not a reshape.
        assert set(route) == {"vrf", "prefix", "next_hop", "permanent", "tag", "metric", "route_id", "generation"}

    def test_the_unallocated_sentinel_goes_on_the_wire_as_null(self):
        """``0`` means "never allocated" — putting it on the wire would let a result correlate
        with a generation that names nothing, which is exactly what the sentinel exists to
        prevent."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._mgmt()
        self._state(mgmt, "10.41.0.0/16", "10.0.0.41", generation=0)

        with patch(PUT) as put:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        route = put.call_args.args[1][0]
        assert route["generation"] is None
        assert route["route_id"] is not None

    def test_interface_only_route_is_still_skipped(self):
        """P1.2 — having a pk does not make an unsupported route pushable."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._mgmt()
        self._state(mgmt, "10.42.0.0/16", None, next_hop_is_none=True)

        with patch(PUT) as put:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        assert put.call_args.args[1] == []

    def test_the_push_returns_the_adapter_response(self):
        """A push must be able to say *failed* rather than *acknowledged* — the fleet driver
        and every settlement expectation are built on that distinction."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._mgmt()
        self._state(mgmt, "10.44.0.0/16", "10.0.0.44")
        response = {"device_id": 4242, "count": 1, "routes": []}

        with patch(PUT, return_value=response):
            assert _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id) == response

        with patch(PUT, side_effect=Exception("boom")):
            assert _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id) is None

    def test_the_echo_records_the_expectation_for_the_generation_pushed(self):
        from netbox_nso_plugin.intent_generation import allocate_intent_generation
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._mgmt()
        generation = allocate_intent_generation()
        state = self._state(mgmt, "10.45.0.0/16", "10.0.0.45", generation=generation)
        response = {
            "device_id": 4242,
            "count": 1,
            "routes": [{"route_id": state.static_route.pk, "generation": generation, "fingerprint": "f00d"}],
        }

        with patch(PUT, return_value=response):
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        state.refresh_from_db()
        assert state.expected_generation == generation
        assert state.expected_fingerprint == "f00d"

    def test_a_response_naming_a_superseded_generation_records_nothing(self):
        """P1.9 — the PUT commits before it answers, so an edit can bump the generation between
        the request and the response. Writing the stale echo would make the *next* result settle
        content the operator has already replaced."""
        from netbox_nso_plugin.intent_generation import allocate_intent_generation
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._mgmt()
        pushed = allocate_intent_generation()
        state = self._state(mgmt, "10.46.0.0/16", "10.0.0.46", generation=pushed)
        newer = allocate_intent_generation()

        def _bump_then_answer(*args, **kwargs):
            # The concurrent edit lands while the request is in flight.
            NSOStaticRouteState.objects.filter(pk=state.pk).update(intent_generation=newer)
            return {
                "device_id": 4242,
                "count": 1,
                "routes": [{"route_id": state.static_route.pk, "generation": pushed, "fingerprint": "stale"}],
            }

        with patch(PUT, side_effect=_bump_then_answer):
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        state.refresh_from_db()
        assert state.intent_generation == newer
        assert state.expected_generation is None
        assert state.expected_fingerprint == ""

    def test_an_echo_for_an_unallocated_row_records_nothing(self):
        """A row still on the sentinel has no generation to correlate — recording a fingerprint
        for it would create an expectation nothing can legitimately match."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._mgmt()
        state = self._state(mgmt, "10.47.0.0/16", "10.0.0.47", generation=0)
        response = {
            "device_id": 4242,
            "count": 1,
            "routes": [{"route_id": state.static_route.pk, "generation": None, "fingerprint": "nope"}],
        }

        with patch(PUT, return_value=response):
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        state.refresh_from_db()
        assert state.expected_generation is None
        assert state.expected_fingerprint == ""

    def test_the_echo_only_reaches_the_device_that_was_pushed(self):
        """A ``StaticRoute`` is shared across devices by M2M, so two overlays can carry the same
        ``route_id`` at the same generation. Without the device predicate, A's echo would write
        B's expectation and B could settle green on A's apply result."""
        from netbox_nso_plugin.intent_generation import allocate_intent_generation
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOStaticRouteState
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device, suppress_intent_push

        mgmt = self._mgmt()
        generation = allocate_intent_generation()
        state = self._state(mgmt, "10.48.0.0/16", "10.0.0.48", generation=generation)

        other = Device.objects.create(name="sr-gen-router-2", device_type=self.dt, role=self.role, site=self.site)
        inst = NSOInstance.objects.get(name="sr-gen-inst")
        other_mgmt = NSODeviceManagement.objects.create(
            device=other, nso_instance=inst, nso_device_name="nso-sr-gen-2", adapter_device_id=4243
        )
        with suppress_intent_push():
            state.static_route.devices.add(other)
        other_state, _ = NSOStaticRouteState.objects.update_or_create(
            management=other_mgmt,
            static_route=state.static_route,
            defaults={"status": "accepted", "intent_generation": generation},
        )

        response = {
            "device_id": 4242,
            "count": 1,
            "routes": [{"route_id": state.static_route.pk, "generation": generation, "fingerprint": "f00d"}],
        }
        with patch(PUT, return_value=response):
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

        state.refresh_from_db()
        other_state.refresh_from_db()
        assert state.expected_generation == generation
        assert state.expected_fingerprint == "f00d"
        assert other_state.expected_generation is None
        assert other_state.expected_fingerprint == ""

    def test_the_armed_field_list_names_every_field_the_arming_helper_writes(self):
        """The accept views persist the armed generation with an explicit field list. A field the
        helper gains that the list does not name is armed in memory and dropped on save, so the
        list has to be derived from one exported constant instead of restated per call site."""
        from netbox_nso_plugin.signals import _STATIC_ROUTE_ARMED_FIELDS, _arm_static_route_generation

        mgmt = self._mgmt()
        state = self._state(mgmt, "10.49.0.0/16", "10.0.0.49", generation=0)
        state.last_apply_error = "boom"
        state.last_result_advisory = "advice"
        before = {field.name: getattr(state, field.name) for field in state._meta.concrete_fields}

        _arm_static_route_generation(state)

        changed = {name for name, value in before.items() if getattr(state, name) != value}
        assert changed == set(_STATIC_ROUTE_ARMED_FIELDS)
