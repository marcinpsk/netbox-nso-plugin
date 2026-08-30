# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""plugin dot1q subinterface reconciler — virtual interface + parent link + overlay."""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase
from ipam.models import VLAN

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOSubinterfaceState

from ._outbox_case import content_update, mirror_update
from .mixins import IntentPushResetMixin


def _make_device(tag="m36"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"UMfg{tag}", slug=f"umfg{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"UDev{tag}", slug=f"udev{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"URole{tag}", slug=f"urole{tag}")
    site, _ = Site.objects.get_or_create(name=f"USite{tag}", slug=f"usite{tag}")
    return Device.objects.create(name=f"rtr-{tag}", device_type=dt, role=role, site=site)


class TestSubinterfaceReconciler(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="nso-dev", adapter_instance_id="nso-dev")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="rtr-m36"
        )
        cls.parent = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        orphan = _make_device("orphan")
        assert (
            reconcile_subinterface(orphan, {"interfaces": [{"interface_name": "Gi0/1.100", "dot1q_vlan": 100}]}) == []
        )

    def test_reconcile_preflights_native_and_overlay_creations(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.subinterface_reconciler import subinterface_reconcile_plan

        plan = subinterface_reconcile_plan(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.1627",
                        "parent_interface": self.parent.name,
                        "dot1q_vlan": 1627,
                    }
                ]
            },
        )

        self.assertIsInstance(plan, RendererMutationPlan)
        self.assertEqual(
            [(write.operation, write.model_label) for write in plan.write_set],
            [
                ("save", "dcim.interface"),
                ("save", "netbox_nso_plugin.nsosubinterfacestate"),
            ],
        )

    def test_creates_subinterface_with_parent_and_records_dot1q(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        rows = reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.100",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 100,
                        "type": "subinterface",
                        "vrf": "TENANT_A",
                    }
                ]
            },
        )
        self.assertEqual(len(rows), 1)
        sub = Interface.objects.get(device=self.device, name="GigabitEthernet0/1.100")
        self.assertEqual(sub.type, "virtual")
        self.assertEqual(sub.parent_id, self.parent.id)
        self.assertEqual(rows[0].dot1q_vlan, 100)
        self.assertEqual(rows[0].vrf, "TENANT_A")
        self.assertEqual(rows[0].status, "imported")
        # A dot1q tag must NOT create a device VLAN object.
        self.assertEqual(VLAN.objects.count(), 0)

    def test_duplicate_interface_entries_use_the_first_observation(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        rows = reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.101",
                        "parent_interface": self.parent.name,
                        "dot1q_vlan": 101,
                    },
                    {
                        "interface_name": "GigabitEthernet0/1.101",
                        "parent_interface": self.parent.name,
                        "dot1q_vlan": 202,
                    },
                ]
            },
        )

        self.assertEqual(len(rows), 1)
        state = NSOSubinterfaceState.objects.get(interface__name="GigabitEthernet0/1.101")
        self.assertEqual(state.dot1q_vlan, 101)

    def test_unowned_existing_subinterface_tracks_a_new_parent(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        new_parent = Interface.objects.create(
            device=self.device,
            name="GigabitEthernet0/2",
            type="1000base-t",
        )
        interface = Interface.objects.create(
            device=self.device,
            name="GigabitEthernet0/1.200",
            type="virtual",
            parent=self.parent,
        )
        NSOSubinterfaceState.objects.create(
            management=self.management,
            interface=interface,
            parent_interface=self.parent,
            dot1q_vlan=200,
            status="imported",
        )

        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": interface.name,
                        "parent_interface": new_parent.name,
                        "dot1q_vlan": 200,
                    }
                ]
            },
        )

        interface.refresh_from_db()
        self.assertEqual(interface.parent_id, new_parent.pk)

    def test_a_concurrently_created_subinterface_is_reused(self):
        from django.db import connection

        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        inserted = None

        def insert_after_interface_read(execute, sql, params, many, context):
            nonlocal inserted
            result = execute(sql, params, many, context)
            if inserted is None and sql.lstrip().upper().startswith("SELECT") and Interface._meta.db_table in sql:
                inserted = Interface.objects.create(
                    device=self.device,
                    name="GigabitEthernet0/1.101",
                    type="virtual",
                    parent=self.parent,
                )
            return result

        with connection.execute_wrapper(insert_after_interface_read):
            rows = reconcile_subinterface(
                self.device,
                {
                    "interfaces": [
                        {
                            "interface_name": "GigabitEthernet0/1.101",
                            "parent_interface": self.parent.name,
                            "dot1q_vlan": 101,
                        }
                    ]
                },
            )

        self.assertEqual(rows[0].interface_id, inserted.pk)
        self.assertEqual(
            Interface.objects.filter(device=self.device, name="GigabitEthernet0/1.101").count(),
            1,
        )

    def test_direct_reconcile_does_not_advance_intent_revision(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        revision, _ = NSOIntentRevision.objects.get_or_create(
            device=self.device,
            scope="subinterface",
        )
        before = revision.revision

        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.1623",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 1623,
                        "type": "subinterface",
                    }
                ]
            },
        )

        revision.refresh_from_db()
        self.assertEqual(revision.revision, before)

    def test_missing_parent_creates_subif_without_parent_flagged_changed(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        rows = reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet9/9.300",
                        "parent_interface": "GigabitEthernet9/9",
                        "dot1q_vlan": 300,
                        "type": "subinterface",
                    }
                ]
            },
        )
        sub = Interface.objects.get(device=self.device, name="GigabitEthernet9/9.300")
        self.assertIsNone(sub.parent_id)
        self.assertEqual(rows[0].status, "changed")  # missing parent flagged for review

    def test_missing_parent_self_heals_when_parent_appears(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        payload = {
            "interfaces": [
                {
                    "interface_name": "GigabitEthernet5/5.100",
                    "parent_interface": "GigabitEthernet5/5",
                    "dot1q_vlan": 100,
                    "type": "subinterface",
                }
            ]
        }
        rows = reconcile_subinterface(self.device, payload)
        self.assertEqual(rows[0].status, "changed")  # parent absent

        # Parent shows up (e.g. via a later device sync) → next reconcile self-heals.
        Interface.objects.create(device=self.device, name="GigabitEthernet5/5", type="1000base-t")
        rows = reconcile_subinterface(self.device, payload)
        self.assertEqual(rows[0].status, "imported")
        self.assertIsNotNone(rows[0].parent_interface_id)

    def test_existing_interface_is_reused_not_duplicated(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        Interface.objects.create(device=self.device, name="GigabitEthernet0/1.200", type="virtual")
        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.200",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 200,
                        "type": "subinterface",
                    }
                ]
            },
        )
        self.assertEqual(Interface.objects.filter(device=self.device, name="GigabitEthernet0/1.200").count(), 1)

    def test_a_concurrently_created_subinterface_is_reused(self):
        from django.db import connection

        from netbox_nso_plugin.intent_state import reconcile_transaction
        from netbox_nso_plugin.subinterface_reconciler import (
            _reconcile_subinterface,
            subinterface_reconcile_plan,
        )

        payload = {
            "interfaces": [
                {
                    "interface_name": "GigabitEthernet0/1.201",
                    "parent_interface": self.parent.name,
                    "dot1q_vlan": 201,
                }
            ]
        }
        plan = subinterface_reconcile_plan(self.device, payload)
        inserted = None

        def insert_after_interface_read(execute, sql, params, many, context):
            nonlocal inserted
            result = execute(sql, params, many, context)
            if inserted is None and sql.lstrip().upper().startswith("SELECT") and Interface._meta.db_table in sql:
                inserted = Interface.objects.create(
                    device=self.device,
                    name="GigabitEthernet0/1.201",
                    type="virtual",
                    parent=self.parent,
                )
            return result

        with reconcile_transaction(plan), connection.execute_wrapper(insert_after_interface_read):
            rows = _reconcile_subinterface(self.device, payload)

        self.assertIsNotNone(inserted, "the wrapper never reached the interface read seam")
        self.assertEqual(rows[0].interface_id, inserted.pk)
        self.assertEqual(
            Interface.objects.filter(device=self.device, name="GigabitEthernet0/1.201").count(),
            1,
        )

    def test_stale_state_pruned(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.300",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 300,
                    }
                ]
            },
        )
        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1.301",
                        "parent_interface": "GigabitEthernet0/1",
                        "dot1q_vlan": 301,
                    }
                ]
            },
        )
        names = set(
            NSOSubinterfaceState.objects.filter(management=self.management).values_list("interface__name", flat=True)
        )
        self.assertEqual(names, {"GigabitEthernet0/1.301"})


class TestSubinterfaceWritePath(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("wp")
        cls.instance = NSOInstance.objects.create(name="nso-wp", adapter_instance_id="nso-wp")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="rtr-wp", adapter_device_id=42
        )
        cls.parent = Interface.objects.create(device=cls.device, name="ge-0/0/0", type="1000base-t")

    def _state(self, name="ge-0/0/0.100", dot1q=100, status="imported"):
        from uuid import uuid4

        iface = Interface.objects.create(device=self.device, name=name, type="virtual", parent=self.parent)
        return NSOSubinterfaceState.objects.create(
            management=self.management,
            interface=iface,
            parent_interface=self.parent,
            dot1q_vlan=dot1q,
            vrf="MTI",
            status=status,
            apply_attempt_id=uuid4() if status == "deploying" else None,
        )

    def test_reconcile_preserves_owned_status(self):
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        reconcile_subinterface(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "ge-0/0/0.100",
                        "parent_interface": "ge-0/0/0",
                        "dot1q_vlan": 100,
                        "type": "subinterface",
                    }
                ]
            },
        )
        self.assertEqual(NSOSubinterfaceState.objects.get(interface__name="ge-0/0/0.100").status, "accepted")

    def test_foreign_overlay_save_does_not_schedule_subinterface_behavior(self):
        from unittest.mock import patch

        state = self._state(name="ge-0/0/0.1627", dot1q=1627, status="accepted")

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule:
            state.vrf = "FOREIGN"
            state.save(update_fields=("vrf",))

        schedule.assert_not_called()

    def test_foreign_native_creation_does_not_acquire_subinterface_ownership(self):
        interface = Interface.objects.create(
            device=self.device,
            name="ge-0/0/0.1628",
            type="virtual",
            parent=self.parent,
        )

        self.assertFalse(NSOSubinterfaceState.objects.filter(interface=interface).exists())

    def test_reconcile_preserves_owned_values_until_the_device_matches(self):
        """A refresh must compare device values without replacing pending NetBox intent."""
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        state = self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        state = content_update(state, dot1q_vlan=200, vrf="CUSTOMER")
        old_device = {
            "interfaces": [
                {
                    "interface_name": "ge-0/0/0.100",
                    "parent_interface": "ge-0/0/0",
                    "dot1q_vlan": 100,
                    "type": "subinterface",
                    "vrf": "MTI",
                }
            ]
        }

        reconcile_subinterface(self.device, old_device)

        state.refresh_from_db()
        self.assertEqual((state.dot1q_vlan, state.vrf), (200, "CUSTOMER"))
        self.assertEqual(state.status, "accepted")

        desired_device = {
            "interfaces": [
                {
                    "interface_name": "ge-0/0/0.100",
                    "parent_interface": "ge-0/0/0",
                    "dot1q_vlan": 200,
                    "type": "subinterface",
                    "vrf": "CUSTOMER",
                }
            ]
        }
        reconcile_subinterface(self.device, desired_device)
        state.refresh_from_db()
        self.assertEqual((state.dot1q_vlan, state.vrf), (200, "CUSTOMER"))
        self.assertEqual(state.status, "in_sync")

    def test_owned_state_survives_when_interface_drops_from_payload(self):
        """An owned subinterface overlay must NOT be hard-deleted when the device stops reporting it.

        NSOSubinterfaceState is in ``_APPLY_DEPLOYING_SCOPES``; a bulk delete of stale rows
        destroys ownership. A confirmed row that vanishes surfaces as ``changed``. That
        rendered-membership change advances the scope revision, so another deploying row is
        re-pended to ``accepted`` and loses its superseded attempt identity.
        """
        from uuid import uuid4

        from netbox_nso_plugin.models import NSOIntentRevision, NSOSubinterfaceState
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        deploying = self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        confirmed = self._state(name="ge-0/0/0.200", dot1q=200, status="in_sync")
        attempt = uuid4()
        # Marked LAST, and lifecycle-only: creating a sibling row re-pends a deploying one.
        mirror_update(deploying, status="deploying", apply_attempt_id=attempt)
        revision = NSOIntentRevision.objects.get(device=self.device, scope="subinterface")
        before = revision.revision
        deploying.refresh_from_db()
        assert (deploying.status, deploying.apply_attempt_id) == ("deploying", attempt)

        reconcile_subinterface(self.device, {"interfaces": []})  # device stops reporting all subifs

        assert NSOSubinterfaceState.objects.filter(pk=deploying.pk).exists(), (
            "deploying (apply-in-flight) overlay deleted"
        )
        assert NSOSubinterfaceState.objects.filter(pk=confirmed.pk).exists(), "in_sync overlay deleted"
        deploying.refresh_from_db()
        revision.refresh_from_db()
        assert deploying.status == "accepted"
        assert deploying.apply_attempt_id is None
        assert NSOSubinterfaceState.objects.get(pk=confirmed.pk).status == "changed"
        assert revision.revision == before + 1  # the vanished confirmed subinterface is a content change

    def _payload(self, name, dot1q):
        """The device read that exactly matches a fixture row's parent, dot1q and VRF."""
        return {
            "interfaces": [
                {
                    "interface_name": name,
                    "parent_interface": "ge-0/0/0",
                    "dot1q_vlan": dot1q,
                    "type": "subinterface",
                    "vrf": "MTI",
                }
            ]
        }

    def test_matching_read_keeps_a_deploying_row_in_flight(self):
        """A matching read is not apply evidence: only a correlated apply result settles it."""
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        deploying = self._state(name="ge-0/0/0.100", dot1q=100, status="deploying")
        attempt = deploying.apply_attempt_id
        assert attempt is not None

        reconcile_subinterface(self.device, self._payload("ge-0/0/0.100", 100))

        deploying.refresh_from_db()
        assert deploying.status == "deploying"
        assert deploying.apply_attempt_id == attempt

    def test_a_vanished_confirmed_sibling_repends_a_deploying_row(self):
        """A vanished confirmed sibling bears content, so every deploying row in the scope is stale.

        The in-flight row is still reported, carrying the pre-apply dot1q the device still
        holds, so only the deferred re-pend can move it off ``deploying``.
        """
        from uuid import uuid4

        from netbox_nso_plugin.models import NSOIntentRevision, NSOSubinterfaceState
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        deploying = self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        confirmed = self._state(name="ge-0/0/0.200", dot1q=200, status="in_sync")
        attempt = uuid4()
        # Marked LAST, and lifecycle-only: creating a sibling row re-pends a deploying one.
        mirror_update(deploying, status="deploying", apply_attempt_id=attempt)
        revision = NSOIntentRevision.objects.get(device=self.device, scope="subinterface")
        before = revision.revision
        deploying.refresh_from_db()
        assert (deploying.status, deploying.apply_attempt_id) == ("deploying", attempt)

        reconcile_subinterface(self.device, self._payload("ge-0/0/0.100", 999))  # ge-0/0/0.200 vanishes

        deploying.refresh_from_db()
        revision.refresh_from_db()
        assert deploying.status == "accepted"
        assert deploying.apply_attempt_id is None
        assert NSOSubinterfaceState.objects.get(pk=confirmed.pk).status == "changed"
        assert revision.revision == before + 1

    def test_a_vanished_confirmed_sibling_repends_a_matching_deploying_row_after_the_read(self):
        """The deferred re-pend runs after the read's transitions, so a matching entry never settles here.

        The transition fence leaves the in-flight row ``deploying``, then the deferred re-pend
        at the end of the reconcile transaction moves it to ``accepted`` with no attempt.
        """
        from uuid import uuid4

        from netbox_nso_plugin.models import NSOIntentRevision, NSOSubinterfaceState
        from netbox_nso_plugin.subinterface_reconciler import reconcile_subinterface

        deploying = self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        confirmed = self._state(name="ge-0/0/0.200", dot1q=200, status="in_sync")
        attempt = uuid4()
        # Marked LAST, and lifecycle-only: creating a sibling row re-pends a deploying one.
        mirror_update(deploying, status="deploying", apply_attempt_id=attempt)
        revision = NSOIntentRevision.objects.get(device=self.device, scope="subinterface")
        before = revision.revision
        deploying.refresh_from_db()
        assert (deploying.status, deploying.apply_attempt_id) == ("deploying", attempt)

        reconcile_subinterface(self.device, self._payload("ge-0/0/0.100", 100))  # ge-0/0/0.200 vanishes

        deploying.refresh_from_db()
        revision.refresh_from_db()
        assert deploying.status == "accepted"
        assert deploying.apply_attempt_id is None
        assert NSOSubinterfaceState.objects.get(pk=confirmed.pk).status == "changed"
        assert revision.revision == before + 1

    def test_push_builds_owned_snapshot(self):
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.signals import reset_intent_push_state

        self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        self._state(name="ge-0/0/0.200", dot1q=200, status="imported")  # not owned → excluded
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent") as mock_put:
            deliver("subinterface", self.device.pk, 42)
        mock_put.assert_called_once()
        ifaces = mock_put.call_args[0][1]
        assert [i["interface_name"] for i in ifaces] == ["ge-0/0/0.100"]
        assert ifaces[0]["dot1q_vlan"] == 100
        assert ifaces[0]["parent_interface"] == "ge-0/0/0"
        assert ifaces[0]["vrf"] == "MTI"

    def test_push_skips_rows_without_dot1q(self):
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.signals import reset_intent_push_state

        self._state(name="ge-0/0/0.100", dot1q=100, status="accepted")
        # Owned but no dot1q tag → the reconciler can't key it; must be excluded.
        self._state(name="ge-0/0/0.110", dot1q=None, status="accepted")
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent") as mock_put:
            deliver("subinterface", self.device.pk, 42)
        mock_put.assert_called_once()
        ifaces = mock_put.call_args[0][1]
        assert [i["interface_name"] for i in ifaces] == ["ge-0/0/0.100"]

    def test_accept_marks_owned(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision, NSOOwnershipManifest

        state = self._state(name="ge-0/0/0.300", dot1q=300, status="conflict")
        User = get_user_model()
        admin = User.objects.create_superuser(username="subif-admin", password="pw", email="s@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent"):
            resp = self.client.post(f"/plugins/nso/subinterface/state/{state.pk}/accept/")
        assert resp.status_code == 302
        state.refresh_from_db()
        assert state.status == "accepted" and state.accepted_at is not None
        revision = NSOIntentRevision.objects.get(device=self.device, scope="subinterface")
        assert revision.verified_revision == revision.revision
        assert revision.verified_fingerprint == delivery.canonical_fingerprint(
            delivery.render("subinterface", self.device.pk, self.management.adapter_device_id).payload
        )
        assert NSOOwnershipManifest.objects.filter(
            device=self.device,
            scope="subinterface",
            native_model_label="dcim.interface",
            native_key={"device_id": state.interface.device_id, "name": state.interface.name},
            ownership_state="owned",
        ).exists()

    def test_accept_refuses_a_row_the_push_snapshot_would_skip(self):
        from django.contrib.auth import get_user_model

        state = self._state(name="ge-0/0/0.400", dot1q=None, status="conflict")
        original_accepted_at = state.accepted_at
        User = get_user_model()
        admin = User.objects.create_superuser(
            username="subif-block-admin",
            password="pw",  # noqa: S106
            email="blocked@test.example",
        )
        self.client.force_login(admin)

        response = self.client.post(f"/plugins/nso/subinterface/state/{state.pk}/accept/")

        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "conflict")
        self.assertEqual(state.accepted_at, original_accepted_at)
