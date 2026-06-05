# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for bfd_reconciler.reconcile_bfd — shared-profile dedup + BFDInterface."""

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase


def _make_device(suffix="bfd"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"BfdMfg{suffix}", slug=f"bfdmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"BfdDev{suffix}", slug=f"bfddev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"BfdRole{suffix}", slug=f"bfdrole{suffix}")
    site, _ = Site.objects.get_or_create(name=f"BfdSite{suffix}", slug=f"bfdsite{suffix}")
    return Device.objects.create(name=f"bfd-router-{suffix}", device_type=dt, role=role, site=site)


class TestReconcileBfd(TestCase):
    """Integration tests for reconcile_bfd() — real Django DB + netbox_routing."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("main")
        cls.ae1 = Interface.objects.create(device=cls.device, name="ae1", type="lag")
        cls.ae2 = Interface.objects.create(device=cls.device, name="ae2", type="lag")
        cls.ge0 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")

    def _entry(self, name, micro=True, tx=300, rx=300, mult=3, **kw):
        e = {"interface_name": name, "micro_bfd": micro, "enabled": True}
        if tx is not None:
            e["min_tx"] = tx
        if rx is not None:
            e["min_rx"] = rx
        if mult is not None:
            e["multiplier"] = mult
        e.update(kw)
        return e

    def test_shared_profile_deduped(self):
        """Two interfaces with the same timer-set link ONE shared BFDProfile."""
        from netbox_routing.models import BFDInterface, BFDProfile

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae1"), self._entry("ae2")])
        self.assertEqual(BFDProfile.objects.filter(name="bfd-300-300-x3").count(), 1)
        self.assertEqual(BFDInterface.objects.filter(interface__device=self.device).count(), 2)
        profiles = {b.bfd_profile_id for b in BFDInterface.objects.filter(interface__device=self.device)}
        self.assertEqual(len(profiles), 1)  # both share the one profile

    def test_micro_vs_normal_recorded(self):
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(
            self.device,
            [self._entry("ae1", micro=True), self._entry("GigabitEthernet0/0", micro=False, tx=750, rx=750, mult=4)],
        )
        self.assertTrue(BFDInterface.objects.get(interface=self.ae1).micro_bfd)
        self.assertFalse(BFDInterface.objects.get(interface=self.ge0).micro_bfd)

    def test_missing_timers_no_profile(self):
        """An entry without full timers still records the interface, with no profile."""
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae1", tx=None, rx=None, mult=None)])
        bi = BFDInterface.objects.get(interface=self.ae1)
        self.assertIsNone(bi.bfd_profile_id)
        self.assertTrue(bi.micro_bfd)

    def test_stale_pruned(self):
        """An interface that stops reporting BFD has its BFDInterface removed."""
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae1"), self._entry("ae2")])
        reconcile_bfd(self.device, [self._entry("ae1")])
        names = set(
            BFDInterface.objects.filter(interface__device=self.device).values_list("interface__name", flat=True)
        )
        self.assertEqual(names, {"ae1"})

    def test_unmodelled_interface_skipped(self):
        from netbox_routing.models import BFDInterface

        from netbox_nso_plugin.bfd_reconciler import reconcile_bfd

        reconcile_bfd(self.device, [self._entry("ae99")])  # not a NetBox interface
        self.assertEqual(BFDInterface.objects.filter(interface__device=self.device).count(), 0)
