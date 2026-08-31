# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""reconcile_lag_config → NSOLACPBundleState + NSOLACPMemberState."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from netbox_nso_plugin.lacp_reconciler import reconcile_lag_config
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOLACPBundleState,
    NSOLACPMemberState,
)

from ._outbox_case import content_update


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

    def test_owned_member_move_bumps_the_lacp_document(self):
        """A lag_bundle change affects the nested LACP document, not only the member row."""
        from netbox_nso_plugin.models import NSOIntentRevision

        second_lag = Interface.objects.create(device=self.device, name="Port-channel2", type="lag")
        reconcile_lag_config(
            self.device,
            _payload(
                [
                    self._bundle(
                        members=[{"interface_name": self.m1.name, "mode": "active"}],
                    )
                ]
            ),
        )
        bundle = NSOLACPBundleState.objects.get(interface=self.lag)
        member = NSOLACPMemberState.objects.get(interface=self.m1)
        bundle.status = "accepted"
        bundle.save(update_fields=["status"])
        member.status = "accepted"
        member.save(update_fields=["status"])
        before = NSOIntentRevision.objects.get(device=self.device, scope="lacp").revision

        moved = self._bundle(
            name=second_lag.name, lag_id=2, members=[{"interface_name": self.m1.name, "mode": "active"}]
        )
        reconcile_lag_config(self.device, _payload([self._bundle(), moved]))

        member.refresh_from_db()
        revision = NSOIntentRevision.objects.get(device=self.device, scope="lacp")
        assert member.lag_bundle_id == second_lag.pk
        assert revision.revision == before + 1

    def test_idempotent_second_reconcile(self):
        data = _payload([self._bundle(min_links=3)])
        reconcile_lag_config(self.device, data)
        reconcile_lag_config(self.device, data)
        assert NSOLACPBundleState.objects.filter(interface=self.lag).count() == 1

    def test_plan_query_count_does_not_grow_with_overlay_rows(self):
        from netbox_nso_plugin.lacp_reconciler import lacp_reconcile_plan

        one_member = _payload(
            [
                self._bundle(
                    members=[{"interface_name": "GigabitEthernet0/1", "mode": "active"}],
                )
            ]
        )
        two_members = _payload(
            [
                self._bundle(
                    members=[
                        {"interface_name": "GigabitEthernet0/1", "mode": "active"},
                        {"interface_name": "GigabitEthernet0/2", "mode": "active"},
                    ]
                )
            ]
        )
        reconcile_lag_config(self.device, one_member)

        with CaptureQueriesContext(connection) as one_member_queries:
            one_member_plan = lacp_reconcile_plan(self.device, one_member)
        reconcile_lag_config(self.device, two_members)
        with CaptureQueriesContext(connection) as two_member_queries:
            plan = lacp_reconcile_plan(self.device, two_members)

        self.assertEqual(len(two_member_queries), len(one_member_queries))
        self.assertFalse(one_member_plan.changes_content)
        self.assertFalse(plan.changes_content)

    def test_stale_bundle_planning_does_not_probe_each_interface(self):
        from netbox_nso_plugin.lacp_reconciler import lacp_reconcile_plan

        second = Interface.objects.create(device=self.device, name="Port-channel2", type="lag")
        third = Interface.objects.create(device=self.device, name="Port-channel3", type="lag")
        reconcile_lag_config(
            self.device,
            _payload(
                [
                    self._bundle(),
                    self._bundle(name=second.name, lag_id=2),
                    self._bundle(name=third.name, lag_id=3),
                ]
            ),
        )

        with CaptureQueriesContext(connection) as queries:
            lacp_reconcile_plan(self.device, _payload([]))

        lag_probes = [
            query["sql"]
            for query in queries
            if 'FROM "dcim_interface"' in query["sql"] and '"lag_id" =' in query["sql"]
        ]
        self.assertEqual(lag_probes, [])

    def test_missing_interface_skipped(self):
        reconcile_lag_config(self.device, _payload([{"name": "Port-channel99", "lag_id": 99, "members": []}]))
        assert NSOLACPBundleState.objects.count() == 0

    def test_operator_status_preserved_on_reread(self):
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        state = NSOLACPBundleState.objects.get(interface=self.lag)
        state.status = "accepted"
        state.save(update_fields=["status"])
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

    def test_stale_finalization_reloads_ownership_before_transition(self):
        from netbox_nso_plugin import status_machine as sm

        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        stale = NSOLACPBundleState.objects.get(interface=self.lag)
        content_update(NSOLACPBundleState.objects.get(pk=stale.pk), status="accepted")

        sm.finalise_stale_overlay(stale, vestigial=False)

        stale.refresh_from_db()
        self.assertEqual(stale.status, "accepted")

    def test_stale_finalization_retries_when_ownership_changes_rendered_content(self):
        from netbox_nso_plugin import status_machine as sm
        from netbox_nso_plugin.models import NSOIntentRevision

        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        stale = NSOLACPBundleState.objects.get(interface=self.lag)
        content_update(NSOLACPBundleState.objects.get(pk=stale.pk), status="in_sync")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="lacp")
        before = revision.revision

        sm.finalise_stale_overlay(stale, vestigial=False)

        stale.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(stale.status, "changed")
        self.assertEqual(revision.revision, before + 1)

    def test_stale_finalization_ignores_deletion_before_first_refresh(self):
        from netbox_nso_plugin import status_machine as sm

        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        stale = NSOLACPBundleState.objects.get(interface=self.lag)
        NSOLACPBundleState.objects.filter(pk=stale.pk).delete()

        sm.finalise_stale_overlay(stale, vestigial=False)

        self.assertFalse(NSOLACPBundleState.objects.filter(pk=stale.pk).exists())
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        self.assertTrue(NSOLACPBundleState.objects.filter(interface=self.lag).exists())

    def test_stale_finalization_ignores_deletion_before_retry_refresh(self):
        from netbox_nso_plugin import status_machine as sm

        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        stale = NSOLACPBundleState.objects.get(interface=self.lag)
        content_update(NSOLACPBundleState.objects.get(pk=stale.pk), status="in_sync")
        refreshes = 0
        deleting = False
        table = NSOLACPBundleState._meta.db_table

        def delete_before_retry_refresh(execute, sql, params, many, context):
            nonlocal deleting, refreshes
            if not deleting and f'FROM "{table}"' in sql and "LIMIT 21" in sql and "FOR UPDATE" not in sql:
                refreshes += 1
                if refreshes == 2:
                    deleting = True
                    NSOLACPBundleState.objects.filter(pk=stale.pk).delete()
            return execute(sql, params, many, context)

        with connection.execute_wrapper(delete_before_retry_refresh):
            sm.finalise_stale_overlay(stale, vestigial=False)

        self.assertEqual(refreshes, 2)
        self.assertFalse(NSOLACPBundleState.objects.filter(pk=stale.pk).exists())
        reconcile_lag_config(self.device, _payload([self._bundle(min_links=2)]))
        self.assertTrue(NSOLACPBundleState.objects.filter(interface=self.lag).exists())

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
