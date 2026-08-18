# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 4 tests for the link-role description consumer.

Real ORM + real interface-ownership path (NSOInterfaceState): renders the M8
template on both ends, records durable interface intent, is idempotent, and validates
template placeholders at save time.
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
from django.test import TestCase

from netbox_nso_plugin.link_role import apply_description_for_role
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceState, NSOLinkRole

from ._outbox_case import entries
from .mixins import IntentPushResetMixin


def _make_cable(iface_a, iface_b):
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


class TestApplyDescriptionForRole(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LdMfg", slug="ldmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LdDev", slug="lddev")
        cls.drole = DeviceRole.objects.create(name="LdRole", slug="ldrole")
        cls.site = Site.objects.create(name="LdSite", slug="ldsite")
        cls.inst = NSOInstance.objects.create(name="ld-nso", adapter_instance_id="ld-nso")
        cls.dev_a = Device.objects.create(name="ld-a", device_type=dt, role=cls.drole, site=cls.site)
        cls.dev_b = Device.objects.create(name="ld-b", device_type=dt, role=cls.drole, site=cls.site)

    def setUp(self):
        super().setUp()
        self.mgmt_a = NSODeviceManagement.objects.create(
            device=self.dev_a, nso_instance=self.inst, nso_device_name="ld-a", adapter_device_id=self.dev_a.pk
        )
        self.if_a = Interface.objects.create(device=self.dev_a, name="Gi0/0", type="1000base-t")
        self.if_b = Interface.objects.create(device=self.dev_b, name="Gi1/0", type="1000base-t")
        self.lo_a = Interface.objects.create(device=self.dev_a, name="Loopback0", type="virtual")

    def _role(self, template, link_type="p2p"):
        return NSOLinkRole.objects.create(
            name=f"ld-{template[:8]}-{link_type}",
            slug=f"ld-{abs(hash(template)) % 10000}-{link_type}",
            link_type=link_type,
            assign_ipv4=False,
            assign_ipv6=False,
            description_template=template,
        )

    def test_renders_peer_facts_p2p(self):
        role = self._role("to {peer_host}:{peer_iface}")
        result = apply_description_for_role(self.if_a, role, other_end=self.if_b)
        self.if_a.refresh_from_db()
        self.assertEqual(self.if_a.description, "to ld-b:Gi1/0")
        self.assertTrue(result["changed"])
        state = NSOInterfaceState.objects.get(interface=self.if_a, attribute="description")
        self.assertEqual(state.status, "accepted")
        self.assertIsNotNone(state.accepted_at)

    def test_single_ended_blank_peer_placeholders(self):
        role = self._role("{self_host} {self_iface} loopback", link_type="single")
        apply_description_for_role(self.lo_a, role)
        self.lo_a.refresh_from_db()
        self.assertEqual(self.lo_a.description, "ld-a Loopback0 loopback")

    def test_blank_template_is_noop(self):
        role = NSOLinkRole.objects.create(
            name="ld-noipdesc",
            slug="ld-noipdesc",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_role="loopback",
            description_template="",
        )
        self.lo_a.description = "keep me"
        self.lo_a.save()
        result = apply_description_for_role(self.lo_a, role)
        self.lo_a.refresh_from_db()
        self.assertEqual(self.lo_a.description, "keep me")
        self.assertIsNotNone(result["skipped"])
        self.assertFalse(NSOInterfaceState.objects.filter(interface=self.lo_a, attribute="description").exists())

    def test_idempotent_rerun(self):
        role = self._role("link to {peer_host}")
        first = apply_description_for_role(self.if_a, role, other_end=self.if_b)
        second = apply_description_for_role(self.if_a, role, other_end=self.if_b)
        self.if_a.refresh_from_db()
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(self.if_a.description, "link to ld-b")

    def test_unmanaged_device_error(self):
        role = self._role("to {peer_host}", link_type="single")
        result = apply_description_for_role(self.if_b, role)  # dev_b is not managed
        self.assertIn("not managed", result["error"])

    def test_records_durable_work_when_owned(self):
        role = self._role("to {peer_host}")
        apply_description_for_role(self.if_a, role, other_end=self.if_b)
        self.assertTrue(entries(self.dev_a, "interface", unconsumed=True))


class TestDescriptionTemplateValidation(TestCase):
    """clean() rejects templates that use placeholders outside the M8 known set."""

    def test_reject_unknown_placeholder(self):
        role = NSOLinkRole(
            name="bad-tmpl",
            slug="bad-tmpl",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            description_template="to {bogus_field}",
        )
        with self.assertRaises(ValidationError) as ctx:
            role.full_clean()
        self.assertIn("description_template", ctx.exception.message_dict)

    def test_accept_known_placeholders(self):
        role = NSOLinkRole(
            name="ok-tmpl",
            slug="ok-tmpl",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            description_template="{self_host} {self_iface} → {peer_host} {peer_iface} {peer_site} {peer_role}",
        )
        role.full_clean()  # must not raise
