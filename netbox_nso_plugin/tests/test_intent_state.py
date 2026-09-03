# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The renderer-input registry and mutation-permit contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from netbox_nso_plugin import delivery, outbox
from netbox_nso_plugin.intent_state import (
    OVERLAY_MODEL_RANKS,
    SOURCE_MODEL_RANKS,
    IntentMutationProtocolError,
    MutationFootprint,
    SourceRow,
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

from ._outbox_case import make_managed, own_vlan, wait_until_postgres_blocks, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestDeleteCollectorContract(SimpleTestCase):
    def test_install_rejects_collector_without_positional_source(self):
        from netbox_nso_plugin.intent_state import ensure_delete_signal_origin

        class IncompatibleCollector:
            def __init__(self, using, origin=None):
                self.origin = origin

            def collect(self, objs, nullable=False):
                pass

        with (
            patch("netbox.models.deletion.CustomCollector", IncompatibleCollector),
            self.assertRaisesRegex(RuntimeError, "CustomCollector.collect"),
        ):
            ensure_delete_signal_origin()

    def test_install_rejects_collector_without_origin_state(self):
        from netbox_nso_plugin.intent_state import ensure_delete_signal_origin

        class IncompatibleCollector:
            def __init__(self, using, origin=None):
                pass

            def collect(self, objs, source=None):
                pass

        with (
            patch("netbox.models.deletion.CustomCollector", IncompatibleCollector),
            self.assertRaisesRegex(RuntimeError, "CustomCollector.origin"),
        ):
            ensure_delete_signal_origin()


class TestIntentMutationProtocol(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Exercise permits through real ORM writes and durable revision rows."""

    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("intent-permit", 1623)
        self.state = own_vlan(self.management, 1623, "intent-permit")

    def test_management_delete_guard_rejects_an_unknown_origin(self):
        from netbox_nso_plugin.intent_state import _validate_explicit_delete

        with transaction.atomic(), self.assertRaisesRegex(IntentMutationProtocolError, "deletion origin"):
            _validate_explicit_delete(type(self.management), self.management, origin=object())

    def test_management_delete_guard_rejects_an_uncovered_active_permit(self):
        with self.assertRaisesRegex(IntentMutationProtocolError, "does not cover the management deletion"):
            with mirror_transaction(MutationFootprint()):
                type(self.management).objects.filter(pk=self.management.pk).delete()

        self.assertTrue(type(self.management).objects.filter(pk=self.management.pk).exists())

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

    def test_registered_bulk_dml_allows_a_non_rendered_interface_update(self):
        from dcim.models import Interface

        interface = Interface.objects.create(device=self.device, name="Ethernet1623", type="1000base-t")
        Interface.objects.filter(pk=interface.pk).update(label="inventory-only")

        interface.refresh_from_db()
        self.assertEqual(interface.label, "inventory-only")

    def test_audit_footprint_reads_no_registered_table_unfiltered(self):
        """The audit fronts every capture, so its footprint may never scan a whole table."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.intent_state import audit_scope_footprint

        tables = {spec.table for spec in renderer_input_specs().values()}

        with CaptureQueriesContext(connection) as captured:
            audit_scope_footprint(self.device.pk, delivery.delivery_keys())

        scanned = {
            table for query in captured.captured_queries for table in tables if f'FROM "{table}"' in query["sql"]
        }
        unfiltered = sorted(
            {
                table
                for query in captured.captured_queries
                for table in tables
                if f'FROM "{table}"' in query["sql"] and " WHERE " not in query["sql"]
            }
        )
        self.assertTrue(scanned)
        self.assertEqual(unfiltered, [])

    def test_audit_footprint_batches_overlay_loads_by_model(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.intent_state import audit_scope_footprint

        table = self.state._meta.db_table

        def overlay_queries():
            with CaptureQueriesContext(connection) as captured:
                audit_scope_footprint(self.device.pk, ("vlan",))
            return [
                query["sql"]
                for query in captured.captured_queries
                if f'FROM "{table}"' in query["sql"]
                and f'"{table}"."id" IN (' in query["sql"]
                and f'ORDER BY "{table}"."id" ASC' in query["sql"]
            ]

        own_vlan(self.management, 1624, "intent-permit-second")
        own_vlan(self.management, 1625, "intent-permit-third")

        self.assertEqual(len(overlay_queries()), 1)

    def test_audit_footprint_passes_each_dependency_candidate_once(self):
        from dataclasses import replace

        from dcim.models import Interface

        from netbox_nso_plugin.intent_state import audit_scope_footprint
        from netbox_nso_plugin.models import NSOSwitchportState

        interface = Interface.objects.create(device=self.device, name="Ethernet1623/2", type="1000base-t")
        state = NSOSwitchportState.objects.create(
            management=self.management,
            interface=interface,
            mode="tagged",
            status="accepted",
        )
        state.tagged_vlans.add(self.state.vlan)
        specs = renderer_input_specs()
        spec = specs[state._meta.label_lower]
        resolver = spec.dependency_resolver
        calls = []

        def record(before, after, candidate_spec):
            calls.append((before, after))
            return resolver(before, after, candidate_spec)

        with patch.dict(specs, {state._meta.label_lower: replace(spec, dependency_resolver=record)}):
            audit_scope_footprint(self.device.pk, ("switchport",))

        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0][0])
        self.assertEqual(calls[0][1].pk, state.pk)

    def test_detected_reconcile_does_not_add_retired_permit_state(self):
        from netbox_nso_plugin.intent_state import _Permit, _upgrade_detected_reconcile

        footprint = MutationFootprint.for_keys({(self.device.pk, "vlan")})
        permit = _Permit(
            footprint=footprint,
            dml_kind="reconcile",
            detect_reconcile_content=True,
        )

        with patch("netbox_nso_plugin.outbox.bump_intent_revision"):
            _upgrade_detected_reconcile(permit, footprint)

        self.assertNotIn("bump_revisions", vars(permit))

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

    def test_foreign_static_route_m2m_skips_behavior_and_closes_its_implicit_permit(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.intent_state import _ACTIVE_PERMIT

        route = StaticRoute.objects.create(prefix="198.18.16.0/28", next_hop="198.18.16.1", metric=1)
        with (
            transaction.atomic(),
            patch(
                "netbox_nso_plugin.signals._schedule_intent_push",
                side_effect=RuntimeError("behavior failed"),
            ) as schedule,
        ):
            route.devices.add(self.device)

        schedule.assert_not_called()
        self.assertIsNone(_ACTIVE_PERMIT.get())

    def test_foreign_post_save_skips_converted_behavior_and_closes_its_implicit_permit(self):
        from netbox_nso_plugin.intent_state import _ACTIVE_PERMIT

        self.state.device_name = "intent-permit-router"
        with (
            transaction.atomic(),
            patch(
                "netbox_nso_plugin.signals._schedule_intent_push",
                side_effect=RuntimeError("behavior failed"),
            ) as schedule,
        ):
            self.state.save(update_fields=["device_name"])

        schedule.assert_not_called()
        self.assertIsNone(_ACTIVE_PERMIT.get())

    def test_writerless_save_preserves_a_cached_foreign_key_created_later(self):
        from ipam.models import VLAN

        planned_vlan = VLAN(
            pk=self.state.vlan_id + 1_000_000,
            vid=1624,
            name="intent-planned-vlan",
        )
        self.state.vlan = planned_vlan

        with without_commit_drain(), transaction.atomic():
            self.state.save(update_fields=["vlan"])
            VLAN.objects.bulk_create([planned_vlan])

        self.state.refresh_from_db()
        self.assertEqual(self.state.vlan, planned_vlan)

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

    def test_content_mutation_rejects_a_bump_key_outside_its_locked_footprint(self):
        from netbox_nso_plugin.intent_state import _bump_and_lock_deploying

        footprint = MutationFootprint.for_keys({(self.device.pk, "vlan")})

        with (
            patch("netbox_nso_plugin.outbox.bump_intent_revision") as bump,
            self.assertRaisesRegex(IntentMutationProtocolError, "not locked"),
            transaction.atomic(),
        ):
            _bump_and_lock_deploying(footprint, ((self.device.pk, "bgp"),))

        bump.assert_not_called()

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

        worker = threading.Thread(target=settle)
        try:
            with without_commit_drain(), mirror_transaction(footprint, detect_content_changes=True) as permit:
                worker.start()
                self.assertTrue(settlement_started.wait(10), "the settlement worker did not start")
                wait_until_postgres_blocks(settlement_pid[0], "the settlement worker", timeout=3)
                _upgrade_detected_reconcile(permit, footprint)
        finally:
            if worker.ident is not None:
                worker.join(10)
        self.assertFalse(worker.is_alive())
        if failures:
            raise failures[0]
        self.state.refresh_from_db()
        self.assertEqual(self.state.status, "in_sync")

    def test_detected_reconcile_does_not_repend_a_row_settled_while_waiting_for_its_lock(self):
        """A reconcile must capture the deploying predicate from the locked row version."""
        import threading

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
        settlement_ready = threading.Event()
        release_settlement = threading.Event()
        reconcile_started = threading.Event()
        reconcile_pid = []
        failures = []

        def settle():
            try:
                with transaction.atomic():
                    current = type(self.state).objects.select_for_update(of=("self",)).get(pk=self.state.pk)
                    with (
                        suppress_intent_push(),
                        mirror_refresh(
                            current,
                            {"status", "apply_attempt_id"},
                        ) as locked,
                    ):
                        locked.status = "in_sync"
                        locked.apply_attempt_id = None
                        locked.save(update_fields=["status", "apply_attempt_id"])
                    settlement_ready.set()
                    if not release_settlement.wait(10):
                        raise AssertionError("the settlement worker was not released")
            except Exception as exc:  # noqa: BLE001 - the main test re-raises worker failures
                failures.append(exc)
            finally:
                connections.close_all()

        def reconcile():
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    reconcile_pid.append(cursor.fetchone()[0])
                reconcile_started.set()
                with (
                    without_commit_drain(),
                    mirror_transaction(
                        footprint,
                        detect_content_changes=True,
                    ) as permit,
                ):
                    _upgrade_detected_reconcile(permit, footprint)
            except Exception as exc:  # noqa: BLE001 - the main test re-raises worker failures
                failures.append(exc)
            finally:
                connections.close_all()

        settlement_worker = threading.Thread(target=settle)
        reconcile_worker = threading.Thread(target=reconcile)
        try:
            settlement_worker.start()
            self.assertTrue(settlement_ready.wait(10), "the settlement worker did not acquire the row lock")
            reconcile_worker.start()
            self.assertTrue(reconcile_started.wait(10), "the reconcile worker did not start")
            wait_until_postgres_blocks(reconcile_pid[0], "the detected reconcile", timeout=3)
        finally:
            release_settlement.set()
            for worker in (settlement_worker, reconcile_worker):
                if worker.ident is not None:
                    worker.join(10)

        self.assertFalse(settlement_worker.is_alive())
        self.assertFalse(reconcile_worker.is_alive())
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

    def test_acquiring_intent_locks_outside_a_transaction_is_refused(self):
        from netbox_nso_plugin.intent_state import _acquire

        with self.assertRaisesRegex(IntentMutationProtocolError, r"intent_transaction requires transaction\.atomic"):
            _acquire(MutationFootprint.for_keys({(self.device.pk, "vlan")}))

    def test_intent_transaction_opens_the_atomic_block_its_locks_need(self):
        from netbox_nso_plugin.intent_state import intent_transaction

        self.assertFalse(transaction.get_connection().in_atomic_block)
        with without_commit_drain(), intent_transaction(MutationFootprint.for_keys({(self.device.pk, "vlan")})):
            self.assertTrue(transaction.get_connection().in_atomic_block)

    def test_offline_mutation_outside_a_transaction_is_refused(self):
        from netbox_nso_plugin.intent_state import offline_mutation

        with self.assertRaisesRegex(IntentMutationProtocolError, r"offline mutation requires transaction\.atomic"):
            with offline_mutation():
                pass

    def test_a_deploying_row_that_vanished_unaccounted_fails_the_repend(self):
        from netbox_nso_plugin.intent_state import _repend_locked_rows, intent_transaction

        vanished = SourceRow(self.state._meta.label_lower, self.state.pk + 1_000_000)

        with without_commit_drain(), intent_transaction(MutationFootprint.for_keys({(self.device.pk, "vlan")})):
            with self.assertRaisesRegex(IntentMutationProtocolError, "vanished"):
                _repend_locked_rows((vanished,))

    def test_a_planned_delete_accounts_for_the_deploying_row_it_consumes(self):
        from uuid import uuid4

        from ._outbox_case import delete_vlan_state, mirror_update

        mirror_update(self.state, status="deploying", apply_attempt_id=uuid4())

        with without_commit_drain():
            delete_vlan_state(self.state)

        self.assertFalse(type(self.state).objects.filter(pk=self.state.pk).exists())

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
        self.assertEqual(len(SOURCE_MODEL_RANKS), len(set(SOURCE_MODEL_RANKS)))
        declared = set(renderer_input_specs())
        classified = set(SOURCE_MODEL_RANKS) | set(OVERLAY_MODEL_RANKS) | {"netbox_nso_plugin.nsodevicemanagement"}
        self.assertEqual(declared - classified, set())
        self.assertEqual(
            classified - declared,
            {
                "dcim.interface_tagged_vlans",
                "ipam.rir",
                "ipam.vlangroup",
                "netbox_nso_plugin.nsoinstance",
                "netbox_nso_plugin.nsoroutepolicyobjectclass",
                "netbox_routing.ospfinstance",
                "netbox_routing.bfdinterface",
                "netbox_routing.bfdprofile",
                "netbox_routing.isisflexalgo",
                "netbox_routing.isisinterface",
                "netbox_routing.isisinterfacelevel",
                "netbox_routing.isisprefixsid",
                "netbox_routing.isissegmentrouting",
                "netbox_routing.isissetting",
                "netbox_routing.isissrv6locator",
                "netbox_routing.ospfarea",
                "netbox_routing.ospfinstance",
                "netbox_routing.ospfinterface",
                "netbox_routing.staticroute_devices",
                "vpn.l2vpn",
                "vpn.l2vpntermination",
            },
        )
        self.assertTrue(
            all(spec.required_trace_fixtures or not spec.content_fields for spec in renderer_input_specs().values())
        )

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
            NSOBGPPeerState,
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
            peer.refresh_from_db()
            NSOBGPPeerState.objects.create(
                management=self.management,
                asn_str=str(local_as.asn),
                peer_address_str=str(peer.peer.address).split("/")[0],
                remote_as_str=str(remote_as.asn),
                enabled=True,
                bgp_peer=peer,
                status="accepted",
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
                if not spec.required_trace_fixtures:
                    self.assertFalse(spec.content_fields)
                    continue
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


class TestRepairBoundaryIsolation(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The repair boundary the audit consumes really runs at REPEATABLE READ."""

    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("intent-isolation", 1625)
        own_vlan(self.management, 1625, "intent-isolation")

    @staticmethod
    def _isolation_level():
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            return cursor.fetchone()[0]

    def test_a_repeatable_read_mirror_transaction_really_is_repeatable_read(self):
        from netbox_nso_plugin.intent_state import audit_scope_footprint

        footprint = audit_scope_footprint(self.device.pk, ("vlan",))

        with mirror_transaction(footprint, repeatable_read=True):
            inside = self._isolation_level()

        self.assertEqual(inside, "repeatable read")

    def test_an_ordinary_mirror_transaction_keeps_the_session_default(self):
        from netbox_nso_plugin.intent_state import audit_scope_footprint

        footprint = audit_scope_footprint(self.device.pk, ("vlan",))

        with mirror_transaction(footprint):
            inside = self._isolation_level()

        self.assertEqual(inside, "read committed")

    def test_a_nested_repeatable_read_request_keeps_the_outer_isolation(self):
        from django.db import transaction

        from netbox_nso_plugin.intent_state import audit_scope_footprint
        from netbox_nso_plugin.models import NSODeviceManagement

        footprint = audit_scope_footprint(self.device.pk, ("vlan",))
        with transaction.atomic():
            NSODeviceManagement.objects.filter(pk=self.management.pk).exists()
            with mirror_transaction(footprint, repeatable_read=True):
                inside = self._isolation_level()

        self.assertEqual(inside, "read committed")


class TestRepeatableReadDegradesUnderATestCaseAtomic(TestCase):
    """A caller-owned transaction keeps the isolation level it already established.

    PostgreSQL accepts SET TRANSACTION ISOLATION LEVEL only before a transaction's first
    statement. A Django TestCase has already run its fixtures inside that block. The sibling
    class pins both the standalone repeatable-read boundary and the production nested case.
    """

    def test_the_marker_this_skip_reads_is_present_only_under_a_testcase(self):
        from django.db import connections

        self.assertTrue(any(getattr(block, "_from_testcase", False) for block in connections["default"].atomic_blocks))

    def test_a_repeatable_read_request_stays_read_committed_inside_a_testcase(self):
        from django.db import connection

        with mirror_transaction(MutationFootprint(), repeatable_read=True):
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                inside = cursor.fetchone()[0]

        self.assertEqual(inside, "read committed")
