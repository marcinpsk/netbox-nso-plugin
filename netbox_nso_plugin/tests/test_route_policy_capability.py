# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Route-policy capability pre-flight: construct extraction, attach-time block/override, panel badge.

The adapter's capability matrix is a separate service over HTTP, so the only thing mocked
here is that HTTP boundary (``adapter_client._request``). Everything else runs for real:
the ``preflight_route_policy`` wrapper, the attach view, the overlay save, the panel
template-extension, and the netbox_routing ORM objects the constructs are extracted from.
"""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_nso_plugin.adapter_client import AdapterError

from .mixins import IntentPushResetMixin

TEST_PASSWORD = "capTestPwd!42"


def _request_returning(verdict):
    """A fake adapter ``_request``: preflight path → *verdict*, anything else (intent PUT) → {}."""

    def _inner(method, path, **kwargs):
        if "preflight" in path:
            return verdict
        return {}

    return _inner


def _request_raising_preflight():
    """A fake adapter ``_request`` whose preflight call fails (adapter unreachable)."""

    def _inner(method, path, **kwargs):
        if "preflight" in path:
            raise AdapterError("adapter down", code="nso_unreachable")
        return {}

    return _inner


class _CapBase(IntentPushResetMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="CapMfg", slug="capmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="CapDev", slug="capdev")
        role = DeviceRole.objects.create(name="CapRole", slug="caprole")
        site = Site.objects.create(name="CapSite", slug="capsite")
        cls.device = Device.objects.create(name="cap-router", device_type=dt, role=role, site=site)

    def _mgmt(self, adapter_device_id=194):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="cap-inst", defaults={"adapter_instance_id": "cap-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "cap-dev", "adapter_device_id": adapter_device_id},
        )[0]

    def _community_list(self, name, members):
        from netbox_routing.models import Community, CommunityList, CommunityListEntry

        cl = CommunityList.objects.create(name=name)
        for value in members:
            CommunityListEntry.objects.create(
                community_list=cl, action="permit", community=Community.objects.create(community=value)
            )
        return cl

    def _route_map(self, name, *, set_=None, match=None):
        from netbox_routing.models import RouteMap, RouteMapEntry

        rm = RouteMap.objects.create(name=name)
        RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit", set=set_ or {}, match=match or {})
        return rm


# ── construct extraction ──────────────────────────────────────────────────────


class TestPreflightConstructs(_CapBase):
    def test_community_list_yields_members(self):
        from netbox_nso_plugin.signals import _preflight_constructs

        cl = self._community_list("CAP-CL", ["color:0:200", "65000:1"])
        members, set_keys, match_keys, aspath_names = _preflight_constructs("community_list", cl)
        assert members == ["65000:1", "color:0:200"]  # sorted
        assert set_keys == [] and match_keys == [] and aspath_names == []

    def test_route_map_yields_set_and_match_keys(self):
        from netbox_nso_plugin.signals import _preflight_constructs

        rm = self._route_map(
            "CAP-RM",
            set_={"metric_type": "internal", "extcommunity_color": "color:0:5"},
            match={"local_preference": 200},
        )
        members, set_keys, match_keys, aspath_names = _preflight_constructs("route_map", rm)
        assert members == []
        assert set_keys == ["extcommunity_color", "metric_type"]  # sorted union of set-json keys
        assert match_keys == ["local_preference"]
        assert aspath_names == []

    def test_as_path_yields_its_name(self):
        from netbox_routing.models import ASPath

        from netbox_nso_plugin.signals import _preflight_constructs

        ap = ASPath.objects.create(name="AP-NAMED")
        members, set_keys, match_keys, aspath_names = _preflight_constructs("as_path", ap)
        assert aspath_names == ["AP-NAMED"]
        assert members == [] and set_keys == [] and match_keys == []

    def test_route_map_collects_referenced_as_path_names(self):
        from netbox_routing.models import ASPath, RouteMapEntry

        from netbox_nso_plugin.signals import _preflight_constructs

        rm = self._route_map("CAP-RM-AP")
        ap = ASPath.objects.create(name="55")
        RouteMapEntry.objects.get(route_map=rm).match_aspath.add(ap)
        _members, _set, _match, aspath_names = _preflight_constructs("route_map", rm)
        assert aspath_names == ["55"]

    def test_prefix_list_has_nothing_flaggable(self):
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.signals import _preflight_constructs

        pl = PrefixList.objects.create(name="CAP-PL")
        assert _preflight_constructs("prefix_list", pl) == ([], [], [], [])

    def test_attach_preflight_passes_aspath_names_and_flags(self):
        """Attaching a named as-path runs preflight with aspath_names and returns the flag."""
        from netbox_routing.models import ASPath

        from netbox_nso_plugin.views import NSORoutePolicyAttachView

        mgmt = self._mgmt()
        ap = ASPath.objects.create(name="AP-NAMED")
        verdict = {
            "known": True,
            "fully_supported": False,
            "unsupported": [{"scope": "as-path", "element": "AP-NAMED", "status": "unsupported", "detail": "x"}],
        }
        with patch("netbox_nso_plugin.adapter_client.preflight_route_policy", return_value=verdict) as pf:
            result = NSORoutePolicyAttachView._preflight(mgmt, "as_path", ap)

        assert result["fully_supported"] is False
        # aspath_names (5th positional) carries the as-path's own name
        assert pf.call_args.args[4] == ["AP-NAMED"]


# ── attach-time block / override ──────────────────────────────────────────────


class TestAttachBlockOverride(_CapBase):
    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser(
            username="capadmin", password=TEST_PASSWORD, email="capadmin@test.example"
        )
        self.client.force_login(self.superuser)

    def _attach_url(self):
        return reverse("plugins:netbox_nso_plugin:route_policy_attach", args=[self.device.pk])

    def _policy_value(self, family, obj):
        from django.contrib.contenttypes.models import ContentType

        return f"{family}:{ContentType.objects.get_for_model(obj).pk}:{obj.pk}"

    def test_known_negative_blocks_and_creates_no_overlay(self):
        from netbox_nso_plugin.models import NSORoutePolicyState

        self._mgmt()
        cl = self._community_list("CAP-CL-COLOR", ["color:0:200"])
        verdict = {
            "known": True,
            "fully_supported": False,
            "sw_version": "15.2(4)E10",
            "unsupported": [
                {"scope": "community", "element": "color:0:200", "status": "skipped", "detail": "no IOS home"}
            ],
        }
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_request_returning(verdict)):
            resp = self.client.post(self._attach_url(), {"policy": self._policy_value("community_list", cl)})

        assert resp.status_code == 200  # re-rendered the warning, did NOT redirect
        self.assertContains(resp, "won't apply")
        self.assertContains(resp, "color:0:200")
        assert NSORoutePolicyState.objects.filter(object_name="CAP-CL-COLOR").count() == 0

    def test_override_attaches_despite_known_negative(self):
        from netbox_nso_plugin.models import NSORoutePolicyState

        self._mgmt()
        cl = self._community_list("CAP-CL-OVR", ["color:0:200"])
        verdict = {"known": True, "fully_supported": False, "unsupported": [{"scope": "community", "element": "x"}]}
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_request_returning(verdict)):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    self._attach_url(),
                    {"policy": self._policy_value("community_list", cl), "override": "1"},
                )

        assert resp.status_code == 302  # attached → redirect to the device NSO tab
        row = NSORoutePolicyState.objects.get(object_name="CAP-CL-OVR")
        assert row.status == "accepted"

    def test_fully_supported_attaches_without_block(self):
        from netbox_nso_plugin.models import NSORoutePolicyState

        self._mgmt()
        cl = self._community_list("CAP-CL-OK", ["65000:1"])
        verdict = {"known": True, "fully_supported": True, "unsupported": []}
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_request_returning(verdict)):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self._attach_url(), {"policy": self._policy_value("community_list", cl)})

        assert resp.status_code == 302
        assert NSORoutePolicyState.objects.filter(object_name="CAP-CL-OK", status="accepted").exists()

    def test_adapter_unreachable_fails_open(self):
        """A preflight that errors must NOT block the attach (block only on a KNOWN-negative)."""
        from netbox_nso_plugin.models import NSORoutePolicyState

        self._mgmt()
        cl = self._community_list("CAP-CL-FAILOPEN", ["color:0:200"])
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_request_raising_preflight()):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self._attach_url(), {"policy": self._policy_value("community_list", cl)})

        assert resp.status_code == 302  # not blocked
        assert NSORoutePolicyState.objects.filter(object_name="CAP-CL-FAILOPEN").exists()

    def test_prefix_list_skips_preflight_entirely(self):
        """Prefix-lists carry nothing flaggable → no adapter call, straight attach."""
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        self._mgmt()
        pl = PrefixList.objects.create(name="CAP-PL-NOCALL")
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=AssertionError("must not call adapter")):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self._attach_url(), {"policy": self._policy_value("prefix_list", pl)})

        assert resp.status_code == 302
        assert NSORoutePolicyState.objects.filter(object_name="CAP-PL-NOCALL").exists()

    def test_attach_route_map_warns_cross_device_provenance(self):
        """End-to-end: attaching a route-map whose greenfield reference's NetBox content was
        sourced (materialized) from ANOTHER device owns the reference here (the route-map needs
        it) AND warns the operator that applying pushes that other device's version onto this
        box. Exercises the real view → cascade → message path."""
        from django.contrib.contenttypes.models import ContentType
        from django.contrib.messages import get_messages
        from netbox_routing.models import PrefixList, RouteMap, RouteMapEntry

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push

        mgmt = self._mgmt()  # attaching onto self.device
        rm = RouteMap.objects.create(name="CAP-RM-XDEV")
        e = RouteMapEntry.objects.create(route_map=rm, sequence=10, action="permit")
        pl = PrefixList.objects.create(name="CAP-PL-XDEV")
        with suppress_intent_push():
            e.match_prefix_list.add(pl)

        # A second device already materialized CAP-PL-XDEV into NetBox (it is the NetBox source).
        other_dev = Device.objects.create(
            name="cap-router-x",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        inst, _ = NSOInstance.objects.get_or_create(name="cap-inst", defaults={"adapter_instance_id": "cap-inst"})
        other_mgmt = NSODeviceManagement.objects.create(
            device=other_dev, nso_instance=inst, nso_device_name="cap-dev-x", adapter_device_id=298
        )
        with suppress_intent_push():
            NSORoutePolicyState.objects.create(
                management=other_mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status="in_sync",
                is_materialized=True,
            )

        verdict = {"known": True, "fully_supported": True, "unsupported": []}
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_request_returning(verdict)):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self._attach_url(), {"policy": self._policy_value("route_map", rm)})

        assert resp.status_code == 302  # attached → redirect
        # The route-map and its greenfield prefix-list reference are both owned on this device…
        assert NSORoutePolicyState.objects.filter(
            management=mgmt, object_name="CAP-RM-XDEV", status="accepted"
        ).exists()
        assert NSORoutePolicyState.objects.filter(
            management=mgmt, family="prefix_list", object_name="CAP-PL-XDEV", status="accepted"
        ).exists()
        # …and the operator is warned that the prefix-list's version comes from the other device.
        msgs = [str(m) for m in get_messages(resp.wsgi_request)]
        assert any("sourced from another device" in m and "cap-router-x" in m for m in msgs), msgs


# ── panel badge ───────────────────────────────────────────────────────────────


class TestPanelCapabilityBadge(_CapBase):
    def _attach_overlay(self, family, obj, mgmt):
        from django.contrib.contenttypes.models import ContentType

        from netbox_nso_plugin.models import NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push

        with suppress_intent_push():
            return NSORoutePolicyState.objects.create(
                management=mgmt,
                family=family,
                object_name=obj.name,
                content_type=ContentType.objects.get_for_model(obj),
                object_id=obj.pk,
                status="accepted",
            )

    def _annotated_states(self, obj):
        from netbox_nso_plugin.template_content import RoutePolicyNSODevices

        ext = object.__new__(RoutePolicyNSODevices)
        ext.context = {"object": obj}
        captured = {}
        ext.render = lambda template, extra_context=None: captured.update(extra_context or {}) or ""
        ext.full_width_page()
        return captured["nso_states"]

    def test_partial_verdict_marks_state_partial(self):
        mgmt = self._mgmt()
        cl = self._community_list("CAP-CL-PANEL", ["color:0:200"])
        self._attach_overlay("community_list", cl, mgmt)
        verdict = {
            "known": True,
            "fully_supported": False,
            "unsupported": [{"scope": "community", "element": "color:0:200", "status": "skipped", "detail": ""}],
        }
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_request_returning(verdict)):
            states = self._annotated_states(cl)

        assert len(states) == 1
        assert states[0].capability["state"] == "partial"
        assert states[0].capability["unsupported"][0]["element"] == "color:0:200"

    def test_unknown_device_when_adapter_says_not_known(self):
        mgmt = self._mgmt()
        cl = self._community_list("CAP-CL-UNK", ["color:0:200"])
        self._attach_overlay("community_list", cl, mgmt)
        with patch(
            "netbox_nso_plugin.adapter_client._request",
            side_effect=_request_returning({"known": False, "fully_supported": True, "unsupported": []}),
        ):
            states = self._annotated_states(cl)

        assert states[0].capability["state"] == "unknown"

    def test_uncovered_platform_marks_state_unassessed(self):
        """A Junos/Nokia verdict (coverage_unknown) shows 'not assessed', not green 'supported'."""
        mgmt = self._mgmt()
        cl = self._community_list("CAP-CL-JUNOS", ["color:0:200"])
        self._attach_overlay("community_list", cl, mgmt)
        verdict = {"known": True, "fully_supported": True, "unsupported": [], "coverage_unknown": True}
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_request_returning(verdict)):
            states = self._annotated_states(cl)

        assert states[0].capability["state"] == "unassessed"

    def test_adapter_failure_short_circuits_remaining_rows(self):
        """A route-policy detail render must not pay a preflight timeout per device row: the FIRST
        adapter failure trips a circuit breaker so the remaining rows degrade to 'unknown' without
        further adapter calls."""
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        cl = self._community_list("CAP-CL-CB", ["color:0:200"])
        mgmt1 = self._mgmt(adapter_device_id=201)
        dev2 = Device.objects.create(
            name="cap-router-2", device_type=self.device.device_type, role=self.device.role, site=self.device.site
        )
        inst = NSOInstance.objects.get(name="cap-inst")
        mgmt2 = NSODeviceManagement.objects.create(
            device=dev2, nso_instance=inst, nso_device_name="cap-dev-2", adapter_device_id=202
        )
        self._attach_overlay("community_list", cl, mgmt1)
        self._attach_overlay("community_list", cl, mgmt2)

        with patch(
            "netbox_nso_plugin.adapter_client.preflight_route_policy",
            side_effect=AdapterError("down", code="nso_unreachable"),
        ) as pf:
            states = self._annotated_states(cl)

        self.assertEqual(len(states), 2)
        self.assertEqual(pf.call_count, 1)  # circuit breaker: one call total, not one per row
        self.assertTrue(all(s.capability["state"] == "unknown" for s in states))

    def test_prefix_list_panel_is_supported_without_adapter_call(self):
        from netbox_routing.models import PrefixList

        mgmt = self._mgmt()
        pl = PrefixList.objects.create(name="CAP-PL-PANEL")
        self._attach_overlay("prefix_list", pl, mgmt)
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=AssertionError("must not call adapter")):
            states = self._annotated_states(pl)

        assert states[0].capability["state"] == "supported"

    def test_propagation_devices_lists_only_owned_overlays(self):
        """Edit-propagation surface: the panel reports which devices an edit re-applies to —
        only the OWNED overlays (accepted / deploying / in_sync / apply_failed). A brownfield
        (imported) overlay is excluded: editing the shared object doesn't auto-push it (it
        surfaces via reconcile), so the operator's blast-radius view must not list it."""
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSORoutePolicyState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.template_content import RoutePolicyNSODevices

        pl = PrefixList.objects.create(name="PROP-PL")
        owned_mgmt = self._mgmt()  # self.device → owned overlay (accepted)
        self._attach_overlay("prefix_list", pl, owned_mgmt)

        # A second device carries a brownfield (imported) overlay → NOT in the propagation set.
        other_dev = Device.objects.create(
            name="cap-router-2",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        inst, _ = NSOInstance.objects.get_or_create(name="cap-inst", defaults={"adapter_instance_id": "cap-inst"})
        other_mgmt = NSODeviceManagement.objects.create(
            device=other_dev, nso_instance=inst, nso_device_name="cap-dev-2", adapter_device_id=295
        )
        with suppress_intent_push():
            NSORoutePolicyState.objects.create(
                management=other_mgmt,
                family="prefix_list",
                object_name=pl.name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=pl.pk,
                status="imported",
            )

        ext = object.__new__(RoutePolicyNSODevices)
        ext.context = {"object": pl}
        captured = {}
        ext.render = lambda template, extra_context=None: captured.update(extra_context or {}) or ""
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=AssertionError("must not call adapter")):
            ext.full_width_page()

        assert [d.name for d in captured["propagation_devices"]] == [self.device.name]  # owned only
        assert len(captured["nso_states"]) == 2  # both devices still listed in the table


# ── operator capabilities page ────────────────────────────────────────────────


class TestCapabilitiesPage(_CapBase):
    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser(
            username="capadmin2", password=TEST_PASSWORD, email="capadmin2@test.example"
        )
        self.client.force_login(self.superuser)

    def _url(self):
        return reverse("plugins:netbox_nso_plugin:route_policy_capabilities", args=[self.device.pk])

    def test_page_lists_grouped_capability_rows(self):
        self._mgmt()
        capability_payload = {
            "known": True,
            "ned_id": "cisco-ios-cli-6.114",
            "sw_version": "15.2(4)E10",
            "elements": [
                {
                    "scope": "community",
                    "name": "color:0:128",
                    "status": "skipped",
                    "detail": "no IOS home",
                    "source": "probe",
                },
                {
                    "scope": "rm-set",
                    "name": "set extcommunity color",
                    "status": "unsupported",
                    "detail": "% Invalid input",
                    "source": "apply",
                },
                {"scope": "rm-set", "name": "set metric-type", "status": "native", "detail": "", "source": "probe"},
            ],
        }
        with patch("netbox_nso_plugin.adapter_client._request", return_value=capability_payload):
            resp = self.client.get(self._url())

        assert resp.status_code == 200
        self.assertContains(resp, "15.2(4)E10")
        self.assertContains(resp, "set extcommunity color")
        self.assertContains(resp, "unsupported")
        self.assertContains(resp, "<strong>2</strong>")  # flagged count (skipped + unsupported)
        self.assertContains(resp, "of 3 construct")  # summary line rendered

    def test_page_get_is_cache_only(self):
        """The browsable GET must read cache-only (refresh=false → no ?refresh=true on the call)."""
        self._mgmt()
        seen = {}

        def _capture(method, path, **kwargs):
            seen["path"] = path
            return {"known": True, "ned_id": "n", "sw_version": "s", "elements": []}

        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_capture):
            self.client.get(self._url())

        assert "refresh=true" not in seen["path"]

    def test_check_now_post_forces_refresh_then_redirects(self):
        self._mgmt()
        seen = {}

        def _capture(method, path, **kwargs):
            seen["path"] = path
            return {"known": True, "ned_id": "n", "sw_version": "s", "elements": []}

        with patch("netbox_nso_plugin.adapter_client._request", side_effect=_capture):
            resp = self.client.post(self._url())

        assert resp.status_code == 302
        assert "refresh=true" in seen["path"]  # POST = authoritative probe

    def test_unknown_device_renders_check_now_prompt(self):
        self._mgmt()
        with patch(
            "netbox_nso_plugin.adapter_client._request",
            return_value={"known": False, "ned_id": "", "sw_version": "", "elements": []},
        ):
            resp = self.client.get(self._url())

        assert resp.status_code == 200
        self.assertContains(resp, "never been probed")

    def test_read_support_rows_render_in_their_own_section(self):
        """source='read' rows (H3: per-scope read-support fed by the live read probe) render in
        a dedicated Read-support table — split out of the route-policy construct groups and
        excluded from the flagged-construct count."""
        self._mgmt()
        payload = {
            "known": True,
            "ned_id": "arcos-v8.1.2X-nc-1.0",
            "sw_version": "",
            "elements": [
                {"scope": "rm-set", "name": "set metric-type", "status": "native", "detail": "", "source": "probe"},
                {
                    "scope": "community",
                    "name": "color:0:128",
                    "status": "skipped",
                    "detail": "no home",
                    "source": "probe",
                },
                {
                    "scope": "bgp",
                    "name": "read",
                    "status": "native",
                    "detail": "read 11 item(s) on rg03",
                    "source": "read",
                },
                {
                    "scope": "isis",
                    "name": "read",
                    "status": "unknown",
                    "detail": "reads empty on rg03",
                    "source": "read",
                },
                {
                    "scope": "vlan",
                    "name": "read",
                    "status": "skipped",
                    "detail": "not applicable on this platform",
                    "source": "read",
                },
                {
                    "scope": "ospf",
                    "name": "read",
                    "status": "unsupported",
                    "detail": "read raised: boom",
                    "source": "read",
                },
            ],
        }
        with patch("netbox_nso_plugin.adapter_client._request", return_value=payload):
            resp = self.client.get(self._url())

        assert resp.status_code == 200
        self.assertContains(resp, "Read support")
        self.assertContains(resp, "readable")  # the native read row's badge
        self.assertContains(resp, "unconfirmed")  # the unknown read row's badge
        self.assertContains(resp, "of 4 probed scope")  # read summary counts the 4 read rows
        # the construct summary counts ONLY the probe/apply constructs: 1 flagged of 2
        self.assertContains(resp, "of 2 construct")

    def test_uncovered_platform_shows_not_assessed_banner(self):
        """A Junos/Nokia device (coverage marker) shows the 'not yet assessed' banner."""
        self._mgmt()
        payload = {
            "known": True,
            "coverage_unknown": True,
            "ned_id": "juniper-junos-nc-4.19",
            "sw_version": "24.4R2",
            "elements": [
                {
                    "scope": "coverage",
                    "name": "juniper-junos-nc-4.19",
                    "status": "unknown",
                    "detail": "not yet implemented",
                    "source": "probe",
                }
            ],
        }
        with patch("netbox_nso_plugin.adapter_client._request", return_value=payload):
            resp = self.client.get(self._url())

        assert resp.status_code == 200
        self.assertContains(resp, "not yet assessed")
        self.assertContains(resp, "juniper-junos-nc-4.19")
