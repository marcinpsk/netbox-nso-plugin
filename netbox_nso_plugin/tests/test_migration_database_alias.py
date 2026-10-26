# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every data migration runs on the database that ``migrate --database`` names."""

from types import SimpleNamespace

from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase
from django.utils.connection import ConnectionDoesNotExist

APP = "netbox_nso_plugin"
#: Not configured: a data migration that honors the schema editor's alias fails to connect to it.
PROBE_ALIAS = "nso_migration_alias_probe"


class TestMigrationDatabaseAlias(SimpleTestCase):
    def test_every_data_migration_queries_the_schema_editor_alias(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        schema_editor = SimpleNamespace(connection=SimpleNamespace(alias=PROBE_ALIAS))
        checked = []
        for (app, name), migration in sorted(loader.disk_migrations.items()):
            if app != APP:
                continue
            for operation in migration.operations:
                if not isinstance(operation, migrations.RunPython):
                    continue
                apps = loader.project_state((app, name), at_end=False).apps
                checked.append((name, operation.code.__name__))
                with self.subTest(migration=name, function=operation.code.__name__):
                    with self.assertRaises(ConnectionDoesNotExist):
                        operation.code(apps, schema_editor)
        self.assertGreaterEqual(len(checked), 6, checked)
