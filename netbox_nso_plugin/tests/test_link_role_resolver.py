# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 2 tests for the link-role resolver (link_role.py).

Real ORM + real cable topology (via find_peer): interface-direct resolution,
cable-derived resolution on both ends, precedence, the config-gated derived
fallback, and intent_bundle flattening. No mocks.
"""

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
from django.apps import apps
from django.test import TestCase
from ipam.models import Prefix, Role

from netbox_nso_plugin.link_role import intent_bundle, resolve_role
from netbox_nso_plugin.models import NSOLinkRole, NSOLinkRoleAssignment


def _make_cable(iface_a, iface_b):
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


class TestIntentBundle(TestCase):
    """intent_bundle flattens a role's outputs; explicit prefix + role slug both surface."""

    @classmethod
    def setUpTestData(cls):
        cls.pool = Prefix.objects.create(prefix="198.18.0.0/24", role=Role.objects.create(name="IbP2P", slug="ibp2p"))

    def test_ipv4_explicit_prefix_only(self):
        role = NSOLinkRole.objects.create(
            name="ib-core",
            slug="ib-core",
            link_type="p2p",
            assign_ipv4=True,
            ipv4_pool_prefix=self.pool,
            ipv4_mask=31,
            assign_ipv6=False,
        )
        bundle = intent_bundle(role)
        self.assertEqual(len(bundle.pools), 1)
        spec = bundle.pools[0]
        self.assertEqual(spec.family, "ipv4")
        self.assertEqual(spec.prefix, self.pool)
        self.assertEqual(spec.role_slug, "")
        self.assertEqual(spec.mask, 31)

    def test_ipv4_role_slug_only(self):
        role = NSOLinkRole.objects.create(
            name="ib-loop",
            slug="ib-loop",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_role="loopback",
            assign_ipv6=False,
        )
        bundle = intent_bundle(role)
        self.assertEqual(len(bundle.pools), 1)
        self.assertIsNone(bundle.pools[0].prefix)
        self.assertEqual(bundle.pools[0].role_slug, "loopback")

    def test_dual_stack_yields_two_specs(self):
        role = NSOLinkRole.objects.create(
            name="ib-dual",
            slug="ib-dual",
            link_type="p2p",
            assign_ipv4=True,
            ipv4_pool_prefix=self.pool,
            assign_ipv6=True,
            ipv6_pool_role="p2p6",
        )
        bundle = intent_bundle(role)
        self.assertEqual([s.family for s in bundle.pools], ["ipv4", "ipv6"])

    def test_passthrough_description_and_igp(self):
        role = NSOLinkRole.objects.create(
            name="ib-desc",
            slug="ib-desc",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            description_template="to {peer_host}",
            igp="ospf",
        )
        bundle = intent_bundle(role)
        self.assertEqual(bundle.pools, ())
        self.assertEqual(bundle.description_template, "to {peer_host}")
        self.assertEqual(bundle.igp, "ospf")
        self.assertEqual(bundle.link_type, "single")


class TestResolveRole(TestCase):
    """resolve_role: interface-direct, cable-derived (both ends), precedence, fallback."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RrMfg", slug="rrmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RrDev", slug="rrdev")
        drole = DeviceRole.objects.create(name="RrRole", slug="rrrole")
        site = Site.objects.create(name="RrSite", slug="rrsite")
        cls.dev_a = Device.objects.create(name="rr-a", device_type=dt, role=drole, site=site)
        cls.dev_b = Device.objects.create(name="rr-b", device_type=dt, role=drole, site=site)
        cls.if_a = Interface.objects.create(device=cls.dev_a, name="Gi0/0", type="1000base-t")
        cls.if_b = Interface.objects.create(device=cls.dev_b, name="Gi0/0", type="1000base-t")
        cls.lo_a = Interface.objects.create(device=cls.dev_a, name="Loopback0", type="virtual")
        pool = Prefix.objects.create(prefix="198.18.2.0/24", role=Role.objects.create(name="RrP2P", slug="rrp2p"))
        cls.p2p_role = NSOLinkRole.objects.create(
            name="rr-core", slug="rr-core", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        cls.single_role = NSOLinkRole.objects.create(
            name="rr-loop", slug="rr-loop", link_type="single", assign_ipv4=True, ipv4_pool_role="loopback"
        )

    def test_no_assignment_returns_none(self):
        role, other = resolve_role(self.lo_a)
        self.assertIsNone(role)
        self.assertIsNone(other)

    def test_direct_interface_assignment(self):
        NSOLinkRoleAssignment.objects.create(role=self.single_role, interface=self.lo_a)
        role, other = resolve_role(self.lo_a)
        self.assertEqual(role, self.single_role)
        self.assertIsNone(other)

    def test_cable_derived_both_ends(self):
        cable = _make_cable(self.if_a, self.if_b)
        NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=cable)
        # Re-load: the cached cable_id on the class-level objects predates the cable.
        self.if_a.refresh_from_db()
        self.if_b.refresh_from_db()
        role_a, peer_a = resolve_role(self.if_a)
        role_b, peer_b = resolve_role(self.if_b)
        self.assertEqual(role_a, self.p2p_role)
        self.assertEqual(peer_a, self.if_b)
        self.assertEqual(role_b, self.p2p_role)
        self.assertEqual(peer_b, self.if_a)

    def test_cable_derived_peer_is_topology_only_not_management_gated(self):
        # dev_b is NOT NSO-managed; resolve_role is pure topology and still returns the peer.
        cable = _make_cable(self.if_a, self.if_b)
        NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=cable)
        self.if_a.refresh_from_db()
        role, peer = resolve_role(self.if_a)
        self.assertEqual(role, self.p2p_role)
        self.assertEqual(peer, self.if_b)

    def test_direct_interface_assignment_wins_over_cable(self):
        cable = _make_cable(self.if_a, self.if_b)
        NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=cable)
        # if_a also carries a direct single-ended assignment → that wins, other end None.
        NSOLinkRoleAssignment.objects.create(role=self.single_role, interface=self.if_a)
        role, other = resolve_role(self.if_a)
        self.assertEqual(role, self.single_role)
        self.assertIsNone(other)

    def test_derived_fallback_off_by_default(self):
        # Loopback0 classifies as "loopback"; a role named that exists, but fallback is off.
        NSOLinkRole.objects.create(
            name="loopback", slug="loopback", link_type="single", assign_ipv4=True, ipv4_pool_role="loopback"
        )
        role, other = resolve_role(self.lo_a)
        self.assertIsNone(role)

    def test_derived_fallback_on_matches_enabled_role(self):
        fb_role = NSOLinkRole.objects.create(
            name="access", slug="access", link_type="single", assign_ipv4=True, ipv4_pool_role="access-lan"
        )
        cfg = apps.get_app_config("netbox_nso_plugin")
        cfg._link_role_derived_fallback = True
        self.addCleanup(setattr, cfg, "_link_role_derived_fallback", False)
        # if_a is a plain physical interface with no tag/cable → classify_interface → "access".
        role, other = resolve_role(self.if_a)
        self.assertEqual(role, fb_role)
        self.assertIsNone(other)

    def test_derived_fallback_skips_disabled_role(self):
        NSOLinkRole.objects.create(
            name="access",
            slug="access",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_role="access-lan",
            enabled=False,
        )
        cfg = apps.get_app_config("netbox_nso_plugin")
        cfg._link_role_derived_fallback = True
        self.addCleanup(setattr, cfg, "_link_role_derived_fallback", False)
        role, other = resolve_role(self.if_a)
        self.assertIsNone(role)
