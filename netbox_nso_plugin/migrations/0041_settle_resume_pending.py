# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Add the durable settlement resume request and the acknowledge-settlement-issue permission."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0040_snapshot_owner_epoch_index"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="nsodevicemanagement",
            options={
                "ordering": ["device"],
                "permissions": [("acknowledge_settlement_issue", "Acknowledge a settlement issue as outcome unknown")],
            },
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_resume_pending",
            field=models.BooleanField(default=False),
        ),
    ]
