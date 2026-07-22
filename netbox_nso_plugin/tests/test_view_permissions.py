# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Authorization tests for the NSO action views.

The Accept / Apply / onboard / attach / re-point views mutate device state or push
config to a device. Gating them on authentication alone (``LoginRequiredMixin``) let
*any* logged-in user — including a read-only NOC viewer — trigger them. These tests
prove the views now require the matching NetBox ObjectPermission and 403 otherwise,
while preserving the login-redirect for anonymous users.

Run in the devcontainer (full NetBox/Django stack). No adapter is reached: the denied
paths 403 in ``dispatch`` before any handler runs, and the one allowed path exercised
here (accept-attribute) only saves an overlay row.
"""

from core.models import ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from users.models import ObjectPermission

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceState

from .mixins import IntentPushResetMixin

User = get_user_model()
TEST_PASSWORD = "permtestpass123"  # noqa: S105


class ActionViewPermissionTests(IntentPushResetMixin, TestCase):
    """Authenticated-but-unprivileged users are blocked from NSO action views."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="PermMfg", slug="permmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="PermDev", slug="permdev")
        role = DeviceRole.objects.create(name="PermRole", slug="permrole")
        site = Site.objects.create(name="PermSite", slug="permsite")
        cls.device = Device.objects.create(name="perm-router-01", device_type=device_type, role=role, site=site)
        cls.nso_instance = NSOInstance.objects.create(name="perm-nso", adapter_instance_id="perm-nso-id")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.nso_instance, nso_device_name="perm-router-01"
        )
        cls.interface = Interface.objects.create(device=cls.device, name="Loopback0", type="virtual")
        cls.iface_state = NSOInterfaceState.objects.create(
            interface=cls.interface, attribute="description", status="changed", nso_value="device desc"
        )

    def setUp(self):
        super().setUp()
        # A plain, non-superuser account: authenticated, but holds no object permissions.
        self.user = User.objects.create_user(username="nso-operator", password=TEST_PASSWORD)
        self.client.force_login(self.user)

    def _grant(self, action, model):
        """Grant the user a model-level NetBox ObjectPermission (e.g. change/add)."""
        perm = ObjectPermission.objects.create(name=f"{action}-{model._meta.model_name}", actions=[action])
        perm.object_types.add(ObjectType.objects.get_for_model(model))
        perm.users.add(self.user)

    # ── change_nsodevicemanagement-gated views ────────────────────────────────

    def test_accept_attribute_denied_without_permission(self):
        """An authenticated user lacking change_nsodevicemanagement gets 403, not a 302 accept."""
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", kwargs={"pk": self.iface_state.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        # And the row was NOT accepted (the handler never ran).
        self.iface_state.refresh_from_db()
        self.assertEqual(self.iface_state.status, "changed")

    def test_accept_attribute_allowed_with_change_permission(self):
        """Granting change_nsodevicemanagement lets the accept through (302 redirect)."""
        self._grant("change", NSODeviceManagement)
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", kwargs={"pk": self.iface_state.pk})
        response = self.client.post(url)
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(response.status_code, 302)
        self.iface_state.refresh_from_db()
        self.assertEqual(self.iface_state.status, "accepted")

    def test_overlay_field_edit_denied_without_permission(self):
        """The inline (popover) overlay field editor mutates intent — 403 for an unprivileged user."""
        from netbox_nso_plugin.models import NSOSnmpSystemInfoState

        row = NSOSnmpSystemInfoState.objects.create(management=self.mgmt, location="keep-loc", status="imported")
        url = reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "snmp_system_info", "pk": row.pk})
        response = self.client.post(url, {"location": "hacked"})
        self.assertEqual(response.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.location, "keep-loc")

    def test_vlan_name_edit_also_requires_native_vlan_change_permission(self):
        """Plugin intent access alone must not grant permission to rename a native VLAN."""
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        self._grant("change", NSODeviceManagement)
        vlan = VLAN.objects.create(vid=120, name="KEEP-NAME")
        row = NSOVLANState.objects.create(
            management=self.mgmt,
            vlan=vlan,
            device_name="KEEP-NAME",
            status="imported",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:overlay_field_edit",
            kwargs={"key": "vlan_name", "pk": row.pk},
        )

        response = self.client.post(url, {"name": "DENIED-NAME"})

        self.assertEqual(response.status_code, 403)
        vlan.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(vlan.name, "KEEP-NAME")
        self.assertEqual(row.status, "imported")

    def test_device_apply_action_denied_without_permission(self):
        """The scariest action — committing config to the device — is blocked for an unprivileged user."""
        url = reverse(
            "plugins:netbox_nso_plugin:nsodevicemanagement_action",
            kwargs={"pk": self.mgmt.pk, "action": "apply"},
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_sync_from_nso_action_denied_without_permission(self):
        """S5a C: the new comprehensive-read action rides the same authz mixin."""
        url = reverse(
            "plugins:netbox_nso_plugin:nsodevicemanagement_action",
            kwargs={"pk": self.mgmt.pk, "action": "sync-from-nso"},
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_capability_probe_denied_without_permission(self):
        """A live route-policy capability probe (POST) touches the device → 403 without change perm."""
        url = reverse("plugins:netbox_nso_plugin:route_policy_capabilities", kwargs={"device_pk": self.device.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_capability_cache_read_allowed_without_permission(self):
        """A plain cache-only GET (no ?refresh) stays a login-only read (no probe) — not over-gated."""
        url = reverse("plugins:netbox_nso_plugin:route_policy_capabilities", kwargs={"device_pk": self.device.pk})
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 403)

    # ── add_nsodevicemanagement-gated views (onboard / manage) ────────────────

    def test_onboard_denied_without_add_permission(self):
        """Onboarding requires add_nsodevicemanagement — an unprivileged user is blocked."""
        url = reverse("plugins:netbox_nso_plugin:onboard")
        response = self.client.post(url, {"device": self.device.pk})
        self.assertEqual(response.status_code, 403)

    def test_onboard_change_permission_is_not_sufficient(self):
        """change is not add: a user with only change_nsodevicemanagement still cannot onboard."""
        self._grant("change", NSODeviceManagement)
        url = reverse("plugins:netbox_nso_plugin:onboard")
        response = self.client.post(url, {"device": self.device.pk})
        self.assertEqual(response.status_code, 403)

    def test_onboard_allowed_with_add_permission(self):
        """With add_nsodevicemanagement the onboard handler runs (redirects, never 403)."""
        self._grant("add", NSODeviceManagement)
        url = reverse("plugins:netbox_nso_plugin:onboard")
        response = self.client.post(url, {"device": self.device.pk})
        self.assertNotEqual(response.status_code, 403)

    # ── login behaviour is preserved for anonymous users ──────────────────────

    def test_anonymous_redirects_to_login_not_403(self):
        """An anonymous user still follows the login redirect (not a hard 403)."""
        self.client.logout()
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", kwargs={"pk": self.iface_state.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_superuser_is_allowed(self):
        """Regression guard: a superuser keeps full access (existing behaviour)."""
        superuser = User.objects.create_superuser(username="nso-super", password=TEST_PASSWORD, email="s@test.example")
        self.client.force_login(superuser)
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_accept", kwargs={"pk": self.iface_state.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
