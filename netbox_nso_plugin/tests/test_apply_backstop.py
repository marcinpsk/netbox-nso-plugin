# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S6) — what may become ``deploying``, and what may not stay there.

Pins S6.3, S6.4 and S6.5 (R3 P5.13). A static-route row is settled by a generation-correlated
result and by nothing else, which puts two obligations on the ``deploying`` state itself:

* a row may only enter it when the adapter is actually holding the intent it will settle
  against — an Apply whose forced push was refused would otherwise mint a row no result can
  ever name (S6.3);
* a row may not stay in it forever. The backstop escalates on the generation clock (S6.4),
  and a row with **no** clock — the state an upgrade leaves behind, and an impossible one
  after the rollout backfill — escalates with its own reason instead of being skipped by a
  NULL-false comparison (S6.5).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from ._settlement_case import _make_device, _make_mgmt, _own, _route, _SettlementCase

_OTHER_PUSHES = (
    "_push_interface_intent_for_device",
    "_push_lacp_intent_for_device",
    "_push_logging_intent_for_device",
    "_push_route_policy_intent_for_device",
    "_push_svi_intent_for_device",
    "_push_subinterface_intent_for_device",
    "_push_bfd_intent_for_device",
    "_push_interface_mtu_intent_for_device",
    "_push_l2_sap_intent_for_device",
    "_push_switchport_intent_for_device",
    "_push_vlan_intent_for_device",
    "_push_snmp_intent_for_device",
)


def _patch_other_pushes():
    """Silence every scope but static routes: they are a different subsystem here."""
    return [patch(f"netbox_nso_plugin.signals.{name}") for name in _OTHER_PUSHES]


def _age_clock(state, minutes=90):
    """Age the generation clock past the stuck-deploying grace (default 10 minutes)."""
    from netbox_nso_plugin.models import NSOStaticRouteState

    NSOStaticRouteState.objects.filter(pk=state.pk).update(
        generation_started_at=timezone.now() - timedelta(minutes=minutes)
    )


class TestApplyPromotion(TestCase):
    """S6.3 — the Apply's forced static-route push is a precondition of promoting its rows."""

    def _setup(self):
        from netbox_routing.models import StaticRoute

        from netbox_nso_plugin.models import (
            NSODeviceManagement,
            NSOInstance,
            NSOLoggingLevelState,
            NSOStaticRouteState,
        )

        device = _make_device("promote")
        inst, _ = NSOInstance.objects.get_or_create(
            name="promote-inst", defaults={"adapter_instance_id": "promote-inst"}
        )
        with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"):
            mgmt = NSODeviceManagement.objects.create(
                device=device, nso_instance=inst, nso_device_name="nso-promote", adapter_device_id=95
            )
        route = StaticRoute.objects.create(prefix="198.19.7.0/24", next_hop="198.19.0.1", metric=1)
        with patch("netbox_nso_plugin.adapter_client.put_static_route_intent"):
            state = NSOStaticRouteState.objects.create(
                management=mgmt,
                static_route=route,
                nso_prefix="198.19.7.0/24",
                nso_next_hop="198.19.0.1",
                status="accepted",
                intent_generation=301,
                generation_started_at=timezone.now(),
            )
        other = NSOLoggingLevelState.objects.create(management=mgmt, console_severity="warning", status="accepted")
        return mgmt, state, other

    def _prepare(self, static_response):
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, state, other = self._setup()
        patches = _patch_other_pushes()
        patches.append(
            patch(
                "netbox_nso_plugin.signals._push_static_route_intent_for_device",
                return_value=static_response,
            )
        )
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in patches])
        _prepare_apply(mgmt)
        state.refresh_from_db()
        other.refresh_from_db()
        return state, other, started[-1]

    def test_a_failed_force_push_skips_promotion(self):
        """A forced push returns ``None`` only on a real rejection — the adapter stored nothing."""
        state, other, push = self._prepare(static_response=None)

        push.assert_called_once()
        assert state.status == "accepted", (
            "the Apply promoted a route whose intent the adapter refused: nothing can ever "
            "settle that row, so it waits for the backstop to call it failed"
        )
        assert other.status == "deploying", "one scope's rejection blocked every other scope's Apply"

    def test_an_acknowledged_push_still_promotes(self):
        """The guard is a precondition, not a new refusal: the normal path is unchanged."""
        state, other, _push = self._prepare(static_response={"device_id": 95, "count": 1, "routes": []})

        assert state.status == "deploying"
        assert other.status == "deploying"


class TestTheStuckDeployingBackstop(_SettlementCase):
    """S6.4/S6.5 — the two ways a static-route row can be stuck, and their two reasons."""

    def _device(self, tag: str, adapter_device_id: int):
        device = _make_device(tag)
        mgmt = _make_mgmt(device, tag, adapter_device_id)
        self.adapter.store.add_device(
            nso_instance=f"se-{tag}-inst",
            nso_device_name=f"nso-se-{tag}",
            netbox_device_id=device.pk,
            device_id=adapter_device_id,
        )
        return device, mgmt

    def test_a_stuck_deploying_row_escalates_on_its_generation_clock(self):
        """S6.4 — promoted the normal way, then no result ever names its generation."""
        from netbox_nso_plugin.views import _prepare_apply

        device, mgmt = self._device("stuckclock", 60)
        sr = _route("10.50.0.0/16", "10.50.0.1", devices=[device])
        state = _own(sr, mgmt, generation=401, status="accepted")
        _age_clock(state)  # armed long before this Apply, which is what dates the wait

        patches = _patch_other_pushes()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        _prepare_apply(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "the setup never reached the state the backstop judges"

        # The adapter reports no result at all: the feed is empty, so the walk drains.
        self._tick()

        state.refresh_from_db()
        assert state.status == "apply_failed", "the row is stranded 'applying' forever"
        assert "401" in state.last_apply_error, state.last_apply_error
        assert self.adapter.store.feed_requests, "the backstop judged without walking the feed"

    def test_a_null_generation_clock_escalates_explicitly(self):
        """S6.5 — the row an upgrade left behind: ``deploying``, sentinel generation, no clock."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        device, mgmt = self._device("noclock", 61)
        sr = _route("10.51.0.0/16", "10.51.0.1", devices=[device])
        state = _own(sr, mgmt, generation=0, expected=False)
        NSOStaticRouteState.objects.filter(pk=state.pk).update(generation_started_at=None)

        self._tick()

        state.refresh_from_db()
        assert state.status == "apply_failed", (
            "a NULL clock is NULL-false against the grace, so the row was skipped in silence — "
            "the 'stuck forever' this appendix exists to end"
        )
        from netbox_nso_plugin.reconcile import _STUCK_STATIC_ROUTE_ERROR, _UNCLOCKED_STATIC_ROUTE_ERROR

        assert state.last_apply_error == _UNCLOCKED_STATIC_ROUTE_ERROR
        assert state.last_apply_error != _STUCK_STATIC_ROUTE_ERROR.format(generation=0), (
            "the impossible state is reported as an ordinary timeout"
        )

    def test_a_null_clock_still_stands_down_while_an_apply_is_in_flight(self):
        """The escalation is new, its preconditions are not: an in-flight Apply still wins."""
        from netbox_nso_plugin.models import NSOStaticRouteState

        device, mgmt = self._device("noclockapply", 62)
        sr = _route("10.52.0.0/16", "10.52.0.1", devices=[device])
        state = _own(sr, mgmt, generation=0, expected=False)
        NSOStaticRouteState.objects.filter(pk=state.pk).update(generation_started_at=None)
        self.adapter.store.queued_job(62)

        self._tick()

        state.refresh_from_db()
        assert state.status == "deploying", "the clock failed a row the running apply is about to settle"
