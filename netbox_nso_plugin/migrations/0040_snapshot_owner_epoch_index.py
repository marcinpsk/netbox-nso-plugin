# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Index an owner's apply snapshots by owner key and mapping epoch."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0039_settlement_issue"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="nsoapplyoverlaysnapshot",
            index=models.Index(
                fields=["model_label", "overlay_pk", "mapping_epoch", "expected_fence_revision"],
                name="nso_snapshot_owner_epoch",
            ),
        ),
    ]
