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
from django.db import connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from .mixins import _CascadeFlushMixin

APP = "netbox_nso_plugin"
OUTBOX = "0018_intent_outbox"
PRE_OUTBOX = "0017_settlement_cursor_epoch"
DEPLOYMENT_CONTROL = "0019_intent_deployment_control"
APPLY_IDENTITY = "0020_nsoapplyattempt_nsointentrevision_and_more"


class TestMigrationGraph(SimpleTestCase):
    def test_the_push_sequence_reverse_is_a_noop(self):
        from django.db import migrations

        migration = importlib.import_module(f"netbox_nso_plugin.migrations.{OUTBOX}")

        assert migration.Migration.operations[0].reverse_sql is migrations.RunSQL.noop

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


class TestDeploymentControlMigration(TestCase):
    def test_active_legacy_claims_gain_their_original_marking_mode_and_identity(self):
        from django.apps import apps

        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        from ._outbox_case import make_managed

        deployment_control = importlib.import_module(f"netbox_nso_plugin.migrations.{DEPLOYMENT_CONTROL}")
        device, management = make_managed("claimflags-migration", 7804)
        payload = {"routes": []}
        state = NSOIntentOutboxState.objects.create(
            device=device,
            scope="static_route",
            push_seq=41,
            claim_payload=payload,
            claim_deletions=[],
            claim_flags={"mode": delivery.MODE_NORMAL, "mark_any": False, "force": False},
            claim_identity="legacy-identity",
            claim_mark=False,
        )

        deployment_control.backfill_active_claim_flags(apps, None)

        state.refresh_from_db()
        self.assertEqual(
            state.claim_flags,
            {
                "mode": delivery.MODE_NORMAL,
                "marking_mode": delivery.MARKING_QUERY_FLAG,
                "mark_any": False,
                "force": False,
            },
        )
        self.assertEqual(
            state.claim_identity,
            drain.request_identity(
                payload,
                mode=delivery.MODE_NORMAL,
                marking_mode=delivery.MARKING_QUERY_FLAG,
                deletions=[],
                mark=False,
                epoch=drain.mapping_epoch(management),
            ),
        )


class TestThePushSequenceOutlivesARollback(_CascadeFlushMixin, TransactionTestCase):
    """``nso_intent_push_seq`` names a logical operation, is replayed on takeover and burned
    on abandon, so it must never wrap: a re-issued value would let the adapter admit a replay
    as new work. The forward SQL is ``CREATE SEQUENCE IF NOT EXISTS``, so a reverse that
    dropped it made a re-apply restart at 1."""

    def _migrate(self, target):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(APP, target)])

    def _migrate_to_leaves(self):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes(APP))

    def _nextval(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval('nso_intent_push_seq');")
            return cursor.fetchone()[0]

    def test_a_rollback_and_re_apply_never_re_issues_a_burnt_value(self):
        # However this ends, the worker's database goes back to the graph's LEAVES, not to a
        # fixed name: this worker's database is reused by every test after this one, and a
        # branch that adds a later migration would stay unapplied for all of them.
        self.addCleanup(self._migrate_to_leaves)

        burnt = max(self._nextval() for _ in range(3))

        self._migrate(PRE_OUTBOX)
        self._migrate(OUTBOX)

        assert self._nextval() > burnt, "the re-applied sequence re-issues values the adapter already admitted"


class TestApplyIdentityMigration(_CascadeFlushMixin, TransactionTestCase):
    def _migrate(self, target):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(APP, target)])

    def _migrate_to_leaves(self):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes(APP))

    def test_unattributed_deploying_rows_return_to_operator_pending(self):
        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.models import NSOLoggingLevelState

        from ._outbox_case import make_managed, without_commit_drain

        _device, management = make_managed("apply-migration", 1625)
        with without_commit_drain(), transaction.atomic():
            row = NSOLoggingLevelState.objects.create(
                management=management,
                console_severity="WARNING",
                status="accepted",
                last_apply_error="stale result",
            )
        self.addCleanup(self._migrate_to_leaves)
        self._migrate(DEPLOYMENT_CONTROL)
        with transaction.atomic(), offline_mutation():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE netbox_nso_plugin_nsologginglevelstate SET status = %s WHERE id = %s",
                    ["deploying", row.pk],
                )

        self._migrate(APPLY_IDENTITY)

        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertIsNone(row.apply_attempt_id)
        self.assertEqual(row.last_apply_error, "")

    def test_auto_assigned_rows_keep_their_cleanup_shape(self):
        from dcim.models import Interface
        from ipam.models import Prefix

        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.models import NSOInterfaceIPState

        from ._outbox_case import make_managed

        device, _management = make_managed("allocation-kind-migration", 1627)
        interface = Interface.objects.create(device=device, name="Ethernet1", type="1000base-t")
        pool = Prefix.objects.create(prefix="198.18.96.0/24")
        with transaction.atomic(), offline_mutation():
            single = NSOInterfaceIPState.objects.create(
                interface=interface,
                address="198.18.96.1/32",
                auto_assigned=True,
                source_pool=pool,
            )
            peer = NSOInterfaceIPState.objects.create(
                interface=interface,
                address="198.18.96.2/31",
                auto_assigned=True,
                source_pool=pool,
            )
            point_to_point = NSOInterfaceIPState.objects.create(
                interface=interface,
                address="198.18.96.3/31",
                auto_assigned=True,
                source_pool=pool,
                peer_state=peer,
            )
            peer.peer_state = point_to_point
            peer.save(update_fields=["peer_state"])
        self.addCleanup(self._migrate_to_leaves)

        self._migrate(PRE_APPLY_IDENTITY)
        self._migrate(APPLY_IDENTITY)

        single.refresh_from_db()
        peer.refresh_from_db()
        point_to_point.refresh_from_db()
        self.assertEqual(single.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_SINGLE)
        self.assertEqual(peer.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_P2P)
        self.assertEqual(point_to_point.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_P2P)
