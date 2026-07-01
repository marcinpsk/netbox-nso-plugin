# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 6 tests for the link-role orchestrator + operator action.

Real ORM end-to-end across all three consumers (IP + description + IGP) on both
ends of a real cable, with atomic rollback verified against actual DB state. The
only patches are the four adapter intent pushes (true external boundaries).
"""

from contextlib import ExitStack
from unittest.mock import patch

from core.models import ObjectType
from dcim.models import (
    Cable,
    CableTermination,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.contrib.auth import get_user_model
from django.test import TestCase
from ipam.models import Prefix, Role
from users.models import ObjectPermission

from netbox_nso_plugin.link_role import provision_link_role
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceIPState,
    NSOInterfaceState,
    NSOISISInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
)

_PUSH_TARGETS = (
    "netbox_nso_plugin.signals._push_ip_intent_for_device",
    "netbox_nso_plugin.signals._push_interface_intent_for_device",
    "netbox_nso_plugin.signals._push_isis_intent_for_device",
    "netbox_nso_plugin.signals._push_ospf_intent_for_device",
)


def _make_cable(iface_a, iface_b):
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LpMfg", slug="lpmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LpDev", slug="lpdev")
        drole = DeviceRole.objects.create(name="LpRole", slug="lprole")
        cls.site = Site.objects.create(name="LpSite", slug="lpsite")
        cls.inst = NSOInstance.objects.create(name="lp-nso", adapter_instance_id="lp-nso")
        cls.dev_a = Device.objects.create(name="lp-a", device_type=dt, role=drole, site=cls.site)
        cls.dev_b = Device.objects.create(name="lp-b", device_type=dt, role=drole, site=cls.site)

    def _manage(self, device):
        return NSODeviceManagement.objects.create(
            device=device, nso_instance=self.inst, nso_device_name=device.name, adapter_device_id=device.pk
        )

    def _provision(self, iface):
        with ExitStack() as stack:
            pushes = [stack.enter_context(patch(t)) for t in _PUSH_TARGETS]
            summary = provision_link_role(iface)
        return summary, pushes


class TestProvisionP2P(_Base):
    def setUp(self):
        self.mgmt_a = self._manage(self.dev_a)
        self.mgmt_b = self._manage(self.dev_b)
        self.if_a = Interface.objects.create(device=self.dev_a, name="Gi0/0", type="1000base-t")
        self.if_b = Interface.objects.create(device=self.dev_b, name="Gi0/0", type="1000base-t")
        self.cable = _make_cable(self.if_a, self.if_b)
        self.if_a.refresh_from_db()
        self.if_b.refresh_from_db()

    def _p2p_role(self, **overrides):
        pool = Prefix.objects.create(prefix="198.18.30.0/24", role=Role.objects.create(name="P", slug="p-core"))
        data = {
            "name": "lp-core",
            "slug": "lp-core",
            "link_type": "p2p",
            "assign_ipv4": True,
            "ipv4_pool_prefix": pool,
            "ipv4_mask": 31,
            "description_template": "to {peer_host}:{peer_iface}",
            "igp": "isis",
            "isis_circuit_type": "point-to-point",
        }
        data.update(overrides)
        return NSOLinkRole.objects.create(**data)

    def test_happy_path_all_three_both_ends(self):
        role = self._p2p_role()
        NSOLinkRoleAssignment.objects.create(role=role, cable=self.cable)
        summary, _pushes = self._provision(self.if_a)
        self.assertTrue(summary["provisioned"], summary)
        self.assertFalse(summary["rolled_back"])
        # IP: both ends
        self.assertTrue(NSOInterfaceIPState.objects.filter(interface=self.if_a, family="ipv4").exists())
        self.assertTrue(NSOInterfaceIPState.objects.filter(interface=self.if_b, family="ipv4").exists())
        # Description: both ends rendered with the peer
        self.if_a.refresh_from_db()
        self.if_b.refresh_from_db()
        self.assertEqual(self.if_a.description, "to lp-b:Gi0/0")
        self.assertEqual(self.if_b.description, "to lp-a:Gi0/0")
        self.assertTrue(NSOInterfaceState.objects.filter(interface=self.if_a, attribute="description").exists())
        # IGP: both ends
        self.assertTrue(NSOISISInterfaceState.objects.filter(interface=self.if_a).exists())
        self.assertTrue(NSOISISInterfaceState.objects.filter(interface=self.if_b).exists())

    def test_pushes_each_affected_device(self):
        role = self._p2p_role()
        NSOLinkRoleAssignment.objects.create(role=role, cable=self.cable)
        _summary, pushes = self._provision(self.if_a)
        ip_push, iface_push, isis_push, _ospf_push = pushes
        # Both device ids pushed for ip / interface / isis.
        for push in (ip_push, iface_push, isis_push):
            pushed_devices = {call.args[0] for call in push.call_args_list}
            self.assertEqual(pushed_devices, {self.dev_a.pk, self.dev_b.pk}, push)

    def test_partial_failure_rolls_back_everything(self):
        # IPv4 pool role does not exist → IP consumer errors → whole txn rolls back,
        # so the description that would otherwise succeed is also reverted.
        role = self._p2p_role(ipv4_pool_prefix=None, ipv4_pool_role="does-not-exist", igp="none")
        NSOLinkRoleAssignment.objects.create(role=role, cable=self.cable)
        self.if_a.description = "original"
        self.if_a.save()
        summary, _pushes = self._provision(self.if_a)
        self.assertFalse(summary["provisioned"])
        self.assertTrue(summary["rolled_back"])
        self.assertTrue(summary["errors"])
        self.if_a.refresh_from_db()
        self.assertEqual(self.if_a.description, "original")  # reverted
        self.assertFalse(NSOInterfaceState.objects.filter(interface=self.if_a, attribute="description").exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.if_a).exists())

    def test_unmanaged_peer_skips(self):
        self.mgmt_b.delete()  # far end no longer NSO-managed
        role = self._p2p_role()
        NSOLinkRoleAssignment.objects.create(role=role, cable=self.cable)
        summary, _pushes = self._provision(self.if_a)
        self.assertFalse(summary["provisioned"])
        self.assertIsNotNone(summary["skipped"])
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.if_a).exists())
        self.assertFalse(NSOISISInterfaceState.objects.filter(interface=self.if_a).exists())

    def test_no_role_skips(self):
        summary, _pushes = self._provision(self.if_a)
        self.assertFalse(summary["provisioned"])
        self.assertEqual(summary["skipped"], "no link role assigned")

    def test_disabled_role_skips(self):
        role = self._p2p_role(enabled=False)
        NSOLinkRoleAssignment.objects.create(role=role, cable=self.cable)
        summary, _pushes = self._provision(self.if_a)
        self.assertFalse(summary["provisioned"])
        self.assertEqual(summary["skipped"], "link role is disabled")

    def test_idempotent_rerun(self):
        role = self._p2p_role()
        NSOLinkRoleAssignment.objects.create(role=role, cable=self.cable)
        self._provision(self.if_a)
        summary2, _pushes = self._provision(self.if_a)
        self.assertTrue(summary2["provisioned"])
        # No duplicate rows.
        self.assertEqual(NSOInterfaceIPState.objects.filter(interface=self.if_a, family="ipv4").count(), 1)
        self.assertEqual(NSOISISInterfaceState.objects.filter(interface=self.if_a).count(), 1)
        self.assertEqual(NSOInterfaceState.objects.filter(interface=self.if_a, attribute="description").count(), 1)


class TestProvisionSingle(_Base):
    def setUp(self):
        self.mgmt_a = self._manage(self.dev_a)
        self.lo_a = Interface.objects.create(device=self.dev_a, name="Loopback0", type="virtual")

    def test_single_ended_all_three(self):
        Prefix.objects.create(prefix="198.18.31.0/24", role=Role.objects.create(name="Lo", slug="lo-pool"))
        role = NSOLinkRole.objects.create(
            name="lp-loop",
            slug="lp-loop",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_role="lo-pool",
            description_template="{self_host} loopback",
            igp="isis",
            isis_passive=True,
        )
        NSOLinkRoleAssignment.objects.create(role=role, interface=self.lo_a)
        with ExitStack() as stack:
            for t in _PUSH_TARGETS:
                stack.enter_context(patch(t))
            summary = provision_link_role(self.lo_a)
        self.assertTrue(summary["provisioned"], summary)
        self.assertTrue(NSOInterfaceIPState.objects.filter(interface=self.lo_a, family="ipv4").exists())
        self.lo_a.refresh_from_db()
        self.assertEqual(self.lo_a.description, "lp-a loopback")
        state = NSOISISInterfaceState.objects.get(interface=self.lo_a)
        self.assertTrue(state.passive)


class TestProvisionActionView(_Base):
    """The device_provision_link_role operator action view."""

    def setUp(self):
        self.mgmt_a = self._manage(self.dev_a)
        self.mgmt_b = self._manage(self.dev_b)
        self.if_a = Interface.objects.create(device=self.dev_a, name="Gi0/0", type="1000base-t")
        self.if_b = Interface.objects.create(device=self.dev_b, name="Gi0/0", type="1000base-t")
        cable = _make_cable(self.if_a, self.if_b)
        self.if_a.refresh_from_db()
        pool = Prefix.objects.create(prefix="198.18.32.0/24", role=Role.objects.create(name="Pv", slug="pv-core"))
        role = NSOLinkRole.objects.create(
            name="lp-view", slug="lp-view", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool, ipv4_mask=31
        )
        NSOLinkRoleAssignment.objects.create(role=role, cable=cable)

    def test_post_provisions_selected_interfaces(self):
        from django.urls import reverse

        user = get_user_model().objects.create_user(username="lp-operator", password="lp-pass")
        perm = ObjectPermission.objects.create(name="change-mgmt", actions=["change"])
        perm.object_types.add(ObjectType.objects.get_for_model(NSODeviceManagement))
        perm.users.add(user)
        self.client.force_login(user)
        url = reverse("plugins:netbox_nso_plugin:device_provision_link_role", args=[self.dev_a.pk])
        with ExitStack() as stack:
            for t in _PUSH_TARGETS:
                stack.enter_context(patch(t))
            resp = self.client.post(url, {"interface_pks": str(self.if_a.pk)})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(NSOInterfaceIPState.objects.filter(interface=self.if_a, family="ipv4").exists())
        self.assertTrue(NSOInterfaceIPState.objects.filter(interface=self.if_b, family="ipv4").exists())
