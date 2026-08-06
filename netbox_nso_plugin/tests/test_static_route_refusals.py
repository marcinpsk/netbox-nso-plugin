# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1396 R3 P4 — the fork's NSO-scoped refusal, exercised where NSO exists.

The fork refuses converting an NSO-managed route to an interface-only next hop, probing
``StaticRoute.nso_states`` without importing this plugin. Fork CI has no plugin, so it can
only pin the inert half; the refusal itself is pinned here. Pins P4.3.
"""

from __future__ import annotations

from django.test import TestCase

from .mixins import IntentPushResetMixin
from .test_static_route_transition import _fixtures, _make_device, _make_mgmt, _own, _route


class TestInterfaceOnlyConversionRefusal(IntentPushResetMixin, TestCase):
    """P4.3 — an NSO-managed route may not drop its IP next hop for an interface.

    The plugin skips ``next_hop is None`` rows from both the push and acceptance, so the
    conversion would silently strip the route from what NSO is told to configure.
    """

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("refuse")
        cls.mgmt = _make_mgmt(cls.device, "refuse", 8901)

    def _form(self, sr):
        from netbox_routing.forms import StaticRouteForm

        # The edit view loads the instance from the database, so next_hop is a
        # netaddr.IPAddress here and not the string the fixture assigned.
        sr.refresh_from_db()
        return StaticRouteForm(
            data={
                "name": "Converted",
                "devices": [self.device.pk],
                "vrf": None,
                "prefix": str(sr.prefix),
                "next_hop": "",
                "interface_next_hop": "GigabitEthernet0/0",
                "metric": 1,
            },
            instance=sr,
        )

    def test_an_owned_route_may_not_convert_to_interface_only(self):
        with _fixtures():
            sr = _route("10.30.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt, status="accepted")

        form = self._form(sr)

        self.assertFalse(form.is_valid())
        self.assertIn("interface_next_hop", form.errors)

    def test_a_merely_imported_route_may_not_convert_either(self):
        """An imported row is equally dropped by the push and can never be accepted."""
        with _fixtures():
            sr = _route("10.31.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt, status="imported")

        form = self._form(sr)

        self.assertFalse(form.is_valid())
        self.assertIn("interface_next_hop", form.errors)

    def test_the_refusal_does_not_read_a_next_hop_for_truth(self):
        """``bool(netaddr.IPAddress('0.0.0.0'))`` is False — a truthiness test reads a
        real next hop as absent and lets the conversion through."""
        with _fixtures():
            sr = _route("10.34.0.0/16", "0.0.0.0", devices=[self.device])
            _own(sr, self.mgmt, status="accepted")

        form = self._form(sr)

        self.assertFalse(form.is_valid())
        self.assertIn("interface_next_hop", form.errors)

    def test_a_route_nso_does_not_manage_may_still_convert(self):
        with _fixtures():
            sr = _route("10.32.0.0/16", "10.0.0.1", devices=[self.device])

        form = self._form(sr)

        self.assertTrue(form.is_valid(), form.errors)

    def test_an_owned_route_may_still_change_its_ip_next_hop(self):
        """The refusal is the conversion, not every edit of an NSO-managed route."""
        from netbox_routing.forms import StaticRouteForm

        with _fixtures():
            sr = _route("10.33.0.0/16", "10.0.0.1", devices=[self.device])
            _own(sr, self.mgmt, status="in_sync")

        sr.refresh_from_db()
        form = StaticRouteForm(
            data={
                "name": "Rehomed",
                "devices": [self.device.pk],
                "vrf": None,
                "prefix": str(sr.prefix),
                "next_hop": "10.0.0.2",
                "metric": 1,
            },
            instance=sr,
        )

        self.assertTrue(form.is_valid(), form.errors)
