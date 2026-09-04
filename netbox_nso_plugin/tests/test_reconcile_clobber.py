# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S6) — a reconcile that read before the writer may not restore what it read.

Pins S6.1b and S6.1c. The static-route reconciler reads its overlay **unlocked**, then makes
its decision, then saves. Everything a writer commits in that gap used to be written straight
back: the generation, the generation clock and both settlement expectations, on every
reconcile for the whole life of P5 — not only during the rollout backfill. And the status
could not be fixed by naming fields, because the reconciler legitimately owns it: a stale
instance holding ``deploying`` computes a verdict of its own and writes it over the one the
backfill just committed.

Both halves are driven through a **barrier**: the real reconciler is paused between its read
and its save while the real fleet backfill arms, demotes and commits.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import connections, transaction
from django.test import SimpleTestCase, TransactionTestCase

from ._outbox_case import content_bulk_update, mirror_update, wait_until_postgres_blocks, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_sync_cache import _SyncCacheTestBase

PREFIX = "10.90.0.0/16"
NEXT_HOP = "10.90.0.1"
PUT = "netbox_nso_plugin.adapter_client.put_static_route_intent"
STALE_ADVISORY = "an advisory about the generation the backfill supersedes"


class _ClobberBarrierCase(IntentPushResetMixin, _CascadeFlushMixin, TransactionTestCase):
    """One pre-P2 owned overlay, mid-apply: the sentinel generation and no clock (S26)."""

    serialized_rollback = False

    def setUp(self):
        super().setUp()
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOApplyAttempt, NSODeviceManagement, NSOInstance, NSOStaticRouteState

        from ._static_route_case import _assign_without_push

        mfg = Manufacturer.objects.create(name="ClobMfg", slug="clobmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="ClobDev", slug="clobdev")
        role = DeviceRole.objects.create(name="ClobRole", slug="clobrole")
        site = Site.objects.create(name="ClobSite", slug="clobsite")
        self.device = Device.objects.create(name="clob-rtr", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="clob-inst", adapter_instance_id="clob-inst")
        management = NSODeviceManagement(
            device=self.device,
            nso_instance=inst,
            nso_device_name="nso-clob",
            adapter_device_id=77,
        )
        with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"):
            with intent_transaction(footprint_for_instance(management)):
                management.save(force_insert=True)
        self.mgmt = management
        with transaction.atomic():
            self.route = StaticRoute.objects.create(prefix=PREFIX, next_hop=NEXT_HOP, metric=1)
        _assign_without_push(self.route, self.device)
        attempt = NSOApplyAttempt.objects.create(management=self.mgmt)
        with patch(PUT), without_commit_drain(), transaction.atomic():
            self.state = NSOStaticRouteState.objects.create(
                management=self.mgmt,
                static_route=self.route,
                status="deploying",
                apply_attempt_id=attempt.pk,
                nso_prefix=PREFIX,
                nso_next_hop=NEXT_HOP,
                expected_generation=None,
                expected_fingerprint="fp-stale",
                last_result_advisory=STALE_ADVISORY,
            )
        self.computed: list[str] = []

    def _payload(self) -> dict:
        """What the adapter reports for this device — the route, unchanged."""
        return {"routes": [{"vrf": "", "prefix": PREFIX, "next_hop": NEXT_HOP, "metric": 1, "tag": None}]}

    def _start_paused_reconcile(self, *, settles_deploying: bool):
        """Run the real reconciler in a thread, paused between its read and its save.

        ``on_reconcile`` is that seam: the reconciler calls it with the status it read and
        saves immediately after. *settles_deploying* selects the transition — ``False`` is
        the tree after S5's handover, ``True`` is the pre-S5 one, and the point of driving
        both is that the two compute **different** verdicts (``deploying`` and ``in_sync``)
        and the backfill has to survive either.
        """
        from netbox_nso_plugin import status_machine as sm
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.template_content import _reconcile_static_routes

        real = sm.on_reconcile
        read_done = threading.Event()
        release = threading.Event()
        failure: list[BaseException] = []

        def _paused(current, **kwargs):
            if not kwargs.get("present", True):
                return real(current, **kwargs)
            kwargs["settles_deploying"] = settles_deploying
            read_done.set()
            assert release.wait(timeout=30), "the barrier never released the reconciler"
            decided = real(current, **kwargs)
            self.computed.append(decided)
            return decided

        def _reconcile():
            try:
                with suppress_intent_push(), patch("netbox_nso_plugin.status_machine.on_reconcile", _paused):
                    _reconcile_static_routes(self.device, self._payload())
            except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
                failure.append(exc)
            finally:
                read_done.set()
                connections.close_all()

        thread = threading.Thread(target=_reconcile)
        thread.start()
        assert read_done.wait(timeout=30), "the reconciler never reached its status decision"
        self.addCleanup(thread.join, 30)
        return thread, release, failure

    def _backfill(self) -> int:
        """Run the real fleet pass, and return the generation it armed this row with."""
        from netbox_nso_plugin.intent_drift import resync_static_route_intent_fleet

        def _ack(adapter_device_id, routes):
            return {
                "device_id": adapter_device_id,
                "count": len(routes),
                "routes": [
                    {"route_id": r["route_id"], "generation": r["generation"], "fingerprint": "fp-new"} for r in routes
                ],
            }

        with patch(PUT, side_effect=_ack):
            results = resync_static_route_intent_fleet(device_ids=[self.device.pk])
        assert [row["armed"] for row in results] == [1], results
        self.state.refresh_from_db()
        assert self.state.intent_generation > 0
        return self.state.intent_generation

    def _run_barrier(self, *, settles_deploying: bool) -> int:
        thread, release, failure = self._start_paused_reconcile(settles_deploying=settles_deploying)
        backfill_started = threading.Event()
        backfill_pid: list[int] = []
        backfill_result: list[int] = []
        backfill_failure: list[BaseException] = []

        def _run_backfill():
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backfill_pid.append(cursor.fetchone()[0])
            backfill_started.set()
            try:
                backfill_result.append(self._backfill())
            except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
                backfill_failure.append(exc)
            finally:
                connections.close_all()

        backfill = threading.Thread(target=_run_backfill)
        backfill.start()
        self.addCleanup(backfill.join, 30)
        self.addCleanup(release.set)
        assert backfill_started.wait(timeout=30)
        blocked_failure = None
        try:
            wait_until_postgres_blocks(backfill_pid[0], "the backfill")
        except BaseException as exc:  # noqa: BLE001 (re-raised after both threads finish)
            blocked_failure = exc
        finally:
            release.set()
        thread.join(timeout=30)
        backfill.join(timeout=30)
        assert not thread.is_alive(), "the reconciler never returned, so its writes are still in flight"
        assert not backfill.is_alive(), "the backfill did not resume after the reconcile committed"
        if blocked_failure is not None:
            raise blocked_failure
        assert not failure, failure
        assert not backfill_failure, backfill_failure
        armed = backfill_result[0]
        self.state.refresh_from_db()
        assert self.state.last_sync_at is not None, (
            "the reconciler never wrote its mirror, so this run proves nothing about what it may write"
        )
        return armed


class TestTheReconcileCannotRestoreGenerationState(_ClobberBarrierCase):
    """S6.1b — the generation, its clock and both expectations survive a stale reconcile."""

    def test_a_stale_reconcile_cannot_restore_generation_state(self):
        armed = self._run_barrier(settles_deploying=False)

        assert self.state.intent_generation == armed, (
            "the reconcile restored the generation sentinel: the adapter now holds a generation "
            "against an overlay that can never correlate with it"
        )
        assert self.state.generation_started_at is not None, "the reconcile restored the NULL generation clock"
        assert self.state.expected_generation == armed, "the reconcile restored a stale settlement expectation"
        assert self.state.expected_fingerprint == "fp-new"
        assert self.state.last_result_advisory == "", "the reconcile restored an advisory about a dead generation"


class TestTheReconcileCannotOverwriteTheBackfilledStatus(_ClobberBarrierCase):
    """S6.1c — and the status too, which no allow-list can protect (S24d)."""

    def test_a_stale_reconcile_cannot_overwrite_the_backfilled_status_post_s5(self):
        """The tree as it stands: the stale instance recomputes ``deploying``."""
        self._run_barrier(settles_deploying=False)

        assert self.computed == ["deploying"], self.computed
        assert self.state.status == "accepted", (
            "the stale reconcile restored 'deploying': the row now waits on an apply result "
            "for a generation nothing is carrying"
        )

    def test_a_stale_reconcile_cannot_overwrite_the_backfilled_status_pre_s5(self):
        """The retired transition is serialized before backfill, so it cannot clobber later state."""
        self._run_barrier(settles_deploying=True)

        assert self.computed == ["in_sync"], self.computed
        assert self.state.status == "in_sync"


class TestTheMirrorAllowList(SimpleTestCase):
    """S6.1b's second arm — asserted explicitly, as P0b.2 does for the incarnation markers."""

    def test_the_reconciler_update_fields_allow_list(self):
        from netbox_nso_plugin.signals import _STATIC_ROUTE_ARMED_FIELDS
        from netbox_nso_plugin.template_content import _STATIC_ROUTE_MIRROR_FIELDS

        assert set(_STATIC_ROUTE_MIRROR_FIELDS) == {"nso_vrf", "nso_prefix", "nso_next_hop", "last_sync_at"}
        # Every field the settlement path owns is absent BY DESIGN, not by luck.
        assert set(_STATIC_ROUTE_MIRROR_FIELDS).isdisjoint(
            set(_STATIC_ROUTE_ARMED_FIELDS)
            | {"status", "expected_generation", "expected_fingerprint", "accepted_at", "last_apply_at"}
        )


class TestLinkReconcileCannotRestoreSourceState(_SyncCacheTestBase):
    """A link sweep that read before a rekey cannot restore the stale source."""

    def test_a_stale_snapshot_cannot_overwrite_a_rekey(self):
        from netbox_nso_plugin.intent_state import footprint_for_instance
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentRevision
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-stale-source", 196)
        snapshot = ([mgmt], {}, {})
        content_bulk_update(
            NSODeviceManagement.objects.get(pk=mgmt.pk),
            nso_device_name="cache-rekeyed",
            source_rekey_pending=True,
        )
        revision_keys = footprint_for_instance(mgmt).revision_keys
        revisions_before = {
            key: NSOIntentRevision.objects.get(device_id=key[0], scope=key[1]).revision for key in revision_keys
        }

        with (
            patch("netbox_nso_plugin.adapter_client.onboard_device") as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope") as set_scope,
            patch("netbox_nso_plugin.adapter_client.sync_notify") as notify,
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all(), snapshot=snapshot)

        self.assertEqual((broken, attempted), (1, 0))
        onboard.assert_not_called()
        set_scope.assert_not_called()
        notify.assert_not_called()
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.nso_device_name, "cache-rekeyed")
        self.assertTrue(mgmt.source_rekey_pending)
        self.assertEqual(mgmt.adapter_device_id, 196)
        revisions_after = {
            key: NSOIntentRevision.objects.get(device_id=key[0], scope=key[1]).revision for key in revision_keys
        }
        self.assertEqual(revisions_after, revisions_before)

    def test_a_stale_snapshot_cannot_overwrite_a_remapped_adapter_id(self):
        from netbox_nso_plugin import intent_state
        from netbox_nso_plugin.models import NSODeviceManagement, NSOIntentRevision
        from netbox_nso_plugin.sync_cache import reconcile_device_links

        mgmt = self._mgmt("cache-stale-adapter-id", 196)
        snapshot = (
            [mgmt],
            {
                196: {
                    "id": 196,
                    "nso_instance": "other-instance",
                    "nso_device_name": "other-device",
                    "netbox_device_id": None,
                }
            },
            {},
        )
        revision_keys = intent_state.footprint_for_instance(mgmt).revision_keys
        revisions_before = {
            key: NSOIntentRevision.objects.get(device_id=key[0], scope=key[1]).revision for key in revision_keys
        }
        real_footprint_for_instance = intent_state.footprint_for_instance

        def remap_before_acquisition(instance):
            mirror_update(NSODeviceManagement.objects.get(pk=mgmt.pk), adapter_device_id=197)
            return real_footprint_for_instance(instance)

        with (
            patch("netbox_nso_plugin.intent_state.footprint_for_instance", side_effect=remap_before_acquisition),
            patch("netbox_nso_plugin.adapter_client.onboard_device", return_value={"id": 198}) as onboard,
            patch("netbox_nso_plugin.adapter_client.set_scope") as set_scope,
            patch("netbox_nso_plugin.adapter_client.sync_notify") as notify,
            self.captureOnCommitCallbacks(execute=True),
        ):
            broken, attempted = reconcile_device_links(NSODeviceManagement.objects.all(), snapshot=snapshot)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.adapter_device_id, 197)
        self.assertEqual((broken, attempted), (1, 0))
        onboard.assert_not_called()
        set_scope.assert_not_called()
        notify.assert_not_called()
        revisions_after = {
            key: NSOIntentRevision.objects.get(device_id=key[0], scope=key[1]).revision for key in revision_keys
        }
        self.assertEqual(revisions_after, revisions_before)
