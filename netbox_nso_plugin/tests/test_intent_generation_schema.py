# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1396 R3 P0b — the intent-generation schema and its allocator.

Pins P0b.1 (the migration is reversible and leaves existing rows unallocated), P0b.2
(``_adopt_incarnation``'s ``update_fields`` allow-list does not clobber the new management
columns) and P0b.3 (the allocator survives a delete/recreate of the management row).
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .mixins import IntentPushResetMixin, _CascadeFlushMixin

APP = "netbox_nso_plugin"
BEFORE = "0015_readsem_1332_atomic_publication"
AFTER = "0016_static_route_intent_generation"


def _make_device(tag: str):
    mfg = Manufacturer.objects.create(name=f"Gen{tag}Mfg", slug=f"gen{tag}mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"Gen{tag}Dev", slug=f"gen{tag}dev")
    role = DeviceRole.objects.create(name=f"Gen{tag}Role", slug=f"gen{tag}role")
    site = Site.objects.create(name=f"Gen{tag}Site", slug=f"gen{tag}site")
    return Device.objects.create(name=f"gen-{tag}-rtr", device_type=dt, role=role, site=site)


def _make_mgmt(device, tag: str, adapter_device_id: int):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    inst, _ = NSOInstance.objects.get_or_create(name=f"gen-{tag}-inst", defaults={"adapter_instance_id": f"gen-{tag}"})
    return NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"nso-gen-{tag}",
        adapter_device_id=adapter_device_id,
    )


class TestIntentGenerationMigration(IntentPushResetMixin, TestCase):
    """P0b.1 — the schema lands unallocated, and the migration can be unapplied."""

    def test_new_overlay_columns_default_to_the_unallocated_sentinel(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.intent_generation import UNALLOCATED
        from netbox_nso_plugin.models import NSOStaticRouteState

        device = _make_device("def")
        mgmt = _make_mgmt(device, "def", 9101)
        sr = StaticRoute.objects.create(prefix="10.9.0.0/16", next_hop="10.9.0.1", metric=1)
        row = NSOStaticRouteState.objects.create(management=mgmt, static_route=sr, status="accepted")

        row.refresh_from_db()
        assert row.intent_generation == UNALLOCATED == 0
        assert row.generation_started_at is None
        assert row.expected_generation is None
        assert row.expected_fingerprint == ""
        assert row.last_result_advisory == ""

    def test_new_management_columns_default_to_an_empty_record(self):
        device = _make_device("mdef")
        mgmt = _make_mgmt(device, "mdef", 9102)

        mgmt.refresh_from_db()
        assert mgmt.settle_cursor_seq is None
        assert mgmt.settle_cursor_incarnation == ""
        assert mgmt.intent_push_errors == {}
        assert mgmt.intent_push_attempts == {}

    def test_every_operation_declares_a_reverse(self):
        """A RunSQL with no ``reverse_sql`` makes the whole migration irreversible."""
        from importlib import import_module

        migration = import_module(f"netbox_nso_plugin.migrations.{AFTER}").Migration
        irreversible = [op.describe() for op in migration.operations if not op.reversible]
        assert irreversible == []


class TestIntentGenerationMigrationRoundTrip(_CascadeFlushMixin, TransactionTestCase):
    """P0b.1 — unapply and re-apply for real, against the database."""

    def test_migration_unapplies_and_reapplies(self):
        from netbox_nso_plugin.intent_generation import SEQUENCE_NAME

        def _sequence_exists() -> bool:
            with connection.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", [SEQUENCE_NAME])
                return bool(cur.fetchone()[0])

        def _columns() -> set[str]:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                    ["netbox_nso_plugin_nsostaticroutestate"],
                )
                return {r[0] for r in cur.fetchall()}

        assert _sequence_exists()
        assert "intent_generation" in _columns()

        try:
            executor = MigrationExecutor(connection)
            executor.migrate([(APP, BEFORE)])
            assert "intent_generation" not in _columns()
            assert not _sequence_exists()
        finally:
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            # Forward to the app's LEAVES, not to a fixed name: re-applying only as far as
            # AFTER leaves every later migration unapplied on this worker's database, and
            # every test that runs after it on that worker fails on a missing column. EVERY
            # leaf, because a branched graph would leave the unnamed branch just as unapplied.
            executor.migrate(executor.loader.graph.leaf_nodes(APP))

        assert "intent_generation" in _columns()
        assert _sequence_exists()


class TestAdoptIncarnationAllowList(TestCase):
    """P0b.2 — the adoption's ``update_fields`` allow-list is why new columns survive."""

    def test_adoption_does_not_clobber_the_cursor_or_the_push_records(self):
        from netbox_nso_plugin.read_gate import _adopt_incarnation

        device = _make_device("adopt")
        mgmt = _make_mgmt(device, "adopt", 9103)
        mgmt.settle_cursor_seq = 41
        mgmt.settle_cursor_incarnation = "inc-old"
        mgmt.intent_push_errors = {"static_route": {"code": "duplicate_triple"}}
        mgmt.intent_push_attempts = {"static_route": 7}
        mgmt.adapter_incarnation = "inc-old"
        # Named explicitly: a plain full save cannot write the push record at all — a
        # pre_save guard restores it from the row, so a stale in-memory instance held by
        # one of the sweeps cannot rewind the never-cleared attempt mark.
        mgmt.save(
            update_fields=[
                "settle_cursor_seq",
                "settle_cursor_incarnation",
                "intent_push_errors",
                "intent_push_attempts",
                "adapter_incarnation",
            ]
        )

        _adopt_incarnation(mgmt, "inc-new", timezone.now())

        mgmt.refresh_from_db()
        assert mgmt.adapter_incarnation == "inc-new"
        assert mgmt.settle_cursor_seq == 41
        assert mgmt.settle_cursor_incarnation == "inc-old"
        assert mgmt.intent_push_errors == {"static_route": {"code": "duplicate_triple"}}
        assert mgmt.intent_push_attempts == {"static_route": 7}

    def test_the_allow_list_names_only_the_incarnation_markers(self):
        """Asserted explicitly: the new columns are absent from it *by design*, not by luck."""
        from netbox_nso_plugin.read_gate import _MARKER_FIELDS

        written = set(_MARKER_FIELDS) | {"adapter_source_epoch", "reset_pending_source_epoch"}
        assert written.isdisjoint(
            {"settle_cursor_seq", "settle_cursor_incarnation", "intent_push_errors", "intent_push_attempts"}
        )


class TestIntentGenerationAllocator(TestCase):
    """P0b.3 — a plugin-global allocator, unaffected by management-row recreation."""

    def test_allocations_are_strictly_increasing(self):
        from netbox_nso_plugin.intent_generation import allocate_intent_generation

        values = [allocate_intent_generation() for _ in range(5)]
        assert values == sorted(values)
        assert len(set(values)) == 5
        assert all(v > 0 for v in values)

    def test_allocation_survives_a_management_delete_and_recreate(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.intent_generation import allocate_intent_generation
        from netbox_nso_plugin.models import NSODeviceManagement, NSOStaticRouteState
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        device = _make_device("recr")
        mgmt = _make_mgmt(device, "recr", 9104)
        sr = StaticRoute.objects.create(prefix="10.10.0.0/16", next_hop="10.10.0.1", metric=1)
        first = allocate_intent_generation()
        NSOStaticRouteState.objects.create(management=mgmt, static_route=sr, status="accepted", intent_generation=first)

        delete_plan = RendererMutationPlan.build(deletes=(planned_delete(mgmt),))
        with renderer_writes(delete_plan) as writer:
            writer.delete(mgmt)
        assert not NSOStaticRouteState.objects.filter(static_route=sr).exists()

        mgmt2 = NSODeviceManagement(
            device=device,
            nso_instance=mgmt.nso_instance,
            nso_device_name=mgmt.nso_device_name,
            adapter_device_id=mgmt.adapter_device_id,
        )
        create_plan = RendererMutationPlan.build(
            saves=(planned_save(mgmt2, force_insert=True, natural_key=("device",)),)
        )
        with renderer_writes(create_plan) as writer:
            writer.save(mgmt2, force_insert=True)
        second = allocate_intent_generation()
        NSOStaticRouteState.objects.create(
            management=mgmt2, static_route=sr, status="accepted", intent_generation=second
        )

        assert second > first

    def test_delete_and_recreate_use_separate_frozen_writer_plans(self):
        """A management cascade cannot leak its writer permit into the new incarnation."""
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.intent_generation import allocate_intent_generation
        from netbox_nso_plugin.models import NSODeviceManagement, NSOStaticRouteState
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            active_renderer_writer,
            planned_delete,
            planned_save,
            renderer_writes,
        )

        device = _make_device("writer-recr")
        management = _make_mgmt(device, "writer-recr", 9105)
        route = StaticRoute.objects.create(prefix="198.18.40.0/24", next_hop="198.18.40.1", metric=1)
        first = allocate_intent_generation()
        state = NSOStaticRouteState.objects.create(
            management=management,
            static_route=route,
            status="accepted",
            intent_generation=first,
        )

        delete_plan = RendererMutationPlan.build(deletes=(planned_delete(management),))
        with renderer_writes(delete_plan) as writer:
            writer.delete(management)

        assert active_renderer_writer() is None
        assert not NSOStaticRouteState.objects.filter(pk=state.pk).exists()

        replacement = NSODeviceManagement(
            device=device,
            nso_instance=management.nso_instance,
            nso_device_name=management.nso_device_name,
            adapter_device_id=management.adapter_device_id,
        )
        create_plan = RendererMutationPlan.build(
            saves=(planned_save(replacement, force_insert=True, natural_key=("device",)),)
        )
        with renderer_writes(create_plan) as writer:
            writer.save(replacement, force_insert=True)

        second = allocate_intent_generation()
        replacement_state = NSOStaticRouteState(
            management=replacement,
            static_route=route,
            status="accepted",
            intent_generation=second,
        )
        state_plan = RendererMutationPlan.build(
            saves=(
                planned_save(
                    replacement_state,
                    force_insert=True,
                    natural_key=("management", "static_route"),
                ),
            )
        )
        with renderer_writes(state_plan) as writer:
            writer.save(replacement_state, force_insert=True)

        assert active_renderer_writer() is None
        assert second > first
