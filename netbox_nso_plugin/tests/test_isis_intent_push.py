# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Regression: IS-IS instance intent push must not crash on the (not-yet-imported)
auth-key fields. The push references area_auth_key/domain_auth_key, which the read
overlay NSOISISInstanceState does not have yet — it must degrade to None, not raise.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase


class TestIsisIntentPush(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="IiMfg", slug="iimfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IiDev", slug="iidev")
        role = DeviceRole.objects.create(name="IiRole", slug="iirole")
        site = Site.objects.create(name="IiSite", slug="iisite")
        cls.device = Device.objects.create(name="ii-router", device_type=dt, role=role, site=site)

    def test_push_does_not_crash_without_auth_key_fields(self):
        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOISISInstanceState
        from netbox_nso_plugin.signals import _push_isis_intent_for_device, suppress_intent_push

        inst, _ = NSOInstance.objects.get_or_create(name="ii-inst", defaults={"adapter_instance_id": "ii-inst"})
        mgmt = NSODeviceManagement.objects.create(
            device=self.device, nso_instance=inst, nso_device_name="ii-dev", adapter_device_id=self.device.pk
        )
        # Create within suppression so the save() itself doesn't push.
        with suppress_intent_push():
            NSOISISInstanceState.objects.create(
                management=mgmt, process_tag="", net="49.0001.00", is_type="level-2-only", status="in_sync"
            )

        captured = {}

        def _fake_put(adapter_id, interfaces, processes=None):
            captured["processes"] = processes

        orig = adapter_client.put_isis_interface_intent
        adapter_client.put_isis_interface_intent = MagicMock(side_effect=_fake_put)
        try:
            _push_isis_intent_for_device(mgmt.device_id, mgmt.adapter_device_id)  # must not raise
        finally:
            adapter_client.put_isis_interface_intent = orig

        assert "processes" in captured and len(captured["processes"]) == 1
        proc = captured["processes"][0]
        assert proc["net"] == "49.0001.00"
        assert proc["area_auth_key"] is None  # degraded, not crashed
        assert proc["domain_auth_key"] is None
