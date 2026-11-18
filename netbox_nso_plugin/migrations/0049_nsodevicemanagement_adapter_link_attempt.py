# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Number the adapter link attempts, so an older attempt cannot overwrite a newer attempt's outcome."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0048_outbox_refused_identity"),
    ]

    operations = [
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="adapter_link_attempt",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="adapter_link_outcome_attempt",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
