# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Foreign greenfield routed sub-interface writes stay outside NSO ownership.

Only an explicit sub-interface workflow may acquire renderer ownership. A generic native
Interface create is not ownership evidence and must not create an overlay as a side effect.
"""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase
from django.utils import timezone

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

    def test_exact_writer_creates_its_preplanned_subinterface_state(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        planned_at = timezone.now()
        subinterface = Interface(device=self.device, name="ae99.998", type="virtual", parent=self.parent)
        subinterface._site = self.device.site
        subinterface._location = self.device.location
        subinterface._rack = self.device.rack
        state = NSOSubinterfaceState(
            management=self.mgmt,
            interface=subinterface,
            parent_interface=self.parent,
            dot1q_vlan=998,
            status="accepted",
            accepted_at=planned_at,
        )
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(subinterface, force_insert=True, natural_key=("device", "name")),
                planned_save(
                    state,
                    force_insert=True,
                    natural_key=("management", "interface"),
                    references=(("interface", subinterface),),
                ),
            ),
            planned_at=planned_at,
        )

        with (
            patch("netbox_nso_plugin.adapter_client.put_subinterface_intent"),
            self.captureOnCommitCallbacks(execute=True),
            renderer_writes(plan) as writer,
        ):
            writer.save(subinterface, force_insert=True)

        created = NSOSubinterfaceState.objects.get(interface=subinterface)
        self.assertEqual(created.parent_interface, self.parent)
        self.assertEqual((created.dot1q_vlan, created.status), (998, "accepted"))
