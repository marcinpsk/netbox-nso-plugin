# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
from django.db import migrations, models
from django.db.models.functions import Coalesce


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0045_mark_pre_ledger_attempts"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="nsoapplyattempt",
            name="nso_attempt_prune",
        ),
        migrations.AddIndex(
            model_name="nsoapplyattempt",
            index=models.Index(
                Coalesce("completed_at", "created_at"),
                models.F("id"),
                condition=models.Q(
                    ("lifecycle", "terminal"),
                    models.Q(
                        ("adapter_apply_attempt_id__isnull", True), ("pre_ledger", False), ("trigger_kind", "manual")
                    ),
                    _connector="OR",
                ),
                name="nso_attempt_prune",
            ),
        ),
    ]
