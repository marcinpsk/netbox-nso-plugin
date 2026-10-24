# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Edits of shared native routing rows require every affected management."""

from uuid import uuid4

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from netbox_routing.models import RouteMap, StaticRoute

from netbox_nso_plugin.models import NSODeviceManagement, NSORoutePolicyState, NSOStaticRouteState

from ._authority_case import AdapterCase, grant
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay


class TestSharedRouteEdits(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device_a, cls.management_a = make_managed("route-edit-a", 17205)
        cls.device_b, cls.management_b = make_managed("route-edit-b", 17206)
        cls.route = StaticRoute.objects.create(prefix="198.18.0.0/24", next_hop="198.18.1.1", metric=10)
        cls.route.devices.add(cls.device_a, cls.device_b)
        cls.route_map = RouteMap.objects.create(name="SHARED-POLICY")
        content_type = ContentType.objects.get_for_model(RouteMap)
        with without_commit_drain():
            cls.static_a, cls.static_b = (
                acquire_overlay(
                    NSOStaticRouteState,
                    management=management,
                    static_route=cls.route,
                    nso_prefix="198.18.0.0/24",
                    nso_next_hop="198.18.1.1",
                    status="in_sync",
                )
                for management in (cls.management_a, cls.management_b)
            )
            cls.policy_a, cls.policy_b = (
                acquire_overlay(
                    NSORoutePolicyState,
                    management=management,
                    family="route_map",
                    object_name="SHARED-POLICY",
                    content_type=content_type,
                    object_id=cls.route_map.pk,
                    status="in_sync",
                )
                for management in (cls.management_a, cls.management_b)
            )

    def _user(self, *, both=True, superuser=False):
        user = get_user_model().objects.create_user(username=f"shared-edit-{uuid4().hex[:8]}", is_superuser=superuser)
        grant(user, ("view",), Device)
        grant(user, ("view",), NSODeviceManagement)
        grant(user, ("change",), NSODeviceManagement, None if both else {"pk": self.management_a.pk})
        grant(user, ("view", "change"), StaticRoute)
        grant(user, ("view", "change"), RouteMap)
        return user

    def _edit(self, family, row, values, user):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": family, "pk": row.pk}),
            values,
        )

    def test_static_route_edit_refuses_a_second_management(self):
        with self.assert_refused_whole():
            response = self._edit("static_route", self.static_a, {"metric": "25"}, self._user(both=False))
        self.assertEqual(response.status_code, 404)
        self.route.refresh_from_db()
        self.static_b.refresh_from_db()
        self.assertEqual((self.route.metric, self.static_b.status), (10, "in_sync"))

    def test_static_route_edit_includes_native_assignments_without_an_overlay(self):
        route = StaticRoute.objects.create(prefix="198.18.2.0/24", next_hop="198.18.1.1", metric=10)
        route.devices.add(self.device_a, self.device_b)
        with without_commit_drain():
            state = acquire_overlay(
                NSOStaticRouteState,
                management=self.management_a,
                static_route=route,
                nso_prefix="198.18.2.0/24",
                nso_next_hop="198.18.1.1",
                status="in_sync",
            )
        with self.assert_refused_whole():
            response = self._edit("static_route", state, {"metric": "25"}, self._user(both=False))
        self.assertEqual(response.status_code, 404)
        route.refresh_from_db()
        self.assertEqual(route.metric, 10)

    def test_static_route_edit_updates_both_permitted_managements(self):
        response = self._edit("static_route", self.static_a, {"metric": "25"}, self._user())
        self.assertEqual(response.status_code, 200, response.content)
        self.route.refresh_from_db()
        self.static_b.refresh_from_db()
        self.assertEqual((self.route.metric, self.static_b.status), (25, "accepted"))

    def test_route_map_rename_refuses_a_second_management(self):
        with self.assert_refused_whole():
            response = self._edit(
                "route_map_name", self.policy_a, {"object_name": "RENAMED-POLICY"}, self._user(both=False)
            )
        self.assertEqual(response.status_code, 404)
        self.route_map.refresh_from_db()
        self.policy_b.refresh_from_db()
        self.assertEqual((self.route_map.name, self.policy_b.object_name), ("SHARED-POLICY", "SHARED-POLICY"))

    def test_route_map_rename_updates_both_permitted_managements(self):
        response = self._edit("route_map_name", self.policy_a, {"object_name": "RENAMED-POLICY"}, self._user())
        self.assertEqual(response.status_code, 200, response.content)
        self.route_map.refresh_from_db()
        self.policy_b.refresh_from_db()
        self.assertEqual((self.route_map.name, self.policy_b.object_name), ("RENAMED-POLICY", "RENAMED-POLICY"))

    def test_shared_edits_still_succeed_for_a_superuser(self):
        user = self._user(superuser=True)
        self.assertEqual(self._edit("static_route", self.static_a, {"metric": "25"}, user).status_code, 200)
        self.assertEqual(
            self._edit("route_map_name", self.policy_a, {"object_name": "RENAMED-POLICY"}, user).status_code, 200
        )
