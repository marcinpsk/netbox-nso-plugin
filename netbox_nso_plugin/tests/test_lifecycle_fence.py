# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Owner successors and telemetry use the same committed PostgreSQL fence."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

from dcim.models import Device, Interface
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from ipam.models import ASN, RIR, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPRouter, BGPScope

from netbox_nso_plugin.models import NSOBGPPeerState, NSOBGPRouterState, NSOInterfaceState
from netbox_nso_plugin.template_content import _upsert_interface_states

from ._outbox_case import content_update, make_managed, mirror_update, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestOwnerLifecycleFence(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("owner-fence", 1767)
        self.management.manage_description = True
        self.management.manage_bgp = True
        self.management.manage_static = True
        with without_commit_drain():
            self.management.save(update_fields={"manage_description", "manage_bgp", "manage_static"})

    def test_owner_and_source_lock_queries_are_constant_with_footprint_size(self):
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.intent_state import MutationFootprint, SourceRow, mirror_transaction

        from ._outbox_case import own_vlan

        owners = []
        counts = []
        for size in (1, 12):
            while len(owners) < size:
                owners.append(own_vlan(self.management, 100 + len(owners), f"lock-{len(owners)}"))
            footprint = MutationFootprint(
                device_ids=(self.device.pk,),
                source_rows=tuple(SourceRow("ipam.vlan", row.vlan_id) for row in owners),
                overlay_rows=tuple(SourceRow(row._meta.label_lower, row.pk) for row in owners),
            )
            with CaptureQueriesContext(connection) as queries, mirror_transaction(footprint):
                pass
            tables = ("netbox_nso_plugin_nsooverlaysettlementfence", owners[0]._meta.db_table, "ipam_vlan")
            counts.append(
                tuple(
                    sum("FOR UPDATE" in query["sql"] and f'FROM "{table}"' in query["sql"] for query in queries)
                    for table in tables
                )
            )
        self.assertEqual(counts, [(1, 1, 1), (1, 1, 1)])

    def test_owner_rows_reads_each_overlay_model_once_for_any_device_count(self):
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.intent_state import MutationFootprint, SourceRow
        from netbox_nso_plugin.lifecycle_fence import owner_rows

        from ._outbox_case import own_vlan

        managements = [self.management]
        owners = [own_vlan(self.management, 100, "rows-0")]
        counts = []
        for size in (1, 4):
            while len(managements) < size:
                _device, management = make_managed(
                    f"owner-rows-{len(managements)}", 1767 + len(managements), index=len(managements) + 1
                )
                managements.append(management)
                owners.append(own_vlan(management, 100 + len(managements), f"rows-{len(managements)}"))
            footprint = MutationFootprint.for_keys((management.device_id, "vlan") for management in managements)
            with CaptureQueriesContext(connection) as queries:
                rows = owner_rows(footprint)
            self.assertEqual(set(rows), {SourceRow(row._meta.label_lower, row.pk) for row in owners})
            counts.append(sum(f'FROM "{owners[0]._meta.db_table}"' in query["sql"] for query in queries))
        self.assertEqual(counts, [1, 1])

    def bgp_owners(self):
        asn = ASN.objects.create(asn=64512, rir=RIR.objects.create(name="Private", slug="private"))
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=asn,
            name="placeholder-router",
            router_id="198.18.0.1",
        )
        scope = BGPScope.objects.create(router=router)
        BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        parent = acquire_overlay(
            NSOBGPRouterState, management=self.management, bgp_router=router, asn_str="64512", status="accepted"
        )
        peers = []
        for address in ("198.18.0.2", "198.18.0.3"):
            native = BGPPeer.objects.create(
                scope=scope, peer=IPAddress.objects.create(address=address + "/32"), name=None
            )
            peers.append(
                acquire_overlay(
                    NSOBGPPeerState,
                    management=self.management,
                    bgp_peer=native,
                    asn_str="64512",
                    vrf_name="",
                    peer_address_str=address,
                    status="accepted",
                )
            )
        attempt_id = uuid4()
        for row in (parent, *peers):
            mirror_update(row, status="deploying", apply_attempt_id=attempt_id)
        return parent, peers

    def test_peer_scalar_successor_does_not_repend_router_and_sibling(self):
        parent, (first, second) = self.bgp_owners()
        attempt = parent.apply_attempt_id
        content_update(first.bgp_peer, enabled=False)
        for unchanged in (parent, second):
            unchanged.refresh_from_db()
            self.assertEqual(unchanged.status, "deploying")
            self.assertEqual(unchanged.apply_attempt_id, attempt)
        first.refresh_from_db()
        self.assertEqual(first.status, "accepted")
        self.assertIsNone(first.apply_attempt_id)

    def test_cached_interface_read_cannot_clear_refusal(self):
        interface = Interface.objects.create(
            device=self.device, name="Ethernet1", type="1000base-t", description="placeholder desired"
        )
        state = acquire_overlay(NSOInterfaceState, interface=interface, attribute="description", status="accepted")
        mirror_update(state, status="apply_failed", last_apply_error={"code": "placeholder_refusal"})
        _upsert_interface_states(
            self.device,
            [
                {
                    "name": interface.name,
                    "attrs": {
                        "description": {
                            "status": "in_sync",
                            "nso_value": "placeholder observed",
                            "last_apply_error": None,
                        }
                    },
                }
            ],
        )
        state.refresh_from_db()
        self.assertEqual(state.status, "apply_failed")
        self.assertEqual(state.last_apply_error, {"code": "placeholder_refusal"})
        self.assertEqual(state.nso_value, "placeholder observed")

    def _interface_snapshot(self):
        interface = Interface.objects.create(
            device=self.device, name="Ethernet1", type="1000base-t", description="placeholder desired"
        )
        state = acquire_overlay(NSOInterfaceState, interface=interface, attribute="description", status="accepted")
        return self._snapshot_state(state)

    def _snapshot_state(self, state):
        from netbox_nso_plugin.durable_capture import _watermark, mapping_epoch
        from netbox_nso_plugin.lifecycle_fence import fence_for
        from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyOverlaySnapshot
        from netbox_nso_plugin.refusal_registry import OVERLAYS
        from netbox_nso_plugin.selection_manifest import current_content_digest

        attempt = NSOApplyAttempt.objects.create(management=self.management)
        mirror_update(state, status="deploying", apply_attempt_id=attempt.pk)
        at = timezone.now() - timedelta(days=1)
        with transaction.atomic():
            fence = fence_for(state)
            snapshot = NSOApplyOverlaySnapshot.objects.create(
                attempt=attempt,
                mapping_epoch=mapping_epoch(self.management),
                model_label=state._meta.label_lower,
                overlay_pk=state.pk,
                prior_status="in_sync",
                prior_last_apply_at=at,
                prior_last_apply_error=(
                    {"code": "placeholder prior cause"}
                    if OVERLAYS[state._meta.label_lower].error_type == "JSONField"
                    else "placeholder prior cause"
                ),
                expected_status=state.status,
                expected_apply_attempt=attempt,
                expected_last_apply_at=state.last_apply_at,
                expected_last_apply_error=state.last_apply_error,
                accepted_at=state.accepted_at,
                content_digest=current_content_digest(state),
                expected_fence_revision=fence.bookkeeping_revision,
                expected_owner_revision=fence.owner_revision,
                observation_watermark=_watermark(self.management, OVERLAYS[state._meta.label_lower]),
            )
        return state, snapshot

    def _settle_snapshot(self, state_pk, snapshot_pk):
        from netbox_nso_plugin.durable_capture import _matches, _write_bookkeeping
        from netbox_nso_plugin.intent_state import footprint_for_instance, mirror_transaction
        from netbox_nso_plugin.lifecycle_fence import advance_fence
        from netbox_nso_plugin.models import NSOApplyOverlaySnapshot

        snapshot = NSOApplyOverlaySnapshot.objects.get(pk=snapshot_pk)
        row = apps.get_model(snapshot.model_label).objects.get(pk=state_pk)
        with without_commit_drain(), mirror_transaction(footprint_for_instance(row)):
            row.refresh_from_db()
            snapshot = NSOApplyOverlaySnapshot.objects.get(pk=snapshot_pk)
            if _matches(snapshot, row, self.management, {}, "expected", check_revisions=False):
                _write_bookkeeping(row, snapshot, "prior")
                row.refresh_from_db()
                advance_fence(row, current_attempt=snapshot.attempt)
                snapshot.settlement_disposition = "not_applied"
            else:
                snapshot.settlement_disposition = "superseded"
            snapshot.save(update_fields={"settlement_disposition"})

    def _publish_interface(self, state_pk, plan=None):
        import contextlib

        from netbox_nso_plugin.intent_state import footprint_for_instance, mirror_transaction
        from netbox_nso_plugin.lifecycle_fence import publish_observation, read_refresh
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes

        row = NSOInterfaceState.objects.select_related("interface__device").get(pk=state_pk)
        footprint = footprint_for_instance(row)
        with read_refresh(), without_commit_drain(), mirror_transaction(footprint):
            with renderer_mirror_writes(plan) if plan is not None else contextlib.nullcontext():
                _upsert_interface_states(row.interface.device, self._interface_payload(row.interface.name))
            publish_observation(
                footprint,
                "interface_attributes",
                {
                    "incarnation": "placeholder-store",
                    "source_epoch": 1,
                    "payload_revision": 2,
                    "digest": "a" * 64,
                },
            )

    def _interface_payload(self, name):
        return [
            {
                "name": name,
                "attrs": {
                    "description": {
                        "status": "in_sync",
                        "nso_value": "placeholder new observation",
                        "last_apply_error": None,
                    }
                },
            }
        ]

    def test_newer_read_preserves_bookkeeping_against_old_settlement(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        state, snapshot = self._interface_snapshot()
        outbox_count = NSOIntentOutboxEntry.objects.count()
        self._committed_schedules(
            lambda: self._publish_interface(state.pk),
            lambda: self._settle_snapshot(state.pk, snapshot.pk),
        )
        snapshot.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(snapshot.settlement_disposition, "superseded")
        self.assertEqual(state.status, "deploying")
        self.assertEqual(state.nso_value, "placeholder new observation")
        self.assertEqual(NSOIntentOutboxEntry.objects.count(), outbox_count)

    def test_old_planned_read_preserves_fresh_restoration(self):
        from netbox_nso_plugin.lifecycle_fence import read_refresh
        from netbox_nso_plugin.models import NSOIntentOutboxEntry
        from netbox_nso_plugin.template_content import interface_reconcile_plan

        state, snapshot = self._interface_snapshot()
        outbox_count = NSOIntentOutboxEntry.objects.count()
        with read_refresh():
            plan = interface_reconcile_plan(self.device, self._interface_payload(state.interface.name))
            refresh_context = copy_context()
        self._committed_schedules(
            lambda: self._settle_snapshot(state.pk, snapshot.pk),
            lambda: refresh_context.run(self._publish_interface, state.pk, plan),
        )
        snapshot.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(snapshot.settlement_disposition, "not_applied")
        self.assertEqual(state.status, snapshot.prior_status)
        self.assertEqual(state.last_apply_at, snapshot.prior_last_apply_at)
        self.assertEqual(state.last_apply_error, snapshot.prior_last_apply_error)
        self.assertEqual(state.nso_value, "placeholder new observation")
        self.assertEqual(NSOIntentOutboxEntry.objects.count(), outbox_count)

    def test_old_planned_read_preserves_fresh_refusal(self):
        from netbox_nso_plugin.intent_state import footprint_for_instance, mirror_transaction
        from netbox_nso_plugin.lifecycle_fence import advance_fence, read_refresh
        from netbox_nso_plugin.template_content import interface_reconcile_plan

        state, snapshot = self._interface_snapshot()
        refusal = {"code": "placeholder correlated refusal"}
        with read_refresh():
            plan = interface_reconcile_plan(self.device, self._interface_payload(state.interface.name))
            refresh_context = copy_context()

        def refuse():
            row = NSOInterfaceState.objects.get(pk=state.pk)
            with without_commit_drain(), mirror_transaction(footprint_for_instance(row)):
                mirror_update(row, status="apply_failed", last_apply_error=refusal)
                advance_fence(row, current_attempt=snapshot.attempt)

        self._committed_schedules(refuse, lambda: refresh_context.run(self._publish_interface, state.pk, plan))
        state.refresh_from_db()
        self.assertEqual(state.status, "apply_failed")
        self.assertEqual(state.last_apply_error, refusal)
        self.assertEqual(state.nso_value, "placeholder new observation")

    def test_deleted_owner_rejects_old_read_as_a_stale_plan(self):
        from netbox_nso_plugin.lifecycle_fence import read_refresh
        from netbox_nso_plugin.ownership_grants import OwnershipGrant
        from netbox_nso_plugin.renderer_writer import (
            IntentPlanStaleError,
            RendererMutationPlan,
            planned_delete,
            renderer_mirror_writes,
            renderer_writes,
        )
        from netbox_nso_plugin.template_content import interface_reconcile_plan

        state, _snapshot = self._interface_snapshot()
        with read_refresh():
            plan = interface_reconcile_plan(self.device, self._interface_payload(state.interface.name))
            removal = RendererMutationPlan.build(
                grant=OwnershipGrant("operator_edit"), deletes=(planned_delete(state),)
            )
            with without_commit_drain(), renderer_writes(removal) as writer:
                writer.delete(state)
            with self.assertRaises(IntentPlanStaleError), renderer_mirror_writes(plan):
                pass

    def test_owner_edit_revert_aba_blocks_stale_settlement(self):
        from netbox_nso_plugin.lifecycle_fence import fence_for

        parent, (first, _second) = self.bgp_owners()
        with transaction.atomic():
            before = fence_for(first).bookkeeping_revision
            parent_before = fence_for(parent).bookkeeping_revision
        enabled = first.bgp_peer.enabled
        content_update(first.bgp_peer, enabled=False)
        content_update(first.bgp_peer, enabled=enabled)
        with transaction.atomic():
            self.assertGreater(fence_for(first).bookkeeping_revision, before)
            self.assertEqual(fence_for(parent).bookkeeping_revision, parent_before)

    def test_same_content_native_rebinding_advances_only_the_owner_fence(self):
        from netbox_nso_plugin.lifecycle_fence import fence_for
        from netbox_nso_plugin.selection_manifest import current_content_digest

        parent, (first, sibling) = self.bgp_owners()
        digest = current_content_digest(first)
        with transaction.atomic():
            before = fence_for(first).bookkeeping_revision
            parent_before = fence_for(parent).bookkeeping_revision
            sibling_before = fence_for(sibling).bookkeeping_revision
        with without_commit_drain():
            replacement = BGPPeer.objects.create(
                scope=first.bgp_peer.scope, peer=first.bgp_peer.peer, name="placeholder replacement"
            )
            content_update(first, bgp_peer=replacement)
        first.refresh_from_db()
        self.assertEqual(current_content_digest(first), digest)
        self.assertEqual(first.status, "accepted")
        with transaction.atomic():
            self.assertGreater(fence_for(first).bookkeeping_revision, before)
            self.assertEqual(fence_for(parent).bookkeeping_revision, parent_before)
            self.assertEqual(fence_for(sibling).bookkeeping_revision, sibling_before)

    def _successor_schedule(self, *, edit_first):
        parent, (first, second) = self.bgp_owners()
        first, snapshot = self._snapshot_state(first)
        sibling_attempt = second.apply_attempt_id

        def edit():
            content_update(BGPPeer.objects.get(pk=first.bgp_peer_id), enabled=False)

        def settle():
            self._settle_snapshot(first.pk, snapshot.pk)

        self._committed_schedules(edit if edit_first else settle, settle if edit_first else edit)
        first.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(first.status, "accepted")
        self.assertIsNone(first.apply_attempt_id)
        self.assertFalse(first.bgp_peer.enabled)
        self.assertEqual(snapshot.settlement_disposition, "superseded" if edit_first else "not_applied")
        for row in (parent, second):
            row.refresh_from_db()
            self.assertEqual(row.status, "deploying")
            self.assertEqual(row.apply_attempt_id, sibling_attempt)

    def test_successor_edit_first_preserves_successor_against_old_settlement(self):
        self._successor_schedule(edit_first=True)

    def test_old_settlement_first_preserves_later_successor_edit(self):
        self._successor_schedule(edit_first=False)

    def _static_echo_case(self):
        from netbox_nso_plugin.models import NSOStaticRouteState

        from ._outbox_case import own_route

        route = own_route(self.management, "198.18.16.0/28", "198.18.16.1")
        return NSOStaticRouteState.objects.get(management=self.management, static_route=route)

    def _echo_static(self, state):
        from netbox_nso_plugin.signals import _record_static_route_expectations

        _record_static_route_expectations(
            self.device.pk,
            {state.static_route_id: state.intent_generation},
            [
                {
                    "route_id": state.static_route_id,
                    "generation": state.intent_generation,
                    "fingerprint": "a" * 64,
                }
            ],
        )

    def test_static_echo_batch_records_every_route_in_one_acquisition(self):
        from unittest.mock import patch

        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.models import NSOStaticRouteState
        from netbox_nso_plugin.signals import _record_static_route_expectations

        from ._outbox_case import own_route

        for index in range(3):
            own_route(self.management, f"198.18.17.{index * 16}/28", f"198.18.17.{index * 16 + 1}")
        states = list(NSOStaticRouteState.objects.filter(management=self.management))
        self.assertEqual(len(states), 3)
        echoes = [
            {"route_id": state.static_route_id, "generation": state.intent_generation, "fingerprint": f"{i}" * 64}
            for i, state in enumerate(states)
        ]
        with patch.object(intent_state, "_acquire", wraps=intent_state._acquire) as acquire:
            _record_static_route_expectations(
                self.device.pk, {state.static_route_id: state.intent_generation for state in states}, echoes
            )
        self.assertEqual(acquire.call_count, 1)
        for i, state in enumerate(states):
            state.refresh_from_db()
            self.assertEqual(state.expected_generation, state.intent_generation)
            self.assertEqual(state.expected_fingerprint, f"{i}" * 64)

    def test_static_echo_first_commits_before_successor_generation(self):
        from netbox_nso_plugin.apply_state import lock_device_intent_transaction

        state = self._static_echo_case()
        generation = state.intent_generation

        def successor():
            lock_device_intent_transaction(self.device.pk)
            content_update(type(state).objects.get(pk=state.pk), intent_generation=generation + 1)

        self._committed_schedules(
            lambda: self._echo_static(state),
            successor,
        )
        state.refresh_from_db()
        self.assertEqual(state.intent_generation, generation + 1)
        self.assertEqual(state.expected_generation, generation)
        self.assertEqual(state.expected_fingerprint, "a" * 64)

    def test_static_successor_first_does_not_deadlock_or_record_old_echo(self):
        from netbox_nso_plugin.apply_state import lock_device_intent_transaction

        state = self._static_echo_case()
        held, release, echo_started = Event(), Event(), Event()
        backends = []

        def write(successor):
            close_old_connections()
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        backends.append(cursor.fetchone()[0])
                    if successor:
                        lock_device_intent_transaction(self.device.pk)
                        held.set()
                        if not release.wait(30):
                            raise AssertionError("echo did not reach the device lock")
                        content_update(
                            type(state).objects.get(pk=state.pk), intent_generation=state.intent_generation + 1
                        )
                    else:
                        echo_started.set()
                        self._echo_static(state)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(write, True)
            self.assertTrue(held.wait(30))
            second = executor.submit(write, False)
            self.assertTrue(echo_started.wait(30))
            try:
                self._wait_for_blocking(backends, second)
            finally:
                release.set()
            first.result(timeout=60)
            second.result(timeout=60)
        self.assertEqual(len(set(backends)), 2)
        state.refresh_from_db()
        self.assertIsNone(state.expected_generation)
        self.assertEqual(state.expected_fingerprint, "")

    def test_router_membership_successor_advances_router_fence(self):
        from netbox_nso_plugin.lifecycle_fence import fence_for
        from netbox_nso_plugin.ownership_grants import OwnershipGrant
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        parent, (_first, second) = self.bgp_owners()
        with transaction.atomic():
            before = fence_for(parent).bookkeeping_revision
            sibling_before = fence_for(second).bookkeeping_revision
        scope = BGPScope.objects.get(router=parent.bgp_router)
        af = BGPAddressFamily(scope=scope, address_family="ipv6-unicast")
        plan = RendererMutationPlan.build(
            grant=OwnershipGrant("operator_edit"),
            saves=(planned_save(af, force_insert=True, natural_key=("scope", "address_family")),),
        )
        with without_commit_drain(), renderer_writes(plan) as writer:
            writer.save(af, force_insert=True)
        parent.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(parent.status, "accepted")
        self.assertEqual(second.status, "deploying")
        with transaction.atomic():
            self.assertGreater(fence_for(parent).bookkeeping_revision, before)
            self.assertEqual(fence_for(second).bookkeeping_revision, sibling_before)

    def _committed_schedules(self, first, second):
        """Hold the first real writer while the second starts on another backend."""
        held, release, started = Event(), Event(), Event()
        backends = []

        def writer(operation, leading):
            close_old_connections()
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        backends.append(cursor.fetchone()[0])
                    if not leading:
                        started.set()
                    operation()
                    if leading:
                        held.set()
                        if not release.wait(30):
                            raise AssertionError("second writer did not start")
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(writer, first, True)
            if not held.wait(30):
                a.result()
                self.fail("first writer did not acquire its fence")
            b = executor.submit(writer, second, False)
            self.assertTrue(started.wait(30))
            try:
                self._wait_for_blocking(backends, b)
            finally:
                release.set()
            a.result(timeout=60)
            b.result(timeout=60)
        self.assertEqual(len(set(backends)), 2)

    def _wait_for_blocking(self, backends, pending):
        deadline = monotonic() + 15
        while True:
            if pending.done():
                pending.result()
                self.fail("second writer completed without waiting for the first writer's PostgreSQL lock")
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [backends[0], backends[1]])
                blocked = cursor.fetchone()[0]
            if blocked:
                return
            if monotonic() >= deadline:
                self.fail("second writer never waited for the first writer's PostgreSQL lock")
            sleep(0.01)

    def test_certified_success_allows_drift_only_from_a_new_payload(self):
        from netbox_nso_plugin.intent_state import footprint_for_instance, mirror_transaction
        from netbox_nso_plugin.lifecycle_fence import advance_fence
        from netbox_nso_plugin.read_gate import gated_family_run
        from netbox_nso_plugin.template_content import interface_reconcile_plan

        from ._scope_observation_case import DOCUMENTS, scope_observation
        from .test_read_gate import _rs

        state, snapshot = self._interface_snapshot()

        def publish(attempt, revision, value):
            payload = self._interface_payload(state.interface.name)
            payload[0]["attrs"]["description"]["nso_value"] = value
            import copy

            document = copy.deepcopy(DOCUMENTS["interface_attributes"])
            document["interfaces"][0].update(name=state.interface.name, description=value)
            return gated_family_run(
                self.management,
                "interface_attributes",
                _rs(attempt_id=attempt, payload_revision=revision),
                lambda: _upsert_interface_states(self.device, payload),
                epoch=self.management.adapter_device_id,
                pre_body=lambda: interface_reconcile_plan(self.device, payload),
                observation=scope_observation("interface_attributes", document=document, revision=revision),
            )

        self.assertEqual(publish(1, 1, "placeholder observed").disposition, "ran")
        mirror_update(state, status="in_sync", last_apply_error=None)
        with mirror_transaction(footprint_for_instance(state)):
            advance_fence(state, current_attempt=snapshot.attempt)
        self.assertEqual(publish(2, 1, "placeholder observed").disposition, "ran")
        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(publish(3, 2, "placeholder changed observation").disposition, "ran")
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.nso_value, "placeholder changed observation")
