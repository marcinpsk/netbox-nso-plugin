# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
# Generated manually for M12 — NSOInterfaceIPState model

import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0003_phase2_m6_auto_apply_and_interface_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSOInterfaceIPState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "address",
                    models.CharField(
                        max_length=64,
                        help_text="IP address in 'ip/prefix-length' notation (e.g. 10.0.0.1/24).",
                    ),
                ),
                (
                    "vrf",
                    models.CharField(
                        max_length=256,
                        blank=True,
                        default="",
                        help_text="VRF name; empty string means the global routing table.",
                    ),
                ),
                (
                    "family",
                    models.CharField(
                        max_length=8,
                        default="ipv4",
                        help_text="Address family: ipv4 or ipv6 (derived, informational).",
                    ),
                ),
                (
                    "secondary",
                    models.BooleanField(
                        default=False,
                        help_text="True if this is a secondary IP address on the interface.",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        max_length=32,
                        choices=[
                            ("unknown", "Unknown"),
                            ("imported", "Imported"),
                            ("changed", "Changed"),
                            ("accepted", "Accepted"),
                            ("deploying", "Deploying"),
                            ("in_sync", "In Sync"),
                            ("apply_failed", "Apply Failed"),
                            ("drifted", "Drifted"),
                            ("error", "Error"),
                            ("conflict", "Conflict"),
                        ],
                        default="unknown",
                    ),
                ),
                (
                    "nso_value",
                    models.TextField(
                        blank=True,
                        null=True,
                        help_text="Last address string reported by NSO (cached for display).",
                    ),
                ),
                ("last_sync_at", models.DateTimeField(null=True, blank=True)),
                ("accepted_at", models.DateTimeField(null=True, blank=True)),
                ("last_apply_at", models.DateTimeField(null=True, blank=True)),
                (
                    "last_apply_error",
                    models.JSONField(
                        null=True,
                        blank=True,
                        help_text="Populated when status=apply_failed.",
                    ),
                ),
                (
                    "interface",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nso_ip_states",
                        to="dcim.interface",
                    ),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(
                        blank=True,
                        help_text="A comma-separated list of tags.",
                        through="extras.TaggedItem",
                        to="extras.Tag",
                        verbose_name="Tags",
                    ),
                ),
            ],
            options={
                "verbose_name": "NSO Interface IP State",
                "verbose_name_plural": "NSO Interface IP States",
                "ordering": ["interface", "address", "vrf"],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddConstraint(
            model_name="nsointerfaceipstate",
            constraint=models.UniqueConstraint(
                fields=("interface", "address", "vrf"),
                name="unique_nso_interface_ip_state",
            ),
        ),
    ]
