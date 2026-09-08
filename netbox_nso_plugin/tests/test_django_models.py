# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django-stack tests for model methods: __str__, get_absolute_url, save(), properties.

These tests require the full NetBox/Django stack (run in devcontainer).
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse


class TestAdapterConnectionModelMethods(TestCase):
    """Tests for AdapterConnection model methods using the real Django model."""

    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import AdapterConnection

        cls.Model = AdapterConnection

    def test_str_with_url(self):
        """__str__ returns the URL when set."""
        obj = object.__new__(self.Model)
        obj.url = "http://adapter:8000"
        self.assertEqual(str(obj), "http://adapter:8000")

    def test_str_without_url(self):
        """__str__ returns fallback when URL is blank."""
        obj = object.__new__(self.Model)
        obj.url = ""
        self.assertEqual(str(obj), "nso-adapter (not configured)")

    def test_get_absolute_url(self):
        """get_absolute_url returns the singleton edit URL."""
        obj = object.__new__(self.Model)
        url = obj.get_absolute_url()
        self.assertEqual(url, reverse("plugins:netbox_nso_plugin:adapterconnection"))

    def test_save_singleton_reuses_pk(self):
        """Second save() reuses the existing row's PK (singleton pattern)."""
        from netbox_nso_plugin.models import AdapterConnection

        first = AdapterConnection(url="http://first:8000")
        first.save()
        first_pk = first.pk
        self.assertIsNotNone(first_pk)

        second = AdapterConnection(url="http://second:8000")
        second.save()
        self.assertEqual(second.pk, first_pk)
        self.assertEqual(AdapterConnection.objects.count(), 1)

    def test_save_first_instance_gets_pk(self):
        """First save() works normally when no instance exists."""
        from netbox_nso_plugin.models import AdapterConnection

        obj = AdapterConnection(url="http://adapter:9000")
        obj.save()
        self.assertIsNotNone(obj.pk)
        self.assertEqual(AdapterConnection.objects.count(), 1)


class TestNSOInstanceModelMethods(TestCase):
    """Tests for NSOInstance model methods."""

    @classmethod
    def setUpTestData(cls):
        from netbox_nso_plugin.models import NSOInstance

        cls.instance = NSOInstance.objects.create(name="prod-nso", adapter_instance_id="prod-nso-01")

    def test_str(self):
        """__str__ returns the instance name."""
        self.assertEqual(str(self.instance), "prod-nso")

    def test_get_absolute_url(self):
        """get_absolute_url returns the correct detail URL."""
        expected = reverse("plugins:netbox_nso_plugin:nsoinstance", args=[self.instance.pk])
        self.assertEqual(self.instance.get_absolute_url(), expected)


class TestNSOInstanceDefault(TestCase):
    """Tests for the is_default behaviour on NSOInstance."""

    def _make(self, name, **kw):
        from netbox_nso_plugin.models import NSOInstance

        return NSOInstance.objects.create(name=name, adapter_instance_id=name + "-id", **kw)

    def test_first_instance_becomes_default(self):
        """The first instance created is automatically the default."""
        inst = self._make("first")
        inst.refresh_from_db()
        self.assertTrue(inst.is_default)

    def test_get_default_returns_default(self):
        """get_default returns the current default instance."""
        from netbox_nso_plugin.models import NSOInstance

        first = self._make("first")
        self.assertEqual(NSOInstance.get_default(), first)

    def test_second_instance_not_default(self):
        """A second instance does not steal default automatically."""
        first = self._make("first")
        second = self._make("second")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.is_default)
        self.assertFalse(second.is_default)

    def test_setting_new_default_clears_previous(self):
        """Marking another instance default clears the previous default."""
        from netbox_nso_plugin.models import NSOInstance

        first = self._make("first")
        second = self._make("second")
        second.is_default = True
        second.save()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)
        self.assertEqual(NSOInstance.get_default(), second)

    def test_default_cannot_be_emptied(self):
        """Unchecking the only default re-asserts it (always one default)."""
        only = self._make("only")
        only.is_default = False
        only.save()
        only.refresh_from_db()
        self.assertTrue(only.is_default)

    def test_default_revalidation_ignores_database_row_order(self):
        from django.db import connection

        from netbox_nso_plugin.models import NSOInstance

        first = self._make("first")
        second = self._make("second")
        third = self._make("third")
        NSOInstance.objects.filter(pk__in=(first.pk, second.pk)).update(is_default=True)
        calls = 0

        def alternate_order(execute, sql, params, many, context):
            nonlocal calls
            if (
                calls < 2
                and sql.lstrip().startswith(f'SELECT "{NSOInstance._meta.db_table}"."id" AS "pk"')
                and '"is_default"' in sql
            ):
                calls += 1
                direction = "ASC" if calls == 1 else "DESC"
                reordered = sql.replace(
                    f'ORDER BY "{NSOInstance._meta.db_table}"."name" ASC',
                    f'ORDER BY "{NSOInstance._meta.db_table}"."id" {direction}',
                )
                self.assertNotEqual(reordered, sql)
                sql = reordered
            return execute(sql, params, many, context)

        with connection.execute_wrapper(alternate_order):
            third.save()

        self.assertEqual(calls, 2)


class TestNSODeviceManagementModelMethods(TestCase):
    """Tests for NSODeviceManagement model methods using real DB fixtures."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        manufacturer = Manufacturer.objects.create(name="ModelMfg", slug="modelmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ModelDev", slug="modeldev")
        role = DeviceRole.objects.create(name="ModelRole", slug="modelrole")
        site = Site.objects.create(name="ModelSite", slug="modelsite")
        cls.device = Device.objects.create(name="core-router-01", device_type=device_type, role=role, site=site)
        cls.nso_instance = NSOInstance.objects.create(name="prod-nso", adapter_instance_id="model-nso-id")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device,
            nso_instance=cls.nso_instance,
            nso_device_name="core-router-01",
        )

    def test_str(self):
        """__str__ formats device, instance, and nso_device_name."""
        result = str(self.mgmt)
        self.assertIn("prod-nso", result)
        self.assertIn("core-router-01", result)

    def test_get_absolute_url(self):
        """get_absolute_url returns correct URL for the management record."""
        expected = reverse("plugins:netbox_nso_plugin:nsodevicemanagement", args=[self.mgmt.pk])
        self.assertEqual(self.mgmt.get_absolute_url(), expected)

    def test_direct_delete_rejects_database_options(self):
        with self.assertRaisesRegex(TypeError, "does not accept delete options"):
            self.mgmt.delete(using="default")

        self.assertTrue(type(self.mgmt).objects.filter(pk=self.mgmt.pk).exists())

    def test_managed_attributes_none(self):
        """managed_attributes returns empty list when both flags are False."""
        self.mgmt.manage_description = False
        self.mgmt.manage_enabled = False
        self.assertEqual(self.mgmt.managed_attributes, [])

    def test_managed_attributes_description_only(self):
        """managed_attributes returns ['description'] when manage_description=True."""
        self.mgmt.manage_description = True
        self.mgmt.manage_enabled = False
        self.assertEqual(self.mgmt.managed_attributes, ["description"])

    def test_managed_attributes_enabled_only(self):
        """managed_attributes returns ['enabled'] when manage_enabled=True."""
        self.mgmt.manage_description = False
        self.mgmt.manage_enabled = True
        self.assertEqual(self.mgmt.managed_attributes, ["enabled"])

    def test_managed_attributes_both(self):
        """managed_attributes returns both when both flags are True."""
        self.mgmt.manage_description = True
        self.mgmt.manage_enabled = True
        self.assertEqual(self.mgmt.managed_attributes, ["description", "enabled"])


class TestNSOInterfaceStateModelMethods(TestCase):
    """Tests for NSOInterfaceState model methods using real DB fixtures."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

        from netbox_nso_plugin.models import NSOInterfaceState

        manufacturer = Manufacturer.objects.create(name="StateMfg", slug="statemfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="StateDev", slug="statedev")
        role = DeviceRole.objects.create(name="StateRole", slug="staterole")
        site = Site.objects.create(name="StateSite", slug="statesite")
        device = Device.objects.create(name="state-router-01", device_type=device_type, role=role, site=site)
        cls.interface = Interface.objects.create(device=device, name="GigabitEthernet0/0", type="1000base-t")
        cls.state = NSOInterfaceState.objects.create(interface=cls.interface, attribute="description", status="changed")

    def test_str(self):
        """__str__ includes interface, attribute, and status."""
        result = str(self.state)
        self.assertIn("GigabitEthernet0/0", result)
        self.assertIn("description", result)
        self.assertIn("changed", result)

    def test_get_absolute_url(self):
        """get_absolute_url returns correct URL."""
        expected = reverse("plugins:netbox_nso_plugin:nsointerfacestate", args=[self.state.pk])
        self.assertEqual(self.state.get_absolute_url(), expected)


class TestNSOFailoverSettingsModel(TestCase):
    """NSOFailoverSettings singleton — defaults, singleton save, get_absolute_url.

    Saving fires the push-to-adapter signal, so put_failover_config is patched (the
    push itself is covered in test_signals.py).
    """

    def test_str(self):
        from netbox_nso_plugin.models import NSOFailoverSettings

        self.assertEqual(str(object.__new__(NSOFailoverSettings)), "NSO Failover Settings")

    def test_get_absolute_url(self):
        from netbox_nso_plugin.models import NSOFailoverSettings

        obj = object.__new__(NSOFailoverSettings)
        self.assertEqual(obj.get_absolute_url(), reverse("plugins:netbox_nso_plugin:nsofailoversettings"))

    def test_defaults_are_spike_prod_values(self):
        from netbox_nso_plugin.models import NSOFailoverSettings

        with patch("netbox_nso_plugin.adapter_client.put_failover_config"):
            s = NSOFailoverSettings.objects.create()
        self.assertTrue(s.enabled)
        self.assertEqual((s.primary_probe_interval, s.oob_probe_interval), (15, 360))
        self.assertEqual((s.failure_threshold, s.success_threshold), (3, 5))
        self.assertEqual(s.probe_timeout, 10)
        self.assertEqual(s.active_probe_timeout, 45)
        self.assertEqual((s.probe_concurrency, s.max_flips_per_tick), (8, 8))
        self.assertTrue(s.sync_from_after_switch)

    def test_save_singleton_reuses_pk(self):
        from netbox_nso_plugin.models import NSOFailoverSettings

        with patch("netbox_nso_plugin.adapter_client.put_failover_config"):
            first = NSOFailoverSettings.objects.create()
            second = NSOFailoverSettings(primary_probe_interval=30)
            second.save()
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(NSOFailoverSettings.objects.count(), 1)
