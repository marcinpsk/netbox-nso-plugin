# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("dcim", "0233_device_render_config_permission"),
        ("extras", "0138_customfieldchoiceset_choice_colors"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSOInstance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("name", models.CharField(max_length=100, unique=True)),
                (
                    "adapter_instance_id",
                    models.CharField(
                        help_text="The instance ID used by the nso-adapter (matches adapter config).",
                        max_length=100,
                        unique=True,
                    ),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag", related_name="+"),
                ),
            ],
            options={
                "verbose_name": "NSO Instance",
                "verbose_name_plural": "NSO Instances",
                "ordering": ["name"],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="NSODeviceManagement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "nso_device_name",
                    models.CharField(
                        help_text="Device name in NSO. Defaults to the NetBox device name.",
                        max_length=255,
                    ),
                ),
                (
                    "manage_description",
                    models.BooleanField(
                        default=False,
                        help_text="Sync interface description attribute from NSO.",
                    ),
                ),
                (
                    "manage_enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Sync interface enabled/shutdown attribute from NSO.",
                    ),
                ),
                (
                    "adapter_device_id",
                    models.IntegerField(
                        blank=True,
                        help_text="The device ID assigned by the nso-adapter after onboarding.",
                        null=True,
                    ),
                ),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_status", models.CharField(blank=True, default="", max_length=50)),
                (
                    "compliance_snapshot",
                    models.JSONField(
                        blank=True,
                        help_text="Cached compliance counts and per-interface statuses from the last sync.",
                        null=True,
                    ),
                ),
                (
                    "device",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nso_management",
                        to="dcim.device",
                    ),
                ),
                (
                    "nso_instance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="managed_devices",
                        to="netbox_nso_plugin.nsoinstance",
                    ),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag", related_name="+"),
                ),
            ],
            options={
                "verbose_name": "NSO Device Management",
                "verbose_name_plural": "NSO Device Management",
                "ordering": ["device"],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
    ]
