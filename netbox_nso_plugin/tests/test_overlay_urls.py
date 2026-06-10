# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Overlay get_absolute_url: NSO*State rows have no detail view, so they must resolve to
the device's NSO tab — otherwise NetBox delete-dependency / linkify rendering raises
NoReverseMatch when a parent object (route, VLAN, ...) with an overlay is deleted."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase
from django.urls import reverse


class TestOverlayGetAbsoluteUrl(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="OvMfg", slug="ovmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="OvDev", slug="ovdev")
        role = DeviceRole.objects.create(name="OvRole", slug="ovrole")
        site = Site.objects.create(name="OvSite", slug="ovsite")
        cls.device = Device.objects.create(name="ov-router", device_type=dt, role=role, site=site)
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst = NSOInstance.objects.create(name="ov-inst", adapter_instance_id="ov-inst")
        cls.mgmt = NSODeviceManagement.objects.create(device=cls.device, nso_instance=inst, nso_device_name="nso-ov")

    def test_static_route_overlay_url_resolves_to_device_tab(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import NSOStaticRouteState

        sr = StaticRoute.objects.create(prefix="10.2.2.2/32", next_hop="192.0.0.30", metric=1)
        state = NSOStaticRouteState.objects.create(management=self.mgmt, static_route=sr, status="accepted")
        # Must not raise NoReverseMatch and must point at the device's NSO tab.
        assert state.get_absolute_url() == reverse("dcim:device_nso", kwargs={"pk": self.device.pk})

    def test_vlan_overlay_url_resolves_to_device_tab(self):
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOVLANState

        group = VLANGroup.objects.create(name="OvG", slug="ovg")
        vlan = VLAN.objects.create(group=group, vid=10, name="X")
        state = NSOVLANState.objects.create(management=self.mgmt, vlan=vlan)
        assert state.get_absolute_url() == reverse("dcim:device_nso", kwargs={"pk": self.device.pk})
