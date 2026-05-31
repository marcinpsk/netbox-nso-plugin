# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for NSODeviceManagementForm — the 'Manage everything' convenience toggle."""

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.forms import NSODeviceManagementForm
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance


def _make_device(name="form-router-01"):
    mfg = Manufacturer.objects.get_or_create(name="FormMfg", slug="formmfg")[0]
    dtype = DeviceType.objects.get_or_create(manufacturer=mfg, model="FormDev", slug="formdev")[0]
    role = DeviceRole.objects.get_or_create(name="FormRole", slug="formrole")[0]
    site = Site.objects.get_or_create(name="FormSite", slug="formsite")[0]
    return Device.objects.create(name=name, device_type=dtype, role=role, site=site)


class TestManageEverythingToggle(TestCase):
    """`manage_all` is a transient toggle that fills every scope flag on save."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device()
        cls.instance = NSOInstance.objects.create(name="form-nso", adapter_instance_id="form-nso-id")

    def _base_data(self, **extra):
        data = {
            "device": self.device.pk,
            "nso_instance": self.instance.pk,
            "nso_device_name": "form-router-01",
        }
        data.update(extra)
        return data

    def test_manage_all_enables_every_scope_but_not_auto_apply(self):
        form = NSODeviceManagementForm(data=self._base_data(manage_all="on"))
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save()
        for name in NSODeviceManagementForm.SCOPE_FIELDS:
            self.assertTrue(getattr(obj, name), f"{name} should be enabled by manage_all")
        # auto_apply is deliberately independent of the toggle.
        self.assertFalse(obj.auto_apply)

    def test_without_toggle_only_checked_scopes_enabled(self):
        form = NSODeviceManagementForm(data=self._base_data(manage_interfaces="on", manage_description="on"))
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save()
        self.assertTrue(obj.manage_interfaces)
        self.assertTrue(obj.manage_description)
        self.assertFalse(obj.manage_bgp)
        self.assertFalse(obj.manage_snmp)
        self.assertFalse(obj.auto_apply)

    def test_toggle_initial_true_when_fully_managed(self):
        obj = NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=self.instance,
            nso_device_name="form-router-01",
            **{name: True for name in NSODeviceManagementForm.SCOPE_FIELDS},
        )
        form = NSODeviceManagementForm(instance=obj)
        self.assertTrue(form.fields["manage_all"].initial)

    def test_toggle_initial_false_when_partially_managed(self):
        obj = NSODeviceManagement.objects.create(
            device=self.device,
            nso_instance=self.instance,
            nso_device_name="form-router-01",
            manage_interfaces=True,
        )
        form = NSODeviceManagementForm(instance=obj)
        self.assertFalse(form.fields["manage_all"].initial)
