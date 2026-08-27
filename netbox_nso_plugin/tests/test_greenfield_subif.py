# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Foreign greenfield routed sub-interface writes stay outside NSO ownership.

Only an explicit sub-interface workflow may acquire renderer ownership. A generic native
Interface create is not ownership evidence and must not create an overlay as a side effect.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOSubinterfaceState

from .mixins import IntentPushResetMixin


class TestGreenfieldSubinterfaceState(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="GfMfg", slug="gfmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="GfDev", slug="gfdev")
        role = DeviceRole.objects.create(name="GfRole", slug="gfrole")
        site = Site.objects.create(name="GfSite", slug="gfsite")
        cls.device = Device.objects.create(name="gf-sw01", device_type=dt, role=role, site=site)
        nso = NSOInstance.objects.create(name="gf-nso", adapter_instance_id="gf-nso-id")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=nso, nso_device_name="gf-sw01", adapter_device_id=402
        )
        cls.parent = Interface.objects.create(device=cls.device, name="ae99", type="lag")

    def test_creating_routed_subif_does_not_acquire_ownership(self):
        """A foreign native create does not acquire a sub-interface overlay."""
        subif = Interface.objects.create(device=self.device, name="ae99.999", type="virtual", parent=self.parent)
        self.assertFalse(NSOSubinterfaceState.objects.filter(interface=subif).exists())

    def test_physical_interface_creates_no_subif_state(self):
        """A plain interface (no dot1q suffix) must NOT be treated as a subinterface."""
        Interface.objects.create(device=self.device, name="ae100", type="lag")
        self.assertFalse(NSOSubinterfaceState.objects.filter(interface__name="ae100").exists())
