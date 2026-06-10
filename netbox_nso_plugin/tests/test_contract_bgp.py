# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Contract test — consumer side of GET /api/v1/devices/{id}/bgp-config.

Pins the JSON shape the plugin CONSUMES in ``bgp_reconciler._reconcile_bgp_config``
(peers + per-AF policies + peer-group templates) against the documented adapter
contract. The adapter is the producer; if it renames/removes a key the plugin
depends on, the plugin silently degrades (a peer loses its remote-AS, a template its
policies) — the device-27 class of "looks fine, is wrong" bug. This test plus its
producer mirror make that break visible on at least one side.

The BGP serializer OMITS optional keys when unset (not ``null``), so the contract is
"required keys always present; extras only from the documented optional set" at every
nesting level.

Canonical contract: ``nso-adapter/docs/api-contract.md`` § "GET .../bgp-config".
Mirror (producer side): ``nso-adapter/tests/api/test_contract_bgp.py`` — the ``*_KEYS``
sets MUST stay identical across both files.
"""

from __future__ import annotations

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
from netbox_nso_plugin.models import NSOBGPPeerState, NSOBGPPeerTemplateState, NSODeviceManagement, NSOInstance

# ── The contract. MUST match nso-adapter/tests/api/test_contract_bgp.py exactly. ──
REQUIRED_TOP_KEYS = {"device_id", "last_refreshed_at", "refresh_source", "routers"}
REQUIRED_ROUTER_KEYS = {"asn", "scopes"}
REQUIRED_SCOPE_KEYS = {"vrf", "address_families", "peers", "peer_groups"}
REQUIRED_PEER_KEYS = {"peer_address", "enabled", "address_families"}
OPTIONAL_PEER_KEYS = {"peer_group", "remote_as", "local_as", "ttl", "password", "source", "bfd_enabled"}
REQUIRED_PEER_AF_KEYS = {"af", "enabled"}
OPTIONAL_PEER_AF_KEYS = {"routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out"}
REQUIRED_PG_KEYS = {"name", "address_families"}
OPTIONAL_PG_KEYS = {"remote_as", "source"}
REQUIRED_PG_AF_KEYS = {"af"}
OPTIONAL_PG_AF_KEYS = {"routemap_in", "routemap_out", "prefixlist_in", "prefixlist_out"}

# One bgp-config response exactly as docs/api-contract.md documents the adapter emitting
# it: a maximal peer (every optional key), a minimal peer (only required), and a
# peer-group template with its own per-AF policies.
CONTRACT_PAYLOAD = {
    "device_id": 7910,
    "last_refreshed_at": "2026-06-01T10:00:00Z",
    "refresh_source": "poll",
    "routers": [
        {
            "asn": "65100",
            "scopes": [
                {
                    "vrf": "",
                    "address_families": ["ipv4-unicast", "ipv6-unicast"],
                    "peers": [
                        {
                            "peer_address": "192.0.2.1",
                            "enabled": False,
                            "peer_group": "UPSTREAM",
                            "remote_as": "65001",
                            "local_as": "65100",
                            "ttl": 2,
                            "password": "s3cret",
                            "source": "Loopback0",
                            "bfd_enabled": True,
                            "address_families": [
                                {
                                    "af": "ipv4-unicast",
                                    "enabled": True,
                                    "routemap_in": "RM-IN",
                                    "routemap_out": "RM-OUT",
                                    "prefixlist_in": "PL-IN",
                                    "prefixlist_out": "PL-OUT",
                                }
                            ],
                        },
                        {
                            "peer_address": "192.0.2.2",
                            "enabled": True,
                            "address_families": [{"af": "ipv4-unicast", "enabled": True}],
                        },
                    ],
                    "peer_groups": [
                        {
                            "name": "UPSTREAM",
                            "remote_as": "65001",
                            "source": "Loopback0",
                            "address_families": [
                                {
                                    "af": "ipv4-unicast",
                                    "routemap_in": "RM-IN",
                                    "routemap_out": "RM-OUT",
                                    "prefixlist_in": "PL-IN",
                                    "prefixlist_out": "PL-OUT",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}


class TestBgpConfigContractConsumer(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="BgpCt", slug="bgpct")
        dt = DeviceType.objects.create(manufacturer=mfg, model="BgpCtDev", slug="bgpctdev")
        role = DeviceRole.objects.create(name="BgpCtRole", slug="bgpctrole")
        site = Site.objects.create(name="BgpCtSite", slug="bgpctsite")
        cls.device = Device.objects.create(name="bgp-ct-rtr", device_type=dt, role=role, site=site)
        inst = NSOInstance.objects.create(name="bgp-ct-inst", adapter_instance_id="bgp-ct-inst")
        cls.mgmt = NSODeviceManagement.objects.create(
            device=cls.device, nso_instance=inst, nso_device_name="bgp-ct", adapter_device_id=7910
        )

    def test_payload_keys_match_the_pinned_contract(self):
        """The example mirrors the producer's pinned key sets (keeps the mirror honest)."""
        self.assertEqual(set(CONTRACT_PAYLOAD.keys()), REQUIRED_TOP_KEYS)
        router = CONTRACT_PAYLOAD["routers"][0]
        self.assertEqual(set(router.keys()), REQUIRED_ROUTER_KEYS)
        scope = router["scopes"][0]
        self.assertEqual(set(scope.keys()), REQUIRED_SCOPE_KEYS)

        peers = {p["peer_address"]: p for p in scope["peers"]}
        self.assertEqual(set(peers["192.0.2.1"].keys()), REQUIRED_PEER_KEYS | OPTIONAL_PEER_KEYS)
        self.assertEqual(set(peers["192.0.2.2"].keys()), REQUIRED_PEER_KEYS)  # optionals omitted
        self.assertEqual(
            set(peers["192.0.2.1"]["address_families"][0].keys()), REQUIRED_PEER_AF_KEYS | OPTIONAL_PEER_AF_KEYS
        )

        pg = scope["peer_groups"][0]
        self.assertEqual(set(pg.keys()), REQUIRED_PG_KEYS | OPTIONAL_PG_KEYS)
        self.assertEqual(set(pg["address_families"][0].keys()), REQUIRED_PG_AF_KEYS | OPTIONAL_PG_AF_KEYS)

    def test_consumer_reads_contract_payload(self):
        """_reconcile_bgp_config ingests the documented shape into the BGP graph + overlays."""
        try:
            from netbox_routing.models import BGPPeer, BGPPeerTemplate, PrefixList, RouteMap
        except ImportError:
            self.skipTest("netbox_routing not installed")

        # Pre-create the policy objects the per-AF keys reference, so the links resolve
        # (proves routemap_in/out + prefixlist_in/out are actually consumed).
        for name in ("RM-IN", "RM-OUT"):
            RouteMap.objects.create(name=name)
        for name in ("PL-IN", "PL-OUT"):
            PrefixList.objects.create(name=name)

        result = _reconcile_bgp_config(self.device, CONTRACT_PAYLOAD)

        # Both peers materialised as overlay rows + netbox_routing BGPPeer objects.
        self.assertEqual(NSOBGPPeerState.objects.filter(management=self.mgmt).count(), 2)
        self.assertEqual(len(result), 2)
        maximal = NSOBGPPeerState.objects.get(management=self.mgmt, peer_address_str="192.0.2.1")
        self.assertEqual(maximal.remote_as_str, "65001")
        self.assertFalse(maximal.enabled)  # enabled=false consumed
        self.assertIsNotNone(maximal.bgp_peer)

        # Per-AF policies resolved by name onto the BGPPeer's AF row.
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import BGPPeerAddressFamily

        ct = ContentType.objects.get_for_model(BGPPeer)
        paf = BGPPeerAddressFamily.objects.get(assigned_object_type=ct, assigned_object_id=maximal.bgp_peer_id)
        self.assertEqual(paf.routemap_in.name, "RM-IN")
        self.assertEqual(paf.prefixlist_out.name, "PL-OUT")

        # Peer-group template overlay + its AF policies.
        tmpl_state = NSOBGPPeerTemplateState.objects.get(management=self.mgmt, template_name="UPSTREAM")
        self.assertEqual(tmpl_state.remote_as_str, "65001")
        tmpl = BGPPeerTemplate.objects.get(name="UPSTREAM")
        tct = ContentType.objects.get_for_model(BGPPeerTemplate)
        tpaf = BGPPeerAddressFamily.objects.get(assigned_object_type=tct, assigned_object_id=tmpl.pk)
        self.assertEqual(tpaf.routemap_in.name, "RM-IN")
