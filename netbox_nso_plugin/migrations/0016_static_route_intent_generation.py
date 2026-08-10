# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1396 R3 P0b — the schema the static-route settlement half is built on.

The overlay gains its intent generation and the expectation an apply result is correlated
against; the management row gains the settlement cursor and the per-scope intent-push
rejection record. The generation allocator is a database sequence, created here — see
``netbox_nso_plugin/intent_generation.py`` for why it is not a column.

Existing rows migrate to the ``0`` sentinel and a NULL expectation: nothing that predates
the first allocation may correlate with a result.
"""

from django.db import migrations, models

from netbox_nso_plugin.intent_generation import SEQUENCE_NAME


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0015_readsem_1332_atomic_publication")]

    operations = [
        migrations.RunSQL(
            sql=f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME} AS bigint START WITH 1 INCREMENT BY 1 NO CYCLE;",
            reverse_sql=f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME};",
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_cursor_seq",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_cursor_incarnation",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="intent_push_errors",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="intent_push_attempts",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="nsostaticroutestate",
            name="intent_generation",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="nsostaticroutestate",
            name="generation_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsostaticroutestate",
            name="expected_generation",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsostaticroutestate",
            name="expected_fingerprint",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="nsostaticroutestate",
            name="last_result_advisory",
            field=models.TextField(blank=True, default=""),
        ),
    ]
