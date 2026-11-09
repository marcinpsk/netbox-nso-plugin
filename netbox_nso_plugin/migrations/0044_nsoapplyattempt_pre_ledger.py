# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0043_attempt_prune_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="nsoapplyattempt",
            name="pre_ledger",
            field=models.BooleanField(default=False),
        ),
    ]
