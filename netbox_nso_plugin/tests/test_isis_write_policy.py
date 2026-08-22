# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""IS-IS write-path capability surfacing (#78).

The registry in ``isis_write_policy`` claims which mirrored fields the intent push
does / does not carry. The core test here captures a REAL push payload (the same
recording-function harness as test_isis_intent_push — no mocks inspected) and
asserts BOTH directions, so the registry cannot silently drift from the code:
a field newly added to the push must leave the read-only list, and a field
dropped from the push must join it.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.template.loader import render_to_string
from django.test import TestCase

from netbox_nso_plugin.isis_write_policy import ISIS_CHILD_NOTES, ISIS_PUSHED_FIELDS, ISIS_READ_ONLY_FIELDS

from .mixins import IntentPushResetMixin


class _IsisPolicyBase(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="IwpMfg", slug="iwpmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IwpDev", slug="iwpdev")
        role = DeviceRole.objects.create(name="IwpRole", slug="iwprole")
        site = Site.objects.create(name="IwpSite", slug="iwpsite")
        cls.device = Device.objects.create(name="iwp-router", device_type=dt, role=role, site=site)
        cls.iface = Interface.objects.create(device=cls.device, name="ge-0/0/0", type="virtual")

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="iwp-inst", defaults={"adapter_instance_id": "iwp-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "iwp-dev", "adapter_device_id": self.device.pk},
        )[0]


class TestRegistryMatchesRealPush(_IsisPolicyBase):
    """The read-only/pushed registry is proven against a captured intent payload."""

    def test_registry_is_disjoint(self):
        for kind, read_only in ISIS_READ_ONLY_FIELDS.items():
            overlap = set(read_only) & set(ISIS_PUSHED_FIELDS.get(kind, ()))
            self.assertFalse(overlap, f"{kind}: fields both read-only and pushed: {overlap}")

    def _capture_push(self):
        from netbox_routing.models import ISISInstance, ISISLevel

        from netbox_nso_plugin import adapter_client
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOISISInstanceState, NSOISISInterfaceState
        from netbox_nso_plugin.signals import suppress_intent_push

        mgmt = self._mgmt()
        # A fork instance carrying every read-only scalar the reconciler mirrors —
        # if the push serialised any of them, the assertion below would catch it.
        fork = ISISInstance.objects.create(
            device=self.device,
            process_tag="",
            net="49.0001.00",
            lsp_mtu=1492,
            te_enabled=True,
            distance=115,
            maximum_paths=4,
            reference_bandwidth=100000,
        )
        ISISLevel.objects.create(instance=fork, level=2, wide_metrics_only=True, default_metric=10, preference=18)
        with suppress_intent_push():
            NSOISISInstanceState.objects.update_or_create(
                management=mgmt,
                process_tag="",
                defaults={"net": "49.0001.00", "is_type": "level-2-only", "status": "in_sync", "isis_instance": fork},
            )
            NSOISISInterfaceState.objects.update_or_create(
                management=mgmt,
                interface=self.iface,
                af="ipv4",
                defaults={"process_tag": "", "status": "in_sync", "metric": 10, "passive": True},
            )

        captured = {}

        def _fake_put(adapter_id, interfaces, processes=None):
            captured["interfaces"] = interfaces
            captured["processes"] = processes

        orig = adapter_client.put_isis_interface_intent
        adapter_client.put_isis_interface_intent = _fake_put
        try:
            deliver("isis", mgmt.device_id, mgmt.adapter_device_id)
        finally:
            adapter_client.put_isis_interface_intent = orig
        self.assertEqual(len(captured.get("processes") or []), 1)
        self.assertEqual(len(captured.get("interfaces") or []), 1)
        return captured["interfaces"][0], captured["processes"][0]

    def test_read_only_fields_absent_and_pushed_fields_present(self):
        iface_payload, proc_payload = self._capture_push()
        levels = proc_payload.get("levels") or []
        self.assertEqual(len(levels), 1)

        for field in ISIS_READ_ONLY_FIELDS["isis_instance"]:
            self.assertNotIn(field, proc_payload, f"read-only instance field {field!r} rode the push")
        for field in ISIS_READ_ONLY_FIELDS["isis_interface"]:
            self.assertNotIn(field, iface_payload, f"read-only interface field {field!r} rode the push")
        for field in ISIS_READ_ONLY_FIELDS["isis_level"]:
            self.assertNotIn(field, levels[0], f"read-only level field {field!r} rode the push")

        for field in ISIS_PUSHED_FIELDS["isis_instance"]:
            self.assertIn(field, proc_payload, f"writable instance field {field!r} missing from the push")
        for field in ISIS_PUSHED_FIELDS["isis_interface"]:
            self.assertIn(field, iface_payload, f"writable interface field {field!r} missing from the push")
        # levels omit None fields per entry — the seeded row carries wide_metrics_only
        self.assertIn("wide_metrics_only", levels[0])


class TestWritePolicyPanel(_IsisPolicyBase):
    """The warning panel renders on managed IS-IS object pages (and only there)."""

    def _render_panel(self, obj):
        from netbox_nso_plugin.template_content import ISISWritePolicyPanel

        ext = object.__new__(ISISWritePolicyPanel)
        ext.context = {"object": obj}
        captured = {}

        def fake_render(template, extra_context=None):
            captured["template"] = template
            captured.update(extra_context or {})
            return "RENDERED"

        ext.render = fake_render  # type: ignore[method-assign]
        return ext.full_width_page(), captured

    def test_panel_lists_read_only_fields_for_managed_instance(self):
        from netbox_routing.models import ISISInstance

        self._mgmt()
        inst = ISISInstance.objects.create(device=self.device, process_tag="", net="49.0001.00")
        html, captured = self._render_panel(inst)
        self.assertEqual(html, "RENDERED")
        self.assertEqual(captured["read_only_fields"], ISIS_READ_ONLY_FIELDS["isis_instance"])
        self.assertEqual(captured["writable_fields"], ISIS_PUSHED_FIELDS["isis_instance"])
        self.assertEqual(captured["child_notes"], ISIS_CHILD_NOTES["isis_instance"])

    def test_panel_resolves_interface_device(self):
        from netbox_routing.models import ISISInstance, ISISInterface

        self._mgmt()
        inst = ISISInstance.objects.create(device=self.device, process_tag="", net="49.0001.00")
        ri = ISISInterface.objects.create(instance=inst, interface=self.iface)
        html, captured = self._render_panel(ri)
        self.assertEqual(html, "RENDERED")
        self.assertEqual(captured["read_only_fields"], ISIS_READ_ONLY_FIELDS["isis_interface"])

    def test_panel_empty_for_unmanaged_device(self):
        from netbox_routing.models import ISISInstance

        inst = ISISInstance.objects.create(device=self.device, process_tag="", net="49.0001.00")
        html, _ = self._render_panel(inst)
        self.assertEqual(html, "")

    def test_template_renders_real_html(self):
        html = render_to_string(
            "netbox_nso_plugin/isis_write_policy.html",
            {
                "read_only_fields": ISIS_READ_ONLY_FIELDS["isis_instance"],
                "writable_fields": ISIS_PUSHED_FIELDS["isis_instance"],
                "child_notes": ISIS_CHILD_NOTES["isis_instance"],
            },
        )
        self.assertIn("NSO write-path coverage", html)
        self.assertIn("lsp_mtu", html)
        self.assertIn("not pushed", html)
        self.assertIn("overload_bit", html)  # the writable list renders too
