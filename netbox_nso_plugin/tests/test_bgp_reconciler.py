# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for A4: adapter_client.get_bgp_config and _reconcile_bgp_config."""

import copy
import unittest
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from ._adapter_http import make_session
from ._outbox_case import content_update
from .mixins import IntentPushResetMixin

_BASE_CFG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


# ---------------------------------------------------------------------------
# adapter_client.get_bgp_config — unit tests (no Django DB)
# ---------------------------------------------------------------------------


class TestGetBgpConfig(unittest.TestCase):
    """Tests for adapter_client.get_bgp_config()."""

    def _make_session(self, status=200, json_data=None):
        return make_session(status_code=status, json_data=json_data)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_calls_expected_endpoint(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_bgp_config

        session = self._make_session(json_data={"device_id": 7, "routers": []})
        mock_session_cls.return_value = session

        get_bgp_config(7)

        args, _ = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://adapter.local/api/v1/devices/7/bgp-config")

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_returns_full_payload(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_bgp_config

        payload = {
            "device_id": 7,
            "routers": [{"asn": "65100", "scopes": []}],
        }
        session = self._make_session(json_data=payload)
        mock_session_cls.return_value = session

        result = get_bgp_config(7)
        self.assertEqual(result, payload)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_404_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        # READSEM S4 D4: 404 raises even without an ErrorEnvelope body (code "404").
        from netbox_nso_plugin.adapter_client import AdapterError, get_bgp_config

        session = self._make_session(status=404, json_data={})
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError):
            get_bgp_config(7)

    @patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_BASE_CFG)
    @patch("netbox_nso_plugin.adapter_client.requests.Session")
    def test_500_raises_adapter_error(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import AdapterError, get_bgp_config

        session = self._make_session(
            status=500,
            json_data={"error": {"code": "internal_error", "message": "boom"}},
        )
        mock_session_cls.return_value = session

        with self.assertRaises(AdapterError):
            get_bgp_config(7)


# ---------------------------------------------------------------------------
# _reconcile_bgp_config — integration tests (real Django DB)
# ---------------------------------------------------------------------------


def _make_bgp_device(suffix="bgp"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"BgpMfg{suffix}", slug=f"bgpmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"BgpDev{suffix}", slug=f"bgpdev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"BgpRole{suffix}", slug=f"bgprole{suffix}")
    site, _ = Site.objects.get_or_create(name=f"BgpSite{suffix}", slug=f"bgpsite{suffix}")
    return Device.objects.create(name=f"bgp-router-{suffix}", device_type=dt, role=role, site=site)


class TestReconcileBgpConfig(IntentPushResetMixin, TestCase):
    """Integration tests for _reconcile_bgp_config() — real Django DB."""

    @classmethod
    def setUpTestData(cls):
        cls.device = _make_bgp_device("main")

    def _make_mgmt(self, device=None):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = device or self.device
        inst, _ = NSOInstance.objects.get_or_create(
            name="bgp-test-inst",
            defaults={"adapter_instance_id": "bgp-test-inst"},
        )
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "bgp-dev",
                "adapter_device_id": device.pk,
            },
        )[0]

    def _peer_entry(self, peer_address="10.0.0.2", remote_as="65200", **kwargs):
        entry = {
            "peer_address": peer_address,
            "enabled": True,
            "address_families": [{"af": "ipv4-unicast", "enabled": True}],
            "remote_as": remote_as,
        }
        entry.update(kwargs)
        return entry

    def _router_payload(self, asn="65100", vrf="", peers=None, address_families=None, router_id=None):
        return {
            "asn": asn,
            "router_id": router_id,
            "scopes": [
                {
                    "vrf": vrf,
                    "address_families": address_families if address_families is not None else ["ipv4-unicast"],
                    "peers": peers if peers is not None else [],
                    "peer_groups": [],
                }
            ],
        }

    def _payload(self, *routers):
        return {"device_id": self.device.pk, "routers": list(routers)}

    # ── Basic cases ────────────────────────────────────────────────────────────

    def test_preflight_plan_freezes_complete_bgp_graph_without_writes(self):
        """The BGP preflight includes every graph row before it changes the database."""
        self._make_mgmt()

        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import (
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPPeerTemplate,
            BGPRouter,
            BGPScope,
        )

        from netbox_nso_plugin.bgp_reconciler import bgp_reconcile_plan
        from netbox_nso_plugin.models import NSOBGPPeerState, NSOBGPPeerTemplateState

        payload = self._router_payload(
            peers=[self._peer_entry(peer_group="EDGE")],
            router_id="198.18.0.1",
        )
        payload["scopes"][0]["peer_groups"] = [
            {
                "name": "EDGE",
                "remote_as": "65200",
                "address_families": [{"af": "ipv4-unicast", "enabled": True}],
            }
        ]

        plan = bgp_reconcile_plan(self.device, self._payload(payload))

        labels = [write.model_label for write in plan.write_set if write.operation == "save"]
        self.assertEqual(
            set(labels),
            {
                "ipam.asn",
                "ipam.ipaddress",
                "ipam.rir",
                "netbox_routing.bgpaddressfamily",
                "netbox_routing.bgppeer",
                "netbox_routing.bgppeeraddressfamily",
                "netbox_routing.bgppeertemplate",
                "netbox_routing.bgprouter",
                "netbox_routing.bgpscope",
                "netbox_nso_plugin.nsobgppeerstate",
                "netbox_nso_plugin.nsobgppeertemplatestate",
            },
        )
        for model in (
            ASN,
            RIR,
            IPAddress,
            BGPAddressFamily,
            BGPPeer,
            BGPPeerAddressFamily,
            BGPPeerTemplate,
            BGPRouter,
            BGPScope,
            NSOBGPPeerState,
            NSOBGPPeerTemplateState,
        ):
            self.assertFalse(model.objects.exists(), model._meta.label_lower)

    def test_preflight_filters_shared_dependency_prefetches_to_the_payload(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from ipam.models import ASN, VRF
        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import bgp_reconcile_plan

        self._make_mgmt()
        payload = self._payload(
            self._router_payload(
                asn="64530",
                vrf="PAYLOAD-VRF",
                peers=[self._peer_entry("198.18.0.32", remote_as="64531", peer_group="PAYLOAD-TEMPLATE")],
            )
        )

        with CaptureQueriesContext(connection) as captured:
            bgp_reconcile_plan(self.device, payload)

        sql = [query["sql"] for query in captured.captured_queries]
        for model in (ASN, VRF, BGPPeerTemplate):
            table = model._meta.db_table
            prefetches = [query for query in sql if query.startswith("SELECT") and f'FROM "{table}"' in query]
            self.assertEqual(len(prefetches), 1)
            self.assertIn(" WHERE ", prefetches[0])

    def test_no_mgmt_returns_empty(self):
        """Device without NSODeviceManagement → empty list, no crash."""
        orphan = _make_bgp_device("orphan")

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        result = _reconcile_bgp_config(orphan, self._payload())
        self.assertEqual(result, [])

    def test_empty_payload_returns_empty(self):
        """Empty routers list → no state rows created."""
        self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        result = _reconcile_bgp_config(self.device, self._payload())
        self.assertEqual(result, [])

    def test_malformed_router_entry_is_a_typed_adapter_error(self):
        """A malformed router fails at the adapter boundary without changing stored BGP."""
        self._make_mgmt()

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry()])),
        )
        state = NSOBGPPeerState.objects.get()
        original = (state.pk, state.status, state.bgp_peer_id, state.device_base_hash)

        with self.assertRaises(AdapterError) as raised:
            _reconcile_bgp_config(self.device, {"routers": [None]})

        self.assertEqual(raised.exception.code, "invalid_response")
        state.refresh_from_db()
        self.assertEqual((state.pk, state.status, state.bgp_peer_id, state.device_base_hash), original)

    def test_missing_required_fields_are_typed_adapter_errors(self):
        """Missing adapter fields fail before stored BGP can change."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        peer_group = {
            "name": "GROUP",
            "remote_as": "65200",
            "address_families": [{"af": "ipv4-unicast"}],
        }
        payload = self._payload(self._router_payload(peers=[self._peer_entry()]))
        payload["routers"][0]["scopes"][0]["peer_groups"] = [peer_group]
        _reconcile_bgp_config(self.device, payload)
        state = NSOBGPPeerState.objects.get()
        original = (state.pk, state.status, state.bgp_peer_id, state.device_base_hash)

        def without(path):
            malformed = copy.deepcopy(payload)
            target = malformed
            for part in path[:-1]:
                target = target[part]
            target.pop(path[-1])
            return malformed

        missing_fields = (
            ("router ID", ("routers", 0, "router_id")),
            ("router scopes", ("routers", 0, "scopes")),
            ("scope address families", ("routers", 0, "scopes", 0, "address_families")),
            ("scope peers", ("routers", 0, "scopes", 0, "peers")),
            ("scope peer groups", ("routers", 0, "scopes", 0, "peer_groups")),
            ("peer address families", ("routers", 0, "scopes", 0, "peers", 0, "address_families")),
            (
                "peer group address families",
                ("routers", 0, "scopes", 0, "peer_groups", 0, "address_families"),
            ),
        )

        for label, path in missing_fields:
            with self.subTest(label=label):
                with self.assertRaises(AdapterError) as raised:
                    _reconcile_bgp_config(self.device, without(path))
                self.assertEqual(raised.exception.code, "invalid_response")

        state.refresh_from_db()
        self.assertEqual((state.pk, state.status, state.bgp_peer_id, state.device_base_hash), original)
        self.assertEqual(BGPPeerAddressFamily.objects.count(), 2)

    def test_malformed_scalar_fields_are_typed_adapter_errors(self):
        """Malformed planner scalars fail before they can make existing rows stale."""
        self._make_mgmt()

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry()])),
        )
        state = NSOBGPPeerState.objects.get()
        original = (state.pk, state.status, state.bgp_peer_id, state.device_base_hash)
        peer_group = {
            "name": "GROUP",
            "remote_as": "65200",
            "address_families": [{"af": "ipv4-unicast"}],
        }
        policy_fields = ("routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out")
        malformed_peer_policies = tuple(
            (
                f"non-string peer {field}",
                self._payload(
                    self._router_payload(
                        peers=[
                            self._peer_entry(
                                address_families=[{"af": "ipv4-unicast", "enabled": True, field: ["invalid"]}]
                            )
                        ]
                    )
                ),
            )
            for field in policy_fields
        )
        malformed_group_policies = tuple(
            (
                f"non-string group {field}",
                self._scope_with_peer_groups(
                    [
                        {
                            **peer_group,
                            "address_families": [{"af": "ipv4-unicast", field: ["invalid"]}],
                        }
                    ]
                ),
            )
            for field in policy_fields
        )
        invalid_payloads = (
            ("null router ASN", self._payload(self._router_payload(asn=None))),
            ("invalid router ASN", self._payload(self._router_payload(asn="not-an-asn"))),
            ("numeric router ASN", self._payload(self._router_payload(asn=65100))),
            ("non-string router ID", self._payload(self._router_payload(router_id=["invalid"]))),
            ("invalid router ID", self._payload(self._router_payload(router_id="not-an-ip"))),
            ("IPv6 router ID", self._payload(self._router_payload(router_id="2001:db8::1"))),
            ("numeric VRF", self._payload(self._router_payload(vrf=7))),
            (
                "non-boolean peer enabled",
                self._payload(self._router_payload(peers=[self._peer_entry(enabled="invalid")])),
            ),
            (
                "non-string peer source",
                self._payload(self._router_payload(peers=[self._peer_entry(source=["invalid"])])),
            ),
            (
                "non-integer peer TTL",
                self._payload(self._router_payload(peers=[self._peer_entry(ttl="invalid")])),
            ),
            (
                "non-string peer password",
                self._payload(self._router_payload(peers=[self._peer_entry(password=["invalid"])])),
            ),
            (
                "non-boolean peer BFD enabled",
                self._payload(self._router_payload(peers=[self._peer_entry(bfd_enabled=1)])),
            ),
            (
                "numeric peer group name",
                self._payload(self._router_payload(peers=[self._peer_entry(peer_group=7)])),
            ),
            (
                "blank peer group name",
                self._payload(self._router_payload(peers=[self._peer_entry(peer_group="")])),
            ),
            (
                "numeric group name",
                self._scope_with_peer_groups([{**peer_group, "name": 7}]),
            ),
            (
                "blank group name",
                self._scope_with_peer_groups([{**peer_group, "name": ""}]),
            ),
            (
                "non-string group source",
                self._scope_with_peer_groups([{**peer_group, "source": ["invalid"]}]),
            ),
            (
                "numeric peer address",
                self._payload(self._router_payload(peers=[self._peer_entry(peer_address=7)])),
            ),
            (
                "blank peer address",
                self._payload(self._router_payload(peers=[self._peer_entry(peer_address="")])),
            ),
            (
                "invalid peer address",
                self._payload(self._router_payload(peers=[self._peer_entry(peer_address="not-an-ip")])),
            ),
            (
                "numeric remote ASN",
                self._payload(self._router_payload(peers=[self._peer_entry(remote_as=65200)])),
            ),
            (
                "invalid remote ASN",
                self._payload(self._router_payload(peers=[self._peer_entry(remote_as="invalid")])),
            ),
            (
                "numeric local ASN",
                self._payload(self._router_payload(peers=[self._peer_entry(local_as=65100)])),
            ),
            (
                "invalid local ASN",
                self._payload(self._router_payload(peers=[self._peer_entry(local_as="invalid")])),
            ),
            (
                "numeric group remote ASN",
                self._scope_with_peer_groups([{**peer_group, "remote_as": 65200}]),
            ),
            (
                "invalid group remote ASN",
                self._scope_with_peer_groups([{**peer_group, "remote_as": "invalid"}]),
            ),
            (
                "numeric scope AF",
                self._payload(self._router_payload(address_families=[7])),
            ),
            (
                "blank scope AF",
                self._payload(self._router_payload(address_families=[""])),
            ),
            (
                "numeric peer AF",
                self._payload(self._router_payload(peers=[self._peer_entry(address_families=[{"af": 7}])])),
            ),
            (
                "blank peer AF",
                self._payload(self._router_payload(peers=[self._peer_entry(address_families=[{"af": ""}])])),
            ),
            (
                "non-boolean peer AF enabled",
                self._payload(
                    self._router_payload(
                        peers=[self._peer_entry(address_families=[{"af": "ipv4-unicast", "enabled": "invalid"}])]
                    )
                ),
            ),
            (
                "numeric group AF",
                self._scope_with_peer_groups([{**peer_group, "address_families": [{"af": 7}]}]),
            ),
            (
                "blank group AF",
                self._scope_with_peer_groups([{**peer_group, "address_families": [{"af": ""}]}]),
            ),
            (
                "non-boolean group AF enabled",
                self._scope_with_peer_groups(
                    [{**peer_group, "address_families": [{"af": "ipv4-unicast", "enabled": "invalid"}]}]
                ),
            ),
            *malformed_peer_policies,
            *malformed_group_policies,
        )

        for label, payload in invalid_payloads:
            with self.subTest(label=label):
                with self.assertRaises(AdapterError) as raised:
                    _reconcile_bgp_config(self.device, payload)
                self.assertEqual(raised.exception.code, "invalid_response")

        state.refresh_from_db()
        self.assertEqual((state.pk, state.status, state.bgp_peer_id, state.device_base_hash), original)

    def test_asdot_asns_are_canonical_across_reconcile_and_push(self):
        """Every asdot ASN becomes stable asplain state before downstream consumers run."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer, BGPPeerTemplate, BGPRouter

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOBGPPeerState

        peer = self._peer_entry(remote_as="65535.65534", local_as="65535.65533", peer_group="GROUP")
        router = self._router_payload(
            asn="65535.65535",
            peers=[peer],
            router_id="198.18.0.1",
        )
        router["scopes"][0]["peer_groups"] = [
            {
                "name": "GROUP",
                "remote_as": "65535.65532",
                "address_families": [{"af": "ipv4-unicast"}],
            }
        ]
        payload = self._payload(router)

        first = _reconcile_bgp_config(self.device, payload)[0]
        second = _reconcile_bgp_config(self.device, payload)[0]

        self.assertEqual(BGPRouter.objects.get().asn.asn, 4_294_967_295)
        materialized_peer = BGPPeer.objects.get()
        self.assertEqual(materialized_peer.remote_as.asn, 4_294_967_294)
        self.assertEqual(materialized_peer.local_as.asn, 4_294_967_293)
        self.assertEqual(BGPPeerTemplate.objects.get(name="GROUP").remote_as.asn, 4_294_967_292)
        state = NSOBGPPeerState.objects.get()
        self.assertEqual((state.asn_str, state.remote_as_str), ("4294967295", "4294967294"))
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.bgp_peer_id, first.bgp_peer_id)
        self.assertEqual(second.device_base_hash, first.device_base_hash)

        content_update(state, status="in_sync")
        captured = {}

        def capture_push(adapter_device_id, routers):
            captured["adapter_device_id"] = adapter_device_id
            captured["routers"] = routers
            return {"device_id": adapter_device_id, "router_count": len(routers)}

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent", side_effect=capture_push):
            deliver("bgp", self.device.pk, self.device.nso_management.adapter_device_id)

        pushed_router = captured["routers"][0]
        self.assertEqual((pushed_router["asn"], pushed_router["router_id"]), ("4294967295", "198.18.0.1"))
        pushed_peer = pushed_router["scopes"][0]["peers"][0]
        self.assertEqual((pushed_peer["remote_as"], pushed_peer["local_as"]), ("4294967294", "4294967293"))

    def test_single_peer_creates_state_row_in_sync(self):
        """New peer with linked bgp_peer FK → NSOBGPPeerState created with status=in_sync."""
        mgmt = self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry()])),
        )

        self.assertEqual(len(result), 1)
        state = result[0]
        self.assertEqual(state.status, "imported")  # unowned, materialized → imported (unified)
        self.assertEqual(state.asn_str, "65100")
        self.assertEqual(state.peer_address_str, "10.0.0.2")
        self.assertEqual(state.management, mgmt)
        self.assertIsNotNone(state.bgp_peer)
        self.assertTrue(state.enabled)

    def test_unknown_vrf_reuses_resolved_global_scope(self):
        """An unknown payload VRF resolves to one global scope across reconciles."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer, BGPScope

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        payload = self._payload(
            self._router_payload(
                vrf="MISSING-VRF",
                peers=[self._peer_entry("198.18.0.2")],
            )
        )

        _reconcile_bgp_config(self.device, payload)
        _reconcile_bgp_config(self.device, payload)

        self.assertEqual(BGPScope.objects.count(), 1)
        self.assertIsNone(BGPScope.objects.get().vrf_id)
        self.assertEqual(BGPPeer.objects.count(), 1)

    def test_existing_vrf_scope_is_idempotent(self):
        """A resolved payload VRF reuses its scope and peer across reconciles."""
        self._make_mgmt()

        from ipam.models import VRF
        from netbox_routing.models import BGPPeer, BGPScope

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        vrf = VRF.objects.create(name="EXISTING-VRF")
        payload = self._payload(
            self._router_payload(
                vrf=vrf.name,
                peers=[self._peer_entry("198.18.0.3")],
            )
        )

        _reconcile_bgp_config(self.device, payload)
        _reconcile_bgp_config(self.device, payload)

        self.assertEqual(BGPScope.objects.filter(vrf=vrf).count(), 1)
        self.assertEqual(BGPPeer.objects.count(), 1)

    def test_existing_global_peer_ip_converges_to_vrf_without_duplicate_peer(self):
        """A legacy named-VRF peer keeps its peer and state while its IP becomes scoped."""
        self._make_mgmt()

        from ipam.models import VRF, IPAddress
        from netbox_routing.models import BGPPeer, BGPScope

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        address = "2001:db8:0:0:0:0:0:4"
        global_payload = self._payload(self._router_payload(peers=[self._peer_entry(address)]))
        _reconcile_bgp_config(self.device, global_payload)
        vrf = VRF.objects.create(name="LEGACY-VRF")
        scope = BGPScope.objects.get()
        scope.vrf = vrf
        scope.save(update_fields=["vrf"])
        state = NSOBGPPeerState.objects.get()
        state.vrf_name = vrf.name
        state.save(update_fields=["vrf_name"])
        original_peer_id = state.bgp_peer_id
        original_ip_id = state.bgp_peer.peer_id
        self.assertIsNone(IPAddress.objects.get(pk=original_ip_id).vrf_id)

        named_payload = self._payload(self._router_payload(vrf=vrf.name, peers=[self._peer_entry(address)]))
        _reconcile_bgp_config(self.device, named_payload)

        state.refresh_from_db()
        peer = BGPPeer.objects.get()
        self.assertEqual(BGPPeer.objects.count(), 1)
        self.assertEqual(NSOBGPPeerState.objects.count(), 1)
        self.assertEqual(peer.pk, original_peer_id)
        self.assertEqual(state.bgp_peer_id, original_peer_id)
        self.assertEqual(peer.peer.vrf_id, vrf.pk)
        self.assertNotEqual(peer.peer_id, original_ip_id)

    def test_duplicate_peer_hosts_are_scoped_and_device_source_is_reused(self):
        """Peer hosts use each scope while all scopes reuse the device source address."""
        self._make_mgmt()

        from dcim.models import Interface
        from ipam.models import VRF, IPAddress
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        loopback = Interface.objects.create(device=self.device, name="Loopback0", type="virtual")
        source = IPAddress.objects.create(address="198.18.10.1/32", assigned_object=loopback)
        vrfs = [VRF.objects.create(name=name) for name in ("BGP-VRF-A", "BGP-VRF-B")]
        for vrf in vrfs:
            IPAddress.objects.create(address="198.18.10.2/32", vrf=vrf)
        scopes = [
            self._router_payload(
                vrf=vrf.name,
                peers=[self._peer_entry("198.18.10.2", source="198.18.10.1")],
            )["scopes"][0]
            for vrf in vrfs
        ]
        payload = self._payload({"asn": "65100", "router_id": None, "scopes": scopes})

        _reconcile_bgp_config(self.device, payload)

        peers = BGPPeer.objects.select_related("scope__vrf", "peer__vrf", "source__vrf").order_by("scope__vrf__name")
        self.assertEqual(
            [(peer.scope.vrf.name, peer.peer.vrf.name, peer.source_id) for peer in peers],
            [(vrf.name, vrf.name, source.pk) for vrf in vrfs],
        )
        for state in NSOBGPPeerState.objects.all():
            content_update(state, status="accepted")

        _reconcile_bgp_config(self.device, payload)

        self.assertEqual(BGPPeer.objects.count(), 2)
        self.assertEqual(IPAddress.objects.filter(address__net_host="198.18.10.1").count(), 1)
        self.assertEqual(set(NSOBGPPeerState.objects.values_list("status", flat=True)), {"in_sync"})

    def test_device_source_prefers_the_scope_vrf_over_global(self):
        """A source assigned in two VRFs uses the row from the peer scope."""
        self._make_mgmt()

        from dcim.models import Interface
        from ipam.models import VRF, IPAddress
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        source_address = "198.18.10.3"
        vrf = VRF.objects.create(name="SOURCE-VRF")
        scoped_loopback = Interface.objects.create(device=self.device, name="Loopback1", type="virtual")
        scoped_source = IPAddress.objects.create(
            address=f"{source_address}/32",
            vrf=vrf,
            assigned_object=scoped_loopback,
        )
        global_loopback = Interface.objects.create(device=self.device, name="Loopback0", type="virtual")
        IPAddress.objects.create(address=f"{source_address}/32", assigned_object=global_loopback)
        payload = self._payload(
            self._router_payload(
                vrf=vrf.name,
                peers=[self._peer_entry("198.18.10.4", source=source_address)],
            )
        )

        _reconcile_bgp_config(self.device, payload)

        peer = BGPPeer.objects.get()
        self.assertEqual(peer.source_id, scoped_source.pk)
        self.assertEqual(peer.source.vrf_id, vrf.pk)

    def test_unique_device_source_in_another_vrf_is_reused(self):
        """A unique device source is valid even when its VRF differs from the peer scope."""
        self._make_mgmt()

        from dcim.models import Interface
        from ipam.models import VRF, IPAddress
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        loopback = Interface.objects.create(device=self.device, name="Loopback0", type="virtual")
        source_address = "198.18.10.5"
        other_vrf = VRF.objects.create(name="OTHER-VRF")
        source = IPAddress.objects.create(address=f"{source_address}/32", vrf=other_vrf, assigned_object=loopback)
        scope_vrf = VRF.objects.create(name="SCOPE-VRF")
        payload = self._payload(
            self._router_payload(
                vrf=scope_vrf.name,
                peers=[self._peer_entry("198.18.10.6", source=source_address)],
            )
        )

        _reconcile_bgp_config(self.device, payload)

        peer = BGPPeer.objects.get()
        self.assertEqual(peer.source_id, source.pk)

    def test_existing_peer_source_row_settles_deploying_state(self):
        """A matching source already linked to the peer remains its source identity."""
        self._make_mgmt()

        from ipam.models import VRF, IPAddress
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _content_hash, _peer_object_content, _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        vrf = VRF.objects.create(name="SOURCE-SELECTION-VRF")
        source_address = "198.18.10.7"
        selected_source = IPAddress.objects.create(address=f"{source_address}/32")
        payload = self._payload(
            self._router_payload(
                vrf=vrf.name,
                peers=[self._peer_entry("198.18.10.8")],
            )
        )
        _reconcile_bgp_config(self.device, payload)
        peer = BGPPeer.objects.get()
        content_update(peer, source=selected_source)
        state = NSOBGPPeerState.objects.get()
        content_update(state, status="deploying")
        payload["routers"][0]["scopes"][0]["peers"][0]["source"] = source_address

        result = _reconcile_bgp_config(self.device, payload)

        peer.refresh_from_db()
        self.assertEqual(peer.source_id, selected_source.pk)
        self.assertEqual(result[0].status, "in_sync")
        self.assertEqual(result[0].device_base_hash, _content_hash(_peer_object_content(peer)))
        self.assertEqual(IPAddress.objects.filter(address__net_host=source_address).count(), 1)

    def test_duplicate_peer_does_not_materialize_ignored_asns(self):
        """Equivalent IPv6 spellings identify one peer before ignored ASNs materialize."""
        self._make_mgmt()

        from ipam.models import ASN
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        first = self._peer_entry("2001:0db8:0000:0000:0000:0000:0000:0002", remote_as="65200")
        duplicate = self._peer_entry("2001:db8::2", remote_as="65300", local_as="65400")

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[first, duplicate])),
        )

        self.assertEqual(BGPPeer.objects.count(), 1)
        self.assertEqual(NSOBGPPeerState.objects.count(), 1)
        self.assertFalse(ASN.objects.filter(asn__in=(65300, 65400)).exists())

    def test_persisted_expanded_ipv6_state_reuses_canonical_identity(self):
        """A pre-canonical owned state remains linked and included in the owned snapshot."""
        mgmt = self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOBGPPeerState

        canonical = "2001:db8::2"
        expanded = "2001:0db8:0000:0000:0000:0000:0000:0002"
        payload = self._payload(self._router_payload(peers=[self._peer_entry(canonical)]))
        original = _reconcile_bgp_config(self.device, payload)[0]
        content_update(original, status="in_sync", peer_address_str=expanded)

        result = _reconcile_bgp_config(self.device, payload)

        state = NSOBGPPeerState.objects.get()
        self.assertEqual(len(result), 1)
        self.assertEqual(state.pk, original.pk)
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(state.peer_address_str, canonical)
        captured = {}

        def capture_push(adapter_device_id, routers):
            captured["routers"] = routers
            return {"device_id": adapter_device_id, "router_count": len(routers)}

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent", side_effect=capture_push):
            deliver("bgp", self.device.pk, mgmt.adapter_device_id)

        pushed_peers = captured["routers"][0]["scopes"][0]["peers"]
        self.assertEqual([peer["peer_address"] for peer in pushed_peers], [canonical])

    def test_persisted_peer_aliases_keep_lower_canonical_pk(self):
        """The lower canonical row remains the identity when an expanded alias follows it."""
        mgmt = self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        canonical = "2001:db8::2"
        expanded = "2001:0db8:0000:0000:0000:0000:0000:0002"
        payload = self._payload(self._router_payload(peers=[self._peer_entry(canonical)]))
        identity = _reconcile_bgp_config(self.device, payload)[0]
        content_update(identity, status="in_sync")
        duplicate = NSOBGPPeerState.objects.create(
            management=mgmt,
            asn_str=identity.asn_str,
            vrf_name=identity.vrf_name,
            peer_address_str=expanded,
            bgp_peer=identity.bgp_peer,
            status="accepted",
        )

        _reconcile_bgp_config(self.device, payload)

        identity.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertLess(identity.pk, duplicate.pk)
        self.assertEqual(identity.peer_address_str, canonical)
        self.assertEqual(identity.status, "in_sync")
        self.assertEqual(duplicate.status, "changed")

    def test_persisted_peer_aliases_keep_lower_expanded_pk_out_of_snapshot(self):
        """The lower expanded row remains the identity and the canonical alias becomes stale."""
        mgmt = self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.delivery import deliver
        from netbox_nso_plugin.models import NSOBGPPeerState

        canonical = "2001:db8::2"
        expanded = "2001:0db8:0000:0000:0000:0000:0000:0002"
        payload = self._payload(self._router_payload(peers=[self._peer_entry(canonical)]))
        identity = _reconcile_bgp_config(self.device, payload)[0]
        content_update(identity, status="in_sync", peer_address_str=expanded)
        duplicate = NSOBGPPeerState.objects.create(
            management=mgmt,
            asn_str=identity.asn_str,
            vrf_name=identity.vrf_name,
            peer_address_str=canonical,
            bgp_peer=identity.bgp_peer,
            status="in_sync",
        )

        _reconcile_bgp_config(self.device, payload)

        identity.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertLess(identity.pk, duplicate.pk)
        self.assertEqual(identity.peer_address_str, expanded)
        self.assertEqual(identity.status, "in_sync")
        self.assertEqual(duplicate.status, "changed")
        captured = {}

        def capture_push(adapter_device_id, routers):
            captured["routers"] = routers
            return {"device_id": adapter_device_id, "router_count": len(routers)}

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent", side_effect=capture_push):
            deliver("bgp", self.device.pk, mgmt.adapter_device_id)

        pushed_peers = captured["routers"][0]["scopes"][0]["peers"]
        self.assertEqual(len(pushed_peers), 1)

    def test_duplicate_router_asn_keeps_first_definition(self):
        """A repeated router ASN is warned and ignored before it can update a planned router."""
        self._make_mgmt()

        from netbox_routing.models import BGPRouter

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        first = self._router_payload(asn="65100")
        duplicate = self._router_payload(asn="65100", router_id="198.18.0.9")

        with self.assertLogs("netbox_nso_plugin.bgp_reconciler", level="WARNING") as captured:
            _reconcile_bgp_config(self.device, self._payload(first, duplicate))

        router = BGPRouter.objects.get()
        self.assertIsNone(router.router_id)
        self.assertTrue(any("repeated router ASN" in message for message in captured.output))

    def test_duplicate_address_family_is_hash_and_status_stable(self):
        """Repeated AF identity persists once and remains stable on the next read."""
        self._make_mgmt()

        from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPPeerAddressFamily

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        peer = self._peer_entry()
        peer["address_families"].append({"af": "ipv4-unicast", "enabled": True})
        payload = self._payload(self._router_payload(peers=[peer]))

        first = _reconcile_bgp_config(self.device, payload)[0]
        first_hash = first.device_base_hash
        second = _reconcile_bgp_config(self.device, payload)[0]

        self.assertEqual(BGPPeer.objects.count(), 1)
        self.assertEqual(NSOBGPPeerState.objects.count(), 1)
        self.assertEqual(BGPAddressFamily.objects.count(), 1)
        self.assertEqual(BGPPeerAddressFamily.objects.count(), 1)
        self.assertEqual(second.device_base_hash, first_hash)
        self.assertEqual(second.status, "imported")

    def test_push_includes_peer_source_ip(self):
        """BGP delivery must send a peer's source (the local-address IP).

        The PUT peer dict previously dropped source entirely, so the reconciler — which can now
        write local-address/update-source — never received it: the BGP session source was lost on
        push. Junos/Nokia source is an IP (resolved to an ipam.IPAddress on import); send it back.
        """
        from unittest.mock import patch

        from ipam.models import IPAddress

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        IPAddress.objects.create(address="198.18.255.1/32")
        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry(source="198.18.255.1")])),
        )
        row = result[0]
        self.assertIsNotNone(row.bgp_peer.source)  # source resolved to the IPAddress on import
        # Make the row operator-owned so the intent push picks it up.
        row.status = "in_sync"
        row.save(update_fields=["status"])

        captured = {}

        def _capture(adapter_device_id, routers):
            captured["routers"] = routers
            return {"device_id": adapter_device_id, "router_count": len(routers)}

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent", side_effect=_capture):
            deliver("bgp", self.device.pk, mgmt.adapter_device_id)

        peers = captured["routers"][0]["scopes"][0]["peers"]
        self.assertEqual(peers[0]["source"], "198.18.255.1")

    def test_push_includes_local_as_ttl_password_peer_group(self):
        """BGP delivery must send local_as, ttl, password, and peer-group.

        These leaves are imported from the device onto the netbox-routing BGPPeer and are
        fully supported by the adapter intent schema + reconciler YANG, but the PUT peer dict
        dropped them — so an accepted brownfield peer's password / ttl / local-AS / peer-group
        silently vanished from the pushed intent (the #99 write-integrity gap). The overlay row
        denormalizes only remote_as + enabled, so the push must read them off ``row.bgp_peer``.
        """
        from unittest.mock import patch

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        result = _reconcile_bgp_config(
            self.device,
            self._payload(
                self._router_payload(
                    peers=[
                        self._peer_entry(
                            local_as="65199",
                            ttl=2,
                            password="s3cr3t",
                            peer_group="EDGE",
                        )
                    ]
                )
            ),
        )
        row = result[0]
        # Sanity: the reconciler imported all four onto the linked BGPPeer.
        self.assertEqual(str(row.bgp_peer.local_as.asn), "65199")
        self.assertEqual(row.bgp_peer.ttl, 2)
        self.assertEqual(row.bgp_peer.password, "s3cr3t")
        self.assertEqual(row.bgp_peer.peer_group.name, "EDGE")
        # Own the row so the intent push picks it up.
        row.status = "in_sync"
        row.save(update_fields=["status"])

        captured = {}

        def _capture(adapter_device_id, routers):
            captured["routers"] = routers
            return {"device_id": adapter_device_id, "router_count": len(routers)}

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent", side_effect=_capture):
            deliver("bgp", self.device.pk, mgmt.adapter_device_id)

        peer = captured["routers"][0]["scopes"][0]["peers"][0]
        self.assertEqual(peer["local_as"], "65199")
        self.assertEqual(peer["ttl"], 2)
        self.assertEqual(peer["password"], "s3cr3t")
        self.assertEqual(peer["peer_group"], "EDGE")

    def test_reconcile_imports_router_id(self):
        """A global router-id in the payload is imported onto BGPRouter on first read."""
        self._make_mgmt()
        from netbox_routing.models import BGPRouter

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(router_id="10.255.0.1", peers=[self._peer_entry()])),
        )
        router = BGPRouter.objects.get(asn__asn=65100)
        self.assertEqual(str(router.router_id), "10.255.0.1")

    def test_reconcile_does_not_clobber_operator_router_id(self):
        """An operator-edited router-id is preserved when a later device read differs.

        router-id has no per-field overlay, so the import-once guard is the only thing
        keeping a pending operator edit from being reverted before it can be pushed.
        """
        self._make_mgmt()
        from netbox_routing.models import BGPRouter

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        # Initial import establishes the router + the brownfield value.
        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(router_id="1.1.1.1", peers=[self._peer_entry()])),
        )
        router = BGPRouter.objects.get(asn__asn=65100)
        router.router_id = "9.9.9.9"  # operator edit, not yet accepted
        router.save(update_fields=["router_id"])

        # A subsequent read still reporting the OLD device value must not overwrite it.
        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(router_id="1.1.1.1", peers=[self._peer_entry()])),
        )
        router.refresh_from_db()
        self.assertEqual(str(router.router_id), "9.9.9.9")

    def test_push_includes_router_id(self):
        """An owned router's global router-id is emitted in the pushed intent."""
        from unittest.mock import patch

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(router_id="10.255.0.1", peers=[self._peer_entry()])),
        )
        row = result[0]
        row.status = "in_sync"  # own it so the push picks up the router
        row.save(update_fields=["status"])

        captured = {}

        def _capture(adapter_device_id, routers):
            captured["routers"] = routers
            return {"device_id": adapter_device_id, "router_count": len(routers)}

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent", side_effect=_capture):
            deliver("bgp", self.device.pk, mgmt.adapter_device_id)

        self.assertEqual(captured["routers"][0]["router_id"], "10.255.0.1")

    def test_idempotent_second_call(self):
        """Calling reconcile twice with same payload → same single state row."""
        self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))
        result = _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "imported")  # unowned, materialized

    def test_creates_bgp_router_and_scope(self):
        """Reconciler creates BGPRouter and BGPScope in netbox-routing."""
        self._make_mgmt()

        from netbox_routing.models import BGPRouter, BGPScope

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(self.device, self._payload(self._router_payload()))

        self.assertEqual(BGPRouter.objects.filter(assigned_object_id=self.device.pk).count(), 1)
        router = BGPRouter.objects.get(assigned_object_id=self.device.pk)
        self.assertEqual(str(router.asn.asn), "65100")
        self.assertEqual(BGPScope.objects.filter(router=router).count(), 1)

    def test_creates_asn_when_absent(self):
        """ASN missing in IPAM → auto-created with placeholder RIR."""
        self._make_mgmt()

        from ipam.models import ASN

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        ASN.objects.filter(asn=65100).delete()
        _reconcile_bgp_config(self.device, self._payload(self._router_payload()))

        self.assertTrue(ASN.objects.filter(asn=65100).exists())

    def test_creates_bgp_peer_with_ip(self):
        """Peer address → ipam.IPAddress created + BGPPeer linked."""
        self._make_mgmt()

        from ipam.models import IPAddress
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))

        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.2").exists())
        self.assertEqual(BGPPeer.objects.filter(peer__address__net_host="10.0.0.2").count(), 1)

    def test_peer_address_family_created(self):
        """Per-peer address family entry → BGPPeerAddressFamily created."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))

        self.assertEqual(BGPPeerAddressFamily.objects.count(), 1)
        paf = BGPPeerAddressFamily.objects.first()
        self.assertEqual(paf.address_family.address_family, "ipv4-unicast")

    def test_peer_af_routemap_linked(self):
        """Per-AF routemap_in/out names resolve to RouteMap objects on the peer-AF."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        rm_in = RouteMap.objects.create(name="RM-IN")
        rm_out = RouteMap.objects.create(name="RM-OUT")
        peer = self._peer_entry()
        peer["address_families"] = [
            {"af": "ipv4-unicast", "enabled": True, "routemap_in": "RM-IN", "routemap_out": "RM-OUT"}
        ]
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        paf = BGPPeerAddressFamily.objects.get(address_family__address_family="ipv4-unicast")
        self.assertEqual(paf.routemap_in, rm_in)
        self.assertEqual(paf.routemap_out, rm_out)

    def test_peer_af_unknown_routemap_left_null(self):
        """An unresolved routemap name is left null, not guessed."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peer = self._peer_entry()
        peer["address_families"] = [{"af": "ipv4-unicast", "enabled": True, "routemap_in": "NOPE"}]
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        paf = BGPPeerAddressFamily.objects.get(address_family__address_family="ipv4-unicast")
        self.assertIsNone(paf.routemap_in_id)

    def test_peer_source_ip_linked(self):
        """source given as an IP resolves to an existing ipam.IPAddress on the peer."""
        self._make_mgmt()

        from ipam.models import IPAddress
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        src = IPAddress.objects.create(address="198.18.255.1/32")
        peer = self._peer_entry()
        peer["source"] = "198.18.255.1"
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        bp = BGPPeer.objects.get(peer__address__net_host="10.0.0.2")
        self.assertEqual(bp.source, src)

    def test_peer_source_prefix_forms_are_canonical(self):
        """IPv4 and IPv6 source prefixes resolve to their canonical host addresses."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peers = [
            self._peer_entry("198.18.20.2", source="198.18.20.1/24"),
            self._peer_entry("2001:db8::2", source="2001:0db8:0000:0000:0000:0000:0000:0001/64"),
        ]

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=peers)))

        sources = {
            str(peer.peer.address.ip): str(peer.source.address.ip)
            for peer in BGPPeer.objects.select_related("peer", "source")
        }
        self.assertEqual(sources, {"198.18.20.2": "198.18.20.1", "2001:db8::2": "2001:db8::1"})

    def test_unparseable_peer_source_prefixes_are_unresolved_interface_names(self):
        """Unparseable source prefixes use exact interface lookup and remain unresolved."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peers = [
            self._peer_entry("198.18.20.2", source="198.18.20.1/33"),
            self._peer_entry("198.18.20.3", source="999.999.999.999/32"),
            self._peer_entry("198.18.20.4", source="2001:db8::not-hex/64"),
        ]

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=peers)))

        for peer in BGPPeer.objects.all():
            self.assertIsNone(peer.source_id)
            self.assertIsNone(peer.update_source_id)

    def test_scoped_ipv6_peer_source_is_a_typed_adapter_error(self):
        """A scoped IPv6 source cannot be represented by NetBox."""
        self._make_mgmt()

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        payload = self._payload(self._router_payload(peers=[self._peer_entry("2001:db8::2", source="fe80::1%eth0/64")]))

        with self.assertRaises(AdapterError) as raised:
            _reconcile_bgp_config(self.device, payload)

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_malformed_dotted_peer_sources_are_unresolved_interface_names(self):
        """Malformed dotted values use exact interface lookup and remain unresolved."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peers = [
            self._peer_entry("198.18.20.2", source="1.2.3.4.5/24"),
            self._peer_entry("198.18.20.3", source="198.18.20.1./32"),
        ]

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=peers)))

        for peer in BGPPeer.objects.all():
            self.assertIsNone(peer.source_id)
            self.assertIsNone(peer.update_source_id)

    def test_peer_source_unknown_ip_creates_stub(self):
        """source IP not present in IPAM → a stub IPAddress is auto-created and linked.

        An IP local-address (Junos/Nokia) not yet modeled in IPAM must still be
        preserved so the source round-trips and is re-pushable, mirroring how the peer
        neighbor address is handled (rather than being silently dropped)."""
        self._make_mgmt()

        from ipam.models import IPAddress
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        self.assertFalse(IPAddress.objects.filter(address__net_host="203.0.113.250").exists())
        peer = self._peer_entry()
        peer["source"] = "203.0.113.250"
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        stub = IPAddress.objects.filter(address__net_host="203.0.113.250").first()
        self.assertIsNotNone(stub, "an IP local-address absent from IPAM should be auto-created as a stub")
        bp = BGPPeer.objects.get(peer__address__net_host="10.0.0.2")
        self.assertEqual(bp.source, stub)

    def test_peer_update_source_iface_linked(self):
        """source given as an interface name (IOS/IOS-XR update-source) → the device's
        dcim.Interface lands on BGPPeer.update_source (kept as itself, not collapsed to one
        of its IPs), and the IPAddress source stays null."""
        self._make_mgmt()

        from dcim.models import Interface
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        loopback = Interface.objects.create(device=self.device, name="100GigE0/0/0", type="virtual")
        peer = self._peer_entry()
        peer["source"] = loopback.name
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        bp = BGPPeer.objects.get(peer__address__net_host="10.0.0.2")
        self.assertEqual(bp.update_source, loopback)
        self.assertIsNone(bp.source_id)

    def test_numeric_slash_peer_source_resolves_as_interface_name(self):
        """A slash-delimited numeric source resolves by exact interface name."""
        self._make_mgmt()

        from dcim.models import Interface
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        interface = Interface.objects.create(device=self.device, name="1/1/1", type="virtual")
        peer = self._peer_entry(source=interface.name)

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        bgp_peer = BGPPeer.objects.get(peer__address__net_host="10.0.0.2")
        self.assertEqual(bgp_peer.update_source, interface)
        self.assertIsNone(bgp_peer.source_id)

    def test_peer_update_source_unknown_iface_left_null(self):
        """An update-source interface absent from the device → both source and update_source
        stay null (we don't fabricate)."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peer = self._peer_entry()
        peer["source"] = "Loopback99"
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        bp = BGPPeer.objects.get(peer__address__net_host="10.0.0.2")
        self.assertIsNone(bp.source_id)
        self.assertIsNone(bp.update_source_id)

    def test_push_sends_update_source_iface_name(self):
        """BGP delivery sends the update-source interface name for a peer whose
        source is a dcim.Interface (IOS/IOS-XR), so the cisco writer round-trips it.

        Counterpart to test_push_includes_peer_source_ip (Junos/Nokia local-address IP): the
        same per-NED-dispatched peer/source string carries an interface name here.
        """
        from unittest.mock import patch

        from dcim.models import Interface

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.delivery import deliver

        mgmt = self._make_mgmt()
        Interface.objects.create(device=self.device, name="Loopback0", type="virtual")
        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry(source="Loopback0")])),
        )
        row = result[0]
        self.assertIsNotNone(row.bgp_peer.update_source)  # resolved to the Interface on import
        self.assertIsNone(row.bgp_peer.source_id)
        # Make the row operator-owned so the intent push picks it up.
        row.status = "in_sync"
        row.save(update_fields=["status"])

        captured = {}

        def _capture(adapter_device_id, routers):
            captured["routers"] = routers
            return {"device_id": adapter_device_id, "router_count": len(routers)}

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent", side_effect=_capture):
            deliver("bgp", self.device.pk, mgmt.adapter_device_id)

        peers = captured["routers"][0]["scopes"][0]["peers"]
        self.assertEqual(peers[0]["source"], "Loopback0")

    def test_peer_bfd_enabled_linked(self):
        """peer bfd_enabled flows onto the BGPPeer."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry("10.7.0.1", bfd_enabled=True)])),
        )
        self.assertTrue(BGPPeer.objects.get(peer__address__net_host="10.7.0.1").bfd_enabled)

    def test_peer_group_template_gets_remote_as(self):
        """The peer-group's BGPPeerTemplate is enriched with the (inherited) remote_as."""
        self._make_mgmt()

        from ipam.models import ASN
        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peer = self._peer_entry(remote_as="65100")
        peer["peer_group"] = "Arbor-IBGP"
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        tmpl = BGPPeerTemplate.objects.get(name="Arbor-IBGP")
        self.assertIsNotNone(tmpl.remote_as)
        self.assertEqual(str(tmpl.remote_as.asn), "65100")
        self.assertTrue(ASN.objects.filter(asn=65100).exists())

    def test_existing_template_gets_a_planned_remote_as(self):
        """A new ASN must enrich an existing template whose remote ASN is null."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        initial = {"name": "PLANNED-AS", "address_families": [{"af": "ipv4-unicast"}]}
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([initial]))

        changed = {**initial, "remote_as": "64520"}
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([changed]))

        template = BGPPeerTemplate.objects.get(name="PLANNED-AS")
        self.assertEqual(template.remote_as.asn, 64520)

    def test_later_template_remote_as_updates_the_planned_save(self):
        """The final reference to a template must define its planned remote ASN."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(
                self._router_payload(asn="64521"),
                self._router_payload(asn="64522"),
            ),
        )
        peer = self._peer_entry("198.18.0.21", remote_as="64521", peer_group="SHARED-AS")
        group = {
            "name": "SHARED-AS",
            "remote_as": "64522",
            "address_families": [{"af": "ipv4-unicast"}],
        }
        payload = self._payload(self._router_payload(peers=[peer]))
        payload["routers"][0]["scopes"][0]["peer_groups"] = [group]

        _reconcile_bgp_config(self.device, payload)

        template = BGPPeerTemplate.objects.get(name="SHARED-AS")
        self.assertEqual(template.remote_as.asn, 64522)

    def test_scoped_peer_address_rejects_the_complete_document(self):
        """An IP address NetBox cannot represent rejects the document before any write."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        with self.assertRaises(AdapterError) as raised:
            _reconcile_bgp_config(
                self.device,
                self._payload(
                    self._router_payload(
                        peers=[
                            self._peer_entry("198.18.0.23"),
                            self._peer_entry("fe80::1%eth0"),
                        ]
                    )
                ),
            )

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertFalse(BGPPeer.objects.filter(scope__router__assigned_object_id=self.device.pk).exists())

    def _scope_with_peer_groups(self, peer_groups, asn="65100", address_families=None):
        """Build a payload whose scope carries peer_groups (full-B objects)."""
        return {
            "device_id": self.device.pk,
            "routers": [
                {
                    "asn": asn,
                    "router_id": None,
                    "scopes": [
                        {
                            "vrf": "",
                            "address_families": address_families if address_families is not None else ["ipv4-unicast"],
                            "peers": [],
                            "peer_groups": peer_groups,
                        }
                    ],
                }
            ],
        }

    def test_peer_group_object_af_policies_attached_to_template(self):
        """A peer_groups entry → BGPPeerTemplate with its OWN per-AF route-maps."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, BGPPeerTemplate, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        RouteMap.objects.create(name="Arbor-IBGP-in")
        RouteMap.objects.create(name="Arbor-IBGP-out")
        pg = {
            "name": "Arbor-IBGP",
            "remote_as": "65100",
            "address_families": [
                {
                    "af": "ipv4-unicast",
                    "routemap_in": "Arbor-IBGP-in",
                    "routemap_out": "Arbor-IBGP-out",
                }
            ],
        }
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg]))

        tmpl = BGPPeerTemplate.objects.get(name="Arbor-IBGP")
        paf = BGPPeerAddressFamily.objects.get(
            assigned_object_type__model="bgppeertemplate", assigned_object_id=tmpl.pk
        )
        self.assertEqual(paf.address_family.address_family, "ipv4-unicast")
        self.assertEqual(paf.routemap_in.name, "Arbor-IBGP-in")
        self.assertEqual(paf.routemap_out.name, "Arbor-IBGP-out")

    def test_peer_group_template_af_edit_surfaces_changed_and_survives(self):
        """3-way templates: an operator edit to a peer-group AF policy drifts + is preserved.

        Edit -> apply contract for the template overlay: the edit must (a) surface as
        'changed' on NSOBGPPeerTemplateState, and (b) survive the next device sync
        (device unchanged) instead of being reverted to the device value.
        """
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, BGPPeerTemplate, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        RouteMap.objects.create(name="Arbor-IBGP-in")
        operator_rm = RouteMap.objects.create(name="Operator-RM")
        pg = {
            "name": "Arbor-IBGP",
            "remote_as": "65100",
            "address_families": [{"af": "ipv4-unicast", "routemap_in": "Arbor-IBGP-in"}],
        }
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg]))

        tmpl = BGPPeerTemplate.objects.get(name="Arbor-IBGP")
        paf = BGPPeerAddressFamily.objects.get(
            assigned_object_type__model="bgppeertemplate", assigned_object_id=tmpl.pk
        )
        paf.routemap_in = operator_rm  # operator edits the peer-group's inbound policy
        paf.save()

        # Re-sync with the original device data → the edit must NOT be clobbered.
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg]))
        paf.refresh_from_db()
        self.assertEqual(paf.routemap_in_id, operator_rm.pk)  # (b) edit preserved
        state = NSOBGPPeerTemplateState.objects.get(management__device=self.device, template_name="Arbor-IBGP")
        self.assertEqual(state.status, "changed")  # (a) surfaced as drift

    def test_peer_group_template_device_change_auto_mirrors(self):
        """3-way templates: device-side AF change with NetBox untouched → object auto-updated."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, BGPPeerTemplate, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        rm_a = RouteMap.objects.create(name="RM-A")
        RouteMap.objects.create(name="RM-B")
        pg_a = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast", "routemap_in": "RM-A"}]}
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg_a]))
        tmpl = BGPPeerTemplate.objects.get(name="PG")
        paf = BGPPeerAddressFamily.objects.get(
            assigned_object_type__model="bgppeertemplate", assigned_object_id=tmpl.pk
        )
        self.assertEqual(paf.routemap_in_id, rm_a.pk)

        # Device moves RM-A -> RM-B; NetBox never touched → auto-mirror.
        pg_b = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast", "routemap_in": "RM-B"}]}
        # The imported template has no rendered consumer, so the exact plan is content-neutral.
        self.assertFalse(bgp_reconcile_plan(self.device, self._scope_with_peer_groups([pg_b])).changes_content)
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg_b]))
        paf.refresh_from_db()
        self.assertEqual(paf.routemap_in.name, "RM-B")  # mirrored to new device value
        state = NSOBGPPeerTemplateState.objects.get(management__device=self.device, template_name="PG")
        self.assertEqual(state.status, "imported")  # unowned + matches → no drift

    def test_duplicate_peer_group_plan_uses_reconcile_traversal_order(self):
        """The content-neutral plan and reconcile select the last peer-group definition."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan

        ipv4 = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast"}]}
        initial = self._payload(
            self._scope_with_peer_groups([ipv4], asn="65100")["routers"][0],
            self._scope_with_peer_groups([ipv4], asn="65200")["routers"][0],
        )
        _reconcile_bgp_config(self.device, initial)

        ipv6 = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv6-unicast"}]}
        reordered = self._payload(
            self._scope_with_peer_groups([ipv6], asn="65200")["routers"][0],
            self._scope_with_peer_groups([ipv4], asn="65100")["routers"][0],
        )

        self.assertFalse(bgp_reconcile_plan(self.device, reordered).changes_content)
        _reconcile_bgp_config(self.device, reordered)

        template = BGPPeerTemplate.objects.get(name="PG")
        address_families = BGPPeerAddressFamily.objects.filter(
            assigned_object_type__model="bgppeertemplate",
            assigned_object_id=template.pk,
        ).values_list("address_family__address_family", flat=True)
        self.assertEqual(list(address_families), ["ipv6-unicast"])

    def test_invalid_router_asn_does_not_change_existing_template(self):
        """An invalid router ASN rejects the document before template changes."""
        self._make_mgmt()

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        group = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast"}]}
        valid_router = self._scope_with_peer_groups([group], asn="65100")["routers"][0]
        _reconcile_bgp_config(self.device, self._payload(valid_router))

        invalid_router = self._scope_with_peer_groups([group], asn="not-a-number")["routers"][0]
        with self.assertRaises(AdapterError) as raised:
            _reconcile_bgp_config(self.device, self._payload(valid_router, invalid_router))

        self.assertEqual(raised.exception.code, "invalid_response")
        state = NSOBGPPeerTemplateState.objects.get(management__device=self.device, template_name="PG")
        self.assertEqual(state.status, "imported")

    def test_duplicate_peer_group_remote_as_uses_reconcile_traversal_order(self):
        """The plan and reconcile select the same remote AS for a duplicate name."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.models import NSOBGPPeerState

        low = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast"}]}
        high = {"name": "PG", "remote_as": "65200", "address_families": [{"af": "ipv4-unicast"}]}
        peer = self._peer_entry("198.18.0.2", remote_as="65100", peer_group="PG")
        low_router = self._scope_with_peer_groups([low], asn="65100")["routers"][0]
        low_router["scopes"][0]["peers"] = [peer]
        initial = self._payload(
            low_router,
            self._scope_with_peer_groups([low], asn="65200")["routers"][0],
        )
        _reconcile_bgp_config(self.device, initial)
        peer_state = NSOBGPPeerState.objects.get(peer_address_str="198.18.0.2")
        peer_state.status = "accepted"
        peer_state.save(update_fields=["status"])

        reordered_low_router = self._scope_with_peer_groups([low], asn="65100")["routers"][0]
        reordered_low_router["scopes"][0]["peers"] = [peer]
        reordered = self._payload(
            self._scope_with_peer_groups([high], asn="65200")["routers"][0],
            reordered_low_router,
        )

        self.assertTrue(bgp_reconcile_plan(self.device, reordered).changes_content)
        _reconcile_bgp_config(self.device, reordered)

        self.assertEqual(BGPPeerTemplate.objects.get(name="PG").remote_as.asn, 65200)

    def test_invalid_peer_rejects_document_before_template_update(self):
        """An invalid peer rejects the document before it can change a template."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.models import NSOBGPPeerState

        anchor = self._peer_entry("198.18.0.3", remote_as="65200", peer_group="PG")
        imported = self._peer_entry("198.18.0.4", remote_as="65200", peer_group="PG")
        initial = self._payload(self._router_payload(peers=[anchor, imported]))
        _reconcile_bgp_config(self.device, initial)
        peer_state = NSOBGPPeerState.objects.get(peer_address_str="198.18.0.3")
        peer_state.status = "accepted"
        peer_state.save(update_fields=["status"])
        changed = self._payload(
            self._router_payload(
                peers=[
                    self._peer_entry("198.18.0.4", remote_as="65100", peer_group="PG"),
                    self._peer_entry("not-an-address", remote_as="65200", peer_group="PG"),
                ]
            )
        )

        with self.assertRaises(AdapterError) as raised:
            bgp_reconcile_plan(self.device, changed)

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(BGPPeerTemplate.objects.get(name="PG").remote_as.asn, 65200)
        peer_state.refresh_from_db()
        self.assertEqual(peer_state.status, "accepted")

    def test_peer_group_template_both_moved_is_conflict(self):
        """3-way templates: NetBox edited AND device changed since base → conflict, edit kept."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, BGPPeerTemplate, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        RouteMap.objects.create(name="RM-A")
        RouteMap.objects.create(name="RM-B")
        operator_rm = RouteMap.objects.create(name="RM-OP")
        pg_a = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast", "routemap_in": "RM-A"}]}
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg_a]))
        tmpl = BGPPeerTemplate.objects.get(name="PG")
        paf = BGPPeerAddressFamily.objects.get(
            assigned_object_type__model="bgppeertemplate", assigned_object_id=tmpl.pk
        )
        paf.routemap_in = operator_rm  # operator edit
        paf.save()

        # Device ALSO moved RM-A -> RM-B: both sides diverged from base → conflict.
        pg_b = {"name": "PG", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast", "routemap_in": "RM-B"}]}
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg_b]))
        state = NSOBGPPeerTemplateState.objects.get(management__device=self.device, template_name="PG")
        self.assertEqual(state.status, "conflict")
        paf.refresh_from_db()
        self.assertEqual(paf.routemap_in_id, operator_rm.pk)  # operator edit preserved

    def test_peer_group_template_state_row_created(self):
        """A peer-group template reconcile creates an NSOBGPPeerTemplateState overlay row."""
        self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerTemplateState

        pg = {"name": "RR", "remote_as": "65100", "address_families": [{"af": "ipv4-unicast"}]}
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg]))
        state = NSOBGPPeerTemplateState.objects.get(management__device=self.device, template_name="RR")
        self.assertEqual(state.status, "imported")
        self.assertEqual(state.remote_as_str, "65100")
        self.assertTrue(state.device_base_hash)

    def test_peer_group_object_idempotent(self):
        """Reconciling peer_groups twice → one template, one AF row (no dupes)."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, BGPPeerTemplate, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        RouteMap.objects.create(name="PG-in")
        pg = {
            "name": "ENC-RR",
            "address_families": [{"af": "ipv4-unicast", "routemap_in": "PG-in"}],
        }
        payload = self._scope_with_peer_groups([pg])
        _reconcile_bgp_config(self.device, payload)
        _reconcile_bgp_config(self.device, payload)

        self.assertEqual(BGPPeerTemplate.objects.filter(name="ENC-RR").count(), 1)
        tmpl = BGPPeerTemplate.objects.get(name="ENC-RR")
        self.assertEqual(
            BGPPeerAddressFamily.objects.filter(
                assigned_object_type__model="bgppeertemplate", assigned_object_id=tmpl.pk
            ).count(),
            1,
        )

    def test_peer_group_object_template_separate_from_member_af(self):
        """A peer-group object's AF row is distinct from a member peer's AF row."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerAddressFamily, BGPPeerTemplate, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        RouteMap.objects.create(name="Shared-in")
        member = self._peer_entry("10.9.9.9", remote_as="65100")
        member["peer_group"] = "Arbor-IBGP"
        member["address_families"] = [{"af": "ipv4-unicast", "enabled": True, "routemap_in": "Shared-in"}]
        payload = {
            "device_id": self.device.pk,
            "routers": [
                {
                    "asn": "65100",
                    "router_id": None,
                    "scopes": [
                        {
                            "vrf": "",
                            "address_families": ["ipv4-unicast"],
                            "peers": [member],
                            "peer_groups": [
                                {
                                    "name": "Arbor-IBGP",
                                    "remote_as": "65100",
                                    "address_families": [{"af": "ipv4-unicast", "routemap_in": "Shared-in"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        _reconcile_bgp_config(self.device, payload)

        tmpl = BGPPeerTemplate.objects.get(name="Arbor-IBGP")
        # One AF row on the template, one on the member peer = 2 total.
        self.assertEqual(BGPPeerAddressFamily.objects.count(), 2)
        self.assertEqual(
            BGPPeerAddressFamily.objects.filter(
                assigned_object_type__model="bgppeertemplate", assigned_object_id=tmpl.pk
            ).count(),
            1,
        )

    def test_stale_state_row_set_to_changed(self):
        """Peer disappears from NSO payload → status set to 'changed'."""
        self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        # First call: two peers
        _reconcile_bgp_config(
            self.device,
            self._payload(
                self._router_payload(
                    peers=[
                        self._peer_entry("10.0.1.1"),
                        self._peer_entry("10.0.1.2"),
                    ]
                )
            ),
        )

        # Second call: only one peer
        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry("10.0.1.1")])),
        )

        self.assertEqual(len(result), 2)
        statuses = {r.peer_address_str: r.status for r in result}
        self.assertEqual(statuses["10.0.1.1"], "imported")  # present, unowned → imported
        self.assertEqual(statuses["10.0.1.2"], "changed")

    def test_accepted_peer_matching_device_settles_in_sync(self):
        """Value overlay: an accepted peer whose device already matches → in_sync.

        (Never reverts to 'imported' — the owned no-clobber guarantee — but, like VLAN,
        an accepted row that the device already confirms is in sync, nothing to apply.)
        """
        mgmt = self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))
        content_update(NSOBGPPeerState.objects.get(management=mgmt), status="accepted")

        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry()])),
        )
        self.assertEqual(result[0].status, "in_sync")

    def test_edit_to_bgp_peer_surfaces_as_changed_and_survives(self):
        """Editing the netbox-routing BGPPeer shows as drift and is NOT clobbered.

        This is the edit->apply contract: the operator's edit must (a) surface as
        'changed' so it can be accepted/applied, and (b) survive the next device sync
        instead of being reverted to the device value.
        """
        self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        try:
            from netbox_routing.models import BGPPeer
        except ImportError:
            self.skipTest("netbox_routing not installed")

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))
        peer = BGPPeer.objects.get()
        # Operator edits in NetBox; the device still reports enabled=True.
        peer.enabled = False
        peer.save()

        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry()])),
        )
        self.assertEqual(result[0].status, "changed")  # (a) edit surfaced as drift
        peer.refresh_from_db()
        self.assertFalse(peer.enabled)  # (b) edit preserved, not reverted to device

    def test_plan_normalizes_equivalent_source_ip_text(self):
        mgmt = self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.models import NSOBGPPeerState

        peer = self._peer_entry()
        peer["source"] = "2001:0DB8:0000:0000:0000:0000:0000:0001"
        payload = self._payload(self._router_payload(peers=[peer]))
        _reconcile_bgp_config(self.device, payload)
        content_update(NSOBGPPeerState.objects.get(management=mgmt), status="in_sync")

        self.assertFalse(bgp_reconcile_plan(self.device, payload).changes_content)

    def test_plan_batches_owned_peer_dependency_lookups(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone
        from netbox_routing.models import PrefixList, RouteMap

        from netbox_nso_plugin.bgp_reconciler import _BGPGraphPlanner, _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.intent_state import SourceRow
        from netbox_nso_plugin.models import NSOBGPPeerState

        mgmt = self._make_mgmt()
        peers = [
            self._peer_entry(
                f"10.0.2.{index}",
                remote_as=str(65200 + index),
                address_families=[
                    {
                        "af": "ipv4-unicast",
                        "enabled": True,
                        "routemap_in": "RM-BATCH",
                        "prefixlist_out": "PL-BATCH",
                    }
                ],
            )
            for index in range(1, 5)
        ]
        payload = self._payload(self._router_payload(peers=peers))
        _reconcile_bgp_config(self.device, payload)
        states = list(NSOBGPPeerState.objects.filter(management=mgmt).order_by("peer_address_str"))
        content_update(states[0], status="in_sync")

        one_peer = self._payload(self._router_payload(peers=peers[:1]))
        plan = bgp_reconcile_plan(self.device, one_peer)
        self.assertIn(("route-policy", "route_map:rm-batch"), plan.lock_footprint.shared_keys)
        self.assertIn(("route-policy", "prefix_list:pl-batch"), plan.lock_footprint.shared_keys)
        self.assertIn(SourceRow("netbox_routing.routemap", None), plan.lock_footprint.source_rows)
        self.assertIn(SourceRow("netbox_routing.prefixlist", None), plan.lock_footprint.source_rows)
        with CaptureQueriesContext(connection) as one_queries:
            _BGPGraphPlanner(self.device, one_peer, timezone.now()).build()
        for state in states[1:]:
            content_update(state, status="in_sync")
        with CaptureQueriesContext(connection) as four_queries:
            _BGPGraphPlanner(self.device, payload, timezone.now()).build()

        policy_tables = (RouteMap._meta.db_table, PrefixList._meta.db_table)
        one_policy_queries = [
            query for query in one_queries if any(f'FROM "{table}"' in query["sql"] for table in policy_tables)
        ]
        four_policy_queries = [
            query for query in four_queries if any(f'FROM "{table}"' in query["sql"] for table in policy_tables)
        ]
        self.assertEqual(len(four_policy_queries), len(one_policy_queries))
        self.assertEqual(len(one_policy_queries), 2)
        self.assertLessEqual(len(four_queries), len(one_queries))

    def test_plan_revalidates_a_missing_route_map_after_lock_acquisition(self):
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.intent_state import IntentMutationProtocolError
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes

        self._make_mgmt()
        payload = self._payload(
            self._router_payload(
                peers=[
                    self._peer_entry(
                        address_families=[
                            {
                                "af": "ipv4-unicast",
                                "enabled": True,
                                "routemap_in": "RM-RACE",
                            }
                        ]
                    )
                ]
            )
        )
        plan = bgp_reconcile_plan(self.device, payload)
        RouteMap.objects.create(name="RM-RACE")

        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with self.assertRaises(IntentMutationProtocolError), mutation(plan):
            _reconcile_bgp_config(self.device, payload)

    def test_plan_revalidates_a_created_source_interface_after_lock_acquisition(self):
        from dcim.models import Interface

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.intent_state import RendererTargetsChanged
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes

        self._make_mgmt()
        peer = self._peer_entry(source="Loopback201")
        payload = self._payload(self._router_payload(peers=[peer]))
        plan = bgp_reconcile_plan(self.device, payload)
        Interface.objects.create(device=self.device, name="Loopback201", type="virtual")

        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with self.assertRaises(RendererTargetsChanged), mutation(plan):
            _reconcile_bgp_config(self.device, payload)

    def test_plan_revalidates_a_renamed_source_interface_after_lock_acquisition(self):
        from dcim.models import Interface

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.intent_state import RendererTargetsChanged
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes

        self._make_mgmt()
        source = Interface.objects.create(device=self.device, name="Loopback202", type="virtual")
        peer = self._peer_entry(source=source.name)
        payload = self._payload(self._router_payload(peers=[peer]))
        plan = bgp_reconcile_plan(self.device, payload)
        source.name = "Loopback203"
        source.save(update_fields=["name"])

        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with self.assertRaises(RendererTargetsChanged), mutation(plan):
            _reconcile_bgp_config(self.device, payload)

    def test_plan_revalidates_a_deleted_source_interface_after_lock_acquisition(self):
        from dcim.models import Interface

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.intent_state import RendererTargetsChanged
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes

        self._make_mgmt()
        source = Interface.objects.create(device=self.device, name="Loopback204", type="virtual")
        peer = self._peer_entry(source=source.name)
        payload = self._payload(self._router_payload(peers=[peer]))
        plan = bgp_reconcile_plan(self.device, payload)
        source.delete()

        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with self.assertRaises(RendererTargetsChanged), mutation(plan):
            _reconcile_bgp_config(self.device, payload)

    def test_plan_locks_and_uses_an_unchanged_source_interface(self):
        from dcim.models import Interface
        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
        from netbox_nso_plugin.intent_state import SourceRow
        from netbox_nso_plugin.renderer_writer import renderer_mirror_writes, renderer_writes

        self._make_mgmt()
        source = Interface.objects.create(device=self.device, name="Loopback205", type="virtual")
        peer = self._peer_entry(source=source.name)
        payload = self._payload(self._router_payload(peers=[peer]))
        plan = bgp_reconcile_plan(self.device, payload)

        self.assertIn(SourceRow("dcim.device", self.device.pk), plan.lock_footprint.source_rows)
        self.assertIn(SourceRow("dcim.interface", None), plan.lock_footprint.source_rows)
        self.assertIn(SourceRow("dcim.interface", source.pk), plan.lock_footprint.source_rows)
        mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
        with mutation(plan):
            _reconcile_bgp_config(self.device, payload)

        self.assertEqual(BGPPeer.objects.get().update_source, source)

    def test_replay_operations_are_built_after_writer_activation(self):
        from netbox_nso_plugin.bgp_reconciler import _bgp_reconcile_operations, _reconcile_bgp_config
        from netbox_nso_plugin.renderer_writer import active_renderer_writer

        self._make_mgmt()
        writer_states = []

        def observe_writer(*args, **kwargs):
            writer_states.append(active_renderer_writer() is not None)
            return _bgp_reconcile_operations(*args, **kwargs)

        with patch("netbox_nso_plugin.bgp_reconciler._bgp_reconcile_operations", side_effect=observe_writer):
            _reconcile_bgp_config(self.device, {"routers": []})

        self.assertEqual(writer_states, [False, True])

    def test_plan_locks_all_devices_that_share_a_route_map_without_expanding_revisions(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import RouteMap

        from netbox_nso_plugin.bgp_reconciler import bgp_reconcile_plan
        from netbox_nso_plugin.models import NSORoutePolicyState

        self._make_mgmt()
        other_device = _make_bgp_device("policy-target")
        other_management = self._make_mgmt(other_device)
        route_map = RouteMap.objects.create(name="RM-SHARED")
        NSORoutePolicyState.objects.create(
            management=other_management,
            content_type=ContentType.objects.get_for_model(RouteMap),
            object_id=route_map.pk,
            family="route_map",
            object_name=route_map.name,
        )
        payload = self._payload(
            self._router_payload(
                peers=[
                    self._peer_entry(
                        address_families=[
                            {
                                "af": "ipv4-unicast",
                                "enabled": True,
                                "routemap_in": route_map.name,
                            }
                        ]
                    )
                ]
            )
        )

        footprint = bgp_reconcile_plan(self.device, payload).lock_footprint

        self.assertEqual(set(footprint.device_ids), {self.device.pk, other_device.pk})
        self.assertEqual(set(footprint.revision_keys), {(self.device.pk, "bgp")})

    def test_plan_ignores_address_family_rows_owned_by_another_content_type(self):
        self._make_mgmt()

        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import BGPPeer, BGPPeerAddressFamily

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan

        payload = self._payload(self._router_payload(peers=[self._peer_entry()]))
        _reconcile_bgp_config(self.device, payload)
        peer = BGPPeer.objects.get()
        peer_address_family = BGPPeerAddressFamily.objects.get()
        colliding = BGPPeerAddressFamily.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=peer.pk,
            address_family=peer_address_family.address_family,
            enabled=True,
        )

        changed_payload = self._payload(
            self._router_payload(peers=[self._peer_entry(address_families=[{"af": "ipv4-unicast", "enabled": False}])])
        )
        planned_rows = {
            (write.model_label, write.pk) for write in bgp_reconcile_plan(self.device, changed_payload).write_set
        }

        self.assertIn((peer_address_family._meta.label_lower, peer_address_family.pk), planned_rows)
        self.assertNotIn((colliding._meta.label_lower, colliding.pk), planned_rows)

    def test_device_change_auto_mirrors_when_netbox_untouched(self):
        """3-way: device-side change with NetBox untouched → object auto-updated, in sync.

        This is what the 3-way base restores over plain freeze: a row the operator
        never edited keeps tracking the device automatically.
        """
        self._make_mgmt()
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        try:
            from netbox_routing.models import BGPPeer
        except ImportError:
            self.skipTest("netbox_routing not installed")

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry(ttl=1)])))
        peer = BGPPeer.objects.get()
        self.assertEqual(peer.ttl, 1)

        # Device changes ttl 1→2; NetBox was never touched → auto-mirror.
        result = _reconcile_bgp_config(
            self.device, self._payload(self._router_payload(peers=[self._peer_entry(ttl=2)]))
        )
        peer.refresh_from_db()
        self.assertEqual(peer.ttl, 2)  # auto-mirrored to the new device value
        self.assertEqual(result[0].status, "imported")  # unowned + matches → in sync, no drift

    def test_both_moved_is_conflict(self):
        """3-way: NetBox edited AND device changed since base → conflict, edit preserved."""
        self._make_mgmt()
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        try:
            from netbox_routing.models import BGPPeer
        except ImportError:
            self.skipTest("netbox_routing not installed")

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry(ttl=1)])))
        peer = BGPPeer.objects.get()
        peer.ttl = 99  # operator edit
        peer.save()

        # Device ALSO moved ttl 1→2: both sides diverged from base → conflict.
        result = _reconcile_bgp_config(
            self.device, self._payload(self._router_payload(peers=[self._peer_entry(ttl=2)]))
        )
        self.assertEqual(result[0].status, "conflict")
        peer.refresh_from_db()
        self.assertEqual(peer.ttl, 99)  # operator edit preserved (never clobbered)

    def test_invalid_asn_is_a_typed_adapter_error(self):
        """A router with an invalid ASN fails at the adapter boundary."""
        self._make_mgmt()

        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        with self.assertRaises(AdapterError) as raised:
            _reconcile_bgp_config(
                self.device,
                {"device_id": self.device.pk, "routers": [{"asn": "not-a-number", "scopes": []}]},
            )

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_multiple_routers(self):
        """Two routers (different ASNs) → two BGPRouter objects, two state rows each peer."""
        self._make_mgmt()

        from netbox_routing.models import BGPRouter

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        result = _reconcile_bgp_config(
            self.device,
            self._payload(
                self._router_payload(asn="65100", peers=[self._peer_entry("10.0.2.1")]),
                self._router_payload(asn="65200", peers=[self._peer_entry("10.0.2.2")]),
            ),
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(BGPRouter.objects.filter(assigned_object_id=self.device.pk).count(), 2)

    def test_remote_as_linked(self):
        """remote_as in peer entry → ipam.ASN created and linked to BGPPeer."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry("10.0.3.1", remote_as="64999")])),
        )

        peer = BGPPeer.objects.filter(peer__address__net_host="10.0.3.1").first()
        self.assertIsNotNone(peer)
        self.assertIsNotNone(peer.remote_as)
        self.assertEqual(peer.remote_as.asn, 64999)

    def test_local_as_linked(self):
        """local_as in peer entry → ipam.ASN created and linked to BGPPeer.local_as."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry("10.0.4.1", local_as="65001")])),
        )

        peer = BGPPeer.objects.filter(peer__address__net_host="10.0.4.1").first()
        self.assertIsNotNone(peer)
        self.assertIsNotNone(peer.local_as)
        self.assertEqual(peer.local_as.asn, 65001)

    def test_peer_group_created_and_linked(self):
        """peer_group name in peer entry → BGPPeerTemplate created and linked."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer, BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry("10.0.5.1", peer_group="EBGP-UPSTREAM")])),
        )

        peer = BGPPeer.objects.filter(peer__address__net_host="10.0.5.1").first()
        self.assertIsNotNone(peer.peer_group)
        self.assertEqual(peer.peer_group.name, "EBGP-UPSTREAM")
        self.assertEqual(BGPPeerTemplate.objects.filter(name="EBGP-UPSTREAM").count(), 1)

    def test_peer_group_shared_across_peers(self):
        """Two peers with the same peer-group name share one BGPPeerTemplate."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeerTemplate

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(
                self._router_payload(
                    peers=[
                        self._peer_entry("10.0.6.1", peer_group="IBGP"),
                        self._peer_entry("10.0.6.2", peer_group="IBGP"),
                    ]
                )
            ),
        )

        self.assertEqual(BGPPeerTemplate.objects.filter(name="IBGP").count(), 1)

    def test_scope_address_family_created(self):
        """Scope-level address families → BGPAddressFamily created."""
        self._make_mgmt()

        from netbox_routing.models import BGPAddressFamily

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(
            self.device,
            self._payload(
                self._router_payload(
                    address_families=["ipv4-unicast", "ipv6-unicast"],
                    peers=[],
                )
            ),
        )

        self.assertEqual(BGPAddressFamily.objects.count(), 2)
        afs = set(BGPAddressFamily.objects.values_list("address_family", flat=True))
        self.assertEqual(afs, {"ipv4-unicast", "ipv6-unicast"})
