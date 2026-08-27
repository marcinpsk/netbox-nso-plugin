# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for A4: adapter_client.get_interface_ips and _reconcile_interface_ips."""

import unittest
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from ._adapter_http import make_session
from ._outbox_case import content_bulk_update
from .mixins import IntentPushResetMixin

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


# ---------------------------------------------------------------------------
# adapter_client.get_interface_ips — unit tests (no Django DB)
# ---------------------------------------------------------------------------


class TestGetInterfaceIps(unittest.TestCase):
    """Tests for adapter_client.get_interface_ips()."""

    def _make_session(self, status=200, json_data=None):
        return make_session(status_code=status, json_data=json_data)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_calls_expected_endpoint(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_interface_ips

        session = self._make_session(json_data={"interfaces": []})
        mock_session_cls.return_value = session

        get_interface_ips(7)

        args, _ = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://adapter.local/api/v1/devices/7/interface-ips")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_returns_response_unchanged(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_interface_ips

        expected = {
            "device_id": 7,
            "last_refreshed_at": "2026-06-01T00:00:00Z",
            "refresh_source": "poll",
            "interfaces": [
                {
                    "interface": "GigabitEthernet0/0",
                    "addresses": [{"address": "10.0.0.1/24", "vrf": "", "family": "ipv4", "secondary": False}],
                }
            ],
        }
        session = self._make_session(json_data=expected)
        mock_session_cls.return_value = session

        result = get_interface_ips(7)
        self.assertEqual(result, expected)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_http_error_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, get_interface_ips

        session = self._make_session(status=404, json_data={"error": {"code": "not_found", "message": "no device"}})
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError):
            get_interface_ips(99)


# ---------------------------------------------------------------------------
# _reconcile_interface_ips — Django DB integration tests
# ---------------------------------------------------------------------------


class TestReconcileInterfaceIps(TestCase):
    """Django-DB tests for _reconcile_interface_ips in template_content.py."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="IpMfg", slug="ipmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="IpDevice", slug="ipdevice")
        role = DeviceRole.objects.create(name="IpRole", slug="iprole")
        site = Site.objects.create(name="IpSite", slug="ipsite")
        cls.device = Device.objects.create(name="ip-router", device_type=device_type, role=role, site=site)
        cls.iface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")
        cls.iface2 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")

    def _auto_create_ctx(self, auto_create: bool):
        """Flip the real AppConfig's auto-create flag (the attribute production reads).

        patch.object targets the live AppConfig singleton and existence-checks the
        attribute, so a rename of `_interface_ip_auto_create` fails the test loudly —
        unlike a MagicMock config, which would silently fabricate any attribute name.
        """
        from django.apps import apps

        cfg = apps.get_app_config("netbox_nso_plugin")
        return patch.object(cfg, "_interface_ip_auto_create", auto_create)

    def _make_payload(self, iface_name, addresses):
        return {
            "device_id": self.device.pk,
            "refresh_source": "poll",
            "last_refreshed_at": "2026-06-01T00:00:00Z",
            "interfaces": [
                {
                    "interface": iface_name,
                    "addresses": addresses,
                }
            ],
        }

    def test_empty_payload_returns_empty_list(self):
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        result = _reconcile_interface_ips(self.device, {"interfaces": []})
        self.assertEqual(result, [])

    def test_new_address_lands_as_imported_when_auto_create_off(self):
        """Without auto_create, a new address lands as 'imported'."""
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [
                {"address": "192.168.1.1/24", "vrf": "", "family": "ipv4", "secondary": False},
            ],
        )

        with self._auto_create_ctx(False):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertIn("192.168.1.1/24", states)
        self.assertEqual(states["192.168.1.1/24"].status, "imported")

    def test_auto_create_creates_ipaddress_and_sets_in_sync(self):
        """With auto_create=True, a new address is created in IPAM and state=in_sync."""
        from ipam.models import IPAddress

        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [
                {"address": "10.10.0.1/30", "vrf": "", "family": "ipv4", "secondary": False},
            ],
        )

        with self._auto_create_ctx(True):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertIn("10.10.0.1/30", states)
        self.assertEqual(states["10.10.0.1/30"].status, "imported")  # unowned materialized → imported (unified)
        ip_exists = IPAddress.objects.filter(address="10.10.0.1/30").exists()
        self.assertTrue(ip_exists)

    def test_existing_ip_on_correct_interface_is_in_sync(self):
        """Address already in IPAM assigned to this interface → in_sync."""
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        ct = ContentType.objects.get_for_model(Interface)
        IPAddress.objects.create(
            address="172.16.0.1/24",
            assigned_object_type=ct,
            assigned_object_id=self.iface.pk,
        )

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [
                {"address": "172.16.0.1/24", "vrf": "", "family": "ipv4", "secondary": False},
            ],
        )

        with self._auto_create_ctx(False):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertEqual(states["172.16.0.1/24"].status, "imported")  # unowned materialized → imported (unified)

    def test_existing_ip_on_different_interface_is_conflict(self):
        """Address in IPAM assigned to a DIFFERENT interface → conflict, no reassignment."""
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        other_device_mfg = Manufacturer.objects.get_or_create(name="OtherMfg", slug="othermfg")[0]
        other_dt = DeviceType.objects.get_or_create(manufacturer=other_device_mfg, model="OtherDev", slug="otherdev")[0]
        other_role = DeviceRole.objects.get_or_create(name="OtherRole", slug="otherrole")[0]
        other_site = Site.objects.get_or_create(name="OtherSite", slug="othersite")[0]
        other_device = Device.objects.create(
            name="other-router", device_type=other_dt, role=other_role, site=other_site
        )
        other_iface = Interface.objects.create(device=other_device, name="Gig0/0", type="1000base-t")

        ip = IPAddress.objects.create(
            address="10.99.0.1/24",
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=other_iface.pk,
        )

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [
                {"address": "10.99.0.1/24", "vrf": "", "family": "ipv4", "secondary": False},
            ],
        )

        with self._auto_create_ctx(True):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertEqual(states["10.99.0.1/24"].status, "conflict")
        # Must not have been reassigned
        ip.refresh_from_db()
        self.assertEqual(ip.assigned_object, other_iface)

    def test_existing_unassigned_ip_is_adopted_not_conflict(self):
        """An IPAM address that exists but is UNASSIGNED is not 'assigned elsewhere':
        with auto_create the reconciler adopts it (assigns to the reporting interface)
        instead of flagging a false conflict. Live case: arcos dev-23 loopback IPs
        pre-existed unassigned in IPAM (harvested months earlier) and every sync
        flagged them 'conflict' although nothing owned them.
        """
        from ipam.models import IPAddress

        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        ip = IPAddress.objects.create(address="10.77.0.1/32")

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [{"address": "10.77.0.1/32", "vrf": "", "family": "ipv4", "secondary": False}],
        )
        with self._auto_create_ctx(True):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertEqual(states["10.77.0.1/32"].status, "imported")
        ip.refresh_from_db()
        self.assertEqual(ip.assigned_object, self.iface)

    def test_existing_unassigned_ip_without_auto_create_imported_untouched(self):
        """auto_create off: an unassigned IPAM match still isn't a conflict — the row
        lands 'imported' and record-only mode leaves the IPAddress untouched.
        """
        from ipam.models import IPAddress

        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        ip = IPAddress.objects.create(address="10.77.0.2/32")

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [{"address": "10.77.0.2/32", "vrf": "", "family": "ipv4", "secondary": False}],
        )
        with self._auto_create_ctx(False):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertEqual(states["10.77.0.2/32"].status, "imported")
        ip.refresh_from_db()
        self.assertIsNone(ip.assigned_object)

    def test_stuck_conflict_row_self_heals_when_ip_is_unassigned(self):
        """A row a previous (buggy) run left at 'conflict' recovers on the next
        reconcile when the matching IP is unassigned: the machine's
        conflict --reconcile--> imported edge ('adoption ambiguity resolved').
        """
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        ip = IPAddress.objects.create(address="10.77.0.3/32")
        NSOInterfaceIPState.objects.create(
            interface=self.iface,
            address="10.77.0.3/32",
            vrf="",
            family="ipv4",
            status="conflict",
            nso_value="10.77.0.3/32",
        )

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [{"address": "10.77.0.3/32", "vrf": "", "family": "ipv4", "secondary": False}],
        )
        with self._auto_create_ctx(True):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertEqual(states["10.77.0.3/32"].status, "imported")
        ip.refresh_from_db()
        self.assertEqual(ip.assigned_object, self.iface)

    def test_removed_address_becomes_changed_and_unassigned(self):
        """Address in DB but not in new payload → status=changed, IPAddress unassigned."""
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        ip = IPAddress.objects.create(
            address="10.50.0.1/24",
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=self.iface2.pk,
        )

        NSOInterfaceIPState.objects.create(
            interface=self.iface2,
            address="10.50.0.1/24",
            vrf="",
            status="in_sync",
        )

        # Payload no longer contains this address
        empty_payload = {"interfaces": []}

        with self._auto_create_ctx(False):
            _reconcile_interface_ips(self.device, empty_payload)

        state = NSOInterfaceIPState.objects.get(interface=self.iface2, address="10.50.0.1/24")
        self.assertEqual(state.status, "changed")

        ip.refresh_from_db()
        self.assertIsNone(ip.assigned_object)

    def test_vrf_rekey_replaces_row_not_phantom_drift(self):
        """Same IP later reported under a different VRF → re-key (one row), not duplicate drift.

        Regression: an IP imported/accepted with no VRF (VRF not captured yet) must
        not be left as a phantom 'changed' row when the VRF capture is later
        corrected (e.g. "" → mgmtVrf); the corrected row is the single source.
        """
        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        empty_vrf = self._make_payload(
            "GigabitEthernet0/0", [{"address": "172.30.150.202/24", "vrf": "", "family": "ipv4", "secondary": False}]
        )
        mgmt_vrf = self._make_payload(
            "GigabitEthernet0/0",
            [{"address": "172.30.150.202/24", "vrf": "mgmtVrf", "family": "ipv4", "secondary": False}],
        )

        with self._auto_create_ctx(False):
            _reconcile_interface_ips(self.device, empty_vrf)
            # Operator had accepted the (no-VRF) row before the VRF was captured.
            content_bulk_update(
                NSOInterfaceIPState.objects.get(
                    interface=self.iface,
                    address="172.30.150.202/24",
                    vrf="",
                ),
                status="accepted",
            )
            _reconcile_interface_ips(self.device, mgmt_vrf)

        rows = NSOInterfaceIPState.objects.filter(interface=self.iface, address="172.30.150.202/24")
        self.assertEqual(rows.count(), 1)  # the stale "" row is gone, not a phantom 'changed'
        self.assertEqual(rows.first().vrf, "mgmtVrf")

    def test_idempotent_rerun_does_not_duplicate(self):
        """Running reconcile twice with the same payload produces exactly one state row."""
        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [
                {"address": "192.0.2.1/30", "vrf": "", "family": "ipv4", "secondary": False},
            ],
        )

        with self._auto_create_ctx(False):
            _reconcile_interface_ips(self.device, payload)
            _reconcile_interface_ips(self.device, payload)

        count = NSOInterfaceIPState.objects.filter(interface=self.iface, address="192.0.2.1/30").count()
        self.assertEqual(count, 1)

    def test_secondary_flag_persisted(self):
        """secondary=True from payload is preserved on the state row."""
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        payload = self._make_payload(
            "GigabitEthernet0/0",
            [
                {"address": "10.1.1.2/24", "vrf": "", "family": "ipv4", "secondary": True},
            ],
        )

        with self._auto_create_ctx(False):
            result = _reconcile_interface_ips(self.device, payload)

        states = {s.address: s for s in result}
        self.assertTrue(states["10.1.1.2/24"].secondary)

    def test_bound_port_rerun_stays_in_sync_no_spurious_drift(self):
        """Nokia logical→physical: an IP bound via bound_port must not be retired as 'changed'.

        Regression: the state row is created against the physical port, but the
        payload key uses the logical router-interface name.  The retire pass must
        compare on interface_id (not name), or every rerun marks the IP 'changed'.
        """
        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        # Physical port exists in NetBox; the logical name does NOT.
        physical = Interface.objects.create(device=self.device, name="1/1/c11/1", type="other")
        payload = {
            "device_id": self.device.pk,
            "interfaces": [
                {
                    "interface": "router-interface-to-core",  # logical, absent from dcim
                    "bound_port": "1/1/c11/1",
                    "addresses": [
                        {"address": "10.20.0.1/31", "vrf": "", "family": "ipv4", "secondary": False},
                    ],
                }
            ],
        }

        with self._auto_create_ctx(True):
            _reconcile_interface_ips(self.device, payload)
            result = _reconcile_interface_ips(self.device, payload)  # second sync

        states = {s.address: s for s in result}
        self.assertEqual(states["10.20.0.1/31"].status, "imported")  # unowned materialized → imported (unified)
        self.assertEqual(states["10.20.0.1/31"].interface_id, physical.pk)
        # Exactly one row, and it is NOT 'changed'.
        self.assertEqual(NSOInterfaceIPState.objects.filter(interface=physical, address="10.20.0.1/31").count(), 1)

    def test_unknown_interface_name_skipped(self):
        """Addresses for an interface name that doesn't exist in NetBox are silently skipped."""
        from netbox_nso_plugin.models import NSOInterfaceIPState
        from netbox_nso_plugin.template_content import _reconcile_interface_ips

        payload = self._make_payload(
            "Loopback999",
            [
                {"address": "1.2.3.4/32", "vrf": "", "family": "ipv4", "secondary": False},
            ],
        )

        with self._auto_create_ctx(False):
            result = _reconcile_interface_ips(self.device, payload)

        self.assertEqual(result, [])
        self.assertFalse(NSOInterfaceIPState.objects.filter(address="1.2.3.4/32").exists())


class TestInterfaceIPReassignment(IntentPushResetMixin, TestCase):
    """Foreign native reassignments do not manufacture IP ownership evidence."""

    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        mfg = Manufacturer.objects.create(name="RaMfg", slug="ramfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RaDev", slug="radev")
        role = DeviceRole.objects.create(name="RaRole", slug="rarole")
        site = Site.objects.create(name="RaSite", slug="rasite")
        cls.device = Device.objects.create(name="ra-router", device_type=dt, role=role, site=site)
        cls.if_a = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")
        cls.if_b = Interface.objects.create(device=cls.device, name="GigabitEthernet0/2", type="1000base-t")
        nso = NSOInstance.objects.create(name="ra-nso", adapter_instance_id="ra-nso")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=nso, nso_device_name="ra-router", adapter_device_id=321
        )

    def test_foreign_reassign_keeps_overlay_unchanged(self):
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState

        ip = IPAddress.objects.create(address="10.44.0.1/24", assigned_object=self.if_a)
        NSOInterfaceIPState.objects.create(
            interface=self.if_a,
            address="10.44.0.1/24",
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_ip_intent") as mock_put:
            ip.assigned_object = self.if_b
            ip.save()

        self.assertTrue(NSOInterfaceIPState.objects.filter(interface=self.if_a, address="10.44.0.1/24").exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(interface=self.if_b, address="10.44.0.1/24").exists())
        mock_put.assert_not_called()


class TestAcceptInterfaceIPConflict(TestCase):
    """The accept view resolves an interface-IP conflict by adopting the device reality.

    Mirrors the live sw01 case: the OOB mgmt IP is reported by NSO on ``vme.0`` but
    NetBox has it on the ``me0`` onboarding stand-in → conflict. Accepting must
    reassign the IPAddress onto ``vme.0`` (no device push — the device already has it)
    and settle the state to in_sync/owned.
    """

    @classmethod
    def setUpTestData(cls):
        from core.models import ObjectType
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceIPState

        mfg = Manufacturer.objects.create(name="Acc Mfg", slug="accmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="AccDev", slug="accdev")
        role = DeviceRole.objects.create(name="AccRole", slug="accrole")
        site = Site.objects.create(name="AccSite", slug="accsite")
        cls.device = Device.objects.create(name="acc-sw01", device_type=dt, role=role, site=site)
        # me0 = onboarding mgmt stand-in (holds the IP); vme.0 = what the NED reports.
        cls.me0 = Interface.objects.create(device=cls.device, name="me0", type="virtual", mgmt_only=True)
        cls.vme0 = Interface.objects.create(device=cls.device, name="vme.0", type="virtual")
        cls.ip = IPAddress.objects.create(
            address="172.30.150.90/24",
            assigned_object_type=ContentType.objects.get_for_model(Interface),
            assigned_object_id=cls.me0.pk,
        )
        nso = NSOInstance.objects.create(name="acc-nso", adapter_instance_id="acc-nso-id")
        NSODeviceManagement.objects.create(device=cls.device, nso_instance=nso, nso_device_name="acc-sw01")
        cls.state = NSOInterfaceIPState.objects.create(
            interface=cls.vme0, address="172.30.150.90/24", vrf="", family="ipv4", status="conflict"
        )
        cls._OT = ObjectType

    def setUp(self):
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        from netbox_nso_plugin.models import NSODeviceManagement

        user = get_user_model().objects.create_user(username="acc-op", password="accpass12345")  # noqa: S106
        perm = ObjectPermission.objects.create(name="acc-change", actions=["change"])
        perm.object_types.add(self._OT.objects.get_for_model(NSODeviceManagement))
        perm.users.add(user)
        self.client.force_login(user)

    def test_accept_moves_ip_to_ned_interface_and_settles_in_sync(self):
        from django.urls import reverse

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision

        url = reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_accept", kwargs={"pk": self.state.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        self.ip.refresh_from_db()
        self.assertEqual(self.ip.assigned_object, self.vme0)  # moved me0 -> vme.0
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, "in_sync")
        self.assertIsNotNone(self.state.accepted_at)
        revision = NSOIntentRevision.objects.get(device=self.device, scope="ip")
        self.assertEqual(revision.verified_revision, revision.revision)
        self.assertEqual(
            revision.verified_fingerprint,
            delivery.canonical_fingerprint(delivery.render("ip", self.device.pk, None).payload),
        )


class TestInterfaceIPInlineEdit(IntentPushResetMixin, TestCase):
    """The NSO-grid IP editor changes native IPAM objects without bypassing safety.

    These are real request -> view -> ORM tests. The management rows intentionally have
    no adapter id, keeping the external adapter boundary out of the test while exercising
    the same native IPAddress and overlay writes used in production.
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Cable, CableTermination
        from django.contrib.auth import get_user_model
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceIPState

        cls.user = get_user_model().objects.create_superuser(
            username="ip-inline-admin",
            password="testpass789",  # noqa: S106
            email="ip-inline@test.example",
        )
        mfg = Manufacturer.objects.create(name="IP Inline Mfg", slug="ip-inline-mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IP Inline", slug="ip-inline")
        role = DeviceRole.objects.create(name="IP Inline Role", slug="ip-inline-role")
        site = Site.objects.create(name="IP Inline Site", slug="ip-inline-site")
        cls.device_a = Device.objects.create(name="ip-inline-a", device_type=dt, role=role, site=site)
        cls.device_b = Device.objects.create(name="ip-inline-b", device_type=dt, role=role, site=site)
        cls.local = Interface.objects.create(device=cls.device_a, name="Gi0/1", type="1000base-t")
        cls.peer = Interface.objects.create(device=cls.device_b, name="Gi0/2", type="1000base-t")
        cls.other = Interface.objects.create(device=cls.device_a, name="Gi0/3", type="1000base-t")
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=cls.local)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=cls.peer)

        nso = NSOInstance.objects.create(name="ip-inline-nso", adapter_instance_id="ip-inline-nso")
        NSODeviceManagement.objects.create(device=cls.device_a, nso_instance=nso, nso_device_name=cls.device_a.name)
        NSODeviceManagement.objects.create(device=cls.device_b, nso_instance=nso, nso_device_name=cls.device_b.name)

        cls.local_ip = IPAddress.objects.create(address="198.18.20.0/31", assigned_object=cls.local)
        cls.peer_ip = IPAddress.objects.create(address="198.18.20.1/31", assigned_object=cls.peer)
        cls.used_ip = IPAddress.objects.create(address="198.18.20.10/32", assigned_object=cls.other)
        cls.local_state = NSOInterfaceIPState.objects.create(
            interface=cls.local,
            address="198.18.20.0/31",
            family="ipv4",
            status="imported",
        )
        cls.peer_state = NSOInterfaceIPState.objects.create(
            interface=cls.peer,
            address="198.18.20.1/31",
            family="ipv4",
            status="imported",
        )

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _url(self, state=None):
        from django.urls import reverse

        return reverse(
            "plugins:netbox_nso_plugin:nsointerfaceipstate_edit",
            kwargs={"pk": (state or self.local_state).pk},
        )

    def test_edit_rekeys_native_ip_and_overlay(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision

        response = self.client.post(
            self._url(),
            {"address": "198.18.20.2/31"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.local_ip.refresh_from_db()
        self.local_state.refresh_from_db()
        self.assertEqual(str(self.local_ip.address), "198.18.20.2/31")
        self.assertEqual(self.local_ip.assigned_object, self.local)
        self.assertEqual(self.local_state.address, "198.18.20.2/31")
        self.assertEqual(self.local_state.status, "accepted")
        revision = NSOIntentRevision.objects.get(device=self.device_a, scope="ip")
        self.assertEqual(revision.verified_revision, revision.revision)
        self.assertEqual(
            revision.verified_fingerprint,
            delivery.canonical_fingerprint(delivery.render("ip", self.device_a.pk, None).payload),
        )

    def test_unchanged_prefilled_peer_is_not_modified(self):
        """The real two-field popover always submits the displayed peer value.

        Leaving that field untouched must not silently take ownership of the far end.
        """
        response = self.client.post(
            self._url(),
            {
                "address": "198.18.20.2/31",
                "peer_address": "198.18.20.1/31",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Updated 1 interface address", response.json()["message"])
        self.peer_ip.refresh_from_db()
        self.peer_state.refresh_from_db()
        self.assertEqual(str(self.peer_ip.address), "198.18.20.1/31")
        self.assertEqual(self.peer_state.status, "imported")

    def test_invalid_address_returns_field_error_without_writing(self):
        response = self.client.post(
            self._url(),
            {"address": "not-an-address"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("valid IPv4 or IPv6", " ".join(response.json()["errors"]["address"]))
        self.local_ip.refresh_from_db()
        self.local_state.refresh_from_db()
        self.assertEqual(str(self.local_ip.address), "198.18.20.0/31")
        self.assertEqual(self.local_state.address, "198.18.20.0/31")

    def test_overlay_only_edit_materializes_native_ip(self):
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState

        iface = Interface.objects.create(device=self.device_a, name="Gi0/4", type="1000base-t")
        state = NSOInterfaceIPState.objects.create(
            interface=iface,
            address="198.18.20.20/31",
            family="ipv4",
            status="imported",
        )

        response = self.client.post(
            self._url(state),
            {"address": "198.18.20.22/31"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        native = IPAddress.objects.get(address="198.18.20.22/31")
        self.assertEqual(native.assigned_object, iface)
        state.refresh_from_db()
        self.assertEqual(state.address, "198.18.20.22/31")
        self.assertEqual(state.status, "accepted")

    def test_rejects_host_already_used_even_with_different_mask(self):
        response = self.client.post(
            self._url(),
            {"address": "198.18.20.10/31"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", " ".join(response.json()["errors"]["address"]))
        self.local_ip.refresh_from_db()
        self.local_state.refresh_from_db()
        self.assertEqual(str(self.local_ip.address), "198.18.20.0/31")
        self.assertEqual(self.local_state.address, "198.18.20.0/31")

    def test_can_edit_both_cable_ends_atomically(self):
        response = self.client.post(
            self._url(),
            {
                "address": "198.18.20.4/31",
                "peer_address": "198.18.20.5/31",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.local_ip.refresh_from_db()
        self.peer_ip.refresh_from_db()
        self.local_state.refresh_from_db()
        self.peer_state.refresh_from_db()
        self.assertEqual(str(self.local_ip.address), "198.18.20.4/31")
        self.assertEqual(str(self.peer_ip.address), "198.18.20.5/31")
        self.assertEqual(self.local_state.address, "198.18.20.4/31")
        self.assertEqual(self.peer_state.address, "198.18.20.5/31")

    def test_peer_collision_rolls_back_local_change(self):
        response = self.client.post(
            self._url(),
            {
                "address": "198.18.20.6/31",
                "peer_address": "198.18.20.10/31",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("peer_address", response.json()["errors"])
        self.local_ip.refresh_from_db()
        self.peer_ip.refresh_from_db()
        self.assertEqual(str(self.local_ip.address), "198.18.20.0/31")
        self.assertEqual(str(self.peer_ip.address), "198.18.20.1/31")
