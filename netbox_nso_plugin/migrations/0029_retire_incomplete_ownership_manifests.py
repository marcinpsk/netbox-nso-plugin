# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Retire manifest rows that predate the native-id and state-model identity columns."""

from django.db import migrations, models


def _retire_incomplete_manifests(apps, _schema_editor):
    """Close every owned manifest that cannot name its native row or its overlay.

    0026 and 0027 added ``native_id``, ``state_model_label`` and ``state_key`` with no
    backfill, and neither value can be recovered from the manifest alone. An owned row
    missing either one is not usable ownership evidence, so retire it here; the next
    renderer audit rebuilds the identity from the surviving native rows and overlays.
    """
    manifest = apps.get_model("netbox_nso_plugin", "NSOOwnershipManifest")
    manifest.objects.filter(ownership_state="owned").filter(
        models.Q(native_id__isnull=True) | models.Q(state_model_label="")
    ).update(ownership_state="retired")


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0028_alter_nsoownershipmanifest_options_and_more"),
    ]

    operations = [
        migrations.RunPython(_retire_incomplete_manifests, migrations.RunPython.noop),
    ]
