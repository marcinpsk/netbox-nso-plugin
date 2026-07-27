# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile_lag_config → NSOLACPBundleState + NSOLACPMemberState."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOLACPBundleState,
    NSOLACPMemberState,
)


def _payload(bundles):
    return {"device_id": 1, "bundles": bundles}


class TestReconcileLagConfig(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LagMfg", slug="lagmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LagDev", slug="lagdev")
        role = DeviceRole.objects.create(name="LagRole", slug="lagrole")
        site = Site.objects.create(name="LagSite", slug="lagsite")
        cls.device = Device.objects.create(name="lag-rtr", device_type=dt, role=role, site=site)
        cls.inst = NSOInstance.objects.create(name="lag-inst", adapter_instance_id="lag-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.inst, nso_device_name="lag-rtr"
        )
        cls.lag = Interface.objects.create(device=cls.device, name="Port-channel1", type="lag")
        cls.m1 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")
        cls.m2 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/2", type="1000base-t")

    def _bundle(self, **kw):
        b = {"name": "Port-channel1", "lag_id": 1, "members": []}
        b.update(kw)
        return b

    def test_creates_bundle_state(self):
        rows = reconcile_lag_config(
            self.device,
            _payload([self._bundle(min_links=2, system_priority=100, timer="fast")]),
        )
        assert len(rows) == 1
        state = NSOLACPBundleState.objects.get(management=self.mgmt, interface=self.lag)
        assert state.min_links == 2
        assert state.system_priority == 100
        assert state.timer == "fast"
        assert state.status == "imported"

    def test_stores_vpc_sensitive_flag(self):
        # NX-P2: a vPC-protected bundle carries vpc_sensitive=True (absent = ordinary). The
        # reconciler stores it so Accept can be gated and the push can exclude it.
        reconcile_lag_config(self.device, _payload([self._bundle(vpc_sensitive=True)]))
        state = NSOLACPBundleState.objects.get(management=self.mgmt, interface=self.lag)
        assert state.vpc_sensitive is True

    def test_ordinary_bundle_not_vpc_sensitive(self):
        reconcile_lag_config(self.device, _payload([self._bundle()]))  # no vpc_sensitive key
        state = NSOLACPBundleState.objects.get(management=self.mgmt, interface=self.lag)
        assert state.vpc_sensitive is False

    def test_creates_member_states(self):
        reconcile_lag_config(
            self.device,
            _payload(
                [
                    self._bundle(
                        members=[
                            {"interface_name": "GigabitEthernet0/1", "mode": "active", "port_priority": 128},
                            {"interface_name": "GigabitEthernet0/2", "mode": "active"},
                        ]
                    )
                ]
            ),
        )
        m1 = NSOLACPMemberState.objects.get(interface=self.m1)
        assert m1.mode == "active"
        assert m1.port_priority == 128
        assert m1.lag_bundle == self.lag
        m2 = NSOLACPMemberState.objects.get(interface=self.m2)
        assert m2.port_priority is None

    def test_idempotent_second_reconcile(self):
        data = _payload([self._bundle(min_links=3)])
        reconcile_lag_config(self.device, data)
        reconcile_lag_config(self.device, data)
        assert NSOLACPBundleState.objects.filter(interface=self.lag).count() == 1

    def test_missing_interface_skipped(self):
        reconcile_lag_config(self.device, _payload([{"name": "Port-channel99", "lag_id": 99, "members": []}]))
        assert NSOLACPBundleState.objects.count() == 0

    def test_operator_status_preserved_on_reread(self):
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        state = NSOLACPBundleState.objects.get(interface=self.lag)
        state.status = "accepted"
        state.save()
        # identical re-read must not revert accepted → imported
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        state.refresh_from_db()
        assert state.status == "accepted"

    def test_value_refreshed_on_reread(self):
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=5)]))
        state = NSOLACPBundleState.objects.get(interface=self.lag)
        assert state.min_links == 5
        assert state.status == "imported"

    def test_stale_bundle_husk_pruned(self):
        # A dropped bundle whose LAG interface has no members left is a vestigial husk
        # → pruned (no perpetual false drift), not left as a changed ghost row.
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        reconcile_lag_config(self.device, _payload([]))
        assert not NSOLACPBundleState.objects.filter(interface=self.lag).exists()

    def test_stale_bundle_with_members_marked_changed(self):
        # The LAG still carries real members (topology-reconciled) → genuine removal → drift.
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        self.m1.lag = self.lag
        self.m1.save(update_fields=["lag"])
        reconcile_lag_config(self.device, _payload([]))
        state = NSOLACPBundleState.objects.get(interface=self.lag)
        assert state.status == "changed"

    def test_stale_owned_bundle_husk_preserved(self):
        # An owned (accepted) row is never pruned, even as a husk.
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        state = NSOLACPBundleState.objects.get(interface=self.lag)
        state.status = "accepted"
        state.save(update_fields=["status"])
        reconcile_lag_config(self.device, _payload([]))
        state.refresh_from_db()
        assert state.status == "accepted"

    def test_stale_member_unbundled_pruned(self):
        # A dropped member whose interface is no longer assigned to any LAG is vestigial.
        reconcile_lag_config(
            self.device,
            _payload([self._bundle(members=[{"interface_name": "GigabitEthernet0/1", "mode": "active"}])]),
        )
        reconcile_lag_config(self.device, _payload([self._bundle(members=[])]))
        assert not NSOLACPMemberState.objects.filter(interface=self.m1).exists()

    def test_stale_member_still_bundled_marked_changed(self):
        reconcile_lag_config(
            self.device,
            _payload([self._bundle(members=[{"interface_name": "GigabitEthernet0/1", "mode": "active"}])]),
        )
        self.m1.lag = self.lag
        self.m1.save(update_fields=["lag"])
        reconcile_lag_config(self.device, _payload([self._bundle(members=[])]))
        state = NSOLACPMemberState.objects.get(interface=self.m1)
        assert state.status == "changed"

    def test_no_management_returns_empty(self):
        other_device = Device.objects.create(
            name="no-mgmt",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        assert reconcile_lag_config(other_device, _payload([self._bundle()])) == []
