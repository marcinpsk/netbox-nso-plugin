# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Add the fleet-wide switch used to quiesce intent work during a deployment."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0018_intent_outbox")]

    operations = [
        migrations.CreateModel(
            name="NSOIntentDeploymentControl",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False),
                ),
                ("quiesced_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "NSO Intent Deployment Control",
                "verbose_name_plural": "NSO Intent Deployment Control",
            },
        ),
    ]
