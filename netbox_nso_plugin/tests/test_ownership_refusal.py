# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Public ownership refusals do not render exception diagnostics."""

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from django.views import View

from netbox_nso_plugin.ownership_planner import OwnershipNotQualified
from netbox_nso_plugin.views import NSOActionPermissionMixin

from ._outbox_case import make_device

_PUBLIC_MESSAGE = "The binding does not qualify for ownership."
_DIAGNOSTIC_MESSAGE = "Internal exception diagnostic placeholder."


class _RefusalAction(NSOActionPermissionMixin, View):
    def post(self, request, device_id):
        refusal = OwnershipNotQualified(_PUBLIC_MESSAGE, device_id=device_id)
        refusal.args = (_DIAGNOSTIC_MESSAGE,)
        raise refusal


urlpatterns = [
    path("ownership-refusal/<int:device_id>/", _RefusalAction.as_view(), name="ownership_refusal"),
    path("", include("netbox.urls")),
]


@override_settings(ROOT_URLCONF=__name__)
class TestOwnershipRefusalResponse(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = make_device("ownership-refusal")
        cls.operator = get_user_model().objects.create_superuser(username="refusal-operator", password=None)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.operator)
        self.url = reverse("ownership_refusal", kwargs={"device_id": self.device.pk})

    def test_ajax_response_preserves_public_text_without_exception_diagnostics(self):
        response = self.client.post(self.url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"status": "error", "message": _PUBLIC_MESSAGE})
        self.assertNotIn(_DIAGNOSTIC_MESSAGE, response.content.decode())

    def test_full_page_refusal_redirects_with_only_the_public_message(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("dcim:device_nso", kwargs={"pk": self.device.pk}))
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], [_PUBLIC_MESSAGE])

    def test_permission_failure_stops_before_the_action(self):
        unprivileged = get_user_model().objects.create_user(username="refusal-unprivileged")
        self.client.force_login(unprivileged)

        response = self.client.post(self.url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(_PUBLIC_MESSAGE, response.content.decode())
        self.assertNotIn(_DIAGNOSTIC_MESSAGE, response.content.decode())
