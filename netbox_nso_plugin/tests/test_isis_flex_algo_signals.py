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


class TestFlexAlgoOwnershipSignals(_FlexAlgoBase):
    def test_foreign_flex_algo_create_is_neutral(self):
        from netbox_routing.models import ISISFlexAlgo

        from netbox_nso_plugin.models import NSOISISFlexAlgoState

        self._mgmt()
        inst = self._isis_instance(process_tag="CORE")

        with patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                ISISFlexAlgo.objects.create(instance=inst, algo_id=130, metric_type="delay-metric", priority=200)

        assert not NSOISISFlexAlgoState.objects.filter(algo_id=130).exists()
        mock_put.assert_not_called()

    def test_foreign_flex_algo_delete_is_neutral(self):
        from netbox_routing.models import ISISFlexAlgo

        from netbox_nso_plugin.models import NSOISISFlexAlgoState

        self._mgmt()
        inst = self._isis_instance(process_tag="CORE")

        fa = ISISFlexAlgo.objects.create(instance=inst, algo_id=130)
        NSOISISFlexAlgoState.objects.create(
            management=self._mgmt(),
            process_tag="CORE",
            algo_id=130,
            isis_flex_algo=fa,
            status="accepted",
        )

        with patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                fa.delete()

        state = NSOISISFlexAlgoState.objects.get(algo_id=130)
        assert state.isis_flex_algo_id is None
        mock_put.assert_not_called()

    def test_exact_overlay_write_pushes_and_records_ownership(self):
        from netbox_routing.models import ISISFlexAlgo

        from netbox_nso_plugin.models import NSOISISFlexAlgoState, NSOOwnershipManifest
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        mgmt = self._mgmt()
        native = ISISFlexAlgo.objects.create(instance=self._isis_instance(process_tag="CORE"), algo_id=130)
        state = NSOISISFlexAlgoState(
            management=mgmt,
            process_tag="CORE",
            algo_id=130,
            isis_flex_algo=native,
            status="accepted",
        )
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(
                    state,
                    force_insert=True,
                    natural_key=("management", "process_tag", "algo_id"),
                ),
            )
        )

        with patch("netbox_nso_plugin.adapter_client.put_isis_flex_algo_intent") as mock_put:
            with self.captureOnCommitCallbacks(execute=True):
                with renderer_writes(plan) as writer:
                    writer.save(state, force_insert=True)

        mock_put.assert_called_once()
        assert NSOOwnershipManifest.objects.filter(
            device_id=self.device.pk,
            scope="isis_flex_algo",
            native_model_label="netbox_routing.isisflexalgo",
            native_key={"instance_id": native.instance_id, "algo_id": 130},
            ownership_state="owned",
        ).exists()
