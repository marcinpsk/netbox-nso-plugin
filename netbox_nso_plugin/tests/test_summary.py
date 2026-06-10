# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Characterization truth-tables for the drift/status display model (summary.py).

summary.py is where the device NSO tab decides what each row *means* — in sync /
drift / pending apply — yet it had no dedicated tests; it only got incidental line
coverage through view tests, which is how the module hit the 90% gate while shipping
the bugs that prompted these tests.

These tests PIN current behavior across the full input space, so a future change is a
visible diff and wrong assumptions surface as explicit cases. ``NOTE:`` comments flag
behaviors that look like smells to revisit in the "then decide" phase — they are not
endorsements, just documentation of what the code does today.

Pure functions (no DB) live in SimpleTestCase; the ORM-aggregating
``_status_breakdown`` lives in a DB-backed TestCase.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from netbox_nso_plugin.summary import (
    _DIFFER_STATUSES,
    _MATCH_STATUSES,
    _status_breakdown,
    display_state,
    interface_row_state,
    interface_status_breakdown,
    matches_device_value,
)


def _state(*, status="imported", accepted_at=None, attribute="description", nso_value=""):
    """Build a stand-in for an NSOInterfaceState row (only the fields the model reads)."""
    return SimpleNamespace(status=status, accepted_at=accepted_at, attribute=attribute, nso_value=nso_value)


def _iface(*, description="", enabled=True):
    return SimpleNamespace(description=description, enabled=enabled)


class _FakeQS:
    """Minimal queryset stand-in: ``interface_status_breakdown`` only calls
    ``.select_related(...)`` then iterates, so a list behind that is enough — keeps
    the bucketing logic testable without the ORM."""

    def __init__(self, rows):
        self._rows = rows

    def select_related(self, *args, **kwargs):
        return self._rows


class TestDisplayState(SimpleTestCase):
    """display_state(status, owned) -> (kind, label). Pure status→badge mapping."""

    def test_deploying_wins_regardless_of_owned(self):
        self.assertEqual(display_state("deploying", True), ("deploying", "deploying"))
        self.assertEqual(display_state("deploying", False), ("deploying", "deploying"))

    def test_match_statuses_are_in_sync_regardless_of_owned(self):
        for status in _MATCH_STATUSES:  # ("imported", "in_sync")
            for owned in (True, False):
                with self.subTest(status=status, owned=owned):
                    self.assertEqual(display_state(status, owned), ("in_sync", "in sync"))

    def test_differ_statuses_split_on_ownership(self):
        # ("changed", "drifted", "conflict", "accepted", "apply_failed")
        for status in _DIFFER_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(display_state(status, True), ("pending", "pending apply"))
                self.assertEqual(display_state(status, False), ("drift", "drift"))

    def test_apply_failed_collapses_at_this_layer(self):
        # display_state itself still collapses apply_failed into the differ buckets; this
        # is only ever reached for non-comparable / deploying rows (see
        # interface_row_state), which never carry apply_failed today. The interfaces tab
        # surfaces "apply failed" distinctly in interface_row_state instead — see
        # TestInterfaceRowState.test_apply_failed_with_differing_values_is_surfaced.
        self.assertEqual(display_state("apply_failed", True), ("pending", "pending apply"))

    def test_unknown_and_blank_fall_through_to_unknown_kind(self):
        self.assertEqual(display_state("unknown", True), ("unknown", "unknown"))
        self.assertEqual(display_state("", False), ("unknown", "unknown"))
        # An unrecognized status echoes itself as the label.
        self.assertEqual(display_state("weird", False), ("unknown", "weird"))

    def test_unknown_is_not_owned_aware(self):
        # DECISION (kept): an owned row whose status is "unknown" renders "unknown", not
        # "pending apply" — owned-awareness is meaningless without a value to compare,
        # and "unknown" honestly means "no info". This branch is also effectively
        # unreachable for real interface data (comparable attrs go value-aware via
        # interface_row_state); routing badges use the template, not display_state.
        self.assertEqual(display_state("unknown", True), ("unknown", "unknown"))


class TestMatchesDeviceValue(SimpleTestCase):
    """matches_device_value(attribute, netbox_value, nso_value) -> bool."""

    def test_description_empty_and_none_are_equivalent(self):
        # The empty/None normalization that fixed the cross-boundary "" vs None vs NULL bug.
        self.assertTrue(matches_device_value("description", None, None))
        self.assertTrue(matches_device_value("description", "", None))
        self.assertTrue(matches_device_value("description", None, ""))
        self.assertTrue(matches_device_value("description", "", ""))

    def test_description_value_compare(self):
        self.assertTrue(matches_device_value("description", "Core Link", "Core Link"))
        self.assertFalse(matches_device_value("description", "Core Link", ""))
        self.assertFalse(matches_device_value("description", "Core Link", None))
        self.assertFalse(matches_device_value("description", "a", "b"))

    def test_description_ignores_cosmetic_surrounding_whitespace(self):
        # Device configs carry cosmetic leading/trailing spaces NetBox stores stripped;
        # these must not manufacture drift (regression: dev prod-lab03d-rc1 et-0/0/6).
        self.assertTrue(matches_device_value("description", "*** IXIA LG 1/8", "*** IXIA LG 1/8 "))
        self.assertTrue(matches_device_value("description", "Core Link ***", " Core Link *** "))
        # Internal whitespace differences are still a real difference.
        self.assertFalse(matches_device_value("description", "Core Link", "Core  Link"))

    def test_enabled_string_device_value_casing(self):
        # nso_value is stored as a string; comparison is case/space tolerant.
        self.assertTrue(matches_device_value("enabled", True, "True"))
        self.assertTrue(matches_device_value("enabled", True, "true"))
        self.assertTrue(matches_device_value("enabled", True, "  TRUE  "))
        self.assertTrue(matches_device_value("enabled", False, "False"))
        self.assertTrue(matches_device_value("enabled", False, "anything-not-true"))
        self.assertFalse(matches_device_value("enabled", True, "False"))
        self.assertFalse(matches_device_value("enabled", False, "true"))

    def test_enabled_empty_or_none_device_value_matches_symmetrically(self):
        # FIXED: when the device did not report 'enabled' (nso_value ""/None) there is
        # nothing to compare, so it's a match regardless of the NetBox value — no
        # manufactured drift, and symmetric (previously False matched but True drifted).
        self.assertTrue(matches_device_value("enabled", False, ""))
        self.assertTrue(matches_device_value("enabled", False, None))
        self.assertTrue(matches_device_value("enabled", True, ""))
        self.assertTrue(matches_device_value("enabled", True, None))


class TestInterfaceRowState(SimpleTestCase):
    """interface_row_state(st, iface) -> (kind, label, owned). Value-aware classifier."""

    def test_owned_is_derived_from_accepted_at(self):
        _, _, owned = interface_row_state(_state(accepted_at=None), _iface())
        self.assertFalse(owned)
        _, _, owned = interface_row_state(_state(accepted_at="2026-01-01"), _iface())
        self.assertTrue(owned)

    def test_matching_values_are_in_sync_even_if_status_says_differ(self):
        # The whole point: values win over the adapter's (possibly stale) status.
        st = _state(status="changed", attribute="description", nso_value="same")
        self.assertEqual(interface_row_state(st, _iface(description="same")), ("in_sync", "in sync", False))

    def test_differing_owned_is_pending_even_if_status_says_match(self):
        # The device-27 ae2.0 regression: status="imported"/"unknown" but NetBox has a
        # value the device lacks, and NetBox owns it -> pending apply.
        for status in ("imported", "unknown", "in_sync"):
            with self.subTest(status=status):
                st = _state(status=status, accepted_at="2026-01-01", attribute="description", nso_value="")
                self.assertEqual(
                    interface_row_state(st, _iface(description="Core Link")),
                    ("pending", "pending apply", True),
                )

    def test_differing_not_owned_is_drift(self):
        st = _state(status="imported", accepted_at=None, attribute="description", nso_value="")
        self.assertEqual(interface_row_state(st, _iface(description="Core Link")), ("drift", "drift", False))

    def test_enabled_attribute_is_value_aware(self):
        st = _state(status="imported", attribute="enabled", nso_value="True")
        self.assertEqual(interface_row_state(st, _iface(enabled=True)), ("in_sync", "in sync", False))
        st_owned = _state(status="imported", accepted_at="2026-01-01", attribute="enabled", nso_value="True")
        self.assertEqual(interface_row_state(st_owned, _iface(enabled=False)), ("pending", "pending apply", True))

    def test_deploying_bypasses_value_comparison(self):
        # Even with matching values, an in-flight deploy shows "deploying".
        st = _state(status="deploying", attribute="description", nso_value="same")
        self.assertEqual(interface_row_state(st, _iface(description="same")), ("deploying", "deploying", False))

    def test_non_comparable_attribute_falls_back_to_status(self):
        # mtu isn't in _COMPARABLE_IFACE_ATTRS, so display_state drives it.
        st = _state(status="changed", accepted_at="2026-01-01", attribute="mtu", nso_value="9000")
        self.assertEqual(interface_row_state(st, _iface()), ("pending", "pending apply", True))

    def test_apply_failed_with_differing_values_is_surfaced(self):
        # Smell #1 fix: a failed apply must not hide as plain "pending apply".
        st = _state(status="apply_failed", accepted_at="2026-01-01", attribute="description", nso_value="")
        self.assertEqual(
            interface_row_state(st, _iface(description="Core Link")),
            ("apply_failed", "apply failed", True),
        )

    def test_apply_failed_but_values_now_match_is_in_sync(self):
        # Value-aware still wins: if the value actually landed, stale apply_failed != failure.
        st = _state(status="apply_failed", accepted_at="2026-01-01", attribute="description", nso_value="x")
        self.assertEqual(interface_row_state(st, _iface(description="x")), ("in_sync", "in sync", True))


class TestInterfaceStatusBreakdown(SimpleTestCase):
    """interface_status_breakdown(qs) -> {total, drift, pending} (value-aware buckets)."""

    def test_buckets_match_row_classification(self):
        rows = [
            # in sync (values match) — counts only toward total
            _row(status="imported", attribute="description", nso_value="x", description="x"),
            # drift (differ, not owned)
            _row(status="imported", attribute="description", nso_value="", description="Core"),
            # pending (differ, owned)
            _row(status="imported", accepted_at="t", attribute="description", nso_value="", description="Core"),
            # deploying -> pending bucket
            _row(status="deploying", attribute="description", nso_value="x", description="x"),
            # apply_failed (differ, owned) -> pending bucket (it needs operator action)
            _row(status="apply_failed", accepted_at="t", attribute="description", nso_value="", description="Core"),
        ]
        self.assertEqual(interface_status_breakdown(_FakeQS(rows)), {"total": 5, "drift": 1, "pending": 3})

    def test_empty_queryset(self):
        self.assertEqual(interface_status_breakdown(_FakeQS([])), {"total": 0, "drift": 0, "pending": 0})


def _row(*, description="", enabled=True, **state_kwargs):
    """An NSOInterfaceState stand-in with a .interface attached (for breakdown)."""
    st = _state(**state_kwargs)
    st.interface = _iface(description=description, enabled=enabled)
    return st


class TestStatusBreakdown(TestCase):
    """_status_breakdown(qs) -> {total, drift, pending} via ORM aggregation.

    This is the owned-aware, STATUS-driven aggregation still used by every routing
    category (and historically by interfaces). It cannot see actual values — it trusts
    the stored status — which is the root behavior that hid the device-27 drift in the
    counts before interfaces switched to interface_status_breakdown.
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

        mfg = Manufacturer.objects.create(name="SBMfg", slug="sbmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="SBDev", slug="sbdev")
        role = DeviceRole.objects.create(name="SBRole", slug="sbrole")
        site = Site.objects.create(name="SBSite", slug="sbsite")
        cls.device = Device.objects.create(name="sb-rtr", device_type=dt, role=role, site=site)
        cls.Interface = Interface

    def _qs(self, rows):
        """rows: list of (status, owned). Create NSOInterfaceState and return a queryset."""
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceState

        NSOInterfaceState.objects.all().delete()
        for n, (status, owned) in enumerate(rows):
            iface = self.Interface.objects.create(device=self.device, name=f"if{n}", type="virtual")
            NSOInterfaceState.objects.create(
                interface=iface,
                attribute="description",
                status=status,
                nso_value="x",
                accepted_at=timezone.now() if owned else None,
            )
        return NSOInterfaceState.objects.filter(interface__device=self.device)

    def test_match_statuses_are_only_in_total(self):
        qs = self._qs([("imported", False), ("in_sync", True)])
        self.assertEqual(_status_breakdown(qs), {"total": 2, "drift": 0, "pending": 0})

    def test_differ_splits_on_ownership(self):
        qs = self._qs([("changed", False), ("changed", True), ("apply_failed", True), ("conflict", False)])
        # owned differ -> pending (changed+True, apply_failed+True = 2); not-owned differ -> drift (2)
        self.assertEqual(_status_breakdown(qs), {"total": 4, "drift": 2, "pending": 2})

    def test_deploying_counts_as_pending(self):
        qs = self._qs([("deploying", False)])
        self.assertEqual(_status_breakdown(qs), {"total": 1, "drift": 0, "pending": 1})

    def test_unknown_is_surfaced_as_needs_attention_not_in_sync(self):
        # FIXED: a row left at "unknown" is an anomaly (reconcilers always set a concrete
        # status), so it is surfaced under drift rather than hidden in the in-sync
        # remainder where it would read as a false "in sync".
        qs = self._qs([("unknown", True), ("unknown", False)])
        self.assertEqual(_status_breakdown(qs), {"total": 2, "drift": 2, "pending": 0})

    def test_match_and_unrecognized_status_split_correctly(self):
        # imported/in_sync are the in-sync remainder; a bogus status is surfaced as drift.
        qs = self._qs([("imported", False), ("in_sync", True), ("bogus", False)])
        self.assertEqual(_status_breakdown(qs), {"total": 3, "drift": 1, "pending": 0})
