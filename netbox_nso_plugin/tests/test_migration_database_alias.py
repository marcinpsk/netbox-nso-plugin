# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every data migration runs on the database that ``migrate --database`` names."""

from importlib import import_module
from types import SimpleNamespace

from django.apps import apps as django_apps
from django.db import connection, migrations
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils.connection import ConnectionDoesNotExist

from netbox_nso_plugin.models import NSOFamilyReadState

from ._outbox_case import make_managed

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


class WritesElsewhere:
    """A database router that sends every write to the probe alias."""

    def db_for_write(self, model, **hints):
        return PROBE_ALIAS


class TestMigrationWritesIgnoreRouters(TestCase):
    def test_a_data_migration_save_writes_the_schema_editor_database(self):
        _device, management = make_managed("migration-alias", 17401)
        state = NSOFamilyReadState.objects.create(
            management=management, family="vlan", applied_attempt_id=7, applied_incarnation="incarnation-a"
        )
        migration = import_module("netbox_nso_plugin.migrations.0015_readsem_1332_atomic_publication")

        with override_settings(DATABASE_ROUTERS=[WritesElsewhere()]):
            migration.copy_applied_to_admitted(django_apps, SimpleNamespace(connection=connection))

        state.refresh_from_db()
        self.assertEqual((state.admitted_attempt_id, state.admitted_incarnation), (7, "incarnation-a"))
