# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A shared policy membership change has priority over source-plan validation."""

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection
from django.urls import reverse
from netbox_routing.models import CustomPrefix, PrefixList, PrefixListEntry

from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOIntentOutboxState,
    NSOIntentRevision,
    NSORoutePolicyObjectClass,
    NSORoutePolicyState,
)
from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

from ._authority_case import AdapterCase, concurrent_writer, grant
from ._outbox_case import make_managed, without_commit_drain


class TestPolicyMembershipConflict(AdapterCase):
    def setUp(self):
        super().setUp()
        self.first, self.first_management = make_managed("policy-race-first", 945)
        self.second, self.second_management = make_managed("policy-race-second", 946)
        self.third, self.third_management = make_managed("policy-race-third", 947)
        with without_commit_drain():
            for device, prefix in ((self.first, "198.18.10.0/24"), (self.second, "198.18.11.0/24")):
                reconcile_route_policy(
                    device,
                    {
                        "prefix_lists": [
                            {
                                "name": "CONCURRENT-POLICY",
                                "family": 4,
                                "entries": [{"sequence": 10, "action": "permit", "prefix": prefix}],
                            }
                        ]
                    },
                )
        self.selected = NSORoutePolicyState.objects.get(management=self.second_management)
        self.client.raise_request_exception = False

    def _login(self, *, authorized):
        user = get_user_model().objects.create_user(username="policy-race-operator")
        grant(user, ("view",), Device)
        grant(user, ("view",), NSODeviceManagement)
        constraints = None if authorized else {"pk__in": [self.first_management.pk, self.second_management.pk]}
        grant(user, ("change",), NSODeviceManagement, constraints)
        for model in (PrefixList, PrefixListEntry, CustomPrefix):
            grant(user, ("view", "add", "change", "delete"), model)
        self.client.force_login(user)

    @staticmethod
    def _snapshot():
        return {
            model._meta.label_lower: list(model.objects.order_by("pk").values())
            for model in (
                PrefixList,
                PrefixListEntry,
                CustomPrefix,
                NSORoutePolicyState,
                NSORoutePolicyObjectClass,
                NSOIntentRevision,
                NSOIntentOutboxEntry,
                NSOIntentOutboxState,
            )
        }

    def _attach_while_waiting(self, action, *, authorized):
        self._login(authorized=authorized)
        before = self._snapshot()
        attached = False

        def concurrent_attachment(execute, sql, params, many, context):
            nonlocal attached
            if not attached and "pg_advisory_xact_lock" in str(sql):
                attached = True
                with concurrent_writer(), without_commit_drain():
                    NSORoutePolicyState.objects.create(
                        management=self.third_management,
                        family=self.selected.family,
                        object_name=self.selected.object_name,
                        content_type=self.selected.content_type,
                        object_id=self.selected.object_id,
                        status="imported",
                    )
            return execute(sql, params, many, context)

        with connection.execute_wrapper(concurrent_attachment), self.assert_refused_whole():
            response = self.client.post(
                reverse(
                    f"plugins:netbox_nso_plugin:routing_{action}_route_policy",
                    kwargs={"pk": self.selected.pk},
                ),
                {"mode": "master"} if action == "classify" else {},
            )
        self.assertTrue(attached)
        self.assertRedirects(
            response, reverse("dcim:device_nso", kwargs={"pk": self.second.pk}), fetch_redirect_response=False
        )
        self.assertTrue(list(get_messages(response.wsgi_request)))
        self.assertEqual(self._snapshot(), before)

    def test_materialize_conflicts_with_a_late_permitted_management(self):
        self._attach_while_waiting("materialize", authorized=True)

    def test_materialize_conflicts_with_a_late_excluded_management(self):
        self._attach_while_waiting("materialize", authorized=False)

    def test_master_classification_conflicts_with_a_late_permitted_management(self):
        self._attach_while_waiting("classify", authorized=True)

    def test_master_classification_conflicts_with_a_late_excluded_management(self):
        self._attach_while_waiting("classify", authorized=False)
