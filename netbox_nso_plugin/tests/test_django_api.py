# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django-stack tests for REST API viewsets.

These tests require the full NetBox/Django stack (run in devcontainer).
"""

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from rest_framework import status
from utilities.testing import APITestCase

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceState


class NSOInstanceAPITest(APITestCase):
    """Test CRUD operations on NSOInstance via REST API."""

    model = NSOInstance
    view_namespace = "plugins-api:netbox_nso_plugin"
    user_permissions = (
        "netbox_nso_plugin.view_nsoinstance",
        "netbox_nso_plugin.add_nsoinstance",
        "netbox_nso_plugin.change_nsoinstance",
        "netbox_nso_plugin.delete_nsoinstance",
    )

    @classmethod
    def setUpTestData(cls):
        cls.inst1 = NSOInstance.objects.create(name="api-nso-1", adapter_instance_id="api-id-1")
        cls.inst2 = NSOInstance.objects.create(name="api-nso-2", adapter_instance_id="api-id-2")
        cls.inst3 = NSOInstance.objects.create(name="api-nso-3", adapter_instance_id="api-id-3")

    def test_list(self):
        """List endpoint returns all instances."""
        url = self._get_list_url()
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 3)

    def test_get_detail(self):
        """Detail endpoint returns a single instance."""
        url = self._get_detail_url(self.inst1)
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "api-nso-1")

    def test_create(self):
        """POST creates a new NSOInstance."""
        url = self._get_list_url()
        data = {"name": "api-nso-new", "adapter_instance_id": "api-id-new"}
        response = self.client.post(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "api-nso-new")

    def test_delete(self):
        """DELETE removes an instance."""
        to_delete = NSOInstance.objects.create(name="api-nso-delete", adapter_instance_id="api-id-del")
        url = self._get_detail_url(to_delete)
        response = self.client.delete(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(NSOInstance.objects.filter(pk=to_delete.pk).exists())


class NSODeviceManagementAPITest(APITestCase):
    """Test CRUD operations on NSODeviceManagement via REST API."""

    model = NSODeviceManagement
    view_namespace = "plugins-api:netbox_nso_plugin"
    user_permissions = (
        "netbox_nso_plugin.view_nsodevicemanagement",
        "netbox_nso_plugin.add_nsodevicemanagement",
        "netbox_nso_plugin.change_nsodevicemanagement",
        "netbox_nso_plugin.delete_nsodevicemanagement",
    )

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="APIMfgMgmt", slug="apimfgmgmt")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="APIDevMgmt", slug="apidevmgmt")
        role = DeviceRole.objects.create(name="APIRoleMgmt", slug="apirolemgmt")
        site = Site.objects.create(name="APISiteMgmt", slug="apistmgmt")
        cls.device = Device.objects.create(name="api-mgmt-router", device_type=device_type, role=role, site=site)
        cls.nso_instance = NSOInstance.objects.create(name="api-mgmt-nso", adapter_instance_id="api-mgmt-id")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=cls.nso_instance, nso_device_name="api-mgmt-router"
        )

    def test_list(self):
        """List endpoint returns all management records."""
        url = self._get_list_url()
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

    def test_get_detail(self):
        """Detail endpoint returns single management record."""
        url = self._get_detail_url(self.mgmt)
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["nso_device_name"], "api-mgmt-router")

    def test_detail_exposes_primary_and_oob_ip(self):
        """The device-management record serializes primary_ip + oob_ip as bare host strings.

        The adapter's scope reconciler reads this endpoint to feed the failover loop both
        management addresses — exercised here through the real DRF serializer + viewset.
        """
        from ipam.models import IPAddress

        dev = Device.objects.create(
            name="api-mgmt-ipd", device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        iface = Interface.objects.create(device=dev, name="mgmt0", type="virtual")
        dev.primary_ip4 = IPAddress.objects.create(address="10.0.0.1/32", assigned_object=iface)
        dev.oob_ip = IPAddress.objects.create(address="192.0.2.5/24", assigned_object=iface)
        dev.save()
        mgmt = NSODeviceManagement.objects.create(
            device=dev, nso_instance=self.nso_instance, nso_device_name="api-mgmt-ipd"
        )

        url = self._get_detail_url(mgmt)
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["primary_ip"], "10.0.0.1")  # /32 stripped
        self.assertEqual(response.data["oob_ip"], "192.0.2.5")  # /24 stripped

    def test_detail_null_ips_when_unset(self):
        """A device with no primary/OOB IP serializes both as null (not an error)."""
        url = self._get_detail_url(self.mgmt)
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["primary_ip"])
        self.assertIsNone(response.data["oob_ip"])


class NSOInterfaceStateAPITest(APITestCase):
    """Test read operations on NSOInterfaceState via REST API."""

    model = NSOInterfaceState
    view_namespace = "plugins-api:netbox_nso_plugin"
    user_permissions = ("netbox_nso_plugin.view_nsointerfacestate",)

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="APIIfsMfg", slug="apiifsmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="APIIfsDev", slug="apiifsdev")
        role = DeviceRole.objects.create(name="APIIfsRole", slug="apiifsrole")
        site = Site.objects.create(name="APIIfsSite", slug="apiifssite")
        device = Device.objects.create(name="api-ifs-router", device_type=device_type, role=role, site=site)
        interface = Interface.objects.create(device=device, name="Loopback0", type="virtual")
        cls.state = NSOInterfaceState.objects.create(
            interface=interface, attribute="description", status="changed", nso_value="test"
        )

    def test_list(self):
        """List endpoint returns interface states."""
        url = self._get_list_url()
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

    def test_get_detail(self):
        """Detail endpoint returns a single interface state."""
        url = self._get_detail_url(self.state)
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["attribute"], "description")


class OnboardingCandidatesAPITest(APITestCase):
    """The CICD-facing candidates API exposes the management IP + oob_only, and onboardable
    OOB-only devices now appear (with primary_ip kept null for backward compatibility)."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Platform
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOPlatformNedMapping

        cls.instance = NSOInstance.objects.create(name="cand-api", adapter_instance_id="cand-api")
        mfg = Manufacturer.objects.create(name="CandMfg", slug="candmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="CandDev", slug="canddev")
        role = DeviceRole.objects.create(name="CandRole", slug="candrole")
        site = Site.objects.create(name="CandSite", slug="candsite")
        plat = Platform.objects.create(name="CandPlat", slug="candplat")
        NSOPlatformNedMapping.objects.create(platform=plat, ned_id="cisco-ios-cli-6.114")
        d = Device.objects.create(name="cand-oob", device_type=dt, role=role, site=site, status="active", platform=plat)
        iface = Interface.objects.create(device=d, name="oob0", type="virtual")
        d.oob_ip = IPAddress.objects.create(address="192.0.2.222/24", assigned_object=iface)
        d.save()

    def test_candidates_api_exposes_mgmt_ip_and_oob_only(self):
        from unittest.mock import patch

        from django.urls import reverse

        url = reverse("plugins-api:netbox_nso_plugin-api:onboarding_candidates")
        with (
            patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[]),
            patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[]),
        ):
            resp = self.client.get(f"{url}?instance=cand-api", **self.header)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        c = next(x for x in resp.json()["candidates"] if x["name"] == "cand-oob")
        self.assertEqual(c["mgmt_ip"], "192.0.2.222")
        self.assertTrue(c["oob_only"])
        self.assertIsNone(c["primary_ip"])  # backward compat: OOB-only keeps primary_ip null
