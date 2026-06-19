# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for A4: adapter_client.get_interface_ips and _reconcile_interface_ips."""

import unittest
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from ._adapter_http import make_session

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
            NSOInterfaceIPState.objects.filter(interface=self.iface, address="172.30.150.202/24", vrf="").update(
                status="accepted"
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
