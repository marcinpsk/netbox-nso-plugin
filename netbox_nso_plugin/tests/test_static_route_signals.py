# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for M10 B3: static route intent push signals."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase


class TestPushStaticRouteIntentForDevice(TestCase):
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

        sr, _ = StaticRoute.objects.get_or_create(
            prefix=prefix,
            next_hop=next_hop,
            vrf=vrf,
            defaults={"metric": 1},
        )
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

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent") as mock_push:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            args = mock_push.call_args[0]
            assert args[0] == mgmt.adapter_device_id
            routes = args[1]
            assert len(routes) == 1
            assert routes[0]["prefix"] == "10.0.0.0/8"
            assert routes[0]["next_hop"] == "192.168.1.1"
            assert routes[0]["vrf"] == ""

    def test_excludes_non_accepted_routes(self):
        """Routes with status=imported are excluded from the intent push."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, prefix="172.16.0.0/12", next_hop="10.0.0.1", status="imported")

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent") as mock_push:
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

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent") as mock_push:
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            routes = mock_push.call_args[0][1]
            assert routes == []

    def test_adapter_error_is_swallowed(self):
        """AdapterError during push is logged but does not propagate."""
        from netbox_nso_plugin.signals import _push_static_route_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, prefix="10.1.0.0/16", next_hop="10.0.0.2", status="accepted")

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent", side_effect=Exception("boom")):
            # Should not raise
            _push_static_route_intent_for_device(self.device.pk, mgmt.adapter_device_id)


class TestOnStaticRouteStateSave(TestCase):
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

        sr, _ = StaticRoute.objects.get_or_create(
            prefix=prefix,
            next_hop=next_hop,
            vrf=None,
            defaults={"metric": 1},
        )
        sr.devices.add(self.device)
        return sr

    def test_save_triggers_intent_push(self):
        """Saving NSOStaticRouteState triggers put_static_route_intent."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        mgmt = self._make_mgmt()
        sr = self._make_route()

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent") as mock_push:
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

        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent") as mock_push:
            from netbox_nso_plugin.signals import _on_static_route_state_save

            _on_static_route_state_save(sender=NSOStaticRouteState, instance=state)
            mock_push.assert_not_called()
