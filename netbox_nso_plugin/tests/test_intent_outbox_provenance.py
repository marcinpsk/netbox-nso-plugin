# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1) — what an operator transaction leaves in the outbox.

The entry is written by the operator's own transaction, so the database decides what
survives: pins O1.2 (a whole-transaction rollback leaves nothing) and O1.3 (a nested
savepoint rollback leaves the outer deletion's provenance and only that). O1.15 keeps
reconcile and render writes out of the outbox altogether, O1.4 keeps an unmigrated
``query_flag`` scope's marking intact, O1.20 records a deleted route id in both marking
modes, and O1.18 proves concurrent appends remain compatible under the shared deployment lock.
"""

from __future__ import annotations

import threading
import uuid
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase

from ._outbox_case import marking_mode, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

PUT_STATIC = "netbox_nso_plugin.adapter_client.put_static_route_intent"
PUT_VLAN = "netbox_nso_plugin.adapter_client.put_vlan_intent"
_CFG = {"url": "http://adapter", "token": "tok", "verify_tls": True, "ca_cert_path": None, "timeout": 30}


class _Abort(Exception):
    """Raised to roll a block back without failing the test."""


def _make_device(tag: str, index: int = 1):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"Ob{tag}Mfg", slug=f"ob{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Ob{tag}Dev", slug=f"ob{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Ob{tag}Role", slug=f"ob{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Ob{tag}Site", slug=f"ob{tag}site")
    return Device.objects.create(name=f"ob-{tag}-rtr-{index}", device_type=dt, role=role, site=site)


def _make_mgmt(device, tag: str, adapter_device_id: int):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    inst, _ = NSOInstance.objects.get_or_create(
        name=f"ob-{tag}-inst", defaults={"adapter_instance_id": f"ob-{tag}-inst"}
    )
    return NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"nso-ob-{tag}-{device.pk}",
        adapter_device_id=adapter_device_id,
    )


def _entries(device, scope):
    from netbox_nso_plugin.models import NSOIntentOutboxEntry

    return list(NSOIntentOutboxEntry.objects.filter(device=device, scope=scope).order_by("id"))


def _transitions(device, scope):
    """Every unconsumed transition for the key, in entry-id order — the fold's input."""
    return [t for entry in _entries(device, scope) if entry.consumed_by_push_seq is None for t in entry.transitions]


def _own_route(mgmt, prefix, next_hop, *, device=None):
    """A route assigned to the device and owned by it, exactly as the accept path leaves it."""
    from netbox_routing.models import StaticRoute

    from ._static_route_case import _assign_and_accept

    with without_commit_drain(), transaction.atomic():
        route = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, metric=1)
        _assign_and_accept(route, device or mgmt.device)
    return route


class TestOutboxSuppression(IntentPushResetMixin, TestCase):
    """O1.15 — a reconcile write and a render write are not operator intent."""

    def setUp(self):
        super().setUp()
        self.device = _make_device("sup")
        self.mgmt = _make_mgmt(self.device, "sup", 7401)

    @staticmethod
    def _request(method: str):
        """A request complete enough for NetBox's own change-logging receiver to run."""
        from django.contrib.auth import get_user_model

        request = getattr(RequestFactory(), method)("/")
        request.user = get_user_model().objects.create_user(username=f"ob-sup-{method}", password="x")
        request.id = uuid.uuid4()
        return request

    def _accepted_vlan_state(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        vlan = VLAN.objects.create(vid=701, name="ob-sup-v701")
        with patch(PUT_VLAN), self.captureOnCommitCallbacks(execute=True):
            return NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, status="accepted")

    def test_a_save_under_suppression_writes_no_entry(self):
        from django.utils import timezone

        from netbox_nso_plugin.intent_state import mirror_refresh
        from netbox_nso_plugin.signals import suppress_intent_push

        state = self._accepted_vlan_state()
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()

        with patch(PUT_VLAN), suppress_intent_push(), self.captureOnCommitCallbacks(execute=True):
            state.last_sync_at = timezone.now()
            with mirror_refresh(state, {"last_sync_at"}):
                state.save(update_fields=["last_sync_at"])

        assert _entries(self.device, "vlan") == []

    def test_a_save_during_a_get_render_writes_no_entry(self):
        from netbox.context import current_request

        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        state = self._accepted_vlan_state()
        NSOIntentOutboxEntry.objects.all().delete()

        token = current_request.set(self._request("get"))
        try:
            with patch(PUT_VLAN), self.captureOnCommitCallbacks(execute=True):
                state.save()
        finally:
            current_request.reset(token)

        assert _entries(self.device, "vlan") == []

    def test_an_own_writer_save_does_write_an_entry(self):
        """The explicit writer preserves prompt bookkeeping for plugin-owned saves."""
        from netbox.context import current_request

        from netbox_nso_plugin.models import NSOIntentOutboxEntry
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes

        state = self._accepted_vlan_state()
        NSOIntentOutboxEntry.objects.all().delete()
        state.status = "imported"

        token = current_request.set(self._request("post"))
        try:
            with patch(PUT_VLAN), self.captureOnCommitCallbacks(execute=True):
                plan = RendererMutationPlan.build(saves=(planned_save(state),))
                with renderer_writes(plan) as writer:
                    writer.save(state)
        finally:
            current_request.reset(token)

        assert len(_entries(self.device, "vlan")) == 1


class TestOutboxRollback(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O1.2, O1.3 — the entry is a database row, so a rollback is the database's answer."""

    def setUp(self):
        super().setUp()
        self.device = _make_device("rb")
        self.mgmt = _make_mgmt(self.device, "rb", 7402)

    def test_a_rolled_back_transaction_leaves_no_entry_and_the_next_edit_still_records(self):
        """O1.2 — the in-memory coalescer keeps a rolled-back key (`signals.py:265`); the
        durable record must not, and the fresh edit's own work must still be pending."""
        from netbox_routing.models import StaticRoute

        route = StaticRoute.objects.create(prefix="203.0.113.0/24", next_hop="203.0.113.1", metric=1)
        with without_commit_drain():
            try:
                with transaction.atomic():
                    route.devices.add(self.device)
                    raise _Abort
            except _Abort:
                pass

        assert _entries(self.device, "static_route") == []

        with without_commit_drain(), transaction.atomic():
            route.devices.add(self.device)

        pending = [e for e in _entries(self.device, "static_route") if e.consumed_by_push_seq is None]
        assert pending, "the fresh edit must leave the drain a durable record of its work"

    def test_a_rolled_back_savepoint_leaves_only_the_committed_deletion(self):
        """O1.3 — provenance, not content: the folded authority is exactly ``{S}``."""
        from netbox_nso_plugin.outbox import fold_transitions

        route_r = _own_route(self.mgmt, "203.0.113.16/28", "203.0.113.2")
        route_s = _own_route(self.mgmt, "203.0.113.32/28", "203.0.113.3")

        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        NSOIntentOutboxEntry.objects.all().delete()

        with without_commit_drain(), transaction.atomic():
            try:
                with transaction.atomic():  # a savepoint Django rolls back on its own
                    route_r.devices.remove(self.device)
                    raise _Abort
            except _Abort:
                pass
            route_s.devices.remove(self.device)

        folded = fold_transitions(_transitions(self.device, "static_route"))
        assert set(folded.queued) == {route_s.pk}


class TestOutboxMarkingModes(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O1.4, O1.20 — the record is mode-blind; only the emission is mode-gated."""

    def setUp(self):
        super().setUp()
        self.device = _make_device("mk")
        self.mgmt = _make_mgmt(self.device, "mk", 7403)

    def _recorded_params(self, act):
        """Run *act* against a recorded transport → the params of every adapter request."""
        from ._adapter_http import make_response, make_session

        session = make_session(response=make_response(200, json_data={"count": 0, "routes": []}))
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            act()
        return [call.kwargs.get("params") or {} for call in session.request.call_args_list]

    def _owned_vlan_state(self, vid: int):
        from ._outbox_case import own_vlan

        return own_vlan(self.mgmt, vid, "mk")

    def test_a_committed_vlan_deletion_still_ships_the_query_flag(self):
        """O1.4 — the outbox must not disturb an unmigrated scope's marking."""
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        from ._outbox_case import delete_vlan_state

        state = self._owned_vlan_state(711)
        NSOIntentOutboxEntry.objects.all().delete()
        with without_commit_drain():
            delete_vlan_state(state)
        assert [(e.mark_and, e.mark_any) for e in _entries(self.device, "vlan")] == [(True, True)]

        # The drain that ships the mark also retires the row that carried it, so the wire is
        # asserted on a second deletion rather than on the record above.
        other = self._owned_vlan_state(713)
        NSOIntentOutboxEntry.objects.all().delete()
        params = self._recorded_params(lambda: delete_vlan_state(other))
        assert any(p.get("delete_origin") == "true" for p in params), f"saw {params}"

    def test_a_rolled_back_vlan_deletion_contributes_no_mark(self):
        """O1.4 — the rolled-back deletion leaves neither an entry nor a mark to AND in."""
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOVLANState

        from ._outbox_case import delete_vlan_state, enqueue

        state = self._owned_vlan_state(712)
        NSOIntentOutboxEntry.objects.all().delete()

        with without_commit_drain():
            try:
                with transaction.atomic():
                    delete_vlan_state(state)
                    raise _Abort
            except _Abort:
                pass

        assert _entries(self.device, "vlan") == []

        # The row came back with the rollback. Add one unmarked contribution as the fold control.
        self.assertTrue(NSOVLANState.objects.filter(management=self.mgmt, vlan__vid=712).exists())
        with without_commit_drain(), transaction.atomic():
            enqueue(self.device, "vlan")

        assert [e.mark_and for e in _entries(self.device, "vlan")] == [False]

    def test_a_deleted_route_id_is_recorded_in_both_marking_modes(self):
        """O1.20 — an O3→O1 rollback must strand no authority, so O1 records the id already."""
        from netbox_nso_plugin.delivery import MARKING_PER_OBJECT, MARKING_QUERY_FLAG, delivery_keys
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        registry = delivery_keys()
        assert registry["static_route"].marking_mode == MARKING_PER_OBJECT
        prefixes = {MARKING_QUERY_FLAG: "203.0.113.64/28", MARKING_PER_OBJECT: "203.0.113.96/28"}
        for mode, prefix in prefixes.items():
            NSOIntentOutboxEntry.objects.all().delete()
            route = _own_route(self.mgmt, prefix, "203.0.113.4")
            NSOIntentOutboxEntry.objects.all().delete()

            with marking_mode("static_route", mode):
                with without_commit_drain():
                    route.devices.remove(self.device)

            recorded = [t for t in _transitions(self.device, "static_route") if t["op"] == "delete"]
            assert [(t["op"], t["route_id"]) for t in recorded] == [("delete", route.pk)], f"{mode}: {recorded}"

    def test_only_the_emission_is_mode_gated(self):
        """O1.20: a query-flag configuration still emits only the legacy query flag."""
        from netbox_nso_plugin.delivery import MARKING_QUERY_FLAG
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        route = _own_route(self.mgmt, "203.0.113.128/28", "203.0.113.5")
        # The accept's own entry is unmarked, and the fold ANDs it in; the pin is about the
        # deletion's flag, so it is the only contributor left.
        NSOIntentOutboxEntry.objects.all().delete()

        with marking_mode("static_route", MARKING_QUERY_FLAG):
            params = self._recorded_params(lambda: route.devices.remove(self.device))

        assert any(p.get("delete_origin") == "true" for p in params), f"saw {params}"


class TestOutboxEnqueueSharedLockCompatibility(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """O1.18 — the shared deployment lock keeps disjoint footprints compatible."""

    def test_opposite_key_orders_both_commit(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOIntentOutboxState

        devices = (_make_device("lk", 1), _make_device("lk", 2))
        for index, device in enumerate(devices):
            _make_mgmt(device, f"lk-{index}", 7404 + index)
        barrier = threading.Barrier(2, timeout=30)
        errors: list[BaseException] = []

        def _append(device_id, scopes):
            from netbox_nso_plugin.intent_state import MutationFootprint, intent_transaction
            from netbox_nso_plugin.signals import _schedule_intent_push

            try:
                footprint = MutationFootprint.for_keys({(device_id, scope) for scope in scopes})
                with intent_transaction(footprint):
                    _schedule_intent_push((device_id, scopes[0]))
                    barrier.wait()
                    _schedule_intent_push((device_id, scopes[1]))
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=_append, args=(devices[0].pk, ("vlan", "interface"))),
            threading.Thread(target=_append, args=(devices[1].pk, ("interface", "vlan"))),
        ]
        with without_commit_drain():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        # join() returns on timeout as readily as on completion, so a deadlock would other-
        # wise read as an empty error list and a row count taken while the writers still run.
        for thread in threads:
            assert not thread.is_alive(), "worker did not finish - deadlock"
        assert errors == []
        assert NSOIntentOutboxEntry.objects.filter(device__in=devices).count() == 4
        # The state row is the drain's mutual-exclusion point; an enqueue that touched it
        # would make two operator transactions serialize on one row for no reason.
        assert not NSOIntentOutboxState.objects.filter(device__in=devices).exists()


class TestOutboxTeardown(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The entry is keyed on ``dcim.Device``, and a teardown cascade is not operator intent.

    Unmanaging tears every overlay down, and each of those post_deletes schedules a
    delete-marked push the drain then drops. The outbox must not record that cascade at all:
    the pending authority of the key has to survive an offboard/re-onboard (P29), and a row
    appended while the device itself is going away would fail its deferred foreign key at
    COMMIT.
    """

    def setUp(self):
        super().setUp()
        self.device = _make_device("td")
        self.mgmt = _make_mgmt(self.device, "td", 7405)

    def _pending_entry(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        route = _own_route(self.mgmt, "203.0.113.192/28", "203.0.113.6")
        NSOIntentOutboxEntry.objects.all().delete()
        with without_commit_drain():
            route.devices.remove(self.device)
        assert _entries(self.device, "static_route"), "the deletion must have been recorded"
        return route

    def test_unmanaging_records_nothing_and_keeps_the_pending_authority(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        self._pending_entry()
        before = [e.pk for e in _entries(self.device, "static_route")]

        with patch("netbox_nso_plugin.adapter_client.delete_device"):
            self.mgmt.delete()

        assert [e.pk for e in _entries(self.device, "static_route")] == before
        assert NSOIntentOutboxEntry.objects.filter(device=self.device).count() == len(before)

    def test_deleting_the_device_cascades_its_entries_away(self):
        from netbox_nso_plugin.models import NSOIntentOutboxEntry

        self._pending_entry()
        device_id = self.device.pk

        with patch("netbox_nso_plugin.adapter_client.delete_device"):
            self.device.delete()

        assert not NSOIntentOutboxEntry.objects.filter(device_id=device_id).exists()

    def test_device_delete_suppresses_a_cascaded_svi_overlay_append(self):
        from dcim.models import Interface

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOSVIState
        from netbox_nso_plugin.signals import suppress_intent_push

        interface = Interface.objects.create(device=self.device, name="Vlan444", type="virtual")
        state = NSOSVIState(management=self.mgmt, interface=interface, status="accepted")
        with suppress_intent_push(), intent_transaction(footprint_for_instance(state)):
            state.save()
        NSOIntentOutboxEntry.objects.all().delete()
        device_id = self.device.pk

        with patch("netbox_nso_plugin.adapter_client.delete_device"):
            self.device.delete()

        assert not NSOIntentOutboxEntry.objects.filter(device_id=device_id).exists()

    def test_a_failed_teardown_does_not_suppress_a_later_edit_in_the_outer_transaction(self):
        from django.db.models.signals import pre_delete

        from netbox_nso_plugin import outbox
        from netbox_nso_plugin.intent_state import content_mutation
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentOutboxEntry

        class AbortTeardown(Exception):
            pass

        def abort_after_mark(sender, instance, **kwargs):
            raise AbortTeardown

        pre_delete.connect(abort_after_mark, sender=NSODeviceManagement, weak=False)
        self.addCleanup(pre_delete.disconnect, abort_after_mark, sender=NSODeviceManagement)

        with transaction.atomic():
            try:
                with transaction.atomic():
                    self.mgmt.delete()
            except AbortTeardown:
                pass
            with content_mutation({(self.device.pk, "vlan")}):
                outbox.enqueue(self.device.pk, "vlan")

        assert NSOIntentOutboxEntry.objects.filter(device=self.device, scope="vlan").exists()

    def test_a_failed_teardown_leaves_no_mark_behind(self):
        """No caller clears a rolled-back mark, so the reader that outlives it must prune it."""
        from django.db.models.signals import pre_delete

        from netbox_nso_plugin import outbox
        from netbox_nso_plugin.intent_state import content_mutation
        from netbox_nso_plugin.models import NSODeviceManagement

        class AbortTeardown(Exception):
            pass

        def abort_after_mark(sender, instance, **kwargs):
            raise AbortTeardown

        pre_delete.connect(abort_after_mark, sender=NSODeviceManagement, weak=False)
        self.addCleanup(pre_delete.disconnect, abort_after_mark, sender=NSODeviceManagement)

        with transaction.atomic():
            try:
                with transaction.atomic():
                    self.mgmt.delete()
            except AbortTeardown:
                pass
            assert self.device.pk in outbox._teardown_marks(), "the aborted deletion never marked the device"
            with content_mutation({(self.device.pk, "vlan")}):
                outbox.enqueue(self.device.pk, "vlan")

            assert self.device.pk not in outbox._teardown_marks(), (
                "the mark of a scope that is gone stays for the life of the worker thread"
            )
