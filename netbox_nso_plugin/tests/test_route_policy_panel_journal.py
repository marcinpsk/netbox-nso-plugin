# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""End-to-end tests for the route-policy "applied to devices" panel.

The panel hangs off the GenericForeignKey from NSORoutePolicyState into the REAL
netbox_routing objects, so these tests build that link the production way — by running
reconcile_route_policy — then exercise the real template render.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.contenttypes.models import ContentType
from django.template.loader import render_to_string
from django.test import TestCase

from ._ownership_case import save_overlay_fixture


class _RoutePolicyFixture(TestCase):
    """Shared setup: a device with one object per route-policy family, GFK-linked."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RpjMfg", slug="rpjmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RpjDev", slug="rpjdev")
        role = DeviceRole.objects.create(name="RpjRole", slug="rpjrole")
        site = Site.objects.create(name="RpjSite", slug="rpjsite")
        cls.device = Device.objects.create(name="rpj-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="rpj-inst", defaults={"adapter_instance_id": "rpj-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "rpj-dev", "adapter_device_id": 777},
        )[0]

    def _payload(self):
        return {
            "prefix_lists": [{"name": "PLJ", "entries": [{"seq": 5, "action": "permit"}]}],
            "community_lists": [{"name": "CLJ", "entries": [{"community": "65000:1"}]}],
            "as_paths": [{"name": "APJ", "entries": [{"regex": "^65000_"}]}],
            "route_maps": [{"name": "RMJ", "entries": [{"seq": 10, "action": "permit"}]}],
        }

    def _reconcile(self):
        """Create real netbox_routing objects + GFK-linked state rows via the reconciler."""
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        reconcile_route_policy(self.device, self._payload())


class TestAppliedToDevicesPanel(_RoutePolicyFixture):
    def test_panel_lists_managing_device_and_status(self):
        """full_width_page() collects the GFK-linked state rows for the object."""
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.template_content import RoutePolicyNSODevices

        self._make_mgmt()
        self._reconcile()
        cl = CommunityList.objects.get(name="CLJ")

        ext = object.__new__(RoutePolicyNSODevices)
        ext.context = {"object": cl}
        captured = {}

        def fake_render(template, extra_context=None):
            captured.update(extra_context or {})
            return ""

        ext.render = fake_render  # type: ignore[method-assign]
        ext.full_width_page()

        states = captured["nso_states"]
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].management.device, self.device)
        self.assertIsInstance(states[0], NSORoutePolicyState)

    def test_panel_template_renders_real_html(self):
        """The real template renders the device link + human status (no mocks)."""
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSORoutePolicyState

        self._make_mgmt()
        self._reconcile()
        cl = CommunityList.objects.get(name="CLJ")
        ct = ContentType.objects.get_for_model(cl)
        # Accepted ownership projects to pending apply in the common badge.
        state = NSORoutePolicyState.objects.get(content_type=ct, object_id=cl.pk)

        state.status = "accepted"
        save_overlay_fixture(state, update_fields=["status"])
        states = list(NSORoutePolicyState.objects.filter(content_type=ct, object_id=cl.pk).select_related("management"))

        html = render_to_string("netbox_nso_plugin/route_policy_nso_devices.html", {"nso_states": states})
        self.assertIn("Applied to Devices", html)
        self.assertIn("rpj-router", html)
        self.assertIn("pending apply", html)
        self.assertIn('data-state="pending"', html)
        self.assertIn('data-pending="true"', html)

    def test_panel_empty_when_no_states(self):
        """No managing device → the panel renders nothing."""
        html = render_to_string("netbox_nso_plugin/route_policy_nso_devices.html", {"nso_states": []})
        self.assertNotIn("Applied to Devices", html)
