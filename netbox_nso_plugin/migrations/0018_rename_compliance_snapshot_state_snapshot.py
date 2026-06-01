# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Rename NSODeviceManagement.compliance_snapshot -> state_snapshot (Stage 2 rename).

Part of dropping the misleading 'compliance' naming. RenameField preserves the
column's data (it is an ALTER TABLE ... RENAME COLUMN, not drop+add)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_nso_plugin", "0017_nsoinstance_is_default"),
    ]

    operations = [
        migrations.RenameField(
            model_name="nsodevicemanagement",
            old_name="compliance_snapshot",
            new_name="state_snapshot",
        ),
    ]
