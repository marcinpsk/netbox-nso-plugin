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

    def test_edit_surfaces_as_changed_and_survives(self):
        """Editing the Redistribution object → drift, and the edit is not clobbered."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        redist = Redistribution.objects.get(source_protocol="static")
        redist.metric = 99  # operator edit; device still reports 10
        redist.save()

        states = reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        self.assertEqual(states[0].status, "changed")  # edit surfaced as drift
        redist.refresh_from_db()
        self.assertEqual(redist.metric, 99)  # edit preserved, not reverted

    def test_device_change_auto_mirrors_when_untouched(self):
        """3-way: device metric change with object untouched → auto-mirror, in sync."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        # Device changes metric 10→20; object never edited → auto-mirror.
        states = reconcile_redistribution(self.device, {"entries": [self._entry(metric=20)]})
        Redistribution.objects.get(source_protocol="static").refresh_from_db()
        self.assertEqual(Redistribution.objects.get(source_protocol="static").metric, 20)
        self.assertEqual(states[0].status, "imported")

    def test_both_moved_is_conflict(self):
        """3-way: object edited AND device changed since base → conflict, edit preserved."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        redist = Redistribution.objects.get(source_protocol="static")
        redist.metric = 99  # operator edit
        redist.save()
        states = reconcile_redistribution(self.device, {"entries": [self._entry(metric=20)]})  # device also moved
        self.assertEqual(states[0].status, "conflict")
        redist.refresh_from_db()
        self.assertEqual(redist.metric, 99)  # edit preserved

    def test_unowned_removed_redistribution_is_deleted(self):
        """An UNOWNED redistribution the device stops reporting is tracked away: the overlay
        row and its (leaf) Redistribution object are removed (no lingering false drift)."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        self.assertEqual(NSORedistributionState.objects.count(), 1)
        self.assertEqual(Redistribution.objects.count(), 1)

        reconcile_redistribution(self.device, {"entries": []})  # device removed it
        self.assertEqual(NSORedistributionState.objects.count(), 0)  # overlay gone
        self.assertEqual(Redistribution.objects.count(), 0)  # object gone

    def test_owned_removed_redistribution_kept_as_drift(self):
        """An ACCEPTED redistribution the device removes is KEPT and flagged: status=changed,
        device_present=False — operator intent is never auto-deleted."""
        from django.utils import timezone

        self._make_mgmt()
        from netbox_routing.models import ISISInstance, Redistribution

        ISISInstance.objects.create(device=self.device, process_tag="")
        from netbox_nso_plugin.models import NSORedistributionState
        from netbox_nso_plugin.redistribution_reconciler import reconcile_redistribution

        reconcile_redistribution(self.device, {"entries": [self._entry(metric=10)]})
        s = NSORedistributionState.objects.get(management__device=self.device)
        s.status = "accepted"
        s.accepted_at = timezone.now()
        s.save(update_fields=["status", "accepted_at"])

        reconcile_redistribution(self.device, {"entries": []})  # device removed it
        s.refresh_from_db()
        self.assertFalse(s.device_present)
        self.assertIn(s.status, ("accepted", "deploying", "in_sync", "apply_failed"))
        self.assertEqual(Redistribution.objects.count(), 1)  # object kept (operator owns it)


class TestBuildBgpRouterList(TestCase):
    """_build_bgp_router_list must materialize redistribution-only scopes.

    An accepted BGP redistribution whose (asn, vrf) has no owned peer previously
    produced an empty router list — the dest_ref join at apply time then found no
    AF and the redistribution silently never reached the device.
    """

    def test_redistribution_only_scope_materializes_router(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        redist = [{"source_protocol": "static", "source_ref": "", "route_map": "PCE-BGP-EXPORT"}]
        out = _build_bgp_router_list({}, {("2222", ""): {"ipv4-unicast": redist}})
        self.assertEqual(
            out,
            [
                {
                    "asn": "2222",
                    "scopes": [
                        {
                            "vrf": "",
                            "peers": [],
                            "address_families": [{"af": "ipv4-unicast", "redistribution": redist}],
                        }
                    ],
                }
            ],
        )

    def test_peer_scope_keeps_redistribution_and_no_duplicate(self):
        from netbox_nso_plugin.signals import _build_bgp_router_list

        redist = [{"source_protocol": "static", "source_ref": ""}]
        routers = {
            "2222": {
                "asn": "2222",
                "scopes": {
                    "": {
                        "vrf": "",
                        "address_families": [],
                        "peers": [
                            {"peer_address": "192.0.2.1", "enabled": True, "remote_as": "1", "address_families": []}
                        ],
                    }
                },
            }
        }
        out = _build_bgp_router_list(routers, {("2222", ""): {"ipv4-unicast": redist}})
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["scopes"]), 1)
        scope = out[0]["scopes"][0]
        self.assertEqual(scope["address_families"], [{"af": "ipv4-unicast", "redistribution": redist}])
        self.assertEqual(len(scope["peers"]), 1)
