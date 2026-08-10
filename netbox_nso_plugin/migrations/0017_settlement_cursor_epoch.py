# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S4) — the cursor epoch, the durable stall triple, the repair clock.

``0016`` gave the management row a cursor keyed on the store incarnation alone. That is
not enough: the adapter's settlement counter is scoped to its ``Device`` primary key, and
several live sites hand one management row a different adapter device inside one
incarnation, each starting at sequence 1. ``settle_cursor_device_id`` is the other half of
the epoch, compared on read so no write-site enumeration can go stale.

The stall triple bounds one unresolvable settlement so it cannot block a device forever,
and it is on the row rather than in memory because the bound has to survive a restart.

``adapter_link_attempted_at`` is the link repair's fairness clock: the repair is capped
per run, so its traversal needs a durable least-recently-attempted order.

Numbering: Appendix O reserves the next free number for its own migration, so whichever
appendix lands second chains off the other. Appendix O had not landed when this was
written, so Appendix S takes ``0017`` off ``0016`` and O rebases onto it — the binding
rule is the single chain, not the pair of numbers.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0016_static_route_intent_generation")]

    operations = [
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_cursor_device_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_stall_seq",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_stall_attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="settle_stall_first_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="nsodevicemanagement",
            name="adapter_link_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
