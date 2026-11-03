# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0041_settle_resume_pending"),
    ]

    operations = [
        migrations.AddField(
            model_name="nsoapplyattempt",
            name="preflight",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsoapplyattempt",
            name="send_seq",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
