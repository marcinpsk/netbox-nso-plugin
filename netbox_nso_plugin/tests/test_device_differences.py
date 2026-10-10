# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Read NetBox and immutable observations without reconcile or native writes."""

from uuid import uuid4

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from ipam.models import VRF, IPAddress

from netbox_nso_plugin.adapter_client import bound_session
from netbox_nso_plugin.device_differences import SCOPE_SPECS, differences
from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState
from netbox_nso_plugin.observations import observation_defaults
from netbox_nso_plugin.ownership_planner import converted_scope_rules

from ._observation_case import ObservationTransport, interface_observation, ip_observation, observation
from .test_gated_reconcile import _make


class TestDeviceDifferences(TestCase):
    def setUp(self):
        self.device, self.management = _make(
            f"diff{uuid4().hex[:8]}", manage_interfaces=True, manage_description=True, manage_enabled=True
        )
        self.user = get_user_model().objects.create_superuser(username=f"diff{uuid4().hex[:8]}")
        self.interface = Interface.objects.get(device=self.device)
        self.interface.name = "Ethernet1"
        self.interface.description = "NetBox description"
        self.interface.save()

    def _snapshot(self, scope, interfaces=(), unprojectable=()):
        family = {"interface": "interface_attributes", "ip": "interface_ip"}[scope]
        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family=family)
        snapshot = observation(family, interfaces=list(interfaces), unprojectable=list(unprojectable))
        NSOFamilyObservation.objects.update_or_create(
            read_state=state, defaults=observation_defaults(family, 1, 1, snapshot)
        )

    def _rows(self, scope):
        return [row for row in differences(self.management, user=self.user) if row.scope == scope]

    def _ip(self, address="198.18.0.1/24", *, interface=None, vrf=None, assigned=True):
        return IPAddress.objects.create(
            address=address,
            vrf=vrf,
            assigned_object_type=ContentType.objects.get_for_model(Interface) if assigned else None,
            assigned_object_id=(interface or self.interface).pk if assigned else None,
        )

    def test_interface_without_read_is_unavailable(self):
        self.assertEqual(
            [(row.kind, row.reason) for row in self._rows("interface")], [("unavailable", "no successful read yet")]
        )

    def test_interface_authoritative_empty_is_netbox_only(self):
        self._snapshot("interface")
        rows = self._rows("interface")
        self.assertEqual([(row.kind, row.identity) for row in rows], [("netbox_only", "Ethernet1")])

    def test_interface_device_only_needs_no_native_anchor(self):
        self._snapshot("interface", [interface_observation("Ethernet2")])
        self.assertIn(("device_only", "Ethernet2"), [(row.kind, row.identity) for row in self._rows("interface")])

    def test_interface_mismatch_uses_managed_attributes_and_raw_values(self):
        self._snapshot("interface", [interface_observation(enabled=False)])
        rows = self._rows("interface")
        self.assertEqual({row.attribute for row in rows}, {"description", "enabled"})
        self.assertTrue(all(row.kind == "mismatch" for row in rows))
        self.assertIs(next(row.device_value for row in rows if row.attribute == "enabled"), False)
        self.management.manage_enabled = False
        self.management.save(update_fields=["manage_enabled"])
        self.assertEqual([row.attribute for row in self._rows("interface")], ["description"])

    def test_interface_description_null_and_empty_follow_existing_normalizer(self):
        self.interface.description = ""
        self.interface.save(update_fields=["description"])
        for description in (None, "", "  "):
            with self.subTest(description=description):
                self._snapshot("interface", [interface_observation(description=description)])
                self.assertEqual(self._rows("interface"), [])

    def test_interface_unprojectable_is_ambiguous(self):
        self._snapshot("interface", unprojectable=[{"index": 0, "reason": "missing name"}])
        self.assertIn(("ambiguous", "missing name"), [(row.kind, row.reason) for row in self._rows("interface")])

    def test_interface_unprojectable_native_name_is_ambiguous(self):
        self.interface.name = ""
        self.interface.save(update_fields=["name"])
        self._snapshot("interface")
        (row,) = self._rows("interface")
        self.assertEqual((row.kind, row.reason), ("ambiguous", "missing interface name"))

    def test_interface_duplicate_device_identity_is_ambiguous(self):
        item = interface_observation()
        self._snapshot("interface", [item, item])
        self.assertEqual([row.kind for row in self._rows("interface")], ["ambiguous"])

    def test_interface_coverage_limits_attribute_comparison(self):
        self._snapshot("interface", [interface_observation(enabled=False)])
        NSOFamilyObservation.objects.filter(read_state__family="interface_attributes").update(
            coverage={"attributes": ["description"]}
        )
        self.assertEqual([row.attribute for row in self._rows("interface")], ["description"])

    def test_ip_without_read_is_unavailable(self):
        self.assertEqual(
            [(row.kind, row.reason) for row in self._rows("ip")], [("unavailable", "no successful read yet")]
        )

    def test_ip_authoritative_empty_is_netbox_only(self):
        self._ip()
        self._snapshot("ip")
        self.assertEqual([row.kind for row in self._rows("ip")], ["netbox_only"])

    def test_ip_device_only_without_association(self):
        self._snapshot("ip", [ip_observation()])
        rows = self._rows("ip")
        self.assertEqual([row.kind for row in rows], ["device_only"])
        self.assertIsNone(rows[0].association_candidate)

    def test_ip_exact_identity_matches(self):
        self._ip()
        self._snapshot("ip", [ip_observation()])
        self.assertEqual(self._rows("ip"), [])

    def test_ip_prefix_length_mismatch(self):
        self._ip()
        self._snapshot("ip", [ip_observation(address="198.18.0.1/32", prefix_length=32)])
        (row,) = self._rows("ip")
        self.assertEqual(
            (row.kind, row.attribute, row.netbox_value, row.device_value), ("mismatch", "prefix_length", 24, 32)
        )

    def test_ip_null_prefix_does_not_match_zero(self):
        self._ip("198.18.0.1/0")
        self._snapshot("ip", [ip_observation(address="198.18.0.1/0", prefix_length=None)])
        (row,) = self._rows("ip")
        self.assertEqual((row.netbox_value, row.device_value), (0, None))

    def test_ip_proposes_unassigned_host_with_different_prefix(self):
        native = self._ip(assigned=False)
        self._snapshot("ip", [ip_observation(address="198.18.0.1/32", prefix_length=32)])
        (row,) = self._rows("ip")
        self.assertEqual(row.kind, "device_only")
        self.assertEqual(row.association_candidate, native)

    def test_ip_proposes_host_assigned_elsewhere(self):
        other = Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t")
        native = self._ip(interface=other)
        self._snapshot("ip", [ip_observation()])
        rows = self._rows("ip")
        self.assertEqual({row.kind for row in rows}, {"device_only", "netbox_only"})
        self.assertEqual(next(row.association_candidate for row in rows if row.kind == "device_only"), native)

    def test_ip_two_native_candidates_are_ambiguous(self):
        self._ip(assigned=False)
        self._ip("198.18.0.1/32", assigned=False)
        self._snapshot("ip", [ip_observation()])
        self.assertEqual([row.kind for row in self._rows("ip")], ["ambiguous"])

    def test_ip_two_device_candidates_are_ambiguous(self):
        self._ip()
        self._snapshot("ip", [ip_observation(), ip_observation(address="198.18.0.1/32", prefix_length=32)])
        self.assertEqual([row.kind for row in self._rows("ip")], ["ambiguous"])

    def test_ip_same_host_on_two_device_interfaces_is_ambiguous(self):
        self._ip()
        self._snapshot("ip", [ip_observation(), ip_observation(interface="Ethernet2")])
        rows = self._rows("ip")
        self.assertEqual([row.kind for row in rows], ["ambiguous", "ambiguous"])
        self.assertTrue(all(row.association_candidate is None for row in rows))

    def test_ip_duplicate_vrf_name_is_ambiguous(self):
        first = VRF.objects.create(name="example-vrf", rd="64512:1")
        VRF.objects.create(name="example-vrf", rd="64512:2")
        self._ip(vrf=first)
        self._snapshot("ip", [ip_observation(vrf="example-vrf")])
        self.assertEqual([row.kind for row in self._rows("ip")], ["ambiguous"])

    def test_ip_vrf_resolution_query_count_does_not_grow_with_vrf_names(self):
        def queries(count):
            VRF.objects.filter(name__startswith="example-vrf-").delete()
            for index in range(count):
                VRF.objects.create(name=f"example-vrf-{index}", rd=f"64512:{index + 10}")
            self._snapshot(
                "ip",
                [
                    ip_observation(f"Ethernet{index + 10}", f"198.18.{index}.1/24", vrf=f"example-vrf-{index}")
                    for index in range(count)
                ],
            )
            with CaptureQueriesContext(connection) as captured:
                differences(self.management, user=self.user)
            return len(captured)

        self.assertEqual(queries(2), queries(6))

    def test_ip_nokia_bound_port_resolves_interface_name(self):
        self._ip()
        self._snapshot("ip", [ip_observation(interface="router-interface", bound_port="Ethernet1")])
        self.assertEqual(self._rows("ip"), [])

    def test_ip_device_only_needs_no_native_interface(self):
        self._snapshot("ip", [ip_observation(interface="Ethernet2")])
        self.assertEqual([row.kind for row in self._rows("ip")], ["device_only"])

    def test_ip_same_host_in_another_vrf_is_not_an_association(self):
        vrf = VRF.objects.create(name="example-vrf", rd="64512:1")
        self._ip(vrf=vrf, assigned=False)
        self._snapshot("ip", [ip_observation()])
        (row,) = self._rows("ip")
        self.assertEqual(row.kind, "device_only")
        self.assertIsNone(row.association_candidate)

    def test_ip_ipv6_host_and_prefix_match(self):
        self._ip("2001:db8::1/64")
        entry = ip_observation(address="2001:db8::1/64", prefix_length=64)
        entry["addresses"][0]["family"] = "ipv6"
        self._snapshot("ip", [entry])
        self.assertEqual(self._rows("ip"), [])

    def test_ip_unprojectable_is_ambiguous(self):
        self._snapshot("ip", unprojectable=[{"index": 0, "reason": "missing interface"}])
        (row,) = self._rows("ip")
        self.assertEqual((row.kind, row.reason), ("ambiguous", "missing interface"))

    def test_ip_unprojectable_native_interface_is_ambiguous(self):
        self._ip()
        self.interface.name = ""
        self.interface.save(update_fields=["name"])
        self._snapshot("ip")
        (row,) = self._rows("ip")
        self.assertEqual((row.kind, row.reason), ("ambiguous", "native interface is unavailable"))

    def test_ip_invalid_address_is_ambiguous(self):
        self._snapshot("ip", [ip_observation(address="invalid")])
        self.assertEqual([row.kind for row in self._rows("ip")], ["ambiguous"])

    def test_differences_view_renders_an_empty_device_address(self):
        from django.urls import reverse

        self._snapshot("ip", [ip_observation(address="")])
        self.client.force_login(self.user)
        response = self.client.get(reverse("plugins:netbox_nso_plugin:device_nso_differences", args=[self.device.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid address")

    def test_every_converted_scope_reports_missing_observation(self):
        rows = differences(self.management, user=self.user)
        self.assertEqual(set(SCOPE_SPECS), set(converted_scope_rules()))
        self.assertEqual({row.scope for row in rows}, set(converted_scope_rules()))
        self.assertTrue(all((row.kind, row.reason) == ("unavailable", "no successful read yet") for row in rows))

    def test_differences_performs_no_writes_or_adapter_calls(self):
        self._ip()
        self._snapshot("interface", [interface_observation()])
        self._snapshot("ip", [ip_observation()])
        counts = (Interface.objects.count(), IPAddress.objects.count(), NSOFamilyObservation.objects.count())
        transport = ObservationTransport(
            self.management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )
        with bound_session(transport.session()), CaptureQueriesContext(connection) as queries:
            differences(self.management, user=self.user)
        self.assertEqual(transport.requests, [])
        self.assertFalse(any(query["sql"].lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE"} for query in queries))
        self.assertEqual(
            counts, (Interface.objects.count(), IPAddress.objects.count(), NSOFamilyObservation.objects.count())
        )
