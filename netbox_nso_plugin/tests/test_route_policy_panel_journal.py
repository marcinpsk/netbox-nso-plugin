# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""End-to-end tests for the route-policy "applied to devices" panel + apply journal.

Both features hang off the GenericForeignKey from NSORoutePolicyState into the REAL
netbox_routing objects, so these tests build that link the production way — by running
reconcile_route_policy — then exercise the real template render and real extras.JournalEntry
writes (no mocks of our own code).
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.contenttypes.models import ContentType
from django.template.loader import render_to_string
from django.test import TestCase
from extras.models import JournalEntry


def _job(job_id, *, in_sync=0, apply_failed=0, status="succeeded", errors=None):
    job = {
        "id": job_id,
        "type": "apply",
        "status": status,
        "result": {"route_policy_count_by_outcome": {"in_sync": in_sync, "apply_failed": apply_failed}},
    }
    if errors:
        # Mirror the adapter's failed-apply job.error shape (jobs API exposes it).
        job["error"] = {
            "code": "nso_commit_failed",
            "message": f"{len(errors)} item(s) failed to apply",
            "detail": {"items": [{"type": "route_policy", "error": e} for e in errors]},
        }
    return job


class _RoutePolicyFixture(TestCase):
    """Shared setup: a device with one object per route-policy family, GFK-linked."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="RpjMfg", slug="rpjmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="RpjDev", slug="rpjdev")
        role = DeviceRole.objects.create(name="RpjRole", slug="rpjrole")
        site = Site.objects.create(name="RpjSite", slug="rpjsite")
        cls.device = Device.objects.create(name="rpj-router", device_type=dt, role=role, site=site)

    def _make_mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="rpj-inst", defaults={"adapter_instance_id": "rpj-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "rpj-dev", "adapter_device_id": 777},
        )[0]

    def _reconcile(self):
        """Create real netbox_routing objects + GFK-linked state rows via the reconciler."""
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        payload = {
            "prefix_lists": [{"name": "PLJ", "entries": [{"seq": 5, "action": "permit"}]}],
            "community_lists": [{"name": "CLJ", "entries": [{"community": "65000:1"}]}],
            "as_paths": [{"name": "APJ", "entries": [{"regex": "^65000_"}]}],
            "route_maps": [{"name": "RMJ", "entries": [{"seq": 10, "action": "permit"}]}],
        }
        reconcile_route_policy(self.device, payload)


class TestAppliedToDevicesPanel(_RoutePolicyFixture):
    def test_panel_lists_managing_device_and_status(self):
        """full_width_page() collects the GFK-linked state rows for the object."""
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.template_content import RoutePolicyNSODevices

        self._make_mgmt()
        self._reconcile()
        cl = CommunityList.objects.get(name="CLJ")

        ext = object.__new__(RoutePolicyNSODevices)
        ext.context = {"object": cl}
        captured = {}

        def fake_render(template, extra_context=None):
            captured.update(extra_context or {})
            return ""

        ext.render = fake_render  # type: ignore[method-assign]
        ext.full_width_page()

        states = captured["nso_states"]
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].management.device, self.device)
        self.assertIsInstance(states[0], NSORoutePolicyState)

    def test_panel_template_renders_real_html(self):
        """The real template renders the device link + human status (no mocks)."""
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.models import NSORoutePolicyState

        self._make_mgmt()
        self._reconcile()
        cl = CommunityList.objects.get(name="CLJ")
        ct = ContentType.objects.get_for_model(cl)
        # Operator owns it → status accepted (so the panel shows a non-import badge).
        NSORoutePolicyState.objects.filter(content_type=ct, object_id=cl.pk).update(status="accepted")
        states = list(NSORoutePolicyState.objects.filter(content_type=ct, object_id=cl.pk).select_related("management"))

        html = render_to_string("netbox_nso_plugin/route_policy_nso_devices.html", {"nso_states": states})
        self.assertIn("Applied to Devices", html)
        self.assertIn("rpj-router", html)
        self.assertIn("Accepted", html)  # get_status_display

    def test_panel_empty_when_no_states(self):
        """No managing device → the panel renders nothing."""
        html = render_to_string("netbox_nso_plugin/route_policy_nso_devices.html", {"nso_states": []})
        self.assertNotIn("Applied to Devices", html)


class TestRoutePolicyApplyJournal(_RoutePolicyFixture):
    def _owned_reconcile(self):
        """Reconcile, then mark every state row owned (accepted) so the apply journals it."""
        from netbox_nso_plugin.models import NSORoutePolicyState

        self._reconcile()
        NSORoutePolicyState.objects.filter(management__device=self.device).update(status="accepted")

    def _entries_for(self, name, model):
        ct = ContentType.objects.get_for_model(model)
        obj = model.objects.get(name=name)
        return JournalEntry.objects.filter(assigned_object_type=ct, assigned_object_id=obj.pk)

    def test_success_writes_journal_on_each_owned_object(self):
        from netbox_routing.models import ASPath, CommunityList, PrefixList, RouteMap

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._owned_reconcile()

        _journal_route_policy_apply(mgmt, _job(8527, in_sync=2))

        # One entry per family object (all four owned + GFK-linked).
        for name, model in (("CLJ", CommunityList), ("RMJ", RouteMap), ("PLJ", PrefixList), ("APJ", ASPath)):
            entries = self._entries_for(name, model)
            self.assertEqual(entries.count(), 1, name)
            entry = entries.first()
            self.assertEqual(entry.kind, "success")
            self.assertIn("succeeded", entry.comments)
            self.assertIn("job #8527", entry.comments)
            self.assertIn("NSO tab", entry.comments)
            self.assertIsNone(entry.created_by)

        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_journaled_apply_job, "8527")
        # last_apply_at stamped on the rows for the panel's "Last apply" column.
        self.assertTrue(all(r.last_apply_at for r in NSORoutePolicyState.objects.filter(management=mgmt)))

    def test_idempotent_same_job_does_not_duplicate(self):
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._owned_reconcile()

        _journal_route_policy_apply(mgmt, _job(8527, in_sync=2))
        _journal_route_policy_apply(mgmt, _job(8527, in_sync=2))  # same job id → no-op

        self.assertEqual(self._entries_for("CLJ", CommunityList).count(), 1)

    def test_failure_marks_entry_danger(self):
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._owned_reconcile()

        _journal_route_policy_apply(mgmt, _job(8530, in_sync=1, apply_failed=1, status="failed"))

        entry = self._entries_for("CLJ", CommunityList).first()
        self.assertEqual(entry.kind, "danger")
        self.assertIn("failed", entry.comments)

    def test_no_route_policy_scope_records_job_but_writes_nothing(self):
        """An apply that touched no route-policy scope is marked seen but journals nothing."""
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._owned_reconcile()

        _journal_route_policy_apply(mgmt, _job(8531, in_sync=0, apply_failed=0))

        self.assertEqual(self._entries_for("CLJ", CommunityList).count(), 0)
        mgmt.refresh_from_db()
        self.assertEqual(mgmt.last_journaled_apply_job, "8531")

    def test_imported_only_object_is_not_journaled(self):
        """A non-owned (imported) object was never pushed → no apply log."""
        from netbox_routing.models import CommunityList

        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._reconcile()  # rows stay 'imported' (NOT accepted)

        _journal_route_policy_apply(mgmt, _job(8540, in_sync=2))

        self.assertEqual(self._entries_for("CLJ", CommunityList).count(), 0)

    def _device_entries(self):
        dev_ct = ContentType.objects.get_for_model(self.device)
        return JournalEntry.objects.filter(assigned_object_type=dev_ct, assigned_object_id=self.device.pk)

    def test_success_writes_a_device_journal_entry(self):
        """The DEVICE journal (where the UI points the operator) records the apply."""
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._owned_reconcile()

        _journal_route_policy_apply(mgmt, _job(8550, in_sync=2))

        entries = self._device_entries()
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.first().kind, "success")
        self.assertIn("job #8550", entries.first().comments)

    def test_failure_writes_device_journal_with_real_error_detail(self):
        """A failed apply lands on the device journal WITH the real device-commit reason
        (not just a generic 'see the adapter job') — the operator's complaint."""
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._owned_reconcile()

        _journal_route_policy_apply(
            mgmt,
            _job(
                8551,
                in_sync=1,
                apply_failed=1,
                status="failed",
                errors=["device rejected 'set extcommunity rt' (unsupported on this NED)"],
            ),
        )

        entry = self._device_entries().first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.kind, "danger")
        self.assertIn("failed", entry.comments)
        self.assertIn("device rejected 'set extcommunity rt'", entry.comments)

    def test_device_journal_idempotent_for_same_job(self):
        from netbox_nso_plugin.reconcile import _journal_route_policy_apply

        mgmt = self._make_mgmt()
        self._owned_reconcile()

        _journal_route_policy_apply(mgmt, _job(8552, in_sync=2))
        _journal_route_policy_apply(mgmt, _job(8552, in_sync=2))

        self.assertEqual(self._device_entries().count(), 1)
