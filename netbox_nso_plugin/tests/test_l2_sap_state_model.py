# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Task 1: NSOL2SapState overlay model + manage_l2 flag."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import IntegrityError
from django.test import TestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOL2SapState


def _make_mgmt(tag="l2s"):
    mfg = Manufacturer.objects.create(name=f"{tag}Mfg", slug=f"{tag}mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{tag}Dev", slug=f"{tag}dev")
    role = DeviceRole.objects.create(name=f"{tag}Role", slug=f"{tag}role")
    site = Site.objects.create(name=f"{tag}Site", slug=f"{tag}site")
    device = Device.objects.create(name=f"{tag}-rtr", device_type=dt, role=role, site=site)
    inst = NSOInstance.objects.create(name=f"{tag}-inst", adapter_instance_id=f"{tag}-inst")
    return NSODeviceManagement.objects.create(device=device, nso_instance=inst, nso_device_name=f"{tag}-rtr")


class TestNSOL2SapStateModel(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mgmt = _make_mgmt()

    def test_manage_l2_flag_defaults_false(self):
        self.assertFalse(self.mgmt.manage_l2)

    def test_create_l2_sap_state(self):
        row = NSOL2SapState.objects.create(
            management=self.mgmt,
            service_name="701",
            service_type="vpls",
            sap_id="1/1/c31/3:701",
            port="1/1/c31/3",
            outer_tag=701,
            status="imported",
        )
        row.refresh_from_db()
        self.assertEqual((row.service_type, row.outer_tag, row.inner_tag), ("vpls", 701, None))
        self.assertIsNone(row.l2vpn)
        self.assertEqual(str(row), f"{self.mgmt} / 701:1/1/c31/3:701 [imported]")

    def test_unique_per_service_and_sap(self):
        NSOL2SapState.objects.create(
            management=self.mgmt, service_name="TL", service_type="epipe", sap_id="lag-60:3999", port="lag-60"
        )
        with self.assertRaises(IntegrityError):
            NSOL2SapState.objects.create(
                management=self.mgmt, service_name="TL", service_type="epipe", sap_id="lag-60:3999", port="lag-60"
            )
