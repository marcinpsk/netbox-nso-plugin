# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
# Generated for M14: IS-IS interface enablement state model

import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("extras", "0138_customfieldchoiceset_choice_colors"),
        ("netbox_nso_plugin", "0008_m13_ip_autoassign"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSOISISInterfaceState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("af", models.CharField(max_length=8)),
                ("process_tag", models.CharField(blank=True, default="", max_length=128)),
                ("circuit_type", models.CharField(blank=True, default="", max_length=32)),
                ("network_type", models.CharField(blank=True, default="", max_length=32)),
                ("metric", models.PositiveIntegerField(blank=True, null=True)),
                ("passive", models.BooleanField(default=False)),
                ("status", models.CharField(default="unknown", max_length=32)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_error", models.TextField(blank=True, default="")),
                (
                    "interface",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nso_isis_states",
                        to="dcim.interface",
                    ),
                ),
                (
                    "management",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="isis_interface_states",
                        to="netbox_nso_plugin.nsodevicemanagement",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "NSO IS-IS Interface State",
                "verbose_name_plural": "NSO IS-IS Interface States",
                "ordering": ["management", "interface", "af"],
                "unique_together": {("management", "interface", "af")},
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
    ]
