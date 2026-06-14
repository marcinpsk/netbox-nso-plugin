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
        members, set_keys, match_keys = _preflight_constructs("community_list", cl)
        assert members == ["65000:1", "color:0:200"]  # sorted
        assert set_keys == [] and match_keys == []

    def test_route_map_yields_set_and_match_keys(self):
        from netbox_nso_plugin.signals import _preflight_constructs

        rm = self._route_map(
            "CAP-RM",
            set_={"metric_type": "internal", "extcommunity_color": "color:0:5"},
            match={"local_preference": 200},
        )
        members, set_keys, match_keys = _preflight_constructs("route_map", rm)
        assert members == []
        assert set_keys == ["extcommunity_color", "metric_type"]  # sorted union of set-json keys
        assert match_keys == ["local_preference"]

    def test_prefix_list_has_nothing_flaggable(self):
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.signals import _preflight_constructs

        pl = PrefixList.objects.create(name="CAP-PL")
        assert _preflight_constructs("prefix_list", pl) == ([], [], [])


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

    def test_prefix_list_panel_is_supported_without_adapter_call(self):
        from netbox_routing.models import PrefixList

        mgmt = self._mgmt()
        pl = PrefixList.objects.create(name="CAP-PL-PANEL")
        self._attach_overlay("prefix_list", pl, mgmt)
        with patch("netbox_nso_plugin.adapter_client._request", side_effect=AssertionError("must not call adapter")):
            states = self._annotated_states(pl)

        assert states[0].capability["state"] == "supported"
