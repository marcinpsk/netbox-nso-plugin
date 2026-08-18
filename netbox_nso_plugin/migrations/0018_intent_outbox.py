# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — the durable push outbox.

Two tables and a sequence. ``NSOIntentOutboxEntry`` is appended by the operator's own
transaction and carries no unique constraint: two transactions contributing to one delivery
key must never collide, and compaction is what collapses them. Its partial index is the one
the fold and the pre-send scan read, so it covers only the unconsumed rows.
``NSOIntentOutboxState`` is one row per ``(device, scope)`` and is the lock every drain-side
operation takes, which is what makes a claim and a compaction pass mutually exclusive.

``nso_intent_push_seq`` names a logical operation, not an attempt: it is replayed on
takeover and burned on abandon, so it must never wrap — a re-issued value would let the
adapter admit a replay as new work.

Reversing this migration drops the sequence. Re-applying it restarts at 1 and can reuse
values that the adapter already admitted. Roll back this migration only when you also
discard the adapter receipts for every key.

``last_acked_triple`` starts NULL for every existing overlay, and that is not a gap to fill
later. Stamping the live mirror would record content the adapter never acknowledged; NULL is
the wire's ``unverified`` flag and the adapter classifies it conservatively.

Numbering: Appendix S landed first, so this chains off ``0017``. The binding rule is the
single chain, not the number — read the chain end, not this sentence.
"""

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models

# Inlined on purpose: a migration records a fixed historical change, so it must not follow a
# later rename of ``outbox.PUSH_SEQ_SEQUENCE``.
PUSH_SEQ_SEQUENCE = "nso_intent_push_seq"


class Migration(migrations.Migration):
    dependencies = [
        # The 0001 floor, NOT the generating environment's dcim head (test_migrations pins this).
        ("dcim", "0234_cablepath_nodes_index"),
        ("netbox_nso_plugin", "0017_settlement_cursor_epoch"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"CREATE SEQUENCE IF NOT EXISTS {PUSH_SEQ_SEQUENCE} AS bigint START WITH 1 INCREMENT BY 1 NO CYCLE;",
            reverse_sql=f"DROP SEQUENCE IF EXISTS {PUSH_SEQ_SEQUENCE};",
        ),
        migrations.AddField(
            model_name="nsostaticroutestate",
            name="last_acked_triple",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.CreateModel(
            name="NSOIntentOutboxEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("scope", models.CharField(max_length=32)),
                ("batch_id", models.BigIntegerField()),
                ("transitions", models.JSONField(blank=True, default=list)),
                ("mark_and", models.BooleanField(default=False)),
                ("mark_any", models.BooleanField(default=False)),
                ("consumed_by_push_seq", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nso_intent_outbox_entries",
                        to="dcim.device",
                    ),
                ),
            ],
            options={
                "verbose_name": "NSO Intent Outbox Entry",
                "verbose_name_plural": "NSO Intent Outbox Entries",
                "ordering": ["id"],
                "indexes": [
                    models.Index(
                        condition=models.Q(("consumed_by_push_seq__isnull", True)),
                        fields=["device", "scope"],
                        name="nso_outbox_unconsumed",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="NSOIntentOutboxState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("scope", models.CharField(max_length=32)),
                ("queued_deletions", models.JSONField(blank=True, default=list)),
                ("revoked_ids", models.JSONField(blank=True, default=list)),
                ("push_seq", models.BigIntegerField(blank=True, null=True)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("claim_payload", models.JSONField(blank=True, null=True)),
                ("claim_deletions", models.JSONField(blank=True, default=list)),
                ("claim_flags", models.JSONField(blank=True, default=dict)),
                ("claim_identity", models.CharField(blank=True, default="", max_length=64)),
                ("claim_mark", models.BooleanField(blank=True, null=True)),
                ("lineage_carry", models.JSONField(blank=True, default=dict)),
                ("last_success_identity", models.CharField(blank=True, default="", max_length=64)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.IntegerField(default=0)),
                ("last_drain_attempted_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_error_code", models.CharField(blank=True, default="", max_length=64)),
                ("last_error_at", models.DateTimeField(blank=True, null=True)),
                ("degraded_deletions", models.JSONField(blank=True, default=list)),
                ("fence_withheld_since", models.DateTimeField(blank=True, null=True)),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nso_intent_outbox_states",
                        to="dcim.device",
                    ),
                ),
            ],
            options={
                "verbose_name": "NSO Intent Outbox State",
                "verbose_name_plural": "NSO Intent Outbox States",
                "ordering": ["device", "scope"],
                "unique_together": {("device", "scope")},
            },
        ),
    ]
