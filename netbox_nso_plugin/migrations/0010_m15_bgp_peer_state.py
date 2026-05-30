# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
# Generated for M15: BGP peer compliance state model

import django.db.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0009_m14_isis_interface_state"),
        ("extras", "0138_customfieldchoiceset_choice_colors"),
    ]

    operations = [
        migrations.CreateModel(
            name="NSOBGPPeerState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(
                        help_text="A comma-separated list of tags.",
                        through="extras.TaggedItem",
                        to="extras.Tag",
                        verbose_name="Tags",
                    ),
                ),
                ("asn_str", models.CharField(max_length=10)),
                ("vrf_name", models.CharField(blank=True, default="", max_length=128)),
                ("peer_address_str", models.CharField(max_length=64)),
                ("remote_as_str", models.CharField(blank=True, default="", max_length=10)),
                ("enabled", models.BooleanField(blank=True, null=True)),
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
                    "bgp_peer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="nso_bgp_states",
                        to="netbox_routing.bgppeer",
                    ),
                ),
                (
                    "management",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bgp_peer_states",
                        to="netbox_nso_plugin.nsodevicemanagement",
                    ),
                ),
            ],
            options={
                "verbose_name": "NSO BGP Peer State",
                "verbose_name_plural": "NSO BGP Peer States",
                "ordering": ["management", "asn_str", "vrf_name", "peer_address_str"],
                "unique_together": {("management", "asn_str", "vrf_name", "peer_address_str")},
            },
        ),
    ]
