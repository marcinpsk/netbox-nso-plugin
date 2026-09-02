# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Foreign writers use normal Django and PostgreSQL semantics with this plugin installed."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.db import connection, connections
from django.db.migrations.state import ProjectState
from django.test import TransactionTestCase

from ._outbox_case import make_managed, mirror_update, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestForeignWriterNeutrality(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("foreign-neutral", 16272)
        self.state = own_vlan(self.management, 1670, "foreign-neutral")
        # A lifecycle a plugin hook would have something to move: an overlay mid-apply.
        mirror_update(self.state, status="deploying", apply_attempt_id=uuid4())

    def assert_no_plugin_behavior(self, revision):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision, NSOVLANState

        current = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        self.assertEqual(current.revision, revision)
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").exists())
        # A foreign write moves no lifecycle. Where the write deleted the overlay outright
        # there is no row to compare, and the delete is what the case asserts instead.
        lifecycle = NSOVLANState.objects.filter(pk=self.state.pk).values("status", "apply_attempt_id").first()
        if lifecycle is not None:
            self.assertEqual((lifecycle["status"], lifecycle["apply_attempt_id"]), self.lifecycle)

    def baseline(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision, NSOVLANState

        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan").revision
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        row = NSOVLANState.objects.values("status", "apply_attempt_id").get(pk=self.state.pk)
        self.lifecycle = (row["status"], row["apply_attempt_id"])
        return revision

    def test_per_instance_content_save_commits_exactly_without_plugin_behavior(self):
        revision = self.baseline()
        self.state.device_name = "foreign-requested"

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule:
            self.state.save(update_fields=["device_name"])

        self.state.refresh_from_db()
        self.assertEqual(self.state.device_name, "foreign-requested")
        schedule.assert_not_called()
        self.assert_no_plugin_behavior(revision)

    def test_bulk_update_case_shape_commits_on_a_registered_core_table(self):
        from ipam.models import VLAN

        revision = self.baseline()
        vlan = self.state.vlan
        vlan.name = "foreign-bulk-update"

        VLAN.objects.bulk_update([vlan], ["name"])

        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "foreign-bulk-update")
        self.assert_no_plugin_behavior(revision)

    def test_queryset_update_commits_on_a_plugin_overlay(self):
        from netbox_nso_plugin.models import NSOVLANState

        revision = self.baseline()

        updated = NSOVLANState.objects.filter(pk=self.state.pk).update(device_name="foreign-set-update")

        self.state.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual(self.state.device_name, "foreign-set-update")
        self.assert_no_plugin_behavior(revision)

    def test_bulk_create_and_queryset_delete_use_normal_orm_semantics(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        revision = self.baseline()
        vlan = VLAN(vid=1671, name="foreign-created")
        VLAN.objects.bulk_create([vlan])
        state = NSOVLANState(management=self.management, vlan=vlan, device_name="foreign-created")
        NSOVLANState.objects.bulk_create([state])

        deleted, _details = NSOVLANState.objects.filter(pk=state.pk).delete()

        self.assertEqual(deleted, 1)
        self.assertFalse(NSOVLANState.objects.filter(pk=state.pk).exists())
        self.assert_no_plugin_behavior(revision)

    def test_framework_cascade_delete_does_not_run_plugin_bookkeeping(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        revision = self.baseline()
        vlan_id = self.state.vlan_id

        VLAN.objects.filter(pk=vlan_id).delete()

        self.assertFalse(NSOVLANState.objects.filter(pk=self.state.pk).exists())
        self.assert_no_plugin_behavior(revision)

    def test_foreign_m2m_change_commits_without_plugin_behavior(self):
        from netbox_routing.models import StaticRoute

        revision = self.baseline()
        route = StaticRoute.objects.create(prefix="198.18.176.0/24", next_hop="198.18.0.176")

        route.devices.add(self.device)

        self.assertEqual(list(route.devices.values_list("pk", flat=True)), [self.device.pk])
        self.assert_no_plugin_behavior(revision)

    def raw_columns(self, source=None, **overrides):
        """The overlay's non-key columns and one row expression for them.

        With *source* the expression reads that relation's columns, which is what an
        ``INSERT … SELECT`` needs; without it every column is a bound parameter, which is
        what a ``VALUES`` list needs. Either way *overrides* replace named fields, so an
        upsert can propose a row that genuinely differs from the one already stored — a
        proposal copied from the target resolves to a no-op whichever conflict action it
        carries, and cannot tell a correct ``DO UPDATE`` from a wrong one.
        """
        from netbox_nso_plugin.models import NSOVLANState

        row = NSOVLANState.objects.get(pk=self.state.pk)
        columns, terms, params = [], [], []
        for field in NSOVLANState._meta.concrete_fields:
            if field.primary_key:
                continue
            columns.append(f'"{field.column}"')
            if source is not None and field.attname not in overrides:
                terms.append(f'{source}."{field.column}"')
                continue
            value = overrides.get(field.attname, getattr(row, field.attname))
            if field.get_internal_type() == "JSONField":
                terms.append("%s::jsonb")
                value = json.dumps(value)
            else:
                terms.append("%s")
            params.append(value)
        return ", ".join(columns), ", ".join(terms), params

    def device_name_of(self, pk):
        from netbox_nso_plugin.models import NSOVLANState

        return NSOVLANState.objects.values_list("device_name", flat=True).get(pk=pk)

    def test_raw_update_delete_cte_update_from_and_upserts_all_commit(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        revision = self.baseline()
        table = NSOVLANState._meta.db_table
        vlan = VLAN.objects.create(vid=1672, name="foreign-raw-target")
        with connection.cursor() as cursor:
            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'UPDATE "{table}" SET device_name = %s WHERE id = %s',
                ["foreign-raw-update", self.state.pk],
            )
            self.assertEqual(self.device_name_of(self.state.pk), "foreign-raw-update")

            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'WITH target AS (SELECT id FROM "{table}" WHERE id = %s) '
                f'UPDATE "{table}" AS state SET device_name = %s FROM target WHERE state.id = target.id',
                [self.state.pk, "foreign-update-from"],
            )
            self.assertEqual(self.device_name_of(self.state.pk), "foreign-update-from")

            columns, terms, params = self.raw_columns(f'"{table}"', vlan_id=vlan.pk, device_name="insert-select")
            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'INSERT INTO "{table}" ({columns}) SELECT {terms} FROM "{table}" WHERE id = %s',
                [*params, self.state.pk],
            )
            inserted = NSOVLANState.objects.get(management=self.management, vlan=vlan)
            self.assertEqual(inserted.device_name, "insert-select")

            columns, terms, params = self.raw_columns(vlan_id=vlan.pk, device_name="conflict-do-nothing")
            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'INSERT INTO "{table}" ({columns}) VALUES ({terms}) ON CONFLICT (management_id, vlan_id) DO NOTHING',
                params,
            )
            self.assertEqual(self.device_name_of(inserted.pk), "insert-select")

            columns, terms, params = self.raw_columns(vlan_id=vlan.pk, device_name="conflict-do-update")
            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'INSERT INTO "{table}" ({columns}) VALUES ({terms}) '
                "ON CONFLICT (management_id, vlan_id) DO UPDATE SET device_name = EXCLUDED.device_name",
                params,
            )
            self.assertEqual(self.device_name_of(inserted.pk), "conflict-do-update")

            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'DELETE FROM "{table}" WHERE id = %s',
                [inserted.pk],
            )

        self.state.refresh_from_db()
        self.assertEqual(self.state.device_name, "foreign-update-from")
        self.assertFalse(NSOVLANState.objects.filter(pk=inserted.pk).exists())
        self.assert_no_plugin_behavior(revision)

    def test_values_inserts_at_both_arities_and_a_cte_led_insert_commit(self):
        """A row list and a CTE-led INSERT are their own DML shapes, not the SELECT one."""
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        revision = self.baseline()
        table = NSOVLANState._meta.db_table
        vlans = [VLAN.objects.create(vid=1680 + index, name=f"foreign-values-{index}") for index in range(4)]
        with connection.cursor() as cursor:
            columns, terms, params = self.raw_columns(vlan_id=vlans[0].pk, device_name="values-single")
            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'INSERT INTO "{table}" ({columns}) VALUES ({terms})',
                params,
            )

            columns, terms, first = self.raw_columns(vlan_id=vlans[1].pk, device_name="values-first")
            _columns, _terms, second = self.raw_columns(vlan_id=vlans[2].pk, device_name="values-second")
            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'INSERT INTO "{table}" ({columns}) VALUES ({terms}), ({terms})',
                [*first, *second],
            )

            columns, terms, params = self.raw_columns("source", vlan_id=vlans[3].pk, device_name="cte-insert")
            cursor.execute(  # noqa: S608 - the quoted table name comes from model metadata
                f'WITH source AS (SELECT * FROM "{table}" WHERE id = %s) '
                f'INSERT INTO "{table}" ({columns}) SELECT {terms} FROM source',
                [self.state.pk, *params],
            )

        landed = dict(NSOVLANState.objects.filter(vlan__in=vlans).values_list("vlan_id", "device_name"))
        self.assertEqual(
            landed,
            {
                vlans[0].pk: "values-single",
                vlans[1].pk: "values-first",
                vlans[2].pk: "values-second",
                vlans[3].pk: "cte-insert",
            },
        )
        self.assert_no_plugin_behavior(revision)

    def test_a_cascade_from_an_interface_takes_its_overlays_and_its_assigned_ip(self):
        """One Collector run over a registered core row, straight through the plugin's overlays.

        The interface overlays hang off ``dcim.Interface`` rather than off the management
        row, and the assigned address hangs off the interface through a generic relation, so
        this cascade class reaches three registered models the VLAN cases never touch.
        """
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState, NSOInterfaceState

        interface = Interface.objects.create(device=self.device, name="et-0/0/9", type="1000base-t")
        address = IPAddress.objects.create(address="198.18.177.1/31", assigned_object=interface)
        attribute = NSOInterfaceState.objects.create(interface=interface, attribute="description", status="in_sync")
        overlay_ip = NSOInterfaceIPState.objects.create(
            interface=interface, address="198.18.177.1/31", status="in_sync"
        )
        revision = self.baseline()

        deleted, details = Interface.objects.filter(pk=interface.pk).delete()

        self.assertEqual(details.get("netbox_nso_plugin.NSOInterfaceState"), 1)
        self.assertEqual(details.get("netbox_nso_plugin.NSOInterfaceIPState"), 1)
        self.assertFalse(NSOInterfaceState.objects.filter(pk=attribute.pk).exists())
        self.assertFalse(NSOInterfaceIPState.objects.filter(pk=overlay_ip.pk).exists())
        self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())
        self.assertGreaterEqual(deleted, 4)
        self.assert_no_plugin_behavior(revision)

    def test_separate_thread_has_no_plugin_writer_context(self):
        from netbox_nso_plugin.models import NSOVLANState

        revision = self.baseline()
        errors = []

        def write():
            try:
                NSOVLANState.objects.filter(pk=self.state.pk).update(device_name="foreign-thread")
            except Exception as exc:  # noqa: BLE001 (the assertion reports the foreign failure)
                errors.append(exc)
            finally:
                connections.close_all()

        worker = threading.Thread(target=write)
        worker.start()
        worker.join(10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.state.refresh_from_db()
        self.assertEqual(self.state.device_name, "foreign-thread")
        self.assert_no_plugin_behavior(revision)

    def test_migration_shaped_historical_model_write_is_not_intercepted(self):
        revision = self.baseline()
        historical_apps = ProjectState.from_apps(apps).apps
        HistoricalVLANState = historical_apps.get_model("netbox_nso_plugin", "NSOVLANState")

        updated = HistoricalVLANState.objects.filter(pk=self.state.pk).update(device_name="foreign-migration")

        self.state.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual(self.state.device_name, "foreign-migration")
        self.assert_no_plugin_behavior(revision)

    def test_existing_and_new_connections_have_no_production_execute_wrapper(self):
        """Any plugin module's wrapper counts: naming one leaves the rest of the package free."""

        def plugin_wrappers():
            return [
                wrapper
                for wrapper in connection.execute_wrappers
                if getattr(wrapper, "__module__", "").startswith("netbox_nso_plugin.")
                and not getattr(wrapper, "__module__", "").startswith("netbox_nso_plugin.tests")
            ]

        self.assertEqual(plugin_wrappers(), [])
        connection.close()
        connection.ensure_connection()
        self.assertEqual(plugin_wrappers(), [])
