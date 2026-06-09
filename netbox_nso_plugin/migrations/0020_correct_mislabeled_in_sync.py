# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Correct legacy ``in_sync`` rows that were never owned.

Older overlays used ``in_sync`` to mean "materialized into NetBox / matches the
device" for UNOWNED rows. The unified status machine reserves ``in_sync`` for
*owned, applied, confirmed*; an unowned resting row is ``imported``. Ownership is
signalled by ``accepted_at`` being set, so any ``in_sync`` row with no
``accepted_at`` is a mislabel and is corrected to ``imported``.

Without this, ``status_machine.is_owned`` (which keys off the status) would treat
those rows as owned and the reconcilers would never re-evaluate them.
"""

from __future__ import annotations

from django.db import migrations


def correct_mislabeled_in_sync(apps, schema_editor):
    for model in apps.get_app_config("netbox_nso_plugin").get_models():
        field_names = {f.name for f in model._meta.get_fields()}
        if "status" not in field_names or "accepted_at" not in field_names:
            continue
        model.objects.filter(status="in_sync", accepted_at__isnull=True).update(status="imported")


def noop(apps, schema_editor):
    """Irreversible in practice: we cannot tell which corrected rows were mislabeled."""


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0019_nsobfdinterfacestate")]

    operations = [migrations.RunPython(correct_mislabeled_in_sync, noop)]
