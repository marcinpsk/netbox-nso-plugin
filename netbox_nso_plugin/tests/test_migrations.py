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
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from ._ownership_case import acquire_overlay
from .mixins import _CascadeFlushMixin

APP = "netbox_nso_plugin"
# Hand-written cross-app pins above the 0001 floor; the schema needs them, and CI installs them.
DELIBERATE_PINS = {("netbox_routing", "0040_case_sensitive_policy_names")}
OUTBOX = "0018_intent_outbox"
PRE_OUTBOX = "0017_settlement_cursor_epoch"
DEPLOYMENT_CONTROL = "0019_intent_deployment_control"
APPLY_IDENTITY = "0020_nsoapplyattempt_nsointentrevision_and_more"
PRE_OWNERSHIP_EXECUTION = "0023_outbox_contribution_kind"


class TestMigrationGraph(SimpleTestCase):
    def test_ledger_choices_are_runtime_metadata(self):
        from django.apps import apps

        loader = MigrationLoader(None, ignore_no_migrations=True)
        state = loader.project_state(loader.graph.leaf_nodes(APP))
        for name in ("NSOApplyOverlaySnapshot", "NSOOverlaySettlementFence"):
            runtime = apps.get_model(APP, name)._meta.get_field("model_label")
            historical = state.models[APP, name.lower()].fields["model_label"]
            self.assertTrue(runtime.choices)
            self.assertNotIn("choices", runtime.deconstruct()[3])
            self.assertEqual(historical.deconstruct()[1:], runtime.deconstruct()[1:])

    def test_interface_ip_allocation_kind_is_added_once(self):
        from django.db import migrations

        loader = MigrationLoader(None, ignore_no_migrations=True)
        additions = [
            name
            for (app, name), migration in loader.disk_migrations.items()
            if app == APP
            for operation in migration.operations
            if isinstance(operation, migrations.AddField)
            and operation.model_name == "nsointerfaceipstate"
            and operation.name == "allocation_kind"
        ]

        self.assertEqual(additions, [APPLY_IDENTITY])

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
            if dep_app != APP and floor.get(dep_app) != dep_name and (dep_app, dep_name) not in DELIBERATE_PINS
        )
        assert not stray, (
            f"cross-app pins off the 0001 floor {floor} — repin to the floor, "
            f"or move the floor deliberately in 0001's successor and here: {stray}"
        )

    def test_the_plugin_schema_requires_case_sensitive_policy_names(self):
        # Exact policy identity cannot store case-distinct names under the pre-0040 Lower(name) constraint.
        loader = MigrationLoader(None, ignore_no_migrations=True)
        (leaf,) = (node for node in loader.graph.leaf_nodes() if node[0] == APP)
        assert ("netbox_routing", "0040_case_sensitive_policy_names") in loader.graph.forwards_plan(leaf)

    def test_the_migration_graph_has_a_single_leaf(self):
        # Built from disk with no connection: two leaves make Django refuse to migrate at
        # all, so a graph check that needs a migrated database cannot report the defect.
        loader = MigrationLoader(None, ignore_no_migrations=True)
        leaves = sorted(name for app, name in loader.graph.leaf_nodes() if app == APP)

        assert leaves == ["0037_delete_stale_observations"]
        assert len(leaves) == 1, f"{APP} has conflicting leaf migrations: {leaves}"
        state = loader.project_state([(APP, leaves[0])])
        for name in (
            "nsoacceptedselection",
            "nsoapplyexecution",
            "nsoapplyoverlaysnapshot",
            "nsooverlaysettlementfence",
            "nsobgprouterstate",
        ):
            assert (APP, name) in state.models, f"missing refusal ledger model: {name}"
        assert "verification_attempt" in state.models[APP, "nsointentdeploymentcontrol"].fields
        assert "paired_activation" in state.models[APP, "nsointentdeploymentcontrol"].fields
        assert "nso_attempt_prune" in {index.name for index in state.models[APP, "nsoapplyattempt"].options["indexes"]}


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
        from django.apps import apps

        for name in (
            "NSOAcceptedSelection",
            "NSOApplyExecution",
            "NSOApplyOverlaySnapshot",
            "NSOOverlaySettlementFence",
            "NSOBGPRouterState",
        ):
            self.assertIsNotNone(apps.get_model(APP, name))


class TestRefusalLedgerMigration(_CascadeFlushMixin, TransactionTestCase):
    def test_refusal_schema_upgrades_from_family_observation(self):
        from django.db.migrations.executor import MigrationExecutor

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        from ._outbox_case import make_device

        device = make_device("refusal-migration")
        instance = NSOInstance.objects.create(name="placeholder-migration", adapter_instance_id="placeholder-migration")
        management = NSODeviceManagement.objects.create(
            device=device, nso_instance=instance, nso_device_name="placeholder-device", adapter_device_id=17
        )
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes(APP)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(leaves))
        parent = (APP, "0033_family_observation")
        executor.migrate([parent])
        old = executor.loader.project_state([parent]).apps
        attempt = old.get_model(APP, "NSOApplyAttempt").objects.create(management_id=management.pk)
        host = old.get_model(APP, "NSOLoggingHostState").objects.create(
            management_id=management.pk, address="192.0.2.1", status="accepted"
        )
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        upgraded = current.get_model(APP, "NSOApplyAttempt").objects.get(pk=attempt.pk)
        self.assertEqual(upgraded.trigger_kind, "manual")
        self.assertIsNone(upgraded.completed_at)
        logging = current.get_model(APP, "NSOLoggingHostState").objects.get(pk=host.pk)
        self.assertEqual(logging.status, "accepted")
        self.assertIsNone(logging.last_apply_at)
        self.assertIsNone(logging.apply_attempt_id)
        self.assertEqual(logging.last_apply_error, "")


class TestDirectSelectionSchema(TestCase):
    def test_direct_selection_constraints(self):
        from django.apps import apps
        from django.utils import timezone

        from ._outbox_case import make_managed

        _device, management = make_managed("direct-selection-migration", 1767)
        model = apps.get_model(APP, "NSOAcceptedSelection")
        model.objects.create(
            management_id=management.pk,
            mapping_epoch="a" * 64,
            stream="vlan",
            source_push_seq=41,
            plugin_revision=7,
            request_digest="b" * 64,
            contributor_manifest=[{"model_label": "netbox_nso_plugin.nsovlanstate", "overlay_pk": 17}],
        )
        values = {
            "management_id": management.pk,
            "mapping_epoch": "c" * 64,
            "stream": "lag",
            "plugin_revision": 8,
            "request_digest": "d" * 64,
        }
        provisional = model.objects.create(**values)
        self.assertIsNone(provisional.source_push_seq)
        self.assertIsNone(provisional.selection_revision)
        for changes in (
            {"stream": "vlan"},
            {"acknowledged_at": timezone.now()},
            {"binding_state": "bound", "store_incarnation": "placeholder-store", "frozen_revision": 1},
            {"selection_revision": 0},
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.create(**(values | changes))
        provisional.selection_revision = 91
        provisional.acknowledged_at = timezone.now()
        provisional.save(update_fields=("selection_revision", "acknowledged_at"))
        self.assertEqual(model.objects.get(pk=provisional.pk).selection_revision, 91)


class TestBGPRouterLedgerSchema(TestCase):
    def test_router_label_is_persistable_after_ledger_migration(self):
        from django.apps import apps

        from ._outbox_case import make_managed

        Router = apps.get_model(APP, "NSOBGPRouterState")
        _device, management = make_managed("router-label-migration", 1767)
        from netbox_nso_plugin.intent_state import offline_mutation

        with transaction.atomic(), offline_mutation():
            router = Router.objects.create(management=management, asn_str="64512")
        attempt = apps.get_model(APP, "NSOApplyAttempt").objects.create(management=management)
        snapshot = apps.get_model(APP, "NSOApplyOverlaySnapshot").objects.create(
            attempt=attempt,
            model_label=router._meta.label_lower,
            overlay_pk=router.pk,
            mapping_epoch="a" * 64,
            prior_status="imported",
            expected_status="deploying",
            content_digest="b" * 64,
        )
        fence = apps.get_model(APP, "NSOOverlaySettlementFence").objects.create(
            management=management,
            mapping_epoch="a" * 64,
            model_label=router._meta.label_lower,
            overlay_pk=router.pk,
            current_attempt=attempt,
        )
        snapshot.refresh_from_db()
        fence.refresh_from_db()
        self.assertEqual(snapshot.overlay_pk, router.pk)
        self.assertEqual(fence.overlay_pk, router.pk)
        for ledger_row in (snapshot, fence):
            self.assertIn(router._meta.label_lower, dict(ledger_row._meta.get_field("model_label").choices))


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

        with connection.schema_editor() as schema_editor:
            deployment_control.backfill_active_claim_flags(apps, schema_editor)

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
            row = acquire_overlay(
                NSOLoggingLevelState,
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

        self._migrate(DEPLOYMENT_CONTROL)
        self._migrate(APPLY_IDENTITY)

        single.refresh_from_db(fields=["allocation_kind"])
        peer.refresh_from_db(fields=["allocation_kind"])
        point_to_point.refresh_from_db(fields=["allocation_kind"])
        self.assertEqual(single.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_SINGLE)
        self.assertEqual(peer.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_P2P)
        self.assertEqual(point_to_point.allocation_kind, NSOInterfaceIPState.ALLOCATION_KIND_P2P)


class TestOwnershipIdentityMigration(_CascadeFlushMixin, TransactionTestCase):
    def _migrate(self, target):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(APP, target)])
        return executor.loader.project_state([(APP, target)]).apps

    def _migrate_to_leaves(self):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes(APP))

    def test_pre_target_manifests_are_discarded(self):
        from ._outbox_case import make_managed

        device, _management = make_managed("ownership-identity-migration", 1627)
        self.addCleanup(self._migrate_to_leaves)
        old_apps = self._migrate(PRE_OWNERSHIP_EXECUTION)
        OldManifest = old_apps.get_model(APP, "NSOOwnershipManifest")
        OldManifest.objects.create(
            device_id=device.pk,
            scope="interface",
            native_model_label="dcim.interface",
            native_key={"device_id": device.pk, "name": "Ethernet1"},
        )

        self._migrate_to_leaves()

        from netbox_nso_plugin.models import NSOOwnershipManifest

        self.assertFalse(NSOOwnershipManifest.objects.exists())


class TestDeploymentProbeSchema(_CascadeFlushMixin, TransactionTestCase):
    def test_probe_gate_schema_preserves_quiesced_state(self):
        from django.db.migrations.executor import MigrationExecutor
        from django.db.models import PROTECT
        from django.utils import timezone

        from ._outbox_case import make_managed

        _device, management = make_managed("probe-schema", 1767)
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes(APP)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(leaves))
        parent = (APP, "0035_owner_revision")
        executor.migrate([parent])
        old = executor.loader.project_state([parent]).apps
        stamp = timezone.now()
        old.get_model(APP, "NSOIntentDeploymentControl").objects.create(pk=1, quiesced_at=stamp)
        attempt = old.get_model(APP, "NSOApplyAttempt").objects.create(management_id=management.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        Control = current.get_model(APP, "NSOIntentDeploymentControl")
        control = Control.objects.get(pk=1)
        self.assertEqual(control.quiesced_at, stamp)
        self.assertIsNone(control.verification_attempt_id)
        self.assertFalse(control.paired_activation)
        self.assertTrue(current.get_model(APP, "NSOApplyAttempt").objects.filter(pk=attempt.pk).exists())
        self.assertIs(Control._meta.get_field("verification_attempt").remote_field.on_delete, PROTECT)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Control.objects.filter(pk=1).update(verification_attempt_id=attempt.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Control.objects.filter(pk=1).update(
                quiesced_at=None, paired_activation=True, verification_attempt_id=attempt.pk
            )
        self.assertIn(
            "nso_attempt_prune", {index.name for index in current.get_model(APP, "NSOApplyAttempt")._meta.indexes}
        )
