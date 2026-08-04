# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1396 R3 P6 — surfacing what an intent push was rejected with.

``_push_changed`` used to swallow every failure into one log line, so an operator whose
edit the adapter refused saw a freshly-accepted green row and no reason. R3 persists the
rejection per ``(device, scope)`` and renders it. Pins P6.1 through P6.7.

R3 records; **#1474 owns the retry** — nothing here re-sends, times out or queues, and
P6.4 pins exactly that.
"""

from __future__ import annotations

import contextlib
import threading
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .mixins import IntentPushResetMixin
from .test_bgp_greenfield import _CascadeFlushMixin

PUT = "netbox_nso_plugin.adapter_client.put_static_route_intent"
PUT_VLAN = "netbox_nso_plugin.adapter_client.put_vlan_intent"


def _make_device(tag: str, index: int = 1):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"Pe{tag}Mfg", slug=f"pe{tag}mfg")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"Pe{tag}Dev", slug=f"pe{tag}dev")
    role, _ = DeviceRole.objects.get_or_create(name=f"Pe{tag}Role", slug=f"pe{tag}role")
    site, _ = Site.objects.get_or_create(name=f"Pe{tag}Site", slug=f"pe{tag}site")
    return Device.objects.create(name=f"pe-{tag}-rtr-{index}", device_type=dt, role=role, site=site)


def _make_mgmt(device, tag: str, adapter_device_id: int):
    from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

    inst, _ = NSOInstance.objects.get_or_create(
        name=f"pe-{tag}-inst", defaults={"adapter_instance_id": f"pe-{tag}-inst"}
    )
    return NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"nso-pe-{tag}-{device.pk}",
        adapter_device_id=adapter_device_id,
    )


@contextlib.contextmanager
def _fixtures():
    """Build fixtures with the adapter patched out, then clear the coalescer."""
    from netbox_nso_plugin.signals import reset_intent_push_state

    with patch(PUT):
        yield
    reset_intent_push_state()


def _route(prefix, next_hop, *, vrf=None, metric=1, devices=()):
    from netbox_routing.models import StaticRoute

    from netbox_nso_plugin.signals import suppress_intent_push

    sr = StaticRoute.objects.create(prefix=prefix, next_hop=next_hop, vrf=vrf, metric=metric)
    if devices:
        with suppress_intent_push():
            sr.devices.add(*devices)
    return sr


def _own(sr, mgmt, *, status="in_sync"):
    from netbox_nso_plugin.intent_generation import allocate_intent_generation
    from netbox_nso_plugin.models import NSOStaticRouteState

    return NSOStaticRouteState.objects.create(
        management=mgmt,
        static_route=sr,
        status=status,
        nso_vrf=sr.vrf.name if sr.vrf else "",
        nso_prefix=str(sr.prefix or ""),
        nso_next_hop=str(sr.next_hop or ""),
        accepted_at=timezone.now(),
        intent_generation=allocate_intent_generation(),
        generation_started_at=timezone.now(),
    )


def _adapter_error(message, code, detail):
    from netbox_nso_plugin.adapter_client import AdapterError

    return AdapterError(message, code=code, detail=detail)


def _duplicate_triple(triple):
    return _adapter_error(
        "Two routes in the payload carry the same (vrf, prefix, next_hop)",
        "validation_error",
        {"reason": "duplicate_triple", "triple": list(triple)},
    )


def _duplicate_route_id(route_id):
    return _adapter_error(
        "Two routes in the payload claim the same route_id",
        "validation_error",
        {"reason": "duplicate_route_id", "route_id": route_id},
    )


def _push(device_id, adapter_device_id, *, force=True):
    from netbox_nso_plugin.signals import _push_static_route_intent_for_device

    return _push_static_route_intent_for_device(device_id, adapter_device_id, force=force)


def _record(mgmt, scope="static_route"):
    mgmt.refresh_from_db()
    return (mgmt.intent_push_errors or {}).get(scope)


class TestIntentPushRejectionRecord(IntentPushResetMixin, TestCase):
    """P6.1, P6.3, P6.4, P6.5 — what gets persisted, and what deliberately does not."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("rec")
        cls.mgmt = _make_mgmt(cls.device, "rec", 9101)

    def test_a_refused_push_is_recorded_and_the_next_success_clears_it(self):
        """P6.1 — the operator's save must still commit, and the reason must outlive the log."""
        with _fixtures():
            sr = _route("10.60.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt)

        with patch(PUT, side_effect=_duplicate_triple(("", "10.60.0.0/16", "10.0.0.1"))):
            self.assertIsNone(_push(self.device.pk, self.mgmt.adapter_device_id))

        entry = _record(self.mgmt)
        self.assertIsNotNone(entry, "a swallowed rejection left no durable record")
        self.assertEqual(entry["code"], "validation_error")
        self.assertEqual(entry["detail"]["reason"], "duplicate_triple")
        self.assertIn("same (vrf, prefix, next_hop)", entry["message"])
        self.assertEqual(entry["attempt"], 1)
        self.assertTrue(entry["at"])
        # The route itself survived — a refused push must never roll the operator's edit back.
        self.assertTrue(type(sr).objects.filter(pk=sr.pk).exists())

        with patch(PUT, return_value={"device_id": 1, "count": 1, "routes": []}):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.assertIsNone(_record(self.mgmt), "a later success left the stale rejection on screen")

    def test_duplicate_route_id_names_one_route_and_duplicate_triple_names_all_of_them(self):
        """P6.3 — `duplicate_triple` fires on two PAYLOAD entries sharing a triple, so it does
        not resolve to one overlay; attributing it to one would point at an arbitrary route."""
        with _fixtures():
            first = _route("10.61.0.0/16", "10.0.0.1", devices=[self.device])
            second = _route("10.61.0.0/16", "10.0.0.1", devices=[self.device])
            _own(first, self.mgmt)
            _own(second, self.mgmt)

        with patch(PUT, side_effect=_duplicate_route_id(first.pk)):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.assertEqual(_record(self.mgmt)["route_ids"], [first.pk])

        with patch(PUT, side_effect=_duplicate_triple(("", "10.61.0.0/16", "10.0.0.1"))):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.assertEqual(_record(self.mgmt)["route_ids"], sorted([first.pk, second.pk]))

    def test_an_unresolvable_detail_stays_device_scoped(self):
        """P6.3 — a triple that matches no owned route yields no attribution rather than a guess."""
        with _fixtures():
            sr = _route("10.62.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt)

        with patch(PUT, side_effect=_duplicate_triple(("", "192.0.2.0/24", "10.9.9.9"))):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.assertEqual(_record(self.mgmt)["route_ids"], [])

    def test_a_device_claimed_409_is_recorded_and_never_retried(self):
        """P6.4 — the retry substrate is #1474's; R3 adding one here would double-apply."""
        with _fixtures():
            sr = _route("10.63.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt)

        claimed = _adapter_error("Device is claimed", "conflict", {"reason": "device_claimed"})
        with patch(PUT, side_effect=claimed) as put:
            _push(self.device.pk, self.mgmt.adapter_device_id)

        self.assertEqual(put.call_count, 1, "R3 re-sent a refused push — that is #1474's job")
        entry = _record(self.mgmt)
        self.assertEqual(entry["detail"]["reason"], "device_claimed")
        self.assertEqual(entry["code"], "conflict")

    def test_a_non_adapter_exception_is_swallowed_and_recorded_by_repr(self):
        """P6.5 — the bare `except` stays (P4); only the structured detail is conditional."""
        with _fixtures():
            sr = _route("10.64.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt)

        with patch(PUT, side_effect=ValueError("boom")):
            self.assertIsNone(_push(self.device.pk, self.mgmt.adapter_device_id))

        entry = _record(self.mgmt)
        self.assertEqual(entry["code"], "")
        self.assertEqual(entry["detail"], {})
        self.assertIn("ValueError", entry["message"])
        self.assertIn("boom", entry["message"])

    def test_a_skipped_unchanged_push_allocates_no_attempt(self):
        """No request was made, so there is nothing to record and no mark to burn."""
        with _fixtures():
            sr = _route("10.65.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt)

        with patch(PUT, return_value={"device_id": 1, "count": 1, "routes": []}):
            _push(self.device.pk, self.mgmt.adapter_device_id, force=False)
            self.mgmt.refresh_from_db()
            self.assertEqual(self.mgmt.intent_push_attempts.get("static_route"), 1)
            _push(self.device.pk, self.mgmt.adapter_device_id, force=False)

        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.intent_push_attempts.get("static_route"), 1)


class TestIntentPushRejectionIsolation(IntentPushResetMixin, TestCase):
    """P6.2 — per-scope isolation, and the watermark that outlives the cleared entry."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("iso")
        cls.mgmt = _make_mgmt(cls.device, "iso", 9102)

    def setUp(self):
        super().setUp()
        with _fixtures():
            self.sr = _route("10.70.0.0/16", "10.0.0.1", devices=[self.device])
            _own(self.sr, self.mgmt)

    def test_one_scopes_failure_and_success_never_touch_another_scopes_record(self):
        """P6.2 — `_push_changed` is shared by every scope, so a device-wide record would let
        the VLAN push's success erase the static route's unresolved rejection."""
        from netbox_nso_plugin.signals import _push_changed

        with patch(PUT, side_effect=_duplicate_triple(("", "10.70.0.0/16", "10.0.0.1"))):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.assertIsNotNone(_record(self.mgmt))

        # Another scope fails, then succeeds. Neither may reach static_route.
        _push_changed((self.device.pk, "vlan"), [{"a": 1}], lambda: (_ for _ in ()).throw(ValueError("vlan down")))
        self.assertIsNotNone(_record(self.mgmt, "vlan"))
        _push_changed((self.device.pk, "vlan"), [{"a": 2}], lambda: {"ok": True})
        self.assertIsNone(_record(self.mgmt, "vlan"))

        self.assertIsNotNone(_record(self.mgmt), "another scope's success cleared the static record")
        self.assertEqual(_record(self.mgmt)["detail"]["reason"], "duplicate_triple")

    def test_a_delayed_failure_cannot_resurrect_over_a_newer_success(self):
        """P6.2 — the watermark must live OUTSIDE the error entry: keeping the attempt token
        inside it means the success that clears the entry takes the token with it, and the
        late attempt-1 failure then looks current."""
        from netbox_nso_plugin.signals import _record_push_outcome

        with patch(PUT, side_effect=_duplicate_triple(("", "10.70.0.0/16", "10.0.0.1"))):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        stale_attempt = _record(self.mgmt)["attempt"]

        with patch(PUT, return_value={"device_id": 1, "count": 1, "routes": []}):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.assertIsNone(_record(self.mgmt))

        # Attempt 1's failure finally lands, long after attempt 2 succeeded and cleared.
        _record_push_outcome(self.device.pk, "static_route", stale_attempt, ValueError("late"))
        self.assertIsNone(_record(self.mgmt), "a superseded attempt resurrected over a newer success")

        self.mgmt.refresh_from_db()
        self.assertGreaterEqual(self.mgmt.intent_push_attempts["static_route"], stale_attempt + 1)

    def test_a_delayed_success_cannot_clear_a_newer_failure(self):
        """The same rule symmetrically: a stale success must not erase the current reason."""
        from netbox_nso_plugin.signals import _record_push_outcome

        with patch(PUT, return_value={"device_id": 1, "count": 1, "routes": []}):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.mgmt.refresh_from_db()
        stale_attempt = self.mgmt.intent_push_attempts["static_route"]

        with patch(PUT, side_effect=_duplicate_triple(("", "10.70.0.0/16", "10.0.0.1"))):
            _push(self.device.pk, self.mgmt.adapter_device_id)
        self.assertIsNotNone(_record(self.mgmt))

        _record_push_outcome(self.device.pk, "static_route", stale_attempt, None)
        self.assertIsNotNone(_record(self.mgmt), "a superseded success cleared a newer failure")


class TestIntentPushRejectionConcurrency(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """P6.2 — two workers writing DIFFERENT scopes' records at once.

    A read-modify-write of the shared JSONField loses one of the two updates; the lock is
    what makes the map safe to share. Needs a real transaction boundary, so
    TransactionTestCase.
    """

    def setUp(self):
        super().setUp()
        self.device = _make_device("conc")
        self.mgmt = _make_mgmt(self.device, "conc", 9103)

    def test_two_workers_writing_different_scopes_both_survive(self):
        from netbox_nso_plugin.signals import _push_changed

        start = threading.Barrier(2, timeout=30)
        errors: list[BaseException] = []

        def _fail(scope):
            try:
                start.wait()
                _push_changed(
                    (self.device.pk, scope),
                    [{"scope": scope}],
                    lambda: (_ for _ in ()).throw(ValueError(f"{scope} down")),
                )
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=_fail, args=(scope,)) for scope in ("static_route", "vlan")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.mgmt.refresh_from_db()
        self.assertEqual(set(self.mgmt.intent_push_errors), {"static_route", "vlan"})
        self.assertEqual(set(self.mgmt.intent_push_attempts), {"static_route", "vlan"})


class TestStaticRouteFailureRender(IntentPushResetMixin, TestCase):
    """P6.6, P6.7 — the grid must say WHY, and must stop calling a lost apply 'pending'."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("rnd")
        cls.mgmt = _make_mgmt(cls.device, "rnd", 9104)

    def test_display_state_splits_an_owned_apply_failed_only_where_asked(self):
        """P6.6 — folding apply_failed into 'pending apply' fleet-wide is pre-existing (P22);
        R3 splits it for static routes ONLY, so the other eight panels stay byte-identical."""
        from netbox_nso_plugin.summary import display_state

        self.assertEqual(display_state("apply_failed", True), ("pending", "pending apply"))
        self.assertEqual(display_state("apply_failed", True, distinguish_failed=True), ("apply_failed", "apply failed"))
        # Un-owned means the DEVICE changed out of band — drift either way.
        self.assertEqual(display_state("apply_failed", False, distinguish_failed=True), ("drift", "drift"))
        # Every other status is untouched by the flag.
        for status in ("in_sync", "imported", "accepted", "changed", "conflict", "deploying", ""):
            self.assertEqual(display_state(status, True), display_state(status, True, distinguish_failed=True), status)

    def _grid(self):
        from netbox_nso_plugin.views import NSOCategoryView

        return NSOCategoryView()._grid_payload("static", self.device, self.mgmt)

    def test_a_failed_route_renders_its_own_error_and_a_failed_state(self):
        """P6.6 — a blue 'pending apply' chip with no text tells the operator to Apply again;
        the apply already ran and lost, and the reason is on the row."""
        with _fixtures():
            sr = _route("10.80.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="apply_failed")
        state.last_apply_error = "static_route_send_failed: NED rejected the route"
        state.save(update_fields=["last_apply_error"])

        row = self._grid()["rows"][0]
        self.assertEqual(row["state"], "apply_failed")
        self.assertEqual(row["label"], "apply failed")
        self.assertIn("NED rejected the route", row["error"])
        self.assertIsNone(row["advisory"])

    def test_an_unproven_verdict_carries_its_advisory(self):
        """P6.7 — `unproven` is a statement about evidence, so it keeps the status and gains a
        qualifier; dropping the text would let this pin pass on a bare `accepted`."""
        with _fixtures():
            sr = _route("10.81.0.0/16", "10.0.0.1", devices=[self.device])
            state = _own(sr, self.mgmt, status="accepted")
        state.last_result_advisory = "verification disabled — nothing proves this route landed"
        state.save(update_fields=["last_result_advisory"])

        row = self._grid()["rows"][0]
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["error"])
        self.assertIn("nothing proves this route landed", row["advisory"])

    def test_the_category_payload_carries_the_push_rejection(self):
        """P6.1's render half — the banner is the only place a REFUSED push is visible; the
        rows themselves look perfectly accepted."""
        with _fixtures():
            sr = _route("10.82.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt)

        with patch(PUT, side_effect=_duplicate_triple(("", "10.82.0.0/16", "10.0.0.1"))):
            _push(self.device.pk, self.mgmt.adapter_device_id)

        self.mgmt.refresh_from_db()
        payload = self._grid()
        self.assertEqual(payload["push_error"]["detail"]["reason"], "duplicate_triple")

    def test_another_category_never_grows_a_push_banner(self):
        """Only scopes whose rejection is persisted and rendered are mapped; a category with
        no mapping must not start reporting another scope's failure."""
        from netbox_nso_plugin.views import _category_push_error

        self.mgmt.intent_push_errors = {"static_route": {"code": "validation_error"}}
        self.mgmt.save(update_fields=["intent_push_errors"])
        self.assertIsNone(_category_push_error("bgp", self.mgmt))
        self.assertIsNotNone(_category_push_error("static", self.mgmt))
