# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — the outbox schema, pin O1.1.

The migration chains off the current end, lands both tables with an unconstrained entry
table, a state row unique on ``(device, scope)``, a partial index on the unconsumed
predicate and a ``NO CYCLE`` sequence, and unapplies one step.
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


def _app_migrations() -> list[str]:
    """Every migration name of this app, in chain order."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return sorted(name for app, name in loader.disk_migrations if app == APP)


def _outbox_migration() -> str:
    """The app's single leaf — Appendix O's migration, whatever number it landed on."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    leaves = [name for app, name in loader.graph.leaf_nodes() if app == APP]
    assert len(leaves) == 1, f"{APP} has conflicting leaf migrations: {leaves}"
    return leaves[0]


class TestOutboxMigrationShape(SimpleTestCase):
    """O1.1 — the properties readable off the migration files alone."""

    def test_the_outbox_migration_chains_off_the_current_end(self):
        """Not off a literal number: whichever appendix landed last is the parent (R12-M3)."""
        from importlib import import_module

        leaf = _outbox_migration()
        names = _app_migrations()
        assert names[-1] == leaf, f"the outbox migration must be the chain end; saw {names[-1]}"

        migration = import_module(f"{APP}.migrations.{leaf}").Migration
        created = {op.name for op in migration.operations if isinstance(op, migrations.CreateModel)}
        assert {"NSOIntentOutboxEntry", "NSOIntentOutboxState"} <= created, f"the leaf creates {created}"
        in_app = [name for app, name in migration.dependencies if app == APP]
        assert in_app == [names[-2]], f"the outbox migration must chain off {names[-2]}; saw {in_app}"

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
        parent = _app_migrations()[-2]

        try:
            executor = MigrationExecutor(connection)
            executor.migrate([(APP, parent)])
            assert not _table_exists(ENTRY_TABLE)
            assert not _table_exists(STATE_TABLE)
            assert not _sequence_exists()
        finally:
            # Forward to the app's LEAVES, not to a fixed name: this worker's database is
            # reused by every test after this one, and a branched graph would leave the
            # unnamed branch just as unapplied.
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes(APP))

        assert _table_exists(ENTRY_TABLE) and _table_exists(STATE_TABLE) and _sequence_exists()
