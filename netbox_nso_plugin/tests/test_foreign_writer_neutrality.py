# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Foreign writers use normal Django and PostgreSQL semantics with this plugin installed."""

from __future__ import annotations

import threading
from unittest.mock import patch

from django.apps import apps
from django.db import connection, connections
from django.db.migrations.state import ProjectState
from django.test import TransactionTestCase

from ._outbox_case import make_managed, own_vlan
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestForeignWriterNeutrality(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("foreign-neutral", 16272)
        self.state = own_vlan(self.management, 1670, "foreign-neutral")

    def assert_no_plugin_behavior(self, revision):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision

        current = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        self.assertEqual(current.revision, revision)
        self.assertFalse(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").exists())

    def baseline(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision

        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan").revision
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
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

    def test_raw_update_delete_cte_update_from_and_upserts_all_commit(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        revision = self.baseline()
        table = NSOVLANState._meta.db_table
        vlan = VLAN.objects.create(vid=1672, name="foreign-raw-target")
        with connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{table}" SET device_name = %s WHERE id = %s',
                ["foreign-raw-update", self.state.pk],
            )
            cursor.execute(
                f'WITH target AS (SELECT id FROM "{table}" WHERE id = %s) '
                f'UPDATE "{table}" AS state SET device_name = %s FROM target WHERE state.id = target.id',
                [self.state.pk, "foreign-update-from"],
            )
            columns = [field.column for field in NSOVLANState._meta.concrete_fields if not field.primary_key]
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            select_columns = [
                "%s" if field.attname == "vlan_id" else f'"{field.column}"'
                for field in NSOVLANState._meta.concrete_fields
                if not field.primary_key
            ]
            cursor.execute(
                f'INSERT INTO "{table}" ({quoted_columns}) '
                f'SELECT {", ".join(select_columns)} FROM "{table}" WHERE id = %s',
                [vlan.pk, self.state.pk],
            )
            inserted = NSOVLANState.objects.get(management=self.management, vlan=vlan)
            cursor.execute(
                f'INSERT INTO "{table}" SELECT * FROM "{table}" WHERE id = %s ON CONFLICT (id) DO NOTHING',
                [inserted.pk],
            )
            cursor.execute(
                f'INSERT INTO "{table}" SELECT * FROM "{table}" WHERE id = %s '
                "ON CONFLICT (id) DO UPDATE SET device_name = EXCLUDED.device_name",
                [inserted.pk],
            )
            cursor.execute(f'DELETE FROM "{table}" WHERE id = %s', [inserted.pk])

        self.state.refresh_from_db()
        self.assertEqual(self.state.device_name, "foreign-update-from")
        self.assertFalse(NSOVLANState.objects.filter(pk=inserted.pk).exists())
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
        def plugin_wrappers():
            return [
                wrapper
                for wrapper in connection.execute_wrappers
                if getattr(wrapper, "__module__", "") == "netbox_nso_plugin.intent_state"
            ]

        self.assertEqual(plugin_wrappers(), [])
        connection.close()
        connection.ensure_connection()
        self.assertEqual(plugin_wrappers(), [])
