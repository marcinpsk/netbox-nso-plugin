# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models


def set_initial_default(apps, schema_editor):
    """Make the first existing NSO instance (by name) the default, so deployments
    that already have instances get a sensible default after the upgrade."""
    NSOInstance = apps.get_model("netbox_nso_plugin", "NSOInstance")
    if not NSOInstance.objects.filter(is_default=True).exists():
        first = NSOInstance.objects.order_by("name").first()
        if first is not None:
            first.is_default = True
            first.save(update_fields=["is_default"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0016_management_scopes"),
    ]

    operations = [
        migrations.AddField(
            model_name="nsoinstance",
            name="is_default",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Pre-selected when onboarding a new device. The first instance created "
                    "becomes the default automatically; setting another clears the previous one."
                ),
            ),
        ),
        migrations.RunPython(set_initial_default, noop),
    ]
