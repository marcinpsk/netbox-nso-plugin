# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the signal handlers in signals.py.

Every handler is driven against real Django model rows — a real device + interface +
NSODeviceManagement / NSOInterfaceState — so the handlers' own ORM queries and updates
are exercised for real. Only the adapter_client HTTP functions are patched, since those
are the genuine external boundary. (Earlier revisions called the handlers with MagicMock'd
instances plus a sys.modules-injected fake `models` module, which bypassed exactly those
queries — e.g. the OWNED-states filter — and so could not catch a regression in them.)
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils import timezone

from .mixins import IntentPushResetMixin

_MOD = "netbox_nso_plugin.adapter_client"


class _SignalDBBase(IntentPushResetMixin, TestCase):
    """Shared real-DB fixture for the signal-handler tests.

    These handlers read and update the real overlay models (NSODeviceManagement /
    NSOInterfaceState) — ``sync_scope_to_adapter`` does
    ``type(instance).objects.filter(pk=…).update(…)`` and ``push_intent_on_accept``
    queries ``NSOInterfaceState.objects.filter(…).select_related(…)`` — so they are
    driven against real rows, not a MagicMock'd ORM. Only the adapter_client HTTP
    functions (onboard_device/set_scope/sync_notify/patch_device/put_intent) are
    patched: those are the genuine external boundary. (A sibling TestIPAddressSignals
    already follows this pattern.)
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

        from netbox_nso_plugin.models import NSOInstance

        mfg = Manufacturer.objects.create(name="SigMfg", slug="sigmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SigDev", slug="sigdev")
        role = DeviceRole.objects.create(name="SigRole", slug="sigrole")
        site = Site.objects.create(name="SigSite", slug="sigsite")
        cls.device = Device.objects.create(name="core-rtr-01", device_type=dt, role=role, site=site)
        cls.iface = Interface.objects.create(
            device=cls.device, name="GigabitEthernet0/0", type="1000base-t", description="uplink", enabled=True
        )
        cls.nso_instance = NSOInstance.objects.create(name="nso-prod", adapter_instance_id="nso-prod")

    def _make_mgmt(
        self,
        *,
        adapter_device_id=None,
        manage_description=True,
        manage_enabled=False,
        auto_apply=False,
        sync_before_apply=True,
    ):
        """Create a real NSODeviceManagement row WITHOUT firing its post_save sync signal.

        bulk_create skips signals, so the test can drive sync_scope_to_adapter explicitly
        rather than have the row's own save trigger it.
        """
        from netbox_nso_plugin.models import NSODeviceManagement

        NSODeviceManagement.objects.bulk_create(
            [
                NSODeviceManagement(
                    device=self.device,
                    nso_instance=self.nso_instance,
                    nso_device_name="core-rtr-01",
                    adapter_device_id=adapter_device_id,
                    manage_description=manage_description,
                    manage_enabled=manage_enabled,
                    auto_apply=auto_apply,
                    sync_before_apply=sync_before_apply,
                    custom_field_data={},
                )
            ]
        )
        return NSODeviceManagement.objects.get(device=self.device)

    def _accepted_state(self, interface, attribute, *, nso_value=""):
        """Create an OWNED (accepted_at-set) NSOInterfaceState without firing the push signal.

        Created as ``imported`` then promoted via a queryset ``update`` (no post_save), so
        the test drives push_intent_on_accept explicitly. The returned in-memory object has
        the accepted markers set to match the row.
        """
        from netbox_nso_plugin.models import NSOInterfaceState

        now = timezone.now()
        state = NSOInterfaceState.objects.create(
            interface=interface, attribute=attribute, status="imported", nso_value=nso_value
        )
        NSOInterfaceState.objects.filter(pk=state.pk).update(status="accepted", accepted_at=now)
        state.status = "accepted"
        state.accepted_at = now
        return state


class TestSyncScopeToAdapter(_SignalDBBase):
    """Tests for the sync_scope_to_adapter signal handler (real NSODeviceManagement row)."""

    def test_created_onboards_device_and_sets_scope(self):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        mgmt = self._make_mgmt(adapter_device_id=None)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 99}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={"device_id": 99}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value={"job_id": 5}) as mock_notify,
            patch(f"{_MOD}.patch_device") as mock_patch,
        ):
            sync_scope_to_adapter(sender=type(mgmt), instance=mgmt, created=True)

            mock_onboard.assert_called_once_with(
                nso_instance="nso-prod",
                nso_device_name="core-rtr-01",
                netbox_device_id=self.device.pk,
            )
            mock_scope.assert_called_once_with(99, ["description"], auto_apply=False, sync_before_apply=True)
            mock_notify.assert_called_once_with(99)
            mock_patch.assert_not_called()

        # The handler wrote the adapter id back to the real row via a queryset update.
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 99)

    def test_update_patches_device_and_sets_scope(self):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        mgmt = self._make_mgmt(adapter_device_id=7)

        with (
            patch(f"{_MOD}.patch_device", return_value=None) as mock_patch,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
            patch(f"{_MOD}.onboard_device") as mock_onboard,
        ):
            sync_scope_to_adapter(sender=type(mgmt), instance=mgmt, created=False)

        mock_patch.assert_called_once_with(
            adapter_device_id=7,
            nso_instance="nso-prod",
            nso_device_name="core-rtr-01",
        )
        mock_scope.assert_called_once()
        mock_onboard.assert_not_called()

    def test_adapter_error_is_swallowed_with_warning(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        mgmt = self._make_mgmt(adapter_device_id=None)

        with patch(f"{_MOD}.onboard_device", side_effect=AdapterError("down", code="nso_unreachable")):
            # Should not raise — a warning is logged instead.
            sync_scope_to_adapter(sender=type(mgmt), instance=mgmt, created=True)

    def test_sync_notify_job_logged(self):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        mgmt = self._make_mgmt(adapter_device_id=None)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 10}),
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value={"job_id": 7}),
        ):
            with self.assertLogs("netbox_nso_plugin.signals", level="DEBUG"):
                sync_scope_to_adapter(sender=type(mgmt), instance=mgmt, created=True)

    def test_created_none_adapter_id_triggers_onboard(self):
        """created=False but adapter_device_id=None should also trigger onboard."""
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        mgmt = self._make_mgmt(adapter_device_id=None)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 3}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            sync_scope_to_adapter(sender=type(mgmt), instance=mgmt, created=False)

        mock_onboard.assert_called_once()

    def test_manage_enabled_included_in_scope(self):
        """manage_enabled=True includes 'enabled' in the managed_attributes scope call."""
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        mgmt = self._make_mgmt(adapter_device_id=None, manage_enabled=True)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 5}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            sync_scope_to_adapter(sender=type(mgmt), instance=mgmt, created=True)

        mock_onboard.assert_called_once()
        # Both description and enabled should be in the scope call.
        mock_scope.assert_called_once_with(5, ["description", "enabled"], auto_apply=False, sync_before_apply=True)


class TestOffboardDeviceFromAdapter(unittest.TestCase):
    """Tests for the offboard_device_from_adapter signal handler.

    The handler reads exactly one attribute (``instance.adapter_device_id``) and calls
    the adapter ``delete_device`` boundary. A SimpleNamespace is the honest stand-in: it
    carries that field and raises AttributeError on anything else, whereas a MagicMock
    would silently fabricate any attribute. ``delete_device`` is the real external
    boundary and stays patched.
    """

    def test_offboards_when_adapter_device_id_set(self):
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = SimpleNamespace(adapter_device_id=55)
        with patch(f"{_MOD}.delete_device") as mock_delete:
            offboard_device_from_adapter(sender=None, instance=instance)

        mock_delete.assert_called_once_with(55)

    def test_skips_when_adapter_device_id_none(self):
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = SimpleNamespace(adapter_device_id=None)
        with patch(f"{_MOD}.delete_device") as mock_delete:
            offboard_device_from_adapter(sender=None, instance=instance)

        mock_delete.assert_not_called()

    def test_adapter_error_swallowed(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = SimpleNamespace(adapter_device_id=5)
        with patch(f"{_MOD}.delete_device", side_effect=AdapterError("gone", code="not_found")):
            # Should not raise — a warning is logged instead.
            offboard_device_from_adapter(sender=None, instance=instance)


class TestPushIntentOnAccept(_SignalDBBase):
    """Tests for push_intent_on_accept (real overlay rows; put_intent is the boundary).

    The handler resolves the device's NSODeviceManagement, then schedules
    _push_interface_intent_for_device, which queries every OWNED NSOInterfaceState for
    the device and builds the put_intent payload. Driving it against real rows exercises
    that real filter + select_related + attribute-building — the part a MagicMock'd
    queryset (returning a hand-built [state]) entirely bypassed.
    """

    def test_skips_when_not_owned(self):
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        # accepted_at is None → not owned, whatever the sync status.
        state = NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="imported", nso_value="x"
        )

        with patch(f"{_MOD}.put_intent") as mock_put:
            push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_not_called()

    def test_pushes_intent_on_accepted(self):
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_called_once()
        adapter_id, attrs = mock_put.call_args[0]
        self.assertEqual(adapter_id, 7)
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["interface"], "GigabitEthernet0/0")
        self.assertEqual(attrs[0]["attribute"], "description")
        self.assertEqual(attrs[0]["intent_value"], "uplink")  # from the real Interface.description

    def test_pushes_enabled_attribute(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=3)
        iface = Interface.objects.create(device=self.device, name="Loopback0", type="virtual", enabled=False)
        state = self._accepted_state(iface, "enabled", nso_value="true")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        attrs = mock_put.call_args[0][1]
        self.assertEqual(attrs[0]["intent_value"], "false")  # str(Interface.enabled).lower()

    def test_skips_when_mgmt_does_not_exist(self):
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        # No NSODeviceManagement for this device → NSODeviceManagement.objects.get raises.
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_not_called()

    def test_skips_when_adapter_id_none(self):
        """A management row without an adapter_device_id yet → nothing to push to."""
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=None)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        mock_put.assert_not_called()

    def test_skips_unknown_attribute(self):
        """An owned state with an attribute outside (description, enabled) is dropped."""
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "mtu", nso_value="1500")

        with patch(f"{_MOD}.put_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)

        # put_intent is still called, but the unknown attribute was filtered out.
        attrs = mock_put.call_args[0][1]
        self.assertEqual(attrs, [])

    def test_put_intent_error_is_swallowed(self):
        """put_intent raising AdapterError is caught and logged, not propagated."""
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=3)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        with patch(f"{_MOD}.put_intent", side_effect=AdapterError("down", code="nso_unreachable")):
            with self.captureOnCommitCallbacks(execute=True):
                # Should not raise — a warning is logged instead.
                push_intent_on_accept(sender=NSOInterfaceState, instance=state)


class TestSkipOnRenderGuard(_SignalDBBase):
    """An intent push must never fire during a GET render.

    Regression for the device-27 NSO-tab loop: rendering the tab re-saves every
    NSOInterfaceState row, and each save of an 'accepted' row pushed the full intent
    snapshot — O(N) pushes per render, each O(N) — hanging the page and re-minting
    accepts. The @_skip_on_render guard drops the push when current_request is a GET.
    """

    def _fire_with_method(self, method):
        """Drive push_intent_on_accept with current_request set to a real GET/POST/None."""
        from netbox.context import current_request

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.signals import push_intent_on_accept

        self._make_mgmt(adapter_device_id=7)
        state = self._accepted_state(self.iface, "description", nso_value="uplink")

        req = None if method is None else getattr(RequestFactory(), method.lower())("/")
        token = current_request.set(req)
        try:
            with patch(f"{_MOD}.put_intent") as mock_put:
                with self.captureOnCommitCallbacks(execute=True):
                    push_intent_on_accept(sender=NSOInterfaceState, instance=state)
                return mock_put
        finally:
            current_request.reset(token)

    def test_get_render_does_not_push(self):
        """A GET (page render) is suppressed even for an accepted state."""
        self._fire_with_method("GET").assert_not_called()

    def test_post_accept_pushes(self):
        """An operator accept arrives as a POST — push proceeds."""
        self._fire_with_method("POST").assert_called_once()

    def test_no_request_pushes(self):
        """Programmatic / CLI context (no request) still pushes."""
        self._fire_with_method(None).assert_called_once()


# ---------------------------------------------------------------------------
# TestIPAddressSignals — Django DB integration tests for the IP signal path
# ---------------------------------------------------------------------------

try:
    from django.test import TestCase as DjangoTestCase

    class TestIPAddressSignals(IntentPushResetMixin, DjangoTestCase):
        """Django-DB integration tests for the IPAddress signal → put_ip_intent path."""

        @classmethod
        def setUpTestData(cls):
            from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

            from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

            manufacturer = Manufacturer.objects.create(name="IpSigMfg", slug="ipsigmfg")
            device_type = DeviceType.objects.create(manufacturer=manufacturer, model="IpSigDev", slug="ipsigdev")
            role = DeviceRole.objects.create(name="IpSigRole", slug="ipsigrole")
            site = Site.objects.create(name="IpSigSite", slug="ipsigsite")
            cls.device = Device.objects.create(name="ip-sig-router", device_type=device_type, role=role, site=site)
            cls.iface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")

            cls.unmanaged_device = Device.objects.create(
                name="ip-sig-unmanaged", device_type=device_type, role=role, site=site
            )
            cls.unmanaged_iface = Interface.objects.create(
                device=cls.unmanaged_device, name="GigabitEthernet0/0", type="1000base-t"
            )

            nso_instance = NSOInstance.objects.create(name="IpSigNSO", adapter_instance_id="nso-ipsig")

            # Bypass sync_scope_to_adapter signal
            NSODeviceManagement.objects.bulk_create(
                [
                    NSODeviceManagement(
                        device=cls.device,
                        nso_instance=nso_instance,
                        nso_device_name="ip-sig-router",
                        adapter_device_id=42,
                        custom_field_data={},
                    )
                ]
            )

        def _ct(self):
            from dcim.models import Interface
            from django.contrib.contenttypes.models import ContentType

            return ContentType.objects.get_for_model(Interface)

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_post_save_creates_ip_state_accepted_and_pushes(self, mock_put):
            """Creating an IPAddress on a managed interface → state=accepted + push."""
            from ipam.models import IPAddress

            from netbox_nso_plugin.models import NSOInterfaceIPState

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="10.1.0.1/24", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )

            state = NSOInterfaceIPState.objects.get(interface=self.iface, address="10.1.0.1/24", vrf="")
            self.assertEqual(state.status, "accepted")
            self.assertIsNotNone(state.accepted_at)

            mock_put.assert_called_once()
            call_device_id, call_addresses = mock_put.call_args[0]
            self.assertEqual(call_device_id, 42)
            self.assertEqual(len(call_addresses), 1)
            self.assertEqual(call_addresses[0]["address"], "10.1.0.1/24")
            self.assertEqual(call_addresses[0]["interface"], "GigabitEthernet0/0")

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_greenfield_nokia_routed_binding_in_push(self, mock_put):
            """A parented LAG99:99 sub-interface pushes routed/parent_binding/encap_tag (M27)."""
            from dcim.models import Interface
            from ipam.models import IPAddress

            lag = Interface.objects.create(device=self.device, name="lag-99", type="lag")
            sub = Interface.objects.create(device=self.device, name="LAG99:99", type="virtual", parent=lag)

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="84.116.249.160/31", assigned_object_type=self._ct(), assigned_object_id=sub.pk
                )

            mock_put.assert_called_once()
            _, call_addresses = mock_put.call_args[0]
            entry = next(a for a in call_addresses if a["interface"] == "LAG99:99")
            self.assertTrue(entry["routed"])
            self.assertEqual(entry["parent_binding"], "lag-99")
            self.assertEqual(entry["encap_tag"], "99")

        def test_nokia_routed_binding_helper(self):
            """_nokia_routed_binding: only emits for a parented :tag interface."""
            from types import SimpleNamespace

            from netbox_nso_plugin.signals import _nokia_routed_binding

            parent = SimpleNamespace(name="lag-99")
            self.assertEqual(
                _nokia_routed_binding(SimpleNamespace(name="LAG99:99", parent=parent)),
                {"routed": True, "parent_binding": "lag-99", "encap_tag": "99"},
            )
            # no parent → not a sub-interface
            self.assertEqual(_nokia_routed_binding(SimpleNamespace(name="LAG99:99", parent=None)), {})
            # IOS/Junos dotted subif (no ':') → no-op
            self.assertEqual(_nokia_routed_binding(SimpleNamespace(name="Gi0/1.100", parent=parent)), {})
            # non-numeric suffix (e.g. VPRN logical name) → no encap tag to derive
            self.assertEqual(_nokia_routed_binding(SimpleNamespace(name="CRPD-VPN:LO7", parent=parent)), {})

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_conflict_state_blocks_push(self, mock_put):
            """Pre-existing conflict state blocks automatic acceptance and push."""
            from ipam.models import IPAddress

            from netbox_nso_plugin.models import NSOInterfaceIPState

            NSOInterfaceIPState.objects.create(
                interface=self.iface, address="10.1.1.1/24", vrf="", status="conflict", family="ipv4"
            )

            IPAddress.objects.create(
                address="10.1.1.1/24", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
            )

            state = NSOInterfaceIPState.objects.get(interface=self.iface, address="10.1.1.1/24")
            self.assertEqual(state.status, "conflict")
            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_no_management_record_skips_push(self, mock_put):
            """IPAddress on an unmanaged device → no push."""
            from ipam.models import IPAddress

            IPAddress.objects.create(
                address="192.168.99.1/24", assigned_object_type=self._ct(), assigned_object_id=self.unmanaged_iface.pk
            )

            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_post_delete_pushes_snapshot_without_deleted_ip(self, mock_put):
            """Deleting an IPAddress fires push with that address excluded."""
            from ipam.models import IPAddress

            with self.captureOnCommitCallbacks(execute=True):
                ip = IPAddress.objects.create(
                    address="10.1.2.1/30", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )
            mock_put.reset_mock()

            with self.captureOnCommitCallbacks(execute=True):
                ip.delete()

            mock_put.assert_called_once()
            call_device_id, call_addresses = mock_put.call_args[0]
            self.assertEqual(call_device_id, 42)
            self.assertFalse(
                any(a["address"] == "10.1.2.1/30" for a in call_addresses),
                "Deleted IP must not appear in the push snapshot",
            )

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_ip_not_assigned_skipped(self, mock_put):
            """IPAddress with no assignment → signal skips push."""
            from ipam.models import IPAddress

            IPAddress.objects.create(address="203.0.113.1/32")
            mock_put.assert_not_called()

        @patch("netbox_nso_plugin.adapter_client.put_ip_intent")
        def test_adapter_error_is_swallowed(self, mock_put):
            """put_ip_intent raising AdapterError is caught and does not propagate."""
            from ipam.models import IPAddress

            from netbox_nso_plugin.adapter_client import AdapterError

            mock_put.side_effect = AdapterError("down", code="nso_unreachable")

            with self.captureOnCommitCallbacks(execute=True):
                IPAddress.objects.create(
                    address="10.1.3.1/28", assigned_object_type=self._ct(), assigned_object_id=self.iface.pk
                )

    class TestGActivatedInterfaceIntentOrigin(IntentPushResetMixin, DjangoTestCase):
        """Decision-G intent signal discriminates operator edits from adapter imports."""

        @classmethod
        def setUpTestData(cls):
            from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

            from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceState

            mfg = Manufacturer.objects.create(name="GsigMfg", slug="gsigmfg")
            dt = DeviceType.objects.create(manufacturer=mfg, model="GsigDev", slug="gsigdev")
            role = DeviceRole.objects.create(name="GsigRole", slug="gsigrole")
            site = Site.objects.create(name="GsigSite", slug="gsigsite")
            cls.device = Device.objects.create(name="gsig-router", device_type=dt, role=role, site=site)
            cls.iface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")
            inst = NSOInstance.objects.create(name="GsigNSO", adapter_instance_id="nso-gsig")
            NSODeviceManagement.objects.bulk_create(
                [
                    NSODeviceManagement(
                        device=cls.device,
                        nso_instance=inst,
                        nso_device_name="gsig-router",
                        adapter_device_id=77,
                        manage_description=True,
                        manage_enabled=True,
                        custom_field_data={},
                    )
                ]
            )
            # An imported (not-yet-accepted) state the signal could promote.
            NSOInterfaceState.objects.create(
                interface=cls.iface, attribute="description", status="imported", nso_value="old"
            )

        def _fire(self, header=None):
            """Invoke the G-activated handler with current_request set to a real request.

            A non-None *header* is the adapter-import marker; it rides on a real POST
            (deliberately not a GET, so it is the import header — not the render guard —
            that suppresses the push). header=None models a programmatic write (no request).
            """
            from netbox.context import current_request

            from netbox_nso_plugin.signals import _push_intent_on_interface_edit

            req = RequestFactory().post("/", headers=header) if header is not None else None
            # Simulate the pre_save snapshot: operator changed the description,
            # left enabled untouched.
            self.iface._nso_old_values = {"description": "PREVIOUS-DESC", "enabled": self.iface.enabled}
            token = current_request.set(req)
            try:
                with patch("netbox_nso_plugin.adapter_client.put_intent") as mock_put:
                    with self.captureOnCommitCallbacks(execute=True):
                        _push_intent_on_interface_edit(None, self.iface, created=False)
                    return mock_put
            finally:
                current_request.reset(token)

        def _state(self):
            from netbox_nso_plugin.models import NSOInterfaceState

            return NSOInterfaceState.objects.get(interface=self.iface, attribute="description")

        def test_operator_edit_promotes_and_pushes(self):
            """A normal (non-adapter) edit promotes imported→accepted and pushes intent.

            (put_intent may fire more than once — the state's own post_save also
            pushes — so assert it was called, not the exact count.)
            """
            mock_put = self._fire(header=None)
            self.assertEqual(self._state().status, "accepted")
            mock_put.assert_called()

        def test_adapter_origin_edit_is_skipped(self):
            """An adapter-origin write (import header) does NOT promote or push."""
            mock_put = self._fire(header={"X-NSO-Adapter-Import": "1"})
            self.assertEqual(self._state().status, "imported")  # unchanged
            mock_put.assert_not_called()

        def test_edit_does_not_own_untouched_attribute(self):
            """Editing description must NOT promote/own the untouched 'enabled' attribute.

            Regression: previously any save promoted every managed attribute, so
            editing the description silently owned enabled (a value never accepted).
            """
            from netbox.context import current_request

            from netbox_nso_plugin.models import NSOInterfaceState
            from netbox_nso_plugin.signals import _push_intent_on_interface_edit

            enabled_state = NSOInterfaceState.objects.create(
                interface=self.iface, attribute="enabled", status="imported", nso_value="True"
            )
            # description changed; enabled untouched.
            self.iface._nso_old_values = {"description": "PREVIOUS-DESC", "enabled": self.iface.enabled}
            token = current_request.set(None)
            try:
                with patch("netbox_nso_plugin.adapter_client.put_intent"):
                    with self.captureOnCommitCallbacks(execute=True):
                        _push_intent_on_interface_edit(None, self.iface, created=False)
            finally:
                current_request.reset(token)

            enabled_state.refresh_from_db()
            self.assertEqual(enabled_state.status, "imported")
            self.assertIsNone(enabled_state.accepted_at)
            # the changed attribute (description) IS promoted
            self.assertEqual(self._state().status, "accepted")

    class TestGreenfieldOspfSignals(IntentPushResetMixin, DjangoTestCase):
        """Operator-created netbox_routing OSPF → accepted overlays + OSPF intent push."""

        @classmethod
        def setUpTestData(cls):
            from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

            from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

            mfg = Manufacturer.objects.create(name="OspfGfMfg", slug="ospfgfmfg")
            dt = DeviceType.objects.create(manufacturer=mfg, model="OspfGfDev", slug="ospfgfdev")
            role = DeviceRole.objects.create(name="OspfGfRole", slug="ospfgfrole")
            site = Site.objects.create(name="OspfGfSite", slug="ospfgfsite")
            cls.device = Device.objects.create(name="ospf-gf-rtr", device_type=dt, role=role, site=site)
            cls.iface = Interface.objects.create(device=cls.device, name="LAG99:99", type="virtual")
            nso_inst = NSOInstance.objects.create(name="OspfGfNSO", adapter_instance_id="nso-ospfgf")
            NSODeviceManagement.objects.bulk_create(
                [
                    NSODeviceManagement(
                        device=cls.device,
                        nso_instance=nso_inst,
                        nso_device_name="ospf-gf-rtr",
                        adapter_device_id=77,
                        custom_field_data={},
                    )
                ]
            )

        @patch("netbox_nso_plugin.adapter_client.put_ospf_intent")
        def test_create_ospf_iface_owns_overlays_and_pushes(self, mock_put):
            from netbox_routing.models import OSPFArea, OSPFInstance, OSPFInterface

            from netbox_nso_plugin.models import NSOOSPFInstanceState, NSOOSPFInterfaceState

            with self.captureOnCommitCallbacks(execute=True):
                inst = OSPFInstance.objects.create(
                    name="ospf-1", router_id="84.116.250.117", process_id="1", device=self.device
                )
                area = OSPFArea.objects.create(area_id="0", area_type="standard")
                OSPFInterface.objects.create(instance=inst, area=area, interface=self.iface, cost=100)

            inst_state = NSOOSPFInstanceState.objects.get(management__device=self.device, process_id="1")
            self.assertEqual(inst_state.status, "accepted")
            iface_state = NSOOSPFInterfaceState.objects.get(management__device=self.device, interface=self.iface)
            self.assertEqual(iface_state.status, "accepted")
            self.assertEqual(iface_state.area_id, "0")
            self.assertEqual(iface_state.process_id, "1")
            self.assertEqual(iface_state.cost, 100)

            self.assertTrue(mock_put.called)
            _, payload = mock_put.call_args[0]
            iface_entry = next(i for i in payload["interfaces"] if i["interface_name"] == "LAG99:99")
            self.assertEqual(iface_entry["area_id"], "0")
            self.assertEqual(iface_entry["process_id"], "1")
            self.assertEqual(iface_entry["cost"], 100)
            self.assertTrue(any(i["process_id"] == "1" for i in payload["instances"]))

except ImportError:
    pass  # Outside devcontainer — Django not available; tests skipped
