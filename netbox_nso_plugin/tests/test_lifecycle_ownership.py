# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Lifecycle tests — the full interface ownership arc, end to end.

The recent bugs lived in the *transitions* between states and in how those
transitions surfaced on the tab — not in any single state. The existing
test_signals.py tests signal gating in isolation. This file walks one interface
through the full arc with exact native and overlay writer plans. It asserts the
user-facing classification (``summary.interface_row_state``) after every step.

Runs against the full NetBox stack (devcontainer).
"""

from __future__ import annotations

import copy

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import TestCase
from django.utils import timezone

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOInterfaceState
from netbox_nso_plugin.summary import interface_row_state

from ._outbox_case import mirror_update


class TestInterfaceOwnershipLifecycle(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="LcMfg", slug="lcmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="LcDev", slug="lcdev")
        role = DeviceRole.objects.create(name="LcRole", slug="lcrole")
        site = Site.objects.create(name="LcSite", slug="lcsite")
        cls.device = Device.objects.create(name="lc-router", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="LcNSO", adapter_instance_id="nso-lc")
        NSODeviceManagement.objects.create(
            device=cls.device,
            nso_instance=inst,
            nso_device_name="lc-router",
            adapter_device_id=77,
            manage_description=True,
            manage_enabled=True,
            custom_field_data={},
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def setUp(self):
        # Fresh interface + imported state per test (matching device value).
        self.iface = Interface.objects.create(
            device=self.device, name="ge-0/0/0", type="1000base-t", description="dev-desc", enabled=True
        )
        self.state = NSOInterfaceState.objects.create(
            interface=self.iface, attribute="description", status="imported", nso_value="dev-desc"
        )

    def _classify(self, attribute="description"):
        """Return interface_row_state for an attribute after refreshing from DB."""
        st = NSOInterfaceState.objects.get(interface=self.iface, attribute=attribute)
        self.iface.refresh_from_db()
        return interface_row_state(st, self.iface)

    def _operator_edit(self, **fields):
        """Edit and acquire one interface attribute through an exact writer."""
        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_writes
        from netbox_nso_plugin.summary import matches_device_value

        self.assertEqual(len(fields), 1)
        attribute, value = next(iter(fields.items()))
        state = NSOInterfaceState.objects.get(interface=self.iface, attribute=attribute)
        interface_candidate = copy.copy(self.iface)
        setattr(interface_candidate, attribute, value)
        state_candidate = copy.copy(state)
        state_candidate.status = "in_sync" if matches_device_value(attribute, value, state.nso_value) else "accepted"
        if state_candidate.accepted_at is None:
            state_candidate.accepted_at = timezone.now()
        state_fields = ("status", "accepted_at")
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(interface_candidate, update_fields=(attribute,)),
                planned_save(state_candidate, update_fields=state_fields),
            )
        )
        with self.captureOnCommitCallbacks(execute=True), renderer_writes(plan) as writer:
            writer.save(interface_candidate, update_fields=(attribute,))
            writer.save(state_candidate, update_fields=state_fields)
        self.iface = interface_candidate

    def _adapter_writes(self, attribute="description", **fields):
        """Simulate an adapter sync writing state (never touches accepted_at)."""
        state = NSOInterfaceState.objects.get(interface=self.iface, attribute=attribute)
        mirror_update(state, **fields)

    # ── the arc ──────────────────────────────────────────────────────────────
    def test_description_arc_import_drift_own_applyfail_converge(self):
        # 1. Imported and matching the device → in sync, not owned.
        self.assertEqual(self._classify(), ("in_sync", "in sync", False))

        # 2. Device changes out of band (adapter reports new value) → drift, not owned.
        self._adapter_writes(status="changed", nso_value="dev-changed")
        self.assertEqual(self._classify(), ("drift", "drift", False))

        # 3. Operator edits the description in NetBox → NetBox takes ownership; its value
        #    now differs from the device → pending apply, owned.
        self._operator_edit(description="operator-desc")
        st = NSOInterfaceState.objects.get(interface=self.iface, attribute="description")
        self.assertIsNotNone(st.accepted_at)  # ownership taken
        self.assertEqual(self._classify(), ("pending", "pending apply", True))

        # 4. Apply is attempted and FAILS (device still differs) → surfaced distinctly.
        self._adapter_writes(status="apply_failed")
        self.assertEqual(self._classify(), ("apply_failed", "apply failed", True))

        # 5. Retry succeeds: the device converges to NetBox's value → in sync, still owned.
        self._adapter_writes(status="in_sync", nso_value="operator-desc")
        self.assertEqual(self._classify(), ("in_sync", "in sync", True))

    def test_edit_back_to_device_value_clears_pending(self):
        # Own a differing value → pending apply.
        self._adapter_writes(status="changed", nso_value="dev-current")
        self._operator_edit(description="operator-typo")
        self.assertEqual(self._classify(), ("pending", "pending apply", True))

        # Operator edits back to the device's current value → nothing to apply → in sync
        # (still owned). Regression guard for the "phantom pending" bug.
        self._operator_edit(description="dev-current")
        st = NSOInterfaceState.objects.get(interface=self.iface, attribute="description")
        self.assertEqual(st.status, "in_sync")
        self.assertEqual(self._classify(), ("in_sync", "in sync", True))

    def test_enabled_toggle_arc(self):
        NSOInterfaceState.objects.create(interface=self.iface, attribute="enabled", status="imported", nso_value="True")
        # Imported + matching (device enabled=True, NetBox enabled=True) → in sync.
        self.assertEqual(self._classify("enabled"), ("in_sync", "in sync", False))

        # Operator disables the interface → owns it; differs from device → pending apply.
        self._operator_edit(enabled=False)
        en = NSOInterfaceState.objects.get(interface=self.iface, attribute="enabled")
        self.assertIsNotNone(en.accepted_at)
        self.assertEqual(self._classify("enabled"), ("pending", "pending apply", True))

        # Operator flips it back to the device value → in sync, owned (no phantom pending).
        self._operator_edit(enabled=True)
        self.assertEqual(self._classify("enabled"), ("in_sync", "in sync", True))
