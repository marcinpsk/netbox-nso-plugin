# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Authorize and render the read-only Differences panel through real Django views."""

from uuid import uuid4

from core.models import ObjectType
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from users.models import ObjectPermission

from netbox_nso_plugin.adapter_client import bound_session
from netbox_nso_plugin.models import NSODeviceManagement, NSOFamilyObservation, NSOFamilyReadState
from netbox_nso_plugin.observations import observation_defaults

from ._observation_case import ObservationTransport, interface_observation, observation
from .test_gated_reconcile import _make


class TestDeviceDifferencesView(TestCase):
    def setUp(self):
        self.device, self.management = _make(f"panel{uuid4().hex[:8]}", manage_description=True)
        self.user = self._viewer()
        self.client.force_login(self.user)
        state = NSOFamilyReadState.objects.create(management=self.management, family="interface_attributes")
        snapshot = observation("interface_attributes", interfaces=[interface_observation("Ethernet2")])
        NSOFamilyObservation.objects.create(
            read_state=state, **observation_defaults("interface_attributes", 1, 1, snapshot)
        )
        self.url = reverse("plugins:netbox_nso_plugin:device_nso_differences", kwargs={"pk": self.device.pk})

    @staticmethod
    def _viewer(*, interfaces=True):
        from dcim.models import Device, Interface

        user = get_user_model().objects.create_user(username=f"panel{uuid4().hex[:8]}")
        permission = ObjectPermission.objects.create(name=f"View differences {user.username}", actions=["view"])
        models = [Device, NSODeviceManagement] + ([Interface] if interfaces else [])
        permission.object_types.add(*(ObjectType.objects.get_for_model(model) for model in models))
        permission.users.add(user)
        return user

    def test_permitted_get_renders_rows_and_snapshot_without_writes(self):
        transport = ObservationTransport(
            self.management.adapter_device_id, observation("interface_attributes"), observation("interface_ip")
        )
        with bound_session(transport.session()), CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ethernet2")
        self.assertContains(response, "interface_attributes")
        self.assertContains(response, "Revision 1")
        self.assertContains(response, "Observed")
        self.assertEqual(transport.requests, [])
        self.assertFalse(
            any(
                query["sql"].lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE"}
                and any(prefix in query["sql"] for prefix in ("dcim_", "ipam_", "netbox_nso_plugin_"))
                for query in queries
            )
        )

    def test_user_without_view_permission_is_refused(self):
        user = get_user_model().objects.create_user(username=f"denied{uuid4().hex[:8]}")
        self.client.force_login(user)
        self.assertIn(self.client.get(self.url).status_code, (403, 404))

    def test_device_view_permission_without_management_permission_is_refused(self):
        from dcim.models import Device

        user = get_user_model().objects.create_user(username=f"partial{uuid4().hex[:8]}")
        permission = ObjectPermission.objects.create(name="View device only", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(Device))
        permission.users.add(user)
        self.client.force_login(user)
        self.assertIn(self.client.get(self.url).status_code, (403, 404))

    def test_object_restricted_management_is_refused(self):
        from dcim.models import Device

        user = get_user_model().objects.create_user(username=f"restricted{uuid4().hex[:8]}")
        device_permission = ObjectPermission.objects.create(name="View devices", actions=["view"])
        device_permission.object_types.add(ObjectType.objects.get_for_model(Device))
        device_permission.users.add(user)
        management_permission = ObjectPermission.objects.create(
            name="View other management rows", actions=["view"], constraints={"pk": self.management.pk + 1}
        )
        management_permission.object_types.add(ObjectType.objects.get_for_model(NSODeviceManagement))
        management_permission.users.add(user)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_scope_and_kind_filters_preserve_counts(self):
        response = self.client.get(self.url, {"scope": "interface", "kind": "device_only"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertEqual(response.context["page_obj"][0]["identity"], "Ethernet2")
        self.assertEqual(response.context["counts"]["netbox_only"], 1)
        self.assertEqual(response.context["counts"]["device_only"], 1)

    def test_panel_is_paginated(self):
        state = NSOFamilyReadState.objects.get(management=self.management, family="interface_attributes")
        snapshot = observation(
            "interface_attributes", interfaces=[interface_observation(f"Ethernet{number:03}") for number in range(60)]
        )
        NSOFamilyObservation.objects.filter(read_state=state).update(
            **observation_defaults("interface_attributes", 1, 1, snapshot)
        )
        response = self.client.get(self.url, {"scope": "interface", "kind": "device_only", "page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(len(response.context["page_obj"]), 10)

    def test_post_is_refused(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_unsupported_scopes_collapse_into_one_line_and_ip_identity_is_readable(self):
        from ._observation_case import ip_observation

        state = NSOFamilyReadState.objects.create(management=self.management, family="interface_ip")
        snapshot = observation("interface_ip", interfaces=[ip_observation("Ethernet2", vrf="example-vrf")])
        NSOFamilyObservation.objects.create(read_state=state, **observation_defaults("interface_ip", 1, 1, snapshot))
        response = self.client.get(self.url)
        self.assertContains(response, "Not compared yet: ", count=1)
        self.assertNotContains(response, "not supported yet")
        self.assertContains(response, "Ethernet2 198.18.0.1/24 (VRF example-vrf)")
        self.assertNotContains(response, "&#x27;Ethernet2&#x27;")

    def _candidate_page(self):
        from ipam.models import IPAddress

        from ._observation_case import ip_observation

        candidate = IPAddress.objects.create(address="198.18.0.1/32")
        state = NSOFamilyReadState.objects.create(management=self.management, family="interface_ip")
        snapshot = observation("interface_ip", interfaces=[ip_observation("Ethernet2")])
        NSOFamilyObservation.objects.create(read_state=state, **observation_defaults("interface_ip", 1, 1, snapshot))
        return candidate

    def _ip_rows(self, response):
        return [row for row in response.context["page_obj"] if row["scope"] == "ip" and row["kind"] == "device_only"]

    def test_association_candidate_the_user_cannot_view_is_not_rendered(self):
        candidate = self._candidate_page()
        response = self.client.get(self.url, {"scope": "ip"})
        self.assertEqual(response.status_code, 200)
        (row,) = self._ip_rows(response)
        self.assertIsNone(row["association_candidate"])
        self.assertNotContains(response, "Proposed association")
        self.assertNotContains(response, candidate.get_absolute_url())

    def test_association_candidate_the_user_can_view_is_rendered(self):
        from ipam.models import IPAddress

        candidate = self._candidate_page()
        permission = ObjectPermission.objects.create(
            name="View the candidate", actions=["view"], constraints={"pk": candidate.pk}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(IPAddress))
        permission.users.add(self.user)
        response = self.client.get(self.url, {"scope": "ip"})
        (row,) = self._ip_rows(response)
        self.assertEqual(row["association_candidate"], candidate)
        self.assertContains(response, f"(IP #{candidate.pk})")

    def test_hidden_duplicate_candidate_does_not_make_the_row_ambiguous(self):
        from ipam.models import IPAddress

        candidate = self._candidate_page()
        IPAddress.objects.create(address="198.18.0.1/31")
        permission = ObjectPermission.objects.create(
            name="View the candidate only", actions=["view"], constraints={"pk": candidate.pk}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(IPAddress))
        permission.users.add(self.user)
        response = self.client.get(self.url, {"scope": "ip"})
        (row,) = self._scope_rows(response, "ip")
        self.assertEqual((row["kind"], row["reason"]), ("device_only", ""))
        self.assertEqual(row["association_candidate"], candidate)
        self.assertNotContains(response, "multiple addresses have the same host and VRF")

    def _scope_rows(self, response, scope):
        return [row for row in response.context["page_obj"] if row["scope"] == scope]

    def _ip_snapshot(self, *interfaces):
        state = NSOFamilyReadState.objects.create(management=self.management, family="interface_ip")
        snapshot = observation("interface_ip", interfaces=list(interfaces))
        NSOFamilyObservation.objects.create(read_state=state, **observation_defaults("interface_ip", 1, 1, snapshot))

    def _assigned_ip(self, address):
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        interface = Interface.objects.get(device=self.device, name="lag-60")
        return IPAddress.objects.create(
            address=address,
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=interface.pk,
        )

    def test_interface_the_user_cannot_view_is_not_listed(self):
        self.client.force_login(self._viewer(interfaces=False))
        response = self.client.get(self.url, {"scope": "interface"})
        self.assertEqual([row["identity"] for row in self._scope_rows(response, "interface")], ["Ethernet2"])
        self.assertNotContains(response, "lag-60")

    def test_interface_the_user_cannot_view_but_the_device_reports_is_ambiguous(self):
        state = NSOFamilyReadState.objects.get(management=self.management, family="interface_attributes")
        snapshot = observation("interface_attributes", interfaces=[interface_observation("lag-60")])
        NSOFamilyObservation.objects.filter(read_state=state).update(
            **observation_defaults("interface_attributes", 1, 1, snapshot)
        )
        self.client.force_login(self._viewer(interfaces=False))
        response = self.client.get(self.url, {"scope": "interface"})
        (row,) = self._scope_rows(response, "interface")
        self.assertEqual((row["kind"], row["identity"]), ("ambiguous", "lag-60"))
        self.assertEqual(row["reason"], "NetBox object is not visible to you")
        self.assertEqual(row["netbox_value"], "missing")

    def test_ip_the_user_cannot_view_is_not_listed(self):
        self._assigned_ip("198.18.0.9/24")
        self._ip_snapshot()
        response = self.client.get(self.url, {"scope": "ip"})
        self.assertEqual(self._scope_rows(response, "ip"), [])
        self.assertNotContains(response, "198.18.0.9")

    def test_ip_the_user_cannot_view_but_the_device_reports_is_ambiguous_without_its_prefix(self):
        from ._observation_case import ip_observation

        self._assigned_ip("198.18.0.9/24")
        self._ip_snapshot(ip_observation("lag-60", "198.18.0.9/32", prefix_length=32))
        response = self.client.get(self.url, {"scope": "ip"})
        (row,) = self._scope_rows(response, "ip")
        self.assertEqual((row["kind"], row["identity"]), ("ambiguous", "lag-60 198.18.0.9/32"))
        self.assertEqual(row["reason"], "NetBox object is not visible to you")
        self.assertEqual(row["netbox_value"], "missing")

    def test_ip_the_user_can_view_is_compared(self):
        from ipam.models import IPAddress

        native = self._assigned_ip("198.18.0.9/24")
        permission = ObjectPermission.objects.create(
            name="View the IP", actions=["view"], constraints={"pk": native.pk}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(IPAddress))
        permission.users.add(self.user)
        self._ip_snapshot()
        response = self.client.get(self.url, {"scope": "ip"})
        (row,) = self._scope_rows(response, "ip")
        self.assertEqual((row["kind"], row["identity"]), ("netbox_only", "lag-60 198.18.0.9/24"))
