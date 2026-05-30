# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M19 A4: NSOOSPFInstanceState and NSOOSPFInterfaceState models."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0012_m18_isis_instance_state"),
        ("dcim", "0001_initial"),
        ("netbox_routing", "0035_ospf_m19_interface_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSOOSPFInstanceState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=None),
                ),
                ("process_id", models.PositiveIntegerField()),
                ("router_id", models.CharField(blank=True, default="", max_length=64)),
                ("vrf", models.CharField(blank=True, default="", max_length=64)),
                ("areas", models.JSONField(blank=True, default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("unknown", "Unknown"),
                            ("imported", "Imported"),
                            ("accepted", "Accepted"),
                            ("deploying", "Deploying"),
                            ("in_sync", "In Sync"),
                            ("apply_failed", "Apply Failed"),
                            ("conflict", "Conflict"),
                            ("changed", "Changed"),
                            ("error", "Error"),
                        ],
                        default="unknown",
                        max_length=32,
                    ),
                ),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_error", models.TextField(blank=True, default="")),
                (
                    "management",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ospf_instance_states",
                        to="netbox_nso_plugin.nsodevicemanagement",
                    ),
                ),
                (
                    "ospf_instance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="nso_ospf_instance_states",
                        to="netbox_routing.ospfinstance",
                    ),
                ),
            ],
            options={
                "verbose_name": "NSO OSPF Instance State",
                "verbose_name_plural": "NSO OSPF Instance States",
                "ordering": ["management", "process_id"],
                "unique_together": {("management", "process_id")},
            },
        ),
        migrations.CreateModel(
            name="NSOOSPFInterfaceState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=None),
                ),
                ("process_id", models.PositiveIntegerField(blank=True, null=True)),
                ("area_id", models.CharField(blank=True, default="", max_length=64)),
                ("passive", models.BooleanField(default=False)),
                ("priority", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("cost", models.PositiveIntegerField(blank=True, null=True)),
                ("network_type", models.CharField(blank=True, default="", max_length=32)),
                ("auth_type", models.CharField(blank=True, default="", max_length=32)),
                ("auth_present", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("unknown", "Unknown"),
                            ("imported", "Imported"),
                            ("accepted", "Accepted"),
                            ("deploying", "Deploying"),
                            ("in_sync", "In Sync"),
                            ("apply_failed", "Apply Failed"),
                            ("conflict", "Conflict"),
                            ("changed", "Changed"),
                            ("error", "Error"),
                        ],
                        default="unknown",
                        max_length=32,
                    ),
                ),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_at", models.DateTimeField(blank=True, null=True)),
                ("last_apply_error", models.TextField(blank=True, default="")),
                (
                    "interface",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nso_ospf_states",
                        to="dcim.interface",
                    ),
                ),
                (
                    "management",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ospf_interface_states",
                        to="netbox_nso_plugin.nsodevicemanagement",
                    ),
                ),
            ],
            options={
                "verbose_name": "NSO OSPF Interface State",
                "verbose_name_plural": "NSO OSPF Interface States",
                "ordering": ["management", "interface"],
                "unique_together": {("management", "interface")},
            },
        ),
    ]
