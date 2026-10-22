# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Share BGP owners and PostgreSQL blocking checks between transaction tests."""

from time import monotonic, sleep
from uuid import uuid4

from dcim.models import Device
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from ipam.models import ASN, RIR, IPAddress
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPRouter, BGPScope

from netbox_nso_plugin.models import NSOBGPPeerState, NSOBGPRouterState

from ._outbox_case import mirror_update
from ._ownership_case import acquire_overlay


class LifecycleFenceHelpers:
    def bgp_owners(self):
        asn = ASN.objects.create(asn=64512, rir=RIR.objects.create(name="Private", slug="private"))
        router = BGPRouter.objects.create(
            assigned_object_type=ContentType.objects.get_for_model(Device),
            assigned_object_id=self.device.pk,
            asn=asn,
            name="placeholder-router",
            router_id="198.18.0.1",
        )
        scope = BGPScope.objects.create(router=router)
        BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
        parent = acquire_overlay(
            NSOBGPRouterState, management=self.management, bgp_router=router, asn_str="64512", status="accepted"
        )
        peers = []
        for address in ("198.18.0.2", "198.18.0.3"):
            native = BGPPeer.objects.create(
                scope=scope, peer=IPAddress.objects.create(address=address + "/32"), name=None
            )
            peers.append(
                acquire_overlay(
                    NSOBGPPeerState,
                    management=self.management,
                    bgp_peer=native,
                    asn_str="64512",
                    vrf_name="",
                    peer_address_str=address,
                    status="accepted",
                )
            )
        attempt_id = uuid4()
        for row in (parent, *peers):
            mirror_update(row, status="deploying", apply_attempt_id=attempt_id)
        return parent, peers

    def _wait_for_blocking(self, backends, pending):
        deadline = monotonic() + 15
        while True:
            if pending.done():
                pending.result()
                self.fail("second writer completed without waiting for the first writer's PostgreSQL lock")
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [backends[0], backends[1]])
                blocked = cursor.fetchone()[0]
            if blocked:
                return
            if monotonic() >= deadline:
                self.fail("second writer never waited for the first writer's PostgreSQL lock")
            sleep(0.01)
