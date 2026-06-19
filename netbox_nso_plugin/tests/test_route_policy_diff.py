# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for route_policy_diff — device-vs-NetBox delta for route-maps + redistribution.

Covers the pure diff core (route_map_diff over capture dicts), the faithful reconstruction
of a materialised RouteMap back into a capture (netbox_route_map_captured), the end-to-end
cross-device route-map diff against the REAL netbox_routing models, and the redistribution
field diff.
"""

from __future__ import annotations

import json

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase


def _rm(name, entries):
    return {"route_maps": [{"name": name, "entries": entries}]}


def _entry(seq, action="permit", match=None, set_=None, **refs):
    e = {"sequence": seq, "action": action, "match": json.dumps(match or {}), "set": json.dumps(set_ or {})}
    e.update(refs)
    return e


class TestRouteMapDiffPure(TestCase):
    """route_map_diff is a pure function over capture dicts (no DB)."""

    def test_identical_captures_report_no_diff(self):
        from netbox_nso_plugin.route_policy_diff import route_map_diff

        cap = _rm("RM", [_entry(10, set_={"local_preference": 100})])["route_maps"][0]
        d = route_map_diff(cap, cap)
        self.assertFalse(d["any_diff"])
        self.assertEqual(d["entries"][0]["presence"], "both")

    def test_changed_set_value_is_flagged(self):
        from netbox_nso_plugin.route_policy_diff import route_map_diff

        dev = _rm("RM", [_entry(10, set_={"local_preference": 200})])["route_maps"][0]
        nb = _rm("RM", [_entry(10, set_={"local_preference": 100})])["route_maps"][0]
        d = route_map_diff(dev, nb)
        self.assertTrue(d["any_diff"])
        entry = d["entries"][0]
        self.assertTrue(entry["differs"])
        setrow = next(f for f in entry["fields"] if f["label"] == "Set")
        self.assertEqual(setrow["device"], "local_preference=200")
        self.assertEqual(setrow["netbox"], "local_preference=100")
        self.assertTrue(setrow["differs"])

    def test_device_only_entry(self):
        from netbox_nso_plugin.route_policy_diff import route_map_diff

        dev = _rm("RM", [_entry(10), _entry(20)])["route_maps"][0]
        nb = _rm("RM", [_entry(10)])["route_maps"][0]
        d = route_map_diff(dev, nb)
        self.assertTrue(d["any_diff"])
        extra = next(e for e in d["entries"] if e["sequence"] == 20)
        self.assertEqual(extra["presence"], "device_only")

    def test_netbox_only_entry(self):
        from netbox_nso_plugin.route_policy_diff import route_map_diff

        dev = _rm("RM", [_entry(10)])["route_maps"][0]
        nb = _rm("RM", [_entry(10), _entry(20)])["route_maps"][0]
        d = route_map_diff(dev, nb)
        gone = next(e for e in d["entries"] if e["sequence"] == 20)
        self.assertEqual(gone["presence"], "netbox_only")

    def test_unchanged_set_value_not_flagged(self):
        from netbox_nso_plugin.route_policy_diff import route_map_diff

        # The real-world ADV_RPKI_INVALID shape: a set as-path replace that matches → no diff.
        cap = _rm("RM", [_entry(10, match={}, set_={"as_path_replace": "AS1136-KPN"}, match_prefix_lists=["RPKI"])])[
            "route_maps"
        ][0]
        d = route_map_diff(cap, cap)
        self.assertFalse(d["any_diff"])


class _RPBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="DiffMfg", slug="diffmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="DiffDev", slug="diffdev")
        role = DeviceRole.objects.create(name="DiffRole", slug="diffrole")
        site = Site.objects.create(name="DiffSite", slug="diffsite")
        cls.d1 = Device.objects.create(name="diff-r1", device_type=dt, role=role, site=site)
        cls.d2 = Device.objects.create(name="diff-r2", device_type=dt, role=role, site=site)

    def _mgmt(self, device):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="diff-inst", defaults={"adapter_instance_id": "diff-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={"nso_instance": inst, "nso_device_name": f"d-{device.pk}", "adapter_device_id": device.pk},
        )[0]


class TestNetboxReconstruction(_RPBase):
    def test_materialised_route_map_round_trips_to_no_diff(self):
        """Reconstructing a materialised RouteMap into a capture and diffing it against the
        device capture it was built from yields NO diff — proving the reconstruction is
        faithful (so a reported diff is always a real config difference)."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import netbox_route_map_captured, route_map_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        payload = _rm(
            "RM-ROUND",
            [
                _entry(10, set_={"local_preference": 150}, match_prefix_lists=["PL-A"]),
                _entry(20, action="deny", set_={"as_path_replace": "AS65000"}),
            ],
        )
        # The prefix-list must be imported too (reconciled before route-maps in production) or
        # the route-map can't link it — otherwise the diff rightly flags the missing reference.
        payload["prefix_lists"] = [{"name": "PL-A", "entries": []}]
        reconcile_route_policy(self.d1, payload)
        st = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-ROUND")
        d = route_map_diff(st.captured, netbox_route_map_captured(st.assigned_object))
        self.assertFalse(d["any_diff"], d)


class TestCrossDeviceStateDiff(_RPBase):
    def test_cross_device_conflict_shows_field_delta(self):
        """A second device whose route-map content diverges is flagged conflict; the diff
        surfaces exactly which field differs (its value vs the materialised owner's)."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, _rm("RM-X", [_entry(10, set_={"local_preference": 100})]))
        reconcile_route_policy(self.d2, _rm("RM-X", [_entry(10, set_={"local_preference": 200})]))

        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="RM-X")
        self.assertEqual(s2.status, "conflict")  # cross-device divergence preserved
        d = route_policy_state_diff(s2)
        self.assertTrue(d["any_diff"])
        setrow = next(f for f in d["entries"][0]["fields"] if f["label"] == "Set")
        self.assertEqual(setrow["device"], "local_preference=200")  # d2 on-box
        self.assertEqual(setrow["netbox"], "local_preference=100")  # materialised (d1) owner

    def test_non_route_map_family_has_no_diff(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        reconcile_route_policy(self.d1, {"prefix_lists": [{"name": "PL", "entries": []}]})
        st = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PL")
        self.assertIsNone(route_policy_state_diff(st))


class TestDiffViews(_RPBase):
    """The diff pages render (login-gated) and surface the concrete delta."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="diff-op", password="pw")  # noqa: S106
        self.client.force_login(user)

    def test_route_policy_diff_page_shows_field_delta(self):
        from django.urls import reverse

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, _rm("RM-V", [_entry(10, set_={"local_preference": 100})]))
        reconcile_route_policy(self.d2, _rm("RM-V", [_entry(10, set_={"local_preference": 200})]))
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="RM-V")

        url = reverse("plugins:netbox_nso_plugin:routing_route_policy_diff", kwargs={"pk": s2.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("local_preference=200", body)  # device on-box
        self.assertIn("local_preference=100", body)  # NetBox materialised

    def test_route_policy_diff_requires_login(self):
        from django.urls import reverse

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        reconcile_route_policy(self.d1, _rm("RM-L", [_entry(10)]))
        st = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-L")
        self.client.logout()
        url = reverse("plugins:netbox_nso_plugin:routing_route_policy_diff", kwargs={"pk": st.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)  # redirect to login

    def test_redistribution_diff_page_renders(self):
        from django.contrib.contenttypes.models import ContentType
        from django.urls import reverse
        from netbox_routing.models import OSPFInstance, Redistribution

        from netbox_nso_plugin.models import NSORedistributionState

        mgmt = self._mgmt(self.d1)
        ospf = OSPFInstance.objects.create(device=self.d1, process_id=7, router_id="7.7.7.7")
        ct = ContentType.objects.get_for_model(OSPFInstance)
        rd = Redistribution.objects.create(
            destination_type=ct, destination_id=ospf.pk, source_protocol="connected", source_ref="", metric=10
        )
        st = NSORedistributionState.objects.create(
            management=mgmt,
            dest_protocol="ospf",
            dest_ref="7",
            source_protocol="connected",
            source_ref="",
            metric=99,
            redistribution=rd,
            status="changed",
        )
        url = reverse("plugins:netbox_nso_plugin:routing_redistribution_diff", kwargs={"pk": st.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("99", body)
        self.assertIn("10", body)


class TestRedistributionDiff(_RPBase):
    def _redist_state(self, **over):
        from netbox_nso_plugin.models import NSORedistributionState

        mgmt = self._mgmt(self.d1)
        defaults = dict(
            management=mgmt,
            dest_protocol="ospf",
            dest_ref="1",
            source_protocol="connected",
            source_ref="",
            route_map="",
            metric=None,
            metric_type="",
            status="imported",
        )
        defaults.update(over)
        return NSORedistributionState.objects.create(**defaults)

    def test_unlinked_state_is_all_device_only(self):
        from netbox_nso_plugin.route_policy_diff import redistribution_diff

        st = self._redist_state(route_map="RM-RD", metric=50)
        d = redistribution_diff(st)
        self.assertFalse(d["linked"])
        self.assertTrue(d["any_diff"])
        rm = next(f for f in d["fields"] if f["label"] == "Route map")
        self.assertEqual(rm["device"], "RM-RD")
        self.assertEqual(rm["netbox"], "—")

    def test_linked_matching_state_reports_no_diff(self):
        """The phantom-'changed' case: device and the linked Redistribution object are equal."""
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution

        from netbox_nso_plugin.route_policy_diff import redistribution_diff

        ospf = OSPFInstance.objects.create(device=self.d1, process_id=1, router_id="1.1.1.1")
        ct = ContentType.objects.get_for_model(OSPFInstance)
        rd = Redistribution.objects.create(
            destination_type=ct, destination_id=ospf.pk, source_protocol="connected", source_ref=""
        )
        st = self._redist_state(redistribution=rd)
        d = redistribution_diff(st)
        self.assertTrue(d["linked"])
        self.assertFalse(d["any_diff"])

    def test_linked_diverging_metric_is_flagged(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution

        from netbox_nso_plugin.route_policy_diff import redistribution_diff

        ospf = OSPFInstance.objects.create(device=self.d1, process_id=2, router_id="2.2.2.2")
        ct = ContentType.objects.get_for_model(OSPFInstance)
        rd = Redistribution.objects.create(
            destination_type=ct, destination_id=ospf.pk, source_protocol="connected", source_ref="", metric=10
        )
        st = self._redist_state(redistribution=rd, metric=99)
        d = redistribution_diff(st)
        self.assertTrue(d["any_diff"])
        metric = next(f for f in d["fields"] if f["label"] == "Metric")
        self.assertEqual(metric["device"], "99")
        self.assertEqual(metric["netbox"], "10")
        self.assertTrue(metric["differs"])
