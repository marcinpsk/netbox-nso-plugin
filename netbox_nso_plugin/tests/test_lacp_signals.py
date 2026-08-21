# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for: LACP bundle intent push + accept→apply round-trip."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from .mixins import IntentPushDeliveryMixin


class _LacpBase(IntentPushDeliveryMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LacpSigMfg", slug="lacpsigmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LacpSigDev", slug="lacpsigdev")
        role = DeviceRole.objects.create(name="LacpSigRole", slug="lacpsigrole")
        site = Site.objects.create(name="LacpSigSite", slug="lacpsigsite")
        cls.device = Device.objects.create(name="lacp-sig-rtr", device_type=dt, role=role, site=site)
        cls.lag = Interface.objects.create(device=cls.device, name="Port-channel1", type="lag")
        cls.m1 = Interface.objects.create(device=cls.device, name="GigabitEthernet0/1", type="1000base-t")

    def _make_mgmt(self, adapter_device_id=42, auto_apply=False):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="lacp-sig-inst", defaults={"adapter_instance_id": "lacp-sig-inst"}
        )
        mgmt = NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-lacp-sig",
                "adapter_device_id": adapter_device_id,
                "manage_interfaces": True,
            },
        )[0]
        if mgmt.auto_apply != auto_apply:
            mgmt.auto_apply = auto_apply
            mgmt.save(update_fields=["auto_apply"])
        return mgmt

    def _bundle(self, mgmt, status="accepted"):
        from netbox_nso_plugin.models import NSOLACPBundleState

        return NSOLACPBundleState.objects.create(
            management=mgmt,
            interface=self.lag,
            lag_id=1,
            min_links=2,
            system_priority=100,
            timer="fast",
            status=status,
        )

    def _member(self, mgmt, status="accepted"):
        from netbox_nso_plugin.models import NSOLACPMemberState

        return NSOLACPMemberState.objects.create(
            management=mgmt,
            interface=self.m1,
            lag_bundle=self.lag,
            mode="active",
            port_priority=128,
            status=status,
        )


class TestPushLacpIntentForDevice(_LacpBase):
    def test_pushes_accepted_bundle_with_members(self):
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        self._bundle(mgmt, status="accepted")
        self._member(mgmt, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config") as mock_apply:
            deliver("lacp", self.device.pk, mgmt.adapter_device_id)

        mock_apply.assert_called_once()
        dev_id, bundles = mock_apply.call_args[0]
        assert dev_id == mgmt.adapter_device_id
        assert len(bundles) == 1
        b = bundles[0]
        assert b["name"] == "Port-channel1"
        assert b["min_links"] == 2
        assert b["timer"] == "fast"
        assert len(b["members"]) == 1
        assert b["members"][0]["interface_name"] == "GigabitEthernet0/1"
        assert b["members"][0]["port_priority"] == 128

    def test_excludes_non_accepted_bundles(self):
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        self._bundle(mgmt, status="imported")

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config") as mock_apply:
            deliver("lacp", self.device.pk, mgmt.adapter_device_id)

        mock_apply.assert_called_once()
        assert mock_apply.call_args[0][1] == []

    def test_excludes_vpc_sensitive_bundles(self):
        # NX-P2 belt-and-suspenders: an (impossibly-)accepted vPC bundle is excluded from the
        # push — the writer refuses the whole service on ANY vPC bundle, which would block the
        # legitimate bundles too. The Accept view already refuses it; this is defence in depth.
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOLACPBundleState

        mgmt = self._make_mgmt()
        NSOLACPBundleState.objects.create(
            management=mgmt, interface=self.lag, lag_id=1, status="accepted", vpc_sensitive=True
        )

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config") as mock_apply:
            deliver("lacp", self.device.pk, mgmt.adapter_device_id)

        mock_apply.assert_called_once()
        assert mock_apply.call_args[0][1] == []  # the vPC bundle never enters the write intent

    def test_an_adapter_failure_is_recorded_and_left_for_the_drain_to_isolate(self):
        """The swallow moved to the drain (#1503 Appendix O): the send itself fails fast."""
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSODeviceManagement

        mgmt = self._make_mgmt()
        self._bundle(mgmt, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config", side_effect=ConnectionError("boom")):
            with self.assertRaises(ConnectionError):
                deliver("lacp", self.device.pk, mgmt.adapter_device_id)

        assert "lacp" in (NSODeviceManagement.objects.get(pk=mgmt.pk).intent_push_errors or {})

    def test_an_adapter_error_is_recorded_with_its_code_and_propagates(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSODeviceManagement

        mgmt = self._make_mgmt()
        self._bundle(mgmt, status="accepted")
        error = AdapterError("rejected", code="validation_error")

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config", side_effect=error):
            with self.assertRaises(AdapterError) as raised:
                deliver("lacp", self.device.pk, mgmt.adapter_device_id)

        assert raised.exception is error
        errors = NSODeviceManagement.objects.get(pk=mgmt.pk).intent_push_errors or {}
        assert errors["lacp"]["code"] == "validation_error"


class TestOnLacpStateSave(_LacpBase):
    def test_save_triggers_intent_push_in_auto_apply(self):
        """In auto-apply mode, accept commits to the device immediately."""
        from netbox_nso_plugin.models import NSOLACPBundleState
        from netbox_nso_plugin.signals import _on_lacp_state_save

        mgmt = self._make_mgmt(auto_apply=True)
        # Unsaved instance (mirrors the L2 SAP signal test) so the real post_save
        # doesn't schedule a push outside the captured on-commit block.
        bundle = NSOLACPBundleState(management=mgmt, interface=self.lag, lag_id=1, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config") as mock_apply:
            with self.captureOnCommitCallbacks(execute=True):
                _on_lacp_state_save(sender=NSOLACPBundleState, instance=bundle)
            mock_apply.assert_called_once()
            assert mock_apply.call_args[0][0] == mgmt.adapter_device_id

    def test_save_no_push_without_auto_apply(self):
        """Default (deferred) flow: accept marks owned but does NOT commit — the
        single device Apply commits later."""
        from netbox_nso_plugin.models import NSOLACPBundleState
        from netbox_nso_plugin.signals import _on_lacp_state_save

        mgmt = self._make_mgmt(auto_apply=False)
        bundle = NSOLACPBundleState(management=mgmt, interface=self.lag, lag_id=1, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config") as mock_apply:
            with self.captureOnCommitCallbacks(execute=True):
                _on_lacp_state_save(sender=NSOLACPBundleState, instance=bundle)
            mock_apply.assert_not_called()

    def test_no_push_without_adapter_device_id(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOLACPBundleState
        from netbox_nso_plugin.signals import _on_lacp_state_save

        inst, _ = NSOInstance.objects.get_or_create(
            name="lacp-noid-inst", defaults={"adapter_instance_id": "lacp-noid-inst"}
        )
        dt = DeviceType.objects.get(slug="lacpsigdev")
        role = DeviceRole.objects.get(slug="lacpsigrole")
        site = Site.objects.get(slug="lacpsigsite")
        dev = Device.objects.create(name="lacp-noid-rtr", device_type=dt, role=role, site=site)
        lag = Interface.objects.create(device=dev, name="Port-channel1", type="lag")
        mgmt = NSODeviceManagement.objects.create(
            device=dev, nso_instance=inst, nso_device_name="nso-lacp-noid", adapter_device_id=None
        )
        bundle = NSOLACPBundleState(management=mgmt, interface=lag, lag_id=1, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.apply_lag_config") as mock_apply:
            _on_lacp_state_save(sender=NSOLACPBundleState, instance=bundle)
            mock_apply.assert_not_called()


class TestLacpAcceptView(_LacpBase):
    def test_accept_marks_owned(self):
        from netbox_nso_plugin.models import NSOLACPMemberState

        mgmt = self._make_mgmt()
        bundle = self._bundle(mgmt, status="changed")
        self._member(mgmt, status="changed")

        self.client.force_login(_superuser())
        url = f"/plugins/nso/lacp/bundle-state/{bundle.pk}/accept/"
        with patch("netbox_nso_plugin.adapter_client.apply_lag_config"):
            resp = self.client.post(url)
        assert resp.status_code == 302
        bundle.refresh_from_db()
        assert bundle.status == "accepted"
        assert bundle.accepted_at is not None
        member = NSOLACPMemberState.objects.get(interface=self.m1)
        assert member.status == "accepted"
        assert member.accepted_at is not None


def _superuser():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    existing = User.objects.filter(username="lacp-admin").first()
    if existing:
        return existing
    return User.objects.create_superuser(username="lacp-admin", password="pw", email="lacp@test.x")  # noqa: S106
