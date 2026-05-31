# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models


def enable_all_scopes(apps, schema_editor):
    """Backfill: existing managed devices rendered every section that had data,
    so preserve that behaviour by enabling all scopes on rows that predate the
    opt-in toggles. New devices default to off (cautious onboarding)."""
    NSODeviceManagement = apps.get_model("netbox_nso_plugin", "NSODeviceManagement")
    NSODeviceManagement.objects.update(
        manage_interfaces=True,
        manage_routing=True,
        manage_static=True,
        manage_isis=True,
        manage_ospf=True,
        manage_bgp=True,
        manage_route_policy=True,
        manage_redistribution=True,
        manage_snmp=True,
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0015_alter_adapterconnection_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_interfaces",
            field=models.BooleanField(
                default=False,
                help_text="Master switch for interface-attribute management (description/enabled).",
            ),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_routing",
            field=models.BooleanField(
                default=False,
                help_text="Master switch for routing management. Enable, then pick protocols below.",
            ),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_static",
            field=models.BooleanField(default=False, help_text="Manage static routes."),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_isis",
            field=models.BooleanField(default=False, help_text="Manage IS-IS interfaces and instances."),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_ospf",
            field=models.BooleanField(default=False, help_text="Manage OSPF instances and interfaces."),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_bgp",
            field=models.BooleanField(default=False, help_text="Manage BGP peers."),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_route_policy",
            field=models.BooleanField(default=False, help_text="Manage route policy objects."),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_redistribution",
            field=models.BooleanField(default=False, help_text="Manage redistribution statements."),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="manage_snmp",
            field=models.BooleanField(default=False, help_text="Manage SNMP configuration for this device."),
        ),
        migrations.RunPython(enable_all_scopes, noop),
    ]
