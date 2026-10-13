# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Capture public prior errors and refuse errors that contain emitted credentials."""

from dcim.models import Device
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TransactionTestCase
from ipam.models import ASN, RIR, IPAddress
from netbox_routing.models import (
    BGPAddressFamily,
    BGPPeer,
    BGPRouter,
    BGPScope,
    Community,
    CommunityList,
    CommunityListEntry,
    ISISInstance,
)

from netbox_nso_plugin import drain
from netbox_nso_plugin.models import (
    NSOAcceptedSelection,
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSOBGPPeerState,
    NSOISISInstanceState,
    NSORoutePolicyState,
)

from ._outbox_case import ReceiptAdapter, make_managed, mirror_update, without_commit_drain
from ._ownership_case import acquire_bgp_router, acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


class TestDurableSecretCapture(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("secret-capture", 1767)
        self.adapter = ReceiptAdapter()

    def _isis(self, field, secret):
        auth = {field: secret, field.removesuffix("_key") + "_type": "md5"}
        with without_commit_drain(), transaction.atomic():
            instance = ISISInstance.objects.create(
                device=self.device,
                process_tag="placeholder-process",
                net="49.0001.0000.0000.0001.00",
                **auth,
            )
            row = acquire_overlay(
                NSOISISInstanceState,
                management=self.management,
                isis_instance=instance,
                process_tag=instance.process_tag,
                net=instance.net,
                status="accepted",
                **auth,
            )
        return mirror_update(row, last_apply_error=f"placeholder prior error contains {secret}")

    def _drain(self):
        config, session = self.adapter.patches()
        with config, session:
            return drain.drain_intent_outbox()

    def _assert_capture_refused(self, row):
        result = self._drain()
        self.assertEqual(result, (0, 1), "An emitted credential in prior error must abort capture")
        self.assertEqual(self.adapter.applied, [])
        self.assertFalse(NSOAcceptedSelection.objects.filter(management=self.management).exists())
        self.assertFalse(NSOApplyAttempt.objects.filter(management=self.management, trigger_kind="push").exists())
        self.assertFalse(NSOApplyOverlaySnapshot.objects.filter(attempt__management=self.management).exists())
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")

    def test_isis_area_auth_key_in_prior_error_blocks_capture(self):
        row = self._isis("area_auth_key", "placeholder-area-secret")

        self._assert_capture_refused(row)

    def test_isis_domain_auth_key_in_prior_error_blocks_capture(self):
        row = self._isis("domain_auth_key", "placeholder-domain-secret")

        self._assert_capture_refused(row)

    def test_escaped_bgp_password_in_prior_error_blocks_capture(self):
        secret = 'placeholder"\\secret'
        with without_commit_drain(), transaction.atomic():
            rir = RIR.objects.create(name="Placeholder private ASNs", slug="placeholder-private-asns", is_private=True)
            asn = ASN.objects.create(asn=64512, rir=rir)
            router = BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(Device),
                assigned_object_id=self.device.pk,
                asn=asn,
                router_id="198.18.0.1",
            )
            scope = BGPScope.objects.create(router=router)
            BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
            acquire_bgp_router(self.management, router)
            peer = BGPPeer.objects.create(
                scope=scope,
                peer=IPAddress.objects.create(address="198.18.0.2/32"),
                password=secret,
                name=None,
            )
            row = acquire_overlay(
                NSOBGPPeerState,
                management=self.management,
                bgp_peer=peer,
                asn_str="64512",
                vrf_name="",
                peer_address_str="198.18.0.2",
                status="accepted",
            )
        row = mirror_update(row, last_apply_error=f"placeholder prior error contains {secret}")

        self._assert_capture_refused(row)

    def test_public_policy_community_error_has_durable_snapshot(self):
        with without_commit_drain(), transaction.atomic():
            policy = CommunityList.objects.create(name="placeholder-community-policy")
            community = Community.objects.create(community="64512:42")
            CommunityListEntry.objects.create(community_list=policy, community=community, action="permit")
            row = acquire_overlay(
                NSORoutePolicyState,
                management=self.management,
                content_type=ContentType.objects.get_for_model(CommunityList),
                object_id=policy.pk,
                family="community_list",
                object_name=policy.name,
                status="accepted",
            )
        error = "Community 64512:42 is unsupported"
        row = mirror_update(row, last_apply_error=error)

        result = self._drain()

        self.assertEqual(result, (1, 0), "A public routing community in prior error must remain capturable")
        self.assertEqual(len(self.adapter.applied), 1)
        selection = NSOAcceptedSelection.objects.get(management=self.management, stream="route_policy")
        snapshot = NSOApplyOverlaySnapshot.objects.get(
            selection=selection, model_label=row._meta.label_lower, overlay_pk=row.pk
        )
        self.assertEqual(snapshot.prior_last_apply_error, error)
        self.assertEqual(snapshot.expected_last_apply_error, error)
        self.assertEqual(snapshot.contributors["rows"][0]["logical_ref"]["subject"], "route-policy.community-list")
        self.assertIsNotNone(selection.acknowledged_at)
