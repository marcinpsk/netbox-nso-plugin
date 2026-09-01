# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Add the fleet-wide switch used to quiesce intent work during a deployment."""

import hashlib
import json

from django.db import migrations, models

LEGACY_CLAIM_FIELDS = frozenset({"mode", "mark_any", "force"})
LEGACY_MARKING_MODE = "query_flag"


def _identity(state, management, flags):
    """Return the identity produced by the new claim shape for one old active claim."""
    material = {
        "payload": state.claim_payload,
        "mode": flags["mode"],
        "marking_mode": flags["marking_mode"],
        "deletions": sorted(int(record["route_id"]) for record in state.claim_deletions or []),
        "mark": bool(state.claim_mark),
        "epoch": [
            management.adapter_device_id,
            management.reset_pending_incarnation or management.adapter_incarnation or "",
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def backfill_active_claim_flags(apps, schema_editor):
    """Preserve active 0018 claims when the validated flag shape takes effect."""
    management_model = apps.get_model("netbox_nso_plugin", "NSODeviceManagement")
    state_model = apps.get_model("netbox_nso_plugin", "NSOIntentOutboxState")
    for state in state_model.objects.filter(push_seq__isnull=False).iterator():
        if not isinstance(state.claim_flags, dict) or set(state.claim_flags) != LEGACY_CLAIM_FIELDS:
            continue
        flags = {**state.claim_flags, "marking_mode": LEGACY_MARKING_MODE}
        updates = {"claim_flags": flags}
        management = management_model.objects.filter(
            device_id=state.device_id,
            adapter_device_id__isnull=False,
        ).first()
        if management is not None:
            updates["claim_identity"] = _identity(state, management, flags)
        state_model.objects.filter(pk=state.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0018_intent_outbox")]

    operations = [
        migrations.CreateModel(
            name="NSOIntentDeploymentControl",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False),
                ),
                ("quiesced_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "NSO Intent Deployment Control",
                "verbose_name_plural": "NSO Intent Deployment Control",
            },
        ),
        migrations.RunPython(backfill_active_claim_flags, migrations.RunPython.noop),
    ]
