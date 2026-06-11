# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the OSPF read-path fill: _reconcile_ospf creating netbox-routing
OSPFInstance / OSPFArea / OSPFInterface objects (M19) plus the overlay rows."""

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase


def _make_ospf_device(suffix="ospf"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"OspfMfg{suffix}", slug=f"ospfmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"OspfDev{suffix}", slug=f"ospfdev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"OspfRole{suffix}", slug=f"ospfrole{suffix}")
    site, _ = Site.objects.get_or_create(name=f"OspfSite{suffix}", slug=f"ospfsite{suffix}")
    return Device.objects.create(name=f"ospf-rtr-{suffix}", device_type=dt, role=role, site=site)


class TestReconcileOspfFill(TestCase):
    """Integration tests for _reconcile_ospf() → netbox-routing graph + overlay."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_ospf_device("main")
        cls.lo0 = Interface.objects.create(device=cls.device, name="Loopback0", type="virtual")
        cls.tun = Interface.objects.create(device=cls.device, name="Tunnel10", type="virtual")

    def _make_mgmt(self, device=None):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = device or self.device
        inst, _ = NSOInstance.objects.get_or_create(
            name="ospf-test-inst",
            defaults={"adapter_instance_id": "ospf-test-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "ospf-dev",
                "adapter_device_id": device.pk,
                "manage_ospf": True,
            },
        )[0]

    def _payload(self, instances=None, interfaces=None):
        return {
            "device_id": self.device.pk,
            "instances": instances if instances is not None else [],
            "interfaces": interfaces if interfaces is not None else [],
        }

    def _instance(self, process_id=10, router_id="10.0.0.1", vrf="", areas=None):
        return {
            "process_id": process_id,
            "router_id": router_id,
            "vrf": vrf,
            "areas": areas if areas is not None else [{"area-id": "0.0.0.0", "area-type": "standard"}],
        }

    def _iface(self, name="Tunnel10", process_id=10, area_id="0.0.0.0", **kw):
        entry = {"interface_name": name, "process_id": process_id, "area_id": area_id}
        entry.update(kw)
        return entry

    # ── Instance fill ───────────────────────────────────────────────────────

    def test_no_mgmt_returns_empty(self):
        orphan = _make_ospf_device("orphan")
        from netbox_nso_plugin.template_content import _reconcile_ospf

        result = _reconcile_ospf(orphan, self._payload([self._instance()]))
        self.assertEqual(result, {"instances": [], "interfaces": []})

    def test_creates_ospf_instance(self):
        self._make_mgmt()
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(self.device, self._payload([self._instance(process_id=10, router_id="10.0.0.1")]))

        inst = OSPFInstance.objects.get(device=self.device, process_id=10)
        self.assertEqual(str(inst.router_id), "10.0.0.1")
        self.assertEqual(inst.name, "10")

    def test_instance_overlay_in_sync_when_linked(self):
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_ospf

        res = _reconcile_ospf(self.device, self._payload([self._instance()]))
        self.assertEqual(len(res["instances"]), 1)
        self.assertEqual(res["instances"][0].status, "imported")  # unowned, materialized → imported (unified)

    def test_stale_instance_with_linked_object_marked_changed(self):
        # Device drops the process but its netbox-routing OSPFInstance still exists → drift.
        self._make_mgmt()
        from netbox_nso_plugin.models import NSOOSPFInstanceState
        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(self.device, self._payload([self._instance(process_id=10)]))
        _reconcile_ospf(self.device, self._payload([]))
        state = NSOOSPFInstanceState.objects.get(process_id="10")
        self.assertEqual(state.status, "changed")
        self.assertIsNotNone(state.ospf_instance_id)

    def test_stale_instance_ghost_pruned(self):
        # An unowned overlay with no linked netbox-routing object is a status-only ghost
        # → pruned rather than left as perpetual false drift.
        mgmt = self._make_mgmt()
        from netbox_nso_plugin.models import NSOOSPFInstanceState
        from netbox_nso_plugin.template_content import _reconcile_ospf

        NSOOSPFInstanceState.objects.create(management=mgmt, process_id="999", status="imported", ospf_instance=None)
        _reconcile_ospf(self.device, self._payload([]))
        self.assertFalse(NSOOSPFInstanceState.objects.filter(process_id="999").exists())

    def test_instance_device_change_auto_mirrors_when_untouched(self):
        """3-way: device router_id change with object untouched → auto-mirror, in sync."""
        self._make_mgmt()
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(self.device, self._payload([self._instance(router_id="10.0.0.1")]))
        res = _reconcile_ospf(self.device, self._payload([self._instance(router_id="10.0.0.2")]))
        self.assertEqual(str(OSPFInstance.objects.get(device=self.device, process_id="10").router_id), "10.0.0.2")
        self.assertEqual(res["instances"][0].status, "imported")

    def test_instance_both_moved_is_conflict(self):
        """3-way: object edited AND device changed since base → conflict, edit preserved."""
        self._make_mgmt()
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(self.device, self._payload([self._instance(router_id="10.0.0.1")]))
        inst = OSPFInstance.objects.get(device=self.device, process_id="10")
        inst.router_id = "10.9.9.9"  # operator edit
        inst.save()
        res = _reconcile_ospf(self.device, self._payload([self._instance(router_id="10.0.0.2")]))  # device also moved
        self.assertEqual(res["instances"][0].status, "conflict")
        inst.refresh_from_db()
        self.assertEqual(str(inst.router_id), "10.9.9.9")  # edit preserved

    def test_instance_without_router_id_skipped_but_overlay_kept(self):
        """router_id is required by the model → no OSPFInstance, but overlay still imported."""
        self._make_mgmt()
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.template_content import _reconcile_ospf

        res = _reconcile_ospf(self.device, self._payload([self._instance(router_id="")]))
        self.assertFalse(OSPFInstance.objects.filter(device=self.device).exists())
        self.assertEqual(res["instances"][0].status, "imported")

    def test_vrf_linked_when_present(self):
        self._make_mgmt()
        from ipam.models import VRF
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.template_content import _reconcile_ospf

        vrf = VRF.objects.create(name="ASPAN")
        _reconcile_ospf(self.device, self._payload([self._instance(process_id=100, vrf="ASPAN")]))
        self.assertEqual(OSPFInstance.objects.get(device=self.device, process_id=100).vrf, vrf)

    def test_vrf_not_created_when_toggle_off(self):
        """Unknown VRF + vrf_auto_create off → instance is global (vrf=None)."""
        self._make_mgmt()
        from ipam.models import VRF
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.models import AdapterConnection
        from netbox_nso_plugin.template_content import _reconcile_ospf

        AdapterConnection.objects.create(url="http://a:8000", enabled=True, vrf_auto_create=False)
        _reconcile_ospf(self.device, self._payload([self._instance(process_id=100, vrf="MTI")]))
        self.assertIsNone(OSPFInstance.objects.get(device=self.device, process_id=100).vrf)
        self.assertFalse(VRF.objects.filter(name="MTI").exists())

    def test_vrf_auto_created_when_toggle_on(self):
        """Unknown VRF + vrf_auto_create on → VRF created and linked."""
        self._make_mgmt()
        from ipam.models import VRF
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.models import AdapterConnection
        from netbox_nso_plugin.template_content import _reconcile_ospf

        AdapterConnection.objects.create(url="http://a:8000", enabled=True, vrf_auto_create=True)
        _reconcile_ospf(self.device, self._payload([self._instance(process_id=100, vrf="MTI")]))
        self.assertTrue(VRF.objects.filter(name="MTI").exists())
        self.assertEqual(OSPFInstance.objects.get(device=self.device, process_id=100).vrf.name, "MTI")

    def test_idempotent(self):
        self._make_mgmt()
        from netbox_routing.models import OSPFInstance

        from netbox_nso_plugin.template_content import _reconcile_ospf

        p = self._payload([self._instance()])
        _reconcile_ospf(self.device, p)
        _reconcile_ospf(self.device, p)
        self.assertEqual(OSPFInstance.objects.filter(device=self.device, process_id=10).count(), 1)

    # ── Interface fill ──────────────────────────────────────────────────────

    def test_creates_ospf_interface_with_knobs(self):
        self._make_mgmt()
        from netbox_routing.models import OSPFInterface

        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(
            self.device,
            self._payload(
                [self._instance()],
                [self._iface(name="Tunnel10", cost=750, network_type="point-to-point")],
            ),
        )
        x = OSPFInterface.objects.get(interface=self.tun)
        self.assertEqual(x.instance.process_id, "10")
        self.assertEqual(x.area.area_id, "0.0.0.0")
        self.assertEqual(x.cost, 750)
        self.assertEqual(x.network_type, "point-to-point")

    def test_resolve_ospf_area_equivalence(self):
        from netbox_routing.models import OSPFArea

        from netbox_nso_plugin.template_content import _resolve_ospf_area

        # Operator created the area as a bare integer; the device reports the dotted form.
        op = OSPFArea.objects.create(area_id="0", area_type="standard")
        resolved = _resolve_ospf_area(OSPFArea, "0.0.0.0")
        self.assertEqual(resolved.pk, op.pk)  # matched, not duplicated
        self.assertEqual(OSPFArea.objects.filter(area_id__in=["0", "0.0.0.0"]).count(), 1)
        # And the reverse: bare-int device value matches an existing dotted area.
        op2 = OSPFArea.objects.create(area_id="0.0.0.1", area_type="standard")
        self.assertEqual(_resolve_ospf_area(OSPFArea, "1").pk, op2.pk)

    def test_device_dotted_area_reuses_operator_bare_area(self):
        # Regression for the LAG99:99 'pending apply' bug: device reports area 0.0.0.0,
        # operator created area '0' — the reconcile must reuse it, not make a duplicate.
        self._make_mgmt()
        from netbox_routing.models import OSPFArea, OSPFInterface

        from netbox_nso_plugin.template_content import _reconcile_ospf

        area0 = OSPFArea.objects.create(area_id="0", area_type="standard")
        _reconcile_ospf(
            self.device,
            self._payload([self._instance()], [self._iface(name="Tunnel10", area_id="0.0.0.0")]),
        )
        x = OSPFInterface.objects.get(interface=self.tun)
        self.assertEqual(x.area.pk, area0.pk)
        self.assertEqual(OSPFArea.objects.filter(area_id__in=["0", "0.0.0.0"]).count(), 1)

    def test_invalid_network_type_and_cost_dropped(self):
        """Out-of-range cost and unknown network-type must not be written verbatim."""
        self._make_mgmt()
        from netbox_routing.models import OSPFInterface

        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(
            self.device,
            self._payload(
                [self._instance()],
                [self._iface(name="Tunnel10", cost=0, network_type="bogus")],
            ),
        )
        x = OSPFInterface.objects.get(interface=self.tun)
        self.assertIsNone(x.cost)
        self.assertIsNone(x.network_type)

    def test_interface_not_in_netbox_dropped(self):
        """An interface NSO reports but NetBox lacks is skipped, not crashed."""
        self._make_mgmt()
        from netbox_routing.models import OSPFInterface

        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(
            self.device,
            self._payload([self._instance()], [self._iface(name="Port-channel9.999")]),
        )
        self.assertFalse(OSPFInterface.objects.filter(instance__device=self.device).exists())

    def test_auth_type_mapped(self):
        self._make_mgmt()
        from netbox_routing.models import OSPFInterface

        from netbox_nso_plugin.template_content import _reconcile_ospf

        _reconcile_ospf(
            self.device,
            self._payload(
                [self._instance()],
                [self._iface(name="Tunnel10", auth_type="message-digest", auth_present=True)],
            ),
        )
        self.assertEqual(OSPFInterface.objects.get(interface=self.tun).authentication, "message-digest")
