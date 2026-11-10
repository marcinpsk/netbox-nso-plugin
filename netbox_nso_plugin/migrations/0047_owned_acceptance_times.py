# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Give stored owned overlays an explicit acceptance time."""

from django.db import migrations
from django.db.models.functions import Coalesce
from django.utils import timezone


def backfill_owned_acceptance(apps, schema_editor):
    now = timezone.now()
    for model in apps.get_app_config("netbox_nso_plugin").get_models():
        fields = {field.name for field in model._meta.concrete_fields}
        if {"status", "accepted_at", "last_sync_at"} <= fields:
            model.objects.using(schema_editor.connection.alias).filter(
                status__in=("accepted", "deploying", "in_sync", "apply_failed"), accepted_at__isnull=True
            ).update(accepted_at=Coalesce("last_sync_at", now))


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0046_attempt_prune_index_pre_ledger")]
    operations = [migrations.RunPython(backfill_owned_acceptance, migrations.RunPython.noop)]
