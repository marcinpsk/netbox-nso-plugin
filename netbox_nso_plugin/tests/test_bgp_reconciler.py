# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for M15 A4: adapter_client.get_bgp_config and _reconcile_bgp_config."""

import unittest
from unittest.mock import MagicMock, patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

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
        response = MagicMock()
        response.ok = status < 400
        response.status_code = status
        response.content = b"{}"
        response.text = ""
        response.json.return_value = json_data or {}
        session = MagicMock()
        session.request.return_value = response
        return session

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
    def test_404_returns_empty_routers(self, mock_session_cls, _mock_cfg):
        from netbox_nso_plugin.adapter_client import get_bgp_config

        session = self._make_session(status=404, json_data={})
        mock_session_cls.return_value = session

        result = get_bgp_config(7)
        self.assertEqual(result, {"device_id": 7, "routers": []})

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


class TestReconcileBgpConfig(TestCase):
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

    def _router_payload(self, asn="65100", vrf="", peers=None, address_families=None):
        return {
            "asn": asn,
            "scopes": [
                {
                    "vrf": vrf,
                    "address_families": address_families if address_families is not None else ["ipv4-unicast"],
                    "peers": peers if peers is not None else [],
                }
            ],
        }

    def _payload(self, *routers):
        return {"device_id": self.device.pk, "routers": list(routers)}

    # ── Basic cases ────────────────────────────────────────────────────────────

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
        self.assertEqual(state.status, "in_sync")
        self.assertEqual(state.asn_str, "65100")
        self.assertEqual(state.peer_address_str, "10.0.0.2")
        self.assertEqual(state.management, mgmt)
        self.assertIsNotNone(state.bgp_peer)
        self.assertTrue(state.enabled)

    def test_idempotent_second_call(self):
        """Calling reconcile twice with same payload → same single state row."""
        self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))
        result = _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "in_sync")

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
        self.assertEqual(statuses["10.0.1.1"], "in_sync")
        self.assertEqual(statuses["10.0.1.2"], "changed")

    def test_write_path_status_preserved(self):
        """Rows in accepted/deploying/in_sync are not overwritten to imported."""
        mgmt = self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
        from netbox_nso_plugin.models import NSOBGPPeerState

        # First call to create the row
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[self._peer_entry()])))

        # Force status to 'accepted'
        NSOBGPPeerState.objects.filter(management=mgmt).update(status="accepted")

        # Second call — should NOT revert to 'imported'
        result = _reconcile_bgp_config(
            self.device,
            self._payload(self._router_payload(peers=[self._peer_entry()])),
        )
        self.assertEqual(result[0].status, "accepted")

    def test_invalid_asn_skipped(self):
        """Router with invalid ASN string → silently skipped."""
        self._make_mgmt()

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        result = _reconcile_bgp_config(
            self.device,
            {"device_id": self.device.pk, "routers": [{"asn": "not-a-number", "scopes": []}]},
        )
        self.assertEqual(result, [])

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
