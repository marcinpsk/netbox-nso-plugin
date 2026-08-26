# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Operator exits from a blocked Apply generation."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TransactionTestCase
from django.urls import reverse

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
