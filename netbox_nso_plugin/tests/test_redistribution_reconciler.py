# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for redistribution_reconciler.reconcile_redistribution create-side.

Verifies it CREATES (not just links) netbox_routing.Redistribution with the
destination scope resolved + route-map linked. Uses an IS-IS destination
(ISISInstance) since that needs no BGP object graph.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase


class TestReconcileRedistribution(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RdMfg", slug="rdmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RdDev", slug="rddev")
        role = DeviceRole.objects.create(name="RdRole", slug="rdrole")
        site = Site.objects.create(name="RdSite", slug="rdsite")
        cls.device = Device.objects.create(name="rd-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="rd-inst", defaults={"adapter_instance_id": "rd-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "rd-dev", "adapter_device_id": self.device.pk},
        )[0]

    def _entry(self, **kw):
        e = {
            "dest_protocol": "isis",
            "dest_ref": "",
            "source_protocol": "static",
            "source_ref": "",
            "route_map": "",
            "metric": None,
            "metric_type": "",
        }
        e.update(kw)
        return e

    def test_creates_redistribution_for_isis_dest(self):
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution, RouteMap

        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        rm = RouteMap.objects.create(name="RM-REDIST")

        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        states = reconcile_redistribution(
            self.device,
            {"entries": [self._entry(route_map="RM-REDIST", metric=10, metric_type="external")]},
        )

        self.assertEqual(len(states), 1)
        s = states[0]
        self.assertTrue(s.redistribution_id is not None)
        self.assertEqual(s.status, "imported")  # unowned, materialized → imported (unified)

        r = Redistribution.objects.get(source_protocol="static")
        self.assertEqual(r.destination, inst)
        self.assertEqual(r.route_map_id, rm.pk)
        self.assertEqual(r.metric, 10)
        self.assertEqual(r.metric_type, "external")

    def test_missing_destination_stays_imported(self):
        """No matching ISISInstance → no Redistribution created, status=imported."""
        self._make_mgmt()
        from netbox_routing.models import Redistribution

        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        states = reconcile_redistribution(self.device, {"entries": [self._entry()]})
        self.assertEqual(len(states), 1)
        self.assertIsNone(states[0].redistribution_id)
        self.assertEqual(states[0].status, "imported")
        self.assertEqual(Redistribution.objects.count(), 0)

    def test_idempotent(self):
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry()]})
        reconcile_redistribution(self.device, {"entries": [self._entry()]})
        self.assertEqual(Redistribution.objects.filter(source_protocol="static").count(), 1)
