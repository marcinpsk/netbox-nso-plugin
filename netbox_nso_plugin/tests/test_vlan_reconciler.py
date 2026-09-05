# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""VLAN + switchport reconcile into NetBox (per-device VLANGroup + native L2)."""

from __future__ import annotations

from uuid import uuid4

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from ipam.models import VLAN, VLANGroup

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOSwitchportState,
    NSOVLANState,
)
from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

from ._outbox_case import without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin, isolate_other_scopes


def _make_device(tag="m34"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"VMfg{tag}", slug=f"vmfg{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"VDev{tag}", slug=f"vdev{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"VRole{tag}", slug=f"vrole{tag}")
    site, _ = Site.objects.get_or_create(name=f"VSite{tag}", slug=f"vsite{tag}")
    return Device.objects.create(name=f"vlan-router-{tag}", device_type=dt, role=role, site=site)


class TestVlanReconciler(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="nso-dev", adapter_instance_id="nso-dev")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="vlan-router-m34"
        )
        cls.interface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")

    def test_vlan_reconciler_creates_group_scoped_state(self):
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        rows = reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}, {"vlan_id": 20, "name": "DATA"}]}
        )
        self.assertEqual(len(rows), 2)
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        self.assertTrue(VLAN.objects.filter(group=group, vid=10).exists())
        self.assertTrue(
            NSOVLANState.objects.filter(management=self.management, vlan__group=group, vlan__vid=10).exists()
        )

    def test_vlan_reconciler_uses_the_first_repeated_vlan_entry(self):
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        rows = reconcile_vlan_database(
            self.device,
            {"vlans": [{"vlan_id": 1627, "name": "FIRST"}, {"vlan_id": 1627, "name": "SECOND"}]},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].device_name, "FIRST")
        self.assertEqual(VLAN.objects.filter(group__slug=f"nso-{self.device.pk}", vid=1627).count(), 1)

    def test_vlan_reconciler_validates_repeated_vlan_entries(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        payload = {"vlans": [{"vlan_id": 1627, "name": "FIRST"}, {"vlan_id": 1627}]}

        with self.assertRaisesRegex(AdapterError, "name must be a string") as raised:
            reconcile_vlan_database(self.device, payload)

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertFalse(NSOVLANState.objects.filter(management=self.management).exists())

    def test_vlan_footprint_does_not_create_the_device_group(self):
        from netbox_nso_plugin.vlan_reconciler import vlan_reconcile_footprint

        footprint = vlan_reconcile_footprint(self.device, {"vlans": [{"vlan_id": 1627, "name": ""}]})

        self.assertFalse(VLANGroup.objects.filter(slug=f"nso-{self.device.pk}").exists())
        self.assertFalse(any(namespace == "vlan-slot" for namespace, _key in footprint.shared_keys))

    def test_switchport_footprint_does_not_create_the_device_group(self):
        from netbox_nso_plugin.vlan_reconciler import switchport_reconcile_footprint

        footprint = switchport_reconcile_footprint(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": self.interface.name,
                        "mode": "access",
                        "untagged_vlan": 1627,
                        "tagged_vlans": [],
                    }
                ]
            },
        )

        self.assertFalse(VLANGroup.objects.filter(slug=f"nso-{self.device.pk}").exists())
        self.assertFalse(any(namespace == "vlan-slot" for namespace, _key in footprint.shared_keys))

    def test_native_vlan_preflight_is_read_only(self):
        from netbox_nso_plugin.reconcile import _native_vlan_footprint

        _native_vlan_footprint(self.device, {"vlans": [{"vlan_id": 1627, "name": ""}]}, "vlan")

        self.assertFalse(VLANGroup.objects.filter(slug=f"nso-{self.device.pk}").exists())

    def test_vlan_reconciler_rejects_entries_without_a_usable_vlan_id(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        payload = {
            "vlans": [
                None,
                {},
                {"vlan_id": None},
                {"vlan_id": "not-an-integer"},
                {"vlan_id": 1624, "name": "VALID"},
            ]
        }
        with self.assertRaisesRegex(AdapterError, "VLAN payload entry"):
            reconcile_vlan_database(self.device, payload)

        self.assertFalse(NSOVLANState.objects.filter(management=self.management).exists())

    def test_vlan_reconciler_rejects_a_fractional_vlan_id(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        with self.assertRaisesRegex(AdapterError, "must be an integer VLAN ID"):
            reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 100.9, "name": "INVALID"}]})

        self.assertFalse(NSOVLANState.objects.filter(management=self.management).exists())

    def test_vlan_reconciler_rejects_a_missing_or_non_string_name(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        for entry in ({"vlan_id": 100}, {"vlan_id": 100, "name": None}):
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(AdapterError, "name must be a string") as raised:
                    reconcile_vlan_database(self.device, {"vlans": [entry]})

                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertFalse(NSOVLANState.objects.filter(management=self.management).exists())

    def test_switchport_reconciler_rejects_a_non_list_document(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        with self.assertRaisesRegex(AdapterError, "interfaces must be a list"):
            reconcile_switchport(self.device, {"interfaces": {"interface_name": self.interface.name}})

        self.assertFalse(NSOSwitchportState.objects.filter(interface=self.interface).exists())

    def test_l2_reconcilers_reject_documents_without_the_required_collection(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        for reconcile in (reconcile_vlan_database, reconcile_switchport):
            with self.subTest(reconcile=reconcile.__name__):
                with self.assertRaises(AdapterError) as raised:
                    reconcile(self.device, {})
                self.assertEqual(raised.exception.code, "invalid_response")

    def test_switchport_reconciler_rejects_falsey_non_list_tagged_vlans(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        for tagged_vlans in ("", {}):
            with self.subTest(tagged_vlans=tagged_vlans):
                with self.assertRaisesRegex(AdapterError, "tagged_vlans must be a list"):
                    reconcile_switchport(
                        self.device,
                        {
                            "interfaces": [
                                {
                                    "interface_name": self.interface.name,
                                    "mode": "trunk",
                                    "tagged_vlans": tagged_vlans,
                                }
                            ]
                        },
                    )

                self.assertFalse(NSOSwitchportState.objects.filter(interface=self.interface).exists())

    def test_switchport_reconciler_rejects_non_list_tagged_vlans(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        payload = {
            "interfaces": [
                {
                    "interface_name": self.interface.name,
                    "mode": "access",
                    "untagged_vlan": None,
                    "tagged_vlans": None,
                }
            ]
        }

        with self.assertRaises(AdapterError) as raised:
            reconcile_switchport(self.device, payload)

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_switchport_reconciler_rejects_an_omitted_untagged_vlan(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        group = VLANGroup.objects.create(name="Strict switchport", slug="strict-switchport")
        vlan = VLAN.objects.create(group=group, vid=100, name="STRICT")
        Interface.objects.filter(pk=self.interface.pk).update(untagged_vlan=vlan)

        with self.assertRaisesRegex(AdapterError, "untagged_vlan is required") as raised:
            reconcile_switchport(
                self.device,
                {
                    "interfaces": [
                        {
                            "interface_name": self.interface.name,
                            "mode": "access",
                            "tagged_vlans": [],
                        }
                    ]
                },
            )

        self.assertEqual(raised.exception.code, "invalid_response")
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan_id, vlan.pk)

    def test_switchport_reconciler_rejects_unknown_modes(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        for mode in ("hybrid", [], {}):
            with self.subTest(mode=mode):
                payload = {
                    "interfaces": [
                        {
                            "interface_name": self.interface.name,
                            "mode": mode,
                            "untagged_vlan": None,
                            "tagged_vlans": [],
                        }
                    ]
                }

                with self.assertRaises(AdapterError) as raised:
                    reconcile_switchport(self.device, payload)

                self.assertEqual(raised.exception.code, "invalid_response")

    def test_switchport_reconciler_accepts_the_unconfigured_mode_sentinel(self):
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": self.interface.name,
                        "mode": "",
                        "untagged_vlan": None,
                        "tagged_vlans": [],
                    }
                ]
            },
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].mode, "")

    def test_vlan_reconcile_preflights_native_and_overlay_creations(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.vlan_reconciler import vlan_reconcile_plan

        plan = vlan_reconcile_plan(
            self.device,
            {"vlans": [{"vlan_id": 1627, "name": "PREFLIGHT"}]},
        )

        self.assertIsInstance(plan, RendererMutationPlan)
        self.assertEqual(
            [(write.operation, write.model_label) for write in plan.write_set],
            [
                ("save", "ipam.vlangroup"),
                ("save", "ipam.vlan"),
                ("save", "netbox_nso_plugin.nsovlanstate"),
            ],
        )

    def test_vlan_reconcile_adopts_a_completed_creation_plan(self):
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes
        from netbox_nso_plugin.vlan_reconciler import (
            _reconcile_vlan_database,
            vlan_reconcile_plan,
        )

        payload = {"vlans": [{"vlan_id": 1645, "name": "RACE"}]}
        waiting_plan = vlan_reconcile_plan(self.device, payload)
        winner_plan = vlan_reconcile_plan(self.device, payload)
        with renderer_mirror_writes(winner_plan) as writer:
            _reconcile_vlan_database(self.device, payload, writer, winner_plan)

        with renderer_mirror_writes(waiting_plan) as writer:
            rows = _reconcile_vlan_database(self.device, payload, writer, waiting_plan)

        self.assertEqual([row.vlan.vid for row in rows], [1645])
        self.assertEqual(NSOVLANState.objects.filter(management=self.management, vlan__vid=1645).count(), 1)

    def test_vlan_reconcile_replays_the_frozen_group_creation_after_a_race(self):
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, vlan_reconcile_plan

        payload = {"vlans": [{"vlan_id": 1627, "name": "PREFLIGHT"}]}
        plan = vlan_reconcile_plan(self.device, payload)
        group = VLANGroup.objects.create(name=f"NSO {self.device.name}", slug=f"nso-{self.device.pk}")

        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
        with mutation:
            rows = reconcile_vlan_database(self.device, payload)

        self.assertEqual([row.vlan.vid for row in rows], [1627])
        self.assertEqual(VLANGroup.objects.get(slug=group.slug).pk, group.pk)

    def test_direct_vlan_reconcile_does_not_advance_intent_revision(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        revision, _ = NSOIntentRevision.objects.get_or_create(device=self.device, scope="vlan")
        before = revision.revision

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 1623, "name": "TEST"}]})

        revision.refresh_from_db()
        self.assertEqual(revision.revision, before)

    def test_direct_switchport_reconcile_does_not_advance_intent_revision(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 1623, "name": "TEST"}]})
        revision, _ = NSOIntentRevision.objects.get_or_create(device=self.device, scope="switchport")
        before = revision.revision

        reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": self.interface.name,
                        "mode": "trunk",
                        "untagged_vlan": None,
                        "tagged_vlans": [1623],
                    }
                ]
            },
        )

        revision.refresh_from_db()
        self.assertEqual(revision.revision, before)

    def test_switchport_reconcile_preflights_seed_and_overlay(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.vlan_reconciler import switchport_reconcile_plan

        plan = switchport_reconcile_plan(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": self.interface.name,
                        "mode": "access",
                        "untagged_vlan": 1627,
                        "tagged_vlans": [],
                    }
                ]
            },
        )

        self.assertIsInstance(plan, RendererMutationPlan)
        self.assertEqual(
            [(write.operation, write.model_label) for write in plan.write_set],
            [
                ("save", "ipam.vlangroup"),
                ("save", "ipam.vlan"),
                ("save", "dcim.interface"),
                ("save", "netbox_nso_plugin.nsoswitchportstate"),
            ],
        )

    def test_switchport_reconcile_reads_a_pristine_interface_twice(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes
        from netbox_nso_plugin.vlan_reconciler import _reconcile_switchport, switchport_reconcile_plan

        group = VLANGroup.objects.create(name=f"NSO {self.device.name}", slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.create(group=group, vid=1648, name="EXISTING")
        payload = {
            "interfaces": [
                {
                    "interface_name": self.interface.name,
                    "mode": "access",
                    "untagged_vlan": vlan.vid,
                    "tagged_vlans": [],
                }
            ]
        }
        plan = switchport_reconcile_plan(self.device, payload)
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        table = connection.ops.quote_name(Interface._meta.db_table)
        pk_column = connection.ops.quote_name(Interface._meta.pk.column)

        with mutation(plan) as writer:
            with CaptureQueriesContext(connection) as queries:
                rows = _reconcile_switchport(self.device, payload, writer, plan)

        interface_reads = [
            query["sql"]
            for query in queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and f"FROM {table}" in query["sql"]
            and f"{table}.{pk_column} = {self.interface.pk}" in query["sql"]
        ]
        self.assertEqual(len(interface_reads), 2, interface_reads)
        self.interface.refresh_from_db()
        state = NSOSwitchportState.objects.get(management=self.management, interface=self.interface)
        self.assertEqual(self.interface.mode, "access")
        self.assertEqual(self.interface.untagged_vlan_id, vlan.pk)
        self.assertEqual(state.mode, "access")
        self.assertEqual(state.untagged_vlan_id, vlan.pk)
        self.assertEqual([row.pk for row in rows], [state.pk])

    def test_switchport_reconcile_adopts_a_completed_creation_plan(self):
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes
        from netbox_nso_plugin.vlan_reconciler import (
            _reconcile_switchport,
            prepare_switchport_reconcile,
        )

        payload = {
            "interfaces": [
                {
                    "interface_name": self.interface.name,
                    "mode": "trunk",
                    "untagged_vlan": 1646,
                    "tagged_vlans": [1647],
                }
            ]
        }
        waiting = prepare_switchport_reconcile(self.device, payload)
        winner = prepare_switchport_reconcile(self.device, payload)
        with renderer_mirror_writes(winner.plan) as writer:
            _reconcile_switchport(self.device, payload, writer, winner.plan)

        with renderer_mirror_writes(waiting.plan) as writer:
            rows = _reconcile_switchport(self.device, payload, writer, waiting.plan)

        self.assertEqual([row.interface_id for row in rows], [self.interface.pk])

    def test_switchport_reconcile_rejects_malformed_tagged_vlan_values(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        with self.assertRaises(AdapterError) as raised:
            reconcile_switchport(
                self.device,
                {
                    "interfaces": [
                        {
                            "interface_name": self.interface.name,
                            "mode": "trunk",
                            "untagged_vlan": None,
                            "tagged_vlans": 1648,
                        }
                    ]
                },
            )

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_switchport_reconcile_rejects_an_unknown_mode(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        with self.assertRaises(AdapterError) as raised:
            reconcile_switchport(
                self.device,
                {
                    "interfaces": [
                        {
                            "interface_name": self.interface.name,
                            "mode": "acess",
                            "untagged_vlan": None,
                            "tagged_vlans": [],
                        }
                    ]
                },
            )

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_switchport_overlay_plan_references_vlans_created_for_the_native_mirror(self):
        from netbox_nso_plugin.renderer_writer import RendererCreationRef
        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group, switchport_reconcile_plan

        _device_vlan_group(self.device)

        plan = switchport_reconcile_plan(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": self.interface.name,
                        "mode": "trunk",
                        "untagged_vlan": 1625,
                        "tagged_vlans": [1626],
                    }
                ]
            },
        )

        overlay = next(write for write in plan.write_set if write.model_label == "netbox_nso_plugin.nsoswitchportstate")
        self.assertIsInstance(dict(overlay.values)["untagged_vlan_id"], RendererCreationRef)
        overlay_m2m = next(
            write
            for write in plan.write_set
            if write.operation == "m2m_set" and write.model_label == "netbox_nso_plugin.nsoswitchportstate"
        )
        self.assertEqual(len(overlay_m2m.selected_pks), 1)
        self.assertIsInstance(overlay_m2m.selected_pks[0], RendererCreationRef)

    def test_switchport_plan_detects_a_reported_owned_fragment_change(self):
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.vlan_reconciler import (
            reconcile_switchport,
            reconcile_vlan_database,
            switchport_reconcile_plan,
        )

        from ._outbox_case import content_update

        reconcile_vlan_database(
            self.device,
            {"vlans": [{"vlan_id": 10, "name": "TEN"}, {"vlan_id": 20, "name": "TWENTY"}]},
        )
        reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": self.interface.name,
                        "mode": "access",
                        "untagged_vlan": 10,
                        "tagged_vlans": [],
                    }
                ]
            },
        )
        state = NSOSwitchportState.objects.get(management=self.management, interface=self.interface)
        content_update(state, status="in_sync")

        plan = switchport_reconcile_plan(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": self.interface.name,
                        "mode": "trunk",
                        "untagged_vlan": None,
                        "tagged_vlans": [20],
                    }
                ]
            },
        )

        self.assertTrue(plan.changes_content)

    def test_switchport_plan_uses_the_registered_renderer_fragment(self):
        from unittest.mock import patch

        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.signals import switchport_intent_item
        from netbox_nso_plugin.vlan_reconciler import (
            reconcile_switchport,
            reconcile_vlan_database,
            switchport_reconcile_plan,
        )

        from ._outbox_case import content_update

        payload = {
            "interfaces": [
                {
                    "interface_name": self.interface.name,
                    "mode": "access",
                    "untagged_vlan": 10,
                    "tagged_vlans": [],
                }
            ]
        }
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "TEN"}]})
        reconcile_switchport(self.device, payload)
        state = NSOSwitchportState.objects.get(management=self.management, interface=self.interface)
        content_update(state, status="in_sync")

        def extended_fragment(row, tagged_vlan_ids):
            return {**switchport_intent_item(row, tagged_vlan_ids), "encapsulation": "dot1q"}

        with patch(
            "netbox_nso_plugin.signals.switchport_intent_item",
            side_effect=extended_fragment,
        ) as fragment:
            plan = switchport_reconcile_plan(self.device, payload)

        self.assertFalse(plan.changes_content)
        self.assertGreaterEqual(fragment.call_count, 2)

    def test_switchport_body_writes_only_the_interfaces_resolved_at_plan_time(self):
        """An interface that arrives after the plan is deferred: writing it escapes the footprint."""
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes
        from netbox_nso_plugin.vlan_reconciler import (
            prepare_switchport_reconcile,
            reconcile_switchport,
            reconcile_vlan_database,
        )

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "TEN"}]})
        payload = {
            "interfaces": [
                {"interface_name": self.interface.name, "mode": "access", "untagged_vlan": 10, "tagged_vlans": []},
                {"interface_name": "GigabitEthernet0/2", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []},
            ]
        }
        attempt = prepare_switchport_reconcile(self.device, payload)
        late = Interface.objects.create(device=self.device, name="GigabitEthernet0/2", type="1000base-t")

        plan = attempt.plan  # the read gate opens the writer for the frozen plan, then runs the body
        with renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan):
            reconcile_switchport(self.device, payload, attempt)

        late.refresh_from_db()
        self.assertFalse(late.mode)
        self.assertIsNone(late.untagged_vlan_id)
        self.assertFalse(NSOSwitchportState.objects.filter(management=self.management, interface=late).exists())
        seeded = NSOSwitchportState.objects.get(management=self.management, interface=self.interface)
        self.assertEqual(seeded.status, "imported")
        self.assertEqual(seeded.mode, "access")
        self.assertEqual(seeded.untagged_vlan.vid, 10)

        reconcile_switchport(self.device, payload)  # the next read resolves it and seeds it
        self.assertTrue(NSOSwitchportState.objects.filter(management=self.management, interface=late).exists())
        # The overlay row is created before the native seed, so assert the native write too.
        late.refresh_from_db()
        self.assertEqual(late.mode, "access")
        self.assertEqual(late.untagged_vlan.vid, 10)

    def test_standalone_switchport_replan_keeps_the_frozen_interface_set(self):
        """A replan re-reads the frozen pks; it must never re-resolve names mid-acquisition."""
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.vlan_reconciler import (
            prepare_switchport_reconcile,
            reconcile_switchport,
            reconcile_vlan_database,
        )

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "TEN"}]})
        payload = {
            "interfaces": [
                {"interface_name": self.interface.name, "mode": "access", "untagged_vlan": 10, "tagged_vlans": []},
                {"interface_name": "GigabitEthernet0/3", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []},
            ]
        }
        attempt = prepare_switchport_reconcile(self.device, payload)
        late = Interface.objects.create(device=self.device, name="GigabitEthernet0/3", type="1000base-t")

        reconcile_switchport(self.device, payload, attempt)

        late.refresh_from_db()
        self.assertFalse(late.mode)
        self.assertIsNone(late.untagged_vlan_id)
        self.assertFalse(NSOSwitchportState.objects.filter(management=self.management, interface=late).exists())
        seeded = NSOSwitchportState.objects.get(management=self.management, interface=self.interface)
        self.assertEqual(seeded.status, "imported")

        reconcile_switchport(self.device, payload)  # a fresh attempt resolves it and seeds it
        self.assertTrue(NSOSwitchportState.objects.filter(management=self.management, interface=late).exists())

    def test_switchport_body_reloads_the_frozen_interfaces_after_acquisition(self):
        """An operator edit committed after the plan must not be clobbered from a stale copy.

        Only the standalone entry replans a stale pre-image, once. The gated read refuses it
        (IntentPlanStaleError), and on the whole-device path that refusal becomes the
        family's scope error: unowned rows go to ``error`` until the next read re-plans.
        SVI behaves the same. Card #1659 tracks the gate-path race.
        """
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.vlan_reconciler import (
            prepare_switchport_reconcile,
            reconcile_switchport,
            reconcile_vlan_database,
        )

        reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}, {"vlan_id": 20, "name": "DATA"}]}
        )
        payload = {
            "interfaces": [
                {"interface_name": self.interface.name, "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
            ]
        }
        attempt = prepare_switchport_reconcile(self.device, payload)

        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        edited = Interface.objects.get(pk=self.interface.pk)
        edited.mode = "access"
        edited.untagged_vlan = VLAN.objects.get(group=group, vid=20)
        edited.save()

        reconcile_switchport(self.device, payload, attempt)

        self.interface.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan.vid, 20)  # operator value NOT clobbered
        state = NSOSwitchportState.objects.get(management=self.management, interface=self.interface)
        self.assertEqual(state.status, "changed")  # existing-value branch, device differs
        self.assertTrue(state.device_base_hash)  # the device version is adopted as the merge base

    def _seed_owned_in_sync_switchport(self):
        """Seed an owned, in_sync switchport row whose native interface matches access/VLAN 10."""
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        from ._outbox_case import content_update

        reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "TEN"}, {"vlan_id": 20, "name": "TWENTY"}]}
        )
        payload = {
            "interfaces": [
                {"interface_name": self.interface.name, "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
            ]
        }
        reconcile_switchport(self.device, payload)
        state = NSOSwitchportState.objects.get(management=self.management, interface=self.interface)
        content_update(state, status="in_sync")
        return payload, state

    def _refuse_gated_replay_after(self, payload, move_native):
        """Plan, let a foreign writer move the native row, then run the gated replay."""
        from netbox_nso_plugin.renderer_writer import (
            IntentPlanStaleError,
            renderer_mirror_writes,
            renderer_writes,
        )
        from netbox_nso_plugin.vlan_reconciler import prepare_switchport_reconcile, reconcile_switchport

        attempt = prepare_switchport_reconcile(self.device, payload)
        move_native(Interface.objects.get(pk=self.interface.pk))

        plan = attempt.plan  # the read gate opens the writer for the frozen plan, then runs the body
        mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
        with self.assertRaises(IntentPlanStaleError), mutation:
            reconcile_switchport(self.device, payload, attempt)

    def test_gated_switchport_replay_refuses_a_foreign_native_vlan_move(self):
        """An owned row must never keep in_sync when the native VLAN moved before the lock.

        The owned branch only READS the native interface, so no write pre-image and no read
        dependency covers it. The body rebuilds the comparison under the lock and refuses the
        frozen replay; the gate has no replan (card #1659), so the next read lands the status
        a fresh comparison gives.
        """
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        payload, state = self._seed_owned_in_sync_switchport()
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")

        def move(interface):
            interface.untagged_vlan = VLAN.objects.get(group=group, vid=20)
            interface.save()

        self._refuse_gated_replay_after(payload, move)

        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")  # the refused replay wrote nothing

        reconcile_switchport(self.device, payload)  # a fresh read compares the moved row
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")  # sm.on_reconcile(in_sync, matches=False)

    def test_gated_switchport_replay_refuses_a_foreign_tagged_set_change(self):
        """The comparison also reads the tagged set, which no frozen read dependency can carry."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        payload, state = self._seed_owned_in_sync_switchport()
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")

        def move(interface):
            interface.tagged_vlans.add(VLAN.objects.get(group=group, vid=20))

        self._refuse_gated_replay_after(payload, move)

        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")

        reconcile_switchport(self.device, payload)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_standalone_switchport_replan_never_retains_a_stale_in_sync(self):
        """The standalone entry meets the same race by replanning once, not by retaining in_sync."""
        from netbox_nso_plugin.vlan_reconciler import (
            prepare_switchport_reconcile,
            reconcile_switchport,
        )

        payload, state = self._seed_owned_in_sync_switchport()
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        attempt = prepare_switchport_reconcile(self.device, payload)
        moved = Interface.objects.get(pk=self.interface.pk)
        moved.untagged_vlan = VLAN.objects.get(group=group, vid=20)
        moved.save()

        reconcile_switchport(self.device, payload, attempt)

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan.vid, 20)  # the owned branch never writes back

    def test_two_nameless_vlans_get_unique_placeholder_names(self):
        """Live arcos shape (vlans 5/6, no names): NetBox's (group, name) unique
        constraint rejects a SECOND name='' VLAN in the per-device group, which
        crashed the whole category. Nameless imports synthesize 'VLAN <vid>'
        placeholders; the drift logic already treats a nameless device VLAN as
        always-matching, so the synthesized name never reads as drift.
        """
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        rows = reconcile_vlan_database(
            self.device,
            {"vlans": [{"vlan_id": 5, "name": ""}, {"vlan_id": 6, "name": ""}]},
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.status for r in rows}, {"imported"})
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        self.assertEqual(VLAN.objects.get(group=group, vid=5).name, "VLAN 5")
        self.assertEqual(VLAN.objects.get(group=group, vid=6).name, "VLAN 6")

    def test_the_placeholder_name_is_never_pushed_to_the_device(self):
        """The 'VLAN <vid>' name is a NetBox display placeholder invented at import for a
        NAMELESS device VLAN — not operator intent. Pushing it verbatim made Apply write a
        name the device never had. The writer omits an empty name, so the placeholder must
        go out as ''.
        """
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        self.management.adapter_device_id = 42
        self.management.save()
        rows = reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 5, "name": ""}, {"vlan_id": 7, "name": "V7"}]}
        )
        from ._outbox_case import content_update

        for row in rows:  # the operator accepts both, renaming neither
            content_update(row, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as mock_put:
            deliver("vlan", self.device.pk, 42)

        pushed = {v["vlan_id"]: v["name"] for v in mock_put.call_args[0][1]}
        self.assertEqual(pushed[5], "", "the fabricated placeholder must not be pushed as a device VLAN name")
        self.assertEqual(pushed[7], "V7", "a real device name still round-trips")

    def test_an_operator_rename_of_a_nameless_vlan_is_pushed(self):
        """The suppression keys on the name being UNTOUCHED — a rename is real intent."""
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        self.management.adapter_device_id = 42
        self.management.save()
        (row,) = reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 5, "name": ""}]})
        row.vlan.name = "STORAGE"
        row.vlan.save()
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction

        row.refresh_from_db()
        with intent_transaction(footprint_for_instance(row)):
            row.status = "accepted"
            row.save(update_fields=["status"])

        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as mock_put:
            deliver("vlan", self.device.pk, 42)

        self.assertEqual(mock_put.call_args[0][1], [{"vlan_id": 5, "name": "STORAGE"}])

    def test_plan_uses_the_shared_name_match_for_a_nameless_vlan(self):
        from unittest.mock import patch

        from netbox_nso_plugin.vlan_reconciler import (
            reconcile_vlan_database,
            vlan_name_matches,
            vlan_reconcile_plan,
        )

        (row,) = reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 5, "name": ""}]})
        row.vlan.name = "STORAGE"
        row.vlan.save()

        with patch("netbox_nso_plugin.vlan_reconciler.vlan_name_matches", wraps=vlan_name_matches) as name_matches:
            vlan_reconcile_plan(self.device, {"vlans": [{"vlan_id": 5, "name": ""}]})

        candidate = name_matches.call_args.args[0]
        self.assertEqual(candidate.device_name, "")
        self.assertEqual(candidate.vlan.name, "STORAGE")

    def test_operator_rename_is_drift_not_clobbered(self):
        """Renaming a VLAN in NetBox must surface as drift, not be reverted to the device name."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2213, "name": "OLD_NAME"}]})
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.get(group=group, vid=2213)

        # Operator renames the VLAN in NetBox.
        vlan.name = "NEW_NAME"
        vlan.save()

        # Next reconcile (e.g. opening the NSO tab) must NOT revert the rename.
        rows = reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2213, "name": "OLD_NAME"}]})
        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "NEW_NAME")  # not clobbered back to OLD_NAME
        self.assertEqual(rows[0].status, "changed")  # drift surfaced
        self.assertEqual(rows[0].device_name, "OLD_NAME")  # device value mirrored for display

    def test_moved_vlan_stays_synced_not_duplicated(self):
        """A VLAN re-scoped to a broader group is followed via the overlay FK, not duplicated."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        per_device = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        vlan = VLAN.objects.get(group=per_device, vid=10)

        # Operator re-scopes the VLAN into a shared, site-wide group.
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")
        vlan.group = site
        vlan.save()

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})

        # No duplicate created back in the per-device group; the moved VLAN is reused.
        self.assertEqual(VLAN.objects.filter(vid=10).count(), 1)
        self.assertFalse(VLAN.objects.filter(group=per_device, vid=10).exists())
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=10)
        self.assertEqual(state.vlan_id, vlan.pk)
        self.assertEqual(state.vlan.group_id, site.pk)

    def test_moved_vlan_still_drifts_when_device_drops_it(self):
        """Stale detection follows the overlay FK even after a re-scope out of the per-device group."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}, {"vlan_id": 20, "name": "DATA"}]}
        )
        vlan10 = NSOVLANState.objects.get(management=self.management, vlan__vid=10).vlan
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")
        vlan10.group = site
        vlan10.save()

        # Device now reports only VLAN 20 → the moved VLAN 10 must still surface as drift.
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 20, "name": "DATA"}]})
        state10 = NSOVLANState.objects.get(management=self.management, vlan__vid=10)
        self.assertEqual(state10.status, "changed")

    def test_switchport_anchors_to_moved_vlan(self):
        """A trunk's tagged VLAN resolves to the re-scoped VLAN, not a per-device duplicate."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        vlan10 = NSOVLANState.objects.get(management=self.management, vlan__vid=10).vlan
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")
        vlan10.group = site
        vlan10.save()

        reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1",
                        "mode": "trunk",
                        "untagged_vlan": None,
                        "tagged_vlans": [10],
                    }
                ]
            },
        )
        self.interface.refresh_from_db()
        self.assertEqual(VLAN.objects.filter(vid=10).count(), 1)
        self.assertEqual(list(self.interface.tagged_vlans.values_list("pk", flat=True)), [vlan10.pk])

    def test_vlan_rename_surfaces_drift_immediately(self):
        """Renaming an ipam.VLAN flips the overlay to changed without a full reconcile."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, save_vlan_content

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 30, "name": "MGMT"}]})
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=30)
        self.assertNotEqual(state.status, "changed")
        vlan = state.vlan

        # Operator renames the VLAN in NetBox — fires ipam.VLAN post_save only.
        vlan.name = "RENAMED"
        save_vlan_content(vlan, update_fields=("name",))

        state.refresh_from_db()
        self.assertEqual(state.status, "changed")

    def test_vlan_rename_back_clears_drift(self):
        """Renaming back to the device value clears the overlay drift immediately."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, save_vlan_content

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 31, "name": "MGMT"}]})
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=31)
        vlan = state.vlan

        vlan.name = "RENAMED"
        save_vlan_content(vlan, update_fields=("name",))
        state.refresh_from_db()
        self.assertEqual(state.status, "changed")

        vlan.name = "MGMT"
        save_vlan_content(vlan, update_fields=("name",))
        state.refresh_from_db()
        self.assertNotEqual(state.status, "changed")

    def test_unrelated_vlan_save_does_not_repend_an_apply(self):
        """A field outside the rendered name and VID does not change VLAN intent."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 32, "name": "MGMT"}]})
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=32)
        from ._outbox_case import content_update

        content_update(state, status="deploying", apply_attempt_id=uuid4())

        state.vlan.description = "Operator note"
        state.vlan.save(update_fields=["description"])

        state.refresh_from_db()
        self.assertEqual(state.status, "deploying")

    def test_matching_read_keeps_a_deploying_vlan_in_flight(self):
        """A matching device name is not apply evidence: the correlated result settles it."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        from ._outbox_case import content_update

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 33, "name": "MGMT"}]})
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=33)
        attempt = uuid4()
        content_update(state, status="deploying", apply_attempt_id=attempt)

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 33, "name": "MGMT"}]})

        state.refresh_from_db()
        self.assertEqual(state.status, "deploying")
        self.assertEqual(state.apply_attempt_id, attempt)

    def test_rescope_move_to_empty_group(self):
        """Re-scoping into a group with no collision just moves the VLAN (stays synced)."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, rescope_vlan

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 40, "name": "MGMT"}]})
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=40)
        vlan = state.vlan
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")

        action, surviving = rescope_vlan(state, site)
        self.assertEqual(action, "moved")
        self.assertEqual(surviving.pk, vlan.pk)
        vlan.refresh_from_db()
        self.assertEqual(vlan.group_id, site.pk)
        self.assertEqual(VLAN.objects.filter(vid=40).count(), 1)
        # Reconcile still tracks it via the overlay FK, no duplicate.
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 40, "name": "MGMT"}]})
        self.assertEqual(VLAN.objects.filter(vid=40).count(), 1)

    def test_rescope_merge_onto_shared_vlan(self):
        """Re-scoping into a group that already has the vid merges onto the shared VLAN."""
        from unittest.mock import patch

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision, NSOOwnershipManifest
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, rescope_vlan

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 41, "name": "MGMT"}]})
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=41)
        per_device_vlan = state.vlan
        # The device's interface carries the per-device VLAN as its access VLAN.
        self.interface.mode = "access"
        self.interface.untagged_vlan = per_device_vlan
        self.interface.save()
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan_id, per_device_vlan.pk)  # sanity

        # A shared, site-wide VLAN 41 already exists.
        site = VLANGroup.objects.create(name="Site Wide", slug="site-wide")
        shared = VLAN.objects.create(group=site, vid=41, name="SHARED_MGMT")

        from ._outbox_case import content_update, mirror_update

        mirror_update(self.management, adapter_device_id=41)
        content_update(state, status="in_sync")
        state.refresh_from_db()
        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule:
            action, surviving = rescope_vlan(state, site)
        self.assertEqual(action, "merged")
        self.assertEqual(surviving.pk, shared.pk)
        # Overlay + native interface re-pointed onto the shared VLAN; duplicate gone.
        state.refresh_from_db()
        self.assertEqual(state.vlan_id, shared.pk)
        self.assertEqual(state.status, "accepted")
        schedule.assert_called_once_with((self.device.pk, "vlan"))
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan_id, shared.pk)
        self.assertFalse(VLAN.objects.filter(pk=per_device_vlan.pk).exists())
        self.assertEqual(VLAN.objects.filter(group=site, vid=41).count(), 1)
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        self.assertEqual(revision.verified_revision, revision.revision)
        self.assertEqual(
            revision.verified_fingerprint,
            delivery.canonical_fingerprint(
                delivery.render("vlan", self.device.pk, self.management.adapter_device_id).payload
            ),
        )
        self.assertTrue(
            NSOOwnershipManifest.objects.filter(
                device_id=self.device.pk,
                scope="vlan",
                native_model_label="ipam.vlan",
                native_key={"group_id": shared.group_id, "vid": shared.vid},
                ownership_state="owned",
            ).exists()
        )

    def test_rescope_merge_repends_an_owned_placeholder_when_its_wire_name_changes(self):
        from unittest.mock import patch

        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, rescope_vlan

        (source_state,) = reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 41, "name": ""}]})
        from ._outbox_case import content_update

        content_update(source_state, status="in_sync")
        source_state.refresh_from_db()
        target_group = VLANGroup.objects.create(name="Rendered Name Target", slug="rendered-name-target")
        target_vlan = VLAN.objects.create(group=target_group, vid=41, name=source_state.vlan.name)
        target_state = NSOVLANState.objects.create(
            management=self.management,
            vlan=target_vlan,
            device_name=target_vlan.name,
            status="in_sync",
        )
        from ._outbox_case import mirror_update

        mirror_update(self.management, adapter_device_id=41)

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule:
            action, surviving = rescope_vlan(source_state, target_group)

        self.assertEqual((action, surviving.pk), ("merged", target_vlan.pk))
        target_state.refresh_from_db()
        self.assertEqual(target_state.status, "accepted")
        schedule.assert_called_once_with((self.device.pk, "vlan"))

    def test_rescope_noop_same_group(self):
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, rescope_vlan

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 42, "name": "MGMT"}]})
        state = NSOVLANState.objects.get(management=self.management, vlan__vid=42)
        action, _ = rescope_vlan(state, state.vlan.group)
        self.assertEqual(action, "noop")

    def test_rescope_merge_transfers_ownership_to_a_same_name_duplicate(self):
        from netbox_nso_plugin.status_machine import is_owned
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, rescope_vlan

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 42, "name": "MGMT"}]})
        source_state = NSOVLANState.objects.get(management=self.management, vlan__vid=42)
        from ._outbox_case import content_update

        content_update(source_state, status="deploying", apply_attempt_id=uuid4())
        target_group = VLANGroup.objects.create(name="Same Name Target", slug="same-name-target")
        target_vlan = VLAN.objects.create(group=target_group, vid=42, name="MGMT")
        surviving_state = NSOVLANState.objects.create(
            management=self.management,
            vlan=target_vlan,
            device_name="MGMT",
            status="imported",
        )

        action, surviving_vlan = rescope_vlan(source_state, target_group)

        self.assertEqual((action, surviving_vlan.pk), ("merged", target_vlan.pk))
        surviving_state.refresh_from_db()
        self.assertTrue(is_owned(surviving_state.status))
        self.assertEqual(surviving_state.status, "accepted")
        self.assertFalse(NSOVLANState.objects.filter(pk=source_state.pk).exists())

    def test_rescope_merge_deduplicates_existing_target_memberships(self):
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, rescope_vlan

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 46, "name": "MGMT"}]})
        source_state = NSOVLANState.objects.get(management=self.management, vlan__vid=46)
        source_vlan = source_state.vlan
        target_group = VLANGroup.objects.create(name="Deduplicate Target", slug="deduplicate-target")
        target_vlan = VLAN.objects.create(group=target_group, vid=46, name="MGMT")
        self.interface.tagged_vlans.set((source_vlan, target_vlan))
        switchport = NSOSwitchportState.objects.create(
            management=self.management,
            interface=self.interface,
            mode="tagged",
            status="imported",
        )
        switchport.tagged_vlans.set((source_vlan, target_vlan))

        action, surviving = rescope_vlan(source_state, target_group)

        self.assertEqual((action, surviving.pk), ("merged", target_vlan.pk))
        self.assertEqual(list(self.interface.tagged_vlans.values_list("pk", flat=True)), [target_vlan.pk])
        self.assertEqual(list(switchport.tagged_vlans.values_list("pk", flat=True)), [target_vlan.pk])

    def test_rescope_retries_a_merge_after_source_identity_changes_while_waiting(self):
        from unittest.mock import patch

        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.vlan_reconciler import (
            reconcile_vlan_database,
            rescope_vlan,
        )

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 43, "name": "MGMT"}]})
        source_state = NSOVLANState.objects.get(management=self.management, vlan__vid=43)
        target_group = VLANGroup.objects.create(name="Identity Target", slug="identity-target")
        target_vlan = VLAN.objects.create(group=target_group, vid=43, name="MGMT")
        changed = False

        def change_source_before_native_lock(plan):
            nonlocal changed
            if not changed:
                changed = True
                footprint = intent_state.footprint_for_instance(source_state.vlan)
                with suppress_intent_push(), intent_state.intent_transaction(footprint):
                    VLAN.objects.filter(pk=source_state.vlan_id).update(vid=1043)
            return plan

        with patch(
            "netbox_nso_plugin.vlan_reconciler._rescope_plan_ready",
            side_effect=change_source_before_native_lock,
        ):
            action, vlan = rescope_vlan(source_state, target_group)

        self.assertTrue(changed)
        self.assertEqual((action, vlan.pk), ("merged", target_vlan.pk))

    def test_rescope_retries_a_source_identity_change_after_planning(self):
        from unittest.mock import patch

        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, rescope_vlan

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 44, "name": "MGMT"}]})
        source_state = NSOVLANState.objects.get(management=self.management, vlan__vid=44)
        target_group = VLANGroup.objects.create(name="Retry Target", slug="retry-target")
        changed = False

        def change_source_before_native_lock(plan):
            nonlocal changed
            if not changed:
                changed = True
                footprint = intent_state.footprint_for_instance(source_state.vlan)
                with suppress_intent_push(), intent_state.intent_transaction(footprint):
                    VLAN.objects.filter(pk=source_state.vlan_id).update(vid=1044)
            return plan

        with patch(
            "netbox_nso_plugin.vlan_reconciler._rescope_plan_ready",
            side_effect=change_source_before_native_lock,
        ):
            action, vlan = rescope_vlan(source_state, target_group)

        self.assertTrue(changed)
        self.assertEqual(action, "moved")
        self.assertEqual(vlan.group_id, target_group.pk)

    def test_rescope_rejects_a_device_that_attaches_before_transaction_acquisition(self):
        from unittest.mock import patch

        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.vlan_reconciler import (
            VLANRescopeConflict,
            reconcile_vlan_database,
            rescope_vlan,
        )

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 44, "name": "MGMT"}]})
        source_state = NSOVLANState.objects.get(management=self.management, vlan__vid=44)
        target_group = VLANGroup.objects.create(name="Membership Target", slug="membership-target")
        VLAN.objects.create(group=target_group, vid=44, name="MGMT")
        late_device = _make_device("late-membership")
        late_management = NSODeviceManagement.objects.create(
            device=late_device,
            nso_instance=self.instance,
            nso_device_name="late-membership",
        )
        attached = False

        def attach_before_transaction(plan):
            nonlocal attached
            if not attached:
                attached = True
                late_state = NSOVLANState(
                    management=late_management,
                    vlan=source_state.vlan,
                    device_name="MGMT",
                    status="imported",
                )
                with intent_state.intent_transaction(intent_state.footprint_for_instance(late_state)):
                    late_state.save()
            return plan

        with (
            patch(
                "netbox_nso_plugin.vlan_reconciler._rescope_plan_ready",
                side_effect=attach_before_transaction,
            ),
            self.assertRaisesRegex(VLANRescopeConflict, "membership changed"),
        ):
            rescope_vlan(source_state, target_group)

    def test_switchport_seeded_when_pristine(self):
        """A pristine NetBox interface is SEEDED from the device (read mirror) → imported, no drift.

        This is the fix for false drift on freshly-imported switchports: an interface with
        no L2 config gets the device's mode/native/tagged written onto it (like VLAN seeds
        its name), instead of being flagged as 'changed'.
        """
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {"interface_name": "GigabitEthernet0/1", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
                ]
            },
        )
        self.assertEqual(rows[0].status, "imported")  # seeded, not false drift
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mode, "access")  # device L2 materialised onto NetBox
        self.assertEqual(self.interface.untagged_vlan.vid, 10)

    def test_switchport_imported_when_netbox_matches_nso(self):
        # Non-pristine interface already matching the device → imported (no drift), no clobber.
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}]})
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        vlan10 = VLAN.objects.get(group=group, vid=10)
        self.interface.mode = "access"
        self.interface.untagged_vlan = vlan10
        self.interface.save()

        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {"interface_name": "GigabitEthernet0/1", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
                ]
            },
        )
        self.assertEqual(rows[0].status, "imported")

    def test_switchport_plan_prefetches_native_tagged_vlans_once(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database, switchport_reconcile_plan

        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 47, "name": "MGMT"}]})
        vlan = NSOVLANState.objects.get(management=self.management, vlan__vid=47).vlan
        peer = Interface.objects.create(device=self.device, name="GigabitEthernet0/2", type="1000base-t")
        for interface in (self.interface, peer):
            interface.mode = "tagged"
            interface.save(update_fields=["mode"])
            interface.tagged_vlans.add(vlan)
        payload = {
            "interfaces": [
                {
                    "interface_name": interface.name,
                    "mode": "trunk",
                    "untagged_vlan": None,
                    "tagged_vlans": [47],
                }
                for interface in (self.interface, peer)
            ]
        }
        through_table = Interface._meta.get_field("tagged_vlans").remote_field.through._meta.db_table

        with CaptureQueriesContext(connection) as queries:
            switchport_reconcile_plan(self.device, payload)

        relation_queries = [query for query in queries if through_table in query["sql"]]
        self.assertEqual(len(relation_queries), 1)

    def test_switchport_changed_when_netbox_has_divergent_value(self):
        """A non-pristine interface whose L2 differs from the device → changed, NOT clobbered."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport, reconcile_vlan_database

        reconcile_vlan_database(
            self.device, {"vlans": [{"vlan_id": 10, "name": "MGMT"}, {"vlan_id": 20, "name": "DATA"}]}
        )
        group = VLANGroup.objects.get(slug=f"nso-{self.device.pk}")
        # NetBox already carries access vlan 20; the device says access vlan 10 → divergence.
        self.interface.mode = "access"
        self.interface.untagged_vlan = VLAN.objects.get(group=group, vid=20)
        self.interface.save()

        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {"interface_name": "GigabitEthernet0/1", "mode": "access", "untagged_vlan": 10, "tagged_vlans": []}
                ]
            },
        )
        self.assertEqual(rows[0].status, "changed")
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan.vid, 20)  # operator value NOT clobbered

    def test_switchport_stale_vestigial_row_pruned(self):
        """A stale row whose interface carries no L2 config is vestigial → pruned, not drift.

        Guards the early-days bug where 'no switchport' (L3) ports got switchport
        overlays that then lingered as perpetual false drift.
        """
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        iface2 = Interface.objects.create(device=self.device, name="GigabitEthernet0/2", type="1000base-t")
        NSOSwitchportState.objects.create(
            management=self.management, interface=iface2, mode="tagged-all", status="imported"
        )
        # iface2 is blank (no L2) and absent from the payload → vestigial → pruned.
        reconcile_switchport(self.device, {"interfaces": []})
        self.assertFalse(NSOSwitchportState.objects.filter(interface=iface2).exists())

    def test_switchport_stale_row_with_operator_value_marked_changed(self):
        """A stale row whose interface still holds an L2 value is a genuine removal → changed, kept."""
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        iface2 = Interface.objects.create(
            device=self.device, name="GigabitEthernet0/3", type="1000base-t", mode="tagged-all"
        )
        state = NSOSwitchportState.objects.create(
            management=self.management, interface=iface2, mode="tagged-all", status="imported"
        )
        reconcile_switchport(self.device, {"interfaces": []})
        self.assertTrue(NSOSwitchportState.objects.filter(pk=state.pk).exists())
        state.refresh_from_db()
        self.assertEqual(state.status, "changed")

    def test_switchport_native_vlan_1_normalized(self):
        """IOS implicit default native VLAN 1 is treated as 'no native' (no false drift)."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_switchport

        rows = reconcile_switchport(
            self.device,
            {
                "interfaces": [
                    {
                        "interface_name": "GigabitEthernet0/1",
                        "mode": "trunk-all",
                        "untagged_vlan": 1,
                        "tagged_vlans": [],
                    }
                ]
            },
        )
        self.assertEqual(rows[0].status, "imported")  # pristine → seeded, in sync
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mode, "tagged-all")
        self.assertIsNone(self.interface.untagged_vlan)  # native VLAN 1 normalised away


class TestVlanWritePath(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("vwp")
        cls.instance = NSOInstance.objects.create(name="nso-vwp", adapter_instance_id="nso-vwp")
        cls.management = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.instance, nso_device_name="vlan-router-vwp", adapter_device_id=77
        )

    def _state(self, vid=2213, name="OLD", status="imported", device_name="OLD"):
        group = _device_vlan_group(self.device)
        vlan = VLAN.objects.create(group=group, vid=vid, name=name)
        return NSOVLANState.objects.create(
            management=self.management, vlan=vlan, device_name=device_name, status=status
        )

    def test_push_builds_owned_snapshot_with_live_name(self):
        from unittest.mock import patch

        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.signals import reset_intent_push_state

        self._state(vid=2213, name="RENAMED", status="accepted", device_name="OLD")
        self._state(vid=10, name="MGMT", status="imported")  # not owned → excluded
        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent") as mock_put:
            deliver("vlan", self.device.pk, 77)
        mock_put.assert_called_once()
        vlans = mock_put.call_args[0][1]
        assert vlans == [{"vlan_id": 2213, "name": "RENAMED"}]  # live NetBox name, owned only

    def test_foreign_overlay_save_does_not_schedule_vlan_behavior(self):
        from unittest.mock import patch

        state = self._state(vid=2214, name="FOREIGN", status="accepted")

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule:
            state.device_name = "FOREIGN-READ"
            state.save(update_fields=("device_name",))

        schedule.assert_not_called()

    def test_accept_marks_owned(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision, NSOOwnershipManifest

        state = self._state(vid=2213, name="RENAMED", status="conflict")
        User = get_user_model()
        admin = User.objects.create_superuser(username="vlan-admin", password="pw", email="v@x.y")  # noqa: S106
        self.client.force_login(admin)
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent"):
            resp = self.client.post(f"/plugins/nso/vlan/state/{state.pk}/accept/")
        assert resp.status_code == 302
        state.refresh_from_db()
        assert state.status == "accepted" and state.accepted_at is not None
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        assert revision.verified_revision == revision.revision
        assert revision.verified_fingerprint == delivery.canonical_fingerprint(
            delivery.render("vlan", self.device.pk, self.management.adapter_device_id).payload
        )
        assert NSOOwnershipManifest.objects.filter(
            device_id=self.device.pk,
            scope="vlan",
            native_model_label="ipam.vlan",
            native_key={"group_id": state.vlan.group_id, "vid": state.vlan.vid},
            ownership_state="owned",
        ).exists()

    def test_owned_settles_in_sync_when_device_matches(self):
        """An accepted VLAN whose device name now matches NetBox → in_sync (apply landed)."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        self._state(vid=2213, name="FW-01", status="accepted")
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2213, "name": "FW-01"}]})
        assert NSOVLANState.objects.get(vlan__vid=2213).status == "in_sync"

    def test_owned_stays_accepted_when_device_differs(self):
        """An accepted VLAN whose device name still differs stays pending (accepted)."""
        from netbox_nso_plugin.vlan_reconciler import reconcile_vlan_database

        self._state(vid=2300, name="NEW", status="accepted")
        reconcile_vlan_database(self.device, {"vlans": [{"vlan_id": 2300, "name": "OLD"}]})
        assert NSOVLANState.objects.get(vlan__vid=2300).status == "accepted"


class TestVlanApplyPush(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The Apply's forced VLAN push, which needs a committed transaction to take.

    ``drain.push_now`` refuses to run nested in a caller's block (#1503 Appendix O), so this
    runs outside a test transaction, against the real claim. The test first consumes the
    rename's ordinary claim, then proves Apply still forces the current live body.
    """

    def setUp(self):
        super().setUp()
        with without_commit_drain(), transaction.atomic():
            self.device = _make_device("vap")
            instance = NSOInstance.objects.create(name="nso-vap", adapter_instance_id="nso-vap")
            self.management = NSODeviceManagement.objects.create(
                device=self.device, nso_instance=instance, nso_device_name="vlan-router-vap", adapter_device_id=77
            )
            vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=2213, name="OLD")
            self.state = NSOVLANState.objects.create(
                management=self.management, vlan=vlan, device_name="OLD", status="in_sync"
            )

    def test_prepare_apply_pushes_vlan_intent(self):
        """_prepare_apply ships owned VLAN intent (not just LACP/switchport)."""
        from unittest.mock import patch

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import _prepare_apply

        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent", return_value={}):
            drain.drain_key(self.device.pk, "vlan")  # the acknowledged baseline, carrying "OLD"
        from ._outbox_case import content_update

        content_update(self.state.vlan, name="LIVE_RENAMED")

        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent", return_value={}) as rename_push:
            drain.drain_key(self.device.pk, "vlan")
        rename_push.assert_called_once()

        # The rename claim is acknowledged, so another ordinary drain sends nothing.
        with patch("netbox_nso_plugin.adapter_client.put_vlan_intent", return_value={}) as unforced:
            drain.drain_key(self.device.pk, "vlan")
        unforced.assert_not_called()

        # Every other scope is isolated from the registry, so this asserts on the VLAN push
        # alone instead of relying on _prepare_apply swallowing the others' adapter failures.
        with (
            isolate_other_scopes("vlan"),
            patch("netbox_nso_plugin.adapter_client.put_vlan_intent", return_value={}) as mock_vlan,
        ):
            _prepare_apply(self.management)
        mock_vlan.assert_called_once()
        self.assertEqual(mock_vlan.call_args[0][1], [{"vlan_id": 2213, "name": "LIVE_RENAMED"}])

    def test_foreign_vlan_rename_commits_without_plugin_bookkeeping(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        vlan = self.state.vlan
        vlan.name = "UNTRANSACTIONAL"

        vlan.save(update_fields=["name"])

        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "UNTRANSACTIONAL")
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").exists())

    def test_unmanaged_vlan_rename_uses_the_shared_dependency_transaction(self):
        with transaction.atomic():
            vlan = VLAN.objects.create(group=_device_vlan_group(self.device), vid=2214, name="UNMANAGED")

        vlan.name = "RENAMED"
        with transaction.atomic():
            vlan.save(update_fields=["name"])

        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "RENAMED")
