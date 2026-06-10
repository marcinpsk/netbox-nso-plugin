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
        self.assertEqual(state.status, "imported")  # unowned, materialized → imported (unified)
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

        src = IPAddress.objects.create(address="84.116.255.1/32")
        peer = self._peer_entry()
        peer["source"] = "84.116.255.1"
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        bp = BGPPeer.objects.get(peer__address__net_host="10.0.0.2")
        self.assertEqual(bp.source, src)

    def test_peer_source_unknown_ip_left_null(self):
        """source IP not present in IPAM → BGPPeer.source stays null (not fabricated)."""
        self._make_mgmt()

        from netbox_routing.models import BGPPeer

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

        peer = self._peer_entry()
        peer["source"] = "203.0.113.250"
        _reconcile_bgp_config(self.device, self._payload(self._router_payload(peers=[peer])))

        self.assertIsNone(BGPPeer.objects.get(peer__address__net_host="10.0.0.2").source_id)

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

    def _scope_with_peer_groups(self, peer_groups, asn="65100", address_families=None):
        """Build a payload whose scope carries peer_groups (full-B objects)."""
        return {
            "device_id": self.device.pk,
            "routers": [
                {
                    "asn": asn,
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

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
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
        _reconcile_bgp_config(self.device, self._scope_with_peer_groups([pg_b]))
        paf.refresh_from_db()
        self.assertEqual(paf.routemap_in.name, "RM-B")  # mirrored to new device value
        state = NSOBGPPeerTemplateState.objects.get(management__device=self.device, template_name="PG")
        self.assertEqual(state.status, "imported")  # unowned + matches → no drift

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
        NSOBGPPeerState.objects.filter(management=mgmt).update(status="accepted")

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
