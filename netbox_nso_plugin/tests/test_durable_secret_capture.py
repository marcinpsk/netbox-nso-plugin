# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Capture prior errors without retaining emitted credentials."""

import json

from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models.signals import post_save
from django.test import TransactionTestCase
from django.urls import reverse
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

from netbox_nso_plugin import drain, durable_capture
from netbox_nso_plugin.models import (
    NSOAcceptedSelection,
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSOBGPPeerState,
    NSOInterfaceState,
    NSOISISInstanceState,
    NSORoutePolicyState,
)

from ._outbox_case import ReceiptAdapter, make_managed, mirror_update, without_commit_drain
from ._ownership_case import acquire_bgp_router, acquire_overlay
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_durable_manual_capture import ManualAdmissionAdapter


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

    def _assert_capture_redacted(self, row, secret):
        result = self._drain()
        self.assertEqual(result, (1, 0), "A credential in prior error must not block delivery")
        self.assertEqual(len(self.adapter.applied), 1)
        snapshot = NSOApplyOverlaySnapshot.objects.get(
            attempt__management=self.management, model_label=row._meta.label_lower, overlay_pk=row.pk
        )
        error = "placeholder prior error contains [redacted]"
        self.assertEqual(snapshot.prior_last_apply_error, error)
        self.assertEqual(snapshot.expected_last_apply_error, error)
        self.assertNotIn(secret, snapshot.prior_last_apply_error)
        self.assertNotIn(secret, snapshot.expected_last_apply_error)
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertEqual(row.last_apply_error, error)
        self.assertNotIn(secret, row.last_apply_error)

    def test_isis_area_auth_key_in_prior_error_is_redacted(self):
        secret = "placeholder-area-secret"
        row = self._isis("area_auth_key", secret)

        self._assert_capture_redacted(row, secret)

    def test_isis_domain_auth_key_in_prior_error_is_redacted(self):
        secret = "placeholder-domain-secret"
        row = self._isis("domain_auth_key", secret)

        self._assert_capture_redacted(row, secret)

    def test_escaped_bgp_password_in_prior_error_is_redacted(self):
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

        self._assert_capture_redacted(row, secret)

    def test_manual_capture_redacts_a_prior_secret_before_promotion(self):
        secret = 'placeholder"\\manual-secret'
        row = self._isis("area_auth_key", secret)
        row = mirror_update(row, last_apply_error="")
        user = get_user_model().objects.create_superuser(
            username="secretmanualadmin", password=None, email="manual@test.example"
        )
        self.client.force_login(user)

        def prior_error_before_capture(sender, instance, created, **kwargs):
            if created and instance.trigger_kind == "manual" and instance.management_id == self.management.pk:
                mirror_update(row, last_apply_error=f"placeholder prior error contains {secret}")

        post_save.connect(prior_error_before_capture, sender=NSOApplyAttempt, weak=False)
        self.addCleanup(post_save.disconnect, prior_error_before_capture, sender=NSOApplyAttempt)
        promoted = []

        def no_op(body):
            row.refresh_from_db()
            promoted.append(row.status)
            return 200, {
                "device_id": self.management.adapter_device_id,
                "outcome": "no_op",
                "job_id": None,
                "selected": body["selected"],
                "skipped": dict.fromkeys(body["selected"], "already_applied"),
                "skipped_detail": None,
                "generations": [],
            }

        adapter = ManualAdmissionAdapter(no_op)
        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.management.pk, "apply"])
        config, session = adapter.patches()
        with config, session:
            response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(promoted, ["deploying"])
        attempt = NSOApplyAttempt.objects.get(management=self.management, trigger_kind="manual")
        self.assertEqual(attempt.adapter_apply_attempt_id, attempt.pk)
        snapshot = attempt.overlay_snapshots.get(model_label=row._meta.label_lower, overlay_pk=row.pk)
        self.assertEqual(snapshot.prior_last_apply_error, "placeholder prior error contains [redacted]")
        self.assertEqual(snapshot.expected_last_apply_error, "")
        self.assertNotIn(secret, snapshot.prior_last_apply_error)
        self.assertNotIn(secret, snapshot.expected_last_apply_error)
        self.assertEqual(snapshot.settlement_disposition, "not_applied")
        row.refresh_from_db()
        self.assertEqual(row.status, "accepted")
        self.assertEqual(row.last_apply_error, snapshot.prior_last_apply_error)

    def test_snapshot_redacts_nested_json_error_values(self):
        secret = 'placeholder"\\json-secret'
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        with without_commit_drain(), transaction.atomic():
            self.management.manage_description = True
            self.management.save(update_fields=("manage_description",))
            interface = Interface.objects.create(
                device=self.device, name="Ethernet17", type="1000base-t", description="Desired description"
            )
            row = acquire_overlay(NSOInterfaceState, interface=interface, attribute="description", status="accepted")
        self.assertEqual(self._drain(), (1, 0))
        selection = NSOAcceptedSelection.objects.get(management=self.management, stream="interface_config")
        row = mirror_update(
            row,
            last_apply_error={
                "message": f"Prior error contains {secret}",
                "detail": [escaped, {"repeated": f"{secret} and {secret}", "count": 2, "retry": False}, None],
            },
        )
        with transaction.atomic():
            attempt = NSOApplyAttempt.objects.create(
                management=self.management, adapter_device_id=self.management.adapter_device_id
            )
            snapshot = durable_capture._snapshot(
                attempt, selection, selection.contributor_manifest[0], row, {}, secrets=("json-secret", secret, escaped)
            )
        error = {
            "message": "Prior error contains [redacted]",
            "detail": ["[redacted]", {"repeated": "[redacted] and [redacted]", "count": 2, "retry": False}, None],
        }
        self.assertEqual(snapshot.prior_last_apply_error, error)
        self.assertEqual(snapshot.expected_last_apply_error, error)
        row.refresh_from_db()
        self.assertEqual(row.last_apply_error, error)

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
