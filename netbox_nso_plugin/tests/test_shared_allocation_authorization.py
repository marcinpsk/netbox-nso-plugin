# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A paired allocation authorizes both management rows before it writes."""

from core.models import ObjectType
from dcim.models import Interface
from django.db import connection
from django.urls import reverse
from ipam.models import IPAddress, Prefix
from users.models import ObjectPermission

from netbox_nso_plugin.intent_state import offline_mutation
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOIntentRevision,
    NSOInterfaceIPState,
    NSOInterfaceState,
    NSOISISInterfaceState,
    NSOLinkRole,
)

from ._authority_case import AdapterCase, concurrent_writer, operator
from ._outbox_case import make_managed
from .test_native_write_allocation import _fixtures


class _SharedAllocationRequests:
    link_role = False
    addresses = True

    def setUp(self):
        super().setUp()
        _fixtures(self, p2p=True, link_role=self.link_role)
        if not self.addresses:
            NSOLinkRole.objects.filter(pk=self.role.pk).update(assign_ipv4=False, assign_ipv6=False)

    def _user(self, *, peer):
        user = operator((("add",), IPAddress, None), (("add",), Prefix, None), (("change",), Interface, None))
        if not peer:
            ObjectPermission.objects.filter(
                users=user, object_types=ObjectType.objects.get_for_model(NSODeviceManagement)
            ).update(constraints={"pk": self.management.pk})
        return user

    def _post(self, user):
        self.client.force_login(user)
        endpoint = "device_provision_link_role" if self.link_role else "device_auto_assign_ip"
        return self.client.post(
            reverse(f"plugins:netbox_nso_plugin:{endpoint}", kwargs={"device_pk": self.device.pk}),
            {"interface_pks": [str(self.interface.pk)]},
        )

    def test_a_peer_management_outside_the_constraint_is_not_found(self):
        with self.assert_refused_whole():
            response = self._post(self._user(peer=False))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(IPAddress.objects.exists())
        self.assertEqual(set(Prefix.objects.values_list("pk", flat=True)), self.prefix_ids)
        self.assertFalse(NSOInterfaceIPState.objects.exists())
        self.assertFalse(NSOInterfaceState.objects.exists())
        self.assertFalse(NSOISISInterfaceState.objects.exists())
        self.assertFalse(NSOIntentOutboxEntry.objects.exists())
        self.assertEqual(set(Interface.objects.values_list("description", flat=True)), {""})

    def test_an_authorized_peer_is_provisioned(self):
        response = self._post(self._user(peer=True))
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(IPAddress.objects.count(), 4 if self.addresses else 0)
        self.assertTrue(NSOIntentOutboxEntry.objects.exists())
        if self.link_role:
            self.assertEqual(set(Interface.objects.values_list("description", flat=True)), {"", "allocated"})
            self.assertEqual(NSOISISInterfaceState.objects.count(), 2)


class TestSharedAutoAssign(_SharedAllocationRequests, AdapterCase):
    def test_repeating_an_occupied_allocation_does_not_advance_revisions(self):
        user = self._user(peer=True)
        self.assertEqual(self._post(user).status_code, 302)
        before = list(
            NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
        )
        with self.captureOnCommitCallbacks() as callbacks:
            response = self._post(user)
        self.assertEqual(response.status_code, 302, response.content)
        after = list(
            NSOIntentRevision.objects.order_by("device_id", "scope").values_list("device_id", "scope", "revision")
        )
        self.assertEqual(after, before)
        self.assertEqual(callbacks, [])

    def test_endpoint_management_change_at_source_lock_precedes_any_prefix_insert(self):
        replacement, _management = make_managed("allocation-new-peer", 17217)
        prefix_inserts = []
        changed = False
        pool_locked = False

        def move_at_endpoint_lock(execute, sql, params, many, context):
            nonlocal changed, pool_locked
            sql = str(sql)
            if sql.lstrip().upper().startswith('INSERT INTO "IPAM_PREFIX"'):
                prefix_inserts.append(sql)
            result = execute(sql, params, many, context)
            if 'FROM "ipam_prefix"' in sql and "FOR UPDATE" in sql.upper():
                pool_locked = True
            if pool_locked and not changed and 'FROM "dcim_interface"' in sql and "FOR UPDATE" in sql.upper():
                changed = True
                with concurrent_writer(), offline_mutation():
                    Interface.objects.filter(pk=self.peer.pk).update(device=replacement)
            return result

        with connection.execute_wrapper(move_at_endpoint_lock), self.assert_refused_whole():
            response = self._post(self._user(peer=True))
        self.assertTrue(changed)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(prefix_inserts, [])
        self.assertFalse(IPAddress.objects.exists())
        self.assertEqual(set(Prefix.objects.values_list("pk", flat=True)), self.prefix_ids)


class TestSharedLinkRole(_SharedAllocationRequests, AdapterCase):
    link_role = True


class TestSharedLinkRoleWithoutAddresses(_SharedAllocationRequests, AdapterCase):
    link_role = True
    addresses = False
