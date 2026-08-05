# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The plugin's migration graph is one chain, and the models on it match the code.

Two appendices add fields to the same management row off the same head. Two leaves off one
head is not a merge conflict a reviewer notices — Django reports *conflicting migrations*
at runtime and refuses to migrate, and each brief's "single chain" claim reads true in
isolation. So the property is asserted mechanically here, on the graph itself.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

APP = "netbox_nso_plugin"


class TestMigrationGraph(SimpleTestCase):
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
        call_command("makemigrations", APP, check=True, dry_run=True, verbosity=1, stdout=out)
