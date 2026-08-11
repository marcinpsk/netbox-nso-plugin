# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""IS-IS instance intent push carries the area/domain auth keys.

The overlay NSOISISInstanceState now holds area_auth_key/domain_auth_key, so the
push sends a held key and maps an empty (unset) key to None — never a literal "".
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from .mixins import IntentPushResetMixin


class TestIsisIntentPush(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="IiMfg", slug="iimfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IiDev", slug="iidev")
        role = DeviceRole.objects.create(name="IiRole", slug="iirole")
        site = Site.objects.create(name="IiSite", slug="iisite")
        cls.device = Device.objects.create(name="ii-router", device_type=dt, role=role, site=site)

    def _push_and_capture(self, **state_kwargs):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOISISInstanceState
        from netbox_nso_plugin.signals import suppress_intent_push

        inst, _ = NSOInstance.objects.get_or_create(name="ii-inst", defaults={"adapter_instance_id": "ii-inst"})
        mgmt = NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "ii-dev", "adapter_device_id": self.device.pk},
        )[0]
        # Create within suppression so the save() itself doesn't push.
        with suppress_intent_push():
            NSOISISInstanceState.objects.update_or_create(
                management=mgmt,
                process_tag="",
                defaults={"net": "49.0001.00", "is_type": "level-2-only", "status": "in_sync", **state_kwargs},
            )

        captured = {}

        def _fake_put(adapter_id, interfaces, processes=None):
            captured["processes"] = processes

        orig = adapter_client.put_isis_interface_intent
        # _fake_put is a real function that records the pushed processes; nothing
        # inspects a mock here, so assign it directly (no MagicMock wrapper needed).
        adapter_client.put_isis_interface_intent = _fake_put
        try:
            deliver("isis", mgmt.device_id, mgmt.adapter_device_id)
        finally:
            adapter_client.put_isis_interface_intent = orig
        assert "processes" in captured and len(captured["processes"]) == 1
        return captured["processes"][0]

    def test_levels_pushed_from_linked_instance(self):
        """Per-level tuning rides the process intent: the fork ISISLevel rows of the
        state's linked instance land as 'levels' (None fields omitted per entry) —
        a level is accepted with its process."""
        from netbox_routing.models import ISISInstance, ISISLevel

        fork_inst = ISISInstance.objects.create(device=self.device, process_tag="", net="49.0001.00")
        ISISLevel.objects.create(instance=fork_inst, level=2, wide_metrics_only=True, labeled_preference=7)
        ISISLevel.objects.create(instance=fork_inst, level=1, disabled=True)
        proc = self._push_and_capture(isis_instance=fork_inst)
        assert proc["levels"] == [
            {"level": 1, "disabled": True},
            {"level": 2, "wide_metrics_only": True, "labeled_preference": 7},
        ]

    def test_no_linked_instance_pushes_no_levels(self):
        proc = self._push_and_capture()
        assert "levels" not in proc

    def test_unset_key_pushed_as_none(self):
        proc = self._push_and_capture()
        assert proc["net"] == "49.0001.00"
        assert proc["area_auth_key"] is None  # empty "" → None, never literal ""
        assert proc["domain_auth_key"] is None

    def test_held_key_is_pushed(self):
        proc = self._push_and_capture(area_auth_key="s3cret-area", domain_auth_key="s3cret-domain")
        assert proc["area_auth_key"] == "s3cret-area"
        assert proc["domain_auth_key"] == "s3cret-domain"
