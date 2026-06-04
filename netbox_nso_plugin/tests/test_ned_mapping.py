# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the NSOPlatformNedMapping model + its form/API (onboarding NED map)."""

from unittest.mock import patch

from dcim.models import Platform
from django.test import TestCase


class TestNSOPlatformNedMapping(TestCase):
    def test_create_and_str(self):
        from netbox_nso_plugin.models import NSOPlatformNedMapping

        plat = Platform.objects.create(name="IOS-XE", slug="ios-xe")
        m = NSOPlatformNedMapping.objects.create(platform=plat, ned_id="cisco-ios-cli-6.114:cisco-ios-cli-6.114")
        self.assertIn("IOS-XE", str(m))
        self.assertIn("cisco-ios-cli", str(m))
        self.assertTrue(m.get_absolute_url().endswith(f"/{m.pk}/"))

    def test_one_mapping_per_platform(self):
        from django.db import IntegrityError, transaction

        from netbox_nso_plugin.models import NSOPlatformNedMapping

        plat = Platform.objects.create(name="Junos", slug="junos")
        NSOPlatformNedMapping.objects.create(platform=plat, ned_id="juniper-junos-nc-23.4")
        with self.assertRaises(IntegrityError), transaction.atomic():
            NSOPlatformNedMapping.objects.create(platform=plat, ned_id="other")

    def test_form_ned_choices_from_adapter(self):
        """When the adapter is reachable, ned_id becomes a dropdown of its NEDs."""
        from netbox_nso_plugin.forms import NSOPlatformNedMappingForm

        fake = [
            {"ned_id": "cisco-ios-cli-6.114:cisco-ios-cli-6.114", "vendor": "Cisco"},
            {"ned_id": "juniper-junos-nc-23.4", "vendor": "Juniper"},
        ]
        with (
            patch("netbox_nso_plugin.adapter_client.get_neds", return_value=fake),
            patch("netbox_nso_plugin.models.NSOInstance.get_default") as gd,
        ):
            gd.return_value = type("I", (), {"adapter_instance_id": "x"})()
            form = NSOPlatformNedMappingForm()
            values = {c[0] for c in form.fields["ned_id"].choices}
        self.assertIn("cisco-ios-cli-6.114:cisco-ios-cli-6.114", values)
        self.assertIn("juniper-junos-nc-23.4", values)

    def test_form_falls_back_to_free_text_when_adapter_down(self):
        """Adapter unreachable → ned_id stays a plain CharField (still editable)."""
        from django.forms import ChoiceField

        from netbox_nso_plugin.forms import NSOPlatformNedMappingForm

        with patch("netbox_nso_plugin.adapter_client.get_neds", side_effect=RuntimeError("down")):
            form = NSOPlatformNedMappingForm()
        self.assertNotIsInstance(form.fields["ned_id"], ChoiceField)
