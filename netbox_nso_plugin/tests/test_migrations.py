# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The plugin's migration graph is one chain, and the models on it match the code.

Two appendices add fields to the same management row off the same head. Two leaves off one
head is not a merge conflict a reviewer notices — Django reports *conflicting migrations*
at runtime and refuses to migrate, and each brief's "single chain" claim reads true in
isolation. So the property is asserted mechanically here, on the graph itself.
"""

from __future__ import annotations

import importlib
from io import StringIO

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

APP = "netbox_nso_plugin"


class TestMigrationGraph(SimpleTestCase):
    def test_sequence_rollback_warning_names_the_reuse_risk(self):
        migration = importlib.import_module("netbox_nso_plugin.migrations.0018_intent_outbox")

        assert "Re-applying it restarts at 1" in migration.__doc__
        sequence = migration.Migration.operations[0]
        assert "DROP SEQUENCE" in sequence.reverse_sql

    def test_cross_app_dependencies_stay_on_the_0001_floor(self):
        """makemigrations pins the GENERATING environment's app heads; a pin newer than
        the 0001 floor breaks DB setup on the oldest supported NetBox (CI matrix floor)."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        ours = {name: mig for (app, name), mig in loader.disk_migrations.items() if app == APP}
        floor = {app: name for app, name in ours["0001_initial"].dependencies if app != APP}
        stray = sorted(
            (name, dep_app, dep_name)
            for name, mig in ours.items()
            for dep_app, dep_name in mig.dependencies
            if dep_app != APP and floor.get(dep_app) != dep_name
        )
        assert not stray, (
            f"cross-app pins off the 0001 floor {floor} — repin to the floor, "
            f"or move the floor deliberately in 0001's successor and here: {stray}"
        )

    def test_the_migration_graph_has_a_single_leaf(self):
        # Built from disk with no connection: two leaves make Django refuse to migrate at
        # all, so a graph check that needs a migrated database cannot report the defect.
        loader = MigrationLoader(None, ignore_no_migrations=True)
        leaves = sorted(name for app, name in loader.graph.leaf_nodes() if app == APP)

        assert len(leaves) == 1, f"{APP} has conflicting leaf migrations: {leaves}"


class TestMigrationsMatchTheModels(TestCase):
    def test_the_models_need_no_further_migration(self):
        """A field added without its migration is a runtime error, not a review finding."""
        out = StringIO()
        try:
            # --check exits the process on pending changes, so the bare call reports a raw
            # SystemExit and never says which model moved.
            call_command("makemigrations", APP, check=True, dry_run=True, verbosity=1, stdout=out)
        except SystemExit:
            self.fail(f"{APP} has model changes with no migration:\n{out.getvalue()}")
