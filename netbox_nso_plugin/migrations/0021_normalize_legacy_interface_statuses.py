# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Fold the legacy interface-overlay statuses into the unified vocabulary.

``drifted`` was a pre-unification synonym for ``changed`` (NSOInterfaceState got it
from the adapter; the plugin now normalises it at ingest). ``reserved`` was a dead
choice on NSOInterfaceIPState (the real reservation lives on ipam.IPAddress). This
converts any rows still carrying the legacy values so they match the trimmed choices.
"""

from __future__ import annotations

from django.db import migrations


def fold_legacy_statuses(apps, schema_editor):
    NSOInterfaceState = apps.get_model("netbox_nso_plugin", "NSOInterfaceState")
    NSOInterfaceIPState = apps.get_model("netbox_nso_plugin", "NSOInterfaceIPState")
    NSOInterfaceState.objects.filter(status="drifted").update(status="changed")
    NSOInterfaceIPState.objects.filter(status="drifted").update(status="changed")
    NSOInterfaceIPState.objects.filter(status="reserved").update(status="imported")


def noop(apps, schema_editor):
    """Irreversible: the original drifted/reserved values are not recoverable."""


class Migration(migrations.Migration):
    dependencies = [("netbox_nso_plugin", "0020_correct_mislabeled_in_sync")]

    operations = [migrations.RunPython(fold_legacy_statuses, noop)]
