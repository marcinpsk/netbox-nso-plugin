# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/interfaces.

Pins the JSON shape the plugin CONSUMES in
``template_content._upsert_interface_states`` against the documented adapter contract.
The adapter is the producer; if it renames/removes a key the plugin depends on, the
plugin silently degrades (missing ``status`` -> ``"unknown"``) — exactly the device-27
class of "looks fine, is wrong" bug. This test plus its adapter mirror make that break
visible on at least one side.

Canonical contract: ``nso-adapter/docs/api-contract.md`` §
"GET /api/v1/devices/{id}/interfaces".
Mirror (producer side): ``nso-adapter/tests/api/test_contract_interfaces.py`` — the
``EXPECTED_*_KEYS`` sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.models import NSOInterfaceState
from netbox_nso_plugin.template_content import _upsert_interface_states

# Must match nso-adapter/tests/api/test_contract_interfaces.py exactly.
EXPECTED_IFACE_KEYS = {"name", "netbox_interface_id", "attrs"}
EXPECTED_ATTR_KEYS = {
    "nso_value",
    "netbox_value",
    "intent_value",
    "status",
    "last_apply_at",
    "last_apply_error",
}

# One interface exactly as docs/api-contract.md documents the adapter emitting it.
CONTRACT_PAYLOAD = [
    {
        "name": "GE0/0",
        "netbox_interface_id": 1000,
        "attrs": {
            "description": {
                "nso_value": "uplink to spine-1",
                "netbox_value": "uplink to spine-1",
                "intent_value": "uplink to spine-1",
                "status": "apply_failed",
                "last_apply_at": "2026-05-20T10:00:00Z",
                "last_apply_error": {"code": "nso_error", "message": "boom"},
            },
            "enabled": {
                "nso_value": "true",
                "netbox_value": "true",
                "intent_value": "true",
                "status": "in_sync",
                "last_apply_at": None,
                "last_apply_error": None,
            },
        },
    }
]


class TestInterfacesContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="CtMfg", slug="ctmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="CtDev", slug="ctdev")
        role = DeviceRole.objects.create(name="CtRole", slug="ctrole")
        site = Site.objects.create(name="CtSite", slug="ctsite")
        cls.device = Device.objects.create(name="ct-rtr", device_type=dt, role=role, site=site)
        cls.iface = Interface.objects.create(device=cls.device, name="GE0/0", type="1000base-t")

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key set (keeps the mirror honest)."""
        iface = CONTRACT_PAYLOAD[0]
        self.assertEqual(set(iface.keys()), EXPECTED_IFACE_KEYS)
        for attr in iface["attrs"].values():
            self.assertEqual(set(attr.keys()), EXPECTED_ATTR_KEYS)

    def test_consumer_reads_contract_payload(self):
        """_upsert_interface_states ingests the documented shape into NSOInterfaceState."""
        result = _upsert_interface_states(self.device, CONTRACT_PAYLOAD)

        desc = result[("GE0/0", "description")]
        self.assertEqual(desc.status, "apply_failed")
        self.assertEqual(desc.nso_value, "uplink to spine-1")
        self.assertIsNotNone(desc.last_apply_at)
        self.assertEqual(desc.last_apply_error, {"code": "nso_error", "message": "boom"})

        enabled = result[("GE0/0", "enabled")]
        self.assertEqual(enabled.status, "in_sync")
        self.assertEqual(enabled.nso_value, "true")

        # Persisted, not just returned.
        self.assertTrue(NSOInterfaceState.objects.filter(interface=self.iface, attribute="description").exists())

    def test_missing_status_key_silently_degrades_to_unknown(self):
        """CHARACTERIZATION of the fragility: the consumer does NOT validate the contract.

        If the producer ever stops sending ``status`` (e.g. a rename to ``sync_state``),
        the plugin stores ``"unknown"`` instead of failing — which then hides in the
        in-sync remainder (see [[drift-netbox-vs-device-value]]). This pins that blind
        spot so a future change to add validation is a visible behavior change.
        """
        payload = [
            {
                "name": "GE0/0",
                "netbox_interface_id": 1000,
                "attrs": {"description": {"nso_value": "x"}},  # no "status" key
            }
        ]
        result = _upsert_interface_states(self.device, payload)
        self.assertEqual(result[("GE0/0", "description")].status, "unknown")
