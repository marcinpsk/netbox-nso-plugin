# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The renderer-input registry and mutation-permit contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import sqlparse
from django.db import connection, transaction
from django.db.models import F
from django.test import TransactionTestCase

from netbox_nso_plugin import delivery, outbox
from netbox_nso_plugin.intent_state import (
    OVERLAY_MODEL_RANKS,
    SOURCE_MODEL_RANKS,
    IntentMutationProtocolError,
    MutationFootprint,
    SourceRow,
    _dml_guard,
    canonical_fragment,
    content_mutation,
    deletion_footprint_for_instance,
    intent_transaction,
    mirror_refresh,
    mirror_transaction,
    renderer_input_specs,
    renderer_query_trace,
)
from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentRevision, NSOVLANState
from netbox_nso_plugin.signals import suppress_intent_push

from ._outbox_case import make_managed, own_vlan, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestIntentMutationProtocol(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Exercise permits through real ORM writes and durable revision rows."""

    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("intent-permit", 1623)
        self.state = own_vlan(self.management, 1623, "intent-permit")

    def test_registered_bulk_dml_requires_a_content_permit(self):
        with self.assertRaises(IntentMutationProtocolError):
            NSOVLANState.objects.filter(pk=self.state.pk).update(vlan_id=self.state.vlan_id + 100000)

    def test_registered_raw_dml_with_unquoted_content_column_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{table}" SET vlan_id = %s WHERE id = %s',
                [self.state.vlan_id, self.state.pk],
            )

    def test_registered_upsert_content_update_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO "{table}" SELECT * FROM "{table}" WHERE id = %s '
                "ON CONFLICT (id) DO UPDATE SET device_name = EXCLUDED.device_name",
                [self.state.pk],
            )

    def test_registered_upsert_with_unknown_update_columns_fails_closed(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO "{table}" SELECT * FROM "{table}" WHERE id = %s '
                "ON CONFLICT (id) DO UPDATE SET "
                "(device_name, status) = (EXCLUDED.device_name, EXCLUDED.status)",
                [self.state.pk],
            )

    def test_registered_upsert_non_content_update_does_not_require_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO "{table}" SELECT * FROM "{table}" WHERE id = %s '
                "ON CONFLICT (id) DO UPDATE SET last_apply_error = %s, last_apply_at = NULL",
                [self.state.pk, "upserted"],
            )

        self.state.refresh_from_db()
        self.assertEqual(self.state.last_apply_error, "upserted")

    def test_registered_insert_on_conflict_do_nothing_does_not_require_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO "{table}" SELECT * FROM "{table}" WHERE id = %s ON CONFLICT (id) DO NOTHING',
                [self.state.pk],
            )

        self.assertEqual(NSOVLANState.objects.filter(pk=self.state.pk).count(), 1)

    def test_registered_raw_dml_with_unknown_columns_fails_closed(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{table}" SET (management_id, vlan_id) = (%s, %s) WHERE id = %s',
                [self.management.pk, self.state.vlan_id, self.state.pk],
            )

    def test_cte_led_raw_dml_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'WITH target AS (SELECT id FROM "{table}" WHERE id = %s) '
                f'UPDATE "{table}" AS state SET vlan_id = %s FROM target WHERE state.id = target.id',
                [self.state.pk, self.state.vlan_id],
            )

    def test_schema_qualified_raw_dml_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "public"."{table}" SET vlan_id = %s WHERE id = %s',
                [self.state.vlan_id, self.state.pk],
            )

    def test_unquoted_uppercase_raw_dml_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table.upper()

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET VLAN_ID = %s WHERE ID = %s",
                [self.state.vlan_id, self.state.pk],
            )

    def test_comment_prefixed_raw_dml_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'/* guard regression */ UPDATE "{table}" SET vlan_id = %s WHERE id = %s',
                [self.state.vlan_id, self.state.pk],
            )

    def test_merge_raw_dml_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'MERGE INTO "{table}" AS target '
                "USING (VALUES (%s, %s)) AS source(id, vlan_id) ON target.id = source.id "
                "WHEN MATCHED THEN UPDATE SET vlan_id = source.vlan_id",
                [self.state.pk, self.state.vlan_id],
            )

    def test_data_mutating_cte_with_outer_select_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'WITH changed AS (UPDATE "{table}" SET vlan_id = %s WHERE id = %s RETURNING id) '
                "SELECT id FROM changed",
                [self.state.vlan_id, self.state.pk],
            )

    def test_data_mutating_cte_after_read_only_cte_requires_a_content_permit(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), connection.cursor() as cursor:
            cursor.execute(
                f'WITH existing AS (SELECT id FROM "{table}" WHERE id = %s), '
                f'changed AS (UPDATE "{table}" SET vlan_id = %s WHERE id IN (SELECT id FROM existing) RETURNING id) '
                "SELECT id FROM changed",
                [self.state.pk, self.state.vlan_id],
            )

    def test_unparseable_sql_that_names_a_registered_table_fails_closed(self):
        table = NSOVLANState._meta.db_table

        with self.assertRaises(IntentMutationProtocolError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f'UPDATE ??? "{table}" SET vlan_id = %s', [self.state.vlan_id])

    def test_registered_bulk_dml_allows_a_non_content_counter_update(self):
        type(self.device).objects.filter(pk=self.device.pk).update(interface_count=F("interface_count") + 1)

        self.device.refresh_from_db()
        self.assertEqual(self.device.interface_count, 1)

    def test_registered_table_select_skips_sqlparse(self):
        table = NSOVLANState._meta.db_table
        with patch("netbox_nso_plugin.intent_state.sqlparse.parse") as parse, connection.cursor() as cursor:
            cursor.execute(f'SELECT id FROM "{table}" WHERE id = %s', [self.state.pk])
            selected = cursor.fetchone()
            for index in range(1000):
                statement = f'SELECT id FROM "{table}" WHERE id = %s /* intent guard select {index} */'
                _dml_guard(lambda *args: None, statement, (self.state.pk,), False, {})

        self.assertEqual(selected, (self.state.pk,))
        parse.assert_not_called()

    def test_unregistered_changelog_insert_skips_sqlparse(self):
        statement = (
            "INSERT INTO intent_guard_unregistered_objectchange "
            "(object_type_id, changed_object_id, action) VALUES (%s, %s, %s)"
        )
        real_parse = sqlparse.parse
        parse_calls = 0

        def counting_parse(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(*args, **kwargs)

        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TEMP TABLE intent_guard_unregistered_objectchange "
                "(object_type_id integer, changed_object_id bigint, action varchar(50))"
            )
            with patch("netbox_nso_plugin.intent_state.sqlparse.parse", counting_parse):
                cursor.execute(statement, [1, 1623, "update"])
                cursor.execute(
                    "SELECT object_type_id, changed_object_id, action FROM intent_guard_unregistered_objectchange"
                )
                inserted = cursor.fetchone()

        self.assertEqual(inserted, (1, 1623, "update"))
        self.assertEqual(parse_calls, 0)

    def test_unregistered_cte_led_update_skips_sqlparse(self):
        statement = (
            "WITH target AS (SELECT id FROM intent_guard_unregistered_cte WHERE id = %s) "
            "UPDATE intent_guard_unregistered_cte AS candidate SET value = %s "
            "FROM target WHERE candidate.id = target.id"
        )
        real_parse = sqlparse.parse
        parse_calls = 0

        def counting_parse(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(*args, **kwargs)

        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE intent_guard_unregistered_cte (id integer PRIMARY KEY, value text)")
            cursor.execute("INSERT INTO intent_guard_unregistered_cte (id, value) VALUES (%s, %s)", [1, "before"])
            with patch("netbox_nso_plugin.intent_state.sqlparse.parse", counting_parse):
                cursor.execute(statement, [1, "after"])
                cursor.execute("SELECT value FROM intent_guard_unregistered_cte WHERE id = %s", [1])
                updated = cursor.fetchone()

        self.assertEqual(updated, ("after",))
        self.assertEqual(parse_calls, 0)

    def test_repeated_insert_shape_is_parsed_at_most_once(self):
        statement = "INSERT INTO intent_guard_parse_cache (value) VALUES (%s)"
        real_parse = sqlparse.parse
        parse_calls = 0

        def counting_parse(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(*args, **kwargs)

        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE intent_guard_parse_cache (value integer)")
            with patch("netbox_nso_plugin.intent_state.sqlparse.parse", counting_parse):
                cursor.execute(statement, [1])
                cursor.execute(statement, [2])

        self.assertLessEqual(parse_calls, 1)

    def test_repeated_registered_dml_shape_caches_column_classification(self):
        table = NSOVLANState._meta.db_table
        statement = f'UPDATE "{table}" SET last_apply_error = %s WHERE id = %s /* intent guard column cache */'
        real_parse = sqlparse.parse
        parse_calls = 0

        def counting_parse(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(*args, **kwargs)

        with patch("netbox_nso_plugin.intent_state.sqlparse.parse", counting_parse), connection.cursor() as cursor:
            cursor.execute(statement, ["first", self.state.pk])
            cursor.execute(statement, ["second", self.state.pk])

        self.assertLessEqual(parse_calls, 2)

    def test_select_for_update_of_a_registered_table_is_not_dml(self):
        with transaction.atomic():
            locked = NSOVLANState.objects.select_for_update(of=("self",)).get(pk=self.state.pk)

        self.assertEqual(locked.pk, self.state.pk)

    def test_interface_create_updates_the_registered_device_counter(self):
        from dcim.models import Interface

        initial_count = self.device.interface_count

        Interface.objects.create(device=self.device, name="Ethernet1623", type="1000base-t")

        self.device.refresh_from_db()
        self.assertEqual(self.device.interface_count, initial_count + 1)

    def test_module_install_creates_registered_interface_without_a_content_permit(self):
        from dcim.models import InterfaceTemplate, Module, ModuleBay, ModuleType

        module_type = ModuleType.objects.create(
            manufacturer=self.device.device_type.manufacturer,
            model="Intent Guard Module",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="Ethernet1623/1",
            type="1000base-t",
        )
        module_bay = ModuleBay.objects.create(device=self.device, name="Module Bay 1623", position="1623")

        module = Module.objects.create(device=self.device, module_bay=module_bay, module_type=module_type)

        self.assertEqual(module.interfaces.get().name, "Ethernet1623/1")

    def test_unpermitted_bulk_creation_is_authorized_and_logged(self):
        from dcim.models import Interface

        with self.assertLogs("netbox_nso_plugin.intent_state", level="WARNING") as logs:
            Interface.objects.bulk_create([Interface(device=self.device, name="Ethernet1623/9", type="1000base-t")])

        self.assertEqual(Interface.objects.filter(name="Ethernet1623/9").count(), 1)
        self.assertTrue(any("dcim_interface" in line for line in logs.output))

    def test_registered_bulk_dml_allows_a_non_rendered_interface_update(self):
        from dcim.models import Interface

        interface = Interface.objects.create(device=self.device, name="Ethernet1623", type="1000base-t")
        Interface.objects.filter(pk=interface.pk).update(label="inventory-only")

        interface.refresh_from_db()
        self.assertEqual(interface.label, "inventory-only")

    def test_device_delete_footprint_includes_assigned_native_addresses(self):
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        device = Device.objects.create(
            name="intent-delete-peer",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        interface = Interface.objects.create(device=device, name="Ethernet1624", type="1000base-t")
        with transaction.atomic():
            address = IPAddress.objects.create(address="198.18.16.0/31", assigned_object=interface)

        footprint = deletion_footprint_for_instance(device)

        self.assertIn(SourceRow(address._meta.label_lower, address.pk), footprint.source_rows)
        from netbox_nso_plugin.intent_state import _ACTIVE_PERMIT

        self.assertIsNone(_ACTIVE_PERMIT.get())

        device.delete()
        self.assertFalse(IPAddress.objects.filter(pk=address.pk).exists())

    def test_failed_static_route_m2m_behavior_closes_its_implicit_permit(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.intent_state import _ACTIVE_PERMIT

        route = StaticRoute.objects.create(prefix="198.18.16.0/28", next_hop="198.18.16.1", metric=1)
        with self.assertRaises(RuntimeError):
            with (
                transaction.atomic(),
                patch(
                    "netbox_nso_plugin.signals._schedule_intent_push",
                    side_effect=RuntimeError("behavior failed"),
                ),
            ):
                route.devices.add(self.device)

        self.assertIsNone(_ACTIVE_PERMIT.get())

    def test_failed_post_save_behavior_closes_its_implicit_permit(self):
        from netbox_nso_plugin.intent_state import _ACTIVE_PERMIT

        self.state.device_name = "intent-permit-router"
        with self.assertRaises(RuntimeError):
            with (
                transaction.atomic(),
                patch(
                    "netbox_nso_plugin.signals._schedule_intent_push",
                    side_effect=RuntimeError("behavior failed"),
                ),
            ):
                self.state.save(update_fields=["device_name"])

        self.assertIsNone(_ACTIVE_PERMIT.get())

    def test_content_permit_rejects_a_write_outside_its_footprint(self):
        other_device, other_management = make_managed("intent-other", 1624, index=2)
        other = own_vlan(other_management, 1624, "intent-other")
        footprint = MutationFootprint.for_keys(
            {(self.device.pk, "vlan")},
            source_rows=(SourceRow(self.state._meta.label_lower, self.state.pk),),
        )

        with self.assertRaises(IntentMutationProtocolError), transaction.atomic():
            with intent_transaction(footprint):
                other.device_name = "outside-footprint"
                other.save(update_fields=["device_name"])

        other.refresh_from_db()
        self.assertEqual(other.device_name, "")
        self.assertNotEqual(other_device.pk, self.device.pk)

    def test_content_mutation_bumps_before_write_and_repends_deploying_rows(self):
        attempt_id = uuid4()
        self.state.status = "deploying"
        self.state.apply_attempt_id = attempt_id
        with transaction.atomic(), suppress_intent_push(), mirror_refresh(self.state, {"status", "apply_attempt_id"}):
            self.state.save(update_fields=["status", "apply_attempt_id"])
        before = NSOIntentRevision.objects.get(device=self.device, scope="vlan").revision

        with (
            without_commit_drain(),
            content_mutation(
                {
                    (self.device.pk, "vlan"),
                    (self.device.pk, "svi"),
                    (self.device.pk, "switchport"),
                },
                source_rows=(SourceRow("ipam.vlan", self.state.vlan_id),),
                overlay_rows=(SourceRow(self.state._meta.label_lower, self.state.pk),),
            ),
        ):
            self.state.vlan.name = "intent-permit-renamed"
            self.state.vlan.save(update_fields=["name"])

        self.state.refresh_from_db()
        revision = NSOIntentRevision.objects.get(device=self.device, scope="vlan")
        self.assertEqual(revision.revision, before + 1)
        self.assertEqual(self.state.status, "accepted")
        self.assertIsNone(self.state.apply_attempt_id)

    def test_detected_reconcile_can_delete_a_captured_deploying_row(self):
        attempt_id = uuid4()
        self.state.status = "deploying"
        self.state.apply_attempt_id = attempt_id
        with transaction.atomic(), suppress_intent_push(), mirror_refresh(self.state, {"status", "apply_attempt_id"}):
            self.state.save(update_fields=["status", "apply_attempt_id"])
        footprint = MutationFootprint.for_keys(
            {(self.device.pk, "vlan")},
            overlay_rows=(SourceRow(self.state._meta.label_lower, self.state.pk),),
        )

        with without_commit_drain(), mirror_transaction(footprint, detect_content_changes=True):
            self.state.delete()

        self.assertFalse(type(self.state).objects.filter(pk=self.state.pk).exists())

    def test_detected_reconcile_locks_deploying_rows_before_capture(self):
        """Apply settlement waits until a detected reconcile finishes its re-pend decision."""
        import threading
        import time

        from django.db import connections

        from netbox_nso_plugin.intent_state import _upgrade_detected_reconcile

        self.state.status = "deploying"
        self.state.apply_attempt_id = uuid4()
        with (
            transaction.atomic(),
            suppress_intent_push(),
            mirror_refresh(
                self.state,
                {"status", "apply_attempt_id"},
            ) as locked,
        ):
            locked.status = self.state.status
            locked.apply_attempt_id = self.state.apply_attempt_id
            locked.save(update_fields=["status", "apply_attempt_id"])
        footprint = MutationFootprint.for_keys({(self.device.pk, "vlan")})
        settlement_pid = []
        settlement_started = threading.Event()
        failures = []

        def settle():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    settlement_pid.append(cursor.fetchone()[0])
                settlement_started.set()
                with (
                    transaction.atomic(),
                    suppress_intent_push(),
                    mirror_refresh(
                        self.state,
                        {"status", "apply_attempt_id"},
                    ) as current,
                ):
                    current.status = "in_sync"
                    current.apply_attempt_id = None
                    current.save(update_fields=["status", "apply_attempt_id"])
            except Exception as exc:  # noqa: BLE001 - the main test re-raises worker failures
                failures.append(exc)
            finally:
                connections.close_all()

        with without_commit_drain(), mirror_transaction(footprint, detect_content_changes=True) as permit:
            worker = threading.Thread(target=settle)
            worker.start()
            self.assertTrue(settlement_started.wait(10), "the settlement worker did not start")
            deadline = time.monotonic() + 3
            blocked = False
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                        [settlement_pid[0]],
                    )
                    if cursor.fetchone()[0]:
                        blocked = True
                        break
                time.sleep(0.01)
            self.assertTrue(blocked, "Apply settlement did not wait for the captured deploying-row lock")
            _upgrade_detected_reconcile(permit, footprint)

        worker.join(10)
        self.assertFalse(worker.is_alive())
        if failures:
            raise failures[0]
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, "in_sync")

    def test_savepoint_rollback_does_not_authorize_a_later_enqueue(self):
        key = (self.device.pk, "vlan")
        NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").delete()
        with without_commit_drain(), transaction.atomic():
            try:
                with transaction.atomic(), content_mutation({key}):
                    outbox.enqueue(*key)
                    raise RuntimeError("roll back the savepoint")
            except RuntimeError:
                pass

            with self.assertRaises(IntentMutationProtocolError):
                outbox.enqueue(*key)
            with content_mutation({key}):
                outbox.enqueue(*key)

        self.assertEqual(NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").count(), 1)

    def test_registry_declares_all_renderer_overlay_tables(self):
        declared = set(renderer_input_specs())
        required = {
            "netbox_nso_plugin.nsobgppeerstate",
            "netbox_nso_plugin.nsoredistributionstate",
            "netbox_nso_plugin.nsoroutepolicystate",
            "netbox_nso_plugin.nsostaticroutestate",
            "netbox_nso_plugin.nsosvistate",
            "netbox_nso_plugin.nsovlanstate",
        }
        self.assertTrue(required <= declared, required - declared)

    def test_registry_has_frozen_ranks_and_trace_fixtures(self):
        declared = set(renderer_input_specs())
        classified = set(SOURCE_MODEL_RANKS) | set(OVERLAY_MODEL_RANKS) | {"netbox_nso_plugin.nsodevicemanagement"}
        self.assertEqual(declared - classified, set())
        self.assertEqual(
            classified - declared,
            {
                "dcim.interface_tagged_vlans",
                "ipam.vlangroup",
                "netbox_nso_plugin.nsoinstance",
                "netbox_nso_plugin.nsoroutepolicyobjectclass",
                "netbox_routing.staticroute_devices",
            },
        )
        self.assertTrue(all(spec.required_trace_fixtures for spec in renderer_input_specs().values()))

    def test_reconcile_footprint_rejects_a_registered_model_without_a_lock_rank(self):
        from netbox_nso_plugin import intent_state

        unranked = SimpleNamespace(scopes=("vlan",), model=NSOIntentRevision)
        with (
            patch.dict(intent_state._REGISTRY, {NSOIntentRevision._meta.label_lower: unranked}),
            self.assertRaisesRegex(IntentMutationProtocolError, "nsointentrevision.*lock rank"),
        ):
            intent_state.reconcile_family_footprint(self.device.pk, ("vlan",))

    def test_registry_entries_carry_executable_resolvers_and_fragments(self):
        for label, spec in renderer_input_specs().items():
            with self.subTest(label=label):
                self.assertTrue(callable(spec.resolver))
                self.assertTrue(callable(spec.fragment))

    def test_interface_channel_cable_fields_are_registered_as_non_content(self):
        spec = renderer_input_specs()["dcim.interface"]
        cable_fields = {"cable", "cable_end", "cable_connector", "cable_positions"}

        self.assertTrue(cable_fields <= spec.lifecycle_fields)
        self.assertTrue(cable_fields.isdisjoint(spec.content_fields))

    def test_vlan_fragment_is_the_exact_renderer_item(self):
        rendered = delivery.render("vlan", self.device.pk, self.management.adapter_device_id)

        self.assertEqual(canonical_fragment(self.state), self._normal_fragment(rendered.payload[0]))

    @staticmethod
    def _normal_fragment(value):
        if isinstance(value, dict):
            return tuple(
                sorted((str(key), TestIntentMutationProtocol._normal_fragment(item)) for key, item in value.items())
            )
        if isinstance(value, (list, tuple)):
            return tuple(TestIntentMutationProtocol._normal_fragment(item) for item in value)
        return value

    def test_nested_direct_apply_fragments_are_exact_renderer_items(self):
        from netbox_nso_plugin.models import NSOLACPBundleState, NSOSwitchportState

        self._install_complete_renderer_trace_fixture()
        for scope, model in (
            ("lacp", NSOLACPBundleState),
            ("switchport", NSOSwitchportState),
        ):
            row = model.objects.get(management=self.management)
            rendered = delivery.render(scope, self.device.pk, self.management.adapter_device_id)
            with self.subTest(scope=scope):
                self.assertEqual(canonical_fragment(row), self._normal_fragment(rendered.payload[0]))

    def _install_complete_renderer_trace_fixture(self):
        """Create every conditional renderer branch named by the registry."""
        from dcim.models import Device, Interface, Platform
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import (
            ASPath,
            ASPathEntry,
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPPeerTemplate,
            BGPRouter,
            BGPScope,
            Community,
            CommunityList,
            CommunityListEntry,
            CustomPrefix,
            ISISInstance,
            ISISLevel,
            PrefixList,
            PrefixListEntry,
            RouteMap,
            RouteMapEntry,
            RouteMapEntrySetCommunity,
        )

        from netbox_nso_plugin.models import (
            NSOISISInstanceState,
            NSOLACPBundleState,
            NSOLACPMemberState,
            NSOPlatformNedMapping,
            NSORoutePolicyState,
            NSOSwitchportState,
        )

        with without_commit_drain(), transaction.atomic():
            platform = Platform.objects.create(name="Trace Platform", slug="trace-platform")
            self.device.platform = platform
            self.device.save(update_fields=["platform"])
            NSOPlatformNedMapping.objects.create(platform=platform, ned_id="test-ned")

            lag = Interface.objects.create(device=self.device, name="Bundle-Ether1", type="lag")
            member = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
            NSOLACPBundleState.objects.create(
                management=self.management,
                interface=lag,
                lag_id=1,
                status="accepted",
            )
            NSOLACPMemberState.objects.create(
                management=self.management,
                interface=member,
                lag_bundle=lag,
                mode="active",
                status="accepted",
            )
            switchport = NSOSwitchportState.objects.create(
                management=self.management,
                interface=member,
                mode="tagged",
                status="accepted",
            )
            switchport.tagged_vlans.add(self.state.vlan)

            isis_instance = ISISInstance.objects.create(
                device=self.device,
                process_tag="TRACE",
                net="49.0001.0000.0000.0001.00",
            )
            ISISLevel.objects.create(instance=isis_instance, level=2, wide_metrics_only=True)
            NSOISISInstanceState.objects.create(
                management=self.management,
                process_tag="TRACE",
                net=isis_instance.net,
                isis_instance=isis_instance,
                status="accepted",
            )

            rir = RIR.objects.create(name="Trace RIR", slug="trace-rir", is_private=True)
            local_as = ASN.objects.create(asn=64512, rir=rir)
            remote_as = ASN.objects.create(asn=64513, rir=rir)
            router = BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(Device),
                assigned_object_id=self.device.pk,
                asn=local_as,
                name="64512",
            )
            scope = BGPScope.objects.create(router=router)
            address_family = BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
            peer_group = BGPPeerTemplate.objects.create(name="TRACE-PEERS", remote_as=remote_as)
            peer = BGPPeer.objects.create(
                scope=scope,
                peer=IPAddress.objects.create(address="198.18.0.2/32"),
                source=IPAddress.objects.create(address="198.18.0.1/32"),
                remote_as=remote_as,
                local_as=local_as,
                peer_group=peer_group,
                enabled=True,
            )
            BGPPeerAddressFamily.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(BGPPeer),
                assigned_object_id=peer.pk,
                address_family=address_family,
                enabled=True,
            )

            prefix_list = PrefixList.objects.create(name="TRACE-PREFIXES")
            custom_prefix = CustomPrefix.objects.create(prefix="198.18.0.0/24")
            PrefixListEntry.objects.create(
                prefix_list=prefix_list,
                assigned_prefix_type=ContentType.objects.get_for_model(CustomPrefix),
                assigned_prefix_id=custom_prefix.pk,
                sequence=10,
                action="permit",
            )
            community_list = CommunityList.objects.create(name="TRACE-COMMUNITIES")
            community = Community.objects.create(community="64512:1623")
            CommunityListEntry.objects.create(
                community_list=community_list,
                action="permit",
                community=community,
            )
            as_path = ASPath.objects.create(name="TRACE-AS-PATH")
            ASPathEntry.objects.create(aspath=as_path, sequence=10, action="permit", pattern="^64512$")
            route_map = RouteMap.objects.create(name="TRACE-ROUTE-MAP")
            route_map_entry = RouteMapEntry.objects.create(route_map=route_map, sequence=10, action="permit")
            route_map_entry.match_prefix_list.add(prefix_list)
            route_map_entry.match_community_list.add(community_list)
            route_map_entry.match_aspath.add(as_path)
            set_community = RouteMapEntrySetCommunity.objects.create(
                route_map_entry=route_map_entry,
                operation="add",
                community_list=community_list,
            )
            set_community.communities.add(community)
            inline_community = RouteMapEntrySetCommunity.objects.create(
                route_map_entry=route_map_entry,
                operation="set",
            )
            inline_community.communities.add(community)
            for family, obj in (
                ("prefix_list", prefix_list),
                ("community_list", community_list),
                ("as_path", as_path),
                ("route_map", route_map),
            ):
                NSORoutePolicyState.objects.create(
                    management=self.management,
                    family=family,
                    object_name=obj.name,
                    content_type=ContentType.objects.get_for_model(type(obj)),
                    object_id=obj.pk,
                    status="accepted",
                )

    def test_render_trace_matches_the_declared_registry_in_both_directions(self):
        self._install_complete_renderer_trace_fixture()
        observed_by_fixture = {}
        for scope in delivery.delivery_keys():
            with renderer_query_trace() as observed:
                delivery.render(scope, self.device.pk, self.management.adapter_device_id)
            observed_by_fixture[scope] = observed

        declared = set(renderer_input_specs())
        observed = set().union(*observed_by_fixture.values())
        self.assertEqual(observed - declared, set(), f"undeclared renderer sources: {sorted(observed - declared)}")
        for label, spec in renderer_input_specs().items():
            with self.subTest(label=label):
                exercised = {
                    fixture
                    for fixture in spec.required_trace_fixtures
                    if label in observed_by_fixture.get(fixture, set())
                }
                self.assertTrue(exercised, f"{label} was not read by {spec.required_trace_fixtures!r}")

    def test_route_policy_registry_matches_every_table_read_by_the_renderer(self):
        declared = set(renderer_input_specs())
        required = {
            "netbox_routing.customprefix",
            "netbox_routing.community",
            "netbox_routing.routemapentrysetcommunity",
            "netbox_routing.routemapentrysetcommunity_communities",
        }

        self.assertTrue(required <= declared, required - declared)
        self.assertNotIn("netbox_routing.routemapentry_match_community", declared)

    def test_route_policy_registry_classifies_only_wire_contributing_fields_as_content(self):
        specs = renderer_input_specs()
        expected = {
            "netbox_routing.customprefix": {"prefix"},
            "netbox_routing.prefixlist": {"name"},
            "netbox_routing.prefixlistentry": {
                "prefix_list",
                "assigned_prefix_type",
                "assigned_prefix_id",
                "sequence",
                "action",
                "ge",
                "le",
            },
            "netbox_routing.community": {"community"},
            "netbox_routing.communitylist": {"name", "invert_match"},
            "netbox_routing.communitylistentry": {"community_list", "action", "community"},
            "netbox_routing.aspath": {"name"},
            "netbox_routing.aspathentry": {"aspath", "sequence", "action", "pattern"},
            "netbox_routing.routemap": {"name"},
            "netbox_routing.routemapentry": {
                "route_map",
                "action",
                "sequence",
                "flow_control",
                "match_afi",
                "call_policy",
                "match",
                "set",
                "vendor_ext",
                "apply_policy",
            },
            "netbox_routing.routemapentrysetcommunity": {
                "route_map_entry",
                "operation",
                "community_list",
            },
        }

        for label, fields in expected.items():
            with self.subTest(label=label):
                self.assertEqual(specs[label].content_fields, fields)
