# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django-stack tests for FilterSet classes.

These tests require the full NetBox/Django stack (run in devcontainer).
"""

from django.test import TestCase


class TestNSOInstanceFilterSet(TestCase):
    """Tests for NSOInstanceFilterSet."""

    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSOInstance

        cls.inst1 = NSOInstance.objects.create(name="prod-nso", adapter_instance_id="prod-id-1")
        cls.inst2 = NSOInstance.objects.create(name="staging-nso", adapter_instance_id="staging-id-2")

    def test_filterset_no_filter(self):
        """FilterSet with empty data returns all instances."""
        from netbox_nso_plugin.filters import NSOInstanceFilterSet
        from netbox_nso_plugin.models import NSOInstance

        fs = NSOInstanceFilterSet(data={}, queryset=NSOInstance.objects.all())
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.qs.count(), 2)

    def test_filterset_by_name(self):
        """FilterSet filters by name."""
        from netbox_nso_plugin.filters import NSOInstanceFilterSet
        from netbox_nso_plugin.models import NSOInstance

        fs = NSOInstanceFilterSet(data={"name": ["prod-nso"]}, queryset=NSOInstance.objects.all())
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.qs.count(), 1)


class TestNSODeviceManagementFilterSet(TestCase):
    """Tests for NSODeviceManagementFilterSet."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        cls.manufacturer = Manufacturer.objects.create(name="FilterMfg", slug="filtermfg")
        cls.device_type = DeviceType.objects.create(manufacturer=cls.manufacturer, model="FilterDev", slug="filterdev")
        cls.role = DeviceRole.objects.create(name="FilterRole", slug="filterrole")
        cls.site = Site.objects.create(name="FilterSite", slug="filtersite")
        cls.device = Device.objects.create(
            name="filter-router-01", device_type=cls.device_type, role=cls.role, site=cls.site
        )
        cls.nso_instance = NSOInstance.objects.create(name="filter-nso", adapter_instance_id="filter-nso-id")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device,
            nso_instance=cls.nso_instance,
            nso_device_name="filter-router-01",
            last_sync_status="success",
        )

    def test_filterset_no_filter(self):
        """FilterSet returns all records with empty data."""
        from netbox_nso_plugin.filters import NSODeviceManagementFilterSet
        from netbox_nso_plugin.models import NSODeviceManagement

        fs = NSODeviceManagementFilterSet(data={}, queryset=NSODeviceManagement.objects.all())
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.qs.count(), 1)

    def test_filterset_by_sync_status(self):
        """last_sync_status filter is case-insensitive substring match."""
        from netbox_nso_plugin.filters import NSODeviceManagementFilterSet
        from netbox_nso_plugin.models import NSODeviceManagement

        fs = NSODeviceManagementFilterSet(data={"last_sync_status": "succ"}, queryset=NSODeviceManagement.objects.all())
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.qs.count(), 1)

    def test_filterset_by_nso_instance_id(self):
        """nso_instance_id filter works."""
        from netbox_nso_plugin.filters import NSODeviceManagementFilterSet
        from netbox_nso_plugin.models import NSODeviceManagement

        fs = NSODeviceManagementFilterSet(
            data={"nso_instance_id": [self.nso_instance.pk]},
            queryset=NSODeviceManagement.objects.all(),
        )
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.qs.count(), 1)


class TestNSOInterfaceStateFilterSet(TestCase):
    """Tests for NSOInterfaceStateFilterSet."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

        from netbox_nso_plugin.models import NSOInterfaceState

        mfg = Manufacturer.objects.create(name="IFSFilterMfg", slug="ifsfiltermfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="IFSFilterDev", slug="ifsfilterdev")
        role = DeviceRole.objects.create(name="IFSFilterRole", slug="ifsfilterrole")
        site = Site.objects.create(name="IFSFilterSite", slug="ifsfiltersite")
        device = Device.objects.create(name="ifs-filter-router", device_type=dt, role=role, site=site)
        iface = Interface.objects.create(device=device, name="Loopback0", type="virtual")
        cls.state = NSOInterfaceState.objects.create(
            interface=iface, attribute="description", status="changed", nso_value="desc"
        )

    def test_filterset_by_status(self):
        """status filter works."""
        from netbox_nso_plugin.filters import NSOInterfaceStateFilterSet
        from netbox_nso_plugin.models import NSOInterfaceState

        fs = NSOInterfaceStateFilterSet(data={"status": ["changed"]}, queryset=NSOInterfaceState.objects.all())
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.qs.count(), 1)

    def test_filterset_by_attribute(self):
        """attribute filter works."""
        from netbox_nso_plugin.filters import NSOInterfaceStateFilterSet
        from netbox_nso_plugin.models import NSOInterfaceState

        fs = NSOInterfaceStateFilterSet(data={"attribute": ["description"]}, queryset=NSOInterfaceState.objects.all())
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.qs.count(), 1)
