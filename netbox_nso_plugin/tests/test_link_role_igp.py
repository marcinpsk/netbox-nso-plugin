# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Phase 5 tests for the link-role IGP consumer.

Real ORM + real IGP overlays (NSOISISInterfaceState / NSOOSPFInterfaceState):
enabling IS-IS or OSPF on an interface creates the accepted overlay with the
role's params and pushes via the existing IGP intent pipe. The only patch is the
adapter IGP push (a true external boundary).
"""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.link_role import enable_igp_for_role
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOInstance,
    NSOISISInterfaceState,
    NSOLinkRole,
    NSOOSPFInterfaceState,
)

from .mixins import IntentPushResetMixin

#: The consumer records the key in the outbox; the drain is what sends it (#1503 Appendix O).
_SCHEDULE = "netbox_nso_plugin.signals._schedule_intent_push"


class TestEnableIgpForRole(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LgMfg", slug="lgmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LgDev", slug="lgdev")
        drole = DeviceRole.objects.create(name="LgRole", slug="lgrole")
        site = Site.objects.create(name="LgSite", slug="lgsite")
        cls.inst = NSOInstance.objects.create(name="lg-nso", adapter_instance_id="lg-nso")
        cls.dev_a = Device.objects.create(name="lg-a", device_type=dt, role=drole, site=site)
        cls.dev_b = Device.objects.create(name="lg-b", device_type=dt, role=drole, site=site)

    def setUp(self):
        super().setUp()
        self.mgmt_a = NSODeviceManagement.objects.create(
            device=self.dev_a, nso_instance=self.inst, nso_device_name="lg-a", adapter_device_id=self.dev_a.pk
        )
        self.if_a = Interface.objects.create(device=self.dev_a, name="Gi0/0", type="1000base-t")
        self.lo_a = Interface.objects.create(device=self.dev_a, name="Loopback0", type="virtual")

    def test_isis_p2p_on_link(self):
        role = NSOLinkRole.objects.create(
            name="g-isis",
            slug="g-isis",
            link_type="p2p",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="isis",
            isis_circuit_type="point-to-point",
            isis_metric=10,
            isis_process_tag="CORE",
        )
        with patch(_SCHEDULE) as push:
            result = enable_igp_for_role(self.if_a, role)
        self.assertTrue(result["enabled"])
        state = NSOISISInterfaceState.objects.get(management=self.mgmt_a, interface=self.if_a, af="ipv4")
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.circuit_type, "point-to-point")
        self.assertEqual(state.metric, 10)
        self.assertEqual(state.process_tag, "CORE")
        push.assert_called_once_with((self.mgmt_a.device_id, "isis"))

    def test_isis_passive_on_loopback(self):
        role = NSOLinkRole.objects.create(
            name="g-isis-lo",
            slug="g-isis-lo",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="isis",
            isis_passive=True,
        )
        with patch(_SCHEDULE):
            enable_igp_for_role(self.lo_a, role)
        state = NSOISISInterfaceState.objects.get(management=self.mgmt_a, interface=self.lo_a, af="ipv4")
        self.assertTrue(state.passive)
        self.assertEqual(state.status, "accepted")

    def test_ospf_area_on_link(self):
        role = NSOLinkRole.objects.create(
            name="g-ospf",
            slug="g-ospf",
            link_type="p2p",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="ospf",
            ospf_area="0",
            ospf_network_type="point-to-point",
            ospf_cost=100,
            ospf_process_id="1",
        )
        with patch(_SCHEDULE) as push:
            result = enable_igp_for_role(self.if_a, role)
        self.assertTrue(result["enabled"])
        state = NSOOSPFInterfaceState.objects.get(management=self.mgmt_a, interface=self.if_a)
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.area_id, "0")
        self.assertEqual(state.network_type, "point-to-point")
        self.assertEqual(state.cost, 100)
        self.assertEqual(state.process_id, "1")
        push.assert_called_once_with((self.mgmt_a.device_id, "ospf"))

    def test_ospf_passive_loopback(self):
        role = NSOLinkRole.objects.create(
            name="g-ospf-lo",
            slug="g-ospf-lo",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="ospf",
            ospf_area="0",
            ospf_passive=True,
        )
        with patch(_SCHEDULE):
            enable_igp_for_role(self.lo_a, role)
        state = NSOOSPFInterfaceState.objects.get(management=self.mgmt_a, interface=self.lo_a)
        self.assertTrue(state.passive)

    def test_igp_none_is_noop(self):
        role = NSOLinkRole.objects.create(
            name="g-noigp",
            slug="g-noigp",
            link_type="single",
            assign_ipv4=True,
            ipv4_pool_role="loopback",
            igp="none",
        )
        with patch(_SCHEDULE):
            result = enable_igp_for_role(self.lo_a, role)
        self.assertFalse(result["enabled"])
        self.assertIsNotNone(result["skipped"])
        self.assertFalse(NSOISISInterfaceState.objects.filter(interface=self.lo_a).exists())
        self.assertFalse(NSOOSPFInterfaceState.objects.filter(interface=self.lo_a).exists())

    def test_idempotent_rerun_updates_single_row(self):
        role = NSOLinkRole.objects.create(
            name="g-idem",
            slug="g-idem",
            link_type="p2p",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="isis",
            isis_metric=5,
        )
        with patch(_SCHEDULE):
            enable_igp_for_role(self.if_a, role)
            enable_igp_for_role(self.if_a, role)
        self.assertEqual(
            NSOISISInterfaceState.objects.filter(management=self.mgmt_a, interface=self.if_a, af="ipv4").count(), 1
        )

    def test_unmanaged_device_error(self):
        role = NSOLinkRole.objects.create(
            name="g-unmgd",
            slug="g-unmgd",
            link_type="single",
            assign_ipv4=False,
            assign_ipv6=False,
            igp="isis",
        )
        if_b = Interface.objects.create(device=self.dev_b, name="Gi9/0", type="1000base-t")
        with patch(_SCHEDULE):
            result = enable_igp_for_role(if_b, role)  # dev_b not managed
        self.assertIn("not managed", result["error"])
