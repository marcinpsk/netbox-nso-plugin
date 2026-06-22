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
# M27R added the logical-interface modeling keys (NULL for physical ports / Cisco / Junos).
EXPECTED_IFACE_KEYS = {
    "name",
    "netbox_interface_id",
    "attrs",
    "parent_binding",
    "kind",
    "encap_tag",
    "vrf",
    "service",
}
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
        # M27R logical-interface modeling — NULL for a physical port like this one.
        "parent_binding": None,
        "kind": None,
        "encap_tag": None,
        "vrf": None,
        "service": None,
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
        # The "Z"-suffixed UTC timestamp is parsed tz-aware, not naive (no RuntimeWarning).
        self.assertIsNotNone(desc.last_apply_at.tzinfo)
        self.assertEqual(desc.last_apply_error, {"code": "nso_error", "message": "boom"})

        enabled = result[("GE0/0", "enabled")]
        self.assertEqual(enabled.status, "in_sync")
        self.assertEqual(enabled.nso_value, "true")

        # Persisted, not just returned.
        self.assertTrue(NSOInterfaceState.objects.filter(interface=self.iface, attribute="description").exists())

    def test_owned_row_not_clobbered_by_adapter_imported(self):
        """Owned-guard: an adapter sync reporting 'imported' must not drop operator ownership.

        Mirrors interface_mtu_reconciler's guard. Without it the (now status-based) intent
        push would stop re-applying an owned attribute the moment the adapter read it back as
        imported — the device-27 ae2.0 class of bug.
        """
        self.iface.description = "operator-desc"
        self.iface.save(update_fields=["description"])
        NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="accepted", nso_value="device-old"
        )
        # Adapter still reads the OLD device value (operator's value not applied yet) + imported.
        payload = [
            {
                "name": "GE0/0",
                "netbox_interface_id": 1000,
                "attrs": {"description": {"nso_value": "device-old", "status": "imported"}},
            }
        ]
        result = _upsert_interface_states(self.device, payload)
        row = result[("GE0/0", "description")]
        # Device still differs from the operator's value → ownership preserved (accepted), not imported.
        self.assertEqual(row.status, "accepted")
        # The device value is still tracked for the value-aware display.
        self.assertEqual(row.nso_value, "device-old")

    def test_owned_row_settles_to_in_sync_when_device_matches(self):
        """Owned-guard settles the row by value: accepted → in_sync once the device catches up."""
        self.iface.description = "operator-desc"
        self.iface.save(update_fields=["description"])
        NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="accepted", nso_value="device-old"
        )
        # Adapter now reads the operator's value on the device → settle accepted → in_sync.
        payload = [
            {
                "name": "GE0/0",
                "netbox_interface_id": 1000,
                "attrs": {"description": {"nso_value": "operator-desc", "status": "imported"}},
            }
        ]
        result = _upsert_interface_states(self.device, payload)
        self.assertEqual(result[("GE0/0", "description")].status, "in_sync")

    def test_unowned_row_tracks_adapter_status(self):
        """An unowned (imported) row still mirrors the adapter status verbatim (drift visible)."""
        NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="imported", nso_value="old"
        )
        payload = [
            {
                "name": "GE0/0",
                "netbox_interface_id": 1000,
                "attrs": {"description": {"nso_value": "new", "status": "changed"}},
            }
        ]
        result = _upsert_interface_states(self.device, payload)
        self.assertEqual(result[("GE0/0", "description")].status, "changed")

    def _inject_templates(self, templates):
        """Monkey-patch _derived_intent_templates on the AppConfig for one test."""
        from django.apps import apps

        cfg = apps.get_app_config("netbox_nso_plugin")
        original = getattr(cfg, "_derived_intent_templates", [])
        cfg._derived_intent_templates = templates
        self.addCleanup(setattr, cfg, "_derived_intent_templates", original)

    def test_derived_managed_description_is_owned_pending(self):
        """A derived-managed description is NetBox intent BY DEFINITION: the reconciler owns
        it even when the adapter reports 'imported', so it pushes instead of reading as drift
        and never reaching the device (the device-27 ae2.0 recovery). Device empty + NetBox
        derived value differs → accepted (pending apply)."""
        from netbox_nso_plugin.derived_intent import SentinelTemplate

        self._inject_templates([SentinelTemplate(sentinel="[auto]", template="[auto] x")])
        # Set via queryset update (no post_save) so the degenerate test template's recompute
        # doesn't rewrite the value — we isolate the reconciler's ownership logic here.
        Interface.objects.filter(pk=self.iface.pk).update(description="[auto] prod - Core Link - unit")
        payload = [
            {
                "name": "GE0/0",
                "netbox_interface_id": 1000,
                "attrs": {"description": {"nso_value": "", "status": "imported"}},
            }
        ]
        row = _upsert_interface_states(self.device, payload)[("GE0/0", "description")]
        self.assertEqual(row.status, "accepted")  # owned, pending apply (device lacks it)
        self.assertIsNotNone(row.accepted_at)

    def test_derived_managed_description_matching_device_is_in_sync(self):
        """A derived description the device already holds → owned + in_sync (nothing to push)."""
        from netbox_nso_plugin.derived_intent import SentinelTemplate

        self._inject_templates([SentinelTemplate(sentinel="[auto]", template="[auto] x")])
        Interface.objects.filter(pk=self.iface.pk).update(description="[auto] match")
        payload = [
            {
                "name": "GE0/0",
                "netbox_interface_id": 1000,
                "attrs": {"description": {"nso_value": "[auto] match", "status": "imported"}},
            }
        ]
        row = _upsert_interface_states(self.device, payload)[("GE0/0", "description")]
        self.assertEqual(row.status, "in_sync")

    def test_non_derived_description_stays_unowned(self):
        """A plain (non-derived) description is NOT auto-owned — only operator action owns it."""
        from netbox_nso_plugin.derived_intent import SentinelTemplate

        self._inject_templates([SentinelTemplate(sentinel="[auto]", template="[auto] x")])
        Interface.objects.filter(pk=self.iface.pk).update(description="hand-typed, not derived")
        payload = [
            {
                "name": "GE0/0",
                "netbox_interface_id": 1000,
                "attrs": {"description": {"nso_value": "", "status": "imported"}},
            }
        ]
        row = _upsert_interface_states(self.device, payload)[("GE0/0", "description")]
        self.assertEqual(row.status, "imported")
        self.assertIsNone(row.accepted_at)

    def test_missing_status_key_silently_degrades_to_unknown(self):
        """The consumer does NOT validate the contract at runtime — by decision.

        If the producer ever stops sending ``status`` the plugin stores ``"unknown"``.
        We deliberately keep it this way: the cross-repo contract test (this file + the
        adapter's producer mirror) guards the seam in CI, so a rename fails a test rather
        than slipping to prod; runtime per-row validation would be redundant overhead.
        And ``unknown`` no longer hides anyway — it now surfaces as needs-attention in
        the counts (see summary._status_breakdown). This test pins the documented
        behavior so any future move to hard validation is a visible change.
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
