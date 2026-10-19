# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Share authenticated refusal display requests between capture test cases."""

from django.urls import reverse

from ._adapter_http import make_apply_preview, make_response


class RefusalDisplayHelpers:
    def category(self, key, **params):
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category", args=(self.device.pk, key)), params
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def preview(self, payload=None):
        original = self.adapter._handle

        def handle(method, url, **kwargs):
            if method == "GET" and url.endswith("/actions/apply-diff"):
                return make_response(200, payload or make_apply_preview(device_id=1558))
            return original(method, url, **kwargs)

        self.adapter._handle = handle
        response = self.client.get(reverse("plugins:netbox_nso_plugin:device_apply_preview", args=(self.device.pk,)))
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def rest_client(self):
        from rest_framework.test import APIClient
        from users.constants import TOKEN_PREFIX
        from users.models import Token, User

        token = Token.objects.create(user=User.objects.get(username="placeholder-bgp-operator"))
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}")
        return api
