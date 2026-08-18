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

from .mixins import IntentPushResetMixin, _CascadeFlushMixin

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

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOStaticRouteState
        from netbox_nso_plugin.signals import suppress_intent_push

        mfg = Manufacturer.objects.create(name="ClobMfg", slug="clobmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="ClobDev", slug="clobdev")
        role = DeviceRole.objects.create(name="ClobRole", slug="clobrole")
        site = Site.objects.create(name="ClobSite", slug="clobsite")
        self.device = Device.objects.create(name="clob-rtr", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="clob-inst", adapter_instance_id="clob-inst")
        with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"):
            self.mgmt = NSODeviceManagement.objects.create(
                device=self.device,
                nso_instance=inst,
                nso_device_name="nso-clob",
                adapter_device_id=77,
            )
        with transaction.atomic():
            self.route = StaticRoute.objects.create(prefix=PREFIX, next_hop=NEXT_HOP, metric=1)
        with suppress_intent_push():
            self.route.devices.add(self.device)
        with patch(PUT), transaction.atomic():
            self.state = NSOStaticRouteState.objects.create(
                management=self.mgmt,
                static_route=self.route,
                status="deploying",
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
        armed = self._backfill()
        release.set()
        thread.join(timeout=30)
        assert not thread.is_alive(), "the reconciler never returned, so its writes are still in flight"
        assert not failure, failure
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
        """And the transition S5 replaced, which computed ``in_sync`` — a green badge with no apply."""
        self._run_barrier(settles_deploying=True)

        assert self.computed == ["in_sync"], self.computed
        assert self.state.status == "accepted", "the stale reconcile wrote a verdict over the backfill's"


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
