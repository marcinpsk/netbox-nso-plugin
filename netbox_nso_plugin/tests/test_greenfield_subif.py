# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Foreign greenfield routed sub-interface writes stay outside NSO ownership.

Only an explicit sub-interface workflow may acquire renderer ownership. A generic native
Interface create is not ownership evidence and must not create an overlay as a side effect.
"""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOSubinterfaceState

from ._ownership_case import acquire_overlay
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

        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        self.assertFalse(NSOSubinterfaceState.objects.filter(interface=subif).exists())

    def test_unit_zero_switching_children_do_not_become_subinterfaces(self):
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        for index in range(3):
            parent = Interface.objects.create(device=self.device, name=f"xe-0/0/{index}", type="1000base-t")
            Interface.objects.create(
                device=self.device,
                name=f"xe-0/0/{index}.0",
                type="virtual",
                parent=parent,
                mode="access",
            )
        loopback = Interface.objects.create(device=self.device, name="lo0", type="virtual")
        Interface.objects.create(device=self.device, name="lo0.0", type="virtual", parent=loopback)

        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        self.assertFalse(NSOSubinterfaceState.objects.filter(management=self.mgmt).exists())

    def test_switchport_replay_does_not_fabricate_subinterfaces(self):
        from netbox_nso_plugin.delivery import render
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        for index in range(59):
            parent = Interface.objects.create(device=self.device, name=f"xe-0/2/{index}", type="1000base-t")
            child = Interface.objects.create(
                device=self.device,
                name=f"xe-0/2/{index}.0",
                type="virtual",
                parent=parent,
                mode="access",
            )
            acquire_overlay(NSOSwitchportState, management=self.mgmt, interface=child, mode="access", status="accepted")
        loopback = Interface.objects.create(device=self.device, name="lo0", type="virtual")
        Interface.objects.create(device=self.device, name="lo0.0", type="virtual", parent=loopback)
        true_states = []
        for parent_name in ("ae50", "ae99"):
            parent = (
                self.parent
                if parent_name == "ae99"
                else Interface.objects.create(device=self.device, name=parent_name, type="lag")
            )
            child = Interface.objects.create(
                device=self.device, name=f"{parent_name}.99", type="virtual", parent=parent
            )
            true_states.append(
                acquire_overlay(
                    NSOSubinterfaceState,
                    management=self.mgmt,
                    interface=child,
                    parent_interface=parent,
                    dot1q_vlan=99,
                    status="accepted",
                )
            )
        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        unit_zero_names = {f"xe-0/2/{index}.0" for index in range(59)} | {"lo0.0"}
        self.assertEqual(
            Interface.objects.filter(device=self.device, name__in=unit_zero_names).count(),
            60,
        )
        self.assertEqual(NSOSubinterfaceState.objects.filter(management=self.mgmt).count(), 2)
        self.assertFalse(
            NSOSubinterfaceState.objects.filter(management=self.mgmt, interface__name__in=unit_zero_names).exists()
        )
        self.assertEqual(
            set(NSOSubinterfaceState.objects.filter(management=self.mgmt).values_list("interface__name", flat=True)),
            {"ae50.99", "ae99.99"},
        )
        for state in true_states:
            state.refresh_from_db()
            self.assertEqual(state.status, "accepted")
        self.assertEqual(
            {
                row["interface_name"]
                for row in render("subinterface", self.device.pk, self.mgmt.adapter_device_id).payload
            },
            {"ae50.99", "ae99.99"},
        )

    def test_physical_interface_creates_no_subif_state(self):
        """A plain interface (no dot1q suffix) must NOT be treated as a subinterface."""
        Interface.objects.create(device=self.device, name="ae100", type="lag")
        self.assertFalse(NSOSubinterfaceState.objects.filter(interface__name="ae100").exists())

    def test_explicit_tag_survives_repeated_audits_and_renders(self):
        from netbox_nso_plugin.delivery import render
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        child = Interface.objects.create(device=self.device, name="ae99.5000", type="virtual", parent=self.parent)
        state = NSOSubinterfaceState.objects.create(
            management=self.mgmt, interface=child, parent_interface=self.parent, dot1q_vlan=100, status="imported"
        )
        self.client.force_login(get_user_model().objects.create_user("subif-import-admin", is_superuser=True))
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent"):
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:subinterface_accept", kwargs={"pk": state.pk})
            )
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")
        self.assertIsNotNone(state.accepted_at)
        accepted_status = state.status
        for _ in range(2):
            reconcile_scope_ownership(self.device.pk, ["subinterface"])
            state.refresh_from_db()
            self.assertEqual(state.status, accepted_status)
        self.assertEqual(
            render("subinterface", self.device.pk, self.mgmt.adapter_device_id).payload,
            [
                {
                    "interface_name": "ae99.5000",
                    "parent_interface": "ae99",
                    "dot1q_vlan": 100,
                    "type": "subinterface",
                    "vrf": "",
                }
            ],
        )

    def test_imported_ios_matching_unit_and_tag_remains_owned(self):
        from netbox_nso_plugin.delivery import render
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        parent = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t")
        child = Interface.objects.create(device=self.device, name="Ethernet2.1724", type="virtual", parent=parent)
        state = NSOSubinterfaceState.objects.create(
            management=self.mgmt, interface=child, parent_interface=parent, dot1q_vlan=1724, status="imported"
        )
        self.client.force_login(get_user_model().objects.create_user("subif-ios-admin", is_superuser=True))
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent"):
            self.client.post(reverse("plugins:netbox_nso_plugin:subinterface_accept", kwargs={"pk": state.pk}))
        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(
            render("subinterface", self.device.pk, self.mgmt.adapter_device_id).payload[0]["dot1q_vlan"], 1724
        )

    def test_bad_owned_tag_blocks_entire_snapshot_until_repaired(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.delivery import deliver, render

        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        state = acquire_overlay(
            NSOSubinterfaceState,
            management=self.mgmt,
            interface=child,
            parent_interface=self.parent,
            dot1q_vlan=0,
            status="accepted",
        )
        rendered = render("subinterface", self.device.pk, self.mgmt.adapter_device_id)
        self.assertIn("dot1q_vlan", rendered.payload["blocked"][0])
        with patch("netbox_nso_plugin.adapter_client.put_subinterface_intent") as put:
            with self.assertRaisesRegex(AdapterError, "gf-sw01 ae99.7"):
                deliver("subinterface", self.device.pk, self.mgmt.adapter_device_id)
            put.assert_not_called()
        NSOSubinterfaceState.objects.filter(pk=state.pk).update(dot1q_vlan=100)
        self.assertEqual(
            render("subinterface", self.device.pk, self.mgmt.adapter_device_id).payload[0]["dot1q_vlan"], 100
        )

    def test_overlay_parent_mismatch_blocks_snapshot_without_retracting(self):
        from netbox_nso_plugin.delivery import render
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership

        other = Interface.objects.create(device=self.device, name="ae50", type="lag")
        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        state = acquire_overlay(
            NSOSubinterfaceState,
            management=self.mgmt,
            interface=child,
            parent_interface=self.parent,
            dot1q_vlan=100,
            status="accepted",
        )
        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        NSOSubinterfaceState.objects.filter(pk=state.pk).update(parent_interface=other)
        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        self.assertEqual(
            NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="subinterface").ownership_state,
            "owned",
        )
        self.assertIn(
            "parent_interface",
            render("subinterface", self.device.pk, self.mgmt.adapter_device_id).payload["blocked"][0],
        )
        NSOSubinterfaceState.objects.filter(pk=state.pk).update(parent_interface=self.parent)
        self.assertEqual(len(render("subinterface", self.device.pk, self.mgmt.adapter_device_id).payload), 1)

    def test_owned_child_with_stale_switchport_parent_stays_in_snapshot(self):
        from netbox_nso_plugin.delivery import render
        from netbox_nso_plugin.models import NSOSwitchportState

        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        acquire_overlay(
            NSOSubinterfaceState,
            management=self.mgmt,
            interface=child,
            parent_interface=self.parent,
            dot1q_vlan=100,
            status="accepted",
        )
        Interface.objects.filter(pk=self.parent.pk).update(mode="access")
        rendered = render("subinterface", self.device.pk, self.mgmt.adapter_device_id)
        self.assertEqual(rendered.payload[0]["interface_name"], "ae99.7")
        Interface.objects.filter(pk=self.parent.pk).update(mode=None)
        NSOSwitchportState.objects.create(management=self.mgmt, interface=self.parent, status="imported")
        rendered = render("subinterface", self.device.pk, self.mgmt.adapter_device_id)
        self.assertEqual(rendered.payload[0]["interface_name"], "ae99.7")

    def test_create_subinterface_writes_native_and_owned_manifest(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.subinterface_create import create_subinterface

        state = create_subinterface(self.mgmt, self.parent, 7, 100, "")
        self.assertEqual(state.interface.name, "ae99.7")
        self.assertEqual(state.interface.parent, self.parent)
        self.assertEqual(state.dot1q_vlan, 100)
        self.assertEqual(state.status, "accepted")
        self.assertEqual(
            NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="subinterface").ownership_state,
            "owned",
        )

    def test_create_view_accepts_explicit_ios_and_junos_unit_tag(self):
        from netbox_nso_plugin.delivery import render

        self.client.force_login(get_user_model().objects.create_user("subif-admin", is_superuser=True))
        url = reverse("plugins:netbox_nso_plugin:subinterface_add", kwargs={"device_pk": self.device.pk})
        ios = Interface.objects.create(device=self.device, name="GigabitEthernet0/1", type="1000base-t")
        for parent, unit, tag in ((ios, 300, 300), (self.parent, 7, 100)):
            response = self.client.post(url, {"parent": parent.pk, "unit": unit, "dot1q_vlan": tag, "vrf": ""})
            self.assertEqual(response.status_code, 302)
        names = {
            item["interface_name"]: item["dot1q_vlan"]
            for item in render("subinterface", self.device.pk, self.mgmt.adapter_device_id).payload
        }
        self.assertEqual(names, {"GigabitEthernet0/1.300": 300, "ae99.7": 100})

    def test_create_view_rejects_subinterface_and_svi_parents(self):
        self.client.force_login(get_user_model().objects.create_user("subif-parent-admin", is_superuser=True))
        url = reverse("plugins:netbox_nso_plugin:subinterface_add", kwargs={"device_pk": self.device.pk})
        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        svi = Interface.objects.create(device=self.device, name="Vlan10", type="virtual")
        for parent, message in (
            (child, "A subinterface cannot be a subinterface parent."),
            (svi, "An SVI cannot be a subinterface parent."),
        ):
            with self.subTest(parent=parent.name):
                response = self.client.post(url, {"parent": parent.pk, "unit": 5, "dot1q_vlan": 200, "vrf": ""})
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, response.context["form"].errors["parent"])
        self.assertFalse(Interface.objects.filter(device=self.device, name__in=("ae99.7.5", "Vlan10.5")).exists())
        self.assertFalse(NSOSubinterfaceState.objects.filter(management=self.mgmt).exists())

    def test_create_rejects_bad_tag_unit_and_parent(self):
        from netbox_nso_plugin.subinterface_create import create_subinterface

        for tag in (None, 0, 4095):
            with self.subTest(tag=tag), self.assertRaises(ValidationError):
                create_subinterface(self.mgmt, self.parent, 9, tag, "")
        for parent in (
            Interface.objects.create(device=self.device, name="lo0", type="virtual"),
            Interface.objects.create(device=self.device, name="irb", type="virtual"),
            Interface.objects.create(device=self.device, name="xe-0/0/1", type="1000base-t", mode="access"),
        ):
            with self.subTest(parent=parent.name), self.assertRaises(ValidationError):
                create_subinterface(self.mgmt, parent, 9, 100, "")
        self.assertFalse(NSOSubinterfaceState.objects.filter(management=self.mgmt).exists())

    def test_create_refuses_existing_unit_and_switchport_overlay_parent(self):
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.subinterface_create import create_subinterface

        create_subinterface(self.mgmt, self.parent, 7, 100, "")
        with self.assertRaises(ValidationError):
            create_subinterface(self.mgmt, self.parent, 7, 200, "")
        switchport = Interface.objects.create(device=self.device, name="xe-0/0/1", type="1000base-t")
        NSOSwitchportState.objects.create(management=self.mgmt, interface=switchport, status="imported")
        with self.assertRaises(ValidationError):
            create_subinterface(self.mgmt, switchport, 9, 200, "")

    def test_validator_rejects_bad_name_parent_and_duplicate_tag(self):
        from netbox_nso_plugin.subinterface_identity import subinterface_errors

        child = Interface.objects.create(device=self.device, name="ae99.7", type="virtual", parent=self.parent)
        state = NSOSubinterfaceState.objects.create(
            management=self.mgmt, interface=child, parent_interface=self.parent, dot1q_vlan=100, status="imported"
        )
        other = Interface.objects.create(device=self.device, name="ae99.8", type="virtual", parent=self.parent)
        duplicate = NSOSubinterfaceState(
            management=self.mgmt, interface=other, parent_interface=self.parent, dot1q_vlan=100
        )
        self.assertIn("dot1q_vlan", subinterface_errors(duplicate))
        child.name = "ae99"
        self.assertIn("interface", subinterface_errors(state))
        child.name = "xe-0/0/1.7"
        self.assertIn("interface", subinterface_errors(state))
        unlinked = Interface(device=self.device, name="ae99.9", type="virtual")
        proposed = NSOSubinterfaceState(
            management=self.mgmt, interface=unlinked, parent_interface=self.parent, dot1q_vlan=200
        )
        self.assertIn("parent_interface", subinterface_errors(proposed))

    def test_foreign_overlay_delete_retires_without_recreation(self):
        from netbox_nso_plugin.models import NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership
        from netbox_nso_plugin.subinterface_create import create_subinterface

        state = create_subinterface(self.mgmt, self.parent, 7, 100, "")
        interface = state.interface
        state.delete()
        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        self.assertFalse(NSOSubinterfaceState.objects.filter(interface=interface).exists())
        self.assertEqual(
            NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="subinterface").ownership_state,
            "retired",
        )

    def test_native_child_delete_retracts_with_deletion_authority(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOOwnershipManifest
        from netbox_nso_plugin.ownership_planner import reconcile_scope_ownership
        from netbox_nso_plugin.subinterface_create import create_subinterface

        state = create_subinterface(self.mgmt, self.parent, 7, 100, "")
        Interface.objects.filter(pk=state.interface_id).delete()
        reconcile_scope_ownership(self.device.pk, ["subinterface"])
        manifest = NSOOwnershipManifest.objects.get(device_id=self.device.pk, scope="subinterface")
        self.assertEqual(manifest.ownership_state, "retired")
        self.assertTrue(manifest.deletion_authority)
        self.assertTrue(
            NSOIntentOutboxEntry.objects.filter(device=self.device, scope="subinterface", mark_any=True).exists()
        )
