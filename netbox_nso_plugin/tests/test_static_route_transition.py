# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1396 R3 P2 — the static-route content-edit transition.

Every native content edit of an owned route — identity or not — demotes each owning
overlay to ``accepted``, refreshes its ``nso_*`` mirror, allocates a fresh generation,
clears the error and advisory of the generation just superseded, and pushes. Pins P2.1
through P2.12 (P2.7's (b)/(c) provenance arms ship with Appendix O).
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_nso_plugin import adapter_client as _adapter_client

from ._static_route_case import PUT, _fixtures, _make_device, _make_mgmt, _own, _route
from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin, _CascadeFlushMixin

#: Captured at import, before any test can patch it — see ``_assert_put_patch_did_not_leak``.
_REAL_PUT = _adapter_client.put_static_route_intent


class TestStaticRouteContentTransition(IntentPushDeliveryMixin, TestCase):
    """P2.1–P2.7(a), P2.10, P2.11 — the transition itself."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("edit")
        cls.mgmt = _make_mgmt(cls.device, "edit", 8801)

    def test_an_identity_edit_demotes_bumps_and_pushes(self):
        """P2.1 — an in_sync row left in_sync is a green badge over content the device lacks."""
        with _fixtures():
            sr = _route("10.20.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="in_sync")
        before = state.intent_generation

        with patch(PUT) as put, self.captureOnCommitCallbacks(execute=True):
            sr.next_hop = "10.0.0.2"
            sr.save()

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.nso_next_hop, "10.0.0.2")
        self.assertGreater(state.intent_generation, before)
        self.assertIsNotNone(state.generation_started_at)
        put.assert_called_once()
        route = put.call_args.args[1][0]
        self.assertEqual(route["next_hop"], "10.0.0.2")
        self.assertEqual(route["generation"], state.intent_generation)

    def test_a_deploying_row_is_demoted_too(self):
        """P2.2 — an apply in flight would otherwise settle the NEW intent from the OLD result."""
        with _fixtures():
            sr = _route("10.21.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="deploying")
        accepted_at = state.accepted_at

        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            sr.next_hop = "10.0.0.2"
            sr.save()

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.accepted_at, accepted_at)

    def test_the_vrf_mirror_is_written_by_the_transition(self):
        """P2.3 — the residue key is the (vrf, prefix, next_hop) triple, so an empty nso_vrf
        makes a VRF route's own device row read as residue."""
        from ipam.models import VRF

        vrf = VRF.objects.create(name="TR-CUST-A")
        with _fixtures():
            sr = _route("10.22.0.0/16", "10.0.0.1", vrf=vrf, devices=[self.device])
            state = _own(sr, self.mgmt, status="in_sync", mirror_vrf="")

        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            sr.next_hop = "10.0.0.2"
            sr.save()

        state.refresh_from_db()
        self.assertEqual(state.nso_vrf, "TR-CUST-A")
        self.assertEqual(state.nso_prefix, "10.22.0.0/16")
        self.assertEqual(state.nso_next_hop, "10.0.0.2")

    def test_a_greenfield_accept_into_a_vrf_writes_the_vrf_mirror(self):
        """P2.3 — the same grain on the assignment path, which never went through reconcile."""
        from ipam.models import VRF

        from netbox_nso_plugin.models import NSOStaticRouteState

        vrf = VRF.objects.create(name="TR-CUST-B")
        with _fixtures():
            sr = _route("10.23.0.0/16", "10.0.0.3", vrf=vrf)

        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            sr.devices.add(self.device)

        state = NSOStaticRouteState.objects.get(management=self.mgmt, static_route=sr)
        self.assertEqual(state.nso_vrf, "TR-CUST-B")
        self.assertGreater(state.intent_generation, 0)
        self.assertIsNotNone(state.generation_started_at)

    def test_a_metric_only_edit_qualifies(self):
        """P2.4 — "identity only" is a false green: a metric edit is unapplied content too."""
        with _fixtures():
            sr = _route("10.24.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="in_sync")
        before = state.intent_generation

        with patch(PUT) as put, self.captureOnCommitCallbacks(execute=True):
            sr.metric = 7
            sr.save(update_fields=["metric"])

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertGreater(state.intent_generation, before)
        put.assert_called_once()
        self.assertEqual(put.call_args.args[1][0]["metric"], 7)

    def test_a_suppressed_save_and_a_no_delta_save_do_nothing(self):
        """P2.5 — reconcile writes are not intent, and neither is an edit the wire never carries."""
        from netbox_nso_plugin.signals import suppress_intent_push

        with _fixtures():
            sr = _route("10.25.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="in_sync")
        before = state.intent_generation

        with patch(PUT) as put, self.captureOnCommitCallbacks(execute=True):
            with suppress_intent_push():
                sr.next_hop = "10.0.0.9"
                sr.save()
        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(state.intent_generation, before)
        put.assert_not_called()

        with patch(PUT) as put, self.captureOnCommitCallbacks(execute=True):
            sr.name = "renamed"  # never reaches the wire
            sr.save()
        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(state.intent_generation, before)
        put.assert_not_called()

    def test_a_field_the_save_did_not_persist_is_not_a_content_change(self):
        """``update_fields`` means the database never saw the other attributes. Reading them
        off the instance invents a change, mirrors a value the row does not hold, and bumps a
        generation — while the push builder re-queries the row and sends the old content."""
        with _fixtures():
            sr = _route("10.26.5.0/24", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="in_sync")
        before = state.intent_generation

        with patch(PUT) as put, self.captureOnCommitCallbacks(execute=True):
            sr.next_hop = "10.0.0.99"  # set in memory, never persisted
            sr.name = "label"
            sr.save(update_fields=["name"])

        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(state.intent_generation, before)
        self.assertEqual(state.nso_next_hop, "10.0.0.1")
        put.assert_not_called()

    def test_a_created_route_carries_no_stash(self):
        """P2.6 — a create has no pre-save row, so nothing may be read as its baseline."""
        from netbox_routing.models import StaticRoute

        with _fixtures():
            edited = _route("10.26.0.0/16", "10.0.0.1", devices=[self.device])
            _own(edited, self.mgmt, status="in_sync")
            edited.next_hop = "10.0.0.2"
            edited.save()

        with patch(PUT) as put, self.captureOnCommitCallbacks(execute=True):
            fresh = StaticRoute.objects.create(prefix="10.27.0.0/16", next_hop="10.0.0.7", metric=1)

        self.assertIsNone(fresh._nso_static_route_content)
        put.assert_not_called()

    def test_a_b_a_in_one_transaction_transitions_twice(self):
        """P2.7(a) — only the final generation is ever pushed, so the intermediate one is
        invisible; dropping idempotence is what makes the design need no transaction identity."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        with _fixtures():
            sr = _route("10.28.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="in_sync")
        before = state.intent_generation

        with patch(PUT) as put, self.captureOnCommitCallbacks(execute=True):
            sr.next_hop = "10.0.0.2"
            sr.save()
            middle = NSOStaticRouteState.objects.get(pk=state.pk).intent_generation
            sr.next_hop = "10.0.0.1"
            sr.save()

        state.refresh_from_db()
        self.assertGreater(middle, before)
        self.assertGreater(state.intent_generation, middle)
        self.assertEqual(state.nso_next_hop, "10.0.0.1")
        self.assertEqual(state.status, "accepted")
        put.assert_called_once()  # the drain sends the final state once

    def test_an_edit_clears_the_superseded_error_and_advisory(self):
        """P2.11 — both describe a generation the operator has just replaced."""
        with _fixtures():
            sr = _route("10.29.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="apply_failed")
            state.last_apply_error = "config-write-failed: no route to host"
            state.last_result_advisory = "unproven: the reader could not be compared"
            state.save(update_fields=["last_apply_error", "last_result_advisory"])

        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            sr.next_hop = "10.0.0.2"
            sr.save()

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.last_apply_error, "")
        self.assertEqual(state.last_result_advisory, "")


class TestStaticRouteReAcceptBumps(IntentPushResetMixin, TestCase):
    """P2.10 — re-accept saves no native object, so it must bump explicitly."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("acc")
        cls.mgmt = _make_mgmt(cls.device, "acc", 8802)

    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_superuser(username="tr-acc-admin", password="x", email="a@example.com")
        self.client.force_login(user)

    def test_single_re_accept_bumps_and_preserves_accepted_at(self):
        with _fixtures():
            sr = _route("10.30.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="apply_failed")
            state.last_apply_error = "config-write-failed"
            state.save(update_fields=["last_apply_error"])
        before, accepted_at = state.intent_generation, state.accepted_at

        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:routing_accept_static_route", kwargs={"pk": state.pk})
            )

        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertGreater(state.intent_generation, before)
        self.assertEqual(state.accepted_at, accepted_at)
        self.assertEqual(state.last_apply_error, "")

    def test_bulk_accept_arms_every_row_it_takes_into_ownership(self):
        with _fixtures():
            sr = _route("10.31.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="in_sync")
            state.status = "changed"
            state.intent_generation = 0
            state.save(update_fields=["status", "intent_generation"])

        with patch(PUT), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    "plugins:netbox_nso_plugin:routing_bulk_accept_static_routes",
                    kwargs={"device_pk": self.device.pk},
                )
            )

        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertGreater(state.intent_generation, 0)
        self.assertIsNotNone(state.generation_started_at)


class TestStaticRouteBulkAcceptOutsideATransaction(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Bulk accept runs in a real request, which NetBox does not wrap in a transaction."""

    def setUp(self):
        super().setUp()
        self.device = _make_device("blk")
        self.mgmt = _make_mgmt(self.device, "blk", 8821)
        user = get_user_model().objects.create_superuser(username="tr-blk-admin", password="x", email="b@example.com")
        self.client.force_login(user)

    def _drifted(self, count):
        states = []
        with _fixtures(), transaction.atomic():
            for index in range(count):
                sr = _route(f"10.39.{index}.0/24", "10.0.0.1", devices=[self.device])
                state = _own(sr, self.mgmt, status="in_sync")
                state.status = "changed"
                state.save(update_fields=["status"])
                states.append(state)
        return states

    def _post(self):
        return self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:routing_bulk_accept_static_routes",
                kwargs={"device_pk": self.device.pk},
            )
        )

    def test_arming_the_accepted_rows_does_not_multiply_the_adapter_calls(self):
        """Without ATOMIC_REQUESTS each overlay save pushes the FULL snapshot immediately, so
        arming N rows one by one turns one bulk accept into N+1 adapter PUTs, each carrying a
        half-armed snapshot."""
        states = self._drifted(3)

        with patch(PUT) as put:
            response = self._post()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(put.call_count, 1)
        for state in states:
            state.refresh_from_db()
            self.assertEqual(state.status, "accepted")
            self.assertGreater(state.intent_generation, 0)
        pushed = put.call_args.args[1]
        self.assertEqual(len(pushed), 3)
        self.assertTrue(all(route["generation"] for route in pushed))

    def test_the_bulk_accept_push_is_claimed_and_sequenced(self):
        """Codex O1 F5 — a bulk accept called the builder directly, so it sent with no claim.

        No sequence, no entry, no durable record: the swallowed failure of a push that
        published ownership left nothing for the tick to carry, which is the one thing the
        outbox exists to prevent.
        """
        from ._outbox_case import ReceiptAdapter, entries, state_of

        self._drifted(2)
        adapter = ReceiptAdapter()
        config, session = adapter.patches()
        with config, session:
            response = self._post()

        assert response.status_code == 302
        mine = [request for request in adapter.requests if "/devices/8821/" in request["url"]]
        assert len(mine) == 1, mine
        assert mine[0]["push_seq"] is not None, "the bulk accept reached the adapter outside the protocol"
        assert state_of(self.device, "static_route").last_success_digest != ""
        assert entries(self.device, "static_route") == []

    def test_no_observer_ever_sees_an_accepted_row_still_on_its_old_generation(self):
        """Committing the status ahead of the generation leaves a window in which a concurrent
        Apply force-pushes the freshly accepted row on the generation the *last* apply named,
        and moves it to deploying — after which the arming pass no longer matches it."""
        from django.db import connections
        from django.db.models import QuerySet

        from netbox_nso_plugin.models import NSOStaticRouteState

        state = self._drifted(1)[0]
        stale_generation = state.intent_generation
        original, observed = QuerySet.update, []

        def _observe(pk):
            try:
                row = NSOStaticRouteState.objects.get(pk=pk)
                observed.append((row.status, row.intent_generation))
            finally:
                connections.close_all()

        def _update_then_observe(self, **kwargs):
            result = original(self, **kwargs)
            if kwargs.get("status") == "accepted":
                # A second connection: it sees only what has been COMMITTED so far.
                watcher = threading.Thread(target=_observe, args=(state.pk,))
                watcher.start()
                watcher.join(timeout=30)
            return result

        with patch(PUT), patch.object(QuerySet, "update", _update_then_observe):
            response = self._post()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0], ("accepted", stale_generation))
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        self.assertGreater(state.intent_generation, stale_generation)

    def test_a_row_a_reconcile_moved_on_is_not_clobbered_by_the_captured_pks(self):
        """Selecting by pk alone drops the status predicate the original UPDATE carried, so a
        row a background reconcile has already re-classified is overwritten with `accepted`."""
        from django.db.models import QuerySet

        from netbox_nso_plugin.models import NSOStaticRouteState

        state = self._drifted(1)[0]
        original, injected = QuerySet.update, []

        def _reclassify_then_update(self, **kwargs):
            # The accept UPDATE only: the imported -> in_sync pass runs first, and injecting
            # there lands outside the window this pin is about.
            if not injected and kwargs.get("status") == "accepted":
                injected.append(True)
                original(NSOStaticRouteState.objects.filter(pk=state.pk), status="imported")
            return original(self, **kwargs)

        with patch(PUT), patch.object(QuerySet, "update", _reclassify_then_update):
            response = self._post()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(injected)
        state.refresh_from_db()
        self.assertNotEqual(state.status, "accepted")


class TestStaticRouteTransitionFanOut(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """P2.8, P2.9, P2.12 — the arms that only exist across a real transaction boundary."""

    def setUp(self):
        super().setUp()
        self.d1 = _make_device("fan", 1)
        self.d2 = _make_device("fan", 2)
        self.mgmt1 = _make_mgmt(self.d1, "fan", 8811)
        self.mgmt2 = _make_mgmt(self.d2, "fan", 8812)
        user = get_user_model().objects.create_superuser(username="tr-fan-admin", password="x", email="f@example.com")
        self.client.force_login(user)

    def _assert_put_patch_did_not_leak(self):
        """``mock.patch`` is not thread-safe. Two overlapping patches of one target make the
        second record the first's ``MagicMock`` as the original, so the last ``__exit__``
        reinstalls that mock for the rest of the process and a later test silently calls it.
        """
        self.assertIs(_adapter_client.put_static_route_intent, _REAL_PUT)

    def test_an_inline_overlay_edit_fans_out_to_every_owning_device(self):
        """P2.8 — the inline edit saves the SHARED fork object under suppression and then only
        the selected overlay, so D2's content changes with no demotion, bump or push."""
        with _fixtures():
            sr = _route("10.32.0.0/16", "10.0.0.1", devices=[self.d1, self.d2])
            s1 = _own(sr, self.mgmt1, status="in_sync")
            s2 = _own(sr, self.mgmt2, status="in_sync")
        g1, g2 = s1.intent_generation, s2.intent_generation

        with patch(PUT) as put:
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "static_route", "pk": s1.pk}),
                {"metric": "9"},
            )

        self.assertEqual(response.status_code, 200)
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertEqual(s1.status, "accepted")
        self.assertEqual(s2.status, "accepted")
        self.assertGreater(s1.intent_generation, g1)
        self.assertGreater(s2.intent_generation, g2)
        pushed = [call.args[0] for call in put.call_args_list]
        self.assertCountEqual(pushed, [8811, 8812])  # exactly one push per device

    def test_a_bulk_edit_coalesces_to_one_push_per_device(self):
        """P2.8 — three routes across two devices in one transaction is one PUT each, not six,
        and every one of the six overlays is demoted and re-armed."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        routes = []
        with _fixtures():
            for index in range(3):
                sr = _route(f"10.33.{index}.0/24", "10.0.0.1", devices=[self.d1, self.d2])
                _own(sr, self.mgmt1, status="in_sync")
                _own(sr, self.mgmt2, status="in_sync")
                routes.append(sr)
        before = dict(
            NSOStaticRouteState.objects.filter(static_route__in=routes).values_list("pk", "intent_generation")
        )

        with patch(PUT) as put:
            with transaction.atomic():
                for sr in routes:
                    sr.metric = 11
                    sr.save(update_fields=["metric"])

        pushed = [call.args[0] for call in put.call_args_list]
        self.assertCountEqual(pushed, [8811, 8812])
        for state in NSOStaticRouteState.objects.filter(static_route__in=routes):
            self.assertEqual(state.status, "accepted")
            self.assertGreater(state.intent_generation, before[state.pk])

    def test_concurrent_edits_of_one_route_never_share_a_generation(self):
        """P2.9(a)/(c) — two transactions edit the same shared route from opposite ends; both
        must commit (the lock order is canonical) and neither may reuse the other's generation."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        with _fixtures():
            sr = _route("10.34.0.0/16", "10.0.0.1", devices=[self.d1, self.d2])
            s1 = _own(sr, self.mgmt1, status="in_sync")
            s2 = _own(sr, self.mgmt2, status="in_sync")
        highest = max(s1.intent_generation, s2.intent_generation)
        seen: list[int] = []
        errors: list[BaseException] = []
        start = threading.Barrier(2, timeout=30)

        def _edit(metric):
            from netbox_routing.models import StaticRoute

            try:
                start.wait()
                with transaction.atomic():
                    row = StaticRoute.objects.get(pk=sr.pk)
                    row.metric = metric
                    row.save(update_fields=["metric"])
                    seen.extend(
                        NSOStaticRouteState.objects.filter(static_route=sr).values_list("intent_generation", flat=True)
                    )
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=_edit, args=(m,)) for m in (21, 22)]
        # One patch, taken in the main thread: `mock.patch` is not thread-safe, so entering it
        # per worker leaves a MagicMock installed on the module for the rest of the process.
        with patch(PUT):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 4)  # no generation is ever issued twice
        self.assertTrue(all(value > highest for value in seen))
        self._assert_put_patch_did_not_leak()

    def test_a_re_added_overlay_outruns_every_generation_ever_issued(self):
        """P2.9(b) — reusing a value an unconsumed result still carries would false-green the
        new lifecycle; the allocator is plugin-global, not per row."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        with _fixtures():
            sr = _route("10.35.0.0/16", "10.0.0.1", devices=[self.d1])
            state = _own(sr, self.mgmt1, status="in_sync")
        with patch(PUT):
            with transaction.atomic():
                sr.metric = 31
                sr.save(update_fields=["metric"])
        state.refresh_from_db()
        highest = state.intent_generation

        with patch(PUT):
            with transaction.atomic():
                NSOStaticRouteState.objects.filter(pk=state.pk).delete()
                sr.devices.remove(self.d1)
                sr.devices.add(self.d1)

        recreated = NSOStaticRouteState.objects.get(management=self.mgmt1, static_route=sr)
        self.assertGreater(recreated.intent_generation, highest)

    def test_the_fan_out_reads_the_overlays_not_the_pre_set_membership(self):
        """P2.12 — the fork's form writes the row and only THEN calls devices.set(), so at
        post_save ``instance.devices`` still lists the pre-edit membership."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        with _fixtures():
            sr = _route("10.36.0.0/16", "10.0.0.1", devices=[self.d1])
            s1 = _own(sr, self.mgmt1, status="in_sync")
        before = s1.intent_generation

        with patch(PUT):
            with transaction.atomic():
                sr.metric = 41
                sr.save(update_fields=["metric"])  # post_save: devices still == [d1]
                sr.devices.set([self.d1, self.d2])  # the form's second step

        s1.refresh_from_db()
        self.assertEqual(s1.status, "accepted")
        self.assertGreater(s1.intent_generation, before)
        s2 = NSOStaticRouteState.objects.get(management=self.mgmt2, static_route=sr)
        self.assertEqual(s2.status, "accepted")
        self.assertGreater(s2.intent_generation, 0)

    def test_an_edit_that_restores_the_value_a_concurrent_edit_replaced_still_transitions(self):
        """The baseline must be read under the same lock as the write. Two edits that both
        load content A cannot both keep A as their baseline: the one that lands second writes
        A back over the first's B, sees "no delta", and pushes nothing — so the adapter is
        left holding B while NetBox reads A."""
        from netbox_routing.models import StaticRoute

        with _fixtures():
            sr = _route("10.38.0.0/16", "10.0.0.1", devices=[self.d1])
            state = _own(sr, self.mgmt1, status="in_sync")

        first_wrote = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def _write(next_hop, before=None, after=None):
            try:
                with transaction.atomic():
                    row = StaticRoute.objects.get(pk=sr.pk)
                    row.next_hop = next_hop
                    if before is not None:
                        before.set()
                    row.save(update_fields=["next_hop"])
                    if after is not None:
                        after.wait(timeout=30)
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)
            finally:
                connection.close()

        # T1 writes B and holds its transaction open; T2 then writes A (the value T1 replaced).
        # One patch, taken in the main thread: `mock.patch` is not thread-safe, so entering it
        # per worker leaves a MagicMock installed on the module for the rest of the process.
        with patch(PUT):
            t1 = threading.Thread(target=_write, args=("10.0.0.2",), kwargs={"before": first_wrote, "after": release})
            t1.start()
            first_wrote.wait(timeout=30)
            t2 = threading.Thread(target=_write, args=("10.0.0.1",))
            t2.start()
            t2.join(timeout=5)  # blocks on T1's row lock until it commits
            release.set()
            t1.join(timeout=60)
            t2.join(timeout=60)

        self.assertEqual(errors, [])
        state.refresh_from_db()
        sr.refresh_from_db()
        self.assertEqual(str(sr.next_hop), "10.0.0.1")
        self.assertEqual(state.nso_next_hop, "10.0.0.1")
        self._assert_put_patch_did_not_leak()

    def test_the_overlay_lock_is_taken_in_ascending_management_id_order(self):
        """P2.9(c) — a fan-out over an unordered queryset can take the same two rows in
        opposite orders in two transactions, which deadlocks the operator's save."""
        from django.test.utils import CaptureQueriesContext

        with _fixtures():
            sr = _route("10.37.0.0/16", "10.0.0.1", devices=[self.d1, self.d2])
            _own(sr, self.mgmt1, status="in_sync")
            _own(sr, self.mgmt2, status="in_sync")

        with patch(PUT), CaptureQueriesContext(connection) as queries:
            with transaction.atomic():
                sr.metric = 51
                sr.save(update_fields=["metric"])

        locking = [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"]]
        self.assertTrue(locking, "the fan-out must lock the overlays it re-arms")
        self.assertTrue(
            any("ORDER BY" in sql and "management_id" in sql for sql in locking),
            f"no management-id-ordered lock among {locking}",
        )
