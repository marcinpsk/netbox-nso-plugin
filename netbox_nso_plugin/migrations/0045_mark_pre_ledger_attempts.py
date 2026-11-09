# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
from django.db import migrations, models


def mark_pre_ledger_attempts(apps, schema_editor):
    attempts = apps.get_model("netbox_nso_plugin", "NSOApplyAttempt").objects.using(schema_editor.connection.alias)
    attempts.filter(trigger_kind="manual", adapter_apply_attempt_id__isnull=True, preflight__isnull=True).update(
        pre_ledger=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0044_nsoapplyattempt_pre_ledger"),
    ]

    operations = [
        migrations.RunPython(mark_pre_ledger_attempts, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="nsoapplyattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("pre_ledger", False),
                    models.Q(
                        ("adapter_apply_attempt_id__isnull", True),
                        ("preflight__isnull", True),
                        ("trigger_kind", "manual"),
                    ),
                    _connector="OR",
                ),
                name="nso_attempt_pre_ledger",
            ),
        ),
    ]
