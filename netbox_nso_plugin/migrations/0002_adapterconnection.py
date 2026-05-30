# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0001_initial"),
        ("extras", "0138_customfieldchoiceset_choice_colors"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdapterConnection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "url",
                    models.URLField(
                        blank=True,
                        help_text="nso-adapter base URL (e.g. http://adapter:8000). Overrides the env bootstrap when set.",
                    ),
                ),
                (
                    "verify_tls",
                    models.BooleanField(
                        default=True,
                        help_text="Verify TLS certificates when calling the adapter.",
                    ),
                ),
                (
                    "ca_cert_path",
                    models.CharField(
                        blank=True,
                        help_text="Path to a CA bundle file on the NetBox host. Leave blank to use the system trust store.",
                        max_length=500,
                    ),
                ),
                (
                    "timeout_seconds",
                    models.PositiveIntegerField(
                        default=30,
                        help_text="Request timeout in seconds.",
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(
                        default=True,
                        help_text="When disabled the plugin falls back to PLUGINS_CONFIG / env for all settings.",
                    ),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag", related_name="+"),
                ),
            ],
            options={
                "verbose_name": "Adapter Connection",
                "verbose_name_plural": "Adapter Connection",
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
    ]
