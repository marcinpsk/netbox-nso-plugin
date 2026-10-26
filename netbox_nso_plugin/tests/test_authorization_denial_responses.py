# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A refused request answers with authored text, never with the text of the caught exception."""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import include, path, reverse
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from utilities.exceptions import PermissionsViolation

from netbox_nso_plugin.models import NSOInterfaceState
from netbox_nso_plugin.native_authorization import SharedOperationConflict
from netbox_nso_plugin.ownership_planner import OwnershipNotQualified
from netbox_nso_plugin.request_authorization import AuthorizedAPIView, AuthorizedView, EndpointPolicy, Management

from ._authority_case import AdapterCase
from ._outbox_case import make_managed

CONFLICT = "The shared object membership changed. Refresh the page and try again."


class BareDenialView(AuthorizedView):
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        raise PermissionsViolation()


class ConflictView(AuthorizedView):
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        raise SharedOperationConflict(CONFLICT)


class UndeclaredWriteView(AuthorizedView):
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        NSOInterfaceState.objects.filter(pk=request.POST["row"]).update(nso_value="undeclared")


class WriteThenNotQualifiedView(AuthorizedView):
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        NSOInterfaceState.objects.filter(pk=request.POST["row"]).update(nso_value="written")
        raise OwnershipNotQualified("not qualified after a write")


class BareDenialAPIView(AuthorizedAPIView, APIView):
    permission_classes = [IsAuthenticated]
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        raise PermissionsViolation()


class ConflictAPIView(AuthorizedAPIView, APIView):
    permission_classes = [IsAuthenticated]
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        raise SharedOperationConflict(CONFLICT)


class WriteThenRejectAPIView(AuthorizedAPIView, APIView):
    permission_classes = [IsAuthenticated]
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        NSOInterfaceState.objects.filter(pk=request.data["row"]).update(nso_value="written")
        raise ValidationError("rejected after a write")


urlpatterns = [
    path("bare-denial/<int:pk>/", BareDenialView.as_view(), name="bare-denial"),
    path("conflict/<int:pk>/", ConflictView.as_view(), name="conflict"),
    path("undeclared/<int:pk>/", UndeclaredWriteView.as_view(), name="undeclared"),
    path("not-qualified/<int:pk>/", WriteThenNotQualifiedView.as_view(), name="not-qualified"),
    path("api-bare-denial/<int:pk>/", BareDenialAPIView.as_view(), name="api-bare-denial"),
    path("api-conflict/<int:pk>/", ConflictAPIView.as_view(), name="api-conflict"),
    path("api-write-then-reject/<int:pk>/", WriteThenRejectAPIView.as_view(), name="api-write-then-reject"),
    path("", include("netbox.urls")),
]


@override_settings(ROOT_URLCONF=__name__)
class TestDenialResponses(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        from dcim.models import Interface

        cls.device, cls.management = make_managed("denial-text", 17301)
        _other_device, cls.other_management = make_managed("denial-text-other", 17302)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet1", type="1000base-t")
        cls.row = NSOInterfaceState.objects.create(
            interface=cls.interface, attribute="description", nso_value="observed", status="imported"
        )
        cls.admin = get_user_model().objects.create_superuser(username=f"denial-text-{uuid4().hex[:8]}")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def post(self, name, management, data=None):
        return self.client.post(
            reverse(name, kwargs={"pk": management.pk}), data or {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

    def test_a_netbox_denial_without_text_answers_its_class_message(self):
        response = self.post("bare-denial", self.management)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["message"], PermissionsViolation.message)

    def test_a_plugin_denial_answers_its_authored_message(self):
        response = self.post("undeclared", self.other_management, {"row": self.row.pk})
        self.assertEqual(response.status_code, 403)
        message = response.json()["message"]
        self.assertTrue(message)
        self.assertNotEqual(message, PermissionsViolation.message)

    def test_a_conflict_answers_its_authored_message(self):
        response = self.post("conflict", self.management)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], CONFLICT)

    def test_an_api_denial_without_text_answers_its_class_message(self):
        response = self.client.post(reverse("api-bare-denial", kwargs={"pk": self.management.pk}))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], PermissionsViolation.message)

    def test_an_api_conflict_answers_its_authored_message(self):
        response = self.client.post(reverse("api-conflict", kwargs={"pk": self.management.pk}))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], CONFLICT)

    def test_an_api_error_response_rolls_back_the_operator_writes(self):
        response = self.client.post(
            reverse("api-write-then-reject", kwargs={"pk": self.management.pk}), {"row": self.row.pk}
        )
        self.assertEqual(response.status_code, 400)
        self.row.refresh_from_db()
        self.assertEqual(self.row.nso_value, "observed")

    def test_a_not_qualified_refusal_rolls_back_the_operator_writes(self):
        response = self.post("not-qualified", self.management, {"row": self.row.pk})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "not qualified after a write")
        self.row.refresh_from_db()
        self.assertEqual(self.row.nso_value, "observed")
