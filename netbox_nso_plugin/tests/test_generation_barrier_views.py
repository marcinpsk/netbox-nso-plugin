# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Operator exits from a blocked Apply generation."""

from unittest.mock import patch

from core.models import ObjectType
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TransactionTestCase
from django.urls import reverse
from users.models import ObjectPermission

from netbox_nso_plugin.models import NSODeviceManagement

from ._adapter_http import make_response, make_session
from ._outbox_case import CFG, make_managed
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestGenerationBarrierViews(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("barrier-action", 1623)
        self.user = get_user_model().objects.create_superuser(
            username="barrier-admin",
            password="test-password-1623",
            email="barrier-admin@test.example",
        )
        self.client.force_login(self.user)

    def _url(self, action):
        return reverse(
            f"plugins:netbox_nso_plugin:generation_{action}",
            args=[self.device.pk],
        )

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_retry_and_abandon_target_the_generation_in_the_url(self, mock_session, _config):
        session = make_session()
        session.request.side_effect = [
            make_response(202, {"generation_id": 73, "seq": 11, "job_id": 901}),
            make_response(202, {"generation_id": 73, "seq": 11, "job_id": 902}),
        ]
        mock_session.return_value = session

        retry = self.client.post(self._url("retry"), {"generation_id": 73})
        abandon = self.client.post(self._url("abandon"), {"generation_id": 73})

        self.assertEqual(retry.status_code, 302)
        self.assertEqual(abandon.status_code, 302)
        self.assertEqual(
            [call.kwargs["json"] for call in session.request.call_args_list],
            [{"generation_id": 73}, {"generation_id": 73}],
        )
        device_actions = f"{CFG['url']}/api/v1/devices/{self.management.adapter_device_id}/actions"
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in session.request.call_args_list],
            [
                ("POST", f"{device_actions}/retry-generation"),
                ("POST", f"{device_actions}/abandon-generation"),
            ],
        )

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_moved_head_conflict_names_the_current_generation(self, mock_session, _config):
        mock_session.return_value = make_session(
            status_code=409,
            json_data={
                "error": {
                    "code": "conflict",
                    "message": "Generation is not the current blocked head",
                    "detail": {"head_generation_id": 74, "head_status": "failed"},
                }
            },
        )

        response = self.client.post(self._url("retry"), {"generation_id": 73})

        self.assertEqual(response.status_code, 302)
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(messages, ["Generation 73 moved. The current blocked head is generation 74."])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_running_action_conflict_names_the_existing_job(self, mock_session, _config):
        mock_session.return_value = make_session(
            status_code=409,
            json_data={
                "error": {
                    "code": "conflict",
                    "message": "An action is already running",
                    "detail": {"job_id": 902},
                }
            },
        )

        response = self.client.post(self._url("retry"), {"generation_id": 73})

        self.assertEqual(response.status_code, 302)
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(messages, ["An action is already running. Job ID: 902."])

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_generation_actions_enforce_management_permission_constraints(self, mock_session, _config):
        session = make_session(status_code=202, json_data={"generation_id": 73, "seq": 11, "job_id": 901})
        mock_session.return_value = session
        other_device, _ = make_managed("barrier-other", 1624)
        user = get_user_model().objects.create_user(username="barrier-operator")
        permission = ObjectPermission.objects.create(
            name="Change one management row",
            actions=["change"],
            constraints={"pk": self.management.pk},
        )
        permission.object_types.add(ObjectType.objects.get_for_model(NSODeviceManagement))
        permission.users.add(user)
        self.client.force_login(user)

        for action in ("retry", "abandon"):
            with self.subTest(action=action, permitted=False):
                session.request.reset_mock()
                url = reverse(f"plugins:netbox_nso_plugin:generation_{action}", args=[other_device.pk])
                response = self.client.post(url, {"generation_id": 73})
                self.assertEqual(response.status_code, 403, session.request.call_args_list)
                session.request.assert_not_called()
            with self.subTest(action=action, permitted=True):
                session.request.reset_mock()
                response = self.client.post(self._url(action), {"generation_id": 73})
                self.assertEqual(response.status_code, 302)
                session.request.assert_called_once()
                self.assertEqual(
                    session.request.call_args.args,
                    (
                        "POST",
                        f"{CFG['url']}/api/v1/devices/{self.management.adapter_device_id}/actions/{action}-generation",
                    ),
                )
                self.assertEqual(session.request.call_args.kwargs["json"], {"generation_id": 73})

    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_generation_actions_require_change_permission(self, mock_session):
        user = get_user_model().objects.create_user(username="barrier-unprivileged")
        self.client.force_login(user)

        for action in ("retry", "abandon"):
            with self.subTest(action=action):
                response = self.client.post(self._url(action), {"generation_id": 73})
                self.assertEqual(response.status_code, 403)
                mock_session.assert_not_called()
