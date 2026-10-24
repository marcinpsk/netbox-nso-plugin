# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""An operator cannot write an undeclared management, even with unrestricted permissions."""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.test import override_settings
from django.urls import include, path, reverse
from utilities.exceptions import PermissionsViolation

from netbox_nso_plugin.models import NSOInterfaceState
from netbox_nso_plugin.native_authorization import authority_scope, save_native
from netbox_nso_plugin.request_authorization import AuthorizedView, EndpointPolicy, Management

from ._authority_case import AdapterCase
from ._outbox_case import make_managed


class UndeclaredOverlayWriteView(AuthorizedView):
    policy = EndpointPolicy(Management("change"), "native_mutation")

    def post(self, request, pk):
        NSOInterfaceState.objects.filter(pk=request.POST["row"]).update(nso_value="undeclared")
        return JsonResponse({"status": "ok"})


urlpatterns = [
    path("operator-effect/<int:pk>/", UndeclaredOverlayWriteView.as_view(), name="undeclared-overlay"),
    path("", include("netbox.urls")),
]


class TestOverlayEffects(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        from dcim.models import Interface

        cls.device, cls.management = make_managed("shared-effect", 17201)
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet1", type="1000base-t")
        cls.row = NSOInterfaceState.objects.create(
            interface=cls.interface, attribute="description", nso_value="observed", status="imported"
        )
        cls.admin = get_user_model().objects.create_superuser(username=f"shared-effect-{uuid4().hex[:8]}")

    def test_an_undeclared_overlay_save_is_refused_even_for_a_superuser(self):
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin):
                self.row.nso_value = "undeclared"
                self.row.save(update_fields=("nso_value",))
        self.row.refresh_from_db()
        self.assertEqual(self.row.nso_value, "observed")

    def test_an_undeclared_overlay_queryset_update_is_refused(self):
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin):
                NSOInterfaceState.objects.filter(pk=self.row.pk).update(nso_value="undeclared")
        self.row.refresh_from_db()
        self.assertEqual(self.row.nso_value, "observed")

    def test_an_undeclared_overlay_bulk_update_is_refused(self):
        self.row.nso_value = "undeclared"
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin):
                NSOInterfaceState.objects.bulk_update([self.row], ["nso_value"])
        self.row.refresh_from_db()
        self.assertEqual(self.row.nso_value, "observed")

    def test_an_undeclared_overlay_bulk_create_is_refused(self):
        row = NSOInterfaceState(interface=self.interface, attribute="enabled", nso_value="true")
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin):
                NSOInterfaceState.objects.bulk_create([row])
        self.assertFalse(NSOInterfaceState.objects.filter(attribute="enabled").exists())

    def test_an_undeclared_overlay_delete_is_refused(self):
        pk = self.row.pk
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin):
                self.row.delete()
        self.assertTrue(NSOInterfaceState.objects.filter(pk=pk).exists())

    def test_an_undeclared_device_scoped_native_save_is_refused(self):
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin):
                self.interface.description = "undeclared"
                save_native(self.interface, update_fields=("description",), origin=None)
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.description, "")

    def test_a_superuser_cannot_write_another_management_outside_the_target(self):
        _other, other_management = make_managed("effect-other", 17207)
        self.client.force_login(self.admin)
        with override_settings(ROOT_URLCONF=__name__), self.assert_refused_whole():
            response = self.client.post(
                reverse("undeclared-overlay", kwargs={"pk": other_management.pk}),
                {"row": self.row.pk},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 403)
        self.row.refresh_from_db()
        self.assertEqual(self.row.nso_value, "observed")

    def test_an_undeclared_overlay_m2m_change_is_refused(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_m2m_add, renderer_mirror_writes

        vlan = VLAN.objects.create(vid=210, name="effect-tagged")
        state = NSOSwitchportState.objects.create(
            management=self.management, interface=self.interface, status="imported"
        )
        plan = RendererMutationPlan.build(m2m_writes=(planned_m2m_add(state, "tagged_vlans", (vlan,)),))
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin), renderer_mirror_writes(plan) as writer:
                writer.m2m_add(state, "tagged_vlans", (vlan,))
        self.assertFalse(state.tagged_vlans.exists())

    def test_an_undeclared_overlay_through_bulk_insert_is_refused(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOSwitchportState

        vlan = VLAN.objects.create(vid=210, name="effect-through")
        state = NSOSwitchportState.objects.create(
            management=self.management, interface=self.interface, status="imported"
        )
        through = NSOSwitchportState.tagged_vlans.through
        with self.assert_refused_whole(), self.assertRaises(PermissionsViolation):
            with authority_scope("native_mutation", self.admin):
                through.objects.bulk_create([through(nsoswitchportstate_id=state.pk, vlan_id=vlan.pk)])
        self.assertFalse(state.tagged_vlans.exists())
