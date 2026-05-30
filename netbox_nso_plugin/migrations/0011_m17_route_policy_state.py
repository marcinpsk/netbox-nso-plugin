# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
# Generated for M17 — NSORoutePolicyState model.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("netbox_nso_plugin", "0010_m15_bgp_peer_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSORoutePolicyState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=None),
                ),
                (
                    "management",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="route_policy_states",
                        to="netbox_nso_plugin.nsodevicemanagement",
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                ("object_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("family", models.CharField(max_length=32)),
                ("object_name", models.CharField(max_length=256)),
                ("content_hash", models.CharField(blank=True, default="", max_length=64)),
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
            ],
            options={
                "verbose_name": "NSO Route Policy State",
                "verbose_name_plural": "NSO Route Policy States",
                "ordering": ["management", "family", "object_name"],
            },
        ),
        migrations.AddConstraint(
            model_name="nsoroutepolicystate",
            constraint=models.UniqueConstraint(
                fields=("management", "family", "object_name"),
                name="netbox_nso_plugin_nsoroutepolicystate_management_family_name_uniq",
            ),
        ),
    ]
