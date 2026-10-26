# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A policy rename includes managements that hold only a dependent fallback."""

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from netbox_routing.models import RouteMap

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOIntentOutboxState,
    NSOIntentRevision,
    NSORedistributionState,
    NSORoutePolicyState,
)

from ._authority_case import AdapterCase, grant
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay


class TestRouteMapDependentAuthorization(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.first, cls.first_management = make_managed("policy-dependent-first", 943)
        cls.second, cls.second_management = make_managed("policy-dependent-second", 944)
        cls.route_map = RouteMap.objects.create(name="FALLBACK-POLICY")
        with without_commit_drain():
            cls.policy = acquire_overlay(
                NSORoutePolicyState,
                management=cls.first_management,
                family="route_map",
                object_name=cls.route_map.name,
                content_type=ContentType.objects.get_for_model(RouteMap),
                object_id=cls.route_map.pk,
                status="in_sync",
            )
            cls.dependent = acquire_overlay(
                NSORedistributionState,
                management=cls.second_management,
                dest_protocol="ospf",
                dest_ref="1",
                source_protocol="connected",
                route_map=cls.route_map.name,
                status="in_sync",
            )

    def _login(self, *, both):
        user = get_user_model().objects.create_user(username="policy-dependent-operator")
        grant(user, ("view",), Device)
        grant(user, ("view",), NSODeviceManagement)
        grant(user, ("change",), NSODeviceManagement, None if both else {"pk": self.first_management.pk})
        grant(user, ("view", "change"), RouteMap)
        self.client.force_login(user)

    def _post(self):
        return self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:overlay_field_edit",
                kwargs={"key": "route_map_name", "pk": self.policy.pk},
            ),
            {"object_name": "RENAMED-FALLBACK-POLICY"},
        )

    def _snapshot(self):
        return {
            model._meta.label_lower: list(model.objects.order_by("pk").values())
            for model in (
                RouteMap,
                NSORoutePolicyState,
                NSORedistributionState,
                NSOIntentRevision,
                NSOIntentOutboxEntry,
                NSOIntentOutboxState,
            )
        }

    def test_rename_refuses_a_management_with_only_a_fallback_dependency(self):
        self._login(both=False)
        before = self._snapshot()
        with self.assert_refused_whole():
            response = self._post()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._snapshot(), before)

    def test_rename_updates_a_permitted_management_with_only_a_fallback_dependency(self):
        self._login(both=True)
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        self.route_map.refresh_from_db()
        self.policy.refresh_from_db()
        self.dependent.refresh_from_db()
        self.assertEqual(self.route_map.name, "RENAMED-FALLBACK-POLICY")
        self.assertEqual(self.policy.object_name, "RENAMED-FALLBACK-POLICY")
        self.assertEqual(self.dependent.route_map, "RENAMED-FALLBACK-POLICY")
        self.assertIsNone(self.dependent.redistribution_id)
        self.assertEqual(self.dependent.status, "accepted")
