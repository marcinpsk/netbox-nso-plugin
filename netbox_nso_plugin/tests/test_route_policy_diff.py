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


class TestInlineTokenDiff(TestCase):
    """inline_token_diff isolates the exact tokens that differ within one value (pure)."""

    def test_extra_token_on_each_side_is_isolated(self):
        from netbox_nso_plugin.route_policy_diff import inline_token_diff

        # device has an extra ASN (1239), NetBox has an extra ASN (15169); the rest matches.
        dev_segs, nb_segs = inline_token_diff("174|701|1239|1273|12956", "174|701|1273|12956|15169")
        dev_changed = "".join(s["text"] for s in dev_segs if s["changed"])
        nb_changed = "".join(s["text"] for s in nb_segs if s["changed"])
        self.assertIn("1239", dev_changed)  # the device-only ASN is highlighted on the device side
        self.assertNotIn("15169", dev_changed)
        self.assertIn("15169", nb_changed)  # the netbox-only ASN is highlighted on the NetBox side
        self.assertNotIn("1239", nb_changed)
        # the shared ASNs are NOT highlighted on either side
        self.assertNotIn("12956", dev_changed)
        self.assertNotIn("701", nb_changed)

    def test_segments_reconstruct_the_original_strings(self):
        """Joining all segments back together must reproduce each side verbatim (no data loss)."""
        from netbox_nso_plugin.route_policy_diff import inline_token_diff

        dev = ".* (174|701|1239|12956) .*"
        nb = ".* (174|701|12956|15169) .*"
        dev_segs, nb_segs = inline_token_diff(dev, nb)
        self.assertEqual("".join(s["text"] for s in dev_segs), dev)
        self.assertEqual("".join(s["text"] for s in nb_segs), nb)

    def test_identical_values_have_no_changed_segments(self):
        from netbox_nso_plugin.route_policy_diff import inline_token_diff

        dev_segs, nb_segs = inline_token_diff("65000:1, 65000:2", "65000:1, 65000:2")
        self.assertFalse(any(s["changed"] for s in dev_segs))
        self.assertFalse(any(s["changed"] for s in nb_segs))


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
        cap = _rm(
            "RM", [_entry(10, match={}, set_={"as_path_replace": "AS64510-EXAMPLE"}, match_prefix_lists=["RPKI"])]
        )["route_maps"][0]
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

    def test_unmaterialised_row_has_no_diff(self):
        """A row with no linked NetBox object yet returns None (nothing to compare)."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff

        mgmt = self._mgmt(self.d1)
        st = NSORoutePolicyState.objects.create(
            management=mgmt, family="prefix_list", object_name="PL-NONE", status="imported"
        )
        self.assertIsNone(route_policy_state_diff(st))


class TestDiffViews(_RPBase):
    """The diff pages render (login-gated) and surface the concrete delta."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="diff-op", password="pw")  # noqa: S106
        self.client.force_login(user)

    def _diverged_state(self, name):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, _rm(name, [_entry(10, set_={"local_preference": 100})]))
        reconcile_route_policy(self.d2, _rm(name, [_entry(10, set_={"local_preference": 200})]))
        return NSORoutePolicyState.objects.get(management__device=self.d2, object_name=name)

    def test_route_policy_diff_page_uses_shared_diff_viewer(self):
        from django.urls import reverse

        s2 = self._diverged_state("RM-V")

        url = reverse("plugins:netbox_nso_plugin:routing_route_policy_diff", kwargs={"pk": s2.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("data-nso-diff-viewer", body)
        self.assertIn("diff2html.min.js", body)
        self.assertIn("local_preference=200", body)
        self.assertIn("local_preference=100", body)
        self.assertNotIn('<th style="width:8rem;">Seq</th>', body)

    def test_versions_diff_opens_in_the_shared_htmx_modal(self):
        from django.urls import reverse

        state = self._diverged_state("RM-MODAL-LINK")
        versions_url = reverse("plugins:netbox_nso_plugin:routing_route_policy_versions", kwargs={"pk": state.pk})
        diff_url = reverse("plugins:netbox_nso_plugin:routing_route_policy_diff", kwargs={"pk": state.pk})

        response = self.client.get(versions_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="nso-diff-modal"')
        self.assertContains(response, 'id="nso-diff-modal-content"')
        self.assertContains(response, f'hx-get="{diff_url}"')
        self.assertContains(response, 'hx-target="#nso-diff-modal-content"')
        self.assertContains(response, 'data-bs-target="#nso-diff-modal"')
        self.assertNotContains(response, "hx-push-url")

    def test_htmx_diff_request_returns_closeable_modal_fragment(self):
        from django.urls import reverse

        state = self._diverged_state("RM-MODAL-BODY")
        diff_url = reverse("plugins:netbox_nso_plugin:routing_route_policy_diff", kwargs={"pk": state.pk})

        response = self.client.get(diff_url, headers={"HX-Request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="modal-header"')
        self.assertContains(response, 'data-bs-dismiss="modal"')
        self.assertContains(response, "data-nso-diff-viewer")
        self.assertNotContains(response, "<html")
        self.assertNotContains(response, "Back to")

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

    def test_redistribution_diff_page_uses_shared_diff_viewer(self):
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
        self.assertIn("data-nso-diff-viewer", body)
        self.assertIn("diff2html.min.js", body)
        self.assertIn("99", body)
        self.assertIn("10", body)
        self.assertNotIn('<th style="width:12rem;">Field</th>', body)


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

    def test_unified_diff_uses_canonical_device_and_netbox_text(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution

        from netbox_nso_plugin.route_policy_diff import unified_redistribution_diff

        ospf = OSPFInstance.objects.create(device=self.d1, process_id=9, router_id="192.0.2.9")
        ct = ContentType.objects.get_for_model(OSPFInstance)
        rd = Redistribution.objects.create(
            destination_type=ct, destination_id=ospf.pk, source_protocol="connected", source_ref="", metric=10
        )
        st = self._redist_state(redistribution=rd, metric=99)

        text = unified_redistribution_diff(st)

        self.assertIn("--- redistribute connected into ospf 1", text)
        self.assertIn("+++ redistribute connected into ospf 1", text)
        self.assertIn("-metric: 99", text)
        self.assertIn("+metric: 10", text)

    def test_removed_on_device_shows_removal_drift(self):
        """The disjoint the status already knew about: a row the device no longer reports
        (device_present=False) is a real drift even though its stale stored fields still match
        the object. The diff must say 'removed', not 'no drift'."""
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import OSPFInstance, Redistribution

        from netbox_nso_plugin.route_policy_diff import redistribution_diff

        ospf = OSPFInstance.objects.create(device=self.d1, process_id=3, router_id="3.3.3.3")
        ct = ContentType.objects.get_for_model(OSPFInstance)
        rd = Redistribution.objects.create(
            destination_type=ct, destination_id=ospf.pk, source_protocol="connected", source_ref="", metric=10
        )
        # Stored device metric deliberately EQUALS the object (the phantom case), but the device
        # removed the entry → device_present False.
        st = self._redist_state(redistribution=rd, metric=10, status="changed", device_present=False)
        d = redistribution_diff(st)
        self.assertTrue(d["any_diff"])  # agrees with status=changed
        self.assertTrue(d["removed_on_device"])
        metric = next(f for f in d["fields"] if f["label"] == "Metric")
        self.assertEqual(metric["device"], "removed")
        self.assertTrue(metric["differs"])


class TestRouteMapRemovalDiff(_RPBase):
    def test_removed_route_map_shows_removal_drift(self):
        """A route-map the device removed (device_present=False) reports drift even though its
        stale captured still matches the materialized object — the diff shows removal, the
        NetBox entries reading 'only in NetBox', agreeing with the changed status."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        reconcile_route_policy(self.d1, _rm("RM-REM", [_entry(10, set_={"local_preference": 100})]))
        st = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-REM")
        # device stopped reporting it (what the reconciler stale loop records)
        st.device_present = False
        st.save(update_fields=["device_present"])

        d = route_policy_state_diff(st)
        self.assertTrue(d["any_diff"])
        self.assertTrue(d["removed_on_device"])
        self.assertTrue(any(e["presence"] == "netbox_only" for e in d["entries"]))


class TestSimpleFamilyDiff(_RPBase):
    """The diff now covers prefix-list / as-path / community-list, not just route-maps."""

    def test_prefix_list_matching_reconstruction_no_diff(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        reconcile_route_policy(
            self.d1,
            {
                "prefix_lists": [
                    {"name": "PLD", "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}]}
                ]
            },
        )
        st = NSORoutePolicyState.objects.get(family="prefix_list", object_name="PLD")
        d = route_policy_state_diff(st)
        self.assertFalse(d["any_diff"], d)  # device capture reconstructs to the NetBox object

    def test_prefix_list_cross_device_diff(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(
            self.d1,
            {
                "prefix_lists": [
                    {"name": "PLX", "entries": [{"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"}]}
                ]
            },
        )
        reconcile_route_policy(
            self.d2,
            {
                "prefix_lists": [
                    {
                        "name": "PLX",
                        "entries": [
                            {"sequence": 10, "action": "permit", "prefix": "10.0.0.0/8"},
                            {"sequence": 20, "action": "permit", "prefix": "192.168.0.0/16"},
                        ],
                    }
                ]
            },
        )
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PLX")
        self.assertEqual(s2.status, "conflict")
        d = route_policy_state_diff(s2)
        self.assertTrue(d["any_diff"])
        extra = next(e for e in d["entries"] if e["presence"] == "device_only")
        self.assertTrue(any(f["device"] == "192.168.0.0/16" for f in extra["fields"]))

    def test_inserted_entry_does_not_cascade_as_all_changed(self):
        """A single entry NetBox has that the device lacks shows as ONE 'only in NetBox' row —
        not a cascade of 'changed' rows. This is the seq-shift trap: insert/remove one entry
        (e.g. a Junos term) in the MIDDLE and a naive position-by-position diff reds the whole
        tail. Content alignment must absorb the shift so only the real difference shows."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)

        def pl(prefixes):
            entries = [{"sequence": 10 * (i + 1), "action": "permit", "prefix": p} for i, p in enumerate(prefixes)]
            return {"prefix_lists": [{"name": "PL-SHIFT", "entries": entries}]}

        # NetBox (owner) has an extra 0.0.0.0/0 in the MIDDLE; the device's other three match.
        reconcile_route_policy(self.d1, pl(["10.0.0.0/8", "0.0.0.0/0", "172.16.0.0/12", "192.168.0.0/16"]))
        reconcile_route_policy(self.d2, pl(["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]))
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-SHIFT")
        d = route_policy_state_diff(s2)

        differing = [e for e in d["entries"] if e["differs"]]
        self.assertEqual(len(differing), 1)  # ONLY the inserted entry — no positional cascade
        self.assertEqual(differing[0]["presence"], "netbox_only")
        prefix = next(f for f in differing[0]["fields"] if f["label"] == "Prefix")
        self.assertEqual(prefix["netbox"], "0.0.0.0/0")

    def test_community_list_invert_in_extra(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(
            self.d1,
            {"community_lists": [{"name": "CLX", "invert_match": False, "entries": [{"community": "65000:1"}]}]},
        )
        reconcile_route_policy(
            self.d2,
            {"community_lists": [{"name": "CLX", "invert_match": True, "entries": [{"community": "65000:1"}]}]},
        )
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="CLX")
        self.assertEqual(s2.status, "conflict")
        d = route_policy_state_diff(s2)
        invert = next(x for x in d["extra"] if x["label"] == "Invert match")
        self.assertTrue(invert["differs"])

    def test_as_path_cross_device_diff(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(
            self.d1, {"as_paths": [{"name": "APX", "entries": [{"action": "permit", "pattern": "^65000_"}]}]}
        )
        reconcile_route_policy(
            self.d2, {"as_paths": [{"name": "APX", "entries": [{"action": "permit", "pattern": "^65001_"}]}]}
        )
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="APX")
        self.assertEqual(s2.status, "conflict")
        d = route_policy_state_diff(s2)
        self.assertTrue(d["any_diff"])
        row = next(f for e in d["entries"] for f in e["fields"] if f["label"] == "Pattern")
        self.assertEqual(row["device"], "^65001_")
        self.assertEqual(row["netbox"], "^65000_")

    def test_as_path_diff_highlights_exact_differing_asn(self):
        """End-to-end: a Pattern that differs by ONE ASN carries inline segments isolating just
        that ASN — device side highlights the device-only ASN, NetBox side the netbox-only one,
        so the diff page can highlight the exact token instead of the whole regex."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        dev_pat = ".* (174|701|1239|12956) .*"  # device has 1239
        nb_pat = ".* (174|701|12956|15169) .*"  # NetBox has 15169
        reconcile_route_policy(self.d1, {"as_paths": [{"name": "APT", "entries": [{"pattern": nb_pat}]}]})
        reconcile_route_policy(self.d2, {"as_paths": [{"name": "APT", "entries": [{"pattern": dev_pat}]}]})
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="APT")
        d = route_policy_state_diff(s2)
        row = next(f for e in d["entries"] for f in e["fields"] if f["label"] == "Pattern")
        self.assertTrue(row["differs"])
        dev_changed = "".join(s["text"] for s in row["device_segments"] if s["changed"])
        nb_changed = "".join(s["text"] for s in row["netbox_segments"] if s["changed"])
        self.assertIn("1239", dev_changed)  # device-only ASN highlighted on device side
        self.assertIn("15169", nb_changed)  # netbox-only ASN highlighted on NetBox side
        self.assertNotIn("12956", dev_changed)  # shared ASN not highlighted
        # segments reproduce the full patterns (nothing dropped in rendering)
        self.assertEqual("".join(s["text"] for s in row["device_segments"]), dev_pat)
        self.assertEqual("".join(s["text"] for s in row["netbox_segments"]), nb_pat)

    def test_simple_family_removal_shows_removed(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import route_policy_state_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        reconcile_route_policy(
            self.d1,
            {"as_paths": [{"name": "APR", "entries": [{"action": "permit", "pattern": "^65000_"}]}]},
        )
        st = NSORoutePolicyState.objects.get(family="as_path", object_name="APR")
        st.device_present = False
        st.save(update_fields=["device_present"])
        d = route_policy_state_diff(st)
        self.assertTrue(d["removed_on_device"])
        self.assertTrue(d["any_diff"])

    def test_as_path_diff_page_renders(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        user = get_user_model().objects.create_user(username="apdiff", password="pw")  # noqa: S106
        self.client.force_login(user)
        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(
            self.d1, {"as_paths": [{"name": "APV", "entries": [{"action": "permit", "pattern": "^65000_"}]}]}
        )
        reconcile_route_policy(
            self.d2, {"as_paths": [{"name": "APV", "entries": [{"action": "permit", "pattern": "^65001_"}]}]}
        )
        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="APV")
        url = reverse("plugins:netbox_nso_plugin:routing_route_policy_diff", kwargs={"pk": s2.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("data-nso-diff-viewer", body)
        self.assertIn("-permit ^65001_", body)
        self.assertIn("+permit ^65000_", body)


class TestUnifiedPolicyDiff(_RPBase):
    """unified_policy_diff (#91): ONE canonical pretty-printer renders BOTH sides,
    difflib unified-diffs them — diff2html-ready (real ---/+++/@@ headers), and
    sequence-free so the reconciler's renumbering never reads as drift."""

    def _diverged_route_map_state(self, name="RM-U"):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(self.d1, _rm(name, [_entry(10, set_={"local_preference": 100})]))
        reconcile_route_policy(self.d2, _rm(name, [_entry(10, set_={"local_preference": 200})]))
        return NSORoutePolicyState.objects.get(management__device=self.d2, object_name=name)

    def test_route_map_delta_produces_real_unified_hunk(self):
        from netbox_nso_plugin.route_policy_diff import unified_policy_diff

        text = unified_policy_diff(self._diverged_route_map_state())
        # both headers carry the SAME label (a from/to mismatch makes diff2html flag "RENAMED")
        self.assertIn("--- route-map RM-U", text)
        self.assertIn("+++ route-map RM-U", text)
        self.assertRegex(text, r"@@ -\d+(,\d+)? \+\d+(,\d+)? @@")  # a REAL hunk header (diff2html requires it)
        self.assertIn("-  set: local_preference=200", text)  # d2 on-box
        self.assertIn("+  set: local_preference=100", text)  # materialised owner

    def test_renumbered_identical_content_yields_empty_diff(self):
        """Device sequences (10/20) vs the reconciler's renumbered materialisation (1..N)
        must NOT read as drift — the canonical text is sequence-free."""
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import unified_policy_diff
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        payload = _rm(
            "RM-SEQ",
            [
                _entry(10, set_={"local_preference": 150}, match_prefix_lists=["PL-A"]),
                _entry(20, action="deny", set_={"as_path_replace": "AS65000"}),
            ],
        )
        payload["prefix_lists"] = [{"name": "PL-A", "entries": []}]
        reconcile_route_policy(self.d1, payload)
        st = NSORoutePolicyState.objects.get(family="route_map", object_name="RM-SEQ")
        self.assertEqual(unified_policy_diff(st), "")

    def test_prefix_list_line_delta(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_reconciler import reconcile_route_policy

        self._mgmt(self.d1)
        self._mgmt(self.d2)
        reconcile_route_policy(
            self.d1,
            {"prefix_lists": [{"name": "PL-U", "entries": [{"action": "permit", "prefix": "10.0.0.0/8", "ge": 16}]}]},
        )
        reconcile_route_policy(
            self.d2,
            {"prefix_lists": [{"name": "PL-U", "entries": [{"action": "permit", "prefix": "10.0.0.0/8", "ge": 24}]}]},
        )
        from netbox_nso_plugin.route_policy_diff import unified_policy_diff

        s2 = NSORoutePolicyState.objects.get(management__device=self.d2, object_name="PL-U")
        text = unified_policy_diff(s2)
        self.assertIn("-permit 10.0.0.0/8 ge 24", text)  # d2 on-box
        self.assertIn("+permit 10.0.0.0/8 ge 16", text)  # materialised owner

    def test_unmaterialised_state_renders_empty(self):
        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.route_policy_diff import unified_policy_diff

        mgmt = self._mgmt(self.d1)
        st = NSORoutePolicyState.objects.create(
            management=mgmt, family="prefix_list", object_name="PL-NONE-U", status="imported"
        )
        self.assertEqual(unified_policy_diff(st), "")

    def test_diff_view_embeds_two_panel(self):
        """The drift page ships the unified diff text + the diff2html container/assets."""
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        user = get_user_model().objects.create_user(username="diff-2panel", password="pw")  # noqa: S106
        self.client.force_login(user)
        st = self._diverged_route_map_state(name="RM-2P")
        resp = self.client.get(reverse("plugins:netbox_nso_plugin:routing_route_policy_diff", kwargs={"pk": st.pk}))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("data-nso-diff-viewer", body)
        self.assertIn('id="nso-diff-text"', body)
        self.assertIn("vendor/diff2html.min.js", body)
