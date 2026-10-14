# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Freeze each emitted BGP owner before real delivery and manual Apply admission."""

import hashlib
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from uuid import UUID

from dcim.models import Device
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from ipam.models import ASN, RIR, VRF, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPPeerAddressFamily, BGPRouter, BGPScope, Redistribution

from netbox_nso_plugin import drain
from netbox_nso_plugin.models import (
    NSOAcceptedSelection,
    NSOApplyAttempt,
    NSOApplyOverlaySnapshot,
    NSOBGPPeerState,
    NSORedistributionState,
)
from netbox_nso_plugin.refusal_registry import owner_spec

from ._outbox_case import content_update, make_managed, mirror_update, without_commit_drain
from ._ownership_case import acquire_bgp_router, acquire_overlay
from ._settlement_adapter import LoopbackOnlySession
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_apply_selector import _ApplyContractAdapter, _promoted


class _CaptureHandler(BaseHTTPRequestHandler):
    """Serve the existing Apply contract double over a real socket."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _serve(self):
        adapter = self.server.adapter
        wire = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(wire) if wire else None
        kwargs = {"headers": dict(self.headers)}
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        kwargs["params"] = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if wire:
            kwargs["json" if self.command == "POST" else "data"] = body if self.command == "POST" else wire
        if self.command == "PUT" and parsed.path.endswith("/bgp-intent"):
            selector = int(self.headers["X-Push-Seq"])
            selections = list(
                NSOAcceptedSelection.objects.filter(
                    management_id=self.server.management_id, stream="bgp", source_push_seq=selector
                ).values()
            )
            snapshots = list(
                NSOApplyOverlaySnapshot.objects.filter(selection_id__in=[row["id"] for row in selections]).values()
            )
            adapter.capture_reads.append(
                {
                    "body": body,
                    "selector": selector,
                    "request_digest": hashlib.sha256(wire).hexdigest(),
                    "selections": selections,
                    "snapshots": snapshots,
                }
            )
        if self.command == "POST" and parsed.path.endswith("/actions/apply"):
            adapter.manual_reads.append(
                {
                    "body": body,
                    "snapshots": list(
                        NSOApplyOverlaySnapshot.objects.filter(attempt_id=UUID(body["apply_attempt_id"])).values()
                    ),
                }
            )
        try:
            response = adapter._handle(self.command, self.server.adapter_url + parsed.path, **kwargs)
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
        finally:
            connections.close_all()

    def do_GET(self):
        self._serve()

    def do_POST(self):
        self._serve()

    def do_PUT(self):
        self._serve()

    def do_DELETE(self):
        self._serve()


class TestDurableBGPCapture(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.device, self.management = make_managed("durable-bgp", 1558)
        self.management.manage_bgp = True
        self.management.save(update_fields=("manage_bgp",))
        self.client.force_login(get_user_model().objects.create_superuser(username="placeholder-bgp-operator"))
        self.adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        self.adapter.capture_reads = []
        self.adapter.manual_reads = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.server.adapter = self.adapter
        self.server.management_id = self.management.pk
        self.server.adapter_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_adapter)
        config = {
            "url": self.server.adapter_url,
            "token": "placeholder",
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 30,
        }
        for transport in (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=config),
            patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession),
        ):
            transport.start()
            self.addCleanup(transport.stop)
        self.stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        self.prior_attempt = NSOApplyAttempt.objects.create(management=self.management)
        with without_commit_drain(), transaction.atomic():
            rir = RIR.objects.create(name="Placeholder private ASNs", slug="placeholder-private-asns", is_private=True)
            self.asn = ASN.objects.create(asn=64512, rir=rir)
            self.remote_asn = ASN.objects.create(asn=64513, rir=rir)
            self.router = BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(Device),
                assigned_object_id=self.device.pk,
                asn=self.asn,
                router_id="198.18.0.1",
            )

    def _stop_adapter(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def scope(self, vrf="", families=("ipv4-unicast",)):
        with without_commit_drain(), transaction.atomic():
            scope = BGPScope.objects.create(router=self.router, vrf=VRF.objects.create(name=vrf) if vrf else None)
            for family in families:
                BGPAddressFamily.objects.create(scope=scope, address_family=family)
        return scope

    def peer(self, scope, address, *, status="accepted"):
        with without_commit_drain(), transaction.atomic():
            peer = BGPPeer.objects.create(
                scope=scope,
                peer=IPAddress.objects.create(address=f"{address}/32"),
                remote_as=self.remote_asn,
                password="placeholder-secret-canary",
                name=None,
            )
            for family in BGPAddressFamily.objects.filter(scope=scope):
                BGPPeerAddressFamily.objects.create(
                    assigned_object_type=ContentType.objects.get_for_model(BGPPeer),
                    assigned_object_id=peer.pk,
                    address_family=family,
                    enabled=True,
                )
            row = acquire_overlay(
                NSOBGPPeerState,
                management=self.management,
                bgp_peer=peer,
                asn_str="64512",
                vrf_name=scope.vrf.name if scope.vrf_id else "",
                peer_address_str=address,
                status="accepted",
            )
        return self.bookkeeping(row, status=status)

    def redistribution(self, scope, family="ipv4-unicast"):
        native_family = BGPAddressFamily.objects.get(scope=scope, address_family=family)
        with without_commit_drain(), transaction.atomic():
            native = Redistribution.objects.create(
                destination_type=ContentType.objects.get_for_model(BGPAddressFamily),
                destination_id=native_family.pk,
                source_protocol="static",
            )
            row = acquire_overlay(
                NSORedistributionState,
                management=self.management,
                redistribution=native,
                dest_protocol="bgp",
                dest_ref=f"64512/{scope.vrf.name if scope.vrf_id else ''}/{family}",
                source_protocol="static",
                source_ref="",
                status="accepted",
            )
        return self.bookkeeping(row)

    def bookkeeping(self, row, status="accepted"):
        return mirror_update(
            row,
            status=status,
            last_apply_at=self.stamp,
            last_apply_error=f"placeholder prior {row._meta.model_name} {row.pk}",
            apply_attempt_id=self.prior_attempt.pk,
        )

    def router_owner(self, status="accepted"):
        with without_commit_drain():
            row = acquire_bgp_router(self.management, self.router)
        return self.bookkeeping(row, status)

    def logical_ref(self, subject, path):
        ref = owner_spec(subject).logical_ref(subject, path)
        return {"family": "bgp", "subject": ref.subject, "key": ref.key}

    def assert_capture(self, capture, owners):
        snapshots = {(row["model_label"], row["overlay_pk"]): row for row in capture["snapshots"]}
        expected_owners = {(row._meta.label_lower, row.pk) for row in owners}
        self.assertEqual(set(snapshots), expected_owners, "Every emitted BGP owner needs a committed pre-send snapshot")
        self.assertEqual(len(capture["selections"]), 1)
        selection = capture["selections"][0]
        self.assertRegex(selection["mapping_epoch"], r"^[0-9a-f]{64}$")
        self.assertEqual(selection["source_push_seq"], capture["selector"])
        self.assertRegex(selection["request_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(selection["request_digest"], capture["request_digest"])
        manifest = {(row["model_label"], row["overlay_pk"]): row for row in selection["contributor_manifest"]}
        self.assertEqual(set(manifest), expected_owners)
        carrier = self.logical_ref("bgp.router", (("router", ("64512",)),))
        for owner in owners:
            snapshot = snapshots[(owner._meta.label_lower, owner.pk)]
            self.assertEqual(snapshot["mapping_epoch"], selection["mapping_epoch"])
            self.assertEqual(snapshot["prior_status"], owner.status)
            self.assertEqual(snapshot["prior_last_apply_at"], self.stamp)
            self.assertEqual(snapshot["prior_last_apply_error"], owner.last_apply_error)
            self.assertEqual(snapshot["prior_apply_attempt_id"], self.prior_attempt.pk)
            self.assertEqual(snapshot["accepted_at"], owner.accepted_at)
            self.assertRegex(snapshot["content_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                manifest[(owner._meta.label_lower, owner.pk)]["content_digest"], snapshot["content_digest"]
            )
            rows = snapshot["contributors"]["rows"]
            self.assertEqual(rows, manifest[(owner._meta.label_lower, owner.pk)]["contributors"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["carrier_ref"], rows[0]["logical_ref"] if isinstance(owner, NSORedistributionState) else carrier
            )
            self.assertEqual(rows[0]["stream"], "bgp")
            self.assertEqual(rows[0]["source_selector"], {"source_push_seq": capture["selector"]})
            self.assertRegex(rows[0]["contribution_digest"], r"^[0-9a-f]{64}$")
            self.assertIn("remote_acceptance_identity", rows[0])
        self.assertNotIn("placeholder-secret-canary", json.dumps(selection["contributor_manifest"]))
        self.assertNotIn("198.18.0.1", json.dumps(selection["contributor_manifest"]))
        return snapshots

    def test_bgp_capture_freezes_router_and_two_peer_owners(self):
        scope = self.scope()
        router = self.router_owner()
        first = self.peer(scope, "198.18.0.2")
        second = self.peer(scope, "198.18.0.3", status="in_sync")
        redistribution = self.redistribution(scope)

        drain.drain_intent_outbox()

        self.assertEqual(len(self.adapter.capture_reads), 1)
        capture = self.adapter.capture_reads[0]
        snapshots = self.assert_capture(capture, (router, first, second, redistribution))
        emitted = capture["body"]["routers"][0]
        self.assertEqual(len(emitted["scopes"][0]["peers"]), 2)
        self.assertEqual(
            emitted["scopes"][0]["address_families"][0]["redistribution"],
            [{"source_protocol": "static", "source_ref": ""}],
        )
        refs = {
            (router._meta.label_lower, router.pk): self.logical_ref("bgp.router", (("router", ("64512",)),)),
            (redistribution._meta.label_lower, redistribution.pk): self.logical_ref(
                "bgp.router.scope.address-family.redistribute",
                (
                    ("router", ("64512",)),
                    ("scope", ("",)),
                    ("address-family", ("ipv4-unicast",)),
                    ("redistribute", ("static", "")),
                ),
            ),
        }
        for peer in (first, second):
            refs[(peer._meta.label_lower, peer.pk)] = self.logical_ref(
                "bgp.router.scope.peer", (("router", ("64512",)), ("scope", ("",)), ("peer", (peer.peer_address_str,)))
            )
        for key, ref in refs.items():
            self.assertEqual(snapshots[key]["contributors"]["rows"][0]["logical_ref"], ref)
        unchanged = snapshots[(second._meta.label_lower, second.pk)]
        self.assertEqual(unchanged["expected_status"], "in_sync")
        self.assertEqual(unchanged["expected_last_apply_at"], self.stamp)
        self.assertEqual(unchanged["expected_last_apply_error"], second.last_apply_error)
        self.assertEqual(unchanged["expected_apply_attempt_id"], self.prior_attempt.pk)

    def test_router_snapshot_includes_emitted_scope_af_membership(self):
        scope = self.scope()
        blue = self.scope("placeholder-blue", ("ipv6-unicast",))
        red = self.scope("placeholder-red")
        router = self.router_owner(status="in_sync")
        peer = self.peer(scope, "198.18.0.2")
        redistribution = self.redistribution(blue, "ipv6-unicast")

        drain.drain_intent_outbox()

        first = self.adapter.capture_reads[0]
        snapshots = self.assert_capture(first, (router, peer, redistribution))
        frozen = snapshots[(router._meta.label_lower, router.pk)]
        self.assertEqual(
            [
                (row["vrf"], [family["af"] for family in row["address_families"]])
                for row in first["body"]["routers"][0]["scopes"]
            ],
            [("", ["ipv4-unicast"]), ("placeholder-blue", ["ipv6-unicast"]), ("placeholder-red", ["ipv4-unicast"])],
        )
        family = BGPAddressFamily.objects.get(scope=red)
        content_update(family, address_family="ipv6-unicast")
        drain.drain_intent_outbox()
        later = self.adapter.capture_reads[-1]
        self.assertNotEqual(later["selector"], first["selector"])
        later_router = next(row for row in later["snapshots"] if row["model_label"] == router._meta.label_lower)
        self.assertNotEqual(later_router["content_digest"], frozen["content_digest"])
        self.assertEqual(NSOApplyOverlaySnapshot.objects.get(pk=frozen["id"]).content_digest, frozen["content_digest"])
        self.assertEqual(
            later["body"]["routers"][0]["scopes"][-1]["address_families"],
            [{"af": "ipv6-unicast", "redistribution": []}],
        )

    def test_router_only_selection_has_durable_snapshot(self):
        self.scope()
        router = self.router_owner(status="in_sync")

        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=(self.management.pk, "apply")),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(len(self.adapter.manual_reads), 1)
        admission = self.adapter.manual_reads[0]
        self.assertEqual(
            {(row["model_label"], row["overlay_pk"]) for row in admission["snapshots"]},
            {(router._meta.label_lower, router.pk)},
            "Router-only Apply must commit its unchanged owner snapshot before adapter admission",
        )
        snapshot = admission["snapshots"][0]
        self.assertEqual(snapshot["prior_status"], snapshot["expected_status"])
        self.assertEqual(snapshot["prior_status"], "in_sync")
        for field, expected in (
            ("last_apply_at", self.stamp),
            ("last_apply_error", router.last_apply_error),
            ("apply_attempt_id", self.prior_attempt.pk),
        ):
            self.assertEqual(snapshot[f"prior_{field}"], expected)
            self.assertEqual(snapshot[f"expected_{field}"], expected)
        self.assertRegex(snapshot["content_digest"], r"^[0-9a-f]{64}$")
        contributor = snapshot["contributors"]["rows"][0]
        ref = self.logical_ref("bgp.router", (("router", ("64512",)),))
        self.assertEqual(contributor["logical_ref"], ref)
        self.assertEqual(contributor["carrier_ref"], ref)
        self.assertEqual(contributor["source_selector"], {"source_push_seq": admission["body"]["selected"]["bgp"]})
        self.assertFalse(NSOBGPPeerState.objects.filter(management=self.management).exists())
        self.assertFalse(NSORedistributionState.objects.filter(management=self.management).exists())
