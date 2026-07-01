# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 1 tests for the link-role provisioning catalog.

Exercises the real ORM: DB-level constraints (cable-XOR-interface CheckConstraint,
per-cable / per-interface uniqueness), model ``clean()`` validation, and the REST
API round-trip. No mocks — these run in the devcontainer against the real DB.
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
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from ipam.models import Prefix, Role
from rest_framework import status
from utilities.testing import APITestCase

from netbox_nso_plugin.models import NSOLinkRole, NSOLinkRoleAssignment


def _make_cable(iface_a, iface_b):
    """Create a connected Cable joining two interfaces (both ends terminated)."""
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


class TestNSOLinkRoleModel(TestCase):
    """NSOLinkRole __str__, get_absolute_url, and clean() validation."""

    @classmethod
    def setUpTestData(cls):
        cls.v4pool = Prefix.objects.create(prefix="198.18.0.0/24", role=Role.objects.create(name="P2P", slug="p2p"))

    def _p2p_role(self, **overrides):
        """Return an unsaved, valid p2p role, with field overrides applied."""
        data = {
            "name": "core-link",
            "slug": "core-link",
            "link_type": "p2p",
            "assign_ipv4": True,
            "ipv4_pool_prefix": self.v4pool,
            "assign_ipv6": False,
        }
        data.update(overrides)
        return NSOLinkRole(**data)

    def test_str(self):
        role = self._p2p_role()
        self.assertEqual(str(role), "core-link")

    def test_get_absolute_url(self):
        role = self._p2p_role()
        role.save()
        self.assertEqual(
            role.get_absolute_url(),
            reverse("plugins:netbox_nso_plugin:nsolinkrole", args=[role.pk]),
        )

    def test_valid_p2p_role_passes_clean(self):
        role = self._p2p_role(ipv4_mask=31)
        role.full_clean()  # must not raise

    def test_valid_single_loopback_role_passes_clean(self):
        role = NSOLinkRole(
            name="loopback",
            slug="loopback",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_role="loopback",
        )
        role.full_clean()  # must not raise

    def test_reject_ipv4_opt_in_without_pool(self):
        role = self._p2p_role(ipv4_pool_prefix=None, ipv4_pool_role="")
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("ipv4_pool_role", ctx.exception.message_dict)

    def test_reject_ipv6_opt_in_without_pool(self):
        role = self._p2p_role(assign_ipv6=True)
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("ipv6_pool_role", ctx.exception.message_dict)

    def test_reject_mask_on_single_role(self):
        role = NSOLinkRole(
            name="single-masked",
            slug="single-masked",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_role="loopback",
            ipv4_mask=30,
        )
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("ipv4_mask", ctx.exception.message_dict)

    def test_reject_p2p_ipv4_mask_that_cannot_fit_a_host_pair(self):
        role = self._p2p_role(ipv4_mask=32)
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("ipv4_mask", ctx.exception.message_dict)

    def test_reject_p2p_ipv6_mask_that_cannot_fit_a_host_pair(self):
        role = self._p2p_role(assign_ipv6=True, ipv6_pool_role="p2p6", ipv6_mask=128)
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("ipv6_mask", ctx.exception.message_dict)

    def test_reject_isis_params_when_igp_not_isis(self):
        role = self._p2p_role(igp="none", isis_circuit_type="point-to-point")
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("igp", ctx.exception.message_dict)

    def test_reject_ospf_params_when_igp_not_ospf(self):
        role = self._p2p_role(igp="isis", ospf_area="0")
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("igp", ctx.exception.message_dict)

    def test_reject_pure_noop_role(self):
        role = NSOLinkRole(
            name="noop",
            slug="noop",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            description_template="",
            igp="none",
        )
        with self.assertRaises(ValidationError):
            role.full_clean()

    def test_description_only_role_is_valid(self):
        role = NSOLinkRole(
            name="desc-only",
            slug="desc-only",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            description_template="to {peer_host}:{peer_iface}",
            igp="none",
        )
        role.full_clean()  # description alone is a valid output

    def test_igp_only_role_is_valid(self):
        role = NSOLinkRole(
            name="isis-only",
            slug="isis-only",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="isis",
            isis_passive=True,
        )
        role.full_clean()  # IGP alone is a valid output


class TestNSOLinkRoleAssignmentModel(TestCase):
    """NSOLinkRoleAssignment DB constraints and clean() validation."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LrMfg", slug="lrmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LrDev", slug="lrdev")
        drole = DeviceRole.objects.create(name="LrRole", slug="lrrole")
        site = Site.objects.create(name="LrSite", slug="lrsite")
        cls.dev_a = Device.objects.create(name="lr-a", device_type=dt, role=drole, site=site)
        cls.dev_b = Device.objects.create(name="lr-b", device_type=dt, role=drole, site=site)
        cls.if_a = Interface.objects.create(device=cls.dev_a, name="Gi0/0", type="1000base-t")
        cls.if_b = Interface.objects.create(device=cls.dev_b, name="Gi0/0", type="1000base-t")
        cls.lo_a = Interface.objects.create(device=cls.dev_a, name="Loopback0", type="virtual")
        cls.cable = _make_cable(cls.if_a, cls.if_b)
        pool = Prefix.objects.create(prefix="198.18.1.0/24", role=Role.objects.create(name="LrP2P", slug="lrp2p"))
        cls.p2p_role = NSOLinkRole.objects.create(
            name="lr-core", slug="lr-core", link_type="p2p", assign_ipv4=True, ipv4_pool_prefix=pool
        )
        cls.single_role = NSOLinkRole.objects.create(
            name="lr-loop", slug="lr-loop", link_type="single", assign_ipv4=True, ipv4_pool_role="loopback"
        )

    def test_cable_assignment_str_and_url(self):
        a = NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=self.cable)
        self.assertIn("lr-core", str(a))
        self.assertEqual(
            a.get_absolute_url(),
            reverse("plugins:netbox_nso_plugin:nsolinkroleassignment", args=[a.pk]),
        )

    def test_db_rejects_both_null(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=None, interface=None)

    def test_db_rejects_both_set(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=self.cable, interface=self.lo_a)

    def test_db_unique_per_cable(self):
        NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=self.cable)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NSOLinkRoleAssignment.objects.create(role=self.p2p_role, cable=self.cable)

    def test_db_unique_per_interface(self):
        NSOLinkRoleAssignment.objects.create(role=self.single_role, interface=self.lo_a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NSOLinkRoleAssignment.objects.create(role=self.single_role, interface=self.lo_a)

    def test_clean_rejects_both_null(self):
        a = NSOLinkRoleAssignment(role=self.p2p_role)
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_clean_rejects_both_set(self):
        a = NSOLinkRoleAssignment(role=self.p2p_role, cable=self.cable, interface=self.lo_a)
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_clean_rejects_p2p_role_on_interface(self):
        a = NSOLinkRoleAssignment(role=self.p2p_role, interface=self.lo_a)
        with self.assertRaises(ValidationError) as ctx:
            a.full_clean()
        self.assertIn("cable", ctx.exception.message_dict)

    def test_clean_rejects_single_role_on_cable(self):
        a = NSOLinkRoleAssignment(role=self.single_role, cable=self.cable)
        with self.assertRaises(ValidationError) as ctx:
            a.full_clean()
        self.assertIn("interface", ctx.exception.message_dict)

    def test_clean_accepts_p2p_role_on_cable(self):
        a = NSOLinkRoleAssignment(role=self.p2p_role, cable=self.cable)
        a.full_clean()  # must not raise

    def test_clean_accepts_single_role_on_interface(self):
        a = NSOLinkRoleAssignment(role=self.single_role, interface=self.lo_a)
        a.full_clean()  # must not raise


class TestNSOLinkRoleAPI(APITestCase):
    """REST round-trip for the link-role catalog."""

    model = NSOLinkRole
    view_namespace = "plugins-api:netbox_nso_plugin"
    user_permissions = (
        "netbox_nso_plugin.view_nsolinkrole",
        "netbox_nso_plugin.add_nsolinkrole",
        "netbox_nso_plugin.change_nsolinkrole",
        "netbox_nso_plugin.delete_nsolinkrole",
    )

    @classmethod
    def setUpTestData(cls):
        cls.role = NSOLinkRole.objects.create(
            name="api-core", slug="api-core", link_type="p2p", assign_ipv4=True, ipv4_pool_role="p2p-core"
        )

    def test_list(self):
        response = self.client.get(self._get_list_url(), **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

    def test_create_round_trip(self):
        data = {
            "name": "api-loop",
            "slug": "api-loop",
            "link_type": "single",
            "assign_ipv4": True,
            "ipv4_pool_role": "loopback",
            "igp": "isis",
            "isis_passive": True,
        }
        response = self.client.post(self._get_list_url(), data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        created = NSOLinkRole.objects.get(slug="api-loop")
        self.assertEqual(created.igp, "isis")
        self.assertTrue(created.isis_passive)


class TestNSOLinkRoleAssignmentAPI(APITestCase):
    """REST round-trip for link-role assignments."""

    model = NSOLinkRoleAssignment
    view_namespace = "plugins-api:netbox_nso_plugin"
    user_permissions = (
        "netbox_nso_plugin.view_nsolinkroleassignment",
        "netbox_nso_plugin.add_nsolinkroleassignment",
        "netbox_nso_plugin.change_nsolinkroleassignment",
        "netbox_nso_plugin.delete_nsolinkroleassignment",
    )

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="ApiLrMfg", slug="apilrmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="ApiLrDev", slug="apilrdev")
        drole = DeviceRole.objects.create(name="ApiLrRole", slug="apilrrole")
        site = Site.objects.create(name="ApiLrSite", slug="apilrsite")
        dev = Device.objects.create(name="api-lr-a", device_type=dt, role=drole, site=site)
        cls.lo = Interface.objects.create(device=dev, name="Loopback0", type="virtual")
        cls.single_role = NSOLinkRole.objects.create(
            name="api-lr-loop", slug="api-lr-loop", link_type="single", assign_ipv4=True, ipv4_pool_role="loopback"
        )

    def test_create_interface_assignment(self):
        data = {"role": self.single_role.pk, "interface": self.lo.pk}
        response = self.client.post(self._get_list_url(), data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(NSOLinkRoleAssignment.objects.filter(interface=self.lo).exists())
