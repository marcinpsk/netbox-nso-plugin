# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Explicit and foreign BGP peer ownership behavior through real Django models.

The in-tab add workflow creates owned intent through one exact graph plan. Direct native
events remain foreign and do not acquire or retire ownership. The adapter HTTP call is the
only mocked boundary.
"""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from ipam.models import ASN, RIR, IPAddress
from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

from netbox_nso_plugin.models import NSOBGPPeerState, NSODeviceManagement, NSOInstance

from .mixins import IntentPushDeliveryMixin, IntentPushResetMixin, _CascadeFlushMixin


def _find_pushed_peer(router_list, addr):
    """Locate a peer entry by address in a pushed BGP router_list snapshot."""
    for router in router_list:
        for scope in router.get("scopes", []):
            for peer in scope.get("peers", []):
                if peer.get("peer_address") == addr:
                    return peer
    return None


def _make_device(suffix="gf"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"BgpGfMfg{suffix}", slug=f"bgpgfmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"BgpGfDev{suffix}", slug=f"bgpgfdev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"BgpGfRole{suffix}", slug=f"bgpgfrole{suffix}")
    from dcim.models import Site

    site, _ = Site.objects.get_or_create(name=f"BgpGfSite{suffix}", slug=f"bgpgfsite{suffix}")
    return Device.objects.create(name=f"bgp-gf-{suffix}", device_type=dt, role=role, site=site)


class BgpGreenfieldBase(IntentPushDeliveryMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("main")
        cls.rir, _ = RIR.objects.get_or_create(name="GF-RIR", slug="gf-rir")
        cls.asn = ASN.objects.create(asn=65100, rir=cls.rir)
        cls.remote_asn = ASN.objects.create(asn=65200, rir=cls.rir)
        cls.inst, _ = NSOInstance.objects.get_or_create(
            name="bgp-gf-inst", defaults={"adapter_instance_id": "bgp-gf-inst"}
        )

    def _mgmt(self, device=None, adapter_device_id=99):
        device = device or self.device
        return NSODeviceManagement.objects.get_or_create(
            device=device,
            defaults={
                "nso_instance": self.inst,
                "nso_device_name": "bgp-gf-dev",
                "adapter_device_id": adapter_device_id,
            },
        )[0]

    def _router(self, device=None, asn=None):
        device = device or self.device
        asn = asn or self.asn
        return BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=device.pk,
            asn=asn,
            name=str(asn.asn),
        )

    def _scope(self, router, vrf=None):
        return BGPScope.objects.create(router=router, vrf=vrf)

    def _ip(self, addr="10.0.0.2/32"):
        return IPAddress.objects.create(address=addr)

    def _create_peer(self, scope, ip, remote_as=None, **kwargs):
        """Create a BGPPeer inside the on-commit capture, patching the adapter boundary."""
        with (
            patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            peer = BGPPeer.objects.create(
                scope=scope, peer=ip, name=None, remote_as=remote_as or self.remote_asn, enabled=True, **kwargs
            )
        return peer, mock_put


class TestBgpPeerGreenfieldCreate(BgpGreenfieldBase):
    def test_state_save_without_writer_does_not_load_management(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_nso_plugin.renderer_writer import RendererMutationPlan, planned_save, renderer_mirror_writes
        from netbox_nso_plugin.signals import _on_bgp_peer_state_save, suppress_intent_push

        mgmt = self._mgmt()
        state = NSOBGPPeerState(
            management=mgmt,
            asn_str="65100",
            vrf_name="",
            peer_address_str="198.18.0.31",
        )
        plan = RendererMutationPlan.build(
            saves=(
                planned_save(
                    state,
                    force_insert=True,
                    natural_key=("management", "asn_str", "vrf_name", "peer_address_str"),
                ),
            )
        )
        with renderer_mirror_writes(plan) as writer, suppress_intent_push():
            writer.save(state, force_insert=True)
        state = NSOBGPPeerState.objects.only("pk", "management_id").get(pk=state.pk)

        with CaptureQueriesContext(connection) as captured:
            _on_bgp_peer_state_save(sender=NSOBGPPeerState, instance=state)

        self.assertEqual(captured.captured_queries, [])

    def test_foreign_native_create_does_not_acquire_or_push(self):
        self._mgmt()
        scope = self._scope(self._router())
        ip = self._ip("10.0.0.2/32")
        _peer, mock_put = self._create_peer(scope, ip)

        self.assertFalse(NSOBGPPeerState.objects.exists())
        mock_put.assert_not_called()

    def test_peer_on_unmanaged_device_is_noop(self):
        # No NSODeviceManagement for this device → no overlay, no push.
        other = _make_device("unmanaged")
        scope = self._scope(self._router(device=other))
        ip = self._ip("10.0.0.9/32")
        _peer, mock_put = self._create_peer(scope, ip)
        self.assertFalse(NSOBGPPeerState.objects.filter(peer_address_str="10.0.0.9").exists())
        mock_put.assert_not_called()

    def test_foreign_native_edit_does_not_repush_owned_overlay(self):
        mgmt = self._mgmt()
        scope = self._scope(self._router())
        ip = self._ip("10.0.0.3/32")
        peer, _ = self._create_peer(scope, ip)
        state = NSOBGPPeerState.objects.create(
            management=mgmt,
            asn_str="65100",
            vrf_name="",
            peer_address_str="10.0.0.3",
            bgp_peer=peer,
            status="accepted",
        )
        with (
            patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            peer.local_as = self.asn
            peer.save()
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")
        mock_put.assert_not_called()

    def test_edit_brownfield_peer_not_force_owned(self):
        """Editing a brownfield-adopted (imported, unowned) peer must NOT force-own it.

        The change surfaces via the 3-way reconcile as 'changed' for an explicit Accept —
        it is never auto-promoted to accepted or pushed. Guards the greenfield-only rule in
        the native save handler (without the foreign-write rule, this edit would promote
        imported→accepted and push)."""
        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.signals import suppress_intent_push

        mgmt = self._mgmt()
        scope = self._scope(self._router())
        ip = self._ip("10.0.0.5/32")
        # Simulate brownfield adoption: peer + imported overlay materialized under suppression
        # (exactly how the reconciler creates them), so no greenfield ownership is taken.
        peer = BGPPeer(scope=scope, peer=ip, name=None, remote_as=self.remote_asn, enabled=True)
        with suppress_intent_push(), intent_transaction(footprint_for_instance(peer)):
            peer.save(force_insert=True)
            NSOBGPPeerState.objects.create(
                management=mgmt,
                asn_str="65100",
                vrf_name="",
                peer_address_str="10.0.0.5",
                status="imported",
                bgp_peer=peer,
            )

        with (
            patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            peer.ttl = 5  # an operator edit (not suppressed)
            peer.save()

        state = NSOBGPPeerState.objects.get(management=mgmt, peer_address_str="10.0.0.5")
        self.assertEqual(state.status, "imported")  # left brownfield, not force-owned
        mock_put.assert_not_called()  # unowned → no intent push

    def test_greenfield_key_matches_reconcile_no_duplicate(self):
        """The greenfield overlay key must equal reconcile's, so a later reconcile of the
        same peer reuses the one overlay (no duplicate) and preserves the accepted status."""
        mgmt = self._mgmt()
        ip = self._ip("10.0.0.2/32")
        from netbox_nso_plugin.views import NSOBgpPeerCreateView

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent"), self.captureOnCommitCallbacks(execute=True):
            NSOBgpPeerCreateView._create_peer(
                self.device,
                {
                    "local_asn": self.asn,
                    "peer": ip,
                    "remote_as": self.remote_asn,
                    "peer_local_as": self.asn,
                    "enabled": True,
                    "address_families": ["ipv4-unicast"],
                },
            )
        self.assertEqual(NSOBGPPeerState.objects.filter(management=mgmt).count(), 1)

        from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

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
                            "peers": [
                                {
                                    "peer_address": "10.0.0.2",
                                    "enabled": True,
                                    "remote_as": "65200",
                                    "address_families": [{"af": "ipv4-unicast", "enabled": True}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        _reconcile_bgp_config(self.device, payload)
        # Still exactly one overlay for this key, and the operator's accepted ownership survived.
        rows = NSOBGPPeerState.objects.filter(
            management=mgmt, asn_str="65100", vrf_name="", peer_address_str="10.0.0.2"
        )
        self.assertEqual(rows.count(), 1)
        self.assertIn(rows.first().status, ("accepted", "deploying", "in_sync", "apply_failed"))


class TestBgpPeerGreenfieldDelete(BgpGreenfieldBase):
    def test_foreign_native_delete_detaches_overlay_without_push(self):
        mgmt = self._mgmt()
        scope = self._scope(self._router())
        ip = self._ip("10.0.0.2/32")
        peer, _ = self._create_peer(scope, ip)
        state = NSOBGPPeerState.objects.create(
            management=mgmt,
            asn_str="65100",
            vrf_name="",
            peer_address_str="10.0.0.2",
            bgp_peer=peer,
            status="imported",
        )

        with (
            patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            peer.delete()

        state.refresh_from_db()
        self.assertIsNone(state.bgp_peer_id)
        mock_put.assert_not_called()

    def test_push_omits_owned_overlay_after_foreign_peer_delete(self):
        mgmt = self._mgmt()
        scope = self._scope(self._router())
        ip = self._ip("198.18.0.4/32")
        peer, _mock_put = self._create_peer(scope, ip)
        state = NSOBGPPeerState.objects.create(
            management=mgmt,
            asn_str="65100",
            vrf_name="",
            peer_address_str="198.18.0.4",
            bgp_peer=peer,
            status="in_sync",
        )

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put:
            peer.delete()

        state.refresh_from_db()
        self.assertIsNone(state.bgp_peer_id)
        mock_put.assert_not_called()

        from netbox_nso_plugin.delivery import deliver

        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put:
            deliver("bgp", self.device.pk, mgmt.adapter_device_id)

        _adapter_id, router_list = mock_put.call_args.args
        self.assertIsNone(_find_pushed_peer(router_list, "198.18.0.4"))


class TestBgpPeerAddView(BgpGreenfieldBase):
    """The in-tab "Add BGP peer" form/view: a POST builds the router→scope→peer→AF graph,
    owns an accepted overlay and pushes — end-to-end through the real HTTP view + signals."""

    def _login_superuser(self):
        from users.models import User

        self.client.force_login(User.objects.create_user("bgpgfadmin", is_superuser=True))

    def test_add_peer_builds_graph_owns_and_pushes(self):
        mgmt = self._mgmt()
        ip = self._ip("10.0.0.7/32")
        self._login_superuser()

        url = reverse("plugins:netbox_nso_plugin:bgp_peer_add", kwargs={"device_pk": self.device.pk})
        with (
            patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put,
            self.captureOnCommitCallbacks(execute=True),
        ):
            resp = self.client.post(
                url,
                {
                    "local_asn": self.asn.pk,
                    "peer": ip.pk,
                    "remote_as": self.remote_asn.pk,
                    "peer_local_as": self.asn.pk,
                    "ttl": 3,
                    "address_families": ["ipv4-unicast"],
                    "enabled": "on",
                },
            )
        self.assertEqual(resp.status_code, 302)

        # router → scope → peer graph, matching the reconciler's identity.
        router = BGPRouter.objects.get(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=self.asn,
        )
        scope = BGPScope.objects.get(router=router, vrf__isnull=True)
        peer = BGPPeer.objects.get(scope=scope, peer=ip)
        # WS-A per-peer fields must land on the model (ttl + local-as).
        self.assertEqual(peer.ttl, 3)
        self.assertEqual(peer.local_as_id, self.asn.pk)
        self.assertTrue(peer.enabled)
        # A default IPv4-unicast AF is created so the neighbor actually activates.
        from netbox_routing.models import BGPPeerAddressFamily

        self.assertTrue(
            BGPPeerAddressFamily.objects.filter(
                assigned_object_type=ContentType.objects.get_for_model(BGPPeer),
                assigned_object_id=peer.pk,
                address_family__address_family="ipv4-unicast",
            ).exists()
        )
        # Accepted overlay owned + push scheduled.
        state = NSOBGPPeerState.objects.get(management=mgmt, asn_str="65100", vrf_name="", peer_address_str="10.0.0.7")
        self.assertEqual(state.status, "accepted")
        self.assertEqual(state.bgp_peer_id, peer.pk)
        mock_put.assert_called()

    def test_add_peer_prefills_existing_local_asn(self):
        """GET pre-fills the local-ASN from a BGPRouter the device already has."""
        self._mgmt()
        self._router()  # existing BGPRouter for self.device with self.asn
        self._login_superuser()
        url = reverse("plugins:netbox_nso_plugin:bgp_peer_add", kwargs={"device_pk": self.device.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"].fields["local_asn"].initial, self.asn.pk)

    def test_add_peer_requires_permission(self):
        """An authenticated user without change_nsodevicemanagement is denied (403)."""
        from users.models import User

        self._mgmt()
        self.client.force_login(User.objects.create_user("bgpgfnoperm"))
        url = reverse("plugins:netbox_nso_plugin:bgp_peer_add", kwargs={"device_pk": self.device.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_add_peer_also_requires_the_netbox_routing_permission(self):
        """This view is a door into ANOTHER app: it mints a BGPRouter→BGPScope→BGPPeer graph
        in netbox_routing. Gating it on the NSO permission alone turned that permission into
        a back-door grant to create routing objects — a user who may manage NSO devices but
        was never given netbox_routing rights could still create BGP peers.
        """
        from core.models import ObjectType
        from netbox_routing.models import BGPPeer
        from users.models import ObjectPermission, User

        from netbox_nso_plugin.models import NSODeviceManagement

        self._mgmt()
        user = User.objects.create_user("bgpgfnsoonly")
        nso_perm = ObjectPermission.objects.create(name="bgpgf-change-mgmt", actions=["change"])
        nso_perm.object_types.add(ObjectType.objects.get_for_model(NSODeviceManagement))
        nso_perm.users.add(user)
        self.client.force_login(user)
        url = reverse("plugins:netbox_nso_plugin:bgp_peer_add", kwargs={"device_pk": self.device.pk})

        self.assertEqual(self.client.get(url).status_code, 403, "the NSO permission alone must not open this view")

        routing_perm = ObjectPermission.objects.create(name="bgpgf-add-peer", actions=["add"])
        routing_perm.object_types.add(ObjectType.objects.get_for_model(BGPPeer))
        routing_perm.users.add(user)
        self.client.force_login(User.objects.get(pk=user.pk))  # drop the cached permission set

        self.assertEqual(self.client.get(url).status_code, 200, "both permissions together must open it")


class TestBgpPeerAddViewPushCarriesAf(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """The greenfield push MUST carry the peer's address-family activation.

    Regression for a write-ordering bug device-caught on ra1.lab: the Add-BGP-peer view
    creates the ``BGPPeer`` (whose post_save fires the intent push) and only THEN attaches
    its address-families. In production the view runs OUTSIDE any transaction, so
    ``_schedule_intent_push`` runs the push INLINE at ``BGPPeer.create()`` time — before the
    AF rows exist — and the pushed intent carried an empty ``address_families``. The
    bgp-reconciler then writes a neighbor with no ``address-family ipv4 unicast``: the
    session comes up but negotiates no AF (an inert peer). Brownfield never hit this — the
    reconciler materialises peer + AFs together under ``suppress_intent_push`` before any
    push.

    This runs as a ``TransactionTestCase`` on purpose. A plain ``TestCase`` MASKS the bug:
    its per-test atomic wrapper makes ``connection.in_atomic_block`` True, so the push is
    deferred to ``on_commit`` and fires AFTER the AFs land. ``TransactionTestCase`` has no
    ambient atomic block, mirroring the real HTTP request — the push fires inline, exactly
    where the bug lives. The fix wraps ``_create_peer``'s graph build in
    ``transaction.atomic()`` so the push defers to ``on_commit`` (after the AFs).
    """

    def setUp(self):
        super().setUp()
        from users.models import User

        self.device = _make_device("txn")
        self.rir, _ = RIR.objects.get_or_create(name="GF-RIR-txn", slug="gf-rir-txn")
        self.asn = ASN.objects.create(asn=65100, rir=self.rir)
        self.remote_asn = ASN.objects.create(asn=65200, rir=self.rir)
        self.inst, _ = NSOInstance.objects.get_or_create(
            name="bgp-gf-inst-txn", defaults={"adapter_instance_id": "bgp-gf-inst-txn"}
        )
        self.mgmt = NSODeviceManagement.objects.create(
            device=self.device, nso_instance=self.inst, nso_device_name="bgp-gf-dev-txn", adapter_device_id=99
        )
        self.ip = IPAddress.objects.create(address="10.0.0.2/32")
        self.client.force_login(User.objects.create_user("bgpgftxnadmin", is_superuser=True))

    def test_push_carries_peer_address_family(self):
        url = reverse("plugins:netbox_nso_plugin:bgp_peer_add", kwargs={"device_pk": self.device.pk})
        with patch("netbox_nso_plugin.adapter_client.put_bgp_intent") as mock_put:
            resp = self.client.post(
                url,
                {
                    "local_asn": self.asn.pk,
                    "peer": self.ip.pk,
                    "remote_as": self.remote_asn.pk,
                    "peer_local_as": self.asn.pk,
                    "ttl": 3,
                    "address_families": ["ipv4-unicast"],
                    "enabled": "on",
                },
            )
        self.assertEqual(resp.status_code, 302)
        mock_put.assert_called()
        # The LAST push is the snapshot the adapter stores + applies. Its peer must carry
        # the ipv4-unicast AF, else the neighbor commits inert on-device.
        _adapter_id, router_list = mock_put.call_args.args
        peer_entry = _find_pushed_peer(router_list, "10.0.0.2")
        self.assertIsNotNone(peer_entry, "peer missing from pushed intent")
        self.assertEqual(
            peer_entry["address_families"],
            [{"af": "ipv4-unicast", "enabled": True}],
            "greenfield push dropped the peer's address-family → inert neighbor on-device",
        )
