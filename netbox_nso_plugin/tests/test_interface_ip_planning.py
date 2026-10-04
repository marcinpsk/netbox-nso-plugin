# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Native IP lookup and lock semantics during interface-IP reconciliation."""

import copy

from dcim.models import Device, Interface
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_delete
from django.test import TestCase
from ipam.models import ASN, RIR, VRF, IPAddress
from netbox_routing.models import BGPPeer, BGPRouter, BGPScope
from virtualization.models import VirtualMachine, VMInterface

from netbox_nso_plugin.intent_state import IntentMutationProtocolError, MutationFootprint, footprint_for_instance
from netbox_nso_plugin.models import NSOBGPPeerState, NSOInterfaceIPState
from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_mirror_writes
from netbox_nso_plugin.template_content import _reconcile_interface_ips, interface_ip_reconcile_plan

from ._outbox_case import make_managed
from ._ownership_case import acquire_overlay


class TestInterfaceIPPlanning(TestCase):
    def setUp(self):
        self.device, self.management = make_managed("ip-native-plan", 18120)
        self.interface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        self.other_device, self.other_management = make_managed("ip-native-other", 18121)
        self.other_interface = Interface.objects.create(device=self.other_device, name="Ethernet1", type="1000base-t")
        self.native = IPAddress.objects.create(address="198.18.0.1/32", assigned_object=self.interface)
        self.state = NSOInterfaceIPState.objects.create(
            interface=self.interface, address="198.18.0.1/32", status="imported"
        )

    def _unassignment_plan(self):
        before = IPAddress.objects.get(pk=self.native.pk)
        candidate = copy.copy(before)
        candidate.assigned_object = None
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(
                    candidate,
                    update_fields=("assigned_object_type", "assigned_object_id"),
                    expected_before=before,
                ),
            ),
        )
        return before, candidate, plan

    def test_unassignment_preserves_the_complete_before_and_after_lock_footprint(self):
        before, after, plan = self._unassignment_plan()
        expected = MutationFootprint.merge(footprint_for_instance(before), footprint_for_instance(after))
        self.assertTrue(plan.lock_footprint.covers(expected))
        self.assertIn((self.device.pk, "ip"), plan.lock_footprint.revision_keys)
        self.assertIn(self.state.pk, [row.pk for row in plan.lock_footprint.overlay_rows])

    def test_unassignment_retains_bgp_consumer_locks(self):
        rir = RIR.objects.create(name="Planning private", slug="planning-private")
        local_as = ASN.objects.create(asn=64530, rir=rir)
        remote_as = ASN.objects.create(asn=64531, rir=rir)
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.other_device.pk,
            asn=local_as,
            name="64530",
        )
        peer = BGPPeer.objects.create(
            scope=BGPScope.objects.create(router=router), peer=self.native, remote_as=remote_as, enabled=True
        )
        acquire_overlay(
            NSOBGPPeerState,
            management=self.other_management,
            bgp_peer=peer,
            asn_str="64530",
            vrf_name="",
            peer_address_str="198.18.0.1",
            remote_as_str="64531",
            status="accepted",
        )
        before, after, plan = self._unassignment_plan()
        expected = MutationFootprint.merge(footprint_for_instance(before), footprint_for_instance(after))
        self.assertTrue(plan.lock_footprint.covers(expected))
        self.assertIn((self.other_device.pk, "bgp"), plan.lock_footprint.revision_keys)

    def test_reassignment_locks_both_devices(self):
        before = IPAddress.objects.get(pk=self.native.pk)
        candidate = copy.copy(before)
        candidate.assigned_object = self.other_interface
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(
                    candidate,
                    update_fields=("assigned_object_type", "assigned_object_id"),
                    expected_before=before,
                ),
            ),
        )
        self.assertIn((self.device.pk, "ip"), plan.lock_footprint.revision_keys)
        self.assertIn((self.other_device.pk, "ip"), plan.lock_footprint.revision_keys)

    def test_changed_native_preimage_rejects_unassignment(self):
        _before, candidate, plan = self._unassignment_plan()
        self.native.assigned_object = self.other_interface
        self.native.save(update_fields=("assigned_object_type", "assigned_object_id"))
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan) as writer:
            writer.save(candidate, update_fields=("assigned_object_type", "assigned_object_id"))
        self.native.refresh_from_db()
        self.assertEqual(self.native.assigned_object_id, self.other_interface.pk)

    def test_an_address_assigned_after_preflight_changes_the_locked_decision(self):
        payload = {"interfaces": [{"interface": "Ethernet1", "addresses": [{"address": "198.18.0.2/32"}]}]}
        plan = interface_ip_reconcile_plan(self.device, payload)
        IPAddress.objects.create(address="198.18.0.2/32", assigned_object=self.other_interface)
        with self.assertRaises(IntentMutationProtocolError), renderer_mirror_writes(plan):
            _reconcile_interface_ips(self.device, payload)
        self.assertFalse(NSOInterfaceIPState.objects.filter(address="198.18.0.2/32").exists())

    def test_duplicate_native_addresses_keep_the_first_assignment_decision(self):
        IPAddress.objects.create(address=self.native.address, assigned_object=self.other_interface)
        payload = {"interfaces": [{"interface": "Ethernet1", "addresses": [{"address": "198.18.0.1/32"}]}]}
        result = _reconcile_interface_ips(self.device, payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "imported")
        self.native.refresh_from_db()
        self.assertEqual(self.native.assigned_object_id, self.interface.pk)

    def test_unknown_interface_skips_an_unused_malformed_address(self):
        payload = {"interfaces": [{"interface": "unknown", "addresses": [{"address": "invalid"}]}]}
        result = _reconcile_interface_ips(self.device, payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pk, self.state.pk)

    def test_equivalent_ipv6_spelling_finds_the_native_assignment(self):
        IPAddress.objects.create(address="2001:db8::1/128", assigned_object=self.other_interface)
        payload = {"interfaces": [{"interface": "Ethernet1", "addresses": [{"address": "2001:0db8:0:0:0:0:0:1/128"}]}]}
        result = _reconcile_interface_ips(self.device, payload)
        selected = next(row for row in result if row.address.startswith("2001:"))
        self.assertEqual(selected.status, "conflict")

    def test_duplicate_vrf_names_preserve_the_existing_first_row_choice(self):
        VRF.objects.create(name="SHARED", rd="64512:1")
        VRF.objects.create(name="SHARED", rd="64512:2")
        selected_vrf = VRF.objects.filter(name="SHARED").first()
        IPAddress.objects.create(address="198.18.0.2/32", vrf=selected_vrf, assigned_object=self.other_interface)
        payload = {
            "interfaces": [{"interface": "Ethernet1", "addresses": [{"address": "198.18.0.2/32", "vrf": "SHARED"}]}]
        }
        result = _reconcile_interface_ips(self.device, payload)
        selected = next(row for row in result if row.address == "198.18.0.2/32")
        self.assertEqual(selected.status, "conflict")

    def test_equal_generic_ids_of_another_type_do_not_unassign_a_vm_interface(self):
        vm = VirtualMachine.objects.create(name="ip-planning-vm", site=self.device.site)
        vm_interface = VMInterface.objects.create(pk=self.interface.pk, name="eth0", virtual_machine=vm)
        self.native.assigned_object = vm_interface
        self.native.save(update_fields=("assigned_object_type", "assigned_object_id"))
        VRF.objects.create(name="NEW")
        payload = {
            "interfaces": [{"interface": "Ethernet1", "addresses": [{"address": "198.18.0.1/32", "vrf": "NEW"}]}]
        }
        _reconcile_interface_ips(self.device, payload)
        self.native.refresh_from_db()
        self.assertEqual(self.native.assigned_object, vm_interface)

    def test_delete_failure_rolls_back_native_unassignment_and_overlay_creation(self):
        VRF.objects.create(name="NEW")
        payload = {
            "interfaces": [{"interface": "Ethernet1", "addresses": [{"address": "198.18.0.1/32", "vrf": "NEW"}]}]
        }

        def fail_delete(sender, instance, **kwargs):
            raise RuntimeError("delete observer failed")

        dispatch_uid = "test_interface_ip_native_unassignment_rollback"
        post_delete.connect(fail_delete, sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, "delete observer failed"):
                _reconcile_interface_ips(self.device, payload)
        finally:
            post_delete.disconnect(sender=NSOInterfaceIPState, dispatch_uid=dispatch_uid)
        self.native.refresh_from_db()
        self.assertEqual(self.native.assigned_object, self.interface)
        self.assertTrue(NSOInterfaceIPState.objects.filter(pk=self.state.pk).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.interface, vrf="NEW").exists())
