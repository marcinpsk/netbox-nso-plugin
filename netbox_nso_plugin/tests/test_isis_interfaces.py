# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for A4: adapter_client.get_isis_interfaces and _reconcile_isis_interfaces."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from django.db import OperationalError
from django.test import TestCase

from ._adapter_http import make_session
from .mixins import IntentPushDeliveryMixin

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
        return make_session(status_code=status, json_data=json_data)

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
    def test_404_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        # READSEM S4 D4: 404 raises even without an ErrorEnvelope body (code "404").
        from netbox_nso_plugin.adapter_client import AdapterError, get_isis_interfaces

        session = self._make_session(status=404, json_data={})
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError):
            get_isis_interfaces(99)

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


class TestIsisChildFailurePolicy(unittest.TestCase):
    def test_interface_level_database_error_is_not_treated_as_a_match(self):
        from netbox_nso_plugin.template_content import _isis_interface_children_match

        with (
            patch("netbox_nso_plugin.template_content._reconcile_isis_settings", return_value=True),
            patch(
                "netbox_nso_plugin.template_content._reconcile_child_levels",
                side_effect=OperationalError("level query failed"),
            ),
        ):
            with self.assertRaisesRegex(OperationalError, "level query failed"):
                _isis_interface_children_match({}, SimpleNamespace(), write=False)


# ---------------------------------------------------------------------------
# _reconcile_isis_interfaces — integration tests (real Django DB)
# ---------------------------------------------------------------------------


class TestReconcileIsisInterfaces(IntentPushDeliveryMixin, TestCase):
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

    def test_greenfield_isis_interface_accept_owns_overlay(self):
        """Operator-created ISISInterface → owned overlay carries the pushed metric/network-type."""
        mgmt = self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOISISInterfaceState

        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        ISISInterface.objects.create(
            interface=self.iface_ge1,
            address_family="ipv4",
            instance=inst,
            metric=77,
            network_type="point-to-point",
            circuit_type="level-2-only",
        )
        state = NSOISISInterfaceState.objects.get(management=mgmt, interface=self.iface_ge1, af="ipv4")
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.metric, 77)
        self.assertEqual(state.network_type, "point-to-point")
        self.assertEqual(state.circuit_type, "level-2-only")

    def test_delete_isis_interface_drops_overlay_and_pushes_removal(self):
        """Deleting an ISISInterface drops its overlay + pushes reduced intent (parity with OSPF).

        Regression: with no pre_delete handler, deleting the ISISInterface only SET_NULLed
        NSOISISInterfaceState.isis_interface — the owned overlay lingered and no reduced IS-IS
        intent was pushed, so the device kept the config NetBox just removed."""
        from unittest.mock import patch

        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOISISInterfaceState

        mgmt = self._make_mgmt()
        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        with patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent"):
            with self.captureOnCommitCallbacks(execute=True):
                isis_if = ISISInterface.objects.create(
                    interface=self.iface_ge1, address_family="ipv4", instance=inst, metric=55
                )
        self.assertTrue(
            NSOISISInterfaceState.objects.filter(management=mgmt, interface=self.iface_ge1, af="ipv4").exists()
        )
        with patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent") as mock_push:
            with self.captureOnCommitCallbacks(execute=True):
                isis_if.delete()
        self.assertFalse(
            NSOISISInterfaceState.objects.filter(management=mgmt, interface=self.iface_ge1, af="ipv4").exists()
        )
        mock_push.assert_called()

    def test_owned_overlay_metric_network_type_not_clobbered(self):
        """A reconcile must not wipe an owned IS-IS row's pushed metric/network-type.

        Greenfield parity with OSPF: operator set metric/network-type via the
        ISISInterface, but the device hasn't applied them yet, so the adapter reports
        them as None/''. The owned overlay must keep the intent (else the next re-push
        would drop it from the adapter too)."""
        mgmt = self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOISISInterfaceState

        # Greenfield via the real signal path: creating the ISISInterface owns the
        # overlay (status accepted) with the operator's metric/network-type — this also
        # links the overlay so a later reconcile doesn't synthesise an empty object.
        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        ISISInterface.objects.create(
            interface=self.iface_ge0,
            address_family="ipv4",
            instance=inst,
            metric=50,
            network_type="point-to-point",
            circuit_type="level-2-only",
        )
        state = NSOISISInterfaceState.objects.get(management=mgmt, interface=self.iface_ge0, af="ipv4")
        self.assertEqual(state.status, "accepted")

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        _reconcile_isis_interfaces(
            self.device,
            self._payload(self._entry(metric=None, network_type="", circuit_type="level-1-2")),
        )

        state.refresh_from_db()
        self.assertEqual(state.metric, 50)
        self.assertEqual(state.network_type, "point-to-point")
        self.assertEqual(state.circuit_type, "level-2-only")
        # Device hasn't caught up to the intent → stays owned/pending (not premature in_sync).
        self.assertEqual(state.status, "accepted")

        # Once the device reports the pushed values, the owned row settles in_sync.
        _reconcile_isis_interfaces(
            self.device,
            self._payload(self._entry(metric=50, network_type="point-to-point", circuit_type="level-2-only")),
        )
        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(state.metric, 50)

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
        self.assertEqual(state.status, "imported")  # unowned, materialized → imported (unified)
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

    def test_empty_circuit_network_type_mirror_as_empty_string(self):
        """Unset circuit_type/network_type must mirror onto the netbox-routing
        ISISInterface as '' — those columns are NOT NULL (default=''), so writing
        None raised IntegrityError before the or-'' fix."""
        self._make_mgmt()
        from netbox_routing.models import ISISInterface

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        result = _reconcile_isis_interfaces(self.device, self._payload(self._entry(circuit_type="", network_type="")))
        self.assertEqual(len(result), 1)
        ri = ISISInterface.objects.get(interface=self.iface_ge0, address_family="ipv4")
        self.assertEqual(ri.circuit_type, "")
        self.assertEqual(ri.network_type, "")

    def test_prefix_sids_materialized_as_isis_prefixsid_rows(self):
        """A per-loopback prefix-SID list on the interface entry creates ISISPrefixSID rows.

        This is the item-86 read loop: node-SIDs the netbox-routing refactor moved out of
        ISISSegmentRouting are now materialized per (ISISInterface, algorithm). Index and
        absolute-label forms are mutually exclusive; flags (N/E) flow through. Exercised
        end-to-end through the real reconcile + real netbox_routing ORM.
        """
        self._make_mgmt()
        from netbox_routing.models import ISISInterface, ISISPrefixSID

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        entry = self._entry(
            iface_name="GigabitEthernet0/0",
            passive=True,
            prefix_sids=[
                {"algorithm": 0, "sid_index": 100006, "n_flag": True},
                {"algorithm": 128, "sid_label": 17128, "explicit_null": True},
            ],
        )
        _reconcile_isis_interfaces(self.device, self._payload(entry))

        ri = ISISInterface.objects.get(interface=self.iface_ge0, address_family="ipv4")
        sids = {p.algorithm: p for p in ISISPrefixSID.objects.filter(interface=ri)}
        self.assertEqual(set(sids), {0, 128})
        # base (algo 0): index form, N-flag set, no absolute label.
        self.assertEqual(sids[0].sid_index, 100006)
        self.assertIsNone(sids[0].sid_label)
        self.assertTrue(sids[0].n_flag)
        # flex-algo 128: absolute-label form, explicit-null set, no index.
        self.assertEqual(sids[128].sid_label, 17128)
        self.assertIsNone(sids[128].sid_index)
        self.assertTrue(sids[128].explicit_null)

    def test_prefix_sids_reconcile_is_clobber_safe_delete(self):
        """A later payload dropping an algorithm removes that ISISPrefixSID (brownfield mirror)."""
        self._make_mgmt()
        from netbox_routing.models import ISISInterface, ISISPrefixSID

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        _reconcile_isis_interfaces(
            self.device,
            self._payload(
                self._entry(prefix_sids=[{"algorithm": 0, "sid_index": 1}, {"algorithm": 128, "sid_index": 2}])
            ),
        )
        ri = ISISInterface.objects.get(interface=self.iface_ge0, address_family="ipv4")
        self.assertEqual(ISISPrefixSID.objects.filter(interface=ri).count(), 2)

        # Device drops the flex-algo SID; an untouched row auto-mirrors the deletion.
        _reconcile_isis_interfaces(
            self.device, self._payload(self._entry(prefix_sids=[{"algorithm": 0, "sid_index": 1}]))
        )
        self.assertEqual(set(ISISPrefixSID.objects.filter(interface=ri).values_list("algorithm", flat=True)), {0})

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

    def test_bfd_enabled_written_to_routing(self):
        """entry bfd_enabled flows onto netbox_routing ISISInterface.bfd_enabled."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        state = _reconcile_isis_interfaces(self.device, self._payload(self._entry(bfd_enabled=True)))[0]
        ri = state.isis_interface
        if ri is not None and hasattr(ri, "bfd_enabled"):
            ri.refresh_from_db()
            self.assertTrue(ri.bfd_enabled)

    def test_frr_written_to_routing_interface(self):
        """#83: entry frr_enabled/frr_protection flow onto netbox_routing ISISInterface.

        Tri-state: an explicit device-side disable (frr_enabled=False, the arcos
        bond2 shape) must persist as False — a falsy-drop would erase the signal."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        state = _reconcile_isis_interfaces(
            self.device, self._payload(self._entry(frr_enabled=True, frr_protection="node"))
        )[0]
        ri = state.isis_interface
        if ri is not None and hasattr(ri, "frr_enabled"):
            ri.refresh_from_db()
            self.assertIs(ri.frr_enabled, True)
            self.assertEqual(ri.frr_protection, "node")

        state2 = _reconcile_isis_interfaces(self.device, self._payload(self._entry(frr_enabled=False)))[0]
        ri2 = state2.isis_interface
        if ri2 is not None and hasattr(ri2, "frr_enabled"):
            ri2.refresh_from_db()
            self.assertIs(ri2.frr_enabled, False)
            self.assertEqual(ri2.frr_protection, "")

    def test_bfd_enabled_mirrored_onto_unowned_overlay(self):
        """Device bfd_enabled mirrors onto the (unowned) overlay too, so the write path
        (push/drift) reads the same tri-state the read path wrote to netbox-routing.

        None on the device stays None on the overlay: no opinion → the reconcile leaves
        any brownfield BFD untouched ('we don't delete what we don't have')."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        state = _reconcile_isis_interfaces(self.device, self._payload(self._entry(bfd_enabled=True)))[0]
        self.assertTrue(state.bfd_enabled)
        # A later payload with no BFD reported → None on the (still unowned) overlay.
        state2 = _reconcile_isis_interfaces(self.device, self._payload(self._entry(bfd_enabled=None)))[0]
        self.assertIsNone(state2.bfd_enabled)

    def test_greenfield_bfd_enabled_owns_overlay_and_is_pushed(self):
        """Operator sets bfd_enabled on the ISISInterface → owned overlay carries it, and
        the IS-IS interface push emits bfd_enabled (drive IS-IS BFD from the plugin UI)."""
        from unittest.mock import patch

        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOISISInterfaceState

        mgmt = self._make_mgmt()
        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        with patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent"):
            with self.captureOnCommitCallbacks(execute=True):
                ISISInterface.objects.create(
                    interface=self.iface_ge1, address_family="ipv4", instance=inst, bfd_enabled=True
                )
        state = NSOISISInterfaceState.objects.get(management=mgmt, interface=self.iface_ge1, af="ipv4")
        self.assertEqual(state.status, "accepted")
        self.assertTrue(state.bfd_enabled)

        captured = {}

        def _fake_put(adapter_id, interfaces, processes=None):
            captured["interfaces"] = interfaces

        orig = adapter_client.put_isis_interface_intent
        adapter_client.put_isis_interface_intent = _fake_put
        try:
            deliver("isis", mgmt.device_id, mgmt.adapter_device_id)
        finally:
            adapter_client.put_isis_interface_intent = orig
        entry = next(i for i in captured["interfaces"] if i["interface_name"] == "GigabitEthernet0/1")
        self.assertTrue(entry["bfd_enabled"])

    def test_clearing_bfd_enabled_flows_none_into_owned_overlay(self):
        """Clearing bfd_enabled on an owned ISISInterface flows None into the overlay so the
        adapter's retract fires — 'clearing a setting still remembers we own that intent'."""
        from unittest.mock import patch

        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOISISInterfaceState

        mgmt = self._make_mgmt()
        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        with patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent"):
            with self.captureOnCommitCallbacks(execute=True):
                ri = ISISInterface.objects.create(
                    interface=self.iface_ge1, address_family="ipv4", instance=inst, bfd_enabled=True
                )
        state = NSOISISInterfaceState.objects.get(management=mgmt, interface=self.iface_ge1, af="ipv4")
        self.assertTrue(state.bfd_enabled)
        # Operator unchecks BFD → None flows into the owned overlay (retract on push).
        with patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent"):
            with self.captureOnCommitCallbacks(execute=True):
                ri.bfd_enabled = None
                ri.save()
        state.refresh_from_db()
        self.assertIsNone(state.bfd_enabled)

    def test_owned_bfd_enabled_blocks_in_sync_until_device_catches_up(self):
        """An owned bfd_enabled=True intent keeps the row pending until the device reports BFD."""
        from unittest.mock import patch

        from netbox_routing.models import ISISInstance, ISISInterface

        from netbox_nso_plugin.models import NSOISISInterfaceState
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        mgmt = self._make_mgmt()
        inst = ISISInstance.objects.create(device=self.device, process_tag="")
        with patch("netbox_nso_plugin.adapter_client.put_isis_interface_intent"):
            with self.captureOnCommitCallbacks(execute=True):
                ISISInterface.objects.create(
                    interface=self.iface_ge0, address_family="ipv4", instance=inst, bfd_enabled=True
                )
        state = NSOISISInterfaceState.objects.get(management=mgmt, interface=self.iface_ge0, af="ipv4")
        # Device does NOT yet report BFD → owned row stays pending (not premature in_sync).
        _reconcile_isis_interfaces(self.device, self._payload(self._entry(circuit_type="", bfd_enabled=None)))
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        # Device catches up (reports bfd_enabled=True) → settles in_sync.
        _reconcile_isis_interfaces(self.device, self._payload(self._entry(circuit_type="", bfd_enabled=True)))
        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")

    def test_isis_device_matches_intent_bfd_tri_state(self):
        """The owned-row drift matcher treats bfd_enabled as tri-state: a None intent
        expresses no opinion (never blocks in_sync); True/False must match the device."""
        from types import SimpleNamespace

        from netbox_nso_plugin.template_content import _isis_device_matches_intent

        base = {"metric": None, "network_type": "", "circuit_type": "", "passive": False}

        def st(bfd, frr=None, prot=""):
            return SimpleNamespace(
                metric=None,
                network_type="",
                circuit_type="",
                passive=False,
                bfd_enabled=bfd,
                frr_enabled=frr,
                frr_protection=prot,
            )

        # None intent → matches regardless of the device's BFD (no opinion).
        self.assertTrue(_isis_device_matches_intent({**base, "bfd_enabled": True}, st(None)))
        # True intent: device with BFD matches; device without does not.
        self.assertTrue(_isis_device_matches_intent({**base, "bfd_enabled": True}, st(True)))
        self.assertFalse(_isis_device_matches_intent({**base, "bfd_enabled": None}, st(True)))
        # False intent: device disabled matches; device enabled does not.
        self.assertTrue(_isis_device_matches_intent({**base, "bfd_enabled": False}, st(False)))
        self.assertFalse(_isis_device_matches_intent({**base, "bfd_enabled": True}, st(False)))
        # FRR (#83): the same tri-state contract; protection only blocks when asserted.
        self.assertTrue(_isis_device_matches_intent({**base, "frr_enabled": True}, st(None)))
        self.assertTrue(_isis_device_matches_intent({**base, "frr_enabled": True}, st(None, frr=True)))
        self.assertFalse(_isis_device_matches_intent({**base, "frr_enabled": None}, st(None, frr=True)))
        self.assertTrue(_isis_device_matches_intent({**base, "frr_enabled": False}, st(None, frr=False)))
        self.assertTrue(
            _isis_device_matches_intent(
                {**base, "frr_enabled": True, "frr_protection": "node"}, st(None, frr=True, prot="node")
            )
        )
        self.assertFalse(_isis_device_matches_intent({**base, "frr_enabled": True}, st(None, frr=True, prot="node")))

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
        self.assertEqual(result[0].status, "imported")  # unowned, materialized → imported (unified)

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
        self.assertEqual(statuses["GigabitEthernet0/0"], "imported")  # unowned, materialized → imported (unified)
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
        # Owned rows are preserved (never reverted to imported). Since 9cc478b an owned
        # IS-IS row settles to in_sync only once the DEVICE confirms the pushed intent
        # (device-vs-intent semantics, mirroring OSPF); absent that confirmation it stays
        # an owned status. Assert the invariant the docstring names, not a premature in_sync.
        self.assertIn(result[0].status, ("accepted", "deploying", "in_sync", "apply_failed"))

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

    def test_owned_explicit_is_type_survives_device_omission(self):
        """An omitted NED default is absence, not permission to erase accepted intent."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "is_type": "level-2-only"}],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_process(self.device, [{"process_tag": "CORE"}])[0]

        self.assertEqual(state.is_type, "level-2-only")
        self.assertEqual(state.isis_instance.is_type, "level-2-only")
        self.assertEqual(state.status, "accepted")

    def test_unowned_historical_default_is_type_migrates_to_absence(self):
        """A pre-sweep served default must not survive into a later Accept payload."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "is_type": "level-1-2"}],
        )[0]
        self.assertEqual(state.status, "imported")

        state = _reconcile_isis_process(self.device, [{"process_tag": "CORE"}])[0]

        state.isis_instance.refresh_from_db()
        self.assertEqual(state.is_type, "")
        self.assertEqual(state.isis_instance.is_type, "")
        self.assertEqual(state.status, "imported")

    def test_nokia_owned_explicit_default_is_type_does_not_converge_on_omission(self):
        """A provenance-explicit default must be reported before intent can settle."""
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        platform = Platform.objects.create(
            name="Nokia SR OS",
            slug="nokia-sr-os",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(
            platform=platform,
            ned_id="timos-nc-23.10",
        )
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self._make_mgmt()

        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "is_type": "level-1-2"}],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_process(self.device, [{"process_tag": "CORE"}])[0]

        self.assertEqual(state.is_type, "level-1-2")
        self.assertEqual(state.status, "accepted")

    def test_junos_owned_explicit_default_is_type_does_not_converge_on_omission(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        platform = Platform.objects.create(
            name="Junos IS-IS",
            slug="junos-isis",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="juniper-junos-nc-4.19")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self._make_mgmt()

        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "", "is_type": "level-1-2"}],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_process(self.device, [{"process_tag": ""}])[0]

        self.assertEqual(state.is_type, "level-1-2")
        self.assertEqual(state.status, "accepted")

    def test_junos_operator_edited_is_type_survives_omission_after_blank_import(self):
        """The producer-faithful lifecycle: the corrected Junos reader never emits process
        is-type while both levels are enabled, so an owned is_type can only arrive by operator
        edit on top of a BLANK import — and a later omitting read must not erase it."""
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        platform = Platform.objects.create(
            name="Junos IS-IS edited",
            slug="junos-isis-edited",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="juniper-junos-nc-4.19")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self._make_mgmt()

        # 1. Import from the corrected reader: is_type is OMITTED, never blank-explicit.
        state = _reconcile_isis_process(self.device, [{"process_tag": ""}])[0]
        self.assertEqual(state.status, "imported")
        self.assertEqual(state.is_type, "")
        self.assertEqual(state.isis_instance.is_type, "")

        # 2. Operator edits the linked instance inline, then accepts the overlay.
        inst = state.isis_instance
        inst.is_type = "level-1-2"
        inst.save(update_fields=["is_type"])
        state.is_type = "level-1-2"
        state.status = "accepted"
        state.save(update_fields=["is_type", "status"])

        # 3. The device still omits is-type — absence is not confirmation, nor permission to erase.
        state = _reconcile_isis_process(self.device, [{"process_tag": ""}])[0]

        state.isis_instance.refresh_from_db()
        self.assertEqual(state.is_type, "level-1-2")
        self.assertEqual(state.isis_instance.is_type, "level-1-2")
        self.assertEqual(state.status, "accepted")

    def test_nokia_omitted_defaults_do_not_confirm_owned_nondefaults(self):
        from netbox_routing.models import ISISSegmentRouting

        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        platform = Platform.objects.create(
            name="Nokia IS-IS nondefaults",
            slug="nokia-isis-nondefaults",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self._make_mgmt()
        state = _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "CORE",
                    "lsp_lifetime": 1300,
                    "segment_routing": {"enabled": True, "tunnel_table_pref": 20},
                }
            ],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_process(self.device, [{"process_tag": "CORE"}])[0]

        state.isis_instance.refresh_from_db()
        sr = ISISSegmentRouting.objects.get(instance=state.isis_instance)
        self.assertEqual(state.isis_instance.lsp_lifetime, 1300)
        self.assertEqual(sr.tunnel_table_pref, 20)
        self.assertEqual(state.status, "accepted")

    def test_nokia_omitted_level_default_does_not_settle_owned_level(self):
        from netbox_routing.models import ISISLevel

        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        platform = Platform.objects.create(
            name="Nokia IS-IS level provenance",
            slug="nokia-isis-level-provenance",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self._make_mgmt()
        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "levels": [{"level": 2, "wide_metrics_only": False}]}],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_process(self.device, [{"process_tag": "CORE"}])[0]

        level = ISISLevel.objects.get(instance=state.isis_instance, level=2)
        self.assertFalse(level.wide_metrics_only)
        self.assertEqual(state.status, "accepted")

    def test_nokia_present_level_with_omitted_default_does_not_settle(self):
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        self._make_mgmt()
        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "levels": [{"level": 2, "wide_metrics_only": False}]}],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "levels": [{"level": 2}]}],
        )[0]

        self.assertEqual(state.status, "accepted")

    def test_nokia_omitted_long_scalar_alone_does_not_settle(self):
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        self._make_mgmt()
        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "lsp_lifetime": 1300}],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_process(self.device, [{"process_tag": "CORE"}])[0]

        self.assertEqual(state.isis_instance.lsp_lifetime, 1300)
        self.assertEqual(state.status, "accepted")

    def test_arcos_omitted_locator_default_does_not_confirm_owned_nondefault(self):
        from netbox_routing.models import ISISSRv6Locator

        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        platform = Platform.objects.create(
            name="ArcOS IS-IS nondefaults",
            slug="arcos-isis-nondefaults",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="arcos-v8.1.2X-nc-1.0")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self._make_mgmt()
        locator = {
            "name": "LOC",
            "prefix": "2001:db8:10::/64",
            "node_length": 20,
            "function_length": 20,
        }
        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "srv6_locators": [locator]}],
        )[0]
        state.status = "accepted"
        state.save(update_fields=["status"])
        corrected = dict(locator)
        corrected.pop("node_length")
        corrected.pop("function_length")

        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "srv6_locators": [corrected]}],
        )[0]

        native = ISISSRv6Locator.objects.get(instance=state.isis_instance, name="LOC")
        self.assertEqual((native.node_length, native.function_length), (20, 20))
        self.assertEqual(state.status, "accepted")

    def test_unowned_arcos_locator_omissions_clear_stale_values(self):
        from netbox_routing.models import ISISSRv6Locator

        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_process

        platform = Platform.objects.create(
            name="ArcOS IS-IS omission mirror",
            slug="arcos-isis-omission-mirror",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="arcos-v8.1.2X-nc-1.0")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self._make_mgmt()
        locator = {
            "name": "LOC",
            "prefix": "2001:db8:10::/64",
            "node_length": 20,
            "function_length": 20,
        }
        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "srv6_locators": [locator]}],
        )[0]
        corrected = {"name": "LOC", "prefix": "2001:db8:10::/64"}

        state = _reconcile_isis_process(
            self.device,
            [{"process_tag": "CORE", "srv6_locators": [corrected]}],
        )[0]

        native = ISISSRv6Locator.objects.get(instance=state.isis_instance, name="LOC")
        self.assertEqual((native.node_length, native.function_length), (None, None))
        self.assertEqual(state.status, "imported")

    def test_owned_auth_match_uses_presence_without_secret_readback(self):
        from netbox_nso_plugin.template_content import _isis_process_device_matches_intent

        state = SimpleNamespace(
            net="",
            is_type="",
            metric_style="",
            overload_bit=None,
            area_auth_type="sha",
            area_auth_present=True,
            area_auth_key="held-out-of-band",
            domain_auth_type="",
            domain_auth_present=False,
            domain_auth_key="",
            fast_reroute="",
            microloop_avoidance=None,
        )
        reported = {"area_auth_type": "sha", "area_auth_present": True}

        self.assertTrue(_isis_process_device_matches_intent(reported, state))
        self.assertFalse(_isis_process_device_matches_intent({**reported, "area_auth_present": False}, state))

    def test_owned_false_true_only_process_flags_match_reported_omission(self):
        from netbox_nso_plugin.template_content import _isis_process_device_matches_intent

        state = SimpleNamespace(
            net="",
            is_type="",
            metric_style="",
            overload_bit=False,
            area_auth_type="",
            area_auth_present=False,
            area_auth_key="",
            domain_auth_type="",
            domain_auth_present=False,
            domain_auth_key="",
            fast_reroute="",
            microloop_avoidance=False,
        )

        inst = SimpleNamespace(microloop_avoidance=False)
        self.assertTrue(_isis_process_device_matches_intent({}, state, device=self.device, inst=inst))
        state.overload_bit = None
        state.microloop_avoidance = None
        self.assertTrue(_isis_process_device_matches_intent({"overload_bit": True}, state))

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

    def test_edit_to_isis_instance_surfaces_as_changed_and_survives(self):
        """Editing the netbox-routing ISISInstance → drift, and the edit is not clobbered."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        payload = [{"process_tag": "0", "net": "49.0001.0000.0000.0001.00", "is_type": "level-2"}]
        _reconcile_isis_process(self.device, payload)
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        inst.is_type = "level-1"  # operator edit; device still reports level-2
        inst.save()

        states = _reconcile_isis_process(self.device, payload)
        self.assertEqual(states[0].status, "changed")  # edit surfaced as drift
        inst.refresh_from_db()
        self.assertEqual(inst.is_type, "level-1")  # edit preserved, not reverted

    def test_routing_instance_p1_scalars_and_settings(self):
        """instance scalar columns + ISISSetting EAV are reconciled from NSO."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISSetting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "lsp_lifetime": 65535,
                    "lsp_refresh_interval": 32767,
                    "te_enabled": True,
                    # The adapter still sends the legacy top-level sr_enabled; the
                    # reconciler must tolerate + ignore it (SR now lives on the
                    # ISISSegmentRouting child, exercised in the p2 test below).
                    "sr_enabled": True,
                    "spf_initial_wait": 1000,
                    "settings": {"spf_second_wait": "1000", "graceful_restart": "true"},
                }
            ],
        )
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        self.assertEqual(inst.lsp_lifetime, 65535)
        self.assertEqual(inst.lsp_refresh_interval, 32767)
        self.assertEqual(inst.spf_initial_wait, 1000)
        self.assertTrue(inst.te_enabled)
        # sr_enabled is no longer an ISISInstance field (moved to the SR child) —
        # the reconciler ignored the legacy top-level key without error.
        self.assertFalse(hasattr(inst, "sr_enabled"))

        settings = {s.key: s.value for s in inst.settings.all()}
        self.assertEqual(settings, {"spf_second_wait": "1000", "graceful_restart": "true"})

        # 3-way: a later device change with the object UNTOUCHED auto-mirrors (the
        # dropped 'graceful_restart' is removed, 'spf_second_wait' updated), stays in sync.
        states = _reconcile_isis_process(
            self.device,
            [{"process_tag": "0", "settings": {"spf_second_wait": "2000"}}],
        )
        settings = {s.key: s.value for s in ISISSetting.objects.all()}
        self.assertEqual(settings, {"spf_second_wait": "2000"})  # auto-mirrored
        self.assertEqual(states[0].status, "imported")

    def test_routing_interface_p1_scalars_and_settings(self):
        """per-interface scalar columns + ISISSetting EAV are reconciled."""
        self._make_mgmt()
        from netbox_routing.models import ISISInterface

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        iface = Interface.objects.create(device=self.device, name="GigabitEthernet0/0", type="1000base-t")
        _reconcile_isis_interfaces(
            self.device,
            [
                {
                    "interface_name": iface.name,
                    "af": "ipv4",
                    "process_tag": "",
                    "circuit_type": "level-1-2",
                    "passive": False,
                    "network_type": "point-to-point",
                    "csnp_interval": 10,
                    "retransmit_interval": 5,
                    "lsp_interval": 100,
                    "mesh_group": "blocked",
                    "settings": {"hello_padding": "true"},
                }
            ],
        )
        ri = ISISInterface.objects.get(interface=iface, address_family="ipv4")
        self.assertEqual(ri.network_type, "point-to-point")
        self.assertEqual(ri.csnp_interval, 10)
        self.assertEqual(ri.retransmit_interval, 5)
        self.assertEqual(ri.lsp_interval, 100)
        self.assertEqual(ri.mesh_group, "blocked")
        self.assertEqual({s.key: s.value for s in ri.settings.all()}, {"hello_padding": "true"})

    def test_routing_instance_p2_levels_and_sr(self):
        """instance per-level rows + segment-routing (1:1) are reconciled."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISLevel, ISISSegmentRouting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "levels": [
                        # Mirrors Junos rc1: L1 disabled, L2 wide-metrics-only + labeled-preference.
                        {"level": 1, "disabled": True},
                        {
                            "level": 2,
                            "default_metric": 10,
                            "wide_metrics_only": True,
                            "labeled_preference": 7,
                        },
                    ],
                    "segment_routing": {"enabled": True, "prefix_sid_range": "global"},
                }
            ],
        )
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        levels = {lvl.level: lvl for lvl in ISISLevel.objects.filter(instance=inst)}
        self.assertEqual(set(levels), {1, 2})
        self.assertTrue(levels[1].disabled)
        self.assertEqual(levels[2].default_metric, 10)
        self.assertTrue(levels[2].wide_metrics_only)
        self.assertEqual(levels[2].labeled_preference, 7)
        sr = ISISSegmentRouting.objects.get(instance=inst)
        self.assertTrue(sr.enabled)
        self.assertEqual(sr.prefix_sid_range, "global")

        # 3-way: a later device change with the object UNTOUCHED auto-mirrors (level 1
        # dropped, level 2 metric updated), stays in sync.
        states = _reconcile_isis_process(
            self.device,
            [{"process_tag": "0", "levels": [{"level": 2, "default_metric": 20}]}],
        )
        self.assertEqual(
            set(ISISLevel.objects.filter(instance=inst).values_list("level", flat=True)), {2}
        )  # auto-mirrored
        self.assertEqual(ISISLevel.objects.get(instance=inst, level=2).default_metric, 20)  # auto-mirrored
        self.assertEqual(states[0].status, "imported")

    def test_sr_child_preserved_when_segment_routing_key_absent(self):
        """A payload that omits ``segment_routing`` must NOT delete an existing SR child.

        The adapter may not report SR in every payload (e.g. an older adapter that
        hasn't wired the bag). A missing key is "unreported", not "device has no SR" —
        clobbering it would silently drop SR state for an SR-enabled device.
        """
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISSegmentRouting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        _reconcile_isis_process(
            self.device, [{"process_tag": "0", "segment_routing": {"enabled": True, "prefix_sid_range": "global"}}]
        )
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        self.assertTrue(ISISSegmentRouting.objects.filter(instance=inst).exists())

        # A later reconcile that carries NO segment_routing key must preserve the child.
        _reconcile_isis_process(self.device, [{"process_tag": "0", "levels": [{"level": 2, "default_metric": 20}]}])
        self.assertTrue(
            ISISSegmentRouting.objects.filter(instance=inst).exists(),
            "SR child must survive when the payload omits the segment_routing key",
        )

    def test_sr_child_deleted_on_authoritative_reported_absence(self):
        """Current-reader provenance can authoritatively remove stale SR state."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISSegmentRouting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        _reconcile_isis_process(self.device, [{"process_tag": "0", "segment_routing": {"enabled": True}}])
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        self.assertTrue(ISISSegmentRouting.objects.filter(instance=inst).exists())

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "segment_routing_reported": True,
                    "segment_routing_configured": False,
                }
            ],
        )
        self.assertFalse(
            ISISSegmentRouting.objects.filter(instance=inst).exists(),
            "an authoritative configured=false report must delete the SR child",
        )

    def test_configured_empty_segment_routing_preserves_existing_child(self):
        from netbox_routing.models import ISISSegmentRouting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        self._make_mgmt()
        _reconcile_isis_process(
            self.device,
            [{"process_tag": "0", "segment_routing": {"enabled": True}}],
        )
        self.assertTrue(ISISSegmentRouting.objects.filter(instance__device=self.device).exists())

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "segment_routing_reported": True,
                    "segment_routing_configured": True,
                }
            ],
        )

        self.assertTrue(ISISSegmentRouting.objects.filter(instance__device=self.device).exists())

    def test_configured_empty_segment_routing_creates_child_on_first_import(self):
        from netbox_routing.models import ISISSegmentRouting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        self._make_mgmt()

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "segment_routing_reported": True,
                    "segment_routing_configured": True,
                }
            ],
        )

        self.assertTrue(ISISSegmentRouting.objects.filter(instance__device=self.device).exists())

    def test_unowned_sr_omitted_columns_are_cleared_before_base_advances(self):
        from netbox_routing.models import ISISInstance, ISISSegmentRouting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        self._make_mgmt()
        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "segment_routing": {
                        "enabled": True,
                        "tunnel_table_pref": 20,
                    },
                }
            ],
        )
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "segment_routing_reported": True,
                    "segment_routing_configured": True,
                    "segment_routing": {"enabled": True},
                }
            ],
        )

        sr = ISISSegmentRouting.objects.get(instance=inst)
        self.assertIsNone(sr.tunnel_table_pref)

    def test_sr_deprecated_node_sid_keys_ignored_new_cols_written(self):
        """Instance-level node_sid_* keys are ignored (no crash); srlb_*/srv6 are mirrored.

        netbox-routing refactored node_sid_index/label (+ v6) OFF ISISSegmentRouting into
        the per-loopback ISISPrefixSID child and added srlb_start/range + srv6_enabled. The
        network-state export is still instance-level, so the SR bag keeps carrying node_sid_*
        — the reconcile must skip them. Before the fix they landed in save(update_fields=[...])
        and raised ValueError ("fields do not exist in this model: node_sid_index, ...") →
        HTTP 500 on any SR accept. Exercised end-to-end through the real reconcile + real
        netbox_routing ORM.
        """
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISSegmentRouting

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "segment_routing": {
                        "enabled": True,
                        "prefix_sid_range": "global",
                        "srgb_start": 16000,
                        "srgb_range": 8000,
                        # deprecated instance-level node-SIDs still emitted by the export:
                        "node_sid_index": 100,
                        "node_sid_label": 100100,
                        "node_sid_v6_index": 200,
                        "node_sid_v6_label": 100200,
                        # newer surviving columns:
                        "srlb_start": 15000,
                        "srlb_range": 1000,
                        "srv6_enabled": True,
                        "maximum_sid_depth": 10,
                        "tunnel_table_pref": 8,
                    },
                }
            ],
        )
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        sr = ISISSegmentRouting.objects.get(instance=inst)
        # Surviving + new instance columns are mirrored.
        self.assertTrue(sr.enabled)
        self.assertEqual(sr.prefix_sid_range, "global")
        self.assertEqual(sr.srgb_start, 16000)
        self.assertEqual(sr.srgb_range, 8000)
        self.assertEqual(sr.srlb_start, 15000)
        self.assertEqual(sr.srlb_range, 1000)
        self.assertTrue(sr.srv6_enabled)
        self.assertEqual(sr.maximum_sid_depth, 10)
        self.assertEqual(sr.tunnel_table_pref, 8)
        # node_sid_* are genuinely gone from the model (guards against a silent re-add
        # that would resurrect the crash path).
        model_fields = {f.name for f in ISISSegmentRouting._meta.get_fields()}
        for gone in ("node_sid_index", "node_sid_label", "node_sid_v6_index", "node_sid_v6_label"):
            self.assertNotIn(gone, model_fields)


class TestReconcileIsisInterfaceLevels(TestCase):
    """per-level interface child rows."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="IL2Mfg", slug="il2mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IL2Dev", slug="il2dev")
        role = DeviceRole.objects.create(name="IL2Role", slug="il2role")
        site = Site.objects.create(name="IL2Site", slug="il2site")
        cls.device = Device.objects.create(name="il2-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="il2-inst", defaults={"adapter_instance_id": "il2-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "il2-dev", "adapter_device_id": self.device.pk},
        )[0]

    def test_interface_levels_reconciled(self):
        self._make_mgmt()
        from netbox_routing.models import ISISInterface, ISISInterfaceLevel

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        iface = Interface.objects.create(device=self.device, name="GigabitEthernet0/0", type="1000base-t")
        _reconcile_isis_interfaces(
            self.device,
            [
                {
                    "interface_name": iface.name,
                    "af": "ipv4",
                    "process_tag": "",
                    "passive": False,
                    "levels": [{"level": 2, "metric": 10, "hello_interval": 3}],
                }
            ],
        )
        ri = ISISInterface.objects.get(interface=iface, address_family="ipv4")
        rows = {lvl.level: lvl for lvl in ISISInterfaceLevel.objects.filter(interface=ri)}
        self.assertEqual(set(rows), {2})
        self.assertEqual(rows[2].metric, 10)
        self.assertEqual(rows[2].hello_interval, 3)

    def test_unowned_explicit_level_omission_clears_stale_value(self):
        self._make_mgmt()
        from netbox_routing.models import ISISInterface, ISISInterfaceLevel

        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        iface = Interface.objects.create(device=self.device, name="to-omit", type="1000base-t")
        _reconcile_isis_interfaces(
            self.device,
            [
                {
                    "interface_name": iface.name,
                    "af": "ipv4",
                    "process_tag": "",
                    "levels": [{"level": 2, "hello_interval": 10}],
                }
            ],
        )
        ri = ISISInterface.objects.get(interface=iface, address_family="ipv4")

        _reconcile_isis_interfaces(
            self.device,
            [
                {
                    "interface_name": iface.name,
                    "af": "ipv4",
                    "process_tag": "",
                    "levels": [{"level": 2}],
                }
            ],
        )

        self.assertIsNone(ISISInterfaceLevel.objects.get(interface=ri, level=2).hello_interval)

    def test_nokia_explicit_default_interface_fields_survive_omission_without_false_settle(self):
        """Provenance-explicit omissions preserve intent but cannot confirm it landed."""
        self._make_mgmt()
        from netbox_routing.models import ISISInterface, ISISInterfaceLevel

        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        platform = Platform.objects.create(
            name="Nokia ISIS defaults",
            slug="nokia-isis-defaults",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        iface = Interface.objects.create(device=self.device, name="to-core", type="1000base-t")
        initial = {
            "interface_name": iface.name,
            "af": "ipv4",
            "process_tag": "",
            "circuit_type": "level-1-2",
            "passive": False,
            "csnp_interval": 10,
            "retransmit_interval": 5,
            "lsp_interval": 100,
            "levels": [
                {
                    "level": 2,
                    "hello_interval": 9,
                    "hello_multiplier": 3,
                    "priority": 64,
                    "passive": False,
                }
            ],
        }
        state = _reconcile_isis_interfaces(self.device, [initial])[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        corrected = {
            "interface_name": iface.name,
            "af": "ipv4",
            "process_tag": "",
            "levels": [],
        }
        state = _reconcile_isis_interfaces(self.device, [corrected])[0]

        native = ISISInterface.objects.get(interface=iface, address_family="ipv4")
        level = ISISInterfaceLevel.objects.get(interface=native, level=2)
        self.assertEqual(native.circuit_type, "level-1-2")
        self.assertEqual((native.csnp_interval, native.retransmit_interval, native.lsp_interval), (10, 5, 100))
        self.assertEqual((level.hello_interval, level.hello_multiplier, level.priority), (9, 3, 64))
        self.assertEqual(state.status, "accepted")

    def test_nokia_owned_interface_without_timer_intent_converges_on_omission(self):
        self._make_mgmt()
        from netbox_nso_plugin.models import NSOPlatformNedMapping
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        platform = Platform.objects.create(
            name="Nokia ISIS no timers",
            slug="nokia-isis-no-timers",
            manufacturer=self.device.device_type.manufacturer,
        )
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="timos-nc-23.10")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        iface = Interface.objects.create(device=self.device, name="to-no-timers", type="1000base-t")
        reported = {
            "interface_name": iface.name,
            "af": "ipv4",
            "process_tag": "",
            "circuit_type": "",
            "network_type": "",
            "metric": None,
            "passive": False,
        }
        state = _reconcile_isis_interfaces(self.device, [reported])[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        state = _reconcile_isis_interfaces(self.device, [reported])[0]

        self.assertEqual(state.status, "in_sync")

    def test_owned_interface_level_omission_does_not_settle_when_scalars_match(self):
        """A level-only provenance gap must survive the top-level intent comparison."""
        self._make_mgmt()
        from netbox_nso_plugin.template_content import _reconcile_isis_interfaces

        iface = Interface.objects.create(device=self.device, name="to-level-only", type="1000base-t")
        initial = {
            "interface_name": iface.name,
            "af": "ipv4",
            "process_tag": "",
            "circuit_type": "",
            "network_type": "",
            "metric": None,
            "passive": False,
            "levels": [{"level": 2, "metric": 10}],
        }
        state = _reconcile_isis_interfaces(self.device, [initial])[0]
        state.status = "accepted"
        state.save(update_fields=["status"])

        reported = dict(initial)
        reported["levels"] = [{"level": 2}]
        state = _reconcile_isis_interfaces(self.device, [reported])[0]

        self.assertEqual(state.status, "accepted")

    def test_routing_instance_flex_algos(self):
        """ISISFlexAlgo rows are reconciled (full-replace by algo_id)."""
        self._make_mgmt()
        from netbox_routing.models import ISISFlexAlgo, ISISInstance

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        _reconcile_isis_process(
            self.device,
            [
                {
                    "process_tag": "0",
                    "flex_algos": [
                        {"algo_id": 128, "metric_type": "igp-metric", "priority": 100, "admin_group_exclude": "BLUE"},
                        {"algo_id": 129, "metric_type": "igp-metric", "priority": 100, "admin_group_exclude": "RED"},
                    ],
                }
            ],
        )
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        fas = {fa.algo_id: fa for fa in ISISFlexAlgo.objects.filter(instance=inst)}
        self.assertEqual(set(fas), {128, 129})
        self.assertEqual(fas[128].metric_type, "igp-metric")
        self.assertEqual(fas[128].admin_group_exclude, "BLUE")

        # 3-way: a later device change with the object UNTOUCHED auto-mirrors (129
        # dropped, 128 metric_type updated), stays in sync.
        states = _reconcile_isis_process(
            self.device,
            [{"process_tag": "0", "flex_algos": [{"algo_id": 128, "metric_type": "delay-metric"}]}],
        )
        fas = {fa.algo_id: fa for fa in ISISFlexAlgo.objects.filter(instance=inst)}
        self.assertEqual(set(fas), {128})  # auto-mirrored
        self.assertEqual(fas[128].metric_type, "delay-metric")  # auto-mirrored
        self.assertEqual(states[0].status, "imported")

    def test_routing_instance_srv6_locators(self):
        """ISISSRv6Locator rows are reconciled (full-replace by name), no churn, clobber-safe."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance, ISISSRv6Locator

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        payload = [
            {
                "process_tag": "0",
                "srv6_locators": [
                    {"name": "LOC1", "prefix": "2001:db8:a1::/64", "algorithm": 128, "enabled": True},
                    {"name": "LOC2", "prefix": "2001:db8:a2::/64", "enabled": True},
                ],
            }
        ]
        _reconcile_isis_process(self.device, payload)
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        locs = {loc.name: loc for loc in ISISSRv6Locator.objects.filter(instance=inst)}
        self.assertEqual(set(locs), {"LOC1", "LOC2"})
        self.assertEqual(str(locs["LOC1"].prefix), "2001:db8:a1::/64")
        self.assertEqual(locs["LOC1"].algorithm, 128)
        self.assertTrue(locs["LOC1"].enabled)

        # Re-running the SAME payload with the object untouched must be a no-op: the
        # IPNetwork prefix is compared stringified, so it does NOT churn to 'changed'.
        states = _reconcile_isis_process(self.device, payload)
        self.assertEqual(states[0].status, "imported")

        # 3-way: device drops LOC2; an untouched object auto-mirrors the deletion.
        _reconcile_isis_process(
            self.device,
            [{"process_tag": "0", "srv6_locators": [{"name": "LOC1", "prefix": "2001:db8:a1::/64"}]}],
        )
        self.assertEqual(set(ISISSRv6Locator.objects.filter(instance=inst).values_list("name", flat=True)), {"LOC1"})

    def test_routing_instance_attached_bit(self):
        """suppress/ignore-attached-bit flow onto ISISInstance; re-reconcile stays in sync."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        payload = [{"process_tag": "0", "suppress_attached_bit": True, "ignore_attached_bit": True}]
        _reconcile_isis_process(self.device, payload)
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        self.assertTrue(inst.suppress_attached_bit)
        self.assertTrue(inst.ignore_attached_bit)

        # Re-running the same payload with the object untouched is a no-op (stays imported).
        states = _reconcile_isis_process(self.device, payload)
        self.assertEqual(states[0].status, "imported")

        # A payload that stops reporting the knobs leaves the accepted values intact
        # (emit-True-only convention: absence is "not reported", never a forced clear).
        _reconcile_isis_process(self.device, [{"process_tag": "0"}])
        inst.refresh_from_db()
        self.assertTrue(inst.suppress_attached_bit)
        self.assertTrue(inst.ignore_attached_bit)

    def test_routing_instance_frr(self):
        """#83: fast_reroute/microloop_avoidance flow onto ISISInstance; re-reconcile
        stays in sync; a payload that stops reporting keeps the mirrored values."""
        self._make_mgmt()
        from netbox_routing.models import ISISInstance

        from netbox_nso_plugin.template_content import _reconcile_isis_process

        payload = [{"process_tag": "0", "fast_reroute": "ti-lfa", "microloop_avoidance": True}]
        _reconcile_isis_process(self.device, payload)
        inst = ISISInstance.objects.get(device=self.device, process_tag="0")
        self.assertEqual(inst.fast_reroute, "ti-lfa")
        self.assertTrue(inst.microloop_avoidance)

        # Re-running the same payload with the object untouched is a no-op (stays imported).
        states = _reconcile_isis_process(self.device, payload)
        self.assertEqual(states[0].status, "imported")

        # Absence = "not reported", never a forced clear.
        _reconcile_isis_process(self.device, [{"process_tag": "0"}])
        inst.refresh_from_db()
        self.assertEqual(inst.fast_reroute, "ti-lfa")
        self.assertTrue(inst.microloop_avoidance)
