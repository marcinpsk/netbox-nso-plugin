# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Record the request the adapter refused deterministically, so the drain does not resend it."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0047_owned_acceptance_times"),
    ]

    operations = [
        migrations.AddField(
            model_name="nsointentoutboxstate",
            name="refused_identity",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
