# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Greenfield routed sub-interface write path (Phase A of intent-integrity).

Creating a routed sub-interface in NetBox (virtual + parent + dot1q name suffix) on a
managed device must own + push it as intent, so the subinterface-reconciler creates the
unit on the device — the operator-driven greenfield write the brownfield pipeline lacked.
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

    def test_creating_routed_subif_owns_and_tracks_it(self):
        """A new virtual subif with a parent + dot1q suffix → an owned NSOSubinterfaceState."""
        subif = Interface.objects.create(device=self.device, name="ae99.999", type="virtual", parent=self.parent)
        state = NSOSubinterfaceState.objects.get(interface=subif)
        self.assertEqual(state.dot1q_vlan, 999)
        self.assertEqual(state.parent_interface_id, self.parent.id)
        self.assertEqual(state.management_id, self.mgmt.id)
        self.assertEqual(state.status, "accepted")
        self.assertIsNotNone(state.accepted_at)

    def test_physical_interface_creates_no_subif_state(self):
        """A plain interface (no dot1q suffix) must NOT be treated as a subinterface."""
        Interface.objects.create(device=self.device, name="ae100", type="lag")
        self.assertFalse(NSOSubinterfaceState.objects.filter(interface__name="ae100").exists())
