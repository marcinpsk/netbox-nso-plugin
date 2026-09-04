# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for IP auto-assignment: Phase A (loopback/access) and Phase B (P2P).

Covers:
- interface classification (loopback by name, tag override, access default, P2P auto-detect)
- pool matching (role+family+site)
- P2P child-prefix carving
- auto_assign_ip happy paths (loopback, access, P2P)
- fill-empty guard, no-pool, unmanaged-device error paths
- rollback_auto_assigned helper (single-ended and P2P cascade)
- reconciler in_sync → active IPAddress activation (single-ended and P2P both-ends)
"""

import threading
from contextlib import contextmanager
from unittest.mock import patch

from dcim.models import Cable, CableTermination, Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.db import connections
from django.test import TestCase, TransactionTestCase
from ipam.models import IPAddress, Prefix, Role

from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestClassifyInterface(TestCase):
    """classify_interface: loopback, access default, tag override."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ClfMfg", slug="clfmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ClfDevice", slug="clfdevice")
        role = DeviceRole.objects.create(name="ClfRole", slug="clfrole")
        site = Site.objects.create(name="ClfSite", slug="clfsite")
        cls.device = Device.objects.create(name="clf-router", device_type=device_type, role=role, site=site)

    def test_loopback_by_name_Loopback0(self):
        from netbox_nso_plugin.ip_autoassign import classify_interface

        iface = Interface.objects.create(device=self.device, name="Loopback0", type="virtual")
        self.assertEqual(classify_interface(iface), "loopback")

    def test_loopback_by_name_lo0(self):
        from netbox_nso_plugin.ip_autoassign import classify_interface

        iface = Interface.objects.create(device=self.device, name="lo0", type="virtual")
        self.assertEqual(classify_interface(iface), "loopback")

    def test_access_default_for_physical(self):
        from netbox_nso_plugin.ip_autoassign import classify_interface

        iface = Interface.objects.create(device=self.device, name="GigabitEthernet1/0", type="1000base-t")
        self.assertEqual(classify_interface(iface), "access")

    def test_tag_override_access(self):
        from extras.models import Tag

        from netbox_nso_plugin.ip_autoassign import classify_interface

        tag = Tag.objects.create(name="access", slug="access")
        iface = Interface.objects.create(device=self.device, name="Gi2/0", type="1000base-t")
        iface.tags.add(tag)
        self.assertEqual(classify_interface(iface), "access")

    def test_tag_override_p2p_core(self):
        from extras.models import Tag

        from netbox_nso_plugin.ip_autoassign import classify_interface

        tag = Tag.objects.create(name="p2p-core", slug="p2p-core")
        iface = Interface.objects.create(device=self.device, name="Gi3/0", type="1000base-t")
        iface.tags.add(tag)
        self.assertEqual(classify_interface(iface), "p2p-core")


class TestFindPool(TestCase):
    """find_pool: role+family matching, site scoping, exhaustion."""

    @classmethod
    def setUpTestData(cls):
        cls.role_loopback = Role.objects.create(name="Loopback", slug="loopback")
        cls.role_access = Role.objects.create(name="Access LAN", slug="access-lan")
        cls.site = Site.objects.create(name="PoolSite", slug="poolsite")
        # IPv4 loopback pool
        cls.pool_lo4 = Prefix.objects.create(prefix="10.0.0.0/24", role=cls.role_loopback)
        # IPv6 loopback pool
        cls.pool_lo6 = Prefix.objects.create(prefix="fc00::/48", role=cls.role_loopback)
        # Access-lan pool
        cls.pool_access4 = Prefix.objects.create(prefix="192.168.0.0/16", role=cls.role_access)

    def test_matches_loopback_ipv4(self):
        from netbox_nso_plugin.ip_autoassign import find_pool

        pool = find_pool("loopback", vrf=None, site=None, family="ipv4")
        self.assertIsNotNone(pool)
        self.assertEqual(pool.pk, self.pool_lo4.pk)

    def test_matches_loopback_ipv6(self):
        from netbox_nso_plugin.ip_autoassign import find_pool

        pool = find_pool("loopback", vrf=None, site=None, family="ipv6")
        self.assertIsNotNone(pool)
        self.assertEqual(pool.pk, self.pool_lo6.pk)

    def test_matches_access_lan_ipv4(self):
        from netbox_nso_plugin.ip_autoassign import find_pool

        pool = find_pool("access", vrf=None, site=None, family="ipv4")
        self.assertIsNotNone(pool)
        self.assertEqual(pool.pk, self.pool_access4.pk)

    def test_returns_none_for_unknown_classification(self):
        from netbox_nso_plugin.ip_autoassign import find_pool

        pool = find_pool("p2p-core", vrf=None, site=None, family="ipv4")
        # No p2p-core pool created → None (no pool exists)
        self.assertIsNone(pool)

    def test_returns_none_when_pool_exhausted(self):
        from netbox_nso_plugin.ip_autoassign import find_pool

        # Fill the loopback pool with one IP so get_first_available_ip still works
        # but create a /32 pool with all space consumed.
        exhausted_role = Role.objects.create(name="Exhausted", slug="exhausted-test")
        Prefix.objects.create(prefix="172.20.0.0/31", role=exhausted_role)
        # Allocate both IPs in the /31
        IPAddress.objects.create(address="172.20.0.0/31")
        IPAddress.objects.create(address="172.20.0.1/31")
        result = find_pool("exhausted-test", vrf=None, site=None, family="ipv4")
        # No role slug mapping → None (classification not in map)
        self.assertIsNone(result)


class TestAutoAssignIP(TestCase):
    """auto_assign_ip: end-to-end allocation through to NSOInterfaceIPState creation."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="AllocMfg", slug="allocmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="AllocDevice", slug="allocdevice")
        cls.device_role = DeviceRole.objects.create(name="AllocRole", slug="allocrole")
        cls.site = Site.objects.create(name="AllocSite", slug="allocsite")
        cls.device = Device.objects.create(
            name="alloc-router",
            device_type=device_type,
            role=cls.device_role,
            site=cls.site,
        )
        # Loopback pool
        cls.lb_role = Role.objects.create(name="LbPool", slug="loopback")
        cls.pool_lo4 = Prefix.objects.create(prefix="10.100.0.0/24", role=cls.lb_role)
        # Access pool
        cls.ac_role = Role.objects.create(name="AcPool", slug="access-lan")
        cls.pool_ac4 = Prefix.objects.create(prefix="192.168.100.0/24", role=cls.ac_role)

    def _make_mgmt(self, adapter_device_id="dev-1"):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        nso = NSOInstance.objects.create(name="test-nso", adapter_instance_id="test-nso-inst")
        return NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=nso,
            nso_device_name="alloc-router",
            adapter_device_id=1,
        )

    def test_loopback_allocates_from_loopback_pool(self):
        from netbox_nso_plugin.models import NSOInterfaceIPState

        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback100", type="virtual")

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            from netbox_nso_plugin.ip_autoassign import auto_assign_ip

            result = auto_assign_ip(iface, families=("ipv4",))

        self.assertEqual(len(result["allocated"]), 1, result)
        self.assertEqual(len(result["errors"]), 0, result)
        entry = result["allocated"][0]
        self.assertEqual(entry["family"], "ipv4")
        self.assertIn("10.100.0.", entry["address"])

        # IPAddress created with status=reserved
        ip = IPAddress.objects.get(address=entry["address"])
        self.assertEqual(ip.status, "reserved")
        self.assertEqual(ip.assigned_object, iface)

        # NSOInterfaceIPState created as accepted with auto_assigned=True
        state = NSOInterfaceIPState.objects.get(interface=iface, address=entry["address"])
        self.assertEqual(state.status, "accepted")
        self.assertTrue(state.auto_assigned)
        self.assertEqual(state.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_SINGLE)
        self.assertEqual(state.source_pool_id, self.pool_lo4.pk)

        mgmt.delete()

    def test_reserve_single_carries_pool_vrf(self):
        """A VRF-scoped pool (as a link-role resolves) must land the reserved IPAddress in that
        VRF — otherwise it goes into the global table (wrong IPAM accounting) and rollback, which
        filters by VRF, can't clean it up."""
        from ipam.models import VRF

        from netbox_nso_plugin.ip_autoassign import _reserve_single

        vrf = VRF.objects.create(name="TENANT-A")
        self.pool_lo4.vrf = vrf
        self.pool_lo4.save()
        # Reload fresh (production resolves the pool via find_pool/_resolve_role_pool) — a
        # class-attr Prefix carries an unconverted .prefix that breaks get_first_available_ip.
        pool = Prefix.objects.get(pk=self.pool_lo4.pk)
        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback150", type="virtual")
        result = {"allocated": [], "errors": [], "skipped": []}
        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            _reserve_single(iface, mgmt, "ipv4", pool, result, push=False)
        self.assertTrue(result["allocated"], result)
        ip = IPAddress.objects.get(address=result["allocated"][0]["address"])
        self.assertEqual(ip.vrf, vrf)  # was None (global table) before the fix
        mgmt.delete()

    def test_state_failure_rolls_back_the_reservation_without_manual_delete(self):
        from django.db import IntegrityError

        from netbox_nso_plugin.ip_autoassign import _reserve_single
        from netbox_nso_plugin.models import NSOIntentRevision, NSOInterfaceIPState

        pool = Prefix.objects.get(pk=self.pool_lo4.pk)
        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback151", type="virtual")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="ip")
        before = revision.revision
        result = {"allocated": [], "errors": [], "skipped": []}
        with (
            patch.object(NSOInterfaceIPState.objects, "update_or_create", side_effect=IntegrityError("duplicate")),
            patch.object(IPAddress, "delete", wraps=IPAddress.delete) as delete,
        ):
            _reserve_single(iface, mgmt, "ipv4", pool, result, push=False)

        assert result["errors"]
        assert "Failed to create NSOInterfaceIPState" in result["errors"][0]["reason"]
        assert not IPAddress.objects.filter(assigned_object_id=iface.pk).exists()
        revision.refresh_from_db()
        assert revision.revision == before, "a failed reservation committed an intent revision"
        delete.assert_not_called()

    def test_reserve_single_rechecks_fill_empty_under_the_intent_lock(self):
        from netbox_nso_plugin.ip_autoassign import _reserve_single
        from netbox_nso_plugin.models import NSOIntentRevision, NSOInterfaceIPState

        pool = Prefix.objects.get(pk=self.pool_lo4.pk)
        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback153", type="virtual")
        existing = NSOInterfaceIPState.objects.create(
            interface=iface,
            address="10.100.0.99/24",
            family="ipv4",
            status="accepted",
        )
        revision = NSOIntentRevision.objects.get(device=self.device, scope="ip")
        before = revision.revision
        result = {"allocated": [], "errors": [], "skipped": []}

        _reserve_single(iface, mgmt, "ipv4", pool, result, push=False)

        assert result["allocated"] == []
        assert result["errors"] == []
        assert result["skipped"] == [
            {
                "interface": str(iface),
                "family": "ipv4",
                "reason": "Already has a managed IP in this family",
            }
        ]
        assert list(NSOInterfaceIPState.objects.filter(interface=iface)) == [existing]
        assert not IPAddress.objects.filter(assigned_object_id=iface.pk).exists()
        revision.refresh_from_db()
        assert revision.revision == before

    def test_reserve_single_exhaustion_does_not_advance_revision(self):
        from netbox_nso_plugin.ip_autoassign import _reserve_single
        from netbox_nso_plugin.models import NSOIntentRevision

        pool = Prefix.objects.get(pk=self.pool_lo4.pk)
        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback154", type="virtual")
        revision = NSOIntentRevision.objects.get(device=self.device, scope="ip")
        before = revision.revision
        result = {"allocated": [], "errors": [], "skipped": []}

        with patch.object(Prefix, "get_first_available_ip", return_value=None):
            _reserve_single(iface, mgmt, "ipv4", pool, result, push=False)

        assert result["errors"]
        revision.refresh_from_db()
        assert revision.revision == before

    def test_push_schedule_failure_rolls_back_the_reservation(self):
        from netbox_nso_plugin.ip_autoassign import _reserve_single

        pool = Prefix.objects.get(pk=self.pool_lo4.pk)
        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback152", type="virtual")
        result = {"allocated": [], "errors": [], "skipped": []}

        with patch(
            "netbox_nso_plugin.signals._schedule_intent_push",
            side_effect=[None, RuntimeError("schedule failed")],
        ) as schedule:
            _reserve_single(iface, mgmt, "ipv4", pool, result, push=True)

        self.assertEqual(schedule.call_count, 2)
        assert result["errors"] == [
            {
                "interface": str(iface),
                "family": "ipv4",
                "reason": "Failed to schedule the IP intent push: schedule failed",
            }
        ]
        assert not IPAddress.objects.filter(assigned_object_id=iface.pk).exists()

    def test_fill_empty_skips_interface_with_managed_ip(self):
        from netbox_nso_plugin.models import NSOInterfaceIPState

        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback101", type="virtual")
        # Pre-existing accepted state → fill-empty guard should fire.
        NSOInterfaceIPState.objects.create(
            interface=iface,
            address="10.100.0.99/24",
            family="ipv4",
            status="accepted",
        )

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            from netbox_nso_plugin.ip_autoassign import auto_assign_ip

            result = auto_assign_ip(iface, families=("ipv4",))

        self.assertEqual(len(result["allocated"]), 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("Already has a managed IP", result["skipped"][0]["reason"])

        mgmt.delete()

    def test_no_pool_returns_error(self):
        mgmt = self._make_mgmt()
        iface = Interface.objects.create(device=self.device, name="Loopback102", type="virtual")

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            with patch("netbox_nso_plugin.ip_autoassign.find_pool", return_value=None):
                from netbox_nso_plugin.ip_autoassign import auto_assign_ip

                result = auto_assign_ip(iface, families=("ipv4",))

        self.assertEqual(len(result["allocated"]), 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("No ipv4 pool found", result["errors"][0]["reason"])

        mgmt.delete()

    def test_unmanaged_device_returns_error(self):
        from netbox_nso_plugin.ip_autoassign import auto_assign_ip

        iface = Interface.objects.create(device=self.device, name="Loopback103", type="virtual")
        result = auto_assign_ip(iface, families=("ipv4",))
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("not managed", result["errors"][0]["reason"])

    def test_p2p_core_no_peer_returns_error(self):
        """P2P interface without a cable peer returns an error (Phase B active)."""
        from extras.models import Tag

        mgmt = self._make_mgmt()
        tag = Tag.objects.create(name="p2p-core-test", slug="p2p-core")
        iface = Interface.objects.create(device=self.device, name="Gi99/0", type="1000base-t")
        iface.tags.add(tag)

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            from netbox_nso_plugin.ip_autoassign import auto_assign_ip

            result = auto_assign_ip(iface, families=("ipv4",))

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("no cable peer found", result["errors"][0]["reason"])

        mgmt.delete()


class TestSingleAllocationPoolLock(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from ._outbox_case import make_managed, without_commit_drain

        with without_commit_drain():
            self.device_a, self.management_a = make_managed("single-pool", 4301, index=1)
            self.device_b, self.management_b = make_managed("single-pool", 4302, index=2)
            self.interface_a = Interface.objects.create(device=self.device_a, name="Loopback1", type="virtual")
            self.interface_b = Interface.objects.create(device=self.device_b, name="Loopback1", type="virtual")
        pool = Prefix.objects.create(prefix="198.18.0.0/29")
        self.pool = Prefix.objects.get(pk=pool.pk)

    def test_concurrent_draws_from_one_pool_lock_the_prefix(self):
        from netbox_nso_plugin.ip_autoassign import _reserve_single
        from netbox_nso_plugin.models import NSOInterfaceIPState

        from ._outbox_case import wait_until_postgres_blocks

        first_queried = threading.Event()
        release_first = threading.Event()
        second_connected = threading.Event()
        second_pid: list[int] = []
        failures: list[BaseException] = []
        first_result = {"allocated": [], "errors": [], "skipped": []}
        second_result = {"allocated": [], "errors": [], "skipped": []}
        real_first_available = Prefix.get_first_available_ip

        def pause_first_draw(pool):
            available = real_first_available(pool)
            if threading.current_thread().name == "first-allocation":
                first_queried.set()
                assert release_first.wait(timeout=30), "the first allocation barrier was not released"
            return available

        def allocate(interface, management, result, *, record_pid=False):
            try:
                if record_pid:
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        second_pid.append(cursor.fetchone()[0])
                    second_connected.set()
                _reserve_single(interface, management, "ipv4", self.pool, result, push=False)
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
            finally:
                connections["default"].close()

        with patch.object(Prefix, "get_first_available_ip", pause_first_draw):
            first = threading.Thread(
                target=allocate,
                args=(self.interface_a, self.management_a, first_result),
                name="first-allocation",
            )
            second = threading.Thread(
                target=allocate,
                args=(self.interface_b, self.management_b, second_result),
                kwargs={"record_pid": True},
                name="second-allocation",
            )
            first.start()
            self.addCleanup(release_first.set)
            self.addCleanup(first.join, 30)
            assert first_queried.wait(timeout=30), (failures, first_result)
            second.start()
            self.addCleanup(second.join, 30)
            assert second_connected.wait(timeout=30), "the second allocation never opened its connection"
            blocked_failure = None
            try:
                wait_until_postgres_blocks(second_pid[0], "the second allocation")
            except BaseException as exc:  # noqa: BLE001
                blocked_failure = exc
            finally:
                release_first.set()
            first.join(timeout=30)
            second.join(timeout=30)

        assert not first.is_alive(), "the first allocation did not finish"
        assert not second.is_alive(), "the second allocation did not finish"
        if blocked_failure is not None:
            raise blocked_failure
        assert not failures, failures
        self.assertEqual(first_result["errors"], [])
        self.assertEqual(second_result["errors"], [])
        addresses = {
            first_result["allocated"][0]["address"],
            second_result["allocated"][0]["address"],
        }
        self.assertEqual(len(addresses), 2)
        states = NSOInterfaceIPState.objects.filter(source_pool=self.pool)
        self.assertEqual(states.count(), 2)
        self.assertEqual(set(states.values_list("address", flat=True)), addresses)
        self.assertEqual(
            IPAddress.objects.filter(assigned_object_id__in=[self.interface_a.pk, self.interface_b.pk]).count(),
            2,
        )

    def test_single_and_p2p_draws_use_one_lock_order(self):
        from netbox_nso_plugin import intent_state, ip_autoassign

        from ._outbox_case import wait_until_postgres_blocks

        single_interface = self.interface_a
        p2p_interface = Interface.objects.create(device=self.device_a, name="Ethernet1", type="1000base-t")
        peer_interface = Interface.objects.create(device=self.device_b, name="Ethernet1", type="1000base-t")
        p2p_ready = threading.Event()
        request_p2p_pool = threading.Event()
        single_has_pool = threading.Event()
        release_single = threading.Event()
        p2p_connected = threading.Event()
        p2p_pid: list[int] = []
        failures: list[BaseException] = []
        single_result = {"allocated": [], "errors": [], "skipped": []}
        p2p_result = {"allocated": [], "errors": [], "skipped": []}
        real_intent_transaction = intent_state.intent_transaction

        def pool_finder(_family, _site):
            p2p_ready.set()
            assert request_p2p_pool.wait(timeout=30), "the P2P pool request was not released"
            return self.pool

        @contextmanager
        def observe_single_intent(footprint):
            with real_intent_transaction(footprint) as mutation:
                if threading.current_thread().name == "single-allocation":
                    single_has_pool.set()
                    assert release_single.wait(timeout=30), "the single allocation was not released"
                yield mutation

        def allocate_single():
            try:
                ip_autoassign._reserve_single(
                    single_interface,
                    self.management_a,
                    "ipv4",
                    self.pool,
                    single_result,
                    push=False,
                )
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
            finally:
                connections["default"].close()

        def allocate_p2p():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    p2p_pid.append(cursor.fetchone()[0])
                p2p_connected.set()
                ip_autoassign._assign_one_p2p_family(
                    p2p_interface,
                    peer_interface,
                    self.management_a,
                    self.management_b,
                    "ipv4",
                    self.device_a.site,
                    p2p_result,
                    pool_finder=pool_finder,
                    no_pool_reason=lambda _family: "no pool",
                    push=False,
                )
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
            finally:
                connections["default"].close()

        with patch.object(intent_state, "intent_transaction", side_effect=observe_single_intent):
            p2p = threading.Thread(target=allocate_p2p, name="p2p-allocation")
            single = threading.Thread(target=allocate_single, name="single-allocation")
            p2p.start()
            self.addCleanup(request_p2p_pool.set)
            self.addCleanup(p2p.join, 30)
            assert p2p_connected.wait(timeout=30), "the P2P allocation never opened its database connection"
            assert p2p_ready.wait(timeout=30), (failures, p2p_result)
            single.start()
            self.addCleanup(release_single.set)
            self.addCleanup(single.join, 30)
            assert single_has_pool.wait(timeout=30), (failures, single_result)
            request_p2p_pool.set()
            try:
                wait_until_postgres_blocks(p2p_pid[0], "the P2P allocation")
            finally:
                release_single.set()
            p2p.join(timeout=30)
            single.join(timeout=30)

        assert not p2p.is_alive(), "the P2P allocation did not finish"
        assert not single.is_alive(), "the single allocation did not finish"
        assert not failures, failures
        self.assertNotIn("deadlock", str(p2p_result["errors"]).lower())
        self.assertEqual(single_result["errors"], [])

    def test_link_role_wrapper_locks_the_pool_before_intent(self):
        from netbox_nso_plugin import intent_state, ip_autoassign
        from netbox_nso_plugin.link_role import provision_link_role
        from netbox_nso_plugin.models import NSOLinkRole, NSOLinkRoleAssignment

        from ._outbox_case import wait_until_postgres_blocks

        wrapper_interface = Interface.objects.create(device=self.device_a, name="Loopback2", type="virtual")
        pool_role = Role.objects.create(name="Single wrapper pool", slug="single-wrapper-pool")
        self.pool.role = pool_role
        self.pool.save(update_fields=["role"])
        role = NSOLinkRole.objects.create(
            name="single-wrapper-lock",
            slug="single-wrapper-lock",
            link_type="single",
            assign_ipv4=True,
            assign_ipv6=False,
            ipv4_pool_role=pool_role.slug,
        )
        NSOLinkRoleAssignment.objects.create(role=role, interface=wrapper_interface)
        single_has_pool = threading.Event()
        release_single = threading.Event()
        wrapper_connected = threading.Event()
        wrapper_at_assignment = threading.Event()
        release_wrapper = threading.Event()
        wrapper_pid: list[int] = []
        failures: list[BaseException] = []
        single_result = {"allocated": [], "errors": [], "skipped": []}
        wrapper_result: list[dict] = []
        real_intent_transaction = intent_state.intent_transaction
        real_assign_ips_for_role = ip_autoassign.assign_ips_for_role

        def observe_single_intent(footprint):
            if threading.current_thread().name == "direct-single-allocation":
                single_has_pool.set()
                assert release_single.wait(timeout=30), "the direct allocation was not released"
            return real_intent_transaction(footprint)

        def pause_wrapper_assignment(*args, **kwargs):
            wrapper_at_assignment.set()
            assert release_wrapper.wait(timeout=30), "the link-role assignment was not released"
            return real_assign_ips_for_role(*args, **kwargs)

        def allocate_single():
            try:
                ip_autoassign._reserve_single(
                    self.interface_a,
                    self.management_a,
                    "ipv4",
                    self.pool,
                    single_result,
                    push=False,
                )
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
            finally:
                connections["default"].close()

        def provision_wrapper():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    wrapper_pid.append(cursor.fetchone()[0])
                wrapper_connected.set()
                wrapper_result.append(provision_link_role(wrapper_interface))
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
            finally:
                connections["default"].close()

        with (
            patch.object(intent_state, "intent_transaction", side_effect=observe_single_intent),
            patch.object(ip_autoassign, "assign_ips_for_role", side_effect=pause_wrapper_assignment),
        ):
            single = threading.Thread(target=allocate_single, name="direct-single-allocation")
            wrapper = threading.Thread(target=provision_wrapper, name="link-role-provision")
            single.start()
            self.addCleanup(release_single.set)
            self.addCleanup(single.join, 30)
            assert single_has_pool.wait(timeout=30), (failures, single_result)
            wrapper.start()
            self.addCleanup(release_wrapper.set)
            self.addCleanup(wrapper.join, 30)
            assert wrapper_connected.wait(timeout=30), "link-role provisioning never opened its connection"
            blocked_failure = None
            try:
                wait_until_postgres_blocks(wrapper_pid[0], "link-role provisioning")
            except BaseException as exc:  # noqa: BLE001
                blocked_failure = exc
            finally:
                release_single.set()
                release_wrapper.set()
            single.join(timeout=30)
            wrapper.join(timeout=30)

        assert not single.is_alive(), "the direct allocation did not finish"
        assert not wrapper.is_alive(), "link-role provisioning did not finish"
        if blocked_failure is not None:
            raise blocked_failure
        assert wrapper_at_assignment.is_set(), "link-role provisioning never reached IP assignment"
        assert not failures, failures
        self.assertEqual(single_result["errors"], [])
        self.assertEqual(wrapper_result[0]["errors"], [])


class TestRollbackAutoAssigned(TestCase):
    """rollback_auto_assigned: deletes IPAddress and NSOInterfaceIPState."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RbMfg", slug="rbmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="RbDev", slug="rbdev")
        role = DeviceRole.objects.create(name="RbRole", slug="rbrole")
        site = Site.objects.create(name="RbSite", slug="rbsite")
        cls.device = Device.objects.create(name="rb-router", device_type=device_type, role=role, site=site)

    def test_rollback_deletes_ip_and_state(self):
        from netbox_nso_plugin.ip_autoassign import rollback_auto_assigned
        from netbox_nso_plugin.models import NSOInterfaceIPState

        iface = Interface.objects.create(device=self.device, name="Loopback200", type="virtual")
        ip = IPAddress.objects.create(address="10.200.0.1/32", status="reserved")
        ip.assigned_object = iface
        ip.save()
        state = NSOInterfaceIPState.objects.create(
            interface=iface,
            address="10.200.0.1/32",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
        )

        rollback_auto_assigned(state)

        self.assertFalse(IPAddress.objects.filter(address="10.200.0.1/32").exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk=state.pk).exists())

    def test_single_address_rollback_preserves_the_shared_source_pool(self):
        from netbox_nso_plugin.ip_autoassign import rollback_auto_assigned
        from netbox_nso_plugin.models import NSOInterfaceIPState

        pool = Prefix.objects.create(prefix="198.18.200.0/24", status="active")
        iface = Interface.objects.create(device=self.device, name="Loopback202", type="virtual")
        ip = IPAddress.objects.create(address="198.18.200.1/32", status="reserved")
        ip.assigned_object = iface
        ip.save()
        state = NSOInterfaceIPState.objects.create(
            interface=iface,
            address="198.18.200.1/32",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
            allocation_kind=NSOInterfaceIPState.ALLOCATION_KIND_SINGLE,
            source_pool=pool,
        )

        rollback_auto_assigned(state)

        self.assertFalse(IPAddress.objects.filter(pk=ip.pk).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk=state.pk).exists())
        self.assertTrue(Prefix.objects.filter(pk=pool.pk).exists())

    def test_rollback_noop_for_non_auto_assigned(self):
        from netbox_nso_plugin.ip_autoassign import rollback_auto_assigned
        from netbox_nso_plugin.models import NSOInterfaceIPState

        iface = Interface.objects.create(device=self.device, name="Loopback201", type="virtual")
        ip = IPAddress.objects.create(address="10.200.0.2/32", status="active")
        ip.assigned_object = iface
        ip.save()
        state = NSOInterfaceIPState.objects.create(
            interface=iface,
            address="10.200.0.2/32",
            family="ipv4",
            status="in_sync",
            auto_assigned=False,
        )

        rollback_auto_assigned(state)

        # Nothing deleted — not auto_assigned
        self.assertTrue(IPAddress.objects.filter(address="10.200.0.2/32").exists())
        self.assertTrue(NSOInterfaceIPState.objects.filter(pk=state.pk).exists())


class TestReconcileAutoAssignedActivation(TestCase):
    """Reconciler: auto_assigned in_sync → IPAddress promoted to active."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RecMfg", slug="recmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="RecDev", slug="recdev")
        role = DeviceRole.objects.create(name="RecRole", slug="recrecrole")
        site = Site.objects.create(name="RecSite", slug="recsite")
        cls.device = Device.objects.create(name="rec-router", device_type=device_type, role=role, site=site)

    def _auto_create_ctx(self, auto_create: bool = False):
        """Flip the real AppConfig's auto-create flag (existence-checked by patch.object)."""
        from django.apps import apps

        cfg = apps.get_app_config("netbox_nso_plugin")
        return patch.object(cfg, "_interface_ip_auto_create", auto_create)

    def test_auto_assigned_in_sync_activates_ip(self):
        """When reconciler sees an auto_assigned accepted→in_sync transition, flip IPAddress to active."""
        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        iface = Interface.objects.create(device=self.device, name="Loopback300", type="virtual")
        # Create IPAddress in 'reserved' state, assigned to interface
        ip = IPAddress.objects.create(address="10.50.0.1/32", status="reserved")
        ip.assigned_object = iface
        ip.save()
        # Create NSOInterfaceIPState as accepted + auto_assigned
        NSOInterfaceIPState.objects.create(
            interface=iface,
            address="10.50.0.1/32",
            vrf="",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
        )

        # Payload: NSO reports the IP is on the interface
        payload = {
            "interfaces": [
                {
                    "interface": "Loopback300",
                    "addresses": [{"address": "10.50.0.1/32", "vrf": "", "family": "ipv4", "secondary": False}],
                }
            ]
        }

        with self._auto_create_ctx(False):
            _reconcile_interface_ips(self.device, payload)

        state = NSOInterfaceIPState.objects.get(interface=iface, address="10.50.0.1/32")
        self.assertEqual(state.status, "in_sync")

        ip.refresh_from_db()
        self.assertEqual(
            ip.status, "active", "IPAddress should be promoted to active when auto_assigned reaches in_sync"
        )

    def test_non_auto_assigned_in_sync_does_not_touch_ip_status(self):
        """Non-auto_assigned in_sync rows must NOT alter the IPAddress status."""
        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        iface = Interface.objects.create(device=self.device, name="Loopback301", type="virtual")
        ip = IPAddress.objects.create(address="10.50.0.2/32", status="active")
        ip.assigned_object = iface
        ip.save()
        NSOInterfaceIPState.objects.create(
            interface=iface,
            address="10.50.0.2/32",
            vrf="",
            family="ipv4",
            status="accepted",
            auto_assigned=False,
        )

        payload = {
            "interfaces": [
                {
                    "interface": "Loopback301",
                    "addresses": [{"address": "10.50.0.2/32", "vrf": "", "family": "ipv4", "secondary": False}],
                }
            ]
        }

        with self._auto_create_ctx(False):
            _reconcile_interface_ips(self.device, payload)

        ip.refresh_from_db()
        self.assertEqual(ip.status, "active")  # unchanged — was already active


# ── tests ─────────────────────────────────────────────────────────


def _make_cable_pair(iface_a, iface_b):
    """Create a Cable + two CableTerminations connecting iface_a ↔ iface_b."""
    cable = Cable.objects.create(status="connected")
    CableTermination.objects.create(cable=cable, cable_end="A", termination=iface_a)
    CableTermination.objects.create(cable=cable, cable_end="B", termination=iface_b)
    return cable


class TestClassifyInterfaceP2PAutoDetect(TestCase):
    """classify_interface: P2P auto-detection via device role slugs."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="P2PMfg", slug="p2pmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="P2PDevice", slug="p2pdevice")
        cls.core_role = DeviceRole.objects.create(name="Core Router", slug="core-router")
        cls.edge_role = DeviceRole.objects.create(name="Edge Router", slug="edge-router")
        site = Site.objects.create(name="P2PSite", slug="p2psite")
        cls.device_a = Device.objects.create(name="p2p-core-a", device_type=dt, role=cls.core_role, site=site)
        cls.device_b = Device.objects.create(name="p2p-core-b", device_type=dt, role=cls.core_role, site=site)
        cls.device_edge = Device.objects.create(name="p2p-edge", device_type=dt, role=cls.edge_role, site=site)

    def _with_core_slugs(self, slugs):
        """Set the real AppConfig's optional core-role-slugs override.

        Production reads it via ``getattr(cfg, "_p2p_core_device_role_slugs", DEFAULT)``,
        so the attribute is normally absent — create=True lets patch.object inject it
        (and tear it back down) on the live AppConfig, matching a real config override.
        """
        from django.apps import apps

        cfg = apps.get_app_config("netbox_nso_plugin")
        return patch.object(cfg, "_p2p_core_device_role_slugs", slugs, create=True)

    def test_p2p_core_detected_via_role_slugs(self):
        from netbox_nso_plugin.ip_autoassign import classify_interface

        iface_a = Interface.objects.create(device=self.device_a, name="Gi0/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi0/0/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)  # refresh cache

        with self._with_core_slugs(frozenset(["core-router"])):
            self.assertEqual(classify_interface(iface_a), "p2p-core")

    def test_p2p_core_not_detected_when_peer_role_not_in_set(self):
        from netbox_nso_plugin.ip_autoassign import classify_interface

        iface_a = Interface.objects.create(device=self.device_a, name="Gi0/1/0", type="1000base-t")
        iface_edge = Interface.objects.create(device=self.device_edge, name="Gi0/1/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_edge)
        iface_a = Interface.objects.get(pk=iface_a.pk)

        with self._with_core_slugs(frozenset(["core-router"])):
            # peer is edge-router, not in set → falls through to access
            self.assertEqual(classify_interface(iface_a), "access")

    def test_p2p_core_not_detected_when_no_cable_peer(self):
        from netbox_nso_plugin.ip_autoassign import classify_interface

        iface_a = Interface.objects.create(device=self.device_a, name="Gi0/2/0", type="1000base-t")

        with self._with_core_slugs(frozenset(["core-router"])):
            self.assertEqual(classify_interface(iface_a), "access")

    def test_p2p_core_not_detected_when_slugs_empty(self):
        from netbox_nso_plugin.ip_autoassign import classify_interface

        iface_a = Interface.objects.create(device=self.device_a, name="Gi0/3/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi0/3/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)

        with self._with_core_slugs(frozenset()):
            # empty slug set → heuristic skipped
            self.assertEqual(classify_interface(iface_a), "access")


class TestCarveP2PChild(TestCase):
    """carve_p2p_child: /31 allocation from /24 pool, custom field override, full pool."""

    @classmethod
    def setUpTestData(cls):
        cls.p2p_role = Role.objects.create(name="P2P Core", slug="p2p-core")
        cls.pool_v4 = Prefix.objects.create(prefix="10.100.0.0/24", role=cls.p2p_role)
        cls.pool_v6 = Prefix.objects.create(prefix="fc01::/48", role=cls.p2p_role)

    def test_carve_returns_child_prefix_and_two_hosts(self):
        from netbox_nso_plugin.ip_autoassign import carve_p2p_child

        result = carve_p2p_child(self.pool_v4, "ipv4")
        self.assertIsNotNone(result)
        child, host_a, host_b = result
        self.assertIsNotNone(child.pk)
        self.assertTrue(host_a.endswith("/31"))
        self.assertTrue(host_b.endswith("/31"))
        self.assertNotEqual(host_a, host_b)

    def test_carve_v6_returns_127_prefix(self):
        from netbox_nso_plugin.ip_autoassign import carve_p2p_child

        result = carve_p2p_child(self.pool_v6, "ipv6")
        self.assertIsNotNone(result)
        child, host_a, host_b = result
        self.assertTrue(host_a.endswith("/127"))

    def test_carve_exhausted_pool_returns_none(self):
        from netbox_nso_plugin.ip_autoassign import carve_p2p_child

        # Create a tiny /32 pool (single host, no space for /31 child)
        tiny_role = Role.objects.create(name="Tiny", slug="tiny-p2p-test")
        tiny_pool = Prefix.objects.create(prefix="10.250.0.0/32", role=tiny_role)
        result = carve_p2p_child(tiny_pool, "ipv4")
        self.assertIsNone(result)


class TestAutoAssignIPP2P(TestCase):
    """auto_assign_ip P2P path: happy path, fill-empty guard, no pool, no peer."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="P2AsgMfg", slug="p2asgmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="P2AsgDev", slug="p2asgdev")
        role = DeviceRole.objects.create(name="P2AsgRole", slug="p2asgrole")
        site = Site.objects.create(name="P2AsgSite", slug="p2asgsite")
        cls.device_a = Device.objects.create(name="p2asg-a", device_type=dt, role=role, site=site)
        cls.device_b = Device.objects.create(name="p2asg-b", device_type=dt, role=role, site=site)
        cls.p2p_role = Role.objects.create(name="P2P Core Asg", slug="p2p-core")
        cls.pool = Prefix.objects.create(prefix="10.99.0.0/24", role=cls.p2p_role)

    def _make_mgmt(self, device, name):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="test-nso",
            defaults={"adapter_instance_id": "test-nso-p2p"},
        )
        return NSODeviceManagement.objects.create(
            device=device,
            nso_instance=inst,
            nso_device_name=name,
            adapter_device_id=device.pk,
        )

    def test_p2p_allocates_two_ips(self):
        from extras.models import Tag

        from netbox_nso_plugin.ip_autoassign import auto_assign_ip
        from netbox_nso_plugin.models import NSOInterfaceIPState

        mgmt_a = self._make_mgmt(self.device_a, "p2p-dev-a")
        mgmt_b = self._make_mgmt(self.device_b, "p2p-dev-b")

        iface_a = Interface.objects.create(device=self.device_a, name="Gi10/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi10/0/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)

        tag = Tag.objects.create(name="p2p-core-asg", slug="p2p-core")
        iface_a.tags.add(tag)

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            result = auto_assign_ip(iface_a, families=("ipv4",))

        self.assertEqual(len(result["allocated"]), 2, result)
        addrs = {r["address"] for r in result["allocated"]}
        self.assertEqual(len(addrs), 2)

        state_a = NSOInterfaceIPState.objects.get(interface=iface_a, family="ipv4")
        state_b = NSOInterfaceIPState.objects.get(interface=iface_b, family="ipv4")
        self.assertEqual(state_a.peer_state_id, state_b.pk)
        self.assertEqual(state_b.peer_state_id, state_a.pk)
        self.assertTrue(state_a.auto_assigned)
        self.assertTrue(state_b.auto_assigned)
        self.assertEqual(state_a.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_P2P)
        self.assertEqual(state_b.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_P2P)

        mgmt_a.delete()
        mgmt_b.delete()

    def test_p2p_allocated_ips_carry_pool_vrf(self):
        """A VRF-scoped P2P pool (as a link-role resolves) must land BOTH end IPAddresses in that
        VRF, not the global table."""
        from ipam.models import VRF

        from netbox_nso_plugin.ip_autoassign import _assign_one_p2p_family

        vrf = VRF.objects.create(name="P2P-VRF")
        self.pool.vrf = vrf
        self.pool.save()
        mgmt_a = self._make_mgmt(self.device_a, "p2p-vrf-a")
        mgmt_b = self._make_mgmt(self.device_b, "p2p-vrf-b")
        iface_a = Interface.objects.create(device=self.device_a, name="Gi11/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi11/0/0", type="1000base-t")
        result = {"allocated": [], "errors": [], "skipped": []}
        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            _assign_one_p2p_family(
                iface_a,
                iface_b,
                mgmt_a,
                mgmt_b,
                "ipv4",
                None,
                result,
                pool_finder=lambda _fam, _s: self.pool,
                no_pool_reason=lambda _fam: "no pool",
                push=False,
            )
        self.assertEqual(len(result["allocated"]), 2, result)
        for entry in result["allocated"]:
            self.assertEqual(IPAddress.objects.get(address=entry["address"]).vrf, vrf)
        mgmt_a.delete()
        mgmt_b.delete()

    def test_p2p_state_failure_rolls_back_carve_and_ips(self):
        """A failure during state creation must roll back the carved child prefix + both reserved
        IPAddresses (one atomic), leaving no IPAM debris — the standalone M13 path has no outer
        transaction to clean up after it."""
        from extras.models import Tag

        from netbox_nso_plugin.ip_autoassign import auto_assign_ip
        from netbox_nso_plugin.models import NSOInterfaceIPState

        mgmt_a = self._make_mgmt(self.device_a, "p2p-rb-a")
        mgmt_b = self._make_mgmt(self.device_b, "p2p-rb-b")
        iface_a = Interface.objects.create(device=self.device_a, name="Gi12/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi12/0/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)
        iface_a.tags.add(Tag.objects.create(name="p2p-core-rb", slug="p2p-core"))

        prefixes_before = Prefix.objects.count()
        with (
            patch("netbox_nso_plugin.signals._push_ip_intent_for_device"),
            patch.object(NSOInterfaceIPState.objects, "update_or_create", side_effect=Exception("state boom")),
        ):
            result = auto_assign_ip(iface_a, families=("ipv4",))
        self.assertTrue(result["errors"], result)
        self.assertEqual(result["allocated"], [])
        # Rolled back: no carved child prefix, no reserved IPAddresses, no state rows.
        self.assertEqual(Prefix.objects.count(), prefixes_before)
        self.assertEqual(IPAddress.objects.filter(status="reserved").count(), 0)
        self.assertEqual(NSOInterfaceIPState.objects.filter(interface__in=[iface_a, iface_b]).count(), 0)
        mgmt_a.delete()
        mgmt_b.delete()

    def test_p2p_fill_empty_guard_skips_when_occupied(self):
        from extras.models import Tag

        from netbox_nso_plugin.ip_autoassign import auto_assign_ip
        from netbox_nso_plugin.models import NSOInterfaceIPState

        mgmt_a = self._make_mgmt(self.device_a, "p2p-dev-a2")
        mgmt_b = self._make_mgmt(self.device_b, "p2p-dev-b2")

        iface_a = Interface.objects.create(device=self.device_a, name="Gi11/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi11/0/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)

        tag = Tag.objects.get_or_create(name="p2p-core-asg", slug="p2p-core")[0]
        iface_a.tags.add(tag)

        # Pre-create a managed state on device A
        NSOInterfaceIPState.objects.create(
            interface=iface_a,
            address="10.99.0.100/31",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
        )

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            result = auto_assign_ip(iface_a, families=("ipv4",))

        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("already have a managed", result["skipped"][0]["reason"])

        mgmt_a.delete()
        mgmt_b.delete()

    def test_p2p_error_when_no_pool(self):
        from extras.models import Tag

        from netbox_nso_plugin.ip_autoassign import auto_assign_ip
        from netbox_nso_plugin.models import NSOIntentRevision

        mgmt_a = self._make_mgmt(self.device_a, "p2p-dev-a3")
        mgmt_b = self._make_mgmt(self.device_b, "p2p-dev-b3")

        iface_a = Interface.objects.create(device=self.device_a, name="Gi12/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi12/0/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)

        tag = Tag.objects.get_or_create(name="p2p-core-asg", slug="p2p-core")[0]
        iface_a.tags.add(tag)
        revisions = list(
            NSOIntentRevision.objects.filter(device__in=(self.device_a, self.device_b), scope="ip").order_by(
                "device_id"
            )
        )
        before = [(revision.pk, revision.revision) for revision in revisions]

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            with patch("netbox_nso_plugin.ip_autoassign.find_pool", return_value=None):
                result = auto_assign_ip(iface_a, families=("ipv4",))

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("No ipv4 p2p-core pool found", result["errors"][0]["reason"])
        self.assertEqual(
            list(
                NSOIntentRevision.objects.filter(pk__in=[revision.pk for revision in revisions])
                .order_by("device_id")
                .values_list("pk", "revision")
            ),
            before,
        )

        mgmt_a.delete()
        mgmt_b.delete()

    def test_p2p_error_when_peer_not_managed(self):
        from extras.models import Tag

        from netbox_nso_plugin.ip_autoassign import auto_assign_ip

        mgmt_a = self._make_mgmt(self.device_a, "p2p-dev-a4")

        iface_a = Interface.objects.create(device=self.device_a, name="Gi13/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi13/0/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)

        tag = Tag.objects.get_or_create(name="p2p-core-asg", slug="p2p-core")[0]
        iface_a.tags.add(tag)

        with patch("netbox_nso_plugin.signals._push_ip_intent_for_device"):
            result = auto_assign_ip(iface_a, families=("ipv4",))

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("peer device is not managed", result["errors"][0]["reason"])

        mgmt_a.delete()


class TestRollbackP2PCascade(TestCase):
    """rollback_auto_assigned: P2P cascade deletes both states and child prefix."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RbP2PMfg", slug="rbp2pmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RbP2PDev", slug="rbp2pdev")
        role = DeviceRole.objects.create(name="RbP2PRole", slug="rbp2prole")
        site = Site.objects.create(name="RbP2PSite", slug="rbp2psite")
        cls.device_a = Device.objects.create(name="rbp2p-a", device_type=dt, role=role, site=site)
        cls.device_b = Device.objects.create(name="rbp2p-b", device_type=dt, role=role, site=site)

    def test_rollback_cascade_deletes_both_states_and_child(self):
        from netbox_nso_plugin.ip_autoassign import rollback_auto_assigned
        from netbox_nso_plugin.models import NSOInterfaceIPState

        child = Prefix.objects.create(prefix="10.88.0.0/31", status="reserved")

        iface_a = Interface.objects.create(device=self.device_a, name="Gi20/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi20/0/0", type="1000base-t")

        ip_a = IPAddress.objects.create(address="10.88.0.0/31", status="reserved")
        ip_a.assigned_object = iface_a
        ip_a.save()

        ip_b = IPAddress.objects.create(address="10.88.0.1/31", status="reserved")
        ip_b.assigned_object = iface_b
        ip_b.save()

        state_a = NSOInterfaceIPState.objects.create(
            interface=iface_a,
            address="10.88.0.0/31",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
            allocation_kind=NSOInterfaceIPState.ALLOCATION_KIND_P2P,
            source_pool=child,
        )
        state_b = NSOInterfaceIPState.objects.create(
            interface=iface_b,
            address="10.88.0.1/31",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
            allocation_kind=NSOInterfaceIPState.ALLOCATION_KIND_P2P,
            source_pool=child,
        )
        state_a.peer_state = state_b
        state_a.save(update_fields=["peer_state"])
        state_b.peer_state = state_a
        state_b.save(update_fields=["peer_state"])

        rollback_auto_assigned(state_a)

        self.assertFalse(IPAddress.objects.filter(address="10.88.0.0/31").exists())
        self.assertFalse(IPAddress.objects.filter(address="10.88.0.1/31").exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk=state_a.pk).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk=state_b.pk).exists())
        self.assertFalse(Prefix.objects.filter(prefix="10.88.0.0/31").exists())

    def test_rollback_deletes_the_p2p_child_after_the_peer_row_disappears(self):
        from netbox_nso_plugin.ip_autoassign import rollback_auto_assigned
        from netbox_nso_plugin.models import NSOInterfaceIPState

        child = Prefix.objects.create(prefix="198.18.202.0/31", status="reserved")
        iface_a = Interface.objects.create(device=self.device_a, name="Gi21/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi21/0/0", type="1000base-t")
        ip_a = IPAddress.objects.create(address="198.18.202.0/31", status="reserved")
        ip_a.assigned_object = iface_a
        ip_a.save()
        state_a = NSOInterfaceIPState.objects.create(
            interface=iface_a,
            address="198.18.202.0/31",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
            allocation_kind=NSOInterfaceIPState.ALLOCATION_KIND_P2P,
            source_pool=child,
        )
        state_b = NSOInterfaceIPState.objects.create(
            interface=iface_b,
            address="198.18.202.1/31",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
            allocation_kind=NSOInterfaceIPState.ALLOCATION_KIND_P2P,
            source_pool=child,
        )
        state_a.peer_state = state_b
        state_a.save(update_fields=["peer_state"])
        state_b.delete()
        state_a.refresh_from_db()
        self.assertIsNone(state_a.peer_state_id)

        rollback_auto_assigned(state_a)

        self.assertFalse(IPAddress.objects.filter(pk=ip_a.pk).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk=state_a.pk).exists())
        self.assertFalse(Prefix.objects.filter(pk=child.pk).exists())


class TestReconcileP2PBothInSync(TestCase):
    """Reconciler: both P2P ends must reach in_sync before IPs become active."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RecP2PMfg", slug="recp2pmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RecP2PDev", slug="recp2pdev")
        role = DeviceRole.objects.create(name="RecP2PRole", slug="recp2prole")
        site = Site.objects.create(name="RecP2PSite", slug="recp2psite")
        cls.device_a = Device.objects.create(name="recp2p-a", device_type=dt, role=role, site=site)
        cls.device_b = Device.objects.create(name="recp2p-b", device_type=dt, role=role, site=site)

    def _auto_create_ctx(self, auto_create: bool = False):
        """Flip the real AppConfig's auto-create flag (existence-checked by patch.object)."""
        from django.apps import apps

        cfg = apps.get_app_config("netbox_nso_plugin")
        return patch.object(cfg, "_interface_ip_auto_create", auto_create)

    def _setup_p2p_pair(self, addr_a="10.77.0.0/31", addr_b="10.77.0.1/31"):
        from netbox_nso_plugin.models import NSOInterfaceIPState

        iface_a = Interface.objects.create(device=self.device_a, name="Gi30/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi30/0/0", type="1000base-t")

        ip_a = IPAddress.objects.create(address=addr_a, status="reserved")
        ip_a.assigned_object = iface_a
        ip_a.save()

        ip_b = IPAddress.objects.create(address=addr_b, status="reserved")
        ip_b.assigned_object = iface_b
        ip_b.save()

        state_a = NSOInterfaceIPState.objects.create(
            interface=iface_a,
            address=addr_a,
            family="ipv4",
            status="accepted",
            auto_assigned=True,
        )
        state_b = NSOInterfaceIPState.objects.create(
            interface=iface_b,
            address=addr_b,
            family="ipv4",
            status="accepted",
            auto_assigned=True,
        )
        state_a.peer_state = state_b
        state_a.save(update_fields=["peer_state"])
        state_b.peer_state = state_a
        state_b.save(update_fields=["peer_state"])

        return iface_a, iface_b, ip_a, ip_b, state_a, state_b

    def test_first_end_in_sync_does_not_activate_ip(self):
        """When only end A reaches in_sync, IP stays reserved (peer not yet in_sync)."""
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        iface_a, iface_b, ip_a, ip_b, state_a, state_b = self._setup_p2p_pair("10.77.2.0/31", "10.77.2.1/31")

        payload = {
            "interfaces": [
                {
                    "interface": "Gi30/0/0",
                    "addresses": [{"address": "10.77.2.0/31", "vrf": "", "family": "ipv4", "secondary": False}],
                }
            ]
        }

        with self._auto_create_ctx(False):
            _reconcile_interface_ips(self.device_a, payload)

        state_a.refresh_from_db()
        self.assertEqual(state_a.status, "in_sync")

        ip_a.refresh_from_db()
        self.assertEqual(ip_a.status, "reserved", "First end should stay reserved until peer also in_sync")

    def test_second_end_in_sync_activates_both_ips(self):
        """When end B reconciles in_sync and peer A is already in_sync, both IPs become active."""
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        iface_a, iface_b, ip_a, ip_b, state_a, state_b = self._setup_p2p_pair("10.77.4.0/31", "10.77.4.1/31")

        # Simulate end A already in_sync (reconciler already ran for device A)
        state_a.status = "in_sync"
        state_a.save(update_fields=["status"])

        payload = {
            "interfaces": [
                {
                    "interface": "Gi30/0/0",
                    "addresses": [{"address": "10.77.4.1/31", "vrf": "", "family": "ipv4", "secondary": False}],
                }
            ]
        }

        with self._auto_create_ctx(False):
            _reconcile_interface_ips(self.device_b, payload)

        state_b.refresh_from_db()
        self.assertEqual(state_b.status, "in_sync")

        ip_a.refresh_from_db()
        ip_b.refresh_from_db()
        self.assertEqual(ip_a.status, "active", "Peer IP (end A) must also be activated")
        self.assertEqual(ip_b.status, "active", "End B IP must be activated")


class TestRollbackContentTypeScoping(TestCase):
    """rollback_auto_assigned: the IPAddress lookup is scoped by content type."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="CtMfg", slug="ctmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="CtDev", slug="ctdev")
        role = DeviceRole.objects.create(name="CtRole", slug="ctrole")
        site = Site.objects.create(name="CtSite", slug="ctsite")
        cls.device = Device.objects.create(name="ct-router", device_type=device_type, role=role, site=site)

    def test_rollback_does_not_delete_ip_of_a_different_content_type(self):
        """A same-address/VRF IP assigned to a NON-Interface object whose pk collides
        with the interface pk must survive rollback (GenericForeignKey id collision)."""
        from django.contrib.contenttypes.models import ContentType

        from netbox_nso_plugin.ip_autoassign import rollback_auto_assigned
        from netbox_nso_plugin.models import NSOInterfaceIPState

        iface = Interface.objects.create(device=self.device, name="Loopback250", type="virtual")
        # Decoy: same address, assigned to a Device (not our Interface) at the SAME pk.
        decoy = IPAddress.objects.create(address="10.210.0.1/32", status="active")
        decoy.assigned_object_type = ContentType.objects.get_for_model(Device)
        decoy.assigned_object_id = iface.pk
        decoy.save()

        state = NSOInterfaceIPState.objects.create(
            interface=iface,
            address="10.210.0.1/32",
            family="ipv4",
            status="accepted",
            auto_assigned=True,
        )
        rollback_auto_assigned(state)

        self.assertTrue(
            IPAddress.objects.filter(pk=decoy.pk).exists(),
            "rollback deleted an IPAddress belonging to a different content type (id collision)",
        )


class TestP2PAllocationFailureCleanup(TestCase):
    """_assign_one_p2p_family: partial-failure cleanup leaves no orphan rows on the peer end."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="P2FailMfg", slug="p2failmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="P2FailDev", slug="p2faildev")
        role = DeviceRole.objects.create(name="P2FailRole", slug="p2failrole")
        site = Site.objects.create(name="P2FailSite", slug="p2failsite")
        cls.device_a = Device.objects.create(name="p2fail-a", device_type=dt, role=role, site=site)
        cls.device_b = Device.objects.create(name="p2fail-b", device_type=dt, role=role, site=site)
        cls.p2p_role = Role.objects.create(name="P2P Fail Core", slug="p2p-core")
        cls.pool = Prefix.objects.create(prefix="10.98.0.0/24", role=cls.p2p_role)

    def _make_mgmt(self, device, name):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="test-nso-fail", defaults={"adapter_instance_id": "test-nso-fail"}
        )
        return NSODeviceManagement.objects.create(
            device=device, nso_instance=inst, nso_device_name=name, adapter_device_id=device.pk
        )

    def _p2p_pair(self):
        from extras.models import Tag

        self._make_mgmt(self.device_a, "p2fail-dev-a")
        self._make_mgmt(self.device_b, "p2fail-dev-b")
        iface_a = Interface.objects.create(device=self.device_a, name="Gi40/0/0", type="1000base-t")
        iface_b = Interface.objects.create(device=self.device_b, name="Gi40/0/0", type="1000base-t")
        _make_cable_pair(iface_a, iface_b)
        iface_a = Interface.objects.get(pk=iface_a.pk)
        tag, _ = Tag.objects.get_or_create(name="p2p-core-fail", defaults={"slug": "p2p-core"})
        iface_a.tags.add(tag)
        return iface_a, iface_b

    def test_state_link_failure_leaves_no_orphan_peer_state(self):
        """If linking peer_state raises after both state rows exist, neither survives.

        Uses a VRF-scoped pool: ``state_b.vrf`` is then the pool VRF while the
        reserved ``ip_b`` carries no VRF, so deleting ip_b in the cleanup does NOT
        cascade to state_b via ``_on_ip_address_delete`` — the state cleanup must
        remove state_b explicitly. This is exactly the orphan the fix closes.
        """
        from ipam.models import VRF

        from netbox_nso_plugin.ip_autoassign import _assign_one_p2p_family
        from netbox_nso_plugin.models import NSOInterfaceIPState

        vrf = VRF.objects.create(name="P2FAIL-RED")
        vrf_pool = Prefix.objects.create(prefix="10.96.0.0/24", role=self.p2p_role, vrf=vrf)

        iface_a, iface_b = self._p2p_pair()
        mgmt_a = iface_a.device.nso_management
        mgmt_b = iface_b.device.nso_management

        real_save = NSOInterfaceIPState.save

        def boom_on_peer_link(self, *args, **kwargs):
            if kwargs.get("update_fields") == ["peer_state"]:
                raise RuntimeError("simulated failure while linking peer_state")
            return real_save(self, *args, **kwargs)

        result = {"allocated": [], "skipped": [], "errors": []}
        with (
            patch.object(NSOInterfaceIPState, "save", boom_on_peer_link),
            patch("netbox_nso_plugin.signals._push_ip_intent_for_device"),
        ):
            _assign_one_p2p_family(
                iface_a,
                iface_b,
                mgmt_a,
                mgmt_b,
                "ipv4",
                iface_a.device.site,
                result,
                pool_finder=lambda fam, s: vrf_pool,
                no_pool_reason=lambda fam: "no pool",
                push=False,
            )

        self.assertTrue(result["errors"], "the linking failure should be reported as an error")
        self.assertEqual(
            NSOInterfaceIPState.objects.filter(interface=iface_b).count(),
            0,
            "a mid-link failure must not leave an orphan NSOInterfaceIPState (state_b) on the peer end",
        )

    def test_reserve_failure_leaves_no_orphan_peer_ip(self):
        """If the peer IPAddress post-save signal raises after INSERT, ip_b is cleaned up."""
        from netbox_nso_plugin.ip_autoassign import auto_assign_ip
        from netbox_nso_plugin.models import NSOInterfaceIPState

        iface_a, iface_b = self._p2p_pair()

        real_goc = NSOInterfaceIPState.objects.get_or_create

        def boom_on_peer(*args, **kwargs):
            if kwargs.get("interface") == iface_b:
                raise RuntimeError("simulated post-save signal failure on the peer IP")
            return real_goc(*args, **kwargs)

        with (
            patch.object(NSOInterfaceIPState.objects, "get_or_create", boom_on_peer),
            patch("netbox_nso_plugin.signals._push_ip_intent_for_device"),
        ):
            result = auto_assign_ip(iface_a, families=("ipv4",))

        self.assertTrue(result["errors"], "the reserve failure should be reported as an error")
        self.assertEqual(
            IPAddress.objects.filter(status="reserved").count(),
            0,
            "a post-INSERT reserve failure must not leave an orphan reserved IPAddress (ip_b)",
        )
