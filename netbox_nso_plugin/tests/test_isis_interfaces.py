# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for M14 A4: adapter_client.get_isis_interfaces and _reconcile_isis_interfaces."""

import unittest
from unittest.mock import MagicMock, patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


# ---------------------------------------------------------------------------
# adapter_client.get_isis_interfaces — unit tests (no Django DB)
# ---------------------------------------------------------------------------


class TestGetIsisInterfaces(unittest.TestCase):
    """Tests for adapter_client.get_isis_interfaces()."""

    def _make_session(self, status=200, json_data=None):
        response = MagicMock()
        response.ok = status < 400
        response.status_code = status
        response.content = b"{}"
        response.text = ""
        response.json.return_value = json_data or {}
        session = MagicMock()
        session.request.return_value = response
        return session

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_calls_expected_endpoint(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_isis_interfaces

        session = self._make_session(json_data={"interfaces": []})
        mock_session_cls.return_value = session

        get_isis_interfaces(99)

        args, _ = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://adapter.local/api/v1/devices/99/isis-interfaces")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_returns_interfaces_list(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_isis_interfaces

        ifaces = [
            {
                "interface_name": "GigabitEthernet0/0",
                "af": "ipv4",
                "process_tag": "",
                "circuit_type": "level-1-2",
                "network_type": "point-to-point",
                "metric": 10,
                "passive": False,
            }
        ]
        session = self._make_session(json_data={"interfaces": ifaces, "processes": []})
        mock_session_cls.return_value = session

        result = get_isis_interfaces(99)
        self.assertEqual(result, {"processes": [], "interfaces": ifaces})

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_404_returns_empty_list(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_isis_interfaces

        session = self._make_session(status=404, json_data={})
        mock_session_cls.return_value = session

        result = get_isis_interfaces(99)
        self.assertEqual(result, {"processes": [], "interfaces": []})

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_500_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, get_isis_interfaces

        session = self._make_session(
            status=500,
            json_data={"error": {"code": "internal_error", "message": "boom"}},
        )
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError):
            get_isis_interfaces(99)


# ---------------------------------------------------------------------------
# _reconcile_isis_interfaces — integration tests (real Django DB)
# ---------------------------------------------------------------------------


class TestReconcileIsisInterfaces(TestCase):
    """Integration tests for _reconcile_isis_interfaces()."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="IsisMfg", slug="isissmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="IsisDevice", slug="isisdevice")
        role = DeviceRole.objects.create(name="IsisRole", slug="isisrole")
        site = Site.objects.create(name="IsisSite", slug="isissite")
        cls.device = Device.objects.create(name="isis-router", device_type=device_type, role=role, site=site)
        cls.iface_ge0 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")
        cls.iface_ge1 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")

    def _make_mgmt(self, device=None):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = device or self.device
        inst, _ = NSOInstance.objects.get_or_create(
            name="isis-test-inst",
            defaults={"adapter_instance_id": "isis-test-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "isis-dev",
                "adapter_device_id": device.pk,
            },
        )[0]

    def _entry(self, iface_name="GigabitEthernet0/0", af="ipv4", **kwargs):
        base = {
            "interface_name": iface_name,
            "af": af,
            "process_tag": "",
            "circuit_type": "level-1-2",
            "network_type": "",
            "metric": None,
            "passive": False,
        }
        base.update(kwargs)
        return base

    def _payload(self, *entries):
        """Return entries as a flat list (matches get_isis_interfaces() return shape)."""
        return list(entries)

    # ── Basic cases ────────────────────────────────────────────────────────────

    def test_no_mgmt_returns_empty(self):
        """Device without NSODeviceManagement → empty list, no crash."""
        mfg = Manufacturer.objects.get_or_create(name="NoIsisMfg", slug="noisissfg")[0]
        dt = DeviceType.objects.get_or_create(manufacturer=mfg, model="NoIsisDevice", slug="noisisdevice")[0]
        role = DeviceRole.objects.get_or_create(name="NoIsisRole", slug="noisisrole")[0]
        site = Site.objects.get_or_create(name="NoIsisSite", slug="noisissite")[0]
        orphan = Device.objects.create(name="orphan-isis", device_type=dt, role=role, site=site)

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(orphan, self._payload())
        self.assertEqual(result, [])

    def test_empty_payload_returns_empty(self):
        """Empty payload → no state rows created."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(self.device, self._payload())
        self.assertEqual(result, [])

    def test_single_entry_creates_state_and_routing_interface(self):
        """New IS-IS entry → NSOISISInterfaceState linked to a netbox_routing.ISISInterface.

        With netbox-routing installed the reconcile now creates the real ISISInterface
        (under its ISISInstance) and links it, so the state is in_sync (linked), not the
        old always-"imported".
        """
        mgmt = self._make_mgmt()
        from netbox_routing.models import ISISInterface

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry(circuit_type="level-2-only")))

        self.assertEqual(len(result), 1)
        state = result[0]
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(state.af, "ipv4")
        self.assertEqual(state.interface, self.iface_ge0)
        self.assertEqual(state.management, mgmt)
        self.assertEqual(state.circuit_type, "level-2-only")
        self.assertFalse(state.passive)

        # The real routing object was created, linked, and carries the value.
        self.assertIsNotNone(state.isis_interface)
        ri = ISISInterface.objects.get(interface=self.iface_ge0, address_family="ipv4")
        self.assertEqual(state.isis_interface_id, ri.pk)
        self.assertEqual(ri.circuit_type, "level-2-only")
        self.assertEqual(ri.instance.device, self.device)

    def test_hello_auth_recorded_on_state(self):
        """hello_auth_type / hello_auth_present flow from the adapter payload onto the
        NSOISISInterfaceState overlay (the netbox_routing write is guarded separately)."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(
            self.device,
            self._payload(self._entry(hello_auth_type="md5", hello_auth_present=True)),
        )
        self.assertEqual(len(result), 1)
        state = result[0]
        self.assertEqual(state.hello_auth_type, "md5")
        self.assertTrue(state.hello_auth_present)
        # When the netbox-routing field exists (isis branch integrated), the
        # reconcile writes it through to the ISISInterface too.
        ri = state.isis_interface
        if ri is not None and hasattr(ri, "hello_auth_type"):
            ri.refresh_from_db()
            self.assertEqual(ri.hello_auth_type, "md5")

    def test_no_hello_auth_defaults_blank(self):
        """An entry without hello-auth leaves the state blank/false."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        state = _reconcile_isis_interfaces(self.device, self._payload(self._entry()))[0]
        self.assertEqual(state.hello_auth_type, "")
        self.assertFalse(state.hello_auth_present)

    def test_idempotent_second_call(self):
        """Calling reconcile twice with same payload produces same single row."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        _reconcile_isis_interfaces(self.device, self._payload(self._entry()))
        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry()))
        self.assertEqual(len(result), 1)

    def test_unknown_interface_skipped(self):
        """Interface name not in NetBox → silently skipped."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry(iface_name="Ethernet99/99")))
        self.assertEqual(result, [])

    def test_nokia_bound_port_correlates_logical_interface(self):
        """Nokia logical IS-IS name (LAG99:10) doesn't match a dcim.Interface, but its
        bound_port (lag-99:10) does → correlate through bound_port."""
        self._make_mgmt()
        port = Interface.objects.create(device=self.device, name="lag-99:10", type="lag")
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(
            self.device,
            self._payload(self._entry(iface_name="LAG99:10", bound_port="lag-99:10")),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].interface_id, port.pk)
        self.assertEqual(result[0].status, "in_sync")

    def test_nokia_bound_port_unmatched_is_dropped(self):
        """A logical name with a bound_port that still matches no dcim.Interface is dropped."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(
            self.device,
            self._payload(self._entry(iface_name="LAG99:99", bound_port="lag-99:99")),
        )
        self.assertEqual(result, [])

    def test_dual_stack_creates_two_rows(self):
        """IPv4 and IPv6 on same interface → two state rows with same interface FK."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(
            self.device,
            self._payload(self._entry(af="ipv4"), self._entry(af="ipv6")),
        )
        self.assertEqual(len(result), 2)
        afs = {r.af for r in result}
        self.assertEqual(afs, {"ipv4", "ipv6"})
        # Both point to the same dcim.Interface
        ifaces = {r.interface_id for r in result}
        self.assertEqual(len(ifaces), 1)

    def test_stale_row_set_to_changed(self):
        """Row present in DB but absent from payload → status=changed."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        # First call: populate two interfaces
        _reconcile_isis_interfaces(
            self.device,
            self._payload(self._entry("GigabitEthernet0/0"), self._entry("GigabitEthernet0/1")),
        )

        # Second call: only one interface in payload
        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry("GigabitEthernet0/0")))

        self.assertEqual(len(result), 2)
        statuses = {r.interface.name: r.status for r in result}
        self.assertEqual(statuses["GigabitEthernet0/0"], "in_sync")
        self.assertEqual(statuses["GigabitEthernet0/1"], "changed")

    def test_write_path_status_preserved(self):
        """Rows in accepted/deploying/in_sync are not overwritten back to imported."""
        mgmt = self._make_mgmt()
        from netbox_nso_plugin.models import NSOISISInterfaceState
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        # Pre-create a state row in 'accepted' status
        NSOISISInterfaceState.objects.create(
            management=mgmt,
            interface=self.iface_ge0,
            af="ipv4",
            status="accepted",
        )

        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry()))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "accepted")

    def test_passive_flag_stored(self):
        """Passive flag from payload is stored on the state row."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry(passive=True)))
        self.assertTrue(result[0].passive)

    def test_metric_stored(self):
        """Metric from payload is stored on the state row."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry(metric=100)))
        self.assertEqual(result[0].metric, 100)

    def test_missing_interface_name_skipped(self):
        """Entry with empty interface_name is silently skipped."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(self.device, self._payload({"interface_name": "", "af": "ipv4"}))
        self.assertEqual(result, [])

    def test_missing_af_skipped(self):
        """Entry with empty af is silently skipped."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(
            self.device, self._payload({"interface_name": "GigabitEthernet0/0", "af": ""})
        )
        self.assertEqual(result, [])


class TestReconcileIsisProcess(TestCase):
    """Tests for _reconcile_isis_process() — esp. Junos' empty default process tag."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="IsisProcMfg", slug="isisprocmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="IsisProcDev", slug="isisprocdev")
        role = DeviceRole.objects.create(name="IsisProcRole", slug="isisprocrole")
        site = Site.objects.create(name="IsisProcSite", slug="isisprocsite")
        cls.device = Device.objects.create(name="isis-proc-router", device_type=device_type, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="isis-proc-inst", defaults={"adapter_instance_id": "isis-proc-inst"}
        )
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "isis-proc-dev", "adapter_device_id": self.device.pk},
        )[0]

    def test_empty_process_tag_creates_row(self):
        """Junos' default IS-IS instance has process_tag='' — it must still be stored."""
        self._make_mgmt()
        from netbox_nso_plugin.models import NSOISISInstanceState
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        result = _reconcile_isis_process(
            self.device,
            [{"process_tag": "", "net": "", "is_type": "level-1-2"}],
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].process_tag, "")
        self.assertEqual(NSOISISInstanceState.objects.filter(management__device=self.device).count(), 1)

    def test_absent_process_tag_skipped(self):
        """An entry that genuinely omits process_tag (None) is skipped."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        result = _reconcile_isis_process(self.device, [{"net": "", "is_type": "level-2"}])
        self.assertEqual(result, [])

    def test_named_process_tag_creates_row(self):
        """A named process tag is stored as before."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        result = _reconcile_isis_process(self.device, [{"process_tag": "CORE", "net": "", "is_type": "level-2"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].process_tag, "CORE")

    def test_routing_instance_filled_with_all_fields(self):
        """The netbox_routing.ISISInstance is filled with every informational field
        NSO reports — not just net + is_type."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        result = _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "CORE",
                    "net": "49.0001.0001.0001.0001.00",
                    "is_type": "level-2-only",
                    "metric_style": "wide",
                    "overload_bit": True,
                    "area_auth_type": "md5",
                    "area_auth_present": True,
                    "domain_auth_type": "text",
                    "domain_auth_present": True,
                }
            ],
        )

        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0].isis_instance)
        inst = ISISInstance.objects.get(device=self.device, process_tag="CORE")
        self.assertEqual(inst.net, "49.0001.0001.0001.0001.00")
        self.assertEqual(inst.is_type, "level-2-only")
        self.assertEqual(inst.metric_style, "wide")
        self.assertTrue(inst.overload_bit)
        self.assertEqual(inst.area_auth_type, "md5")
        self.assertEqual(inst.domain_auth_type, "text")
        # No key in the payload (only the present flag) → key stays empty.
        self.assertEqual(inst.area_auth_key, "")
        self.assertEqual(inst.domain_auth_key, "")

    def test_auth_keys_imported_when_reported(self):
        """When NSO reports the actual area/domain auth keys they are filled into the
        overlay row and the netbox_routing.ISISInstance (routing-protocol secrets,
        not device-access credentials)."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        result = _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "CORE",
                    "net": "49.0001.0001.0001.0001.00",
                    "is_type": "level-2-only",
                    "area_auth_type": "md5",
                    "area_auth_present": True,
                    "area_auth_key": "s3cret-area",
                    "domain_auth_type": "text",
                    "domain_auth_present": True,
                    "domain_auth_key": "s3cret-domain",
                }
            ],
        )

        self.assertEqual(result[0].area_auth_key, "s3cret-area")
        self.assertEqual(result[0].domain_auth_key, "s3cret-domain")
        inst = ISISInstance.objects.get(device=self.device, process_tag="CORE")
        self.assertEqual(inst.area_auth_key, "s3cret-area")
        self.assertEqual(inst.domain_auth_key, "s3cret-domain")

    def test_routing_instance_overload_false_synced_none_left_alone(self):
        """overload_bit is tri-state: False is synced; None leaves the field untouched."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        _reconcile_isis_process(self.device, [{"process_tag": "OL", "overload_bit": False}])
        inst = ISISInstance.objects.get(device=self.device, process_tag="OL")
        self.assertEqual(inst.overload_bit, False)

        # A later report that omits overload_bit (None) must not clobber the stored value.
        inst.overload_bit = True
        inst.save(update_fields=["overload_bit"])
        _reconcile_isis_process(self.device, [{"process_tag": "OL"}])
        inst.refresh_from_db()
        self.assertTrue(inst.overload_bit)
