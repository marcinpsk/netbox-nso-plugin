# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0011_m17_route_policy_state"),
        ("netbox_routing", "0034_isis_m18_process_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSOISISInstanceState",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=None),
                ),
                ("process_tag", models.CharField(max_length=128)),
                ("net", models.CharField(blank=True, default="", max_length=100)),
                ("is_type", models.CharField(blank=True, default="", max_length=50)),
                ("metric_style", models.CharField(blank=True, default="", max_length=20)),
                ("overload_bit", models.BooleanField(blank=True, null=True)),
                ("area_auth_type", models.CharField(blank=True, default="", max_length=10)),
                ("area_auth_present", models.BooleanField(default=False)),
                ("domain_auth_type", models.CharField(blank=True, default="", max_length=10)),
                ("domain_auth_present", models.BooleanField(default=False)),
                ("status", models.CharField(default="unknown", max_length=32)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_error", models.TextField(blank=True, default="")),
                (
                    "management",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="isis_instance_states",
                        to="netbox_nso_plugin.nsodevicemanagement",
                    ),
                ),
                (
                    "isis_instance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="nso_instance_states",
                        to="netbox_routing.isisinstance",
                    ),
                ),
            ],
            options={
                "verbose_name": "NSO IS-IS Instance State",
                "verbose_name_plural": "NSO IS-IS Instance States",
                "ordering": ["management", "process_tag"],
                "unique_together": {("management", "process_tag")},
            },
        ),
    ]
