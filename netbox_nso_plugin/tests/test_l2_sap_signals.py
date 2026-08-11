# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for: Nokia L2 SAP intent push signals."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin


class TestPushL2SapIntentForDevice(IntentPushResetMixin, TestCase):
    """Unit tests for _push_l2_sap_intent_for_device."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="L2SigMfg", slug="l2sigmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="L2SigDev", slug="l2sigdev")
        role = DeviceRole.objects.create(name="L2SigRole", slug="l2sigrole")
        site = Site.objects.create(name="L2SigSite", slug="l2sigsite")
        cls.device = Device.objects.create(name="l2-sig-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self, adapter_device_id=42):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="l2-sig-inst",
            defaults={"adapter_instance_id": "l2-sig-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-l2-sig",
                "adapter_device_id": adapter_device_id,
                "manage_l2": True,
            },
        )[0]

    def _make_state(self, mgmt, service_name="TL", sap_id="lag-60:3999", status="accepted"):
        from netbox_nso_plugin.models import NSOL2SapState

        return NSOL2SapState.objects.create(
            management=mgmt,
            service_name=service_name,
            service_type="epipe",
            sap_id=sap_id,
            port="lag-60",
            outer_tag=3999,
            status=status,
        )

    def test_pushes_accepted_saps(self):
        """Accepted SAPs are included in the intent push payload."""
        from netbox_nso_plugin.signals import _push_l2_sap_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent") as mock_push:
            _push_l2_sap_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            args = mock_push.call_args[0]
            assert args[0] == mgmt.adapter_device_id
            saps = args[1]
            assert len(saps) == 1
            assert saps[0]["service_name"] == "TL"
            assert saps[0]["service_type"] == "epipe"
            assert saps[0]["sap_id"] == "lag-60:3999"
            assert saps[0]["outer_tag"] == 3999

    def test_excludes_non_accepted_saps(self):
        """SAPs with status=imported are excluded from the intent push."""
        from netbox_nso_plugin.signals import _push_l2_sap_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, sap_id="lag-60:1", status="imported")

        with patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent") as mock_push:
            _push_l2_sap_intent_for_device(self.device.pk, mgmt.adapter_device_id)

            mock_push.assert_called_once()
            assert mock_push.call_args[0][1] == []

    def test_adapter_error_is_swallowed(self):
        """AdapterError during push is logged but does not propagate."""
        from netbox_nso_plugin.signals import _push_l2_sap_intent_for_device

        mgmt = self._make_mgmt()
        self._make_state(mgmt, status="accepted")

        with patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent", side_effect=Exception("boom")):
            _push_l2_sap_intent_for_device(self.device.pk, mgmt.adapter_device_id)


class TestOnL2SapStateSave(IntentPushDeliveryMixin, TestCase):
    """Tests for _on_l2_sap_state_save signal handler."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="L2SaveMfg", slug="l2savemfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="L2SaveDev", slug="l2savedev")
        role = DeviceRole.objects.create(name="L2SaveRole", slug="l2saverole")
        site = Site.objects.create(name="L2SaveSite", slug="l2savesite")
        cls.device = Device.objects.create(name="l2-save-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(
            name="l2-save-inst",
            defaults={"adapter_instance_id": "l2-save-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-l2-save",
                "adapter_device_id": 99,
                "manage_l2": True,
            },
        )[0]

    def test_save_triggers_intent_push(self):
        """Saving NSOL2SapState triggers put_l2_sap_intent."""
        from netbox_nso_plugin.models import NSOL2SapState
        from netbox_nso_plugin.signals import _on_l2_sap_state_save

        mgmt = self._make_mgmt()

        with patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent") as mock_push:
            state = NSOL2SapState(
                management=mgmt,
                service_name="TL",
                service_type="epipe",
                sap_id="lag-60:3999",
                port="lag-60",
                status="accepted",
            )
            with self.captureOnCommitCallbacks(execute=True):
                _on_l2_sap_state_save(sender=NSOL2SapState, instance=state)
            mock_push.assert_called_once()
            assert mock_push.call_args[0][0] == 99

    def test_no_push_when_no_adapter_device_id(self):
        """No push when management.adapter_device_id is None."""
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOL2SapState
        from netbox_nso_plugin.signals import _on_l2_sap_state_save

        inst, _ = NSOInstance.objects.get_or_create(
            name="l2-noid-inst",
            defaults={"adapter_instance_id": "l2-noid-inst"},
        )
        dt = DeviceType.objects.get(slug="l2savedev")
        role = DeviceRole.objects.get(slug="l2saverole")
        site = Site.objects.get(slug="l2savesite")
        extra_dev = Device.objects.create(name="l2-noid-router", device_type=dt, role=role, site=site)
        mgmt = NSODeviceManagement.objects.create(
            device=extra_dev,
            nso_instance=inst,
            nso_device_name="nso-l2-noid",
            adapter_device_id=None,
        )
        state = NSOL2SapState(management=mgmt, service_name="TL", service_type="epipe", sap_id="x:1", status="accepted")

        with patch("netbox_nso_plugin.adapter_client.put_l2_sap_intent") as mock_push:
            _on_l2_sap_state_save(sender=NSOL2SapState, instance=state)
            mock_push.assert_not_called()
