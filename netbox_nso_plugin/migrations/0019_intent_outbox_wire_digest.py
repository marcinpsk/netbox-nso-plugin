# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — two digests on the outbox state row, named for their two jobs.

One value cannot answer both questions. The adapter's receipt digests the raw body it
received (§4.4), so a restore comparing it against a plugin-internal structure could never
match and always failed closed. That structure is still needed: the mode, the deletion
authority and the legacy flag ride as query flags rather than in the body, so the unchanged
claim drop has to discriminate on facts no wire digest carries.

``claim_digest`` therefore becomes ``claim_identity`` (what it always held) and
``claim_wire_digest`` is added beside it; ``last_success_digest`` becomes
``last_success_identity`` for the same reason.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0018_intent_outbox")]

    operations = [
        migrations.RenameField(
            model_name="nsointentoutboxstate",
            old_name="claim_digest",
            new_name="claim_identity",
        ),
        migrations.RenameField(
            model_name="nsointentoutboxstate",
            old_name="last_success_digest",
            new_name="last_success_identity",
        ),
        migrations.AddField(
            model_name="nsointentoutboxstate",
            name="claim_wire_digest",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
