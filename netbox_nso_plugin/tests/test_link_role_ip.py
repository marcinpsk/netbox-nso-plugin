# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 3 tests for assign_ips_for_role (link-role IP consumer).

Exercises the real M13 carve/reserve engine parameterized by an NSOLinkRole:
p2p both-ends from an explicit Prefix FK and from a role slug, role-driven child
mask, dual-stack, single-ended loopback, fill-empty-only, unmanaged peer, no pool,
and rollback. The only patch is the adapter HTTP push (a true external boundary).
"""

from unittest.mock import patch

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
from django.test import TestCase
from ipam.models import Prefix, Role

from netbox_nso_plugin.ip_autoassign import assign_ips_for_role, rollback_auto_assigned
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceIPState, NSOLinkRole

_PUSH = "netbox_nso_plugin.signals._push_ip_intent_for_device"


def _make_cable(iface_a, iface_b):
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LripMfg", slug="lripmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LripDev", slug="lripdev")
        drole = DeviceRole.objects.create(name="LripRole", slug="lriprole")
        cls.site = Site.objects.create(name="LripSite", slug="lripsite")
        cls.inst = NSOInstance.objects.create(name="lrip-nso", adapter_instance_id="lrip-nso")
        cls.dev_a = Device.objects.create(name="lrip-a", device_type=dt, role=drole, site=cls.site)
        cls.dev_b = Device.objects.create(name="lrip-b", device_type=dt, role=drole, site=cls.site)

    def _manage(self, device):
        return NSODeviceManagement.objects.create(
            device=device, nso_instance=self.inst, nso_device_name=device.name, adapter_device_id=device.pk
        )


class TestAssignP2PForRole(_Base):
    """p2p roles: both ends from explicit prefix / role slug, mask override, dual-stack."""

    def setUp(self):
        self.mgmt_a = self._manage(self.dev_a)
        self.mgmt_b = self._manage(self.dev_b)
        self.if_a = Interface.objects.create(device=self.dev_a, name="Gi0/0", type="1000base-t")
        self.if_b = Interface.objects.create(device=self.dev_b, name="Gi0/0", type="1000base-t")
        _make_cable(self.if_a, self.if_b)

    def test_p2p_explicit_prefix_both_ends(self):
        pool = Prefix.objects.create(prefix="198.18.10.0/24")
        role = NSOLinkRole.objects.create(
            name="c1", slug="c1", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool, ipv4_mask=31
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["allocated"]), 2, result)
        self.assertTrue(all(a["address"].endswith("/31") for a in result["allocated"]))
        state_a = NSOInterfaceIPState.objects.get(interface=self.if_a, family="ipv4")
        state_b = NSOInterfaceIPState.objects.get(interface=self.if_b, family="ipv4")
        self.assertEqual(state_a.peer_state_id, state_b.pk)
        self.assertTrue(state_a.auto_assigned and state_b.auto_assigned)

    def test_p2p_from_role_slug(self):
        Prefix.objects.create(prefix="198.18.11.0/24", role=Role.objects.create(name="Core", slug="core-links"))
        role = NSOLinkRole.objects.create(
            name="c2", slug="c2", link_type="p2p", assign_ipv4=True, ipv4_pool_role="core-links"
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["allocated"]), 2, result)

    def test_role_mask_override_reaches_carve(self):
        pool = Prefix.objects.create(prefix="198.18.12.0/24")
        role = NSOLinkRole.objects.create(
            name="c3", slug="c3", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool, ipv4_mask=30
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["allocated"]), 2, result)
        # The role's /30 mask must win over the built-in /31 default.
        self.assertTrue(all(a["address"].endswith("/30") for a in result["allocated"]))

    def test_p2p_dual_stack(self):
        v4 = Prefix.objects.create(prefix="198.18.13.0/24")
        v6 = Prefix.objects.create(prefix="2001:db8:13::/64")
        role = NSOLinkRole.objects.create(
            name="c4",
            slug="c4",
            link_type="p2p",
            assign_ipv4=True,
            ipv4_pool_prefix=v4,
            assign_ipv6=True,
            ipv6_pool_prefix=v6,
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["allocated"]), 4, result)
        fams = {a["family"] for a in result["allocated"]}
        self.assertEqual(fams, {"ipv4", "ipv6"})

    def test_p2p_fill_empty_skips_when_occupied(self):
        pool = Prefix.objects.create(prefix="198.18.14.0/24")
        role = NSOLinkRole.objects.create(
            name="c5", slug="c5", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        NSOInterfaceIPState.objects.create(
            interface=self.if_a, address="198.18.14.100/31", family="ipv4", status="accepted", auto_assigned=True
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["allocated"]), 0)
        self.assertEqual(len(result["skipped"]), 1)

    def test_p2p_no_pool_error_names_the_role(self):
        role = NSOLinkRole.objects.create(
            name="c6", slug="c6", link_type="p2p", assign_ipv4=True, ipv4_pool_role="nonexistent"
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("No ipv4 pool found for role 'c6'", result["errors"][0]["reason"])

    def test_p2p_peer_not_managed(self):
        self.mgmt_b.delete()  # dev_b no longer NSO-managed
        pool = Prefix.objects.create(prefix="198.18.15.0/24")
        role = NSOLinkRole.objects.create(
            name="c7", slug="c7", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("peer device is not managed", result["errors"][0]["reason"])

    def test_p2p_no_peer(self):
        pool = Prefix.objects.create(prefix="198.18.16.0/24")
        role = NSOLinkRole.objects.create(
            name="c8", slug="c8", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=None)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("no cable peer found", result["errors"][0]["reason"])

    def test_p2p_peer_managed_but_no_adapter_device_id(self):
        self.mgmt_b.adapter_device_id = None
        self.mgmt_b.save(update_fields=["adapter_device_id"])
        pool = Prefix.objects.create(prefix="198.18.18.0/24")
        role = NSOLinkRole.objects.create(
            name="c10", slug="c10", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("peer device has no adapter_device_id", result["errors"][0]["reason"])

    def test_p2p_rollback_cascades(self):
        pool = Prefix.objects.create(prefix="198.18.17.0/24")
        role = NSOLinkRole.objects.create(
            name="c9", slug="c9", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool, ipv4_mask=31
        )
        with patch(_PUSH):
            assign_ips_for_role(self.if_a, role, other_end=self.if_b)
        state_a = NSOInterfaceIPState.objects.get(interface=self.if_a, family="ipv4")
        child = state_a.source_pool
        rollback_auto_assigned(state_a)
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.if_a, family="ipv4").exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.if_b, family="ipv4").exists())
        self.assertFalse(Prefix.objects.filter(pk=child.pk).exists())


class TestAssignSingleForRole(_Base):
    """single-ended roles: loopback/access host allocation, fill-empty, no pool, no-op."""

    def setUp(self):
        self.mgmt_a = self._manage(self.dev_a)
        self.lo_a = Interface.objects.create(device=self.dev_a, name="Loopback0", type="virtual")

    def test_single_explicit_prefix(self):
        pool = Prefix.objects.create(prefix="198.18.20.0/24")
        role = NSOLinkRole.objects.create(
            name="s1", slug="s1", link_type="single", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.lo_a, role)
        self.assertEqual(len(result["allocated"]), 1, result)
        self.assertTrue(NSOInterfaceIPState.objects.filter(interface=self.lo_a, family="ipv4").exists())

    def test_single_from_role_slug(self):
        Prefix.objects.create(prefix="198.18.21.0/24", role=Role.objects.create(name="Lo", slug="loopbacks"))
        role = NSOLinkRole.objects.create(
            name="s2", slug="s2", link_type="single", assign_ipv4=True, ipv4_pool_role="loopbacks"
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.lo_a, role)
        self.assertEqual(len(result["allocated"]), 1, result)

    def test_single_fill_empty_skips(self):
        pool = Prefix.objects.create(prefix="198.18.22.0/24")
        role = NSOLinkRole.objects.create(
            name="s3", slug="s3", link_type="single", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        NSOInterfaceIPState.objects.create(
            interface=self.lo_a, address="198.18.22.9/32", family="ipv4", status="accepted", auto_assigned=True
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.lo_a, role)
        self.assertEqual(len(result["allocated"]), 0)
        self.assertEqual(len(result["skipped"]), 1)

    def test_single_no_pool_error_names_the_role(self):
        role = NSOLinkRole.objects.create(
            name="s4", slug="s4", link_type="single", assign_ipv4=True, ipv4_pool_role="nope"
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.lo_a, role)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("No ipv4 pool found for role 's4'", result["errors"][0]["reason"])

    def test_role_managing_no_ip_is_noop(self):
        role = NSOLinkRole.objects.create(
            name="s5",
            slug="s5",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            description_template="to {peer_host}",
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.lo_a, role)
        self.assertEqual(result, {"allocated": [], "skipped": [], "errors": []})

    def test_unmanaged_device_error(self):
        self.mgmt_a.delete()
        pool = Prefix.objects.create(prefix="198.18.23.0/24")
        role = NSOLinkRole.objects.create(
            name="s6", slug="s6", link_type="single", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.lo_a, role)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("not managed", result["errors"][0]["reason"])

    def test_managed_but_no_adapter_device_id(self):
        self.mgmt_a.adapter_device_id = None
        self.mgmt_a.save(update_fields=["adapter_device_id"])
        pool = Prefix.objects.create(prefix="198.18.24.0/24")
        role = NSOLinkRole.objects.create(
            name="s7", slug="s7", link_type="single", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        with patch(_PUSH):
            result = assign_ips_for_role(self.lo_a, role)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("no adapter_device_id", result["errors"][0]["reason"])
