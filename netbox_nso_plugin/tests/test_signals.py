# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for signal handlers in signals.py.

Handlers are called directly with mocked model instances so that no Django
database is required.  adapter_client functions are patched at source.

Django DB integration tests (TestIPAddressSignals) use real Django models and
trigger signals via normal IPAddress CRUD to verify the full IP intent push path.
"""

import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from .mixins import IntentPushResetMixin

# A non-None accepted_at marks a state as OWNED (push_intent_on_accept triggers on it).
_ACCEPTED_AT = datetime(2025, 1, 1, 12, 0, 0)


def _make_mgmt_class():
    """Return a fake NSODeviceManagement *class* whose class-level `objects` is a MagicMock.

    signals.sync_scope_to_adapter does ``type(instance).objects.filter(pk=…).update(…)``.
    When instance is a plain MagicMock, ``type(instance)`` is the real MagicMock class
    which has no `objects` attribute, causing AttributeError.  Using a custom class avoids this.
    """
    return type(
        "FakeNSODeviceManagement",
        (),
        {"objects": MagicMock(), "DoesNotExist": type("DoesNotExist", (Exception,), {})},
    )


def _make_mgmt_instance(
    *,
    pk=1,
    adapter_device_id=None,
    device_id=42,
    manage_description=True,
    manage_enabled=False,
    auto_apply=False,
):
    """Return a fake NSODeviceManagement instance with all attrs needed by signals."""
    cls = _make_mgmt_class()
    inst = object.__new__(cls)
    inst.pk = pk
    inst.adapter_device_id = adapter_device_id
    inst.device_id = device_id
    inst.nso_device_name = "core-rtr-01"
    inst.auto_apply = auto_apply

    nso_inst = MagicMock()
    nso_inst.adapter_instance_id = "nso-prod"
    inst.nso_instance = nso_inst

    inst.managed_attributes = []
    if manage_description:
        inst.managed_attributes.append("description")
    if manage_enabled:
        inst.managed_attributes.append("enabled")
    return inst


def _fake_models_module(mgmt_cls=None, istate_cls=None):
    """Return a MagicMock to substitute for netbox_nso_plugin.models.

    push_intent_on_accept does ``from .models import NSODeviceManagement, NSOInterfaceState``
    inside the function body.  Importing the real module fails outside the devcontainer
    because Django model registration requires INSTALLED_APPS.  Injecting a fake module
    via patch.dict(sys.modules, …) intercepts the import at runtime.
    """
    mod = MagicMock()
    mod.NSODeviceManagement = mgmt_cls or MagicMock()
    mod.NSOInterfaceState = istate_cls or MagicMock()
    return mod


_MOD = "netbox_nso_plugin.adapter_client"


class TestSyncScopeToAdapter(unittest.TestCase):
    """Tests for sync_scope_to_adapter signal handler."""

    def test_created_onboards_device_and_sets_scope(self):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        instance = _make_mgmt_instance()
        # type(instance).objects.filter(...).update(...) uses MagicMock chain — works automatically

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 99}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={"device_id": 99}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value={"job_id": 5}) as mock_notify,
            patch(f"{_MOD}.patch_device") as mock_patch,
        ):
            sync_scope_to_adapter(sender=MagicMock(), instance=instance, created=True)

            mock_onboard.assert_called_once_with(
                nso_instance="nso-prod",
                nso_device_name="core-rtr-01",
                netbox_device_id=42,
            )
            mock_scope.assert_called_once_with(99, ["description"], auto_apply=False)
            mock_notify.assert_called_once_with(99)
            mock_patch.assert_not_called()

    def test_update_patches_device_and_sets_scope(self):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        instance = _make_mgmt_instance(adapter_device_id=7)

        with (
            patch(f"{_MOD}.patch_device", return_value=None) as mock_patch,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
            patch(f"{_MOD}.onboard_device") as mock_onboard,
        ):
            sync_scope_to_adapter(sender=MagicMock(), instance=instance, created=False)

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

        instance = _make_mgmt_instance()

        with patch(f"{_MOD}.onboard_device", side_effect=AdapterError("down", code="nso_unreachable")):
            # Should not raise — warning is logged instead
            sync_scope_to_adapter(sender=MagicMock(), instance=instance, created=True)

    def test_sync_notify_job_logged(self):
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        instance = _make_mgmt_instance()

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 10}),
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value={"job_id": 7}),
        ):
            with self.assertLogs("netbox_nso_plugin.signals", level="DEBUG"):
                sync_scope_to_adapter(sender=MagicMock(), instance=instance, created=True)

    def test_created_none_adapter_id_triggers_onboard(self):
        """created=False but adapter_device_id=None should also trigger onboard."""
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        instance = _make_mgmt_instance(adapter_device_id=None)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 3}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={}),
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            sync_scope_to_adapter(sender=MagicMock(), instance=instance, created=False)

        mock_onboard.assert_called_once()

    def test_manage_enabled_included_in_scope(self):
        """manage_enabled=True includes 'enabled' in the managed_attributes scope call."""
        from netbox_nso_plugin.signals import sync_scope_to_adapter

        instance = _make_mgmt_instance(manage_enabled=True)

        with (
            patch(f"{_MOD}.onboard_device", return_value={"id": 5}) as mock_onboard,
            patch(f"{_MOD}.set_scope", return_value={}) as mock_scope,
            patch(f"{_MOD}.sync_notify", return_value=None),
        ):
            sync_scope_to_adapter(sender=MagicMock(), instance=instance, created=True)

        mock_onboard.assert_called_once()
        # Both description and enabled should be in the scope call
        mock_scope.assert_called_once_with(5, ["description", "enabled"], auto_apply=False)


class TestOffboardDeviceFromAdapter(unittest.TestCase):
    """Tests for offboard_device_from_adapter signal handler."""

    def test_offboards_when_adapter_device_id_set(self):
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = MagicMock()
        instance.adapter_device_id = 55

        with patch(f"{_MOD}.delete_device") as mock_delete:
            offboard_device_from_adapter(sender=MagicMock(), instance=instance)

        mock_delete.assert_called_once_with(55)

    def test_skips_when_adapter_device_id_none(self):
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = MagicMock()
        instance.adapter_device_id = None

        with patch(f"{_MOD}.delete_device") as mock_delete:
            offboard_device_from_adapter(sender=MagicMock(), instance=instance)

        mock_delete.assert_not_called()

    def test_adapter_error_swallowed(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.signals import offboard_device_from_adapter

        instance = MagicMock()
        instance.adapter_device_id = 5

        with patch(f"{_MOD}.delete_device", side_effect=AdapterError("gone", code="not_found")):
            offboard_device_from_adapter(sender=MagicMock(), instance=instance)


class TestPushIntentOnAccept(IntentPushResetMixin, unittest.TestCase):
    """Tests for push_intent_on_accept signal handler."""

    def test_skips_when_not_owned(self):
        """A not-owned row (accepted_at is None) does not push, whatever its sync status."""
        from netbox_nso_plugin.signals import push_intent_on_accept

        instance = MagicMock()
        instance.status = "imported"
        instance.accepted_at = None

        with patch(f"{_MOD}.put_intent") as mock_put:
            push_intent_on_accept(sender=MagicMock(), instance=instance)
            mock_put.assert_not_called()

    def test_pushes_intent_on_accepted(self):
        from netbox_nso_plugin.signals import push_intent_on_accept

        iface = MagicMock()
        iface.device_id = 42
        iface.name = "GigabitEthernet0/0"
        iface.description = "uplink"
        iface.enabled = True

        state = MagicMock()
        state.status = "accepted"
        state.interface = iface
        state.attribute = "description"
        state.accepted_at = _ACCEPTED_AT

        mgmt = MagicMock()
        mgmt.adapter_device_id = 7

        mock_mgmt_cls = MagicMock()
        mock_mgmt_cls.objects.get.return_value = mgmt
        mock_mgmt_cls.DoesNotExist = Exception

        mock_istate_cls = MagicMock()
        mock_istate_cls.objects.filter.return_value.select_related.return_value = [state]

        fake_models = _fake_models_module(mock_mgmt_cls, mock_istate_cls)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch(f"{_MOD}.put_intent") as mock_put,
        ):
            push_intent_on_accept(sender=MagicMock(), instance=state)

        mock_put.assert_called_once()
        call_args = mock_put.call_args[0]
        self.assertEqual(call_args[0], 7)
        attrs = call_args[1]
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["interface"], "GigabitEthernet0/0")
        self.assertEqual(attrs[0]["attribute"], "description")
        self.assertEqual(attrs[0]["intent_value"], "uplink")

    def test_pushes_enabled_attribute(self):
        from netbox_nso_plugin.signals import push_intent_on_accept

        iface = MagicMock()
        iface.device_id = 42
        iface.name = "Loopback0"
        iface.enabled = False

        state = MagicMock()
        state.status = "accepted"
        state.interface = iface
        state.attribute = "enabled"
        state.accepted_at = _ACCEPTED_AT

        mgmt = MagicMock()
        mgmt.adapter_device_id = 3

        mock_mgmt_cls = MagicMock()
        mock_mgmt_cls.objects.get.return_value = mgmt
        mock_mgmt_cls.DoesNotExist = Exception

        mock_istate_cls = MagicMock()
        mock_istate_cls.objects.filter.return_value.select_related.return_value = [state]

        fake_models = _fake_models_module(mock_mgmt_cls, mock_istate_cls)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch(f"{_MOD}.put_intent") as mock_put,
        ):
            push_intent_on_accept(sender=MagicMock(), instance=state)

        attrs = mock_put.call_args[0][1]
        self.assertEqual(attrs[0]["intent_value"], "false")

    def test_skips_when_mgmt_does_not_exist(self):
        from netbox_nso_plugin.signals import push_intent_on_accept

        state = MagicMock()
        state.status = "accepted"
        state.interface.device_id = 99

        mock_mgmt_cls = MagicMock()
        mock_mgmt_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_mgmt_cls.objects.get.side_effect = mock_mgmt_cls.DoesNotExist("not found")

        fake_models = _fake_models_module(mock_mgmt_cls)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch(f"{_MOD}.put_intent") as mock_put,
        ):
            push_intent_on_accept(sender=MagicMock(), instance=state)

        mock_put.assert_not_called()

    def test_skips_unknown_attribute(self):
        """state.attribute not in (description, enabled) hits the 'continue' branch."""
        from netbox_nso_plugin.signals import push_intent_on_accept

        iface = MagicMock()
        iface.device_id = 42
        iface.name = "GigabitEthernet0/0"

        state = MagicMock()
        state.status = "accepted"
        state.interface = iface
        state.attribute = "custom_field"  # unknown — should be skipped
        state.accepted_at = _ACCEPTED_AT

        mgmt = MagicMock()
        mgmt.adapter_device_id = 7

        mock_mgmt_cls = MagicMock()
        mock_mgmt_cls.objects.get.return_value = mgmt
        mock_mgmt_cls.DoesNotExist = Exception

        mock_istate_cls = MagicMock()
        mock_istate_cls.objects.filter.return_value.select_related.return_value = [state]

        fake_models = _fake_models_module(mock_mgmt_cls, mock_istate_cls)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch(f"{_MOD}.put_intent") as mock_put,
        ):
            push_intent_on_accept(sender=MagicMock(), instance=state)

        # put_intent called with empty attributes list (unknown attribute was skipped)
        attrs = mock_put.call_args[0][1]
        self.assertEqual(attrs, [])

    def test_put_intent_error_is_swallowed(self):
        """put_intent raising AdapterError is caught and logged."""
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.signals import push_intent_on_accept

        iface = MagicMock()
        iface.device_id = 42
        iface.name = "Loopback0"
        iface.description = "test"

        state = MagicMock()
        state.status = "accepted"
        state.interface = iface
        state.attribute = "description"
        state.accepted_at = _ACCEPTED_AT

        mgmt = MagicMock()
        mgmt.adapter_device_id = 3

        mock_mgmt_cls = MagicMock()
        mock_mgmt_cls.objects.get.return_value = mgmt
        mock_mgmt_cls.DoesNotExist = Exception

        mock_istate_cls = MagicMock()
        mock_istate_cls.objects.filter.return_value.select_related.return_value = [state]

        fake_models = _fake_models_module(mock_mgmt_cls, mock_istate_cls)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch(f"{_MOD}.put_intent", side_effect=AdapterError("down", code="nso_unreachable")),
        ):
            # should not raise — warning is logged instead
            push_intent_on_accept(sender=MagicMock(), instance=state)
        from netbox_nso_plugin.signals import push_intent_on_accept

        state = MagicMock()
        state.status = "accepted"
        state.interface.device_id = 42

        mgmt = MagicMock()
        mgmt.adapter_device_id = None

        mock_mgmt_cls = MagicMock()
        mock_mgmt_cls.objects.get.return_value = mgmt
        mock_mgmt_cls.DoesNotExist = Exception

        fake_models = _fake_models_module(mock_mgmt_cls)

        with (
            patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
            patch(f"{_MOD}.put_intent") as mock_put,
        ):
            push_intent_on_accept(sender=MagicMock(), instance=state)

        mock_put.assert_not_called()


class TestSkipOnRenderGuard(IntentPushResetMixin, unittest.TestCase):
    """An intent push must never fire during a GET render.

    Regression for the device-27 NSO-tab loop: rendering the tab re-saves every
    NSOInterfaceState row, and each save of an 'accepted' row pushed the full intent
    snapshot — O(N) pushes per render, each O(N) — hanging the page and re-minting
    accepts. The @_skip_on_render guard drops the push when current_request is a GET.
    """

    def _fire_with_method(self, method):
        from netbox.context import current_request

        from netbox_nso_plugin.signals import push_intent_on_accept

        iface = MagicMock()
        iface.device_id = 42
        iface.name = "GigabitEthernet0/0"
        iface.description = "uplink"
        iface.enabled = True

        state = MagicMock()
        state.status = "accepted"
        state.interface = iface
        state.attribute = "description"
        state.accepted_at = _ACCEPTED_AT

        mgmt = MagicMock()
        mgmt.adapter_device_id = 7
        mock_mgmt_cls = MagicMock()
        mock_mgmt_cls.objects.get.return_value = mgmt
        mock_mgmt_cls.DoesNotExist = Exception
        mock_istate_cls = MagicMock()
        mock_istate_cls.objects.filter.return_value.select_related.return_value = [state]
        fake_models = _fake_models_module(mock_mgmt_cls, mock_istate_cls)

        req = None
        if method is not None:
            req = MagicMock()
            req.method = method
        token = current_request.set(req)
        try:
            with (
                patch.dict(sys.modules, {"netbox_nso_plugin.models": fake_models}),
                patch(f"{_MOD}.put_intent") as mock_put,
            ):
                push_intent_on_accept(sender=MagicMock(), instance=state)
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
            """Invoke the G-activated handler with an optional request header set."""
            from netbox.context import current_request

            from netbox_nso_plugin.signals import _push_intent_on_interface_edit

            req = None
            if header is not None:
                from unittest.mock import MagicMock

                req = MagicMock()
                req.headers = header
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

except ImportError:
    pass  # Outside devcontainer — Django not available; tests skipped
