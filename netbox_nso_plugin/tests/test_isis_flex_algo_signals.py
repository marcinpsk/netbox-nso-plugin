# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""IS-IS Flex-Algo intent push + greenfield accept→apply signals."""

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from .mixins import IntentPushDeliveryMixin


class _FlexAlgoBase(IntentPushDeliveryMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="FaMfg", slug="famfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="FaDev", slug="fadev")
        role = DeviceRole.objects.create(name="FaRole", slug="farole")
        site = Site.objects.create(name="FaSite", slug="fasite")
        cls.device = Device.objects.create(name="fa-router", device_type=dt, role=role, site=site)

    def _mgmt(self, adapter_device_id=77):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="fa-inst", defaults={"adapter_instance_id": "fa-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-fa",
                "adapter_device_id": adapter_device_id,
            },
        )[0]

    def _isis_instance(self, process_tag="CORE"):
        from netbox_routing.models import ISISInstance

        return ISISInstance.objects.create(device=self.device, process_tag=process_tag, net="49.0001.0001.0001.0001.00")


class TestPushIsisFlexAlgoIntent(_FlexAlgoBase):
    def test_push_builds_snapshot_from_accepted_states(self):
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOISISFlexAlgoState

        mgmt = self._mgmt()
        NSOISISFlexAlgoState.objects.create(
            management=mgmt,
            process_tag="CORE",
            algo_id=130,
            metric_type="delay-metric",
            priority=200,
            status="accepted",
        )
        # An imported (not-owned) row must NOT be pushed.
        NSOISISFlexAlgoState.objects.create(
            management=mgmt,
            process_tag="CORE",
            algo_id=140,
            status="imported",
        )

        with patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent") as mock_put:
            deliver("isis_flex_algo", mgmt.device_id, mgmt.adapter_device_id)

        mock_put.assert_called_once()
        _, flex_algos = mock_put.call_args[0]
        assert len(flex_algos) == 1
        assert flex_algos[0]["algo_id"] == 130
        assert flex_algos[0]["process_tag"] == "CORE"
        assert flex_algos[0]["metric_type"] == "delay-metric"
        assert flex_algos[0]["priority"] == 200


class TestGreenfieldFlexAlgo(_FlexAlgoBase):
    def test_create_flex_algo_owns_and_pushes(self):
        from netbox_routing.models import ISISFlexAlgo

        from netbox_nso_plugin.models import NSOISISFlexAlgoState

        self._mgmt()
        inst = self._isis_instance(process_tag="CORE")

        with patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                ISISFlexAlgo.objects.create(instance=inst, algo_id=130, metric_type="delay-metric", priority=200)

        state = NSOISISFlexAlgoState.objects.get(algo_id=130)
        assert state.status == "accepted"
        assert state.process_tag == "CORE"
        assert state.metric_type == "delay-metric"
        mock_put.assert_called()
        _, flex_algos = mock_put.call_args[0]
        assert flex_algos[0]["algo_id"] == 130

    def test_delete_flex_algo_drops_overlay_and_pushes_removal(self):
        from netbox_routing.models import ISISFlexAlgo

        from netbox_nso_plugin.models import NSOISISFlexAlgoState

        self._mgmt()
        inst = self._isis_instance(process_tag="CORE")

        with patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent"):
            with self.captureOnCommitCallbacks(execute=True):
                fa = ISISFlexAlgo.objects.create(instance=inst, algo_id=130)
        assert NSOISISFlexAlgoState.objects.filter(algo_id=130).exists()

        with patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                fa.delete()

        assert not NSOISISFlexAlgoState.objects.filter(algo_id=130).exists()
        # Removal push sends the now-empty snapshot.
        mock_put.assert_called()
        _, flex_algos = mock_put.call_args[0]
        assert flex_algos == []
