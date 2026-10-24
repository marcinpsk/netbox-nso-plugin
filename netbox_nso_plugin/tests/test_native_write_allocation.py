# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Allocation requests require native authority and deliver after commit."""

import json
from unittest.mock import patch

import requests
from dcim.models import Cable, CableTermination, Interface
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models.signals import post_save
from django.test import TransactionTestCase
from django.urls import reverse
from extras.models import Tag
from ipam.models import IPAddress, Prefix, Role
from utilities.exceptions import PermissionsViolation

from netbox_nso_plugin.ip_autoassign import _assign_one_p2p_family, _reserve_single, auto_assign_ip, carve_p2p_child
from netbox_nso_plugin.models import (
    NSODeviceManagement,
    NSOIntentOutboxEntry,
    NSOIntentOutboxState,
    NSOInterfaceIPState,
    NSOInterfaceState,
    NSOISISInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
)
from netbox_nso_plugin.native_authorization import NativeWriteRefused, OwnedIntentRefused, authority_scope

from ._authority_case import AdapterCase, FakeAdapter, operator
from ._outbox_case import make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_native_authorization import _system_scope


def _fixtures(case, *, p2p, link_role):
    with without_commit_drain():
        case.device, case.management = make_managed("allocation", 17201)
        case.peer_device, case.peer_management = make_managed("allocation-peer", 17202)
        NSODeviceManagement.objects.filter(pk__in=(case.management.pk, case.peer_management.pk)).update(
            manage_description=True
        )
        case.interface = Interface.objects.create(device=case.device, name="Loopback1", type="virtual")
        case.second = Interface.objects.create(device=case.device, name="Loopback2", type="virtual")
        case.peer = Interface.objects.create(device=case.peer_device, name="Ethernet1", type="1000base-t")
        pool_role = Role.objects.create(name="Allocation", slug="p2p-core" if p2p else "loopback")
        case.pool = Prefix.objects.create(prefix="198.18.0.0/24", role=pool_role)
        case.pool6 = Prefix.objects.create(prefix="2001:db8::/64", role=pool_role)
        if p2p:
            cable = Cable.objects.create(status="connected")
            CableTermination.objects.create(cable=cable, cable_end="A", termination=case.interface)
            CableTermination.objects.create(cable=cable, cable_end="B", termination=case.peer)
            case.interface.tags.add(Tag.objects.create(name="P2P allocation", slug="p2p-core"))
            case.interface.refresh_from_db()
            case.peer.refresh_from_db()
        if link_role:
            case.role = NSOLinkRole.objects.create(
                name="Allocation",
                slug="allocation",
                link_type="p2p" if p2p else "single",
                assign_ipv4=True,
                ipv4_pool_prefix=case.pool,
                assign_ipv6=True,
                ipv6_pool_prefix=case.pool6,
                description_template="allocated",
                igp="isis",
                isis_circuit_type="point-to-point",
            )
            target = {"cable": cable} if p2p else {"interface": case.interface}
            NSOLinkRoleAssignment.objects.create(role=case.role, **target)
        case.prefix_ids = set(Prefix.objects.values_list("pk", flat=True))


class _AllocationRequests:
    p2p = False
    link_role = False

    def setUp(self):
        super().setUp()
        _fixtures(self, p2p=self.p2p, link_role=self.link_role)

    def _user(
        self,
        *,
        ip=True,
        prefix=True,
        description=True,
        ip_constraint=None,
        prefix_constraint=None,
        description_constraint=None,
        constrained=True,
    ):
        grants = []
        if ip:
            grants.append((("add",), IPAddress, ip_constraint or ({"status": "reserved"} if constrained else None)))
        if prefix:
            grants.append((("add",), Prefix, prefix_constraint or ({"status": "reserved"} if constrained else None)))
        if description:
            grants.append(
                (
                    ("change",),
                    Interface,
                    description_constraint
                    or ({"device_id__in": [self.device.pk, self.peer_device.pk]} if constrained else None),
                )
            )
        return operator(*grants)

    def _post(self, user, interfaces=None):
        self.client.force_login(user)
        endpoint = "device_provision_link_role" if self.link_role else "device_auto_assign_ip"
        return self.client.post(
            reverse(f"plugins:netbox_nso_plugin:{endpoint}", kwargs={"device_pk": self.device.pk}),
            {"interface_pks": [str(row.pk) for row in (interfaces or [self.interface])]},
        )

    def _assert_empty(self):
        self.assertFalse(IPAddress.objects.exists())
        self.assertFalse(NSOInterfaceIPState.objects.exists())
        self.assertFalse(NSOInterfaceState.objects.exists())
        self.assertFalse(NSOISISInterfaceState.objects.exists())
        self.assertFalse(NSOIntentOutboxEntry.objects.exists())
        self.assertEqual(set(Prefix.objects.values_list("pk", flat=True)), self.prefix_ids)
        self.assertEqual(set(Interface.objects.values_list("description", flat=True)), {""})

    def _refused(self, user, interfaces=None):
        with self.assert_refused_whole():
            response = self._post(user, interfaces)
        self.assertEqual(response.status_code, 403)
        self._assert_empty()

    def _assert_success(self, user):
        response = self._post(user)
        self.assertEqual(response.status_code, 302, response.content)
        count = 4 if self.p2p else 2
        self.assertEqual(IPAddress.objects.count(), count)
        self.assertEqual(NSOInterfaceIPState.objects.count(), count)
        self.assertEqual(Prefix.objects.count(), len(self.prefix_ids) + (2 if self.p2p else 0))
        self.assertTrue(NSOIntentOutboxEntry.objects.exists())
        if self.link_role:
            self.interface.refresh_from_db()
            self.assertEqual(self.interface.description, "allocated")
            self.assertEqual(NSOISISInterfaceState.objects.count(), 2 if self.p2p else 1)

    def test_missing_address_add_refuses_the_whole_request(self):
        self._refused(self._user(ip=False))

    def test_address_add_checks_the_completed_row(self):
        self._refused(self._user(ip_constraint={"status": "active"}))

    def test_second_address_refusal_rolls_back_the_first(self):
        if self.p2p:
            self._refused(self._user(ip_constraint={"assigned_object_id": self.interface.pk}))
        else:
            if self.link_role:
                NSOLinkRoleAssignment.objects.create(role=self.role, interface=self.second)
            self._refused(
                self._user(ip_constraint={"assigned_object_id": self.interface.pk}), [self.interface, self.second]
            )

    def test_constrained_holder_succeeds(self):
        self._assert_success(self._user())

    def test_unconstrained_holder_succeeds(self):
        self._assert_success(self._user(constrained=False))

    def test_superuser_succeeds(self):
        self._assert_success(get_user_model().objects.create_superuser(username="allocation-admin"))


class TestAutoAssignSingle(_AllocationRequests, AdapterCase):
    pass


class _P2PRequests:
    p2p = True

    def test_missing_prefix_add_refuses_the_whole_request(self):
        self._refused(self._user(prefix=False))

    def test_prefix_add_checks_the_completed_row(self):
        self._refused(self._user(prefix_constraint={"status": "active"}))


class TestAutoAssignP2P(_P2PRequests, _AllocationRequests, AdapterCase):
    pass


class _LinkRoleRequests:
    link_role = True

    def test_missing_description_change_refuses_the_whole_request(self):
        self._refused(self._user(description=False))

    def test_description_change_checks_the_completed_row(self):
        self._refused(self._user(description_constraint={"description": ""}))

    def test_description_change_checks_the_stored_row(self):
        self._refused(self._user(description_constraint={"description": "allocated"}))

    def test_expected_consumer_failure_keeps_the_rollback_flash(self):
        NSOLinkRole.objects.filter(pk=self.role.pk).update(ipv4_pool_prefix=None, ipv4_pool_role="absent")
        with self.captureOnCommitCallbacks() as callbacks:
            response = self._post(self._user())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("rolled back" in str(message) for message in response.wsgi_request._messages))
        self.assertEqual(callbacks, [])
        self._assert_empty()


class TestLinkRoleSingle(_LinkRoleRequests, _AllocationRequests, AdapterCase):
    pass


class TestLinkRoleP2P(_P2PRequests, _LinkRoleRequests, _AllocationRequests, AdapterCase):
    pass


class TestAllocationGuards(AdapterCase):
    def setUp(self):
        super().setUp()
        _fixtures(self, p2p=False, link_role=False)

    def test_single_allocation_refuses_owned_intent_in_system_scope(self):
        with self.assertRaises(OwnedIntentRefused), _system_scope() as run:
            run(lambda: auto_assign_ip(self.interface, families=("ipv4",)))
        self.assertFalse(IPAddress.objects.exists())

    def test_p2p_carve_refuses_owned_intent_in_system_scope(self):
        with self.assertRaises(OwnedIntentRefused), _system_scope() as run:
            run(lambda: carve_p2p_child(self.pool, "ipv4"))
        self.assertEqual(set(Prefix.objects.values_list("pk", flat=True)), self.prefix_ids)

    def test_allocation_refusal_propagates_from_the_helper(self):
        result = {"allocated": [], "skipped": [], "errors": []}
        with self.assertRaises(PermissionsViolation), authority_scope("native_mutation", operator()):
            with self.assertRaises(PermissionsViolation):
                _reserve_single(self.interface, self.management, "ipv4", self.pool, result)
        self.assertEqual(result, {"allocated": [], "skipped": [], "errors": []})

    def test_p2p_refusal_propagates_from_the_helper(self):
        result = {"allocated": [], "skipped": [], "errors": []}
        user = operator((("add",), Prefix, {"status": "reserved"}))
        with self.assertRaises(PermissionsViolation), authority_scope("native_mutation", user):
            with self.assertRaises(PermissionsViolation):
                _assign_one_p2p_family(
                    self.interface,
                    self.peer,
                    self.management,
                    self.peer_management,
                    "ipv4",
                    self.device.site,
                    result,
                    pool_finder=lambda family, site: self.pool,
                    no_pool_reason=lambda family: "no pool",
                )
        self.assertEqual(result, {"allocated": [], "skipped": [], "errors": []})

    def test_child_with_fewer_than_two_hosts_writes_nothing(self):
        saved = []

        def record(sender, instance, **kwargs):
            saved.append(instance.pk)

        post_save.connect(record, sender=Prefix)
        try:
            self.assertIsNone(carve_p2p_child(self.pool, "ipv4", override_mask=32))
        finally:
            post_save.disconnect(record, sender=Prefix)
        self.assertEqual(saved, [])


class _CommitAdapter(FakeAdapter):
    observations = []

    def request(self, method, url, *args, **kwargs):
        if method != "PUT":
            return super().request(method, url, *args, **kwargs)
        FakeAdapter.calls.append((method, url))
        self.observations.append((connection.in_atomic_block, IPAddress.objects.count()))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"count": 1}).encode()
        return response


class TestAllocationCommit(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        _fixtures(self, p2p=False, link_role=True)
        FakeAdapter.calls = []
        _CommitAdapter.observations = []
        transport = patch("netbox_nso_plugin.adapter_client.requests.Session", _CommitAdapter)
        transport.start()
        self.addCleanup(transport.stop)

    def _post(self, *, link_role, user):
        self.client.force_login(user)
        endpoint = "device_provision_link_role" if link_role else "device_auto_assign_ip"
        return self.client.post(
            reverse(f"plugins:netbox_nso_plugin:{endpoint}", kwargs={"device_pk": self.device.pk}),
            {"interface_pks": str(self.interface.pk)},
        )

    def _commits_before_delivery(self, *, link_role):
        user = operator((("add",), IPAddress, None), (("change",), Interface, None))
        with transaction.atomic():
            response = self._post(link_role=link_role, user=user)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(NSOIntentOutboxEntry.objects.exists())
            self.assertEqual(FakeAdapter.calls, [])
        self.assertTrue(_CommitAdapter.observations)
        self.assertTrue(all(not atomic and count == 2 for atomic, count in _CommitAdapter.observations))
        self.assertTrue(any(url.endswith("/ip-intent") for method, url in FakeAdapter.calls if method == "PUT"))
        if link_role:
            self.assertTrue(any(url.endswith("/intent") for method, url in FakeAdapter.calls if method == "PUT"))

    def test_auto_assign_delivers_only_after_commit(self):
        self._commits_before_delivery(link_role=False)

    def test_link_role_delivers_only_after_commit(self):
        self._commits_before_delivery(link_role=True)

    def test_refusal_never_delivers_after_commit(self):
        for link_role in (False, True):
            with self.subTest(link_role=link_role), transaction.atomic():
                response = self._post(link_role=link_role, user=operator())
                self.assertEqual(response.status_code, 403)
            self.assertEqual(FakeAdapter.calls, [])
            self.assertFalse(IPAddress.objects.exists())
            self.assertFalse(NSOIntentOutboxEntry.objects.exists())

    def test_provisioned_delivery_propagates_a_native_guard_refusal(self):
        from netbox_nso_plugin.link_role import _push_provisioned

        def write_native_from_signal(sender, **kwargs):
            Interface.objects.filter(pk=self.interface.pk).update(description="unexpected")

        post_save.connect(write_native_from_signal, sender=NSOIntentOutboxState)
        try:
            with authority_scope("read"):
                with self.assertRaises(NativeWriteRefused):
                    _push_provisioned(self.role, [self.device.pk])
        finally:
            post_save.disconnect(write_native_from_signal, sender=NSOIntentOutboxState)
        self.assertEqual(FakeAdapter.calls, [])
