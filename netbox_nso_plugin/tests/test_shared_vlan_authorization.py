# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shared VLAN mutations authorize the complete graph before any write."""

from uuid import uuid4

from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse
from ipam.models import VLAN, VLANGroup

from netbox_nso_plugin.models import NSODeviceManagement, NSOSwitchportState, NSOVLANState

from ._authority_case import AdapterCase, concurrent_writer, grant
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay


class TestSharedVlanRequests(AdapterCase):
    @classmethod
    def setUpTestData(cls):
        cls.device_a, cls.management_a = make_managed("shared-vlan-a", 17202)
        cls.device_b, cls.management_b = make_managed("shared-vlan-b", 17203)
        cls.device_c, cls.management_c = make_managed("shared-vlan-c", 17204)
        cls.source_group = VLANGroup.objects.create(name="source", slug="shared-auth-source")
        cls.target_group = VLANGroup.objects.create(name="destination", slug="shared-auth-destination")
        cls.empty_group = VLANGroup.objects.create(name="empty", slug="shared-auth-empty")
        cls.source = VLAN.objects.create(group=cls.source_group, vid=207, name="source")
        cls.target = VLAN.objects.create(group=cls.target_group, vid=207, name="destination")
        cls.interface = Interface.objects.create(
            device=cls.device_b, name="Ethernet1", type="1000base-t", mode="tagged", untagged_vlan=cls.source
        )
        cls.interface.tagged_vlans.add(cls.source)
        with without_commit_drain():
            cls.state_a = acquire_overlay(
                NSOVLANState, management=cls.management_a, vlan=cls.source, device_name="source", status="in_sync"
            )
            cls.state_b = acquire_overlay(
                NSOVLANState, management=cls.management_b, vlan=cls.source, device_name="source", status="in_sync"
            )
            cls.switchport = acquire_overlay(
                NSOSwitchportState,
                management=cls.management_b,
                interface=cls.interface,
                mode="tagged",
                untagged_vlan=cls.source,
                status="in_sync",
            )
            cls.switchport.tagged_vlans.add(cls.source)

    def _user(self, *, both=True, vlan_actions=("view", "change", "delete"), interface=True, superuser=False):
        user = get_user_model().objects.create_user(username=f"shared-vlan-{uuid4().hex[:8]}", is_superuser=superuser)
        grant(user, ("view",), Device)
        grant(user, ("view",), NSODeviceManagement)
        grant(user, ("change",), NSODeviceManagement, None if both else {"pk": self.management_a.pk})
        grant(user, vlan_actions, VLAN)
        grant(user, ("view",), VLANGroup)
        if interface:
            grant(user, ("change",), Interface)
        return user

    def _rescope(self, user, group=None):
        self.client.force_login(user)
        return self.client.post(
            reverse("plugins:netbox_nso_plugin:vlan_rescope", kwargs={"pk": self.state_a.pk}),
            {"group": (group or self.target_group).pk},
        )

    def _assert_original(self):
        self.source.refresh_from_db()
        self.interface.refresh_from_db()
        self.state_a.refresh_from_db()
        self.state_b.refresh_from_db()
        self.switchport.refresh_from_db()
        self.assertEqual(self.source.group_id, self.source_group.pk)
        self.assertEqual(self.interface.untagged_vlan_id, self.source.pk)
        self.assertEqual(list(self.interface.tagged_vlans.all()), [self.source])
        self.assertEqual((self.state_a.vlan_id, self.state_b.vlan_id), (self.source.pk, self.source.pk))
        self.assertEqual(self.switchport.untagged_vlan_id, self.source.pk)
        self.assertEqual(list(self.switchport.tagged_vlans.all()), [self.source])

    def test_merge_refuses_an_excluded_affected_management_before_writing(self):
        with self.assert_refused_whole():
            response = self._rescope(self._user(both=False))
        self.assertEqual(response.status_code, 404)
        self._assert_original()

    def test_merge_moves_every_reference_and_deletes_the_source(self):
        response = self._rescope(self._user())
        self.assertEqual(response.status_code, 302, response.content)
        self.assertFalse(VLAN.objects.filter(pk=self.source.pk).exists())
        self.interface.refresh_from_db()
        self.switchport.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan_id, self.target.pk)
        self.assertEqual(list(self.interface.tagged_vlans.all()), [self.target])
        self.assertEqual(self.switchport.untagged_vlan_id, self.target.pk)
        self.assertEqual(list(self.switchport.tagged_vlans.all()), [self.target])
        self.assertEqual(NSOVLANState.objects.filter(vlan=self.target).count(), 2)

    def test_merge_without_change_on_the_second_native_interface_rolls_back(self):
        user = self._user(interface=False)
        grant(user, ("change",), Interface, {"device_id": self.device_a.pk})
        with self.assert_refused_whole():
            response = self._rescope(user)
        self.assertEqual(response.status_code, 403)
        self._assert_original()

    def test_merge_without_native_delete_rolls_back(self):
        with self.assert_refused_whole():
            response = self._rescope(self._user(vlan_actions=("view", "change")))
        self.assertEqual(response.status_code, 403)
        self._assert_original()

    def test_move_refuses_an_excluded_affected_management(self):
        with self.assert_refused_whole():
            response = self._rescope(self._user(both=False), self.empty_group)
        self.assertEqual(response.status_code, 404)
        self._assert_original()

    def test_move_preserves_references_for_an_unconstrained_holder(self):
        response = self._rescope(self._user(), self.empty_group)
        self.assertEqual(response.status_code, 302)
        self.source.refresh_from_db()
        self.assertEqual(self.source.group_id, self.empty_group.pk)
        self.state_b.refresh_from_db()
        self.assertEqual(self.state_b.vlan_id, self.source.pk)

    def test_merge_target_hidden_inside_a_visible_group_is_refused(self):
        user = self._user(vlan_actions=("change", "delete"))
        grant(user, ("view",), VLAN, {"pk": self.source.pk})
        with self.assert_refused_whole():
            response = self._rescope(user)
        self.assertEqual(response.status_code, 404)
        self._assert_original()

    def test_merge_still_succeeds_for_a_superuser(self):
        self.assertEqual(self._rescope(self._user(superuser=True)).status_code, 302)
        self.assertFalse(VLAN.objects.filter(pk=self.source.pk).exists())

    def test_rename_with_all_managements_permitted_succeeds(self):
        self.client.force_login(self._user())
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "vlan_name", "pk": self.state_a.pk}),
            {"name": "renamed"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.source.refresh_from_db()
        self.assertEqual(self.source.name, "renamed")

    def test_vlan_rename_preserves_success_for_a_superuser(self):
        self.client.force_login(self._user(superuser=True))
        url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "vlan_name", "pk": self.state_a.pk}
        )
        self.assertEqual(self.client.post(url, {"name": "renamed"}).status_code, 200)

    def test_move_preserves_success_for_a_superuser(self):
        response = self._rescope(self._user(superuser=True), self.empty_group)
        self.assertEqual(response.status_code, 302)
        self.source.refresh_from_db()
        self.assertEqual(self.source.group_id, self.empty_group.pk)

    def _attach_while_waiting(self, *, authorized, move):
        user = self._user()
        if not authorized:
            permissions = user.object_permissions.filter(
                actions__contains=["change"], object_types__model="nsodevicemanagement"
            )
            permissions.update(constraints={"pk__in": [self.management_a.pk, self.management_b.pk]})
        attached = False

        def concurrent_attachment(execute, sql, params, many, context):
            nonlocal attached
            if not attached and "pg_advisory_xact_lock" in str(sql):
                attached = True
                with concurrent_writer(), without_commit_drain():
                    NSOVLANState.objects.create(management=self.management_c, vlan=self.source, status="imported")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(concurrent_attachment), self.assert_refused_whole():
            response = self._rescope(user, self.empty_group if move else self.target_group)
        self.assertTrue(attached)
        self.assertEqual(response.status_code, 409)
        self._assert_original()

    def test_merge_refuses_a_new_unauthorized_attachment_with_conflict(self):
        self._attach_while_waiting(authorized=False, move=False)

    def test_merge_refuses_a_new_authorized_attachment_with_conflict(self):
        self._attach_while_waiting(authorized=True, move=False)

    def test_move_refuses_a_new_authorized_attachment_with_conflict(self):
        self._attach_while_waiting(authorized=True, move=True)
