# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep invalid settlement evidence visible across worker restarts."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0037_delete_stale_observations")]

    operations = [
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_evidence_error",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
