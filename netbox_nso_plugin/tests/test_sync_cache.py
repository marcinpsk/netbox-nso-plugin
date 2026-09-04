# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The cached mirror of the adapter's per-device last-sync state, and link reconciliation.

``NSODeviceManagement.last_sync_at``/``last_sync_status``/``degraded_surfaces`` are a copy of
the adapter's device row. The adapter refreshes its own side on a scheduler, so what these
tests answer is whether NetBox's copy tracks it *without* an operator opening a page — a device
onboarded and never visited showed a blank status forever, and rows last visited days ago
showed a stale "succeeded" for devices the adapter now reports as partial or never-synced.

Every adapter payload here is built by :func:`_adapter_row` FROM the management row it
describes, so logical identity (``nso_instance`` + ``nso_device_name`` + NetBox device) always
matches by construction. Hand-written payloads with a fixed id and someone else's identity are
exactly the mismatch the production code now refuses to trust.
"""

import threading
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from dcim.models import Device
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

from ._adapter_http import make_session
from ._outbox_case import in_thread
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_django_views import ViewTestBase

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}

_SYNC_TS = "2026-07-28T15:14:10.002723Z"
_SYNCED_AT = datetime(2026, 7, 28, 15, 14, 10, 2723, tzinfo=UTC)

# A standalone adapter device row, for tests that only need an opaque payload.
_ANY_DEVICE = {
    "id": 619,
    "nso_instance": "nso-dev",
    "nso_device_name": "some-rtr",
    "netbox_device_id": 124,
    "last_sync_at": _SYNC_TS,
    "last_sync_status": "succeeded",
    "degraded_surfaces": None,
}


def _adapter_row(mgmt, **overrides):
    """Build the adapter's ``DeviceOut`` for *mgmt*, as ``GET /api/v1/devices`` serves it."""
    row = {
        "id": mgmt.adapter_device_id,
        "nso_instance": mgmt.nso_instance.adapter_instance_id,
        "nso_device_name": mgmt.nso_device_name,
        "netbox_device_id": mgmt.device_id,
        "last_sync_at": _SYNC_TS,
        "last_sync_status": "succeeded",
        "degraded_surfaces": None,
    }
    row.update(overrides)
    return row


def _stamp_verified_baselines(mgmt):
    """Give *mgmt* a verified delivery baseline on every scope, as a settled audit leaves it."""
    from netbox_nso_plugin import delivery
    from netbox_nso_plugin.models import NSOIntentRevision

    for scope in delivery.delivery_keys():
        NSOIntentRevision.objects.update_or_create(
            device=mgmt.device,
            scope=scope,
            defaults={
                "revision": 4,
                "verified_revision": 4,
                "verified_fingerprint": f"verified-{scope}",
                "verified_at": timezone.now(),
            },
        )


def _scope_404_for(*dead_ids):
    """Return a ``set_scope`` stand-in that 404s the given ids and succeeds otherwise.

    Mirrors ``PUT /devices/{id}/scope``, which answers ``not_found`` only when that numeric id
    has no row. That 404 is what drives the re-onboard, so a test that lets the dead id succeed
    proves nothing — it passes through a path production never takes.
    """
    dead = set(dead_ids)

    def _set_scope(adapter_device_id, *args, **kwargs):
        if adapter_device_id in dead:
            raise AdapterError("Device not found", code="not_found")
        return {}

    return _set_scope


class TestListDevicesClient(unittest.TestCase):
    """adapter_client.list_devices() — the one bulk call the sweep is built on."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_calls_expected_endpoint_and_returns_payload(self, mock_session_cls, _cfg):
        from netbox_nso_plugin.adapter_client import list_devices

        session = make_session(json_data=[_ANY_DEVICE])
        mock_session_cls.return_value = session

        result = list_devices()

        args, _kwargs = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://adapter.local/api/v1/devices")
        self.assertEqual(result, [_ANY_DEVICE])


class _SyncCacheTestBase(TestCase):
    """Real NSODeviceManagement rows against a faked adapter device list."""

    @classmethod
    def setUpTestData(cls):
        cls.instance = NSOInstance.objects.create(name="cache", adapter_instance_id="cache-nso")

    def _mgmt(self, name, adapter_device_id, **kwargs):
        from .test_onboarding import _device

        return NSODeviceManagement.objects.create(
            device=_device(name),
            nso_instance=self.instance,
            nso_device_name=name,
            adapter_device_id=adapter_device_id,
            **kwargs,
        )


class TestInvalidateDeliveryBaselines(_SyncCacheTestBase):
    def test_a_partially_held_scope_set_locks_only_the_remaining_revisions(self):
        from netbox_nso_plugin import apply_state, delivery
        from netbox_nso_plugin.intent_state import MutationFootprint, mirror_transaction
        from netbox_nso_plugin.sync_cache import invalidate_delivery_baselines

        management = self._mgmt("cache-partial-baseline-lock", 618)
        _stamp_verified_baselines(management)
        scopes = tuple(delivery.delivery_keys())
        self.assertGreater(len(scopes), 1)
        held = min(scopes)
        unlocked = tuple(scope for scope in scopes if scope != held)

        with mirror_transaction(MutationFootprint.for_keys(((management.device_id, held),))):
            with patch(
                "netbox_nso_plugin.apply_state.lock_intent_revisions",
                wraps=apply_state.lock_intent_revisions,
            ) as lock_revisions:
                invalidate_delivery_baselines(management.device_id)

        lock_revisions.assert_called_once_with(management.device_id, unlocked)


class TestRefreshSyncCaches(_SyncCacheTestBase):
    """refresh_sync_caches(): mirror the adapter's last-sync state onto the NetBox rows."""

    def test_fills_a_row_that_was_never_visited(self):
        """A device onboarded but never opened gets its blank last-sync columns filled."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-rtr", 619)
        self.assertIsNone(mgmt.last_sync_at)

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(mgmt)]):
            checked, updated = refresh_sync_caches(NSODeviceManagement.objects.all())

        self.assertEqual((checked, updated), (1, 1))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_at, _SYNCED_AT)
        self.assertEqual(mgmt.last_sync_status, "succeeded")

    def test_overwrites_a_stale_succeeded_with_the_current_partial(self):
        """A row cached days ago as 'succeeded' tracks the adapter's current 'partial'."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt(
            "cache-partial",
            620,
            last_sync_at=datetime(2026, 7, 18, 11, 50, 6, tzinfo=UTC),
            last_sync_status="succeeded",
        )
        payload = _adapter_row(mgmt, last_sync_status="partial", degraded_surfaces=["bgp", "ospf"])

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[payload]):
            refresh_sync_caches(NSODeviceManagement.objects.all())

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "partial")
        self.assertEqual(mgmt.degraded_surfaces, ["bgp", "ospf"])
        self.assertEqual(mgmt.last_sync_at, _SYNCED_AT)

    def test_clears_a_stale_timestamp_when_the_adapter_never_synced(self):
        """Adapter reports last_sync_at=None → the cached date is cleared, not kept.

        Keeping it left a device the adapter has never synced reading 'succeeded' with a
        ten-day-old date — a lie in the exact place an operator checks for freshness.
        """
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt(
            "cache-never",
            621,
            last_sync_at=datetime(2026, 7, 18, 11, 50, 6, tzinfo=UTC),
            last_sync_status="succeeded",
        )
        payload = _adapter_row(mgmt, last_sync_at=None, last_sync_status=None)

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[payload]):
            refresh_sync_caches(NSODeviceManagement.objects.all())

        mgmt.refresh_from_db()
        self.assertIsNone(mgmt.last_sync_at)
        self.assertEqual(mgmt.last_sync_status, "")

    def test_does_not_clear_a_link_error_on_an_older_successful_sync(self):
        """A fresh scope-push failure survives a refresh that reads an OLDER successful sync.

        The two fields are different axes: ``adapter_link_error`` is a plugin→adapter LINK
        failure, ``last_sync_status`` is the adapter→NSO DEVICE sync outcome, stamped whenever
        the adapter last synced. Retiring the error on a 'succeeded' that predates it erased the
        operator's only signal — and its "Retry adapter link" button — for a scope that never
        landed. The real clears are the successful push (signals) and the retry action.
        """
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-link-err", 619)
        # Stage the error the way production writes it — .update() fires no signals, so the
        # row's own save can't clear it first.
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(adapter_link_error="Internal Server Error")

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(mgmt)]):
            refresh_sync_caches(NSODeviceManagement.objects.all())

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_link_error, "Internal Server Error")
        self.assertEqual(mgmt.last_sync_status, "succeeded")  # the refresh did run

    def test_refuses_to_mirror_a_reused_id(self):
        """An id that now belongs to a DIFFERENT device must not have its status copied over.

        Adapter ids are per-install serials, so a rebuilt/restored adapter DB can hand id 619
        to another device. Trusting the bare number would show that device's sync state here —
        a wrong answer that looks perfectly healthy.
        """
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-ours", 619, last_sync_status="")
        stranger = _adapter_row(mgmt, nso_device_name="somebody-else", netbox_device_id=mgmt.device_id + 999)

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[stranger]):
            checked, updated = refresh_sync_caches(NSODeviceManagement.objects.all())

        self.assertEqual((checked, updated), (1, 0))
        mgmt.refresh_from_db()
        self.assertIsNone(mgmt.last_sync_at)  # nothing copied from the stranger
        self.assertEqual(mgmt.last_sync_status, "")

    def test_mirrors_an_adapter_row_not_yet_linked_to_netbox(self):
        """A row for our NSO node with netbox_device_id still null is ours to mirror."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-unlinked", 622)

        with patch(
            "netbox_nso_plugin.adapter_client.list_devices",
            return_value=[_adapter_row(mgmt, netbox_device_id=None)],
        ):
            checked, updated = refresh_sync_caches(NSODeviceManagement.objects.all())

        self.assertEqual((checked, updated), (1, 1))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "succeeded")

    def test_flags_a_broken_mapping_so_the_page_stops_showing_green(self):
        """A render that PROVES the mapping is wrong records it, rather than leaving green.

        Page renders refresh but do not repair (that is the job's work), so without this the row
        keeps rendering its last good 'succeeded' until the next sweep.
        """
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-flag-broken", 196, last_sync_status="succeeded")

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]):
            refresh_sync_caches(NSODeviceManagement.objects.all())

        mgmt.refresh_from_db()
        self.assertTrue(mgmt.adapter_link_error)

    def test_does_not_flag_a_row_mid_rekey(self):
        """A rekey in flight reads as 'reused' by design — it is not a broken mapping."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-rekeying", 196, last_sync_status="succeeded")
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]):
            refresh_sync_caches(NSODeviceManagement.objects.all())

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_link_error, "")

    def test_refresh_sync_cache_refuses_a_foreign_payload_directly(self):
        """The per-device helper fails closed too — the device tab calls it with one payload.

        The tab fetches GET /devices/{stored id} and mirrors the result; a reused id would
        otherwise show another device's sync state on this device's page.
        """
        from netbox_nso_plugin.sync_cache import refresh_sync_cache

        mgmt = self._mgmt("cache-tab-foreign", 619)
        stranger = _adapter_row(mgmt, nso_device_name="somebody-else", netbox_device_id=mgmt.device_id + 999)

        changed = refresh_sync_cache(mgmt, stranger)

        self.assertEqual(changed, [])
        mgmt.refresh_from_db()
        self.assertIsNone(mgmt.last_sync_at)

    def test_leaves_a_row_the_adapter_does_not_report(self):
        """A managed row missing from the adapter payload keeps its cached values."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-absent", 999, last_sync_at=_SYNCED_AT, last_sync_status="succeeded")
        other = self._mgmt("cache-other", 619)

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(other)]):
            checked, updated = refresh_sync_caches(NSODeviceManagement.objects.all())

        self.assertEqual(updated, 1)  # only the reported one
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "succeeded")

    def test_skips_unmapped_rows_without_calling_the_adapter(self):
        """A row with no adapter_device_id is not a device the adapter knows — no call."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        self._mgmt("cache-unmapped", None)

        with patch("netbox_nso_plugin.adapter_client.list_devices") as bulk:
            checked, updated = refresh_sync_caches(NSODeviceManagement.objects.all())

        bulk.assert_not_called()
        self.assertEqual((checked, updated), (0, 0))

    def test_adapter_error_leaves_the_cache_untouched(self):
        """A transient adapter outage is swallowed; the mirror keeps its last known values."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        mgmt = self._mgmt("cache-down", 619, last_sync_at=_SYNCED_AT, last_sync_status="succeeded")

        with patch("netbox_nso_plugin.adapter_client.list_devices", side_effect=AdapterError("adapter down")):
            checked, updated = refresh_sync_caches(NSODeviceManagement.objects.all())

        self.assertEqual((checked, updated), (1, 0))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "succeeded")

    def test_one_bulk_call_for_many_rows(self):
        """The sweep is one adapter call regardless of fleet size (not one per device)."""
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        rows = [self._mgmt(f"cache-fleet-{i}", 700 + i) for i in range(5)]
        payload = [_adapter_row(m) for m in rows]

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=payload) as bulk:
            checked, updated = refresh_sync_caches(NSODeviceManagement.objects.all())

        self.assertEqual(bulk.call_count, 1)
        self.assertEqual((checked, updated), (5, 5))


class TestRefreshSyncCacheConcurrency(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.instance = NSOInstance.objects.create(
            name="cache-race",
            adapter_instance_id="cache-race",
        )

    def _mgmt(self, name, adapter_device_id):
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction

        from .test_onboarding import _device

        row = NSODeviceManagement(
            device=_device(name),
            nso_instance=self.instance,
            nso_device_name=name,
            adapter_device_id=adapter_device_id,
        )
        with intent_transaction(footprint_for_instance(row)):
            NSODeviceManagement.objects.bulk_create([row])
        return NSODeviceManagement.objects.get(pk=row.pk)

    def test_stale_error_mirror_does_not_abort_the_cache_sweep(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.sync_cache import refresh_sync_caches

        first = self._mgmt("cache-race-first", 196)
        second = self._mgmt("cache-race-second", 197)
        original_build = RendererMutationPlan.build
        plan_staled = False

        def build_then_stale(*args, **kwargs):
            nonlocal plan_staled
            plan = original_build(*args, **kwargs)
            if not plan_staled:
                plan_staled = True
                in_thread(lambda: NSODeviceManagement.objects.filter(pk=first.pk).update(last_sync_status="concurrent"))
            return plan

        errors = []
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch.object(RendererMutationPlan, "build", side_effect=build_then_stale),
            self.assertLogs("netbox_nso_plugin.sync_cache", level="WARNING") as captured,
        ):
            try:
                result = refresh_sync_caches(NSODeviceManagement.objects.all())
            except Exception as exc:  # noqa: BLE001 (the assertion reports the aborted sweep)
                errors.append(exc)
                result = None

        self.assertEqual(errors, [], f"the cache sweep raised {errors!r}")
        self.assertEqual(result, (2, 0))
        self.assertTrue(any(f"management row {first.pk}" in message for message in captured.output))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.last_sync_status, "concurrent")
        self.assertEqual(first.adapter_link_error, "")
        self.assertTrue(second.adapter_link_error)

    def test_stale_error_mirror_is_not_reported_as_updated(self):
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.sync_cache import refresh_sync_cache, refresh_sync_caches

        mgmt = self._mgmt("cache-race-skipped", 198)
        adapter_device = _adapter_row(mgmt)
        original_build = RendererMutationPlan.build
        concurrent_updates = 0

        def build_then_stale(*args, **kwargs):
            nonlocal concurrent_updates
            plan = original_build(*args, **kwargs)
            concurrent_updates += 1
            in_thread(
                lambda: NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
                    last_sync_status=f"concurrent-{concurrent_updates}"
                )
            )
            return plan

        identity = (mgmt.nso_instance.adapter_instance_id, mgmt.nso_device_name)
        snapshot = ([mgmt], {mgmt.adapter_device_id: adapter_device}, {identity: [adapter_device]})
        with patch.object(RendererMutationPlan, "build", side_effect=build_then_stale):
            changed = refresh_sync_cache(mgmt, adapter_device)
            self.assertEqual(mgmt.last_sync_status, "concurrent-1")
            mgmt.refresh_from_db()
            sweep_result = refresh_sync_caches([mgmt], snapshot=snapshot)

        self.assertEqual(changed, [])
        self.assertEqual(sweep_result, (1, 0))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "concurrent-2")


class TestReconcileDeviceLinks(_SyncCacheTestBase):
    """Repair rows whose adapter_device_id no longer resolves to their own adapter device.

    The repair works by re-saving the row, and the adapter push hangs off
    ``transaction.on_commit`` — hence ``captureOnCommitCallbacks`` around each call, which
    stands in for the autocommit that fires the push inline in production.
    """

    def test_relinks_a_row_the_adapter_no_longer_knows(self):
        """A device absent from the adapter is re-onboarded onto a live device row."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-dangling", 196, last_sync_status="succeeded")
        live = self._mgmt("cache-live", 619)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(live)]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 700}),
            # The adapter 404s the dead id — that is what drives the re-onboard. A blanket
            # success here would make the test pass through a path production never takes.
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=_scope_404_for(196)),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        mgmt.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 700)  # re-onboarded
        self.assertEqual(mgmt.adapter_link_error, "")
        self.assertEqual(live.adapter_device_id, 619)  # the healthy row is untouched

    def test_a_stale_attempt_stamp_does_not_abort_the_sweep(self):
        from netbox_nso_plugin.management_lifecycle import save_management
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        first = self._mgmt("cache-stale-first", 196)
        second = self._mgmt("cache-stale-second", 197)
        original_build = RendererMutationPlan.build
        full_save_pks = []
        stamp_plan_staled = False

        def build_then_stale_first_stamp(*args, **kwargs):
            nonlocal stamp_plan_staled
            proposed_save = next(iter(kwargs.get("saves", ())), None)
            if proposed_save is not None and proposed_save.update_fields is None:
                full_save_pks.append(proposed_save.instance.pk)
            plan = original_build(*args, **kwargs)
            if (
                proposed_save is not None
                and proposed_save.update_fields == ("adapter_link_attempted_at",)
                and proposed_save.instance.pk == first.pk
                and not stamp_plan_staled
            ):
                stamp_plan_staled = True
                concurrent = NSODeviceManagement.objects.get(pk=first.pk)
                concurrent.last_sync_status = "concurrent"
                save_management(concurrent, update_fields={"last_sync_status"})
            return plan

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 700}) as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=_scope_404_for(196, 197)) as set_scope,
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            patch.object(RendererMutationPlan, "build", side_effect=build_then_stale_first_stamp),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        # A rejected stamp stops its own row only: nothing was persisted, so the row is not an
        # attempt, keeps its mapping, and reaches neither the full save nor the adapter.
        self.assertEqual((broken, attempted), (2, 1))
        self.assertTrue(stamp_plan_staled)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.adapter_link_attempted_at)
        self.assertIsNotNone(second.adapter_link_attempted_at)
        self.assertEqual((first.adapter_device_id, second.adapter_device_id), (196, 700))
        self.assertNotIn(first.pk, full_save_pks)
        self.assertNotIn(196, [call.args[0] for call in set_scope.call_args_list])
        self.assertEqual(onboard.call_count, 1)

    def test_repairs_an_unmapped_management_row(self):
        """A row with no stored adapter id is an explicit repair class, not invisible."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-unmapped", None)
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 704}) as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        onboard.assert_called_once()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 704)

    def test_unmapped_row_adopts_its_one_matching_adapter_device(self):
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-unmapped-adopt", None)
        _stamp_verified_baselines(mgmt)
        present = _adapter_row(mgmt, id=812)
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[present]),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}) as set_scope,
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        onboard.assert_not_called()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 812)
        self.assertEqual(set_scope.call_args[0][0], 812)
        self.assertFalse(NSOIntentRevision.objects.filter(device=mgmt.device, verified_revision__isnull=False).exists())

    def test_unmapped_row_with_multiple_matching_devices_is_ambiguous(self):
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-unmapped-ambiguous", None)
        twins = [_adapter_row(mgmt, id=813), _adapter_row(mgmt, id=814)]
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=twins),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope") as set_scope,
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        onboard.assert_not_called()
        set_scope.assert_not_called()
        mgmt.refresh_from_db()
        self.assertIsNone(mgmt.adapter_device_id)
        self.assertEqual(mgmt.adapter_link_error, "Adapter identity is ambiguous; repair requires operator action.")

    def test_relink_pushes_scope_against_the_fresh_id(self):
        """The dead id is tried, then the whole link is redone against the new device row.

        Proves the recovery goes through the real link path (onboard → scope → sync-notify),
        not just a mapping rewrite that would leave the adapter without the device's scope.
        """
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        # managed_attributes is a read-only property derived from the manage_* booleans.
        self._mgmt("cache-relink-scope", 196, manage_description=True, manage_enabled=True)
        scope_calls = []

        def fake_set_scope(adapter_device_id, attributes, **kwargs):
            scope_calls.append((adapter_device_id, list(attributes)))
            if adapter_device_id == 196:
                raise AdapterError("Device not found", code="not_found")
            return {}

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 702}),
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=fake_set_scope),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None) as notify,
            self.captureOnCommitCallbacks(execute=True),
        ):
            reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual(scope_calls, [(196, ["description", "enabled"]), (702, ["description", "enabled"])])
        notify.assert_called_once_with(702)  # the fresh device is the one told to sync

    def test_adopts_our_device_found_under_a_different_id(self):
        """Our NSO node present under a new id is adopted directly — no onboard round-trip.

        This is the restored/rebuilt-adapter case: the device is there, its serial changed.
        Re-onboarding would work too, but adopting is one call fewer and cannot 409.
        """
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-moved", 196)
        moved = _adapter_row(mgmt, id=808)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[moved]),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}) as set_scope,
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        onboard.assert_not_called()  # adopted, not re-created
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 808)
        self.assertEqual(set_scope.call_args[0][0], 808)  # scope pushed to the adopted row

    def test_failed_remap_adoption_skips_the_full_save(self):
        """A rejected adoption keeps the old id out of the full save, so no push targets it."""
        from netbox_nso_plugin.management_lifecycle import save_management
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        stale = self._mgmt("cache-moved-stale", 196)
        healthy = self._mgmt("cache-moved-ok", 197)
        moved_stale = _adapter_row(stale, id=806)
        moved_healthy = _adapter_row(healthy, id=807)
        original_build = RendererMutationPlan.build
        full_save_pks = []
        remap_plan_staled = False
        scope_calls = []

        def build_then_stale_first_remap(*args, **kwargs):
            nonlocal remap_plan_staled
            proposed_save = next(iter(kwargs.get("saves", ())), None)
            if proposed_save is not None and proposed_save.update_fields is None:
                full_save_pks.append(proposed_save.instance.pk)
            plan = original_build(*args, **kwargs)
            if (
                proposed_save is not None
                and proposed_save.update_fields == ("adapter_device_id",)
                and proposed_save.instance.pk == stale.pk
                and not remap_plan_staled
            ):
                remap_plan_staled = True
                concurrent = NSODeviceManagement.objects.get(pk=stale.pk)
                concurrent.last_sync_status = "concurrent"
                save_management(concurrent, update_fields={"last_sync_status"})
            return plan

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[moved_stale, moved_healthy]),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch(
                "netbox_nso_plugin.adapter_client.set_scope",
                side_effect=lambda adapter_device_id, *a, **k: scope_calls.append(adapter_device_id),
            ),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            patch.object(RendererMutationPlan, "build", side_effect=build_then_stale_first_remap),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (2, 2))
        self.assertTrue(remap_plan_staled)
        self.assertEqual(full_save_pks, [healthy.pk])  # the rejected row never reached the full save
        onboard.assert_not_called()
        self.assertEqual(scope_calls, [807])  # nothing was pushed against the old id 196
        stale.refresh_from_db()
        healthy.refresh_from_db()
        self.assertEqual(stale.adapter_device_id, 196)  # the original mapping is retained
        self.assertEqual(stale.last_sync_status, "concurrent")
        self.assertEqual(healthy.adapter_device_id, 807)  # the next row still proceeds

    def test_remap_invalidates_every_delivery_baseline(self):
        """A new mapping identity creates audit work instead of only changing the pointer."""
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-remap-baseline", 196)
        _stamp_verified_baselines(mgmt)
        moved = _adapter_row(mgmt, id=809)
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[moved]),
            patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertFalse(
            NSOIntentRevision.objects.filter(
                device=mgmt.device,
                verified_revision__isnull=False,
            ).exists()
        )

    def test_ambiguous_identity_keeps_its_verified_baselines_across_sweeps(self):
        """A mapping only an operator can repair must not blank 18 baselines every sweep.

        Two adapter rows claim this device's logical identity, so the sweep can prove nothing
        and writes nothing. It flags the row and moves on. Invalidating on the way in made
        that no-op branch throw away every verified fingerprint on the device, every five
        minutes, forcing a full 18-scope repair for as long as the ambiguity lasts.
        """
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-ambiguous", 196)
        _stamp_verified_baselines(mgmt)
        twins = [_adapter_row(mgmt, id=810), _adapter_row(mgmt, id=811)]

        for _sweep in range(2):
            with (
                patch("netbox_nso_plugin.adapter_client.list_devices", return_value=twins),
                patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
                patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}) as set_scope,
                patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
                self.captureOnCommitCallbacks(execute=True),
            ):
                broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

            self.assertEqual((broken, attempted), (1, 1))
            onboard.assert_not_called()
            set_scope.assert_not_called()

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 196)  # nothing was remapped
        self.assertEqual(mgmt.adapter_link_error, "Adapter identity is ambiguous; repair requires operator action.")
        self.assertEqual(
            NSOIntentRevision.objects.filter(device=mgmt.device, verified_revision=4).count(),
            len(delivery.delivery_keys()),
        )

    def test_identity_change_routes_through_the_fenced_rekey(self):
        """The same adapter mapping under an old source tuple is patched, not re-onboarded."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-identity-changed", 196)
        adapter_row = _adapter_row(mgmt, nso_device_name="old-device-name")
        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[adapter_row]),
            patch(
                "netbox_nso_plugin.adapter_client.patch_device",
                return_value={"source_epoch": 9},
            ) as rekey,
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        rekey.assert_called_once_with(
            adapter_device_id=196,
            nso_instance=mgmt.nso_instance.adapter_instance_id,
            nso_device_name=mgmt.nso_device_name,
        )
        onboard.assert_not_called()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 196)
        self.assertFalse(mgmt.source_rekey_pending)

    def test_drops_a_reused_id_before_pushing_anything(self):
        """An id owned by another device is dropped FIRST, so no scope reaches that device.

        The dangerous shape: PUT scope against a reused id SUCCEEDS (the row exists), which
        would push this device's managed attributes, failover IPs and auto-apply onto someone
        else's device. The pointer must be cleared before any push can be attempted.
        """
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-reused", 619)
        stranger = _adapter_row(mgmt, nso_device_name="somebody-else", netbox_device_id=mgmt.device_id + 999)
        scope_calls = []

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[stranger]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 900}),
            patch(
                "netbox_nso_plugin.adapter_client.set_scope",
                side_effect=lambda adapter_device_id, *a, **k: scope_calls.append(adapter_device_id),
            ),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        self.assertNotIn(619, scope_calls)  # the stranger was never written to
        self.assertEqual(scope_calls, [900])
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 900)

    def test_failed_reused_id_clear_skips_the_full_save(self):
        from netbox_nso_plugin.management_lifecycle import save_management
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-reused-stale", 619)
        stranger = _adapter_row(mgmt, nso_device_name="somebody-else", netbox_device_id=mgmt.device_id + 999)
        original_build = RendererMutationPlan.build
        full_save_ids = []
        pointer_plan_staled = False

        def build_then_stale_pointer_clear(*args, **kwargs):
            nonlocal pointer_plan_staled
            proposed_save = next(iter(kwargs.get("saves", ())), None)
            if proposed_save is not None and proposed_save.update_fields is None:
                full_save_ids.append(proposed_save.instance.adapter_device_id)
            plan = original_build(*args, **kwargs)
            if proposed_save is not None and proposed_save.update_fields == ("adapter_device_id",):
                pointer_plan_staled = True
                concurrent = NSODeviceManagement.objects.get(pk=mgmt.pk)
                concurrent.last_sync_status = "concurrent"
                save_management(concurrent, update_fields={"last_sync_status"})
            return plan

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[stranger]),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope") as set_scope,
            patch("netbox_nso_plugin.adapter_client.sync_notify") as sync_notify,
            patch.object(RendererMutationPlan, "build", side_effect=build_then_stale_pointer_clear),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        self.assertTrue(pointer_plan_staled)
        self.assertEqual(full_save_ids, [])
        onboard.assert_not_called()
        set_scope.assert_not_called()
        sync_notify.assert_not_called()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 619)
        self.assertEqual(mgmt.last_sync_status, "concurrent")

    def test_drops_an_unlinked_id_owned_by_another_logical_device(self):
        """An unlinked foreign row is reuse, not evidence of an in-place source rekey."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-reused-unlinked", 620)
        stranger = _adapter_row(mgmt, nso_device_name="somebody-else", netbox_device_id=None)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[stranger]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 901}) as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope", return_value={}),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        onboard.assert_called_once()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 901)
        self.assertFalse(mgmt.source_rekey_pending)

    def test_leaves_healthy_rows_alone(self):
        """Every mapping resolving to its own device → nothing to reconcile, no onboard call."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-ok", 619)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(mgmt)]),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        onboard.assert_not_called()
        self.assertEqual((broken, attempted), (0, 0))

    def test_repairs_are_capped_and_the_remainder_is_flagged(self):
        """Over the per-run cap, the rest keep an operator-visible error — never silent.

        A fleet-wide loss heals over several ticks instead of one onboard+sync burst, and —
        unlike a "suppress when most look broken" rule — it can never wedge into repairing
        nothing forever while telling nobody.
        """
        from netbox_nso_plugin.sync_cache import MAX_RELINKS_PER_RUN, reconcile_device_links

        rows = [self._mgmt(f"cache-cap-{i}", 800 + i) for i in range(MAX_RELINKS_PER_RUN + 3)]

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 950}),
            patch(
                "netbox_nso_plugin.adapter_client.set_scope",
                side_effect=_scope_404_for(*(800 + i for i in range(MAX_RELINKS_PER_RUN + 3))),
            ),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual(broken, MAX_RELINKS_PER_RUN + 3)
        self.assertEqual(attempted, MAX_RELINKS_PER_RUN)
        deferred = [m for m in rows if NSODeviceManagement.objects.get(pk=m.pk).adapter_link_error]
        self.assertEqual(len(deferred), 3)  # the remainder is surfaced, not silently dropped

    def test_an_empty_adapter_still_heals(self):
        """A wiped adapter table must heal progressively, not suppress itself forever.

        onboard_device adopts by (nso_instance, nso_device_name), so recreating genuinely lost
        rows cannot duplicate; refusing to act here just strands the fleet with nobody told.
        """
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-wiped", 196)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 960}),
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=_scope_404_for(196)),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 960)

    def test_never_drops_the_pointer_of_a_row_mid_rekey(self):
        """A rekey in flight must not be 'repaired' — doing so strands it permanently.

        During a rekey NetBox holds the NEW nso_device_name while the adapter still holds the
        OLD one, so the row reads as 'reused'. Clearing its pointer removes it from every
        future sweep (_snapshot only considers rows that HAVE an id), and re-onboarding the new
        identity collides with the old adapter row still owning this netbox_device_id.
        """
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-rekey-inflight", 196)
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(source_rekey_pending=True)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        onboard.assert_not_called()
        self.assertEqual((broken, attempted), (0, 0))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 196)  # still sweepable next tick

    def test_skips_rows_still_provisioning(self):
        """A row mid-onboard has no adapter device yet by design — not a broken mapping."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        self._mgmt("cache-provisioning", 196, onboard_status="provisioning", onboard_job_id="J1")
        known = self._mgmt("cache-known", 619)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(known)]),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        onboard.assert_not_called()
        self.assertEqual((broken, attempted), (0, 0))

    def test_a_failed_relink_is_surfaced_not_swallowed(self):
        """If the re-onboard fails, the row keeps an operator-visible adapter_link_error."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-relink-fail", 196)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[]),
            patch(
                "netbox_nso_plugin.adapter_client.onboard_device",
                side_effect=AdapterError("adapter down", code="nso_unreachable"),
            ),
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=_scope_404_for(196)),
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        self.assertEqual((broken, attempted), (1, 1))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 196)  # still broken — nothing invented
        self.assertEqual(mgmt.adapter_link_error, "The NSO adapter request failed. See the server log.")

    def test_adapter_outage_reconciles_nothing(self):
        """An unreachable adapter proves nothing about any mapping — do not touch the rows."""
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-recon-down", 196)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", side_effect=AdapterError("down")),
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all())

        onboard.assert_not_called()
        self.assertEqual((broken, attempted), (0, 0))
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 196)


class TestReconcileDeviceLinksConcurrency(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction, update_mirror_fields

        from .test_onboarding import _device

        self.instance = NSOInstance.objects.create(name="cache-race", adapter_instance_id="cache-race-nso")
        management = NSODeviceManagement(
            device=_device("cache-rekey-race"),
            nso_instance=self.instance,
            nso_device_name="cache-rekey-race",
            adapter_device_id=196,
            onboard_status="provisioning",
        )
        with intent_transaction(footprint_for_instance(management)):
            management.save(force_insert=True)
        update_mirror_fields(management, onboard_status="")
        self.mgmt = management

    def test_a_rekey_started_before_repair_does_not_advance_revision(self):
        """A repair skipped after acquisition leaves the intent revision unchanged."""
        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.models import NSOIntentRevision
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        revision = NSOIntentRevision.objects.get(device=self.mgmt.device, scope="static_route")
        before = revision.revision
        candidate_loaded = threading.Barrier(2, timeout=30)
        row_changed = threading.Barrier(2, timeout=30)
        errors: list[BaseException] = []
        from dcim.models import Interface

        candidate_query_seen = False

        def wait_after_candidate_query(execute, sql, params, many, context):
            nonlocal candidate_query_seen
            result = execute(sql, params, many, context)
            if not candidate_query_seen and Interface._meta.db_table in sql:
                candidate_query_seen = True
                candidate_loaded.wait()
                row_changed.wait()
            return result

        def start_rekey():
            try:
                candidate_loaded.wait()
                current = NSODeviceManagement.objects.get(pk=self.mgmt.pk)
                intent_state.update_mirror_fields(current, source_rekey_pending=True)
                row_changed.wait()
            except BaseException as exc:  # noqa: BLE001, the main thread reports worker failures
                errors.append(exc)
                row_changed.abort()
            finally:
                connection.close()

        worker = threading.Thread(target=start_rekey)
        worker.start()
        snapshot = ([self.mgmt], {}, {})
        with self.assertNoLogs("netbox_nso_plugin.sync_cache", level="ERROR"):
            with connection.execute_wrapper(wait_after_candidate_query):
                result = reconcile_device_links(NSODeviceManagement.objects.all(), snapshot=snapshot)
        worker.join(timeout=60)

        assert not worker.is_alive(), "concurrent rekey writer did not finish"
        assert errors == []
        assert candidate_query_seen
        assert result == (1, 0)
        self.mgmt.refresh_from_db()
        revision.refresh_from_db()
        assert self.mgmt.source_rekey_pending is True
        assert self.mgmt.adapter_device_id == 196
        assert revision.revision == before, "a skipped link repair committed an intent revision"


class TestRefreshSyncCacheJob(_SyncCacheTestBase):
    """The periodic system job — the refresh that needs nobody looking at a page."""

    def test_registered_as_a_system_job(self):
        """The job is registered so NetBox schedules it without operator action."""
        from netbox.registry import registry

        from netbox_nso_plugin.jobs import SYNC_CACHE_REFRESH_MINUTES, RefreshDeviceSyncCacheJob

        self.assertIn(RefreshDeviceSyncCacheJob, registry["system_jobs"])
        self.assertEqual(registry["system_jobs"][RefreshDeviceSyncCacheJob]["interval"], 5)
        self.assertEqual(SYNC_CACHE_REFRESH_MINUTES, 5)

    def _job(self):
        from uuid import uuid4

        from core.models import Job

        from netbox_nso_plugin.jobs import RefreshDeviceSyncCacheJob

        return RefreshDeviceSyncCacheJob(Job.objects.create(name=RefreshDeviceSyncCacheJob.name, job_id=uuid4()))

    def test_job_run_refreshes_every_managed_row(self):
        """Running the real JobRunner fills the mirror with no page open anywhere."""
        mgmt = self._mgmt("cache-job-rtr", 619)

        with patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(mgmt)]):
            self._job().run()

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_at, _SYNCED_AT)
        self.assertEqual(mgmt.last_sync_status, "succeeded")

    def test_the_summary_reports_how_long_the_settlement_sweep_took(self):
        """The sweep is the one pass whose cost grows with the fleet, so the tick times it."""
        mgmt = self._mgmt("cache-job-timed", 621)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(mgmt)]),
            self.assertLogs("netbox_nso_plugin.jobs", level="INFO") as logs,
        ):
            self._job().run()

        summary = [line for line in logs.output if "settlement polled" in line]
        self.assertEqual(len(summary), 1, logs.output)
        self.assertRegex(summary[0], r"settlement sweep \d+\.\d+s")

    def test_job_run_repairs_a_broken_mapping(self):
        """The same periodic job that refreshes the mirror also heals a broken mapping."""
        mgmt = self._mgmt("cache-job-dangling", 196)
        known = self._mgmt("cache-job-known", 619)

        with (
            patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[_adapter_row(known)]) as bulk,
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 701}),
            patch("netbox_nso_plugin.adapter_client.set_scope", side_effect=_scope_404_for(196)),
            patch("netbox_nso_plugin.adapter_client.sync_notify", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._job().run()

        # Both passes share ONE snapshot — a second call could disagree with the first.
        self.assertEqual(bulk.call_count, 1)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 701)


class TestDashboardRefreshesOnRender(ViewTestBase):
    """The onboarding dashboard shows current values, not whatever was last cached."""

    def _mgmt_row(self, **fields):
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(**fields)
        self.mgmt.refresh_from_db()
        return self.mgmt

    def _dashboard(self):
        return self.client.get(reverse("plugins:netbox_nso_plugin:onboarding_dashboard"))

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_render_refreshes_a_never_visited_row(self, mock_session_cls, _cfg, _list, _neds):
        """GET on the dashboard mirrors the adapter's state onto a row nobody has opened.

        Drives the whole path — view → adapter_client → real requests plumbing → model —
        with only the network send faked, so the rendered page and the DB row are both real.
        """
        mgmt = self._mgmt_row(adapter_device_id=619)
        self.assertIsNone(mgmt.last_sync_at)

        mock_session_cls.return_value = make_session(json_data=[_adapter_row(mgmt)])

        response = self._dashboard()

        self.assertEqual(response.status_code, 200)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_at, _SYNCED_AT)
        self.assertEqual(mgmt.last_sync_status, "succeeded")
        self.assertContains(response, "succeeded")

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_devices", side_effect=RuntimeError("boom"))
    def test_render_survives_a_broken_refresh(self, _bulk, _list, _neds):
        """A refresh that blows up must never 500 the dashboard — it is a display nicety."""
        mgmt = self._mgmt_row(adapter_device_id=619, last_sync_status="succeeded")

        response = self._dashboard()

        self.assertEqual(response.status_code, 200)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_sync_status, "succeeded")

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_render_replaces_a_stale_succeeded_badge_with_partial(self, mock_session_cls, _cfg, _list, _neds):
        """The dashboard stops showing a days-old 'succeeded' for a now-degraded device."""
        mgmt = self._mgmt_row(adapter_device_id=619, last_sync_status="succeeded")
        payload = _adapter_row(mgmt, last_sync_status="partial", degraded_surfaces=["bgp", "ospf"])
        mock_session_cls.return_value = make_session(json_data=[payload])

        response = self._dashboard()

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("partial", html)
        self.assertIn("bgp, ospf", html)

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_devices")
    def test_render_skips_the_adapter_when_nothing_is_mapped(self, bulk, _list, _neds):
        """No mapped rows → no bulk call (the fixture row has no adapter_device_id)."""
        response = self._dashboard()

        self.assertEqual(response.status_code, 200)
        bulk.assert_not_called()

    @patch("netbox_nso_plugin.adapter_client.get_neds", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=[])
    @patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[])
    def test_dashboard_shows_an_adapter_link_error(self, _bulk, _list, _neds):
        """A broken link is visible on the dashboard, not only on the device tab.

        Without this the row keeps rendering its last good 'succeeded' — the exact stale-green
        lie this whole change exists to remove — while the only warning sits one page away.
        """
        self._mgmt_row(adapter_device_id=None, last_sync_status="succeeded", adapter_link_error="Device not found")

        response = self._dashboard()

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Device not found", html)
        self.assertNotIn('<span class="badge text-bg-success">succeeded</span>', html)


class TestManagedListRefreshesOnRender(ViewTestBase):
    """The managed-devices list keeps refreshing — now through the same bulk sweep."""

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_list_render_refreshes_rows(self, mock_session_cls, _cfg):
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(adapter_device_id=619)
        self.mgmt.refresh_from_db()

        mock_session_cls.return_value = make_session(json_data=[_adapter_row(self.mgmt)])

        response = self.client.get(reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list"))

        self.assertEqual(response.status_code, 200)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.last_sync_at, _SYNCED_AT)
        self.assertEqual(self.mgmt.last_sync_status, "succeeded")

    @patch("netbox_nso_plugin.adapter_client.list_devices", return_value=[])
    def test_list_shows_an_adapter_link_error(self, _bulk):
        """The managed list surfaces a broken link instead of a stale green badge."""
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(
            adapter_device_id=None, last_sync_status="succeeded", adapter_link_error="Device not found"
        )

        response = self.client.get(reverse("plugins:netbox_nso_plugin:nsodevicemanagement_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Device not found")


class TestDeviceTabStillRefreshes(ViewTestBase):
    """The device NSO tab keeps its own per-device refresh (it already has the payload)."""

    @patch("netbox_nso_plugin.adapter_client.get_device")
    def test_tab_render_refreshes_the_row(self, get_device):
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(adapter_device_id=619)
        self.mgmt.refresh_from_db()
        get_device.return_value = _adapter_row(self.mgmt)

        device = Device.objects.get(pk=self.device.pk)
        response = self.client.get(f"/dcim/devices/{device.pk}/nso/")

        self.assertEqual(response.status_code, 200)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.last_sync_at, _SYNCED_AT)
        self.assertEqual(self.mgmt.last_sync_status, "succeeded")
