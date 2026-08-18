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

from contextlib import ExitStack
from itertools import count
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from ._settlement_case import _make_device, _make_mgmt, _own, _route, _SettlementCase, _stale_clock

#: Every transport an Apply reaches bar the static route's. Doubled where the Apply claims
#: for real: the adapter double serves the static-route endpoint, and the rest are a
#: different subsystem here.
_OTHER_TRANSPORTS = (
    "apply_lag_config",
    "apply_switchport_config",
    "put_bfd_intent",
    "put_intent",
    "put_interface_mtu_intent",
    "put_l2_sap_intent",
    "put_logging_intent",
    "put_route_policy_intent",
    "put_snmp_intent",
    "put_subinterface_intent",
    "put_svi_intent",
    "put_vlan_intent",
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
        """Run the Apply with the static-route claim answering *static_response*.

        The Apply routes every scope through ``drain.push_now``, so that is the boundary the
        promotion gate reads: the other scopes are a different subsystem here and answer
        ``None``, which the gate consults for none of them.
        """
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, state, other = self._setup()
        patcher = patch(
            "netbox_nso_plugin.drain.push_now",
            side_effect=lambda device_id, scope, **kwargs: static_response if scope == "static_route" else None,
        )
        push = patcher.start()
        self.addCleanup(patcher.stop)
        _prepare_apply(mgmt)
        state.refresh_from_db()
        other.refresh_from_db()
        return state, other, push

    def test_a_failed_force_push_skips_promotion(self):
        """A forced claim answers ``None`` only on a real rejection — the adapter stored nothing."""
        state, other, push = self._prepare(static_response=None)

        assert [call.args[1] for call in push.call_args_list].count("static_route") == 1
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

    def test_a_refused_snmp_refresh_stops_the_apply_before_any_promotion(self):
        """codex O1 r4 F2: an Apply against a stale SNMP store re-applies what was deleted.

        The refusal is the SNMP claim's own outcome, so it is the outcome the Apply reads:
        a transport failure answers ``None`` too and leaves the Apply to fail at the trigger
        as it always has.
        """
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import ApplyRefused, _prepare_apply

        mgmt, state, other = self._setup()
        # Every push settles, so the SNMP refusal is the only thing that can abort the Apply.
        for name, answer in (("push_now", {"count": 0}), ("drain_key", drain.REFUSED)):
            patcher = patch(f"netbox_nso_plugin.drain.{name}", side_effect=lambda *args, answer=answer, **kw: answer)
            patcher.start()
            self.addCleanup(patcher.stop)

        with self.assertRaisesRegex(ApplyRefused, "SNMP"):
            _prepare_apply(mgmt)

        state.refresh_from_db()
        other.refresh_from_db()
        assert (state.status, other.status) == ("accepted", "accepted"), "the abort promotes nothing"

    def test_apply_uses_one_decreasing_deadline_for_all_preparation_sends(self):
        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import _prepare_apply

        mgmt, _state, _other = self._setup()
        with (
            patch("time.monotonic", side_effect=count()),
            patch(
                "netbox_nso_plugin.drain.push_now",
                side_effect=lambda device_id, scope, **kwargs: {"count": 0} if scope == "static_route" else None,
            ) as push,
            patch("netbox_nso_plugin.drain.drain_key", return_value=drain.SUCCEEDED) as snmp,
        ):
            _prepare_apply(mgmt)

        deadlines = [call.kwargs["deadline"] for call in [*push.call_args_list, *snmp.call_args_list]]
        assert len(deadlines) == 13
        assert all(later < earlier for earlier, later in zip(deadlines, deadlines[1:]))

    def test_apply_stops_before_the_first_send_when_its_total_budget_is_spent(self):
        from netbox_nso_plugin.views import ApplyRefused, _prepare_apply

        mgmt, state, other = self._setup()
        with (
            patch("time.monotonic", side_effect=[0, 121]),
            patch("netbox_nso_plugin.drain.push_now") as push,
            self.assertRaisesRegex(ApplyRefused, "preparation deadline"),
        ):
            _prepare_apply(mgmt)

        push.assert_not_called()
        state.refresh_from_db()
        other.refresh_from_db()
        assert (state.status, other.status) == ("accepted", "accepted")


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
        from netbox_nso_plugin.reconcile import _STUCK_STATIC_ROUTE_ERROR
        from netbox_nso_plugin.views import _prepare_apply

        device, mgmt = self._device("stuckclock", 60)
        sr = _route("10.50.0.0/16", "10.50.0.1", devices=[device])
        state = _own(sr, mgmt, generation=401, status="accepted")
        _stale_clock(state)  # armed long before this Apply, which is what dates the wait

        # The static-route claim runs for real against the adapter double, which is what
        # promotes the row; the other scopes only have their transports doubled.
        with ExitStack() as stack:
            for name in _OTHER_TRANSPORTS:
                stack.enter_context(patch(f"netbox_nso_plugin.adapter_client.{name}"))
            _prepare_apply(mgmt)

        state.refresh_from_db()
        assert state.status == "deploying", "the setup never reached the state the backstop judges"

        # The adapter reports no result at all: the feed is empty, so the walk drains.
        self._tick()

        state.refresh_from_db()
        assert state.status == "apply_failed", "the row is stranded 'applying' forever"
        assert state.last_apply_error == _STUCK_STATIC_ROUTE_ERROR.format(generation=401), state.last_apply_error
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

    def test_an_unreadable_jobs_probe_stands_the_escalation_down(self):
        """Codex S6 P2 — a probe that failed did not answer "is an Apply running".

        The feed can drain while the separate jobs request times out, and a NULL-clock row
        has no grace to absorb that: reading the failure as "nothing is running" fails a row
        whose Apply is in flight, and no ``in_sync`` can lift a row back out of
        ``apply_failed``. Standing down costs one tick.
        """
        from netbox_nso_plugin.models import NSOStaticRouteState

        device, mgmt = self._device("probefail", 63)
        sr = _route("10.53.0.0/16", "10.53.0.1", devices=[device])
        state = _own(sr, mgmt, generation=0, expected=False)
        NSOStaticRouteState.objects.filter(pk=state.pk).update(generation_started_at=None)
        # The Apply IS running; the probe that would have said so is what breaks.
        self.adapter.store.queued_job(63)
        self.adapter.store.jobs_error_devices.add(63)

        self._tick()

        state.refresh_from_db()
        assert state.status == "deploying", (
            "an unreadable jobs list was read as 'no apply is active', so the backstop failed "
            "a row whose Apply is in flight — unrecoverable without an operator"
        )

        # And it is a stand-down, not a refusal: the row still escalates once the probe answers.
        self.adapter.store.jobs_error_devices.discard(63)
        self.adapter.store.jobs.clear()
        self._tick()

        state.refresh_from_db()
        assert state.status == "apply_failed"

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


# ── CodeQL py/stack-trace-exposure — the refusal wording is rebuilt, never serialized ────


class TestApplyRefusalSealing(TestCase):
    """PR #24 CodeQL alert 18: no exception object may flow into an HTTP response."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.superuser = get_user_model().objects.create_superuser(
            username="sealtestnsoadmin", password="seal-test-pass", email="seal@test.example"
        )

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def _mgmt(self, tag, adapter_device_id):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = _make_device(tag)
        inst, _ = NSOInstance.objects.get_or_create(name=f"{tag}-inst", defaults={"adapter_instance_id": f"{tag}-inst"})
        with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"):
            return NSODeviceManagement.objects.create(
                device=device,
                nso_instance=inst,
                nso_device_name=f"nso-{tag}",
                adapter_device_id=adapter_device_id,
            )

    def test_the_refusal_handler_never_serializes_the_exception(self):
        """The ApplyRefused handler rebuilds its wording; the exception reaches only the log."""
        import ast
        import inspect

        from netbox_nso_plugin import views

        handlers = [
            node
            for node in ast.walk(ast.parse(inspect.getsource(views)))
            if isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "ApplyRefused"
        ]
        assert handlers, "the apply action lost its ApplyRefused handler"
        for handler in handlers:
            for call in (node for node in ast.walk(handler) if isinstance(node, ast.Call)):
                target = ast.unparse(call.func)
                if target in {"JsonResponse", "messages.error", "messages.warning", "messages.success"}:
                    names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
                    assert handler.name not in names, (
                        f"{target} in the ApplyRefused handler uses the bound exception; "
                        "rebuild the message from the refusal type instead"
                    )

    def test_a_refused_snmp_refresh_answers_the_rebuilt_wording(self):
        """End to end: POST → view → 409 whose body carries the recorded cause, not str(exc)."""
        from django.urls import reverse

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.models import NSODeviceManagement
        from netbox_nso_plugin.views import _snmp_refusal_message

        mgmt = self._mgmt("seal-snmp", 97)
        NSODeviceManagement.objects.filter(pk=mgmt.pk).update(
            intent_push_errors={"snmp": {"message": "the store refused the shrink"}}
        )
        for name, answer in (("push_now", {"count": 0}), ("drain_key", drain.REFUSED)):
            patcher = patch(f"netbox_nso_plugin.drain.{name}", side_effect=lambda *a, answer=answer, **kw: answer)
            patcher.start()
            self.addCleanup(patcher.stop)

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "apply"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        assert response.status_code == 409
        mgmt.refresh_from_db()
        assert response.json()["message"] == _snmp_refusal_message(mgmt)
        assert "the store refused the shrink" in response.json()["message"]

    def test_an_expired_budget_answers_the_deadline_wording(self):
        """End to end: the deadline refusal serves its fixed wording with a 409."""
        from itertools import chain, repeat

        from django.urls import reverse

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import _APPLY_DEADLINE_MESSAGE

        mgmt = self._mgmt("seal-deadline", 98)
        spent = drain.SEND_DEADLINE.total_seconds() + 1
        with patch("time.monotonic", side_effect=chain([0, spent], repeat(spent))):
            url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "apply"])
            response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        assert response.status_code == 409
        assert response.json()["message"] == _APPLY_DEADLINE_MESSAGE
