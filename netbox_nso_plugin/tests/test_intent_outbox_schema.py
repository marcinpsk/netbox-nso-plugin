# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — the outbox schema, pin O1.1.

The migration declares one in-app parent, lands both tables with an unconstrained entry table,
a state row unique on ``(device, scope)``, a partial index on the unconsumed predicate and a
``NO CYCLE`` sequence, and unapplies one step.
"""

from __future__ import annotations

from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from .mixins import _CascadeFlushMixin

APP = "netbox_nso_plugin"
ENTRY_TABLE = "netbox_nso_plugin_nsointentoutboxentry"
STATE_TABLE = "netbox_nso_plugin_nsointentoutboxstate"


def _outbox_migration() -> str:
    """Return the migration that creates both outbox models."""
    from importlib import import_module

    loader = MigrationLoader(None, ignore_no_migrations=True)
    candidates = []
    for app, name in loader.disk_migrations:
        if app != APP:
            continue
        migration = import_module(f"{APP}.migrations.{name}").Migration
        created = {op.name for op in migration.operations if isinstance(op, migrations.CreateModel)}
        if {"NSOIntentOutboxEntry", "NSOIntentOutboxState"} <= created:
            candidates.append(name)
    assert len(candidates) == 1, f"expected one outbox migration; saw {candidates}"
    return candidates[0]


class TestOutboxMigrationShape(SimpleTestCase):
    """O1.1 — the properties readable off the migration files alone."""

    def test_the_outbox_migration_declares_its_single_in_app_parent(self):
        """The graph parent matches the migration's one declared in-app dependency."""
        from importlib import import_module

        outbox = _outbox_migration()
        loader = MigrationLoader(None, ignore_no_migrations=True)

        migration = import_module(f"{APP}.migrations.{outbox}").Migration
        created = {op.name for op in migration.operations if isinstance(op, migrations.CreateModel)}
        assert {"NSOIntentOutboxEntry", "NSOIntentOutboxState"} <= created, f"the migration creates {created}"
        in_app = [name for app, name in migration.dependencies if app == APP]
        assert len(in_app) == 1, f"the outbox migration must have one in-app parent; saw {in_app}"
        graph_parents = {node.key[1] for node in loader.graph.node_map[(APP, outbox)].parents if node.key[0] == APP}
        assert graph_parents == set(in_app), (
            f"the migration graph parent must match the declared in-app dependency; saw {graph_parents}"
        )

    def test_the_migrations_sequence_name_still_matches_the_running_one(self):
        """0018 inlines the name so a rename cannot rewrite history; this is the drift alarm."""
        from importlib import import_module

        from netbox_nso_plugin import outbox

        historical = import_module(f"{APP}.migrations.0018_intent_outbox").PUSH_SEQ_SEQUENCE
        assert historical == outbox.PUSH_SEQ_SEQUENCE, (
            "outbox.PUSH_SEQ_SEQUENCE was renamed; add a migration that renames the sequence too"
        )

    def test_every_operation_declares_a_reverse(self):
        """A ``RunSQL`` with no ``reverse_sql`` makes the whole migration irreversible."""
        from importlib import import_module

        migration = import_module(f"{APP}.migrations.{_outbox_migration()}").Migration
        irreversible = [op.describe() for op in migration.operations if not op.reversible]
        assert irreversible == []


class TestOutboxSchema(TestCase):
    """O1.1 — what the migration actually left in the database."""

    @staticmethod
    def _unique_constraints(table: str) -> list[str]:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype IN ('u', 'p')",
                [table],
            )
            return [row[0] for row in cur.fetchall()]

    @staticmethod
    def _index_definitions(table: str) -> list[str]:
        with connection.cursor() as cur:
            cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename = %s", [table])
            return [row[0] for row in cur.fetchall()]

    def test_the_entry_table_carries_no_unique_constraint(self):
        """An operator transaction APPENDS: two contributions to one key must never collide."""
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        assert NSOIntentOutboxEntry._meta.unique_together == ()
        assert [c for c in NSOIntentOutboxEntry._meta.constraints if hasattr(c, "fields")] == []
        # The primary key is the only uniqueness the entry table may carry.
        assert self._unique_constraints(ENTRY_TABLE) == [f"{ENTRY_TABLE}_pkey"]

    def test_the_state_row_is_unique_per_device_and_scope(self):
        from netbox_nso_plugin.models import NSOIntentOutboxState

        assert NSOIntentOutboxState._meta.unique_together == (("device", "scope"),)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT array_agg(a.attname ORDER BY a.attname)
                FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                WHERE c.conrelid = %s::regclass AND c.contype = 'u'
                GROUP BY c.conname
                """,
                [STATE_TABLE],
            )
            assert [tuple(row[0]) for row in cur.fetchall()] == [("device_id", "scope")]

    def test_the_unconsumed_predicate_has_its_partial_index(self):
        """The pre-send scan and every fold read it; a full index would scan retired rows too."""
        partial = [d for d in self._index_definitions(ENTRY_TABLE) if "consumed_by_push_seq IS NULL" in d]
        assert partial, f"no partial index on the unconsumed predicate; saw {self._index_definitions(ENTRY_TABLE)}"

    def test_the_push_sequence_is_a_bigint_that_never_cycles(self):
        from netbox_nso_plugin.outbox import PUSH_SEQ_SEQUENCE

        with connection.cursor() as cur:
            cur.execute(
                "SELECT data_type, start_value, increment_by, cycle FROM pg_sequences WHERE sequencename = %s",
                [PUSH_SEQ_SEQUENCE],
            )
            row = cur.fetchone()
        assert row is not None, f"{PUSH_SEQ_SEQUENCE} does not exist"
        data_type, start_value, increment_by, cycle = row
        assert data_type == "bigint"
        assert (start_value, increment_by) == (1, 1)
        assert cycle is False, "a wrapped sequence re-issues a logical operation id"

    def test_an_existing_overlay_migrates_in_with_no_acknowledged_triple(self):
        """NULL is not a gap to fill in: it IS the wire's ``unverified`` flag (R9-B3)."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        field = NSOStaticRouteState._meta.get_field("last_acked_triple")
        assert field.null is True
        assert field.get_default() is None


class TestOutboxMigrationRoundTrip(_CascadeFlushMixin, TransactionTestCase):
    """O1.1 — one step back and one step forward, for real, against the database."""

    def test_the_migration_unapplies_and_reapplies(self):
        from netbox_nso_plugin.outbox import PUSH_SEQ_SEQUENCE

        def _table_exists(table: str) -> bool:
            with connection.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
                return bool(cur.fetchone()[0])

        def _sequence_exists() -> bool:
            with connection.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", [PUSH_SEQ_SEQUENCE])
                return bool(cur.fetchone()[0])

        assert _table_exists(ENTRY_TABLE) and _table_exists(STATE_TABLE) and _sequence_exists()
        executor = MigrationExecutor(connection)
        outbox = _outbox_migration()
        migration = executor.loader.disk_migrations[APP, outbox]
        parents = [name for app, name in migration.dependencies if app == APP]
        assert len(parents) == 1, f"the outbox migration must have one in-app parent; saw {parents}"

        try:
            executor.migrate([(APP, parents[0])])
            assert not _table_exists(ENTRY_TABLE)
            assert not _table_exists(STATE_TABLE)
            # The sequence stays: dropping it would let the re-apply below restart at 1 and
            # re-issue values the adapter already admitted (the reverse is a noop).
            assert _sequence_exists()
        finally:
            # Forward to the app's LEAVES, not to a fixed name: this worker's database is
            # reused by every test after this one, and a branched graph would leave the
            # unnamed branch just as unapplied.
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes(APP))

        assert _table_exists(ENTRY_TABLE) and _table_exists(STATE_TABLE) and _sequence_exists()
